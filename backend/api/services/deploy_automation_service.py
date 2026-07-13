"""Ticket-driven deploy automation: Zoho ticket → registry gate → Jenkins router
build → registry verify → Change Bundle (approval clusters) or direct image apply.

Flow per run (DEPLOY-AUTOMATION-PLAN.md):

1. ``queued``            resolve the live deployment's current image → derive the
                         target ``repo:tag`` from the ticket's tag.
2. ``checking_image``    HEAD the registry. Found → skip the build entirely.
                         Missing → trigger the Jenkins ROUTER pipeline
                         (APP_NAME/NAMESPACE/IMAGE_TAG/TICKET) and poll it.
3. ``building``          queue item → router build → SUCCESS/FAILURE. The router
                         waits on the routed child job and propagates its result.
4. ``verifying_image``   re-HEAD the registry — the registry is the source of
                         truth; a green build alone is not enough.
5. Handoff by cluster policy: ``cluster_required_approvals(cluster) > 0`` →
   author + submit a Change Bundle (normal quorum approval + window; the bundle
   executor deploys it); ``0`` → ``kubectl set image`` immediately.

Variable-change runs (``change_type == "env_var"``, ticket carries a variable +
value instead of a tag) skip the Jenkins build entirely: queued → registry gate
on the RUNNING image (the change restarts pods, which must re-pull it) →
validate the variable against the live spec → the SAME handoff (bundle with the
env value edited in the live YAML, or direct ``kubectl set env``) → the same
pod-health rollout watch + rollback.

Every run is a DB row advanced by the 15s scheduler tick — fully resumable
across restarts. One active run per ticket and per (cluster, namespace,
deployment). System actions audit with ``actor=None`` (the established
convention for scheduler-driven work).
"""

from __future__ import annotations

import json
import logging
import re
import threading
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

import yaml
from sqlalchemy import and_, or_

from ..audit import log_audit
from ..db import db
from ..models import (
    ChangeBundle,
    DeployAutomationRun,
    JenkinsConnection,
    RegistryConnection,
    ZohoDeploymentSnapshot,
    ZohoInboundTicket,
)
from ..secret_encryption import decrypt_secret, encrypt_secret
from . import jenkins_client
from .jenkins_client import JenkinsConfig, JenkinsError

logger = logging.getLogger(__name__)

ACTIVE_STATUSES = (
    "queued",
    "checking_image",
    "building",
    "verifying_image",
    "awaiting_approval",
    "verifying_rollout",
)
TERMINAL_STATUSES = ("deployed", "failed", "cancelled")

# Serializes run advancement. The inline advance in start_run (web/webhook
# thread) and the scheduler tick (its own thread) must never advance the same
# run concurrently — otherwise both could pass the trigger step and fire the
# Jenkins build twice for one run. Whoever holds the lock advances to completion
# and commits; the other re-reads the committed status before proceeding.
_advance_lock = threading.RLock()

STEP_KEYS = ("image_check", "build", "verify", "approval", "deploy", "pods")

# Transient registry/Jenkins hiccups are retried this many ticks before failing.
_MAX_TRANSIENT_RETRIES = 4
# Bounded status chain per tick so one advance call can't loop forever.
_MAX_CHAIN_PER_TICK = 6


class AutomationError(Exception):
    """A user-facing automation failure. ``status`` is the HTTP code to return."""

    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


def _iso(dt: Optional[datetime]) -> Optional[str]:
    return dt.isoformat() if dt else None


def _aware(dt: Optional[datetime]) -> Optional[datetime]:
    """SQLite returns naive datetimes; normalize to aware-UTC for arithmetic."""
    if dt is None:
        return None
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt


# ---------------------------------------------------------------------------
# Jenkins connection config (single row, id=1 — same pattern as ZohoIntegration)
# ---------------------------------------------------------------------------

def get_or_create_jenkins() -> JenkinsConnection:
    row = JenkinsConnection.query.get(1)
    if row is None:
        row = JenkinsConnection(id=1)
        db.session.add(row)
        db.session.commit()
    return row


def serialize_jenkins(row: JenkinsConnection) -> Dict[str, Any]:
    """Public config view — the API token is never returned, only whether it's set."""
    return {
        "enabled": bool(row.enabled),
        "baseUrl": row.base_url or "",
        "username": row.username or "",
        "apiTokenConfigured": bool(row.api_token_encrypted),
        "buildTokenConfigured": bool(row.build_token_encrypted),
        "routerJobPath": row.router_job_path or "",
        "verifyTls": bool(row.verify_tls),
        "sendParamApp": row.send_param_app is not False,
        "sendParamNamespace": row.send_param_namespace is not False,
        "sendParamTag": row.send_param_tag is not False,
        "autoRunTickets": bool(row.auto_run_tickets),
        "autoRunClusters": row.auto_run_clusters or {},
        "imageTagTemplate": row.image_tag_template or "{tag}",
        "buildTimeoutMinutes": int(row.build_timeout_minutes or 45),
        "queueTimeoutMinutes": int(row.queue_timeout_minutes or 10),
        "bundleWindowHours": int(row.bundle_window_hours or 24),
        "rolloutTimeoutMinutes": int(row.rollout_timeout_minutes or 15),
        "rollbackOnFailure": bool(row.rollback_on_failure),
        "registryConnectionId": row.registry_connection_id,
        "lastTestAt": _iso(row.last_test_at),
        "lastTestStatus": row.last_test_status,
        "lastTestMessage": row.last_test_message,
        "updatedAt": _iso(row.updated_at),
    }


def get_jenkins_dict() -> Dict[str, Any]:
    return serialize_jenkins(get_or_create_jenkins())


def update_jenkins(payload: Dict[str, Any]) -> Dict[str, Any]:
    """Validate + apply a Jenkins config payload. Raises AutomationError."""
    row = get_or_create_jenkins()
    errors: List[str] = []

    if "enabled" in payload:
        row.enabled = bool(payload.get("enabled"))
    for key, attr in (
        ("baseUrl", "base_url"),
        ("username", "username"),
        ("routerJobPath", "router_job_path"),
    ):
        if key in payload and payload.get(key) is not None:
            setattr(row, attr, str(payload.get(key)).strip())
    if "verifyTls" in payload:
        row.verify_tls = bool(payload.get("verifyTls"))
    if "autoRunTickets" in payload:
        row.auto_run_tickets = bool(payload.get("autoRunTickets"))
    for key, attr in (
        ("sendParamApp", "send_param_app"),
        ("sendParamNamespace", "send_param_namespace"),
        ("sendParamTag", "send_param_tag"),
    ):
        if key in payload:
            setattr(row, attr, bool(payload.get(key)))
    # NULL (pre-migration row) means "send" — only three explicit offs are invalid.
    if all(
        v is False
        for v in (row.send_param_app, row.send_param_namespace, row.send_param_tag)
    ):
        errors.append("At least one router parameter (APP, NAMESPACE or TAG) must be sent.")
    if "rollbackOnFailure" in payload:
        row.rollback_on_failure = bool(payload.get("rollbackOnFailure"))
    if "autoRunClusters" in payload:
        # Per-cluster overrides: {clusterId: "auto"|"manual"}. Anything else is
        # dropped — an absent cluster inherits the global autoRunTickets default.
        raw = payload.get("autoRunClusters")
        overrides = {}
        if isinstance(raw, dict):
            for cluster_id, mode in raw.items():
                key = str(cluster_id).strip()
                value = str(mode).strip().lower()
                if key and value in ("auto", "manual"):
                    overrides[key] = value
        row.auto_run_clusters = overrides
    if "imageTagTemplate" in payload:
        template = str(payload.get("imageTagTemplate") or "").strip() or "{tag}"
        if "{tag}" not in template:
            errors.append("The image tag template must contain {tag}.")
        else:
            row.image_tag_template = template
    if "registryConnectionId" in payload:
        # Null/blank/0 = auto (match by image host). Otherwise the pinned
        # connection must actually exist so a stale pick fails loudly here
        # instead of silently falling back at check time.
        value = payload.get("registryConnectionId")
        if value in (None, "", 0, "0"):
            row.registry_connection_id = None
        else:
            try:
                conn_id = int(value)
            except (TypeError, ValueError):
                conn_id = None
            if conn_id and RegistryConnection.query.get(conn_id):
                row.registry_connection_id = conn_id
            else:
                errors.append("The selected image-check registry no longer exists.")

    for key, attr, lo, hi in (
        ("buildTimeoutMinutes", "build_timeout_minutes", 1, 24 * 60),
        ("queueTimeoutMinutes", "queue_timeout_minutes", 1, 24 * 60),
        ("bundleWindowHours", "bundle_window_hours", 1, 24 * 14),
        ("rolloutTimeoutMinutes", "rollout_timeout_minutes", 1, 24 * 60),
    ):
        if key in payload:
            try:
                setattr(row, attr, max(lo, min(hi, int(payload.get(key)))))
            except (TypeError, ValueError):
                errors.append(f"{key} must be a whole number.")

    # Tokens: write-only — blank keeps the current value; an explicit clear wipes.
    if payload.get("clearApiToken"):
        row.api_token_encrypted = None
    else:
        token = payload.get("apiToken")
        if token is not None and str(token).strip():
            row.api_token_encrypted = encrypt_secret(str(token).strip())
    if payload.get("clearBuildToken"):
        row.build_token_encrypted = None
    else:
        build_token = payload.get("buildToken")
        if build_token is not None and str(build_token).strip():
            row.build_token_encrypted = encrypt_secret(str(build_token).strip())

    if row.enabled:
        for label, value in (
            ("Base URL", row.base_url),
            ("Username", row.username),
            ("Router job path", row.router_job_path),
        ):
            if not value:
                errors.append(f"{label} is required to enable Jenkins.")
        if not row.api_token_encrypted:
            errors.append("An API token is required to enable Jenkins.")

    if errors:
        db.session.rollback()
        raise AutomationError(" ".join(errors), 400)

    db.session.add(row)
    db.session.commit()
    return serialize_jenkins(row)


