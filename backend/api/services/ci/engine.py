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

The Flask process orchestrates; it never executes a build command itself. The
one kind of stage it carries out is a Deploy stage — not a build command but a
KubeSight deploy, through the same approval gate and registry check as any other
(see ``deploy_stage.py``). Those come last, after the runner is done.

Stages advance in STEPS. A step is one stage, or a parallel group: consecutive
stages sharing a ``parallelGroup`` (see ``parallel_groups.py``). A group's
members start together, are polled together, and the build moves on only once
every one of them is terminal. Failure follows Jenkins' ``parallel``: siblings
of a failed member run to completion and the group fails afterwards, unless the
group is fail-fast, which stops them. Where the runner cannot run members side
by side the group runs one member at a time with the same failure semantics, and
the build records why (``pipeline_snapshot["parallel"]``).
"""

from __future__ import annotations

import logging
import os
import threading
import time
import re
import secrets as secrets_module
from datetime import datetime, timedelta, timezone
from hashlib import sha256
from typing import Any, Dict, List, Optional, Tuple

from ...audit import log_audit
from ...db import db
from ...models_ci import SERVER_STAGE_TYPES, TRIGGER_TYPES, CiBuild, CiBuildStage, CiService
from . import agents as agents_service
from . import artifacts as artifacts_service
from . import build_inputs
from . import build_status as build_status_service
from . import code_scan
from . import deploy_stage
from . import deployment_links
from . import logs as logs_service
from . import parallel_groups
from . import pipelines as pipelines_service
from . import post_actions
from . import queue as queue_service
from . import resources as ci_resources
from . import scan_stage
from . import scheduler as scheduler_service
from . import server_stages
from . import secrets as secrets_service
from . import source as source_port
from .runners import (
    CANCELLED,
    FAILED,
    RUNNING,
    SKIPPED,
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
# A running build with no PROGRESS for this long is presumed lost. Progress is
# the latest of: a stage starting or ending, a log line arriving, and — for an
# external agent — the agent heartbeating. A long build that keeps doing things
# is never reaped by this; one whose runner went silent is.
_STALE_BUILD_MINUTES = int(os.getenv("CI_STALE_BUILD_MINUTES", "60"))
# The overall deadline is the sum of the snapshot's stage timeouts plus this
# grace (dispatch, image pulls, artifact upload), never more than the hard cap.
# The cap exists for a snapshot whose timeouts add up to something absurd.
_BUILD_DEADLINE_GRACE_MINUTES = int(os.getenv("CI_BUILD_DEADLINE_GRACE_MINUTES", "30"))
_BUILD_HARD_CAP_MINUTES = int(os.getenv("CI_BUILD_HARD_CAP_MINUTES", str(24 * 60)))
_DEFAULT_STAGE_TIMEOUT_SECONDS = 1800

# One pass at a time in this process. The dedicated CI ticker and the shared
# scheduler can both ask, and a pass that overlapped its own previous run would
# poll the same stage twice and race on the transition it is in the middle of.
_pass_lock = threading.Lock()

# Runner housekeeping (derive builtin statuses, offline the silent agents,
# rebuild leaked load counters) is self-healing rather than latency-critical, so
# it keeps its own slower cadence instead of running on every fast pass.
_BOOKKEEPING_SECONDS = float(os.getenv("CI_BOOKKEEPING_SECONDS", "5"))
_last_bookkeeping = 0.0

# What a runner can execute is the RUNNER's statement, not the engine's: each
# adapter exposes ``supported_stage_types()`` (the Kubernetes adapter adds
# container_image once BuildKit is configured). A stage of any other type is
# SKIPPED with an explanation rather than dispatched.
#
# This matters more than it looks: a stage dispatched to a runner that cannot
# do its work would run zero commands, exit 0, and report success — a build
# claiming it pushed an image that does not exist. Skipping says the true thing.
_DEFAULT_SUPPORTED_STAGE_TYPES = frozenset({"checkout", "command"})

# Stage types no runner executes. They are no longer accepted on save (see
# pipelines.RETIRED_STAGE_TYPES), but snapshots and pipelines stored before that
# still carry them, and they must keep loading — and keep being skipped.
_NEVER_EXECUTED_STAGE_TYPES = frozenset({"publish_artifact"})

# A scan stage saved while the kind was retired names no scanner. It still
# loads, and is skipped with this — never run with a guessed tool.
_UNCONFIGURED_SCAN_REASON = (
    "This scan stage was saved before scan stages could run, so it names no scanner. "
    "Open it in the pipeline editor, choose Trivy, Semgrep, Dependency-Check or Syft, "
    "and save — or remove it."
)


def _never_executed(definition: Dict[str, Any]) -> bool:
    stage_type = definition.get("stageType") or "command"
    if stage_type in _NEVER_EXECUTED_STAGE_TYPES:
        return True
    return stage_type == "scan" and not scan_stage.configured(definition.get("scan"))

_STAGE_TYPE_PENDING_REASON = {
    "container_image": "Container image builds arrive with BuildKit.",
    "publish_artifact": (
        "Publish-artifact stages have no executor. Declare the files as "
        "artifacts on the stage that produces them, and remove this stage."
    ),
    "scan": (
        "Scan stages run on the Kubernetes runner, and this build was given a runner "
        "that does not run them. Pin the pipeline to the Kubernetes runner to scan."
    ),
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


def _sanitize_repository(value: str) -> str:
    """An image repository name: like a tag, but ``/`` separates its parts.

    ``areeba/issuing-ms`` stays ``areeba/issuing-ms`` — tidying it as a tag made
    it ``areeba-issuing-ms``, a repository no deployment pulls from. Each part is
    tidied on its own (lowercase, no leading/trailing separator, no ``..``) and
    empty parts are dropped, so ``//a//b/`` is ``a/b``.
    """
    parts = []
    for part in str(value or "").lower().split("/"):
        part = re.sub(r"[^a-z0-9._-]+", "-", part)
        part = re.sub(r"\.{2,}", ".", part).strip("-._")
        if part:
            parts.append(part)
    return "/".join(parts)[:255].strip("/") or "build"


# A tag the build's own shell finishes, e.g. ``V${VERSION}-${KUBESIGHT_BUILD_NUMBER}``
# where VERSION was exported by an earlier stage into $KUBESIGHT_ENV. Validated
# on save (pipelines._check_image_tag_template) and re-checked here, because a
# snapshot can outlive the validation that produced it.
_TAG_TEMPLATE_RE = re.compile(r"^[A-Za-z0-9._${}-]{1,255}$")


def _is_tag_template(value: str) -> bool:
    return "$" in str(value or "") and bool(_TAG_TEMPLATE_RE.match(str(value)))


class BuildError(ValueError):
    """A build could not be triggered. Message is user-facing."""


_IMAGE_VARIABLE_RE = re.compile(
    r"\$(?:\{(?P<braced>[A-Za-z_][A-Za-z0-9_]*)\}|(?P<plain>[A-Za-z_][A-Za-z0-9_]*))"
)


def _resolve_stage_image(
    image: Any, env: Dict[str, Any], stage_name: str
) -> Optional[str]:
    """Expand build variables in a stage image without invoking a shell.

    Jenkins commonly uses images such as
    ``registry.example/gradle:${GradleVersion}-jdk11``. Kubernetes does not
    expand that syntax itself, so handing it the template verbatim leaves the
    pod in ``InvalidImageName`` before the container can produce any logs.
    """
    template = str(image or "").strip()
    if not template:
        return None

    resolved = _expand_build_variables(template, env, stage_name, "its container image")
    # A remaining dollar sign means the template used unsupported syntax such
    # as ${params.NAME}; pass a useful error instead of Kubernetes' opaque
    # InvalidImageName event. Whitespace is likewise never valid in an image.
    if "$" in resolved or any(char.isspace() for char in resolved):
        raise BuildError(
            f"Stage '{stage_name}' resolved to an invalid container image "
            f"'{resolved}'. Use ${{VARIABLE}} or $VARIABLE with a build input."
        )
    return resolved


def _expand_build_variables(
    template: str, env: Dict[str, Any], stage_name: str, what: str
) -> str:
    """``${NAME}`` / ``$NAME`` replaced by build variables, server side.

    Raises when a referenced variable is empty or missing: an empty module name
    would build the repository root and push it under a name nobody asked for.
    """
    missing: List[str] = []

    def replace(match: re.Match) -> str:
        name = match.group("braced") or match.group("plain")
        value = str(env.get(name, "")).strip()
        if not value:
            missing.append(name)
        return value

    resolved = _IMAGE_VARIABLE_RE.sub(replace, template)
    if missing:
        names = ", ".join(sorted(set(missing)))
        raise BuildError(
            f"Stage '{stage_name}' cannot resolve {what} because "
            f"build input {names} is empty or missing. Set it in Run build or "
            "give the input a default value."
        )
    return resolved


# A working directory filled from a build input reaches the runner's shell, and
# the input is whatever the person running the build typed: held to a plain path
# alphabet, not just "relative, no ..".
_EXPANDED_PATH_RE = re.compile(r"^[A-Za-z0-9._/-]{1,512}$")


def _resolve_working_directory(
    value: Any, env: Dict[str, Any], stage_name: str
) -> Optional[str]:
    """A stage's working directory with build inputs expanded, e.g.
    ``modules/${MODULE}`` -> ``modules/ds-amex``. Literal paths pass through."""
    template = str(value or "").strip()
    if not template or "$" not in template:
        return template or None
    resolved = _expand_build_variables(template, env, stage_name, "its working directory")
    resolved = "/".join(part for part in resolved.strip().split("/") if part)
    problem = build_inputs.working_directory_problem(resolved)
    if not problem and (not resolved or not _EXPANDED_PATH_RE.match(resolved)):
        problem = (
            "A working directory filled from a build input may use letters, "
            "digits, '.', '_', '-' and '/' only."
        )
    if problem:
        raise BuildError(f"Stage '{stage_name}' resolved its working directory to '{resolved}': {problem}")
    return resolved


def _resolve_image_name(registry: Dict[str, Any], env: Dict[str, Any], stage_name: str) -> None:
    """Finish a templated IMAGE_NAME (``${MODULE}``) in place, from build inputs.

    Unlike a tag, every input exists before the stage starts, so the name is
    final here — the log, the push, the build record and a Deploy stage all see
    the same repository."""
    template = registry.pop("repositoryTemplate", None)
    if not template:
        return
    resolved = _sanitize_repository(
        _expand_build_variables(template, env, stage_name, "its image name")
    )
    problem = build_inputs.image_name_problem(resolved)
    if problem:
        raise BuildError(f"Stage '{stage_name}' resolved its image name to '{resolved}': {problem}")
    registry["repository"] = resolved


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


def _wake_engine() -> None:
    """Ask the CI ticker for a pass now rather than at its next interval.

    Imported at the call site: the ticker's loop imports this module, and the
    dependency has to run in that direction only.
    """
    try:
        from .ticker import wake

        wake()
    except Exception:  # pragma: no cover - a missed wake only costs a tick
        logger.debug("Could not wake the CI engine", exc_info=True)


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
    schedule: Optional[Dict[str, Any]] = None,
    webhook: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Create a queued build. Does not execute anything — the tick does that.

    ``schedule`` is the provenance of a scheduled build (``{"id", "name"}``),
    kept in the snapshot so the build still says which schedule queued it after
    that schedule is renamed or deleted. ``webhook`` is the same for a build a
    webhook trigger queued (``{"id", "name", "kind", "event", ...}``).

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

    blocked = can_run_build(service, pipeline_id=pipeline_id)
    if blocked:
        raise BuildError(blocked)

    selected_ref = commit_sha or branch or service.default_branch or "main"
    pipeline, stages = pipelines_service.resolve_for_build(
        service, pipeline_id, revision=selected_ref
    )

    # Values are checked against the pipeline's own parameter definitions, and a
    # bad one is refused rather than dropped: a build that quietly ignored a
    # parameter would run differently from what was asked for and say nothing.
    # A pipeline that declares no parameters still accepts free-form variables,
    # which is how the deploy automation pins a tag.
    clean_variables = pipelines_service.validate_parameter_values(pipeline, variables)

    # Snapshot the pipeline now. Editing it later must not rewrite the history
    # of a build that already ran, and a retry must re-run what actually ran.
    snapshot = {
        "pipelineId": pipeline.id,
        "pipelineName": pipeline.name,
        "pipelineVersion": pipeline.version,
        "pipelineSource": (
            "kubesight_default"
            if getattr(pipeline, "generated_default", False)
            else "shared"
            if getattr(pipeline, "shared", None)
            else "configured"
        ),
        "variables": clean_variables,
        # The definitions as they stood, so a build still shows what it was
        # asked after the pipeline changes underneath it.
        "parameters": pipelines_service.parameter_definitions(pipeline),
        "refType": ref_type if ref_type in ("branch", "tag") else "branch",
        "stages": [stage_definition(stage) for stage in stages],
    }
    shared = getattr(pipeline, "shared", None)
    if shared:
        # Which shared pipeline (Pipelines page) ran, at which version. Its home
        # also widens secret resolution for this build (secrets.resolve_for_build).
        snapshot["sharedPipeline"] = dict(shared)
    # "Deploy to the service's linked deployment" stages get a concrete target
    # now, so the build records where it deployed even if links change later.
    deployment_links.resolve_snapshot_targets(service, snapshot["stages"])
    if schedule:
        snapshot["schedule"] = dict(schedule)
    if webhook:
        snapshot["webhook"] = dict(webhook)
    # What happens when the build ends (post_actions.py) — snapshotted with the
    # stages for the same reason: editing the pipeline must not change what a
    # build already running will send or clean up.
    snapshot["postActions"] = post_actions.of_pipeline(pipeline)

    number = int(service.next_build_number or 1)
    service.next_build_number = number + 1
    db.session.add(service)

    raw_token = secrets_module.token_urlsafe(32)
    build = CiBuild(
        service_id=service.id,
        pipeline_id=pipeline.id,
        number=number,
        status="queued",
        trigger_type=trigger_type if trigger_type in TRIGGER_TYPES else "manual",
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
    post_actions.create_rows(build, snapshot["postActions"])
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
            **({"sharedPipeline": shared.get("slug")} if shared else {}),
            "trigger": build.trigger_type,
            "retryOf": retry_of.number if retry_of else None,
            **({"schedule": schedule.get("name")} if schedule else {}),
            **({"webhook": webhook.get("name")} if webhook else {}),
        },
    )
    # Dispatch this build now instead of at the ticker's next interval: the
    # gap between clicking Run build and a runner picking it up is the first
    # thing anyone judges CI on.
    _wake_engine()
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
    # A running build is stopped by the engine, not here; ask it to look now.
    _wake_engine()
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

def advance_ci_builds() -> bool:
    """One scheduler pass. No-ops quickly when nothing is queued or running.

    Returns whether there was active work, which is how the ticker decides
    between its fast and idle intervals.
    """
    # First, before the "anything to do?" count: a schedule coming due is how
    # an idle installation gets work, and the build it queues should be
    # dispatched by this same pass rather than the next one.
    _fire_due_schedules()
    active = (
        CiBuild.query.filter(CiBuild.status.in_(("queued", "running"))).count()
    )
    if not active:
        # Merge checks settle and DELIVER on this same clock, and a verdict
        # waiting to be handed to Bitbucket outlives the build that produced it
        # — a retry after a backoff is work with no running build behind it. So
        # the early return asks about that too, or a delivery that failed once
        # would wait for whatever happens to build next. Post-action
        # notifications are the same kind of work (post_actions.py).
        return (_settle_merge_checks() + _deliver_post_actions()) > 0

    # A pass already in flight is doing exactly this work; a second one would
    # poll the same stage and race on the transition the first is committing.
    if not _pass_lock.acquire(blocking=False):
        return True
    try:
        return _run_pass()
    finally:
        _pass_lock.release()


def _run_pass() -> bool:
    global _last_bookkeeping
    now = time.monotonic()
    # Always while something is queued: "no runner is online" must never be a
    # stale answer for a build that is waiting on exactly that.
    if now - _last_bookkeeping >= _BOOKKEEPING_SECONDS or queue_service.depth():
        _last_bookkeeping = now
        try:
            # Runners KubeSight manages in-process have nothing to heartbeat
            # from; their status is derived (enabled + adapter registered).
            scheduler_service.sync_builtin_runner_statuses()
            # An agent that stops heartbeating is offline, not
            # online-and-silent. Without this the scheduler keeps assigning
            # work to a machine that has been switched off, and those builds
            # queue against nothing.
            agents_service.mark_stale_agents_offline()
            scheduler_service.recompute_loads()
        except Exception:
            logger.exception("CI runner bookkeeping failed")

    for step in (
        _reap_stale_builds,
        _process_cancellations,
        _advance_running,
        _dispatch_queued,
        _settle_merge_checks,
        _deliver_post_actions,
    ):
        try:
            step()
        except Exception:
            logger.exception("CI engine step %s failed", step.__name__)
            db.session.rollback()
    return True


def _fire_due_schedules() -> int:
    """Queue the builds of schedules that have come due.

    Imported at the call site for the same reason as merge checks: schedules
    trigger ordinary builds through this module. Never raises — a schedule
    that cannot fire records why on its own row, and must not stop builds
    that are already running from advancing.
    """
    try:
        from .schedules import fire_due_schedules

        return fire_due_schedules()
    except Exception:
        logger.exception("Firing due CI schedules failed")
        db.session.rollback()
        return 0


def _settle_merge_checks() -> int:
    """Judge and deliver merge check verdicts, as one step of the CI pass.

    Imported here rather than at module scope: merge checks are built ON the
    engine (they trigger ordinary builds through it), so a module-level import
    would be a cycle. The engine knowing one function name is the whole of the
    coupling in this direction.

    Never raises. A source host that is down must not stop builds advancing.
    """
    try:
        from .merge_checks import pending_work, settle

        if not pending_work():
            return 0
        return settle()
    except Exception:
        logger.exception("Merge check settlement failed")
        db.session.rollback()
        return 0


def _deliver_post_actions() -> int:
    """Claim the post-action notifications that are due and hand them to the
    senders (post_actions.py). Claiming is a quick conditional UPDATE; the
    sending itself happens off this thread, so a slow SMTP relay or webhook
    never holds up a pass. Never raises."""
    try:
        if not post_actions.pending_work():
            return 0
        return post_actions.deliver_due()
    except Exception:
        logger.exception("Post-action delivery failed")
        db.session.rollback()
        return 0


def advance_build_now(build_id: int) -> None:
    """Advance one build immediately, outside the tick.

    Called from the callbacks that already know a stage just ended — an agent
    posting its exit code, above all. Doing the transition here means the
    successor stage is queued before that request returns, so the agent's very
    next claim picks it up instead of waiting for a tick to notice.

    Deliberately forgiving: anything that goes wrong is left to the tick, which
    has the error handling that fails a build honestly. A callback must never
    fail because the engine could not advance yet.
    """
    if not _pass_lock.acquire(blocking=False):
        return
    try:
        build = db.session.get(CiBuild, int(build_id))
        if build is None or build.status != "running":
            return
        _advance_one(build)
    except Exception:
        logger.exception("Immediate advance of build %s failed", build_id)
        db.session.rollback()
    finally:
        _pass_lock.release()


def _last_progress_at(build: CiBuild) -> Optional[datetime]:
    """When this build last visibly did something."""
    from ...models_ci import CiLogChunk

    moments: List[Optional[datetime]] = [
        _aware(build.started_at),
        _aware(build.queued_at),
    ]
    stage_ids = []
    for stage in build.stages:
        stage_ids.append(stage.id)
        moments.append(_aware(stage.started_at))
        moments.append(_aware(stage.finished_at))
    if stage_ids:
        latest_log = (
            db.session.query(db.func.max(CiLogChunk.created_at))
            .filter(CiLogChunk.build_stage_id.in_(stage_ids))
            .scalar()
        )
        moments.append(_aware(latest_log))
    runner = build.runner
    # Only an external agent's heartbeat says anything about this build: it is
    # the agent executing it. Self-managed runners have no heartbeat at all.
    if runner is not None and runner.runner_type not in ("mock", "kubernetes"):
        moments.append(_aware(runner.last_heartbeat_at))
    present = [moment for moment in moments if moment is not None]
    return max(present) if present else None


def _build_deadline_minutes(build: CiBuild) -> int:
    """The overall budget: every stage's own timeout, plus grace, capped."""
    stages = (build.pipeline_snapshot or {}).get("stages") or []
    total_seconds = 0
    for definition in stages:
        try:
            total_seconds += int(
                (definition or {}).get("timeoutSeconds") or _DEFAULT_STAGE_TIMEOUT_SECONDS
            )
        except (TypeError, ValueError):
            total_seconds += _DEFAULT_STAGE_TIMEOUT_SECONDS
    # Cleanup commands run inside the build too (post_actions.py).
    total_seconds += post_actions.timeout_budget_seconds(build.pipeline_snapshot)
    budget = total_seconds // 60 + _BUILD_DEADLINE_GRACE_MINUTES
    return max(1, min(budget, _BUILD_HARD_CAP_MINUTES))


