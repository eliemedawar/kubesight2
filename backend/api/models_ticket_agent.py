"""Hermes ticket agent — Hermes reads each inbound ticket and handles it through MCP.

Two tables:

* :class:`TicketAgentSettings` — single row (id=1): the on/off switch, the
  confidence bar a decision must clear to run unattended, comment visibility,
  and the Telegram bot that carries the approval request when it does not.
* :class:`TicketInterpretation` — one row per task handed to Hermes (a new
  ticket, or a follow-up after the deploy finished). Hermes acts through the
  ``kubesight_ticket_*`` MCP tools, which write the outcome onto the row.

The tools are the guard rail: a deploy Hermes asks for must match a published
target exactly, and a request under the confidence bar (or contradicting the
ticket's own dropdowns) is turned into a Telegram approval instead of a run.
"""

from __future__ import annotations

from datetime import datetime, timezone

from .db import db


def _now():
    return datetime.now(timezone.utc)


class TicketAgentSettings(db.Model):
    """Single row (id=1)."""

    __tablename__ = "ticket_agent_settings"

    id = db.Column(db.Integer, primary_key=True)
    # Off by default: turning it on moves EVERY inbound ticket onto the Hermes
    # path (the deterministic auto-run no longer fires on its own).
    enabled = db.Column(db.Boolean, nullable=False, default=False)
    # The lowest Hermes confidence that executes without a human: "High" or
    # "Medium". Anything under it — and every decision that disagrees with the
    # ticket's own structured fields — goes to Telegram for approval instead.
    min_confidence = db.Column(db.String(16), nullable=False, default="High")
    # Hermes' comments are addressed to the requester, so they are public by
    # default (a private Desk comment is invisible to the person who asked).
    public_comments = db.Column(db.Boolean, nullable=False, default=True)
    # An approval nobody answers turns into an impediment after this long.
    approval_timeout_hours = db.Column(db.Integer, nullable=False, default=24)

    # --- Telegram approvals ---
    telegram_enabled = db.Column(db.Boolean, nullable=False, default=False)
    telegram_bot_token_encrypted = db.Column(db.Text, nullable=True)
    # Where approval requests are posted (a group id like -100123…, or a user id).
    telegram_chat_id = db.Column(db.String(64), nullable=True)
    # Who may press Approve/Reject: comma-separated Telegram user ids and/or
    # @usernames. Empty = anyone in the chat.
    telegram_approvers = db.Column(db.Text, nullable=True)
    # getUpdates offset — the next update id to ask for, so a restart neither
    # replays old button presses nor loses new ones.
    telegram_update_offset = db.Column(db.BigInteger, nullable=True)

    last_test_at = db.Column(db.DateTime(timezone=True), nullable=True)
    last_test_status = db.Column(db.String(16), nullable=True)
    last_test_message = db.Column(db.Text, nullable=True)

    created_at = db.Column(db.DateTime(timezone=True), nullable=False, default=_now)
    updated_at = db.Column(db.DateTime(timezone=True), nullable=False, default=_now, onupdate=_now)


