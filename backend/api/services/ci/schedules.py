"""Scheduled builds: CRUD, preview, and firing on the CI engine's clock.

A schedule is a saved Run build — pipeline, ref, build-input values — plus a
cron expression in a named timezone. When it comes due the engine queues a
build through :func:`engine.trigger_build`, the same function the Run build
button calls, so readiness checks, parameter validation, the snapshot, the
audit entry and the dispatch wake-up are the ones every other build gets. There
is no second way to start a build here, and so no second set of rules.

Three properties the firing path is built to hold:

*Exactly once across workers.* Every gunicorn worker (and every replica) runs
the CI ticker. A due schedule is claimed by a compare-and-set UPDATE that moves
``next_run_at`` from the value that was read to the next one; the database lets
exactly one of the racing UPDATEs match. The claim is committed BEFORE the build
is triggered, which makes the failure mode at-most-once: a process that dies in
between loses one run, and never produces two.

*A missed run fires once.* The next run is computed from NOW, not from the run
that was due, so a server that was down over three nightly runs fires one build
when it comes back — not three in a row.

*A schedule never takes the engine down.* Each fire is its own transaction and
its own try. A deleted pipeline, a creator who lost the right to build, a
service that was paused: each is written to the schedule's own row as the
reason it did not run, and the pass moves on to the next one.
"""

from __future__ import annotations

import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

from sqlalchemy import update

from ...audit import log_audit
from ...db import db
from ...models_ci import CiBuild, CiPipeline, CiService
from ...models_ci_schedules import CiSchedule
from . import cron
from . import pipelines as pipelines_service

logger = logging.getLogger(__name__)

MAX_SCHEDULES_PER_SERVICE = 20
MAX_NAME_CHARS = 120
# A schedule that queues a build every minute is almost always a typo for
# "every hour", and on a shared runner fleet it is a denial of service on every
# other team. Overridable for the installation that really wants it.
MIN_INTERVAL_MINUTES = max(1, int(os.getenv("CI_SCHEDULE_MIN_INTERVAL_MINUTES", "5") or 5))
# How many due schedules one pass will fire. The rest are still due on the next
# pass, a second later — this only stops a thundering herd at 02:00 from
# holding the pass while builds that are already running wait to advance.
FIRE_PER_PASS = max(1, int(os.getenv("CI_SCHEDULE_FIRE_PER_PASS", "10") or 10))
PREVIEW_RUNS = 5

_ACTIVE_BUILD_STATUSES = ("queued", "running")


class ScheduleError(ValueError):
    """A schedule could not be saved or run. Message is user-facing."""


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _aware(value: Optional[datetime]) -> Optional[datetime]:
    if value is None:
        return None
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def _iso(value: Optional[datetime]) -> Optional[str]:
    value = _aware(value)
    return value.isoformat() if value else None


def _clean(value: Any, limit: int) -> str:
    return " ".join(str(value or "").split())[:limit]


# ---------------------------------------------------------------------------
# Reads
# ---------------------------------------------------------------------------

def get_schedule(service: CiService, schedule_id: int) -> CiSchedule:
    row = db.session.get(CiSchedule, int(schedule_id))
    if row is None or row.service_id != service.id:
        raise LookupError("Schedule not found.")
    return row


def _pipeline_problem(row: CiSchedule) -> Optional[str]:
    """Why the pipeline a schedule names cannot be used — or None."""
    if not row.pipeline_id:
        return None
    pipeline = db.session.get(CiPipeline, int(row.pipeline_id))
    if pipeline is None or pipeline.service_id != row.service_id:
        return (
            f"The pipeline this schedule runs (#{row.pipeline_id}) no longer exists. "
            "Edit the schedule and pick another pipeline."
        )
    if (pipeline.purpose or "build") != "build":
        return f"Pipeline '{pipeline.name}' is a merge check pipeline, not a build pipeline."
    return None