def _reap_stale_builds() -> None:
    """Fail builds whose runner is no longer reporting, or that ran out of time.

    Without this, a build orphaned by a backend restart or a deleted Job stays
    'running' forever and holds a runner slot. Two separate questions:

    * **Idle** — nothing has happened for ``CI_STALE_BUILD_MINUTES``. Anchored to
      the last progress, not to when the build started: a two-hour release
      build that is still printing is healthy.
    * **Overall** — the build outlived the sum of its stages' timeouts (plus
      grace, never beyond ``CI_BUILD_HARD_CAP_MINUTES``).
    """
    now = _now()
    idle_cutoff = now - timedelta(minutes=_STALE_BUILD_MINUTES)
    for build in CiBuild.query.filter(CiBuild.status == "running").all():
        started = _aware(build.started_at) or _aware(build.queued_at)
        deadline = _build_deadline_minutes(build)
        if started and started < now - timedelta(minutes=deadline):
            message = (
                f"The build exceeded its overall deadline of {deadline} minutes "
                "(the sum of its stage timeouts plus grace)."
            )
            _fail_current_stage(build, message, status="timeout")
            _finish_build(build, "timeout", message)
            db.session.commit()
            continue
        last = _last_progress_at(build)
        if last and last < idle_cutoff:
            message = (
                f"No progress for {_STALE_BUILD_MINUTES} minutes: no stage changed, "
                "no log output arrived and the runner stopped reporting."
            )
            _fail_current_stage(build, message, status="timeout")
            _finish_build(build, "timeout", message)
            db.session.commit()


