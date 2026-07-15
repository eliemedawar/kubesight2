"""Client Service Access Topology — client-specific connectivity overlays.

Each client↔service link carries a single :class:`ClientServiceConnection`
describing how *this* client connects to the service: the direction (client →
service, service → client, or both), the transport (VPN, Leased Line, …), the
source/destination IPs, and **which component(s)** of the service the connection
attaches to. The reusable service topology is never duplicated per client: the
composed client topology simply overlays a client node and a transport node onto
the shared service topology, linking the transport to each selected component
(falling back to the service entrypoint when no components are chosen).
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

from ..audit import log_audit
from ..db import db
from ..models import (
    ApplicationService,
    Client,
    ClientApplicationService,
    ClientServiceConnection,
)

# Allowed transport types (dropdown). "Other" permits a custom transport_name.
TRANSPORT_TYPES = [
    "VPN",
    "Leased Line",
    "MPLS",
    "Internet",
    "Private Link",
    "Direct Connect",
    "Internal Network",
    "Other",
]

_NOT_CONFIGURED = "Not configured"

# Direction of the client↔service connectivity link drawn in the composed
# topology:
#   inbound  → client talks to the service   (client → transport → component)
#   outbound → service talks to the client   (component → transport → client)
#   both     → bidirectional
DIRECTIONS = ["inbound", "outbound", "both"]
_DEFAULT_DIRECTION = "inbound"


# ---------------------------------------------------------------------------
# Serializers
# ---------------------------------------------------------------------------

def _parse_component_refs(raw: Any) -> List[Dict[str, str]]:
    """Decode the stored JSON component_refs into ``[{ref, name}]``.

    Tolerant of legacy/empty values and malformed JSON (returns ``[]``).
    """
    if not raw:
        return []
    try:
        data = raw if isinstance(raw, list) else json.loads(raw)
    except (ValueError, TypeError):
        return []
    out: List[Dict[str, str]] = []
    for item in data or []:
        if isinstance(item, dict) and item.get("ref") is not None:
            out.append({
                "ref": str(item["ref"]),
                "name": str(item.get("name") or item["ref"]),
                "sourceIp": str(item.get("sourceIp") or ""),
                "destinationIp": str(item.get("destinationIp") or ""),
                "nettedSourceIp": str(item.get("nettedSourceIp") or ""),
                "nettedDestinationIp": str(item.get("nettedDestinationIp") or ""),
            })
        elif item is not None:
            out.append({
                "ref": str(item), "name": str(item),
                "sourceIp": "", "destinationIp": "",
                "nettedSourceIp": "", "nettedDestinationIp": "",
            })
    return out


def _connection_to_dict(conn: Optional[ClientServiceConnection]) -> Optional[Dict[str, Any]]:
    if conn is None:
        return None
    return {
        "id": conn.id,
        "clientId": conn.client_id,
        "serviceId": conn.service_id,
        "sourceIp": conn.source_ip or "",
        "destinationIp": conn.destination_ip or "",
        "nettedSourceIp": conn.netted_source_ip or "",
        "nettedDestinationIp": conn.netted_destination_ip or "",
        "transportType": conn.transport_type or "",
        "transportName": conn.transport_name or "",
        "transportNotes": conn.transport_notes or "",
        "clusterId": conn.cluster_id or "",
        "namespace": conn.namespace or "",
        "environment": conn.environment or "",
        "componentRefs": _parse_component_refs(conn.component_refs),
        "direction": conn.direction or _DEFAULT_DIRECTION,
        "status": conn.status or "active",
        "isActive": bool(conn.is_active),
        "createdAt": conn.created_at.isoformat() if conn.created_at else None,
        "updatedAt": conn.updated_at.isoformat() if conn.updated_at else None,
    }


def _connections_by_service(client_id: int) -> Dict[int, ClientServiceConnection]:
    rows = ClientServiceConnection.query.filter_by(client_id=client_id).all()
    return {row.service_id: row for row in rows}


def _service_components(nodes: List[Dict[str, Any]]) -> List[Dict[str, str]]:
    """The service's topology nodes offered as connection targets in the picker."""
    return [
        {
            "ref": str(n.get("id")),
            "name": n.get("name") or str(n.get("id")),
            "type": n.get("type") or "",
        }
        for n in nodes
    ]


