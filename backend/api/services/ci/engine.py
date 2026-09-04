"""The CI engine.

One pass of :func:`advance_ci_builds` does four things, in this order:

1. **Reap**   — running builds whose runner lost them, or that outlived their
                deadline, are failed rather than left hanging forever.
2. **Cancel** — honour ``cancel_requested`` before starting anything new.
3. **Advance**— poll each running build's current stage and move it on.
4. **Dispatch**— claim queued builds and start them if a runner is free.

Two properties this file is built to hold:

*Restart safety.* Every transition is committed before any work is dispatched,
so the persisted row is always the truth. A backend restart resumes a build from
whatever state it reached; nothing is held only in memory.

*Runner independence.* The engine resolves a :class:`RunnerAdapter` by name and
calls the port. It contains no ``kubectl``, no HTTP, and no branch on runner
type. Adding the Kubernetes Job runner or an external agent changes nothing
here.

The Flask process orchestrates; it never executes a build command itself.
"""

from __future__ import annotations

import logging
import os
import re
import secrets as secrets_module
from datetime import datetime, timedelta, timezone
from hashlib import sha256
from typing import Any, Dict, List, Optional, Tuple

from ...audit import log_audit
from ...db import db
from ...models_ci import CiBuild, CiBuildStage, CiService
from . import artifacts as artifacts_service
from . import logs as logs_service
from . import pipelines as pipelines_service
from . import queue as queue_service
from . import scheduler as scheduler_service
from . import secrets as secrets_service
from . import source as source_port
from .runners import (
    CANCELLED,
    FAILED,
    RUNNING,
    SUCCEEDED,
    TERMINAL_STATUSES,
    TIMEOUT,
    RunnerError,
    RunnerHandle,
    StageExecution,
    get_adapter,
)
from .serializers import build_to_dict, stage_definition

logger = logging.getLogger(__name__)

# How many queued builds one tick will try to start. Keeps a flood of triggers
# from monopolising the shared scheduler thread.
_DISPATCH_PER_TICK = int(os.getenv("CI_DISPATCH_PER_TICK", "5"))
# A running build with no progress for this long is presumed lost.
_STALE_BUILD_MINUTES = int(os.getenv("CI_STALE_BUILD_MINUTES", "60"))

# What a runner can execute is the RUNNER's statement, not the engine's: each
# adapter exposes ``supported_stage_types()`` (the Kubernetes adapter adds
# container_image once BuildKit is configured). A stage of any other type is
# SKIPPED with an explanation rather than dispatched.
#
# This matters more than it looks: a stage dispatched to a runner that cannot
# do its work would run zero commands, exit 0, and report success — a build
# claiming it pushed an image that does not exist. Skipping says the true thing.
_DEFAULT_SUPPORTED_STAGE_TYPES = frozenset({"checkout", "command"})

_STAGE_TYPE_PENDING_REASON = {
    "container_image": "Container image builds arrive with BuildKit.",
    "publish_artifact": "Artifact publishing arrives with the registry integration.",
    "scan": "Security scanning arrives with the scanner integration.",
}

# `backend-service` is the Service name k8s/ingress.yaml ships, so it is what a
# cluster built from this repo actually resolves. (The application-analysis
# modules still default to `kubesight-backend`, which is stale — deployments
# override it via the Hermes ConfigMap.) Override here with CI_WORKER_CALLBACK_URL.
_DEFAULT_CALLBACK_URL = (
    "http://backend-service.kubesight.svc.cluster.local:5000/api/ci/worker"
)


def _callback_url() -> str:
    return os.getenv("CI_WORKER_CALLBACK_URL", _DEFAULT_CALLBACK_URL).strip()


def _sanitize_tag(value: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "-", str(value or "")).strip("-.")
    return cleaned[:100] or "build"


class BuildError(ValueError):
    """A build could not be triggered. Message is user-facing."""


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _aware(value: Optional[datetime]) -> Optional[datetime]:
    if value is None:
        return None
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def _seconds_between(start: Optional[datetime], end: Optional[datetime]) -> Optional[int]:
    start, end = _aware(start), _aware(end)
    if not start or not end:
        return None
    return max(0, int((end - start).total_seconds()))


