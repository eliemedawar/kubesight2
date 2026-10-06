"""Cluster templates: the shapes the wizard starts from.

The built-in ones are code, so they cannot be edited away and every
installation offers the same three. Admins save their own from any build; those
are ``ClusterTemplate`` rows. Both carry the same spec:

    {"counts": {"loadbalancer": 1, "controlPlane": 1, "worker": 2},
     "sizes": {"loadbalancer": {"cpu": 2, "memoryGb": 2, "diskGb": 40}, ...},
     "network": {"cniPlugin": "calico", "podCidr": ..., "serviceCidr": ...},
     "addons": [{"id": "metallb", "config": {...}}]}

A template never says where in vCenter the machines go or which addresses they
get. That is the build's decision, so one template works on any vCenter.
"""

from __future__ import annotations

import ipaddress
from typing import Any, Dict, List, Optional

from ....db import db
from ....models import ClusterTemplate

ROLES = ("loadbalancer", "controlPlane", "worker")
ROLE_TO_NODE_ROLE = {
    "loadbalancer": "loadbalancer",
    "controlPlane": "control_plane",
    "worker": "worker",
}
# Short role names used in VM names: <cluster>-cp-1, <cluster>-wk-3, <cluster>-lb-1.
ROLE_SHORT = {"loadbalancer": "lb", "controlPlane": "cp", "worker": "wk"}

# The smallest machine each role is allowed. Below these, kubeadm's own
# preflight refuses a control plane (2 vCPU) and a node has no room to run.
MINIMUM_SIZES = {
    "loadbalancer": {"cpu": 1, "memoryGb": 2, "diskGb": 20},
    "controlPlane": {"cpu": 2, "memoryGb": 4, "diskGb": 40},
    "worker": {"cpu": 2, "memoryGb": 4, "diskGb": 40},
}
MAXIMUM_SIZE = {"cpu": 64, "memoryGb": 512, "diskGb": 4096}
MAX_WORKERS = 50

BUILTIN_TEMPLATES: List[Dict[str, Any]] = [
    {
        "id": "lab",
        "name": "Lab",
        "description": (
            "Try a Kubernetes version, test a chart, break things. One machine "
            "failing takes the cluster down."
        ),
        "useFor": "Labs and spikes",
        "survives": "Nothing",
        "spec": {
            "counts": {"loadbalancer": 0, "controlPlane": 1, "worker": 1},
            "sizes": {
                "loadbalancer": {"cpu": 2, "memoryGb": 2, "diskGb": 40},
                "controlPlane": {"cpu": 2, "memoryGb": 4, "diskGb": 60},
                "worker": {"cpu": 4, "memoryGb": 8, "diskGb": 80},
            },
        },
    },
    {
        "id": "small",
        "name": "Small",
        "description": (
            "Dev and UAT. HAProxy in front, so the API address stays the same if "
            "this grows into a highly available cluster later."
        ),
        "useFor": "Dev and UAT",
        "survives": "Losing a worker",
        "spec": {
            "counts": {"loadbalancer": 1, "controlPlane": 1, "worker": 2},
            "sizes": {
                "loadbalancer": {"cpu": 2, "memoryGb": 2, "diskGb": 40},
                "controlPlane": {"cpu": 4, "memoryGb": 8, "diskGb": 80},
                "worker": {"cpu": 4, "memoryGb": 8, "diskGb": 100},
            },
        },
    },
    {
        "id": "standard-ha",
        "name": "Standard HA",
        "description": (
            "Production. Survives losing one control plane and one load balancer; "
            "the API address floats between the two balancers."
        ),
        "useFor": "Production",
        "survives": "1 control plane and 1 load balancer",
        "spec": {
            "counts": {"loadbalancer": 2, "controlPlane": 3, "worker": 3},
            "sizes": {
                "loadbalancer": {"cpu": 2, "memoryGb": 4, "diskGb": 40},
                "controlPlane": {"cpu": 4, "memoryGb": 8, "diskGb": 80},
                "worker": {"cpu": 8, "memoryGb": 16, "diskGb": 120},
            },
        },
    },
]
_BUILTIN_BY_ID = {item["id"]: item for item in BUILTIN_TEMPLATES}


# ---------------------------------------------------------------------------
# The rules every shape obeys
# ---------------------------------------------------------------------------

def _int(value: Any, field: str) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field} must be a whole number.") from exc
    if isinstance(value, float) and value != number:
        raise ValueError(f"{field} must be a whole number.")
    return number


