"""Hermes ticket agent — settings, the Telegram test, and the human side of approvals.

Hermes itself never calls these: it acts through the ``kubesight_ticket_*`` MCP
tools. These are for operators — configure the agent, see what it did, approve
or reject what it asked for, and hand a ticket back to it after the requester
fixed it. Approving lives here and on Telegram, never in an MCP tool, so the
agent that proposed an action can't also approve it.
"""

from flask import Blueprint, request

from ..audit import log_audit
from ..auth_utils import get_current_user
from ..decorators import require_permission
from ..response import error_response, success_response
from ..services.ticket_agent import engine, settings as agent_settings
from ..services.ticket_agent.engine import AgentError

ticket_agent_bp = Blueprint("ticket_agent", __name__, url_prefix="/api/ticket-agent")


def _who(user) -> str:
    return str(getattr(user, "username", "") or getattr(user, "email", "") or "operator")


@ticket_agent_bp.route("/settings", methods=["GET"])
@require_permission("ticketing:view")
def get_settings():
    return success_response(agent_settings.serialize())


@ticket_agent_bp.route("/settings", methods=["PUT"])
@require_permission("ticketing:manage")
def update_settings():
    payload = request.get_json(silent=True) or {}
    try:
        data = agent_settings.update(payload)
    except ValueError as exc:
        return error_response(str(exc), 400)
    log_audit(
        "ticket_agent_settings_updated",
        actor=get_current_user(),
        target_type="ticket_agent_settings",
        target_id="1",
        details={k: data.get(k) for k in ("enabled", "minConfidence", "publicComments",
                                           "approvalTimeoutHours", "telegramEnabled")},
    )
    return success_response(data)


@ticket_agent_bp.route("/telegram/test", methods=["POST"])
@require_permission("ticketing:manage")
def test_telegram():
    return success_response(agent_settings.test_telegram())


@ticket_agent_bp.route("/tasks/<int:task_id>/approve", methods=["POST"])
@require_permission("ticketing:manage")
def approve(task_id: int):
    user = get_current_user()
    try:
        return success_response(engine.approve(task_id, _who(user), user=user))
    except AgentError as exc:
        return error_response(str(exc), exc.status)


@ticket_agent_bp.route("/tasks/<int:task_id>/reject", methods=["POST"])
@require_permission("ticketing:manage")
def reject(task_id: int):
    user = get_current_user()
    note = str((request.get_json(silent=True) or {}).get("note") or "")
    try:
        return success_response(engine.reject(task_id, _who(user), note=note, user=user))
    except AgentError as exc:
        return error_response(str(exc), exc.status)


@ticket_agent_bp.route("/tickets/<int:record_id>/handle", methods=["POST"])
@require_permission("ticketing:manage")
def handle_again(record_id: int):
    if not agent_settings.is_active():
        return error_response(
            "The ticket agent is off, or Hermes is not configured for it "
            "(Settings → Integrations → Hermes ticket agent).",
            409,
        )
    try:
        task = engine.reinterpret(record_id, user=get_current_user())
    except AgentError as exc:
        return error_response(str(exc), exc.status)
    return success_response(engine.serialize(task), status_code=201)
