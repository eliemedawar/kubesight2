"""The durable job queue.

Contract 3. Replaces `threading.Thread(daemon=True)` for work that must survive
the process: deploys, alert evaluation, cluster builds, ticket sync, mobile
builds.

Three properties the threads did not have.

**It survives a restart.** State lives in a row, written before the work
continues. A worker killed mid-job leaves that row in `running` with a stale
heartbeat, and the reaper returns it to `queued` if attempts remain.

**It cannot double-run.** `(job_type, idempotency_key)` is unique in the
database. Enqueueing the same key twice returns the existing job. This is a
constraint rather than a check-then-insert because two workers racing is exactly
what check-then-insert loses, and "we deployed twice" is the failure that
outranks the rest.

**It says who asked.** Every job carries `actor_user_id`, so the audit record it
writes names a person rather than "system".

No polling loop or worker process here -- this is the mechanism. Wiring it to a
deployment is separate, and the existing threaded callers move over one at a
time, deploy automation first.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Dict, Iterable, Optional

from ..audit import log_audit
from ..db import db
from ..models_jobs import (
    CANCELLED,
    DEAD_LETTER,
    FAILED,
    QUEUED,
    RUNNING,
    SUCCEEDED,
    TERMINAL_STATES,
    Job,
    worker_identity,
)

logger = logging.getLogger(__name__)

# Handlers by job_type, populated by @handler.
_HANDLERS: Dict[str, Callable[[Job], None]] = {}

# Substring match, lowercased. Deliberately broad: a payload key nobody
# anticipated is the one that leaks, and over-redacting a log costs nothing.
_SECRET_HINTS = (
    "password", "secret", "token", "credential", "authorization", "auth",
    "key", "cert", "private", "kubeconfig", "passphrase", "session",
)

REDACTED = "[redacted]"


class JobError(Exception):
    """Raised by a handler to fail a job with a message an operator can read.

    Retryable: the job goes back to the queue while attempts remain.
    """


class PermanentJobError(JobError):
    """A failure another attempt cannot fix. Dead-letters immediately.

    SMTP not configured, a malformed payload, a deleted target. Burning three
    more attempts on a certainty delays the moment a human finds out, and fills
    the retry budget with work that was never going to succeed. `dead_letter`
    is also the state that means "somebody should look", which is exactly true
    here and misleading for a refused connection.
    """


def redact(value: Any) -> Any:
    """Recursively mask anything that looks like a secret.

    Job payloads carry registry credentials and kubeconfig fragments. This shape
    is what reaches logs and API responses, and a payload is logged at exactly
    the moment something has gone wrong -- which is the worst time to discover
    a token in the log aggregator.
    """
    if isinstance(value, dict):
        out = {}
        for key, item in value.items():
            if any(hint in str(key).lower() for hint in _SECRET_HINTS):
                out[key] = REDACTED
            else:
                out[key] = redact(item)
        return out
    if isinstance(value, (list, tuple)):
        return [redact(item) for item in value]
    return value


def handler(job_type: str):
    """Register the function that runs a job type."""

    def decorate(fn: Callable[[Job], None]) -> Callable[[Job], None]:
        if job_type in _HANDLERS:
            raise RuntimeError(f"two handlers registered for {job_type!r}")
        _HANDLERS[job_type] = fn
        return fn

    return decorate


def registered_types() -> Iterable[str]:
    return tuple(_HANDLERS)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _aware(value: Optional[datetime]) -> Optional[datetime]:
    """SQLite hands back naive datetimes even for timezone=True columns.

    Comparing one to an aware `now` raises TypeError, which would surface as the
    reaper crashing rather than as a stuck queue -- so normalise on the way out.
    """
    if value is None or value.tzinfo is not None:
        return value
    return value.replace(tzinfo=timezone.utc)


def enqueue(
    job_type: str,
    payload: Optional[Dict[str, Any]] = None,
    *,
    idempotency_key: str,
    max_attempts: int = 3,
    timeout_seconds: int = 300,
    actor_user_id: Optional[int] = None,
) -> Job:
    """Queue work, or return the job already queued under this key.

    `idempotency_key` is required and has no default on purpose. A default would
    be silently unique per call, which is the same as having no idempotency at
    all while looking like it has some.
    """
    if not idempotency_key:
        raise ValueError("idempotency_key is required")

    existing = Job.query.filter_by(
        job_type=job_type, idempotency_key=idempotency_key
    ).one_or_none()
    if existing is not None:
        return existing

    job = Job(
        job_type=job_type,
        idempotency_key=idempotency_key,
        payload_json=payload or {},
        max_attempts=max_attempts,
        timeout_seconds=timeout_seconds,
        actor_user_id=actor_user_id,
        state=QUEUED,
    )
    db.session.add(job)
    try:
        db.session.commit()
    except Exception:
        # Lost the race against another worker inserting the same key. The
        # unique constraint did its job; return the row that won.
        db.session.rollback()
        winner = Job.query.filter_by(
            job_type=job_type, idempotency_key=idempotency_key
        ).one_or_none()
        if winner is None:
            raise
        return winner
    return job


def claim_next(job_types: Optional[Iterable[str]] = None, worker: Optional[str] = None) -> Optional[Job]:
    """Atomically take the oldest queued job, or return None.

    The guarded UPDATE is the lock. Selecting a candidate and then writing it
    unconditionally would let two workers claim the same row; conditioning the
    write on `state = queued` and checking the row count means exactly one wins,
    on both SQLite and PostgreSQL, without advisory locks or SELECT FOR UPDATE.
    """
    worker = worker or worker_identity()
    query = Job.query.filter(Job.state == QUEUED)
    if job_types:
        query = query.filter(Job.job_type.in_(list(job_types)))

    # `id` is time-sortable, so ordering by it is oldest-first.
    for candidate in query.order_by(Job.id).limit(5).all():
        now = _utcnow()
        claimed = (
            db.session.query(Job)
            .filter(Job.id == candidate.id, Job.state == QUEUED)
            .update(
                {
                    "state": RUNNING,
                    "attempt": Job.attempt + 1,
                    "started_at": now,
                    "heartbeat_at": now,
                    "claimed_by": worker,
                },
                synchronize_session=False,
            )
        )
        db.session.commit()
        if claimed == 1:
            return db.session.get(Job, candidate.id)
    return None


def heartbeat(job: Job, *, step: Optional[str] = None, percent: Optional[int] = None) -> None:
    """Report liveness, and optionally progress.

    A job that does not heartbeat inside its timeout is treated as a dead
    worker. Long-running handlers must call this or they will be reaped and
    retried while still running.
    """
    job.heartbeat_at = _utcnow()
    if step is not None or percent is not None:
        progress = dict(job.progress_json or {})
        if step is not None:
            progress["step"] = step
        if percent is not None:
            progress["percent"] = percent
        job.progress_json = progress
    db.session.commit()


def _audit(job: Job, action: str, **extra: Any) -> None:
    """Record a terminal outcome against the person who asked for it.

    Only terminal states. A retry is visible in the job row and auditing each
    attempt would bury the outcome that matters in noise.

    `actor_user_id` is what makes this an audit trail rather than a log:
    scheduled work has none and reads as system, but an operator-triggered
    deploy names the operator, months later, without the job row still existing.
    """
    try:
        log_audit(
            action,
            actor_user_id=job.actor_user_id,
            target_type="job",
            target_id=job.id,
            details={
                "jobType": job.job_type,
                "attempt": job.attempt,
                "maxAttempts": job.max_attempts,
                "idempotencyKey": job.idempotency_key,
                "error": job.error,
                **extra,
            },
        )
    except Exception:  # noqa: BLE001
        # A failure to audit must not turn a finished job into a failed one, but
        # it must not pass silently either -- an audit trail with unexplained
        # gaps is worse than one known to be incomplete.
        logger.exception("failed to write audit record for job %s (%s)", job.id, action)


def succeed(job: Job) -> None:
    job.state = SUCCEEDED
    job.finished_at = _utcnow()
    job.error = None
    db.session.commit()
    _audit(job, "job_succeeded")


def fail(job: Job, error: str) -> None:
    """Retry if attempts remain, otherwise dead-letter.

    `failed` and `dead_letter` are distinct on purpose: the first means it ran
    out of attempts, the second that it was not worth retrying. Collapsing them
    loses the only signal that says whether retrying by hand is sensible.
    """
    job.error = (error or "")[:4000]
    exhausted = job.attempt >= job.max_attempts
    if exhausted:
        job.state = FAILED
        job.finished_at = _utcnow()
    else:
        job.state = QUEUED
        job.started_at = None
        job.heartbeat_at = None
        job.claimed_by = None
    db.session.commit()
    if exhausted:
        _audit(job, "job_failed")


def dead_letter(job: Job, error: str) -> None:
    job.state = DEAD_LETTER
    job.error = (error or "")[:4000]
    job.finished_at = _utcnow()
    db.session.commit()
    _audit(job, "job_dead_lettered")


def cancel(job: Job, *, actor_user_id: Optional[int] = None) -> bool:
    """Cancel a job that has not finished. Returns False if it already had.

    `actor_user_id` is whoever pressed cancel, which is not necessarily whoever
    enqueued it -- "who stopped this deploy" is a different question from "who
    started it", and an audit trail that cannot tell them apart answers neither.
    """
    if job.state in TERMINAL_STATES:
        return False
    job.state = CANCELLED
    job.finished_at = _utcnow()
    db.session.commit()
    _audit(job, "job_cancelled", cancelledBy=actor_user_id)
    return True


def reap_stale(*, now: Optional[datetime] = None) -> int:
    """Return jobs whose worker died to the queue. Returns how many.

    A row in `running` whose heartbeat is older than its timeout means the
    process holding it is gone -- the one failure the daemon threads could not
    even represent, since their work simply vanished.
    """
    now = now or _utcnow()
    reaped = 0
    abandoned: list[Job] = []
    for job in Job.query.filter(Job.state == RUNNING).all():
        last_seen = _aware(job.heartbeat_at) or _aware(job.started_at)
        if last_seen is None:
            continue
        if now - last_seen <= timedelta(seconds=job.timeout_seconds):
            continue

        message = f"worker {job.claimed_by or 'unknown'} stopped reporting"
        if job.attempt >= job.max_attempts:
            job.state = FAILED
            job.finished_at = now
            job.error = message
            abandoned.append(job)
        else:
            job.state = QUEUED
            job.started_at = None
            job.heartbeat_at = None
            job.claimed_by = None
            job.error = message
        reaped += 1
    if reaped:
        db.session.commit()
    # Audited after the commit: a job abandoned by a dead worker is exactly the
    # outcome nobody watched happen, so it is the one that most needs a record.
    for job in abandoned:
        _audit(job, "job_failed", reason="worker_abandoned")
    return reaped


def run_once(job_types: Optional[Iterable[str]] = None) -> Optional[Job]:
    """Claim one job, run its handler, record the outcome. Returns the job.

    Handlers must be idempotent: a job can run twice. A worker that completes
    the work and dies before committing `succeeded` leaves a row that will be
    reaped and retried, and no amount of care here removes that -- the honest
    response is to require idempotence rather than to pretend exactly-once.
    """
    job = claim_next(job_types)
    if job is None:
        return None

    fn = _HANDLERS.get(job.job_type)
    if fn is None:
        # Not retryable: another attempt cannot conjure a handler.
        dead_letter(job, f"no handler registered for {job.job_type!r}")
        return job

    try:
        fn(job)
    except PermanentJobError as exc:
        # Before JobError: PermanentJobError subclasses it, so the specific
        # handler has to come first or it would never be reached.
        dead_letter(job, str(exc))
    except JobError as exc:
        fail(job, str(exc))
    except Exception as exc:  # noqa: BLE001 -- a handler must not kill the worker
        logger.exception(
            "job %s (%s) raised; payload=%s",
            job.id,
            job.job_type,
            redact(job.payload_json),
        )
        fail(job, f"{type(exc).__name__}: {exc}")
    else:
        if job.state == RUNNING:
            succeed(job)
    return job
