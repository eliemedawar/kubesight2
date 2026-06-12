"""Evaluate alert policies against live or mock cluster signals."""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

from ..access_engine import can_access_cluster, can_view_alert, is_admin
from ..alert_policy_catalog import METRIC_BY_KEY, evaluate_conditions
from ..db import db
from ..k8s_provider import K8sCommandError, _run_for_access, resolve_cluster_access, should_use_real_k8s
from ..mock_data import ALERTS, CLUSTER_OVERVIEWS
from ..models import AlertHistory, AlertPolicy, User


def _iso_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _scope_targets(scope: Dict[str, Any]) -> List[Dict[str, Optional[str]]]:
    from ..alert_policy_catalog import ALL_RESOURCES_SCOPE_NAME, normalize_scope

    normalized = normalize_scope(scope)
    scope_type = normalized["type"]
    namespace = normalized["namespace"] or None
    resource_name = normalized["resourceName"]
    if resource_name == ALL_RESOURCES_SCOPE_NAME:
        resource_name = None
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


def _pod_matches_labels(pod: Dict[str, Any], match_labels: Dict[str, str]) -> bool:
    if not match_labels:
        return False
    labels = pod.get("metadata", {}).get("labels") or {}
    return all(labels.get(key) == value for key, value in match_labels.items())


def _pod_matches_deployment_selector(pod: Dict[str, Any], selector: Dict[str, Any]) -> bool:
    labels = pod.get("metadata", {}).get("labels") or {}
    match_labels = selector.get("matchLabels") or {}
    if match_labels and not _pod_matches_labels(pod, match_labels):
        return False

    for expression in selector.get("matchExpressions") or []:
        key = expression.get("key")
        operator = expression.get("operator")
        values = expression.get("values") or []
        value = labels.get(key)
        if operator == "In":
            if value not in values:
                return False
        elif operator == "NotIn":
            if value in values:
                return False
        elif operator == "Exists":
            if key not in labels:
                return False
        elif operator == "DoesNotExist":
            if key in labels:
                return False
        else:
            return False
    return bool(match_labels or selector.get("matchExpressions"))


def _deployment_selector(access, namespace: str, deployment_name: str) -> Dict[str, Any]:
    try:
        dep_output = _run_for_access(
            access,
            ["get", "deployment", deployment_name, "-n", namespace, "-o", "json"],
        )
        deployment = json.loads(dep_output)
        return (deployment.get("spec", {}).get("selector") or {}).copy()
    except K8sCommandError:
        return {}


def _deployment_match_labels(access, namespace: str, deployment_name: str) -> Dict[str, str]:
    return _deployment_selector(access, namespace, deployment_name).get("matchLabels") or {}


def _label_selector_string(selector: Dict[str, Any]) -> Optional[str]:
    match_labels = selector.get("matchLabels") or {}
    if not match_labels:
        return None
    return ",".join(f"{key}={value}" for key, value in match_labels.items())


def _list_pods_for_scope(
    access,
    namespace: str,
    resource_type: str,
    resource_name: Optional[str],
) -> List[Dict[str, Any]]:
    if resource_type == "pod":
        if resource_name:
            try:
                pod_output = _run_for_access(
                    access,
                    ["get", "pod", resource_name, "-n", namespace, "-o", "json"],
                )
                pod = json.loads(pod_output)
                return [pod] if pod.get("metadata", {}).get("name") else []
            except K8sCommandError:
                return []
        try:
            pods_output = _run_for_access(access, ["get", "pods", "-n", namespace, "-o", "json"])
            return json.loads(pods_output).get("items", [])
        except K8sCommandError:
            return []

    if resource_type == "deployment":
        if resource_name:
            selector = _deployment_selector(access, namespace, resource_name)
            if not selector:
                return []
            label_selector = _label_selector_string(selector)
            if label_selector:
                try:
                    pods_output = _run_for_access(
                        access,
                        ["get", "pods", "-n", namespace, "-l", label_selector, "-o", "json"],
                    )
                    pods = json.loads(pods_output).get("items", [])
                    if pods:
                        return pods
                except K8sCommandError:
                    pass
            try:
                pods_output = _run_for_access(access, ["get", "pods", "-n", namespace, "-o", "json"])
                pod_items = json.loads(pods_output).get("items", [])
            except K8sCommandError:
                return []
            return [pod for pod in pod_items if _pod_matches_deployment_selector(pod, selector)]
        try:
            pods_output = _run_for_access(access, ["get", "pods", "-n", namespace, "-o", "json"])
            return json.loads(pods_output).get("items", [])
        except K8sCommandError:
            return []

    return []