def _process_cancellations() -> None:
    builds = CiBuild.query.filter(
        CiBuild.cancel_requested.is_(True),
        CiBuild.status.in_(("queued", "running")),
    ).all()
    for build in builds:
        # Every running stage, not only the current one: a parallel group has
        # several. A whole-build runner is told once — cancelling its one Job
        # stops every member — while a per-stage runner hears about each.
        whole_build_cancelled = False
        for stage in sorted(build.stages, key=lambda s: s.position):
            if stage.status != "running":
                continue
            if _is_server_stage(build, stage):
                try:
                    server_stages.executor(_server_stage_type(build, stage)).cancel(build, stage)
                except Exception:
                    logger.exception("Cancelling server stage %s failed", stage.id)
                    _close_stage(stage, "cancelled", "Cancelled by request.")
                continue
            adapter = _adapter_for(build)
            handle = _handle_for(build, stage)
            if adapter and handle and not whole_build_cancelled:
                try:
                    adapter.cancel(handle)
                    adapter.cleanup(handle)
                except Exception:
                    logger.exception("Cancelling stage %s failed", stage.id)
                whole_build_cancelled = _runs_whole_build(adapter)
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


def _runs_whole_build(adapter) -> bool:
    """Whether one dispatch of this adapter executes the entire build.

    The Kubernetes runner builds one Job per build, so every stage's container
    exists from the start and the pod must be allowed to finish — the collector
    that uploads artifacts is the pod's last act.
    """
    return bool(getattr(adapter, "runs_whole_build", False))


