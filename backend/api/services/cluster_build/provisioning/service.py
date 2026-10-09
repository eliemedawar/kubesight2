"""What the routes call: shape a VMware build, plan it, apply it, grow it,
destroy it — with the guards that keep those from stepping on each other.

Who may do what (the routes add the permission check):
  * a create or grow plan is applied by anyone allowed to execute builds,
    the requester included — they reviewed the plan;
  * a destroy plan waits for a *second* person with that permission. The
    requester can withdraw it, never approve it.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

from ....db import db
from ....models import ClusterBuild, ClusterInfraState, ClusterProvisionJob, VSphereConnection
from . import inventory, ip_pool, jobs, state_store, templates, tofu_runner

_NAME_RE = re.compile(r"^[a-z0-9]([a-z0-9-]{0,48}[a-z0-9])?$")
_ID_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,80}$")
_BUSY_BUILD_STATUSES = ("building", "preflighting", "provisioning", "destroying")


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def is_vmware(build: ClusterBuild) -> bool:
    return (build.machine_source or "existing") == "vmware"


# ---------------------------------------------------------------------------
# The spec a VMware build carries
# ---------------------------------------------------------------------------

def validate_cluster_name(name: str) -> str:
    if not _NAME_RE.fullmatch(name or ""):
        raise ValueError(
            "When KubeSight creates the VMs, the cluster name also names them "
            "(<name>-cp-1) and becomes their hostname: use lowercase letters, "
            "digits and hyphens, at most 50 characters."
        )
    return name


def _object_id(raw: Dict[str, Any], key: str, label: str, *, required: bool = True) -> Optional[str]:
    value = str(raw.get(key) or "").strip()
    if not value:
        if required:
            raise ValueError(f"Choose a {label}.")
        return None
    if not _ID_RE.fullmatch(value):
        raise ValueError(f"{label} id is not a vCenter object id.")
    return value


def _text(raw: Dict[str, Any], key: str, limit: int = 255) -> str:
    return str(raw.get(key) or "").strip()[:limit]


def _normalize_template(raw: Any) -> Dict[str, Any]:
    raw = raw if isinstance(raw, dict) else {}
    uuid = str(raw.get("uuid") or "").strip()
    if not uuid or len(uuid) > 64:
        raise ValueError("Choose the VM template to clone.")
    disks = []
    for disk in raw.get("disks") or []:
        if isinstance(disk, dict):
            disks.append({
                "unit": int(disk.get("unit") or 0),
                "sizeGb": int(disk.get("sizeGb") or 0),
                "thin": bool(disk.get("thin", True)),
                "eagerlyScrub": bool(disk.get("eagerlyScrub", False)),
            })
    nics = [
        {"type": str(nic.get("type") or "vmxnet3")[:16]}
        for nic in raw.get("nics") or [] if isinstance(nic, dict)
    ]
    return {
        "id": _text(raw, "id", 80),
        "name": _text(raw, "name"),
        "path": _text(raw, "path", 512),
        "uuid": uuid,
        "guestId": _text(raw, "guestId", 64) or None,
        "guestFullName": _text(raw, "guestFullName") or None,
        "firmware": (_text(raw, "firmware", 8) or "bios").lower(),
        "scsiType": _text(raw, "scsiType", 16) or "pvscsi",
        # The template's own size: what a clone keeps when KubeSight does not
        # resize it (sizeMode "template").
        "cpu": int(raw.get("cpu") or 0) or None,
        "memoryMb": int(raw.get("memoryMb") or 0) or None,
        "nicType": (nics[0]["type"] if nics else "vmxnet3"),
        "nics": nics,
        "disks": disks,
    }


_LOCKED_ONCE_CREATED = (
    "vsphereConnectionId", "datacenterId", "clusterId", "resourcePoolId",
    "folderParentId", "folderParent", "folderMode", "networkId", "counts", "vmCount",
)
# create: OpenTofu makes <folder>/<cluster name> and removes it with the
#         cluster (needs Folder.Create / Folder.Delete on the chosen folder).
# existing: the VMs go straight into a folder someone already made — often
#         the one a vSphere admin set aside and granted the account on.
_FOLDER_MODES = ("create", "existing")
# custom: KubeSight sets CPU / memory / disk per role.
# template: every clone keeps the VM template's own size, for an account that
#         may not change CPU or memory.
_SIZE_MODES = ("custom", "template")


def normalize_spec(build: ClusterBuild, raw: Any) -> Dict[str, Any]:
    """The provisioning spec from a wizard payload, checked for shape.

    Whether the objects still exist in vCenter is checked when the plan is
    made, against a fresh read of vCenter — never trusted from the browser.
    """
    raw = raw if isinstance(raw, dict) else {}
    previous = build.provisioning_json or {}
    connection_id = raw.get("vsphereConnectionId") or previous.get("vsphereConnectionId")
    try:
        connection_id = int(connection_id)
    except (TypeError, ValueError) as exc:
        raise ValueError("Choose the vCenter to create the VMs in.") from exc
    connection = db.session.get(VSphereConnection, connection_id)
    if connection is None:
        raise ValueError("vCenter connection not found.")

    template = _normalize_template(raw.get("template"))
    size_mode = str(raw.get("sizeMode") or previous.get("sizeMode") or "custom").strip()
    if size_mode not in _SIZE_MODES:
        raise ValueError("sizeMode must be 'custom' or 'template'.")
    if size_mode == "template":
        templates.template_size(template)  # refuses a template with no reported size
    if build.vms_only:
        # VMs first: just how many. Their roles are chosen when (if) someone
        # installs Kubernetes on them.
        counts = None
        vm_count = templates.normalize_vm_count(raw.get("vmCount") or previous.get("vmCount"))
        raw_sizes = raw.get("sizes") or previous.get("sizes") or {}
        sizes = {templates.VM_ROLE: templates.normalize_vm_size(
            raw_sizes.get(templates.VM_ROLE) if isinstance(raw_sizes, dict) else None
        ) if size_mode == "custom" else templates.template_size(template)}
    else:
        vm_count = None
        counts = templates.normalize_counts(raw.get("counts") or previous.get("counts"))
        sizes = templates.normalize_sizes(
            raw.get("sizes") or previous.get("sizes"), counts if size_mode == "custom" else {}
        )
    folder_mode = str(raw.get("folderMode") or previous.get("folderMode") or "create").strip()
    if folder_mode not in _FOLDER_MODES:
        raise ValueError("folderMode must be 'create' or 'existing'.")
    network_name = _text(raw, "networkName")
    range_row = ip_pool.range_for_network(connection.id, network_name)
    if range_row is None:
        raise ValueError(
            f"{network_name or 'That network'} has no address range in KubeSight. "
            "An administrator adds one under Sources → Networks."
        )

    spec = {
        "vsphereConnectionId": connection.id,
        "datacenterId": _object_id(raw, "datacenterId", "datacenter"),
        "datacenterName": _text(raw, "datacenterName"),
        "clusterId": _object_id(raw, "clusterId", "vSphere cluster"),
        "clusterName": _text(raw, "clusterName"),
        "resourcePoolId": _object_id(raw, "resourcePoolId", "resource pool"),
        "resourcePoolName": _text(raw, "resourcePoolName"),
        "folderParentId": _object_id(raw, "folderParentId", "folder",
                                     required=folder_mode == "existing"),
        "folderParent": _text(raw, "folderParent", 512).strip("/"),
        "folderMode": folder_mode,
        "datastoreId": _object_id(raw, "datastoreId", "datastore"),
        "datastoreName": _text(raw, "datastoreName"),
        "networkId": _object_id(raw, "networkId", "network"),
        "networkName": network_name,
        "networkRangeId": range_row.id,
        "template": template,
        "counts": counts,
        "vmCount": vm_count,
        "sizes": sizes,
        "sizeMode": size_mode,
        "antiAffinity": bool(raw.get("antiAffinity", True)),
        "machines": list(previous.get("machines") or []),
    }

    if folder_mode == "existing" and not spec["folderParent"]:
        raise ValueError("Choose the folder the VMs go into.")

    if state_store.has_resources(build.id):
        changed = [key for key in _LOCKED_ONCE_CREATED if spec.get(key) != previous.get(key)]
        if (spec["template"]["uuid"] != (previous.get("template") or {}).get("uuid")):
            changed.append("template")
        if changed:
            raise ValueError(
                "Some of this cluster's VMs already exist, so where they live and "
                "how many there are can no longer change. Only the datastore and "
                "sizes of machines not created yet can."
            )
    return spec


def apply_shape_to_build(build: ClusterBuild) -> None:
    """Topology and endpoint fields as the Cluster Builder phases expect them."""
    counts = (build.provisioning_json or {}).get("counts")
    if not counts:
        return
    shape = templates.topology_for(counts)
    build.topology_type = shape["topologyType"]
    build.endpoint_mode = shape["endpointMode"]


# ---------------------------------------------------------------------------
# Machines and addresses
# ---------------------------------------------------------------------------

def machine_size(spec: Dict[str, Any], role: str, sizes: Optional[Dict[str, Any]] = None) -> Dict[str, int]:
    """CPU, memory (GB and MB) and disk one new machine of ``role`` gets."""
    if spec.get("sizeMode") == "template":
        return templates.template_size(spec.get("template") or {})
    size = (sizes or spec.get("sizes") or {})[role]
    return {
        "cpu": int(size["cpu"]),
        "memoryGb": int(size["memoryGb"]),
        "memoryMb": int(size["memoryGb"]) * 1024,
        "diskGb": int(size["diskGb"]),
    }


def _wanted_roles(spec: Dict[str, Any]) -> List[Tuple[str, int]]:
    if spec.get("vmCount"):
        return [(templates.VM_ROLE, int(spec["vmCount"]))]
    counts = spec["counts"]
    return [(role, counts[role]) for role in ("loadbalancer", "controlPlane", "worker")]


def _plan_machines(build: ClusterBuild) -> List[Dict[str, Any]]:
    """Every VM the build should have, with reserved addresses.

    Machines OpenTofu already created keep everything. The others take the
    spec's current sizes and datastore, which is how "pick another datastore
    and plan again" works after a partial failure.
    """
    spec = dict(build.provisioning_json or {})
    counts = spec.get("counts") or {}
    created = set(state_store.vm_instances(build.id))
    previous = {m["name"]: m for m in spec.get("machines") or []}
    wanted: List[Dict[str, Any]] = []
    for role, count in _wanted_roles(spec):
        for index in range(1, count + 1):
            name = f"{build.name}-{templates.ROLE_SHORT[role]}-{index}"
            if name in created and name in previous:
                wanted.append(previous[name])
                continue
            wanted.append({
                "name": name,
                "role": role,
                **machine_size(spec, role),
                "datastoreId": spec["datastoreId"],
            })

    range_row = ip_pool.get_range(int(spec["networkRangeId"]))
    keys = [{"key": m["name"], "purpose": "node"} for m in wanted]
    if counts.get("loadbalancer"):
        keys.insert(0, {"key": "vip", "purpose": "vip"})
    addresses = ip_pool.reserve(range_row, build.id, keys)
    stale = {
        r.node_name for r in ip_pool.reservations_for(build.id)
        if r.node_name not in addresses and r.status == "reserved"
    }
    if stale:
        ip_pool.release(build.id, keys=stale, only_reserved=True)
    for machine in wanted:
        machine["ip"] = addresses[machine["name"]]
    spec["machines"] = wanted
    build.provisioning_json = spec
    if counts.get("loadbalancer"):
        build.vip_address = addresses["vip"]
        build.control_plane_endpoint = f"{addresses['vip']}:6443"
    else:
        # A VMs-only build has no API endpoint until Kubernetes is installed.
        primary = next((m for m in wanted if m["role"] == "controlPlane"), None)
        build.vip_address = None
        build.control_plane_endpoint = f"{primary['ip']}:6443" if primary else ""
    return wanted


def _sync_nodes(build: ClusterBuild, machines: List[Dict[str, Any]]) -> None:
    from ....models import ClusterBuildNode

    by_name = {n.vsphere_vm_name: n for n in build.nodes if n.vsphere_vm_name}
    keep = []
    for position, machine in enumerate(machines):
        node = by_name.get(machine["name"])
        if node is None:
            node = ClusterBuildNode(build_id=build.id, status="pending")
        node.role = templates.ROLE_TO_NODE_ROLE[machine["role"]]
        node.hostname = machine["name"]
        node.vsphere_vm_name = machine["name"]
        node.address = machine["ip"]
        node.address_source = "provisioned"
        node.vsphere_cpu = machine["cpu"]
        node.vsphere_memory_mb = machine["memoryGb"] * 1024
        node.position = position
        node.is_primary_cp = False
        node.is_lb_master = False
        keep.append(node)
    first_cp = next((n for n in keep if n.role == "control_plane"), None)
    if first_cp is not None:
        first_cp.is_primary_cp = True
    first_lb = next((n for n in keep if n.role == "loadbalancer"), None)
    if first_lb is not None:
        first_lb.is_lb_master = True
    build.nodes = keep


# ---------------------------------------------------------------------------
# Requests
# ---------------------------------------------------------------------------

def _require_vmware(build: ClusterBuild) -> None:
    if not is_vmware(build):
        raise ValueError("This build uses machines that already exist; KubeSight did not create them.")


def _require_no_open_job(build: ClusterBuild, *, supersede_planned: bool = False) -> None:
    current = jobs.open_job(build.id)
    if current is None:
        return
    if supersede_planned and current.status == "planned":
        current.status = "discarded"
        current.finished_at = _utcnow()
        current.decision_note = "Replaced by a newer plan."
        db.session.commit()
        return
    described = {
        "planning": "a plan is being made",
        "planned": "a plan is waiting to be applied or discarded",
        "awaiting_approval": "a destroy request is waiting for approval",
        "applying": "OpenTofu is applying a plan",
        "connecting": "KubeSight is waiting for the new VMs to answer",
    }.get(current.status, current.status)
    raise ValueError(f"Not now: {described} (job #{current.id}).")


def _new_job(build: ClusterBuild, operation: str, *, actor: str, user, **fields) -> ClusterProvisionJob:
    job = ClusterProvisionJob(
        build_id=build.id,
        operation=operation,
        status="planning",
        requested_by=actor or None,
        requested_by_user_id=getattr(user, "id", None),
        prior_build_status=build.status,
        **fields,
    )
    db.session.add(job)
    db.session.flush()
    jobs._sync_build_status(build, job)
    return job


def request_create_plan(build: ClusterBuild, *, actor: str = "", user=None) -> ClusterProvisionJob:
    from .. import service as build_service

    _require_vmware(build)
    if build.status not in ("draft", "preflight_failed", "provision_failed"):
        raise ValueError(f"A plan for new VMs cannot be made while the build is '{build.status}'.")
    if not build.connection_profile_id:
        raise ValueError("Choose the SSH route KubeSight uses to reach the new VMs (Sources row).")
    if not build.provisioning_json:
        raise ValueError("Choose where in vCenter the VMs go first.")
    _require_no_open_job(build, supersede_planned=True)
    validate_cluster_name(build.name)
    apply_shape_to_build(build)
    machines = _plan_machines(build)
    _sync_nodes(build, machines)
    if not build.vms_only:
        build_service._validate_topology(
            build,
            [{"role": n.role, "hostname": n.hostname, "address": n.address} for n in build.nodes],
        )
    if build.status == "provision_failed":
        build.status = "draft"
    build.error = None
    job = _new_job(build, "create", actor=actor, user=user)
    db.session.commit()
    jobs.start_worker(job.id, "plan")
    db.session.refresh(job)
    return job


def _grow_counts(payload: Dict[str, Any]) -> Dict[str, int]:
    """How many of each role to add. ``count`` alone means workers (the
    original, workers-only form of this request)."""
    def whole(key: str) -> int:
        try:
            value = int(payload.get(key) or 0)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"{key} must be a whole number.") from exc
        if value < 0:
            raise ValueError(f"{key} cannot be negative.")
        return value

    counts = {
        "worker": whole("workers") or whole("count"),
        "controlPlane": whole("controlPlanes"),
        "loadbalancer": whole("loadBalancers"),
    }
    if not any(counts.values()):
        raise ValueError("Add at least one machine.")
    if counts["worker"] > 20:
        raise ValueError("Add at most 20 workers at a time.")
    return counts


def request_grow_plan(build: ClusterBuild, payload: Dict[str, Any], *, actor: str = "", user=None) -> ClusterProvisionJob:
    from .. import service as build_service

    _require_vmware(build)
    build_service._require_growable(build)
    if build_service.growth_nodes(build):
        raise ValueError("Machines are already queued to join. Finish or remove those first.")
    _require_no_open_job(build, supersede_planned=True)
    spec = build.provisioning_json or {}
    adding = _grow_counts(payload)
    build_service.check_tier_growth(
        build,
        {templates.ROLE_TO_NODE_ROLE[role]: n for role, n in adding.items()},
        final=True,
    )
    raw_sizes = dict(spec.get("sizes") or {})
    for role, size in (payload.get("sizes") or {}).items():
        if role in raw_sizes and isinstance(size, dict):
            raw_sizes[role] = size
    if payload.get("size") and isinstance(payload["size"], dict):
        raw_sizes["worker"] = payload["size"]  # the workers-only form
    sizes = templates.normalize_sizes(
        raw_sizes, adding if spec.get("sizeMode") != "template" else {}
    )

    new: List[Dict[str, Any]] = []
    for role in ("loadbalancer", "controlPlane", "worker"):
        if not adding[role]:
            continue
        short = templates.ROLE_SHORT[role]
        taken = [m["name"] for m in spec.get("machines") or [] if m["role"] == role]
        numbers = [
            int(match.group(1)) for name in taken
            if (match := re.search(rf"-{short}-(\d+)$", name))
        ]
        start = max(numbers, default=0) + 1
        new.extend({
            "name": f"{build.name}-{short}-{start + offset}",
            "role": role,
            **machine_size(spec, role, sizes),
            "datastoreId": spec["datastoreId"],
        } for offset in range(adding[role]))
    range_row = ip_pool.get_range(int(spec["networkRangeId"]))
    addresses = ip_pool.reserve(range_row, build.id, [{"key": m["name"]} for m in new])
    for machine in new:
        machine["ip"] = addresses[machine["name"]]
    job = _new_job(build, "grow", actor=actor, user=user, config_json={"newMachines": new})
    db.session.commit()
    jobs.start_worker(job.id, "plan")
    db.session.refresh(job)
    return job


def request_destroy(build: ClusterBuild, payload: Dict[str, Any], *, actor: str = "", user=None) -> ClusterProvisionJob:
    _require_vmware(build)
    if build.status in _BUSY_BUILD_STATUSES or build.status == "destroyed":
        raise ValueError(f"The VMs cannot be destroyed while the build is '{build.status}'.")
    if not state_store.has_resources(build.id):
        raise ValueError("OpenTofu's state lists nothing to destroy for this build.")
    if str(payload.get("confirmName") or "").strip() != build.name:
        raise ValueError(f"Type the cluster name, {build.name}, to confirm.")
    reason = str(payload.get("reason") or "").strip()[:1000] or None
    _require_no_open_job(build, supersede_planned=True)
    job = _new_job(build, "destroy", actor=actor, user=user, reason=reason)
    db.session.commit()
    jobs.start_worker(job.id, "plan")
    db.session.refresh(job)
    return job


def apply_plan(build: ClusterBuild, job: ClusterProvisionJob, *, actor: str = "", user=None) -> ClusterProvisionJob:
    if job.operation == "destroy":
        raise ValueError("A destroy plan is applied by approving it — and not by the person who asked.")
    if job.status != "planned":
        raise ValueError(f"Only a finished plan can be applied (this one is '{job.status}').")
    latest = jobs.open_job(build.id)
    if latest is None or latest.id != job.id:
        raise ValueError("A newer plan replaced this one.")
    blocked = (job.plan_summary_json or {}).get("blocked")
    if blocked:
        raise ValueError(blocked)
    if build.status in ("building", "preflighting", "destroying"):
        raise ValueError(f"The build is '{build.status}'.")
    job.status = "applying"
    job.applied_by = actor or None
    job.applied_by_user_id = getattr(user, "id", None)
    job.prior_build_status = build.status
    build.status = "provisioning"
    build.error = None
    jobs._sync_build_status(build, job)
    db.session.commit()
    jobs.start_worker(job.id, "apply")
    db.session.refresh(job)
    return job


def approve_destroy(build: ClusterBuild, job: ClusterProvisionJob, *, actor: str = "", user=None,
                    note: str = "") -> ClusterProvisionJob:
    if job.operation != "destroy" or job.status != "awaiting_approval":
        raise ValueError("There is no destroy request waiting for approval.")
    if user is not None and job.requested_by_user_id and job.requested_by_user_id == user.id:
        raise PermissionError(
            "You asked for this cluster to be destroyed, so someone else has to approve it."
        )
    if build.status in _BUSY_BUILD_STATUSES:
        raise ValueError(f"Wait: the build is '{build.status}'.")
    if (job.plan_summary_json or {}).get("blocked"):
        raise ValueError(job.plan_summary_json["blocked"])
    job.approved_by = actor or None
    job.approved_by_user_id = getattr(user, "id", None)
    job.approved_at = _utcnow()
    job.decision_note = (note or "").strip()[:1000] or None
    job.applied_by = actor or None
    job.applied_by_user_id = getattr(user, "id", None)
    job.status = "applying"
    job.prior_build_status = build.status
    build.status = "destroying"
    jobs._sync_build_status(build, job)
    db.session.commit()
    jobs.start_worker(job.id, "apply")
    db.session.refresh(job)
    return job


def reject_destroy(build: ClusterBuild, job: ClusterProvisionJob, *, actor: str = "", user=None,
                   note: str = "") -> ClusterProvisionJob:
    if job.operation != "destroy" or job.status != "awaiting_approval":
        raise ValueError("There is no destroy request waiting for a decision.")
    if user is not None and job.requested_by_user_id == user.id:
        raise PermissionError("Withdraw your own request instead of rejecting it.")
    job.status = "rejected"
    job.decision_note = (note or "").strip()[:1000] or None
    job.approved_by = actor or None
    job.approved_by_user_id = getattr(user, "id", None)
    job.finished_at = _utcnow()
    jobs._sync_build_status(build, job)
    db.session.commit()
    return job


def discard(build: ClusterBuild, job: ClusterProvisionJob, *, actor: str = "", user=None) -> ClusterProvisionJob:
    """Throw away a plan (or withdraw a destroy request) that was never applied."""
    if job.status not in ("planned", "plan_failed", "awaiting_approval"):
        raise ValueError(f"A job that is '{job.status}' cannot be discarded.")
    if job.status == "awaiting_approval" and user is not None and job.requested_by_user_id not in (None, user.id):
        raise PermissionError("Only the person who asked can withdraw a destroy request; others reject it.")
    job.status = "discarded"
    job.decision_note = f"Discarded by {actor}" if actor else "Discarded"
    job.finished_at = _utcnow()
    if job.operation == "grow":
        ip_pool.release(
            build.id,
            keys=[m["name"] for m in (job.config_json or {}).get("newMachines") or []],
            only_reserved=True,
        )
    elif job.operation == "create" and not state_store.has_resources(build.id):
        # Nothing exists: hand the addresses back and forget the machines.
        ip_pool.release(build.id, only_reserved=True)
        spec = dict(build.provisioning_json or {})
        spec["machines"] = []
        build.provisioning_json = spec
        build.nodes = []
        build.vip_address = None
    jobs._sync_build_status(build, job)
    db.session.commit()
    return job


def retry_connect(build: ClusterBuild, job: ClusterProvisionJob) -> ClusterProvisionJob:
    if job.status != "connect_failed":
        raise ValueError("Only a job whose VMs did not answer can be retried this way.")
    job.status = "connecting"
    job.error = None
    job.finished_at = None
    build.status = "provisioning"
    jobs._sync_build_status(build, job)
    db.session.commit()
    jobs.start_worker(job.id, "connect")
    db.session.refresh(job)
    return job


def _assign_roles(build: ClusterBuild, counts: Dict[str, int]) -> List[Dict[str, Any]]:
    """Give the build's VMs Kubernetes roles, in VM order: balancers first, then
    control planes, then workers. Names and addresses stay as they are."""
    spec = dict(build.provisioning_json or {})
    machines = [dict(m) for m in spec.get("machines") or []]
    roles = (["loadbalancer"] * counts["loadbalancer"]
             + ["controlPlane"] * counts["controlPlane"]
             + ["worker"] * counts["worker"])
    for machine, role in zip(machines, roles):
        machine["role"] = role
    sizes = dict(spec.get("sizes") or {})
    for role in ("loadbalancer", "controlPlane", "worker"):
        first = next((m for m in machines if m["role"] == role), None)
        if first is not None:
            sizes[role] = {"cpu": first["cpu"], "memoryGb": first["memoryGb"], "diskGb": first["diskGb"]}
    spec.update(machines=machines, counts=counts, sizes=sizes, vmCount=None)
    build.provisioning_json = spec
    offered = templates.catalog()
    build.template_id = next(
        (t["id"] for t in offered["builtin"] + offered["custom"] if (t.get("counts") or {}) == counts),
        "custom",
    )
    return machines


def _reserve_vip(build: ClusterBuild) -> str:
    """An API address for a VMs-only build that turns out to need balancers."""
    spec = build.provisioning_json or {}
    range_row = ip_pool.get_range(int(spec["networkRangeId"]))
    skip: set = set()
    for _ in range(3):
        address = ip_pool.reserve(range_row, build.id, [{"key": "vip", "purpose": "vip"}], skip=skip)["vip"]
        if not ip_pool.probe_in_use([address]):
            ip_pool.mark_in_use(build.id, {"vip"})
            return address
        skip.add(address)
    raise ValueError(f"No free API address on {spec.get('networkName')}: the ones tried all answer.")


def install_kubernetes(build: ClusterBuild, payload: Optional[Dict[str, Any]] = None, *,
                       actor: str = "", user=None) -> Optional[str]:
    """Turn a VMs-only build into a cluster build on the VMs it already has.

    ``payload["counts"]`` says which shape the VMs take (Lab, Small, …); it must
    add up to the number of VMs. Then the same hand-off a normal VMware build
    takes once its VMs answer: preflight, then the build starts on its own when
    preflight is clean. Returns what a person still has to look at, or None
    when the build started.
    """
    from .. import service as build_service
    from .. import k8s_versions

    payload = payload or {}
    _require_vmware(build)
    if not build.vms_only or build.status != "vms_ready":
        raise ValueError("Kubernetes can be installed only on a VMs-only build whose VMs are ready.")
    _require_no_open_job(build)
    created = next(
        (j for j in jobs.jobs_for(build.id) if j.operation == "create" and j.status == "succeeded"),
        None,
    )
    if created is None or not state_store.has_resources(build.id):
        raise ValueError("OpenTofu's state lists no VMs for this build.")
    total = len((build.provisioning_json or {}).get("machines") or [])
    counts = templates.counts_for_vms(payload.get("counts"), total)
    if payload.get("k8sVersion"):
        build.k8s_version = k8s_versions.validate_version(payload["k8sVersion"])

    machines = _assign_roles(build, counts)
    _sync_nodes(build, machines)
    apply_shape_to_build(build)
    if counts["loadbalancer"]:
        vip = build.vip_address or _reserve_vip(build)
        build.vip_address = vip
        build.control_plane_endpoint = f"{vip}:6443"
    else:
        primary = next(m for m in machines if m["role"] == "controlPlane")
        build.vip_address = None
        build.control_plane_endpoint = f"{primary['ip']}:6443"
    build_service._validate_topology(
        build,
        [{"role": n.role, "hostname": n.hostname, "address": n.address} for n in build.nodes],
    )
    build.vms_only = False
    build.status = "draft"
    build.finished_at = None
    build.error = None
    db.session.commit()
    note = jobs.start_kubernetes(build, created, user=user, actor=actor)
    db.session.rollback()
    job = db.session.get(ClusterProvisionJob, created.id)
    progress = dict(job.progress_json or {})
    if note:
        progress["handoffNote"] = note[:1000]
    else:
        progress.pop("handoffNote", None)
    job.progress_json = progress
    db.session.commit()
    return note


# ---------------------------------------------------------------------------
# Reading
# ---------------------------------------------------------------------------

def summary_for_list(build: ClusterBuild) -> Optional[Dict[str, Any]]:
    if not is_vmware(build):
        return None
    current = jobs.open_job(build.id) or (jobs.jobs_for(build.id) or [None])[0]
    return {
        "provisionStatus": build.provision_status,
        "job": jobs.serialize_job(current) if current else None,
    }


def detail(build: ClusterBuild) -> Optional[Dict[str, Any]]:
    if not is_vmware(build):
        return None
    history = jobs.jobs_for(build.id)
    current = jobs.open_job(build.id) or (history[0] if history else None)
    reservations = ip_pool.reservations_for(build.id)
    return {
        "provisionStatus": build.provision_status,
        "spec": build.provisioning_json or {},
        "job": jobs.serialize_job(current, include_plan=True) if current else None,
        "history": [jobs.serialize_job(item) for item in history[:12]],
        "state": state_store.summary(build.id),
        "reservations": [
            {"address": r.address, "purpose": r.purpose, "name": r.node_name, "status": r.status}
            for r in reservations
        ],
    }


def overview() -> Dict[str, Any]:
    """For Sources: is OpenTofu usable, and what state does KubeSight hold."""
    rows = ClusterInfraState.query.order_by(ClusterInfraState.updated_at.desc()).all()
    states = []
    for row in rows:
        build = db.session.get(ClusterBuild, row.build_id)
        if build is None:
            continue
        last = (jobs.jobs_for(build.id) or [None])[0]
        states.append({
            "buildId": build.id,
            "name": build.name,
            "buildStatus": build.status,
            "provisionStatus": build.provision_status,
            "state": state_store.summary(build.id),
            "lastJob": jobs.serialize_job(last) if last else None,
        })
    return {
        "engine": tofu_runner.engine_status(),
        "simulated": inventory.simulation_enabled(),
        "states": states,
        "requiredPrivileges": [
            {"privilege": privilege, "purpose": purpose, "scope": scope}
            for privilege, purpose, scope in inventory.REQUIRED_PRIVILEGES
        ],
    }


def release_stale_lock(build_id: int) -> Dict[str, Any]:
    row = ClusterInfraState.query.filter_by(build_id=build_id).first()
    if row is None or not row.lock_id:
        raise ValueError("This cluster's state is not locked.")
    if row.lock_job_id:
        holder = db.session.get(ClusterProvisionJob, row.lock_job_id)
        if holder is not None and holder.status in jobs.ACTIVE_STATUSES:
            raise ValueError(
                f"Job #{holder.id} holds this lock and is still {holder.status}. "
                "It releases the lock itself when it finishes."
            )
    released = state_store.force_release(build_id)
    return released or {}
