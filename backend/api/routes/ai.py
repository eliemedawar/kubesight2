"""Read-only AI API endpoints consumed by the Hermes Agent."""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from flask import Blueprint, request

from ..access_engine import filter_clusters_for_user, filter_namespaces_for_user
from ..audit import log_audit
from ..auth_utils import get_current_user
from ..decorators import require_permission
from ..k8s_provider import (
    cluster_overview_from_k8s,
    list_clusters_from_k8s,
    list_namespaces_from_k8s,
    list_nodes_from_k8s,
    namespace_resource_list_from_k8s,
    resolve_cluster_access,
    should_use_real_k8s,
)
from ..mock_data import ALERTS, CLUSTER_NODES, CLUSTER_OVERVIEWS, CLUSTERS, NAMESPACES
from ..response import error_response, success_response
from ..services.alert_policy_evaluator import list_active_policy_alerts

logger = logging.getLogger(__name__)

ai_bp = Blueprint("ai", __name__, url_prefix="/api/ai")

_AI_TOOLS_AVAILABLE = 7


# ---------------------------------------------------------------------------
# Audit helper
# ---------------------------------------------------------------------------

def _audit(endpoint: str, user, details: Optional[Dict[str, Any]] = None) -> None:
    try:
        log_audit(
            f"ai.{endpoint}",
            actor=user,
            target_type="ai_endpoint",
            target_id=endpoint,
            details=details or {},
        )
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

def _cluster_status(pods_failed: int, pods_pending: int, critical: int, warning: int) -> str:
    if critical > 0 or pods_failed > 0:
        return "critical"
    if warning > 0 or pods_pending > 0:
        return "warning"
    return "healthy"


def _count_alerts_by_severity(alerts: List[Dict[str, Any]], cluster_id: str) -> Dict[str, int]:
    counts: Dict[str, int] = {"critical": 0, "warning": 0, "info": 0}
    for a in alerts:
        if a.get("clusterId") != cluster_id and a.get("cluster_id") != cluster_id:
            continue
        sev = (a.get("severity") or "").lower()
        if sev in counts:
            counts[sev] += 1
    return counts


def _get_visible_clusters(user) -> List[Dict[str, Any]]:
    if should_use_real_k8s():
        try:
            raw = list_clusters_from_k8s()
            all_clusters = raw.get("items", raw) if isinstance(raw, dict) else raw
        except Exception as exc:
            logger.error("ai: cluster list fetch failed: %s", exc)
            all_clusters = []
    else:
        all_clusters = list(CLUSTERS)
    return filter_clusters_for_user(user, all_clusters)


def _get_cluster_alerts(cluster_id: str, user) -> List[Dict[str, Any]]:
    policy_alerts: List[Dict[str, Any]] = []
    try:
        policy_alerts = list_active_policy_alerts(cluster_id=cluster_id, user=user, evaluate=False)
    except Exception:
        pass
    mock_cluster_alerts = [a for a in ALERTS if a.get("clusterId") == cluster_id]
    return policy_alerts + mock_cluster_alerts


# ---------------------------------------------------------------------------
# Cluster summary helpers
# ---------------------------------------------------------------------------

def _summarize_cluster_mock(cl: Dict[str, Any], user) -> Dict[str, Any]:
    cid = cl.get("id", "")
    overview = CLUSTER_OVERVIEWS.get(cid, {})
    pods = overview.get("pods") or {}
    pods_running = pods.get("running", 0)
    pods_pending = pods.get("pending", 0)
    pods_failed = pods.get("failed", 0)
    pods_unhealthy = pods_pending + pods_failed

    raw_nodes = CLUSTER_NODES.get(cid, [])
    nodes_ready = sum(1 for n in raw_nodes if n.get("status") == "Ready")
    nodes_total = len(raw_nodes)

    all_alerts = _get_cluster_alerts(cid, user)
    alert_counts = _count_alerts_by_severity(all_alerts, cid)
    status = _cluster_status(pods_failed, pods_pending, alert_counts["critical"], alert_counts["warning"])

    return {
        "id": cid,
        "name": cl.get("name", cid),
        "status": status,
        "nodes_ready": nodes_ready,
        "nodes_total": nodes_total,
        "pods_running": pods_running,
        "pods_unhealthy": pods_unhealthy,
        "critical_alerts": alert_counts["critical"],
        "warning_alerts": alert_counts["warning"],
    }


