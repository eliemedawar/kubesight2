from __future__ import annotations

import pytest
from flask import Flask

from api.production_guards import (
    ProductionGuardError,
    default_user_seeding_enabled,
    run_startup_guards,
)
from api.secret_encryption import (
    decrypt_secret,
    encrypt_secret,
    secret_encryption_key_configured,
)


SAFE_ENVIRONMENT = {
    "KUBESIGHT_ENV": "production",
    "JWT_SECRET_KEY": "jwt-signing-key-that-is-at-least-32-characters",
    "FLASK_DEBUG": "false",
    "AUTH_REQUIRED": "true",
    "KUBESIGHT_SEED_DEFAULT_USERS": "false",
    "ALERT_ROUTING_SECRET_KEY": "credential-key-that-is-at-least-32-characters",
    "CORS_ORIGINS": "https://kubesight.example.com",
    "K8S_REAL_MODE": "true",
}


@pytest.fixture()
def production_app(monkeypatch):
    for name, value in SAFE_ENVIRONMENT.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setattr("api.production_guards.is_at_head", lambda: True)
    app = Flask(__name__)
    app.config["DEBUG"] = False
    return app


def test_non_production_environment_is_not_guarded(monkeypatch):
    monkeypatch.delenv("KUBESIGHT_ENV", raising=False)
    run_startup_guards(Flask(__name__))


def test_safe_production_configuration_passes(production_app):
    run_startup_guards(production_app)


@pytest.mark.parametrize(
    ("setting", "unsafe_value"),
    [
        ("JWT_SECRET_KEY", "change-me-generate-with-openssl-rand-hex-32"),
        ("FLASK_DEBUG", "true"),
        ("AUTH_REQUIRED", "false"),
        ("KUBESIGHT_SEED_DEFAULT_USERS", "true"),
        ("ALERT_ROUTING_SECRET_KEY", ""),
        ("ALERT_ROUTING_SECRET_KEY", "change-me-credential-encryption-key"),
        ("CORS_ORIGINS", "*"),
        ("CORS_ORIGINS", "null"),
        ("K8S_REAL_MODE", "false"),
        ("K8S_REAL_MODE", "auto"),
    ],
)
def test_each_unsafe_setting_names_itself(
    production_app, monkeypatch, setting, unsafe_value
):
    monkeypatch.setenv(setting, unsafe_value)

    with pytest.raises(ProductionGuardError, match=setting):
        run_startup_guards(production_app)


def test_missing_jwt_secret_is_rejected(production_app, monkeypatch):
    monkeypatch.delenv("JWT_SECRET_KEY")

    with pytest.raises(ProductionGuardError, match="JWT_SECRET_KEY"):
        run_startup_guards(production_app)


def test_debug_app_is_rejected(production_app):
    production_app.config["DEBUG"] = True

    with pytest.raises(ProductionGuardError, match="DEBUG/FLASK_DEBUG"):
        run_startup_guards(production_app)


def test_signing_and_encryption_keys_must_be_different(
    production_app, monkeypatch
):
    monkeypatch.setenv(
        "ALERT_ROUTING_SECRET_KEY", SAFE_ENVIRONMENT["JWT_SECRET_KEY"]
    )

    with pytest.raises(
        ProductionGuardError,
        match="ALERT_ROUTING_SECRET_KEY must be different from JWT_SECRET_KEY",
    ):
        run_startup_guards(production_app)


def test_database_migrations_must_be_at_head(production_app, monkeypatch):
    monkeypatch.setattr("api.production_guards.is_at_head", lambda: False)

    with pytest.raises(ProductionGuardError, match="DATABASE_MIGRATIONS"):
        run_startup_guards(production_app)


def test_database_migration_check_fails_closed(production_app, monkeypatch):
    def unavailable():
        raise RuntimeError("database details must not escape")

    monkeypatch.setattr("api.production_guards.is_at_head", unavailable)

    with pytest.raises(ProductionGuardError) as exc_info:
        run_startup_guards(production_app)

    message = str(exc_info.value)
    assert "DATABASE_MIGRATIONS" in message
    assert "database details must not escape" not in message


def test_all_violations_are_reported_together(production_app, monkeypatch):
    monkeypatch.setenv("JWT_SECRET_KEY", "change-me")
    monkeypatch.setenv("AUTH_REQUIRED", "off")
    monkeypatch.setenv("CORS_ORIGINS", "*")

    with pytest.raises(ProductionGuardError) as exc_info:
        run_startup_guards(production_app)

    message = str(exc_info.value)
    assert "JWT_SECRET_KEY" in message
    assert "AUTH_REQUIRED" in message
    assert "CORS_ORIGINS" in message


@pytest.mark.parametrize("value", ["false", "0", "no", "off", "FALSE"])
def test_default_user_seeding_can_be_disabled(monkeypatch, value):
    monkeypatch.setenv("KUBESIGHT_SEED_DEFAULT_USERS", value)
    assert default_user_seeding_enabled() is False


def test_default_user_seeding_is_fail_safe(monkeypatch):
    monkeypatch.delenv("KUBESIGHT_SEED_DEFAULT_USERS", raising=False)
    assert default_user_seeding_enabled() is True


def test_credential_encryption_does_not_fall_back_to_jwt(monkeypatch):
    monkeypatch.delenv("ALERT_ROUTING_SECRET_KEY", raising=False)
    monkeypatch.setenv("JWT_SECRET_KEY", "jwt-only-key-that-is-at-least-32-characters")
    assert secret_encryption_key_configured() is False


def test_credential_encryption_uses_its_dedicated_key(monkeypatch):
    monkeypatch.setenv(
        "ALERT_ROUTING_SECRET_KEY", "credential-key-that-is-at-least-32-characters"
    )
    cipher = encrypt_secret("operator-secret")
    assert cipher != "operator-secret"
    assert decrypt_secret(cipher) == "operator-secret"
