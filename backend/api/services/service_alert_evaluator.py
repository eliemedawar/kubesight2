"""Evaluate service alert policies against Application Service health.

A service alert policy watches an Application Service (or all of them) and fires
one alert per linked workload (deployment/statefulset/daemonset/pod) or predefined
topology component that is down or degraded. Alerts auto-resolve when the
resource recovers.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

from ..alert_policy_catalog import (
    ALL_SERVICES_ID,
    SERVICE_ALERT_CLUSTER_ID,
    normalize_service_config,
)
from ..db import db
from ..models import AlertHistory, AlertPolicy, ApplicationService, User

logger = logging.getLogger(__name__)


def _iso_now() -> str:
    return datetime.now(timezone.utc).isoformat()


# Component statuses (healthy/degraded/unhealthy/unknown) mapped onto the
# workload health scale (healthy/warning/critical/unknown) used for triggering.
_COMPONENT_STATUS_TO_HEALTH = {
    "healthy": "healthy",
    "degraded": "warning",
    "unhealthy": "critical",
    "unknown": "unknown",
}

# Which observed statuses fire an alert for each configured trigger level.
# "unknown" on a live cluster means the workload was not found or the cluster is
# unreachable — that is a down condition, so it fires at both levels.
_TRIGGER_STATUSES = {
    "critical": {"critical", "unknown"},
    "degraded": {"critical", "warning", "unknown"},
}

_STATUS_WORDS = {
    "critical": "down",
    "warning": "degraded",
    "unknown": "unreachable or not found",
}


def list_alertable_services() -> List[Dict[str, Any]]:
    """Application services selectable in a service alert policy (id + name).

    Prefers DB-backed services; falls back to the demo/mock list when the DB has
    none and no live cluster is configured (mirrors the Application Services API).
    """
    rows = ApplicationService.query.order_by(ApplicationService.name.asc()).all()
    if rows:
        return [{"id": row.id, "name": row.name} for row in rows]
    from ..k8s_provider import should_use_real_k8s

    if should_use_real_k8s():
        return []
    from .application_service_service import _MOCK_SERVICES

    return [{"id": svc["id"], "name": svc["name"]} for svc in _MOCK_SERVICES]


def mock_service_name(service_id: Any) -> Optional[str]:
    from .application_service_service import _MOCK_SERVICES

    try:
        service_id = int(service_id)
    except (TypeError, ValueError):
        return None
    svc = next((s for s in _MOCK_SERVICES if s["id"] == service_id), None)
    return svc["name"] if svc else None


def _mock_workload_status(cluster_id: str, namespace: str, name: str) -> Tuple[str, Dict[str, Any]]:
    """Demo-mode health for a linked workload, from the shared mock health table."""
    from .application_service_service import _MOCK_DEPLOYMENT_HEALTH, _deployment_status

    health = _MOCK_DEPLOYMENT_HEALTH.get(
        (cluster_id, namespace, name),
        {"desired": 1, "available": 1, "ready": 1},
    )
    desired = health["desired"]
    available = min(health["available"], health["ready"])
    return _deployment_status(desired, available), {
        "desiredReplicas": desired,
        "availableReplicas": health["available"],
        "readyReplicas": health["ready"],
    }


def _db_service_snapshot(svc: ApplicationService) -> Dict[str, Any]:
    """Normalize a DB service into {workloads: [...], components: [...]} with statuses."""
    from ..k8s_provider import should_use_real_k8s
    from .application_service_service import (
        _any_real_cluster,
        _build_k8s_health_map,
        _live_deployment_detail,
        _normalize_kind,
    )

    deployments = list(svc.deployments)
    k8s_map = None
    if deployments and _any_real_cluster(deployments):
        k8s_map = _build_k8s_health_map(deployments)

    workloads: List[Dict[str, Any]] = []
    for dep in deployments:
        kind = _normalize_kind(getattr(dep, "resource_kind", None))
        if k8s_map is not None and should_use_real_k8s(dep.cluster_id):
            detail = _live_deployment_detail(dep, k8s_map.get((dep.cluster_id, dep.namespace), {}))
            status = detail.get("status", "unknown")
            replicas = {
                "desiredReplicas": detail.get("desiredReplicas"),
                "availableReplicas": detail.get("availableReplicas"),
                "readyReplicas": detail.get("readyReplicas"),
            }
        else:
            status, replicas = _mock_workload_status(dep.cluster_id, dep.namespace, dep.deployment_name)
        workloads.append(
            {
                "clusterId": dep.cluster_id,
                "namespace": dep.namespace,
                "name": dep.deployment_name,
                "kind": kind,
                "status": status,
                **replicas,
            }
        )

    components: List[Dict[str, Any]] = []
    for node in svc.topology_nodes:
        component = getattr(node, "component", None)
        if not node.component_id or not component:
            continue
        raw_status = component.last_status or "unknown"
        components.append(
            {
                "nodeId": node.id,
                "componentId": component.id,
                "name": component.name,
                "rawStatus": raw_status,
                "status": _COMPONENT_STATUS_TO_HEALTH.get(raw_status, "unknown"),
                "message": component.last_message,
            }
        )

    # Clients consuming this service — carried on every alert so notifications
    # can say who is affected by the outage.
    clients = sorted({link.client.name for link in svc.client_links if link.client})

    return {
        "id": svc.id,
        "name": svc.name,
        "clients": clients,
        "workloads": workloads,
        "components": components,
    }


def _mock_service_snapshot(service_id: Optional[int]) -> List[Dict[str, Any]]:
    """Demo-mode snapshots when the DB has no application services."""
    from .application_service_service import _MOCK_SERVICES

    snapshots = []
    for svc in _MOCK_SERVICES:
        if service_id is not None and svc["id"] != service_id:
            continue
        workloads = []
        for dep in svc["deployments"]:
            status, replicas = _mock_workload_status(
                dep["clusterId"], dep["namespace"], dep["deploymentName"]
            )
            workloads.append(
                {
                    "clusterId": dep["clusterId"],
                    "namespace": dep["namespace"],
                    "name": dep["deploymentName"],
                    "kind": "deployment",
                    "status": status,
                    **replicas,
                }
            )
        snapshots.append(
            {"id": svc["id"], "name": svc["name"], "clients": [], "workloads": workloads, "components": []}
        )
    return snapshots


def _target_service_snapshots(config: Dict[str, Any]) -> Tuple[List[Dict[str, Any]], Optional[str]]:
    from ..k8s_provider import should_use_real_k8s

    service_id = config.get("serviceId")
    if service_id == ALL_SERVICES_ID:
        rows = ApplicationService.query.order_by(ApplicationService.name.asc()).all()
        if rows:
            return [_db_service_snapshot(svc) for svc in rows], None
        if not should_use_real_k8s():
            return _mock_service_snapshot(None), None
        return [], None

    svc = ApplicationService.query.get(service_id)
    if svc:
        return [_db_service_snapshot(svc)], None
    if not should_use_real_k8s():
        snapshots = _mock_service_snapshot(int(service_id))
        if snapshots:
            return snapshots, None
    return [], "Application service not found"


def _refresh_linked_component_healths(snapshots_source_ids: List[int]) -> None:
    """Re-check stale component healths for the targeted services so alerts do not
    act on old manual check results."""
    from ..models import ApplicationServiceTopologyNode
    from .topology_component_service import refresh_stale_component_healths

    query = ApplicationServiceTopologyNode.query.filter(
        ApplicationServiceTopologyNode.component_id.isnot(None)
    )
    if snapshots_source_ids:
        query = query.filter(ApplicationServiceTopologyNode.service_id.in_(snapshots_source_ids))
    component_ids = sorted({node.component_id for node in query.all()})
    if component_ids:
        refresh_stale_component_healths(component_ids=component_ids)


def _workload_alert_key(policy_id: int, service_id: int, item: Dict[str, Any]) -> str:
    return ":".join(
        [
            f"policy-{policy_id}",
            SERVICE_ALERT_CLUSTER_ID,
            f"svc-{service_id}",
            "workload",
            item.get("clusterId") or "*",
            item.get("namespace") or "*",
            item.get("kind") or "deployment",
            item.get("name") or "*",
        ]
    )


def _component_alert_key(policy_id: int, service_id: int, item: Dict[str, Any]) -> str:
    return ":".join(
        [
            f"policy-{policy_id}",
            SERVICE_ALERT_CLUSTER_ID,
            f"svc-{service_id}",
            "component",
            str(item.get("nodeId") or item.get("componentId") or item.get("name")),
        ]
    )


def _workload_description(service_name: str, item: Dict[str, Any]) -> str:
    status_word = _STATUS_WORDS.get(item["status"], item["status"])
    location = f"{item.get('clusterId')}/{item.get('namespace')}/{item.get('name')}"
    desired = item.get("desiredReplicas")
    ready = item.get("readyReplicas")
    replicas = ""
    if desired is not None and ready is not None and item["status"] != "unknown":
        replicas = f" ({ready}/{desired} replicas ready)"
    return f"Service '{service_name}': {item.get('kind', 'deployment')} {location} is {status_word}{replicas}"


def _component_description(service_name: str, item: Dict[str, Any]) -> str:
    detail = f" — {item['message']}" if item.get("message") else ""
    return f"Service '{service_name}': component '{item['name']}' is {item['rawStatus']}{detail}"


def _triggered_condition(trigger: str, observed: str, label: str) -> Dict[str, Any]:
    return {
        "metricKey": "service_component_health",
        "metricLabel": label,
        "operator": "=",
        "threshold": trigger,
        "observedValue": observed,
        "matched": True,
    }


def _history_to_service_alert_dict(row: AlertHistory) -> Dict[str, Any]:
    snapshot = row.metric_snapshot if isinstance(row.metric_snapshot, dict) else {}
    return {
        "id": f"history-{row.id}",
        "alertType": "service",
        "severity": row.severity,
        "clusterId": row.cluster_id,
        "namespace": row.namespace,
        "pod": None,
        "resourceType": row.resource_type,
        "resourceName": row.resource_name,
        "serviceId": snapshot.get("serviceId"),
        "serviceName": snapshot.get("serviceName"),
        "affectedClients": snapshot.get("affectedClients") or [],
        "title": row.title,
        "description": row.description,
        "policyId": row.policy_id,
        "policyName": row.policy_name,
        "triggeredConditions": row.triggered_conditions,
        "metricSnapshot": row.metric_snapshot,
        "firedAt": row.fired_at.isoformat() if row.fired_at else _iso_now(),
        "status": "firing" if row.status == "active" else "resolved",
        "source": "alert_policy",
    }


def _upsert_alert(
    policy: AlertPolicy,
    alert_key: str,
    *,
    cluster_id: str,
    namespace: Optional[str],
    resource_type: str,
    resource_name: str,
    title: str,
    description: str,
    triggered_conditions: List[Dict[str, Any]],
    snapshot: Dict[str, Any],
    now: datetime,
    persist: bool,
) -> AlertHistory:
    row = AlertHistory.query.filter_by(alert_key=alert_key).first()
    if not row:
        row = AlertHistory(
            alert_key=alert_key,
            policy_id=policy.id,
            policy_name=policy.name,
            cluster_id=cluster_id,
            namespace=namespace,
            resource_type=resource_type,
            resource_name=resource_name,
            alert_type="service",
            severity=policy.severity,
            status="active",
            title=title,
            description=description,
            triggered_conditions=triggered_conditions,
            metric_snapshot=snapshot,
            fired_at=now,
        )
        db.session.add(row)
    else:
        row.status = "active"
        row.resolved_at = None
        row.policy_name = policy.name
        row.severity = policy.severity
        row.title = title
        row.description = description
        row.triggered_conditions = triggered_conditions
        row.metric_snapshot = snapshot
        row.fired_at = row.fired_at or now
    if persist:
        db.session.flush()
        from ..alert_notifier import dispatch_policy_alert_notifications

        dispatch_policy_alert_notifications(_history_to_service_alert_dict(row))
    return row


def evaluate_service_policy(
    policy: AlertPolicy,
    *,
    user: Optional[User] = None,
    persist: bool = True,
) -> Tuple[List[AlertHistory], Optional[str], Optional[str]]:
    """Evaluate one service alert policy. Returns (updated_rows, measured, error).

    ``updated_rows`` contains both newly fired/still-firing rows and rows that
    were resolved this pass.
    """
    config = normalize_service_config(policy.service_config)
    trigger = config["triggerOn"]
    firing_statuses = _TRIGGER_STATUSES.get(trigger, _TRIGGER_STATUSES["critical"])

    # Re-check stale component healths first so the evaluation below never acts
    # on an old manual check result.
    try:
        if config["serviceId"] == ALL_SERVICES_ID:
            _refresh_linked_component_healths([])
        else:
            _refresh_linked_component_healths([int(config["serviceId"])])
    except Exception:
        logger.exception("Component health refresh failed during service alert evaluation")

    snapshots, error = _target_service_snapshots(config)
    if error:
        return [], None, error

    now = datetime.now(timezone.utc)
    updated_rows: List[AlertHistory] = []
    current_keys: set = set()
    unhealthy_count = 0
    total_items = 0

    for snap in snapshots:
        service_id = snap["id"]
        service_name = snap["name"]

        for item in snap["workloads"]:
            total_items += 1
            if item["status"] not in firing_statuses:
                continue
            unhealthy_count += 1
            key = _workload_alert_key(policy.id, service_id, item)
            current_keys.add(key)
            description = _workload_description(service_name, item)
            row = _upsert_alert(
                policy,
                key,
                cluster_id=item.get("clusterId") or SERVICE_ALERT_CLUSTER_ID,
                namespace=item.get("namespace"),
                resource_type=item.get("kind") or "deployment",
                resource_name=item.get("name") or "",
                title=f"{policy.name} triggered",
                description=description,
                triggered_conditions=[
                    _triggered_condition(trigger, item["status"], f"{service_name} workload health")
                ],
                snapshot={
                    "serviceId": service_id,
                    "serviceName": service_name,
                    "affectedClients": snap.get("clients") or [],
                    "itemType": "workload",
                    "workload": item,
                },
                now=now,
                persist=persist,
            )
            updated_rows.append(row)

        for item in snap["components"]:
            total_items += 1
            # An unknown component has simply never been checked — never alert on it.
            if item["status"] == "unknown" or item["status"] not in firing_statuses:
                continue
            unhealthy_count += 1
            key = _component_alert_key(policy.id, service_id, item)
            current_keys.add(key)
            description = _component_description(service_name, item)
            row = _upsert_alert(
                policy,
                key,
                cluster_id=SERVICE_ALERT_CLUSTER_ID,
                namespace=None,
                resource_type="component",
                resource_name=item["name"],
                title=f"{policy.name} triggered",
                description=description,
                triggered_conditions=[
                    _triggered_condition(trigger, item["status"], f"{service_name} component health")
                ],
                snapshot={
                    "serviceId": service_id,
                    "serviceName": service_name,
                    "affectedClients": snap.get("clients") or [],
                    "itemType": "component",
                    "component": item,
                },
                now=now,
                persist=persist,
            )
            updated_rows.append(row)

    # Resolve every previously active alert on this policy that is no longer
    # unhealthy (covers recoveries, unlinked resources, and retargeted policies).
    for row in AlertHistory.query.filter_by(policy_id=policy.id, status="active").all():
        if row.alert_key in current_keys:
            continue
        row.status = "resolved"
        row.resolved_at = now
        updated_rows.append(row)

    if not snapshots:
        measured = "No application services to evaluate"
    elif unhealthy_count:
        measured = f"{unhealthy_count} of {total_items} components unhealthy"
    else:
        measured = f"All {total_items} components healthy"
    return updated_rows, measured, None