def schedule_to_dict(row: CiSchedule, *, now: Optional[datetime] = None) -> Dict[str, Any]:
    now = now or _now()
    expression = None
    description = ""
    upcoming: List[str] = []
    problem = ""
    try:
        expression = cron.parse(row.cron)
        description = cron.describe(expression)
        if row.enabled:
            upcoming = [_iso(at) for at in cron.upcoming(expression, now, row.timezone, 3)]
    except cron.CronError as exc:
        # Only reachable for a row written before a validation rule existed;
        # say so rather than failing the whole list.
        problem = str(exc)

    pipeline = db.session.get(CiPipeline, int(row.pipeline_id)) if row.pipeline_id else None
    last_build = row.last_build
    run_as = row.updated_by or row.created_by
    return {
        "id": row.id,
        "serviceId": row.service_id,
        "name": row.name,
        "cron": row.cron,
        "timezone": row.timezone,
        "description": description,
        "pipelineId": row.pipeline_id,
        "pipelineName": pipeline.name if pipeline else None,
        "pipelineProblem": _pipeline_problem(row),
        "branch": row.branch,
        "refType": row.ref_type or "branch",
        "variables": dict(row.variables or {}) if isinstance(row.variables, dict) else {},
        "enabled": bool(row.enabled),
        "skipIfRunning": bool(row.skip_if_running),
        "nextRunAt": _iso(row.next_run_at) if row.enabled else None,
        "upcoming": upcoming,
        "lastRunAt": _iso(row.last_run_at),
        "lastOutcome": row.last_outcome,
        "lastError": row.last_error,
        "lastBuild": (
            {
                "id": last_build.id,
                "number": last_build.number,
                "status": last_build.status,
            }
            if last_build
            else None
        ),
        "cronProblem": problem or None,
        "createdBy": row.created_by.username if row.created_by else None,
        "runsAs": run_as.username if run_as else None,
        "createdAt": _iso(row.created_at),
        "updatedAt": _iso(row.updated_at),
    }


def list_schedules(service: CiService) -> List[Dict[str, Any]]:
    now = _now()
    rows = service.schedules.order_by(CiSchedule.name).all()
    return [schedule_to_dict(row, now=now) for row in rows]


def pipeline_choices(service: CiService) -> List[Dict[str, Any]]:
    """The build pipelines a schedule may name, each with what it asks.

    Parameters are the stored definitions, not resolved: a dynamic choice
    would need a repository listing for every pipeline on every page load,
    and the schedule form takes a typed ref for those instead.
    """
    out: List[Dict[str, Any]] = []
    default = service.default_pipeline()
    for pipeline in service.build_pipelines():
        out.append(
            {
                "id": pipeline.id,
                "name": pipeline.name,
                "isDefault": default is not None and pipeline.id == default.id,
                "enabled": bool(pipeline.enabled),
                "parameters": _parameters_for(service, pipeline.id),
                "conditions": _conditions(pipeline),
            }
        )
    if not out:
        # Nothing saved: Run build uses the generated default for the
        # application type, and so does a schedule with no pipeline named.
        out.append(
            {
                "id": None,
                "name": "KubeSight default",
                "isDefault": True,
                "enabled": True,
                "parameters": _parameters_for(service, None),
                "conditions": _conditions(_parameter_pipeline(service, None)),
            }
        )
    return out


def _conditions(pipeline) -> List[Dict[str, Any]]:
    """Which stages switch on a build input, so the form can say what a value
    does: "Dependency-Check runs only when NIGHTLY_SCAN is true"."""
    out: List[Dict[str, Any]] = []
    for stage in getattr(pipeline, "stages", None) or []:
        condition = getattr(stage, "run_condition", None)
        if not getattr(stage, "enabled", True) or not isinstance(condition, dict):
            continue
        if not condition.get("variable"):
            continue
        out.append(
            {
                "stage": stage.name,
                "variable": condition.get("variable"),
                "operator": condition.get("operator") or "equals",
                "value": str(condition.get("value") or ""),
            }
        )
    return out


def _parameter_pipeline(service: CiService, pipeline_id: Optional[int]):
    """The pipeline whose declared parameters a schedule's values answer.

    The same resolution a build makes (an empty saved pipeline means the
    generated default), falling back to the stored row when the pipeline
    cannot run right now — disabled, say. A schedule for a pipeline that is
    switched off for the afternoon is still a schedule worth saving.
    """
    try:
        pipeline, _ = pipelines_service.resolve_for_build(
            service, pipeline_id, revision=service.default_branch or ""
        )
        return pipeline
    except pipelines_service.PipelineError:
        if pipeline_id:
            return db.session.get(CiPipeline, int(pipeline_id))
        return service.default_pipeline()


def _parameters_for(service: CiService, pipeline_id: Optional[int]) -> List[Dict[str, Any]]:
    pipeline = _parameter_pipeline(service, pipeline_id)
    return pipelines_service.parameter_definitions(pipeline) if pipeline is not None else []


