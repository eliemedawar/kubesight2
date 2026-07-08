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

Every run is a DB row advanced by the 15s scheduler tick — fully resumable
across restarts. One active run per ticket and per (cluster, namespace,
deployment). System actions audit with ``actor=None`` (the established
convention for scheduler-driven work).
"""

from __future__ import annotations

import json
import re
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
    ZohoDeploymentSnapshot,
    ZohoInboundTicket,
)
from ..secret_encryption import decrypt_secret, encrypt_secret
from . import jenkins_client
from .jenkins_client import JenkinsConfig, JenkinsError

ACTIVE_STATUSES = (
    "queued",
    "checking_image",
    "building",
    "verifying_image",
    "awaiting_approval",
    "verifying_rollout",
)
TERMINAL_STATUSES = ("deployed", "failed", "cancelled")

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
        "autoRunTickets": bool(row.auto_run_tickets),
        "autoRunClusters": row.auto_run_clusters or {},
        "imageTagTemplate": row.image_tag_template or "{tag}",
        "buildTimeoutMinutes": int(row.build_timeout_minutes or 45),
        "queueTimeoutMinutes": int(row.queue_timeout_minutes or 10),
        "bundleWindowHours": int(row.bundle_window_hours or 24),
        "rolloutTimeoutMinutes": int(row.rollout_timeout_minutes or 15),
        "rollbackOnFailure": bool(row.rollback_on_failure),
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
    """Create a run for one inbound ticket. Raises AutomationError on bad input."""
    ticket = ZohoInboundTicket.query.get(int(ticket_record_id))
    if ticket is None:
        raise AutomationError("Inbound ticket not found.", 404)
    if not ticket.resolved or not ticket.app_service_id:
        raise AutomationError("This ticket did not resolve to a deployment — nothing to run.", 400)
    tag = (ticket.tag or "").strip()
    if not tag:
        raise AutomationError("This ticket has no image tag — automation needs one.", 400)
    snapshot = ZohoDeploymentSnapshot.query.get(ticket.app_service_id)
    if snapshot is None:
        raise AutomationError("The ticket's deployment snapshot no longer exists.", 404)

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
        # Registry/deploy tag = template applied; the router gets the raw tag.
        image_tag=resolve_image_tag(jrow, tag),
        ticket_tag=tag,
        status="queued",
        steps=_initial_steps(),
        auto=bool(auto),
        triggered_by=(getattr(user, "username", None) or ("auto" if auto else None)),
    )
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
            "tag": run.image_tag,
            "ticketTag": run.ticket_tag,
            "auto": bool(auto),
        },
    )

    # Advance immediately so a manual Run gives instant feedback (the first
    # stages are seconds); errors are recorded on the run, never raised.
    try:
        _advance(run, jrow)
        db.session.commit()
    except Exception:  # pragma: no cover — advance already isolates per-step errors
        db.session.rollback()
    return serialize_run(run)


def cancel_run(run_id: int, user=None) -> Dict[str, Any]:
    run = DeployAutomationRun.query.get(int(run_id))
    if run is None:
        raise AutomationError("Automation run not found.", 404)
    if run.status not in ACTIVE_STATUSES:
        raise AutomationError(f"Run is already {run.status} — nothing to cancel.", 409)
    note = "Cancelled by operator."
    if run.status == "building":
        note = "Cancelled by operator — the Jenkins build itself keeps running."
    for step in ("image_check", "build", "verify", "approval", "deploy"):
        current = next((s for s in (run.steps or []) if s.get("key") == step), None)
        if current and current.get("status") in ("run", "wait"):
            _set_step(run, step, "skip", "cancelled")
    run.status = "cancelled"
    run.error = note
    run.finished_at = datetime.now(timezone.utc)
    db.session.commit()
    log_audit(
        "automation_run_cancelled",
        actor=user,
        target_type="deploy_automation_run",
        target_id=str(run.id),
        details={"ticket": run.ticket_number, "deployment": run.deployment_name},
    )
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
            _do_resolve(run)
        elif run.status == "checking_image":
            _do_check(run, jrow)
        elif run.status == "building":
            _do_poll_build(run, jrow)
        elif run.status == "verifying_image":
            _do_verify(run)
        elif run.status == "awaiting_approval":
            _do_watch_bundle(run)
        elif run.status == "verifying_rollout":
            _do_check_pods(run, jrow)
        if run.status == before or run.status in TERMINAL_STATUSES:
            break


def _target_image(run: DeployAutomationRun) -> str:
    return f"{run.image_repo}:{run.image_tag}"


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


def _do_check(run: DeployAutomationRun, jrow: JenkinsConnection) -> None:
    """checking_image → handoff (found) | building (missing) | failed."""
    from .registry_service import check_image

    target = _target_image(run)
    result = check_image(target)
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
        # convention (e.g. v{tag}-prod).
        app = run.deployment_name
        queue_url = jenkins_client.trigger_build(
            _to_client_config(jrow),
            {
                "APP": app,
                "TAG": run.ticket_tag or run.image_tag,
                "NAMESPACE": run.namespace,
            },
        )
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
        _set_step(run, "build", "done", f"router build #{run.jenkins_build_number} succeeded")
        run.status = "verifying_image"
        _set_step(run, "verify", "run", "re-checking the registry for the built tag")
    else:
        _fail(run, "build", f"Router build #{run.jenkins_build_number} finished {outcome}.")


def _do_verify(run: DeployAutomationRun) -> None:
    """verifying_image → handoff | failed. The registry is the source of truth."""
    from .registry_service import check_image

    target = _target_image(run)
    result = check_image(target)
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
        swapped = _swap_image_in_yaml(
            data["yaml"], run.deployment_name, run.container_name or "", _target_image(run)
        )
    except (AutomationError, yaml.YAMLError) as exc:
        _fail(run, "approval", f"Could not prepare the image change: {exc}")
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
        submitted = submit_bundle(
            None,
            bundle.id,
            note=(
                f"Zoho {run.ticket_number or 'ticket'} — {run.deployment_name} → "
                f"{run.image_tag} (automated deploy request)"
            ),
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
            "tag": run.image_tag,
        },
        commit=False,
    )


def _handoff_direct(run: DeployAutomationRun) -> None:
    """No approval required on this cluster → apply the image change immediately."""
    from ..k8s_provider import K8sCommandError, should_use_real_k8s
    from .deployment_service import _run_kubectl_for_cluster

    _set_step(run, "approval", "skip", "cluster requires no approvals")
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
            "image": _target_image(run),
            "mode": "bundle" if run.bundle_id else "direct",
            "pods": pods_detail,
        },
        commit=False,
    )


def _do_check_pods(run: DeployAutomationRun, jrow: JenkinsConnection) -> None:
    """verifying_rollout → deployed once readyReplicas covers the desired count.

    Read errors during a rollout are treated as "still waiting" (pods churn,
    apiservers hiccup) — only the rollout timeout fails the run.
    """
    from ..k8s_provider import (
        K8sCommandError,
        _run_for_access,
        resolve_cluster_access,
        should_use_real_k8s,
    )

    if not should_use_real_k8s(run.cluster_id):
        _complete_deployed(run, "[mock] 1/1 ready")
        return

    started = _aware(run.rollout_started_at) or datetime.now(timezone.utc)
    now = datetime.now(timezone.utc)
    timeout_min = int(jrow.rollout_timeout_minutes or 15)

    detail = ""
    try:
        access = resolve_cluster_access(run.cluster_id)
        if not access:
            raise K8sCommandError(f"Cluster '{run.cluster_id}' was not found.")
        raw = _run_for_access(
            access,
            ["get", "deployment", run.deployment_name, "-n", run.namespace, "-o", "json"],
        )
        spec = json.loads(raw)
        desired = int((spec.get("spec") or {}).get("replicas") or 0)
        status = spec.get("status") or {}
        ready = int(status.get("readyReplicas") or 0)
        updated = int(status.get("updatedReplicas") or 0)

        if desired == 0:
            _complete_deployed(run, "deployment is scaled to 0 — no pods expected")
            return
        if ready >= desired and updated >= desired:
            _complete_deployed(run, f"{ready}/{desired} ready")
            return
        detail = f"{ready}/{desired} ready ({updated}/{desired} on the new image)"
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

    message = (
        f"The image was applied but the pods did not become ready within {timeout_min} min"
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
                "image": _target_image(run),
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
                f"Image:        {_target_image(run)}",
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
        return start_run(ticket_record_id, user=None, auto=True)
    except AutomationError:
        return None
    except Exception:
        db.session.rollback()
        return None
