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
from typing import TYPE_CHECKING

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


def _loop(app: Flask) -> None:
    from .engine import advance_ci_builds

    while True:
        busy = False
        try:
            with app.app_context():
                busy = bool(advance_ci_builds())
        except Exception:
            logger.exception("CI engine tick failed")
        # A pass that found work almost certainly has more to do; one that found
        # none can afford to wait, because whatever creates the next build wakes
        # us anyway.
        _wake.wait(tick_seconds() if busy else idle_seconds())
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