# ---------------------------------------------------------------------------
# Preview
# ---------------------------------------------------------------------------

def preview(expression_text: Any, timezone_name: Any, *, count: int = PREVIEW_RUNS) -> Dict[str, Any]:
    """What an expression means and when it would run — for the form.

    The one implementation of cron the product has: the UI shows these words
    and these times rather than evaluating cron itself, so what the form says
    and what the engine does cannot disagree.
    """
    try:
        expression = cron.parse(expression_text)
        tz = cron.zone(timezone_name)
        _check_interval(expression)
    except cron.CronError as exc:
        return {"valid": False, "error": str(exc), "description": "", "nextRuns": []}
    runs = cron.upcoming(expression, _now(), tz, max(1, min(int(count or PREVIEW_RUNS), 10)))
    return {
        "valid": True,
        "error": None,
        "description": cron.describe(expression),
        "expression": expression.fields,
        "timezone": tz.key,
        "nextRuns": [_iso(at) for at in runs],
    }


def _check_interval(expression: cron.CronExpression) -> None:
    gap = cron.min_interval_minutes(expression)
    if gap < MIN_INTERVAL_MINUTES:
        raise cron.CronError(
            f"This would start a build every {gap} minute{'s' if gap != 1 else ''}. "
            f"The shortest interval a schedule may use is {MIN_INTERVAL_MINUTES} minutes."
        )


# ---------------------------------------------------------------------------
# Writes
# ---------------------------------------------------------------------------

def _apply(row: CiSchedule, service: CiService, payload: Dict[str, Any], *, creating: bool) -> None:
    if creating or "name" in payload:
        name = _clean(payload.get("name"), MAX_NAME_CHARS + 1)
        if not name:
            raise ScheduleError("Give the schedule a name, e.g. 'Nightly build'.")
        if len(name) > MAX_NAME_CHARS:
            raise ScheduleError(f"A schedule name is at most {MAX_NAME_CHARS} characters.")
        clash = (
            CiSchedule.query.filter(
                CiSchedule.service_id == service.id,
                db.func.lower(CiSchedule.name) == name.lower(),
                CiSchedule.id != (row.id or 0),
            ).first()
        )
        if clash is not None:
            raise ScheduleError(f"This service already has a schedule called '{clash.name}'.")
        row.name = name

    if creating or "cron" in payload:
        try:
            expression = cron.parse(payload.get("cron"))
            _check_interval(expression)
        except cron.CronError as exc:
            raise ScheduleError(str(exc))
        row.cron = expression.source

    if creating or "timezone" in payload:
        try:
            row.timezone = cron.zone(payload.get("timezone")).key
        except cron.CronError as exc:
            raise ScheduleError(str(exc))

    if "pipelineId" in payload:
        raw = payload.get("pipelineId")
        if raw in (None, "", 0):
            row.pipeline_id = None
        else:
            try:
                pipeline_id = int(raw)
            except (TypeError, ValueError):
                raise ScheduleError("Pick a pipeline from the list.")
            pipeline = db.session.get(CiPipeline, pipeline_id)
            if pipeline is None or pipeline.service_id != service.id:
                raise ScheduleError("That pipeline does not belong to this service.")
            if (pipeline.purpose or "build") != "build":
                raise ScheduleError(
                    f"Pipeline '{pipeline.name}' is a merge check pipeline. A schedule runs a build pipeline."
                )
            row.pipeline_id = pipeline.id

    if creating or "refType" in payload:
        ref_type = _clean(payload.get("refType"), 8).lower() or "branch"
        if ref_type not in ("branch", "tag"):
            raise ScheduleError("A schedule builds a branch or a tag.")
        row.ref_type = ref_type

    if creating or "branch" in payload:
        row.branch = _clean(payload.get("branch"), 255) or None
    if row.ref_type == "tag" and not row.branch:
        # A branch can fall back to the service's default; a tag has nothing
        # to fall back to.
        raise ScheduleError("Name the tag to build — a tag has no default to fall back to.")

    if creating or "variables" in payload:
        raw = payload.get("variables")
        if raw in (None, ""):
            raw = {}
        if not isinstance(raw, dict):
            raise ScheduleError("Build inputs must be an object of name to value.")
        # Strings, the way a stage receives them; a JSON true from the form is
        # the "true" a run condition compares against.
        row.variables = {
            str(key): ("true" if value is True else "false" if value is False else str(value if value is not None else ""))
            for key, value in raw.items()
        }

    for key, column in (("enabled", "enabled"), ("skipIfRunning", "skip_if_running")):
        if key in payload:
            setattr(row, column, bool(payload.get(key)))
        elif creating:
            setattr(row, column, True)

    _check_variables(row, service)


