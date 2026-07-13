"""Event-driven "automation" alerts over ticket-driven deploy automation runs.

Unlike metric/log/service policies these are never evaluated on a schedule —
deploy_automation_service calls ``fire_run_failure_alerts`` when a run fails
and ``resolve_run_success_alerts`` when a later run deploys the same target
successfully. An automation policy carries no conditions: severity plus the
per-policy receivers/receiver groups (who gets notified) are the whole config,
and every enabled one fires for every failed run.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Dict

from ..alert_policy_catalog import AUTOMATION_ALERT_CLUSTER_ID
from ..db import db
from ..models import AlertHistory, AlertPolicy

logger = logging.getLogger(__name__)


def _iso_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _history_to_automation_alert_dict(row: AlertHistory) -> Dict[str, Any]:
    snapshot = row.metric_snapshot if isinstance(row.metric_snapshot, dict) else {}
    return {
        "id": f"history-{row.id}",
        "alertType": "automation",
        "severity": row.severity,
        "clusterId": row.cluster_id,
        "namespace": row.namespace,
        "pod": None,
        "resourceType": row.resource_type,
        "resourceName": row.resource_name,
        "ticketNumber": snapshot.get("ticketNumber"),
        "runId": snapshot.get("runId"),
        "changeType": snapshot.get("changeType"),
        "changeSummary": snapshot.get("changeSummary"),
        "failedStep": snapshot.get("failedStep"),
        "jenkinsBuildUrl": snapshot.get("jenkinsBuildUrl"),
        "error": snapshot.get("error"),
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


def fire_run_failure_alerts(run) -> None:
    """Fire one alert per enabled automation policy for a failed run.

    Runs inside the run's transaction: rows are flushed (not committed) so
    notification emails can reference their ids; the caller's commit persists
    them alongside the failed run itself.
    """
    policies = (
        AlertPolicy.query.filter_by(
            cluster_id=AUTOMATION_ALERT_CLUSTER_ID, alert_type="automation", enabled=True
        )
        .order_by(AlertPolicy.id.asc())
        .all()
    )
    if not policies:
        return

    from .deploy_automation_service import _change_summary

    now = datetime.now(timezone.utc)
    ticket = run.ticket_number or f"run #{run.id}"
    error = run.error or "unknown error"
    change = _change_summary(run)
    failed_step = next(
        (s.get("key") for s in reversed(run.steps or []) if s.get("status") == "fail"), None
    )
    title = f"Deploy automation failed — {ticket}"
    description = (
        f"Ticket {ticket}: automation for {run.deployment_name} -> {change} "
        f"in {run.namespace} ({run.cluster_id}) failed: {error}"
    )
    triggered = [
        {
            "metricKey": "automation_run_failed",
            "metricLabel": "Deploy automation run",
            "operator": "=",
            "threshold": "failed",
            "observedValue": failed_step or "failed",
            "matched": True,
        }
    ]
    snapshot = {
        "ticketNumber": run.ticket_number,
        "runId": run.id,
        "changeType": run.change_type or "image",
        "changeSummary": change,
        "failedStep": failed_step,
        "jenkinsBuildUrl": run.jenkins_build_url,
        "error": error,
    }

    from ..alert_notifier import dispatch_policy_alert_notifications

    for policy in policies:
        alert_key = f"policy-{policy.id}:automation:run-{run.id}"
        row = AlertHistory.query.filter_by(alert_key=alert_key).first()
        if not row:
            row = AlertHistory(
                alert_key=alert_key,
                policy_id=policy.id,
                policy_name=policy.name,
                cluster_id=run.cluster_id,
                namespace=run.namespace,
                resource_type="deployment",
                resource_name=run.deployment_name,
                alert_type="automation",
                severity=policy.severity,
                status="active",
                title=title,
                description=description,
                triggered_conditions=triggered,
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
            row.triggered_conditions = triggered
            row.metric_snapshot = snapshot
            row.fired_at = row.fired_at or now
        db.session.flush()
        try:
            dispatch_policy_alert_notifications(_history_to_automation_alert_dict(row))
        except Exception:  # a delivery problem must never mask the run failure
            logger.exception(
                "Automation alert notification failed: policy_id=%s run_id=%s",
                policy.id,
                run.id,
            )


def resolve_run_success_alerts(run) -> None:
    """A run deployed successfully — resolve any active automation alerts for
    the same deployment target (earlier failed attempts of this change)."""
    rows = AlertHistory.query.filter_by(
        alert_type="automation",
        status="active",
        cluster_id=run.cluster_id,
        namespace=run.namespace,
        resource_name=run.deployment_name,
    ).all()
    if not rows:
        return
    now = datetime.now(timezone.utc)
    for row in rows:
        row.status = "resolved"
        row.resolved_at = now
