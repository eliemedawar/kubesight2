"""Cluster build CRUD, validation, preflight orchestration, and lifecycle.

Topology rules enforced here (not in the UI, which merely mirrors them):
  * control-plane count must be 1 or 3 (5 allowed but warned) — never 2/4
    (etcd quorum arithmetic);
  * managed_haproxy requires 1 LB for single-CP labs or 2 LBs for HA + a VIP;
  * every build gets a stable controlPlaneEndpoint, single-CP included.
"""

from __future__ import annotations

import ipaddress
import re
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from ...db import db
from ...models import (
    BuildProfile,
    ClusterBuild,
    ClusterBuildNode,
    ClusterBuildStep,
)
from ...secret_encryption import encrypt_secret
from .. import ssh_profile_service, vsphere_service
from ..vsphere_client import VSphereError
from . import addons as addon_registry
from . import cni as cni_registry
from .addons import metallb as metallb_addon
from . import executor, k8s_versions, os_adapters, preflight
from .profiles import resolve as resolve_profile
from .scrub import scrub

_ENDPOINT_MODES = {"managed_haproxy", "external_lb", "manual_endpoint"}
_TOPOLOGIES = {"single_cp", "stacked_ha"}
_ROLES = {"control_plane", "worker", "loadbalancer"}
_EDITABLE_STATUSES = {"draft", "preflight_passed", "preflight_failed"}

# The versions KubeSight ships when upstream release discovery is unreachable.
# The live list served to the wizard is discovered per supported minor — see
# ``k8s_versions`` — but this stays the documented, tested floor.
SUPPORTED_K8S_VERSIONS = k8s_versions.STATIC_FALLBACK_VERSIONS

_ENDPOINT_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9.\-]*(:\d{1,5})?$")
_DNS_LABEL_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]*[a-z0-9])?$")
_IFNAME_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,15}$")


def _iso(dt) -> Optional[str]:
    return dt.isoformat() if dt else None


# ---------------------------------------------------------------------------
# Serialization
# ---------------------------------------------------------------------------

def serialize_node(node: ClusterBuildNode) -> Dict[str, Any]:
    return {
        "id": node.id,
        "hostname": node.hostname,
        "address": node.address,
        "addressSource": node.address_source,
        "role": node.role,
        "isPrimaryCp": node.is_primary_cp,
        "isLbMaster": node.is_lb_master,
        "vsphereVmMoid": node.vsphere_vm_moid,
        "vsphereVmName": node.vsphere_vm_name,
        "vsphereHost": node.vsphere_host,
        "vsphereDatastore": node.vsphere_datastore,
        "vsphereToolsStatus": node.vsphere_tools_status,
        "vspherePowerState": node.vsphere_power_state,
        "vsphereCpu": node.vsphere_cpu,
        "vsphereMemoryMb": node.vsphere_memory_mb,
        "connectionProfileId": node.connection_profile_id,
        "osFamily": node.os_family,
        "osVersion": node.os_version,
        "arch": node.arch,
        "status": node.status,
        "preflight": node.preflight_json,
        "error": node.error,
        "position": node.position,
    }


def serialize_step(step: ClusterBuildStep) -> Dict[str, Any]:
    return {
        "id": step.id,
        "nodeId": step.node_id,
        "phase": step.phase,
        "status": step.status,
        "attempt": step.attempt,
        "startedAt": _iso(step.started_at),
        "finishedAt": _iso(step.finished_at),
        "error": step.error,
    }


_PHASE_ORDER = (
    "base_prep", "loadbalancer", "pull_images", "init", "cni",
    "join_cp", "join_workers", "verify", "onboard", "addons",
)


def _current_phase(build: ClusterBuild) -> Optional[str]:
    """The phase a running build is on: whatever is executing, else the
    furthest phase that has started. Drives the live label on build cards."""
    running = [s.phase for s in build.steps if s.status == "running"]
    if running:
        return min(running, key=lambda p: _PHASE_ORDER.index(p)
                   if p in _PHASE_ORDER else len(_PHASE_ORDER))
    started = [s.phase for s in build.steps if s.status != "pending"]
    if not started:
        return None
    return max(started, key=lambda p: _PHASE_ORDER.index(p)
               if p in _PHASE_ORDER else -1)


