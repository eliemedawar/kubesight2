"""vCenter inventory for provisioning: where VMs can go, and what to clone.

vCenter's REST Automation API cannot list VM templates kept in folders — it
only knows Content Library items — so this talks to the vSphere Web Services
API through pyvmomi (imported lazily, like paramiko: tests never need it).
Everything comes back with its managed object id, because the generated
OpenTofu configuration refers to objects by id rather than by name: a renamed
datastore or a template moved to another folder must not turn into a failed
plan, or worse, a destroy that cannot be planned.

Two seams: ``set_placement_fetcher`` and ``set_privilege_checker`` replace the
vCenter calls in tests; ``simulation_enabled()`` swaps in a demo inventory for
local walk-throughs (opt-in through KUBESIGHT_PROVISIONING_SIMULATE).
"""

from __future__ import annotations

import logging
import os
import re
import ssl
import time
from typing import Any, Callable, Dict, List, Optional, Tuple
from urllib.parse import urlsplit

from ...vsphere_client import VSphereConfig, VSphereError
from ....ttl_cache import TTLCache

logger = logging.getLogger(__name__)

_PLACEMENT_TTL_SECONDS = 120
_placement_cache = TTLCache("vsphere-placement")

_placement_fetcher: Optional[Callable[[VSphereConfig], Dict[str, Any]]] = None
_privilege_checker: Optional[Callable[..., List[Dict[str, Any]]]] = None


def set_placement_fetcher(fetcher) -> None:
    global _placement_fetcher
    _placement_fetcher = fetcher
    _placement_cache.invalidate()


def set_privilege_checker(checker) -> None:
    global _privilege_checker
    _privilege_checker = checker


def simulation_enabled() -> bool:
    return os.getenv("KUBESIGHT_PROVISIONING_SIMULATE", "").strip().lower() in {
        "1", "true", "yes", "on",
    }


# ---------------------------------------------------------------------------
# What the provisioning account must be allowed to do
# ---------------------------------------------------------------------------

# (privilege id, what it is for, where it is checked)
REQUIRED_PRIVILEGES: List[Tuple[str, str, str]] = [
    ("Folder.Create", "Create the cluster's VM folder", "folder"),
    ("Folder.Delete", "Remove that folder when the cluster is destroyed", "folder"),
    ("VirtualMachine.Inventory.CreateFromExisting", "Clone from the template", "folder"),
    ("VirtualMachine.Inventory.Create", "Create virtual machines", "folder"),
    ("VirtualMachine.Inventory.Delete", "Delete virtual machines", "folder"),
    ("VirtualMachine.Provisioning.DeployTemplate", "Deploy the VM template", "template"),
    ("VirtualMachine.Provisioning.Customize", "Set hostname and address in the guest", "template"),
    ("VirtualMachine.Provisioning.ReadCustSpecs", "Read customization specifications", "datacenter"),
    ("VirtualMachine.Config.CPUCount", "Set the CPU count", "folder"),
    ("VirtualMachine.Config.Memory", "Set memory", "folder"),
    ("VirtualMachine.Config.Settings", "Change VM settings", "folder"),
    ("VirtualMachine.Config.DiskExtend", "Grow the cloned disk", "folder"),
    ("VirtualMachine.Config.AddNewDisk", "Add disks", "folder"),
    ("VirtualMachine.Config.EditDevice", "Change the network card", "folder"),
    ("VirtualMachine.Config.AdvancedConfig", "Write guest settings", "folder"),
    ("VirtualMachine.Interact.PowerOn", "Power the VM on", "folder"),
    ("VirtualMachine.Interact.PowerOff", "Power the VM off before deleting it", "folder"),
    ("Resource.AssignVMToPool", "Place VMs in the resource pool", "pool"),
    ("Datastore.AllocateSpace", "Allocate disk space", "datastore"),
    ("Datastore.Browse", "Browse the datastore", "datastore"),
    ("Datastore.FileManagement", "Manage VM files", "datastore"),
    ("Network.Assign", "Connect VMs to the network", "network"),
    ("Host.Inventory.EditCluster", "Create keep-apart (DRS) rules", "cluster"),
]


# ---------------------------------------------------------------------------
# Template compatibility — pure, so it is tested without a vCenter
# ---------------------------------------------------------------------------

