"""Fail-closed validation for production startup configuration."""

from __future__ import annotations

import os
from typing import TYPE_CHECKING

from .auth_utils import auth_required_enabled
from .migrations import is_at_head
from .models import User
from .passwords import verify_password
from .rbac_data import DEFAULT_USERS
from .secret_encryption import secret_encryption_key_configured

if TYPE_CHECKING:
    from flask import Flask


_TRUE_VALUES = frozenset({"1", "true", "yes", "on"})
_INSECURE_SECRET_VALUES = frozenset(
    {
        "change-me",
        "changeme",
        "kubesight",
        "kubesight-dev-secret-change-me",
        "secret",
    }
)
_MINIMUM_SECRET_LENGTH = 32


class ProductionGuardError(RuntimeError):
    """Raised when KubeSight is configured unsafely for production."""


def _environment_value(name: str) -> str:
    return os.getenv(name, "").strip()


def _is_true(value: str) -> bool:
    return value.strip().lower() in _TRUE_VALUES


def _is_insecure_secret(value: str) -> bool:
    normalized = value.strip().lower()
    return (
        not normalized
        or len(value) < _MINIMUM_SECRET_LENGTH
        or normalized in _INSECURE_SECRET_VALUES
        or normalized.startswith("change-me")
        or "do-not-use-in-production" in normalized
    )


def production_environment_enabled() -> bool:
    """Whether strict startup validation applies to this process."""
    return _environment_value("KUBESIGHT_ENV").lower() == "production"


def _cors_is_unsafe() -> bool:
    raw = _environment_value("CORS_ORIGINS")
    if not raw:
        return True
    origins = {item.strip().lower() for item in raw.split(",") if item.strip()}
    return not origins or any(
        origin == "null" or "*" in origin for origin in origins
    )


def _default_seeded_usernames() -> list[str]:
    """Return built-in accounts that still accept their shipped password."""
    unsafe: list[str] = []
    for spec in DEFAULT_USERS:
        user = User.query.filter_by(username=spec["username"]).first()
        if user and verify_password(spec["password"], user.password_hash):
            unsafe.append(spec["username"])
    return unsafe


def _collect_violations(app: Flask) -> list[str]:
    violations: list[str] = []

    jwt_secret = str(app.config.get("JWT_SECRET_KEY") or "").strip()
    if _is_insecure_secret(jwt_secret):
        violations.append(
            "JWT_SECRET_KEY must be explicitly set to a non-placeholder value "
            f"of at least {_MINIMUM_SECRET_LENGTH} characters"
        )

    if bool(app.debug) or _is_true(_environment_value("FLASK_DEBUG")):
        violations.append("DEBUG/FLASK_DEBUG must be disabled")

    if not auth_required_enabled():
        violations.append("AUTH_REQUIRED must be enabled")

    encryption_key = _environment_value("ALERT_ROUTING_SECRET_KEY")
    if (
        not secret_encryption_key_configured()
        or _is_insecure_secret(encryption_key)
    ):
        violations.append(
            "ALERT_ROUTING_SECRET_KEY must be explicitly set to a non-default "
            "credential encryption key"
        )
    elif jwt_secret and encryption_key == jwt_secret:
        violations.append(
            "ALERT_ROUTING_SECRET_KEY must be different from JWT_SECRET_KEY"
        )

    if _cors_is_unsafe():
        violations.append(
            "CORS_ORIGINS must contain explicit trusted origins, not '*' or 'null'"
        )

    if not _is_true(_environment_value("K8S_REAL_MODE")):
        violations.append(
            "K8S_REAL_MODE must be explicitly enabled so demo-mode fallback is off"
        )

    migrations_at_head = False
    default_seeded_users: list[str] | None = None
    try:
        with app.app_context():
            migrations_at_head = is_at_head()
            if migrations_at_head:
                default_seeded_users = _default_seeded_usernames()
    except Exception:
        if migrations_at_head:
            default_seeded_users = None
    if not migrations_at_head:
        violations.append("DATABASE_MIGRATIONS must be at the Alembic head revision")
    elif default_seeded_users is None:
        violations.append("DEFAULT_SEEDED_USERS could not be verified safely")
    elif default_seeded_users:
        usernames = ", ".join(default_seeded_users)
        violations.append(
            "DEFAULT_SEEDED_USERS must not retain shipped passwords "
            f"(found: {usernames})"
        )

    return violations


def run_startup_guards(app: Flask) -> None:
    """Refuse unsafe production startup with every offending setting named."""
    if not production_environment_enabled():
        return

    violations = _collect_violations(app)
    if violations:
        details = "; ".join(violations)
        raise ProductionGuardError(
            f"KubeSight production startup refused: {details}."
        )