def _advance_one(build: CiBuild) -> None:
    stage = _current_stage(build)
    if stage is None:
        # Every stage reached a terminal state; the build's outcome is whatever
        # the stages said. Committed here: with cleanup commands holding the
        # decision (post_actions.hold_for_cleanup), this is the branch that
        # finally decides the build, and a later step's rollback must not undo it.
        _finalize(build)
        db.session.commit()
        return
    if stage.status == "pending":
        # Skipped stages resolve instantly, so walk past a run of them in this
        # pass instead of burning one scheduler tick each. A step is one stage
        # or a whole parallel group, whose members all start here together.
        while stage is not None and stage.status == "pending":
            step = _step_rows(build, stage)
            _start_step(build, step)
            next_stage = _current_stage(build)
            if next_stage is None or next_stage in step:
                break
            stage = next_stage
        if _current_stage(build) is None:
            _finalize(build)
            db.session.commit()
        return
    if stage.status != "running":
        return

    if _is_server_stage(build, stage):
        _advance_server_stage(build, stage)
        return

    step = _step_rows(build, stage)
    if len(step) > 1:
        _advance_group(build, step)
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

    _close_from_runner(build, stage, status, adapter, handle, definition, timeout)
    if _parallel_mode(build)[0] == parallel_groups.SEQUENTIAL and not _runs_whole_build(adapter):
        # A group member run on its own: say so in its log. Appended once the
        # stage is over, never before — a per-stage runner numbers its own
        # output from 1, and a line slipped in ahead would hide its first one.
        # (The Kubernetes runner prints the same line from inside the pod.)
        name = parallel_groups.group_name(_snapshot_stages(build), stage.position)
        if name:
            logs_service.append_system(
                stage, parallel_groups.sequential_notice(name, _parallel_mode(build)[1]), commit=False
            )

    # A whole-build runner keeps its pod walking after a failure so the collector
    # still uploads what earlier stages produced; its remaining stages report
    # themselves as skipped through the normal poll path. Marking them here
    # would end the build early and lose those artifacts. Per-stage runners have
    # nothing left to run, so they still need the shortcut.
    if (
        stage.status not in ("success", "skipped")
        and not bool(definition.get("continueOnFailure"))
        and not _runs_whole_build(adapter)
    ):
        # A member of a group run one stage at a time: its siblings still run
        # (Jenkins parallel semantics hold whether or not the runner could run
        # them side by side) unless the group is fail-fast.
        spared = _siblings_still_to_run(build, stage)
        for pending in build.stages:
            if pending.status == "pending" and pending.position not in spared:
                pending.status = "skipped"
                db.session.add(pending)
    db.session.commit()

    following = _current_stage(build)
    if following is None:
        _finalize(build)
        db.session.commit()
        return
    if following.status == "pending" and build.status == "running":
        # Start the successor now. Leaving it to the next pass put a whole tick
        # of dead air between every pair of stages — the single largest source
        # of "why is CI slow" when the stages themselves take seconds. Exactly
        # one level deep: that call takes the pending branch above, which starts
        # the stage (walking a run of instantly-skipped ones) and returns
        # without polling what it just started.
        _advance_one(build)


def _close_from_runner(
    build: CiBuild,
    stage: CiBuildStage,
    status: str,
    adapter,
    handle: RunnerHandle,
    definition: Dict[str, Any],
    timeout: int,
    *,
    cancelled_message: Optional[str] = None,
) -> None:
    """Close a stage the runner reported terminal, and release what it held."""
    # One final drain: output flushed as the container exited would otherwise
    # be lost, because the pump before the poll ran ahead of the terminal state.
    _pump_logs(build, stage, adapter, handle)

    if status == SUCCEEDED:
        _collect_artifacts(build, stage, adapter, handle, definition)
        _close_stage(stage, "success", None)
    elif status == SKIPPED:
        # The runner reports a stage that declined to run because an earlier one
        # failed. It is not a failure of this stage, and it has no artifacts.
        _close_stage(stage, "skipped", "An earlier stage failed.")
    elif status == TIMEOUT:
        _close_stage(stage, "timeout", f"Stage exceeded its {timeout}s timeout.")
    elif status == CANCELLED:
        _close_stage(stage, "cancelled", cancelled_message or "The runner cancelled this stage.")
    else:
        _close_stage(stage, "failed", "The stage reported failure.")

    try:
        adapter.cleanup(handle)
    except Exception:
        logger.exception("Runner cleanup failed for stage %s", stage.id)


