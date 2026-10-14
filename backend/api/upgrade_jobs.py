"""Upgrade job tracking, persisted to ``upgrade_jobs``.

A job is one upgrade of one cluster to one version, from request to result.
Its dict (the ``/api/upgrades/jobs/<id>`` shape) is the ``payload`` column.

Lifecycle (the ``status`` column)::

    awaiting_approval -> scheduled -> queued -> running <-> paused
                      \\-> rejected  \\-> withdrawn / expired
    running -> completed | failed | stopped

``guided`` is a run a person executes from the runbook while KubeSight
watches the cluster; it ends ``completed`` when the cluster reaches the target.

While a job runs, its dict is kept in two places:

* an in-process hot copy, updated by the worker thread and read first — the
  process running an upgrade always answers from its own, freshest state;
* the table, written through on every change, so other worker processes can
  answer the poll and a restart does not lose the record.

Requests that are not running yet (awaiting approval, scheduled) live only in
the table: any process may vote on them, and the one that starts the run
adopts the row into its hot copy.

Pause and stop are requested through ``control`` in the stored payload. Any
process may set it (the request can land on a worker that is not running the
upgrade), so the running process never writes ``control`` back: it reads it
from the table at each checkpoint.

A job whose process died (restart, crash, OOM) can never finish, so it must not
report "running" forever. The worker thread bumps ``heartbeat_at`` while it is
alive; on startup (``interrupt_orphaned_jobs``) and on every DB read, a
queued/running/paused row with a stale heartbeat is marked failed as interrupted.
"""

from __future__ import annotations

import logging
import os
import socket
import threading
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Dict, Iterable, List, Optional

logger = logging.getLogger(__name__)

_lock = threading.Lock()
_jobs: Dict[str, Dict[str, Any]] = {}
# job id -> Flask app object, so worker threads can open an app context.
_apps: Dict[str, Any] = {}

_ACTIVE_STATUSES = ("queued", "running", "paused")
# Waiting on a person or a clock: no thread, no heartbeat.
PENDING_STATUSES = ("awaiting_approval", "scheduled")
OPEN_STATUSES = PENDING_STATUSES + _ACTIVE_STATUSES + ("guided",)
FINAL_STATUSES = ("completed", "failed", "stopped", "rejected", "withdrawn", "expired")

HEARTBEAT_SECONDS = int(os.getenv("UPGRADE_JOB_HEARTBEAT_SECONDS", "20"))
STALE_AFTER_SECONDS = int(os.getenv("UPGRADE_JOB_STALE_AFTER_SECONDS", "120"))
INTERRUPTED_MESSAGE = (
    "Upgrade interrupted: KubeSight restarted while this upgrade was running. "
    "Check each node's version and cordon state before retrying."
)
# The run log a job keeps (oldest lines drop first).
LOG_LIMIT = 400


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _owner() -> str:
    return f"{socket.gethostname()}:{os.getpid()}"


def _as_aware(value: Optional[datetime]) -> Optional[datetime]:
    if value is None:
        return None
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def new_job_id() -> str:
    return f"upgrade-{uuid.uuid4().hex[:12]}"


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


def _row(job_id: str):
    from .models_upgrade import UpgradeJob

    return UpgradeJob.query.filter_by(job_id=job_id).first()


def _apply_columns(row, job: Dict[str, Any]) -> None:
    row.cluster_id = str(job.get("clusterId") or "")
    row.target_version = job.get("targetVersion")
    row.provider = job.get("provider")
    row.status = str(job.get("status") or "queued")


def _persist(job_id: str, job: Dict[str, Any], *, heartbeat: bool = True) -> None:
    def _write() -> None:
        from .db import db
        from .models_upgrade import UpgradeJob

        try:
            row = _row(job_id)
            if row is None:
                row = UpgradeJob(job_id=job_id, owner=_owner())
                db.session.add(row)
            stored = dict(job)
            # ``control`` belongs to whoever pressed Pause/Stop, maybe in another
            # process: the table's copy wins over this process's.
            if row.payload and "control" in row.payload:
                stored["control"] = row.payload.get("control")
            _apply_columns(row, stored)
            row.payload = stored
            row.owner = _owner()
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
# Running jobs
# ---------------------------------------------------------------------------

