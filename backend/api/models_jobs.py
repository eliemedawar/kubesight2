"""Durable job records.

The work these replace ran on `threading.Thread(daemon=True)` inside the Flask
process -- deploys, alert evaluation, cluster builds, ticket sync, mobile
builds. Daemon threads die with the process. A deploy interrupted by a pod
restart left no row, no error and no retry: the operator saw a request that
never finished and no way to find out why.

Everything here exists to answer one question after the fact: what ran, on whose
authority, and how did it end.

In its own module rather than `models.py` (2,846 lines, 62 classes) following
the convention already set by `models_cluster_build` and
`models_application_intelligence`. Registered in `alembic/env.py` -- a model
module missing from there is invisible to autogenerate, and the next revision
proposes dropping its tables.
"""

from __future__ import annotations

import os
import secrets
import time
from datetime import datetime, timezone

from .db import db

# Customer-facing job states. A job is in exactly one.
QUEUED = "queued"
RUNNING = "running"
SUCCEEDED = "succeeded"
FAILED = "failed"
CANCELLED = "cancelled"
DEAD_LETTER = "dead_letter"

TERMINAL_STATES = frozenset({SUCCEEDED, FAILED, CANCELLED, DEAD_LETTER})
ALL_STATES = frozenset({QUEUED, RUNNING}) | TERMINAL_STATES

_ID_ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"  # Crockford base32: no I, L, O, U


def _utcnow():
    return datetime.now(timezone.utc)


def new_job_id() -> str:
    """Time-sortable id, so `ORDER BY id` is chronological.

    Scanning a job table is nearly always "what happened around then", and a
    random uuid forces a join against created_at to answer it. Millisecond
    timestamp in Crockford base32 followed by randomness, which is the useful
    half of ULID without taking a dependency for it.
    """
    millis = int(time.time() * 1000)
    stamp = ""
    for _ in range(10):
        millis, remainder = divmod(millis, 32)
        stamp = _ID_ALPHABET[remainder] + stamp
    suffix = "".join(secrets.choice(_ID_ALPHABET) for _ in range(16))
    return stamp + suffix


class Job(db.Model):
    """One unit of durable work.

    `(job_type, idempotency_key)` is unique, and that constraint is the whole
    safety story for retries: enqueueing the same key twice returns the existing
    job instead of running a deploy a second time. It is enforced by the
    database rather than by a check-then-insert, because two workers racing is
    exactly the case a check-then-insert loses.
    """

    __tablename__ = "jobs"
    __table_args__ = (
        db.UniqueConstraint("job_type", "idempotency_key", name="uq_job_type_idempotency"),
        db.Index("ix_jobs_state_created", "state", "created_at"),
        db.Index("ix_jobs_claimable", "state", "job_type"),
    )

    id = db.Column(db.String(32), primary_key=True, default=new_job_id)
    job_type = db.Column(db.String(64), nullable=False)
    idempotency_key = db.Column(db.String(255), nullable=False)
    state = db.Column(db.String(16), nullable=False, default=QUEUED)

    payload_json = db.Column(db.JSON, nullable=False, default=dict)
    progress_json = db.Column(db.JSON, nullable=True)

    attempt = db.Column(db.Integer, nullable=False, default=0)
    max_attempts = db.Column(db.Integer, nullable=False, default=3)
    timeout_seconds = db.Column(db.Integer, nullable=False, default=300)

    # Nullable: scheduled work has no human behind it. Where there is one, every
    # audit record the job writes is attributed to them rather than to "system",
    # which is the difference between an audit trail and a log.
    actor_user_id = db.Column(db.Integer, nullable=True)

    error = db.Column(db.Text, nullable=True)

    created_at = db.Column(db.DateTime(timezone=True), nullable=False, default=_utcnow)
    started_at = db.Column(db.DateTime(timezone=True), nullable=True)
    finished_at = db.Column(db.DateTime(timezone=True), nullable=True)
    # Written by the running worker. A row still marked running with a stale
    # heartbeat is how a crashed worker is told apart from a slow one.
    heartbeat_at = db.Column(db.DateTime(timezone=True), nullable=True)
    # Which worker holds it, for diagnosing a stuck queue.
    claimed_by = db.Column(db.String(120), nullable=True)

    def to_dict(self) -> dict:
        """Contract 3's job record.

        The payload is deliberately absent. It can hold anything a caller passed
        -- registry credentials, kubeconfig fragments -- and this shape is what
        goes into API responses and logs.
        """
        return {
            "jobId": self.id,
            "jobType": self.job_type,
            "state": self.state,
            "attempt": self.attempt,
            "maxAttempts": self.max_attempts,
            "progress": self.progress_json,
            "createdAt": _iso(self.created_at),
            "startedAt": _iso(self.started_at),
            "finishedAt": _iso(self.finished_at),
            "error": self.error,
            "actorUserId": self.actor_user_id,
        }


def _iso(value):
    return value.isoformat() if value is not None else None


def worker_identity() -> str:
    """Host and pid, so `claimed_by` points at something you can go look at."""
    return f"{os.uname().nodename if hasattr(os, 'uname') else os.environ.get('COMPUTERNAME', 'worker')}:{os.getpid()}"
