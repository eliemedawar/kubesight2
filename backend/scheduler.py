"""Scheduler: the periodic tick, as its own process.

It used to be a thread inside the API, and the only thing stopping it running
twice was `gunicorn -w 1` and a comment explaining why. Every worker that
imported `create_app` started its own copy, so a second web replica -- or
`replicaCount: 2` in a Helm chart, which is the first thing anyone turns up --
ran all eight periodic tasks concurrently: the same deploy advanced twice, the
same change bundle executed twice, two processes each treating the other's live
cluster build as orphaned. No error, no warning.

Splitting it out makes the constraint enforceable instead of advisory. The
singleton is now a Deployment with `replicas: 1`, which an operator can see, and
the web tier is free to scale.

    python scheduler.py
    python scheduler.py --once     # one tick, for testing

**Run exactly one.** Nothing here elects a leader; two schedulers do the damage
described above. If you need failover, that is a lease in the database, and it
is not built yet -- so for now this is one replica, and a restart is a gap in
ticking rather than a duplicate.

The tasks themselves are unchanged. They were already idempotent and DB-backed,
resuming from wherever they were rather than holding state in memory, which is
why this is a relocation and not a rewrite.
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

# Before create_app: the scheduler must not migrate, for the same reason the
# worker must not. It verifies the head and refuses.
os.environ.setdefault("KUBESIGHT_SKIP_STARTUP_MIGRATION", "1")
# And it must not start the very thread it replaces.
os.environ["KUBESIGHT_IN_PROCESS_SCHEDULER"] = "0"

from api import create_app  # noqa: E402
from api.migrations import current_revision, head_revision, is_at_head  # noqa: E402
from api.services.alert_policy_scheduler import (  # noqa: E402
    _scheduler_tick_seconds,
    run_tick,
)

logger = logging.getLogger("kubesight.scheduler")


class _Shutdown:
    """Flips on SIGTERM/SIGINT. Checked between ticks, never mid-tick.

    A tick advances deploys and executes change bundles; interrupting one
    halfway would leave a bundle partly applied. Ticks are short and every task
    resumes, so finishing the one in hand is both cheap and safer.
    """

    def __init__(self) -> None:
        self.requested = False

    def install(self) -> None:
        for sig in (signal.SIGTERM, signal.SIGINT):
            try:
                signal.signal(sig, self._handle)
            except (ValueError, OSError):
                logger.warning("could not install handler for %s", sig)

    def _handle(self, signum, _frame) -> None:
        if self.requested:
            logger.warning("second signal %s, exiting now", signum)
            raise SystemExit(1)
        logger.info("signal %s received, finishing current tick then stopping", signum)
        self.requested = True


def run(once: bool = False) -> int:
    app = create_app()

    with app.app_context():
        if not is_at_head():
            logger.error(
                "database is not at the migration head (current=%s, expected=%s); "
                "run `python manage.py upgrade` before starting the scheduler",
                current_revision(),
                head_revision(),
            )
            return 1

    shutdown = _Shutdown()
    shutdown.install()

    tick_seconds = _scheduler_tick_seconds()
    logger.info("scheduler starting (pid=%s, tick=%ss)", os.getpid(), tick_seconds)

    ticks = 0
    with app.app_context():
        while True:
            started = time.monotonic()
            try:
                run_tick(app)
            except Exception:  # noqa: BLE001
                # run_tick already wraps each task; reaching here means the tick
                # machinery itself failed. Log and keep ticking -- a scheduler
                # that exits on one bad tick stops all eight tasks.
                logger.exception("tick failed")
            ticks += 1

            if once or shutdown.requested:
                break

            # Sleep the remainder, so a slow tick does not compound into drift.
            elapsed = time.monotonic() - started
            time.sleep(max(0.0, tick_seconds - elapsed))

            if shutdown.requested:
                break

    logger.info("scheduler stopped after %s tick(s)", ticks)
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="scheduler.py", description=__doc__)
    parser.add_argument("--once", action="store_true", help="run one tick and exit")
    parser.add_argument("--log-level", default=os.getenv("LOG_LEVEL", "INFO"))
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=args.log_level.upper(),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    return run(once=args.once)


if __name__ == "__main__":
    raise SystemExit(main())
