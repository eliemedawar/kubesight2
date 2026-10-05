"""Post actions — what happens when a build ends. Jenkins' ``post { }``.

A pipeline carries a list of post actions (``CiPipeline.post_actions``), copied
into every build's snapshot like its stages. Three kinds:

``email``     a message to up to 25 people, with the facts of the build
``webhook``   a Slack / Microsoft Teams / plain JSON POST to a URL kept in a
              CI SECRET (a webhook URL is a credential: whoever has it can post)
``commands``  cleanup shell commands that run IN THE BUILD WORKSPACE, with an
              image and secrets exactly like a command stage

Each has a ``when``: ``always``, ``success``, ``failure`` — and, for the two
notifications only, ``fixed`` (the first success after a failed build of the
same pipeline and branch, the message people actually want).

The two halves run at different moments, and that is the whole design:

**Notifications fire once, when the BUILD is over** — after every stage,
including the ones KubeSight runs itself (approval, deploy, app store upload),
so "the build succeeded" really means the deploy went through. Which build
results trigger which ``when``:

    success                -> always, success, fixed (when it is a fix)
    failed, timeout        -> always, failure
    cancelled              -> always only. A cancel is a person's decision, not
                              a failure of the code; ``failure`` paging somebody
                              about it would be noise. (Jenkins treats aborted
                              the same way.)
    never started          -> nothing (cancelled in the queue, refused before
                              dispatch): no build ran, so there is nothing to
                              report, and the queue page already says why.

Sending never happens on the engine's pass. The terminal transition only marks
the matching rows *queued*; a later step claims each one (a conditional UPDATE,
committed BEFORE anything is sent) and hands it to a small worker pool. Network
failures retry with backoff (webhook 4 attempts, email 3). A claim that never
recorded a result — the process died mid-send — is closed as interrupted and
NOT retried: it may already have arrived, and a notification sent twice is worse
than one that says it might not have been. So a restart can lose at most the
message that was in flight, and never repeats one.

**Cleanup runs when the last RUNNER stage ends**, in that runner's workspace,
with the outcome of the runner stages (``failure`` when any of them failed,
timed out or was cancelled — continue-on-failure stages included, exactly as
they fail the build). It has to be then: on Kubernetes the pod — and with it the
workspace — is gone by the time a server stage runs.

* Kubernetes: one ``post-N`` initContainer per cleanup, after every stage and
  before the collector. It runs whatever the fail flag says and decides by it
  (success = no flag, failure = a flag), then exits 0 so the collector still
  uploads. The engine reads its result off the finished pod.
* Agents and the mock runner: the engine dispatches each cleanup after the
  runner stages, in order, decides ``when`` itself, and waits for it before a
  server stage starts or the build is decided.

A cleanup NEVER changes the build's result. A failed one is visible — its own
row, red, with its log — but a successful build stays successful: cleanup is
housekeeping (a temp namespace, a lock, a cache), and failing a build whose
code and artifacts are fine because ``rm`` returned 1 would teach people to
ignore red builds. A cancelled build on Kubernetes has had its Job deleted, so
its cleanup cannot run; on every runner a cancelled build's cleanup is closed
as skipped, saying so.

Storage: one ``ci_build_stages`` row per action with ``stage_type="post"`` and
``position = 1000 + index`` — so cleanup logs, agent tasks and the log viewer
are a stage's — kept out of ``CiBuild.stages`` (see models_ci) so nothing that
walks a build's stages, decides its result or counts its progress sees them.
The row's ``server_state`` holds the kind, the trigger and the delivery attempts.
"""

from __future__ import annotations

import html
import json
import logging
import os
import threading
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Dict, List, Optional, Tuple

from ...db import db
from ...models_ci import SERVER_STAGE_TYPES, TERMINAL_BUILD_STATUSES, CiBuild, CiBuildStage, CiPipeline

logger = logging.getLogger(__name__)

POST_STAGE_TYPE = "post"
# Post rows sit after every stage position a pipeline can have (MAX_STAGES is
# 40), and the same number names their Kubernetes secret keys, so it must never
# collide with a stage's.
POSITION_BASE = 1000

ACTION_TYPES = ("email", "webhook", "commands")
WHEN_VALUES = ("always", "success", "failure", "fixed")
# A cleanup runs with the runner stages' outcome, which knows nothing about the
# previous build — so it cannot be "fixed".
CLEANUP_WHEN_VALUES = ("always", "success", "failure")
WEBHOOK_FORMATS = ("slack", "teams", "json")

MAX_ACTIONS = 10
MAX_SUBJECT_CHARS = 200
MAX_MESSAGE_CHARS = 4000
DEFAULT_CLEANUP_TIMEOUT = 600
MIN_CLEANUP_TIMEOUT = 30
# Below the engine's idle reaper (60 minutes without progress): a cleanup that
# runs inside the finished pod adds no log lines to any stage while it works.
MAX_CLEANUP_TIMEOUT = 1800

WEBHOOK_TIMEOUT_SECONDS = float(os.getenv("CI_POST_WEBHOOK_TIMEOUT_SECONDS", "10"))
# Delay before attempt 2, 3, 4.
RETRY_DELAYS_SECONDS = (30, 120, 600)
MAX_ATTEMPTS = {"webhook": 4, "email": 3}
# A claimed delivery that has not recorded a result after this long died with
# the process that claimed it (a send is bounded by its timeouts, far below this).
STALE_CLAIM_SECONDS = 300
MAX_IN_FLIGHT = 8
# A whole-build runner's cleanup has already run inside the pod by the time the
# engine looks; this is how long past its own timeout the engine keeps asking.
_ATTACH_GRACE_SECONDS = 120

_WHEN_PHRASES = {
    "always": "always",
    "success": "on success",
    "failure": "on failure",
    "fixed": "when fixed",
}
_KIND_LABELS = {"email": "Email", "commands": "Cleanup"}
_FORMAT_LABELS = {"slack": "Slack", "teams": "Teams", "json": "Webhook"}
_RESULT_WORDS = {
    "success": "succeeded",
    "failed": "failed",
    "timeout": "timed out",
    "cancelled": "was cancelled",
}


class PostActionError(ValueError):
    """A post action was rejected on save. Message is user-facing."""


class DeliveryError(RuntimeError):
    """A notification could not be delivered. Message is user-facing and
    never carries the webhook URL."""

    def __init__(self, message: str, *, retryable: bool):
        super().__init__(message)
        self.retryable = retryable


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _aware(value: Optional[datetime]) -> Optional[datetime]:
    if value is None:
        return None
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def _parse_iso(value: Any) -> Optional[datetime]:
    if not value:
        return None
    try:
        return _aware(datetime.fromisoformat(str(value)))
    except ValueError:
        return None