def _collect_workload_cpu_memory_observations(
    access,
    namespace: str,
    resource_type: str,
    resource_name: Optional[str],
    metric_keys: List[str],
) -> List[Dict[str, Any]]:
    from ..k8s_metrics import aggregate_pod_usage_percents, fetch_pod_top_metrics, pod_usage_percents

    if resource_type not in {"pod", "deployment"}:
        return []
    if "cpu_usage_percent" not in metric_keys and "memory_usage_percent" not in metric_keys:
        return []

    pods = _list_pods_for_scope(access, namespace, resource_type, resource_name)
    if not pods:
        return []

    top_by_pod = fetch_pod_top_metrics(access)
    observations: List[Dict[str, Any]] = []

    if resource_type == "deployment" and resource_name:
        cpu_pct, mem_pct = aggregate_pod_usage_percents(pods, top_by_pod)
        base = {
            "namespace": namespace,
            "resourceType": "deployment",
            "resourceName": resource_name,
        }
        if "cpu_usage_percent" in metric_keys and cpu_pct is not None:
            observations.append({**base, "metricKey": "cpu_usage_percent", "value": round(float(cpu_pct), 2)})
        if "memory_usage_percent" in metric_keys and mem_pct is not None:
            observations.append({**base, "metricKey": "memory_usage_percent", "value": round(float(mem_pct), 2)})
        return observations

    for pod in pods:
        meta = pod.get("metadata", {})
        pod_name = meta.get("name")
        pod_ns = meta.get("namespace") or namespace
        cpu_pct, mem_pct = pod_usage_percents(pod, top_by_pod.get((pod_ns, pod_name), {}))

        if resource_type == "pod" and resource_name:
            if "cpu_usage_percent" in metric_keys and cpu_pct is not None:
                observations.append({"metricKey": "cpu_usage_percent", "value": round(float(cpu_pct), 2)})
            if "memory_usage_percent" in metric_keys and mem_pct is not None:
                observations.append({"metricKey": "memory_usage_percent", "value": round(float(mem_pct), 2)})
            break

        base = {
            "namespace": pod_ns,
            "resourceType": "pod",
            "resourceName": pod_name,
        }
        if "cpu_usage_percent" in metric_keys and cpu_pct is not None:
            observations.append({**base, "metricKey": "cpu_usage_percent", "value": round(float(cpu_pct), 2)})
        if "memory_usage_percent" in metric_keys and mem_pct is not None:
            observations.append({**base, "metricKey": "memory_usage_percent", "value": round(float(mem_pct), 2)})

    return observations


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

    if "cpu_usage_percent" in metric_keys or "memory_usage_percent" in metric_keys:
        if resource_type in {"pod", "deployment"} and namespace:
            observations.extend(
                _collect_workload_cpu_memory_observations(
                    access,
                    namespace,
                    resource_type,
                    resource_name,
                    metric_keys,
                )
            )
        else:
            cpu_pct, mem_pct = (None, None)
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
    resource_type = target.get("resourceType")
    namespace = target.get("namespace") or "default"
    resource_name = target.get("resourceName")
    observations: List[Dict[str, Any]] = []

    workload_cpu = 85.0
    workload_memory = 72.0
    if resource_type in {"pod", "deployment"}:
        if resource_type == "pod" and resource_name:
            if "cpu_usage_percent" in metric_keys:
                observations.append({"metricKey": "cpu_usage_percent", "value": workload_cpu})
            if "memory_usage_percent" in metric_keys:
                observations.append({"metricKey": "memory_usage_percent", "value": workload_memory})
        elif resource_type == "deployment" and resource_name:
            base = {
                "namespace": namespace,
                "resourceType": "deployment",
                "resourceName": resource_name,
            }
            if "cpu_usage_percent" in metric_keys:
                observations.append({**base, "metricKey": "cpu_usage_percent", "value": workload_cpu})
            if "memory_usage_percent" in metric_keys:
                observations.append({**base, "metricKey": "memory_usage_percent", "value": workload_memory})
        else:
            if "cpu_usage_percent" in metric_keys:
                observations.append(
                    {
                        "metricKey": "cpu_usage_percent",
                        "value": workload_cpu,
                        "namespace": namespace,
                        "resourceType": "pod",
                        "resourceName": "mock-pod-1",
                    }
                )
            if "memory_usage_percent" in metric_keys:
                observations.append(
                    {
                        "metricKey": "memory_usage_percent",
                        "value": workload_memory,
                        "namespace": namespace,
                        "resourceType": "pod",
                        "resourceName": "mock-pod-1",
                    }
                )
    else:
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
        last_details: List[Dict[str, Any]] = []
        for group in grouped.values():
            matched, details = evaluate_conditions(group, conditions, logic)
            last_details = details
            if matched:
                sample = group[0]
                return True, details, sample
        return False, last_details, {}

    matched, details = evaluate_conditions(scalar_obs, conditions, logic)
    return matched, details, {}


