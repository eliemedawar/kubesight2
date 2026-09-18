"""What the platform is doing right now, and what it did: logs, alerts, audit.

The tools an agent reaches for when something is wrong and nobody knows why yet.
Two of them deserve a note.

**Logs are the expensive one.** A pod's output is unbounded and most of it is
not the answer, so this module never returns a whole log. It tails, it accepts a
time window, and it accepts a ``contains`` filter that greps before trimming —
which is the difference between four hundred lines of healthy startup and the
six lines with the stack trace in them. An agent that pulls a full tail and then
searches it in its own head has spent its context to do what a grep argument
does for nothing.

**Alerts come from two places and are one list.** KubeSight derives some from the
cluster itself and evaluates the rest from alert policies, and the UI merges
them. This does too, through the same merge, because an agent given only half
would answer "there are no alerts" while the other half was firing.

Audit is read-only here and always will be. It is the record of what everybody
— including this agent — did, and a record something under audit can edit is not
a record.
"""

from __future__ import annotations

from typing import Any, Dict, List

from ..protocol import ToolError
from .common import pick, require_namespace, resolve_cluster, take, unwrap2, visible_clusters
from .registry import MAX_LOG_LINES, MAX_ROWS, _limit, tool as _register


def tool(name, **kwargs):
    kwargs.setdefault("domain", "observability")
    return _register(name, **kwargs)


def _user():
    from ...auth_utils import get_current_user

    try:
        return get_current_user()
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Logs
# ---------------------------------------------------------------------------

@tool(
    "kubesight_pods_for_logs",
    permission="logs:view",
    description=(
        "The pods in a namespace whose logs this token may read, with their "
        "containers. Call this before kubesight_pod_logs when you do not already "
        "have an exact pod name — pod names carry a generated suffix and "
        "guessing one costs a failed call."
    ),
    schema={
        "type": "object",
        "properties": {
            "cluster": {"type": "string"},
            "namespace": {"type": "string"},
        },
        "required": ["cluster", "namespace"],
    },
)
def _pods_for_logs(arguments: Dict[str, Any]) -> Dict[str, Any]:
    from ...services.logs_service import list_pods_for_logs

    user = _user()
    cluster_id = resolve_cluster(user, arguments.get("cluster"))
    namespace = require_namespace(user, cluster_id, arguments.get("namespace"))
    payload = unwrap2(list_pods_for_logs(cluster_id, namespace), what="pods") or {}
    return {
        "clusterId": cluster_id,
        "namespace": namespace,
        "count": payload.get("count", 0),
        "pods": [pick(pod, ("name", "status", "containers", "restarts")) for pod in payload.get("items") or []],
    }


# The window options the log service accepts. Anything else is refused by
# ``parse_log_time_filters``, so they are named in the schema rather than left
# for an agent to discover by being told no.
_SINCE_CHOICES = [900, 3600, 21600, 86400]