def _to_client_config(row: JenkinsConnection) -> JenkinsConfig:
    return JenkinsConfig(
        base_url=row.base_url or "",
        username=row.username or "",
        api_token=decrypt_secret(row.api_token_encrypted or ""),
        router_job_path=row.router_job_path or "",
        verify_tls=bool(row.verify_tls),
        build_token=decrypt_secret(row.build_token_encrypted or ""),
    )


def test_jenkins() -> Dict[str, Any]:
    """Verify auth + router job existence; record the outcome on the config row."""
    row = get_or_create_jenkins()
    try:
        info = jenkins_client.test_connection(_to_client_config(row))
        status = "ok"
        message = f"Connected. Router job: {info.get('job')}."
    except JenkinsError as exc:
        status = "error"
        message = str(exc)
    row.last_test_at = datetime.now(timezone.utc)
    row.last_test_status = status
    row.last_test_message = message
    db.session.add(row)
    db.session.commit()
    return {"status": status, "message": message, **serialize_jenkins(row)}


# ---------------------------------------------------------------------------
# Step log — the JSON list the UI renders as pipeline chips
# ---------------------------------------------------------------------------

def _initial_steps() -> List[Dict[str, Any]]:
    return [{"key": k, "status": "wait", "detail": "", "at": None} for k in STEP_KEYS]


def _set_step(run: DeployAutomationRun, key: str, status: str, detail: str = "") -> None:
    """Update one step. Reassigns ``run.steps`` — SQLAlchemy JSON columns don't
    track in-place mutation."""
    steps = [dict(s) for s in (run.steps or _initial_steps())]
    by_key = {s.get("key"): s for s in steps}
    step = by_key.get(key)
    if step is None:
        step = {"key": key, "status": "wait", "detail": "", "at": None}
        steps.append(step)
    step["status"] = status
    step["detail"] = detail
    step["at"] = datetime.now(timezone.utc).isoformat()
    run.steps = steps


def _fail(run: DeployAutomationRun, step_key: str, message: str) -> None:
    _set_step(run, step_key, "fail", message)
    run.status = "failed"
    run.error = message
    run.finished_at = datetime.now(timezone.utc)
    log_audit(
        "automation_run_failed",
        actor=None,
        target_type="deploy_automation_run",
        target_id=str(run.id),
        details={"ticket": run.ticket_number, "deployment": run.deployment_name, "error": message},
        commit=False,
    )
    try:
        from .automation_alert_service import fire_run_failure_alerts

        fire_run_failure_alerts(run)
    except Exception:  # alerting must never mask the failure itself
        logger.exception("Automation failure alerting failed: run_id=%s", run.id)
    _report_outcome(run, "failed")


# ---------------------------------------------------------------------------
# Run lifecycle
# ---------------------------------------------------------------------------

def resolve_image_tag(row: Optional[JenkinsConnection], raw_tag: str) -> str:
    """Apply the image tag template to a ticket's raw tag.

    ``"1.72.1"`` with template ``"v{tag}-prod"`` → ``"v1.72.1-prod"``. If the
    operator already typed the full registry form (the raw value matches the
    template's shape, case-insensitively), it is used as-is instead of being
    templated twice ("vV1.72.1-prod-prod").
    """
    raw = (raw_tag or "").strip()
    template = ((row.image_tag_template if row else None) or "{tag}").strip()
    if template == "{tag}" or "{tag}" not in template:
        return raw
    prefix, _, suffix = template.partition("{tag}")
    already = re.fullmatch(
        re.escape(prefix) + r".+" + re.escape(suffix), raw, flags=re.IGNORECASE
    )
    if already:
        return raw
    return template.replace("{tag}", raw)


def _active_conflict(ticket_record_id: int, cluster_id: str, namespace: str, name: str):
    return (
        DeployAutomationRun.query.filter(
            DeployAutomationRun.status.in_(ACTIVE_STATUSES),
            or_(
                DeployAutomationRun.ticket_record_id == ticket_record_id,
                and_(
                    DeployAutomationRun.cluster_id == cluster_id,
                    DeployAutomationRun.namespace == namespace,
                    DeployAutomationRun.deployment_name == name,
                ),
            ),
        )
        .order_by(DeployAutomationRun.id.desc())
        .first()
    )


def start_run(ticket_record_id: int, user=None, auto: bool = False) -> Dict[str, Any]:
    """Create a run for one inbound ticket. Raises AutomationError on bad input.

    A ticket carries exactly ONE change: an image tag (deploy flow) or a
    variable + value (env-var change flow) — the run's ``change_type`` follows.
    """
    ticket = ZohoInboundTicket.query.get(int(ticket_record_id))
    if ticket is None:
        raise AutomationError("Inbound ticket not found.", 404)
    if not ticket.resolved or not ticket.app_service_id:
        raise AutomationError("This ticket did not resolve to a deployment — nothing to run.", 400)
    tag = (ticket.tag or "").strip()
    variable = (ticket.variable_name or "").strip()
    value = (ticket.variable_value or "").strip()
    if tag and variable:
        raise AutomationError(
            "This ticket has both an image tag and a variable — automation needs exactly one change.",
            400,
        )
    if not tag and not variable:
        raise AutomationError(
            "This ticket has no image tag and no variable — automation needs one change.", 400
        )
    if variable and not value:
        raise AutomationError("This ticket picks a variable but has no value to set it to.", 400)
    snapshot = ZohoDeploymentSnapshot.query.get(ticket.app_service_id)
    if snapshot is None:
        raise AutomationError("The ticket's deployment snapshot no longer exists.", 404)
    from .zoho_sync_service import CUSTOM_SOURCE_CLUSTER

    if snapshot.cluster_id == CUSTOM_SOURCE_CLUSTER and variable:
        raise AutomationError(
            f"'{snapshot.namespace}' is a custom environment — variable changes need a live "
            "cluster deployment; only tag deploys route to Jenkins.",
            400,
        )

    conflict = _active_conflict(ticket.id, snapshot.cluster_id, snapshot.namespace, snapshot.deployment_name)
    if conflict:
        raise AutomationError(
            f"Run #{conflict.id} is already active for this "
            f"{'ticket' if conflict.ticket_record_id == ticket.id else 'deployment'} "
            f"({conflict.status.replace('_', ' ')}).",
            409,
        )

    jrow = get_or_create_jenkins()
    run = DeployAutomationRun(
        ticket_record_id=ticket.id,
        ticket_number=ticket.ticket_number or ticket.ticket_id,
        snapshot_id=snapshot.id,
        cluster_id=snapshot.cluster_id,
        namespace=snapshot.namespace,
        deployment_name=snapshot.deployment_name,
        change_type="env_var" if variable else "image",
        variable_name=variable or None,
        variable_value=value if variable else None,
        # Registry/deploy tag = template applied; the router gets the raw tag.
        image_tag=resolve_image_tag(jrow, tag) if tag else "",
        ticket_tag=tag or None,
        status="queued",
        steps=_initial_steps(),
        auto=bool(auto),
        triggered_by=(getattr(user, "username", None) or ("auto" if auto else None)),
    )
    if variable:
        # No Jenkins build for a variable change; image_check gates the RUNNING
        # image (the change restarts pods) and verify becomes the variable check.
        _set_step(run, "build", "skip", "not needed for a variable change")
    db.session.add(run)
    db.session.commit()

    log_audit(
        "automation_run_started",
        actor=user,
        target_type="deploy_automation_run",
        target_id=str(run.id),
        details={
            "ticket": run.ticket_number,
            "deployment": run.deployment_name,
            "namespace": run.namespace,
            "clusterId": run.cluster_id,
            "changeType": run.change_type,
            "tag": run.image_tag,
            "ticketTag": run.ticket_tag,
            "variable": run.variable_name,
            "value": run.variable_value,
            "auto": bool(auto),
        },
    )
    # Mark the ticket as picked up (status → started/Open, owner → zagent).
    _report_outcome(run, "started")

    # Advance immediately so a manual Run gives instant feedback (the first
    # stages are seconds); errors are recorded on the run, never raised. Under
    # the advance lock + a refresh so a concurrent scheduler tick can't have
    # already moved this run past the trigger (which would double-fire the build).
    try:
        with _advance_lock:
            db.session.refresh(run)
            if run.status in ACTIVE_STATUSES:
                _advance(run, jrow)
                db.session.commit()
    except Exception:  # pragma: no cover — advance already isolates per-step errors
        db.session.rollback()
    return serialize_run(run)


