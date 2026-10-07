"""The promotion timetable: releases leave each environment on a schedule.

Each hop (SIT → UAT is the hop *into* UAT) may have a weekly schedule —
departure times per weekday, a cut-off, a timezone. The departures are
computed from it; nothing is stored for a departure until somebody touches it
(hold, skip, move an application off it) or it runs (``PromotionDeparture``).

At a departure's cut-off the scheduler closes it: every application eligible
for the hop at that moment becomes one release, sent as one change bundle
whose window opens at the departure time. A cluster that needs approval waits
for its approvers; one that needs none is approved on submission. Either way
the bundle executor deploys at departure — with the ladder and the registry
checked again — so there is no second way to deploy. Applications that were
not eligible by the cut-off simply ride the next departure.

A hop without a schedule is "on demand": it always has an open release that
someone promotes by hand. Promoting a scheduled departure early does the same
and closes the departure.

Scheduled releases run as the person who saved the schedule (they must still
be able to deploy), the way a CI Deploy stage runs as whoever authorised it.
"""

from __future__ import annotations

import logging
import os
import re
from datetime import date, datetime, time as dtime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from ..audit import log_audit
from ..db import db
from ..models_promotion import PromotionDeparture, PromotionEnvironment, PromotionRelease
from . import promotion_service as core
from .promotion_service import PromotionError

logger = logging.getLogger(__name__)

WEEKDAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
DEFAULT_TIMEZONE = os.getenv("PROMOTION_TIMEZONE", "UTC")
DEFAULT_CUTOFF = 15
MAX_PER_DAY = 24
_TIME_RE = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)$")

# How far the board looks.
BOARD_PAST = timedelta(hours=30)
BOARD_AHEAD = timedelta(days=4)
GRAPH_PAST = timedelta(days=3)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _aware(dt: Optional[datetime]) -> Optional[datetime]:
    if dt is None:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _iso(dt: Optional[datetime]) -> Optional[str]:
    dt = _aware(dt)
    return dt.isoformat() if dt else None


# ---------------------------------------------------------------------------
# Schedules
# ---------------------------------------------------------------------------

def _zone(name: Optional[str]) -> ZoneInfo:
    for candidate in (name, DEFAULT_TIMEZONE, "UTC"):
        if not candidate:
            continue
        try:
            return ZoneInfo(candidate)
        except (ZoneInfoNotFoundError, ValueError):
            continue
    return ZoneInfo("UTC")


def schedule_of(env: PromotionEnvironment) -> Optional[Dict[str, Any]]:
    """The hop's schedule when it is switched on, else None (on demand)."""
    sched = env.schedule or None
    if not sched or not sched.get("enabled"):
        return None
    if not any(sched.get("days", {}).get(d) for d in WEEKDAYS):
        return None
    return sched


def clean_schedule(data: Dict[str, Any]) -> Dict[str, Any]:
    days_in = data.get("days") or {}
    if not isinstance(days_in, dict):
        raise PromotionError("days must map a weekday to a list of times.")
    days: Dict[str, List[str]] = {}
    for day in WEEKDAYS:
        raw = days_in.get(day) or []
        if not isinstance(raw, list):
            raise PromotionError(f"{day}: give a list of times.")
        times = sorted({str(t).strip() for t in raw if str(t).strip()})
        for value in times:
            if not _TIME_RE.match(value):
                raise PromotionError(f"'{value}' is not a time — use HH:MM, 24-hour.")
        if len(times) > MAX_PER_DAY:
            raise PromotionError(f"At most {MAX_PER_DAY} departures a day.")
        days[day] = times
    try:
        cutoff = int(data.get("cutoffMinutes", DEFAULT_CUTOFF))
    except (TypeError, ValueError):
        raise PromotionError("Cut-off must be a number of minutes.")
    if cutoff < 0 or cutoff > 12 * 60:
        raise PromotionError("Cut-off must be between 0 and 720 minutes before departure.")
    tz_name = str(data.get("timezone") or DEFAULT_TIMEZONE).strip()
    try:
        ZoneInfo(tz_name)
    except (ZoneInfoNotFoundError, ValueError):
        raise PromotionError(f"Unknown timezone '{tz_name}'.")
    return {"enabled": bool(data.get("enabled", True)), "days": days, "cutoffMinutes": cutoff, "timezone": tz_name}