def _check_variables(row: CiSchedule, service: CiService) -> None:
    """Values checked the way a manual trigger checks them, at save time.

    The engine checks again when the build is triggered — the pipeline can
    change in between — but a schedule that would fail at 02:00 because of a
    typo made at 15:00 should fail at 15:00, in front of the person who made it.
    """
    pipeline = _parameter_pipeline(service, row.pipeline_id)
    if pipeline is None:
        return
    try:
        pipelines_service.validate_parameter_values(pipeline, row.variables or None)
    except pipelines_service.PipelineError as exc:
        raise ScheduleError(str(exc))


def _arm(row: CiSchedule, now: Optional[datetime] = None) -> None:
    """Set the next run from now — or clear it, for a schedule that is off."""
    if not row.enabled:
        row.next_run_at = None
        return
    expression = cron.parse(row.cron)
    row.next_run_at = cron.next_after(expression, now or _now(), row.timezone)


def _audit_details(row: CiSchedule, service: CiService) -> Dict[str, Any]:
    return {
        "service": service.slug,
        "schedule": row.name,
        "cron": row.cron,
        "timezone": row.timezone,
        "pipelineId": row.pipeline_id,
        "branch": row.branch,
        "refType": row.ref_type,
        "enabled": bool(row.enabled),
        "skipIfRunning": bool(row.skip_if_running),
        # Names only. A build input can carry a token somebody typed into a
        # text field, and the audit trail is not the place to keep a copy.
        "variables": sorted((row.variables or {}).keys()),
    }


def create_schedule(service: CiService, payload: Dict[str, Any], *, actor=None) -> Dict[str, Any]:
    if service.schedules.count() >= MAX_SCHEDULES_PER_SERVICE:
        raise ScheduleError(
            f"A service may have at most {MAX_SCHEDULES_PER_SERVICE} schedules."
        )
    row = CiSchedule(service_id=service.id, variables={})
    _apply(row, service, payload or {}, creating=True)
    row.created_by_user_id = getattr(actor, "id", None)
    row.updated_by_user_id = getattr(actor, "id", None)
    _arm(row)
    db.session.add(row)
    db.session.commit()
    log_audit(
        "ci_schedule_created",
        actor=actor,
        target_type="ci_schedule",
        target_id=str(row.id),
        details=_audit_details(row, service),
    )
    _wake_engine()
    return schedule_to_dict(row)


def update_schedule(row: CiSchedule, payload: Dict[str, Any], *, actor=None) -> Dict[str, Any]:
    service = row.service
    before = _audit_details(row, service)
    timing_before = (row.cron, row.timezone, bool(row.enabled))
    _apply(row, service, payload or {}, creating=False)
    row.updated_by_user_id = getattr(actor, "id", None) or row.updated_by_user_id
    # Re-armed only when WHEN changed. Saving a new branch at 01:59 must not
    # push a 02:00 run to tomorrow.
    if (row.cron, row.timezone, bool(row.enabled)) != timing_before or (
        row.enabled and row.next_run_at is None
    ):
        _arm(row)
    db.session.add(row)
    db.session.commit()
    after = _audit_details(row, service)
    log_audit(
        "ci_schedule_updated",
        actor=actor,
        target_type="ci_schedule",
        target_id=str(row.id),
        details={
            **after,
            "changed": sorted(key for key in after if after[key] != before.get(key)),
        },
    )
    _wake_engine()
    return schedule_to_dict(row)


def delete_schedule(row: CiSchedule, *, actor=None) -> None:
    service = row.service
    details = _audit_details(row, service)
    schedule_id = row.id
    db.session.delete(row)
    db.session.commit()
    log_audit(
        "ci_schedule_deleted",
        actor=actor,
        target_type="ci_schedule",
        target_id=str(schedule_id),
        details=details,
    )


