from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

from ..audit import log_audit
from ..auth_utils import get_current_user
from ..db import db
from ..email_delivery import EmailDeliveryError, send_temporary_password_email
from ..models import Role, User
from ..passwords import generate_temporary_password, hash_password
from ..serializers import (
    apply_user_access,
    parse_cluster_access_payload,
    parse_namespace_access_payload,
    user_list_item,
    user_to_dict,
)


def _temp_password_expiry_hours() -> int:
    from .auth_service import temp_password_expiry_hours

    return temp_password_expiry_hours()


def _provision_temporary_password(user: User) -> str:
    """Generate a fresh temporary password, store its hash, and reset onboarding.

    Puts the account back into the first-login state: the temporary password must
    be changed within the expiry window, and dashboard access stays blocked until
    the full onboarding (password change + MFA setup) is completed.
    """
    temp_password = generate_temporary_password()
    user.password_hash = hash_password(temp_password)
    user.must_change_password = True
    user.temporary_password_used = False
    user.temporary_password_expires_at = datetime.now(timezone.utc) + timedelta(
        hours=_temp_password_expiry_hours()
    )
    user.first_login_completed = False
    # Re-provisioning a temporary password is an admin recovery action, so clear
    # any failure counters and lock state to restore access.
    user.failed_login_attempts = 0
    user.mfa_failed_attempts = 0
    user.locked_until = None
    user.lock_reason = None
    user.lock_count_24h = 0
    user.requires_admin_unlock = False
    return temp_password


def _deliver_temporary_password(user: User, temp_password: str) -> bool:
    """Email the temporary password. Returns True if delivery succeeded."""
    try:
        send_temporary_password_email(
            user.email,
            username=user.username,
            full_name=user.full_name,
            temporary_password=temp_password,
            expires_hours=_temp_password_expiry_hours(),
        )
        return True
    except EmailDeliveryError:
        return False
    except Exception:
        return False


def active_admin_count() -> int:
    admin_role = Role.query.filter_by(name="admin").first()
    if not admin_role:
        return 0
    return User.query.filter_by(role_id=admin_role.id, is_active=True).count()


def list_users() -> Dict[str, Any]:
    users = User.query.order_by(User.username.asc()).all()
    return {"items": [user_list_item(u) for u in users], "count": len(users)}


def get_user(user_id: int) -> Tuple[Optional[Dict[str, Any]], Optional[str], int]:
    user = User.query.get(user_id)
    if not user:
        return None, "User not found", 404
    return user_to_dict(user, include_access=True), None, 200


def create_user(payload: Dict[str, Any]) -> Tuple[Optional[Dict[str, Any]], Optional[str], int]:
    """Create a user with a system-generated temporary password.

    The admin never sets a password: KubeSight generates a random temporary
    password, hashes it, and emails it to the user. The account is provisioned in
    the first-login state (must change password + set up MFA before any dashboard
    access). When SMTP is not configured, the plaintext temporary password is
    returned in the response so the admin can convey it out of band.
    """
    username = (payload.get("username") or "").strip()
    email = (payload.get("email") or "").strip()
    full_name = (payload.get("fullName") or "").strip()
    role_id = payload.get("roleId")

    if not username or not role_id:
        return None, "username, email, and roleId are required", 400
    if not email or "@" not in email:
        return None, "A valid email address is required so the temporary password can be sent", 400

    if User.query.filter_by(username=username).first():
        return None, "Username already exists", 409

    role = Role.query.get(role_id)
    if not role:
        return None, "Role not found", 404

    actor = get_current_user()
    user = User(
        username=username,
        email=email,
        full_name=full_name or username,
        role_id=role.id,
        is_active=True,
        created_by_admin_id=actor.id if actor else None,
    )
    temp_password = _provision_temporary_password(user)
    db.session.add(user)
    db.session.flush()

    access_rules = payload.get("accessRules")
    if access_rules is not None:
        apply_user_access(user, [], [], access_rules=access_rules)
    else:
        cluster_ids = parse_cluster_access_payload(payload.get("clusterAccess"))
        namespace_rows = parse_namespace_access_payload(payload.get("namespaceAccess"))
        apply_user_access(user, cluster_ids, namespace_rows)

    db.session.commit()
    log_audit(
        "user_created",
        actor=get_current_user(),
        target_type="user",
        target_id=str(user.id),
        details={"username": user.username, "email": user.email, "role": role.name},
    )

    emailed = _deliver_temporary_password(user, temp_password)
    log_audit(
        "temporary_password_sent",
        actor=get_current_user(),
        target_type="user",
        target_id=str(user.id),
        details={"username": user.username, "email": user.email, "emailed": emailed},
    )

    result = user_to_dict(user, include_access=True)
    result["temporaryPasswordEmailed"] = emailed
    result["temporaryPasswordExpiresAt"] = (
        user.temporary_password_expires_at.isoformat()
        if user.temporary_password_expires_at
        else None
    )
    if not emailed:
        # SMTP unavailable — surface the plaintext so the admin can deliver it.
        result["temporaryPassword"] = temp_password
    return result, None, 201