# ---------------------------------------------------------------------------
# Triggering
# ---------------------------------------------------------------------------

def trigger_build(
    service: CiService,
    *,
    branch: Optional[str] = None,
    commit_sha: Optional[str] = None,
    pipeline_id: Optional[int] = None,
    trigger_type: str = "manual",
    actor=None,
    retry_of: Optional[CiBuild] = None,
    variables: Optional[Dict[str, str]] = None,
    ref_type: Optional[str] = None,
) -> Dict[str, Any]:
    """Create a queued build. Does not execute anything — the tick does that.

    ``variables`` are per-trigger environment overrides applied to every stage
    (and consulted for IMAGE_NAME/IMAGE_TAG on image stages) — how the deploy
    automation pins a build to a ticket's exact tag. They live inside the
    snapshot so retries re-run with the same values.

    ``ref_type`` says what ``branch`` actually names: a branch (default) or a
    git tag — the Jenkins-style "build this release tag" flow. A tag build's
    image is tagged with the git tag itself rather than ``<branch>-<number>``,
    because the whole point of building v1.2.3 is an image called v1.2.3.
    """
    from .catalog import can_run_build

    blocked = can_run_build(service)
    if blocked:
        raise BuildError(blocked)

    pipeline, stages = pipelines_service.resolve_for_build(service, pipeline_id)

    clean_variables = {
        str(key)[:128]: str(value)[:4000]
        for key, value in (variables or {}).items()
        if str(key).strip()
    }

    # Snapshot the pipeline now. Editing it later must not rewrite the history
    # of a build that already ran, and a retry must re-run what actually ran.
    snapshot = {
        "pipelineId": pipeline.id,
        "pipelineName": pipeline.name,
        "pipelineVersion": pipeline.version,
        "variables": clean_variables,
        "refType": ref_type if ref_type in ("branch", "tag") else "branch",
        "stages": [stage_definition(stage) for stage in stages],
    }

    number = int(service.next_build_number or 1)
    service.next_build_number = number + 1
    db.session.add(service)

    raw_token = secrets_module.token_urlsafe(32)
    build = CiBuild(
        service_id=service.id,
        pipeline_id=pipeline.id,
        number=number,
        status="queued",
        trigger_type=trigger_type if trigger_type in
        ("manual", "retry", "api", "webhook", "automation") else "manual",
        branch=(branch or service.default_branch or "main")[:255],
        commit_sha=(commit_sha or None),
        requested_by_user_id=getattr(actor, "id", None),
        retry_of_build_id=retry_of.id if retry_of else None,
        pipeline_snapshot=snapshot,
        queued_at=_now(),
        # Stored hashed, exactly like ApiToken — the plaintext only ever travels
        # to the runner that needs it.
        worker_callback_token_hash=sha256(raw_token.encode("utf-8")).hexdigest(),
    )
    db.session.add(build)
    db.session.flush()

    for index, definition in enumerate(snapshot["stages"]):
        db.session.add(
            CiBuildStage(
                build_id=build.id,
                pipeline_stage_id=definition.get("pipelineStageId"),
                position=index,
                name=definition.get("name") or f"Stage {index + 1}",
                stage_type=definition.get("stageType") or "command",
                status="pending",
            )
        )
    db.session.commit()

    log_audit(
        "ci_build_triggered",
        actor=actor,
        target_type="ci_build",
        target_id=str(build.id),
        details={
            "service": service.slug,
            "buildNumber": build.number,
            "branch": build.branch,
            "refType": snapshot["refType"],
            "pipeline": pipeline.name,
            "trigger": build.trigger_type,
            "retryOf": retry_of.number if retry_of else None,
        },
    )
    return build_to_dict(build)


def cancel_build(build: CiBuild, *, actor=None) -> Dict[str, Any]:
    """Request cancellation.

    A queued build is cancelled immediately — nothing is running to stop. A
    running build is flagged and the next tick tells its runner, because the
    request handler must not block on a runner round-trip.
    """
    if build.status not in ("queued", "running"):
        raise BuildError(f"Build #{build.number} already finished ({build.status}).")

    build.cancel_requested = True
    build.cancel_requested_by_user_id = getattr(actor, "id", None)
    if build.status == "queued":
        _finish_build(build, "cancelled", "Cancelled before it started.")
    db.session.add(build)
    db.session.commit()

    log_audit(
        "ci_build_cancelled",
        actor=actor,
        target_type="ci_build",
        target_id=str(build.id),
        details={
            "service": build.service.slug if build.service else None,
            "buildNumber": build.number,
            "statusAtRequest": build.status,
        },
    )
    return build_to_dict(build)