def serialize_build(build: ClusterBuild, *, include_detail: bool = False) -> Dict[str, Any]:
    data = {
        "id": build.id,
        "name": build.name,
        "status": build.status,
        "k8sVersion": build.k8s_version,
        "cri": build.cri,
        "topologyType": build.topology_type,
        "controlPlaneEndpoint": build.control_plane_endpoint,
        "endpointMode": build.endpoint_mode,
        "vipAddress": build.vip_address,
        "vipInterface": build.vip_interface,
        "cniPlugin": build.cni_plugin,
        "cniVersion": build.cni_version,
        "podCidr": build.pod_cidr,
        "serviceCidr": build.service_cidr,
        "addons": list(build.addons_json or []),
        "vsphereConnectionId": build.vsphere_connection_id,
        "buildProfileId": build.build_profile_id,
        "connectionProfileId": build.connection_profile_id,
        "resultClusterId": build.result_cluster_id,
        "currentPhase": _current_phase(build),
        "error": build.error,
        "createdBy": build.created_by,
        "createdAt": _iso(build.created_at),
        "startedAt": _iso(build.started_at),
        "finishedAt": _iso(build.finished_at),
        # Growth reuses the phase machine, so it needs its own clock; the
        # original build's duration is banked and never recomputed.
        "growthStartedAt": _iso(build.growth_started_at),
        "buildSeconds": build.build_seconds,
        "pendingNodeCount": sum(
            1 for n in build.nodes
            if n.status in ("pending", "preflight_passed", "preflight_failed")
        ),
        "canGrow": bool(build.status == "completed" and build.result_cluster_id),
        "nodeCounts": {
            "controlPlane": sum(1 for n in build.nodes if n.role == "control_plane"),
            "worker": sum(1 for n in build.nodes if n.role == "worker"),
            "loadbalancer": sum(1 for n in build.nodes if n.role == "loadbalancer"),
        },
        # Minimal per-node shape for the build-card glyph (LBs, then CPs, then
        # workers): circles = LB, red = CP, filled = joined.
        "nodeShape": [
            {"role": n.role, "status": n.status}
            for n in sorted(
                build.nodes,
                key=lambda n: (
                    {"loadbalancer": 0, "control_plane": 1}.get(n.role, 2),
                    n.position,
                ),
            )
        ],
    }
    if include_detail:
        data["nodes"] = [serialize_node(n) for n in build.nodes]
        data["steps"] = [serialize_step(s) for s in build.steps]
        data["warningsAck"] = build.warnings_ack_json
    return data


def list_builds() -> List[Dict[str, Any]]:
    rows = ClusterBuild.query.order_by(ClusterBuild.created_at.desc()).all()
    return [serialize_build(row) for row in rows]


def get_build(build_id: int) -> ClusterBuild:
    row = db.session.get(ClusterBuild, build_id)
    if row is None:
        raise LookupError("Cluster build not found.")
    return row


# ---------------------------------------------------------------------------
# Validation + CRUD
# ---------------------------------------------------------------------------

def _validate_cidr(value: str, field: str) -> str:
    value = str(value or "").strip()
    try:
        ipaddress.ip_network(value)
    except ValueError as exc:
        raise ValueError(f"{field} must be a valid CIDR (e.g. 10.244.0.0/16).") from exc
    return value


def _validate_dns_subdomain(value: str, field: str) -> str:
    value = str(value or "").strip()
    labels = value.split(".")
    if (
        not value
        or len(value) > 253
        or any(
            not label
            or len(label) > 63
            or not _DNS_LABEL_RE.fullmatch(label)
            for label in labels
        )
    ):
        raise ValueError(
            f"{field} must be a lowercase Kubernetes DNS subdomain."
        )
    return value


def _validate_node_address(value: str) -> str:
    value = str(value or "").strip()
    try:
        return str(ipaddress.ip_address(value))
    except ValueError:
        return _validate_dns_subdomain(value.lower(), "node address")


def _validate_interface(value: str) -> str:
    value = str(value or "").strip()
    if not _IFNAME_RE.fullmatch(value):
        raise ValueError(
            "vipInterface must be a Linux interface name using only "
            "letters, digits, '_', '.', ':', or '-' (maximum 15 characters)."
        )
    return value


