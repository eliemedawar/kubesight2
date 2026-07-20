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


def _component_health_refresh_enabled() -> bool:
    return os.getenv("COMPONENT_HEALTH_AUTO_REFRESH", "true").strip().lower() not in {
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
        # Auto-refresh component health first so the policy evaluation below
        # (and the UI) sees current statuses without manual "Check now" clicks.
        # Self-gating: only components whose last check is stale are re-checked.
        if _component_health_refresh_enabled():
            try:
                with app.app_context():
                    from .topology_component_service import refresh_stale_component_healths

                    refresh_stale_component_healths()
            except Exception:
                logger.exception("Component health auto-refresh tick failed")
        try:
            with app.app_context():
                from .alert_policy_evaluator import evaluate_all_enabled_policies

                evaluate_all_enabled_policies(persist=True)
        except Exception:
            logger.exception("Alert policy scheduler tick failed")
        try:
            with app.app_context():
                from .deployment_request_service import auto_decline_overdue_requests

                auto_decline_overdue_requests()
        except Exception:
            logger.exception("Deployment request auto-decline tick failed")
        try:
            with app.app_context():
                from .change_bundle_executor import process_due_bundles

                process_due_bundles()
        except Exception:
            logger.exception("Change bundle execution tick failed")
        try:
            with app.app_context():
                from .change_bundle_executor import watch_bundle_rollouts

                # Post-execution pod-health watches on bundle-applied
                # deployments (rollback + notify on rollout failure).
                watch_bundle_rollouts()
        except Exception:
            logger.exception("Bundle rollout watch tick failed")
        try:
            with app.app_context():
                from .zoho_sync_service import run_due_sync

                # Self-gating: only runs when the integration is enabled and its
                # configured interval has elapsed since the last sync.
                run_due_sync()
        except Exception:
            logger.exception("Zoho field-sync tick failed")
        try:
            with app.app_context():
                from .deploy_automation_service import advance_runs

                # Ticket-driven deploy automation: advance every active run one
                # step (registry gate → Jenkins build poll → verify → handoff).
                # No-ops instantly when there are no active runs.
                advance_runs()
        except Exception:
            logger.exception("Deploy automation tick failed")
        try:
            with app.app_context():
                from .mobile_app_service import (
                    advance_mobile_builds,
                    advance_mobile_publishes,
                    advance_mobile_resigns,
                )

                # Mobile Applications: dispatch pending artifact downloads and
                # store uploads to worker threads (the heavy transfers never run
                # on this tick thread) and poll App Store processing states.
                # No-ops instantly when nothing is pending.
                advance_mobile_builds()
                advance_mobile_publishes()
                # Signing jobs launch and report back by callback; this only
                # launches queued ones and catches the ones that died silently.
                advance_mobile_resigns()
        except Exception:
            logger.exception("Mobile applications tick failed")


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