def _json_list(value: Any) -> List[Any]:
    if isinstance(value, str):
        try:
            value = json.loads(value or "[]")
        except ValueError:
            return []
    return list(value) if isinstance(value, (list, tuple)) else []


# ---------------------------------------------------------------------------
# Configuration (saved with the pipeline, validated here)
# ---------------------------------------------------------------------------

def label(action: Dict[str, Any]) -> str:
    """The name a post action's row carries: "Email · on failure"."""
    kind = action.get("type")
    if kind == "webhook":
        noun = _FORMAT_LABELS.get(action.get("format") or "json", "Webhook")
    elif kind == "commands":
        noun = str(action.get("name") or "").strip() or "Cleanup"
    else:
        noun = _KIND_LABELS.get(kind, "Post action")
    return f"{noun} · {_WHEN_PHRASES.get(action.get('when') or 'always', 'always')}"[:120]


def normalize(value: Any, known_secret_keys: set) -> List[Dict[str, Any]]:
    """Validate and normalize a pipeline's ``postActions``.

    The same strictness as stages for the two things that can hurt: a secret
    that does not exist (a webhook that can never be sent, a cleanup missing
    its credential) and a recipient that is not an address.
    """
    if value in (None, ""):
        return []
    if not isinstance(value, (list, tuple)):
        raise PostActionError("Post actions must be a list.")
    if len(value) > MAX_ACTIONS:
        raise PostActionError(f"A pipeline may have at most {MAX_ACTIONS} post actions.")
    out = [_normalize_one(item, index, known_secret_keys) for index, item in enumerate(value)]
    names = [a["name"].lower() for a in out if a["type"] == "commands"]
    repeated = sorted({n for n in names if names.count(n) > 1})
    if repeated:
        raise PostActionError(
            f"Cleanup names must be unique: '{repeated[0]}' is used twice."
        )
    return out


def _normalize_one(item: Any, index: int, known_keys: set) -> Dict[str, Any]:
    from . import build_inputs, code_scan
    from .pipelines import _clean, _command_lines, _env_map

    where = f"Post action {index + 1}"
    if not isinstance(item, dict):
        raise PostActionError(f"{where} is not an object.")
    kind = str(item.get("type") or "").strip().lower()
    if kind not in ACTION_TYPES:
        raise PostActionError(
            f"{where} has an unknown type '{kind or '(none)'}'. Choose email, webhook or commands."
        )
    when = str(item.get("when") or "always").strip().lower()
    if kind == "commands" and when == "fixed":
        raise PostActionError(
            f"{where}: cleanup commands run with the outcome of this build's stages, "
            "which cannot be 'fixed'. Choose always, success or failure."
        )
    allowed = CLEANUP_WHEN_VALUES if kind == "commands" else WHEN_VALUES
    if when not in allowed:
        raise PostActionError(f"{where}: 'when' must be one of {', '.join(allowed)}.")

    out: Dict[str, Any] = {"type": kind, "when": when}

    if kind == "email":
        try:
            recipients = code_scan.clean_recipients(item.get("recipients"), source=f"{where} (email)")
        except code_scan.CodeScanConfigError as exc:
            raise PostActionError(str(exc))
        if not recipients:
            raise PostActionError(f"{where} (email) needs at least one recipient.")
        out["recipients"] = recipients
        out["subject"] = " ".join(str(item.get("subject") or "").split())[:MAX_SUBJECT_CHARS]
        out["message"] = str(item.get("message") or "").strip()[:MAX_MESSAGE_CHARS]
        return out

    if kind == "webhook":
        secret = _clean(item.get("urlSecret"), 120)
        if not secret:
            raise PostActionError(
                f"{where} (webhook) needs the CI secret that holds its URL. Webhook URLs "
                "are credentials, so they are kept as secrets, never in the pipeline."
            )
        if secret not in known_keys:
            raise PostActionError(
                f"{where} (webhook) uses secret '{secret}', which is not defined for this service."
            )
        fmt = str(item.get("format") or "json").strip().lower()
        if fmt == "generic":
            fmt = "json"
        if fmt not in WEBHOOK_FORMATS:
            raise PostActionError(f"{where} (webhook): format must be slack, teams or json.")
        out["urlSecret"] = secret
        out["format"] = fmt
        return out

    # commands
    name = _clean(item.get("name"), 80) or "Cleanup"
    commands = _command_lines(item.get("commands"))
    if not any(line.strip() for line in commands):
        raise PostActionError(f"Cleanup '{name}' has no commands.")
    working_directory = _clean(item.get("workingDirectory"), 512) or None
    problem = build_inputs.working_directory_problem(working_directory)
    if problem:
        raise PostActionError(f"Cleanup '{name}': {problem}")
    secret_refs: List[Dict[str, str]] = []
    for ref in (item.get("secretRefs") or [])[:50] if isinstance(item.get("secretRefs"), list) else []:
        if isinstance(ref, str):
            ref = {"name": ref}
        if not isinstance(ref, dict):
            continue
        ref_name = _clean(ref.get("name"), 120)
        if not ref_name:
            continue
        if ref_name not in known_keys:
            raise PostActionError(
                f"Cleanup '{name}' uses secret '{ref_name}', which is not defined for this service."
            )
        secret_refs.append({"name": ref_name, "envVar": _clean(ref.get("envVar"), 128) or ref_name})
    timeout = item.get("timeoutSeconds")
    try:
        timeout = int(timeout) if timeout not in (None, "") else DEFAULT_CLEANUP_TIMEOUT
    except (TypeError, ValueError):
        raise PostActionError(f"Cleanup '{name}' has an invalid timeout.")
    if not MIN_CLEANUP_TIMEOUT <= timeout <= MAX_CLEANUP_TIMEOUT:
        raise PostActionError(
            f"Cleanup '{name}' timeout must be between {MIN_CLEANUP_TIMEOUT} seconds and "
            f"{MAX_CLEANUP_TIMEOUT // 60} minutes."
        )
    out.update(
        {
            "name": name,
            "image": _clean(item.get("image"), 512) or None,
            "workingDirectory": working_directory,
            "commands": commands,
            "env": _env_map(item.get("env")),
            "secretRefs": secret_refs,
            "timeoutSeconds": timeout,
        }
    )
    return out