def _withdraw_bundle(run: DeployAutomationRun, user) -> str:
    """Stop the run's Change Bundle so a cancelled run can't deploy later.

    Without this, a pending or approved bundle would outlive the cancelled run:
    the executor would still apply it, with no run watching the rollout (no
    pod-health gate, no rollback). A ``deploying`` bundle can't be stopped.
    Returns a sentence for the cancel note ("" when there is no bundle or it is
    already terminal).
    """
    from .change_bundle_service import ChangeBundleError, decide_bundle

    bundle = ChangeBundle.query.get(run.bundle_id) if run.bundle_id else None
    if bundle is None:
        return ""
    reason = f"Automation run #{run.id} was cancelled."
    if bundle.status == "pending_approval":
        try:
            decide_bundle(bundle.id, "decline", actor=user, reason=reason)
            return f"Change bundle #{bundle.id} was rejected."
        except ChangeBundleError:
            # The bundle moved on concurrently (approved/expired/…) — re-read
            # and fall through to the state it is in now.
            db.session.rollback()
            bundle = ChangeBundle.query.get(run.bundle_id)
            if bundle is None:
                return ""
    if bundle.status in ("approved", "scheduled"):
        # Approved but not yet picked up by the executor. decide_bundle only
        # handles pending bundles, so withdraw directly — the executor skips
        # anything that is no longer approved/scheduled.
        bundle.status = "rejected"
        bundle.rejection_reason = reason
        db.session.commit()
        return f"Change bundle #{bundle.id} was withdrawn before execution."
    if bundle.status == "deploying":
        return f"Change bundle #{bundle.id} is already executing and cannot be stopped."
    return ""


def cancel_run(run_id: int, user=None) -> Dict[str, Any]:
    run = DeployAutomationRun.query.get(int(run_id))
    if run is None:
        raise AutomationError("Automation run not found.", 404)
    if run.status not in ACTIVE_STATUSES:
        raise AutomationError(f"Run is already {run.status} — nothing to cancel.", 409)
    note = "Cancelled by operator."
    if run.status == "building":
        note = "Cancelled by operator — the Jenkins build itself keeps running."
    bundle_note = _withdraw_bundle(run, user)
    if bundle_note:
        note = f"{note} {bundle_note}"
    for step in ("image_check", "build", "verify", "approval", "deploy"):
        current = next((s for s in (run.steps or []) if s.get("key") == step), None)
        if current and current.get("status") in ("run", "wait"):
            _set_step(run, step, "skip", "cancelled")
    run.status = "cancelled"
    run.error = note
    run.finished_at = datetime.now(timezone.utc)
    db.session.commit()
    details = {"ticket": run.ticket_number, "deployment": run.deployment_name}
    if run.bundle_id:
        details["bundleId"] = run.bundle_id
        details["bundleOutcome"] = bundle_note or "already terminal"
    log_audit(
        "automation_run_cancelled",
        actor=user,
        target_type="deploy_automation_run",
        target_id=str(run.id),
        details=details,
    )
    _report_outcome(run, "cancelled")
    return serialize_run(run)


def serialize_run(run: DeployAutomationRun) -> Dict[str, Any]:
    bundle_status = None
    if run.bundle_id:
        bundle = ChangeBundle.query.get(run.bundle_id)
        bundle_status = bundle.status if bundle else "deleted"
    return {
        "id": run.id,
        "ticketRecordId": run.ticket_record_id,
        "ticketNumber": run.ticket_number,
        "clusterId": run.cluster_id,
        "namespace": run.namespace,
        "deploymentName": run.deployment_name,
        "containerName": run.container_name,
        "changeType": run.change_type or "image",
        "customEnvironment": _is_custom_run(run),
        "variableName": run.variable_name,
        "variableValue": run.variable_value,
        "imageRepo": run.image_repo,
        "imageTag": run.image_tag,
        "ticketTag": run.ticket_tag,
        "status": run.status,
        "error": run.error,
        "steps": run.steps or [],
        "jenkinsBuildUrl": run.jenkins_build_url,
        "jenkinsBuildNumber": run.jenkins_build_number,
        "bundleId": run.bundle_id,
        "bundleStatus": bundle_status,
        "auto": bool(run.auto),
        "triggeredBy": run.triggered_by,
        "createdAt": _iso(_aware(run.created_at)),
        "updatedAt": _iso(_aware(run.updated_at)),
        "finishedAt": _iso(_aware(run.finished_at)),
    }


def list_runs(limit: int = 50) -> List[Dict[str, Any]]:
    rows = (
        DeployAutomationRun.query.order_by(DeployAutomationRun.id.desc())
        .limit(max(1, min(int(limit or 50), 200)))
        .all()
    )
    return [serialize_run(r) for r in rows]


# ---------------------------------------------------------------------------
# State machine — advanced on every scheduler tick
# ---------------------------------------------------------------------------

def advance_runs() -> int:
    """Advance every active run one (chained) step. Returns how many advanced.

    Each run is isolated in try/except so one broken run can't stall the rest;
    an unexpected exception fails that run with the error recorded.
    """
    # Hold the advance lock for the whole batch and query INSIDE it, so an inline
    # advance from start_run can't interleave and re-trigger a run mid-flight.
    with _advance_lock:
        runs = (
            DeployAutomationRun.query.filter(DeployAutomationRun.status.in_(ACTIVE_STATUSES))
            .order_by(DeployAutomationRun.id.asc())
            .all()
        )
        if not runs:
            return 0
        jrow = get_or_create_jenkins()
        advanced = 0
        for run in runs:
            try:
                _advance(run, jrow)
                db.session.commit()
                advanced += 1
            except Exception as exc:  # defensive: never let one run break the tick
                db.session.rollback()
                try:
                    _fail(run, _current_step_key(run), f"Unexpected automation error: {exc}")
                    db.session.commit()
                except Exception:
                    db.session.rollback()
        return advanced


def _current_step_key(run: DeployAutomationRun) -> str:
    return {
        "queued": "image_check",
        "checking_image": "image_check",
        "building": "build",
        "verifying_image": "verify",
        "awaiting_approval": "approval",
        "verifying_rollout": "pods",
    }.get(run.status, "deploy")


def _advance(run: DeployAutomationRun, jrow: JenkinsConnection) -> None:
    """Advance one run, chaining cheap transitions within the same tick."""
    for _ in range(_MAX_CHAIN_PER_TICK):
        before = run.status
        if run.status == "queued":
            # Custom (non-cluster) environments go straight to their Jenkins job;
            # variable changes skip the registry/Jenkins stages: validate the
            # variable against the live spec, then hand off straight away.
            if _is_custom_run(run):
                _do_trigger_custom(run, jrow)
            elif _is_env_run(run):
                _do_resolve_variable(run, jrow)
            else:
                _do_resolve(run)
        elif run.status == "checking_image":
            _do_check(run, jrow)
        elif run.status == "building":
            _do_poll_build(run, jrow)
        elif run.status == "verifying_image":
            _do_verify(run, jrow)
        elif run.status == "awaiting_approval":
            _do_watch_bundle(run)
        elif run.status == "verifying_rollout":
            _do_check_pods(run, jrow)
        if run.status == before or run.status in TERMINAL_STATUSES:
            break