def set_schedule(user, env_id: int, data: Dict[str, Any]) -> Dict[str, Any]:
    from ..access_engine import user_has_permission

    env = db.session.get(PromotionEnvironment, env_id)
    if env is None:
        raise PromotionError("Environment not found.", 404)
    if core.previous_environment(env) is None:
        raise PromotionError("The entry environment has nothing below it to release from.")
    sched = clean_schedule(data)
    if sched["enabled"]:
        # The schedule deploys as this person, so they must be able to.
        if user is not None and not user_has_permission(user, "apps:deploy"):
            raise PromotionError("Switching on a schedule needs the permission to deploy — releases run as you.", 403)
        env.schedule_owner_id = getattr(user, "id", None)
        env.schedule_owner = core._actor_name(user)
    env.schedule = sched
    db.session.commit()
    log_audit(
        "promotion_schedule_updated",
        actor=user,
        target_type="promotion_environment",
        target_id=str(env.id),
        details={"environment": env.name, **sched},
    )
    return serialize_schedule(env)


def serialize_schedule(env: PromotionEnvironment) -> Dict[str, Any]:
    sched = env.schedule or {}
    return {
        "enabled": bool(sched.get("enabled")) and schedule_of(env) is not None,
        "days": {d: list((sched.get("days") or {}).get(d) or []) for d in WEEKDAYS},
        "cutoffMinutes": int(sched.get("cutoffMinutes", DEFAULT_CUTOFF)),
        "timezone": sched.get("timezone") or DEFAULT_TIMEZONE,
        "owner": env.schedule_owner,
    }


def departures_between(env: PromotionEnvironment, start: datetime, end: datetime) -> List[Dict[str, Any]]:
    """Every scheduled departure of the hop into ``env`` in [start, end)."""
    sched = schedule_of(env)
    if sched is None:
        return []
    tz = _zone(sched.get("timezone"))
    cutoff = timedelta(minutes=int(sched.get("cutoffMinutes", DEFAULT_CUTOFF)))
    out = []
    day = start.astimezone(tz).date() - timedelta(days=1)
    last = end.astimezone(tz).date() + timedelta(days=1)
    while day <= last:
        times = (sched.get("days") or {}).get(WEEKDAYS[day.weekday()]) or []
        for index, value in enumerate(times, start=1):
            hh, mm = (int(x) for x in value.split(":"))
            local = datetime.combine(day, dtime(hh, mm), tzinfo=tz)
            at = local.astimezone(timezone.utc)
            if start <= at < end:
                out.append(
                    {
                        "departsAt": at,
                        "cutoffAt": at - cutoff,
                        "local": local,
                        "code": code_for(env, local, str(index)),
                        "version": f"{local:%Y.%m.%d}.{index}",
                    }
                )
        day += timedelta(days=1)
    out.sort(key=lambda d: d["departsAt"])
    return out


def code_for(env: PromotionEnvironment, local: datetime, suffix: str) -> str:
    prefix = re.sub(r"[^A-Z0-9]", "", env.key.upper())[:4] or "REL"
    return f"{prefix}-{local:%m%d}-{suffix}"


def manual_identity(env: PromotionEnvironment) -> Tuple[str, str]:
    """Code and version of a release promoted by hand (not on a departure)."""
    tz = _zone((env.schedule or {}).get("timezone"))
    local = _now().astimezone(tz)
    start = datetime.combine(local.date(), dtime(0, 0), tzinfo=tz).astimezone(timezone.utc)
    n = (
        PromotionRelease.query.filter(
            PromotionRelease.environment_id == env.id,
            PromotionRelease.departs_at.is_(None),
            PromotionRelease.created_at >= start,
        ).count()
        + 1
    )
    return code_for(env, local, f"M{n}"), f"{local:%Y.%m.%d}.m{n}"


def slot_identity(env: PromotionEnvironment, departs_at: datetime) -> Optional[Dict[str, Any]]:
    """The scheduled departure at exactly ``departs_at``, if the schedule has one."""
    departs_at = _aware(departs_at)
    for dep in departures_between(env, departs_at - timedelta(minutes=1), departs_at + timedelta(minutes=1)):
        if dep["departsAt"] == departs_at:
            return dep
    return None


# ---------------------------------------------------------------------------
# Departures people touch
# ---------------------------------------------------------------------------

def _departure_row(env_id: int, departs_at: datetime, create: bool = True) -> Optional[PromotionDeparture]:
    departs_at = _aware(departs_at)
    row = PromotionDeparture.query.filter_by(environment_id=env_id, departs_at=departs_at).first()
    if row is None:
        # SQLite drops the timezone: compare on the naive UTC value too.
        row = PromotionDeparture.query.filter_by(
            environment_id=env_id, departs_at=departs_at.replace(tzinfo=None)
        ).first()
    if row is None and create:
        row = PromotionDeparture(environment_id=env_id, departs_at=departs_at, state="open", excluded=[])
        db.session.add(row)
        db.session.flush()
    return row


