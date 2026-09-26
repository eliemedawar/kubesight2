"""Upgrade job tracking for async automated upgrades, persisted to ``upgrade_jobs``.

The job dict (the ``/api/upgrades/jobs/<id>`` shape) is kept in two places:

* an in-process hot copy, updated by the worker thread and read first — the
  process running an upgrade always answers from its own, freshest state;
* the ``upgrade_jobs`` table, written through on every change, so other worker
  processes can answer the poll and a restart does not lose the record.

A job whose process died (restart, crash, OOM) can never finish, so it must not
report "running" forever. The worker thread bumps ``heartbeat_at`` while it is
alive; on startup (``interrupt_orphaned_jobs``) and on every DB read, a
queued/running row with a stale heartbeat is marked failed as interrupted.
"""

from __future__ import annotations

import logging
import os
import socket
import threading
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Dict, Optional

logger = logging.getLogger(__name__)

_lock = threading.Lock()
_jobs: Dict[str, Dict[str, Any]] = {}
# job id -> Flask app object, so worker threads can open an app context.
_apps: Dict[str, Any] = {}

_ACTIVE_STATUSES = ("queued", "running")
HEARTBEAT_SECONDS = int(os.getenv("UPGRADE_JOB_HEARTBEAT_SECONDS", "20"))
STALE_AFTER_SECONDS = int(os.getenv("UPGRADE_JOB_STALE_AFTER_SECONDS", "120"))
INTERRUPTED_MESSAGE = (
    "Upgrade interrupted: KubeSight restarted while this upgrade was running. "
    "Check each node's version and cordon state before retrying."
)


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _owner() -> str:
    return f"{socket.gethostname()}:{os.getpid()}"


def _as_aware(value: Optional[datetime]) -> Optional[datetime]:
    if value is None:
        return None
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# Persistence helpers — best-effort, never break the upgrade itself
# ---------------------------------------------------------------------------

def _current_app_or_none():
    from flask import current_app, has_app_context

    return current_app._get_current_object() if has_app_context() else None


def _with_app(job_id: str, fn: Callable[[], Any]) -> Any:
    """Run ``fn`` inside an app context (the current one, or the job's app)."""
    from flask import has_app_context

    if has_app_context():
        return fn()
    app = _apps.get(job_id)
    if app is None:
        return None
    with app.app_context():
        return fn()


def _persist(job_id: str, job: Dict[str, Any], *, heartbeat: bool = True) -> None:
    def _write() -> None:
        from .db import db
        from .models_upgrade import UpgradeJob

        try:
            row = UpgradeJob.query.filter_by(job_id=job_id).first()
            if row is None:
                row = UpgradeJob(job_id=job_id, owner=_owner())
                db.session.add(row)
            row.cluster_id = str(job.get("clusterId") or "")
            row.target_version = job.get("targetVersion")
            row.provider = job.get("provider")
            row.status = str(job.get("status") or "queued")
            row.payload = dict(job)
            if heartbeat:
                row.heartbeat_at = datetime.now(timezone.utc)
            db.session.commit()
        except Exception:  # noqa: BLE001 — the upgrade must not die on a DB hiccup
            db.session.rollback()
            logger.warning("Could not persist upgrade job %s", job_id, exc_info=True)

    try:
        _with_app(job_id, _write)
    except Exception:  # noqa: BLE001
        logger.warning("Could not persist upgrade job %s", job_id, exc_info=True)


def _interrupted_payload(payload: Dict[str, Any]) -> Dict[str, Any]:
    job = dict(payload or {})
    job.update(
        status="failed",
        message=INTERRUPTED_MESSAGE,
        error="interrupted",
        interrupted=True,
        finishedAt=job.get("finishedAt") or _utc_now(),
    )
    return job


def _is_stale(row, now: datetime, stale_after: int) -> bool:
    last = _as_aware(row.heartbeat_at) or _as_aware(row.updated_at) or _as_aware(row.created_at)
    return last is None or now - last > timedelta(seconds=stale_after)


# ---------------------------------------------------------------------------
# Public API (unchanged signatures)
# ---------------------------------------------------------------------------