def _target_image(run: DeployAutomationRun) -> str:
    return f"{run.image_repo}:{run.image_tag}"


def _is_env_run(run: DeployAutomationRun) -> bool:
    return (run.change_type or "image") == "env_var"


def _is_custom_run(run: DeployAutomationRun) -> bool:
    """A run against a custom (non-cluster) environment — snapshotted under the
    sentinel cluster id. No live deployment exists: the whole deploy is the
    configured Jenkins job, and build success is the outcome."""
    from .zoho_sync_service import CUSTOM_SOURCE_CLUSTER

    return run.cluster_id == CUSTOM_SOURCE_CLUSTER


def _change_summary(run: DeployAutomationRun) -> str:
    """Human one-liner of what this run changes — used in messages/emails/audit."""
    if _is_env_run(run):
        return f"{run.variable_name}={run.variable_value}"
    return _target_image(run)


def _container_names(run: DeployAutomationRun) -> List[str]:
    """The container_name column holds a comma-joined list for env-var runs."""
    return [c.strip() for c in (run.container_name or "").split(",") if c.strip()]


def _zoho_ticket_id(run: DeployAutomationRun) -> Optional[str]:
    """The Desk ticket id (not KubeSight's row id) for write-back, if any."""
    if not run.ticket_record_id:
        return None
    rec = ZohoInboundTicket.query.get(run.ticket_record_id)
    return rec.ticket_id if rec else None


def _report_outcome(run: DeployAutomationRun, outcome: str) -> None:
    """Write this run's result back to its Desk ticket (best-effort, off-thread).

    ``outcome`` ∈ started | deployed | failed | cancelled — maps to the operator's
    configured status label, with a plain-text comment (and a resolution on
    terminal outcomes) describing what happened.
    """
    from .zoho_sync_service import report_ticket_outcome

    ticket_id = _zoho_ticket_id(run)
    if not ticket_id:
        return
    who = run.ticket_number or f"run #{run.id}"
    env_run = _is_env_run(run)
    # What the ticket asked for, phrased per change type.
    change = _change_summary(run) if env_run else run.image_tag
    comment = None
    resolution = None
    if outcome == "started":
        comment = (
            f"KubeSight automation started for {run.deployment_name} -> {change} "
            f"in {run.namespace} ({who})."
        )
    elif outcome == "deployed":
        mode = "change bundle" if run.bundle_id else "direct apply"
        if _is_custom_run(run):
            shown_tag = run.ticket_tag or run.image_tag
            comment = (
                f"Deploy succeeded: the Jenkins job for {run.namespace} finished successfully "
                f"({run.deployment_name} @ {shown_tag})."
            )
            resolution = (
                f"KubeSight routed {run.deployment_name} (tag {shown_tag}) for {run.namespace} "
                f"to Jenkins; the build succeeded. ({who})"
            )
        elif env_run:
            comment = (
                f"Variable change applied: {change} on {run.deployment_name} "
                f"in {run.namespace} (via {mode}); pods reported healthy."
            )
            resolution = (
                f"KubeSight set {change} on {run.deployment_name} in {run.namespace}; "
                f"pods reported healthy. ({who})"
            )
        else:
            comment = (
                f"Deployment succeeded: {run.deployment_name} is now running {_target_image(run)} "
                f"in {run.namespace} (via {mode})."
            )
            resolution = (
                f"KubeSight deployed {run.deployment_name} to {run.namespace} at tag "
                f"{run.image_tag}; pods reported healthy. ({who})"
            )
    elif outcome == "failed":
        comment = (
            f"Automation failed for {run.deployment_name} -> {change} in "
            f"{run.namespace}: {run.error or 'unknown error'}"
        )
        resolution = f"KubeSight automation did not complete: {run.error or 'unknown error'}"
    elif outcome == "cancelled":
        comment = (
            f"Automation cancelled for {run.deployment_name} -> {change} in "
            f"{run.namespace}. {run.error or ''}".strip()
        )
    try:
        report_ticket_outcome(ticket_id, outcome, comment=comment, resolution=resolution)
    except Exception:  # write-back must never affect the run
        pass


def _do_resolve(run: DeployAutomationRun) -> None:
    """queued → checking_image: read the live deployment's image, derive the repo."""
    from ..k8s_provider import (
        K8sCommandError,
        _run_for_access,
        resolve_cluster_access,
        should_use_real_k8s,
    )
    from .registry_client import parse_image_reference

    _set_step(run, "image_check", "run", "reading the live deployment image")

    containers: List[Dict[str, str]] = []
    if should_use_real_k8s(run.cluster_id):
        access = resolve_cluster_access(run.cluster_id)
        if not access:
            _fail(run, "image_check", f"Cluster '{run.cluster_id}' was not found.")
            return
        try:
            raw = _run_for_access(
                access,
                ["get", "deployment", run.deployment_name, "-n", run.namespace, "-o", "json"],
            )
            spec = json.loads(raw)
        except (K8sCommandError, ValueError) as exc:
            _fail(run, "image_check", f"Could not read deployment '{run.deployment_name}': {exc}")
            return
        for c in ((spec.get("spec") or {}).get("template") or {}).get("spec", {}).get("containers", []) or []:
            if c.get("name") and c.get("image"):
                containers.append({"name": c["name"], "image": c["image"]})
    else:
        from ..mock_data import NAMESPACE_RESOURCES

        ns_res = (NAMESPACE_RESOURCES.get(run.cluster_id) or {}).get(run.namespace) or {}
        entry = next(
            (d for d in ns_res.get("deployments", []) if d.get("name") == run.deployment_name), None
        )
        if entry and entry.get("image"):
            containers.append({"name": run.deployment_name, "image": entry["image"]})

    if not containers:
        _fail(
            run,
            "image_check",
            f"Deployment '{run.deployment_name}' in '{run.namespace}' has no readable container image.",
        )
        return

    chosen = containers[0]
    parsed = parse_image_reference(chosen["image"])
    if parsed is None:
        _fail(run, "image_check", f"Could not parse the running image reference '{chosen['image']}'.")
        return

    run.container_name = chosen["name"]
    run.image_repo = f"{parsed.registry}/{parsed.repository}" if parsed.registry else parsed.repository
    note = f"target {_target_image(run)}"
    if len(containers) > 1:
        note += f" (first of {len(containers)} containers: {chosen['name']})"
    _set_step(run, "image_check", "run", note)
    run.retry_count = 0
    run.status = "checking_image"


def _gate_running_images(run: DeployAutomationRun, images: List[str], check_image) -> bool:
    """Image gate for variable runs: every RUNNING container image must still be
    pullable before we trigger a restart. Returns True to continue; on False the
    run was failed, or left queued so the next tick retries a flaky registry.
    """
    unverified: List[str] = []
    for image in images:
        result = check_image(image)
        status = result.get("status")
        if status == "not_found":
            _fail(
                run,
                "image_check",
                f"The running image {image} is no longer in the registry — a variable "
                "change restarts the pods and they would fail to pull it. Restore the "
                "image (or deploy a valid tag) first.",
            )
            return False
        if status == "unreachable":
            run.retry_count = (run.retry_count or 0) + 1
            if run.retry_count > _MAX_TRANSIENT_RETRIES:
                _fail(run, "image_check", f"Registry unreachable while checking {image}: {result.get('message')}")
            else:
                _set_step(
                    run, "image_check", "run",
                    f"registry unreachable — retrying ({run.retry_count}/{_MAX_TRANSIENT_RETRIES})",
                )
            return False
        if status == "no_connection":
            unverified.append(image)
    if not images:
        _set_step(run, "image_check", "skip", "no readable container image to verify")
    elif len(unverified) == len(images):
        _set_step(
            run, "image_check", "done",
            "no linked registry owns the running image(s) — cannot verify; continuing",
        )
    elif unverified:
        _set_step(
            run, "image_check", "done",
            f"{len(images) - len(unverified)} image(s) verified; "
            f"{len(unverified)} not covered by a linked registry",
        )
    else:
        _set_step(
            run, "image_check", "done",
            f"running image{'s' if len(images) > 1 else ''} still in the registry",
        )
    run.retry_count = 0
    return True