_DEBIAN_GUESTS = ("ubuntu", "debian")
_RHEL_GUESTS = ("rhel", "rocky", "alma", "centos", "oracle")
_VALIDATED = {"debian": ("22.04", "24.04"), "rhel": ("9",)}


def _family(guest_id: str) -> Optional[str]:
    guest_id = (guest_id or "").lower()
    if guest_id.startswith("windows") or guest_id.startswith("win"):
        return "windows"
    if guest_id.startswith(_DEBIAN_GUESTS):
        return "debian"
    if guest_id.startswith(_RHEL_GUESTS):
        return "rhel"
    return None


def assess_template(template: Dict[str, Any]) -> Dict[str, Any]:
    """Whether KubeSight can build a node from this template, and why not.

    vCenter can tell us the guest OS family, whether VMware Tools is in the
    image, its disks and network cards. It cannot tell us whether cloud-init or
    perl is inside, which guest customization also needs — that is said, not
    guessed.
    """
    checks: List[Dict[str, str]] = []

    def add(status: str, label: str, detail: str) -> None:
        checks.append({"status": status, "label": label, "detail": detail})

    full_name = template.get("guestDetail") or template.get("guestFullName") or ""
    family = _family(template.get("guestId", ""))
    if family == "windows":
        add("bad", "Guest OS", f"{full_name or 'Windows'} — Kubernetes nodes here run Linux")
    elif family is None:
        add("warn", "Guest OS", f"{full_name or template.get('guestId') or 'Unknown'} — "
            "not a family KubeSight has validated; preflight decides")
    else:
        version = None
        # "Ubuntu Linux (64-bit)" names the architecture, not a release.
        match = re.search(r"(\d+(?:\.\d+)?)", re.sub(r"\(\d+-bit\)", "", full_name or ""))
        if match:
            version = match.group(1)
        validated = _VALIDATED[family]
        if version and not any(version.startswith(v) for v in validated):
            add("warn", "Guest OS", f"{full_name} — validated: {', '.join(validated)}")
        else:
            add("ok", "Guest OS", f"{full_name or template.get('guestId')} — supported")

    tools_version = template.get("toolsVersion") or 0
    if not tools_version:
        add("bad", "VMware Tools", "not installed: vSphere cannot set the hostname or address")
    else:
        add("ok", "VMware Tools", f"installed (version {tools_version})")

    nics = template.get("nics") or []
    if not nics:
        add("bad", "Network card", "none: the VM would have no network")
    elif len(nics) > 1:
        add("warn", "Network cards", f"{len(nics)} cards; only the first is connected and configured")
    else:
        add("ok", "Network card", f"1 × {nics[0].get('type') or 'adapter'}")

    disks = template.get("disks") or []
    if not disks:
        add("bad", "Disk", "no disk to clone")
    else:
        first = disks[0]
        detail = f"{first.get('sizeGb')} GB, grows to the size you choose"
        if len(disks) > 1:
            add("warn", "Disks", f"{len(disks)} disks; only the first grows, the rest keep their size")
        else:
            add("ok", "Disk", detail)

    add("info", "Network setup inside the image",
        "Guest customization also needs cloud-init or perl in the template, "
        "which vCenter cannot see. The first VM proves it.")

    status = "bad" if any(c["status"] == "bad" for c in checks) else (
        "warn" if any(c["status"] == "warn" for c in checks) else "ok"
    )
    return {"status": status, "checks": checks}


# ---------------------------------------------------------------------------
# pyvmomi
# ---------------------------------------------------------------------------

def _host_port(cfg: VSphereConfig) -> Tuple[str, int]:
    parts = urlsplit(cfg.root)
    return parts.hostname or "", parts.port or 443


def _ssl_context(cfg: VSphereConfig) -> ssl.SSLContext:
    ctx = ssl.create_default_context()
    if cfg.skip_tls_verify:
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
    elif cfg.ca_pem:
        ctx.load_verify_locations(cadata=cfg.ca_pem)
    return ctx


def _connect(cfg: VSphereConfig):
    try:
        from pyVim.connect import SmartConnect
        from pyVmomi import vim
    except ImportError as exc:  # pragma: no cover - deployment problem
        raise VSphereError(
            "pyvmomi is not installed in this KubeSight image; VM templates "
            "cannot be listed."
        ) from exc
    host, port = _host_port(cfg)
    try:
        return SmartConnect(
            host=host, port=port, user=cfg.username, pwd=cfg.password,
            sslContext=_ssl_context(cfg), connectionPoolTimeout=30,
        )
    except vim.fault.InvalidLogin as exc:
        raise VSphereError("vCenter rejected the credentials.", status=401) from exc
    except Exception as exc:  # noqa: BLE001 — socket, TLS and SOAP faults alike
        raise VSphereError(f"vCenter unreachable: {exc}") from exc