def _validate_topology(build: ClusterBuild, nodes: List[Dict[str, Any]]) -> List[str]:
    """Returns warnings; raises on hard violations."""
    warnings: List[str] = []
    cp_count = sum(1 for n in nodes if n.get("role") == "control_plane")
    lb_count = sum(1 for n in nodes if n.get("role") == "loadbalancer")
    worker_count = sum(1 for n in nodes if n.get("role") == "worker")

    if build.vip_interface:
        _validate_interface(build.vip_interface)
    effective_node_names = []
    for node in nodes:
        address = _validate_node_address(str(node.get("address") or ""))
        hostname = str(node.get("hostname") or "").strip()
        if hostname:
            _validate_dns_subdomain(hostname, "node hostname")
        if node.get("role") != "loadbalancer":
            effective_node_names.append(_validate_dns_subdomain(
                hostname or address,
                "Kubernetes node name",
            ))
    duplicate_names = sorted({
        name for name in effective_node_names
        if effective_node_names.count(name) > 1
    })
    if duplicate_names:
        raise ValueError(
            "Kubernetes node names must be unique (duplicates: "
            f"{', '.join(duplicate_names)})."
        )

    if build.topology_type == "single_cp":
        if cp_count != 1:
            raise ValueError("single_cp topology requires exactly 1 control-plane node.")
    else:  # stacked_ha
        if cp_count == 5:
            warnings.append(
                "5 control planes: quorum tolerates 2 failures but etcd write "
                "latency rises; 3 is the recommended shape."
            )
        elif cp_count != 3:
            raise ValueError(
                "stacked_ha topology requires 3 control planes (5 tolerated). "
                f"Got {cp_count} — even counts cannot form a stable etcd quorum."
            )
    if build.endpoint_mode == "managed_haproxy":
        expected_lbs = 1 if build.topology_type == "single_cp" else 2
        if lb_count != expected_lbs:
            raise ValueError(
                f"managed_haproxy with {build.topology_type} requires exactly "
                f"{expected_lbs} load-balancer node"
                f"{'s' if expected_lbs != 1 else ''} (got {lb_count})."
            )
        if expected_lbs == 1:
            warnings.append(
                "Single managed load balancer: KubeSight will install HAProxy "
                "and Keepalived, but the API endpoint is not highly available."
            )
        if not build.vip_address:
            raise ValueError("managed_haproxy endpoint mode requires vipAddress.")
    elif lb_count:
        raise ValueError(
            f"{build.endpoint_mode} endpoint mode does not use load-balancer nodes."
        )
    if worker_count == 0 and build.addons_json:
        labels = []
        for item in build.addons_json:
            descriptor = addon_registry.get(str(item.get("id") or ""))
            labels.append(
                descriptor.display_name if descriptor is not None
                else str(item.get("id") or "unknown")
            )
        names = ", ".join(labels)
        raise ValueError(
            f"Selected add-ons ({names}) require at least one worker node; "
            "kubeadm control planes are tainted against normal workloads."
        )
    if any(
        item.get("id") == "metrics-server"
        for item in (build.addons_json or [])
    ):
        missing_hostnames = [
            str(node.get("address") or "unknown")
            for node in nodes
            if node.get("role") != "loadbalancer"
            and not str(node.get("hostname") or "").strip()
        ]
        if missing_hostnames:
            raise ValueError(
                "Metrics Server requires an explicit hostname for every "
                "Kubernetes node so serving certificates can be approved "
                f"safely (missing: {', '.join(missing_hostnames)})."
            )
        invalid_addresses = []
        for node in nodes:
            if node.get("role") == "loadbalancer":
                continue
            address = str(node.get("address") or "")
            try:
                ipaddress.ip_address(address)
            except ValueError:
                invalid_addresses.append(address or "unknown")
        if invalid_addresses:
            raise ValueError(
                "Metrics Server's serving-certificate policy requires node "
                "addresses to be IP literals (invalid: "
                f"{', '.join(invalid_addresses)})."
            )
    metallb = next(
        (
            item for item in (build.addons_json or [])
            if item.get("id") == "metallb"
        ),
        None,
    )
    if metallb is not None:
        pools = list((metallb.get("config") or {}).get("addressPools") or [])
        # A pool that swallows the API VIP or a node address hands MetalLB the
        # power to take the cluster offline the first time someone creates a
        # LoadBalancer service.
        reserved = [
            (str(build.vip_address or ""), "the API VIP"),
        ] + [
            (str(node.get("address") or ""),
             f"node {node.get('hostname') or node.get('address')}")
            for node in nodes
        ]
        collisions = [
            label for address, label in reserved
            if address and metallb_addon.pool_contains(pools, address)
        ]
        if collisions:
            raise ValueError(
                "The MetalLB address pool overlaps "
                f"{', '.join(dict.fromkeys(collisions))}. Reserve a range that "
                "no cluster address uses."
            )

    if worker_count == 0:
        warnings.append("No worker nodes: workloads will need control-plane tolerations.")
    return warnings


