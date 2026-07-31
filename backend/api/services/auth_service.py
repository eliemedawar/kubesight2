"""Authentication service — staged login, first-login onboarding and MFA.

Login is a small state machine rather than a single password check:

* Brand-new users are created with a random temporary password and
  ``must_change_password = True`` / ``first_login_completed = False``. Their
  first sign-in returns an *onboarding* token (not a full access token) and
  walks them through: change password → set up TOTP MFA → done.
* Fully-onboarded users with MFA enabled sign in with password, then answer a
  TOTP challenge (an interim *mfa* token) before receiving an access token.
* Legacy / seeded accounts (``first_login_completed = True``,
  ``mfa_enabled = False``) sign in with password only, exactly as before.

Password failures and MFA-code failures are counted independently. Five
consecutive failures of either kind lock the account for a cooldown window; a
third temporary lock inside 24 hours escalates to an admin-only unlock. Every
state transition is written to the audit log and (best-effort) emailed to the
user.
"""

from __future__ import annotations

import os
import re
import threading
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Dict, Optional, Tuple

from flask import current_app, request

from ..audit import log_audit
from ..auth_utils import (
    PURPOSE_MFA,
    PURPOSE_ONBOARDING,
    create_access_token,
    create_interim_token,
    current_user_profile,
)
from ..db import db
from ..email_delivery import (
    EmailDeliveryError,
    send_login_notification_email,
    send_security_event_email,
)
from ..models import AuditLog, User
from ..passwords import hash_password, verify_password
from .totp_service import build_enrollment, generate_totp_secret, verify_totp

# Onboarding stages surfaced to the client so it can render the right screen.
STAGE_AUTHENTICATED = "authenticated"
STAGE_PASSWORD_CHANGE = "password_change"
STAGE_MFA_SETUP = "mfa_setup"
STAGE_MFA = "mfa"  # verify an already-enrolled authenticator
STAGE_MFA_CHALLENGE = "mfa_challenge"  # normal-login TOTP challenge

# Lock reasons persisted in ``User.lock_reason``.
LOCK_PASSWORD = "failed_password"
LOCK_MFA = "failed_mfa"
LOCK_ADMIN = "admin"

# A third temporary lock within 24h escalates to an admin-only unlock.
_LOCK_ESCALATION_THRESHOLD = 3

_LOCKED_TEMP_MESSAGE = (
    "Too many failed login attempts. Your account has been temporarily locked."
)
_LOCKED_MFA_MESSAGE = (
    "Too many incorrect verification codes. Your account has been temporarily locked."
)
_LOCKED_ADMIN_MESSAGE = (
    "Your account has been locked for security reasons. Please contact your administrator."
)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _as_utc(value: Optional[datetime]) -> Optional[datetime]:
    if value is None:
        return None
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def max_failed_attempts() -> int:
    try:
        return max(1, int(os.getenv("MAX_FAILED_LOGIN_ATTEMPTS", "5")))
    except ValueError:
        return 5


def max_mfa_attempts() -> int:
    try:
        return max(1, int(os.getenv("MAX_FAILED_MFA_ATTEMPTS", "5")))
    except ValueError:
        return 5


def lockout_minutes() -> int:
    try:
        return max(1, int(os.getenv("ACCOUNT_LOCKOUT_MINUTES", "15")))
    except ValueError:
        return 15


def temp_password_expiry_hours() -> int:
    try:
        return max(1, int(os.getenv("TEMP_PASSWORD_EXPIRY_HOURS", "24")))
    except ValueError:
        return 24


def min_password_length() -> int:
    try:
        return max(8, int(os.getenv("MIN_PASSWORD_LENGTH", "12")))
    except ValueError:
        return 12


def validate_password_policy(password: str) -> Optional[str]:
    """Return an error string if the password fails policy, else ``None``.

    Policy: minimum length (default 12), at least one uppercase, one lowercase,
    one digit and one special character.
    """
    password = password or ""
    minimum = min_password_length()
    if len(password) < minimum:
        return f"Password must be at least {minimum} characters long."
    if not re.search(r"[A-Z]", password):
        return "Password must include at least one uppercase letter."
    if not re.search(r"[a-z]", password):
        return "Password must include at least one lowercase letter."
    if not re.search(r"[0-9]", password):
        return "Password must include at least one number."
    if not re.search(r"[^A-Za-z0-9]", password):
        return "Password must include at least one special character."
    return None