def retry_build(build: CiBuild, *, actor=None) -> Dict[str, Any]:
    """Queue a new build with the same coordinates as a finished one."""
    if build.status not in ("success", "failed", "cancelled", "timeout"):
        raise BuildError(f"Build #{build.number} is still {build.status}.")
    service = build.service
    if service is None:
        raise BuildError("The service for this build no longer exists.")

    # Coordinates include the trigger's variables and ref kind — a retried
    # tag build must produce the same image tag the original would have.
    old_snapshot = build.pipeline_snapshot or {}
    result = trigger_build(
        service,
        branch=build.branch,
        commit_sha=build.commit_sha,
        pipeline_id=build.pipeline_id,
        trigger_type="retry",
        actor=actor,
        retry_of=build,
        variables=old_snapshot.get("variables") or None,
        ref_type=old_snapshot.get("refType"),
    )
    log_audit(
        "ci_build_retried",
        actor=actor,
        target_type="ci_build",
        target_id=str(result["id"]),
        details={
            "service": service.slug,
            "retryOfBuildNumber": build.number,
            "newBuildNumber": result["number"],
        },
    )
    return result


# ---------------------------------------------------------------------------
# The tick
# ---------------------------------------------------------------------------

def advance_ci_builds() -> None:
    """One scheduler pass. No-ops quickly when nothing is queued or running."""
    active = (
        CiBuild.query.filter(CiBuild.status.in_(("queued", "running"))).count()
    )
    if not active:
        return

    try:
        # Runners KubeSight manages in-process have nothing to heartbeat from;
        # their status is derived (enabled + adapter registered) each pass.
        scheduler_service.sync_builtin_runner_statuses()
        scheduler_service.recompute_loads()
    except Exception:
        logger.exception("CI runner bookkeeping failed")

    for step in (_reap_stale_builds, _process_cancellations, _advance_running, _dispatch_queued):
        try:
            step()
        except Exception:
            logger.exception("CI engine step %s failed", step.__name__)
            db.session.rollback()


def _reap_stale_builds() -> None:
    """Fail builds whose runner is no longer reporting.

    Without this, a build orphaned by a backend restart or a deleted Job stays
    'running' forever and holds a runner slot.
    """
    cutoff = _now() - timedelta(minutes=_STALE_BUILD_MINUTES)
    for build in CiBuild.query.filter(CiBuild.status == "running").all():
        anchor = _aware(build.started_at) or _aware(build.queued_at)
        if anchor and anchor < cutoff:
            _fail_current_stage(
                build,
                f"No progress for {_STALE_BUILD_MINUTES} minutes; the runner stopped reporting.",
                status="timeout",
            )
            _finish_build(build, "timeout", "The build exceeded its overall deadline.")
            db.session.commit()


def _process_cancellations() -> None:
    builds = CiBuild.query.filter(
        CiBuild.cancel_requested.is_(True),
        CiBuild.status.in_(("queued", "running")),
    ).all()
    for build in builds:
        stage = _current_stage(build)
        if stage is not None and stage.status == "running":
            adapter = _adapter_for(build)
            handle = _handle_for(build, stage)
            if adapter and handle:
                try:
                    adapter.cancel(handle)
                    adapter.cleanup(handle)
                except Exception:
                    logger.exception("Cancelling stage %s failed", stage.id)
            _close_stage(stage, "cancelled", "Cancelled by request.")
        for pending in build.stages:
            if pending.status == "pending":
                pending.status = "skipped"
                db.session.add(pending)
        _finish_build(build, "cancelled", "Cancelled by request.")
        db.session.commit()


def _advance_running() -> None:
    for build in CiBuild.query.filter(CiBuild.status == "running").all():
        try:
            _advance_one(build)
        except Exception:
            logger.exception("Advancing build %s failed", build.id)
            db.session.rollback()
            _fail_current_stage(build, "The build engine could not advance this stage.")
            _finish_build(build, "failed", "The build engine could not advance this stage.")
            db.session.commit()