# ---------------------------------------------------------------------------
# Parallel groups
# ---------------------------------------------------------------------------


def _snapshot_stages(build: CiBuild) -> List[Dict[str, Any]]:
    return (build.pipeline_snapshot or {}).get("stages") or []


def _parallel_mode(build: CiBuild) -> Tuple[str, str]:
    """``(mode, reason)``: how this build runs its parallel groups.

    Decided ONCE, the first time a build with groups asks (its dispatch), from
    the assigned runner's capability, and kept in the snapshot. A whole-build
    runner lays its pod out from this answer, so it must never change under a
    running build — not after a restart, not when the cluster is upgraded
    halfway through. ``("", "")`` for a build with no groups, or one that has no
    runner yet.
    """
    snapshot = build.pipeline_snapshot or {}
    decided = snapshot.get("parallel")
    if isinstance(decided, dict) and decided.get("mode") in (
        parallel_groups.PARALLEL,
        parallel_groups.SEQUENTIAL,
    ):
        return decided["mode"], str(decided.get("reason") or "")
    if not parallel_groups.has_groups(snapshot.get("stages") or []):
        return "", ""
    adapter = _adapter_for(build)
    if adapter is None:
        return "", ""
    mode, reason = parallel_groups.resolve(adapter)
    from sqlalchemy.orm.attributes import flag_modified

    updated = dict(snapshot)
    updated["parallel"] = {"mode": mode, "reason": reason}
    build.pipeline_snapshot = updated
    flag_modified(build, "pipeline_snapshot")
    db.session.add(build)
    return mode, reason


def _step_rows(build: CiBuild, stage: CiBuildStage) -> List[CiBuildStage]:
    """The stages that advance together with ``stage``: its whole parallel
    group when this build runs groups side by side, else just itself."""
    if _parallel_mode(build)[0] != parallel_groups.PARALLEL:
        return [stage]
    positions = parallel_groups.group_positions(_snapshot_stages(build), stage.position)
    if len(positions) < 2:
        return [stage]
    by_position = {row.position: row for row in build.stages}
    rows = [by_position[position] for position in positions if position in by_position]
    return rows if stage in rows else [stage]


def _siblings_still_to_run(build: CiBuild, stage: CiBuildStage) -> set:
    """Positions of the later members of ``stage``'s group that must still run
    after it failed — every one of them, unless the group is fail-fast."""
    definitions = _snapshot_stages(build)
    positions = parallel_groups.group_positions(definitions, stage.position)
    if len(positions) < 2 or parallel_groups.fail_fast(definitions, positions):
        return set()
    return {position for position in positions if position > stage.position}


def _fail_fast_trigger(build: CiBuild, rows: List[CiBuildStage]) -> Optional[CiBuildStage]:
    """The member whose failure stops a fail-fast group, or None."""
    for row in rows:
        if row.status != "cancelled" and parallel_groups.member_failed(
            row.status, _definition_for(build, row)
        ):
            return row
    return None


def _start_step(build: CiBuild, rows: List[CiBuildStage]) -> None:
    """Start every pending stage of a step, committing after each.

    For a lone stage this is exactly the old start-and-commit. For a group it
    starts the members back to back in one pass, so they run together; a
    fail-fast group that already lost a member starts no more of them.
    """
    grouped = len(rows) > 1
    fail_fast = grouped and parallel_groups.fail_fast(
        _snapshot_stages(build), [row.position for row in rows]
    )
    for row in rows:
        if row.status != "pending" or build.status != "running":
            continue
        trigger = _fail_fast_trigger(build, rows) if fail_fast else None
        if trigger is not None:
            _close_stage(
                row,
                "skipped",
                f"Not started: '{trigger.name}' failed, and this group stops at its first failure.",
            )
        else:
            _start_stage(build, row)
        db.session.commit()


def _advance_group(build: CiBuild, rows: List[CiBuildStage]) -> None:
    """One step of a parallel group run side by side.

    Start what has not started, poll every running member, close the ones
    that ended, and move on only when every member is terminal. Failure is
    Jenkins' ``parallel`` default: siblings of a failed member run on, and the
    group fails once they are done. A fail-fast group stops the others instead.
    """
    if any(row.status == "pending" for row in rows):
        # Members not started yet: the pass that started the group was cut
        # short, or a restart followed it. Start them; they are polled next.
        _start_step(build, rows)
        if any(row.status == "running" for row in rows):
            return

    definitions = _snapshot_stages(build)
    fail_fast = parallel_groups.fail_fast(definitions, [row.position for row in rows])
    adapter = _adapter_for(build)
    whole_build = _runs_whole_build(adapter)
    running = [row for row in rows if row.status == "running"]

    if running:
        statuses: Dict[int, str] = {}
        handles: Dict[int, RunnerHandle] = {}
        for row in running:
            handle = _handle_for(build, row)
            if adapter is None or handle is None:
                _close_stage(row, "failed", "The runner for this stage is no longer available.")
                continue
            handles[row.id] = handle
            _pump_logs(build, row, adapter, handle)
            timed_out = _member_timed_out(build, row, adapter, handle, whole_build)
            if timed_out:
                statuses[row.id] = TIMEOUT

        to_poll = [row for row in running if row.id in handles and row.id not in statuses]
        statuses.update(_poll_members(adapter, to_poll, handles))

        # Cancelled members last, so a fail-fast stop can name the member
        # whose failure caused it even when both were seen in this one poll.
        ordered = sorted(
            (row for row in running if statuses.get(row.id) in TERMINAL_STATUSES),
            key=lambda row: (statuses[row.id] == CANCELLED, row.position),
        )
        for row in ordered:
            definition = _definition_for(build, row)
            message = None
            if statuses[row.id] == CANCELLED and fail_fast:
                trigger = _fail_fast_trigger(build, rows)
                if trigger is not None:
                    message = f"Stopped: '{trigger.name}' failed, and this group stops at its first failure."
            _close_from_runner(
                build,
                row,
                statuses[row.id],
                adapter,
                handles[row.id],
                definition,
                int(definition.get("timeoutSeconds") or _DEFAULT_STAGE_TIMEOUT_SECONDS),
                cancelled_message=message,
            )
        db.session.commit()

    if fail_fast:
        _stop_group_after_failure(build, rows, adapter, whole_build)

    if any(row.status in ("pending", "running") for row in rows):
        return

    # The whole group is over. On a per-stage runner nothing after it may run
    # once a member that does not continue on failure has failed; a
    # whole-build runner's pod skips them itself (the group's barrier writes
    # the same fail flag a failed stage does), so its stages report skipped.
    if not whole_build and any(
        parallel_groups.member_failed(row.status, _definition_for(build, row)) for row in rows
    ):
        for pending in build.stages:
            if pending.status == "pending":
                pending.status = "skipped"
                db.session.add(pending)
    db.session.commit()

    following = _current_stage(build)
    if following is None:
        _finalize(build)
        db.session.commit()
        return
    if following.status == "pending" and build.status == "running":
        # Start what follows the group in this pass, as after any stage.
        _advance_one(build)