def _history_to_alert_dict(row: AlertHistory) -> Dict[str, Any]:
    if getattr(row, "alert_type", "metric") == "log":
        from .log_alert_evaluator import _history_to_log_alert_dict

        return _history_to_log_alert_dict(row)
    return {
        "id": f"history-{row.id}",
        "alertType": "metric",
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


def _summarize_evaluation_details(details: List[Dict[str, Any]]) -> Tuple[Optional[str], Optional[str]]:
    if not details:
        return None, None
    primary = next((item for item in details if item.get("matched")), None) or details[0]
    metric_label = primary.get("metricLabel") or primary.get("metricKey") or ""
    observed = primary.get("observedValue")
    threshold = primary.get("threshold")
    operator = str(primary.get("operator") or "").strip()

    if observed is None:
        measured = None
    elif metric_label:
        measured = f"{metric_label}: {observed}"
    else:
        measured = str(observed)

    threshold_display = None
    if threshold is not None:
        threshold_display = f"{operator} {threshold}".strip() if operator else str(threshold)
    return measured, threshold_display


def _record_policy_evaluation(
    policy: AlertPolicy,
    now: datetime,
    result: str,
    measured_value: Optional[str],
    threshold: Optional[str],
    error_message: Optional[str],
) -> None:
    policy.last_evaluated_at = now
    policy.last_evaluation_result = result
    policy.last_measured_value = measured_value
    policy.last_threshold = threshold
    policy.last_evaluation_error = error_message
    logger.info(
        "Alert policy evaluated: policy_id=%s name=%r cluster=%s result=%s measured_value=%r threshold=%r error=%r",
        policy.id,
        policy.name,
        policy.cluster_id,
        result,
        measured_value,
        threshold,
        error_message,
    )


def _policy_due_for_evaluation(policy: AlertPolicy, now: datetime) -> bool:
    from ..alert_policy_catalog import DEFAULT_EVALUATION_INTERVAL_SECONDS

    interval = int(policy.evaluation_interval_seconds or DEFAULT_EVALUATION_INTERVAL_SECONDS)
    if policy.last_evaluated_at is None:
        return True
    last_evaluated = policy.last_evaluated_at
    if last_evaluated.tzinfo is None:
        last_evaluated = last_evaluated.replace(tzinfo=timezone.utc)
    return (now - last_evaluated).total_seconds() >= interval


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
    policies_evaluated = False
    now = datetime.now(timezone.utc)

    for policy in policies:
        if not _policy_due_for_evaluation(policy, now):
            continue

        alert_type = getattr(policy, "alert_type", "metric") or "metric"
        if alert_type == "log":
            policies_evaluated = True
            eval_error: Optional[str] = None
            measured_value: Optional[str] = None
            result = "not_met"
            try:
                from .log_alert_evaluator import evaluate_log_policy

                new_rows, measured_value, _ = evaluate_log_policy(
                    policy,
                    cluster_id,
                    access,
                    user=user,
                    persist=persist,
                )
                updated_rows.extend(new_rows)
                result = "met" if new_rows else "not_met"
            except Exception as exc:
                eval_error = str(exc)
                result = "error"
                measured_value = None
                logger.exception(
                    "Log alert policy evaluation failed: policy_id=%s name=%r cluster=%s",
                    policy.id,
                    policy.name,
                    cluster_id,
                )
            if eval_error:
                _record_policy_evaluation(policy, now, "error", None, None, eval_error)
            else:
                _record_policy_evaluation(policy, now, result, measured_value, None, None)
            continue

        conditions = policy.conditions or []
        if not conditions:
            continue

        policies_evaluated = True
        metric_keys = [c.get("metricKey") for c in conditions if c.get("metricKey") in METRIC_BY_KEY]
        logic = policy.condition_logic or "any"

        any_matched = False
        snapshot_details: List[Dict[str, Any]] = []
        eval_error: Optional[str] = None

        try:
            for target in _scope_targets(policy.scope or {}):
                if access:
                    observations = _collect_real_observations(access, cluster_id, target, metric_keys)
                else:
                    observations = _mock_observations(cluster_id, target, metric_keys)

                matched, details, sample = _pick_evaluation_observations(observations, conditions, logic)
                if details and not snapshot_details:
                    snapshot_details = details
                if matched:
                    any_matched = True
                    snapshot_details = details

                key = _alert_key(policy.id, cluster_id, target)
                row = AlertHistory.query.filter_by(alert_key=key).first()

                namespace = sample.get("namespace") or target.get("namespace")
                if target.get("resourceType") == "deployment" and target.get("resourceName"):
                    resource_type = "deployment"
                    resource_name = target.get("resourceName")
                else:
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

                            dispatch_policy_alert_notifications(_history_to_alert_dict(row))
                    else:
                        row.status = "active"
                        row.resolved_at = None
                        row.title = title
                        row.description = description
                        row.triggered_conditions = details
                        row.metric_snapshot = {"observations": observations}
                        row.severity = policy.severity
                        row.fired_at = row.fired_at or now
                        if persist:
                            from ..alert_notifier import dispatch_policy_alert_notifications

                            dispatch_policy_alert_notifications(_history_to_alert_dict(row))
                    updated_rows.append(row)
                elif row and row.status == "active":
                    row.status = "resolved"
                    row.resolved_at = now
                    updated_rows.append(row)
        except Exception as exc:
            eval_error = str(exc)
            logger.exception(
                "Alert policy evaluation failed: policy_id=%s name=%r cluster=%s",
                policy.id,
                policy.name,
                cluster_id,
            )

        if eval_error:
            _record_policy_evaluation(policy, now, "error", None, None, eval_error)
        else:
            measured_value, threshold = _summarize_evaluation_details(snapshot_details)
            result = "met" if any_matched else "not_met"
            _record_policy_evaluation(policy, now, result, measured_value, threshold, None)

    if persist and (updated_rows or policies_evaluated):
        db.session.commit()
    return [r for r in updated_rows if r.status == "active"]


def evaluate_all_enabled_policies(*, persist: bool = True) -> None:
    """Evaluate enabled policies for every cluster that has at least one policy."""
    cluster_ids = {
        row[0]
        for row in db.session.query(AlertPolicy.cluster_id)
        .filter_by(enabled=True)
        .distinct()
        .all()
        if row[0]
    }
    for cluster_id in sorted(cluster_ids):
        evaluate_policies_for_cluster(cluster_id, user=None, persist=persist)


def list_active_policy_alerts(
    cluster_id: Optional[str] = None,
    user: Optional[User] = None,
    *,
    evaluate: bool = True,
) -> List[Dict[str, Any]]:
    if cluster_id and evaluate:
        evaluate_policies_for_cluster(cluster_id, user=user, persist=True)

    query = AlertHistory.query.filter_by(status="active")
    if cluster_id:
        query = query.filter_by(cluster_id=cluster_id)

    from .alert_policy_service import policy_show_on_dashboard

    items: List[Dict[str, Any]] = []
    for row in query.order_by(AlertHistory.fired_at.desc()).all():
        if row.policy_id:
            policy = AlertPolicy.query.get(row.policy_id)
            if policy and not policy_show_on_dashboard(policy):
                continue
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
