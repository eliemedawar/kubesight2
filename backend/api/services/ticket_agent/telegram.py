"""A minimal Telegram Bot API client — just what an approval needs.

Button presses are collected with ``getUpdates`` (long polling) from the
scheduler tick, so KubeSight needs no public URL for Telegram to call back.
That has one consequence operators must know: a bot can have only ONE
getUpdates consumer and no webhook. Use a bot dedicated to KubeSight — if the
same token also drives a Hermes Telegram gateway, Telegram answers 409.

Messages are plain text (no parse_mode) so ticket text never needs escaping.
"""

from __future__ import annotations

import json
import os
from typing import Any, Dict, List, Optional
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


class TelegramError(RuntimeError):
    pass


# Telegram rejects messages over 4096 characters.
MAX_TEXT = 4000


def _base(token: str) -> str:
    root = (os.getenv("TELEGRAM_API_BASE") or "https://api.telegram.org").rstrip("/")
    return f"{root}/bot{token}"


def _call(token: str, method: str, payload: Dict[str, Any], timeout: int = 15) -> Any:
    if not token:
        raise TelegramError("No Telegram bot token is configured.")
    request = Request(
        f"{_base(token)}/{method}",
        data=json.dumps(payload).encode("utf-8"),
        method="POST",
        headers={"Content-Type": "application/json", "User-Agent": "KubeSight/ticket-agent"},
    )
    try:
        with urlopen(request, timeout=timeout) as response:
            body = json.loads(response.read(1_000_000).decode("utf-8"))
    except HTTPError as exc:
        detail = ""
        try:
            detail = json.loads(exc.read().decode("utf-8")).get("description") or ""
        except Exception:  # noqa: BLE001 — the status code alone is still useful
            pass
        if exc.code == 409:
            detail = (
                detail
                or "another client is reading this bot's updates"
            ) + " — use a bot dedicated to KubeSight (no webhook, no other poller)"
        raise TelegramError(f"Telegram {method} failed (HTTP {exc.code}){': ' + detail if detail else ''}.") from exc
    except (URLError, TimeoutError, ValueError) as exc:
        raise TelegramError(f"Telegram is unreachable: {exc}") from exc
    if not isinstance(body, dict) or not body.get("ok"):
        raise TelegramError(f"Telegram {method} failed: {(body or {}).get('description', 'unknown error')}")
    return body.get("result")


def _clip(text: str) -> str:
    text = text or ""
    return text if len(text) <= MAX_TEXT else text[: MAX_TEXT - 1] + "…"


def get_me(token: str) -> Dict[str, Any]:
    return _call(token, "getMe", {}) or {}


def send_message(
    token: str,
    chat_id: str,
    text: str,
    buttons: Optional[List[List[Dict[str, str]]]] = None,
) -> int:
    """Post a message; returns its message id."""
    payload: Dict[str, Any] = {
        "chat_id": chat_id,
        "text": _clip(text),
        "disable_web_page_preview": True,
    }
    if buttons:
        payload["reply_markup"] = {"inline_keyboard": buttons}
    result = _call(token, "sendMessage", payload) or {}
    return int(result.get("message_id") or 0)


def edit_message(token: str, chat_id: str, message_id: int, text: str) -> None:
    """Replace a message's text. Omitting reply_markup drops the buttons."""
    _call(
        token,
        "editMessageText",
        {
            "chat_id": chat_id,
            "message_id": int(message_id),
            "text": _clip(text),
            "disable_web_page_preview": True,
        },
    )


def answer_callback(token: str, callback_id: str, text: str = "") -> None:
    _call(token, "answerCallbackQuery", {"callback_query_id": callback_id, "text": text[:190]})


def get_updates(token: str, offset: Optional[int]) -> List[Dict[str, Any]]:
    """Button presses since ``offset`` (non-blocking: timeout 0)."""
    payload: Dict[str, Any] = {"timeout": 0, "allowed_updates": ["callback_query"]}
    if offset is not None:
        payload["offset"] = int(offset)
    return _call(token, "getUpdates", payload) or []
