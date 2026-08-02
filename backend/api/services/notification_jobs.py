"""Email delivery as durable jobs.

Every outbound email in this codebase was sent from a `threading.Thread` whose
body ended in `except Exception: pass`. Two failures follow from that, and the
second is the one that matters.

A send that fails is silently dropped -- no retry, no record, no error. And a
process that restarts between spawning the thread and the SMTP handshake drops
the mail with no trace it was ever attempted.

That is tolerable for a newsletter. These are password resets, MFA changes and
deploy-failure notifications: the product tells an operator "we will email you
when this happens", and the failure mode was that it silently did not. For a
platform sold on auditability, a notification nobody can prove was attempted is
worse than one that visibly failed.

As a job it retries, records its outcome, and is attributable. The cost is that
**a worker must be running** -- see `enqueue_email` for what happens when one is
not.
"""

from __future__ import annotations

import hashlib
import logging
from typing import Optional

from ..email_delivery import EmailDeliveryError, send_email
from ..models_jobs import Job
from . import job_queue

logger = logging.getLogger(__name__)

EMAIL_JOB = "notify.email"


def _fingerprint(to_address: str, subject: str, body: str) -> str:
    """Stable key for one logical message.

    Deliberately content-derived rather than random: a caller retrying after a
    failed request should not send a second copy of the same mail. Callers that
    genuinely want a second copy -- a resend the operator asked for -- pass
    their own key.
    """
    digest = hashlib.sha256(
        "\x00".join([to_address, subject, body]).encode("utf-8")
    ).hexdigest()
    return f"email:{digest[:32]}"


def enqueue_email(
    to_address: str,
    subject: str,
    body: str,
    *,
    html_body: Optional[str] = None,
    idempotency_key: Optional[str] = None,
    actor_user_id: Optional[int] = None,
) -> Optional[Job]:
    """Queue an email. Returns the job, or None when there is nothing to send.

    **This needs a worker.** Nothing is delivered until one drains the queue, so
    a deployment without `worker.py` collects mail instead of sending it. That
    is a visible failure -- queued jobs are countable and the count grows --
    where the thread it replaces failed invisibly. Preferring a backlog you can
    see to a silence you cannot is the whole point.
    """
    if not to_address or "@" not in to_address:
        # Not an error: plenty of users have no address, and the old code
        # returned early for exactly this.
        return None

    return job_queue.enqueue(
        EMAIL_JOB,
        {
            "to": to_address,
            "subject": subject,
            "body": body,
            "htmlBody": html_body,
        },
        idempotency_key=idempotency_key or _fingerprint(to_address, subject, body),
        max_attempts=4,
        timeout_seconds=120,
        actor_user_id=actor_user_id,
    )


@job_queue.handler(EMAIL_JOB)
def _deliver(job: Job) -> None:
    payload = job.payload_json or {}
    to_address = payload.get("to") or ""
    try:
        send_email(
            to_address,
            payload.get("subject") or "",
            payload.get("body") or "",
            html_body=payload.get("htmlBody"),
        )
    except EmailDeliveryError as exc:
        # SMTP not configured, or the address unusable. Another attempt cannot
        # fix either, so dead-letter rather than burn four attempts on a
        # certainty -- and dead_letter means "somebody should look", which is
        # true here and misleading for a refused connection.
        raise job_queue.PermanentJobError(str(exc)) from exc
    # Anything else -- a refused connection, a timeout, a greylisting 4xx --
    # propagates and is retried, which is the case the thread handled worst:
    # it swallowed the exception and the mail was simply never sent.