def _parse_slot(env_id: Any, departs_at: Any) -> Tuple[PromotionEnvironment, datetime]:
    env = db.session.get(PromotionEnvironment, int(env_id or 0))
    if env is None:
        raise PromotionError("Environment not found.", 404)
    try:
        at = datetime.fromisoformat(str(departs_at).replace("Z", "+00:00"))
    except ValueError:
        raise PromotionError("departsAt must be an ISO date-time.")
    at = _aware(at).astimezone(timezone.utc)
    if slot_identity(env, at) is None:
        raise PromotionError("There is no departure at that time on the schedule.", 404)
    return env, at


def _editable(row: PromotionDeparture) -> None:
    if row.state == "closed":
        raise PromotionError("That release has already closed — change it in its change bundle.", 409)


def set_departure_state(user, env_id: Any, departs_at: Any, state: str) -> Dict[str, Any]:
    if state not in ("open", "held", "skipped"):
        raise PromotionError("State must be open, held or skipped.")
    env, at = _parse_slot(env_id, departs_at)
    row = _departure_row(env.id, at)
    _editable(row)
    row.state = state
    row.updated_by = core._actor_name(user)
    db.session.commit()
    log_audit(
        f"promotion_departure_{state}",
        actor=user,
        target_type="promotion_environment",
        target_id=str(env.id),
        details={"environment": env.name, "departsAt": _iso(at)},
    )
    return {"state": row.state, "departsAt": _iso(at), "environmentId": env.id}


def set_excluded(user, env_id: Any, departs_at: Any, repository: str, excluded: bool) -> Dict[str, Any]:
    env, at = _parse_slot(env_id, departs_at)
    repository = (repository or "").strip()
    if not repository:
        raise PromotionError("Name the application (its image repository).")
    row = _departure_row(env.id, at)
    _editable(row)
    current = list(row.excluded or [])
    if excluded and repository not in current:
        current.append(repository)
    if not excluded and repository in current:
        current.remove(repository)
    row.excluded = current
    row.updated_by = core._actor_name(user)
    db.session.commit()
    return {"excluded": current, "departsAt": _iso(at), "environmentId": env.id}


def link_release(env: PromotionEnvironment, departs_at: datetime, release_id: int) -> None:
    row = _departure_row(env.id, departs_at)
    row.state = "closed"
    row.release_id = release_id
    db.session.flush()


# ---------------------------------------------------------------------------
# Closing a departure (the scheduler)
# ---------------------------------------------------------------------------

def _owner(env: PromotionEnvironment):
    from ..access_engine import user_has_permission
    from ..models import User

    if not env.schedule_owner_id:
        return None, "Nobody owns this schedule — save it again to run releases as you."
    user = db.session.get(User, int(env.schedule_owner_id))
    if user is None or not getattr(user, "is_active", True):
        return None, f"{env.schedule_owner or 'The schedule owner'} can no longer sign in — someone must save the schedule again."
    if not user_has_permission(user, "apps:deploy"):
        return None, f"{env.schedule_owner or 'The schedule owner'} can no longer deploy — someone who can must save the schedule again."
    return user, ""


def eligible_items(env: PromotionEnvironment, excluded: List[str], overview: Optional[Dict[str, Any]] = None) -> List[Dict[str, Any]]:
    """What a release into ``env`` takes right now: every application whose hop
    is ready (eligible), minus the ones moved off this departure."""
    overview = overview or core.overview()
    ids = [e["id"] for e in overview["environments"]]
    if env.id not in ids or ids.index(env.id) == 0:
        return []
    hop = ids.index(env.id) - 1
    items = []
    for app in overview["apps"]:
        step = app["steps"][hop]
        if step["state"] != "ready" or app["repository"] in excluded:
            continue
        targets = [
            {k: t[k] for k in ("clusterId", "namespace", "kind", "name", "container")}
            for t in step["targets"]
            if t["image"] != step["image"]
        ]
        if targets:
            items.append({"name": app["name"], "image": step["image"], "targets": targets})
    return items