def _apply_build_payload(build: ClusterBuild, payload: Dict[str, Any]) -> None:
    name = str(payload.get("name", build.name or "")).strip()
    if not name:
        raise ValueError("name is required.")
    build.name = name

    # Same policy the options endpoint publishes, so every version the wizard
    # offers is accepted here. Minor-scoped rather than list-scoped on purpose:
    # a draft pinned to 1.32.4 must stay editable once 1.32.5 is discovered,
    # and the build keeps its own exact patch either way.
    build.k8s_version = k8s_versions.validate_version(
        payload.get("k8sVersion", build.k8s_version or "")
    )

    topology = str(payload.get("topologyType", build.topology_type or "single_cp")).strip()
    if topology not in _TOPOLOGIES:
        raise ValueError("topologyType must be 'single_cp' or 'stacked_ha'.")
    build.topology_type = topology

    endpoint_mode = str(payload.get("endpointMode", build.endpoint_mode or "managed_haproxy")).strip()
    if endpoint_mode not in _ENDPOINT_MODES:
        raise ValueError(
            "endpointMode must be 'managed_haproxy', 'external_lb', or 'manual_endpoint'."
        )
    build.endpoint_mode = endpoint_mode

    vip = str(payload.get("vipAddress", build.vip_address or "") or "").strip()
    if endpoint_mode == "managed_haproxy":
        if not vip:
            raise ValueError("vipAddress is required for managed_haproxy.")
        try:
            ipaddress.ip_address(vip)
        except ValueError as exc:
            raise ValueError("vipAddress must be a valid IP address.") from exc
        build.vip_address = vip
        build.control_plane_endpoint = f"{vip}:6443"
    else:
        build.vip_address = vip or None
        endpoint = str(
            payload.get("controlPlaneEndpoint", build.control_plane_endpoint or "")
        ).strip()
        if not endpoint:
            raise ValueError(
                "controlPlaneEndpoint is required — a stable endpoint keeps the "
                "single→HA migration path open (plan §5.1)."
            )
        if not _ENDPOINT_RE.match(endpoint):
            raise ValueError("controlPlaneEndpoint must be host[:port].")
        if ":" not in endpoint:
            endpoint = f"{endpoint}:6443"
        build.control_plane_endpoint = endpoint

    if "vipInterface" in payload:
        value = str(payload.get("vipInterface") or "").strip()
        build.vip_interface = _validate_interface(value) if value else None
    if "vrrpRouterId" in payload and payload.get("vrrpRouterId"):
        router_id = int(payload["vrrpRouterId"])
        if router_id < 1 or router_id > 255:
            raise ValueError("vrrpRouterId must be 1-255.")
        build.vrrp_router_id = router_id

    cni_plugin = str(payload.get("cniPlugin", build.cni_plugin or "calico")).strip()
    descriptor = cni_registry.get(cni_plugin)
    if descriptor is None:
        raise ValueError(
            f"cniPlugin must be one of: "
            f"{', '.join(d.id for d in cni_registry.available())}."
        )
    build.cni_plugin = cni_plugin
    # CNI support windows move with Kubernetes, so the default is the newest
    # release validated on *this build's* minor — not simply the newest release.
    # Picking versions[0] blindly would pair a 1.29 build with a Calico that
    # dropped 1.29, and the mismatch would only surface at the CNI phase.
    k8s_minor = k8s_versions.minor_of(build.k8s_version)
    # Only a cniVersion in *this* payload counts as the caller's explicit
    # choice. A value carried over from the row is a default the server derived
    # earlier, so changing a draft's Kubernetes version re-derives it instead of
    # failing on a pin the user never asked for.
    requested_cni_version = str(payload.get("cniVersion") or "").strip()
    cni_version = requested_cni_version or str(build.cni_version or "").strip()
    if cni_version and cni_version not in descriptor.versions:
        if requested_cni_version:
            raise ValueError(
                f"cniVersion for {descriptor.display_name} must be one of: "
                f"{', '.join(descriptor.versions)}."
            )
        cni_version = ""
    if cni_version and not requested_cni_version and not descriptor.supports_k8s_minor(
        cni_version, k8s_minor
    ):
        cni_version = ""  # stale carry-over; fall through to the default below
    if not cni_version:
        cni_version = descriptor.default_version_for_k8s_minor(k8s_minor)
        if not cni_version:
            usable = ", ".join(
                d.display_name for d in cni_registry.available()
                if d.versions_for_k8s_minor(k8s_minor)
            )
            raise ValueError(
                f"{descriptor.display_name} has no version validated on "
                f"Kubernetes {k8s_minor}. "
                + (f"Choose one of: {usable}." if usable
                   else "No CNI plugin covers that Kubernetes version.")
            )
    elif not descriptor.supports_k8s_minor(cni_version, k8s_minor):
        usable = descriptor.versions_for_k8s_minor(k8s_minor)
        raise ValueError(
            f"{descriptor.display_name} {cni_version} is not validated on "
            f"Kubernetes {k8s_minor}. "
            + (
                f"Use {', '.join(usable)} instead." if usable
                else "No version of this CNI covers that Kubernetes version."
            )
        )
    build.cni_version = cni_version

    build.pod_cidr = _validate_cidr(
        payload.get("podCidr", build.pod_cidr or descriptor.default_pod_cidr), "podCidr"
    )
    build.service_cidr = _validate_cidr(
        payload.get("serviceCidr", build.service_cidr or "10.96.0.0/12"), "serviceCidr"
    )
    if "addons" in payload and "plugins" in payload:
        raise ValueError("Use either addons or plugins, not both.")
    if "addons" in payload:
        build.addons_json = addon_registry.normalize_selection(
            payload.get("addons"), k8s_minor
        )
    elif "plugins" in payload:
        # Friendly alias for callers using the UI's "Plugins & add-ons" label.
        build.addons_json = addon_registry.normalize_selection(
            payload.get("plugins"), k8s_minor
        )

    for key, attr in (
        ("vsphereConnectionId", "vsphere_connection_id"),
        ("buildProfileId", "build_profile_id"),
        ("connectionProfileId", "connection_profile_id"),
    ):
        if key in payload:
            value = payload.get(key)
            setattr(build, attr, int(value) if value else None)
    if build.build_profile_id:
        profile = db.session.get(BuildProfile, build.build_profile_id)
        if profile is None:
            raise ValueError("Build profile not found.")
    if build.connection_profile_id:
        ssh_profile_service.get_profile(build.connection_profile_id)


def _resolve_vsphere_inventory(
    build: ClusterBuild, nodes_payload: List[Dict[str, Any]]
) -> Dict[str, Dict[str, Any]]:
    """vCenter metadata for every picked VM, keyed by moid."""
    moids = [str(n.get("vsphereVmMoid")) for n in nodes_payload if n.get("vsphereVmMoid")]
    if not (moids and build.vsphere_connection_id):
        return {}
    try:
        inventory = vsphere_service.get_inventory(build.vsphere_connection_id)
    except VSphereError as exc:
        raise ValueError(
            f"vCenter inventory is unavailable ({exc}); the selected VMs "
            "cannot be resolved. Retry when vCenter is reachable, or enter "
            "the nodes as manual hosts."
        ) from exc
    return {item["moid"]: item for item in inventory}


