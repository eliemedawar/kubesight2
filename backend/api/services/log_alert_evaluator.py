"""Evaluate log-based alert policies against pod logs."""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

from ..access_engine import can_view_alert, is_admin
from ..alert_policy_catalog import normalize_log_config
from ..db import db
from ..k8s_logs import find_log_matches, scan_pod_logs_for_matches
from ..models import AlertHistory, AlertPolicy, LogAlertSeen, User

logger = logging.getLogger(__name__)


def _iso_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _log_alert_key(
    policy_id: int,
    cluster_id: str,
    namespace: str,
    pod_name: str,
    container_name: str,
    log_hash: str,
) -> str:
    return ":".join(
        [
            f"policy-{policy_id}",
            cluster_id,
            namespace or "*",
            pod_name,
            container_name,
            log_hash,
        ]
    )


def _is_log_seen(policy_id: int, pod_name: str, container_name: str, log_hash: str) -> bool:
    return (
        LogAlertSeen.query.filter_by(
            policy_id=policy_id,
            pod_name=pod_name,
            container_name=container_name,
            log_hash=log_hash,
        ).first()
        is not None
    )


def _mark_log_seen(
    policy_id: int,
    pod_name: str,
    container_name: str,
    log_timestamp: str,
    log_hash: str,
) -> None:
    if _is_log_seen(policy_id, pod_name, container_name, log_hash):
        return
    db.session.add(
        LogAlertSeen(
            policy_id=policy_id,
            pod_name=pod_name,
            container_name=container_name,
            log_timestamp=log_timestamp,
            log_hash=log_hash,
        )
    )


def _mock_log_matches(
    *,
    pattern: str,
    match_type: str,
    case_sensitive: bool,
    namespace: str,
    pod_name: str,
    container_name: str,
    deployment_name: Optional[str],
    context_before: int,
    context_after: int,
    max_lines: int,
) -> List[Dict[str, Any]]:
    """Return synthetic log matches for mock cluster evaluation."""
    sample_lines = [
        f"{_iso_now()} Connecting to database...",
        f"{_iso_now()} ERROR Database connection failed",
        "psycopg2.OperationalError: could not connect to server",
        "Connection refused",
        f"{_iso_now()} Retrying connection...",
    ]
    log_text = "\n".join(sample_lines)
    matches = find_log_matches(
        log_text,
        match_type=match_type,
        pattern=pattern,
        case_sensitive=case_sensitive,
        context_before=context_before,
        context_after=context_after,
        max_lines=max_lines,
    )
    results: List[Dict[str, Any]] = []
    for match in matches:
        log_timestamp = match["logTimestamp"]
        matching_line = match["matchingLine"]
        from ..k8s_logs import _log_entry_hash

        log_hash = _log_entry_hash(
            pod_name=pod_name,
            container_name=container_name,
            log_timestamp=log_timestamp,
            matching_line=matching_line,
        )
        results.append(
            {
                "podName": pod_name,
                "containerName": container_name,
                "namespace": namespace,
                "deploymentName": deployment_name,
                "logTimestamp": log_timestamp,
                "matchedPattern": pattern,
                "matchingLine": matching_line,
                "logLines": match["logLines"],
                "logSnippet": match["logSnippet"],
                "logHash": log_hash,
            }
        )
    return results


def _history_to_log_alert_dict(row: AlertHistory) -> Dict[str, Any]:
    snapshot = row.log_snapshot if isinstance(row.log_snapshot, dict) else {}
    deployment = snapshot.get("deploymentName")
    return {
        "id": f"history-{row.id}",
        "alertType": "log",
        "severity": row.severity,
        "clusterId": row.cluster_id,
        "namespace": row.namespace,
        "pod": snapshot.get("podName") or row.resource_name,
        "container": snapshot.get("containerName"),
        "deployment": deployment,
        "resourceType": row.resource_type,
        "resourceName": row.resource_name,
        "title": row.title,
        "description": row.description,
        "policyId": row.policy_id,
        "policyName": row.policy_name,
        "matchedPattern": snapshot.get("matchedPattern"),
        "detectedAt": snapshot.get("detectedAt") or snapshot.get("logTimestamp"),
        "logLines": snapshot.get("logLines") or [],
        "logSnippet": snapshot.get("logSnippet") or "",
        "firedAt": row.fired_at.isoformat() if row.fired_at else _iso_now(),
        "status": "firing" if row.status == "active" else "resolved",
        "source": "alert_policy",
    }


