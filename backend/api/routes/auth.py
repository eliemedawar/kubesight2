from flask import Blueprint, request

from ..auth_utils import (
    PURPOSE_MFA,
    PURPOSE_ONBOARDING,
    get_current_user,
    get_interim_token,
    load_user_for_purpose,
)
from ..decorators import require_auth
from ..response import error_response, success_response
from ..session_auth import (
    REFRESH_COOKIE,
    apply_login_cookies,
    clear_auth_cookies,
    csrf_violation,
    current_session_id,
    issue_csrf_token,
    list_user_sessions,
    revoke_all_user_sessions,
    revoke_request_session,
    revoke_user_session,
    rotate_refresh_token,
    set_csrf_cookie,
    set_session_cookies,
)
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
    token = get_interim_token()
    if not token:
        return None
    return load_user_for_purpose(token, purpose)


def _success_with_login_cookies(data, status_code=200):
    response, status = success_response(data, status_code)
    apply_login_cookies(response, data)
    return response, status


@auth_bp.before_app_request
def require_csrf_for_cookie_mutations():
    violation = csrf_violation()
    if violation:
        return error_response(violation, 403)
    return None


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
    return _success_with_login_cookies(data)


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
    return _success_with_login_cookies(data)


@auth_bp.route("/first-login/totp/setup", methods=["POST"])
def first_login_totp_setup():
    user = _user_for_purpose(PURPOSE_ONBOARDING)
    if not user:
        return error_response("Onboarding session is invalid or expired.", 401)
    data, error, status = setup_totp(user)
    if error:
        return error_response(error, status)
    return _success_with_login_cookies(data)


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
    return _success_with_login_cookies(data)


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
    return _success_with_login_cookies(data)


@auth_bp.route("/csrf", methods=["GET"])
def csrf():
    token = issue_csrf_token()
    response, status = success_response({"csrfToken": token})
    set_csrf_cookie(response, token)
    return response, status


@auth_bp.route("/refresh", methods=["POST"])
def refresh():
    user, issued, error = rotate_refresh_token(
        request.cookies.get(REFRESH_COOKIE, "")
    )
    if error or not user or not issued:
        response, status = error_response(
            error or "Refresh session is invalid or expired.", 401
        )
        clear_auth_cookies(response)
        return response, status
    response, status = success_response(
        {
            "user": profile_for_user(user),
            "sessionId": issued.session_id,
        }
    )
    set_session_cookies(response, issued)
    return response, status


@auth_bp.route("/me", methods=["GET"])
@require_auth
def me():
    user = get_current_user()
    if not user:
        return error_response("Unauthorized", 401)
    return success_response(profile_for_user(user))


@auth_bp.route("/logout", methods=["POST"])
def logout():
    user = get_current_user()
    revoke_request_session(user)
    response, status = success_response(logout_user(user))
    clear_auth_cookies(response)
    return response, status


@auth_bp.route("/sessions", methods=["GET"])
@require_auth
def sessions():
    user = get_current_user()
    return success_response({"items": list_user_sessions(user)})


@auth_bp.route("/sessions/<session_id>", methods=["DELETE"])
@require_auth
def revoke_session(session_id: str):
    user = get_current_user()
    if not revoke_user_session(user, session_id, "user_revoked"):
        return error_response("Session not found.", 404)
    response, status = success_response({"revoked": True, "id": session_id})
    if current_session_id() == session_id:
        clear_auth_cookies(response)
    return response, status


@auth_bp.route("/sessions/revoke-all", methods=["POST"])
@require_auth
def revoke_all_sessions():
    user = get_current_user()
    count = revoke_all_user_sessions(user, "global_logout")
    response, status = success_response({"revoked": count})
    clear_auth_cookies(response)
    return response, status