def normalize_counts(raw: Any) -> Dict[str, int]:
    """Role counts, checked against etcd and load-balancer arithmetic.

    1, 3 or 5 control planes (an even number cannot keep etcd quorum, and 2 is
    *less* safe than 1). One control plane takes 0 or 1 load balancer; several
    need exactly 2, because the floating API address is a keepalived pair.
    """
    raw = raw if isinstance(raw, dict) else {}
    counts = {role: _int(raw.get(role, 0), f"counts.{role}") for role in ROLES}
    if counts["controlPlane"] not in (1, 3, 5):
        raise ValueError(
            "Control planes must be 1, 3 or 5 — etcd needs an odd number of "
            "members to keep quorum, and 2 is less safe than 1."
        )
    if counts["controlPlane"] == 1 and counts["loadbalancer"] not in (0, 1):
        raise ValueError("One control plane takes 0 or 1 load balancer.")
    if counts["controlPlane"] > 1 and counts["loadbalancer"] != 2:
        raise ValueError(
            "Highly available control planes need exactly 2 load balancers for "
            "the floating API address."
        )
    if counts["worker"] < 1:
        raise ValueError("A cluster needs at least 1 worker.")
    if counts["worker"] > MAX_WORKERS:
        raise ValueError(f"At most {MAX_WORKERS} workers in one build.")
    return counts


def normalize_sizes(raw: Any, counts: Optional[Dict[str, int]] = None) -> Dict[str, Dict[str, int]]:
    """Per-role CPU / memory / disk, held to each role's minimum.

    A role with no machines still carries a size (so a template can be grown
    into later), but only roles that will exist are held to the minimums.
    """
    raw = raw if isinstance(raw, dict) else {}
    sizes: Dict[str, Dict[str, int]] = {}
    for role in ROLES:
        entry = raw.get(role) if isinstance(raw.get(role), dict) else {}
        minimum = MINIMUM_SIZES[role]
        size = {
            key: _int(entry.get(key, minimum[key]), f"sizes.{role}.{key}")
            for key in ("cpu", "memoryGb", "diskGb")
        }
        if counts is None or counts.get(role, 0) > 0:
            label = {"loadbalancer": "Load balancers", "controlPlane": "Control planes",
                     "worker": "Workers"}[role]
            for key, unit in (("cpu", "vCPU"), ("memoryGb", "GB of memory"),
                              ("diskGb", "GB of disk")):
                if size[key] < minimum[key]:
                    raise ValueError(f"{label} need at least {minimum[key]} {unit}.")
                if size[key] > MAXIMUM_SIZE[key]:
                    raise ValueError(
                        f"{label}: {size[key]} {unit} is more than the "
                        f"{MAXIMUM_SIZE[key]} a single VM may have here."
                    )
        sizes[role] = size
    return sizes


def topology_for(counts: Dict[str, int]) -> Dict[str, str]:
    """How the Cluster Builder's existing topology fields read for a shape."""
    if counts["controlPlane"] == 1:
        if counts["loadbalancer"] == 1:
            return {"topologyType": "single_cp", "endpointMode": "managed_haproxy"}
        return {"topologyType": "single_cp", "endpointMode": "manual_endpoint"}
    return {"topologyType": "stacked_ha", "endpointMode": "managed_haproxy"}


def _normalize_network(raw: Any) -> Dict[str, str]:
    raw = raw if isinstance(raw, dict) else {}
    network: Dict[str, str] = {}
    if raw.get("cniPlugin"):
        network["cniPlugin"] = str(raw["cniPlugin"]).strip()[:24]
    for key in ("podCidr", "serviceCidr"):
        if raw.get(key):
            value = str(raw[key]).strip()
            try:
                ipaddress.ip_network(value)
            except ValueError as exc:
                raise ValueError(f"network.{key} must be a CIDR.") from exc
            network[key] = value
    return network


def _normalize_addons(raw: Any) -> List[Dict[str, Any]]:
    """Add-on ids (and their config) only. Versions are chosen again for the
    Kubernetes release of whichever build uses the template."""
    items = []
    for entry in raw or []:
        if isinstance(entry, str):
            entry = {"id": entry}
        if not isinstance(entry, dict) or not entry.get("id"):
            continue
        item: Dict[str, Any] = {"id": str(entry["id"]).strip()[:64]}
        if isinstance(entry.get("config"), dict) and entry["config"]:
            item["config"] = entry["config"]
        items.append(item)
    return items


def normalize_spec(raw: Any) -> Dict[str, Any]:
    raw = raw if isinstance(raw, dict) else {}
    counts = normalize_counts(raw.get("counts"))
    spec: Dict[str, Any] = {
        "counts": counts,
        "sizes": normalize_sizes(raw.get("sizes"), counts),
    }
    network = _normalize_network(raw.get("network"))
    if network:
        spec["network"] = network
    addons = _normalize_addons(raw.get("addons"))
    if addons:
        spec["addons"] = addons
    return spec


# ---------------------------------------------------------------------------
# Catalog
# ---------------------------------------------------------------------------

def _iso(dt) -> Optional[str]:
    return dt.isoformat() if dt else None


def _builtin_view(item: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "id": item["id"],
        "name": item["name"],
        "description": item["description"],
        "useFor": item["useFor"],
        "survives": item["survives"],
        "builtin": True,
        **item["spec"],
        **topology_for(item["spec"]["counts"]),
    }


