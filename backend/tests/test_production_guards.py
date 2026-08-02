from __future__ import annotations

import pytest
from flask import Flask

from api.production_guards import (
    ProductionGuardError,
    _default_seeded_usernames,
    production_environment_enabled,
    run_startup_guards,
)
from api.secret_encryption import (
    _fernet,
    decrypt_secret,
    encrypt_secret,
    rotate_database_secrets,
    rotate_encrypted_secret,
    secret_encryption_key_configured,
    secret_needs_rotation,
)


SAFE_ENVIRONMENT = {
    "KUBESIGHT_ENV": "production",
    "JWT_SECRET_KEY": "jwt-signing-key-that-is-at-least-32-characters",
    "FLASK_DEBUG": "false",
    "AUTH_REQUIRED": "true",
    "ALERT_ROUTING_SECRET_KEY": "credential-key-that-is-at-least-32-characters",
    "CORS_ORIGINS": "https://kubesight.example.com",
    "K8S_REAL_MODE": "true",
    "OIDC_ENABLED": "false",
}


@pytest.fixture()
def production_app(monkeypatch):
    for name, value in SAFE_ENVIRONMENT.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setattr("api.production_guards.is_at_head", lambda: True)
    monkeypatch.setattr(
        "api.production_guards._default_seeded_usernames", lambda: []
    )
    app = Flask(__name__)
    app.config["DEBUG"] = False
    app.config["JWT_SECRET_KEY"] = SAFE_ENVIRONMENT["JWT_SECRET_KEY"]
    return app


def test_non_production_environment_is_not_guarded(monkeypatch):
    monkeypatch.delenv("KUBESIGHT_ENV", raising=False)
    run_startup_guards(Flask(__name__))


@pytest.mark.parametrize("legacy_setting", ["FLASK_ENV", "APP_ENV"])
def test_legacy_environment_signals_do_not_enable_production_guards(
    monkeypatch, legacy_setting
):
    monkeypatch.delenv("KUBESIGHT_ENV", raising=False)
    monkeypatch.setenv("FLASK_DEBUG", "false")
    monkeypatch.setenv(legacy_setting, "production")

    assert production_environment_enabled() is False


def test_kubesight_environment_is_the_authoritative_production_signal(
    monkeypatch,
):
    monkeypatch.setenv("KUBESIGHT_ENV", "  ProDucTion  ")
    monkeypatch.setenv("FLASK_ENV", "development")
    monkeypatch.setenv("APP_ENV", "development")

    assert production_environment_enabled() is True


def test_safe_production_configuration_passes(production_app):
    run_startup_guards(production_app)


def test_app_factory_runs_guards_before_blueprint_registration(monkeypatch):
    import api as api_package

    for name, value in SAFE_ENVIRONMENT.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setattr("api.production_guards.is_at_head", lambda: True)
    monkeypatch.setattr(
        "api.production_guards._default_seeded_usernames", lambda: []
    )

    def blueprints_must_not_be_registered(_app):
        raise AssertionError("blueprints registered before production guards")

    monkeypatch.setattr(
        api_package, "register_blueprints", blueprints_must_not_be_registered
    )

    class UnsafeProductionConfig:
        TESTING = True
        SQLALCHEMY_DATABASE_URI = "sqlite:///:memory:"
        SQLALCHEMY_TRACK_MODIFICATIONS = False
        JWT_SECRET_KEY = "change-me-generate-with-openssl-rand-hex-32"

    with pytest.raises(ProductionGuardError, match="JWT_SECRET_KEY"):
        api_package.create_app(UnsafeProductionConfig)


