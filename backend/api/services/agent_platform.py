"""KubeSight's side of the agent platform (PydanticAI agents on Temporal).

The platform replaces Hermes one flow at a time. Each flow has its own switch,
read from the environment (the chart sets it):

=================  ==========================  ==================================
flow               switch                      what moves
=================  ==========================  ==================================
``tickets``        ``AGENT_ENGINE_TICKETS``    every ticket task (handle, follow-up,
                                               requester reply)
``analysis``       ``AGENT_ENGINE_ANALYSIS``   Application Intelligence source analysis
``ci_assist``      ``AGENT_ENGINE_CI_ASSIST``  CI pipeline generation and repair
=================  ==========================  ==================================

``hermes`` (the default) keeps today's path; ``platform`` sends the flow to
``AGENT_PLATFORM_URL``. Switching back is the same edit.

Analysis and CI assist already speak OpenAI-style chat completions, so for
them only the endpoint, key and model change (:func:`chat_target`). Ticket
tasks no longer hold a thread for the length of an agent run: KubeSight hands
the task over (:func:`submit_ticket_task`) and the platform reports the end of
it to ``/api/agent-platform/ticket-tasks/<id>/result``.

One shared secret, ``AGENT_PLATFORM_TOKEN``, authenticates both directions.
"""

from __future__ import annotations

import hmac
import json
import os
from typing import Any, Dict, NamedTuple, Optional
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

from .application_intelligence_hermes import HermesError, _secret_value, _validate_endpoint

FLOWS = ("tickets", "analysis", "ci_assist")
HERMES, PLATFORM = "hermes", "platform"
_AGENT_DEFAULTS = {"analysis": "analysis", "ci_assist": "ci_assist"}


class PlatformError(RuntimeError):
    """The platform refused, or is misconfigured. Not worth retrying."""


class PlatformUnavailable(PlatformError):
    """Down, timing out or overloaded: try again later."""


class ChatTarget(NamedTuple):
    endpoint: str
    token: str
    model: str


def engine(flow: str) -> str:
    value = os.getenv(f"AGENT_ENGINE_{flow.upper()}", "").strip().lower()
    return PLATFORM if value == PLATFORM else HERMES


def uses_platform(flow: str) -> bool:
    return engine(flow) == PLATFORM


def base_url() -> str:
    return os.getenv("AGENT_PLATFORM_URL", "").strip().rstrip("/")


def token() -> str:
    return _secret_value("AGENT_PLATFORM_TOKEN")


def configuration_hint() -> str:
    """Why the platform cannot be used, or "" when it can."""
    url = base_url()
    if not url:
        return "AGENT_PLATFORM_URL is not set."
    if not token():
        return "AGENT_PLATFORM_TOKEN is not set."
    try:
        _validate_endpoint(url)
    except HermesError as exc:
        return str(exc).replace("Hermes", "The agent platform")
    return ""


def is_configured() -> bool:
    return not configuration_hint()


def agent_name(flow: str) -> str:
    return (
        os.getenv(f"AGENT_PLATFORM_{flow.upper()}_AGENT", "").strip()
        or _AGENT_DEFAULTS.get(flow, flow)
    )


def chat_target(flow: str) -> Optional[ChatTarget]:
    """Where a chat-completions flow goes when it is switched to the platform.

    None when the flow stays on Hermes. ``model`` names the platform's
    completion agent for the flow.
    """
    if not uses_platform(flow):
        return None
    return ChatTarget(f"{base_url()}/v1/chat/completions" if base_url() else "", token(), agent_name(flow))


def service_location() -> Optional[tuple]:
    """(namespace, port) of an in-cluster platform, for NetworkPolicy egress."""
    url = base_url()
    if not url:
        return None
    parsed = urlsplit(url)
    host = (parsed.hostname or "").lower()
    if parsed.scheme != "http" or not (host.endswith(".svc") or host.endswith(".svc.cluster.local")):
        return None
    namespace = os.getenv("AGENT_PLATFORM_SERVICE_NAMESPACE", "").strip()
    if not namespace:
        labels = host.split(".")
        namespace = labels[1] if len(labels) >= 2 else ""
    raw_port = os.getenv("AGENT_PLATFORM_SERVICE_PORT", "").strip()
    port = int(raw_port) if raw_port else (parsed.port or 80)
    return namespace, port


def _int_env(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, "").strip() or default)
    except ValueError:
        return default


def _call(method: str, path: str, body: Optional[Dict[str, Any]] = None, timeout: int = 15) -> Dict[str, Any]:
    hint = configuration_hint()
    if hint:
        raise PlatformError(hint)
    data = json.dumps(body, ensure_ascii=False, default=str).encode("utf-8") if body is not None else None
    request = Request(
        f"{base_url()}{path}",
        data=data,
        method=method,
        headers={
            "Authorization": f"Bearer {token()}",
            "Content-Type": "application/json",
            "User-Agent": "KubeSight/agent-platform",
        },
    )
    try:
        with urlopen(request, timeout=timeout) as response:
            raw = response.read(1_000_000)
    except HTTPError as exc:
        if exc.code == 429 or exc.code >= 500:
            raise PlatformUnavailable(f"The agent platform answered {exc.code}.") from exc
        if exc.code in (401, 403):
            raise PlatformError(
                f"The agent platform refused KubeSight's key ({exc.code}): AGENT_PLATFORM_TOKEN must "
                "be the same on both sides."
            ) from exc
        raise PlatformError(f"The agent platform refused the request ({exc.code}).") from exc
    except (URLError, TimeoutError, OSError) as exc:
        raise PlatformUnavailable("The agent platform is unreachable or timed out.") from exc
    try:
        return json.loads(raw.decode("utf-8")) if raw else {}
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise PlatformUnavailable("The agent platform returned malformed JSON.") from exc


def submit_ticket_task(*, provider: str, record_id: int, task_id: int, kind: str,
                       message: Dict[str, Any]) -> Dict[str, Any]:
    """Hand one ticket task to the ticket's workflow. Returns at once.

    Safe to repeat: the workflow ignores a task it is already working on and
    re-sends the report of one it finished.
    """
    return _call(
        "POST",
        "/v1/tickets/tasks",
        {"provider": provider, "recordId": record_id, "taskId": task_id, "kind": kind, "message": message},
        timeout=_int_env("AGENT_PLATFORM_SUBMIT_TIMEOUT_SECONDS", 15),
    )


def check() -> Dict[str, Any]:
    """The connection test: the platform answers and can reach Temporal."""
    return _call("GET", "/readyz", timeout=10)


def verify_callback(authorization: str) -> bool:
    expected = token()
    if not expected:
        return False
    scheme, _, presented = (authorization or "").partition(" ")
    return scheme.lower() == "bearer" and hmac.compare_digest(presented.strip(), expected)


def summary() -> Dict[str, Any]:
    """Which engine each flow uses, for the Integrations card."""
    return {
        "configured": is_configured(),
        "hint": configuration_hint(),
        "url": base_url(),
        "flows": {flow: engine(flow) for flow in FLOWS},
    }