def evaluate_log_policy(
    policy: AlertPolicy,
    cluster_id: str,
    access,
    *,
    user: Optional[User] = None,
    persist: bool = True,
) -> Tuple[List[AlertHistory], Optional[str], Optional[str]]:
    """Scan pod logs for a log alert policy. Returns (new_alerts, measured_value, error)."""
    from .alert_policy_evaluator import _list_pods_for_scope, _scope_targets

    log_config = normalize_log_config(policy.log_config)
    pattern = log_config["pattern"]
    scope = policy.scope or {}
    targets = _scope_targets(scope)
    new_rows: List[AlertHistory] = []
    match_count = 0
    now = datetime.now(timezone.utc)

    for target in targets:
        namespace = target.get("namespace") or ""
        resource_type = target.get("resourceType") or "deployment"
        resource_name = target.get("resourceName")

        if access:
            pods = _list_pods_for_scope(access, namespace, resource_type, resource_name)
        else:
            pod_name = resource_name or "mock-pod-1"
            if resource_type == "deployment" and resource_name:
                pod_name = f"{resource_name}-abc123"
            pods = [{"metadata": {"name": pod_name, "namespace": namespace}, "spec": {"containers": [{"name": "app"}]}}]

        deployment_name = resource_name if resource_type == "deployment" else None

        for pod in pods:
            meta = pod.get("metadata", {}) or {}
            pod_name = meta.get("name") or "unknown"
            pod_ns = meta.get("namespace") or namespace

            if access:
                matches = scan_pod_logs_for_matches(
                    access,
                    namespace=pod_ns,
                    pod=pod,
                    since_seconds=log_config["logWindowSeconds"],
                    match_type=log_config["matchType"],
                    pattern=pattern,
                    case_sensitive=log_config["caseSensitive"],
                    context_before=log_config["contextLinesBefore"],
                    context_after=log_config["contextLinesAfter"],
                    max_lines=log_config["maxLines"],
                )
                for match in matches:
                    if deployment_name:
                        match["deploymentName"] = deployment_name
            else:
                matches = _mock_log_matches(
                    pattern=pattern,
                    match_type=log_config["matchType"],
                    case_sensitive=log_config["caseSensitive"],
                    namespace=pod_ns,
                    pod_name=pod_name,
                    container_name="app",
                    deployment_name=deployment_name,
                    context_before=log_config["contextLinesBefore"],
                    context_after=log_config["contextLinesAfter"],
                    max_lines=log_config["maxLines"],
                )

            for match in matches:
                log_hash = match["logHash"]
                container_name = match.get("containerName") or ""
                if _is_log_seen(policy.id, pod_name, container_name, log_hash):
                    continue

                if user and not is_admin(user):
                    view_resource = deployment_name or pod_name
                    if not can_view_alert(user, cluster_id, pod_ns, view_resource):
                        continue

                alert_key = _log_alert_key(policy.id, cluster_id, pod_ns, pod_name, container_name, log_hash)
                if AlertHistory.query.filter_by(alert_key=alert_key).first():
                    _mark_log_seen(policy.id, pod_name, container_name, match["logTimestamp"], log_hash)
                    continue

                resource_type_for_alert = resource_type
                resource_name_for_alert = resource_name or pod_name
                if resource_type == "deployment" and resource_name:
                    resource_type_for_alert = "deployment"
                    resource_name_for_alert = resource_name
                elif resource_type == "pod":
                    resource_type_for_alert = "pod"
                    resource_name_for_alert = pod_name

                log_snapshot = {
                    "podName": pod_name,
                    "containerName": container_name,
                    "deploymentName": deployment_name,
                    "matchedPattern": pattern,
                    "logTimestamp": match["logTimestamp"],
                    "detectedAt": match["logTimestamp"],
                    "matchingLine": match.get("matchingLine"),
                    "logLines": match.get("logLines") or [],
                    "logSnippet": match.get("logSnippet") or "",
                }

                title = "Error detected in logs"
                description = f"Pattern '{pattern}' matched in {pod_name}/{container_name}"

                row = AlertHistory(
                    alert_key=alert_key,
                    policy_id=policy.id,
                    policy_name=policy.name,
                    cluster_id=cluster_id,
                    namespace=pod_ns,
                    resource_type=resource_type_for_alert,
                    resource_name=resource_name_for_alert,
                    alert_type="log",
                    severity=policy.severity,
                    status="active",
                    title=title,
                    description=description,
                    triggered_conditions=[],
                    log_snapshot=log_snapshot,
                    fired_at=now,
                )
                db.session.add(row)
                if persist:
                    db.session.flush()
                    _mark_log_seen(policy.id, pod_name, container_name, match["logTimestamp"], log_hash)
                    from ..alert_notifier import dispatch_policy_alert_notifications

                    dispatch_policy_alert_notifications(_history_to_log_alert_dict(row))
                new_rows.append(row)
                match_count += 1

    measured = f"{match_count} new log match(es)" if match_count else "No new log matches"
    return new_rows, measured, None