@tool(
    "kubesight_pod_logs",
    permission="logs:view",
    description=(
        "A pod container's log, tailed. Use 'contains' to keep only matching "
        "lines — it filters before the tail, so a narrow filter over a long "
        "window finds the error a plain tail would have scrolled past. "
        "'previous' reads the log of the container's last crashed instance, "
        "which is where a CrashLoopBackOff explains itself."
    ),
    schema={
        "type": "object",
        "properties": {
            "cluster": {"type": "string"},
            "namespace": {"type": "string"},
            "pod": {"type": "string"},
            "container": {"type": "string", "description": "Required for a multi-container pod."},
            "tail": {"type": "integer", "minimum": 1, "maximum": MAX_LOG_LINES},
            "sinceSeconds": {
                "type": "integer",
                "enum": _SINCE_CHOICES,
                "description": "900 (15m), 3600 (1h), 21600 (6h) or 86400 (24h).",
            },
            "contains": {
                "type": "string",
                "description": "Keep only lines containing this, case-insensitive.",
            },
            "previous": {
                "type": "boolean",
                "description": "Read the previous, crashed instance of the container.",
            },
        },
        "required": ["cluster", "namespace", "pod"],
    },
)
def _pod_logs(arguments: Dict[str, Any]) -> Dict[str, Any]:
    from ...log_time_filters import parse_log_time_filters
    from ...services.logs_service import fetch_pod_logs

    user = _user()
    cluster_id = resolve_cluster(user, arguments.get("cluster"))
    namespace = require_namespace(user, cluster_id, arguments.get("namespace"))
    pod = str(arguments.get("pod") or "").strip()
    if not pod:
        raise ToolError("Name the pod — kubesight_pods_for_logs lists them.")
    container = str(arguments.get("container") or "").strip() or None

    since = arguments.get("sinceSeconds")
    time_filters, error = parse_log_time_filters(str(since) if since else "", "", "")
    if error:
        raise ToolError(error)

    tail = _limit({"limit": arguments.get("tail", 200)}, 200)
    tail = min(tail, MAX_LOG_LINES)
    params = {
        "live": False,
        "previous": bool(arguments.get("previous")),
        "timestamps": True,
        "time_filters": time_filters,
        "incremental": False,
        # Over-fetch when filtering: the lines that match may be anywhere in the
        # window, and tailing first would throw away the ones being looked for.
        "tail_lines": MAX_LOG_LINES * 4 if arguments.get("contains") else tail,
    }
    payload = unwrap2(
        fetch_pod_logs(
            cluster_id=cluster_id,
            namespace=namespace,
            pod_name=pod,
            container_name=container,
            params=params,
        ),
        what=pod,
    ) or {}

    lines = [
        line.get("content") if isinstance(line, dict) else str(line)
        for line in payload.get("lines") or payload.get("items") or []
    ]
    needle = str(arguments.get("contains") or "").strip().lower()
    matched = None
    if needle:
        filtered = [line for line in lines if needle in str(line).lower()]
        matched = len(filtered)
        lines = filtered
    return {
        "clusterId": cluster_id,
        "namespace": namespace,
        "pod": pod,
        "container": container or payload.get("container"),
        "previous": bool(arguments.get("previous")),
        "matchedLines": matched,
        "truncated": len(lines) > tail,
        "lines": lines[-tail:],
    }


# ---------------------------------------------------------------------------
# Alerts
# ---------------------------------------------------------------------------

_ALERT_FIELDS = (
    "id", "severity", "title", "message", "clusterId", "namespace", "resource",
    "source", "status", "firstSeen", "lastSeen", "count",
)


@tool(
    "kubesight_alerts_list",
    permission="alerts:view",
    description=(
        "Firing alerts, from the cluster and from KubeSight's alert policies, "
        "merged. Without a cluster it covers every cluster this token can see. "
        "Filter by severity to cut straight to what matters."
    ),
    schema={
        "type": "object",
        "properties": {
            "cluster": {"type": "string", "description": "Omit for every visible cluster."},
            "severity": {"type": "string", "enum": ["critical", "warning", "info"]},
            "limit": {"type": "integer", "minimum": 1, "maximum": MAX_ROWS},
        },
    },
)
def _alerts_list(arguments: Dict[str, Any]) -> Dict[str, Any]:
    from ...access_engine import filter_alerts_for_user
    from ...k8s_provider import should_use_real_k8s
    from ...routes.alerts import _filter_mock_alerts, _merge_policy_alerts

    user = _user()
    cluster_id = resolve_cluster(user, arguments.get("cluster")) if arguments.get("cluster") else None

    cluster_ids = [cluster_id] if cluster_id else [
        str(item.get("id")) for item in visible_clusters(user)
    ]
    items: List[Dict[str, Any]] = []
    for cid in cluster_ids:
        if should_use_real_k8s(cid):
            try:
                from ...k8s_provider import list_alerts_from_k8s

                items.extend(list_alerts_from_k8s(cluster_id=cid).get("items") or [])
            except Exception:
                # One unreachable cluster must not blank the other clusters'
                # alerts. Said in metadata rather than swallowed entirely.
                items.append(
                    {
                        "id": f"kubesight-unreachable-{cid}",
                        "severity": "warning",
                        "title": "Alerts unavailable",
                        "message": f"KubeSight could not read alerts from '{cid}'.",
                        "clusterId": cid,
                        "source": "kubesight",
                    }
                )
        else:
            items.extend(_filter_mock_alerts(cid))

    items = _merge_policy_alerts(items, user, cluster_id)
    if user:
        items = filter_alerts_for_user(user, items)
    severity = str(arguments.get("severity") or "").strip().lower()
    if severity:
        items = [item for item in items if str(item.get("severity", "")).lower() == severity]

    total = len(items)
    rows = [pick(item, _ALERT_FIELDS) for item in take(items, _limit(arguments, MAX_ROWS))]
    return {
        "clusterId": cluster_id,
        "clustersScanned": len(cluster_ids),
        "totalMatching": total,
        "count": len(rows),
        "alerts": rows,
    }