def _node_from_payload(
    build: ClusterBuild,
    payload: Dict[str, Any],
    position: int,
    inventory_by_moid: Dict[str, Dict[str, Any]],
) -> ClusterBuildNode:
    """One validated node. Shared by the draft-time replace and growth append
    paths so a machine added later is resolved exactly like an original one."""
    role = str(payload.get("role", "")).strip()
    if role not in _ROLES:
        raise ValueError(
            f"node role must be one of: {', '.join(sorted(_ROLES))}."
        )
    node = ClusterBuildNode(role=role, position=position)
    moid = str(payload.get("vsphereVmMoid") or "").strip()
    vm = inventory_by_moid.get(moid) if moid else None
    if moid and build.vsphere_connection_id and vm is None:
        raise ValueError(
            f"VM '{moid}' is no longer in the vCenter inventory — refresh "
            "the VM picker and re-select it."
        )
    if vm:
        node.vsphere_vm_moid = moid
        node.vsphere_vm_name = vm.get("name")
        node.vsphere_host = vm.get("esxiHost")
        node.vsphere_datastore = vm.get("datastore")
        node.vsphere_tools_status = vm.get("toolsRunState")
        node.vsphere_power_state = vm.get("powerState")
        node.vsphere_cpu = vm.get("cpuCount")
        node.vsphere_memory_mb = vm.get("memoryMiB")
        node.hostname = str(
            payload.get("hostname") or vm.get("guestHostname") or vm.get("name") or ""
        ).strip()
    else:
        node.hostname = str(payload.get("hostname") or "").strip()
    if node.hostname:
        node.hostname = _validate_dns_subdomain(node.hostname, "node hostname")

    manual_address = str(payload.get("address") or "").strip()
    tools_ip = (vm or {}).get("guestIp")
    if manual_address:
        node.address = _validate_node_address(manual_address)
        node.address_source = "manual"
    elif tools_ip:
        node.address = _validate_node_address(str(tools_ip))
        node.address_source = "vmware_tools"
    else:
        raise ValueError(
            f"Node '{node.hostname or moid or position}': no address. VMware "
            "Tools reported no IP — enter a manual management IP."
        )

    if payload.get("connectionProfileId"):
        profile_id = int(payload["connectionProfileId"])
        ssh_profile_service.get_profile(profile_id)
        node.connection_profile_id = profile_id
    return node


def _apply_nodes_payload(build: ClusterBuild, nodes_payload: List[Dict[str, Any]]) -> None:
    """Replace the node set (draft-time only). vSphere metadata is captured
    from the connection's inventory when a moid is given; Tools-less VMs need
    a manual address."""
    inventory_by_moid = _resolve_vsphere_inventory(build, nodes_payload)

    seen_primary_cp = False
    seen_lb_master = False
    new_nodes: List[ClusterBuildNode] = []
    for position, payload in enumerate(nodes_payload):
        node = _node_from_payload(build, payload, position, inventory_by_moid)
        if node.role == "control_plane" and payload.get("isPrimaryCp") and not seen_primary_cp:
            node.is_primary_cp = True
            seen_primary_cp = True
        if node.role == "loadbalancer" and payload.get("isLbMaster") and not seen_lb_master:
            node.is_lb_master = True
            seen_lb_master = True
        new_nodes.append(node)

    # Defaults: first CP is primary, first LB is VRRP master.
    if not seen_primary_cp:
        for node in new_nodes:
            if node.role == "control_plane":
                node.is_primary_cp = True
                break
    if not seen_lb_master:
        for node in new_nodes:
            if node.role == "loadbalancer":
                node.is_lb_master = True
                break

    addresses = [n.address for n in new_nodes]
    if len(set(addresses)) != len(addresses):
        raise ValueError("Node addresses must be unique.")

    build.nodes = new_nodes


def create_build(payload: Dict[str, Any], created_by: str = "") -> Dict[str, Any]:
    build = ClusterBuild(created_by=created_by or None)
    _apply_build_payload(build, payload)
    nodes_payload = payload.get("nodes") or []
    if nodes_payload:
        _apply_nodes_payload(build, nodes_payload)
        _validate_topology(
            build,
            [
                {"role": n.role, "hostname": n.hostname, "address": n.address}
                for n in build.nodes
            ],
        )
    db.session.add(build)
    db.session.commit()
    return serialize_build(build, include_detail=True)


def update_build(build_id: int, payload: Dict[str, Any]) -> Dict[str, Any]:
    build = get_build(build_id)
    if build.status not in _EDITABLE_STATUSES:
        raise ValueError(f"Build cannot be edited in status '{build.status}'.")
    _apply_build_payload(build, payload)
    if "nodes" in payload:
        _apply_nodes_payload(build, payload.get("nodes") or [])
    if build.nodes:
        _validate_topology(
            build,
            [
                {"role": n.role, "hostname": n.hostname, "address": n.address}
                for n in build.nodes
            ],
        )
    # Any edit invalidates a previous preflight verdict.
    build.status = "draft"
    db.session.commit()
    return serialize_build(build, include_detail=True)


def delete_build(build_id: int) -> None:
    build = get_build(build_id)
    if build.status == "building":
        raise ValueError("Cancel the build before deleting it.")
    db.session.delete(build)
    db.session.commit()


# ---------------------------------------------------------------------------
# Lifecycle
# ---------------------------------------------------------------------------

