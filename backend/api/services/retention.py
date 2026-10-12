"""Opt-in data retention, run from the scheduler's background lane.

Nothing is deleted by default. Each kind of history has its own switch, and a
kind whose switch is unset is never touched:

* ``AUDIT_LOG_RETENTION_DAYS``           -> audit_service.purge_old_audit_logs
* ``CI_LOG_RETENTION_DAYS``              -> ci.logs.prune_old_logs
* ``ALERT_DELIVERY_LOG_RETENTION_DAYS``  -> alert_routing_service.prune_delivery_logs

The last two are owned by their features; they are called only when the
function exists in this build (looked up by name, so this module needs no
change when they land or move) and their variable is set. The whole pass runs
at most once an hour per process; only the scheduler leader runs it at all.
"""

from __future__ import annotations

import importlib
import logging
import os
import threading
import time
from typing import Callable, Dict, Optional

logger = logging.getLogger(__name__)

RUN_EVERY_SECONDS = 3600

# (env var, module, function) for each retention job. The function is called
# with no arguments and reads its own setting.
RETENTION_JOBS = (
    ("AUDIT_LOG_RETENTION_DAYS", "api.services.audit_service", "purge_old_audit_logs"),
    ("CI_LOG_RETENTION_DAYS", "api.services.ci.logs", "prune_old_logs"),
    (
        "ALERT_DELIVERY_LOG_RETENTION_DAYS",
        "api.services.alert_routing_service",
        "prune_delivery_logs",
    ),
)

_last_run = 0.0
_lock = threading.Lock()


def _enabled(env_var: str) -> bool:
    raw = os.getenv(env_var, "").strip()
    try:
        return int(raw) > 0
    except ValueError:
        return False


def _resolve(module_name: str, function_name: str) -> Optional[Callable[[], object]]:
    try:
        module = importlib.import_module(module_name)
    except ImportError:
        return None
    function = getattr(module, function_name, None)
    return function if callable(function) else None


def run_due_retention(*, force: bool = False) -> Dict[str, object]:
    """Run every switched-on retention job, at most once an hour.

    Returns {env var: job result} for the jobs that ran ({} when none is on or
    the hour is not up). A failing job is logged and rolled back; the others
    still run.
    """
    global _last_run
    enabled = [job for job in RETENTION_JOBS if _enabled(job[0])]
    if not enabled:
        return {}
    with _lock:
        now = time.monotonic()
        if not force and _last_run and now - _last_run < RUN_EVERY_SECONDS:
            return {}
        _last_run = now

    from ..db import db

    results: Dict[str, object] = {}
    for env_var, module_name, function_name in enabled:
        function = _resolve(module_name, function_name)
        if function is None:
            logger.debug("%s is set but %s.%s is not in this build", env_var, module_name, function_name)
            continue
        try:
            results[env_var] = function()
        except Exception:  # noqa: BLE001 — one job must not stop the others
            db.session.rollback()
            logger.exception("Retention job %s.%s failed", module_name, function_name)
            continue
        if results[env_var]:
            logger.info("Retention (%s): %s", env_var, results[env_var])
    return results