def resend_temporary_password(
    user_id: int,
) -> Tuple[Optional[Dict[str, Any]], Optional[str], int]:
    """Regenerate a temporary password for a user and re-send the onboarding email."""
    user = User.query.get(user_id)
    if not user:
        return None, "User not found", 404
    if not user.is_active:
        return None, "Cannot send a temporary password to a disabled account", 400
    if not user.email or "@" not in user.email:
        return None, "User has no valid email address on file", 400

    temp_password = _provision_temporary_password(user)
    db.session.commit()

    emailed = _deliver_temporary_password(user, temp_password)
    log_audit(
        "temporary_password_resent",
        actor=get_current_user(),
        target_type="user",
        target_id=str(user.id),
        details={"username": user.username, "email": user.email, "emailed": emailed},
    )

    result = {
        "id": user.id,
        "username": user.username,
        "temporaryPasswordEmailed": emailed,
        "temporaryPasswordExpiresAt": (
            user.temporary_password_expires_at.isoformat()
            if user.temporary_password_expires_at
            else None
        ),
    }
    if not emailed:
        result["temporaryPassword"] = temp_password
    return result, None, 200


def reset_mfa(user_id: int) -> Tuple[Optional[Dict[str, Any]], Optional[str], int]:
    """Clear a user's MFA enrolment so they must set it up again at next login.

    Their password is left intact; ``first_login_completed`` is reset so the next
    sign-in routes back through MFA setup before any dashboard access.
    """
    user = User.query.get(user_id)
    if not user:
        return None, "User not found", 404

    user.mfa_enabled = False
    user.totp_secret = None
    user.first_login_completed = False
    db.session.commit()
    log_audit(
        "mfa_reset",
        actor=get_current_user(),
        target_type="user",
        target_id=str(user.id),
        details={"username": user.username},
    )
    _notify_security_event(
        user,
        "Your KubeSight MFA has been reset",
        "Your KubeSight multi-factor authentication was reset",
        [
            "An administrator reset the multi-factor authentication on your account.",
            "You will be asked to set up MFA again the next time you sign in.",
        ],
    )
    return {"id": user.id, "username": user.username, "mfaEnabled": False}, None, 200


def _notify_security_event(user: User, subject: str, headline: str, lines) -> None:
    """Best-effort security email for admin-initiated account changes."""
    from ..email_delivery import send_security_event_email

    if not user.email or "@" not in user.email:
        return
    try:
        send_security_event_email(
            user.email,
            username=user.username,
            full_name=user.full_name,
            subject=subject,
            headline=headline,
            lines=list(lines),
        )
    except Exception:
        pass