def _summarize_cluster_real(cl: Dict[str, Any], user) -> Dict[str, Any]:
    cid = cl.get("id", "")
    name = cl.get("name", cid)
    try:
        access = resolve_cluster_access(cid)
        if not access:
            return {"id": cid, "name": name, "status": "unreachable",
                    "nodes_ready": 0, "nodes_total": 0, "pods_running": 0,
                    "pods_unhealthy": 0, "critical_alerts": 0, "warning_alerts": 0}

        overview = cluster_overview_from_k8s(access)
        pods = overview.get("pods") or {}
        pods_running = pods.get("running", 0)
        pods_pending = pods.get("pending", 0)
        pods_failed = pods.get("failed", 0)
        pods_unhealthy = pods_pending + pods_failed

        raw_nodes = list_nodes_from_k8s(access)
        nodes_ready = sum(1 for n in raw_nodes if n.get("status") == "Ready")
        nodes_total = len(raw_nodes)
    except Exception as exc:
        logger.warning("ai/cluster-summary: cluster %s unreachable: %s", cid, exc)
        return {"id": cid, "name": name, "status": "unreachable",
                "nodes_ready": 0, "nodes_total": 0, "pods_running": 0,
                "pods_unhealthy": 0, "critical_alerts": 0, "warning_alerts": 0}

    alert_counts: Dict[str, int] = {"critical": 0, "warning": 0}
    try:
        policy_alerts = list_active_policy_alerts(cluster_id=cid, user=user, evaluate=False)
        alert_counts = _count_alerts_by_severity(policy_alerts, cid)
    except Exception as exc:
        logger.warning("ai/cluster-summary: alert fetch failed for %s: %s", cid, exc)

    status = _cluster_status(pods_failed, pods_pending, alert_counts["critical"], alert_counts["warning"])
    return {
        "id": cid,
        "name": name,
        "status": status,
        "nodes_ready": nodes_ready,
        "nodes_total": nodes_total,
        "pods_running": pods_running,
        "pods_unhealthy": pods_unhealthy,
        "critical_alerts": alert_counts["critical"],
        "warning_alerts": alert_counts["warning"],
    }


# ---------------------------------------------------------------------------
# GET /api/ai/health  (no auth required)
# ---------------------------------------------------------------------------

@ai_bp.route("/health", methods=["GET"])
def ai_health():
    cluster_count = 0
    try:
        if should_use_real_k8s():
            raw = list_clusters_from_k8s()
            cluster_count = len(raw.get("items", raw) if isinstance(raw, dict) else raw)
        else:
            cluster_count = len(CLUSTERS)
    except Exception:
        pass
    return success_response({
        "status": "healthy",
        "clusters": cluster_count,
        "tools_available": _AI_TOOLS_AVAILABLE,
        "mode": "real" if should_use_real_k8s() else "mock",
    })


# ---------------------------------------------------------------------------
# GET /api/ai/cluster-summary
# ---------------------------------------------------------------------------

@ai_bp.route("/cluster-summary", methods=["GET"])
@require_permission("overview:view")
def cluster_summary():
    user = get_current_user()
    single_cluster_id: Optional[str] = request.args.get("cluster_id", "").strip() or None

    visible_clusters = _get_visible_clusters(user)
    if single_cluster_id:
        visible_clusters = [c for c in visible_clusters if c.get("id") == single_cluster_id]

    summaries: List[Dict[str, Any]] = []
    for cl in visible_clusters:
        cid = cl.get("id", "")
        if should_use_real_k8s(cid):
            entry = _summarize_cluster_real(cl, user)
        else:
            entry = _summarize_cluster_mock(cl, user)
        summaries.append(entry)

    _audit("cluster-summary", user, {"cluster_filter": single_cluster_id, "count": len(summaries)})
    return success_response({"clusters": summaries})


# ---------------------------------------------------------------------------
# GET /api/ai/alerts-summary
# ---------------------------------------------------------------------------

