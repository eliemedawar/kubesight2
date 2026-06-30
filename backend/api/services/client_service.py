from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

from ..audit import log_audit
from ..db import db
from ..models import ApplicationService, Client, ClientApplicationService

# ---------------------------------------------------------------------------
# Mock data
# ---------------------------------------------------------------------------

_MOCK_CLIENTS = [
    {
        "id": 1,
        "name": "Acme Corporation",
        "contactPerson": "Jane Smith",
        "email": "jane.smith@acmecorp.example",
        "phone": "+1-555-0100",
        "notes": "Enterprise tier. SLA: 99.9% uptime.",
        "serviceIds": [1, 3],
        "createdAt": "2024-02-01T09:00:00+00:00",
        "updatedAt": "2024-05-15T12:00:00+00:00",
    },
    {
        "id": 2,
        "name": "Beta Industries",
        "contactPerson": "Carlos Rivera",
        "email": "c.rivera@beta.example",
        "phone": "+1-555-0200",
        "notes": "Startup plan. Billing integration is their primary use case.",
        "serviceIds": [2, 5],
        "createdAt": "2024-03-10T14:00:00+00:00",
        "updatedAt": "2024-06-01T10:00:00+00:00",
    },
    {
        "id": 3,
        "name": "Gamma Solutions",
        "contactPerson": "Priya Nair",
        "email": "priya@gamma.example",
        "phone": None,
        "notes": "Full platform subscriber.",
        "serviceIds": [1, 2, 3, 4, 5],
        "createdAt": "2024-04-05T11:00:00+00:00",
        "updatedAt": "2024-06-10T08:00:00+00:00",
    },
    {
        "id": 4,
        "name": "Delta Tech",
        "contactPerson": None,
        "email": "admin@deltatech.example",
        "phone": "+44-20-7946-0100",
        "notes": None,
        "serviceIds": [4],
        "createdAt": "2024-05-20T16:00:00+00:00",
        "updatedAt": "2024-05-20T16:00:00+00:00",
    },
]


# ---------------------------------------------------------------------------
# Health helpers (delegated to application_service_service)
# ---------------------------------------------------------------------------

def _aggregate_health(statuses: List[str]) -> str:
    if not statuses:
        return "unknown"
    if "critical" in statuses:
        return "critical"
    if "warning" in statuses:
        return "warning"
    return "healthy"


def _service_health_from_dict(svc_dict: Dict[str, Any]) -> str:
    return svc_dict.get("health", "unknown")


# ---------------------------------------------------------------------------
# Serializers
# ---------------------------------------------------------------------------