def close_departure(env: PromotionEnvironment, dep: Dict[str, Any], *, user=None, items=None) -> Optional[Dict[str, Any]]:
    """Turn one departure into its release. Idempotent: a closed departure is
    left alone. Returns the release, or None when nothing was eligible."""
    from .promotion_releases import create_release

    row = _departure_row(env.id, dep["departsAt"])
    if row.state == "closed":
        return None
    if row.state in ("held", "skipped"):
        return None
    actor = user
    if actor is None:
        actor, problem = _owner(env)
        if actor is None:
            row.note = problem
            db.session.commit()
            core.record_event("blocked", None, path="schedule", message=problem, environment=env, images=[])
            return None
    items = items if items is not None else eligible_items(env, list(row.excluded or []))
    if not items:
        row.state = "closed"
        row.note = "Nothing was eligible at the cut-off."
        db.session.commit()
        return None
    release = create_release(
        actor,
        environment_id=env.id,
        items=items,
        name=f"{env.name} · {dep['local']:%a %d %b %H:%M}",
        code=dep["code"],
        version=dep["version"],
        departs_at=dep["departsAt"],
        slot=dep["departsAt"],
    )
    return release


def run_due(now: Optional[datetime] = None) -> Dict[str, Any]:
    """The scheduler's tick: close every departure whose cut-off has passed
    (within the last hour, so a restart catches up without replaying a week)."""
    now = now or _now()
    closed, skipped = [], []
    for env in core.ladder():
        if schedule_of(env) is None or core.previous_environment(env) is None:
            continue
        for dep in departures_between(env, now - timedelta(hours=1), now + timedelta(days=1)):
            if not (dep["cutoffAt"] <= now and dep["departsAt"] > now - timedelta(minutes=30)):
                continue
            try:
                release = close_departure(env, dep)
            except PromotionError as exc:
                logger.warning("Could not close %s: %s", dep["code"], exc)
                skipped.append(dep["code"])
                db.session.rollback()
                continue
            if release:
                closed.append(release["code"])
    return {"closed": closed, "skipped": skipped}


_last_tick = 0.0


def tick() -> None:
    """Self-throttled scheduler hook (every 30 s at most)."""
    import time

    global _last_tick
    if time.time() - _last_tick < 30:
        return
    _last_tick = time.time()
    try:
        if any(schedule_of(env) for env in core.ladder()):
            run_due()
    except Exception:  # noqa: BLE001
        logger.exception("Promotion timetable tick failed")
        db.session.rollback()


# ---------------------------------------------------------------------------
# The board
# ---------------------------------------------------------------------------

def _required_approvals(env: PromotionEnvironment) -> int:
    from .deployment_request_service import cluster_required_approvals

    clusters = {b.cluster_id for b in env.bindings}
    required = 0
    for cluster_id in clusters:
        try:
            required = max(required, int(cluster_required_approvals(cluster_id) or 0))
        except Exception:  # noqa: BLE001
            continue
    return required


def _release_state(row: PromotionRelease, serialized: Dict[str, Any], bundles: Dict[int, Any], watches: Dict[int, List[str]], now: datetime) -> Dict[str, Any]:
    """Board status of a release, from its bundles and their rollout watches."""
    approvals = {"required": 0, "obtained": 0, "approver": None}
    state = None
    rows = [bundles[i] for i in (row.bundle_ids or []) if i in bundles]
    for bundle in rows:
        from .change_bundle_service import _vote_tally

        got, _declines = _vote_tally(bundle)
        approvals["required"] = max(approvals["required"], int(bundle.required_approvals or 0))
        approvals["obtained"] = max(approvals["obtained"], got if bundle.status == "pending_approval" else int(bundle.required_approvals or 0))
        if bundle.approved_by is not None:
            approvals["approver"] = bundle.approved_by.full_name or bundle.approved_by.username
    statuses = {b.status for b in rows}
    failed_watch = any("failed" in watches.get(b.id, []) for b in rows)
    watching = any("watching" in watches.get(b.id, []) for b in rows)
    departs = _aware(row.departs_at)
    if statuses & {"rejected"}:
        state = "rejected"
    elif statuses & {"failed", "partially_failed"} or failed_watch:
        state = "failed"
    elif statuses & {"expired"}:
        state = "expired"
    elif statuses & {"pending_approval"}:
        state = "exception" if row.kind == "exception" else "approval"
    elif statuses & {"approved", "scheduled"}:
        state = "ready" if departs and departs > now else "promoting"
    elif statuses & {"deploying"} or watching:
        state = "promoting"
    if state is None:
        state = {
            "applied": "promoted",
            "pending_approval": "approval",
            "partial": "failed",
            "refused": "refused",
        }.get(serialized["status"], serialized["status"])
    return {"status": state, "approval": approvals}