def _disconnect(si) -> None:
    try:
        from pyVim.connect import Disconnect

        Disconnect(si)
    except Exception:  # noqa: BLE001 — best-effort logout
        pass


def _collect(content, view_type, paths: List[str]) -> List[Tuple[Any, Dict[str, Any]]]:
    """Every object of ``view_type`` with the given properties, in pages."""
    from pyVmomi import vim, vmodl

    view = content.viewManager.CreateContainerView(content.rootFolder, [view_type], True)
    try:
        pc = vmodl.query.PropertyCollector
        traversal = pc.TraversalSpec(
            name="traverseView", path="view", skip=False, type=vim.view.ContainerView
        )
        spec = pc.FilterSpec(
            objectSet=[pc.ObjectSpec(obj=view, skip=True, selectSet=[traversal])],
            propSet=[pc.PropertySpec(type=view_type, pathSet=paths, all=False)],
        )
        return _retrieve(content, spec)
    finally:
        try:
            view.Destroy()
        except Exception:  # noqa: BLE001
            pass


def _retrieve(content, spec) -> List[Tuple[Any, Dict[str, Any]]]:
    from pyVmomi import vmodl

    collector = content.propertyCollector
    options = vmodl.query.PropertyCollector.RetrieveOptions(maxObjects=500)
    result = collector.RetrievePropertiesEx([spec], options)
    out: List[Tuple[Any, Dict[str, Any]]] = []
    while result is not None:
        for item in result.objects or []:
            out.append((item.obj, {prop.name: prop.val for prop in (item.propSet or [])}))
        if not result.token:
            break
        result = collector.ContinueRetrievePropertiesEx(result.token)
    return out


def _moid(obj) -> Optional[str]:
    return getattr(obj, "_moId", None) if obj is not None else None


_SCSI_TYPES = {
    "ParaVirtualSCSIController": "pvscsi",
    "VirtualLsiLogicSASController": "lsilogic-sas",
    "VirtualLsiLogicController": "lsilogic",
    "VirtualBusLogicController": "buslogic",
}
_NIC_TYPES = {
    "VirtualVmxnet3": "vmxnet3",
    "VirtualE1000e": "e1000e",
    "VirtualE1000": "e1000",
    "VirtualVmxnet2": "vmxnet2",
}


def _template_hardware(devices) -> Dict[str, Any]:
    disks, nics, scsi = [], [], None
    for device in devices or []:
        kind = type(device).__name__.split(".")[-1]
        if kind == "VirtualDisk":
            backing = getattr(device, "backing", None)
            disks.append({
                "unit": getattr(device, "unitNumber", len(disks)),
                "sizeGb": round((getattr(device, "capacityInKB", 0) or 0) / 1024 / 1024),
                "thin": bool(getattr(backing, "thinProvisioned", False)),
                "eagerlyScrub": bool(getattr(backing, "eagerlyScrub", False)),
            })
        elif kind in _NIC_TYPES:
            nics.append({"type": _NIC_TYPES[kind]})
        elif kind in _SCSI_TYPES and scsi is None:
            scsi = _SCSI_TYPES[kind]
    disks.sort(key=lambda d: d["unit"])
    return {"disks": disks, "nics": nics, "scsiType": scsi or "pvscsi"}


