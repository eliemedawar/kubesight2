"""Running an analysis off the request thread, and reaping what dies.

Reading a repository and asking a model takes tens of seconds. Holding an HTTP
request open for that is the fragile option — a proxy timeout, a closed laptop
or a page refresh loses work that was nearly done, and the user has no way to
find out what happened. So a request creates the row and returns it; a worker
thread does the work and writes progress onto that row; the UI polls it. The
same shape the CI engine already uses for builds, for the same reasons.

The honest cost of an in-process worker is that an analysis belongs to the
replica that started it. If that replica restarts, nothing is left driving the
row and it would sit at "Analyzing…" forever. :func:`reap_stale` is the answer:
every analysis heartbeats while it works, and one that has gone quiet for longer
than the timeout is closed as failed with a message saying so. A visibly failed
analysis is recoverable — the user presses Retry. An invisibly stalled one is
not.
"""

from __future__ import annotations

import logging
import os
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Optional

from ...db import db
from ...models_ci import CiRepositoryAnalysis

if TYPE_CHECKING:
    from flask import Flask

logger = logging.getLogger(__name__)

# Small on purpose. Each analysis is one model call and a handful of API reads;
# the constraint is the model gateway's concurrency, not this process's.
DEFAULT_WORKERS = 2
# An analysis that has not heartbeated in this long is not running any more.
# Generously above the Hermes timeout (180s) plus its one retry, so a slow model
# is never mistaken for a dead worker.
DEFAULT_STALE_SECONDS = 900

_executor: Optional[ThreadPoolExecutor] = None
_app: Optional["Flask"] = None
_lock = threading.Lock()


def _int_env(name: str, default: int) -> int:
    try:
        return max(1, int(os.getenv(name, "").strip() or default))
    except ValueError:
        return default


def start(app: "Flask") -> None:
    """Attach the worker pool to this process.

    Called from the same place the CI engine ticker starts. In TESTING the pool
    is deliberately not created: tests drive :func:`run_now` directly, so an
    analysis happens synchronously and assertions do not race a thread.
    """
    global _executor, _app
    with _lock:
        if _executor is not None:
            return
        _app = app
        if app.config.get("TESTING"):
            return
        workers = _int_env("CI_ASSIST_WORKERS", DEFAULT_WORKERS)
        _executor = ThreadPoolExecutor(
            max_workers=workers, thread_name_prefix="ci-assist"
        )
        logger.info("CI assist worker pool started (workers=%s)", workers)

    # Anything left mid-flight by the process this one replaced has nothing
    # driving it. Closing those now means a user who restarts the backend and
    # reloads the page sees "retry", not a spinner that never resolves.
    try:
        with app.app_context():
            closed = reap_stale()
        if closed:
            logger.info("Closed %s stale CI analyses left by a previous process", closed)
    except Exception:
        logger.exception("Stale CI analysis sweep failed at startup")


def submit(analysis_id: int) -> None:
    """Queue an analysis, or run it inline when there is no pool.

    Inline is the TESTING path and the "somebody disabled the pool" path. Both
    want the work to happen; neither wants it to happen invisibly.
    """
    if _executor is None:
        run_now(analysis_id)
        return
    _executor.submit(_run_in_context, analysis_id)


def _run_in_context(analysis_id: int) -> None:
    from . import generator

    if _app is None:
        return
    try:
        with _app.app_context():
            try:
                generator.run(analysis_id)
            finally:
                # A worker thread holds its own session; leaving it open leaks a
                # connection per analysis.
                db.session.remove()
    except Exception:
        logger.exception("CI assist worker failed for analysis %s", analysis_id)


def run_now(analysis_id: int) -> None:
    """Run one analysis on the calling thread, inside whatever context it has."""
    from . import generator

    generator.run(analysis_id)


def reap_stale(*, commit: bool = True) -> int:
    """Close analyses whose worker is gone. Returns how many.

    Safe to call from any process: it only touches rows that have stopped
    heartbeating, and a live worker heartbeats at every step.
    """
    cutoff = datetime.now(timezone.utc) - timedelta(
        seconds=_int_env("CI_ASSIST_STALE_SECONDS", DEFAULT_STALE_SECONDS)
    )
    rows = (
        CiRepositoryAnalysis.query.filter(
            CiRepositoryAnalysis.state.in_(("queued", "analyzing"))
        ).all()
    )
    closed = 0
    for row in rows:
        last = row.last_heartbeat_at or row.started_at or row.created_at
        if last is None:
            continue
        if last.tzinfo is None:
            last = last.replace(tzinfo=timezone.utc)
        if last >= cutoff:
            continue
        row.state = "failed"
        row.pipeline_state = "not_generated"
        row.failure_stage = row.current_stage or "Analyzing"
        row.safe_error_message = (
            "The analysis stopped responding and was closed. This usually means "
            "KubeSight restarted while it was running. Retry the analysis, or "
            "configure the service manually."
        )
        row.completed_at = datetime.now(timezone.utc)
        db.session.add(row)
        closed += 1
    if closed and commit:
        db.session.commit()
    return closed