def timetable(now: Optional[datetime] = None) -> Dict[str, Any]:
    from ..models import BundleRolloutWatch, ChangeBundle
    from .promotion_releases import serialize_release

    now = now or _now()
    rungs = core.ladder()
    hops = []
    departures: List[Dict[str, Any]] = []
    for index in range(1, len(rungs)):
        env, prev = rungs[index], rungs[index - 1]
        sched = schedule_of(env)
        required = _required_approvals(env)
        hops.append(
            {
                "fromEnvironmentId": prev.id,
                "toEnvironmentId": env.id,
                "schedule": serialize_schedule(env),
                "requiredApprovals": required,
            }
        )
        rows = {
            _aware(r.departs_at): r
            for r in PromotionDeparture.query.filter(
                PromotionDeparture.environment_id == env.id,
                PromotionDeparture.departs_at >= (now - BOARD_PAST).replace(tzinfo=None),
            ).all()
        }
        if sched is None:
            departures.append(
                {
                    "key": f"{env.id}:ondemand",
                    "kind": "ondemand",
                    "code": f"{code_for(env, now, 'NEXT').rsplit('-', 2)[0]}-NEXT",
                    "fromEnvironmentId": prev.id,
                    "toEnvironmentId": env.id,
                    "departsAt": None,
                    "cutoffAt": None,
                    "status": "boarding",
                    "excluded": [],
                    "approval": {"required": required, "obtained": 0, "approver": None},
                }
            )
            continue
        boarding_set = False
        for dep in departures_between(env, now - BOARD_PAST, now + BOARD_AHEAD):
            row = rows.get(dep["departsAt"])
            if row is not None and row.state == "closed" and row.release_id:
                continue  # shown as its release below
            if dep["departsAt"] <= now and not (row and row.state in ("skipped", "held")):
                if row is not None and row.state == "closed":
                    status = "empty"
                else:
                    continue  # the past, nothing ran: nothing to show
            elif row is not None and row.state in ("held", "skipped"):
                status = row.state
            elif row is not None and row.state == "closed":
                status = "empty"
            elif not boarding_set:
                status = "boarding"
                boarding_set = True
            else:
                status = "scheduled"
            departures.append(
                {
                    "key": f"{env.id}:{_iso(dep['departsAt'])}",
                    "kind": "scheduled",
                    "code": dep["code"],
                    "version": dep["version"],
                    "fromEnvironmentId": prev.id,
                    "toEnvironmentId": env.id,
                    "departsAt": _iso(dep["departsAt"]),
                    "cutoffAt": _iso(dep["cutoffAt"]),
                    "status": status,
                    "excluded": list((row.excluded if row else None) or []),
                    "note": row.note if row else None,
                    "approval": {"required": required, "obtained": 0, "approver": None},
                }
            )

    releases = (
        PromotionRelease.query.filter(PromotionRelease.created_at >= (now - GRAPH_PAST).replace(tzinfo=None))
        .order_by(PromotionRelease.created_at.desc())
        .limit(300)
        .all()
    )
    bundle_ids = sorted({i for r in releases for i in (r.bundle_ids or [])})
    bundles = {b.id: b for b in ChangeBundle.query.filter(ChangeBundle.id.in_(bundle_ids)).all()} if bundle_ids else {}
    watches: Dict[int, List[str]] = {}
    if bundle_ids:
        for w in BundleRolloutWatch.query.filter(BundleRolloutWatch.bundle_id.in_(bundle_ids)).all():
            watches.setdefault(w.bundle_id, []).append(w.status)
    states = {b.id: b.status for b in bundles.values()}
    for row in releases:
        serialized = serialize_release(row, states)
        state = _release_state(row, serialized, bundles, watches, now)
        departures.append(
            {
                "key": f"release:{row.id}",
                "kind": "release",
                "code": row.code or f"REL-{row.id}",
                "version": row.version,
                "fromEnvironmentId": next((e.id for e in rungs if e.name == row.from_environment_name), None),
                "toEnvironmentId": row.environment_id,
                "departsAt": _iso(row.departs_at) or serialized["createdAt"],
                "cutoffAt": None,
                "createdAt": serialized["createdAt"],
                "status": state["status"],
                "approval": state["approval"],
                "release": serialized,
                "excluded": [],
            }
        )

    def sort_key(d):
        return (d["departsAt"] is None, d["departsAt"] or "")

    departures.sort(key=sort_key)
    try:
        from .change_bundle_executor import _watch_settings

        timeout_min, rollback = _watch_settings()
    except Exception:  # noqa: BLE001
        timeout_min, rollback = 15, True
    return {
        "rollback": {"automatic": bool(rollback), "timeoutMinutes": int(timeout_min)},
        "now": _iso(now),
        "timezone": next((h["schedule"]["timezone"] for h in hops if h["schedule"]["enabled"]), DEFAULT_TIMEZONE),
        "hops": hops,
        "departures": departures,
    }
