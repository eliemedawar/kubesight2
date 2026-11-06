"""Conversations, sending a message, and the worker that waits on Hermes.

Sending a message writes the question and a ``pending`` reply row, then hands
the reply to a small worker pool and returns. One Hermes turn is an agent loop
that can run for minutes; an HTTP request held open that long dies at the
first proxy timeout, and the answer with it. The UI polls the conversation
instead — the shape CI Assist uses, for the same reason.

A reply belongs to the process that started it. If that process restarts,
nothing finishes the row, so :func:`reap_stale` closes a reply that has been
pending for longer than Hermes is allowed to take: a visible "try again" beats
a spinner that never stops.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Any, Dict, List, Optional

from ...datetime_utils import serialize_utc_datetime
from ...db import db
from ...models_assistant import AssistantConversation, AssistantMessage
from ..application_intelligence_hermes import HermesError
from . import hermes

if TYPE_CHECKING:
    from flask import Flask

logger = logging.getLogger(__name__)

MAX_MESSAGE_CHARS = 8000
MAX_CONVERSATIONS_LISTED = 50
DEFAULT_WORKERS = 2
# How often a streaming reply is written to its row. The UI polls faster
# than this, so the answer appears in small steps rather than at the end.
PROGRESS_WRITE_SECONDS = 0.3

_executor: Optional[ThreadPoolExecutor] = None
_app: Optional["Flask"] = None
_lock = threading.Lock()


class AssistantError(Exception):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _aware(value: Optional[datetime]) -> Optional[datetime]:
    if value is not None and value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value


# --- worker pool ------------------------------------------------------------


def start(app: "Flask") -> None:
    """Attach the worker pool. In TESTING there is none: replies run inline."""
    global _executor, _app
    with _lock:
        if _executor is not None:
            return
        _app = app
        if app.config.get("TESTING"):
            return
        try:
            workers = max(1, int(os.getenv("ASSISTANT_WORKERS", "").strip() or DEFAULT_WORKERS))
        except ValueError:
            workers = DEFAULT_WORKERS
        _executor = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="assistant")
    try:
        with app.app_context():
            reap_stale()
    except Exception:
        logger.exception("Stale assistant reply sweep failed at startup")


def _submit(message_id: int) -> None:
    if _executor is None:
        run_reply(message_id)
        return
    _executor.submit(_run_in_context, message_id)


def _run_in_context(message_id: int) -> None:
    if _app is None:
        return
    try:
        with _app.app_context():
            try:
                run_reply(message_id)
            finally:
                db.session.remove()
    except Exception:
        logger.exception("Assistant worker failed for message %s", message_id)


def reap_stale(*, commit: bool = True) -> int:
    """Fail replies whose worker is gone (pending past Hermes' own timeout)."""
    cutoff = _now() - timedelta(seconds=hermes.timeout_seconds() + 120)
    closed = 0
    for row in AssistantMessage.query.filter_by(status="pending").all():
        created = _aware(row.created_at)
        if created is None or created >= cutoff:
            continue
        row.status = "failed"
        row.error = "No answer arrived — KubeSight probably restarted while Hermes was working. Ask again."
        row.completed_at = _now()
        closed += 1
    if closed and commit:
        db.session.commit()
    return closed


# --- reading ----------------------------------------------------------------


def _who(user) -> str:
    name = str(getattr(user, "full_name", "") or "").strip()
    username = str(getattr(user, "username", "") or "").strip()
    if name and username and name != username:
        return f"{name} ({username})"
    return name or username or "an administrator"


def _clean_context(raw: Any) -> Dict[str, str]:
    if not isinstance(raw, dict):
        return {}
    clean = {}
    for key in ("page", "pageLabel", "clusterId", "clusterName", "namespace", "route"):
        value = raw.get(key)
        if value is None or isinstance(value, (dict, list)):
            continue
        text = str(value).strip()[:300]
        if text:
            clean[key] = text
    return clean


def serialize_message(row: AssistantMessage) -> Dict[str, Any]:
    return {
        "id": row.id,
        "role": row.role,
        "content": row.content or "",
        "status": row.status,
        "error": row.error,
        "activity": row.activity if row.status == "pending" else None,
        "createdAt": serialize_utc_datetime(row.created_at),
        "completedAt": serialize_utc_datetime(row.completed_at),
    }


def serialize_conversation(row: AssistantConversation, *, with_messages: bool = False) -> Dict[str, Any]:
    data = {
        "id": row.id,
        "title": row.title,
        "createdAt": serialize_utc_datetime(row.created_at),
        "updatedAt": serialize_utc_datetime(row.updated_at),
    }
    if with_messages:
        data["messages"] = [serialize_message(m) for m in row.messages]
        data["pending"] = any(m.status == "pending" for m in row.messages)
    return data


def list_conversations(user) -> List[Dict[str, Any]]:
    rows = (
        AssistantConversation.query.filter_by(user_id=user.id)
        .order_by(AssistantConversation.updated_at.desc(), AssistantConversation.id.desc())
        .limit(MAX_CONVERSATIONS_LISTED)
        .all()
    )
    return [serialize_conversation(row) for row in rows]


def _owned(user, conversation_id: int) -> AssistantConversation:
    row = db.session.get(AssistantConversation, conversation_id)
    # Someone else's conversation answers exactly like a missing one.
    if row is None or row.user_id != user.id:
        raise AssistantError("Conversation not found.", 404)
    return row


def get_conversation(user, conversation_id: int) -> Dict[str, Any]:
    row = _owned(user, conversation_id)
    if any(m.status == "pending" for m in row.messages) and reap_stale():
        db.session.refresh(row)
    return serialize_conversation(row, with_messages=True)


def delete_conversation(user, conversation_id: int) -> None:
    row = _owned(user, conversation_id)
    db.session.delete(row)
    db.session.commit()


# --- writing ----------------------------------------------------------------


def _title_from(text: str) -> str:
    line = " ".join(text.split())
    return line if len(line) <= 80 else line[:77].rstrip() + "…"


def send_message(user, text: Any, context: Any = None, conversation_id: Optional[int] = None) -> Dict[str, Any]:
    """Store the question and a pending reply; Hermes answers in the background."""
    text = str(text or "").strip()
    if not text:
        raise AssistantError("Type a message first.")
    if len(text) > MAX_MESSAGE_CHARS:
        raise AssistantError(f"Messages are limited to {MAX_MESSAGE_CHARS} characters.")
    hint = hermes.configuration_hint()
    if hint:
        raise AssistantError(f"The assistant is not connected to Hermes. {hint}", 503)

    if conversation_id is None:
        conversation = AssistantConversation(user_id=user.id, title=_title_from(text))
        db.session.add(conversation)
        db.session.flush()
    else:
        conversation = _owned(user, conversation_id)
        if any(m.status == "pending" for m in conversation.messages):
            raise AssistantError("Hermes is still answering the last message.", 409)

    now = _now()
    db.session.add(
        AssistantMessage(
            conversation_id=conversation.id,
            role="user",
            content=text,
            status="done",
            context=_clean_context(context) or None,
            created_at=now,
            completed_at=now,
        )
    )
    reply = AssistantMessage(conversation_id=conversation.id, role="assistant", status="pending", created_at=now)
    db.session.add(reply)
    conversation.updated_at = now
    db.session.commit()

    _submit(reply.id)
    db.session.refresh(conversation)
    return serialize_conversation(conversation, with_messages=True)


def run_reply(message_id: int) -> None:
    """Ask Hermes for one pending reply and write the outcome onto the row."""
    reply = db.session.get(AssistantMessage, message_id)
    if reply is None or reply.status != "pending":
        return
    conversation = reply.conversation
    earlier = [m for m in conversation.messages if m.id < reply.id]
    history = [
        {"role": m.role, "content": m.content}
        for m in earlier
        if m.role == "user" or m.status == "done"
    ]
    latest_question = next((m for m in reversed(earlier) if m.role == "user"), None)
    from ...models import User

    user = db.session.get(User, conversation.user_id)
    messages = hermes.build_messages(history, _who(user), latest_question.context if latest_question else None)
    last_write = [0.0]

    def progress(text=None, activity=None):
        # Write what has streamed in so far; the UI shows it while Hermes works.
        if text is not None:
            reply.content = text
            reply.activity = None
        if activity is not None:
            reply.activity = activity
        now = time.monotonic()
        if activity is not None or now - last_write[0] >= PROGRESS_WRITE_SECONDS:
            last_write[0] = now
            db.session.commit()

    try:
        text, model = hermes.chat(
            messages,
            session_key=f"kubesight-assistant-user-{conversation.user_id}",
            on_progress=progress,
        )
        if not text:
            raise HermesError("Hermes finished without writing an answer.")
        reply.status = "done"
        reply.content = text
        reply.model = model
    except HermesError as exc:
        reply.status = "failed"
        reply.error = str(exc)
    except Exception:
        logger.exception("Assistant reply %s failed", message_id)
        reply.status = "failed"
        reply.error = "Something went wrong while asking Hermes. Check the backend log."
    reply.activity = None
    reply.completed_at = _now()
    conversation.updated_at = reply.completed_at
    db.session.commit()
