"""Runner selection.

Given what a stage needs, pick the runner that should execute it — or explain
why none can. The explanation matters as much as the choice: a build that sits
queued must say "no online runner has capability 'macos'", not just wait.

Selection is capability-based, never machine-based. A stage declares labels; a
runner advertises capabilities; a runner is a candidate when its capabilities
cover the labels. That is what lets the same iOS pipeline run on whichever Mac
happens to be online.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import List, Optional, Tuple

from ...db import db
from ...models_ci import CiRunner
from .runners.base import StageRequirements, available_runner_types, capabilities_cover

# A runner that has not checked in within this window is not eligible, whatever
# its stored status says. Runners KubeSight manages in-process are exempt —
# they have nothing to heartbeat from.
HEARTBEAT_GRACE_SECONDS = 120
_SELF_MANAGED_TYPES = frozenset({"mock", "kubernetes"})


@dataclass
class Selection:
    runner: Optional[CiRunner]
    reason: str = ""

    @property
    def ok(self) -> bool:
        return self.runner is not None


def _aware(value: Optional[datetime]) -> Optional[datetime]:
    if value is None:
        return None
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def heartbeat_fresh(runner: CiRunner) -> bool:
    if runner.runner_type in _SELF_MANAGED_TYPES:
        return True
    last = _aware(runner.last_heartbeat_at)
    if last is None:
        return False
    return last >= datetime.now(timezone.utc) - timedelta(seconds=HEARTBEAT_GRACE_SECONDS)


def eligible_runners() -> List[CiRunner]:
    """Runners that could take work right now, before stage requirements."""
    rows = (
        CiRunner.query.filter(
            CiRunner.enabled.is_(True), CiRunner.status == "online"
        )
        .order_by(CiRunner.id.asc())
        .all()
    )
    shipped = set(available_runner_types())
    return [
        row
        for row in rows
        # A runner whose adapter has not shipped yet must never be assigned to;
        # the build would be accepted and then stall with nothing driving it.
        if row.runner_type in shipped and heartbeat_fresh(row)
    ]


def _has_capacity(runner: CiRunner) -> bool:
    return int(runner.current_load or 0) < max(1, int(runner.max_concurrent or 1))


def select_runner(requirements: StageRequirements) -> Selection:
    """Pick the least-loaded compatible runner with free capacity.

    Ordering is by load ratio, then by least-recently-assigned, so work spreads
    across a fleet instead of piling onto whichever runner sorts first.
    """
    candidates = eligible_runners()
    if not candidates:
        return Selection(None, "No CI runner is online.")

    typed = [
        runner
        for runner in candidates
        if not requirements.runner_type or runner.runner_type == requirements.runner_type
    ]
    if not typed:
        return Selection(
            None,
            f"No online runner of type '{requirements.runner_type}'.",
        )

    capable = [
        runner
        for runner in typed
        if capabilities_cover(runner.capabilities, requirements.labels)
    ]
    if not capable:
        missing = _missing_capabilities(typed, requirements.labels)
        detail = ", ".join(sorted(missing)) if missing else "the required capabilities"
        return Selection(None, f"No online runner provides: {detail}.")

    free = [runner for runner in capable if _has_capacity(runner)]
    if not free:
        return Selection(None, "Every compatible runner is at capacity.")

    free.sort(
        key=lambda r: (
            int(r.current_load or 0) / max(1, int(r.max_concurrent or 1)),
            _aware(r.last_assigned_at) or datetime.min.replace(tzinfo=timezone.utc),
            r.id,
        )
    )
    return Selection(free[0])


def _missing_capabilities(runners: List[CiRunner], labels) -> set:
    """Labels that no candidate runner advertises — the useful half of 'why not'."""
    needed = {str(l).strip().lower() for l in (labels or []) if str(l).strip()}
    provided = set()
    for runner in runners:
        provided.update(
            str(c).strip().lower() for c in (runner.capabilities or []) if str(c).strip()
        )
    return needed - provided


def acquire_slot(runner: CiRunner, *, commit: bool = False) -> None:
    runner.current_load = int(runner.current_load or 0) + 1
    runner.last_assigned_at = datetime.now(timezone.utc)
    db.session.add(runner)
    if commit:
        db.session.commit()


def release_slot(runner_id: Optional[int], *, commit: bool = False) -> None:
    if not runner_id:
        return
    runner = db.session.get(CiRunner, runner_id)
    if runner is None:
        return
    runner.current_load = max(0, int(runner.current_load or 0) - 1)
    db.session.add(runner)
    if commit:
        db.session.commit()


def sync_builtin_runner_statuses(*, commit: bool = True) -> None:
    """Derive online/offline for the runners KubeSight manages in-process.

    They have no agent to heartbeat, so status is a pure function of "enabled
    and an adapter is registered". Runs on the engine tick; the PUT route
    deliberately refuses manual status writes on builtin rows for this reason.
    """
    from .runners.base import get_adapter

    changed = False
    for runner in CiRunner.query.filter(CiRunner.is_builtin.is_(True)).all():
        if runner.runner_type not in _SELF_MANAGED_TYPES:
            continue
        desired = (
            "online"
            if runner.enabled and get_adapter(runner.runner_type) is not None
            else "offline"
        )
        if runner.status != desired:
            runner.status = desired
            db.session.add(runner)
            changed = True
    if changed and commit:
        db.session.commit()


def recompute_loads(*, commit: bool = True) -> None:
    """Rebuild ``current_load`` from the builds actually running.

    A crash between "acquire slot" and "record the build" leaks a slot; this
    runs on the scheduler tick so a leak self-heals within one interval rather
    than permanently shrinking the fleet's capacity.
    """
    from ...models_ci import CiBuild

    counts = dict(
        db.session.query(CiBuild.runner_id, db.func.count(CiBuild.id))
        .filter(CiBuild.status == "running", CiBuild.runner_id.isnot(None))
        .group_by(CiBuild.runner_id)
        .all()
    )
    changed = False
    for runner in CiRunner.query.all():
        actual = int(counts.get(runner.id, 0))
        if int(runner.current_load or 0) != actual:
            runner.current_load = actual
            db.session.add(runner)
            changed = True
    if changed and commit:
        db.session.commit()


def requirements_for(stage_definition: dict) -> StageRequirements:
    """Build :class:`StageRequirements` from a snapshotted stage dict."""
    return StageRequirements(
        runner_type=(stage_definition.get("runnerType") or None),
        labels=tuple(stage_definition.get("runnerLabels") or ()),
        image=stage_definition.get("image") or None,
        resources=stage_definition.get("resources") or {},
    )
