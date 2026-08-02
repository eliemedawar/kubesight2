"""Job worker: a process, not a thread.

The whole point of the job platform is that work outlives the process that
started it. Running the drain loop inside the API would put it back where it
was -- sharing a lifecycle with request handling, dying on a rolling restart,
scaling only when the web tier scales.

So this is a separate entrypoint and, in a cluster, a separate Deployment. Run
as many as you like: claiming is atomic, so two workers never take the same job.

    python worker.py                       # drain everything
    python worker.py --types deploy.execute
    python worker.py --once                # one job, for testing

Shutdown is the part worth reading. On SIGTERM the loop stops claiming new work
and lets the job in hand finish, because Kubernetes sends SIGTERM before every
rolling update -- if that killed jobs outright, the platform would lose work on
exactly the routine event it was built to survive. If the job outlasts the grace
period the pod is killed anyway, and the reaper requeues it: interrupted, but
never lost.
"""

from __future__ import annotations

import argparse
import logging
import os
import signal
import sys
import time
from pathlib import Path

BACKEND_ROOT = Path(__file__).resolve().parent
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from api import create_app  # noqa: E402
from api.migrations import current_revision, head_revision, is_at_head  # noqa: E402
from api.models_jobs import worker_identity  # noqa: E402
from api.services import job_queue  # noqa: E402

logger = logging.getLogger("kubesight.worker")

# How long to wait after finding nothing. Short enough that a queued deploy does
# not visibly sit, long enough that an idle worker is not a busy loop against
# the database.
IDLE_SLEEP_SECONDS = float(os.getenv("WORKER_IDLE_SLEEP_SECONDS", "2"))

# The reaper only has to run often enough to notice a dead worker, and every
# worker runs it, so a short interval multiplied by the worker count is wasted
# queries.
REAP_INTERVAL_SECONDS = float(os.getenv("WORKER_REAP_INTERVAL_SECONDS", "60"))


class _Shutdown:
    """Flips on SIGTERM/SIGINT. Checked between jobs, never mid-job."""

    def __init__(self) -> None:
        self.requested = False

    def install(self) -> None:
        for sig in (signal.SIGTERM, signal.SIGINT):
            try:
                signal.signal(sig, self._handle)
            except (ValueError, OSError):
                # Not the main thread, or a platform without the signal. Not
                # fatal: the worker simply loses graceful shutdown.
                logger.warning("could not install handler for %s", sig)

    def _handle(self, signum, _frame) -> None:
        if self.requested:
            # Second signal: the operator is done waiting.
            logger.warning("second signal %s, exiting now", signum)
            raise SystemExit(1)
        logger.info("signal %s received, finishing current job then stopping", signum)
        self.requested = True


def run(job_types=None, once: bool = False) -> int:
    # Set before create_app, which would otherwise migrate on a development
    # start. That is fine for one process and a race for several starting
    # together -- and scaling workers out is the ordinary reason to have more
    # than one. A worker verifies the head and refuses; it never sets it.
    os.environ["KUBESIGHT_SKIP_STARTUP_MIGRATION"] = "1"
    app = create_app()

    with app.app_context():
        if not is_at_head():
            logger.error(
                "database is not at the migration head (current=%s, expected=%s); "
                "run `python manage.py upgrade` before starting workers",
                current_revision(),
                head_revision(),
            )
            return 1

    shutdown = _Shutdown()
    shutdown.install()

    identity = worker_identity()
    logger.info(
        "worker %s starting (types=%s, handlers=%s)",
        identity,
        ",".join(job_types) if job_types else "all",
        ",".join(sorted(job_queue.registered_types())) or "none",
    )

    if not job_queue.registered_types():
        # Not fatal -- handlers register on import, and a deployment may
        # legitimately run a worker before any caller has migrated -- but a
        # worker that can only dead-letter is worth saying out loud.
        logger.warning("no handlers registered; every claimed job will dead-letter")

    processed = 0
    last_reap = 0.0

    with app.app_context():
        while True:
            if shutdown.requested:
                break

            now = time.monotonic()
            if now - last_reap >= REAP_INTERVAL_SECONDS:
                try:
                    reaped = job_queue.reap_stale()
                    if reaped:
                        logger.warning("requeued %s job(s) from dead workers", reaped)
                except Exception:  # noqa: BLE001
                    # A failing reaper must not stop the worker draining.
                    logger.exception("reaper failed")
                last_reap = now

            try:
                job = job_queue.run_once(job_types)
            except Exception:  # noqa: BLE001
                # run_once already contains handler errors; reaching here means
                # the queue itself failed -- a dropped connection, most likely.
                # Back off rather than spin.
                logger.exception("claim/run failed; backing off")
                time.sleep(IDLE_SLEEP_SECONDS)
                continue

            if job is None:
                if once:
                    break
                time.sleep(IDLE_SLEEP_SECONDS)
                continue

            processed += 1
            logger.info("job %s (%s) -> %s", job.id, job.job_type, job.state)
            if once:
                break

    logger.info("worker %s stopped after %s job(s)", identity, processed)
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="worker.py", description=__doc__)
    parser.add_argument(
        "--types",
        help="comma-separated job types to claim; default is every type",
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="process one job and exit; returns immediately when idle",
    )
    parser.add_argument("--log-level", default=os.getenv("LOG_LEVEL", "INFO"))
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=args.log_level.upper(),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )

    types = [t.strip() for t in args.types.split(",")] if args.types else None
    return run(job_types=types, once=args.once)


if __name__ == "__main__":
    raise SystemExit(main())