# ---------------------------------------------------------------------------
# Validation helpers
# ---------------------------------------------------------------------------

def _clean(value: Any, limit: int) -> Optional[str]:
    text = (str(value).strip() if value is not None else "")
    return text[:limit] or None


def _normalize_transport_type(value: Any) -> Tuple[Optional[str], Optional[str]]:
    """Return (transport_type, error). Empty is allowed (not configured yet)."""
    raw = (str(value).strip() if value is not None else "")
    if not raw:
        return None, None
    match = next((t for t in TRANSPORT_TYPES if t.lower() == raw.lower()), None)
    if not match:
        allowed = ", ".join(TRANSPORT_TYPES)
        return None, f"Invalid transport type. Allowed: {allowed}."
    return match, None


def _normalize_direction(value: Any, default: str = _DEFAULT_DIRECTION) -> Tuple[str, Optional[str]]:
    """Return (direction, error). Empty falls back to ``default``."""
    raw = (str(value).strip().lower() if value is not None else "")
    if not raw:
        return default, None
    if raw not in DIRECTIONS:
        allowed = ", ".join(DIRECTIONS)
        return default, f"Invalid direction. Allowed: {allowed}."
    return raw, None


def _normalize_component_refs(
    value: Any, svc_nodes: List[Dict[str, Any]]
) -> Tuple[Optional[str], Optional[str]]:
    """Validate the requested component refs against the live service topology.

    Returns (json_or_none, error). Empty/absent selection stores ``None`` (the
    composed topology then falls back to the service entrypoint). Every ref must
    match a node id in the service topology.
    """
    if value is None:
        return None, None
    if not isinstance(value, (list, tuple)):
        return None, "componentRefs must be a list."

    # Accept a list of ref strings or of {ref, name, sourceIp, destinationIp,
    # nettedSourceIp, nettedDestinationIp}.
    requested: List[Dict[str, Any]] = []
    for item in value:
        if isinstance(item, dict):
            ref = item.get("ref")
            src = item.get("sourceIp")
            dst = item.get("destinationIp")
            nsrc = item.get("nettedSourceIp")
            ndst = item.get("nettedDestinationIp")
        else:
            ref, src, dst, nsrc, ndst = item, None, None, None, None
        if ref is None or str(ref).strip() == "":
            continue
        requested.append({
            "ref": str(ref).strip(),
            "sourceIp": src, "destinationIp": dst,
            "nettedSourceIp": nsrc, "nettedDestinationIp": ndst,
        })

    if not requested:
        return None, None

    by_id = {str(n.get("id")): n for n in svc_nodes}
    if not by_id:
        return None, "This service has no topology components to attach to."

    resolved: List[Dict[str, str]] = []
    seen: set = set()
    for item in requested:
        ref = item["ref"]
        node = by_id.get(ref)
        if node is None:
            return None, f"Component '{ref}' is not part of this service topology."
        if ref in seen:
            continue
        seen.add(ref)
        resolved.append({
            "ref": ref,
            "name": str(node.get("name") or ref),
            # Per-component addressing (each component can have its own IPs).
            "sourceIp": _clean(item.get("sourceIp"), 64) or "",
            "destinationIp": _clean(item.get("destinationIp"), 64) or "",
            # Optional NAT ("netted") addresses.
            "nettedSourceIp": _clean(item.get("nettedSourceIp"), 64) or "",
            "nettedDestinationIp": _clean(item.get("nettedDestinationIp"), 64) or "",
        })

    return json.dumps(resolved), None