def _fetch_placement(cfg: VSphereConfig) -> Dict[str, Any]:
    from pyVmomi import vim

    si = _connect(cfg)
    try:
        content = si.RetrieveContent()
        datacenters = _collect(content, vim.Datacenter, ["name", "parent", "vmFolder", "hostFolder"])
        folders = _collect(content, vim.Folder, ["name", "parent"])
        clusters = _collect(
            content, vim.ClusterComputeResource,
            ["name", "parent", "host", "resourcePool", "configurationEx"],
        )
        pools = _collect(content, vim.ResourcePool, ["name", "parent", "owner"])
        datastores = _collect(
            content, vim.Datastore,
            ["name", "parent", "summary.capacity", "summary.freeSpace",
             "summary.accessible", "summary.type"],
        )
        networks = _collect(content, vim.Network, ["name", "parent"])
        vms = _collect(content, vim.VirtualMachine, ["name", "parent", "config.template"])

        names: Dict[str, str] = {}
        parents: Dict[str, Optional[str]] = {}
        for obj, props in datacenters + folders + clusters + pools + datastores + networks + vms:
            names[_moid(obj)] = props.get("name", "")
            parents[_moid(obj)] = _moid(props.get("parent"))
        dc_ids = {_moid(obj) for obj, _ in datacenters}

        def datacenter_of(moid: Optional[str]) -> Optional[str]:
            seen = 0
            while moid and seen < 64:
                if moid in dc_ids:
                    return moid
                moid = parents.get(moid)
                seen += 1
            return None

        def path_below(moid: Optional[str], stop: Optional[str]) -> str:
            parts = []
            seen = 0
            while moid and moid != stop and seen < 64:
                parts.append(names.get(moid, ""))
                moid = parents.get(moid)
                seen += 1
            return "/".join(reversed(parts))

        templates_raw = [(obj, props) for obj, props in vms if props.get("config.template")]
        details: Dict[str, Dict[str, Any]] = {}
        if templates_raw:
            from pyVmomi import vmodl

            pc = vmodl.query.PropertyCollector
            spec = pc.FilterSpec(
                objectSet=[pc.ObjectSpec(obj=obj, skip=False) for obj, _ in templates_raw],
                propSet=[pc.PropertySpec(
                    type=vim.VirtualMachine,
                    pathSet=[
                        "config.uuid", "config.guestId", "config.guestFullName",
                        "config.firmware", "config.tools.toolsVersion",
                        "config.hardware.numCPU", "config.hardware.memoryMB",
                        "config.hardware.device", "guest.guestFullName",
                    ],
                    all=False,
                )],
            )
            for obj, props in _retrieve(content, spec):
                details[_moid(obj)] = props

        out: List[Dict[str, Any]] = []
        for dc_obj, dc_props in datacenters:
            dc_id = _moid(dc_obj)
            vm_folder = _moid(dc_props.get("vmFolder"))
            host_folder = _moid(dc_props.get("hostFolder"))
            entry: Dict[str, Any] = {
                "id": dc_id, "name": dc_props.get("name", ""),
                "clusters": [], "folders": [], "datastores": [],
                "networks": [], "templates": [], "vmNames": [],
            }
            for obj, props in clusters:
                if datacenter_of(_moid(obj)) != dc_id:
                    continue
                cluster_id = _moid(obj)
                root_pool = _moid(props.get("resourcePool"))
                drs = getattr(getattr(props.get("configurationEx"), "drsConfig", None), "enabled", None)
                pool_entries = []
                for pool_obj, pool_props in pools:
                    pool_id = _moid(pool_obj)
                    if pool_id == root_pool or _moid(pool_props.get("owner")) != cluster_id:
                        continue
                    pool_entries.append({
                        "id": pool_id,
                        "name": pool_props.get("name", ""),
                        "path": path_below(pool_id, root_pool),
                    })
                entry["clusters"].append({
                    "id": cluster_id,
                    "name": props.get("name", ""),
                    "path": path_below(cluster_id, host_folder),
                    "hostCount": len(props.get("host") or []),
                    "drsEnabled": bool(drs),
                    "rootResourcePoolId": root_pool,
                    "resourcePools": sorted(pool_entries, key=lambda p: p["path"]),
                })
            for obj, props in folders:
                folder_id = _moid(obj)
                if folder_id == vm_folder or datacenter_of(folder_id) != dc_id:
                    continue
                path = path_below(folder_id, vm_folder)
                # Only folders under this datacenter's VM folder hold VMs.
                walker, inside = folder_id, False
                for _ in range(64):
                    walker = parents.get(walker)
                    if walker == vm_folder:
                        inside = True
                        break
                    if walker is None or walker == dc_id:
                        break
                if inside:
                    entry["folders"].append({"id": folder_id, "path": path})
            for obj, props in datastores:
                if datacenter_of(_moid(obj)) != dc_id:
                    continue
                entry["datastores"].append({
                    "id": _moid(obj),
                    "name": props.get("name", ""),
                    "capacityGb": round((props.get("summary.capacity") or 0) / 1024 ** 3),
                    "freeGb": round((props.get("summary.freeSpace") or 0) / 1024 ** 3),
                    "accessible": bool(props.get("summary.accessible", True)),
                    "type": props.get("summary.type"),
                })
            for obj, props in networks:
                if datacenter_of(_moid(obj)) != dc_id:
                    continue
                name = props.get("name", "")
                if "DVUplinks" in name:
                    continue
                kind = type(obj).__name__.split(".")[-1]
                entry["networks"].append({
                    "id": _moid(obj),
                    "name": name,
                    "kind": "distributed" if "Portgroup" in kind else "standard",
                })
            for obj, props in vms:
                vm_id = _moid(obj)
                if datacenter_of(vm_id) != dc_id:
                    continue
                entry["vmNames"].append(props.get("name", ""))
                if not props.get("config.template"):
                    continue
                info = details.get(vm_id, {})
                hardware = _template_hardware(info.get("config.hardware.device"))
                template = {
                    "id": vm_id,
                    "name": props.get("name", ""),
                    "path": path_below(vm_id, vm_folder),
                    "uuid": info.get("config.uuid"),
                    "guestId": info.get("config.guestId"),
                    "guestFullName": info.get("config.guestFullName"),
                    "guestDetail": info.get("guest.guestFullName"),
                    "firmware": info.get("config.firmware") or "bios",
                    "toolsVersion": info.get("config.tools.toolsVersion") or 0,
                    "cpu": info.get("config.hardware.numCPU"),
                    "memoryMb": info.get("config.hardware.memoryMB"),
                    **hardware,
                }
                template["compatibility"] = assess_template(template)
                entry["templates"].append(template)
            for key, sort_key in (("clusters", "path"), ("folders", "path"),
                                  ("datastores", "name"), ("networks", "name"),
                                  ("templates", "path")):
                entry[key].sort(key=lambda item, k=sort_key: str(item.get(k) or "").lower())
            entry["vmNames"].sort()
            out.append(entry)
        out.sort(key=lambda item: item["name"].lower())
        return {"datacenters": out, "fetchedAt": time.time()}
    finally:
        _disconnect(si)