def create_job(
    *,
    cluster_id: str,
    target_version: str,
    provider: str,
    steps: Optional[list] = None,
) -> Dict[str, Any]:
    job_id = f"upgrade-{uuid.uuid4().hex[:12]}"
    job = {
        "jobId": job_id,
        "clusterId": cluster_id,
        "targetVersion": target_version,
        "provider": provider,
        "status": "queued",
        "message": "Upgrade queued.",
        "steps": steps or [],
        "activeStep": -1,
        "executionSupported": True,
        "startedAt": _utc_now(),
        "finishedAt": None,
        "error": None,
    }
    app = _current_app_or_none()
    with _lock:
        _jobs[job_id] = job
        if app is not None:
            _apps[job_id] = app
    _persist(job_id, job)
    return dict(job)


def get_job(job_id: str) -> Optional[Dict[str, Any]]:
    with _lock:
        job = _jobs.get(job_id)
        if job:
            return dict(job)
    if _current_app_or_none() is None:
        return None
    return _load_job(job_id)


def _load_job(job_id: str) -> Optional[Dict[str, Any]]:
    from .db import db
    from .models_upgrade import UpgradeJob

    try:
        row = UpgradeJob.query.filter_by(job_id=job_id).first()
    except Exception:  # noqa: BLE001 — table missing on a half-migrated DB
        db.session.rollback()
        return None
    if row is None:
        return None
    payload = dict(row.payload or {})
    if row.status in _ACTIVE_STATUSES and _is_stale(
        row, datetime.now(timezone.utc), STALE_AFTER_SECONDS
    ):
        payload = _interrupted_payload(payload)
        row.status = "failed"
        row.payload = payload
        try:
            db.session.commit()
        except Exception:  # noqa: BLE001
            db.session.rollback()
    return payload


def update_job(job_id: str, **fields: Any) -> Optional[Dict[str, Any]]:
    with _lock:
        job = _jobs.get(job_id)
        if not job:
            return None
        job.update(fields)
        snapshot = dict(job)
    _persist(job_id, snapshot)
    return snapshot


def _heartbeat(job_id: str, stop: threading.Event) -> None:
    while not stop.wait(HEARTBEAT_SECONDS):
        with _lock:
            job = _jobs.get(job_id)
            snapshot = dict(job) if job else None
        if snapshot is None or snapshot.get("status") not in _ACTIVE_STATUSES:
            return
        _persist(job_id, snapshot)


def run_job_async(job_id: str, worker: Callable[[], None]) -> None:
    def _runner() -> None:
        stop = threading.Event()
        threading.Thread(
            target=_heartbeat, args=(job_id, stop), name=f"{job_id}-heartbeat", daemon=True
        ).start()
        update_job(job_id, status="running", message="Automated upgrade in progress.")
        try:
            worker()
        except Exception as exc:
            update_job(
                job_id,
                status="failed",
                message="Automated upgrade failed.",
                error=str(exc),
                finishedAt=_utc_now(),
            )
        finally:
            stop.set()
            with _lock:
                _apps.pop(job_id, None)

    threading.Thread(target=_runner, daemon=True).start()


def interrupt_orphaned_jobs(stale_after: Optional[int] = None) -> int:
    """Mark queued/running jobs no live process owns as failed (interrupted).

    Called at startup. A job is orphaned when this process is not running it
    and its heartbeat is older than ``stale_after`` seconds — the heartbeat
    check keeps a rolling restart from failing a job another, still-alive
    process is executing. Returns the number of jobs marked.
    """
    from .db import db
    from .models_upgrade import UpgradeJob

    threshold = STALE_AFTER_SECONDS if stale_after is None else stale_after
    now = datetime.now(timezone.utc)
    with _lock:
        local = set(_jobs)
    marked = 0
    for row in UpgradeJob.query.filter(UpgradeJob.status.in_(_ACTIVE_STATUSES)).all():
        if row.job_id in local or not _is_stale(row, now, threshold):
            continue
        row.payload = _interrupted_payload(row.payload or {})
        row.status = "failed"
        marked += 1
    if marked:
        db.session.commit()
    return marked
