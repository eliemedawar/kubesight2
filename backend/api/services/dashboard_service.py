from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import logging
import os
import threading
import time
from typing import Any, Dict, List, Optional, Tuple

from ..access_summary import build_effective_access_summary
from ..access import get_user_cluster_ids, is_admin
from ..access_engine import (
    can_access_cluster,
    filter_alerts_for_user,
    filter_namespaces_for_user,
    get_user_permission_keys,
    user_has_permission,
)
from ..k8s_provider import (
    K8sCommandError,
    resolve_cluster_access,
    should_use_real_k8s,
)
from ..mock_data import ALERTS, CLUSTER_OVERVIEWS, CLUSTERS, NAMESPACES
from ..models import AuditLog, User
from ..serializers import audit_log_to_dict
from ..dashboard_intelligence import evaluate_version_status, utilization_from_overview_resources
from ..upgrade_provider import _fetch_latest_k8s_version

SEVERITY_ORDER = {"critical": 0, "warning": 1, "info": 2}
HEALTH_ORDER = {"critical": 0, "warning": 1, "healthy": 2, "unknown": 3}

logger = logging.getLogger(__name__)

_DASHBOARD_CACHE_TTL_HEALTHY = int(os.getenv("DASHBOARD_CACHE_TTL_SECONDS", "60"))
_DASHBOARD_CACHE_TTL_INCIDENT = int(os.getenv("DASHBOARD_CACHE_TTL_INCIDENT_SECONDS", "20"))
_DASHBOARD_CACHE_TTL_PENDING = int(os.getenv("DASHBOARD_CACHE_TTL_PENDING_SECONDS", "40"))
_dashboard_summary_cache: Dict[str, Tuple[float, Dict[str, Any]]] = {}
_dashboard_summary_cache_lock = threading.Lock()

PROVIDER_DISPLAY = {
    "aws": "EKS",
    "gcp": "GKE",
    "azure": "AKS",
    "kubernetes": "Kubernetes",
    "custom": "Custom",
    "docker-desktop": "Docker Desktop",
    "kind": "kind",
    "minikube": "Minikube",
    "kubeadm": "kubeadm",
    "eks": "EKS",
    "aks": "AKS",
    "gke": "GKE",
    "unknown": "Unknown",
}

ACTIVITY_MESSAGES = {
    "login_success": lambda e: f"User {e.get('actorUsername') or 'unknown'} logged in",
    "logout": lambda e: f"User {e.get('actorUsername') or 'unknown'} logged out",
    "upgrade_precheck_run": lambda e: "Upgrade precheck completed",
    "upgrade_precheck_failed": lambda e: "Upgrade precheck failed",
    "upgrade_plan_generated": lambda e: "Upgrade plan generated",
    "upgrade_start_run": lambda e: "Upgrade workflow started",
    "upgrade_start_blocked": lambda e: "Upgrade blocked by precheck",
    "upgrade_start_failed": lambda e: "Upgrade attempt failed",
    "upgrade_info_viewed": lambda e: "Upgrade information viewed",
    "user_created": lambda e: "User account created",
    "user_disabled": lambda e: "User account disabled",
    "permissions_changed": lambda e: "Role permissions changed",
    "forbidden_access_attempt": lambda e: "Access denied event",
}

OPERATIONAL_ACTIONS = {
    "upgrade_precheck_run",
    "upgrade_precheck_failed",
    "upgrade_plan_generated",
    "upgrade_start_run",
    "upgrade_start_blocked",
    "upgrade_start_failed",
    "upgrade_info_viewed",
    "forbidden_access_attempt",
}

USER_ACTIVITY_ACTIONS = {
    "login_success",
    "logout",
    "user_created",
    "user_disabled",
    "permissions_changed",
}

def _cache_ttl_for_payload(payload: Dict[str, Any]) -> int:
    """Choose cache TTL based on cluster health — shorten it during active incidents."""
    pods = payload.get("pods") or {}
    alerts = payload.get("alerts") or {}
    health = (payload.get("clusterHealth") or payload.get("health") or {}).get("status", "healthy")

    failed = int(pods.get("failed") or 0)
    critical = int(alerts.get("critical") or 0)
    pending = int(pods.get("pending") or 0)

    if failed > 0 or critical > 0 or health == "critical":
        return _DASHBOARD_CACHE_TTL_INCIDENT   # 20s — rapid refresh during incident
    if pending > 0 or health == "warning":
        return _DASHBOARD_CACHE_TTL_PENDING    # 40s — moderate refresh while pods stabilise
    return _DASHBOARD_CACHE_TTL_HEALTHY        # 60s — conservative refresh when all clear