# vCenter's built-in Administrator role. It holds every privilege, including
# ones added after the role list was read.
_ADMIN_ROLE_ID = -1


def privileges_from_roles(role_ids, role_list) -> Optional[set]:
    """Privileges held through ``role_ids`` (an entity's ``effectiveRole``), or
    None when one of them is the Administrator role (everything)."""
    wanted = set(role_ids or [])
    if _ADMIN_ROLE_ID in wanted:
        return None
    held: set = set()
    for role in role_list or []:
        if role.roleId in wanted:
            held.update(role.privilege or [])
    return held


def no_permission_detail(exc: Exception) -> str:
    """What vCenter said was missing, from a vim.fault.NoPermission."""
    privilege = getattr(exc, "privilegeId", None)
    target = getattr(exc, "object", None)
    target_id = getattr(target, "_moId", None) or (str(target) if target is not None else "")
    if privilege and target_id:
        return f"{privilege} on {target_id}"
    return privilege or target_id or ""


def _check_privileges(cfg: VSphereConfig, entity_ids: Dict[str, str]) -> List[Dict[str, Any]]:
    """Which REQUIRED_PRIVILEGES this session holds on the given entities.

    ``entity_ids`` maps a scope ("folder", "datastore", ...) to a moid. A scope
    with no entity is checked on the datacenter, which can under-report a role
    granted on a narrower folder — the result says where each was checked.
    """
    from pyVmomi import vim

    si = _connect(cfg)
    try:
        content = si.RetrieveContent()
        session_key = content.sessionManager.currentSession.key
        manager = content.authorizationManager
        stubs: Dict[str, Any] = {}
        kinds = {
            "folder": vim.Folder, "datacenter": vim.Datacenter, "template": vim.VirtualMachine,
            "pool": vim.ResourcePool, "datastore": vim.Datastore, "network": vim.Network,
            "cluster": vim.ClusterComputeResource,
        }
        fallback = entity_ids.get("datacenter")
        results: List[Dict[str, Any]] = []
        by_entity: Dict[str, List[Tuple[str, str]]] = {}
        for privilege, purpose, scope in REQUIRED_PRIVILEGES:
            moid = entity_ids.get(scope) or fallback
            if not moid:
                continue
            kind = kinds[scope] if entity_ids.get(scope) else vim.Datacenter
            stubs[moid] = kind(moid, si._stub)
            by_entity.setdefault(moid, []).append((privilege, purpose))
        # HasPrivilegeOnEntities needs System.View where vCenter checks it (the
        # root), which an account whose role is granted only on a datacenter,
        # cluster or folder does not have. Then the privileges are worked out
        # from the roles the account holds on each entity instead, which needs
        # nothing beyond reading that entity.
        denied: Optional[Exception] = None
        role_list = None
        for moid, wanted in by_entity.items():
            granted: Dict[str, bool] = {}
            if denied is None:
                try:
                    answer = manager.HasPrivilegeOnEntities(
                        entity=[stubs[moid]], sessionId=session_key,
                        privId=[privilege for privilege, _ in wanted],
                    )
                    for entity_privilege in answer or []:
                        for availability in entity_privilege.privAvailability or []:
                            granted[availability.privId] = bool(availability.isGranted)
                except vim.fault.NoPermission as exc:
                    denied = exc
            if denied is not None:
                try:
                    if role_list is None:
                        role_list = list(manager.roleList or [])
                    held = privileges_from_roles(stubs[moid].effectiveRole, role_list)
                except vim.fault.NoPermission as exc:
                    raise VSphereError(
                        "vCenter will not tell the provisioning account which privileges it holds "
                        f"(missing {no_permission_detail(denied) or 'System.View'}; reading its roles "
                        f"was refused too: {no_permission_detail(exc) or 'no detail'}). Give the "
                        "account the Read-only role at the top of the vCenter (no need to "
                        "propagate), then check again."
                    ) from exc
                granted = {
                    privilege: held is None or privilege in held for privilege, _ in wanted
                }
            for privilege, purpose in wanted:
                results.append({
                    "privilege": privilege, "purpose": purpose,
                    "entity": moid, "granted": granted.get(privilege, False),
                })
        return results
    finally:
        _disconnect(si)


