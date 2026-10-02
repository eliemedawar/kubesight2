"""The CI engine's own clock.

CI used to advance on the shared alert-policy tick: every 15 seconds, and only
after component health, alert evaluation, ticketing syncs, deploy automation and
mobile builds had each had their turn on that thread. A build therefore waited
seconds to be picked up and seconds again at every stage boundary, which reads
as a slow CI even when the actual work is fast.

So CI gets its own loop, with two properties the shared tick cannot have:

*Fast when there is work.* One second between passes while anything is queued or
running, and a long idle interval otherwise — the pass itself no-ops on a single
COUNT when there is nothing to do, so the cost of being responsive is one cheap
query a second during a build and nothing at all between builds.

*Woken, not waited on.* :func:`wake` releases the loop immediately, so
triggering a build, cancelling one, or an agent reporting a stage's outcome
starts the next pass in milliseconds instead of at the next interval. The
interval is what catches everything that has no event to announce it — a pod
that finished, an agent that went quiet.
"""

from __future__ import annotations

import logging
import os
import threading
from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:
    from flask import Flask

logger = logging.getLogger(__name__)

# While builds are active. One second is the resolution the UI polls at, so
# anything finer would only be visible to the database.
DEFAULT_TICK_SECONDS = 1.0
# While nothing is queued or running. Long on purpose: every path that creates
# work calls wake(), so this is a safety net, not the latency anyone waits on.
DEFAULT_IDLE_SECONDS = 15.0

_wake = threading.Event()
_started = False
_lock = threading.Lock()


def _seconds(name: str, default: float, floor: float) -> float:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        return max(floor, float(raw))
    except ValueError:
        return default


def tick_seconds() -> float:
    return _seconds("CI_TICK_SECONDS", DEFAULT_TICK_SECONDS, 0.2)


def idle_seconds() -> float:
    return _seconds("CI_IDLE_TICK_SECONDS", DEFAULT_IDLE_SECONDS, 1.0)


def enabled() -> bool:
    return os.getenv("CI_ENGINE_TICKER", "true").strip().lower() not in {
        "0",
        "false",
        "no",
        "off",
    }


def is_running() -> bool:
    """Whether this process drives CI on its own loop.

    The shared scheduler asks before running a CI pass of its own, so exactly
    one clock is in charge and a deployment with the ticker turned off still
    advances builds.
    """
    return _started


def wake() -> None:
    """Run the next pass now. Safe to call from a request thread."""
    _wake.set()


_pass_lock = None
_pass_lock_guard = threading.Lock()
# The advisory lock below is per process, and re-asking for a lock this
# process already holds says yes. So threads of one process must not share it
# freely — a request thread's release would pull it out from under the ticker's
# pass. This mutex makes "one guarded pass per process" explicit; the advisory
# lock then makes it "one per cluster".
_pass_mutex = threading.Lock()


def _cross_process_lock(app: Flask):
    """This process's handle on the cluster-wide "one CI pass at a time" lock.

    The engine's own ``_pass_lock`` only serialises threads of one process.
    With several gunicorn workers or replicas, each runs this ticker (so a
    trigger landing on any of them is picked up in milliseconds, not at the
    leader's next idle interval), and this advisory lock makes sure only one
    of them is inside a pass at any moment. On SQLite it is always granted.
    """
    global _pass_lock
    with _pass_lock_guard:
        if _pass_lock is None:
            from ..leader_election import CI_ENGINE_LOCK, AdvisoryLock, database_url_for

            _pass_lock = AdvisoryLock(CI_ENGINE_LOCK, database_url_for(app))
        return _pass_lock


def _guarded(app: Flask, work):
    """Run ``work()`` holding both locks, or return ``None`` if either is taken."""
    if not _pass_mutex.acquire(blocking=False):
        return None
    try:
        lock = _cross_process_lock(app)
        if not lock.try_acquire():
            return None
        try:
            return work()
        finally:
            lock.release()
    finally:
        _pass_mutex.release()


def guarded_pass(app: Flask) -> Optional[bool]:
    """Run one CI pass if no other thread or process is in one.

    Returns the pass's "busy" answer, or ``None`` when the pass is held
    elsewhere — the caller should come back soon, because that pass may have
    started before the work that woke us was committed.
    Must be called inside an app context.
    """
    from .engine import advance_ci_builds

    return _guarded(app, lambda: bool(advance_ci_builds()))


def guarded_advance_build_now(build_id: int) -> None:
    """:func:`engine.advance_build_now` under the same cross-process guard.

    Skipping when another pass holds the lock is what advance_build_now
    already does for its own process: whatever it leaves is the tick's job,
    and the caller wakes the ticker right after.
    """
    from flask import current_app

    from .engine import advance_build_now

    _guarded(current_app._get_current_object(), lambda: advance_build_now(build_id))


def _idle_wait(app: Flask) -> float:
    """The idle interval, cut short if a schedule comes due before it ends.

    A schedule is the one kind of work nothing announces: no request wakes the
    ticker at 02:00. So an idle loop sleeps until the sooner of its interval
    and the next due schedule, and a nightly fires on its minute even on an
    installation that raised CI_IDLE_TICK_SECONDS to minutes.
    """
    wait = idle_seconds()
    try:
        with app.app_context():
            from .schedules import seconds_until_next_due

            due_in = seconds_until_next_due()
        if due_in is not None:
            # A small floor so a schedule that is due but could not be claimed
            # (another worker has it) does not spin this loop.
            wait = min(wait, max(0.5, due_in + 0.05))
    except Exception:
        logger.debug("Could not read the next CI schedule", exc_info=True)
    return wait


def _loop(app: Flask) -> None:
    while True:
        busy = False
        try:
            with app.app_context():
                result = guarded_pass(app)
                # Another process is mid-pass: retry on the fast interval.
                busy = True if result is None else result
        except Exception:
            logger.exception("CI engine tick failed")
        # A pass that found work almost certainly has more to do; one that found
        # none can afford to wait, because whatever creates the next build wakes
        # us anyway — except a schedule, which _idle_wait accounts for.
        _wake.wait(tick_seconds() if busy else _idle_wait(app))
        _wake.clear()


def start_ci_engine(app: Flask) -> None:
    global _started
    with _lock:
        if _started:
            return
        if app.config.get("TESTING"):
            return
        if not enabled():
            logger.info("CI engine ticker disabled (CI_ENGINE_TICKER=false)")
            return
        # Same reasoning as the alert scheduler: Werkzeug's reloader imports the
        # app twice and only the serving child should hold a clock.
        from ..alert_policy_scheduler import _should_start_in_process

        if not _should_start_in_process():
            return
        _started = True

    threading.Thread(target=_loop, args=(app,), daemon=True, name="ci-engine").start()
    logger.info(
        "CI engine ticker started (tick=%ss, idle=%ss)", tick_seconds(), idle_seconds()
    )