def _client_ip() -> str:
    if not request:
        return ""
    forwarded = request.headers.get("X-Forwarded-For", "")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.remote_addr or ""


def _user_agent() -> str:
    if not request:
        return ""
    return (request.headers.get("User-Agent") or "").strip()[:512]


def find_user(identifier: str) -> Optional[User]:
    """Resolve a login identifier that may be either a username or an email."""
    identifier = (identifier or "").strip()
    if not identifier:
        return None
    user = User.query.filter_by(username=identifier).first()
    if user:
        return user
    if "@" in identifier:
        return User.query.filter(db.func.lower(User.email) == identifier.lower()).first()
    return None


def onboarding_stage(user: User) -> str:
    """Compute which first-login step a user still needs to complete."""
    if user.must_change_password:
        return STAGE_PASSWORD_CHANGE
    if not user.mfa_enabled:
        return STAGE_MFA_SETUP
    if not user.first_login_completed:
        return STAGE_MFA
    return STAGE_AUTHENTICATED


# ---------------------------------------------------------------------------
# Lock / failed-attempt helpers
# ---------------------------------------------------------------------------

def _is_temp_locked(user: User) -> bool:
    locked_until = _as_utc(user.locked_until)
    return bool(locked_until and locked_until > _now())


def is_locked(user: User) -> bool:
    return bool(user.requires_admin_unlock) or _is_temp_locked(user)