def of_pipeline(pipeline: Any) -> List[Dict[str, Any]]:
    """A pipeline's saved post actions. A generated default (an unsaved
    stand-in) inherits the ones saved on the row it stands in for."""
    raw = getattr(pipeline, "post_actions", None)
    if raw is None and not isinstance(pipeline, CiPipeline) and getattr(pipeline, "id", None):
        row = db.session.get(CiPipeline, int(pipeline.id))
        raw = row.post_actions if row is not None else None
    return [
        dict(item)
        for item in _json_list(raw)
        if isinstance(item, dict) and item.get("type") in ACTION_TYPES
    ]


def timeout_budget_seconds(snapshot: Optional[Dict[str, Any]]) -> int:
    """What the build's overall deadline must allow for its cleanups."""
    total = 0
    for action in _json_list((snapshot or {}).get("postActions")):
        if isinstance(action, dict) and action.get("type") == "commands":
            try:
                total += int(action.get("timeoutSeconds") or DEFAULT_CLEANUP_TIMEOUT)
            except (TypeError, ValueError):
                total += DEFAULT_CLEANUP_TIMEOUT
    return total


# ---------------------------------------------------------------------------
# Rows on a build
# ---------------------------------------------------------------------------

def create_rows(build: CiBuild, actions: List[Dict[str, Any]]) -> None:
    """One row per post action, at trigger time, beside the stage rows."""
    for index, action in enumerate(actions):
        db.session.add(
            CiBuildStage(
                build_id=build.id,
                position=POSITION_BASE + index,
                name=label(action),
                stage_type=POST_STAGE_TYPE,
                status="pending",
                server_state={"kind": action.get("type"), "when": action.get("when") or "always", "phase": "waiting"},
            )
        )


def rows(build: CiBuild) -> List[CiBuildStage]:
    return sorted(getattr(build, "post_stages", None) or [], key=lambda r: r.position)


def _state(row: CiBuildStage) -> Dict[str, Any]:
    return dict(row.server_state) if isinstance(row.server_state, dict) else {}


def _save(row: CiBuildStage, patch: Dict[str, Any]) -> None:
    # A new dict: the JSON column is not mutation-tracked.
    row.server_state = {**_state(row), **patch}
    db.session.add(row)


def action_for(build: CiBuild, row: CiBuildStage) -> Dict[str, Any]:
    actions = _json_list((build.pipeline_snapshot or {}).get("postActions"))
    index = int(row.position or 0) - POSITION_BASE
    action = actions[index] if 0 <= index < len(actions) else None
    return dict(action) if isinstance(action, dict) else {}


def kind_of(row: CiBuildStage) -> str:
    return str(_state(row).get("kind") or "")


def definition_for(build: CiBuild, row: CiBuildStage) -> Dict[str, Any]:
    """The stage-shaped definition a cleanup runs from — what
    ``engine._definition_for`` answers for a post row, so the agent claim and
    ``engine._build_execution`` treat a cleanup like a command stage."""
    action = action_for(build, row)
    if action.get("type") != "commands":
        return {"stageType": POST_STAGE_TYPE, "name": row.name, "commands": []}
    try:
        timeout = int(action.get("timeoutSeconds") or DEFAULT_CLEANUP_TIMEOUT)
    except (TypeError, ValueError):
        timeout = DEFAULT_CLEANUP_TIMEOUT
    return {
        "stageType": POST_STAGE_TYPE,
        "name": row.name,
        "image": action.get("image"),
        "workingDirectory": action.get("workingDirectory"),
        "commands": list(action.get("commands") or []),
        "env": dict(action.get("env") or {}),
        "secretRefs": list(action.get("secretRefs") or []),
        "artifacts": [],
        "hostAliases": [],
        "resources": None,
        "timeoutSeconds": timeout,
        # A cleanup's failure is its own, never the build's.
        "continueOnFailure": True,
        "postWhen": action.get("when") or "always",
    }


def _cleanup_rows(build: CiBuild) -> List[CiBuildStage]:
    return [row for row in rows(build) if kind_of(row) == "commands"]


def _close(row: CiBuildStage, status: str, error: Optional[str], *, log: Optional[str] = None) -> None:
    from . import engine
    from . import logs as logs_service

    if row.started_at is None:
        row.started_at = _now()
    engine._close_stage(row, status, error)
    # The why of a skip is on the row as well as in its log, so the build
    # drawer can say it without opening the log.
    _save(row, {"phase": "done", **({"detail": log.replace("[kubesight] ", "", 1)} if log else {})})
    if log:
        db.session.flush()
        logs_service.append_system(row, log, commit=False)


# ---------------------------------------------------------------------------
# Cleanup — when the runner stages are over
# ---------------------------------------------------------------------------

def runner_outcome(build: CiBuild) -> str:
    """``failure`` when any runner stage failed, timed out or was cancelled —
    continue-on-failure ones included, as they fail the build — else ``success``."""
    for stage in build.stages:
        if stage.stage_type in SERVER_STAGE_TYPES:
            continue
        if stage.status in ("failed", "timeout", "cancelled"):
            return "failure"
    return "success"


def _cleanup_matches(when: str, outcome: str) -> bool:
    return when == "always" or when == outcome


def _not_run_reason(when: str, outcome: str) -> str:
    if when == "success":
        return "This cleanup runs when the stages succeed, and one of them failed."
    return "This cleanup runs when a stage fails, and every stage succeeded."


def plan_for(build: CiBuild, adapter, callback_token: str = "") -> List[Any]:
    """The cleanups a whole-build runner bakes into its Job, fully resolved.

    Every cleanup goes in, whatever its ``when``: the outcome is not known when
    the Job is created, so each container decides by the fail flag in the pod.
    A cleanup that cannot even be prepared (its image names a build input that
    is empty) is closed as failed here rather than failing the build's start.
    """
    from . import engine

    if not engine._runs_whole_build(adapter):
        return []
    plan = []
    for row in _cleanup_rows(build):
        if row.status != "pending":
            continue
        definition = definition_for(build, row)
        try:
            execution = engine._build_execution(build, row, definition, callback_token=callback_token)
        except Exception as exc:  # BuildError from the image template, above all
            _close(row, "failed", engine._safe_message(exc))
            continue
        execution.post_when = definition.get("postWhen") or "always"
        plan.append(execution)
    return plan