def _member_timed_out(
    build: CiBuild, row: CiBuildStage, adapter, handle: RunnerHandle, whole_build: bool
) -> bool:
    """Whether a running group member is past its own timeout.

    The clock starts when the work actually began. Members are all handed to
    the runner at once, and a one-slot agent runs them one after another, so a
    member still waiting its turn is not counted — ``running_since`` says when
    it really started, and the stage's start time moves there so its duration
    is its own and not its wait.

    On a whole-build runner the pod times members out itself (see
    ``parallel_groups.POD_TIMEOUT_GRACE_SECONDS``): cancelling would delete the
    one Job every sibling runs in. The engine only steps in, without
    cancelling, if the pod has said nothing well past that point.
    """
    definition = _definition_for(build, row)
    timeout = int(definition.get("timeoutSeconds") or _DEFAULT_STAGE_TIMEOUT_SECONDS)
    clock = _aware(row.started_at)
    asker = getattr(adapter, "running_since", None)
    if callable(asker):
        try:
            since = _aware(asker(handle))
        except Exception:
            logger.exception("running_since failed for stage %s", row.id)
            since = clock
        if since is None:
            return False  # Still waiting for capacity; the build deadline bounds it.
        if clock is None or since > clock:
            row.started_at = since
            db.session.add(row)
            clock = since
    if clock is None:
        return False
    limit = timeout + (parallel_groups.ENGINE_TIMEOUT_GRACE_SECONDS if whole_build else 0)
    if (_seconds_between(clock, _now()) or 0) <= limit:
        return False
    if not whole_build:
        try:
            adapter.cancel(handle)
        except Exception:
            logger.exception("Timeout cancel failed for stage %s", row.id)
    return True


def _poll_members(adapter, rows: List[CiBuildStage], handles: Dict[int, RunnerHandle]) -> Dict[int, str]:
    """Every member's status, in one observation when the runner offers one."""
    if not rows:
        return {}
    many = getattr(adapter, "poll_many", None)
    if callable(many):
        try:
            by_ref = many([handles[row.id] for row in rows])
            return {row.id: by_ref.get(handles[row.id].external_ref, RUNNING) for row in rows}
        except RunnerError as exc:
            logger.warning("Runner poll failed for a parallel group: %s", exc)
            return {row.id: FAILED for row in rows}
    statuses: Dict[int, str] = {}
    for row in rows:
        try:
            statuses[row.id] = adapter.poll(handles[row.id])
        except RunnerError as exc:
            logger.warning("Runner poll failed for stage %s: %s", row.id, exc)
            statuses[row.id] = FAILED
    return statuses


def _stop_group_after_failure(build: CiBuild, rows: List[CiBuildStage], adapter, whole_build: bool) -> None:
    """Fail fast: once a member fails, stop the members still running.

    A per-stage runner is told to cancel each one, and they close as
    cancelled. A whole-build runner is not: cancelling means deleting the
    build's one Job, and the pod already stops waiting by itself — its barrier
    sees the failure and reports the others cancelled through the next poll.
    """
    trigger = _fail_fast_trigger(build, rows)
    if trigger is None:
        return
    message = f"Stopped: '{trigger.name}' failed, and this group stops at its first failure."
    changed = False
    for row in rows:
        if row.status == "pending":
            _close_stage(row, "skipped", f"Not started: '{trigger.name}' failed, and this group stops at its first failure.")
            changed = True
        elif row.status == "running" and not whole_build:
            handle = _handle_for(build, row)
            if adapter is not None and handle is not None:
                try:
                    adapter.cancel(handle)
                    adapter.cleanup(handle)
                except Exception:
                    logger.exception("Fail-fast cancel failed for stage %s", row.id)
            _close_stage(row, "cancelled", message)
            logs_service.append_system(row, f"[kubesight] {message}", commit=False)
            changed = True
    if changed:
        db.session.commit()


def _is_server_stage(build: CiBuild, stage: CiBuildStage) -> bool:
    return _server_stage_type(build, stage) in SERVER_STAGE_TYPES


def _server_stage_type(build: CiBuild, stage: CiBuildStage) -> str:
    return _definition_for(build, stage).get("stageType") or stage.stage_type


def _advance_server_stage(build: CiBuild, stage: CiBuildStage) -> None:
    """One step of a stage KubeSight executes itself (Deploy, Approval, App
    store upload — see server_stages.py for which module runs each).

    Same shape as the runner path below: advance, and once the stage is over,
    start whatever follows in this pass. Nothing follows a server stage but
    other server stages — each decides for itself whether an earlier failure
    means it must not act.
    """
    definition = _definition_for(build, stage)
    stage_type = _server_stage_type(build, stage)
    try:
        server_stages.executor(stage_type).advance(build, stage, definition)
    except Exception:
        logger.exception("Advancing %s stage %s failed", stage_type, stage.id)
        db.session.rollback()
        _close_stage(
            stage,
            "failed",
            server_stages.ADVANCE_FAILED.get(stage_type, server_stages.ADVANCE_FAILED["deploy"]),
        )
    db.session.commit()
    if stage.status in ("pending", "running"):
        return
    following = _current_stage(build)
    if following is None:
        _finalize(build)
        db.session.commit()
        return
    if following.status == "pending" and build.status == "running":
        _advance_one(build)


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

        # A build runs on ONE runner, so that runner has to satisfy every stage
        # that will actually run — not only the first, which is usually a
        # label-less checkout.
        selection = scheduler_service.select_runner(_build_requirements(build))
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
        # INPROGRESS now when the commit is already known; otherwise when the
        # checkout reports it (routes/ci_worker.report_meta).
        build_status_service.report(build)

        # The first step: one stage, or every member of a leading parallel
        # group (the build's parallel mode is decided here, on first ask).
        _start_step(build, _step_rows(build, stage))
        db.session.commit()

        # A first stage that resolves instantly — skipped because this runner
        # cannot execute its type — must not cost a tick before the stage that
        # does the work starts.
        current = _current_stage(build)
        if build.status == "running" and (current is None or current.status == "pending"):
            _advance_one(build)
            db.session.commit()


def _build_requirements(build: CiBuild):
    """The union of what every stage that will run needs from the runner."""
    definitions = [
        _definition_for(build, stage)
        for stage in sorted(build.stages, key=lambda s: s.position)
    ]

    def will_not_run(definition: Dict[str, Any]) -> bool:
        stage_type = definition.get("stageType") or "command"
        # A Deploy stage runs on the KubeSight server, so it asks nothing of the
        # runner the build is assigned to.
        if _never_executed(definition) or stage_type in SERVER_STAGE_TYPES:
            return True
        return _condition_reason(build, definition) is not None

    return scheduler_service.requirements_for_build(definitions, will_not_run)


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
    if stage_type == "checkout" and build.service is not None and not build.service.source_ready():
        # Only a pipeline on the Pipelines page can get here — a catalog
        # service cannot build without a repository (catalog.can_run_build).
        return _NO_REPOSITORY_REASON
    if stage_type == "scan" and not scan_stage.configured(definition.get("scan")):
        # Before the runner question: whichever runner this is, there is
        # nothing to run, and the fix is in the editor, not the fleet.
        return _UNCONFIGURED_SCAN_REASON
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
    return _condition_reason(build, definition)