def _do_resolve_variable(run: DeployAutomationRun, jrow: JenkinsConnection) -> None:
    """queued → handoff for env-var runs, through two gates.

    1. Image gate — the change rolls every pod, and rescheduled pods must
       re-pull their images: if the RUNNING tag has been pruned from the
       registry the rollout would die in ImagePullBackOff, so verify it first.
    2. Variable gate — the ticket's variable must exist as a LITERAL value on
       the live deployment (a ``valueFrom`` ConfigMap/Secret reference can't be
       set to a literal safely); the apply targets exactly the containers that
       define it.
    """
    from ..k8s_provider import (
        K8sCommandError,
        _run_for_access,
        resolve_cluster_access,
        should_use_real_k8s,
    )
    from .registry_service import check_image

    def check(image: str):
        return check_image(image, preferred_connection_id=jrow.registry_connection_id)

    var = run.variable_name or ""
    _set_step(run, "image_check", "run", "checking the running image is still in the registry")

    if not should_use_real_k8s(run.cluster_id):
        from ..mock_data import NAMESPACE_RESOURCES

        ns_res = (NAMESPACE_RESOURCES.get(run.cluster_id) or {}).get(run.namespace) or {}
        entry = next(
            (d for d in ns_res.get("deployments", []) if d.get("name") == run.deployment_name), None
        )
        image = (entry or {}).get("image")
        if not _gate_running_images(run, [image] if image else [], check):
            return
        run.container_name = run.deployment_name
        _set_step(run, "verify", "done", f"[mock] {var} accepted")
        _do_handoff(run)
        return

    access = resolve_cluster_access(run.cluster_id)
    if not access:
        _fail(run, "image_check", f"Cluster '{run.cluster_id}' was not found.")
        return
    try:
        raw = _run_for_access(
            access,
            ["get", "deployment", run.deployment_name, "-n", run.namespace, "-o", "json"],
        )
        spec = json.loads(raw)
    except (K8sCommandError, ValueError) as exc:
        _fail(run, "image_check", f"Could not read deployment '{run.deployment_name}': {exc}")
        return

    containers = (
        (((spec.get("spec") or {}).get("template") or {}).get("spec") or {}).get("containers") or []
    )

    # Gate 1: every unique running image must still be pullable.
    images: List[str] = []
    for c in containers:
        img = c.get("image")
        if img and img not in images:
            images.append(img)
    if not _gate_running_images(run, images, check):
        return

    # Gate 2: the variable itself. The Zoho picklist merges names
    # case-insensitively, so the ticket's value may not match the deployment's
    # exact spelling — match loosely, then apply with the ACTUAL live name
    # (kubectl/env names are case-sensitive).
    _set_step(run, "verify", "run", f"checking {var} on the live deployment")
    by_actual: Dict[str, List[str]] = {}
    referenced = False
    available: List[str] = []
    for c in containers:
        for entry in c.get("env") or []:
            name = entry.get("name")
            if not name:
                continue
            if name.casefold() == var.casefold():
                if "valueFrom" in entry:
                    referenced = True
                elif c.get("name"):
                    by_actual.setdefault(name, []).append(c["name"])
            if "valueFrom" not in entry and name not in available:
                available.append(name)

    if by_actual:
        if var in by_actual:
            actual = var
        elif len(by_actual) == 1:
            actual = next(iter(by_actual))
        else:
            _fail(
                run,
                "verify",
                f"'{var}' matches several differently-cased variables on "
                f"'{run.deployment_name}' ({', '.join(sorted(by_actual))}) — use the exact name.",
            )
            return
        run.variable_name = actual
        run.container_name = ",".join(by_actual[actual])
        _set_step(run, "verify", "done", f"{actual} found on container(s) {run.container_name}")
        run.retry_count = 0
        _do_handoff(run)
        return
    if referenced:
        _fail(
            run,
            "verify",
            f"'{var}' on '{run.deployment_name}' comes from a ConfigMap/Secret reference "
            "(valueFrom) — change it at its source instead of on the Deployment.",
        )
        return
    hint = ", ".join(sorted(available)[:15]) or "none"
    _fail(
        run,
        "verify",
        f"Deployment '{run.deployment_name}' in '{run.namespace}' has no environment "
        f"variable '{var}'. Changeable variables: {hint}.",
    )


# A parameter template placeholder: {app}, {tag}, {environment}, {ticket} or any
# inbound-ticket field name like {cf_country} / {cf_debugProd}.
_PARAM_PLACEHOLDER_RE = re.compile(r"\{([A-Za-z0-9_.\-]+)\}")


def _render_param(template: str, run: DeployAutomationRun, payload: Dict[str, Any]) -> str:
    """Fill one Jenkins parameter template from the run + its ticket payload.

    Built-ins resolve from the run ({app}/{tag}/{environment}/{ticket}); anything
    else is looked up in the inbound webhook payload (top level and the nested
    cf/customFields containers), so a Zoho custom field like cf_country flows
    through as {cf_country}. Unknown fields render as "" — the Jenkins job's
    parameter default then applies. Booleans (Zoho checkboxes) become
    "true"/"false" as Jenkins checkbox params expect.
    """
    from .zoho_sync_service import _extract

    def _value(match) -> str:
        key = match.group(1)
        low = key.lower()
        if low == "app":
            return run.deployment_name or ""
        if low == "tag":
            return run.ticket_tag or run.image_tag or ""
        if low in ("environment", "env", "namespace"):
            return run.namespace or ""
        if low == "ticket":
            return run.ticket_number or ""
        value = _extract(payload, key)
        if value is None:
            return ""
        if isinstance(value, bool):
            return "true" if value else "false"
        return str(value)

    return _PARAM_PLACEHOLDER_RE.sub(_value, str(template))


def _custom_params(
    run: DeployAutomationRun, env_cfg: Dict[str, Any], jrow: JenkinsConnection
) -> Dict[str, str]:
    """The buildWithParameters fields for a custom-environment run.

    The entry's operator-defined parameter map wins (each value a template
    rendered via :func:`_render_param`); with no map configured, fall back to
    the standard router contract (APP/TAG/NAMESPACE, honoring the toggles).
    """
    templates = (env_cfg or {}).get("jenkinsParams") or {}
    if not templates:
        params: Dict[str, str] = {}
        if jrow.send_param_app is not False:
            params["APP"] = run.deployment_name
        if jrow.send_param_tag is not False:
            params["TAG"] = run.ticket_tag or run.image_tag
        if jrow.send_param_namespace is not False:
            params["NAMESPACE"] = run.namespace
        return params
    payload: Dict[str, Any] = {}
    if run.ticket_record_id:
        record = ZohoInboundTicket.query.get(run.ticket_record_id)
        if record is not None and isinstance(record.payload, dict):
            payload = record.payload
    return {name: _render_param(template, run, payload) for name, template in templates.items()}


def _do_trigger_custom(run: DeployAutomationRun, jrow: JenkinsConnection) -> None:
    """queued → building for a custom (non-cluster) environment run.

    There is no live deployment behind these targets — no image to read, no
    registry repo to gate on, nothing for kubectl to apply or watch. The
    configured Jenkins job (the entry's own path, else the router) owns the
    whole deploy; it is triggered with the entry's parameter map and build
    success is the run's outcome.
    """
    from .zoho_sync_service import custom_environment_by_name

    _set_step(run, "image_check", "skip", "custom environment — no live cluster image to gate on")

    env_cfg = custom_environment_by_name(run.namespace) or {}
    job_path = str(env_cfg.get("jenkinsJobPath") or "").strip()
    path = job_path or (jrow.router_job_path or "")
    if not (jrow.enabled and jrow.base_url and path and jrow.api_token_encrypted):
        _fail(
            run,
            "build",
            f"'{run.namespace}' is a custom environment — it deploys only through Jenkins, "
            "and the Jenkins connection (or a job path for it) is not configured.",
        )
        return

    params = _custom_params(run, env_cfg, jrow)
    cfg = _to_client_config(jrow)
    if job_path:
        cfg = replace(cfg, router_job_path=job_path)
    try:
        queue_url = jenkins_client.trigger_build(cfg, params)
    except JenkinsError as exc:
        _fail(run, "build", f"Could not trigger the Jenkins build on '{path}': {exc}")
        return

    run.jenkins_queue_url = queue_url
    run.build_triggered_at = datetime.now(timezone.utc)
    run.retry_count = 0
    run.status = "building"
    shown = ", ".join(f"{k}={v}" for k, v in params.items()) or "no parameters"
    _set_step(run, "build", "run", f"build queued on {path} ({shown})")
    log_audit(
        "automation_build_triggered",
        actor=None,
        target_type="deploy_automation_run",
        target_id=str(run.id),
        details={
            "ticket": run.ticket_number,
            "deployment": run.deployment_name,
            "environment": run.namespace,
            "custom": True,
            "job": path,
            "params": params,
            "queueUrl": queue_url,
        },
        commit=False,
    )