# ---------------------------------------------------------------------------
# List services linked to a client (with connectivity overlay + health)
# ---------------------------------------------------------------------------

def list_client_services(
    client_id: int,
    user=None,
) -> Tuple[Optional[Dict[str, Any]], Optional[str], int]:
    client = Client.query.get(client_id)
    if not client:
        return None, "Client not found", 404

    linked_ids = [link.service_id for link in client.service_links]
    if not linked_ids:
        return {"items": [], "count": 0, "clientId": client.id, "clientName": client.name}, None, 200

    # Reuse the batched service health map (one concurrent kubectl pass) so this
    # never re-runs per-service live calls serially.
    from .application_service_service import list_services

    services_index = {s["id"]: s for s in list_services(user=user).get("items", [])}
    connections = _connections_by_service(client_id)

    items: List[Dict[str, Any]] = []
    for sid in linked_ids:
        svc = services_index.get(sid)
        if svc is None:
            # Service exists as a link but no live/summary data (deleted or
            # unreadable): fall back to a minimal record so the row still shows.
            raw = ApplicationService.query.get(sid)
            if not raw:
                continue
            svc = {
                "id": raw.id,
                "name": raw.name,
                "description": raw.description or "",
                "health": "unknown",
                "topology": {"nodes": [], "edges": []},
            }
        conn = connections.get(sid)
        svc_nodes = (svc.get("topology") or {}).get("nodes") or []
        items.append({
            "serviceId": svc["id"],
            "serviceName": svc["name"],
            "serviceDescription": svc.get("description", ""),
            "health": svc.get("health", "unknown"),
            "hasTopology": bool(svc_nodes),
            # Components the Edit Connection modal offers for multi-select.
            "components": _service_components(svc_nodes),
            "connection": _connection_to_dict(conn),
        })

    return (
        {"items": items, "count": len(items), "clientId": client.id, "clientName": client.name},
        None,
        200,
    )


# ---------------------------------------------------------------------------
# Create or update the client-service connectivity overlay
# ---------------------------------------------------------------------------

def upsert_connection(
    client_id: int,
    service_id: int,
    payload: Dict[str, Any],
    actor_user_id: Optional[int] = None,
    user=None,
) -> Tuple[Optional[Dict[str, Any]], Optional[str], int]:
    client = Client.query.get(client_id)
    if not client:
        return None, "Client not found", 404
    service = ApplicationService.query.get(service_id)
    if not service:
        return None, "Application service not found", 404

    transport_type, terr = _normalize_transport_type(payload.get("transportType"))
    if terr:
        return None, terr, 400

    transport_name = _clean(payload.get("transportName"), 255)
    if transport_type == "Other" and not transport_name:
        return None, "Transport name is required when transport type is 'Other'.", 400

    direction, derr = _normalize_direction(payload.get("direction"), default="inbound")
    if derr:
        return None, derr, 400

    # Validate the selected components against the live service topology.
    _, svc_topo = _service_topology(service_id, user=user)
    svc_nodes = list(svc_topo.get("nodes") or [])
    component_refs, cerr = _normalize_component_refs(payload.get("componentRefs"), svc_nodes)
    if cerr:
        return None, cerr, 400

    # Ensure the client↔service link exists so connectivity always implies the
    # service is one of the client's services (idempotent).
    link = ClientApplicationService.query.filter_by(
        client_id=client_id, service_id=service_id
    ).first()
    if not link:
        db.session.add(ClientApplicationService(client_id=client_id, service_id=service_id))

    conn = ClientServiceConnection.query.filter_by(
        client_id=client_id, service_id=service_id
    ).first()
    is_new = conn is None
    if is_new:
        conn = ClientServiceConnection(client_id=client_id, service_id=service_id)
        db.session.add(conn)

    conn.source_ip = _clean(payload.get("sourceIp"), 64)
    conn.destination_ip = _clean(payload.get("destinationIp"), 64)
    conn.netted_source_ip = _clean(payload.get("nettedSourceIp"), 64)
    conn.netted_destination_ip = _clean(payload.get("nettedDestinationIp"), 64)
    conn.transport_type = transport_type
    conn.transport_name = transport_name
    conn.transport_notes = _clean(payload.get("transportNotes"), 4000)
    conn.cluster_id = _clean(payload.get("clusterId"), 120)
    conn.namespace = _clean(payload.get("namespace"), 253)
    conn.environment = _clean(payload.get("environment"), 64)
    # Only overwrite the stored components when the payload carried the key, so a
    # partial update (e.g. status-only) doesn't silently clear the selection.
    if "componentRefs" in payload:
        conn.component_refs = component_refs
    conn.direction = direction
    conn.status = _clean(payload.get("status"), 32) or "active"
    if "isActive" in payload:
        conn.is_active = bool(payload.get("isActive"))
    conn.updated_at = datetime.now(timezone.utc)

    db.session.commit()
    log_audit(
        "client_service_connection_created" if is_new else "client_service_connection_updated",
        actor_user_id=actor_user_id,
        target_type="client_service_connection",
        target_id=str(conn.id),
        details={
            "clientId": client_id,
            "clientName": client.name,
            "serviceId": service_id,
            "serviceName": service.name,
            "transportType": conn.transport_type,
            "direction": conn.direction,
            "componentRefs": _parse_component_refs(conn.component_refs),
            "status": conn.status,
        },
    )
    return _connection_to_dict(conn), None, (201 if is_new else 200)