def _advance_one(build: CiBuild) -> None:
    stage = _current_stage(build)
    if stage is None:
        # Every stage reached a terminal state; the build's outcome is whatever
        # the stages said.
        _finalize(build)
        return
    if stage.status == "pending":
        # Skipped stages resolve instantly, so walk past a run of them in this
        # pass instead of burning one scheduler tick each.
        while stage is not None and stage.status == "pending":
            _start_stage(build, stage)
            db.session.commit()
            next_stage = _current_stage(build)
            if next_stage is stage or next_stage is None:
                break
            stage = next_stage
        if _current_stage(build) is None:
            _finalize(build)
            db.session.commit()
        return
    if stage.status != "running":
        return

    adapter = _adapter_for(build)
    handle = _handle_for(build, stage)
    if adapter is None or handle is None:
        _close_stage(stage, "failed", "The runner for this stage is no longer available.")
        _finalize(build)
        db.session.commit()
        return

    _pump_logs(build, stage, adapter, handle)

    definition = _definition_for(build, stage)
    timeout = int(definition.get("timeoutSeconds") or 1800)
    elapsed = _seconds_between(stage.started_at, _now()) or 0
    if elapsed > timeout:
        try:
            adapter.cancel(handle)
        except Exception:
            logger.exception("Timeout cancel failed for stage %s", stage.id)
        status = TIMEOUT
    else:
        try:
            status = adapter.poll(handle)
        except RunnerError as exc:
            logger.warning("Runner poll failed for stage %s: %s", stage.id, exc)
            status = FAILED

    if status not in TERMINAL_STATUSES:
        return

    # One final drain: output flushed as the container exited would otherwise
    # be lost, because the pump above ran before the terminal poll.
    _pump_logs(build, stage, adapter, handle)

    if status == SUCCEEDED:
        _collect_artifacts(build, stage, adapter, handle, definition)
        _close_stage(stage, "success", None)
    elif status == TIMEOUT:
        _close_stage(stage, "timeout", f"Stage exceeded its {timeout}s timeout.")
    elif status == CANCELLED:
        _close_stage(stage, "cancelled", "The runner cancelled this stage.")
    else:
        _close_stage(stage, "failed", "The stage reported failure.")

    try:
        adapter.cleanup(handle)
    except Exception:
        logger.exception("Runner cleanup failed for stage %s", stage.id)

    if stage.status != "success" and not bool(definition.get("continueOnFailure")):
        for pending in build.stages:
            if pending.status == "pending":
                pending.status = "skipped"
                db.session.add(pending)
    db.session.commit()

    if _current_stage(build) is None:
        _finalize(build)
        db.session.commit()


def _dispatch_queued() -> None:
    claimed = queue_service.claim_next(_DISPATCH_PER_TICK)
    if not claimed:
        db.session.rollback()
        return
    for build_id in claimed:
        build = db.session.get(CiBuild, build_id)
        if build is None or build.status != "queued":
            continue
        service = build.service
        if service is None:
            _finish_build(build, "failed", "The service for this build no longer exists.")
            db.session.commit()
            continue

        # Per-service concurrency, enforced before a runner slot is taken.
        running = queue_service.running_count(service.id)
        if running >= max(1, int(service.max_concurrent_builds or 1)):
            queue_service.requeue(
                build,
                f"Waiting: {service.name} allows {service.max_concurrent_builds} "
                f"concurrent build(s).",
            )
            continue

        stage = _current_stage(build)
        if stage is None:
            _finish_build(build, "failed", "This build has no stages to run.")
            db.session.commit()
            continue

        definition = _definition_for(build, stage)
        selection = scheduler_service.select_runner(
            scheduler_service.requirements_for(definition)
        )
        if not selection.ok:
            queue_service.requeue(build, selection.reason)
            continue

        build.runner_id = selection.runner.id
        build.status = "running"
        build.started_at = _now()
        build.queue_reason = None
        build.workspace_ref = f"{service.slug}-{build.number}"
        scheduler_service.acquire_slot(selection.runner)
        db.session.add(build)
        db.session.commit()

        _start_stage(build, stage)
        db.session.commit()