def hold_for_cleanup(build: CiBuild) -> bool:
    """Advance this build's cleanups; True while one is still outstanding.

    Called by the engine before it decides the build or starts a server stage:
    while this answers True, neither may happen. Returns False straight away
    while a runner stage is still pending or running, and when there is
    nothing to clean up. Commits its own transitions. Never raises — a cleanup
    that breaks the engine must not fail a build it could not have changed.
    """
    try:
        open_rows = [row for row in _cleanup_rows(build) if row.status in ("pending", "running")]
        if not open_rows or build.status != "running":
            return False
        if any(
            stage.status in ("pending", "running")
            for stage in build.stages
            if stage.stage_type not in SERVER_STAGE_TYPES
        ):
            return False
        changed, holding = _advance_cleanup(build, open_rows)
        if changed:
            db.session.commit()
        return holding
    except Exception:
        logger.exception("Advancing the cleanup of build %s failed", getattr(build, "id", "?"))
        try:
            for row in _cleanup_rows(build):
                if row.status in ("pending", "running"):
                    _close(
                        row,
                        "failed",
                        "The cleanup could not be run; see the server log. The build's result is not affected.",
                    )
            db.session.commit()
        except Exception:
            logger.exception("Closing the cleanup of build %s failed", getattr(build, "id", "?"))
            db.session.rollback()
        return False


def _advance_cleanup(build: CiBuild, open_rows: List[CiBuildStage]) -> Tuple[bool, bool]:
    from . import engine

    changed = False
    ran = [
        stage
        for stage in build.stages
        if stage.stage_type not in SERVER_STAGE_TYPES and stage.external_ref
    ]
    if not ran:
        for row in open_rows:
            _close(
                row,
                "skipped",
                None,
                log="[kubesight] Skipped: no stage of this build ran on a runner, so there was no workspace to clean up.",
            )
        return True, False

    adapter = engine._adapter_for(build)
    if adapter is None:
        for row in open_rows:
            _close(row, "failed", "The runner for this build is no longer available, so the cleanup could not run.")
        return True, False
    whole_build = engine._runs_whole_build(adapter)
    outcome = runner_outcome(build)

    for row in open_rows:
        if row.status == "pending":
            when = _state(row).get("when") or "always"
            # A whole-build runner's container already decided by the fail
            # flag inside the pod; its log says which way. Elsewhere the
            # engine decides, with the same rule.
            if not whole_build and not _cleanup_matches(when, outcome):
                _close(row, "skipped", None, log=f"[kubesight] Skipped: {_not_run_reason(when, outcome)}")
                changed = True
                continue
            _start_cleanup(build, row, adapter, outcome)
            changed = True
        if row.status == "running":
            if _poll_cleanup(build, row, adapter, whole_build):
                changed = True
            if row.status == "running":
                return changed, True
    return changed, False


def _start_cleanup(build: CiBuild, row: CiBuildStage, adapter, outcome: str) -> None:
    from . import engine
    from . import secrets as secrets_service

    definition = definition_for(build, row)
    try:
        execution = engine._build_execution(build, row, definition)
    except Exception as exc:
        _close(row, "failed", engine._safe_message(exc))
        return
    # The same name the pod's script exports, so a cleanup can branch on it on
    # every runner: rm the half-built release on failure, tag it on success.
    execution.env = {**(execution.env or {}), "KUBESIGHT_STAGES_RESULT": outcome}
    execution.post_when = definition.get("postWhen") or "always"

    row.status = "running"
    row.started_at = _now()
    row.runner_id = build.runner_id
    _save(row, {"phase": "running", "stagesResult": outcome})
    db.session.flush()
    try:
        handle = adapter.start(execution)
    except Exception as exc:
        logger.warning("Starting cleanup %s failed: %s", row.id, exc)
        _close(row, "failed", engine._safe_message(exc))
        return
    row.external_ref = handle.external_ref
    db.session.add(row)
    if execution.secrets:
        secrets_service.mark_used(
            build.service_id,
            list(execution.secrets),
            fallback_service_id=secrets_service.shared_home_id(build),
        )


def _poll_cleanup(build: CiBuild, row: CiBuildStage, adapter, whole_build: bool) -> bool:
    """One look at a running cleanup. Returns whether it ended."""
    from . import engine
    from .runners import CANCELLED, FAILED, SKIPPED, SUCCEEDED, TERMINAL_STATUSES, TIMEOUT, RunnerError

    handle = engine._handle_for(build, row)
    if handle is None:
        _close(row, "failed", "The cleanup lost its runner handle.")
        return True
    engine._pump_logs(build, row, adapter, handle)

    definition = definition_for(build, row)
    timeout = int(definition.get("timeoutSeconds") or DEFAULT_CLEANUP_TIMEOUT)
    elapsed = engine._seconds_between(row.started_at, _now()) or 0
    limit = timeout + (_ATTACH_GRACE_SECONDS if whole_build else 0)
    if elapsed > limit:
        # Never cancel through a whole-build runner: that deletes the build's
        # Job. Its container is bounded by `timeout` inside the pod already.
        if not whole_build:
            try:
                adapter.cancel(handle)
            except Exception:
                logger.exception("Cancelling cleanup %s failed", row.id)
        status = TIMEOUT
    else:
        try:
            status = adapter.poll(handle)
        except RunnerError as exc:
            logger.warning("Polling cleanup %s failed: %s", row.id, exc)
            status = FAILED
    if status not in TERMINAL_STATUSES:
        return False

    engine._pump_logs(build, row, adapter, handle)
    if status == SUCCEEDED:
        _close(row, "success", None)
    elif status == SKIPPED:
        _close(row, "skipped", None)
    elif status == TIMEOUT:
        _close(row, "timeout", f"The cleanup exceeded its {timeout}s timeout. The build's result is not affected.")
    elif status == CANCELLED:
        _close(row, "cancelled", "The runner cancelled the cleanup.")
    elif whole_build and not row.log_line_count:
        _close(
            row,
            "failed",
            "The cleanup never ran: the build pod stopped before reaching it "
            "(a stage timed out, or the Job was removed).",
        )
    else:
        _close(row, "failed", "The cleanup commands failed. The build's result is not affected.")
    try:
        adapter.cleanup(handle)
    except Exception:
        logger.exception("Runner cleanup failed for post row %s", row.id)
    return True


# ---------------------------------------------------------------------------
# The build reached its terminal state
# ---------------------------------------------------------------------------

def _is_fixed(build: CiBuild) -> bool:
    """Whether this success follows a failure: the previous finished build of
    the same service, pipeline and branch failed or timed out. Cancelled
    builds are passed over — they say nothing about the code."""
    if build.status != "success":
        return False
    query = CiBuild.query.filter(
        CiBuild.service_id == build.service_id,
        CiBuild.id < build.id,
        CiBuild.status.in_(("success", "failed", "timeout")),
    )
    query = (
        query.filter(CiBuild.pipeline_id == build.pipeline_id)
        if build.pipeline_id is not None
        else query.filter(CiBuild.pipeline_id.is_(None))
    )
    query = query.filter(CiBuild.branch == build.branch) if build.branch else query
    previous = query.order_by(CiBuild.id.desc()).first()
    return previous is not None and previous.status in ("failed", "timeout")