def unlock_account(user_id: int) -> Tuple[Optional[Dict[str, Any]], Optional[str], int]:
    """Admin action: clear all lock state and failure counters."""
    user = User.query.get(user_id)
    if not user:
        return None, "User not found", 404

    user.locked_until = None
    user.lock_reason = None
    user.requires_admin_unlock = False
    user.lock_count_24h = 0
    user.failed_login_attempts = 0
    user.mfa_failed_attempts = 0
    db.session.commit()
    log_audit(
        "account_unlocked_by_admin",
        actor=get_current_user(),
        target_type="user",
        target_id=str(user.id),
        details={"username": user.username},
    )
    _notify_security_event(
        user,
        "Your KubeSight account has been unlocked",
        "Your KubeSight account has been unlocked",
        ["An administrator unlocked your account. You can sign in again."],
    )
    return {"id": user.id, "username": user.username, "isLocked": False}, None, 200


def reset_failed_attempts(user_id: int) -> Tuple[Optional[Dict[str, Any]], Optional[str], int]:
    """Admin action: clear failed password/MFA counters without lifting an admin lock."""
    user = User.query.get(user_id)
    if not user:
        return None, "User not found", 404

    user.failed_login_attempts = 0
    user.mfa_failed_attempts = 0
    # Lift a purely time-based lock, but leave an admin-required lock in place.
    if not user.requires_admin_unlock:
        user.locked_until = None
        user.lock_reason = None
    db.session.commit()
    log_audit(
        "account_unlocked_by_admin",
        actor=get_current_user(),
        target_type="user",
        target_id=str(user.id),
        details={"username": user.username, "action": "reset_failed_attempts"},
    )
    return {"id": user.id, "username": user.username}, None, 200


def force_password_reset(user_id: int) -> Tuple[Optional[Dict[str, Any]], Optional[str], int]:
    """Admin action: invalidate the current password and issue a new temporary one.

    Unlike a first-time create, an already-MFA-enrolled user keeps their MFA: the
    next sign-in requires the new temporary password → new permanent password →
    an MFA challenge with their existing authenticator.
    """
    user = User.query.get(user_id)
    if not user:
        return None, "User not found", 404
    if not user.email or "@" not in user.email:
        return None, "User has no valid email address on file", 400

    had_mfa = bool(user.mfa_enabled)
    temp_password = _provision_temporary_password(user)
    # Preserve an existing MFA enrolment; force re-verification, not re-enrolment.
    if had_mfa:
        user.mfa_enabled = True
    db.session.commit()

    emailed = _deliver_temporary_password(user, temp_password)
    log_audit(
        "password_changed",
        actor=get_current_user(),
        target_type="user",
        target_id=str(user.id),
        details={"username": user.username, "context": "admin_force_reset", "emailed": emailed},
    )

    result = {
        "id": user.id,
        "username": user.username,
        "temporaryPasswordEmailed": emailed,
        "temporaryPasswordExpiresAt": (
            user.temporary_password_expires_at.isoformat()
            if user.temporary_password_expires_at
            else None
        ),
    }
    if not emailed:
        result["temporaryPassword"] = temp_password
    return result, None, 200


def lock_account(user_id: int) -> Tuple[Optional[Dict[str, Any]], Optional[str], int]:
    """Admin action: manually lock an account until an admin unlocks it."""
    from ..access import is_admin

    actor = get_current_user()
    if actor and actor.id == user_id:
        return None, "You cannot lock your own account", 400

    user = User.query.get(user_id)
    if not user:
        return None, "User not found", 404
    if is_admin(user) and active_admin_count() <= 1:
        return None, "Cannot lock the last active admin", 400

    user.requires_admin_unlock = True
    user.lock_reason = "admin"
    db.session.commit()
    log_audit(
        "account_admin_locked",
        actor=actor,
        target_type="user",
        target_id=str(user.id),
        details={"username": user.username, "action": "manual"},
    )
    _notify_security_event(
        user,
        "Your KubeSight account has been locked",
        "Your KubeSight account has been locked",
        [
            "An administrator locked your account.",
            "Please contact your administrator to regain access.",
        ],
    )
    return {"id": user.id, "username": user.username, "isLocked": True}, None, 200