# ---------------------------------------------------------------------------
# Stage lifecycle
# ---------------------------------------------------------------------------

def _supported_stage_types(adapter) -> set:
    getter = getattr(adapter, "supported_stage_types", None)
    if callable(getter):
        return set(getter())
    return set(_DEFAULT_SUPPORTED_STAGE_TYPES)


def _skip_reason(build: CiBuild, adapter, definition: Dict[str, Any]) -> Optional[str]:
    """Why this stage must be skipped rather than dispatched — or None to run.

    Used identically when starting a stage and when composing a whole-build
    plan, so a runner that builds everything up front (one Kubernetes Job per
    build) contains exactly the containers the engine will actually advance.
    """
    stage_type = definition.get("stageType") or "command"
    if stage_type not in _supported_stage_types(adapter):
        reason = None
        asker = getattr(adapter, "skip_reason", None)
        if callable(asker):
            reason = asker(stage_type)
        return reason or _STAGE_TYPE_PENDING_REASON.get(
            stage_type, f"Stage type '{stage_type}' has no executor yet."
        )
    if stage_type == "container_image":
        _, reason = _registry_for(build, definition)
        if reason:
            return reason
    return None


def _start_stage(build: CiBuild, stage: CiBuildStage) -> None:
    definition = _definition_for(build, stage)
    stage_type = definition.get("stageType") or stage.stage_type

    adapter = _adapter_for(build)
    if adapter is None:
        _close_stage(stage, "failed", "No adapter is registered for the assigned runner.")
        _finalize(build)
        return

    skip = _skip_reason(build, adapter, definition)
    if skip:
        # Never dispatch a stage whose work cannot actually be done — an
        # unexecuted stage reporting success is worse than an honest skip.
        stage.started_at = _now()
        _close_stage(stage, "skipped", None)
        logs_service.append_system(
            stage,
            f"[kubesight] Skipped: this stage is a '{stage_type}' stage. {skip} "
            "Nothing was built, and no artifact was recorded.",
        )
        return

    # The first stage that actually starts carries the whole resolved plan and
    # a fresh callback token: whole-build runners create everything from it.
    needs_plan = not any(s.external_ref for s in build.stages)
    callback_token = _fresh_callback_token(build) if needs_plan else ""

    try:
        execution = _build_execution(
            build, stage, definition, callback_token=callback_token
        )
        if needs_plan:
            execution.plan = _build_plan(build, adapter, callback_token)
    except Exception as exc:
        logger.exception("Preparing stage %s failed", stage.id)
        _close_stage(stage, "failed", _safe_message(exc))
        _finalize(build)
        return

    stage.status = "running"
    stage.started_at = _now()
    stage.runner_id = build.runner_id
    db.session.add(stage)
    db.session.flush()

    try:
        handle = adapter.start(execution)
    except RunnerError as exc:
        _close_stage(stage, "failed", _safe_message(exc))
        _finalize(build)
        return
    except Exception as exc:
        logger.exception("Starting stage %s failed", stage.id)
        _close_stage(stage, "failed", _safe_message(exc))
        _finalize(build)
        return

    stage.external_ref = handle.external_ref
    db.session.add(stage)

    if execution.secrets:
        secrets_service.mark_used(build.service_id, list(execution.secrets))


def _close_stage(stage: CiBuildStage, status: str, error: Optional[str]) -> None:
    stage.status = status
    stage.finished_at = _now()
    stage.duration_seconds = _seconds_between(stage.started_at, stage.finished_at)
    if error:
        stage.error = error[:2000]
    db.session.add(stage)


def _fail_current_stage(build: CiBuild, message: str, status: str = "failed") -> None:
    stage = _current_stage(build)
    if stage is not None and stage.status in ("pending", "running"):
        _close_stage(stage, status, message)
    for pending in build.stages:
        if pending.status == "pending":
            pending.status = "skipped"
            db.session.add(pending)