def run_now(row: CiSchedule, *, actor=None) -> Dict[str, Any]:
    """Queue this schedule's build immediately, as the person asking.

    Does not move ``next_run_at``: running the nightly by hand at 15:00 does
    not cancel tonight's. It does become the schedule's last build, so
    skip-if-running sees it.
    """
    from .engine import BuildError, trigger_build

    problem = _pipeline_problem(row)
    if problem:
        raise ScheduleError(problem)
    service = row.service
    try:
        build = trigger_build(
            service,
            **_trigger_arguments(row, service),
            actor=actor,
            schedule={"id": row.id, "name": row.name, "manual": True},
        )
    except (BuildError, pipelines_service.PipelineError) as exc:
        db.session.rollback()
        raise ScheduleError(str(exc))
    row = db.session.get(CiSchedule, row.id)
    row.last_build_id = build["id"]
    row.last_run_at = _now()
    row.last_outcome = "triggered"
    row.last_error = None
    db.session.commit()
    log_audit(
        "ci_schedule_run_now",
        actor=actor,
        target_type="ci_schedule",
        target_id=str(row.id),
        details={"service": service.slug, "schedule": row.name, "buildNumber": build["number"]},
    )
    return build


def _trigger_arguments(row: CiSchedule, service: CiService) -> Dict[str, Any]:
    return {
        "branch": row.branch or service.default_branch or "main",
        "pipeline_id": row.pipeline_id or None,
        "trigger_type": "schedule",
        "variables": dict(row.variables or {}) or None,
        "ref_type": row.ref_type or "branch",
    }


def _wake_engine() -> None:
    """A new or re-timed schedule may now be due sooner than the ticker's
    current sleep; let it recompute."""
    try:
        from .ticker import wake

        wake()
    except Exception:  # pragma: no cover - a missed wake costs one idle tick
        logger.debug("Could not wake the CI engine", exc_info=True)


# ---------------------------------------------------------------------------
# Firing
# ---------------------------------------------------------------------------

def seconds_until_next_due(now: Optional[datetime] = None) -> Optional[float]:
    """How long the idle ticker may sleep before a schedule needs it, or None.

    Lets the ticker wake on the minute a schedule names instead of on its
    next idle interval, without polling any faster.
    """
    soonest = (
        db.session.query(db.func.min(CiSchedule.next_run_at))
        .filter(CiSchedule.enabled.is_(True), CiSchedule.next_run_at.isnot(None))
        .scalar()
    )
    if soonest is None:
        return None
    return max(0.0, (_aware(soonest) - (now or _now())).total_seconds())


def fire_due_schedules(now: Optional[datetime] = None) -> int:
    """Fire every schedule whose time has come. Returns how many it claimed."""
    now = now or _now()
    due: List[Tuple[int, datetime]] = (
        db.session.query(CiSchedule.id, CiSchedule.next_run_at)
        .filter(
            CiSchedule.enabled.is_(True),
            CiSchedule.next_run_at.isnot(None),
            CiSchedule.next_run_at <= now,
        )
        .order_by(CiSchedule.next_run_at, CiSchedule.id)
        .limit(FIRE_PER_PASS)
        .all()
    )
    # The read above is all this pass knows; release it so the claims below
    # are judged against the database, not against this session's snapshot.
    db.session.commit()

    claimed = 0
    for schedule_id, due_at in due:
        try:
            if _fire_one(schedule_id, due_at, now):
                claimed += 1
        except Exception:
            logger.exception("CI schedule %s could not fire", schedule_id)
            db.session.rollback()
    return claimed


def _claim(schedule_id: int, due_at: datetime, now: datetime, next_at: Optional[datetime]) -> bool:
    """Move next_run_at on from exactly the value this pass read — or lose.

    The WHERE on the old value is the whole guard: two workers that both read
    02:00 both issue this UPDATE, the first one to commit changes the row, and
    the second one's WHERE no longer matches anything.
    """
    result = db.session.execute(
        update(CiSchedule)
        .where(
            CiSchedule.id == schedule_id,
            CiSchedule.enabled.is_(True),
            CiSchedule.next_run_at == due_at,
        )
        .values(next_run_at=next_at, last_run_at=now)
        .execution_options(synchronize_session=False)
    )
    db.session.commit()
    return result.rowcount == 1


