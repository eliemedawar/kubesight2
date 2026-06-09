"""Extensible alert policy metric and operator catalog."""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

METRIC_TYPES = {
    "percent": "Percentage",
    "count": "Count",
    "boolean": "Boolean",
}

OPERATORS = {
    "percent": [">", ">=", "<", "<=", "="],
    "count": [">", ">=", "<", "<=", "="],
    "boolean": ["="],
}

# Metric keys are stable API identifiers; new metrics can be added here without DB migrations.
METRIC_DEFINITIONS: List[Dict[str, Any]] = [
    {
        "key": "cpu_usage_percent",
        "label": "CPU Usage (%)",
        "type": "percent",
        "unit": "%",
        "description": "CPU usage as a percentage of configured limits or allocatable capacity.",
    },
    {
        "key": "memory_usage_percent",
        "label": "Memory Usage (%)",
        "type": "percent",
        "unit": "%",
        "description": "Memory usage as a percentage of configured limits or allocatable capacity.",
    },
    {
        "key": "pod_restart_count",
        "label": "Pod Restart Count",
        "type": "count",
        "unit": "restarts",
        "description": "Total container restart count for a pod.",
    },
    {
        "key": "node_not_ready",
        "label": "Node Not Ready",
        "type": "boolean",
        "unit": None,
        "description": "True when a node is not in Ready state.",
    },
    {
        "key": "pod_crashloop_backoff",
        "label": "Pod CrashLoopBackOff",
        "type": "boolean",
        "unit": None,
        "description": "True when a pod container is in CrashLoopBackOff.",
    },
    {
        "key": "pod_pending",
        "label": "Pod Pending",
        "type": "boolean",
        "unit": None,
        "description": "True when a pod is stuck in Pending phase.",
    },
    {
        "key": "deployment_unavailable_replicas",
        "label": "Deployment Unavailable Replicas",
        "type": "count",
        "unit": "replicas",
        "description": "Number of unavailable replicas on a deployment.",
    },
    {
        "key": "pvc_usage_percent",
        "label": "PVC Usage (%)",
        "type": "percent",
        "unit": "%",
        "description": "Persistent volume claim usage percentage when capacity data is available.",
    },
    {
        "key": "disk_usage_percent",
        "label": "Disk Usage (%)",
        "type": "percent",
        "unit": "%",
        "description": "Node or volume disk usage percentage when metrics are available.",
    },
]

METRIC_BY_KEY = {item["key"]: item for item in METRIC_DEFINITIONS}

SCOPE_TYPES = ("deployment", "pod")
ALL_RESOURCES_SCOPE_NAME = "*"

SEVERITY_LEVELS = ("info", "warning", "critical")

CONDITION_LOGIC = ("any", "all")

DEFAULT_EVALUATION_INTERVAL_SECONDS = 300

EVALUATION_INTERVALS: List[Dict[str, Any]] = [
    {"seconds": 60, "label": "1 minute"},
    {"seconds": 300, "label": "5 minutes"},
    {"seconds": 600, "label": "10 minutes"},
    {"seconds": 900, "label": "15 minutes"},
    {"seconds": 1800, "label": "30 minutes"},
    {"seconds": 3600, "label": "1 hour"},
]

ALLOWED_EVALUATION_INTERVAL_SECONDS = {item["seconds"] for item in EVALUATION_INTERVALS}


def normalize_evaluation_interval_seconds(raw: Any) -> int:
    try:
        seconds = int(raw)
    except (TypeError, ValueError):
        return DEFAULT_EVALUATION_INTERVAL_SECONDS
    if seconds in ALLOWED_EVALUATION_INTERVAL_SECONDS:
        return seconds
    return DEFAULT_EVALUATION_INTERVAL_SECONDS


def validate_evaluation_interval(seconds: int) -> Optional[str]:
    if seconds not in ALLOWED_EVALUATION_INTERVAL_SECONDS:
        return "Invalid evaluation interval"
    return None


def evaluation_interval_display(seconds: int) -> str:
    mapping = {
        60: "Every 1 min",
        300: "Every 5 min",
        600: "Every 10 min",
        900: "Every 15 min",
        1800: "Every 30 min",
        3600: "Every 1 hour",
    }
    return mapping.get(seconds, f"Every {seconds}s")


