"""Where the agent platform reports back.

Not a user route: the platform authenticates with the shared
``AGENT_PLATFORM_TOKEN``, the same secret KubeSight presents to it. It never
acts through these — every change it makes goes through the MCP tools under
its own account. This only tells KubeSight a ticket task is over.
"""

from flask import Blueprint, request

from ..audit import log_audit
from ..response import error_response, success_response
from ..services import agent_platform
from ..services.ticket_agent import engine
from ..services.ticket_agent.engine import AgentError

agent_platform_bp = Blueprint("agent_platform", __name__, url_prefix="/api/agent-platform")


@agent_platform_bp.route("/ticket-tasks/<int:task_id>/result", methods=["POST"])
def ticket_task_result(task_id: int):
    if not agent_platform.verify_callback(request.headers.get("Authorization", "")):
        return error_response("Unauthorized", 401)
    payload = request.get_json(silent=True)
    if not isinstance(payload, dict) or payload.get("status") not in ("finished", "failed"):
        return error_response("Expected {status: finished|failed, ...}.", 400)
    try:
        result = engine.on_platform_report(task_id, payload)
    except AgentError as exc:
        return error_response(str(exc), exc.status)
    log_audit(
        "ticket_agent_platform_report",
        actor=None,
        target_type="ticket_interpretation",
        target_id=str(task_id),
        details={
            "status": payload.get("status"),
            "outcome": str(payload.get("outcome") or "")[:40] or None,
            "taskStatus": result.get("status"),
        },
    )
    return success_response(result)