_NO_REPOSITORY_REASON = (
    "This pipeline has no repository, so there is nothing to check out. Connect one on its "
    "Repository tab, or remove the Checkout stage."
)


def _condition_reason(build: CiBuild, definition: Dict[str, Any]) -> Optional[str]:
    """Why this stage's own ``when`` clause says not to run — or None.

    Evaluated against the build's variables, which are exactly what the stage
    would have received as environment, so what the condition reads and what the
    commands would have read cannot diverge. A condition naming a variable the
    pipeline does not define compares against the empty string rather than
    erroring: deleting a parameter should stop the stages that depended on it,
    not break the build.
    """
    condition = definition.get("runCondition")
    if not isinstance(condition, dict):
        return None
    variable = str(condition.get("variable") or "")
    if not variable:
        return None

    variables = (build.pipeline_snapshot or {}).get("variables") or {}
    actual = str(variables.get(variable, ""))
    expected = str(condition.get("value") or "")
    operator = condition.get("operator") or "equals"

    matched = actual != expected if operator == "not_equals" else actual == expected
    if matched:
        return None
    shown = actual or "(empty)"
    comparison = "is not" if operator == "not_equals" else "is"
    return (
        f"This stage runs only when {variable} {comparison} '{expected}'. "
        f"It was '{shown}' for this build."
    )


def _start_stage(build: CiBuild, stage: CiBuildStage) -> None:
    definition = _definition_for(build, stage)
    stage_type = definition.get("stageType") or stage.stage_type

    if stage_type in SERVER_STAGE_TYPES:
        _start_server_stage(build, stage, definition)
        return

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
        # A stage skipped by its own condition was configured to behave this
        # way; one skipped because nothing can execute it was not. Saying "this
        # stage is a 'command' stage" about the first would read as a fault.
        message = (
            f"[kubesight] Skipped: {skip}"
            if _condition_reason(build, definition)
            else (
                f"[kubesight] Skipped: this stage is a '{stage_type}' stage. {skip} "
                "Nothing was built, and no artifact was recorded."
            )
        )
        logs_service.append_system(stage, message)
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
            # Cleanup commands, for a whole-build runner to bake in after the
            # stages (post_actions.py). Empty for per-stage runners.
            execution.post_plan = post_actions.plan_for(build, adapter, callback_token)
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
        secrets_service.mark_used(
            build.service_id,
            list(execution.secrets),
            fallback_service_id=secrets_service.shared_home_id(build),
        )


def _start_server_stage(build: CiBuild, stage: CiBuildStage, definition: Dict[str, Any]) -> None:
    """Start a stage no runner executes. Its own ``when`` clause still applies."""
    # Not before the cleanup commands are done: they run in the runner's
    # workspace, which a server stage outlives (post_actions.py). The stage
    # stays pending, and the next pass asks again.
    if post_actions.hold_for_cleanup(build):
        return
    condition = _condition_reason(build, definition)
    if condition:
        stage.started_at = _now()
        _close_stage(stage, "skipped", None)
        logs_service.append_system(stage, f"[kubesight] Skipped: {condition}")
        return
    stage_type = definition.get("stageType") or stage.stage_type
    try:
        server_stages.executor(stage_type).start(build, stage, definition)
    except Exception:
        logger.exception("Starting %s stage %s failed", stage_type, stage.id)
        db.session.rollback()
        stage.started_at = stage.started_at or _now()
        _close_stage(
            stage,
            "failed",
            server_stages.START_FAILED.get(stage_type, server_stages.START_FAILED["deploy"]),
        )


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
    # The other members of a parallel group are running too; a build that
    # ends must not leave any stage behind it still saying "running".
    for other in build.stages:
        if other.status == "running":
            _close_stage(other, status, message)
    for pending in build.stages:
        if pending.status == "pending":
            pending.status = "skipped"
            db.session.add(pending)


def _stopped_by_fail_fast(build: CiBuild, stage: CiBuildStage) -> bool:
    """Whether a cancelled stage was a fail-fast group stopping its members
    because a sibling failed — the build failed then; nobody cancelled it."""
    definitions = _snapshot_stages(build)
    positions = parallel_groups.group_positions(definitions, stage.position)
    if len(positions) < 2 or not parallel_groups.fail_fast(definitions, positions):
        return False
    rows = [row for row in build.stages if row.position in positions and row is not stage]
    return _fail_fast_trigger(build, rows) is not None