def _do_check(run: DeployAutomationRun, jrow: JenkinsConnection) -> None:
    """checking_image → handoff (found) | building (missing) | failed."""
    from .registry_service import check_image

    target = _target_image(run)
    result = check_image(target, preferred_connection_id=jrow.registry_connection_id)
    status = result.get("status")

    if status == "found":
        _set_step(run, "image_check", "done", f"{target} already in the registry")
        _set_step(run, "build", "skip", "image already present — no build needed")
        _set_step(run, "verify", "skip", "verified by the pre-check")
        _do_handoff(run)
        return

    if status == "unreachable":
        run.retry_count = (run.retry_count or 0) + 1
        if run.retry_count > _MAX_TRANSIENT_RETRIES:
            _fail(run, "image_check", f"Registry unreachable while checking {target}: {result.get('message')}")
        else:
            _set_step(
                run, "image_check", "run",
                f"registry unreachable — retrying ({run.retry_count}/{_MAX_TRANSIENT_RETRIES})",
            )
        return

    # not_found or no_connection → a build is required.
    if status == "no_connection":
        _set_step(
            run, "image_check", "done",
            "no linked registry owns this image host — cannot verify; the build result will be trusted",
        )
    else:
        _set_step(run, "image_check", "done", f"{run.image_tag} not in the registry — build required")

    if not (jrow.enabled and jrow.base_url and jrow.router_job_path and jrow.api_token_encrypted):
        _fail(
            run, "build",
            "The image tag is not in the registry and Jenkins is not configured — configure the "
            "Jenkins router connection or push the image manually.",
        )
        return

    try:
        # The verified router contract (2026-07-08): APP / TAG / NAMESPACE, with
        # the job-level `token` appended by the client. APP is the Kubernetes
        # deployment name; TAG is the RAW ticket tag — the router owns the naming
        # convention (e.g. v{tag}-prod). Each parameter can be turned off in the
        # Jenkins settings for routers that aren't parameterized with all three.
        app = run.deployment_name
        params: Dict[str, str] = {}
        if jrow.send_param_app is not False:
            params["APP"] = app
        if jrow.send_param_tag is not False:
            params["TAG"] = run.ticket_tag or run.image_tag
        if jrow.send_param_namespace is not False:
            params["NAMESPACE"] = run.namespace
        queue_url = jenkins_client.trigger_build(_to_client_config(jrow), params)
    except JenkinsError as exc:
        _fail(run, "build", f"Could not trigger the router build: {exc}")
        return

    run.jenkins_queue_url = queue_url
    run.build_triggered_at = datetime.now(timezone.utc)
    run.retry_count = 0
    run.status = "building"
    _set_step(run, "build", "run", f"router build queued (APP={app})")
    log_audit(
        "automation_build_triggered",
        actor=None,
        target_type="deploy_automation_run",
        target_id=str(run.id),
        details={
            "ticket": run.ticket_number,
            "deployment": run.deployment_name,
            "app": app,
            "tag": run.ticket_tag or run.image_tag,
            "queueUrl": queue_url,
        },
        commit=False,
    )


