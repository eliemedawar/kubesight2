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
from api.models import AuditLog
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
    """`ORDER BY id` has to be oldest-first: claim_next depends on it.

    The first version of this test allowed `ids == sorted(ids) OR all unique`,
    and every id is unique, so the ordering half never ran. It passed against an
    implementation where 52% of same-millisecond pairs sorted backwards.
    """
    ids = [new_job_id() for _ in range(5000)]
    assert ids == sorted(ids), "ids minted in one burst must sort in mint order"
    assert len(set(ids)) == len(ids), "and still be unique"


def test_ids_stay_ordered_when_the_clock_does_not_move(monkeypatch):
    """The case that broke it: a frozen clock must not produce ties."""
    import api.models_jobs as mod

    monkeypatch.setattr(mod.time, "time", lambda: 1_700_000_000.0)
    ids = [new_job_id() for _ in range(1000)]
    assert ids == sorted(ids)
    assert len(set(ids)) == len(ids)


def test_ids_survive_a_clock_stepping_backwards(monkeypatch):
    """NTP correction must not mint ids that sort before existing rows."""
    import api.models_jobs as mod

    monkeypatch.setattr(mod.time, "time", lambda: 1_700_000_000.0)
    before = new_job_id()
    monkeypatch.setattr(mod.time, "time", lambda: 1_600_000_000.0)
    assert new_job_id() > before


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


# ─── audit attribution ───
#
# The reason actor_user_id exists. A job row can be pruned; the audit record is
# what still answers "who asked for this deploy" months later.


def _audits(action: str):
    return AuditLog.query.filter_by(action=action, target_type="job").all()


def test_success_is_audited_against_the_person_who_asked(ctx):
    @job_queue.handler("deploy.execute")
    def _run(job):
        return None

    job_queue.enqueue("deploy.execute", idempotency_key="k", actor_user_id=7)
    job_queue.run_once()

    entries = _audits("job_succeeded")
    assert len(entries) == 1
    assert entries[0].actor_user_id == 7
    assert entries[0].details["jobType"] == "deploy.execute"


def test_a_retry_is_not_audited_but_the_final_failure_is(ctx):
    """Auditing every attempt buries the outcome that matters in noise."""
    job_queue.enqueue("deploy.execute", idempotency_key="k", max_attempts=2)

    job = job_queue.claim_next()
    job_queue.fail(job, "first")
    assert _audits("job_failed") == [], "a retry is visible in the job row"

    job = job_queue.claim_next()
    job_queue.fail(job, "second")
    assert len(_audits("job_failed")) == 1


def test_dead_letter_is_audited(ctx):
    job_queue.enqueue("deploy.execute", idempotency_key="k")
    job_queue.dead_letter(job_queue.claim_next(), "malformed")
    assert len(_audits("job_dead_lettered")) == 1


def test_cancellation_records_who_cancelled_not_only_who_asked(ctx):
    """Who stopped this deploy is a different question from who started it."""
    job_queue.enqueue("deploy.execute", idempotency_key="k", actor_user_id=7)
    job_queue.cancel(job_queue.claim_next(), actor_user_id=99)

    entry = _audits("job_cancelled")[0]
    assert entry.actor_user_id == 7, "the job still belongs to whoever asked"
    assert entry.details["cancelledBy"] == 99


def test_a_job_abandoned_by_a_dead_worker_is_audited(ctx):
    """The outcome nobody watched happen is the one that most needs a record."""
    job_queue.enqueue(
        "deploy.execute", idempotency_key="k", max_attempts=1, timeout_seconds=1
    )
    job_queue.claim_next()
    job_queue.reap_stale(now=datetime.now(timezone.utc) + timedelta(seconds=120))

    entry = _audits("job_failed")[0]
    assert entry.details["reason"] == "worker_abandoned"


def test_a_failing_audit_does_not_fail_the_job(ctx, monkeypatch):
    """An audit outage must not turn a finished deploy into a failed one."""
    monkeypatch.setattr(
        job_queue, "log_audit", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("audit down"))
    )

    @job_queue.handler("deploy.execute")
    def _run(job):
        return None

    job_queue.enqueue("deploy.execute", idempotency_key="k")
    job = job_queue.run_once()
    assert job.state == SUCCEEDED
