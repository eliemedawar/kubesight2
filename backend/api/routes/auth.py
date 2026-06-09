from flask import Blueprint, request

from ..auth_utils import get_current_user
from ..decorators import require_auth
from ..response import error_response, success_response
from ..services.auth_service import login_user, logout_user, profile_for_user

auth_bp = Blueprint("auth", __name__, url_prefix="/api/auth")


@auth_bp.route("/login", methods=["POST"])
def login():
    payload = request.get_json(silent=True) or {}
    username = (payload.get("username") or "").strip()
    password = payload.get("password") or ""

    data, error, status = login_user(username, password)
    if error:
        return error_response(error, status)
    return success_response(data)


@auth_bp.route("/me", methods=["GET"])
@require_auth
def me():
    user = get_current_user()
    if not user:
        return error_response("Unauthorized", 401)
    return success_response(profile_for_user(user))


@auth_bp.route("/logout", methods=["POST"])
@require_auth
def logout():
    user = get_current_user()
    return success_response(logout_user(user))