def _dashboard_cache_disabled() -> bool:
    """Disable cache during pytest runs to avoid stale results with patches/mutations."""
    try:
        from flask import current_app

        return bool(getattr(current_app, "config", {}).get("TESTING"))
    except Exception:
        return False


def _dashboard_summary_cache_key(cluster_id: str, user: Optional[User]) -> str:
    if not user:
        return f"cluster:{cluster_id}|user:anon"

    # Permissions are used in the dashboard summary response. Include a signature derived
    # from the user's role to avoid cross-role RBAC leakage.
    perm_keys = sorted(get_user_permission_keys(user))
    return (
        f"cluster:{cluster_id}|user:{user.id}"
        f"|role:{user.role_id}|admin:{is_admin(user)}"
        f"|perms:{','.join(perm_keys)}"
    )


def _display_provider(raw: Optional[str]) -> str:
    if not raw:
        return "Unknown"
    key = str(raw).strip().lower()
    return PROVIDER_DISPLAY.get(key, raw.replace("-", " ").title())


def _severity_bucket(severity: str) -> str:
    value = str(severity or "").lower()
    if value == "critical":
        return "critical"
    if value == "warning":
        return "warning"
    return "info"


def _count_alerts(alerts: List[Dict[str, Any]]) -> Dict[str, int]:
    counts = {"critical": 0, "warning": 0, "info": 0, "total": len(alerts)}
    for alert in alerts:
        bucket = _severity_bucket(alert.get("severity"))
        counts[bucket] += 1
    return counts


def _format_failed_pods_reason(problem_pods: List[Dict[str, Any]], limit: int = 3) -> str:
    named = [
        f"{pod.get('name')} ({pod.get('status')})"
        for pod in problem_pods[:limit]
        if pod.get("name")
    ]
    suffix = f" +{len(problem_pods) - limit} more" if len(problem_pods) > limit else ""
    if not named:
        return f"{len(problem_pods)} failed pod(s)"
    return f"{len(problem_pods)} failed pod(s): " + ", ".join(named) + suffix


