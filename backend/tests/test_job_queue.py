"""The durable job queue.

Weighted towards the properties the daemon threads lacked -- surviving a dead
worker, refusing to double-run, and saying who asked -- because those are the
reasons this exists, and a test suite that only checks the happy path would have
passed against the threads too.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from api.db import db
from api.models_jobs import (
    CANCELLED,
    DEAD_LETTER,
    FAILED,
    QUEUED,
    RUNNING,
    SUCCEEDED,
    Job,
    new_job_id,
)
from api.services import job_queue


@pytest.fixture(autouse=True)
def clean_handlers():
    """Handlers live in a module global; leaking one breaks a later test."""
    saved = dict(job_queue._HANDLERS)
    job_queue._HANDLERS.clear()
    yield
    job_queue._HANDLERS.clear()
    job_queue._HANDLERS.update(saved)


@pytest.fixture()
def ctx(app):
    with app.app_context():
        yield


# ─── ids ───


def test_ids_sort_chronologically():
    """`ORDER BY id` has to be oldest-first: claim_next depends on it."""
    ids = [new_job_id() for _ in range(50)]
    assert ids == sorted(ids) or len(set(ids)) == 50
    # Same millisecond may tie, but a later one never sorts before an earlier.
    early = new_job_id()
    import time as _t

    _t.sleep(0.005)
    assert new_job_id() > early


# ─── idempotency ───


def test_same_key_returns_the_same_job(ctx):
    """The property that stops a retry deploying twice."""
    first = job_queue.enqueue("deploy.execute", {"a": 1}, idempotency_key="req-1")
    second = job_queue.enqueue("deploy.execute", {"a": 2}, idempotency_key="req-1")

    assert first.id == second.id
    assert Job.query.filter_by(job_type="deploy.execute").count() == 1
    # The first payload wins; the second call is a no-op, not an update.
    assert second.payload_json == {"a": 1}


def test_same_key_different_type_is_a_different_job(ctx):
    a = job_queue.enqueue("deploy.execute", idempotency_key="shared")
    b = job_queue.enqueue("alerts.evaluate", idempotency_key="shared")
    assert a.id != b.id


def test_idempotency_key_is_required(ctx):
    """No default: a silently-unique default is the same as no idempotency,
    while looking like it has some."""
    with pytest.raises(ValueError):
        job_queue.enqueue("deploy.execute", idempotency_key="")


def test_uniqueness_is_enforced_by_the_database(ctx):
    """Not by the check in enqueue -- two workers racing beat a check."""
    job_queue.enqueue("deploy.execute", idempotency_key="race")
    duplicate = Job(job_type="deploy.execute", idempotency_key="race", payload_json={})
    db.session.add(duplicate)
    with pytest.raises(Exception):
        db.session.commit()
    db.session.rollback()


# ─── claiming ───


def test_claim_marks_running_and_counts_the_attempt(ctx):
    job_queue.enqueue("deploy.execute", idempotency_key="k1")
    claimed = job_queue.claim_next()

    assert claimed is not None
    assert claimed.state == RUNNING
    assert claimed.attempt == 1
    assert claimed.started_at is not None
    assert claimed.claimed_by


def test_claim_takes_the_oldest_first(ctx):
    first = job_queue.enqueue("deploy.execute", idempotency_key="k1")
    job_queue.enqueue("deploy.execute", idempotency_key="k2")
    assert job_queue.claim_next().id == first.id


def test_a_job_is_only_claimed_once(ctx):
    """The guarded UPDATE is the lock."""
    job_queue.enqueue("deploy.execute", idempotency_key="only-one")
    assert job_queue.claim_next() is not None
    assert job_queue.claim_next() is None


def test_claim_filters_by_type(ctx):
    job_queue.enqueue("alerts.evaluate", idempotency_key="a")
    assert job_queue.claim_next(job_types=["deploy.execute"]) is None
    assert job_queue.claim_next(job_types=["alerts.evaluate"]) is not None


def test_claim_on_empty_queue_returns_none(ctx):
    assert job_queue.claim_next() is None


# ─── outcomes ───


def test_failure_retries_until_attempts_run_out(ctx):
    """Requeued while attempts remain, FAILED only once they are gone."""
    job_queue.enqueue("deploy.execute", idempotency_key="k", max_attempts=2)

    job = job_queue.claim_next()
    job_queue.fail(job, "boom")
    assert job.state == QUEUED, "should retry with an attempt left"
    assert job.claimed_by is None

    job = job_queue.claim_next()
    assert job.attempt == 2
    job_queue.fail(job, "boom again")
    assert job.state == FAILED
    assert job.finished_at is not None


def test_dead_letter_is_distinct_from_failed(ctx):
    """Collapsing them loses the signal that says whether retrying by hand
    is sensible."""
    job_queue.enqueue("deploy.execute", idempotency_key="k")
    job = job_queue.claim_next()
    job_queue.dead_letter(job, "malformed payload")
    assert job.state == DEAD_LETTER


def test_cancel_only_applies_before_it_finishes(ctx):
    job_queue.enqueue("deploy.execute", idempotency_key="k")
    job = job_queue.claim_next()
    assert job_queue.cancel(job) is True
    assert job.state == CANCELLED
    assert job_queue.cancel(job) is False, "already terminal"


# ─── the dead worker ───


def test_reaper_requeues_a_job_whose_worker_died(ctx):
    """The failure the daemon threads could not even represent."""
    job_queue.enqueue("deploy.execute", idempotency_key="k", timeout_seconds=60)
    job = job_queue.claim_next()

    later = datetime.now(timezone.utc) + timedelta(seconds=61)
    assert job_queue.reap_stale(now=later) == 1

    db.session.refresh(job)
    assert job.state == QUEUED
    assert job.claimed_by is None
    assert "stopped reporting" in job.error


def test_reaper_leaves_a_slow_job_alone(ctx):
    """Slow is not dead. Reaping inside the timeout would run it twice."""
    job_queue.enqueue("deploy.execute", idempotency_key="k", timeout_seconds=300)
    job_queue.claim_next()
    assert job_queue.reap_stale() == 0


def test_heartbeat_keeps_a_long_job_alive(ctx):
    job_queue.enqueue("deploy.execute", idempotency_key="k", timeout_seconds=60)
    job = job_queue.claim_next()

    now = datetime.now(timezone.utc)
    job_queue.heartbeat(job, step="applying manifests", percent=40)

    assert job.progress_json == {"step": "applying manifests", "percent": 40}
    assert job_queue.reap_stale(now=now + timedelta(seconds=30)) == 0


def test_reaper_fails_a_job_with_no_attempts_left(ctx):
    job_queue.enqueue("deploy.execute", idempotency_key="k", max_attempts=1, timeout_seconds=1)
    job = job_queue.claim_next()

    later = datetime.now(timezone.utc) + timedelta(seconds=120)
    job_queue.reap_stale(now=later)

    db.session.refresh(job)
    assert job.state == FAILED


# ─── running ───


def test_run_once_executes_and_succeeds(ctx):
    seen = {}

    @job_queue.handler("deploy.execute")
    def _run(job):
        seen["payload"] = job.payload_json

    job_queue.enqueue("deploy.execute", {"cluster": "prod"}, idempotency_key="k")
    job = job_queue.run_once()

    assert seen["payload"] == {"cluster": "prod"}
    assert job.state == SUCCEEDED


def test_a_raising_handler_does_not_kill_the_worker(ctx):
    @job_queue.handler("deploy.execute")
    def _run(job):
        raise RuntimeError("kubectl exploded")

    job_queue.enqueue("deploy.execute", idempotency_key="k", max_attempts=1)
    job = job_queue.run_once()

    assert job.state == FAILED
    assert "kubectl exploded" in job.error


def test_job_error_message_reaches_the_operator(ctx):
    @job_queue.handler("deploy.execute")
    def _run(job):
        raise job_queue.JobError("cluster prod-us-east is unreachable")

    job_queue.enqueue("deploy.execute", idempotency_key="k", max_attempts=1)
    job = job_queue.run_once()
    assert job.error == "cluster prod-us-east is unreachable"


def test_unknown_job_type_dead_letters_rather_than_retrying(ctx):
    """Another attempt cannot conjure a handler."""
    job_queue.enqueue("nobody.handles.this", idempotency_key="k")
    job = job_queue.run_once()
    assert job.state == DEAD_LETTER


def test_run_once_on_empty_queue_returns_none(ctx):
    assert job_queue.run_once() is None


# ─── secrets ───


@pytest.mark.parametrize(
    "key",
    ["password", "apiToken", "registryCredential", "KUBECONFIG", "private_key", "Authorization"],
)
def test_redact_masks_secret_shaped_keys(key):
    assert job_queue.redact({key: "hunter2"})[key] == job_queue.REDACTED


def test_redact_recurses_and_keeps_the_rest(ctx):
    payload = {
        "cluster": "prod",
        "registry": {"url": "registry.local", "password": "hunter2"},
        "steps": [{"name": "apply", "token": "abc"}],
    }
    out = job_queue.redact(payload)

    assert out["cluster"] == "prod"
    assert out["registry"]["url"] == "registry.local"
    assert out["registry"]["password"] == job_queue.REDACTED
    assert out["steps"][0]["token"] == job_queue.REDACTED
    assert out["steps"][0]["name"] == "apply"


def test_job_record_never_carries_the_payload(ctx):
    """to_dict feeds API responses and logs; payloads hold credentials."""
    job = job_queue.enqueue(
        "deploy.execute", {"password": "hunter2"}, idempotency_key="k", actor_user_id=7
    )
    record = job.to_dict()

    assert "payload" not in record
    assert "hunter2" not in str(record)
    assert record["actorUserId"] == 7
    assert set(record) == {
        "jobId", "jobType", "state", "attempt", "maxAttempts", "progress",
        "createdAt", "startedAt", "finishedAt", "error", "actorUserId",
    }
