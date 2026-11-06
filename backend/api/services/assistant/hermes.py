"""The call to Hermes for one chat turn.

The assistant talks to the operators' Hermes — the one with the ``kubesight``
skill and KubeSight's MCP tools, the same one behind the Telegram bot. That is
the ticket agent's Hermes, so the endpoint falls back to its settings and an
installation with the ticket agent wired up has the chat working too:

* ``ASSISTANT_HERMES_URL``  → ``TICKET_AGENT_HERMES_URL``  (…/v1/chat/completions)
* ``ASSISTANT_HERMES_TOKEN`` → ``TICKET_AGENT_HERMES_TOKEN`` → ``HERMES_API_TOKEN``
* ``ASSISTANT_HERMES_MODEL`` → ``TICKET_AGENT_HERMES_MODEL`` → ``API_SERVER_MODEL_NAME``

The shared ``HERMES_API_URL`` alone is deliberately not enough: that Hermes is
the no-tools one Application Intelligence uses, and a chat that cannot look at
anything would answer from guesses.

Hermes' chat endpoint is stateless — the conversation so far travels in
``messages`` with every turn. ``X-Hermes-Session-Key`` only scopes Hermes'
long-term memory, one scope per admin, so chat never lands in the ticket
agent's memory (or another admin's).
"""

from __future__ import annotations

import json
import os
import time
from typing import Any, Callable, Dict, List, Optional, Tuple
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from ..application_intelligence_hermes import (
    HermesError,
    HermesTransientError,
    _candidate_from_response,
    _secret_value,
    _validate_endpoint,
)
from ..application_intelligence_security import bounded_json_bytes

SYSTEM_PROMPT = """You are the KubeSight assistant, answering inside KubeSight's web UI. You are
talking to {who}, a KubeSight administrator. You have the kubesight skill and KubeSight's MCP
tools: use them to look things up — clusters, namespaces, pods and their logs, CI services,
pipelines and builds, what is deployed at which version, Helm releases, approvals, alerts, the
audit trail, registries, tickets. Never guess at live state: read it with a tool, and say what you
looked at.

Changes (deploy, restart, scale, rollback, apply YAML, Helm install/upgrade, start a build, edit
a Dockerfile, anything else that writes): first say exactly what you will change, where, and
from what to what, then ask the administrator to confirm — unless their last message already
asked for that exact change. KubeSight's approval gates still apply to you: when a change is
queued for approval instead of applied, say so and that it waits on the Approvals page. Never
try to approve anything yourself.

The administrator's screen is described below (page, cluster, namespace). When they say "this
pod", "here" or "this cluster", they mean what is on that screen.

Reply in Markdown: short paragraphs, bullet lists, `code` for names, fenced code blocks for YAML,
logs and commands, a table when you list several resources. Lead with the answer; keep it short.
"""

MAX_HISTORY_MESSAGES = 40


def _int_env(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, "").strip() or default)
    except ValueError:
        return default


def _endpoint() -> str:
    return (
        os.getenv("ASSISTANT_HERMES_URL", "").strip()
        or os.getenv("TICKET_AGENT_HERMES_URL", "").strip()
    )


def _token() -> str:
    return (
        _secret_value("ASSISTANT_HERMES_TOKEN")
        or _secret_value("TICKET_AGENT_HERMES_TOKEN")
        or _secret_value("HERMES_API_TOKEN")
    )


def model() -> str:
    return (
        os.getenv("ASSISTANT_HERMES_MODEL", "").strip()
        or os.getenv("TICKET_AGENT_HERMES_MODEL", "").strip()
        or os.getenv("API_SERVER_MODEL_NAME", "").strip()
        or "hermes-agent"
    )


def timeout_seconds() -> int:
    # One turn can be a long agent loop: several MCP reads, logs, then a reply.
    return _int_env("ASSISTANT_HERMES_TIMEOUT_SECONDS", 600)


def configuration_hint() -> str:
    """Empty when the chat can reach Hermes; otherwise what to set."""
    endpoint = _endpoint()
    if not endpoint:
        return (
            "Set ASSISTANT_HERMES_URL (or TICKET_AGENT_HERMES_URL) to the Hermes that has the "
            "kubesight skill — its …/v1/chat/completions."
        )
    if not _token():
        return "Set ASSISTANT_HERMES_TOKEN (or TICKET_AGENT_HERMES_TOKEN) to that Hermes' API_SERVER_KEY."
    try:
        _validate_endpoint(endpoint)
    except HermesError as exc:
        return str(exc)
    return ""


def is_configured() -> bool:
    return not configuration_hint()


def describe_screen(context: Optional[Dict[str, Any]]) -> str:
    context = context or {}
    parts = []
    for key, label in (
        ("pageLabel", "Page"),
        ("clusterName", "Cluster"),
        ("namespace", "Namespace"),
        ("route", "URL"),
    ):
        value = str(context.get(key) or "").strip()
        if value:
            parts.append(f"{label}: {value}")
    return "\n".join(parts) or "Not known."


def build_messages(
    history: List[Dict[str, str]], who: str, context: Optional[Dict[str, Any]]
) -> List[Dict[str, str]]:
    system = SYSTEM_PROMPT.format(who=who) + "\nThe administrator's screen:\n" + describe_screen(context)
    turns = [m for m in history if m.get("role") in ("user", "assistant") and m.get("content")]
    return [{"role": "system", "content": system}] + turns[-MAX_HISTORY_MESSAGES:]


