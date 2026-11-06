"""The KubeSight assistant (Ctrl+G): chat with Hermes from inside KubeSight.

Admins only, every route. Hermes acts through its own MCP token, which can
write — so the chat is opened to the people who could make those changes from
the UI themselves. Each admin sees only their own conversations.
"""

from flask import Blueprint, request

from ..audit import log_audit
from ..auth_utils import get_current_user
from ..decorators import require_admin
from ..response import error_response, success_response
from ..services.assistant import hermes, service
from ..services.assistant.service import AssistantError

assistant_bp = Blueprint("assistant", __name__, url_prefix="/api/assistant")


def _user():
    user = get_current_user()
    if user is None:
        raise AssistantError("Sign in to use the assistant.", 401)
    return user


@assistant_bp.route("/status", methods=["GET"])
@require_admin
def status():
    hint = hermes.configuration_hint()
    return success_response({"configured": not hint, "hint": hint or None, "model": hermes.model()})


@assistant_bp.route("/conversations", methods=["GET"])
@require_admin
def list_conversations():
    try:
        return success_response(service.list_conversations(_user()))
    except AssistantError as exc:
        return error_response(str(exc), exc.status)


@assistant_bp.route("/conversations", methods=["POST"])
@require_admin
def start_conversation():
    payload = request.get_json(silent=True) or {}
    try:
        user = _user()
        data = service.send_message(user, payload.get("message"), payload.get("context"))
    except AssistantError as exc:
        return error_response(str(exc), exc.status)
    _audit(user, data)
    return success_response(data, 201)


@assistant_bp.route("/conversations/<int:conversation_id>", methods=["GET"])
@require_admin
def get_conversation(conversation_id: int):
    try:
        return success_response(service.get_conversation(_user(), conversation_id))
    except AssistantError as exc:
        return error_response(str(exc), exc.status)


@assistant_bp.route("/conversations/<int:conversation_id>", methods=["DELETE"])
@require_admin
def delete_conversation(conversation_id: int):
    try:
        service.delete_conversation(_user(), conversation_id)
    except AssistantError as exc:
        return error_response(str(exc), exc.status)
    return success_response({"deleted": True})


@assistant_bp.route("/conversations/<int:conversation_id>/messages", methods=["POST"])
@require_admin
def send_message(conversation_id: int):
    payload = request.get_json(silent=True) or {}
    try:
        user = _user()
        data = service.send_message(
            user, payload.get("message"), payload.get("context"), conversation_id=conversation_id
        )
    except AssistantError as exc:
        return error_response(str(exc), exc.status)
    _audit(user, data)
    return success_response(data)


def _audit(user, conversation):
    # What was asked is the record that matters: whatever Hermes then changed
    # is audited again by the MCP tool it used.
    question = next(
        (m["content"] for m in reversed(conversation.get("messages") or []) if m["role"] == "user"),
        "",
    )
    log_audit(
        "assistant_message_sent",
        actor=user,
        target_type="assistant_conversation",
        target_id=str(conversation["id"]),
        details={"message": question[:500]},
    )