def notification_matches(when: str, status: str, fixed: bool) -> bool:
    if when == "always":
        return True
    if when == "success":
        return status == "success"
    if when == "failure":
        return status in ("failed", "timeout")
    if when == "fixed":
        return status == "success" and fixed
    return False


def _not_triggered_reason(when: str, status: str) -> str:
    result = _RESULT_WORDS.get(status, status)
    if when == "fixed":
        if status == "success":
            return "Sent only when a build fixes a failure; the previous build had not failed."
        return f"Sent only when a build fixes a failure; this build {result}."
    if when == "failure" and status == "cancelled":
        return "A cancelled build is not a failure; only 'always' notifications go out for it."
    phrase = _WHEN_PHRASES.get(when, when)
    return f"Sent {phrase}; this build {result}."


def on_build_finished(build: CiBuild) -> None:
    """The build just reached its terminal state (``engine._finish_build``).

    Closes any cleanup that will now never run, and decides each notification:
    queued for delivery or not triggered. Sends nothing — that is
    :func:`deliver_due`, off this transaction and off the engine's pass.
    Joins the caller's transaction; never raises.
    """
    try:
        post_rows = rows(build)
    except Exception:
        logger.exception("Reading the post actions of build %s failed", getattr(build, "id", "?"))
        return
    if not post_rows:
        return
    try:
        _settle_on_finish(build, post_rows)
    except Exception:
        logger.exception("Settling the post actions of build %s failed", getattr(build, "id", "?"))


def _settle_on_finish(build: CiBuild, post_rows: List[CiBuildStage]) -> None:
    from . import engine

    status = build.status
    started = build.started_at is not None
    fixed: Optional[bool] = None
    for row in post_rows:
        kind = kind_of(row)
        state = _state(row)
        if kind == "commands":
            if row.status == "pending":
                if status == "cancelled":
                    reason = (
                        "The build was cancelled. Cancelling stops a build where it is "
                        "(on Kubernetes it deletes the build's Job, and its workspace with it), "
                        "so this cleanup did not run."
                    )
                else:
                    reason = f"The build ended ({_RESULT_WORDS.get(status, status)}) before its cleanup could run."
                _close(row, "skipped", None, log=f"[kubesight] Skipped: {reason}")
            elif row.status == "running":
                adapter = engine._adapter_for(build)
                handle = engine._handle_for(build, row)
                if adapter is not None and handle is not None and not engine._runs_whole_build(adapter):
                    try:
                        adapter.cancel(handle)
                        adapter.cleanup(handle)
                    except Exception:
                        logger.exception("Cancelling cleanup %s failed", row.id)
                _close(row, "cancelled", f"The build ended ({_RESULT_WORDS.get(status, status)}) while the cleanup was running.")
            continue
        if row.status != "pending" or state.get("phase") != "waiting":
            continue  # already decided — a terminal transition is only handled once
        when = state.get("when") or "always"
        if not started:
            _close(
                row,
                "skipped",
                None,
                log="[kubesight] Not sent: the build never started, so there was nothing to report.",
            )
            continue
        if when == "fixed" and fixed is None:
            fixed = _is_fixed(build)
        if notification_matches(when, status, bool(fixed)):
            _save(
                row,
                {
                    "phase": "queued",
                    "trigger": status,
                    "attempts": 0,
                    "nextAttemptAt": _now().isoformat(),
                },
            )
        else:
            _close(row, "skipped", None, log=f"[kubesight] Not sent: {_not_triggered_reason(when, status)}")


# ---------------------------------------------------------------------------
# Delivery — claimed on the engine's pass, sent off it
# ---------------------------------------------------------------------------

_executor: Optional[ThreadPoolExecutor] = None
_executor_lock = threading.Lock()
_in_flight: set = set()
_in_flight_lock = threading.Lock()
_delivery_runner: Optional[Callable[[int], None]] = None


def set_delivery_runner(fn: Optional[Callable[[int], None]]) -> None:
    """Test hook: ``fn(row_id)`` receives each claimed delivery instead of the
    worker pool. ``None`` restores the default."""
    global _delivery_runner
    _delivery_runner = fn


def _pool() -> ThreadPoolExecutor:
    global _executor
    with _executor_lock:
        if _executor is None:
            _executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="ci-post-actions")
        return _executor


def pending_work() -> bool:
    """Cheap: is any notification queued or being sent?"""
    return (
        db.session.query(CiBuildStage.id)
        .filter(
            CiBuildStage.stage_type == POST_STAGE_TYPE,
            CiBuildStage.status.in_(("pending", "running")),
        )
        .join(CiBuild, CiBuild.id == CiBuildStage.build_id)
        .filter(CiBuild.status.in_(TERMINAL_BUILD_STATUSES))
        .first()
        is not None
    )


def deliver_due() -> int:
    """Claim every notification that is due and hand it to the senders.

    Returns how many were claimed. The claim is committed before anything is
    sent, so two passes (or two processes) can never send the same row, and a
    process that dies mid-send leaves a claim :func:`_reap_interrupted` closes
    instead of repeating.
    """
    now = _now()
    candidates = (
        CiBuildStage.query.join(CiBuild, CiBuild.id == CiBuildStage.build_id)
        .filter(
            CiBuildStage.stage_type == POST_STAGE_TYPE,
            CiBuildStage.status.in_(("pending", "running")),
            CiBuild.status.in_(TERMINAL_BUILD_STATUSES),
        )
        .order_by(CiBuildStage.id.asc())
        .limit(200)
        .all()
    )
    claimed = 0
    for row in candidates:
        state = _state(row)
        if state.get("kind") not in ("email", "webhook"):
            continue
        if row.status == "running":
            _reap_interrupted(row, state, now)
            continue
        if state.get("phase") == "waiting":
            # The build ended without its notifications being decided (the
            # terminal transition's own attempt failed): decide them now.
            build = db.session.get(CiBuild, row.build_id)
            if build is not None:
                _settle_on_finish(build, [row])
                db.session.commit()
                state = _state(row)
        if row.status != "pending" or state.get("phase") != "queued":
            continue
        due = _parse_iso(state.get("nextAttemptAt"))
        if due is not None and due > now:
            continue
        with _in_flight_lock:
            if len(_in_flight) >= MAX_IN_FLIGHT:
                break
        if not _claim(row, state, now):
            continue
        claimed += 1
        _submit(row.id)
    return claimed