def _finalize(build: CiBuild) -> None:
    """Decide the build's outcome from its stages.

    A stage that failed with ``continueOnFailure`` still fails the build — the
    flag means "keep going and collect more information", not "pretend it
    passed". Anything else would let a red build report green.
    """
    statuses = [stage.status for stage in build.stages]
    if any(status in ("pending", "running") for status in statuses):
        return
    # Cleanup commands run once the runner stages are over, and the build is
    # decided after them — though never BY them: their rows are not stages
    # (post_actions.py), so a failed cleanup cannot turn a green build red.
    if post_actions.hold_for_cleanup(build):
        return
    cancelled = [stage for stage in build.stages if stage.status == "cancelled"]
    if cancelled and all(_stopped_by_fail_fast(build, stage) for stage in cancelled):
        # Members a fail-fast group stopped: the build FAILED (a sibling did),
        # and calling it cancelled would blame a person who did nothing.
        statuses = [status for status in statuses if status != "cancelled"]
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
    # Only a build that actually ran is reported: one cancelled in the queue
    # never posted INPROGRESS, so a STOPPED would be the first thing said.
    if build.started_at is not None:
        build_status_service.report(build)
    # Every terminal transition passes here, after server stages too, so this
    # is where post-action notifications are decided (queued, never sent on
    # this thread) and any cleanup that can no longer run is closed.
    post_actions.on_build_finished(build)


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
    if stage.stage_type == post_actions.POST_STAGE_TYPE:
        # A post-action row: its definition is the snapshot's postActions
        # entry, shaped like a command stage (what an agent claim runs).
        return post_actions.definition_for(build, stage)
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
    requested_tag = str(env.get("IMAGE_TAG") or "")
    # ``${MODULE}`` is finished by _resolve_image_name once the stage's full
    # environment is known; sanitizing it here would turn it into dashes.
    requested_name = str(env.get("IMAGE_NAME") or "")
    name_template = requested_name if "$" in requested_name else ""
    return (
        {
            "host": host,
            "port": urlsplit(row.base_url).port,
            "repository": _sanitize_repository(
                "" if name_template else (requested_name or service.slug)
            ),
            "repositoryTemplate": name_template,
            # A tag holding ${...} is passed through whole for the runner to
            # expand; sanitizing it here would turn the expansion into dashes.
            # The runner sanitizes the RESOLVED value, which is the string that
            # actually has to be a valid tag.
            "tag": (
                requested_tag
                if _is_tag_template(requested_tag)
                else _sanitize_tag(requested_tag or default_tag)
            ),
            "tagIsTemplate": _is_tag_template(requested_tag),
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
    resolved = secrets_service.resolve_for_build(build)
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
    working_directory = _resolve_working_directory(working_directory, env, stage.name)

    registry = None
    image_scan = None
    if stage_type == "container_image":
        registry, _ = _registry_for(build, definition)
        if registry is not None:
            _resolve_image_name(registry, env, stage.name)
        # Read off the SNAPSHOT like everything else here, so a build retried
        # from an old snapshot is gated exactly as it was when it first ran.
        # Absent on snapshots taken before scanning existed — those simply have
        # no gate, which is what they had.
        candidate = definition.get("imageScan")
        if isinstance(candidate, dict) and candidate.get("enabled") is not False:
            image_scan = candidate

    commands = list(definition.get("commands") or [])
    code_scan_gate = None
    if stage_type == "command":
        # Off the snapshot too: a retried build is gated as it first was.
        candidate = definition.get("codeScan")
        if code_scan.armed(candidate):
            code_scan_gate = candidate
            # Folded into the commands HERE rather than in one runner, so the
            # agent and the cluster run the same gate - one text, no runner on
            # which the findings quietly stop counting.
            commands = code_scan.wrap_commands(commands, candidate, stage.position)

    image = _resolve_stage_image(definition.get("image"), env, stage.name)
    scan_config = None
    if stage_type == "scan" and scan_stage.configured(definition.get("scan")):
        # Generated here, off the snapshot, for the same reasons as the code
        # scan gate above: one script for every runner, and a retried build
        # scans exactly as it first did. The image is the catalog's, never the
        # stage's — see scan_stage.image_for.
        scan_config = definition["scan"]
        if scan_config.get("tool") == "semgrep":
            code_scan_gate = scan_stage.gate_for(scan_config, definition.get("codeScan"), stage.name)
        commands = scan_stage.commands(
            scan_config,
            position=stage.position,
            application_type=service.application_type or "",
            gate=code_scan_gate,
        )
        image = scan_stage.image_for(scan_config) or None
        stage_secrets = {
            **secrets_service.env_for_stage(
                {"secretRefs": scan_stage.secret_refs(scan_config)}, resolved
            ),
            **stage_secrets,
        }

    return StageExecution(
        build_id=build.id,
        build_number=build.number,
        stage_id=stage.id,
        service_slug=service.slug,
        stage_name=stage.name,
        stage_type=stage_type,
        image=image,
        working_directory=working_directory,
        commands=commands,
        env=env,
        secrets=stage_secrets,
        # Stage over service, and whatever neither sets is left to the
        # installation default in the runner. Read off the SERVICE row rather
        # than the build snapshot on purpose: the envelope is infrastructure
        # sizing, not pipeline definition, so raising it has to fix the retry of
        # the build that was just evicted — not only builds started afterwards.
        resources=ci_resources.merge(
            definition.get("resources"), service.build_resources
        ),
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
        image_scan=image_scan,
        code_scan=code_scan_gate,
        scan=scan_config,
        callback_url=_callback_url(),
        callback_token=callback_token,
        runner_id=build.runner_id,
        **_parallel_fields(build, stage),
    )


def _parallel_fields(build: CiBuild, stage: CiBuildStage) -> Dict[str, Any]:
    """The StageExecution fields that describe this stage's parallel group.

    The mode and its reason go on EVERY stage of a build with groups, so a
    whole-build runner reads the same answer off whichever stage carries the
    plan; the group name only on members of a group of two or more.
    """
    mode, reason = _parallel_mode(build)
    definitions = _snapshot_stages(build)
    name = parallel_groups.group_name(definitions, stage.position)
    return {
        "parallel_group": name,
        "parallel_fail_fast": bool(
            name
            and parallel_groups.fail_fast(
                definitions, parallel_groups.group_positions(definitions, stage.position)
            )
        ),
        "parallel_mode": mode,
        "parallel_reason": reason,
    }


def _build_plan(build: CiBuild, adapter, callback_token: str) -> List[StageExecution]:
    """Every stage the adapter will actually run, fully resolved, in order.

    Skipped-by-policy stages are filtered with the SAME predicate the engine
    applies when it reaches them, so a whole-build runner's Job contains
    exactly the containers the engine will advance through.
    """
    plan: List[StageExecution] = []
    for stage_row in sorted(build.stages, key=lambda s: s.position):
        definition = _definition_for(build, stage_row)
        if (definition.get("stageType") or stage_row.stage_type) in SERVER_STAGE_TYPES:
            continue  # KubeSight runs it after the runner is done.
        if _skip_reason(build, adapter, definition):
            continue
        plan.append(
            _build_execution(build, stage_row, definition, callback_token=callback_token)
        )
    return plan



# A viewer polls a running stage about once a second, but logs were only
# ingested on the shared scheduler tick — so output could sit unseen for a whole
# tick even though the pod had already printed it. Draining on demand closes
# that gap; the cooldown keeps several viewers of one build from turning into
# several `kubectl logs` calls a second. Matched to the viewer's own poll so a
# tailing log is never a whole beat behind the pod.
_LOG_PUMP_COOLDOWN_SECONDS = float(os.getenv("CI_LOG_PUMP_COOLDOWN_SECONDS", "1"))
_last_log_pump: Dict[int, float] = {}


def pump_stage_logs(build: CiBuild, stage: CiBuildStage) -> None:
    """Drain a running stage's newest output now, if it is not too soon."""
    if stage.status != "running":
        return
    now = time.monotonic()
    if now - _last_log_pump.get(stage.id, 0.0) < _LOG_PUMP_COOLDOWN_SECONDS:
        return
    if len(_last_log_pump) > 500:
        _last_log_pump.clear()  # Bounded: these are only rate-limit timestamps.
    _last_log_pump[stage.id] = now

    adapter = _adapter_for(build)
    handle = _handle_for(build, stage)
    if adapter is None or handle is None:
        return
    try:
        _pump_logs(build, stage, adapter, handle)
        db.session.commit()
    except Exception:
        # Reading logs must never break reading logs: the scheduler tick will
        # drain the same output shortly.
        db.session.rollback()
        logger.exception("On-demand log pump failed for stage %s", stage.id)


def _mask_values(build: CiBuild) -> List[str]:
    """Every secret that could surface in this build's output.

    Service/global CI secrets, the git clone token, and the registry password —
    the last two are not ``ci_secrets`` rows, so relying on those alone would
    let a stray ``set -x`` print them.
    """
    from ...secret_encryption import decrypt_secret

    values: List[str] = []
    try:
        values.extend(secrets_service.resolve_for_build(build).values())
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
    if status == "awaiting_approval":
        # Not a build status: running builds held at an Approval stage — the
        # list somebody who can approve wants to find.
        query = query.filter(
            CiBuild.status == "running",
            CiBuild.stages.any(
                db.and_(CiBuildStage.stage_type == "approval", CiBuildStage.status == "running")
            ),
        )
    elif status and status != "all":
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
    # Post-action rows too: a cleanup's log, or a notification's attempts, is
    # read through the same stage-log endpoints.
    for stage in list(build.stages) + list(build.post_stages):
        if stage.id == int(stage_id):
            return stage
    raise LookupError("Build stage not found.")
