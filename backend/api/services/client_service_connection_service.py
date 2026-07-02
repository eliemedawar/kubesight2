"""Client Service Access Topology — client-specific connectivity overlays.

Each client↔service link can carry a single :class:`ClientServiceConnection`
describing how *this* client reaches the service (source/destination IP,
transport, landing cluster/namespace/environment). The reusable service topology
is never duplicated per client: the composed client topology simply prepends a
client node and a transport node onto the shared service topology.
"""

from __future__ import annotations

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


# ---------------------------------------------------------------------------
# Serializers
# ---------------------------------------------------------------------------

def _connection_to_dict(conn: Optional[ClientServiceConnection]) -> Optional[Dict[str, Any]]:
    if conn is None:
        return None
    return {
        "id": conn.id,
        "clientId": conn.client_id,
        "serviceId": conn.service_id,
        "sourceIp": conn.source_ip or "",
        "destinationIp": conn.destination_ip or "",
        "transportType": conn.transport_type or "",
        "transportName": conn.transport_name or "",
        "transportNotes": conn.transport_notes or "",
        "clusterId": conn.cluster_id or "",
        "namespace": conn.namespace or "",
        "environment": conn.environment or "",
        "status": conn.status or "active",
        "isActive": bool(conn.is_active),
        "createdAt": conn.created_at.isoformat() if conn.created_at else None,
        "updatedAt": conn.updated_at.isoformat() if conn.updated_at else None,
    }


def _connections_by_service(client_id: int) -> Dict[int, ClientServiceConnection]:
    rows = ClientServiceConnection.query.filter_by(client_id=client_id).all()
    return {row.service_id: row for row in rows}


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
        items.append({
            "serviceId": svc["id"],
            "serviceName": svc["name"],
            "serviceDescription": svc.get("description", ""),
            "health": svc.get("health", "unknown"),
            "hasTopology": bool((svc.get("topology") or {}).get("nodes")),
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
    conn.transport_type = transport_type
    conn.transport_name = transport_name
    conn.transport_notes = _clean(payload.get("transportNotes"), 4000)
    conn.cluster_id = _clean(payload.get("clusterId"), 120)
    conn.namespace = _clean(payload.get("namespace"), 253)
    conn.environment = _clean(payload.get("environment"), 64)
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
# Composed client → transport → service topology
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

    source_ip = (conn.source_ip if conn else None) or _NOT_CONFIGURED
    dest_ip = (conn.destination_ip if conn else None) or _NOT_CONFIGURED
    transport_type = (conn.transport_type if conn else None) or _NOT_CONFIGURED
    transport_name = (conn.transport_name if conn else None) or ""

    client_node_id = f"client-{client_id}"
    transport_node_id = f"transport-{client_id}-{service_id}"

    # Overlay nodes (string ids never collide with the integer service node ids).
    client_node = {
        "id": client_node_id,
        "name": client.name,
        "type": "Client",
        "description": client.contact_person or client.email or "Client",
        "overlay": "client",
    }
    transport_desc_parts = [f"Source: {source_ip}", f"Destination: {dest_ip}"]
    if transport_name:
        transport_desc_parts.append(transport_name)
    transport_node = {
        "id": transport_node_id,
        "name": transport_type,
        "type": "Connectivity",
        "description": " · ".join(transport_desc_parts),
        "overlay": "transport",
        "sourceIp": source_ip,
        "destinationIp": dest_ip,
        "transportName": transport_name,
    }

    overlay_edges = [
        {
            "id": f"edge-{client_node_id}-{transport_node_id}",
            "sourceNodeId": client_node_id,
            "targetNodeId": transport_node_id,
            "protocol": transport_type if transport_type != _NOT_CONFIGURED else None,
            "scope": "external",
            "description": f"Source {source_ip}",
        }
    ]

    nodes = [client_node, transport_node]
    edges = list(overlay_edges)

    entry_id = _entry_node_id(svc_nodes, svc_edges)
    if entry_id is None:
        # Service has no topology yet: still show Client → Transport → Service.
        service_node_id = f"service-{service_id}"
        nodes.append({
            "id": service_node_id,
            "name": service.name,
            "type": "Service",
            "description": service.description or "Service entrypoint",
            "overlay": "service",
        })
        edges.append({
            "id": f"edge-{transport_node_id}-{service_node_id}",
            "sourceNodeId": transport_node_id,
            "targetNodeId": service_node_id,
            "scope": "external",
            "description": f"Destination {dest_ip}",
        })
    else:
        # Splice the transport node into the existing service entrypoint.
        nodes.extend(svc_nodes)
        edges.extend(svc_edges)
        edges.append({
            "id": f"edge-{transport_node_id}-{entry_id}",
            "sourceNodeId": transport_node_id,
            "targetNodeId": entry_id,
            "scope": "external",
            "description": f"Destination {dest_ip}",
        })

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