def _claim(row: CiBuildStage, state: Dict[str, Any], now: datetime) -> bool:
    updated = (
        db.session.query(CiBuildStage)
        .filter(CiBuildStage.id == row.id, CiBuildStage.status == "pending")
        .update({"status": "running"}, synchronize_session=False)
    )
    if updated != 1:
        db.session.rollback()
        return False
    row.status = "running"
    if row.started_at is None:
        row.started_at = now
    _save(
        row,
        {
            "phase": "sending",
            "attempts": int(state.get("attempts") or 0) + 1,
            "claimedAt": now.isoformat(),
        },
    )
    db.session.commit()
    return True


def _reap_interrupted(row: CiBuildStage, state: Dict[str, Any], now: datetime) -> None:
    with _in_flight_lock:
        if row.id in _in_flight:
            return
    claimed_at = _parse_iso(state.get("claimedAt"))
    if claimed_at is not None and now - claimed_at < timedelta(seconds=STALE_CLAIM_SECONDS):
        return
    _close(
        row,
        "failed",
        "Delivery was interrupted (KubeSight restarted while sending). It is not retried, "
        "because it may already have arrived.",
    )
    db.session.commit()


def _submit(row_id: int) -> None:
    if _delivery_runner is not None:
        _delivery_runner(row_id)
        return
    from flask import current_app

    app = current_app._get_current_object()
    if app.config.get("TESTING"):
        # No worker threads against a test database: deliver inline.
        deliver(row_id)
        return
    with _in_flight_lock:
        _in_flight.add(row_id)
    try:
        _pool().submit(_deliver_in_app, app, row_id)
    except Exception:
        with _in_flight_lock:
            _in_flight.discard(row_id)
        raise


def _deliver_in_app(app, row_id: int) -> None:
    try:
        with app.app_context():
            try:
                deliver(row_id)
            except Exception:
                logger.exception("Delivering post action %s failed", row_id)
                db.session.rollback()
    finally:
        with _in_flight_lock:
            _in_flight.discard(row_id)


def deliver(row_id: int) -> None:
    """Send one claimed notification and record what happened."""
    from . import logs as logs_service

    row = db.session.get(CiBuildStage, int(row_id))
    if row is None or row.status != "running" or row.stage_type != POST_STAGE_TYPE:
        return
    build = db.session.get(CiBuild, row.build_id)
    if build is None:
        return
    state = _state(row)
    kind = state.get("kind")
    attempt = int(state.get("attempts") or 1)
    action = action_for(build, row)
    try:
        if not action:
            raise DeliveryError("This post action is missing from the build's snapshot.", retryable=False)
        detail = _send_email(build, action) if kind == "email" else _send_webhook(build, action)
    except DeliveryError as exc:
        message = str(exc)[:500]
        logs_service.append_system(row, f"[kubesight] Attempt {attempt} failed: {message}", commit=False)
        if exc.retryable and attempt < MAX_ATTEMPTS.get(kind, 1):
            delay = RETRY_DELAYS_SECONDS[min(attempt - 1, len(RETRY_DELAYS_SECONDS) - 1)]
            row.status = "pending"
            row.error = message
            _save(
                row,
                {
                    "phase": "queued",
                    "lastError": message,
                    "nextAttemptAt": (_now() + timedelta(seconds=delay)).isoformat(),
                },
            )
            logs_service.append_system(row, f"[kubesight] Retrying in {delay}s.", commit=False)
        else:
            _close(row, "failed", message)
            _save(row, {"lastError": message})
        db.session.commit()
        return
    except Exception:
        logger.exception("Post action %s raised while sending", row.id)
        _close(row, "failed", "Sending failed unexpectedly; see the server log.")
        db.session.commit()
        return
    row.error = None
    _close(row, "success", None)
    _save(row, {"detail": detail, "deliveredAt": _now().isoformat()})
    logs_service.append_system(row, f"[kubesight] {detail}", commit=False)
    db.session.commit()


# ---------------------------------------------------------------------------
# What a notification says
# ---------------------------------------------------------------------------

def _duration(seconds: Optional[int]) -> str:
    if seconds is None:
        return ""
    seconds = int(seconds)
    if seconds < 60:
        return f"{seconds}s"
    minutes, rest = divmod(seconds, 60)
    if minutes < 60:
        return f"{minutes}m {rest}s"
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h {minutes}m"


def _short(text: Any, limit: int = 300) -> str:
    value = " ".join(str(text or "").split())
    return value if len(value) <= limit else value[: limit - 1] + "…"


def _tests_line(tests: Optional[Dict[str, Any]]) -> str:
    if not tests:
        return ""
    parts = []
    if tests.get("total") is not None:
        parts.append(f"{tests.get('passed') or 0} passed")
        failed = int(tests.get("failed") or 0) + int(tests.get("errors") or 0)
        parts.append(f"{failed} failed")
        if tests.get("skipped"):
            parts.append(f"{tests['skipped']} skipped")
    if tests.get("linesPct") is not None:
        parts.append(f"{tests['linesPct']}% line coverage")
    return ", ".join(parts)


def build_facts(build: CiBuild) -> Dict[str, Any]:
    """Everything a notification says, from the build as it finished."""
    from . import test_reports
    from .code_scan_report import _build_link

    service = build.service
    snapshot = build.pipeline_snapshot or {}
    failed = next(
        (s for s in sorted(build.stages, key=lambda s: s.position) if s.status in ("failed", "timeout")),
        None,
    )
    reason = ""
    if build.status in ("failed", "timeout", "cancelled"):
        reason = _short((failed.error if failed is not None else "") or build.error or "")
    tests = test_reports.compact(getattr(build, "test_summary", None))
    commit = build.commit_sha or ""
    message = (build.commit_message or "").strip().splitlines()
    return {
        "service": service.name if service else "Service",
        "serviceSlug": service.slug if service else "",
        "pipeline": snapshot.get("pipelineName") or "",
        "buildId": build.id,
        "buildNumber": build.number,
        "status": build.status,
        "result": _RESULT_WORDS.get(build.status, build.status),
        "branch": build.branch or "",
        "refType": snapshot.get("refType") or "branch",
        "commit": commit,
        "commitShort": commit[:12],
        "commitMessage": _short(message[0], 120) if message else "",
        "trigger": build.trigger_type or "",
        "requestedBy": build.requested_by.username if build.requested_by else "",
        "durationSeconds": build.duration_seconds,
        "duration": _duration(build.duration_seconds),
        "finishedAt": build.finished_at.isoformat() if build.finished_at else None,
        "failedStage": failed.name if failed is not None else "",
        "reason": reason,
        "tests": tests,
        "testsLine": _tests_line(tests),
        "link": _build_link(build),
    }


