from flask import Blueprint, g, request

from ..auth_utils import get_current_user
from ..decorators import require_permission
from ..k8s_provider import should_use_real_k8s
from ..response import error_response, success_response
from ..services.client_service import (
    create_client,
    delete_client,
    get_client,
    get_client_mock,
    list_clients,
    list_clients_mock,
    update_client,
)

clients_bp = Blueprint("clients", __name__, url_prefix="/api/clients")


def _actor_user_id() -> int | None:
    user = getattr(g, "current_user", None)
    return user.id if user else None


def _use_mock() -> bool:
    return not should_use_real_k8s()


# ---------------------------------------------------------------------------
# List & Create
# ---------------------------------------------------------------------------

@clients_bp.route("", methods=["GET"])
@require_permission("clients:view")
def list_all_clients():
    user = get_current_user()
    # Prefer real, DB-backed clients (incl. those linked by Deploy From
    # Blueprint). Fall back to the demo/mock list only when there are none and no
    # live cluster is configured.
    real = list_clients(user=user)
    if real.get("count", 0) == 0 and _use_mock():
        return success_response(list_clients_mock())
    return success_response(real)


@clients_bp.route("", methods=["POST"])
@require_permission("clients:create")
def create_new_client():
    payload = request.get_json(silent=True) or {}
    data, error, status = create_client(payload, actor_user_id=_actor_user_id())
    if error:
        return error_response(error, status)
    return success_response(data, status_code=status)


# ---------------------------------------------------------------------------
# Single resource
# ---------------------------------------------------------------------------

@clients_bp.route("/<int:client_id>", methods=["GET"])
@require_permission("clients:view")
def get_single_client(client_id: int):
    user = get_current_user()
    data, error, status = get_client(client_id, user=user)
    if error and status == 404 and _use_mock():
        data, error, status = get_client_mock(client_id)
    if error:
        return error_response(error, status)
    return success_response(data)


@clients_bp.route("/<int:client_id>", methods=["PUT"])
@require_permission("clients:update")
def update_existing_client(client_id: int):
    payload = request.get_json(silent=True) or {}
    data, error, status = update_client(client_id, payload, actor_user_id=_actor_user_id())
    if error:
        return error_response(error, status)
    return success_response(data)


@clients_bp.route("/<int:client_id>", methods=["DELETE"])
@require_permission("clients:delete")
def delete_existing_client(client_id: int):
    data, error, status = delete_client(client_id, actor_user_id=_actor_user_id())
    if error:
        return error_response(error, status)
    return success_response(data)