def _finalize(build: CiBuild) -> None:
    """Decide the build's outcome from its stages.

    A stage that failed with ``continueOnFailure`` still fails the build — the
    flag means "keep going and collect more information", not "pretend it
    passed". Anything else would let a red build report green.
    """
    statuses = [stage.status for stage in build.stages]
    if any(status in ("pending", "running") for status in statuses):
        return
    if "cancelled" in statuses:
        _finish_build(build, "cancelled", "A stage was cancelled.")
    elif "timeout" in statuses:
        _finish_build(build, "timeout", "A stage exceeded its timeout.")
    elif "failed" in statuses:
        failed = next(s for s in build.stages if s.status == "failed")
        _finish_build(build, "failed", f"Stage '{failed.name}' failed.")
    else:
        _finish_build(build, "success", None)


def _finish_build(build: CiBuild, status: str, error: Optional[str]) -> None:
    build.status = status
    build.finished_at = _now()
    build.duration_seconds = _seconds_between(
        build.started_at or build.queued_at, build.finished_at
    )
    build.queue_reason = None
    if error and status != "success":
        build.error = error[:2000]
    scheduler_service.release_slot(build.runner_id)
    db.session.add(build)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _current_stage(build: CiBuild) -> Optional[CiBuildStage]:
    """The first stage not yet in a terminal state."""
    for stage in sorted(build.stages, key=lambda s: s.position):
        if stage.status in ("pending", "running"):
            return stage
    return None


def _definition_for(build: CiBuild, stage: CiBuildStage) -> Dict[str, Any]:
    """The snapshotted definition for a stage, matched by position.

    Position rather than id: the pipeline stage may have been deleted since,
    and the snapshot is the authority for what this build runs.
    """
    stages = (build.pipeline_snapshot or {}).get("stages") or []
    if 0 <= stage.position < len(stages):
        return stages[stage.position] or {}
    return {}


def _adapter_for(build: CiBuild):
    runner = build.runner
    if runner is None:
        return None
    return get_adapter(runner.runner_type)


def _handle_for(build: CiBuild, stage: CiBuildStage) -> Optional[RunnerHandle]:
    if not stage.external_ref:
        return None
    return RunnerHandle(
        runner_id=build.runner_id or 0, external_ref=stage.external_ref, metadata={}
    )


def _fresh_callback_token(build: CiBuild) -> str:
    """Mint the token the in-cluster job will present on its callbacks.

    Only the hash persists; the plaintext travels once, inside the runner's
    per-build Secret. Re-minted on every (re)start so a token can never outlive
    the dispatch that issued it.
    """
    raw = secrets_module.token_urlsafe(32)
    build.worker_callback_token_hash = sha256(raw.encode("utf-8")).hexdigest()
    db.session.add(build)
    return raw


def _registry_for(
    build: CiBuild, definition: Dict[str, Any]
) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    """Resolve the push target for a container_image stage.

    Returns ``(registry, None)`` or ``(None, user-facing reason to skip)``.
    Stage env keys ``IMAGE_NAME`` / ``IMAGE_TAG`` / ``DOCKERFILE_PATH`` override
    the defaults (service slug / ``<branch>-<number>`` / ``Dockerfile``).
    """
    from urllib.parse import urlsplit

    from ...models import RegistryConnection
    from ...secret_encryption import decrypt_secret
    from .. import registry_client

    service = build.service
    if not service or not service.registry_connection_id:
        return None, (
            "This service has no linked registry connection to push images to. "
            "Link one on the service settings, then retry."
        )
    row = db.session.get(RegistryConnection, service.registry_connection_id)
    if row is None or not row.enabled:
        return None, "The linked registry connection is disabled or was removed."

    # Per-trigger variables outrank the stage's own env: an automation-pinned
    # IMAGE_TAG must beat whatever the pipeline author hardcoded.
    snapshot = build.pipeline_snapshot or {}
    env = {**(definition.get("env") or {}), **snapshot.get("variables", {})}
    host = registry_client.registry_host_of(row.base_url)
    # A tag build's image carries the git tag verbatim; branch builds get the
    # disambiguating build number because branch heads move.
    default_tag = (
        _sanitize_tag(build.branch)
        if snapshot.get("refType") == "tag"
        else f"{_sanitize_tag(build.branch)}-{build.number}"
    )
    return (
        {
            "host": host,
            "port": urlsplit(row.base_url).port,
            "repository": _sanitize_tag(env.get("IMAGE_NAME") or service.slug).lower(),
            "tag": _sanitize_tag(env.get("IMAGE_TAG") or default_tag),
            "dockerfile": env.get("DOCKERFILE_PATH") or "Dockerfile",
            # An inline Dockerfile replaces the one in the checkout. The runner
            # mounts it beside the context rather than writing into the
            # workspace, so the repository is never modified by building it.
            "dockerfileContent": service.dockerfile or "",
            "username": row.username or "",
            "password": decrypt_secret(row.password_encrypted or ""),
            "verifyTls": bool(row.verify_tls),
            "connectionId": row.id,
        },
        None,
    )