def create_job(
    *,
    cluster_id: str,
    target_version: str,
    provider: str,
    steps: Optional[list] = None,
    extra: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    job_id = new_job_id()
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
    if extra:
        job.update(extra)
    app = _current_app_or_none()
    with _lock:
        _jobs[job_id] = job
        if app is not None:
            _apps[job_id] = app
    _persist(job_id, job)
    return dict(job)


def adopt_job(job_id: str, **fields: Any) -> Optional[Dict[str, Any]]:
    """Take a stored request into this process's hot copy to run it here."""
    row = _row(job_id)
    if row is None:
        return None
    job = dict(row.payload or {})
    job.update(fields)
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

    try:
        row = _row(job_id)
    except Exception:  # noqa: BLE001 — table missing on a half-migrated DB
        db.session.rollback()
        return None
    if row is None:
        return None
    return _payload_of(row)


def _payload_of(row) -> Dict[str, Any]:
    from .db import db

    payload = dict(row.payload or {})
    if row.status in _ACTIVE_STATUSES and _is_stale(
        row, datetime.now(timezone.utc), STALE_AFTER_SECONDS
    ):
        with _lock:
            local = row.job_id in _jobs
        if not local:
            payload = _interrupted_payload(payload)
            row.status = "failed"
            row.payload = payload
            try:
                db.session.commit()
            except Exception:  # noqa: BLE001
                db.session.rollback()
    payload.setdefault("jobId", row.job_id)
    payload.setdefault("status", row.status)
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


def mutate_job(job_id: str, fn: Callable[[Dict[str, Any]], None], *, heartbeat: bool = False) -> Optional[Dict[str, Any]]:
    """Change a job in place under the lock, then write it through."""
    with _lock:
        job = _jobs.get(job_id)
        if not job:
            return None
        fn(job)
        snapshot = dict(job)
    _persist(job_id, snapshot, heartbeat=heartbeat)
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
            _after_finish(job_id)

    threading.Thread(target=_runner, daemon=True, name=f"{job_id}-run").start()


def _after_finish(job_id: str) -> None:
    """A finished run changes the cluster's version: let the fleet see it."""
    with _lock:
        job = dict(_jobs.get(job_id) or {})
    cluster_id = job.get("clusterId")
    if not cluster_id:
        return
    try:
        from .k8s_provider import _K8S_READ_CACHE

        _K8S_READ_CACHE.invalidate(f"version:{cluster_id}:")
        _K8S_READ_CACHE.invalidate(f"upg-nodes:{cluster_id}", exact=True)
    except Exception:  # noqa: BLE001
        logger.debug("Version cache invalidation failed", exc_info=True)


# ---------------------------------------------------------------------------
# Requests (stored only) and controls
# ---------------------------------------------------------------------------

def store_request(job: Dict[str, Any]) -> Dict[str, Any]:
    """Save a request that is not running yet (no hot copy, no heartbeat)."""
    from .db import db
    from .models_upgrade import UpgradeJob

    row = UpgradeJob(job_id=job["jobId"], owner=None)
    _apply_columns(row, job)
    row.payload = dict(job)
    db.session.add(row)
    db.session.commit()
    return dict(job)


def change_stored(job_id: str, fn: Callable[[Dict[str, Any]], None]) -> Optional[Dict[str, Any]]:
    """Read-modify-write a stored job (a vote, a withdrawal, a control flag).

    Postgres locks the row for the change; the hot copy, if this process has
    one, gets the same change."""
    from .db import db
    from .models_upgrade import UpgradeJob

    query = UpgradeJob.query.filter_by(job_id=job_id)
    try:
        row = query.with_for_update().first()
    except Exception:  # noqa: BLE001 — SQLite and friends: no row locks
        db.session.rollback()
        row = query.first()
    if row is None:
        return None
    payload = dict(row.payload or {})
    fn(payload)
    _apply_columns(row, payload)
    row.payload = payload
    db.session.commit()
    with _lock:
        hot = _jobs.get(job_id)
        if hot is not None:
            hot.clear()
            hot.update(payload)
    return payload


def get_control(job_id: str) -> Dict[str, Any]:
    """The Pause/Stop flags as stored (any process may have set them)."""
    def _read() -> Dict[str, Any]:
        from .db import db

        try:
            row = _row(job_id)
            control = dict((row.payload or {}).get("control") or {}) if row else {}
            db.session.commit()  # end the read so the next one sees fresh data
            return control
        except Exception:  # noqa: BLE001
            db.session.rollback()
            return {}

    return _with_app(job_id, _read) or {}


def list_jobs(
    *,
    cluster_ids: Optional[Iterable[str]] = None,
    statuses: Optional[Iterable[str]] = None,
    limit: int = 50,
    include_log: bool = False,
) -> List[Dict[str, Any]]:
    from .db import db
    from .models_upgrade import UpgradeJob

    query = UpgradeJob.query
    if cluster_ids is not None:
        ids = [str(c) for c in cluster_ids]
        if not ids:
            return []
        query = query.filter(UpgradeJob.cluster_id.in_(ids))
    if statuses:
        query = query.filter(UpgradeJob.status.in_(list(statuses)))
    try:
        rows = query.order_by(UpgradeJob.created_at.desc()).limit(max(1, min(int(limit), 500))).all()
    except Exception:  # noqa: BLE001
        db.session.rollback()
        return []
    out = []
    for row in rows:
        with _lock:
            hot = _jobs.get(row.job_id)
            payload = dict(hot) if hot else None
        if payload is None:
            payload = _payload_of(row)
        payload.setdefault("createdAt", _as_aware(row.created_at).isoformat() if row.created_at else None)
        if not include_log:
            payload.pop("log", None)
        out.append(payload)
    return out


def interrupt_orphaned_jobs(stale_after: Optional[int] = None) -> int:
    """Mark queued/running/paused jobs no live process owns as failed (interrupted).

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
