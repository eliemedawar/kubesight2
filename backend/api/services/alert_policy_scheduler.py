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
    """Run once under Werkzeug's debug reloader, normally otherwise.

    Werkzeug imports the app in both the reloader parent and its serving child.
    Only the child sets ``WERKZEUG_RUN_MAIN=true``. Starting a scheduler in
    both processes gives each one a separate in-memory worker registry, so the
    parent can misclassify a live Cluster Builder run as orphaned.
    """
    run_main = os.environ.get("WERKZEUG_RUN_MAIN", "").strip().lower()
    debug = os.environ.get("FLASK_DEBUG", "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }
    if debug:
        return run_main == "true"
    if run_main:
        return run_main == "true"
    return True


def run_tick(app: Flask) -> None:
    """One pass over every periodic task. Safe to call from anywhere.

    Extracted from the loop so the in-process thread and the standalone
    scheduler process run identical code -- two copies of a tick list this
    long would diverge, and the divergence would be silent.

    Every task is wrapped individually: one failing tick must not stop the
    other seven. That was true of the original loop and is preserved.
    """
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
            from . import ticketing

            # Self-gating per provider: each only runs when its integration
            # is enabled and its configured interval has elapsed.
            ticketing.run_due_syncs()
    except Exception:
        logger.exception("Ticketing field-sync tick failed")
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
    try:
        with app.app_context():
            from .application_intelligence_service import reap_stale_analyses

            # Analyses report back over a callback, and nothing watched for the
            # container that never calls. Without this a dead analysis reads as
            # "Running" indefinitely and blocks deleting its application.
            closed = reap_stale_analyses()
            if closed:
                logger.warning("closed %s analysis(es) that stopped reporting", closed)
    except Exception:
        logger.exception("Stale analysis reap tick failed")
    try:
        with app.app_context():
            from .cluster_build.executor import advance_cluster_builds

            # Cluster Builder: resume builds orphaned by a backend restart
            # (completed steps are skipped — resume, not restart). No-ops
            # instantly when nothing is in status 'building'.
            advance_cluster_builds()
    except Exception:
        logger.exception("Cluster build tick failed")



def _scheduler_loop(app: Flask) -> None:
    tick = _scheduler_tick_seconds()
    while True:
        time.sleep(tick)
        run_tick(app)

_scheduler_started = False


def in_process_scheduler_enabled() -> bool:
    """Whether this process should tick inside the API.

    Off in production, where the scheduler is its own Deployment. On in
    development, where one process doing everything is the point.

    The in-process thread is safe only while exactly one process runs it, and
    that was previously guaranteed by `gunicorn -w 1` and a comment. A second
    web replica ran all eight periodic tasks twice: the same deploy advanced
    twice, the same change bundle executed twice. Defaulting it off in
    production makes the web tier scalable and moves the singleton somewhere an
    operator can see it.

    `KUBESIGHT_IN_PROCESS_SCHEDULER` overrides in either direction, which is
    what `scheduler.py` uses to make sure it never starts the thread it is.
    """
    raw = os.getenv("KUBESIGHT_IN_PROCESS_SCHEDULER", "").strip().lower()
    if raw:
        return raw in {"1", "true", "yes", "on"}

    from .. import _is_production_env

    return not _is_production_env()


def start_alert_policy_scheduler(app: Flask) -> None:
    global _scheduler_started
    if _scheduler_started:
        return
    if app.config.get("TESTING"):
        return
    if not _scheduler_enabled():
        logger.info("Alert policy scheduler disabled (ALERT_POLICY_SCHEDULER=false)")
        return
    if not in_process_scheduler_enabled():
        logger.info(
            "In-process scheduler off; run scheduler.py as its own process "
            "(exactly one replica)"
        )
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