def _fill(template: str, facts: Dict[str, Any]) -> str:
    """``{service}`` / ``{build}`` / ``{result}`` / ``{branch}`` / ``{status}``,
    by plain replacement — never str.format on text a user wrote."""
    values = {
        "service": facts["service"],
        "build": f"#{facts['buildNumber']}",
        "result": facts["result"],
        "status": facts["status"],
        "branch": facts["branch"],
    }
    for key, value in values.items():
        template = template.replace("{" + key + "}", str(value))
    return template


def render_email(facts: Dict[str, Any], action: Dict[str, Any]) -> Tuple[str, str, str]:
    """``(subject, text body, html body)``."""
    custom = str(action.get("subject") or "").strip()
    subject = (
        _fill(custom, facts)
        if custom
        else f"[KubeSight] {facts['service']} #{facts['buildNumber']} {facts['result']}"
        + (f" ({facts['branch']})" if facts["branch"] else "")
    )
    subject = " ".join(subject.split())[:MAX_SUBJECT_CHARS]

    rows: List[Tuple[str, str]] = [("Result", facts["result"])]
    if facts["pipeline"]:
        rows.append(("Pipeline", facts["pipeline"]))
    if facts["branch"]:
        rows.append(("Tag" if facts["refType"] == "tag" else "Branch", facts["branch"]))
    if facts["commit"]:
        rows.append(
            ("Commit", facts["commitShort"] + (f" — {facts['commitMessage']}" if facts["commitMessage"] else ""))
        )
    trigger = facts["trigger"] + (f" by {facts['requestedBy']}" if facts["requestedBy"] else "")
    if trigger:
        rows.append(("Trigger", trigger))
    if facts["duration"]:
        rows.append(("Duration", facts["duration"]))
    if facts["failedStage"]:
        rows.append(("Failed stage", facts["failedStage"]))
    if facts["reason"]:
        rows.append(("Reason", facts["reason"]))
    if facts["testsLine"]:
        rows.append(("Tests", facts["testsLine"]))

    headline = f"{facts['service']} build #{facts['buildNumber']} {facts['result']}."
    message = str(action.get("message") or "").strip()
    width = max(len(name) for name, _ in rows) + 2
    lines: List[str] = []
    if message:
        lines += [_fill(message, facts), ""]
    lines += [headline, ""]
    lines += [f"{(name + ':').ljust(width)}{value}" for name, value in rows]
    if facts["link"]:
        lines += ["", f"Open the build: {facts['link']}"]
    lines += ["", f"Sent by KubeSight CI — a post action of {facts['service']}'s pipeline."]
    text = "\n".join(lines)

    accent = "#15803d" if facts["status"] == "success" else ("#6b7280" if facts["status"] == "cancelled" else "#b91c1c")
    esc = html.escape
    table = "".join(
        f'<tr><td style="padding:4px 16px 4px 0;color:#6b7280;white-space:nowrap;vertical-align:top">{esc(name)}</td>'
        f'<td style="padding:4px 0;color:#111827">{esc(str(value))}</td></tr>'
        for name, value in rows
    )
    note = (
        f'<p style="margin:0 0 16px;color:#111827;white-space:pre-wrap">{esc(_fill(message, facts))}</p>'
        if message
        else ""
    )
    button = (
        f'<p style="margin:20px 0 0"><a href="{esc(facts["link"])}" style="display:inline-block;padding:8px 14px;'
        f'border-radius:6px;background:{accent};color:#ffffff;text-decoration:none">Open the build</a></p>'
        if facts["link"]
        else ""
    )
    html_body = (
        '<div style="font-family:Arial,Helvetica,sans-serif;font-size:14px;line-height:1.5;max-width:640px">'
        f'<div style="border-left:4px solid {accent};padding:4px 0 4px 14px;margin-bottom:16px">'
        f'<div style="font-size:16px;font-weight:bold;color:#111827">{esc(headline)}</div></div>'
        f"{note}<table style=\"border-collapse:collapse\">{table}</table>{button}"
        '<p style="margin:24px 0 0;color:#9ca3af;font-size:12px">Sent by KubeSight CI — a post action of '
        f"{esc(facts['service'])}'s pipeline.</p></div>"
    )
    return subject, text, html_body