def catalog_payload(
    *,
    receivers: Optional[List[Dict[str, Any]]] = None,
    receiver_groups: Optional[List[Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    return {
        "metrics": METRIC_DEFINITIONS,
        "operators": OPERATORS,
        "scopeTypes": list(SCOPE_TYPES),
        "severityLevels": list(SEVERITY_LEVELS),
        "conditionLogic": list(CONDITION_LOGIC),
        "evaluationIntervals": EVALUATION_INTERVALS,
        "defaultEvaluationIntervalSeconds": DEFAULT_EVALUATION_INTERVAL_SECONDS,
        "receivers": receivers or [],
        "receiverGroups": receiver_groups or [],
    }


def validate_condition(condition: Dict[str, Any]) -> Optional[str]:
    metric_key = str(condition.get("metricKey") or "").strip()
    metric = METRIC_BY_KEY.get(metric_key)
    if not metric:
        return f"Unknown metric: {metric_key}"

    operator = str(condition.get("operator") or "").strip()
    allowed_ops = OPERATORS.get(metric["type"], [])
    if operator not in allowed_ops:
        return f"Invalid operator '{operator}' for metric {metric_key}"

    if metric["type"] == "boolean":
        threshold = condition.get("threshold")
        if threshold not in (True, False, 0, 1, "0", "1", "true", "false"):
            return f"Boolean metric {metric_key} requires threshold true or false"
        return None

    try:
        float(condition.get("threshold"))
    except (TypeError, ValueError):
        return f"Metric {metric_key} requires a numeric threshold"
    return None


def normalize_scope(scope: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    raw = scope if isinstance(scope, dict) else {}
    legacy_type = str(raw.get("type") or "deployment").strip().lower()
    scope_type = legacy_type if legacy_type in SCOPE_TYPES else "deployment"
    namespace = str(raw.get("namespace") or "").strip()
    resource_name = str(raw.get("resourceName") or ALL_RESOURCES_SCOPE_NAME).strip() or ALL_RESOURCES_SCOPE_NAME

    if legacy_type == "cluster":
        scope_type = "deployment"
        resource_name = ALL_RESOURCES_SCOPE_NAME
        if not namespace:
            namespace = "default"
    elif legacy_type == "namespace":
        scope_type = "deployment"
        resource_name = ALL_RESOURCES_SCOPE_NAME

    if resource_name in {"", ALL_RESOURCES_SCOPE_NAME}:
        resource_name = ALL_RESOURCES_SCOPE_NAME

    return {
        "type": scope_type,
        "namespace": namespace,
        "resourceName": resource_name,
    }


def validate_scope(scope: Dict[str, Any]) -> Optional[str]:
    normalized = normalize_scope(scope)
    if normalized["type"] not in SCOPE_TYPES:
        return "Scope target must be deployment or pod"
    if not normalized["namespace"]:
        return "Namespace is required"
    if not normalized["resourceName"]:
        return "Resource selection is required"
    return None


def compare_value(observed: Any, operator: str, threshold: Any, metric_type: str) -> bool:
    if metric_type == "boolean":
        obs_bool = bool(observed)
        if isinstance(threshold, str):
            thr_bool = threshold.strip().lower() in {"1", "true", "yes", "on"}
        else:
            thr_bool = bool(threshold)
        return obs_bool == thr_bool

    try:
        left = float(observed)
        right = float(threshold)
    except (TypeError, ValueError):
        return False

    if operator == ">":
        return left > right
    if operator == ">=":
        return left >= right
    if operator == "<":
        return left < right
    if operator == "<=":
        return left <= right
    if operator == "=":
        return left == right
    return False


def evaluate_conditions(
    observations: List[Dict[str, Any]],
    conditions: List[Dict[str, Any]],
    logic: str,
) -> Tuple[bool, List[Dict[str, Any]]]:
    """Return whether policy matches and per-condition evaluation details."""
    triggered: List[Dict[str, Any]] = []
    results: List[bool] = []

    for condition in conditions:
        metric_key = condition.get("metricKey")
        metric = METRIC_BY_KEY.get(metric_key or "")
        if not metric:
            continue
        observation = next((o for o in observations if o.get("metricKey") == metric_key), None)
        observed_value = observation.get("value") if observation else None
        matched = observation is not None and compare_value(
            observed_value,
            str(condition.get("operator") or ">"),
            condition.get("threshold"),
            metric["type"],
        )
        results.append(matched)
        triggered.append(
            {
                "metricKey": metric_key,
                "metricLabel": metric.get("label"),
                "operator": condition.get("operator"),
                "threshold": condition.get("threshold"),
                "observedValue": observed_value,
                "matched": matched,
            }
        )

    if not results:
        return False, triggered

    if logic == "all":
        return all(results), triggered
    return any(results), triggered