# ---------------------------------------------------------------------------
# Public entry points
# ---------------------------------------------------------------------------

def _as_vsphere_error(what: str, exc: Exception) -> VSphereError:
    """Anything a vCenter read raises after login — a SOAP fault such as
    NoPermission on one object, a dropped session, or an object shaped in a way
    this code did not expect — as a message a person can act on, instead of a
    bare 500. The traceback goes to the log."""
    logger.exception("vCenter %s failed", what)
    name = type(exc).__name__
    detail = str(getattr(exc, "msg", "") or exc).strip()
    missing = no_permission_detail(exc)
    if missing:
        detail = f"{detail} (missing {missing})"
    return VSphereError(f"vCenter {what} failed ({name}){': ' + detail[:500] if detail else ''}")


def placement(cfg: VSphereConfig, *, cache_key: str, force_refresh: bool = False) -> Dict[str, Any]:
    fetch = _placement_fetcher or (demo_placement if simulation_enabled() else _fetch_placement)
    if force_refresh:
        _placement_cache.invalidate(cache_key)

    def read() -> Dict[str, Any]:
        try:
            return fetch(cfg)
        except VSphereError:
            raise
        except Exception as exc:  # noqa: BLE001 — see _as_vsphere_error
            raise _as_vsphere_error("inventory read", exc) from exc

    return _placement_cache.get_or_compute(cache_key, _PLACEMENT_TTL_SECONDS, read)


def invalidate(cache_key: Optional[str] = None) -> None:
    if cache_key:
        _placement_cache.invalidate(cache_key)
    else:
        _placement_cache.invalidate()


def check_privileges(cfg: VSphereConfig, entity_ids: Dict[str, str]) -> List[Dict[str, Any]]:
    if _privilege_checker is not None:
        return _privilege_checker(cfg, entity_ids)
    if simulation_enabled():
        return [
            {"privilege": p, "purpose": purpose, "entity": entity_ids.get(scope) or "datacenter",
             "granted": True}
            for p, purpose, scope in REQUIRED_PRIVILEGES
        ]
    try:
        return _check_privileges(cfg, entity_ids)
    except VSphereError:
        raise
    except Exception as exc:  # noqa: BLE001 — see _as_vsphere_error
        raise _as_vsphere_error("privilege check", exc) from exc


def find_datacenter(inventory: Dict[str, Any], datacenter_id: str) -> Optional[Dict[str, Any]]:
    return next(
        (dc for dc in inventory.get("datacenters") or [] if dc.get("id") == datacenter_id),
        None,
    )