# ---------------------------------------------------------------------------
# Remove / deactivate the connectivity overlay
# ---------------------------------------------------------------------------

def delete_connection(
    client_id: int,
    service_id: int,
    actor_user_id: Optional[int] = None,
    deactivate: bool = False,
) -> Tuple[Optional[Dict[str, Any]], Optional[str], int]:
    conn = ClientServiceConnection.query.filter_by(
        client_id=client_id, service_id=service_id
    ).first()
    if not conn:
        return None, "Connection not found", 404

    conn_id = conn.id
    if deactivate:
        # Soft-remove: keep the row (and its history) but mark it inactive.
        conn.is_active = False
        conn.status = "inactive"
        conn.updated_at = datetime.now(timezone.utc)
        db.session.commit()
        result = {"id": conn_id, "deactivated": True, "clientId": client_id, "serviceId": service_id}
        action = "client_service_connection_deactivated"
    else:
        db.session.delete(conn)
        db.session.commit()
        result = {"id": conn_id, "deleted": True, "clientId": client_id, "serviceId": service_id}
        action = "client_service_connection_deleted"

    log_audit(
        action,
        actor_user_id=actor_user_id,
        target_type="client_service_connection",
        target_id=str(conn_id),
        details={"clientId": client_id, "serviceId": service_id},
    )
    return result, None, 200


# ---------------------------------------------------------------------------
# Composed client → transport → component(s) topology
# ---------------------------------------------------------------------------

def _service_topology(service_id: int, user=None) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    """Return (service_summary, topology) for a service, real or mock."""
    from .application_service_service import get_service, get_service_mock
    from ..k8s_provider import should_use_real_k8s

    svc_data, err, _ = get_service(service_id, user=user)
    if err or not svc_data:
        # Fall back to the demo service only when nothing is live.
        if not should_use_real_k8s():
            svc_data, err, _ = get_service_mock(service_id)
    if not svc_data:
        return {}, {"nodes": [], "edges": []}
    return svc_data, svc_data.get("topology") or {"nodes": [], "edges": []}