def _lock_remaining_minutes(user: User) -> int:
    locked_until = _as_utc(user.locked_until)
    if not locked_until:
        return 0
    delta = locked_until - _now()
    return max(1, int(delta.total_seconds() // 60) + 1)


def _locked_message(user: User) -> str:
    if user.requires_admin_unlock:
        return _LOCKED_ADMIN_MESSAGE
    if user.lock_reason == LOCK_MFA:
        return _LOCKED_MFA_MESSAGE
    return _LOCKED_TEMP_MESSAGE


def _lock_details(user: User) -> Dict[str, Any]:
    """Structured lock info returned alongside 423 errors so the client can
    render the right state (admin unlock vs. a temporary hold with countdown)."""
    if user.requires_admin_unlock:
        return {"kind": "admin"}
    locked_until = _as_utc(user.locked_until)
    remaining = 0
    if locked_until:
        remaining = max(0, int((locked_until - _now()).total_seconds()))
    return {
        "kind": "temporary",
        "reason": user.lock_reason,
        "retryAfterSeconds": remaining,
        "lockedUntil": locked_until.isoformat() if locked_until else None,
    }


def _recent_temp_lock_count(user: User) -> int:
    """Number of temporary locks recorded for this user within the last 24h."""
    since = _now() - timedelta(hours=24)
    return (
        AuditLog.query.filter(
            AuditLog.action == "account_temp_locked",
            AuditLog.target_id == str(user.id),
            AuditLog.created_at >= since,
        ).count()
    )


def _send_email_async(task: Callable[[], None]) -> None:
    """Run an email-send task without blocking the request.

    The SMTP handshake can take seconds (up to the 30s socket timeout when the
    relay is slow or down), and these notifications sit directly on the login
    path — the user must never wait on them for their session token. Under
    TESTING the task runs inline so tests stay deterministic. Failures are
    swallowed: notification email is always best-effort.
    """
    app = current_app._get_current_object()
    if app.config.get("TESTING"):
        try:
            task()
        except Exception:
            pass
        return

    def _runner() -> None:
        with app.app_context():
            try:
                task()
            except Exception:
                pass

    threading.Thread(target=_runner, name="kubesight-auth-email", daemon=True).start()


def _send_security_email(user: User, subject: str, headline: str, lines, *, contact_admin=True) -> None:
    if not user.email or "@" not in user.email:
        return
    # Capture plain values now: the background task must not touch the request
    # context or the session-bound user instance.
    email = user.email
    username = user.username
    full_name = user.full_name
    ip_address = _client_ip()
    detail_lines = list(lines)

    def _task() -> None:
        send_security_event_email(
            email,
            username=username,
            full_name=full_name,
            subject=subject,
            headline=headline,
            lines=detail_lines,
            ip_address=ip_address,
            show_contact_admin=contact_admin,
        )

    _send_email_async(_task)


def _apply_lock(user: User, reason: str) -> None:
    """Lock the account after a failed-attempt threshold is hit.

    Applies a temporary 15-minute lock, then escalates to an admin-only unlock if
    this is the third temporary lock inside 24 hours.
    """
    user.locked_until = _now() + timedelta(minutes=lockout_minutes())
    user.lock_reason = reason

    prior_locks = _recent_temp_lock_count(user)  # excludes the one we log below
    lock_number = prior_locks + 1
    user.lock_count_24h = lock_number
    db.session.commit()

    log_audit(
        "account_temp_locked",
        actor_user_id=user.id,
        target_type="user",
        target_id=str(user.id),
        details={
            "username": user.username,
            "reason": reason,
            "lockedUntil": user.locked_until.isoformat() if user.locked_until else None,
            "lockCount24h": lock_number,
            "ip": _client_ip(),
        },
    )

    if reason == LOCK_MFA:
        self_msg = "Too many incorrect verification codes were entered."
        subject = "KubeSight account temporarily locked (verification codes)"
    else:
        self_msg = "Too many failed sign-in attempts were made."
        subject = "KubeSight account temporarily locked"
    self_lines = [
        self_msg,
        f"Your account is locked for {lockout_minutes()} minutes.",
    ]
    _send_security_email(user, subject, "Your KubeSight account has been temporarily locked", self_lines)

    if lock_number >= _LOCK_ESCALATION_THRESHOLD:
        user.requires_admin_unlock = True
        db.session.commit()
        log_audit(
            "account_admin_locked",
            actor_user_id=user.id,
            target_type="user",
            target_id=str(user.id),
            details={"username": user.username, "lockCount24h": lock_number, "ip": _client_ip()},
        )
        _send_security_email(
            user,
            "KubeSight account locked — administrator unlock required",
            "Your KubeSight account has been locked for security reasons",
            [
                "Your account was locked multiple times and now requires an administrator to unlock it.",
                "Please contact your administrator to regain access.",
            ],
        )


def _register_password_failure(user: Optional[User], identifier: str) -> None:
    if not user:
        log_audit(
            "login_failed",
            target_type="user",
            target_id=identifier,
            details={"reason": "invalid_credentials", "ip": _client_ip()},
        )
        return
    user.failed_login_attempts = (user.failed_login_attempts or 0) + 1
    user.last_failed_login_at = _now()
    db.session.commit()
    log_audit(
        "login_failed",
        actor_user_id=user.id,
        target_type="user",
        target_id=str(user.id),
        details={
            "reason": "invalid_credentials",
            "username": user.username,
            "failedAttempts": user.failed_login_attempts,
            "ip": _client_ip(),
        },
    )
    if user.failed_login_attempts >= max_failed_attempts() and not _is_temp_locked(user):
        _apply_lock(user, LOCK_PASSWORD)


def _register_mfa_failure(user: User) -> bool:
    """Count a wrong TOTP code. Returns True if this failure locked the account."""
    user.mfa_failed_attempts = (user.mfa_failed_attempts or 0) + 1
    user.last_failed_login_at = _now()
    db.session.commit()
    log_audit(
        "login_failed_mfa",
        actor_user_id=user.id,
        target_type="user",
        target_id=str(user.id),
        details={
            "username": user.username,
            "mfaFailedAttempts": user.mfa_failed_attempts,
            "ip": _client_ip(),
        },
    )
    if user.mfa_failed_attempts >= max_mfa_attempts() and not _is_temp_locked(user):
        _apply_lock(user, LOCK_MFA)
        return True
    return False


def _reset_counters(user: User) -> None:
    """Clear failed-attempt counters and any expired temporary lock state."""
    user.failed_login_attempts = 0
    user.mfa_failed_attempts = 0
    if not user.requires_admin_unlock:
        user.locked_until = None
        user.lock_reason = None


# ---------------------------------------------------------------------------
# Payload builders / login completion
# ---------------------------------------------------------------------------

def _onboarding_payload(user: User, stage: str) -> Dict[str, Any]:
    return {
        "stage": stage,
        "onboardingToken": create_interim_token(user, PURPOSE_ONBOARDING),
        "mustChangePassword": bool(user.must_change_password),
        "mfaEnabled": bool(user.mfa_enabled),
        "username": user.username,
    }


def _send_login_email(user: User) -> None:
    """Send the "your account was signed in" security email. Never fatal."""
    if not user.email or "@" not in user.email:
        return
    # Capture plain values now: the background task must not touch the request
    # context or the session-bound user instance.
    email = user.email
    username = user.username
    full_name = user.full_name
    user_id = user.id
    login_time = _now().strftime("%Y-%m-%d %H:%M:%S UTC")
    ip_address = _client_ip()
    user_agent = _user_agent()

    def _task() -> None:
        try:
            send_login_notification_email(
                email,
                username=username,
                full_name=full_name,
                login_time=login_time,
                ip_address=ip_address,
                user_agent=user_agent,
            )
        except (EmailDeliveryError, Exception):
            return
        log_audit(
            "login_email_sent",
            actor_user_id=user_id,
            target_type="user",
            target_id=str(user_id),
            details={"email": email, "ip": ip_address},
        )

    _send_email_async(_task)


def _complete_login(user: User) -> Tuple[Dict[str, Any], None, int]:
    """Finalize a successful authentication and issue a full access token."""
    user.last_login_at = _now()
    user.last_login_ip = _client_ip()
    _reset_counters(user)
    db.session.commit()

    log_audit(
        "login_success",
        actor_user_id=user.id,
        target_type="user",
        target_id=str(user.id),
        details={"username": user.username, "ip": _client_ip()},
    )
    _send_login_email(user)

    token = create_access_token(user)
    return (
        {"stage": STAGE_AUTHENTICATED, "token": token, "user": current_user_profile(user)},
        None,
        200,
    )


def _locked_response(user: User) -> Tuple[Optional[Dict[str, Any]], str, int]:
    log_audit(
        "login_failed",
        actor_user_id=user.id,
        target_type="user",
        target_id=str(user.id),
        details={
            "reason": "account_admin_locked" if user.requires_admin_unlock else "account_locked",
            "username": user.username,
            "ip": _client_ip(),
        },
    )
    return {"lock": _lock_details(user)}, _locked_message(user), 423


# ---------------------------------------------------------------------------
# Login state machine
# ---------------------------------------------------------------------------

def login_user(
    identifier: str, password: str
) -> Tuple[Optional[Dict[str, Any]], Optional[str], int]:
    """Validate credentials and return the next step of the login state machine.

    Returns ``(payload, error_message, http_status)``.
    """
    user = find_user(identifier)

    if not user or not user.is_active:
        if user and not user.is_active:
            log_audit(
                "login_failed",
                actor_user_id=user.id,
                target_type="user",
                target_id=str(user.id),
                details={"reason": "account_disabled", "username": user.username, "ip": _client_ip()},
            )
            return None, "This account has been disabled. Contact your administrator.", 403
        _register_password_failure(None, identifier)
        return None, "Invalid credentials", 401

    # A lock (temporary or admin) blocks login even with the correct password.
    if is_locked(user):
        return _locked_response(user)

    if not verify_password(password, user.password_hash):
        _register_password_failure(user, identifier)
        if is_locked(user):
            return _locked_response(user)
        return None, "Invalid credentials", 401

    if getattr(user, "is_service_account", False) or not getattr(
        user, "interactive_login_enabled", True
    ):
        log_audit(
            "login_failed",
            actor_user_id=user.id,
            target_type="service_account",
            target_id=str(user.id),
            details={
                "reason": "interactive_login_disabled",
                "username": user.username,
                "ip": _client_ip(),
            },
        )
        return None, "Interactive login is disabled for this service account.", 403

    # Password is correct — clear failure counters (but keep an admin lock).
    _reset_counters(user)

    # --- Temporary-password path -----------------------------------------
    if user.must_change_password:
        if user.temporary_password_used:
            db.session.commit()
            return None, "This temporary password has already been used. Ask an administrator to resend it.", 403
        expires_at = _as_utc(user.temporary_password_expires_at)
        if expires_at and expires_at < _now():
            db.session.commit()
            log_audit(
                "login_failed",
                actor_user_id=user.id,
                target_type="user",
                target_id=str(user.id),
                details={"reason": "temporary_password_expired", "username": user.username},
            )
            return None, "Your temporary password has expired. Ask an administrator to resend it.", 403
        db.session.commit()
        log_audit(
            "first_login_started",
            actor_user_id=user.id,
            target_type="user",
            target_id=str(user.id),
            details={"username": user.username, "ip": _client_ip()},
        )
        return _onboarding_payload(user, STAGE_PASSWORD_CHANGE), None, 200

    # --- Onboarding not yet complete (e.g. MFA reset / forced re-enrolment) --
    if not user.first_login_completed:
        db.session.commit()
        stage = onboarding_stage(user)
        if stage == STAGE_AUTHENTICATED:
            user.first_login_completed = True
            return _complete_login(user)
        return _onboarding_payload(user, stage), None, 200

    # --- Normal login with MFA challenge ---------------------------------
    if user.mfa_enabled:
        db.session.commit()
        return (
            {
                "stage": STAGE_MFA_CHALLENGE,
                "mfaToken": create_interim_token(user, PURPOSE_MFA),
                "username": user.username,
            },
            None,
            200,
        )

    # --- Legacy / MFA-less account: straight in --------------------------
    return _complete_login(user)


# ---------------------------------------------------------------------------
# First-login onboarding steps (authorized by an onboarding-purpose token)
# ---------------------------------------------------------------------------

def change_temporary_password(
    user: User, new_password: str
) -> Tuple[Optional[Dict[str, Any]], Optional[str], int]:
    new_password = new_password or ""
    policy_error = validate_password_policy(new_password)
    if policy_error:
        return None, policy_error, 400
    if verify_password(new_password, user.password_hash):
        return None, "New password must be different from the temporary password.", 400

    user.password_hash = hash_password(new_password)
    user.must_change_password = False
    user.temporary_password_used = True
    user.temporary_password_expires_at = None
    db.session.commit()
    log_audit(
        "password_changed",
        actor_user_id=user.id,
        target_type="user",
        target_id=str(user.id),
        details={"username": user.username, "context": "first_login"},
    )
    _send_security_email(
        user,
        "Your KubeSight password was changed",
        "Your KubeSight password was changed",
        ["Your account password was changed successfully."],
    )

    stage = onboarding_stage(user)
    return {"stage": stage, "mfaEnabled": bool(user.mfa_enabled)}, None, 200


def setup_totp(user: User) -> Tuple[Optional[Dict[str, Any]], Optional[str], int]:
    """Generate (or regenerate) a pending TOTP secret and return enrolment data."""
    if user.must_change_password:
        return None, "Change your temporary password before setting up MFA.", 400
    if user.mfa_enabled:
        return None, "MFA is already enabled for this account.", 400

    secret = generate_totp_secret()
    user.totp_secret = secret
    db.session.commit()
    log_audit(
        "mfa_setup_started",
        actor_user_id=user.id,
        target_type="user",
        target_id=str(user.id),
        details={"username": user.username},
    )
    account_name = user.email or user.username
    return build_enrollment(secret, account_name), None, 200


def verify_first_login_totp(
    user: User, code: str
) -> Tuple[Optional[Dict[str, Any]], Optional[str], int]:
    if is_locked(user):
        return _locked_response(user)
    if user.must_change_password:
        return None, "Change your temporary password before verifying MFA.", 400
    if not user.totp_secret:
        return None, "Start MFA setup before verifying a code.", 400
    if not verify_totp(user.totp_secret, code):
        locked = _register_mfa_failure(user)
        if locked:
            return {"lock": _lock_details(user)}, _locked_message(user), 423
        return None, "Invalid or expired authentication code. Try again.", 400

    first_time = not user.mfa_enabled
    user.mfa_enabled = True
    user.first_login_completed = True
    user.must_change_password = False
    user.temporary_password_expires_at = None
    db.session.commit()
    if first_time:
        log_audit(
            "mfa_enabled",
            actor_user_id=user.id,
            target_type="user",
            target_id=str(user.id),
            details={"username": user.username},
        )
    return _complete_login(user)


# ---------------------------------------------------------------------------
# Normal-login MFA challenge (authorized by an mfa-purpose token)
# ---------------------------------------------------------------------------

def verify_login_mfa(
    user: User, code: str
) -> Tuple[Optional[Dict[str, Any]], Optional[str], int]:
    if is_locked(user):
        return _locked_response(user)
    if not user.mfa_enabled or not user.totp_secret:
        return None, "MFA is not configured for this account.", 400
    if not verify_totp(user.totp_secret, code):
        locked = _register_mfa_failure(user)
        if locked:
            # Pending MFA session is cancelled: the interim token no longer maps
            # to a usable state and the client must restart login after the lock.
            return {"lock": _lock_details(user)}, _locked_message(user), 423
        return None, "Invalid or expired authentication code. Try again.", 400
    return _complete_login(user)


# ---------------------------------------------------------------------------
# Session helpers
# ---------------------------------------------------------------------------

def profile_for_user(user: User) -> Dict[str, Any]:
    return current_user_profile(user)


def logout_user(user: Optional[User]) -> Dict[str, str]:
    if user:
        log_audit("logout", actor=user, target_type="user", target_id=str(user.id))
    return {"message": "Logged out"}