def _client_to_dict(
    client: Client,
    services: Optional[List[Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    if services is None:
        services = []
    service_healths = [_service_health_from_dict(s) for s in services]
    status = _aggregate_health(service_healths)
    return {
        "id": client.id,
        "name": client.name,
        "contactPerson": client.contact_person or "",
        "email": client.email or "",
        "phone": client.phone or "",
        "notes": client.notes or "",
        "serviceCount": len(services),
        "services": services,
        "status": status,
        "createdAt": client.created_at.isoformat() if client.created_at else None,
        "updatedAt": client.updated_at.isoformat() if client.updated_at else None,
    }


def _load_services_for_client(client: Client, user=None) -> List[Dict[str, Any]]:
    from .application_service_service import get_service, list_services_mock
    from ..k8s_provider import should_use_real_k8s

    service_ids = [link.service_id for link in client.service_links]
    if not service_ids:
        return []

    services = []
    for sid in service_ids:
        svc = ApplicationService.query.get(sid)
        if not svc:
            continue
        # Check if any deployment is in real mode to decide how to fetch health
        if svc.deployments and any(should_use_real_k8s(d.cluster_id) for d in svc.deployments):
            svc_data, err, _ = get_service(sid, user=user)
            if svc_data and not err:
                services.append(svc_data)
        else:
            # Mock mode: use mock health data
            from .application_service_service import get_service_mock
            svc_data, err, _ = get_service_mock(sid)
            if svc_data and not err:
                services.append(svc_data)
            elif svc:
                # Fallback: return basic info
                services.append({
                    "id": svc.id,
                    "name": svc.name,
                    "description": svc.description or "",
                    "health": "unknown",
                    "deploymentCount": len(svc.deployments),
                })
    return services


# ---------------------------------------------------------------------------
# Mock helpers
# ---------------------------------------------------------------------------

def _mock_service_summary(service_id: int) -> Optional[Dict[str, Any]]:
    from .application_service_service import get_service_mock
    data, _, _ = get_service_mock(service_id)
    return data


def _mock_client_dict(client: Dict[str, Any]) -> Dict[str, Any]:
    services = [s for s in (_mock_service_summary(sid) for sid in client["serviceIds"]) if s]
    service_healths = [s.get("health", "unknown") for s in services]
    return {
        "id": client["id"],
        "name": client["name"],
        "contactPerson": client.get("contactPerson") or "",
        "email": client.get("email") or "",
        "phone": client.get("phone") or "",
        "notes": client.get("notes") or "",
        "serviceCount": len(services),
        "services": services,
        "status": _aggregate_health(service_healths),
        "createdAt": client["createdAt"],
        "updatedAt": client["updatedAt"],
    }


# ---------------------------------------------------------------------------
# Public CRUD API
# ---------------------------------------------------------------------------

def list_clients(user=None) -> Dict[str, Any]:
    clients = Client.query.order_by(Client.name.asc()).all()
    if not clients:
        return {"items": [], "count": 0}

    # Fetch every service's health in a single batched (and cached) pass instead of
    # calling get_service() per service per client. Clients share services, so the
    # old per-client loop issued the same live kubectl calls repeatedly and serially
    # — the dominant cost when opening the Clients tab. list_services() already
    # builds one concurrent health map across all deployments and memoizes it.
    from .application_service_service import list_services

    services_index = {s["id"]: s for s in list_services(user=user).get("items", [])}

    items = []
    for client in clients:
        svcs = [
            services_index[link.service_id]
            for link in client.service_links
            if link.service_id in services_index
        ]
        items.append(_client_to_dict(client, services=svcs))
    return {"items": items, "count": len(items)}


def list_clients_mock() -> Dict[str, Any]:
    items = [_mock_client_dict(c) for c in _MOCK_CLIENTS]
    return {"items": items, "count": len(items)}


def get_client(client_id: int, user=None) -> Tuple[Optional[Dict[str, Any]], Optional[str], int]:
    client = Client.query.get(client_id)
    if not client:
        return None, "Client not found", 404
    svcs = _load_services_for_client(client, user=user)
    return _client_to_dict(client, services=svcs), None, 200


def get_client_mock(client_id: int) -> Tuple[Optional[Dict[str, Any]], Optional[str], int]:
    client = next((c for c in _MOCK_CLIENTS if c["id"] == client_id), None)
    if not client:
        return None, "Client not found", 404
    return _mock_client_dict(client), None, 200


def create_client(
    payload: Dict[str, Any],
    actor_user_id: Optional[int] = None,
) -> Tuple[Optional[Dict[str, Any]], Optional[str], int]:
    name = (payload.get("name") or "").strip()
    if not name:
        return None, "Client name is required.", 400
    if len(name) > 120:
        return None, "Client name must be 120 characters or less.", 400
    if Client.query.filter_by(name=name).first():
        return None, f"A client named '{name}' already exists.", 409

    client = Client(
        name=name,
        contact_person=(payload.get("contactPerson") or "").strip() or None,
        email=(payload.get("email") or "").strip() or None,
        phone=(payload.get("phone") or "").strip() or None,
        notes=(payload.get("notes") or "").strip() or None,
    )
    db.session.add(client)
    db.session.flush()

    service_ids = [int(sid) for sid in (payload.get("serviceIds") or []) if str(sid).isdigit()]
    _sync_client_services(client, service_ids)

    db.session.commit()
    log_audit(
        "client_created",
        actor_user_id=actor_user_id,
        target_type="client",
        target_id=str(client.id),
        details={"name": name, "serviceCount": len(service_ids)},
    )
    data, _, _ = get_client(client.id)
    return data, None, 201


def update_client(
    client_id: int,
    payload: Dict[str, Any],
    actor_user_id: Optional[int] = None,
) -> Tuple[Optional[Dict[str, Any]], Optional[str], int]:
    client = Client.query.get(client_id)
    if not client:
        return None, "Client not found", 404

    name = (payload.get("name") or "").strip()
    if not name:
        return None, "Client name is required.", 400
    if len(name) > 120:
        return None, "Client name must be 120 characters or less.", 400
    existing = Client.query.filter_by(name=name).first()
    if existing and existing.id != client_id:
        return None, f"A client named '{name}' already exists.", 409

    client.name = name
    client.contact_person = (payload.get("contactPerson") or "").strip() or None
    client.email = (payload.get("email") or "").strip() or None
    client.phone = (payload.get("phone") or "").strip() or None
    client.notes = (payload.get("notes") or "").strip() or None
    client.updated_at = datetime.now(timezone.utc)

    service_ids = [int(sid) for sid in (payload.get("serviceIds") or []) if str(sid).isdigit()]
    _sync_client_services(client, service_ids)

    db.session.commit()
    log_audit(
        "client_updated",
        actor_user_id=actor_user_id,
        target_type="client",
        target_id=str(client.id),
        details={"name": name, "serviceCount": len(service_ids)},
    )
    data, _, _ = get_client(client.id)
    return data, None, 200


def delete_client(
    client_id: int,
    actor_user_id: Optional[int] = None,
) -> Tuple[Optional[Dict[str, Any]], Optional[str], int]:
    client = Client.query.get(client_id)
    if not client:
        return None, "Client not found", 404
    name = client.name
    db.session.delete(client)
    db.session.commit()
    log_audit(
        "client_deleted",
        actor_user_id=actor_user_id,
        target_type="client",
        target_id=str(client_id),
        details={"name": name},
    )
    return {"id": client_id, "deleted": True}, None, 200


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _sync_client_services(client: Client, service_ids: List[int]) -> None:
    """Replace the client's service links with the given list of service IDs."""
    existing_links = {link.service_id: link for link in client.service_links}
    wanted = set(service_ids)

    # Remove links no longer wanted.
    for sid, link in list(existing_links.items()):
        if sid not in wanted:
            db.session.delete(link)

    # Add new links.
    for sid in wanted:
        if sid not in existing_links:
            svc = ApplicationService.query.get(sid)
            if svc:
                db.session.add(ClientApplicationService(client_id=client.id, service_id=sid))

    db.session.flush()
