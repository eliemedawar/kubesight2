"""Webhook triggers: a URL that starts an ordinary build when something calls it.

Bolted onto the CI domain the way schedules are: the tables reference
``ci_services``, but nothing on the build path reads them. A webhook build is a
normal :class:`CiBuild` with ``trigger_type='webhook'`` and the trigger's name
in its snapshot — runners, caches, logs, masking and restart safety apply with
no second execution path.

Two kinds share one table and one inbound URL scheme:

* ``generic`` — anything that can POST: a script, Nexus, another CI, a release
  tool. The request may name the ref and set build inputs, but only within what
  the trigger allows.
* ``bitbucket_push`` — Bitbucket's ``repo:push``: every pushed branch or tag
  that matches the trigger's filters is built at the commit that was pushed.
"""

from __future__ import annotations

from datetime import datetime, timezone

from .db import db


def _now():
    return datetime.now(timezone.utc)


WEBHOOK_KINDS = ("generic", "bitbucket_push")

# What happened to one delivery.
#   triggered — at least one build was queued (``build_ids`` names them)
#   ignored   — deliberately nothing: switched off, a ref outside the filters,
#               the previous build still running, a branch deletion
#   duplicate — a redelivery of one already handled; nothing new queued
#   refused   — the request asked for something the trigger does not allow
#               (an input it may not set, a value the pipeline rejects)
#   failed    — it should have built and could not (pipeline gone, the owner
#               lost the right to build, service paused)
DELIVERY_OUTCOMES = ("triggered", "ignored", "duplicate", "refused", "failed")


class CiWebhookTrigger(db.Model):
    """One inbound URL and what a call to it builds."""

    __tablename__ = "ci_webhook_triggers"
    __table_args__ = (
        db.UniqueConstraint("service_id", "name", name="uq_ci_webhook_service_name"),
    )

    id = db.Column(db.Integer, primary_key=True)
    service_id = db.Column(
        db.Integer,
        db.ForeignKey("ci_services.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    name = db.Column(db.String(120), nullable=False)
    kind = db.Column(db.String(24), nullable=False, default="generic")
    # The URL path segment. Random and unguessable, but NOT the credential:
    # the secret below is, so the URL can be shown to anybody who can see the
    # service without handing them the ability to start builds.
    public_id = db.Column(db.String(48), nullable=False, unique=True, index=True)
    secret_encrypted = db.Column(db.Text, nullable=True)

    # NULL is "the service's default build pipeline, whichever that is when the
    # call arrives". Not a foreign key, for the reason CiSchedule gives.
    pipeline_id = db.Column(db.Integer, nullable=True)
    # The ref built when the request names none (generic only). NULL branch is
    # the service's default branch at delivery time.
    branch = db.Column(db.String(255), nullable=True)
    ref_type = db.Column(db.String(8), nullable=False, default="branch")
    # Fixed build-input values every build from this trigger gets.
    variables = db.Column(db.JSON, nullable=False, default=dict)

    # Generic: the build inputs a request body may set under "variables", and
    # whether it may name the branch / tag / commit itself.
    allowed_inputs = db.Column(db.JSON, nullable=False, default=list)
    allow_ref_override = db.Column(db.Boolean, nullable=False, default=False)
    # Generic: values lifted out of a body KubeSight does not control —
    # [{"target": "VERSION" | "ref:branch" | "ref:tag" | "ref:commit",
    #   "path": "release.tag_name"}].
    mappings = db.Column(db.JSON, nullable=False, default=list)

    # Which refs may build, as fnmatch patterns. Empty branch filters means any
    # branch; tags build only with ``build_tags`` (push) or when a request
    # names one (generic), and then only those matching ``tag_filters``.
    branch_filters = db.Column(db.JSON, nullable=False, default=list)
    build_tags = db.Column(db.Boolean, nullable=False, default=False)
    tag_filters = db.Column(db.JSON, nullable=False, default=list)

    enabled = db.Column(db.Boolean, nullable=False, default=True)
    skip_if_running = db.Column(db.Boolean, nullable=False, default=False)

    last_delivery_at = db.Column(db.DateTime(timezone=True), nullable=True)
    last_outcome = db.Column(db.String(16), nullable=True)
    last_message = db.Column(db.Text, nullable=True)
    last_build_id = db.Column(
        db.Integer, db.ForeignKey("ci_builds.id", ondelete="SET NULL"), nullable=True
    )
    # A call with the wrong secret is not logged as a delivery (anybody can
    # send one), but the trigger remembers the last one: "Bitbucket says it
    # delivered and nothing happened" is usually a secret that does not match.
    last_rejected_at = db.Column(db.DateTime(timezone=True), nullable=True)

    created_by_user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=True)
    # Whoever last saved it — and so whose rights its builds run with,
    # re-checked on every delivery, the rule schedules follow.
    updated_by_user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=True)
    created_at = db.Column(db.DateTime(timezone=True), nullable=False, default=_now)
    updated_at = db.Column(
        db.DateTime(timezone=True), nullable=False, default=_now, onupdate=_now
    )

    service = db.relationship(
        "CiService",
        backref=db.backref("webhook_triggers", cascade="all, delete-orphan", lazy="dynamic"),
    )
    last_build = db.relationship("CiBuild", foreign_keys=[last_build_id])
    created_by = db.relationship("User", foreign_keys=[created_by_user_id])
    updated_by = db.relationship("User", foreign_keys=[updated_by_user_id])


class CiWebhookDelivery(db.Model):
    """One authenticated call to a trigger, and what it led to.

    A short rolling log (the newest few dozen per trigger), because the first
    question after wiring a webhook is always "did it arrive, and why did it
    not build?" — and the answer is here rather than in a server log.
    """

    __tablename__ = "ci_webhook_deliveries"
    __table_args__ = (
        db.Index("ix_ci_webhook_delivery_trigger_time", "trigger_id", "received_at"),
    )

    id = db.Column(db.Integer, primary_key=True)
    trigger_id = db.Column(
        db.Integer,
        db.ForeignKey("ci_webhook_triggers.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    received_at = db.Column(db.DateTime(timezone=True), nullable=False, default=_now)
    # "repo:push", "generic", "test" — what the sender said it was.
    event = db.Column(db.String(64), nullable=True)
    # The sender's own delivery id (X-Request-UUID, X-GitHub-Delivery,
    # Idempotency-Key), so a redelivery is recognised and not built twice.
    delivery_key = db.Column(db.String(128), nullable=True, index=True)
    outcome = db.Column(db.String(16), nullable=False)
    message = db.Column(db.Text, nullable=True)
    # [{"type": "branch"|"tag", "name": ..., "commit": ...}] — what was asked.
    refs = db.Column(db.JSON, nullable=False, default=list)
    build_ids = db.Column(db.JSON, nullable=False, default=list)
    # The SHAPE of a generic body — dotted paths, never values — so the
    # mapping form can offer what the sender actually sends without KubeSight
    # keeping a copy of whatever it sent.
    payload_paths = db.Column(db.JSON, nullable=False, default=list)
    # Who pressed "Start a test build", for a delivery made from the page.
    tested_by_user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=True)

    trigger = db.relationship(
        "CiWebhookTrigger",
        backref=db.backref("deliveries", cascade="all, delete-orphan", lazy="dynamic"),
    )
    tested_by = db.relationship("User", foreign_keys=[tested_by_user_id])
