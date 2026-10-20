"""Which ESXi host each new VM is cloned onto.

Without a host, vCenter picks one. On a cluster where some hosts lack the
storage array's VAAI plugin, a clone that lands there is a full copy (minutes)
instead of an offloaded one (seconds). So a vCenter connection may name, per
vSphere cluster, the hosts KubeSight may use ("Allowed ESXi hosts"), and the
plan spreads new VMs over the ones that offload clones to the datastore.

A host is a name or a managed-object id. Placement only ever applies to VMs
not created yet: a created VM stays where it is (OpenTofu ignores
``host_system_id`` after creation, so a vMotion never plans a rebuild).
"""

from __future__ import annotations

from typing import Any, Dict, Iterable, List, Optional, Tuple

SUPPORTED = "vStorageSupported"
UNSUPPORTED = "vStorageUnsupported"
CHECK_LABEL = "Fast clone (VAAI) on target hosts"
PLACEMENT_LABEL = "ESXi hosts"


def normalize_allowed(raw: Any) -> Optional[Dict[str, List[str]]]:
    """``{clusterId: [host name or id, ...]}`` from the API; None when empty."""
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise ValueError("allowedHosts must map a vSphere cluster id to a list of ESXi hosts.")
    out: Dict[str, List[str]] = {}
    for cluster_id, hosts in raw.items():
        key = str(cluster_id or "").strip()
        if not key:
            continue
        if isinstance(hosts, str):
            hosts = hosts.replace(";", ",").replace("\n", ",").split(",")
        if not isinstance(hosts, (list, tuple)):
            raise ValueError(f"allowedHosts for {key} must be a list of ESXi host names or ids.")
        items: List[str] = []
        for host in hosts:
            text = str(host or "").strip()
            if text and text not in items:
                items.append(text[:255])
        if len(items) > 64:
            raise ValueError("At most 64 allowed ESXi hosts per vSphere cluster.")
        if items:
            out[key[:64]] = items
    return out or None


def allowed_for(allowed: Optional[Dict[str, List[str]]], cluster_id: Optional[str]) -> List[str]:
    return list((allowed or {}).get(str(cluster_id or "")) or [])


def _usable(host: Dict[str, Any]) -> bool:
    return host.get("connected", True) and not host.get("maintenance")


def candidates(cluster: Dict[str, Any], allowed: List[str]) -> Tuple[List[Dict[str, Any]], List[str], List[str]]:
    """(hosts that may take a new VM, allowed entries matching no host in the
    cluster, allowed hosts left out because disconnected or in maintenance).

    With no allowed list every usable host of the cluster is a candidate.
    """
    hosts = list(cluster.get("hosts") or [])
    if not allowed:
        return [h for h in hosts if _usable(h)], [], []
    picked: List[Dict[str, Any]] = []
    unknown: List[str] = []
    for ref in allowed:
        host = next((h for h in hosts if ref in (h.get("id"), h.get("name"))), None)
        if host is None:
            unknown.append(ref)
        elif host not in picked:
            picked.append(host)
    down = [h["name"] for h in picked if not _usable(h)]
    return [h for h in picked if _usable(h)], unknown, down


def offload(host_id: str, datastore_ids: Iterable[str],
            datastores: Dict[str, Dict[str, Any]]) -> List[Tuple[str, Optional[str]]]:
    """[(datastore name, vStorageSupport)] for every datastore the host does not
    offload clones to. A datastore the host does not mount reads "notMounted"."""
    out: List[Tuple[str, Optional[str]]] = []
    for datastore_id in datastore_ids:
        datastore = datastores.get(datastore_id)
        if datastore is None:
            continue
        mount = next((m for m in datastore.get("hostMounts") or [] if m.get("hostId") == host_id), None)
        if mount is None:
            if datastore.get("hostMounts") is not None:
                out.append((datastore.get("name") or datastore_id, "notMounted"))
            continue
        support = mount.get("vStorageSupport")
        if support != SUPPORTED:
            out.append((datastore.get("name") or datastore_id, support))
    return out


def blocks_placement(problems: List[Tuple[str, Optional[str]]]) -> bool:
    """A host is skipped when it says plainly that it cannot offload (or does
    not mount the datastore). "Unknown" is only warned about."""
    return any(support in (UNSUPPORTED, "notMounted") for _, support in problems)


def assign(names: List[str], hosts: List[Dict[str, Any]], load: Dict[str, int]) -> Dict[str, Dict[str, Any]]:
    """Each name onto the least-used host, in list order on a tie: round-robin
    from an empty cluster, and growth fills the emptiest hosts first. Machines
    come in role order, so consecutive control planes land on different hosts."""
    counts = {h["id"]: int(load.get(h["id"], 0)) for h in hosts}
    order = {h["id"]: index for index, h in enumerate(hosts)}
    out: Dict[str, Dict[str, Any]] = {}
    for name in names:
        host = min(hosts, key=lambda h: (counts[h["id"]], order[h["id"]]))
        counts[host["id"]] += 1
        out[name] = host
    return out


def describe(problems: List[Tuple[str, Optional[str]]]) -> str:
    parts = []
    for name, support in problems:
        if support == "notMounted":
            parts.append(f"{name} (not mounted)")
        elif support == UNSUPPORTED:
            parts.append(name)
        else:
            parts.append(f"{name} (offload {support or 'not reported'})")
    return ", ".join(parts)