# ---------------------------------------------------------------------------
# Demo inventory (KUBESIGHT_PROVISIONING_SIMULATE only)
# ---------------------------------------------------------------------------

def demo_placement(_cfg: Optional[VSphereConfig] = None) -> Dict[str, Any]:
    """A plausible vCenter for local walk-throughs. Never used unless the
    simulation switch is on; real installations always read their vCenter."""

    def template(moid, name, path, guest_id, full_name, tools, nics, disks, firmware="efi"):
        item = {
            "id": moid, "name": name, "path": path, "uuid": f"4211{moid[-4:]:0>4}-demo",
            "guestId": guest_id, "guestFullName": full_name, "guestDetail": full_name,
            "firmware": firmware, "toolsVersion": tools, "cpu": 2, "memoryMb": 4096,
            "disks": disks, "nics": nics, "scsiType": "pvscsi",
        }
        item["compatibility"] = assess_template(item)
        return item

    one_disk = [{"unit": 0, "sizeGb": 40, "thin": True, "eagerlyScrub": False}]
    vmx = [{"type": "vmxnet3"}]
    return {
        "fetchedAt": time.time(),
        "demo": True,
        "datacenters": [{
            "id": "datacenter-3",
            "name": "DC-Beirut",
            "clusters": [
                {"id": "domain-c8", "name": "Cluster-Prod-A", "path": "Cluster-Prod-A",
                 "hostCount": 4, "drsEnabled": True, "rootResourcePoolId": "resgroup-9",
                 "resourcePools": [{"id": "resgroup-20", "name": "k8s-builds", "path": "k8s-builds"}]},
                {"id": "domain-c31", "name": "Cluster-Lab", "path": "Cluster-Lab",
                 "hostCount": 2, "drsEnabled": False, "rootResourcePoolId": "resgroup-32",
                 "resourcePools": []},
            ],
            "folders": [
                {"id": "group-v22", "path": "KubeSight"},
                {"id": "group-v23", "path": "Templates"},
            ],
            "datastores": [
                {"id": "datastore-11", "name": "ds-ssd-01", "capacityGb": 2048, "freeGb": 380,
                 "accessible": True, "type": "VMFS"},
                {"id": "datastore-12", "name": "ds-ssd-02", "capacityGb": 4096, "freeGb": 2150,
                 "accessible": True, "type": "VMFS"},
                {"id": "datastore-13", "name": "ds-nvme-01", "capacityGb": 1536, "freeGb": 910,
                 "accessible": True, "type": "VMFS"},
            ],
            "networks": [
                {"id": "dvportgroup-41", "name": "VM-Net-K8S-30", "kind": "distributed"},
                {"id": "dvportgroup-42", "name": "VM-Net-DMZ-12", "kind": "distributed"},
            ],
            "templates": [
                template("vm-101", "ubuntu-22.04-k8s-base", "Templates/ubuntu-22.04-k8s-base",
                         "ubuntu64Guest", "Ubuntu 22.04.5 LTS", 12389, vmx, one_disk),
                template("vm-102", "ubuntu-24.04-cloud", "Templates/ubuntu-24.04-cloud",
                         "ubuntu64Guest", "Ubuntu 24.04.1 LTS", 12416, vmx,
                         [{"unit": 0, "sizeGb": 30, "thin": True, "eagerlyScrub": False}]),
                template("vm-103", "rocky-9.4-minimal", "Templates/rocky-9.4-minimal",
                         "rockylinux_64Guest", "Rocky Linux 9.4", 12389, vmx + vmx,
                         [{"unit": 0, "sizeGb": 20, "thin": True, "eagerlyScrub": False}]),
                template("vm-104", "debian-12-golden", "Templates/debian-12-golden",
                         "debian12_64Guest", "Debian GNU/Linux 12", 0, vmx,
                         [{"unit": 0, "sizeGb": 32, "thin": True, "eagerlyScrub": False}]),
                template("vm-105", "win-2022-std", "Templates/win-2022-std",
                         "windows2019srvNext_64Guest", "Microsoft Windows Server 2022", 12389,
                         [{"type": "e1000e"}],
                         [{"unit": 0, "sizeGb": 60, "thin": True, "eagerlyScrub": False}],
                         firmware="efi"),
            ],
            "vmNames": ["payments-uat-01-cp-1", "payments-uat-01-wk-1", "vcsa-01"],
        }],
    }