def _entry_node_id(nodes: List[Dict[str, Any]], edges: List[Dict[str, Any]]) -> Optional[Any]:
    """The service's entrypoint: a node with no inbound edge (lowest id wins).

    Falls back to the first node when every node has an inbound edge (a cycle).
    """
    if not nodes:
        return None
    targets = {str(e.get("targetNodeId")) for e in edges}
    roots = [n for n in nodes if str(n.get("id")) not in targets]
    pool = roots or nodes
    # Prefer numeric ordering when ids are ints; otherwise lexical.
    try:
        return sorted(pool, key=lambda n: (0, int(n["id"])))[0]["id"]
    except (ValueError, TypeError, KeyError):
        return sorted(pool, key=lambda n: str(n.get("id")))[0]["id"]


def get_client_service_topology(
    client_id: int,
    service_id: int,
    user=None,
) -> Tuple[Optional[Dict[str, Any]], Optional[str], int]:
    client = Client.query.get(client_id)
    if not client:
        return None, "Client not found", 404
    service = ApplicationService.query.get(service_id)
    if not service:
        return None, "Application service not found", 404

    conn = ClientServiceConnection.query.filter_by(
        client_id=client_id, service_id=service_id
    ).first()

    svc_summary, svc_topo = _service_topology(service_id, user=user)
    svc_nodes = list(svc_topo.get("nodes") or [])
    svc_edges = list(svc_topo.get("edges") or [])

    conn_source = (conn.source_ip if conn else None) or ""
    conn_dest = (conn.destination_ip if conn else None) or ""
    # Optional NAT addresses — empty when the connection isn't NATted.
    conn_netted_source = (conn.netted_source_ip if conn else None) or ""
    conn_netted_dest = (conn.netted_destination_ip if conn else None) or ""
    transport_type = (conn.transport_type if conn else None) or _NOT_CONFIGURED
    transport_name = (conn.transport_name if conn else None) or ""

    client_node_id = f"client-{client_id}"

    # Overlay client node (string ids never collide with integer service node ids).
    client_node = {
        "id": client_node_id,
        "name": client.name,
        "type": "Client",
        "description": client.contact_person or client.email or "Client",
        "overlay": "client",
    }

    nodes: List[Dict[str, Any]] = [client_node]
    edges: List[Dict[str, Any]] = []

    # Determine the service-side attach point(s): the connection's selected
    # components (each with its own source/destination IP) when set, otherwise the
    # service entrypoint (fallback, using the connection-level IPs). When the
    # service has no topology at all, synthesize a single service node so we still
    # render Client ↔ Transport ↔ Service.
    node_index = {str(n.get("id")): n for n in svc_nodes}
    saved_refs = _parse_component_refs(conn.component_refs) if conn else []

    # Each target: {"id", "sourceIp", "destinationIp"}. Per-component IP falls back
    # to the connection-level IP, then to "Not configured".
    targets: List[Dict[str, Any]] = []
    if svc_nodes:
        nodes.extend(svc_nodes)
        edges.extend(svc_edges)
        resolved = [r for r in saved_refs if r["ref"] in node_index]
        if resolved:
            for r in resolved:
                targets.append({
                    "id": node_index[r["ref"]]["id"],
                    "sourceIp": r.get("sourceIp") or conn_source or _NOT_CONFIGURED,
                    "destinationIp": r.get("destinationIp") or conn_dest or _NOT_CONFIGURED,
                    "nettedSourceIp": r.get("nettedSourceIp") or conn_netted_source,
                    "nettedDestinationIp": r.get("nettedDestinationIp") or conn_netted_dest,
                })
        else:
            entry_id = _entry_node_id(svc_nodes, svc_edges)
            if entry_id is not None:
                targets.append({
                    "id": entry_id,
                    "sourceIp": conn_source or _NOT_CONFIGURED,
                    "destinationIp": conn_dest or _NOT_CONFIGURED,
                    "nettedSourceIp": conn_netted_source,
                    "nettedDestinationIp": conn_netted_dest,
                })
        # Accent the components this connection lands on.
        attach_id_strs = {str(t["id"]) for t in targets}
        for n in nodes:
            if str(n.get("id")) in attach_id_strs:
                n["overlay"] = "target"
    else:
        service_attach_id = f"service-{service_id}"
        nodes.append({
            "id": service_attach_id,
            "name": service.name,
            "type": "Service",
            "description": service.description or "Service entrypoint",
            "overlay": "target",
        })
        targets.append({
            "id": service_attach_id,
            "sourceIp": conn_source or _NOT_CONFIGURED,
            "destinationIp": conn_dest or _NOT_CONFIGURED,
            "nettedSourceIp": conn_netted_source,
            "nettedDestinationIp": conn_netted_dest,
        })

    # One transport node per target so each component's own source/destination IP
    # is shown unambiguously. With multiple components this branches from the
    # client into parallel client → transport → component paths (a readable tree)
    # instead of chaining every component through a single shared hop. The saved
    # `direction` flips the arrows:
    #   inbound  → client → transport → component  (client talks to service)
    #   outbound → component → transport → client  (service talks to client)
    #   both     → both directions (bidirectional)
    direction = (conn.direction if conn else None) or _DEFAULT_DIRECTION
    seen_pairs = {(str(e["sourceNodeId"]), str(e["targetNodeId"])) for e in edges}

    def _add_edge(edge_id: str, a: Any, b: Any, desc: str) -> None:
        if (str(a), str(b)) in seen_pairs:
            return
        edges.append({
            "id": edge_id,
            "sourceNodeId": a, "targetNodeId": b,
            "scope": "external", "description": desc,
        })
        seen_pairs.add((str(a), str(b)))

    for idx, target in enumerate(targets):
        src_ip = target["sourceIp"]
        dst_ip = target["destinationIp"]
        netted_src = target.get("nettedSourceIp") or ""
        netted_dst = target.get("nettedDestinationIp") or ""
        transport_node_id = f"transport-{client_id}-{service_id}-{idx}"
        transport_desc_parts = [f"Source: {src_ip}", f"Destination: {dst_ip}"]
        if netted_src:
            transport_desc_parts.append(f"NAT source: {netted_src}")
        if netted_dst:
            transport_desc_parts.append(f"NAT destination: {netted_dst}")
        if transport_name:
            transport_desc_parts.append(transport_name)
        nodes.append({
            "id": transport_node_id,
            "name": transport_type,
            "type": "Connectivity",
            "description": " · ".join(transport_desc_parts),
            "overlay": "transport",
            "sourceIp": src_ip,
            "destinationIp": dst_ip,
            "nettedSourceIp": netted_src,
            "nettedDestinationIp": netted_dst,
            "transportName": transport_name,
        })
        # The NAT-translated address renders as a second label line under the
        # real one (the viewer splits edge descriptions on newlines).
        src_label = f"Source {src_ip}" + (f"\nNAT {netted_src}" if netted_src else "")
        dst_label = f"Destination {dst_ip}" + (f"\nNAT {netted_dst}" if netted_dst else "")
        if direction in ("inbound", "both"):
            _add_edge(f"edge-conn-in-{idx}-a", client_node_id, transport_node_id, src_label)
            _add_edge(f"edge-conn-in-{idx}-b", transport_node_id, target["id"], dst_label)
        if direction in ("outbound", "both"):
            _add_edge(f"edge-conn-out-{idx}-a", transport_node_id, client_node_id, src_label)
            _add_edge(f"edge-conn-out-{idx}-b", target["id"], transport_node_id, dst_label)

    return (
        {
            "client": {
                "id": client.id,
                "name": client.name,
                "contactPerson": client.contact_person or "",
                "email": client.email or "",
            },
            "service": {
                "id": service.id,
                "name": service.name,
                "health": svc_summary.get("health", "unknown"),
            },
            "connection": _connection_to_dict(conn),
            "topology": {"nodes": nodes, "edges": edges},
        },
        None,
        200,
    )