@pytest.mark.parametrize(
    ("setting", "unsafe_value"),
    [
        ("FLASK_DEBUG", "true"),
        ("AUTH_REQUIRED", "false"),
        ("ALERT_ROUTING_SECRET_KEY", ""),
        ("ALERT_ROUTING_SECRET_KEY", "change-me-credential-encryption-key"),
        ("CORS_ORIGINS", "*"),
        ("CORS_ORIGINS", "https://*.example.com"),
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


def test_default_jwt_secret_config_is_rejected(production_app, monkeypatch):
    monkeypatch.setenv("JWT_SECRET_KEY", SAFE_ENVIRONMENT["JWT_SECRET_KEY"])
    production_app.config["JWT_SECRET_KEY"] = (
        "change-me-generate-with-openssl-rand-hex-32"
    )

    with pytest.raises(ProductionGuardError, match="JWT_SECRET_KEY"):
        run_startup_guards(production_app)


def test_missing_jwt_secret_is_rejected(production_app, monkeypatch):
    monkeypatch.delenv("JWT_SECRET_KEY", raising=False)
    production_app.config.pop("JWT_SECRET_KEY", None)

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


def test_enabled_oidc_must_be_complete_and_secure(production_app, monkeypatch):
    monkeypatch.setenv("OIDC_ENABLED", "true")
    monkeypatch.delenv("OIDC_ISSUER_URL", raising=False)

    with pytest.raises(
        ProductionGuardError, match="OIDC_CONFIGURATION.*OIDC_ISSUER_URL"
    ):
        run_startup_guards(production_app)


def test_oidc_client_secret_must_be_independent(production_app, monkeypatch):
    values = {
        "OIDC_ENABLED": "true",
        "OIDC_ISSUER_URL": "https://idp.example.test/tenant",
        "OIDC_CLIENT_ID": "kubesight",
        "OIDC_CLIENT_SECRET": SAFE_ENVIRONMENT["JWT_SECRET_KEY"],
        "OIDC_REDIRECT_URI": "https://kubesight.example.com/api/auth/oidc/callback",
        "OIDC_ALLOWED_DOMAINS": "example.test",
        "OIDC_DEFAULT_ROLE": "viewer",
    }
    for name, value in values.items():
        monkeypatch.setenv(name, value)

    with pytest.raises(ProductionGuardError, match="OIDC_CLIENT_SECRET"):
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


def test_default_seeded_credentials_are_rejected(production_app, monkeypatch):
    monkeypatch.setattr(
        "api.production_guards._default_seeded_usernames",
        lambda: ["admin", "viewer"],
    )

    with pytest.raises(ProductionGuardError) as exc_info:
        run_startup_guards(production_app)

    message = str(exc_info.value)
    assert "DEFAULT_SEEDED_USERS" in message
    assert "admin, viewer" in message


def test_default_seeded_credential_check_fails_closed(
    production_app, monkeypatch
):
    def unavailable():
        raise RuntimeError("user query details must not escape")

    monkeypatch.setattr(
        "api.production_guards._default_seeded_usernames", unavailable
    )

    with pytest.raises(ProductionGuardError) as exc_info:
        run_startup_guards(production_app)

    message = str(exc_info.value)
    assert "DEFAULT_SEEDED_USERS" in message
    assert "user query details must not escape" not in message


def test_shipped_seeded_credentials_are_detected(app):
    assert set(_default_seeded_usernames()) == {
        "admin",
        "viewer",
        "operator",
        "hermes-agent",
    }


def test_all_violations_are_reported_together(production_app, monkeypatch):
    production_app.config["JWT_SECRET_KEY"] = "change-me"
    monkeypatch.setenv("AUTH_REQUIRED", "off")
    monkeypatch.setenv("CORS_ORIGINS", "*")

    with pytest.raises(ProductionGuardError) as exc_info:
        run_startup_guards(production_app)

    message = str(exc_info.value)
    assert "JWT_SECRET_KEY" in message
    assert "AUTH_REQUIRED" in message
    assert "CORS_ORIGINS" in message


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


def test_credential_key_rotation_reads_previous_and_rewrites_primary(monkeypatch):
    old_key = "old-credential-key-that-is-at-least-32-characters"
    new_key = "new-credential-key-that-is-at-least-32-characters"
    monkeypatch.setenv("ALERT_ROUTING_SECRET_KEY", old_key)
    old_cipher = encrypt_secret("operator-secret")

    monkeypatch.setenv("ALERT_ROUTING_SECRET_KEY", new_key)
    monkeypatch.setenv("ALERT_ROUTING_SECRET_KEY_PREVIOUS", old_key)

    assert decrypt_secret(old_cipher) == "operator-secret"
    assert secret_needs_rotation(old_cipher) is True
    rotated = rotate_encrypted_secret(old_cipher)
    assert rotated != old_cipher
    assert secret_needs_rotation(rotated) is False

    monkeypatch.delenv("ALERT_ROUTING_SECRET_KEY_PREVIOUS")
    assert decrypt_secret(old_cipher) == ""
    assert decrypt_secret(rotated) == "operator-secret"


def test_legacy_untagged_ciphertext_can_be_rotated(monkeypatch):
    key = "credential-key-that-is-at-least-32-characters"
    monkeypatch.setenv("ALERT_ROUTING_SECRET_KEY", key)
    legacy = _fernet(key).encrypt(b"legacy-secret").decode("ascii")

    assert decrypt_secret(legacy) == "legacy-secret"
    assert secret_needs_rotation(legacy) is True
    rotated = rotate_encrypted_secret(legacy)
    assert rotated.startswith("ks1:")
    assert decrypt_secret(rotated) == "legacy-secret"


def test_rotation_refuses_ciphertext_outside_the_configured_keyring(monkeypatch):
    monkeypatch.setenv(
        "ALERT_ROUTING_SECRET_KEY", "first-key-that-is-at-least-32-characters"
    )
    cipher = encrypt_secret("cannot-lose-this")
    monkeypatch.setenv(
        "ALERT_ROUTING_SECRET_KEY", "second-key-that-is-at-least-32-characters"
    )
    monkeypatch.delenv("ALERT_ROUTING_SECRET_KEY_PREVIOUS", raising=False)

    assert decrypt_secret(cipher) == ""
    with pytest.raises(ValueError, match="configured keyring"):
        rotate_encrypted_secret(cipher)


def test_database_secret_rotation_is_atomic_and_supports_dry_run(
    app, monkeypatch
):
    from api.db import db
    from api.models import AuditLog
    from api.models_cluster_build import SshCredential

    old_key = "old-database-key-that-is-at-least-32-characters"
    new_key = "new-database-key-that-is-at-least-32-characters"
    monkeypatch.setenv("ALERT_ROUTING_SECRET_KEY", old_key)
    row = SshCredential(
        name="rotation-test",
        username="operator",
        auth_method="password",
        secret_cipher=encrypt_secret("stored-password"),
    )
    db.session.add(row)
    db.session.commit()
    original = row.secret_cipher

    monkeypatch.setenv("ALERT_ROUTING_SECRET_KEY", new_key)
    monkeypatch.setenv("ALERT_ROUTING_SECRET_KEY_PREVIOUS", old_key)
    preview = rotate_database_secrets(dry_run=True)
    db.session.refresh(row)
    assert preview["rotated"] == 1
    assert preview["tables"] == {"ssh_credentials": 1}
    assert row.secret_cipher == original

    result = rotate_database_secrets()
    db.session.refresh(row)
    assert result["rotated"] == 1
    assert row.secret_cipher != original
    assert decrypt_secret(row.secret_cipher) == "stored-password"
    audit = AuditLog.query.filter_by(action="credential_secrets_rotated").one()
    assert audit.details == {
        "ip": None,
        "scanned": 1,
        "rotated": 1,
        "tables": {"ssh_credentials": 1},
    }
    assert original not in str(audit.details)


def test_database_secret_rotation_rolls_back_if_any_value_is_unreadable(
    app, monkeypatch
):
    from api.db import db
    from api.models import AuditLog
    from api.models_cluster_build import SshCredential

    old_key = "old-rollback-key-that-is-at-least-32-characters"
    new_key = "new-rollback-key-that-is-at-least-32-characters"
    monkeypatch.setenv("ALERT_ROUTING_SECRET_KEY", old_key)
    readable = SshCredential(
        name="readable",
        username="operator",
        auth_method="password",
        secret_cipher=encrypt_secret("preserve-me"),
    )
    unreadable = SshCredential(
        name="unreadable",
        username="operator",
        auth_method="password",
        secret_cipher="ks1:0000000000000000:not-a-token",
    )
    db.session.add_all([readable, unreadable])
    db.session.commit()
    original = readable.secret_cipher

    monkeypatch.setenv("ALERT_ROUTING_SECRET_KEY", new_key)
    monkeypatch.setenv("ALERT_ROUTING_SECRET_KEY_PREVIOUS", old_key)
    with pytest.raises(ValueError, match="configured keyring"):
        rotate_database_secrets()

    db.session.refresh(readable)
    assert readable.secret_cipher == original
    audit = AuditLog.query.filter_by(
        action="credential_secret_rotation_failed"
    ).one()
    assert audit.details == {"ip": None, "errorType": "ValueError"}
    assert original not in str(audit.details)
