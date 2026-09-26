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

# The simulated runner executes no command and reports every stage green. That
# is exactly right for mock mode (demos, the test suite) and exactly wrong for
# an installation that talks to real clusters, where a "successful" build it ran
# would be a lie a deploy could act on.
MOCK_RUNNER_TYPE = "mock"
MOCK_RUNNER_REFUSED = (
    "The simulated 'mock' runner runs no commands, so it is never used while "
    "KubeSight is connected to real clusters."
)


def mock_runner_permitted() -> bool:
    """Whether the simulated runner may take work in this installation.

    Only in mock mode. Fails closed: if the mode cannot be determined, a build
    queues with an honest reason rather than going fake-green.
    """
    try:
        from ...k8s_provider import is_real_mode_enabled

        return not is_real_mode_enabled()
    except Exception:  # pragma: no cover - defensive
        return False


def runner_usable_here(runner: CiRunner) -> bool:
    """False for a runner this installation must never dispatch to."""
    if runner.runner_type == MOCK_RUNNER_TYPE:
        return mock_runner_permitted()
    return True


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
        if row.runner_type in shipped
        and heartbeat_fresh(row)
        and runner_usable_here(row)
    ]


def _has_capacity(runner: CiRunner) -> bool:
    return int(runner.current_load or 0) < max(1, int(runner.max_concurrent or 1))


def runner_provides(runner: CiRunner) -> set:
    """What a runner offers a stage: its capabilities AND its routing labels.

    Operators tag agents with labels ("mac-mini-2", "gpu") precisely so a stage
    can route to them; matching capabilities alone made those labels dead.
    """
    return {
        str(item).strip().lower()
        for item in list(runner.capabilities or []) + list(runner.labels or [])
        if str(item).strip()
    }


def select_runner(requirements: StageRequirements) -> Selection:
    """Pick the least-loaded compatible runner with free capacity.

    Ordering is by load ratio, then by least-recently-assigned, so work spreads
    across a fleet instead of piling onto whichever runner sorts first.
    """
    if requirements.conflict:
        return Selection(None, requirements.conflict)
    candidates = eligible_runners()
    if not candidates:
        reason = "No CI runner is online."
        if not mock_runner_permitted():
            reason += (
                " The simulated mock runner is never used while KubeSight is "
                "connected to real clusters: enable the Kubernetes runner or "
                "register an agent."
            )
        return Selection(None, reason)

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
        if capabilities_cover(runner_provides(runner), requirements.labels)
    ]
    if not capable:
        missing = _missing_capabilities(typed, requirements.labels)
        detail = ", ".join(sorted(missing)) if missing else "the required capabilities"
        if missing:
            return Selection(None, f"No online runner provides: {detail}.")
        needed = ", ".join(
            sorted({str(l).strip().lower() for l in requirements.labels if str(l).strip()})
        )
        return Selection(
            None,
            f"No single online runner provides everything this build's stages "
            f"need together: {needed}.",
        )

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
        provided.update(runner_provides(runner))
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
            if runner.enabled
            and get_adapter(runner.runner_type) is not None
            and runner_usable_here(runner)
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


def requirements_for_build(
    stage_definitions: List[dict], stage_is_skipped=None
) -> StageRequirements:
    """What the WHOLE build needs from the one runner it is assigned.

    A build runs on a single runner (``build.runner_id`` is set once, at
    dispatch), so choosing from the first stage alone let a checkout stage with
    no labels land an Android build on a box without the Android SDK. The union
    of every stage that will actually run is the honest requirement.

    ``stage_is_skipped(definition)`` lets the engine drop stages it already
    knows will not run (a ``when`` clause that is false for this build, a type
    nothing executes), so their labels do not block the build.
    """
    labels: List[str] = []
    seen = set()
    runner_types = []
    resources: dict = {}
    image = None
    for definition in stage_definitions or []:
        definition = definition or {}
        if stage_is_skipped is not None and stage_is_skipped(definition):
            continue
        for label in definition.get("runnerLabels") or ():
            key = str(label).strip().lower()
            if key and key not in seen:
                seen.add(key)
                labels.append(key)
        pinned = definition.get("runnerType") or None
        if pinned and pinned not in runner_types:
            runner_types.append(pinned)
        if image is None and definition.get("image"):
            image = definition.get("image")
        if not resources and definition.get("resources"):
            resources = definition.get("resources") or {}
    conflict = None
    if len(runner_types) > 1:
        conflict = (
            "This build's stages pin different runner types ("
            + ", ".join(sorted(runner_types))
            + "), but a build runs on one runner. Pin them to the same type, "
            "or use labels instead."
        )
    return StageRequirements(
        runner_type=runner_types[0] if len(runner_types) == 1 else None,
        labels=tuple(labels),
        image=image,
        resources=resources,
        conflict=conflict,
    )


def requirements_for(stage_definition: dict) -> StageRequirements:
    """Build :class:`StageRequirements` from a snapshotted stage dict."""
    return StageRequirements(
        runner_type=(stage_definition.get("runnerType") or None),
        labels=tuple(stage_definition.get("runnerLabels") or ()),
        image=stage_definition.get("image") or None,
        resources=stage_definition.get("resources") or {},
    )