def _build_execution(
    build: CiBuild,
    stage: CiBuildStage,
    definition: Dict[str, Any],
    *,
    callback_token: str = "",
) -> StageExecution:
    """Resolve everything a runner needs, including decrypted secrets.

    The returned object is passed to the adapter and discarded. Its ``secrets``
    are never persisted; the same values are handed to the log masker so they
    cannot surface in output.
    """
    service = build.service
    resolved = secrets_service.resolve_for_service(service.id)
    stage_secrets = secrets_service.env_for_stage(definition, resolved)
    stage_type = definition.get("stageType") or stage.stage_type

    env = dict(definition.get("env") or {})
    # Per-trigger variables override stage env; KUBESIGHT_* identity wins last.
    snapshot = build.pipeline_snapshot or {}
    env.update(snapshot.get("variables") or {})
    ref_type = snapshot.get("refType") or "branch"
    env.update(
        {
            "KUBESIGHT_BUILD_ID": str(build.id),
            "KUBESIGHT_BUILD_NUMBER": str(build.number),
            "KUBESIGHT_SERVICE": service.slug,
            "KUBESIGHT_BRANCH": build.branch or "",
            "KUBESIGHT_COMMIT": build.commit_sha or "",
            # What the build was asked to check out: branch or tag. Tag builds
            # also expose the tag itself so scripts can version artifacts.
            "KUBESIGHT_REF_TYPE": ref_type,
            "KUBESIGHT_TAG": (build.branch or "") if ref_type == "tag" else "",
        }
    )

    working_directory = definition.get("workingDirectory") or service.working_directory

    if stage_type == "checkout" and service.source_ready():
        handler = source_port.get_provider(service.repository_provider)
        ref = handler.parse_repository_url(service.repository_url)
        spec = handler.checkout_spec(
            ref,
            service.credential_profile,
            build.commit_sha or build.branch or service.default_branch,
            service.working_directory,
        )
        # Clone credentials join the secret set so the masker covers them too.
        stage_secrets = {**stage_secrets, **spec.credential_env}
        working_directory = definition.get("workingDirectory") or spec.working_directory

    registry = None
    if stage_type == "container_image":
        registry, _ = _registry_for(build, definition)

    return StageExecution(
        build_id=build.id,
        build_number=build.number,
        stage_id=stage.id,
        service_slug=service.slug,
        stage_name=stage.name,
        stage_type=stage_type,
        image=definition.get("image"),
        working_directory=working_directory,
        commands=list(definition.get("commands") or []),
        env=env,
        secrets=stage_secrets,
        resources=definition.get("resources") or {},
        artifacts=list(definition.get("artifacts") or []),
        # Absent from snapshots taken before host aliases existed — a build
        # retried from such a snapshot simply gets none.
        host_aliases=list(definition.get("hostAliases") or []),
        timeout_seconds=int(definition.get("timeoutSeconds") or 1800),
        continue_on_failure=bool(definition.get("continueOnFailure")),
        position=stage.position,
        workspace_ref=build.workspace_ref or f"{service.slug}-{build.number}",
        repository_url=service.repository_url,
        branch=build.branch,
        commit_sha=build.commit_sha,
        registry=registry,
        callback_url=_callback_url(),
        callback_token=callback_token,
    )


