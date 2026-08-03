from flask import Blueprint, redirect, request

from ..audit import log_audit
from ..auth_utils import (
    PURPOSE_MFA,
    PURPOSE_ONBOARDING,
    create_interim_token,
    get_current_user,
    get_interim_token,
    load_user_for_purpose,
)
from ..decorators import require_auth
from ..mfa_recovery import (
    complete_login_with_recovery_code,
    consume_admin_recovery_grant,
    recovery_code_count,
    regenerate_recovery_codes,
)
from ..oidc import OidcConfigurationError, OidcProtocolError, safe_return_to
from ..oidc_auth import (
    OIDC_FLOW_COOKIE,
    clear_oidc_flow_cookie,
    complete_oidc_login,
    oidc_status,
    set_oidc_flow_cookie,
    start_oidc_login,
)
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
from ..services.totp_service import verify_totp

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
    was_mfa_enabled = bool(user.mfa_enabled)
    data, error, status = verify_first_login_totp(user, code)
    if error:
        return error_response(error, status, data=data)
    if not was_mfa_enabled and data and data.get("stage") == "authenticated":
        data["recoveryCodes"] = regenerate_recovery_codes(user)
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


@auth_bp.route("/mfa/recover", methods=["POST"])
def mfa_recover():
    user = _user_for_purpose(PURPOSE_MFA)
    if not user:
        return error_response("MFA session is invalid or expired.", 401)
    payload = request.get_json(silent=True) or {}
    data, error, status = complete_login_with_recovery_code(
        user, payload.get("recoveryCode") or ""
    )
    if error:
        return error_response(error, status)
    return _success_with_login_cookies(data)


@auth_bp.route("/mfa/recovery-codes", methods=["GET"])
@require_auth
def mfa_recovery_status():
    user = get_current_user()
    return success_response({"remaining": recovery_code_count(user)})


@auth_bp.route("/mfa/recovery-codes", methods=["POST"])
@require_auth
def mfa_recovery_regenerate():
    user = get_current_user()
    payload = request.get_json(silent=True) or {}
    if not user.mfa_enabled or not user.totp_secret:
        return error_response("MFA is not configured for this account.", 400)
    if not verify_totp(user.totp_secret, payload.get("code") or ""):
        log_audit(
            "mfa_recovery_codes_regeneration_rejected",
            actor=user,
            target_type="user",
            target_id=user.id,
            details={"outcome": "rejected"},
        )
        return error_response("Invalid or expired authentication code.", 400)
    codes = regenerate_recovery_codes(user)
    return success_response({"recoveryCodes": codes, "remaining": len(codes)})


@auth_bp.route("/admin-recovery", methods=["POST"])
def admin_recovery():
    payload = request.get_json(silent=True) or {}
    user = consume_admin_recovery_grant(
        payload.get("username") or "", payload.get("recoveryToken") or ""
    )
    if not user:
        return error_response("Admin recovery credentials are invalid or expired.", 401)
    token = create_interim_token(user, PURPOSE_ONBOARDING)
    return _success_with_login_cookies(
        {
            "stage": "mfa_setup",
            "onboardingToken": token,
            "mustChangePassword": False,
            "mfaEnabled": False,
            "username": user.username,
        }
    )


def _no_store(response):
    response.headers["Cache-Control"] = "no-store"
    response.headers["Pragma"] = "no-cache"
    response.headers["Referrer-Policy"] = "no-referrer"
    return response


@auth_bp.route("/oidc/status", methods=["GET"])
def oidc_provider_status():
    response, status = success_response(oidc_status())
    return _no_store(response), status


@auth_bp.route("/oidc/login", methods=["GET"])
def oidc_login():
    try:
        return_to = safe_return_to(request.args.get("returnTo"))
    except OidcProtocolError:
        return error_response("OIDC returnTo must be a local path.", 400)
    try:
        started = start_oidc_login(return_to=return_to)
    except OidcConfigurationError as exc:
        log_audit(
            "oidc_login_failed",
            target_type="oidc",
            details={"stage": "start", "errorType": type(exc).__name__},
        )
        return error_response("OIDC is not configured.", 503)
    except OidcProtocolError as exc:
        log_audit(
            "oidc_login_failed",
            target_type="oidc",
            details={"stage": "start", "errorType": type(exc).__name__},
        )
        return error_response("OIDC provider is unavailable.", 502)
    response = redirect(started.authorization_url, code=302)
    set_oidc_flow_cookie(response, started.browser_binding)
    return _no_store(response)


@auth_bp.route("/oidc/callback", methods=["GET"])
def oidc_callback():
    provider_error = request.args.get("error")
    state = request.args.get("state") or ""
    code = request.args.get("code") or ""
    browser_binding = request.cookies.get(OIDC_FLOW_COOKIE, "")
    if provider_error or not state or not code or not browser_binding:
        log_audit(
            "oidc_login_failed",
            target_type="oidc",
            details={"stage": "callback", "errorType": "ProviderResponse"},
        )
        response = redirect("/login?oidc=failed", code=303)
        clear_oidc_flow_cookie(response)
        return _no_store(response)
    try:
        completed = complete_oidc_login(
            state=state, code=code, browser_binding=browser_binding
        )
    except (OidcConfigurationError, OidcProtocolError):
        response = redirect("/login?oidc=failed", code=303)
        clear_oidc_flow_cookie(response)
        return _no_store(response)
    response = redirect(completed.return_to, code=303)
    set_session_cookies(response, completed.issued_session)
    clear_oidc_flow_cookie(response)
    return _no_store(response)


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