@ai_bp.route("/alerts-summary", methods=["GET"])
@require_permission("alerts:view")
def alerts_summary():
    user = get_current_user()
    cluster_id_filter: Optional[str] = request.args.get("cluster_id", "").strip() or None

    visible_clusters = _get_visible_clusters(user)
    if cluster_id_filter:
        visible_clusters = [c for c in visible_clusters if c.get("id") == cluster_id_filter]

    total = 0
    by_severity: Dict[str, int] = {"critical": 0, "warning": 0, "info": 0}
    by_cluster: Dict[str, Any] = {}
    alert_list: List[Dict[str, Any]] = []

    for cl in visible_clusters:
        cid = cl.get("id", "")
        cluster_alerts = _get_cluster_alerts(cid, user)
        counts = _count_alerts_by_severity(cluster_alerts, cid)
        cluster_total = sum(counts.values())
        total += cluster_total
        for sev, cnt in counts.items():
            by_severity[sev] = by_severity.get(sev, 0) + cnt
        by_cluster[cid] = {"name": cl.get("name", cid), **counts, "total": cluster_total}

        for a in cluster_alerts:
            alert_list.append({
                "id": a.get("id") or a.get("alert_key", ""),
                "title": a.get("title") or a.get("name", ""),
                "severity": (a.get("severity") or "info").lower(),
                "cluster": cid,
                "cluster_name": cl.get("name", cid),
                "namespace": a.get("namespace") or "",
                "resource": a.get("pod") or a.get("resource_name") or "",
                "status": a.get("status", "firing"),
                "fired_at": a.get("firedAt") or a.get("fired_at") or "",
            })

    _audit("alerts-summary", user, {"clusters_checked": len(visible_clusters), "total": total})
    return success_response({
        "total": total,
        "by_severity": by_severity,
        "by_cluster": by_cluster,
        "alerts": alert_list[:100],
    })


# ---------------------------------------------------------------------------
# GET /api/ai/unhealthy-pods
# ---------------------------------------------------------------------------

