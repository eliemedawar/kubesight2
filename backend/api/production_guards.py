"""Fail-closed validation for production startup configuration."""

from __future__ import annotations

import os
from typing import TYPE_CHECKING

from .auth_utils import auth_required_enabled
from .migrations import is_at_head
from .secret_encryption import secret_encryption_key_configured

if TYPE_CHECKING:
    from flask import Flask


_TRUE_VALUES = frozenset({"1", "true", "yes", "on"})
_FALSE_VALUES = frozenset({"0", "false", "no", "off"})
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


def default_user_seeding_enabled() -> bool:
    """Whether the built-in demo accounts may be created at startup.

    This remains enabled by default for development compatibility. Production
    startup requires the operator to disable it explicitly.
    """
    raw = _environment_value("KUBESIGHT_SEED_DEFAULT_USERS")
    return raw.lower() not in _FALSE_VALUES


def _cors_is_unsafe() -> bool:
    raw = _environment_value("CORS_ORIGINS")
    if not raw:
        return True
    origins = {item.strip().lower() for item in raw.split(",") if item.strip()}
    return not origins or "*" in origins or "null" in origins


def _collect_violations(app: Flask) -> list[str]:
    violations: list[str] = []

    jwt_secret = _environment_value("JWT_SECRET_KEY")
    if _is_insecure_secret(jwt_secret):
        violations.append(
            "JWT_SECRET_KEY must be explicitly set to a non-placeholder value "
            f"of at least {_MINIMUM_SECRET_LENGTH} characters"
        )

    if bool(app.debug) or _is_true(_environment_value("FLASK_DEBUG")):
        violations.append("DEBUG/FLASK_DEBUG must be disabled")

    if not auth_required_enabled():
        violations.append("AUTH_REQUIRED must be enabled")

    if default_user_seeding_enabled():
        violations.append("KUBESIGHT_SEED_DEFAULT_USERS must be set to false")

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

    try:
        with app.app_context():
            migrations_at_head = is_at_head()
    except Exception:
        migrations_at_head = False
    if not migrations_at_head:
        violations.append("DATABASE_MIGRATIONS must be at the Alembic head revision")

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