def _do_poll_build(run: DeployAutomationRun, jrow: JenkinsConnection) -> None:
    """building → verifying_image | failed. Polls queue item, then the router build."""
    cfg = _to_client_config(jrow)
    triggered = _aware(run.build_triggered_at) or datetime.now(timezone.utc)
    now = datetime.now(timezone.utc)

    try:
        if not run.jenkins_build_url:
            state = jenkins_client.queue_state(cfg, run.jenkins_queue_url or "")
            if state["state"] == "cancelled":
                _fail(run, "build", "The router build was cancelled while still queued.")
                return
            if state["state"] == "pending":
                if now - triggered > timedelta(minutes=int(jrow.queue_timeout_minutes or 10)):
                    _fail(
                        run, "build",
                        f"Build stuck in the Jenkins queue for {jrow.queue_timeout_minutes} min "
                        f"({state.get('why') or 'no executor'}).",
                    )
                else:
                    _set_step(run, "build", "run", f"queued in Jenkins: {state.get('why') or 'waiting for executor'}")
                return
            run.jenkins_build_number = state["buildNumber"]
            run.jenkins_build_url = state["buildUrl"]
            _set_step(run, "build", "run", f"router build #{run.jenkins_build_number} running")

        result = jenkins_client.build_state(cfg, run.jenkins_build_url)
    except JenkinsError as exc:
        run.retry_count = (run.retry_count or 0) + 1
        if run.retry_count > _MAX_TRANSIENT_RETRIES:
            _fail(run, "build", f"Lost contact with Jenkins while polling the build: {exc}")
        else:
            _set_step(
                run, "build", "run",
                f"Jenkins poll failed — retrying ({run.retry_count}/{_MAX_TRANSIENT_RETRIES}): {exc}",
            )
        return

    run.retry_count = 0
    if result["building"]:
        if now - triggered > timedelta(minutes=int(jrow.build_timeout_minutes or 45)):
            _fail(
                run, "build",
                f"Router build #{run.jenkins_build_number} exceeded the {jrow.build_timeout_minutes} min "
                "timeout — KubeSight stopped tracking it (the build itself may still be running).",
            )
        else:
            elapsed = int((now - triggered).total_seconds() // 60)
            _set_step(run, "build", "run", f"router build #{run.jenkins_build_number} running ({elapsed} min)")
        return

    outcome = result.get("result") or "UNKNOWN"
    if outcome in jenkins_client.SUCCESS_RESULTS:
        if _is_custom_run(run):
            # The Jenkins job IS the deploy for a custom environment — nothing
            # in a registry or cluster to verify afterwards.
            _set_step(run, "build", "done", f"build #{run.jenkins_build_number} succeeded")
            _set_step(run, "verify", "skip", "no registry gate for a custom environment")
            _set_step(run, "approval", "skip", "Jenkins owns the deploy for custom environments")
            _set_step(run, "deploy", "done", "performed by the Jenkins job")
            _complete_deployed(
                run, "no cluster rollout to watch — the Jenkins build result is the outcome"
            )
            return
        _set_step(run, "build", "done", f"router build #{run.jenkins_build_number} succeeded")
        run.status = "verifying_image"
        _set_step(run, "verify", "run", "re-checking the registry for the built tag")
    else:
        _fail(run, "build", f"Router build #{run.jenkins_build_number} finished {outcome}.")


def _do_verify(run: DeployAutomationRun, jrow: JenkinsConnection) -> None:
    """verifying_image → handoff | failed. The registry is the source of truth."""
    from .registry_service import check_image

    target = _target_image(run)
    result = check_image(target, preferred_connection_id=jrow.registry_connection_id)
    status = result.get("status")

    if status == "found":
        _set_step(run, "verify", "done", f"{target} confirmed in the registry")
        _do_handoff(run)
        return
    if status == "no_connection":
        _set_step(run, "verify", "skip", "no linked registry — trusting the successful build")
        _do_handoff(run)
        return
    if status == "unreachable":
        run.retry_count = (run.retry_count or 0) + 1
        if run.retry_count > _MAX_TRANSIENT_RETRIES:
            _fail(run, "verify", f"Registry unreachable while verifying {target}.")
        else:
            _set_step(
                run, "verify", "run",
                f"registry unreachable — retrying ({run.retry_count}/{_MAX_TRANSIENT_RETRIES})",
            )
        return
    _fail(
        run, "verify",
        f"The router build succeeded but {target} is still not in the registry — "
        "the routed job may not push this image.",
    )


# ---------------------------------------------------------------------------
# Handoff — Change Bundle (approval clusters) or direct apply
# ---------------------------------------------------------------------------

def _do_handoff(run: DeployAutomationRun) -> None:
    from .deployment_request_service import cluster_required_approvals

    required = cluster_required_approvals(run.cluster_id)
    if required > 0:
        _handoff_bundle(run, required)
    else:
        _handoff_direct(run)


def _swap_image_in_yaml(yaml_text: str, deployment_name: str, container_name: str, new_image: str) -> str:
    """Return the manifest with ONLY the chosen container's image replaced."""
    doc = yaml.safe_load(yaml_text)
    if not isinstance(doc, dict) or (doc.get("kind") or "").lower() != "deployment":
        raise AutomationError("The live manifest is not a Deployment.", 500)
    containers = (((doc.get("spec") or {}).get("template") or {}).get("spec") or {}).get("containers") or []
    target = next((c for c in containers if c.get("name") == container_name), None) or (
        containers[0] if containers else None
    )
    if target is None:
        raise AutomationError("The live manifest has no containers.", 500)
    target["image"] = new_image
    return yaml.safe_dump(doc, sort_keys=False, default_flow_style=False)


def _set_env_in_yaml(yaml_text: str, variable: str, value: str) -> str:
    """Return the manifest with ONLY the named literal env var's value replaced.

    Every container that defines ``variable`` as a plain value gets the new
    value (mirrors what the direct ``kubectl set env -c`` apply targets);
    ``valueFrom`` entries are left untouched — the resolve step already refused
    those runs.
    """
    doc = yaml.safe_load(yaml_text)
    if not isinstance(doc, dict) or (doc.get("kind") or "").lower() != "deployment":
        raise AutomationError("The live manifest is not a Deployment.", 500)
    containers = (((doc.get("spec") or {}).get("template") or {}).get("spec") or {}).get("containers") or []
    hit = False
    for container in containers:
        for entry in container.get("env") or []:
            if entry.get("name") == variable and "valueFrom" not in entry:
                entry["value"] = str(value)
                hit = True
    if not hit:
        raise AutomationError(
            f"The live manifest defines no literal environment variable '{variable}'.", 500
        )
    return yaml.safe_dump(doc, sort_keys=False, default_flow_style=False)


def _handoff_bundle(run: DeployAutomationRun, required: int) -> None:
    """Author + submit a Change Bundle with an edit_deployment item (image swap only).

    NEVER use the change_image action type here — it regenerates a whole
    Deployment+Service from generator defaults and would clobber the live spec.
    """
    from .change_bundle_service import (
        ChangeBundleError,
        add_item,
        get_or_create_draft,
        submit_bundle,
    )
    from .resource_actions_service import get_resource_yaml

    _set_step(run, "approval", "run", "authoring the change bundle")

    data, err, _code = get_resource_yaml(None, run.cluster_id, run.namespace, "deployment", run.deployment_name)
    if err or not data or not data.get("yaml"):
        _fail(run, "approval", f"Could not read the live deployment manifest: {err or 'empty YAML'}")
        return

    try:
        if _is_env_run(run):
            swapped = _set_env_in_yaml(data["yaml"], run.variable_name or "", run.variable_value or "")
        else:
            swapped = _swap_image_in_yaml(
                data["yaml"], run.deployment_name, run.container_name or "", _target_image(run)
            )
    except (AutomationError, yaml.YAMLError) as exc:
        _fail(run, "approval", f"Could not prepare the {'variable' if _is_env_run(run) else 'image'} change: {exc}")
        return

    now = datetime.now(timezone.utc)
    jrow = get_or_create_jenkins()
    try:
        bundle = get_or_create_draft(None)  # user=None → always a fresh draft
        add_item(
            None,
            bundle.id,
            {
                "actionType": "edit_deployment",
                "clusterId": run.cluster_id,
                "namespace": run.namespace,
                "yaml": swapped,
            },
        )
        change_note = (
            f"{run.variable_name}={run.variable_value} (automated variable change)"
            if _is_env_run(run)
            else f"{run.image_tag} (automated deploy request)"
        )
        submitted = submit_bundle(
            None,
            bundle.id,
            note=f"Zoho {run.ticket_number or 'ticket'} — {run.deployment_name} → {change_note}",
            window_start=(now + timedelta(minutes=2)).isoformat(),
            window_end=(now + timedelta(hours=int(jrow.bundle_window_hours or 24))).isoformat(),
            window_timezone="UTC",
        )
    except ChangeBundleError as exc:
        _fail(run, "approval", f"Could not create the change bundle: {exc}")
        return

    run.bundle_id = bundle.id
    run.status = "awaiting_approval"
    _set_step(
        run, "approval", "run",
        f"bundle #{bundle.id} awaiting approval ({submitted.get('requiredApprovals', required)} required)",
    )
    _set_step(run, "deploy", "wait", "deploys via the bundle executor once approved")
    log_audit(
        "automation_bundle_created",
        actor=None,
        target_type="deploy_automation_run",
        target_id=str(run.id),
        details={
            "ticket": run.ticket_number,
            "bundleId": bundle.id,
            "deployment": run.deployment_name,
            "changeType": run.change_type or "image",
            "change": _change_summary(run),
        },
        commit=False,
    )


def _handoff_direct(run: DeployAutomationRun) -> None:
    """No approval required on this cluster → apply the change immediately."""
    from ..k8s_provider import K8sCommandError, should_use_real_k8s
    from .deployment_service import _run_kubectl_for_cluster

    _set_step(run, "approval", "skip", "cluster requires no approvals")

    if _is_env_run(run):
        _set_step(run, "deploy", "run", "applying the variable change")
        containers = _container_names(run)
        args = [
            "set",
            "env",
            f"deployment/{run.deployment_name}",
            f"{run.variable_name}={run.variable_value}",
            "-n",
            run.namespace,
        ]
        # Target only the containers that define the var — a bare `set env`
        # would ADD it to every container (sidecars included).
        if containers:
            args += ["-c", ",".join(containers)]
        if should_use_real_k8s(run.cluster_id):
            try:
                _run_kubectl_for_cluster(run.cluster_id, args)
            except K8sCommandError as exc:
                _fail(run, "deploy", f"kubectl set env failed: {exc}")
                return
            detail = f"{_change_summary(run)} on {run.container_name or 'deployment'}"
        else:
            detail = f"[mock] {_change_summary(run)} on {run.container_name or 'deployment'}"
        _set_step(run, "deploy", "done", detail)
        _begin_rollout_watch(run)
        return

    _set_step(run, "deploy", "run", "applying the image change")
    target = _target_image(run)
    if should_use_real_k8s(run.cluster_id):
        try:
            _run_kubectl_for_cluster(
                run.cluster_id,
                [
                    "set",
                    "image",
                    f"deployment/{run.deployment_name}",
                    f"{run.container_name or run.deployment_name}={target}",
                    "-n",
                    run.namespace,
                ],
            )
        except K8sCommandError as exc:
            _fail(run, "deploy", f"kubectl set image failed: {exc}")
            return
        detail = f"{run.container_name} → {target}"
    else:
        detail = f"[mock] {run.container_name} → {target}"

    _set_step(run, "deploy", "done", detail)
    _begin_rollout_watch(run)


def _begin_rollout_watch(run: DeployAutomationRun) -> None:
    """The image change is applied — now wait for the pods to actually come up.

    A run only counts as deployed once the deployment reports ready (e.g. 1/1);
    ``verifying_rollout`` is advanced by ``_do_check_pods`` on every tick.
    """
    run.status = "verifying_rollout"
    run.rollout_started_at = datetime.now(timezone.utc)
    run.retry_count = 0
    _set_step(run, "pods", "run", "waiting for the pods to become ready")


def _complete_deployed(run: DeployAutomationRun, pods_detail: str) -> None:
    run.status = "deployed"
    run.finished_at = datetime.now(timezone.utc)
    _set_step(run, "pods", "done", pods_detail)
    log_audit(
        "automation_run_deployed",
        actor=None,
        target_type="deploy_automation_run",
        target_id=str(run.id),
        details={
            "ticket": run.ticket_number,
            "deployment": run.deployment_name,
            "namespace": run.namespace,
            "clusterId": run.cluster_id,
            "changeType": run.change_type or "image",
            "change": _change_summary(run),
            "mode": "bundle" if run.bundle_id else "direct",
            "pods": pods_detail,
        },
        commit=False,
    )
    try:
        from .automation_alert_service import resolve_run_success_alerts

        resolve_run_success_alerts(run)
    except Exception:  # alert bookkeeping must never affect the run
        logger.exception("Automation alert resolution failed: run_id=%s", run.id)
    _report_outcome(run, "deployed")


def _do_check_pods(run: DeployAutomationRun, jrow: JenkinsConnection) -> None:
    """verifying_rollout → deployed once the NEW pods cover the desired count.

    Read errors during a rollout are treated as "still waiting" (pods churn,
    apiservers hiccup) — only the rollout timeout fails the run.
    """
    from ..k8s_provider import K8sCommandError, should_use_real_k8s
    from .deployment_service import rollout_health

    if not should_use_real_k8s(run.cluster_id):
        _complete_deployed(run, "[mock] 1/1 ready")
        return

    started = _aware(run.rollout_started_at) or datetime.now(timezone.utc)
    now = datetime.now(timezone.utc)
    timeout_min = int(jrow.rollout_timeout_minutes or 15)

    detail = ""
    try:
        health = rollout_health(run.cluster_id, run.namespace, run.deployment_name)
        if not health["observedCurrent"]:
            # Right after `set image` the controller may not have reconciled
            # yet — the counters would still describe the OLD template and
            # read fully ready (`kubectl rollout status` uses this guard).
            detail = "waiting for the controller to observe the new spec"
        else:
            desired, updated = health["desired"], health["updated"]
            total, available = health["total"], health["available"]
            if desired == 0:
                _complete_deployed(run, "deployment is scaled to 0 — no pods expected")
                return
            # ready/updated alone are not enough: during a rolling update the
            # OLD pod keeps readyReplicas satisfied while the crashlooping NEW
            # pod counts as updated. Completion = the full `kubectl rollout
            # status` condition: all pods on the new template, no old pods
            # left, and the new pods available.
            if updated >= desired and total <= updated and available >= updated:
                _complete_deployed(run, f"{available}/{desired} ready")
                return
            what = "spec" if _is_env_run(run) else "image"
            if updated < desired:
                detail = f"{updated}/{desired} pods updated to the new {what}"
            elif total > updated:
                detail = f"waiting for {total - updated} old pod(s) to terminate"
            else:
                detail = f"{available}/{updated} new pods available"
    except (K8sCommandError, ValueError) as exc:
        detail = f"could not read pod status — retrying: {exc}"

    if now - started > timedelta(minutes=timeout_min):
        _handle_rollout_failure(run, jrow, detail, timeout_min)
        return
    elapsed = int((now - started).total_seconds() // 60)
    _set_step(run, "pods", "run", f"{detail} — waiting ({elapsed}/{timeout_min} min)")


def _handle_rollout_failure(
    run: DeployAutomationRun, jrow: JenkinsConnection, last_seen: str, timeout_min: int
) -> None:
    """Rollout timed out: optionally roll back to the previous revision, fail
    the run, and email every administrator.

    ``kubectl rollout undo`` re-activates the previous ReplicaSet — the version
    that was serving before this run — so the rollback itself is non-disruptive;
    with RollingUpdate semantics the old pods were still carrying traffic while
    the new ones failed readiness.
    """
    from ..k8s_provider import K8sCommandError
    from .deployment_service import _run_kubectl_for_cluster

    what = "variable change" if _is_env_run(run) else "image"
    message = (
        f"The {what} was applied but the pods did not become ready within {timeout_min} min"
        + (f" (last seen: {last_seen})." if last_seen else ".")
    )

    rollback_note = ""
    if jrow.rollback_on_failure:
        try:
            _run_kubectl_for_cluster(
                run.cluster_id,
                ["rollout", "undo", f"deployment/{run.deployment_name}", "-n", run.namespace],
            )
            rollback_note = "Rolled back to the previous version."
        except K8sCommandError as exc:
            rollback_note = f"Automatic rollback FAILED: {exc}"
        log_audit(
            "automation_rollback",
            actor=None,
            target_type="deploy_automation_run",
            target_id=str(run.id),
            details={
                "ticket": run.ticket_number,
                "deployment": run.deployment_name,
                "namespace": run.namespace,
                "clusterId": run.cluster_id,
                "changeType": run.change_type or "image",
                "change": _change_summary(run),
                "result": rollback_note,
            },
            commit=False,
        )
    else:
        rollback_note = "Auto-rollback is disabled — the deployment was left as-is."

    _fail(run, "pods", f"{message} {rollback_note}")
    _notify_admins_rollout_failure(run, message, rollback_note)


def _admin_emails() -> List[str]:
    """Email addresses of every active administrator (shared admin convention)."""
    from ..access_engine import is_admin
    from ..models import User

    out = set()
    for user in User.query.filter_by(is_active=True).all():
        try:
            if (user.email or "").strip() and is_admin(user):
                out.add(user.email.strip())
        except Exception:
            continue
    return sorted(out)


def _notify_admins_rollout_failure(run: DeployAutomationRun, message: str, rollback_note: str) -> None:
    """Best-effort email to all admins — sent off-thread so the scheduler tick
    (or webhook request) never waits on SMTP. Failures are swallowed."""
    import threading

    from flask import current_app

    from ..email_delivery import send_email, smtp_is_configured

    try:
        if not smtp_is_configured():
            return
        recipients = _admin_emails()
        if not recipients:
            return
        subject = f"KubeSight: rollout failed — {run.deployment_name} ({run.ticket_number or f'run #{run.id}'})"
        body = "\n".join(
            [
                "A ticket-driven deployment failed its rollout health check.",
                "",
                f"Ticket:       {run.ticket_number or '-'}",
                f"Deployment:   {run.deployment_name}",
                f"Namespace:    {run.namespace}",
                f"Cluster:      {run.cluster_id}",
                f"Change:       {_change_summary(run)}",
                "",
                message,
                rollback_note,
                "",
                "Details: KubeSight → Zoho Integration → Deploy automation.",
            ]
        )
        app = current_app._get_current_object()

        def _send_all() -> None:
            for address in recipients:
                try:
                    send_email(address, subject, body)
                except Exception:
                    continue

        if app.config.get("TESTING"):
            _send_all()
            return
        threading.Thread(
            target=lambda: _run_in_app_context(app, _send_all),
            name="automation-failure-mail",
            daemon=True,
        ).start()
    except Exception:
        pass  # notification must never break the run handling


def _run_in_app_context(app, task) -> None:
    with app.app_context():
        try:
            task()
        except Exception:
            pass


def _do_watch_bundle(run: DeployAutomationRun) -> None:
    """awaiting_approval → deployed | failed, mirroring the bundle's lifecycle."""
    bundle = ChangeBundle.query.get(run.bundle_id) if run.bundle_id else None
    if bundle is None:
        _fail(run, "approval", "The change bundle was deleted before it executed.")
        return

    status = bundle.status
    if status in ("pending_approval",):
        return  # still collecting votes — step detail already says so
    if status in ("approved", "scheduled"):
        _set_step(run, "approval", "done", f"bundle #{bundle.id} approved")
        _set_step(run, "deploy", "run", "waiting for the deployment window / executor")
        return
    if status == "deploying":
        _set_step(run, "approval", "done", f"bundle #{bundle.id} approved")
        _set_step(run, "deploy", "run", "bundle executing")
        return
    if status == "completed":
        _set_step(run, "approval", "done", f"bundle #{bundle.id} approved")
        _set_step(run, "deploy", "done", f"applied by bundle #{bundle.id}")
        _begin_rollout_watch(run)
        return
    if status == "rejected":
        _fail(run, "approval", f"Change bundle #{bundle.id} was rejected" +
              (f": {bundle.rejection_reason}" if bundle.rejection_reason else "."))
        return
    if status == "expired":
        _fail(run, "approval", f"Change bundle #{bundle.id} expired before it was executed.")
        return
    if status in ("failed", "partially_failed"):
        _fail(run, "deploy", f"Change bundle #{bundle.id} execution {status.replace('_', ' ')}.")
        return


# ---------------------------------------------------------------------------
# Auto-trigger hook (called from the Zoho webhook path — must never raise)
# ---------------------------------------------------------------------------

def auto_run_enabled_for(row: Optional[JenkinsConnection], cluster_id: str) -> bool:
    """Per-cluster auto-start decision: the override map wins, else the global default."""
    if row is None:
        return False
    override = (row.auto_run_clusters or {}).get(str(cluster_id))
    if override == "auto":
        return True
    if override == "manual":
        return False
    return bool(row.auto_run_tickets)


def maybe_auto_run(ticket_record_id: int) -> Optional[Dict[str, Any]]:
    """Start a run for a freshly-resolved webhook ticket when the target
    cluster is set to auto-start (per-cluster override, else the global toggle).

    Called from ``resolve_inbound`` — swallows every error (the webhook response
    to Zoho must never depend on automation health).
    """
    try:
        row = JenkinsConnection.query.get(1)
        ticket = ZohoInboundTicket.query.get(int(ticket_record_id))
        if ticket is None or not ticket.app_service_id:
            return None
        snapshot = ZohoDeploymentSnapshot.query.get(ticket.app_service_id)
        if snapshot is None:
            return None
        if not auto_run_enabled_for(row, snapshot.cluster_id):
            return None
        # Idempotent per ticket: Zoho re-delivers webhooks (retries, edits), and
        # each delivery reuses the SAME inbound-ticket row. Auto-start is a
        # one-shot per ticket — if any run already exists for it, don't spawn
        # another. (An operator can still manually re-run via start_run.)
        if DeployAutomationRun.query.filter_by(ticket_record_id=ticket.id).first():
            return None
        return start_run(ticket_record_id, user=None, auto=True)
    except AutomationError:
        return None
    except Exception:
        db.session.rollback()
        return None
