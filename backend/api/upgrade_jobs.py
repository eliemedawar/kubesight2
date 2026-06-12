"""In-memory upgrade job tracking for async automated upgrades."""

from __future__ import annotations

import threading
import uuid
from datetime import datetime, timezone
from typing import Any, Callable, Dict, Optional


_lock = threading.Lock()
_jobs: Dict[str, Dict[str, Any]] = {}


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


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
    with _lock:
        _jobs[job_id] = job
    return dict(job)


def get_job(job_id: str) -> Optional[Dict[str, Any]]:
    with _lock:
        job = _jobs.get(job_id)
        return dict(job) if job else None


def update_job(job_id: str, **fields: Any) -> Optional[Dict[str, Any]]:
    with _lock:
        job = _jobs.get(job_id)
        if not job:
            return None
        job.update(fields)
        return dict(job)


def run_job_async(job_id: str, worker: Callable[[], None]) -> None:
    def _runner() -> None:
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

    threading.Thread(target=_runner, daemon=True).start()
