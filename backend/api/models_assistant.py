"""KubeSight assistant — the in-app chat with Hermes (Ctrl+G, admins only).

The same Hermes the operators talk to on Telegram — the one with the
``kubesight`` skill and KubeSight's MCP tools — answering inside KubeSight.

Two tables:

* :class:`AssistantConversation` — one chat thread, owned by the admin who
  started it. Nobody else reads it, other admins included.
* :class:`AssistantMessage` — one turn. A reply is written as a ``pending``
  row the moment the question is sent; a worker thread asks Hermes and fills
  it in (``done``) or explains what went wrong (``failed``). The UI polls the
  row — filled in as Hermes streams — so a slow agent loop never hangs on an
  HTTP request and the answer appears as it is written.
"""

from __future__ import annotations

from datetime import datetime, timezone

from .db import db


def _now():
    return datetime.now(timezone.utc)


class AssistantConversation(db.Model):
    __tablename__ = "assistant_conversations"

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    # The first question, shortened. Shown in the history list.
    title = db.Column(db.String(200), nullable=False, default="New chat")
    created_at = db.Column(db.DateTime(timezone=True), nullable=False, default=_now)
    updated_at = db.Column(db.DateTime(timezone=True), nullable=False, default=_now, index=True)

    messages = db.relationship(
        "AssistantMessage",
        backref="conversation",
        cascade="all, delete-orphan",
        order_by="AssistantMessage.id",
        lazy="select",
    )


class AssistantMessage(db.Model):
    __tablename__ = "assistant_messages"

    id = db.Column(db.Integer, primary_key=True)
    conversation_id = db.Column(
        db.Integer,
        db.ForeignKey("assistant_conversations.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    # "user" | "assistant"
    role = db.Column(db.String(16), nullable=False)
    content = db.Column(db.Text, nullable=False, default="")
    # "pending" | "done" | "failed" (user rows are always "done")
    status = db.Column(db.String(16), nullable=False, default="done")
    error = db.Column(db.Text, nullable=True)
    # While a reply streams in: what Hermes is doing right now ("Reading pod
    # logs…"), from its tool-progress events. Cleared when the reply is done.
    activity = db.Column(db.String(300), nullable=True)
    # Where the admin was when they asked (page, cluster, namespace) — handed
    # to Hermes so "why is this pod crashing" means the one on screen.
    context = db.Column(db.JSON, nullable=True)
    model = db.Column(db.String(120), nullable=True)
    created_at = db.Column(db.DateTime(timezone=True), nullable=False, default=_now)
    completed_at = db.Column(db.DateTime(timezone=True), nullable=True)