def serialize(row: ClusterTemplate) -> Dict[str, Any]:
    spec = row.spec_json or {}
    counts = spec.get("counts") or {}
    view = {
        "id": f"custom:{row.id}",
        "dbId": row.id,
        "name": row.name,
        "description": row.description or "",
        "builtin": False,
        "counts": counts,
        "sizes": spec.get("sizes") or {},
        "network": spec.get("network") or {},
        "addons": spec.get("addons") or [],
        "createdBy": row.created_by,
        "createdAt": _iso(row.created_at),
        "updatedAt": _iso(row.updated_at),
    }
    try:
        view.update(topology_for(normalize_counts(counts)))
    except ValueError:
        pass
    return view


def catalog() -> Dict[str, Any]:
    rows = ClusterTemplate.query.order_by(ClusterTemplate.name.asc()).all()
    return {
        "builtin": [_builtin_view(item) for item in BUILTIN_TEMPLATES],
        "custom": [serialize(row) for row in rows],
        "minimumSizes": MINIMUM_SIZES,
        "maximumSize": MAXIMUM_SIZE,
        "maxWorkers": MAX_WORKERS,
    }


def is_known_template_id(template_id: str) -> bool:
    template_id = str(template_id or "")
    if template_id in _BUILTIN_BY_ID or template_id == "custom":
        return True
    if template_id.startswith("custom:"):
        try:
            return db.session.get(ClusterTemplate, int(template_id.split(":", 1)[1])) is not None
        except ValueError:
            return False
    return False


def _name(value: Any) -> str:
    name = str(value or "").strip()
    if not name:
        raise ValueError("A template needs a name.")
    if len(name) > 120 or any(ord(char) < 32 for char in name):
        raise ValueError("Template names are at most 120 printable characters.")
    if name.lower() in {item["name"].lower() for item in BUILTIN_TEMPLATES}:
        raise ValueError(f"“{name}” is a built-in template's name. Choose another.")
    return name


def _get(template_db_id: int) -> ClusterTemplate:
    row = db.session.get(ClusterTemplate, template_db_id)
    if row is None:
        raise LookupError("Template not found.")
    return row


def spec_from_build(build) -> Dict[str, Any]:
    """What a finished (or drafted) build would save as a template."""
    provisioning = build.provisioning_json or {}
    counts = provisioning.get("counts")
    if not counts:
        by_role = {"loadbalancer": 0, "control_plane": 0, "worker": 0}
        for node in build.nodes:
            if node.role in by_role:
                by_role[node.role] += 1
        counts = {
            "loadbalancer": by_role["loadbalancer"],
            "controlPlane": by_role["control_plane"],
            "worker": by_role["worker"],
        }
    sizes = provisioning.get("sizes")
    if not sizes:
        sizes = {}
        for role, node_role in ROLE_TO_NODE_ROLE.items():
            node = next((n for n in build.nodes if n.role == node_role and n.vsphere_cpu), None)
            if node is not None:
                sizes[role] = {
                    "cpu": node.vsphere_cpu,
                    "memoryGb": max(int((node.vsphere_memory_mb or 0) / 1024), 1),
                    "diskGb": MINIMUM_SIZES[role]["diskGb"],
                }
    return normalize_spec({
        "counts": counts,
        "sizes": sizes,
        "network": {
            "cniPlugin": build.cni_plugin,
            "podCidr": build.pod_cidr,
            "serviceCidr": build.service_cidr,
        },
        "addons": [
            {"id": item.get("id"), "config": item.get("config")}
            for item in (build.addons_json or [])
            if isinstance(item, dict)
        ],
    })


def create_template(payload: Dict[str, Any], created_by: str = "") -> Dict[str, Any]:
    from .. import service as build_service

    name = _name(payload.get("name"))
    if ClusterTemplate.query.filter(db.func.lower(ClusterTemplate.name) == name.lower()).first():
        raise ValueError(f"A template called “{name}” already exists.")
    if payload.get("fromBuildId"):
        build = build_service.get_build(int(payload["fromBuildId"]))
        spec = spec_from_build(build)
    else:
        spec = normalize_spec(payload.get("spec") or payload)
    row = ClusterTemplate(
        name=name,
        description=str(payload.get("description") or "").strip()[:2000] or None,
        spec_json=spec,
        created_by=created_by or None,
    )
    db.session.add(row)
    db.session.commit()
    return serialize(row)


def update_template(template_db_id: int, payload: Dict[str, Any]) -> Dict[str, Any]:
    row = _get(template_db_id)
    if "name" in payload:
        name = _name(payload.get("name"))
        clash = ClusterTemplate.query.filter(
            db.func.lower(ClusterTemplate.name) == name.lower(),
            ClusterTemplate.id != row.id,
        ).first()
        if clash:
            raise ValueError(f"A template called “{name}” already exists.")
        row.name = name
    if "description" in payload:
        row.description = str(payload.get("description") or "").strip()[:2000] or None
    if "spec" in payload:
        row.spec_json = normalize_spec(payload.get("spec"))
    db.session.commit()
    return serialize(row)


def delete_template(template_db_id: int) -> None:
    row = _get(template_db_id)
    db.session.delete(row)
    db.session.commit()