def _build_plan(build: CiBuild, adapter, callback_token: str) -> List[StageExecution]:
    """Every stage the adapter will actually run, fully resolved, in order.

    Skipped-by-policy stages are filtered with the SAME predicate the engine
    applies when it reaches them, so a whole-build runner's Job contains
    exactly the containers the engine will advance through.
    """
    plan: List[StageExecution] = []
    for stage_row in sorted(build.stages, key=lambda s: s.position):
        definition = _definition_for(build, stage_row)
        if _skip_reason(build, adapter, definition):
            continue
        plan.append(
            _build_execution(build, stage_row, definition, callback_token=callback_token)
        )
    return plan


def _mask_values(build: CiBuild) -> List[str]:
    """Every secret that could surface in this build's output.

    Service/global CI secrets, the git clone token, and the registry password —
    the last two are not ``ci_secrets`` rows, so relying on those alone would
    let a stray ``set -x`` print them.
    """
    from ...secret_encryption import decrypt_secret

    values: List[str] = []
    try:
        values.extend(secrets_service.resolve_for_service(build.service_id).values())
    except Exception:
        pass
    service = build.service
    if service is not None and service.credential_profile is not None:
        token = decrypt_secret(service.credential_profile.secret_cipher or "")
        if token:
            values.append(token)
    if service is not None and service.registry_connection_id:
        from ...models import RegistryConnection

        row = db.session.get(RegistryConnection, service.registry_connection_id)
        if row is not None:
            password = decrypt_secret(row.password_encrypted or "")
            if password:
                values.append(password)
    return values


def _pump_logs(build: CiBuild, stage: CiBuildStage, adapter, handle: RunnerHandle) -> None:
    """Drain new output into ``ci_log_chunks``, masked on the way in."""
    mask = logs_service.build_masker(_mask_values(build))
    after = logs_service.highest_seq(stage.id)
    try:
        chunks = list(adapter.drain_logs(handle, after))
    except Exception:
        logger.exception("Draining logs for stage %s failed", stage.id)
        return
    if chunks:
        logs_service.append(
            stage,
            [(chunk.seq, chunk.content, chunk.stream) for chunk in chunks],
            mask=mask,
        )


def _collect_artifacts(
    build: CiBuild,
    stage: CiBuildStage,
    adapter,
    handle: RunnerHandle,
    definition: Dict[str, Any],
) -> None:
    if not definition.get("artifacts"):
        return
    try:
        refs = adapter.collect_artifacts(handle)
    except Exception:
        logger.exception("Collecting artifacts for stage %s failed", stage.id)
        logs_service.append_system(
            stage, "[kubesight] artifact collection failed; see server logs"
        )
        return
    for ref in refs:
        try:
            artifacts_service.record_artifact(
                service_id=build.service_id,
                build_id=build.id,
                build_stage_id=stage.id,
                ref=ref,
                commit_sha=build.commit_sha,
                branch=build.branch,
                version=str(build.number),
                registry_connection_id=(
                    build.service.registry_connection_id if build.service else None
                ),
            )
        except Exception:
            logger.exception("Recording artifact %s failed", ref.name)


def _safe_message(exc: Exception) -> str:
    """A failure message safe to show a user.

    Runner errors are authored for humans; anything else is summarised, because
    an arbitrary exception's text can carry paths or connection strings.
    """
    if isinstance(exc, (RunnerError, ValueError)):
        return str(exc)[:500]
    return "The stage could not be started. See the server log for details."


# ---------------------------------------------------------------------------
# Reads used by the API
# ---------------------------------------------------------------------------

def get_build(build_id: int) -> CiBuild:
    row = db.session.get(CiBuild, int(build_id))
    if row is None:
        raise LookupError("Build not found.")
    return row


def list_builds(
    service_id: Optional[int] = None,
    status: Optional[str] = None,
    limit: int = 50,
    offset: int = 0,
) -> Tuple[List[CiBuild], int]:
    query = CiBuild.query
    if service_id is not None:
        query = query.filter(CiBuild.service_id == service_id)
    if status and status != "all":
        query = query.filter(CiBuild.status == status)
    total = query.count()
    rows = (
        query.order_by(CiBuild.id.desc())
        .limit(max(1, min(int(limit), 200)))
        .offset(max(0, int(offset)))
        .all()
    )
    return rows, total


def get_build_stage(build: CiBuild, stage_id: int) -> CiBuildStage:
    for stage in build.stages:
        if stage.id == int(stage_id):
            return stage
    raise LookupError("Build stage not found.")
