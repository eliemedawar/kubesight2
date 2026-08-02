"""Security-contract tests for the schema-independent OIDC core."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from urllib.error import HTTPError
from urllib.parse import parse_qs, urlparse

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa

from api.mfa_recovery import (
    consume_admin_recovery_grant,
    consume_recovery_code,
    mint_admin_recovery_grant,
    regenerate_recovery_codes,
)
from api.oidc_auth import complete_oidc_login, start_oidc_login
from api.oidc import (
    OidcConfig,
    OidcConfigurationError,
    OidcDiscovery,
    OidcProtocolError,
    begin_authorization,
    exchange_code,
    fetch_discovery,
    hash_transaction_secret,
    oidc_enabled,
    principal_from_claims,
    safe_return_to,
    validate_id_token,
)


def test_oidc_and_recovery_records_are_hash_only_and_one_time(app):
    from api.db import db
    from api.models import User
    from api.models_auth import (
        AdminRecoveryGrant,
        MfaRecoveryCode,
        OidcAuthorizationRequest,
        OidcIdentity,
    )

    now = datetime.now(timezone.utc)
    user = User.query.filter_by(username="admin").one()
    identity = OidcIdentity(
        issuer="https://idp.example.test/tenant",
        subject="provider-subject",
        user_id=user.id,
        email="admin@example.test",
    )
    request = OidcAuthorizationRequest(
        state_hash="a" * 64,
        nonce_hash="b" * 64,
        browser_binding_hash="c" * 64,
        code_verifier_cipher="ks1:key-id:encrypted-verifier",
        issuer="https://idp.example.test/tenant",
        redirect_uri="https://kubesight.example.test/api/auth/oidc/callback",
        return_to="/clusters",
        expires_at=now + timedelta(minutes=5),
    )
    recovery = MfaRecoveryCode(user_id=user.id, code_hash="d" * 64)
    grant = AdminRecoveryGrant(
        user_id=user.id,
        token_hash="e" * 64,
        expires_at=now + timedelta(minutes=10),
    )
    db.session.add_all([identity, request, recovery, grant])
    db.session.commit()

    assert request.is_active(now) is True
    assert grant.is_active(now) is True
    assert not hasattr(recovery, "code")
    assert not hasattr(grant, "token")
    request.consumed_at = now
    grant.used_at = now
    assert request.is_active(now) is False
    assert grant.is_active(now) is False


def test_mfa_recovery_codes_are_hash_only_rotating_and_single_use(app):
    from api.models import AuditLog, User
    from api.models_auth import MfaRecoveryCode

    user = User.query.filter_by(username="admin").one()
    first_set = regenerate_recovery_codes(user, count=3)
    assert len(first_set) == 3
    assert all(len(code) == 19 and code.count("-") == 3 for code in first_set)
    stored_hashes = {row.code_hash for row in MfaRecoveryCode.query.all()}
    assert all(code.replace("-", "") not in stored_hashes for code in first_set)

    second_set = regenerate_recovery_codes(user, count=2)
    assert consume_recovery_code(user, first_set[0]) is False
    assert consume_recovery_code(user, f"{second_set[0]}!") is False
    assert consume_recovery_code(user, second_set[0].lower()) is True
    assert consume_recovery_code(user, second_set[0]) is False

    serialized_audit = json.dumps(
        [entry.details for entry in AuditLog.query.order_by(AuditLog.id).all()]
    )
    for code in first_set + second_set:
        assert code not in serialized_audit
        assert code.replace("-", "") not in serialized_audit


def test_admin_break_glass_is_short_lived_hash_only_and_revokes_sessions(
    app, client
):
    from api.db import db
    from api.models import AuditLog, User
    from api.models_auth import AdminRecoveryGrant, AuthSession

    login = client.post(
        "/api/auth/login", json={"username": "admin", "password": "admin123"}
    )
    assert login.status_code == 200
    user = User.query.filter_by(username="admin").one()
    user.mfa_enabled = True
    user.totp_secret = "JBSWY3DPEHPK3PXP"
    user.requires_admin_unlock = True
    user.lock_reason = "mfa"
    db.session.commit()

    _user, raw_token, expires_at = mint_admin_recovery_grant(
        "admin", duration_minutes=5
    )
    stored = AdminRecoveryGrant.query.one()
    assert stored.token_hash != raw_token
    assert stored.expires_at.replace(tzinfo=timezone.utc) == expires_at

    recovered = consume_admin_recovery_grant("admin", raw_token)
    assert recovered is not None
    assert recovered.mfa_enabled is False
    assert recovered.totp_secret is None
    assert recovered.first_login_completed is False
    assert recovered.requires_admin_unlock is False
    assert AuthSession.query.one().revoked_at is not None
    assert consume_admin_recovery_grant("admin", raw_token) is None

    serialized_audit = json.dumps(
        [entry.details for entry in AuditLog.query.order_by(AuditLog.id).all()]
    )
    assert raw_token not in serialized_audit


def test_admin_break_glass_rejects_non_admin_and_expired_grants(app):
    from api.db import db
    from api.models import User
    from api.models_auth import AdminRecoveryGrant

    with pytest.raises(ValueError, match="administrator"):
        mint_admin_recovery_grant("viewer")

    user, raw_token, _expires = mint_admin_recovery_grant("admin")
    grant = AdminRecoveryGrant.query.one()
    grant.expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
    db.session.commit()

    assert consume_admin_recovery_grant(user.username, raw_token) is None
    assert user.first_login_completed is True


def _environment(**overrides) -> dict[str, str]:
    values = {
        "OIDC_ENABLED": "true",
        "KUBESIGHT_ENV": "production",
        "OIDC_ISSUER_URL": "https://idp.example.test/tenant",
        "OIDC_CLIENT_ID": "kubesight-client",
        "OIDC_CLIENT_SECRET": "provider-secret-must-never-leak",
        "OIDC_REDIRECT_URI": "https://kubesight.example.test/api/auth/oidc/callback",
        "OIDC_ALLOWED_DOMAINS": "example.test, xn--bcher-kva.example",
        "OIDC_GROUP_ROLE_MAPPINGS": json.dumps(
            {"platform-admins": "admin", "platform-viewers": "viewer"}
        ),
    }
    values.update(overrides)
    return values


def _config(**overrides) -> OidcConfig:
    return OidcConfig.from_environment(_environment(**overrides))


def _set_oidc_environment(monkeypatch, **overrides):
    for name, value in _environment(**overrides).items():
        monkeypatch.setenv(name, value)
    monkeypatch.setenv(
        "ALERT_ROUTING_SECRET_KEY",
        "oidc-transaction-key-that-is-at-least-32-characters",
    )


def _discovery(config: OidcConfig | None = None) -> OidcDiscovery:
    config = config or _config()
    return OidcDiscovery.from_document(
        config,
        {
            "issuer": config.issuer,
            "authorization_endpoint": "https://idp.example.test/authorize",
            "token_endpoint": "https://idp.example.test/token",
            "jwks_uri": "https://idp.example.test/jwks",
            "code_challenge_methods_supported": ["S256"],
        },
    )


class _JsonResponse:
    def __init__(self, document):
        self._raw = json.dumps(document).encode("utf-8")

    def read(self, _size):
        return self._raw

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False


def test_oidc_enablement_is_explicit():
    assert oidc_enabled({}) is False
    assert oidc_enabled({"OIDC_ENABLED": "true"}) is True


def test_config_requires_https_verified_domains_and_asymmetric_algorithms():
    with pytest.raises(OidcConfigurationError, match="HTTPS"):
        _config(OIDC_ISSUER_URL="http://idp.example.test")
    with pytest.raises(OidcConfigurationError, match="verified domain"):
        _config(OIDC_ALLOWED_DOMAINS="")
    with pytest.raises(OidcConfigurationError, match="asymmetric"):
        _config(OIDC_ALLOWED_ALGORITHMS="HS256")


def test_insecure_http_is_only_available_for_local_development():
    config = _config(
        KUBESIGHT_ENV="development",
        OIDC_ALLOW_INSECURE_HTTP="true",
        OIDC_ISSUER_URL="http://localhost:8080/tenant",
        OIDC_REDIRECT_URI="http://127.0.0.1:5000/api/auth/oidc/callback",
    )
    assert config.allow_insecure_localhost is True

    with pytest.raises(OidcConfigurationError, match="HTTPS"):
        _config(
            OIDC_ALLOW_INSECURE_HTTP="true",
            OIDC_ISSUER_URL="http://localhost:8080/tenant",
        )


def test_discovery_requires_exact_issuer_pkce_and_secure_endpoints():
    config = _config()
    base = {
        "issuer": config.issuer,
        "authorization_endpoint": "https://idp.example.test/authorize",
        "token_endpoint": "https://idp.example.test/token",
        "jwks_uri": "https://idp.example.test/jwks",
        "code_challenge_methods_supported": ["S256"],
    }
    with pytest.raises(OidcProtocolError, match="exactly match"):
        OidcDiscovery.from_document(config, {**base, "issuer": "https://evil.test"})
    with pytest.raises(OidcProtocolError, match="PKCE S256"):
        OidcDiscovery.from_document(
            config, {**base, "code_challenge_methods_supported": ["plain"]}
        )
    with pytest.raises(OidcProtocolError, match="HTTPS"):
        OidcDiscovery.from_document(
            config, {**base, "token_endpoint": "http://idp.example.test/token"}
        )


def test_discovery_fetch_uses_well_known_document_and_timeout():
    config = _config(OIDC_HTTP_TIMEOUT_SECONDS="7")
    captured = {}

    def opener(request, *, timeout):
        captured["url"] = request.full_url
        captured["timeout"] = timeout
        return _JsonResponse(
            {
                "issuer": config.issuer,
                "authorization_endpoint": "https://idp.example.test/authorize",
                "token_endpoint": "https://idp.example.test/token",
                "jwks_uri": "https://idp.example.test/jwks",
                "code_challenge_methods_supported": ["S256"],
            }
        )

    discovered = fetch_discovery(config, opener=opener)

    assert captured == {
        "url": "https://idp.example.test/tenant/.well-known/openid-configuration",
        "timeout": 7,
    }
    assert discovered.issuer == config.issuer


def test_issuer_trailing_slash_is_preserved_for_exact_validation():
    config = _config(OIDC_ISSUER_URL="https://idp.example.test/tenant/")
    captured = {}

    def opener(request, *, timeout):
        captured["url"] = request.full_url
        return _JsonResponse(
            {
                "issuer": config.issuer,
                "authorization_endpoint": "https://idp.example.test/authorize",
                "token_endpoint": "https://idp.example.test/token",
                "jwks_uri": "https://idp.example.test/jwks",
                "code_challenge_methods_supported": ["S256"],
            }
        )

    assert fetch_discovery(config, opener=opener).issuer.endswith("/")
    assert captured["url"] == (
        "https://idp.example.test/tenant/.well-known/openid-configuration"
    )


def test_authorization_uses_code_pkce_nonce_state_and_no_secret():
    config = _config()
    transaction = begin_authorization(
        config, _discovery(config), return_to="/clusters?scope=mine"
    )
    query = parse_qs(urlparse(transaction.authorization_url).query)

    assert query["response_type"] == ["code"]
    assert query["code_challenge_method"] == ["S256"]
    assert query["state"] == [transaction.state]
    assert query["nonce"] == [transaction.nonce]
    assert query["redirect_uri"] == [config.redirect_uri]
    assert 43 <= len(transaction.code_verifier) <= 128
    assert transaction.return_to == "/clusters?scope=mine"
    assert config.client_secret not in transaction.authorization_url


@pytest.mark.parametrize(
    "value",
    ["https://evil.test", "//evil.test", "/\\evil.test", "/ok\r\nLocation: bad"],
)
def test_return_to_rejects_open_redirect_shapes(value):
    with pytest.raises(OidcProtocolError, match="local absolute path"):
        safe_return_to(value)


def test_token_exchange_uses_basic_auth_and_pkce_without_leaking_secret():
    config = _config(
        OIDC_CLIENT_ID="kubesight:client",
        OIDC_CLIENT_SECRET="secret with spaces:and-colon",
    )
    captured = {}

    def opener(request, *, timeout):
        captured["request"] = request
        captured["timeout"] = timeout
        return _JsonResponse({"id_token": "signed-id-token", "access_token": "opaque"})

    tokens = exchange_code(
        config,
        _discovery(config),
        code="one-time-code",
        code_verifier="v" * 64,
        opener=opener,
    )

    request = captured["request"]
    form = parse_qs(request.data.decode("ascii"))
    assert request.get_header("Authorization") == (
        "Basic a3ViZXNpZ2h0JTNB" "Y2xpZW50OnNlY3JldCt3aXRoK3NwYWNlcyUzQWFuZC1jb2xvbg=="
    )
    assert form["code_verifier"] == ["v" * 64]
    assert form["redirect_uri"] == [config.redirect_uri]
    assert "client_secret" not in form
    assert tokens["id_token"] == "signed-id-token"


def test_token_exchange_error_is_sanitized():
    config = _config()

    def opener(request, *, timeout):
        raise HTTPError(request.full_url, 401, "bad secret echoed", {}, None)

    with pytest.raises(OidcProtocolError) as raised:
        exchange_code(
            config,
            _discovery(config),
            code="one-time-code",
            code_verifier="v" * 64,
            opener=opener,
        )
    assert config.client_secret not in str(raised.value)
    assert "bad secret echoed" not in str(raised.value)


def _signed_id_token(
    private_key,
    config: OidcConfig,
    *,
    nonce="expected-nonce",
    audience=None,
    **overrides,
):
    now = datetime.now(timezone.utc)
    claims = {
        "iss": config.issuer,
        "aud": audience or config.client_id,
        "sub": "provider-subject-123",
        "nonce": nonce,
        "iat": now,
        "exp": now + timedelta(minutes=5),
        "email": "operator@example.test",
        "email_verified": True,
        "groups": ["platform-admins"],
    }
    claims.update(overrides)
    return jwt.encode(claims, private_key, algorithm="RS256", headers={"kid": "key-1"})


def _jwk_factory(public_key):
    class Client:
        def __init__(self, uri, timeout):
            self.uri = uri
            self.timeout = timeout

        def get_signing_key_from_jwt(self, _token):
            return SimpleNamespace(key=public_key)

    return Client


def test_id_token_validation_checks_signature_issuer_audience_nonce_and_azp():
    config = _config()
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    factory = _jwk_factory(private_key.public_key())
    valid = _signed_id_token(private_key, config)

    claims = validate_id_token(
        config,
        _discovery(config),
        id_token=valid,
        expected_nonce="expected-nonce",
        jwk_client_factory=factory,
    )
    assert claims["sub"] == "provider-subject-123"

    hashed_nonce_claims = validate_id_token(
        config,
        _discovery(config),
        id_token=valid,
        expected_nonce_hash=hash_transaction_secret("expected-nonce"),
        jwk_client_factory=factory,
    )
    assert hashed_nonce_claims["sub"] == "provider-subject-123"

    with pytest.raises(OidcProtocolError, match="nonce"):
        validate_id_token(
            config,
            _discovery(config),
            id_token=valid,
            expected_nonce="wrong-nonce",
            jwk_client_factory=factory,
        )

    multiple_audiences = _signed_id_token(
        private_key,
        config,
        audience=[config.client_id, "another-client"],
    )
    with pytest.raises(OidcProtocolError, match="authorized-party"):
        validate_id_token(
            config,
            _discovery(config),
            id_token=multiple_audiences,
            expected_nonce="expected-nonce",
            jwk_client_factory=factory,
        )


def test_claim_policy_requires_verified_allowed_email_and_one_role():
    config = _config()
    principal = principal_from_claims(
        config,
        {
            "sub": "subject",
            "email": "Operator@Example.Test",
            "email_verified": True,
            "preferred_username": "Platform Operator",
            "name": "Platform Operator",
            "groups": ["platform-admins"],
        },
    )
    assert principal.email == "operator@example.test"
    assert principal.username == "Platform-Operator"
    assert principal.role_name == "admin"

    with pytest.raises(OidcProtocolError, match="not verified"):
        principal_from_claims(
            config,
            {
                "sub": "subject",
                "email": "operator@example.test",
                "email_verified": False,
                "groups": ["platform-admins"],
            },
        )
    with pytest.raises(OidcProtocolError, match="not allowed"):
        principal_from_claims(
            config,
            {
                "sub": "subject",
                "email": "operator@evil.test",
                "email_verified": True,
                "groups": ["platform-admins"],
            },
        )
    with pytest.raises(OidcProtocolError, match="multiple roles"):
        principal_from_claims(
            config,
            {
                "sub": "subject",
                "email": "operator@example.test",
                "email_verified": True,
                "groups": ["platform-admins", "platform-viewers"],
            },
        )


def test_oidc_start_persists_only_hashes_and_encrypted_pkce(
    app, monkeypatch
):
    from api.models import AuditLog
    from api.models_auth import OidcAuthorizationRequest
    from api.secret_encryption import decrypt_secret

    _set_oidc_environment(monkeypatch)
    monkeypatch.setattr(
        "api.oidc_auth.fetch_discovery", lambda config: _discovery(config)
    )

    started = start_oidc_login(return_to="/clusters")
    query = parse_qs(urlparse(started.authorization_url).query)
    row = OidcAuthorizationRequest.query.one()

    assert query["state"][0] not in {
        row.state_hash,
        row.nonce_hash,
        row.browser_binding_hash,
    }
    assert started.browser_binding not in {
        row.state_hash,
        row.nonce_hash,
        row.browser_binding_hash,
    }
    verifier = decrypt_secret(row.code_verifier_cipher)
    assert 43 <= len(verifier) <= 128
    assert verifier not in row.code_verifier_cipher
    audit = AuditLog.query.filter_by(action="oidc_login_started").one()
    assert audit.details == {
        "ip": None,
        "issuer": "https://idp.example.test/tenant",
    }


def test_oidc_callback_is_browser_bound_single_use_and_provisions_session(
    app, monkeypatch
):
    from api.models import AuditLog, User
    from api.models_auth import AuthSession, OidcAuthorizationRequest, OidcIdentity

    _set_oidc_environment(monkeypatch)
    monkeypatch.setattr(
        "api.oidc_auth.fetch_discovery", lambda config: _discovery(config)
    )
    started = start_oidc_login(return_to="/clusters")
    state = parse_qs(urlparse(started.authorization_url).query)["state"][0]
    row = OidcAuthorizationRequest.query.one()
    captured = {}

    def fake_exchange(config, discovery, *, code, code_verifier):
        captured["code"] = code
        captured["verifier"] = code_verifier
        return {"id_token": "validated-separately"}

    def fake_validate(
        config,
        discovery,
        *,
        id_token,
        expected_nonce_hash,
    ):
        assert expected_nonce_hash == row.nonce_hash
        return {
            "sub": "enterprise-subject",
            "email": "oidc-admin@example.test",
            "email_verified": True,
            "preferred_username": "oidc-admin",
            "name": "OIDC Admin",
            "groups": ["platform-admins"],
        }

    monkeypatch.setattr("api.oidc_auth.exchange_code", fake_exchange)
    monkeypatch.setattr("api.oidc_auth.validate_id_token", fake_validate)

    with pytest.raises(OidcProtocolError, match="invalid or expired"):
        complete_oidc_login(
            state=state,
            code="authorization-code",
            browser_binding="wrong-browser",
        )
    with app.test_request_context("/api/auth/oidc/callback"):
        completed = complete_oidc_login(
            state=state,
            code="authorization-code",
            browser_binding=started.browser_binding,
        )

    assert completed.return_to == "/clusters"
    assert completed.provisioned is True
    assert completed.user.username == "oidc-admin"
    assert captured["code"] == "authorization-code"
    assert 43 <= len(captured["verifier"]) <= 128
    assert OidcIdentity.query.one().user_id == completed.user.id
    assert AuthSession.query.filter_by(user_id=completed.user.id).count() == 1
    assert User.query.filter_by(username="oidc-admin").one().first_login_completed
    assert AuditLog.query.filter_by(action="oidc_login_succeeded").count() == 1

    with pytest.raises(OidcProtocolError, match="invalid or expired"):
        complete_oidc_login(
            state=state,
            code="replayed-code",
            browser_binding=started.browser_binding,
        )


def test_oidc_does_not_silently_link_an_existing_email(app, monkeypatch):
    from api.db import db
    from api.models import User

    _set_oidc_environment(monkeypatch, OIDC_LINK_BY_EMAIL="false")
    existing = User.query.filter_by(username="viewer").one()
    existing.email = "collision@example.test"
    db.session.commit()
    monkeypatch.setattr(
        "api.oidc_auth.fetch_discovery", lambda config: _discovery(config)
    )
    started = start_oidc_login()
    state = parse_qs(urlparse(started.authorization_url).query)["state"][0]
    monkeypatch.setattr(
        "api.oidc_auth.exchange_code",
        lambda *args, **kwargs: {"id_token": "validated-separately"},
    )
    monkeypatch.setattr(
        "api.oidc_auth.validate_id_token",
        lambda *args, **kwargs: {
            "sub": "collision-subject",
            "email": "collision@example.test",
            "email_verified": True,
            "groups": ["platform-viewers"],
        },
    )

    with pytest.raises(OidcProtocolError, match="explicit linking"):
        complete_oidc_login(
            state=state,
            code="authorization-code",
            browser_binding=started.browser_binding,
        )
    assert User.query.filter_by(email="collision@example.test").count() == 1