_POLICY_FIELDS = (
    "id", "name", "alertType", "severity", "enabled", "clusterId", "namespace",
    "threshold", "forDuration", "lastEvaluatedAt", "lastFiredAt", "state",
)


@tool(
    "kubesight_alert_policies_list",
    permission="alerts:view",
    description=(
        "The alert rules KubeSight evaluates itself: what each watches, its "
        "threshold, whether it is enabled and whether it is currently firing. "
        "An alert nobody expected usually has its rule here."
    ),
    schema={
        "type": "object",
        "properties": {
            "cluster": {"type": "string"},
            "limit": {"type": "integer", "minimum": 1, "maximum": MAX_ROWS},
        },
    },
)
def _alert_policies_list(arguments: Dict[str, Any]) -> Dict[str, Any]:
    from ...services.alert_policy_service import list_policies

    user = _user()
    cluster_id = resolve_cluster(user, arguments.get("cluster")) if arguments.get("cluster") else None
    rows = list_policies(user, cluster_id) or []
    total = len(rows)
    rows = [pick(row, _POLICY_FIELDS) for row in take(rows, _limit(arguments, MAX_ROWS))]
    return {"totalMatching": total, "count": len(rows), "policies": rows}


@tool(
    "kubesight_alert_policy_set_enabled",
    permission="alerts:manage",
    description=(
        "Turn one alert policy on or off. Silencing a rule silences it for "
        "everybody, indefinitely, and nothing turns it back on by itself — say "
        "what you disabled and why, so somebody can undo it."
    ),
    write=True,
    schema={
        "type": "object",
        "properties": {
            "policyId": {"type": "integer"},
            "enabled": {"type": "boolean"},
        },
        "required": ["policyId", "enabled"],
    },
)
def _alert_policy_set_enabled(arguments: Dict[str, Any], *, user=None) -> Dict[str, Any]:
    from ...services.alert_policy_service import set_policy_enabled
    from .common import unwrap

    policy_id = arguments.get("policyId")
    enabled = arguments.get("enabled")
    if policy_id is None or enabled is None:
        raise ToolError("Both 'policyId' and 'enabled' are required.")
    data = unwrap(set_policy_enabled(user, int(policy_id), bool(enabled)), what="policy") or {}
    state = "enabled" if enabled else "disabled"
    return {"changed": f"{state} alert policy {policy_id}", **data}


# ---------------------------------------------------------------------------
# What happened, and who did it
# ---------------------------------------------------------------------------

_AUDIT_FIELDS = ("id", "action", "actorUsername", "targetType", "targetId", "details", "createdAt")


@tool(
    "kubesight_audit_logs",
    permission="audit:view",
    description=(
        "KubeSight's audit trail, newest first: who did what, to which object, "
        "when. Filter by action or by username. This is how to answer 'who "
        "changed this' — including when the answer is an agent, because every "
        "MCP tool call is recorded here too."
    ),
    schema={
        "type": "object",
        "properties": {
            "action": {"type": "string", "description": "Exact action name, e.g. deployment_applied."},
            "actor": {"type": "string", "description": "Username."},
            "limit": {"type": "integer", "minimum": 1, "maximum": MAX_ROWS},
        },
    },
)
def _audit_logs(arguments: Dict[str, Any]) -> Dict[str, Any]:
    from ...services.audit_service import list_audit_logs

    payload = list_audit_logs(
        limit=_limit(arguments, 50),
        action=str(arguments.get("action") or "").strip() or None,
        actor_username=str(arguments.get("actor") or "").strip() or None,
    )
    rows = [pick(row, _AUDIT_FIELDS) for row in payload.get("items") or []]
    return {"count": len(rows), "entries": rows}


@tool(
    "kubesight_dashboard_summary",
    permission="overview:view",
    description=(
        "The whole platform in one call, for one cluster: health, node and pod "
        "counts, namespace health, firing alerts and recent activity. The widest "
        "possible orientation before narrowing anywhere."
    ),
    schema={
        "type": "object",
        "properties": {"cluster": {"type": "string"}},
        "required": ["cluster"],
    },
)
def _dashboard_summary(arguments: Dict[str, Any]) -> Dict[str, Any]:
    from ...services.dashboard_service import get_dashboard_summary
    from .common import unwrap

    user = _user()
    cluster_id = resolve_cluster(user, arguments.get("cluster"))
    return unwrap(get_dashboard_summary(cluster_id, user), what=cluster_id) or {}
