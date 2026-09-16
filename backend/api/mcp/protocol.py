"""The MCP half of Streamable HTTP, as much of it as a server actually needs.

JSON-RPC 2.0 over a POST: ``initialize`` to agree a version, ``tools/list`` to
advertise, ``tools/call`` to do the work, ``ping`` to prove liveness, and the
``notifications/*`` messages which take no reply at all. That is the entire
surface a read-only tool server has to implement, which is why there is no SDK
dependency behind it.

Two details that are easy to get wrong and expensive to debug:

* a NOTIFICATION has no ``id`` and MUST NOT be answered. Replying to one makes a
  client that is following the spec disconnect, and the error it reports points
  at the next request rather than the reply that caused it.
* the protocol version is echoed, not asserted. A client that speaks a version
  this server has not heard of is answered in the newest version this server
  knows, because refusing an unfamiliar date string breaks every client that
  ships ahead of the server.
"""

from __future__ import annotations

import logging
from typing import Any, Callable, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

# Versions this server knows how to behave as. Newest first — the first entry is
# what an unrecognised request is answered with.
SUPPORTED_PROTOCOL_VERSIONS = ("2025-06-18", "2025-03-26", "2024-11-05")

SERVER_NAME = "kubesight"
SERVER_TITLE = "KubeSight Control Plane"

# JSON-RPC error codes. -32603 is the catch-all; the rest are the spec's.
PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602
INTERNAL_ERROR = -32603


class ToolError(Exception):
    """A tool could not answer. The message is shown to the agent verbatim.

    Distinct from an unexpected exception: this is a refusal the agent can act
    on ("no service with that id"), not a fault it should retry.
    """


def _response(request_id: Any, result: Any) -> Dict[str, Any]:
    return {"jsonrpc": "2.0", "id": request_id, "result": result}


def _error(request_id: Any, code: int, message: str) -> Dict[str, Any]:
    return {"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}}


def negotiate_version(requested: Any) -> str:
    """The version to answer in. Echoed when known, newest when not."""
    candidate = str(requested or "").strip()
    return candidate if candidate in SUPPORTED_PROTOCOL_VERSIONS else SUPPORTED_PROTOCOL_VERSIONS[0]


def handle(
    message: Any,
    *,
    tools: List[Dict[str, Any]],
    call: Callable[[str, Dict[str, Any]], Any],
    server_version: str = "1.0.0",
) -> Optional[Dict[str, Any]]:
    """One JSON-RPC message in, at most one response out.

    Returns None for a notification, which the transport turns into an empty
    202 rather than a body.
    """
    if not isinstance(message, dict) or message.get("jsonrpc") != "2.0":
        return _error(None, INVALID_REQUEST, "Expected a JSON-RPC 2.0 message.")

    method = str(message.get("method") or "")
    request_id = message.get("id")
    params = message.get("params") if isinstance(message.get("params"), dict) else {}

    # A notification carries no id and is never answered.
    if request_id is None:
        if not method.startswith("notifications/"):
            logger.debug("MCP: ignoring id-less request for %s", method)
        return None

    if method == "initialize":
        return _response(
            request_id,
            {
                "protocolVersion": negotiate_version(params.get("protocolVersion")),
                # Only tools. No resources, prompts, sampling or roots — an
                # honest capability list is what stops a client waiting on a
                # feature that will never arrive.
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {
                    "name": SERVER_NAME,
                    "title": SERVER_TITLE,
                    "version": server_version,
                },
                "instructions": (
                    "KubeSight is a Kubernetes control plane: clusters and workloads, "
                    "a CI service catalog with pipelines and builds, artifacts, "
                    "runners, and alerts. These tools are read-only — they answer "
                    "questions and change nothing. Start with kubesight_overview for "
                    "orientation, then narrow with the listing tools. Every result is "
                    "scoped to the permissions of the token you are using."
                ),
            },
        )

    if method == "ping":
        return _response(request_id, {})

    if method == "tools/list":
        return _response(request_id, {"tools": tools})

    if method == "tools/call":
        name = str(params.get("name") or "")
        arguments = params.get("arguments") if isinstance(params.get("arguments"), dict) else {}
        if not name:
            return _error(request_id, INVALID_PARAMS, "A tool name is required.")
        try:
            payload = call(name, arguments)
        except ToolError as exc:
            # A refusal the agent should read and act on, NOT a transport error:
            # isError keeps it inside the conversation instead of failing the
            # call and hiding the reason.
            return _response(
                request_id,
                {"content": [{"type": "text", "text": str(exc)}], "isError": True},
            )
        except Exception:
            logger.exception("MCP tool %s failed", name)
            return _response(
                request_id,
                {
                    "content": [
                        {
                            "type": "text",
                            "text": (
                                f"'{name}' failed inside KubeSight. The error was logged; "
                                "nothing was changed."
                            ),
                        }
                    ],
                    "isError": True,
                },
            )
        return _response(request_id, payload)

    return _error(request_id, METHOD_NOT_FOUND, f"KubeSight's MCP server has no '{method}'.")


def batch(
    messages: Any,
    *,
    tools: List[Dict[str, Any]],
    call: Callable[[str, Dict[str, Any]], Any],
    server_version: str = "1.0.0",
) -> Tuple[Optional[Any], bool]:
    """Handle one message or an array of them.

    Returns ``(body, has_body)``. A batch of nothing but notifications produces
    no body at all, which is the case that turns into a 202.
    """
    if isinstance(messages, list):
        replies = [
            reply
            for item in messages
            for reply in (handle(item, tools=tools, call=call, server_version=server_version),)
            if reply is not None
        ]
        return (replies, True) if replies else (None, False)

    reply = handle(messages, tools=tools, call=call, server_version=server_version)
    return (reply, True) if reply is not None else (None, False)