def run_preflight(build_id: int) -> Dict[str, Any]:
    build = get_build(build_id)
    if build.status in ("building", "preflighting"):
        raise ValueError("Build is already running.")
    if not build.nodes:
        raise ValueError("Add nodes before running preflight.")
    warnings = _validate_topology(
        build,
        [
            {"role": n.role, "hostname": n.hostname, "address": n.address}
            for n in build.nodes
        ],
    )

    build.status = "preflighting"
    db.session.commit()

    try:
        profile_row = (
            db.session.get(BuildProfile, build.build_profile_id)
            if build.build_profile_id else None
        )
        resolved = resolve_profile(profile_row)

        # Resolve and integrity-check the selected CNI before changing any
        # node. The same process-local cache is reused by image pre-pull and
        # apply, while the digest protects fetches after a backend restart.
        cni_descriptor = cni_registry.get(build.cni_plugin)
        if cni_descriptor is None:
            raise ValueError(
                f"Selected CNI plugin '{build.cni_plugin}' is unavailable."
            )
        cni_descriptor.render(
            build.cni_version or cni_descriptor.versions[0],
            build.pod_cidr,
            resolved,
        )

        # Fail in preflight, before any node is changed, when a selected
        # add-on's pinned manifest is unavailable in the chosen repo mode.
        for selection in build.addons_json or []:
            descriptor = addon_registry.get(str(selection.get("id") or ""))
            if descriptor is None:
                raise ValueError(
                    f"Selected add-on '{selection.get('id')}' is unavailable."
                )
            descriptor.render(
                str(selection.get("version") or descriptor.versions[0]),
                resolved,
            )

        # The exact pinned version is judged before any node is touched: an
        # unsupported version/CNI/add-on combination must fail here rather than
        # part-way through cluster creation.
        build_checks = preflight.version_checks(build, resolved)

        vsphere_results = preflight.vsphere_checks(build)
        node_results = preflight.run_node_preflight(
            build, resolved, lambda node: executor._target_for(build, node)
        )
        merged = preflight.merge_preflight(
            build, vsphere_results, node_results, build_checks=build_checks
        )
        merged["topologyWarnings"] = warnings
        merged["buildChecks"] = build_checks
    except Exception as exc:
        # Never strand the build in 'preflighting' — that status blocks both
        # editing and re-running from the wizard.
        db.session.rollback()
        build.status = "preflight_failed"
        safe_error = scrub(str(exc))
        build.error = f"Preflight crashed: {safe_error}"[:2000]
        db.session.commit()
        raise ValueError(f"Preflight failed to run: {safe_error}") from exc

    build.status = "preflight_passed" if merged["status"] != "fail" else "preflight_failed"
    build.error = None
    db.session.commit()
    return merged


def start_build(build_id: int, *, ack_warnings: Optional[List[str]] = None,
                actor: str = "") -> Dict[str, Any]:
    build = get_build(build_id)
    if build.status == "building":
        raise ValueError("Build is already running.")
    if build.status not in ("preflight_passed",):
        raise ValueError(
            "Preflight must pass before starting (current status: "
            f"'{build.status}'). Warnings can be acknowledged; failures cannot."
        )
    has_warnings = any(
        (n.preflight_json or {}).get("status") == "warn" for n in build.nodes
    )
    if has_warnings and not ack_warnings:
        raise ValueError(
            "Preflight raised warnings; pass ackWarnings to acknowledge them."
        )
    if ack_warnings:
        build.warnings_ack_json = {
            "acknowledgedBy": actor,
            "acknowledgedAt": datetime.now(timezone.utc).isoformat(),
            "notes": ack_warnings,
        }
    build.status = "building"
    build.error = None
    build.started_at = build.started_at or datetime.now(timezone.utc)
    build.finished_at = None
    db.session.commit()
    executor.start_build_worker(build.id)
    # Under TESTING the worker runs synchronously in a nested app context with
    # its own session — refresh so the response reflects the outcome.
    db.session.refresh(build)
    return serialize_build(build, include_detail=True)


# ---------------------------------------------------------------------------
# Day two: growing a finished cluster
#
# The phase machine is already node-scoped and skips completed steps, so growth
# reuses it wholesale rather than duplicating a join path: base_prep,
# pull_images and join_workers pick up only the new machines, and init, CNI,
# join_cp, onboard and add-ons all no-op. Only 'verify' is deliberately reopened
# so the cluster is re-checked with the new machine in it.
# ---------------------------------------------------------------------------

def growth_nodes(build: ClusterBuild) -> List[ClusterBuildNode]:
    """Machines added to a finished build that have not joined yet."""
    return [
        node for node in build.nodes
        if node.status in ("pending", "preflight_passed", "preflight_failed")
    ]


def _require_growable(build: ClusterBuild) -> None:
    if build.status != "completed":
        raise ValueError(
            "Only a completed build can be grown (current status: "
            f"'{build.status}')."
        )
    if not build.result_cluster_id:
        raise ValueError(
            "This build never registered a cluster, so there is nothing to grow."
        )


