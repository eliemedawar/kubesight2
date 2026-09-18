"""KubeSight's MCP endpoint.

One route, speaking Streamable HTTP. An agent POSTs JSON-RPC and gets JSON back;
there is no session state, no server-initiated stream and no SSE, because this
server needs none of them — every call is a request with an answer, and the few
that change something finish before they reply.

Authentication is the ordinary one. A caller presents a KubeSight API token
(``ksa_…``) or a user JWT in ``Authorization``, ``get_current_user`` resolves it
exactly as it does for every other route, and each tool then checks its own
permission against that user. There is no MCP identity and no service account:
an agent is precisely as privileged as the token it was given, which is the
property that makes handing one to an agent a decision somebody can reason about.
"""

from __future__ import annotations

import json

from flask import Blueprint, Response, request

from ..audit import log_audit
from ..auth_utils import auth_required_enabled, get_current_user
from ..decorators import require_auth
from ..mcp import protocol, tools
from ..response import error_response

mcp_bp = Blueprint("mcp", __name__, url_prefix="/api/mcp")

SERVER_VERSION = "1.0.0"


def _json(payload, status: int = 200) -> Response:
    return Response(
        json.dumps(payload, ensure_ascii=False, default=str),
        status=status,
        mimetype="application/json",
    )


@mcp_bp.route("", methods=["POST"])
@mcp_bp.route("/", methods=["POST"])
@require_auth
def mcp_endpoint():
    """Handle one JSON-RPC message, or a batch of them."""
    try:
        message = request.get_json(force=True, silent=False)
    except Exception:
        return _json(
            {
                "jsonrpc": "2.0",
                "id": None,
                "error": {"code": protocol.PARSE_ERROR, "message": "Malformed JSON."},
            },
            400,
        )

    user = get_current_user()
    if auth_required_enabled() and user is None:
        return error_response("Unauthorized", 401)

    def call(name, arguments):
        return tools.call(name, arguments, user=user)

    body, has_body = protocol.batch(
        message,
        tools=tools.definitions(),
        call=call,
        server_version=SERVER_VERSION,
    )

    # A batch of nothing but notifications has no reply. 202 is what the spec
    # asks for, and a client following it will hang waiting for a body if we
    # send 200 with an empty one instead.
    if not has_body:
        return Response(status=202)

    _audit(message, user)
    return _json(body)


def _audit(message, user) -> None:
    """Record which tools were called, never what they returned.

    An agent reading the catalog is not interesting; an agent reading it with
    somebody's token, repeatedly, is. Arguments are not recorded either — a
    service slug is harmless, but the habit of logging tool arguments is how
    something sensitive eventually ends up in an audit row.

    Writes are named separately in the same row. The pipeline service writes its
    own ``ci_pipeline_saved`` entry with the stage count and the new version, so
    what changed is already recorded; what that entry cannot say is that an agent
    asked for it rather than a person in the editor. This is where that is said.
    """
    items = message if isinstance(message, list) else [message]
    called = sorted(
        {
            str((item.get("params") or {}).get("name") or "")
            for item in items
            if isinstance(item, dict) and item.get("method") == "tools/call"
        }
        - {""}
    )
    if not called:
        return
    writes = [name for name in called if tools.is_write(name)]
    log_audit(
        "mcp_tools_called",
        actor=user,
        target_type="mcp",
        target_id="kubesight",
        details={"tools": called, "count": len(called), "writes": writes},
    )


@mcp_bp.route("", methods=["GET"])
@mcp_bp.route("/", methods=["GET"])
def mcp_discovery():
    """What this server is, without needing to speak JSON-RPC.

    Unauthenticated on purpose and deliberately free of anything about this
    installation: it names the protocol and the transport so somebody pointing a
    client at the wrong URL gets an answer instead of a 405. It lists no tools —
    that needs a token.
    """
    return _json(
        {
            "name": protocol.SERVER_NAME,
            "title": protocol.SERVER_TITLE,
            "version": SERVER_VERSION,
            "protocol": "mcp",
            "protocolVersions": list(protocol.SUPPORTED_PROTOCOL_VERSIONS),
            "transport": "streamable-http",
            "readOnly": False,
            # Named rather than left to "readOnly: false", which says a write
            # exists but not how far it reaches. Somebody deciding whether to
            # hand an agent a token needs the second thing.
            "writes": "A service's CI pipeline, with ci_pipelines:edit. Nothing else.",
            "authentication": "Bearer — a KubeSight API token or user token.",
            "usage": "POST JSON-RPC 2.0 to this URL.",
        }
    )