def _slack_escape(value: Any) -> str:
    return str(value or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def webhook_payload(facts: Dict[str, Any], fmt: str) -> Dict[str, Any]:
    """The body POSTed for one format. The same facts in each."""
    title = f"{facts['service']} #{facts['buildNumber']} {facts['result']}"
    if fmt == "slack":
        icon = {"success": ":white_check_mark:", "cancelled": ":no_entry_sign:"}.get(facts["status"], ":x:")
        head = (
            f"<{facts['link']}|{_slack_escape(facts['service'])} #{facts['buildNumber']}>"
            if facts["link"]
            else f"{_slack_escape(facts['service'])} #{facts['buildNumber']}"
        )
        fields = []
        if facts["branch"]:
            fields.append({"type": "mrkdwn", "text": f"*{'Tag' if facts['refType'] == 'tag' else 'Branch'}*\n{_slack_escape(facts['branch'])}"})
        if facts["commitShort"]:
            fields.append({"type": "mrkdwn", "text": f"*Commit*\n`{facts['commitShort']}`"})
        if facts["trigger"]:
            who = f" by {facts['requestedBy']}" if facts["requestedBy"] else ""
            fields.append({"type": "mrkdwn", "text": f"*Trigger*\n{_slack_escape(facts['trigger'] + who)}"})
        if facts["duration"]:
            fields.append({"type": "mrkdwn", "text": f"*Duration*\n{facts['duration']}"})
        blocks: List[Dict[str, Any]] = [
            {"type": "section", "text": {"type": "mrkdwn", "text": f"{icon} *{head}* {facts['result']}"}},
        ]
        if fields:
            blocks.append({"type": "section", "fields": fields})
        if facts["failedStage"] or facts["reason"]:
            failure = f"*Failed stage:* {_slack_escape(facts['failedStage'])}\n" if facts["failedStage"] else ""
            blocks.append(
                {"type": "section", "text": {"type": "mrkdwn", "text": failure + _slack_escape(facts["reason"])}}
            )
        if facts["testsLine"]:
            blocks.append({"type": "context", "elements": [{"type": "mrkdwn", "text": f"Tests: {facts['testsLine']}"}]})
        return {"text": f"{icon} {title}", "blocks": blocks}

    if fmt == "teams":
        color = {"success": "Good", "cancelled": "Default"}.get(facts["status"], "Attention")
        fact_set = []
        for name, value in (
            ("Tag" if facts["refType"] == "tag" else "Branch", facts["branch"]),
            ("Commit", facts["commitShort"]),
            ("Pipeline", facts["pipeline"]),
            ("Trigger", facts["trigger"] + (f" by {facts['requestedBy']}" if facts["requestedBy"] else "")),
            ("Duration", facts["duration"]),
            ("Failed stage", facts["failedStage"]),
            ("Tests", facts["testsLine"]),
        ):
            if value:
                fact_set.append({"title": name, "value": str(value)})
        body: List[Dict[str, Any]] = [
            {"type": "TextBlock", "size": "Medium", "weight": "Bolder", "text": title, "color": color, "wrap": True},
        ]
        if fact_set:
            body.append({"type": "FactSet", "facts": fact_set})
        if facts["reason"]:
            body.append({"type": "TextBlock", "text": facts["reason"], "wrap": True, "isSubtle": True})
        card: Dict[str, Any] = {
            "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
            "type": "AdaptiveCard",
            "version": "1.4",
            "body": body,
        }
        if facts["link"]:
            card["actions"] = [{"type": "Action.OpenUrl", "title": "Open the build", "url": facts["link"]}]
        return {
            "type": "message",
            "attachments": [
                {"contentType": "application/vnd.microsoft.card.adaptive", "contentUrl": None, "content": card}
            ],
        }

    return {
        "event": "ci.build.finished",
        "service": {"name": facts["service"], "slug": facts["serviceSlug"]},
        "pipeline": facts["pipeline"],
        "build": {
            "id": facts["buildId"],
            "number": facts["buildNumber"],
            "status": facts["status"],
            "result": facts["result"],
            "branch": facts["branch"],
            "refType": facts["refType"],
            "commit": facts["commit"],
            "commitMessage": facts["commitMessage"],
            "trigger": facts["trigger"],
            "requestedBy": facts["requestedBy"] or None,
            "durationSeconds": facts["durationSeconds"],
            "finishedAt": facts["finishedAt"],
            "url": facts["link"] or None,
        },
        "failedStage": (
            {"name": facts["failedStage"], "reason": facts["reason"]} if facts["failedStage"] else None
        ),
        "reason": facts["reason"] or None,
        "tests": facts["tests"],
    }


def _send_email(build: CiBuild, action: Dict[str, Any]) -> str:
    from ...email_delivery import EmailDeliveryError, send_email, smtp_is_configured

    recipients = [r for r in action.get("recipients") or [] if r]
    if not recipients:
        raise DeliveryError("This email has no recipients.", retryable=False)
    if not smtp_is_configured():
        raise DeliveryError(
            "Email is not set up on this KubeSight: configure SMTP in Settings.", retryable=False
        )
    subject, text, html_body = render_email(build_facts(build), action)
    try:
        send_email(", ".join(recipients), subject, text, html_body=html_body)
    except EmailDeliveryError as exc:
        message = str(exc)
        retryable = not any(word in message.lower() for word in ("not configured", "recipient"))
        raise DeliveryError(message, retryable=retryable)
    return f"Email sent to {', '.join(recipients)}."


def _http_post(url: str, body: bytes, timeout: float) -> int:
    """POST ``body`` as JSON; the HTTP status. Raises DeliveryError — whose
    message never contains the URL, which is the credential."""
    request = urllib.request.Request(
        url,
        data=body,
        method="POST",
        headers={"Content-Type": "application/json", "User-Agent": "KubeSight-CI/post-actions"},
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310 - http(s) only, checked by the caller
            response.read(4096)
            return int(response.status)
    except urllib.error.HTTPError as exc:
        code = int(exc.code)
        retryable = code == 429 or code >= 500
        raise DeliveryError(f"The webhook answered HTTP {code}.", retryable=retryable)
    except urllib.error.URLError as exc:
        reason = str(getattr(exc, "reason", exc)).replace(url, "<webhook>")
        raise DeliveryError(f"Could not reach the webhook: {_short(reason, 200)}", retryable=True)
    except (TimeoutError, OSError) as exc:
        reason = str(exc).replace(url, "<webhook>") or exc.__class__.__name__
        raise DeliveryError(f"Could not reach the webhook: {_short(reason, 200)}", retryable=True)


def _send_webhook(build: CiBuild, action: Dict[str, Any]) -> str:
    from . import secrets as secrets_service

    name = str(action.get("urlSecret") or "")
    url = (secrets_service.resolve_for_build(build).get(name) or "").strip()
    if not url:
        raise DeliveryError(
            f"The CI secret '{name}' that holds this webhook's URL does not exist any more, or is empty.",
            retryable=False,
        )
    if not url.lower().startswith(("https://", "http://")):
        raise DeliveryError(f"The CI secret '{name}' does not hold an http(s) URL.", retryable=False)
    fmt = action.get("format") if action.get("format") in WEBHOOK_FORMATS else "json"
    body = json.dumps(webhook_payload(build_facts(build), fmt)).encode("utf-8")
    status = _http_post(url, body, WEBHOOK_TIMEOUT_SECONDS)
    if not 200 <= status < 300:
        raise DeliveryError(f"The webhook answered HTTP {status}.", retryable=status >= 500 or status == 429)
    return f"{_FORMAT_LABELS.get(fmt, 'Webhook')} notified (HTTP {status})."


# ---------------------------------------------------------------------------
# The API's view of a build's post actions
# ---------------------------------------------------------------------------

def serialize(build: CiBuild) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    for row in rows(build):
        state = _state(row)
        action = action_for(build, row)
        kind = state.get("kind") or action.get("type") or ""
        item: Dict[str, Any] = {
            "id": row.id,
            "index": int(row.position or 0) - POSITION_BASE,
            "name": row.name,
            "type": kind,
            "when": state.get("when") or action.get("when") or "always",
            "status": row.status,
            "phase": state.get("phase"),
            "trigger": state.get("trigger"),
            "attempts": int(state.get("attempts") or 0),
            "nextAttemptAt": state.get("nextAttemptAt") if row.status == "pending" and state.get("phase") == "queued" else None,
            "deliveredAt": state.get("deliveredAt"),
            "detail": state.get("detail"),
            "stagesResult": state.get("stagesResult"),
            "error": row.error,
            "startedAt": row.started_at.isoformat() if row.started_at else None,
            "finishedAt": row.finished_at.isoformat() if row.finished_at else None,
            "durationSeconds": row.duration_seconds,
            "logLineCount": row.log_line_count,
        }
        if kind == "email":
            item["recipients"] = list(action.get("recipients") or [])
        elif kind == "webhook":
            # The secret's NAME only: the URL is the credential.
            item["format"] = action.get("format") or "json"
            item["urlSecret"] = action.get("urlSecret") or ""
        elif kind == "commands":
            item["image"] = action.get("image")
            item["commandCount"] = len(action.get("commands") or [])
        out.append(item)
    return out
