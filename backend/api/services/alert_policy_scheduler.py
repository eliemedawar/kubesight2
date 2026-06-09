"""Background scheduler for alert policy evaluation and repeat notifications."""

from __future__ import annotations

import logging
import os
import threading
import time
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from flask import Flask

logger = logging.getLogger(__name__)

DEFAULT_TICK_SECONDS = 15


def _scheduler_tick_seconds() -> int:
    raw = os.getenv("ALERT_POLICY_SCHEDULER_TICK_SECONDS", str(DEFAULT_TICK_SECONDS)).strip()
    try:
        return max(5, int(raw))
    except ValueError:
        return DEFAULT_TICK_SECONDS


def _scheduler_enabled() -> bool:
    return os.getenv("ALERT_POLICY_SCHEDULER", "true").strip().lower() not in {
        "0",
        "false",
        "no",
        "off",
    }


def _should_start_in_process() -> bool:
    """Avoid duplicate schedulers under Flask/Werkzeug debug reloader parent process."""
    if os.environ.get("WERKZEU_RUN_MAIN") == "true":
        return True
    if os.environ.get("WERKZEU_RUN_RELOAD") == "true":
        return False
    return True


def _scheduler_loop(app: Flask) -> None:
    tick = _scheduler_tick_seconds()
    while True:
        time.sleep(tick)
        try:
            with app.app_context():
                from .alert_policy_evaluator import evaluate_all_enabled_policies

                evaluate_all_enabled_policies(persist=True)
        except Exception:
            logger.exception("Alert policy scheduler tick failed")


_scheduler_started = False


def start_alert_policy_scheduler(app: Flask) -> None:
    global _scheduler_started
    if _scheduler_started:
        return
    if app.config.get("TESTING"):
        return
    if not _scheduler_enabled():
        logger.info("Alert policy scheduler disabled (ALERT_POLICY_SCHEDULER=false)")
        return
    if not _should_start_in_process():
        return

    _scheduler_started = True

    thread = threading.Thread(
        target=_scheduler_loop,
        args=(app,),
        daemon=True,
        name="alert-policy-scheduler",
    )
    thread.start()
    logger.info("Alert policy scheduler started (tick=%ss)", _scheduler_tick_seconds())
