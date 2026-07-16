from flask import Blueprint, request

from ..auth_utils import (
    PURPOSE_MFA,
    PURPOSE_ONBOARDING,
    get_bearer_token,
    get_current_user,
    load_user_for_purpose,
)
from ..decorators import require_auth
from ..response import error_response, success_response
from ..services.auth_service import (
    change_temporary_password,
    login_user,
    logout_user,
    profile_for_user,
    setup_totp,
    verify_first_login_totp,
    verify_login_mfa,
)

auth_bp = Blueprint("auth", __name__, url_prefix="/api/auth")


def _user_for_purpose(purpose: str):
    """Resolve the current request's interim (onboarding / MFA) token holder."""
    token = get_bearer_token()
    if not token:
        return None
    return load_user_for_purpose(token, purpose)


@auth_bp.route("/login", methods=["POST"])
def login():
    payload = request.get_json(silent=True) or {}
    # Accept either "username" or "email" as the identifier field.
    identifier = (payload.get("username") or payload.get("email") or "").strip()
    password = payload.get("password") or ""

    data, error, status = login_user(identifier, password)
    if error:
        # `data` may carry structured details (e.g. lock kind + retry seconds).
        return error_response(error, status, data=data)
    return success_response(data)


@auth_bp.route("/first-login/change-password", methods=["POST"])
def first_login_change_password():
    user = _user_for_purpose(PURPOSE_ONBOARDING)
    if not user:
        return error_response("Onboarding session is invalid or expired.", 401)
    payload = request.get_json(silent=True) or {}
    new_password = payload.get("newPassword") or payload.get("password") or ""
    data, error, status = change_temporary_password(user, new_password)
    if error:
        return error_response(error, status)
    return success_response(data)


@auth_bp.route("/first-login/totp/setup", methods=["POST"])
def first_login_totp_setup():
    user = _user_for_purpose(PURPOSE_ONBOARDING)
    if not user:
        return error_response("Onboarding session is invalid or expired.", 401)
    data, error, status = setup_totp(user)
    if error:
        return error_response(error, status)
    return success_response(data)


@auth_bp.route("/first-login/totp/verify", methods=["POST"])
def first_login_totp_verify():
    user = _user_for_purpose(PURPOSE_ONBOARDING)
    if not user:
        return error_response("Onboarding session is invalid or expired.", 401)
    payload = request.get_json(silent=True) or {}
    code = payload.get("code") or ""
    data, error, status = verify_first_login_totp(user, code)
    if error:
        return error_response(error, status, data=data)
    return success_response(data)


@auth_bp.route("/mfa/verify", methods=["POST"])
def mfa_verify():
    user = _user_for_purpose(PURPOSE_MFA)
    if not user:
        return error_response("MFA session is invalid or expired.", 401)
    payload = request.get_json(silent=True) or {}
    code = payload.get("code") or ""
    data, error, status = verify_login_mfa(user, code)
    if error:
        return error_response(error, status, data=data)
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