def _compute_health(
    *,
    alert_counts: Dict[str, int],
    pods: Dict[str, int],
    ready_nodes: int,
    total_nodes: int,
    problem_pods: Optional[List[Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    reasons: List[str] = []
    failed = len(problem_pods) if problem_pods is not None else int(pods.get("failed") or 0)
    pending = int(pods.get("pending") or 0)

    if alert_counts["critical"] > 0:
        reasons.append(f"{alert_counts['critical']} critical alert(s) active")
    if failed > 0:
        if problem_pods:
            reasons.append(_format_failed_pods_reason(problem_pods))
        else:
            reasons.append(f"{failed} failed pod(s)")
    if total_nodes > 0 and ready_nodes < total_nodes:
        reasons.append(f"{total_nodes - ready_nodes} node(s) not ready")

    if alert_counts["critical"] > 0 or failed > 0 or (total_nodes > 0 and ready_nodes < total_nodes):
        status = "critical"
    elif alert_counts["warning"] > 0 or pending > 0:
        if alert_counts["warning"] > 0:
            reasons.append(f"{alert_counts['warning']} warning alert(s) active")
        if pending > 0:
            reasons.append(f"{pending} pending pod(s)")
        status = "warning"
    else:
        status = "healthy"

    return {"status": status, "reasons": reasons, "failedPods": problem_pods or []}


def _node_readiness(cluster: Dict[str, Any]) -> Tuple[int, int]:
    total = int(cluster.get("nodes") or 0)
    cluster_status = str(cluster.get("status") or "healthy").lower()
    if total <= 0:
        return 0, 0
    if cluster_status in {"healthy", "connected"}:
        return total, total
    if cluster_status == "warning":
        return max(total - 1, 0), total
    return max(total - 2, 0), total


def _namespace_health(
    namespaces: List[Dict[str, Any]],
    alerts: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    alerts_by_ns: Dict[str, List[Dict[str, Any]]] = {}
    for alert in alerts:
        ns = alert.get("namespace") or "default"
        alerts_by_ns.setdefault(ns, []).append(alert)

    seen = set()
    rows: List[Dict[str, Any]] = []

    for ns in namespaces:
        name = ns.get("name", "unknown")
        seen.add(name)
        ns_alerts = alerts_by_ns.get(name, [])
        status = "healthy"
        if any(_severity_bucket(a.get("severity")) == "critical" for a in ns_alerts):
            status = "critical"
        elif ns_alerts:
            status = "warning"
        rows.append(
            {
                "name": name,
                "pods": int(ns.get("pods") or 0),
                "status": status,
                "alertCount": len(ns_alerts),
            }
        )

    for ns_name, ns_alerts in alerts_by_ns.items():
        if ns_name in seen:
            continue
        status = "critical" if any(_severity_bucket(a.get("severity")) == "critical" for a in ns_alerts) else "warning"
        rows.append({"name": ns_name, "pods": 0, "status": status, "alertCount": len(ns_alerts)})

    rows.sort(
        key=lambda row: (
            HEALTH_ORDER.get(row["status"], 3),
            -row["pods"],
            row["name"],
        )
    )
    return rows[:8]


def _format_activity_time(iso_value: Optional[str]) -> str:
    if not iso_value:
        return "—"
    try:
        dt = datetime.fromisoformat(iso_value.replace("Z", "+00:00"))
        return dt.strftime("%H:%M")
    except ValueError:
        return "—"


def _activity_message(entry: Dict[str, Any]) -> str:
    action = entry.get("action") or ""
    formatter = ACTIVITY_MESSAGES.get(action)
    if formatter:
        return formatter(entry)
    target = entry.get("targetType") or "resource"
    target_id = entry.get("targetId") or ""
    return f"{action.replace('_', ' ').title()} on {target}{f' {target_id}' if target_id else ''}"


def _recent_activity(user: Optional[User], cluster_id: str, limit: int = 10) -> List[Dict[str, Any]]:
    if not user or not user_has_permission(user, "audit:view"):
        return []

    allowed_clusters = set(get_user_cluster_ids(user)) if not is_admin(user) else None
    entries = AuditLog.query.order_by(AuditLog.created_at.desc()).limit(200).all()
    items: List[Dict[str, Any]] = []

    for entry in entries:
        serialized = audit_log_to_dict(entry)
        target_type = serialized.get("targetType")
        target_id = serialized.get("targetId")
        action = serialized.get("action") or ""

        if allowed_clusters is not None:
            if target_type == "cluster" and target_id not in allowed_clusters:
                continue
            if action.startswith("upgrade_") and target_id and target_id not in allowed_clusters:
                continue
            if target_type == "cluster" and target_id and target_id != cluster_id:
                continue
            if action in ("login_success", "logout"):
                pass
            elif target_type == "cluster" and target_id != cluster_id:
                continue

        items.append(
            {
                "time": _format_activity_time(serialized.get("createdAt")),
                "message": _activity_message(serialized),
                "action": action,
                "createdAt": serialized.get("createdAt"),
            }
        )
        if len(items) >= limit:
            break

    return items


def _operational_events(user: Optional[User], cluster_id: str, limit: int = 10) -> List[Dict[str, Any]]:
    if not user or user_has_permission(user, "audit:view"):
        return []
    if not user_has_permission(user, "upgrades:precheck"):
        return []

    allowed_clusters = set(get_user_cluster_ids(user)) if not is_admin(user) else None
    entries = AuditLog.query.order_by(AuditLog.created_at.desc()).limit(200).all()
    items: List[Dict[str, Any]] = []

    for entry in entries:
        serialized = audit_log_to_dict(entry)
        action = serialized.get("action") or ""
        if action not in OPERATIONAL_ACTIONS:
            continue

        target_type = serialized.get("targetType")
        target_id = serialized.get("targetId")

        if allowed_clusters is not None:
            if target_id and target_id not in allowed_clusters:
                continue
            if target_type == "cluster" and target_id and target_id != cluster_id:
                continue
            if action.startswith("upgrade_") and target_id and target_id != cluster_id:
                continue

        items.append(
            {
                "time": _format_activity_time(serialized.get("createdAt")),
                "message": _activity_message(serialized),
                "action": action,
                "createdAt": serialized.get("createdAt"),
            }
        )
        if len(items) >= limit:
            break

    return items


def _user_activity(user: Optional[User], limit: int = 10) -> List[Dict[str, Any]]:
    if not user or not user_has_permission(user, "users:view"):
        return []

    entries = AuditLog.query.order_by(AuditLog.created_at.desc()).limit(200).all()
    items: List[Dict[str, Any]] = []

    for entry in entries:
        serialized = audit_log_to_dict(entry)
        action = serialized.get("action") or ""
        if action not in USER_ACTIVITY_ACTIONS:
            continue

        items.append(
            {
                "time": _format_activity_time(serialized.get("createdAt")),
                "message": _activity_message(serialized),
                "action": action,
                "createdAt": serialized.get("createdAt"),
            }
        )
        if len(items) >= limit:
            break

    return items


def _accessible_pod_stats(
    user: Optional[User],
    namespaces: List[Dict[str, Any]],
    overview_pods: Dict[str, Any],
) -> Dict[str, int]:
    if not user or is_admin(user):
        return {
            "running": int(overview_pods.get("running") or 0),
            "pending": int(overview_pods.get("pending") or 0),
            "failed": int(overview_pods.get("failed") or 0),
        }

    accessible_running = sum(int(ns.get("pods") or 0) for ns in namespaces)
    overview_running = int(overview_pods.get("running") or 0) or 1
    scale = accessible_running / overview_running if overview_running else 1.0

    return {
        "running": accessible_running,
        "pending": max(0, round(int(overview_pods.get("pending") or 0) * scale)),
        "failed": max(0, round(int(overview_pods.get("failed") or 0) * scale)),
    }


def _cluster_label_map(
    user: Optional[User],
    cluster: Optional[Dict[str, Any]] = None,
) -> Dict[str, str]:
    labels: Dict[str, str] = {}
    for item in CLUSTERS:
        cluster_id = item.get("id")
        if cluster_id:
            labels[cluster_id] = item.get("name") or cluster_id

    if cluster and cluster.get("id"):
        labels[cluster["id"]] = cluster.get("name") or cluster.get("id")

    if user and not is_admin(user):
        for cluster_id in get_user_cluster_ids(user):
            labels.setdefault(cluster_id, cluster_id)

    return labels


def _build_my_access(
    user: Optional[User],
    cluster_id: str,
    cluster: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    if not user:
        return {
            "clusters": [],
            "namespaces": [],
            "resources": [],
            "permissions": [],
            "counts": {"pods": 0, "deployments": 0, "services": 0, "total": 0},
            "hasAccessibleScope": False,
        }

    summary = build_effective_access_summary(
        user,
        cluster_labels=_cluster_label_map(user, cluster),
        focus_cluster_id=cluster_id,
    )

    if not summary.get("hasAccessibleScope") and can_access_cluster(user, cluster_id):
        summary["hasAccessibleScope"] = True

    return summary


def _latest_precheck_status(user: Optional[User], cluster_id: str) -> Dict[str, Any]:
    if not user or not user_has_permission(user, "upgrades:precheck"):
        return {"status": "none", "at": None}

    entries = (
        AuditLog.query.filter(
            AuditLog.action.in_(("upgrade_precheck_run", "upgrade_precheck_failed")),
            AuditLog.target_type == "cluster",
            AuditLog.target_id == cluster_id,
        )
        .order_by(AuditLog.created_at.desc())
        .limit(1)
        .all()
    )
    if not entries:
        return {"status": "none", "at": None}

    entry = entries[0]
    details = entry.details or {}
    if entry.action == "upgrade_precheck_failed":
        status = "failed"
    elif details.get("canUpgrade") is False:
        status = "failed"
    else:
        status = "passed"
    return {
        "status": status,
        "at": entry.created_at.isoformat() if entry.created_at else None,
    }


def _cluster_record(cluster_id: str) -> Optional[Dict[str, Any]]:
    for cluster in CLUSTERS:
        if cluster.get("id") == cluster_id:
            return cluster
    return None


def _mock_alerts(cluster_id: str) -> List[Dict[str, Any]]:
    return [alert for alert in ALERTS if alert.get("clusterId") == cluster_id]


def _mock_overview(cluster_id: str) -> Optional[Dict[str, Any]]:
    return CLUSTER_OVERVIEWS.get(cluster_id)


def _mock_namespaces(cluster_id: str) -> List[Dict[str, Any]]:
    return NAMESPACES.get(cluster_id, [])


def _run_with_app_context(app, fn, *args, **kwargs):
    with app.app_context():
        return fn(*args, **kwargs)


def _lightweight_upgrade_info_for_dashboard(
    cluster_info: Dict[str, Any],
    target_version: str = "v1.31.0",
) -> Tuple[Optional[Dict[str, Any]], Optional[str], int]:
    """Version/provider snapshot for dashboard — no external HTTP calls.

    latestAvailable is intentionally left "unknown" here; it is filled in
    separately by the pre-fetched version that runs in parallel with kubectl.
    """
    from ..upgrade_provider import get_provider_support

    provider_support = get_provider_support(cluster_info.get("provider", "unknown"))
    version_info = {
        "currentVersion": cluster_info.get("controlPlaneVersion") or "unknown",
        "latestAvailable": "unknown",
        "upgradeSupported": bool(provider_support.get("upgradeSupported")),
        "reason": provider_support.get("reason", ""),
    }
    return (
        {
            "clusterInfo": cluster_info,
            "provider": provider_support,
            "versionInfo": version_info,
        },
        None,
        200,
    )


def _cluster_record_from_access(
    cluster_id: str,
    access: Any,
    cluster_info: Dict[str, Any],
    now: str,
) -> Tuple[Dict[str, Any], int, int]:
    nodes = cluster_info.get("nodes") or []
    cluster = {
        "id": cluster_id,
        "name": access.display_name or cluster_id,
        "provider": cluster_info.get("provider") or "kubernetes",
        "k8sVersion": cluster_info.get("controlPlaneVersion") or "unknown",
        "nodes": len(nodes),
        "status": cluster_info.get("health") or "healthy",
        "contextName": cluster_info.get("contextName") or access.context_name,
        "lastSync": now,
    }
    ready_nodes = sum(1 for node in nodes if node.get("ready"))
    return cluster, ready_nodes, len(nodes)


def _load_real_k8s_dashboard_data(
    cluster_id: str,
    access: Any,
    user: Optional[User],
) -> Tuple[
    Dict[str, Any],
    List[Dict[str, Any]],
    Dict[str, Any],
    List[Dict[str, Any]],
    Dict[str, Any],
    Dict[str, Any],
    Optional[Tuple[Optional[Dict[str, Any]], Optional[str], int]],
    Optional[Dict[str, int]],
    str,
    bool,
]:
    """Fetch Kubernetes inputs once, then derive dashboard sections in memory."""
    from flask import current_app

    from ..dashboard_k8s_snapshot import (
        alerts_from_snapshot,
        cluster_info_from_snapshot,
        fetch_dashboard_k8s_snapshot,
        overview_from_snapshot,
        utilization_from_snapshot,
    )
    from ..services.inventory_service import get_dashboard_inventory_summary

    app = current_app._get_current_object()
    needs_inventory = bool(user and user_has_permission(user, "resources:view"))

    with ThreadPoolExecutor(max_workers=3) as pool:
        snapshot_future = pool.submit(fetch_dashboard_k8s_snapshot, access)
        # Fetch the latest k8s version in parallel with kubectl — it hits dl.k8s.io
        # (5 s timeout) and would otherwise block serially after the snapshot.
        version_latest_future = pool.submit(_fetch_latest_k8s_version)
        inventory_future = (
            pool.submit(
                _run_with_app_context,
                app,
                get_dashboard_inventory_summary,
                user,
                cluster_id,
            )
            if needs_inventory
            else None
        )
        snapshot = snapshot_future.result()
        try:
            prefetched_latest_version: str = version_latest_future.result()
        except Exception:
            prefetched_latest_version = "unknown"
        inventory_summary = inventory_future.result() if inventory_future else None

    try:
        cluster_info = cluster_info_from_snapshot(access, snapshot)
    except Exception:
        cluster_info = {}

    overview = overview_from_snapshot(access, snapshot)
    alerts = alerts_from_snapshot(access, cluster_id, snapshot)
    cpu_usage, memory_usage = utilization_from_snapshot(snapshot)

    upgrade_result = None
    if user and user_has_permission(user, "upgrades:precheck") and cluster_info:
        target = cluster_info.get("controlPlaneVersion") or "v1.31.0"
        upgrade_result = _lightweight_upgrade_info_for_dashboard(cluster_info, target or "v1.31.0")

    return (
        overview,
        snapshot.namespaces,
        cluster_info,
        alerts,
        cpu_usage,
        memory_usage,
        upgrade_result,
        inventory_summary,
        prefetched_latest_version,
        not snapshot.reachable,
    )


def get_dashboard_summary(cluster_id: str, user: Optional[User] = None) -> Tuple[Optional[Dict[str, Any]], Optional[str], int]:
    if not cluster_id:
        return None, "clusterId is required.", 400

    cache_key: Optional[str] = None
    if not _dashboard_cache_disabled():
        cache_key = _dashboard_summary_cache_key(cluster_id, user)
        now_ts = time.time()
        with _dashboard_summary_cache_lock:
            cached = _dashboard_summary_cache.get(cache_key)
        if cached and cached[0] > now_ts:
            logger.info(
                "dashboard_summary cache hit (clusterId=%s userId=%s)",
                cluster_id,
                getattr(user, "id", None),
            )
            return cached[1], None, 200

        logger.info(
            "dashboard_summary cache miss (clusterId=%s userId=%s) - recomputing",
            cluster_id,
            getattr(user, "id", None),
        )

    recompute_started = time.time()
    now = datetime.now(timezone.utc).isoformat()
    cluster: Optional[Dict[str, Any]] = None
    cluster_info: Dict[str, Any] = {}
    overview: Optional[Dict[str, Any]] = None
    namespaces: List[Dict[str, Any]] = []
    alerts: List[Dict[str, Any]] = []
    ready_nodes: Optional[int] = None
    total_nodes: Optional[int] = None
    cpu_usage: Dict[str, Any] = {}
    memory_usage: Dict[str, Any] = {}
    parallel_upgrade_result: Optional[Tuple[Optional[Dict[str, Any]], Optional[str], int]] = None
    parallel_inventory_summary: Optional[Dict[str, int]] = None
    parallel_latest_version: str = "unknown"

    cluster_unreachable = False
    if should_use_real_k8s(cluster_id):
        access = resolve_cluster_access(cluster_id)
        if not access:
            return None, "Cluster not found", 404
        try:
            (
                overview,
                namespaces,
                cluster_info,
                alerts,
                cpu_usage,
                memory_usage,
                parallel_upgrade_result,
                parallel_inventory_summary,
                parallel_latest_version,
                cluster_unreachable,
            ) = _load_real_k8s_dashboard_data(cluster_id, access, user)
            cluster, ready_nodes, total_nodes = _cluster_record_from_access(
                cluster_id,
                access,
                cluster_info,
                now,
            )
        except K8sCommandError as exc:
            return None, f"Failed to load dashboard summary: {exc}", 503
    else:
        cluster = _cluster_record(cluster_id)
        if not cluster:
            return None, "Cluster not found", 404
        overview = _mock_overview(cluster_id)
        namespaces = _mock_namespaces(cluster_id)
        alerts = _mock_alerts(cluster_id)
        cpu_usage, memory_usage = utilization_from_overview_resources(
            (overview or {}).get("resources") or {}
        )

    if user:
        namespaces = filter_namespaces_for_user(user, cluster_id, namespaces)
        if user_has_permission(user, "alerts:view"):
            alerts = filter_alerts_for_user(user, alerts)
        else:
            alerts = []

    if user_has_permission(user, "alerts:view") if user else True:
        from ..services.alert_policy_evaluator import list_active_policy_alerts
        from ..services.alert_policy_service import policy_stats

        policy_alerts = list_active_policy_alerts(
            cluster_id=cluster_id,
            user=user,
            evaluate=False,
        )
        existing_ids = {a.get("id") for a in alerts}
        for policy_alert in policy_alerts:
            if policy_alert.get("id") not in existing_ids:
                alerts.append(policy_alert)
        alert_policy_stats = policy_stats(cluster_id=cluster_id)
    else:
        alert_policy_stats = {
            "activeTotal": 0,
            "critical": 0,
            "warning": 0,
            "info": 0,
            "byCluster": [],
            "bySeverity": {"critical": 0, "warning": 0, "info": 0},
            "topTriggeredPolicies": [],
            "enabledPolicies": 0,
        }

    if not overview:
        overview = {
            "clusterId": cluster_id,
            "pods": {"running": 0, "pending": 0, "failed": 0},
            "updatedAt": now,
        }

    pods = _accessible_pod_stats(user, namespaces, overview.get("pods") or {})
    # None in mock mode (no per-pod data) → _compute_health keeps count-based reasons.
    problem_pods = overview.get("problemPods")
    if problem_pods and user and not is_admin(user):
        accessible_ns = {ns.get("name") for ns in namespaces}
        problem_pods = [pod for pod in problem_pods if pod.get("namespace") in accessible_ns]
    alert_counts = _count_alerts(alerts)
    if ready_nodes is None or total_nodes is None:
        ready_nodes, total_nodes = _node_readiness(cluster or {})
    health = _compute_health(
        alert_counts=alert_counts,
        pods=pods,
        ready_nodes=ready_nodes,
        total_nodes=total_nodes,
        problem_pods=problem_pods,
    )
    if cluster_unreachable:
        health = {"status": "unreachable", "reasons": ["Cluster is offline or unreachable"]}

    node_status = "healthy"
    if total_nodes > 0 and ready_nodes < total_nodes:
        node_status = "critical" if ready_nodes <= total_nodes // 2 else "warning"

    total_pod_count = sum(int(ns.get("pods") or 0) for ns in namespaces) or int(pods.get("running") or 0)
    provider_raw = cluster.get("provider") if cluster else "unknown"
    provider_key = str(provider_raw or "unknown").lower()

    upgrade_status: Dict[str, Any] = {
        "currentVersion": cluster.get("k8sVersion") if cluster else "unknown",
        "latestAvailable": "unknown",
        "provider": _display_provider(provider_raw),
        "providerKey": provider_key,
        "upgradeSupported": False,
        "reason": "",
        "precheckStatus": "none",
        "lastPrecheckAt": None,
    }

    if user and user_has_permission(user, "upgrades:precheck"):
        if parallel_upgrade_result is not None:
            upgrade_data, _, upgrade_code = parallel_upgrade_result
        else:
            target = cluster.get("k8sVersion") if cluster else "v1.31.0"
            fallback_cluster_info = cluster_info if cluster_info else {
                "controlPlaneVersion": target,
                "provider": provider_key,
            }
            upgrade_data, _, upgrade_code = _lightweight_upgrade_info_for_dashboard(
                fallback_cluster_info,
                target or "v1.31.0",
            )
        if upgrade_data and upgrade_code == 200:
            version_info = upgrade_data.get("versionInfo") or {}
            provider_info = upgrade_data.get("provider") or {}
            provider_key = provider_info.get("provider") or provider_key
            if should_use_real_k8s(cluster_id) and version_info.get("currentVersion"):
                upgrade_status["currentVersion"] = version_info.get("currentVersion")
            upgrade_status.update(
                {
                    "latestAvailable": version_info.get("latestAvailable") or "unknown",
                    "provider": provider_info.get("providerDisplay") or upgrade_status["provider"],
                    "providerKey": provider_key,
                    "upgradeSupported": bool(version_info.get("upgradeSupported")),
                    "reason": version_info.get("reason") or provider_info.get("reason") or "",
                }
            )
        precheck = _latest_precheck_status(user, cluster_id)
        upgrade_status["precheckStatus"] = precheck["status"]
        upgrade_status["lastPrecheckAt"] = precheck["at"]

    version_current = upgrade_status.get("currentVersion") or cluster.get("k8sVersion") if cluster else "unknown"
    version_latest = upgrade_status.get("latestAvailable") or "unknown"
    if version_latest == "unknown":
        # Use the version pre-fetched in parallel with kubectl; fall back to a
        # synchronous call only for mock clusters where parallel_latest_version
        # was never populated.
        if parallel_latest_version and parallel_latest_version != "unknown":
            version_latest = parallel_latest_version
        else:
            fetched_latest = _fetch_latest_k8s_version()
            if fetched_latest != "unknown":
                version_latest = fetched_latest

    version_evaluation = evaluate_version_status(version_current, version_latest)

    inventory_summary = {"applications": 0, "healthy": 0, "warning": 0, "critical": 0, "unknown": 0}
    if parallel_inventory_summary is not None:
        inventory_summary = parallel_inventory_summary
    elif user and user_has_permission(user, "resources:view"):
        from ..services.inventory_service import get_dashboard_inventory_summary

        inventory_summary = get_dashboard_inventory_summary(user, cluster_id)

    payload = {
        "clusterId": cluster_id,
        "lastUpdated": overview.get("updatedAt") or now,
        "inventory": inventory_summary,
        "clusterHealth": health,
        "health": health,
        "cpuUsage": cpu_usage,
        "memoryUsage": memory_usage,
        "version": {
            "current": version_current,
            "latest": version_latest,
            "latestAvailable": version_latest,
            "status": version_evaluation["status"],
            "statusLabel": version_evaluation["label"],
            "statusIcon": version_evaluation["icon"],
            "statusMessage": version_evaluation["message"],
            "minorVersionsBehind": version_evaluation.get("minorVersionsBehind"),
            "provider": upgrade_status.get("provider") or _display_provider(provider_raw),
            "providerKey": provider_key,
            "upgradeSupported": upgrade_status.get("upgradeSupported", False),
            "upgradeSupportReason": upgrade_status.get("reason") or "",
        },
        "nodes": {
            "ready": ready_nodes,
            "total": total_nodes,
            "status": node_status,
        },
        "pods": {
            "running": int(pods.get("running") or 0),
            "pending": int(pods.get("pending") or 0),
            "failed": int(pods.get("failed") or 0),
        },
        "alerts": alert_counts,
        "alertPolicies": alert_policy_stats,
        "clusterInfo": {
            "name": cluster.get("name") if cluster else cluster_id,
            "contextName": cluster.get("contextName") or cluster.get("name") if cluster else cluster_id,
            "provider": upgrade_status.get("provider") or _display_provider(provider_raw),
            "version": version_current,
            "nodeCount": total_nodes,
            "namespaceCount": len(namespaces),
            "podCount": total_pod_count,
            "lastSync": cluster.get("lastSync") if cluster else now,
        },
        "namespaces": _namespace_health(namespaces, alerts),
        "recentActivity": _recent_activity(user, cluster_id),
        "operationalEvents": _operational_events(user, cluster_id),
        "userActivity": _user_activity(user),
        "myAccess": _build_my_access(user, cluster_id, cluster=cluster),
        "upgradeStatus": {
            **upgrade_status,
            "currentVersion": version_current,
            "latestAvailable": version_latest,
            "versionStatus": version_evaluation["status"],
            "versionStatusLabel": version_evaluation["label"],
            "versionStatusMessage": version_evaluation["message"],
        },
        "permissions": {
            "audit": bool(user and user_has_permission(user, "audit:view")),
            "upgrades": bool(user and user_has_permission(user, "upgrades:precheck")),
            "alerts": bool(user and user_has_permission(user, "alerts:view")),
            "users": bool(user and user_has_permission(user, "users:view")),
            "overview": bool(user and user_has_permission(user, "overview:view")),
            "clusters": bool(user and user_has_permission(user, "clusters:view")),
            "namespaces": bool(user and user_has_permission(user, "namespaces:view")),
            "resources": bool(
                user
                and (
                    user_has_permission(user, "resources:view")
                    or user_has_permission(user, "pods:view")
                )
            ),
        },
    }

    if cache_key:
        ttl = _cache_ttl_for_payload(payload)
        with _dashboard_summary_cache_lock:
            _dashboard_summary_cache[cache_key] = (
                time.time() + ttl,
                payload,
            )
        logger.debug(
            "dashboard_summary cache TTL=%ds (clusterId=%s health=%s failed=%s)",
            ttl,
            cluster_id,
            (payload.get("clusterHealth") or {}).get("status"),
            (payload.get("pods") or {}).get("failed"),
        )

    logger.info(
        "dashboard_summary recomputed in %.2fs (clusterId=%s userId=%s)",
        time.time() - recompute_started,
        cluster_id,
        getattr(user, "id", None),
    )

    return payload, None, 200