def enable_user(user_id: int) -> Tuple[Optional[Dict[str, Any]], Optional[str], int]:
    """Admin action: re-enable a disabled account."""
    user = User.query.get(user_id)
    if not user:
        return None, "User not found", 404
    if not user.is_active:
        user.is_active = True
        db.session.commit()
        log_audit(
            "user_enabled",
            actor=get_current_user(),
            target_type="user",
            target_id=str(user.id),
            details={"username": user.username},
        )
    return {"id": user.id, "username": user.username, "isActive": True}, None, 200


def disable_user(user_id: int) -> Tuple[Optional[Dict[str, Any]], Optional[str], int]:
    from ..access import is_admin

    user = User.query.get(user_id)
    if not user:
        return None, "User not found", 404
    if not user.is_active:
        return {"id": user.id, "isActive": False}, None, 200

    if is_admin(user) and active_admin_count() <= 1:
        return None, "Cannot disable the last active admin", 400

    user.is_active = False
    db.session.commit()
    log_audit(
        "user_disabled",
        actor=get_current_user(),
        target_type="user",
        target_id=str(user.id),
        details={"username": user.username},
    )
    return {"id": user.id, "isActive": False}, None, 200


def delete_user(user_id: int) -> Tuple[Optional[Dict[str, Any]], Optional[str], int]:
    """Permanently remove a user.

    Cascades the user's own access entries/rules, deletes their API tokens, and
    nulls out references that merely attribute past activity (audit logs, created
    catalog entries, deployment requests, alert receivers) so history is kept.
    """
    from ..access import is_admin
    from ..models import (
        AlertPolicy,
        AlertRoutingReceiver,
        ApiToken,
        AppCatalogEntry,
        ApplicationDeploymentVersion,
        AuditLog,
        DeploymentRequest,
        UserTemplate,
    )

    user = User.query.get(user_id)
    if not user:
        return None, "User not found", 404
    if is_admin(user) and active_admin_count() <= 1:
        return None, "Cannot delete the last active admin", 400

    username = user.username

    # Detach references that only record who did something (preserve the history).
    AuditLog.query.filter_by(actor_user_id=user_id).update(
        {"actor_user_id": None}, synchronize_session=False
    )
    AppCatalogEntry.query.filter_by(created_by_user_id=user_id).update(
        {"created_by_user_id": None}, synchronize_session=False
    )
    ApplicationDeploymentVersion.query.filter_by(created_by_user_id=user_id).update(
        {"created_by_user_id": None}, synchronize_session=False
    )
    AlertPolicy.query.filter_by(created_by_user_id=user_id).update(
        {"created_by_user_id": None}, synchronize_session=False
    )
    UserTemplate.query.filter_by(created_by=user_id).update(
        {"created_by": None}, synchronize_session=False
    )
    DeploymentRequest.query.filter_by(requester_id=user_id).update(
        {"requester_id": None}, synchronize_session=False
    )
    DeploymentRequest.query.filter_by(decided_by_user_id=user_id).update(
        {"decided_by_user_id": None}, synchronize_session=False
    )
    # Alert receivers linked to this user no longer resolve to anyone.
    AlertRoutingReceiver.query.filter_by(user_id=user_id).update(
        {"user_id": None}, synchronize_session=False
    )
    # API tokens have a non-null user_id — remove them outright.
    ApiToken.query.filter_by(user_id=user_id).delete(synchronize_session=False)

    # User-owned access entries/rules cascade via the relationships.
    db.session.delete(user)
    db.session.commit()

    log_audit(
        "user_deleted",
        actor=get_current_user(),
        target_type="user",
        target_id=str(user_id),
        details={"username": username},
    )
    return {"id": user_id, "deleted": True}, None, 200