def chat(
    messages: List[Dict[str, str]],
    session_key: str,
    on_progress: Optional[Callable[..., None]] = None,
) -> Tuple[str, str]:
    """One chat turn, streamed. Returns ``(reply, model)``.

    ``on_progress(text=…)`` is called as the answer grows and
    ``on_progress(activity=…)`` when Hermes starts a tool, so the reply row
    can show the answer while it is still being written. A Hermes (or proxy)
    that answers with one JSON body instead of a stream works too.

    Raises :class:`HermesTransientError` for outages/timeouts and
    :class:`HermesError` for everything else.
    """
    endpoint, token = _endpoint(), _token()
    hint = configuration_hint()
    if hint:
        raise HermesError(hint)
    payload = {"model": model(), "messages": messages, "stream": True}
    body = bounded_json_bytes(payload, _int_env("ASSISTANT_HERMES_MAX_BYTES", 1_000_000))
    request = Request(
        endpoint,
        data=body,
        method="POST",
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "text/event-stream",
            "Content-Type": "application/json",
            "User-Agent": "KubeSight/assistant",
            "X-Hermes-Session-Key": session_key,
        },
    )
    limit = _int_env("ASSISTANT_HERMES_RESPONSE_MAX_BYTES", 500_000)
    deadline = time.monotonic() + timeout_seconds()
    try:
        # The socket timeout is per read: Hermes sends a keepalive every few
        # seconds while a tool runs, so silence this long means it is gone.
        with urlopen(request, timeout=_int_env("ASSISTANT_HERMES_IDLE_SECONDS", 120)) as response:
            if "text/event-stream" not in (response.headers.get("Content-Type") or ""):
                raw = response.read(limit + 1)
                if len(raw) > limit:
                    raise HermesError("Hermes' reply is larger than KubeSight accepts.")
                candidate = _candidate_from_response(json.loads(raw.decode("utf-8")))
                if isinstance(candidate, (dict, list)):
                    candidate = json.dumps(candidate, indent=2)
                return str(candidate or "").strip(), model()
            text = _read_stream(response, deadline, limit, on_progress)
    except HTTPError as exc:
        if exc.code in (401, 403):
            raise HermesError(
                f"Hermes rejected KubeSight's key ({exc.code}). ASSISTANT_HERMES_TOKEN (or "
                "TICKET_AGENT_HERMES_TOKEN) must equal that Hermes' API_SERVER_KEY."
            ) from exc
        if exc.code == 404:
            raise HermesError(
                "Hermes answered 404 — the Hermes URL must end in /v1/chat/completions."
            ) from exc
        if exc.code == 429 or 500 <= exc.code < 600:
            raise HermesTransientError(f"Hermes is busy or failing ({exc.code}). Try again shortly.") from exc
        raise HermesError(f"Hermes rejected the message ({exc.code}).") from exc
    except (URLError, TimeoutError, OSError) as exc:
        raise HermesTransientError("Hermes is unavailable or took too long to answer.") from exc
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise HermesTransientError("Hermes returned malformed JSON.") from exc
    return text.strip(), model()


def _read_stream(response, deadline: float, limit: int, on_progress) -> str:
    """Collect an OpenAI-style SSE stream (plus Hermes' tool-progress events)."""
    parts: List[str] = []
    received = 0
    event, data_lines = "message", []

    def dispatch(name: str, data: str) -> bool:
        """Handle one event. True = the stream is finished."""
        if data == "[DONE]":
            return True
        if name == "hermes.tool.progress":
            try:
                info = json.loads(data)
            except ValueError:
                return False
            if isinstance(info, dict) and info.get("status", "running") == "running" and on_progress:
                label = str(info.get("label") or info.get("tool") or "").strip()
                if label:
                    on_progress(activity=label[:300])
            return False
        chunk = json.loads(data)
        choices = chunk.get("choices") if isinstance(chunk, dict) else None
        if not choices:
            return False
        first = choices[0] if isinstance(choices[0], dict) else {}
        if first.get("finish_reason") == "error":
            raise HermesTransientError("Hermes stopped with an error partway through its answer.")
        piece = (first.get("delta") or {}).get("content")
        if isinstance(piece, str) and piece:
            parts.append(piece)
            if on_progress:
                on_progress(text="".join(parts))
        return False

    for raw_line in response:
        received += len(raw_line)
        if received > limit:
            raise HermesError("Hermes' reply is larger than KubeSight accepts.")
        if time.monotonic() > deadline:
            raise HermesTransientError("Hermes took too long to answer.")
        line = raw_line.decode("utf-8").rstrip("\r\n")
        if not line:
            if data_lines and dispatch(event, "\n".join(data_lines)):
                break
            event, data_lines = "message", []
        elif line.startswith(":"):
            continue  # keepalive
        elif line.startswith("event:"):
            event = line[6:].strip()
        elif line.startswith("data:"):
            data_lines.append(line[5:].lstrip(" "))
    else:
        if data_lines:
            dispatch(event, "\n".join(data_lines))
    return "".join(parts)