def _unhealthy_pods_mock(visible_clusters: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    pods: List[Dict[str, Any]] = []
    for cl in visible_clusters:
        cid = cl.get("id", "")
        overview = CLUSTER_OVERVIEWS.get(cid, {})
        pod_counts = overview.get("pods") or {}
        failed = pod_counts.get("failed", 0)
        pending = pod_counts.get("pending", 0)
        if failed == 0 and pending == 0:
            continue

        # Use alert data to surface real pod names where available
        alerts_with_pods = [
            a for a in ALERTS
            if a.get("clusterId") == cid and a.get("pod")
        ]
        for a in alerts_with_pods:
            pods.append({
                "name": a.get("pod", ""),
                "namespace": a.get("namespace", ""),
                "cluster": cid,
                "cluster_name": cl.get("name", cid),
                "status": "CrashLoopBackOff" if a.get("severity") == "critical" else "Pending",
                "restarts": 0,
            })

        remaining = max(0, (failed + pending) - len(alerts_with_pods))
        ns_list = NAMESPACES.get(cid, [{"name": "default"}])
        default_ns = ns_list[0].get("name", "default") if ns_list else "default"
        for i in range(remaining):
            pods.append({
                "name": f"pod-unknown-{i + 1}",
                "namespace": default_ns,
                "cluster": cid,
                "cluster_name": cl.get("name", cid),
                "status": "Failed" if i < failed else "Pending",
                "restarts": 0,
            })
    return pods


def _unhealthy_pods_real(visible_clusters: List[Dict[str, Any]], user) -> List[Dict[str, Any]]:
    pods: List[Dict[str, Any]] = []
    for cl in visible_clusters:
        cid = cl.get("id", "")
        try:
            access = resolve_cluster_access(cid)
            if not access:
                continue
            ns_data = list_namespaces_from_k8s(access)
            all_namespaces = ns_data.get("items", []) if isinstance(ns_data, dict) else []
            namespaces = filter_namespaces_for_user(user, cid, all_namespaces)
            for ns_item in namespaces[:30]:  # cap at 30 namespaces
                ns = ns_item.get("name", "")
                if not ns:
                    continue
                try:
                    result = namespace_resource_list_from_k8s(access, ns, "pods")
                    for p in result.get("pods", []):
                        phase = (p.get("status") or "Unknown")
                        restarts = p.get("restarts", 0)
                        if phase.lower() in ("running", "succeeded") and restarts <= 5:
                            continue
                        pods.append({
                            "name": p.get("name", ""),
                            "namespace": ns,
                            "cluster": cid,
                            "cluster_name": cl.get("name", cid),
                            "status": phase,
                            "restarts": restarts,
                        })
                except Exception:
                    continue
        except Exception as exc:
            logger.warning("ai/unhealthy-pods: cluster %s error: %s", cid, exc)
    return pods


@ai_bp.route("/unhealthy-pods", methods=["GET"])
@require_permission("pods:view")
def unhealthy_pods():
    user = get_current_user()
    cluster_id_filter: Optional[str] = request.args.get("cluster_id", "").strip() or None

    visible_clusters = _get_visible_clusters(user)
    if cluster_id_filter:
        visible_clusters = [c for c in visible_clusters if c.get("id") == cluster_id_filter]

    if should_use_real_k8s():
        pods = _unhealthy_pods_real(visible_clusters, user)
    else:
        pods = _unhealthy_pods_mock(visible_clusters)

    _audit("unhealthy-pods", user, {"total": len(pods)})
    return success_response({"total": len(pods), "pods": pods})


# ---------------------------------------------------------------------------
# GET /api/ai/deployments-health
# ---------------------------------------------------------------------------

def _deployments_health_mock(visible_clusters: List[Dict[str, Any]]) -> Dict[str, Any]:
    total = 0
    healthy_count = 0
    degraded_count = 0
    degraded_list: List[Dict[str, Any]] = []

    for cl in visible_clusters:
        cid = cl.get("id", "")
        overview = CLUSTER_OVERVIEWS.get(cid, {})
        workloads = overview.get("workloads") or {}
        cluster_total = workloads.get("deployments", 0)
        pods = overview.get("pods") or {}
        cluster_degraded = min(pods.get("failed", 0), cluster_total)
        total += cluster_total
        degraded_count += cluster_degraded
        healthy_count += max(0, cluster_total - cluster_degraded)

        if cluster_degraded > 0:
            degraded_list.append({
                "name": "<see-cluster-details>",
                "namespace": "multiple",
                "cluster": cid,
                "cluster_name": cl.get("name", cid),
                "desired": 1,
                "available": 0,
                "ready": 0,
                "status": "degraded",
                "estimated_count": cluster_degraded,
            })

    return {
        "total": total,
        "healthy": healthy_count,
        "degraded": degraded_count,
        "degraded_deployments": degraded_list,
    }


def _deployments_health_real(visible_clusters: List[Dict[str, Any]], user) -> Dict[str, Any]:
    total = 0
    healthy_count = 0
    degraded_count = 0
    degraded_list: List[Dict[str, Any]] = []

    for cl in visible_clusters:
        cid = cl.get("id", "")
        try:
            access = resolve_cluster_access(cid)
            if not access:
                continue
            ns_data = list_namespaces_from_k8s(access)
            all_namespaces = ns_data.get("items", []) if isinstance(ns_data, dict) else []
            namespaces = filter_namespaces_for_user(user, cid, all_namespaces)
            for ns_item in namespaces[:30]:
                ns = ns_item.get("name", "")
                if not ns:
                    continue
                try:
                    result = namespace_resource_list_from_k8s(access, ns, "deployments")
                    for d in result.get("deployments", []):
                        total += 1
                        desired = d.get("desired", 0)
                        available = d.get("available", 0)
                        ready = d.get("ready", 0)
                        if desired == 0 or available >= desired:
                            healthy_count += 1
                        else:
                            degraded_count += 1
                            degraded_list.append({
                                "name": d.get("name", ""),
                                "namespace": ns,
                                "cluster": cid,
                                "cluster_name": cl.get("name", cid),
                                "desired": desired,
                                "available": available,
                                "ready": ready,
                                "status": "degraded" if available > 0 else "down",
                            })
                except Exception:
                    continue
        except Exception as exc:
            logger.warning("ai/deployments-health: cluster %s error: %s", cid, exc)

    return {
        "total": total,
        "healthy": healthy_count,
        "degraded": degraded_count,
        "degraded_deployments": degraded_list,
    }


@ai_bp.route("/deployments-health", methods=["GET"])
@require_permission("deployments:view")
def deployments_health():
    user = get_current_user()
    cluster_id_filter: Optional[str] = request.args.get("cluster_id", "").strip() or None

    visible_clusters = _get_visible_clusters(user)
    if cluster_id_filter:
        visible_clusters = [c for c in visible_clusters if c.get("id") == cluster_id_filter]

    if should_use_real_k8s():
        result = _deployments_health_real(visible_clusters, user)
    else:
        result = _deployments_health_mock(visible_clusters)

    _audit("deployments-health", user, {"total": result.get("total"), "degraded": result.get("degraded")})
    return success_response(result)


# ---------------------------------------------------------------------------
# GET /api/ai/application-services
# ---------------------------------------------------------------------------

@ai_bp.route("/application-services", methods=["GET"])
@require_permission("app_services:view")
def application_services():
    from ..services.application_service_service import list_services, list_services_mock

    user = get_current_user()
    try:
        if should_use_real_k8s():
            data = list_services(user=user)
        else:
            data = list_services_mock()
    except Exception as exc:
        logger.error("ai/application-services: %s", exc)
        return error_response("Failed to fetch services")

    items = data.get("items") or []
    simplified = [
        {
            "id": s.get("id"),
            "name": s.get("name"),
            "description": s.get("description") or "",
            "health": s.get("health") or "unknown",
            "deployment_count": s.get("deploymentCount") or len(s.get("deployments") or []),
            "topology_node_count": len((s.get("topology") or {}).get("nodes") or []),
        }
        for s in items
    ]

    _audit("application-services", user, {"total": len(simplified)})
    return success_response({"total": len(simplified), "services": simplified})


# ---------------------------------------------------------------------------
# GET /api/ai/clients
# ---------------------------------------------------------------------------

@ai_bp.route("/clients", methods=["GET"])
@require_permission("clients:view")
def clients():
    from ..services.client_service import list_clients, list_clients_mock

    user = get_current_user()
    try:
        if should_use_real_k8s():
            data = list_clients(user=user)
        else:
            data = list_clients_mock()
    except Exception as exc:
        logger.error("ai/clients: %s", exc)
        return error_response("Failed to fetch clients")

    items = data.get("items") or []
    simplified = [
        {
            "id": c.get("id"),
            "name": c.get("name"),
            "health": c.get("status") or "unknown",
            "service_count": c.get("serviceCount") or len(c.get("services") or []),
            "services": [s.get("name") for s in (c.get("services") or []) if s.get("name")],
        }
        for c in items
    ]

    _audit("clients", user, {"total": len(simplified)})
    return success_response({"total": len(simplified), "clients": simplified})


# ---------------------------------------------------------------------------
# Shared service-impact logic (no auth decorator — callers are already gated)
# ---------------------------------------------------------------------------

def _build_service_impact_response(service_id: int, user) -> Any:
    from ..models import Client, ClientApplicationService
    from ..services.application_service_service import get_service, get_service_mock

    try:
        if should_use_real_k8s():
            svc_data, err, _ = get_service(service_id, user=user)
        else:
            svc_data, err, _ = get_service_mock(service_id)
        if err or not svc_data:
            return error_response(err or "Service not found", 404)
    except Exception as exc:
        logger.error("ai/service-impact: %s", exc)
        return error_response("Failed to fetch service")

    affected_clients: List[Dict[str, Any]] = []
    try:
        links = ClientApplicationService.query.filter_by(service_id=service_id).all()
        for link in links:
            client = Client.query.get(link.client_id)
            if client:
                affected_clients.append({"id": client.id, "name": client.name})
    except Exception:
        pass

    deployments = svc_data.get("deployments") or []
    topology = svc_data.get("topology") or {}
    nodes = topology.get("nodes") or []
    cluster_ids = {d.get("clusterId") or d.get("cluster") for d in deployments} - {None}
    summary = (
        f"{svc_data.get('name', 'Service')} has {len(deployments)} deployment(s) "
        f"across {len(cluster_ids)} cluster(s) "
        f"and affects {len(affected_clients)} client(s)."
    )

    _audit("service-impact", user, {"service_id": service_id, "affected_clients": len(affected_clients)})
    return success_response({
        "service": {
            "id": svc_data.get("id"),
            "name": svc_data.get("name"),
            "description": svc_data.get("description") or "",
            "health": svc_data.get("health") or "unknown",
        },
        "deployments": [
            {
                "cluster": d.get("clusterId") or d.get("cluster"),
                "namespace": d.get("namespace"),
                "name": d.get("deploymentName") or d.get("name"),
                "health": d.get("health") or d.get("status") or "unknown",
            }
            for d in deployments
        ],
        "topology_nodes": [{"name": n.get("name"), "type": n.get("type")} for n in nodes],
        "affected_clients": affected_clients,
        "summary": summary,
    })


# ---------------------------------------------------------------------------
# GET /api/ai/service-impact/<int:service_id>
# ---------------------------------------------------------------------------

@ai_bp.route("/service-impact/<int:service_id>", methods=["GET"])
@require_permission("app_services:view")
def service_impact(service_id: int):
    user = get_current_user()
    return _build_service_impact_response(service_id, user)


# ---------------------------------------------------------------------------
# GET /api/ai/service-impact-by-name/<name>
# ---------------------------------------------------------------------------

@ai_bp.route("/service-impact-by-name/<path:service_name>", methods=["GET"])
@require_permission("app_services:view")
def service_impact_by_name(service_name: str):
    from ..models import ApplicationService

    user = get_current_user()
    svc = ApplicationService.query.filter(
        ApplicationService.name.ilike(f"%{service_name}%")
    ).first()

    if not svc:
        return error_response(f"No service matching '{service_name}'", 404)

    return _build_service_impact_response(svc.id, user)


# ---------------------------------------------------------------------------
# GET /api/ai/snapshot  — all critical data in one parallel call
# ---------------------------------------------------------------------------

@ai_bp.route("/snapshot", methods=["GET"])
@require_permission("overview:view")
def snapshot():
    from concurrent.futures import ThreadPoolExecutor, as_completed

    user = get_current_user()
    visible_clusters = _get_visible_clusters(user)

    def _clusters():
        summaries = []
        for cl in visible_clusters:
            cid = cl.get("id", "")
            if should_use_real_k8s(cid):
                summaries.append(_summarize_cluster_real(cl, user))
            else:
                summaries.append(_summarize_cluster_mock(cl, user))
        return {"clusters": summaries}

    def _alerts():
        total = 0
        by_severity: Dict[str, int] = {"critical": 0, "warning": 0, "info": 0}
        alert_list: List[Dict[str, Any]] = []
        for cl in visible_clusters:
            cid = cl.get("id", "")
            cluster_alerts = _get_cluster_alerts(cid, user)
            counts = _count_alerts_by_severity(cluster_alerts, cid)
            total += sum(counts.values())
            for sev, cnt in counts.items():
                by_severity[sev] = by_severity.get(sev, 0) + cnt
            for a in cluster_alerts:
                alert_list.append({
                    "id": a.get("id") or a.get("alert_key", ""),
                    "title": a.get("title") or a.get("name", ""),
                    "severity": (a.get("severity") or "info").lower(),
                    "cluster": cid,
                    "namespace": a.get("namespace") or "",
                    "resource": a.get("pod") or a.get("resource_name") or "",
                    "fired_at": a.get("firedAt") or a.get("fired_at") or "",
                })
        return {"total": total, "by_severity": by_severity, "alerts": alert_list[:50]}

    def _pods():
        if should_use_real_k8s():
            pods = _unhealthy_pods_real(visible_clusters)
        else:
            pods = _unhealthy_pods_mock(visible_clusters)
        return {"total": len(pods), "pods": pods}

    def _services():
        from ..services.application_service_service import list_services, list_services_mock
        try:
            data = list_services(user=user) if should_use_real_k8s() else list_services_mock()
            items = data.get("items") or []
            return [
                {
                    "id": s.get("id"),
                    "name": s.get("name"),
                    "health": s.get("health") or "unknown",
                    "deployment_count": s.get("deploymentCount") or len(s.get("deployments") or []),
                }
                for s in items
            ]
        except Exception:
            return []

    tasks = {"clusters": _clusters, "alerts": _alerts, "unhealthy_pods": _pods, "services": _services}
    result: Dict[str, Any] = {}

    with ThreadPoolExecutor(max_workers=4) as pool:
        futures = {pool.submit(fn): key for key, fn in tasks.items()}
        for future in as_completed(futures):
            key = futures[future]
            try:
                result[key] = future.result()
            except Exception as exc:
                logger.warning("ai/snapshot: %s failed: %s", key, exc)
                result[key] = {"error": str(exc)}

    _audit("snapshot", user, {"clusters": len(visible_clusters)})
    return success_response(result)