class TicketInterpretation(db.Model):
    """One Hermes task on one ticket, and what came of it.

    ``kind``:
      handle    a new ticket: Hermes reads it and acts through the ticket tools
      followup  something happened after Hermes acted (the run finished, an
                approval was rejected or expired) — Hermes writes the comment and
                moves the ticket; ``event`` says what happened

    ``status``:
      pending            queued; the tick hands it to a worker
      running            a worker is waiting on Hermes (``heartbeat_at``)
      executed           Hermes started a deploy run (``run_id``)
      awaiting_approval  Hermes asked for approval (Telegram / UI buttons)
      impediment         Hermes parked the ticket (or an approval was refused)
      on_hold            Hermes parked it waiting on something (not a defect)
      done               Hermes closed the ticket / wrote the follow-up
      error              Hermes failed or finished without acting (retryable)
      superseded         an operator asked Hermes again; a newer task replaced it

    The tools write the outcome columns while Hermes is still running; the
    worker only decides, once Hermes returns, whether anything was recorded.

    A ticket Hermes parked (impediment / on hold) is picked up again when the
    requester comments: a new ``handle`` task whose ``event`` is
    ``{"type": "requester_replied", "comments": [...]}``.
    """

    __tablename__ = "ticket_interpretations"
    __table_args__ = (
        db.Index("ix_ticket_interp_status", "status"),
        db.Index("ix_ticket_interp_ticket", "ticket_record_id"),
    )

    id = db.Column(db.Integer, primary_key=True)
    ticket_record_id = db.Column(
        db.Integer, db.ForeignKey("zoho_inbound_tickets.id", ondelete="CASCADE"), nullable=True
    )
    provider = db.Column(db.String(16), nullable=False, default="zoho")
    kind = db.Column(db.String(16), nullable=False, default="handle")
    # For a follow-up: {"type": "run_finished"|"approval_rejected"|"approval_expired", ...}
    # plus the fallback comment KubeSight posts itself if Hermes cannot.
    event = db.Column(db.JSON, nullable=True)
    ticket_number = db.Column(db.String(64), nullable=True)
    status = db.Column(db.String(24), nullable=False, default="pending")
    # How many times the task was handed to Hermes (retries after an outage).
    attempts = db.Column(db.Integer, nullable=False, default=0)

    # --- What Hermes did (written by the ticket tools) ---
    # The action Hermes executed or proposed, as the tool received it
    # (action/target/change/confidence/concerns, plus commentOnApprove).
    decision = db.Column(db.JSON, nullable=True)
    # Hermes' closing message for the task (its own one-line summary).
    final_message = db.Column(db.Text, nullable=True)
    confidence = db.Column(db.String(16), nullable=True)
    understanding = db.Column(db.Text, nullable=True)
    # The last comment Hermes wrote onto the ticket through the tools.
    comment = db.Column(db.Text, nullable=True)
    questions = db.Column(db.JSON, nullable=True)

    # --- What KubeSight made of it ---
    # execute | approval | impediment | status — which tool settled the task.
    route = db.Column(db.String(16), nullable=True)
    # image | env_var | restart
    change_type = db.Column(db.String(16), nullable=True)
    snapshot_id = db.Column(db.Integer, nullable=True)
    cluster_id = db.Column(db.String(120), nullable=True)
    namespace = db.Column(db.String(253), nullable=True)
    deployment_name = db.Column(db.String(253), nullable=True)
    tag = db.Column(db.String(200), nullable=True)
    variable_name = db.Column(db.Text, nullable=True)
    variable_value = db.Column(db.Text, nullable=True)
    # Why it needed approval (Hermes' reasons + the tool's own checks).
    reasons = db.Column(db.JSON, nullable=True)
    run_id = db.Column(db.Integer, nullable=True)
    error = db.Column(db.Text, nullable=True)
    model = db.Column(db.String(120), nullable=True)

    # --- Approval ---
    telegram_chat_id = db.Column(db.String(64), nullable=True)
    telegram_message_id = db.Column(db.BigInteger, nullable=True)
    # Random per-request token carried in the button payload, so a forged or
    # stale callback for the same row id cannot decide it.
    approval_nonce = db.Column(db.String(32), nullable=True)
    expires_at = db.Column(db.DateTime(timezone=True), nullable=True)
    decided_by = db.Column(db.String(120), nullable=True)
    decided_at = db.Column(db.DateTime(timezone=True), nullable=True)
    decision_note = db.Column(db.Text, nullable=True)

    heartbeat_at = db.Column(db.DateTime(timezone=True), nullable=True)
    # A pending row is not picked up before this (backoff after a Hermes outage).
    retry_at = db.Column(db.DateTime(timezone=True), nullable=True)
    started_at = db.Column(db.DateTime(timezone=True), nullable=True)
    finished_at = db.Column(db.DateTime(timezone=True), nullable=True)
    requested_by = db.Column(db.String(120), nullable=True)
    created_at = db.Column(db.DateTime(timezone=True), nullable=False, default=_now, index=True)
    updated_at = db.Column(db.DateTime(timezone=True), nullable=False, default=_now, onupdate=_now)


class TicketAgentPostedComment(db.Model):
    """A fingerprint of every comment KubeSight posted on a ticket.

    Zoho reports a comment-added event for OUR comments too (they are posted
    through the same Desk account). Without this, Hermes' own "which
    environment?" would come straight back as a requester reply and wake it
    again — forever. A comment whose normalised text matches one of these is
    ignored.
    """

    __tablename__ = "ticket_agent_posted_comments"
    __table_args__ = (db.Index("ix_ticket_agent_posted_ticket", "ticket_record_id"),)

    id = db.Column(db.Integer, primary_key=True)
    ticket_record_id = db.Column(
        db.Integer, db.ForeignKey("zoho_inbound_tickets.id", ondelete="CASCADE"), nullable=True
    )
    digest = db.Column(db.String(64), nullable=False)
    created_at = db.Column(db.DateTime(timezone=True), nullable=False, default=_now)
