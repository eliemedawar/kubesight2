"""Ticket agent settings (single row, id=1) — read, validate, save, test."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from ...db import db
from ...models import TicketAgentSettings
from ...secret_encryption import decrypt_secret, encrypt_secret
from . import hermes, telegram

CONFIDENCE_BARS = ("High", "Medium")


def get_or_create() -> TicketAgentSettings:
    row = db.session.get(TicketAgentSettings, 1)
    if row is None:
        row = TicketAgentSettings(id=1)
        db.session.add(row)
        db.session.commit()
    return row


def bot_token(row: Optional[TicketAgentSettings] = None) -> str:
    row = row or get_or_create()
    return decrypt_secret(row.telegram_bot_token_encrypted or "") if row.telegram_bot_token_encrypted else ""


def telegram_ready(row: Optional[TicketAgentSettings] = None) -> bool:
    row = row or get_or_create()
    return bool(row.telegram_enabled and row.telegram_bot_token_encrypted and (row.telegram_chat_id or "").strip())


def is_active() -> bool:
    """Whether inbound tickets go to Hermes (switched on AND Hermes reachable).

    Hermes unconfigured means the deterministic path keeps working exactly as
    before — turning the agent on must never be what stops deployments. The
    agent needs its OWN Hermes endpoint: the shared one has no MCP tools and
    could only fail every ticket.
    """
    row = db.session.get(TicketAgentSettings, 1)
    return bool(row and row.enabled and hermes.dedicated() and hermes.is_configured())


def comments_public() -> bool:
    row = db.session.get(TicketAgentSettings, 1)
    return bool(row.public_comments) if row else True


def approvers(row: Optional[TicketAgentSettings] = None) -> List[str]:
    row = row or get_or_create()
    raw = row.telegram_approvers or ""
    return [p.strip().lstrip("@").casefold() for p in raw.replace("\n", ",").split(",") if p.strip()]


def _iso(dt):
    return dt.isoformat() if dt else None


def serialize(row: Optional[TicketAgentSettings] = None) -> Dict[str, Any]:
    row = row or get_or_create()
    return {
        "enabled": bool(row.enabled),
        "active": is_active(),
        "minConfidence": row.min_confidence or "High",
        "publicComments": bool(row.public_comments),
        "approvalTimeoutHours": int(row.approval_timeout_hours or 24),
        "telegramEnabled": bool(row.telegram_enabled),
        "telegramBotTokenConfigured": bool(row.telegram_bot_token_encrypted),
        "telegramChatId": row.telegram_chat_id or "",
        "telegramApprovers": row.telegram_approvers or "",
        "telegramReady": telegram_ready(row),
        "hermesConfigured": hermes.is_configured(),
        "hermesHint": hermes.configuration_hint(),
        "hermesDedicated": hermes.dedicated(),
        "lastTestAt": _iso(row.last_test_at),
        "lastTestStatus": row.last_test_status,
        "lastTestMessage": row.last_test_message,
        "updatedAt": _iso(row.updated_at),
    }


def update(payload: Dict[str, Any]) -> Dict[str, Any]:
    """Apply a settings payload. Raises ValueError on bad input."""
    row = get_or_create()
    errors: List[str] = []
    for key, attr in (("enabled", "enabled"), ("publicComments", "public_comments"),
                      ("telegramEnabled", "telegram_enabled")):
        if key in payload:
            setattr(row, attr, bool(payload.get(key)))
    if "minConfidence" in payload:
        value = str(payload.get("minConfidence") or "").strip().capitalize()
        if value not in CONFIDENCE_BARS:
            errors.append("minConfidence must be High or Medium.")
        else:
            row.min_confidence = value
    if "approvalTimeoutHours" in payload:
        try:
            hours = int(payload.get("approvalTimeoutHours"))
            if not 1 <= hours <= 168:
                raise ValueError
            row.approval_timeout_hours = hours
        except (TypeError, ValueError):
            errors.append("approvalTimeoutHours must be a whole number between 1 and 168.")
    if "telegramChatId" in payload:
        chat = str(payload.get("telegramChatId") or "").strip()
        if chat and not (chat.lstrip("-").isdigit() or chat.startswith("@")):
            errors.append("telegramChatId must be a numeric chat id (e.g. -1001234567890) or @channelname.")
        else:
            row.telegram_chat_id = chat or None
    if "telegramApprovers" in payload:
        row.telegram_approvers = str(payload.get("telegramApprovers") or "").strip() or None
    token = payload.get("telegramBotToken")
    if token:
        token = str(token).strip()
        if ":" not in token:
            errors.append("That does not look like a Telegram bot token (expected 123456:ABC…).")
        else:
            row.telegram_bot_token_encrypted = encrypt_secret(token)
            row.telegram_update_offset = None  # a new bot has its own update ids
    if payload.get("clearTelegramBotToken"):
        row.telegram_bot_token_encrypted = None
        row.telegram_update_offset = None
    if errors:
        db.session.rollback()
        raise ValueError(" ".join(errors))
    db.session.commit()
    return serialize(row)


def test_telegram() -> Dict[str, Any]:
    """Check the bot token and post a test message to the approval chat."""
    row = get_or_create()
    now = datetime.now(timezone.utc)
    try:
        token = bot_token(row)
        if not token:
            raise telegram.TelegramError("Set a bot token first.")
        if not (row.telegram_chat_id or "").strip():
            raise telegram.TelegramError("Set the approval chat id first.")
        me = telegram.get_me(token)
        telegram.send_message(
            token,
            row.telegram_chat_id,
            "KubeSight ticket agent: test message. Approval requests for tickets Hermes is not "
            "sure about will be posted here with Approve / Reject buttons.",
        )
        status, message = "ok", f"Posted a test message as @{me.get('username') or 'bot'}."
    except telegram.TelegramError as exc:
        status, message = "error", str(exc)
    row.last_test_at, row.last_test_status, row.last_test_message = now, status, message
    db.session.commit()
    return {"status": status, "message": message, **serialize(row)}
