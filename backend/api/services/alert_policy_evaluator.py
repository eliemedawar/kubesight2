"""Evaluate alert policies against live or mock cluster signals."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

from ..access_engine import can_access_cluster, can_view_alert, is_admin
from ..alert_policy_catalog import METRIC_BY_KEY, evaluate_conditions
from ..db import db
from ..k8s_provider import K8sCommandError, _run_for_access, resolve_cluster_access, should_use_real_k8s
from ..mock_data import ALERTS, CLUSTER_OVERVIEWS
from ..models import AlertHistory, AlertPolicy, User


def _iso_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _scope_targets(scope: Dict[str, Any]) -> List[Dict[str, Optional[str]]]:
    scope_type = str(scope.get("type") or "cluster").lower()
    namespace = str(scope.get("namespace") or "").strip() or None
    resource_name = str(scope.get("resourceName") or "").strip() or None
    if scope_type == "cluster":
        return [{"namespace": None, "resourceType": "cluster", "resourceName": None}]
    if scope_type == "namespace":
        return [{"namespace": namespace, "resourceType": "namespace", "resourceName": namespace}]
    return [
        {
            "namespace": namespace,
            "resourceType": scope_type,
            "resourceName": resource_name,
        }
    ]


def _alert_key(policy_id: int, cluster_id: str, target: Dict[str, Optional[str]]) -> str:
    return ":".join(
        [
            f"policy-{policy_id}",
            cluster_id,
            target.get("namespace") or "*",
            target.get("resourceType") or "cluster",
            target.get("resourceName") or "*",
        ]
    )


def _collect_cluster_cpu_memory(access, namespace: Optional[str]) -> Tuple[Optional[float], Optional[float]]:
    from ..k8s_metrics import cluster_utilization_metrics

    try:
        metrics = cluster_utilization_metrics(access)
        return metrics.get("cpuPercent"), metrics.get("memoryPercent")
    except Exception:
        return None, None


def _collect_real_observations(
    access,
    cluster_id: str,
    target: Dict[str, Optional[str]],
    metric_keys: List[str],
) -> List[Dict[str, Any]]:
    namespace = target.get("namespace")
    resource_type = target.get("resourceType")
    resource_name = target.get("resourceName")
    observations: List[Dict[str, Any]] = []

    cpu_pct, mem_pct = (None, None)
    if "cpu_usage_percent" in metric_keys or "memory_usage_percent" in metric_keys:
        if resource_type == "cluster":
            cpu_pct, mem_pct = _collect_cluster_cpu_memory(access, None)
        elif resource_type == "namespace" and namespace:
            from ..k8s_metrics import aggregate_pod_top_by_namespace, cluster_utilization_metrics

            cluster_cpu, cluster_mem = _collect_cluster_cpu_memory(access, None)
            ns_usage = aggregate_pod_top_by_namespace(access).get(namespace, {})
            if cluster_cpu and ns_usage.get("cpu") is not None:
                cpu_pct = min(100.0, (float(ns_usage["cpu"]) / float(cluster_cpu)) * 100.0)
            if cluster_mem and ns_usage.get("memory_mib") is not None:
                mem_pct = min(100.0, (float(ns_usage["memory_mib"]) / float(cluster_mem)) * 100.0)
            if cpu_pct is None and mem_pct is None:
                cpu_metric, mem_metric = cluster_utilization_metrics(access)
                cpu_pct = cpu_metric.get("percent") if isinstance(cpu_metric, dict) else None
                mem_pct = mem_metric.get("percent") if isinstance(mem_metric, dict) else None

    if "cpu_usage_percent" in metric_keys and cpu_pct is not None:
        observations.append({"metricKey": "cpu_usage_percent", "value": round(float(cpu_pct), 2)})
    if "memory_usage_percent" in metric_keys and mem_pct is not None:
        observations.append({"metricKey": "memory_usage_percent", "value": round(float(mem_pct), 2)})

    pod_metrics = {
        "pod_restart_count",
        "pod_crashloop_backoff",
        "pod_pending",
    }
    if pod_metrics.intersection(metric_keys):
        ns_flag = ["-n", namespace] if namespace else ["-A"]
        try:
            pods_output = _run_for_access(access, ["get", "pods", *ns_flag, "-o", "json"])
            pod_items = json.loads(pods_output).get("items", [])
        except K8sCommandError:
            pod_items = []

        for pod in pod_items:
            meta = pod.get("metadata", {})
            pod_name = meta.get("name")
            pod_ns = meta.get("namespace")
            if resource_type == "pod" and resource_name and pod_name != resource_name:
                continue
            if resource_type == "namespace" and namespace and pod_ns != namespace:
                continue

            status = pod.get("status", {})
            phase = status.get("phase", "")
            restarts = sum(c.get("restartCount", 0) for c in status.get("containerStatuses") or [])
            crash_loop = any(
                (c.get("state", {}).get("waiting") or {}).get("reason") == "CrashLoopBackOff"
                for c in status.get("containerStatuses") or []
            )

            if resource_type in ("cluster", "namespace", "deployment"):
                if "pod_restart_count" in metric_keys:
                    observations.append(
                        {
                            "metricKey": "pod_restart_count",
                            "value": restarts,
                            "namespace": pod_ns,
                            "resourceType": "pod",
                            "resourceName": pod_name,
                        }
                    )
                if "pod_crashloop_backoff" in metric_keys and crash_loop:
                    observations.append(
                        {
                            "metricKey": "pod_crashloop_backoff",
                            "value": True,
                            "namespace": pod_ns,
                            "resourceType": "pod",
                            "resourceName": pod_name,
                        }
                    )
                if "pod_pending" in metric_keys and phase == "Pending":
                    observations.append(
                        {
                            "metricKey": "pod_pending",
                            "value": True,
                            "namespace": pod_ns,
                            "resourceType": "pod",
                            "resourceName": pod_name,
                        }
                    )
            else:
                if "pod_restart_count" in metric_keys:
                    observations.append({"metricKey": "pod_restart_count", "value": restarts})
                if "pod_crashloop_backoff" in metric_keys:
                    observations.append({"metricKey": "pod_crashloop_backoff", "value": crash_loop})
                if "pod_pending" in metric_keys:
                    observations.append({"metricKey": "pod_pending", "value": phase == "Pending"})
            if resource_type == "pod":
                break

    if "node_not_ready" in metric_keys or "disk_usage_percent" in metric_keys:
        try:
            nodes_output = _run_for_access(access, ["get", "nodes", "-o", "json"])
            node_items = json.loads(nodes_output).get("items", [])
        except K8sCommandError:
            node_items = []
        for node in node_items:
            name = node.get("metadata", {}).get("name")
            ready = any(
                c.get("type") == "Ready" and c.get("status") == "True"
                for c in node.get("status", {}).get("conditions") or []
            )
            disk_pressure = any(
                c.get("type") == "DiskPressure" and c.get("status") == "True"
                for c in node.get("status", {}).get("conditions") or []
            )
            if "node_not_ready" in metric_keys:
                observations.append(
                    {
                        "metricKey": "node_not_ready",
                        "value": not ready,
                        "resourceType": "node",
                        "resourceName": name,
                    }
                )
            if "disk_usage_percent" in metric_keys and disk_pressure:
                observations.append(
                    {
                        "metricKey": "disk_usage_percent",
                        "value": 90.0,
                        "resourceType": "node",
                        "resourceName": name,
                    }
                )

    if "deployment_unavailable_replicas" in metric_keys:
        ns_flag = ["-n", namespace] if namespace else ["-A"]
        try:
            dep_output = _run_for_access(access, ["get", "deployments", *ns_flag, "-o", "json"])
            dep_items = json.loads(dep_output).get("items", [])
        except K8sCommandError:
            dep_items = []
        for dep in dep_items:
            meta = dep.get("metadata", {})
            dep_name = meta.get("name")
            dep_ns = meta.get("namespace")
            if resource_type == "deployment" and resource_name and dep_name != resource_name:
                continue
            unavailable = dep.get("status", {}).get("unavailableReplicas") or 0
            observations.append(
                {
                    "metricKey": "deployment_unavailable_replicas",
                    "value": int(unavailable),
                    "namespace": dep_ns,
                    "resourceType": "deployment",
                    "resourceName": dep_name,
                }
            )

    if "pvc_usage_percent" in metric_keys:
        ns_flag = ["-n", namespace] if namespace else ["-A"]
        try:
            pvc_output = _run_for_access(access, ["get", "pvc", *ns_flag, "-o", "json"])
            pvc_items = json.loads(pvc_output).get("items", [])
        except K8sCommandError:
            pvc_items = []
        for pvc in pvc_items:
            meta = pvc.get("metadata", {})
            pvc_name = meta.get("name")
            pvc_ns = meta.get("namespace")
            status = pvc.get("status", {}) or {}
            capacity = status.get("capacity", {}).get("storage", "")
            # Without volume metrics, treat Bound PVC without capacity signal as 0; Pending as high usage.
            phase = status.get("phase", "")
            value = 85.0 if phase == "Pending" else 0.0
            if capacity:
                value = 50.0
            observations.append(
                {
                    "metricKey": "pvc_usage_percent",
                    "value": value,
                    "namespace": pvc_ns,
                    "resourceType": "pvc",
                    "resourceName": pvc_name,
                }
            )

    return observations


def _mock_observations(cluster_id: str, target: Dict[str, Optional[str]], metric_keys: List[str]) -> List[Dict[str, Any]]:
    overview = CLUSTER_OVERVIEWS.get(cluster_id, {})
    cpu = overview.get("cpuUsage")
    memory = overview.get("memoryUsage")
    observations: List[Dict[str, Any]] = []
    if "cpu_usage_percent" in metric_keys and cpu is not None:
        observations.append({"metricKey": "cpu_usage_percent", "value": float(cpu)})
    if "memory_usage_percent" in metric_keys and memory is not None:
        observations.append({"metricKey": "memory_usage_percent", "value": float(memory)})

    for alert in ALERTS:
        if alert.get("clusterId") != cluster_id:
            continue
        if "pod_restart_count" in metric_keys:
            observations.append({"metricKey": "pod_restart_count", "value": alert.get("restarts", 3)})
        if "cpu_usage_percent" in metric_keys and alert.get("cpuPercent") is not None:
            observations.append({"metricKey": "cpu_usage_percent", "value": float(alert["cpuPercent"])})

    if "node_not_ready" in metric_keys:
        observations.append({"metricKey": "node_not_ready", "value": False})
    if "pod_crashloop_backoff" in metric_keys:
        observations.append({"metricKey": "pod_crashloop_backoff", "value": False})
    if "pod_pending" in metric_keys:
        observations.append({"metricKey": "pod_pending", "value": False})
    if "deployment_unavailable_replicas" in metric_keys:
        observations.append({"metricKey": "deployment_unavailable_replicas", "value": 0})
    if "pvc_usage_percent" in metric_keys:
        observations.append({"metricKey": "pvc_usage_percent", "value": 40.0})
    if "disk_usage_percent" in metric_keys:
        observations.append({"metricKey": "disk_usage_percent", "value": 55.0})
    return observations


def _pick_evaluation_observations(
    observations: List[Dict[str, Any]],
    conditions: List[Dict[str, Any]],
    logic: str,
) -> Tuple[bool, List[Dict[str, Any]], Dict[str, Any]]:
    """Aggregate list observations (per pod/node) into policy-level match."""
    metric_keys = [c.get("metricKey") for c in conditions if c.get("metricKey")]
    scalar_obs = [o for o in observations if o.get("metricKey") in metric_keys and "resourceName" not in o]
    resource_obs = [o for o in observations if o.get("resourceName")]

    if resource_obs:
        grouped: Dict[str, List[Dict[str, Any]]] = {}
        for obs in resource_obs:
            key = f"{obs.get('resourceType')}:{obs.get('namespace')}:{obs.get('resourceName')}"
            grouped.setdefault(key, []).append(obs)
        for group in grouped.values():
            matched, details = evaluate_conditions(group, conditions, logic)
            if matched:
                sample = group[0]
                return True, details, sample
        return False, [], {}

    matched, details = evaluate_conditions(scalar_obs, conditions, logic)
    return matched, details, {}


def _history_to_alert_dict(row: AlertHistory) -> Dict[str, Any]:
    return {
        "id": f"history-{row.id}",
        "severity": row.severity,
        "clusterId": row.cluster_id,
        "namespace": row.namespace,
        "pod": row.resource_name if row.resource_type == "pod" else None,
        "resourceType": row.resource_type,
        "resourceName": row.resource_name,
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


def evaluate_policies_for_cluster(
    cluster_id: str,
    user: Optional[User] = None,
    *,
    persist: bool = True,
) -> List[AlertHistory]:
    if user and not is_admin(user) and not can_access_cluster(user, cluster_id):
        return []

    policies = (
        AlertPolicy.query.filter_by(cluster_id=cluster_id, enabled=True)
        .order_by(AlertPolicy.id.asc())
        .all()
    )
    if not policies:
        return []

    access = resolve_cluster_access(cluster_id) if should_use_real_k8s(cluster_id) else None
    updated_rows: List[AlertHistory] = []
    now = datetime.now(timezone.utc)

    for policy in policies:
        conditions = policy.conditions or []
        if not conditions:
            continue
        metric_keys = [c.get("metricKey") for c in conditions if c.get("metricKey") in METRIC_BY_KEY]
        logic = policy.condition_logic or "any"

        for target in _scope_targets(policy.scope or {}):
            if access:
                observations = _collect_real_observations(access, cluster_id, target, metric_keys)
            else:
                observations = _mock_observations(cluster_id, target, metric_keys)

            matched, details, sample = _pick_evaluation_observations(observations, conditions, logic)
            key = _alert_key(policy.id, cluster_id, target)
            row = AlertHistory.query.filter_by(alert_key=key).first()

            namespace = sample.get("namespace") or target.get("namespace")
            resource_type = sample.get("resourceType") or target.get("resourceType")
            resource_name = sample.get("resourceName") or target.get("resourceName")

            if user and not is_admin(user):
                if not can_view_alert(user, cluster_id, namespace, resource_name or ""):
                    continue

            if matched:
                title = f"{policy.name} triggered"
                description = "; ".join(
                    f"{d.get('metricLabel') or d.get('metricKey')} {d.get('operator')} {d.get('threshold')} (observed {d.get('observedValue')})"
                    for d in details
                    if d.get("matched")
                )
                if not row:
                    row = AlertHistory(
                        alert_key=key,
                        policy_id=policy.id,
                        policy_name=policy.name,
                        cluster_id=cluster_id,
                        namespace=namespace,
                        resource_type=resource_type,
                        resource_name=resource_name,
                        severity=policy.severity,
                        status="active",
                        title=title,
                        description=description,
                        triggered_conditions=details,
                        metric_snapshot={"observations": observations},
                        fired_at=now,
                    )
                    db.session.add(row)
                    if persist:
                        db.session.flush()
                        from ..alert_notifier import dispatch_policy_alert_notifications

                        dispatch_policy_alert_notifications(
                            policy.notification_channels or [],
                            _history_to_alert_dict(row),
                        )
                else:
                    row.status = "active"
                    row.resolved_at = None
                    row.title = title
                    row.description = description
                    row.triggered_conditions = details
                    row.metric_snapshot = {"observations": observations}
                    row.severity = policy.severity
                    row.fired_at = row.fired_at or now
                updated_rows.append(row)
            elif row and row.status == "active":
                row.status = "resolved"
                row.resolved_at = now
                updated_rows.append(row)

    if persist and updated_rows:
        db.session.commit()
    return [r for r in updated_rows if r.status == "active"]


def list_active_policy_alerts(
    cluster_id: Optional[str] = None,
    user: Optional[User] = None,
) -> List[Dict[str, Any]]:
    if cluster_id:
        evaluate_policies_for_cluster(cluster_id, user=user, persist=True)

    query = AlertHistory.query.filter_by(status="active")
    if cluster_id:
        query = query.filter_by(cluster_id=cluster_id)

    items: List[Dict[str, Any]] = []
    for row in query.order_by(AlertHistory.fired_at.desc()).all():
        if user and not is_admin(user):
            if not can_view_alert(user, row.cluster_id, row.namespace, row.resource_name or ""):
                continue
        items.append(_history_to_alert_dict(row))
    return items


def list_alert_history(
    *,
    cluster_id: Optional[str] = None,
    status: Optional[str] = None,
    limit: int = 100,
    user: Optional[User] = None,
) -> List[Dict[str, Any]]:
    query = AlertHistory.query
    if cluster_id:
        query = query.filter_by(cluster_id=cluster_id)
    if status:
        query = query.filter_by(status=status)

    rows = query.order_by(AlertHistory.fired_at.desc()).limit(limit).all()
    results: List[Dict[str, Any]] = []
    for row in rows:
        if user and not is_admin(user):
            if not can_view_alert(user, row.cluster_id, row.namespace, row.resource_name or ""):
                continue
        payload = _history_to_alert_dict(row)
        payload["status"] = row.status
        payload["resolvedAt"] = row.resolved_at.isoformat() if row.resolved_at else None
        results.append(payload)
    return results