def _fire_one(schedule_id: int, due_at: datetime, now: datetime) -> bool:
    row = db.session.get(CiSchedule, schedule_id)
    if row is None:
        return False
    try:
        expression = cron.parse(row.cron)
        next_at = cron.next_after(expression, now, row.timezone)
    except cron.CronError as exc:
        # A stored expression that no longer parses cannot be re-armed. Disarm
        # it with the reason, rather than retrying it every second forever.
        if _claim(schedule_id, due_at, now, None):
            _record(schedule_id, "failed", f"The schedule could not be evaluated: {exc}")
            return True
        return False

    if not _claim(schedule_id, due_at, now, next_at):
        return False  # another worker fired it

    # Everything below runs exactly once per due time.
    db.session.expire_all()
    row = db.session.get(CiSchedule, schedule_id)
    service = row.service
    late = (now - _aware(due_at)).total_seconds()

    if service is None or service.status != "active":
        # Not audited: an archived service's nightly would write a "skipped"
        # row to the trail every night forever. The schedule's own row says it.
        _record(
            schedule_id,
            "skipped",
            f"Not run: the service is {getattr(service, 'status', 'gone')}. "
            "Scheduled builds resume when it is set back to active.",
        )
        return True

    if row.skip_if_running and row.last_build_id:
        previous = db.session.get(CiBuild, row.last_build_id)
        if previous is not None and previous.status in _ACTIVE_BUILD_STATUSES:
            reason = (
                f"Skipped: build #{previous.number} from this schedule was still "
                f"{previous.status}."
            )
            _record(schedule_id, "skipped", reason)
            _audit_fire(row, service, "ci_schedule_skipped", reason=reason)
            return True

    problem = _pipeline_problem(row)
    user, user_problem = _run_as(row)
    problem = problem or user_problem
    if problem:
        _record(schedule_id, "failed", problem)
        _audit_fire(row, service, "ci_schedule_failed", reason=problem)
        return True

    from .engine import BuildError, trigger_build

    try:
        build = trigger_build(
            service,
            **_trigger_arguments(row, service),
            actor=user,
            schedule={"id": row.id, "name": row.name},
        )
    except (BuildError, pipelines_service.PipelineError) as exc:
        db.session.rollback()
        _record(schedule_id, "failed", str(exc))
        _audit_fire(db.session.get(CiSchedule, schedule_id), service, "ci_schedule_failed", reason=str(exc))
        return True

    _record(schedule_id, "triggered", None, build_id=build["id"])
    _audit_fire(
        db.session.get(CiSchedule, schedule_id),
        service,
        "ci_schedule_fired",
        build_number=build["number"],
        late_seconds=int(late) if late >= 60 else 0,
    )
    return True


def _run_as(row, noun: str = "schedule"):
    """The person a scheduled build runs as, still allowed to — or why not.

    Whoever last saved the schedule, re-checked at every fire, the rule a
    Deploy stage's target follows: somebody who has since left, or lost the
    right to run builds, must not keep starting them every night. Webhook
    triggers apply the same rule to every delivery (``noun`` names which).
    """
    from ...access_engine import user_has_permission
    from ...auth_utils import auth_required_enabled
    from ...models import User

    user_id = row.updated_by_user_id or row.created_by_user_id
    if not auth_required_enabled():
        return (db.session.get(User, int(user_id)) if user_id else None), ""
    if not user_id:
        return None, f"Nobody owns this {noun}. Someone who can run builds must save it again."
    user = db.session.get(User, int(user_id))
    if user is None or not getattr(user, "is_active", True):
        return None, (
            f"This {noun} runs as an account that is no longer active. "
            "Someone who can run builds must save it again."
        )
    if not user_has_permission(user, "ci_builds:run"):
        return None, (
            f"This {noun} runs as {user.username}, who can no longer run builds. "
            "Someone who can must save it again."
        )
    return user, ""


def _record(
    schedule_id: int, outcome: str, error: Optional[str], *, build_id: Optional[int] = None
) -> None:
    values: Dict[str, Any] = {"last_outcome": outcome, "last_error": (error or None)}
    if build_id is not None:
        values["last_build_id"] = build_id
    db.session.execute(
        update(CiSchedule)
        .where(CiSchedule.id == schedule_id)
        .values(**values)
        .execution_options(synchronize_session=False)
    )
    db.session.commit()
    db.session.expire_all()


def _audit_fire(row: Optional[CiSchedule], service: CiService, action: str, **details: Any) -> None:
    if row is None:
        return
    log_audit(
        action,
        actor_user_id=row.updated_by_user_id or row.created_by_user_id,
        target_type="ci_schedule",
        target_id=str(row.id),
        details={
            "service": service.slug,
            "schedule": row.name,
            "cron": row.cron,
            "timezone": row.timezone,
            **{key: value for key, value in details.items() if value not in (None, "")},
        },
    )