def add_worker_nodes(build_id: int, nodes_payload: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Attach new worker machines to a finished build, ready for preflight.

    Workers only. Growing the control plane changes etcd quorum arithmetic
    (3 → 4 members is *worse* than 3) and adding a load balancer means
    re-forming VRRP on a live VIP; neither is a safe side effect of an
    "add machines" button, so both are refused with a reason.
    """
    build = get_build(build_id)
    _require_growable(build)
    if not nodes_payload:
        raise ValueError("Select at least one machine to add.")

    for payload in nodes_payload:
        role = str(payload.get("role") or "worker").strip() or "worker"
        if role != "worker":
            raise ValueError(
                "Only workers can be added to a running cluster. Changing the "
                "control-plane or load-balancer tier of a live cluster is a "
                "separate operation, because it re-forms etcd quorum or the VIP."
            )

    inventory_by_moid = _resolve_vsphere_inventory(build, nodes_payload)
    existing_addresses = {node.address for node in build.nodes}
    existing_moids = {node.vsphere_vm_moid for node in build.nodes if node.vsphere_vm_moid}
    next_position = max((node.position for node in build.nodes), default=-1) + 1

    added: List[ClusterBuildNode] = []
    for offset, payload in enumerate(nodes_payload):
        node = _node_from_payload(
            build, {**payload, "role": "worker"}, next_position + offset, inventory_by_moid
        )
        if node.address in existing_addresses:
            raise ValueError(
                f"{node.hostname or node.address} is already part of this cluster."
            )
        if node.vsphere_vm_moid and node.vsphere_vm_moid in existing_moids:
            raise ValueError(
                f"{node.hostname or node.address} is already part of this cluster."
            )
        existing_addresses.add(node.address)
        if node.vsphere_vm_moid:
            existing_moids.add(node.vsphere_vm_moid)
        node.build_id = build.id
        node.status = "pending"
        db.session.add(node)
        added.append(node)

    db.session.commit()
    return serialize_build(build, include_detail=True)


def remove_growth_node(build_id: int, node_id: int) -> Dict[str, Any]:
    """Drop a machine that was added for growth but has not joined."""
    build = get_build(build_id)
    node = db.session.get(ClusterBuildNode, node_id)
    if node is None or node.build_id != build.id:
        raise LookupError("Node not found on this build.")
    if node not in growth_nodes(build):
        raise ValueError(
            "Only a machine that has not joined yet can be removed here."
        )
    db.session.delete(node)
    db.session.commit()
    db.session.refresh(build)
    return serialize_build(build, include_detail=True)


def preflight_growth(build_id: int) -> Dict[str, Any]:
    """Preflight only the machines being added.

    The build keeps its 'completed' status throughout: a live cluster is not
    "preflighting", and the wizard's status transitions would strand it.
    """
    build = get_build(build_id)
    _require_growable(build)
    pending = growth_nodes(build)
    if not pending:
        raise ValueError("Add machines before running preflight.")

    profile_row = (
        db.session.get(BuildProfile, build.build_profile_id)
        if build.build_profile_id else None
    )
    resolved = resolve_profile(profile_row)
    try:
        # A machine joining a live cluster must satisfy the same version policy
        # the cluster was built under — its pinned version, unchanged.
        build_checks = preflight.version_checks(build, resolved)
        vsphere_results = preflight.vsphere_checks(build, nodes=pending)
        node_results = preflight.run_node_preflight(
            build, resolved, lambda node: executor._target_for(build, node),
            nodes=pending,
        )
        merged = preflight.merge_preflight(
            build, vsphere_results, node_results, nodes=pending,
            build_checks=build_checks,
        )
    except Exception as exc:
        db.session.rollback()
        safe_error = scrub(str(exc))
        raise ValueError(f"Preflight failed to run: {safe_error}") from exc

    merged["topologyWarnings"] = []
    merged["buildChecks"] = build_checks
    db.session.commit()
    return merged


def grow_build(build_id: int, *, ack_warnings: Optional[List[str]] = None,
               actor: str = "") -> Dict[str, Any]:
    """Run the phase machine again to prepare and join the added machines."""
    build = get_build(build_id)
    _require_growable(build)
    pending = growth_nodes(build)
    if not pending:
        raise ValueError("Add machines before growing the cluster.")

    unchecked = [n for n in pending if n.status == "pending"]
    if unchecked:
        raise ValueError(
            "Run preflight on the new machines before growing the cluster."
        )
    failed = [n for n in pending if n.status == "preflight_failed"]
    if failed:
        names = ", ".join(n.hostname or n.address for n in failed)
        raise ValueError(f"Preflight failed on: {names}. Fix and re-run.")
    has_warnings = any(
        (n.preflight_json or {}).get("status") == "warn" for n in pending
    )
    if has_warnings and not ack_warnings:
        raise ValueError(
            "Preflight raised warnings; pass ackWarnings to acknowledge them."
        )
    if ack_warnings:
        build.warnings_ack_json = {
            "acknowledgedBy": actor,
            "acknowledgedAt": datetime.now(timezone.utc).isoformat(),
            "notes": ack_warnings,
        }

    # Reopen verification so the cluster is re-checked with the new machines in
    # it. Everything else that already completed stays completed and is skipped.
    ClusterBuildStep.query.filter(
        ClusterBuildStep.build_id == build.id,
        ClusterBuildStep.phase == "verify",
    ).update(
        {"status": "pending", "error": None, "started_at": None, "finished_at": None},
        synchronize_session=False,
    )

    build.status = "building"
    build.error = None
    build.growth_started_at = datetime.now(timezone.utc)
    build.finished_at = None
    db.session.commit()
    executor.start_build_worker(build.id)
    db.session.refresh(build)
    return serialize_build(build, include_detail=True)


# ---------------------------------------------------------------------------
# Kubeconfig
# ---------------------------------------------------------------------------

def build_kubeconfig(build_id: int) -> Dict[str, Any]:
    """The cluster-admin kubeconfig of the cluster this build produced.

    This is full control of the cluster, outside KubeSight's own RBAC, for as
    long as the certificate lives — hence its own permission and an audit entry
    at the route.
    """
    from ...cluster_store import get_active_cluster_by_public_id, read_kubeconfig_file

    build = get_build(build_id)
    if build.status != "completed" or not build.result_cluster_id:
        raise ValueError(
            "This build has not produced a cluster, so it has no kubeconfig."
        )
    cluster = get_active_cluster_by_public_id(build.result_cluster_id)
    if cluster is None:
        raise LookupError(
            "The cluster this build registered is no longer present in KubeSight."
        )
    try:
        content = read_kubeconfig_file(cluster.id)
    except (OSError, ValueError) as exc:
        raise LookupError(f"The kubeconfig file could not be read: {exc}") from exc
    if not content.strip():
        raise LookupError("The stored kubeconfig is empty.")
    # A suggested download name, so it must not carry path components: dots are
    # legal in a cluster name but ".." is not something to hand to a filesystem.
    safe_name = re.sub(r"[^A-Za-z0-9._-]+", "-", build.name or "cluster")
    safe_name = re.sub(r"\.{2,}", ".", safe_name).strip("-._")
    return {
        "filename": f"{safe_name or 'cluster'}-kubeconfig.yaml",
        "content": content,
        "clusterId": build.result_cluster_id,
    }


def retry_build(build_id: int) -> Dict[str, Any]:
    build = get_build(build_id)
    # Cancelled is resumable too: cancelling stops the phase machine but leaves
    # completed phases intact, and without this a cancelled build is a dead end
    # that can only be deleted.
    if build.status not in ("failed", "cancelled"):
        raise ValueError("Only failed or cancelled builds can be retried.")
    reset_labels = executor.reset_failed_nodes(build)
    # Reset both failed steps and any left stuck in 'running' (a phase that
    # crashed on an unexpected error, e.g. a mid-command connection drop, never
    # got to mark its step failed). Completed steps stay completed → resume.
    ClusterBuildStep.query.filter(
        ClusterBuildStep.build_id == build.id,
        ClusterBuildStep.status.in_(("failed", "running")),
    ).update(
        {"status": "pending", "error": None,
         "attempt": ClusterBuildStep.attempt + 1},
        synchronize_session=False,
    )
    build.status = "building"
    build.error = None
    build.finished_at = None
    db.session.commit()
    executor.start_build_worker(build.id)
    db.session.refresh(build)
    result = serialize_build(build, include_detail=True)
    result["resetNodes"] = reset_labels
    return result


def cancel_build(build_id: int) -> Dict[str, Any]:
    build = get_build(build_id)
    if build.status not in ("building", "preflighting"):
        raise ValueError("Only a running build can be cancelled.")
    build.status = "cancelled"
    db.session.commit()
    return serialize_build(build)


def build_logs(build_id: int, *, node_id: Optional[int] = None) -> List[Dict[str, Any]]:
    build = get_build(build_id)
    steps = build.steps
    if node_id is not None:
        steps = [s for s in steps if s.node_id == node_id]
    return [
        {**serialize_step(step), "logTail": step.log_tail or ""}
        for step in steps
    ]


def wizard_options() -> Dict[str, Any]:
    # One discovery pass feeds both the list and its provenance.
    releases = k8s_versions.releases()
    return {
        # Contract: a plain string[] of exact patch versions, newest first.
        # Additive metadata lives alongside it in k8sVersionInfo.
        "k8sVersions": k8s_versions.supported_versions(releases),
        "k8sVersionInfo": k8s_versions.version_metadata(releases),
        "cniPlugins": cni_registry.catalog(),
        "addons": addon_registry.catalog(),
        "osMatrix": os_adapters.supported_matrix(),
        "endpointModes": [
            {"id": "managed_haproxy",
             "label": "KubeSight-managed HAProxy + Keepalived",
             "description": "1 LB for a single-control-plane lab, or 2 LBs for "
                            "HA; KubeSight installs and manages the VIP.",
             "default": True},
            {"id": "external_lb", "label": "Existing external load balancer",
             "description": "You supply a VIP/DNS already load-balancing :6443.",
             "default": False},
            {"id": "manual_endpoint", "label": "Manual API endpoint",
             "description": "A host:port KubeSight does not manage. Validated "
                            "for reachability only.",
             "default": False},
        ],
        "topologies": [
            {"id": "stacked_ha", "label": "Highly available (recommended)",
             "shape": "2 load balancers · 3 control planes · N workers"},
            {"id": "single_cp", "label": "Single control plane",
             "shape": "1 control plane · 1 managed LB when selected · N workers"},
        ],
        "defaults": {"podCidr": "10.244.0.0/16", "serviceCidr": "10.96.0.0/12"},
    }
