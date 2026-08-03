"""Email as durable jobs.

Every send used to happen in a daemon thread ending in `except Exception: pass`.
These are password resets, MFA changes and deploy-failure notices -- the product
promises to send them, and the old failure mode was that it silently did not.
"""

from __future__ import annotations

import pytest

from api.email_delivery import EmailDeliveryError
from api.models_jobs import DEAD_LETTER, FAILED, QUEUED, SUCCEEDED
from api.services import job_queue, notification_jobs


@pytest.fixture()
def ctx(app):
    with app.app_context():
        yield


def test_queued_rather_than_sent_inline(ctx):
    job = notification_jobs.enqueue_email("ops@example.com", "Subject", "Body")
    assert job is not None and job.state == QUEUED
    assert job.job_type == notification_jobs.EMAIL_JOB


def test_the_same_message_twice_queues_once(ctx):
    """A retried request must not send the operator a second copy."""
    a = notification_jobs.enqueue_email("ops@example.com", "Password changed", "Body")
    b = notification_jobs.enqueue_email("ops@example.com", "Password changed", "Body")
    assert a.id == b.id


def test_a_different_message_is_a_different_job(ctx):
    a = notification_jobs.enqueue_email("ops@example.com", "One", "Body")
    b = notification_jobs.enqueue_email("ops@example.com", "Two", "Body")
    assert a.id != b.id


def test_an_explicit_key_allows_a_deliberate_resend(ctx):
    a = notification_jobs.enqueue_email("ops@example.com", "S", "B")
    b = notification_jobs.enqueue_email("ops@example.com", "S", "B", idempotency_key="resend-1")
    assert a.id != b.id


def test_no_address_is_not_an_error(ctx):
    """Plenty of users have none; the thread returned early for this too."""
    assert notification_jobs.enqueue_email("", "S", "B") is None
    assert notification_jobs.enqueue_email("not-an-address", "S", "B") is None


def test_delivery_marks_the_job_succeeded(ctx, monkeypatch):
    sent = {}
    monkeypatch.setattr(
        notification_jobs,
        "send_email",
        lambda to, subject, body, html_body=None: sent.update(to=to, subject=subject),
    )
    notification_jobs.enqueue_email("ops@example.com", "Subject", "Body")
    job = job_queue.run_once([notification_jobs.EMAIL_JOB])

    assert job.state == SUCCEEDED
    assert sent["to"] == "ops@example.com"


def test_a_transient_failure_is_retried(ctx, monkeypatch):
    """The case the thread handled worst: it swallowed this and sent nothing."""
    def refuse(*a, **k):
        raise ConnectionRefusedError("smtp down")

    monkeypatch.setattr(notification_jobs, "send_email", refuse)
    notification_jobs.enqueue_email("ops@example.com", "S", "B")

    job = job_queue.run_once([notification_jobs.EMAIL_JOB])
    assert job.state == QUEUED, "a refused connection must be retried"
    assert job.attempt == 1


def test_unconfigured_smtp_dead_letters_instead_of_retrying(ctx, monkeypatch):
    """Another attempt cannot configure SMTP; four tries only delay the news."""
    def unconfigured(*a, **k):
        raise EmailDeliveryError("SMTP is not configured.")

    monkeypatch.setattr(notification_jobs, "send_email", unconfigured)
    notification_jobs.enqueue_email("ops@example.com", "S", "B")

    job = job_queue.run_once([notification_jobs.EMAIL_JOB])
    assert job.state == DEAD_LETTER
    assert job.attempt == 1, "should not have burned its retry budget"


def test_a_transient_failure_eventually_fails_not_dead_letters(ctx, monkeypatch):
    """failed and dead_letter answer different questions; keep them distinct."""
    monkeypatch.setattr(
        notification_jobs,
        "send_email",
        lambda *a, **k: (_ for _ in ()).throw(ConnectionRefusedError("down")),
    )
    from api.db import db
    from api.models_jobs import Job

    notification_jobs.enqueue_email("ops@example.com", "S", "B", idempotency_key="k")
    row = Job.query.filter_by(idempotency_key="k").one()
    row.max_attempts = 2
    db.session.commit()

    job_queue.run_once([notification_jobs.EMAIL_JOB])
    job = job_queue.run_once([notification_jobs.EMAIL_JOB])
    assert job.state == FAILED
