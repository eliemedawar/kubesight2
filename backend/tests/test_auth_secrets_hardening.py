"""Go-live hardening of auth and secrets.

Each block pins one fail-open default that used to be reachable in production:
API-token routes that skipped the first-login gate, TOTP seeds in plaintext, a
hard-coded signing/encryption key, a production switch that FLASK_DEBUG's
default turned off, a silent SQLite fallback, the AUTH_REQUIRED kill switch,
logout that revoked nothing, temporary passwords echoed back when SMTP failed,
and Secret values readable by anyone with resources:view.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import jwt
import pyotp
import pytest
from sqlalchemy import text

from api import auth_utils, runtime_config
from api.db import db
from api.models import User
from tests.conftest import auth_headers

CLUSTER = "prod-us-east"
NAMESPACE = "payments"


def _prod_env(monkeypatch, **extra):
    """A production process with a correctly configured key set."""
    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.delenv("FLASK_DEBUG", raising=False)
    monkeypatch.setenv("JWT_SECRET_KEY", "j" * 40)
    monkeypatch.setenv("KUBESIGHT_SECRET_KEY", "k" * 40)
    monkeypatch.setenv("AUTH_REQUIRED", "true")
    for key, value in extra.items():
        monkeypatch.setenv(key, value)


def _clear_env(monkeypatch):
    for key in (
        "APP_ENV", "FLASK_ENV", "KUBESIGHT_ENV", "FLASK_DEBUG",
        "KUBESIGHT_SECRET_KEY", "ALERT_ROUTING_SECRET_KEY", "FLASK_SECRET_KEY",
        "KUBESIGHT_PREVIOUS_SECRET_KEYS",
    ):
        monkeypatch.delenv(key, raising=False)


# ---------------------------------------------------------------------------
# 4. Production detection
# ---------------------------------------------------------------------------

class TestProductionDetection:
    def test_app_env_alone_is_production(self, monkeypatch):
        _clear_env(monkeypatch)
        monkeypatch.setenv("APP_ENV", "production")
        # FLASK_DEBUG unset used to default to "true" and make this look like dev.
        assert runtime_config.is_production_env() is True

    def test_flask_debug_does_not_override_production(self, monkeypatch):
        _clear_env(monkeypatch)
        monkeypatch.setenv("KUBESIGHT_ENV", "prod")
        monkeypatch.setenv("FLASK_DEBUG", "true")
        assert runtime_config.is_production_env() is True

    def test_nothing_set_is_development(self, monkeypatch):
        _clear_env(monkeypatch)
        assert runtime_config.is_production_env() is False

    def test_legacy_flask_env_still_counts(self, monkeypatch):
        _clear_env(monkeypatch)
        monkeypatch.setenv("FLASK_ENV", "production")
        assert runtime_config.is_production_env() is True


# ---------------------------------------------------------------------------
# 3 + 6. Boot checks: keys and the AUTH_REQUIRED kill switch
# ---------------------------------------------------------------------------

class TestProductionBootChecks:
    def test_good_config_boots(self, monkeypatch):
        _clear_env(monkeypatch)
        _prod_env(monkeypatch)
        runtime_config.enforce_production_config()  # no raise

    @pytest.mark.parametrize("value", ["", "kubesight-dev-secret-change-me"])
    def test_missing_or_default_jwt_key_refused(self, monkeypatch, value):
        _clear_env(monkeypatch)
        _prod_env(monkeypatch, JWT_SECRET_KEY=value)
        with pytest.raises(runtime_config.InsecureConfigurationError, match="JWT_SECRET_KEY"):
            runtime_config.enforce_production_config()

    def test_missing_encryption_key_refused(self, monkeypatch):
        _clear_env(monkeypatch)
        _prod_env(monkeypatch)
        monkeypatch.delenv("KUBESIGHT_SECRET_KEY")
        with pytest.raises(runtime_config.InsecureConfigurationError, match="KUBESIGHT_SECRET_KEY"):
            runtime_config.enforce_production_config()

    def test_legacy_alert_routing_key_satisfies_the_check(self, monkeypatch):
        _clear_env(monkeypatch)
        _prod_env(monkeypatch)
        monkeypatch.delenv("KUBESIGHT_SECRET_KEY")
        monkeypatch.setenv("ALERT_ROUTING_SECRET_KEY", "a" * 40)
        runtime_config.enforce_production_config()

    def test_auth_kill_switch_refused_in_production(self, monkeypatch):
        _clear_env(monkeypatch)
        _prod_env(monkeypatch, AUTH_REQUIRED="false")
        with pytest.raises(runtime_config.InsecureConfigurationError, match="AUTH_REQUIRED"):
            runtime_config.enforce_production_config()

    def test_auth_kill_switch_ignored_at_runtime_in_production(self, monkeypatch):
        _clear_env(monkeypatch)
        _prod_env(monkeypatch, AUTH_REQUIRED="false")
        assert auth_utils.auth_required_enabled() is True

    def test_auth_kill_switch_still_works_in_development(self, monkeypatch):
        _clear_env(monkeypatch)
        monkeypatch.setenv("AUTH_REQUIRED", "false")
        assert auth_utils.auth_required_enabled() is False

    def test_development_boots_with_nothing_configured(self, monkeypatch):
        _clear_env(monkeypatch)
        monkeypatch.delenv("JWT_SECRET_KEY", raising=False)
        runtime_config.enforce_production_config()  # warnings only
        assert auth_utils._jwt_secret() == runtime_config.INSECURE_DEVELOPMENT_KEY

    def test_jwt_signing_refuses_default_in_production(self, monkeypatch):
        _clear_env(monkeypatch)
        _prod_env(monkeypatch)
        monkeypatch.delenv("JWT_SECRET_KEY")
        with pytest.raises(runtime_config.InsecureConfigurationError):
            auth_utils._jwt_secret()

    def test_create_app_refuses_insecure_production(self, monkeypatch):
        from api import create_app

        _clear_env(monkeypatch)
        _prod_env(monkeypatch)
        monkeypatch.delenv("KUBESIGHT_SECRET_KEY")
        with pytest.raises(runtime_config.InsecureConfigurationError):
            create_app()


# ---------------------------------------------------------------------------
# 3. Encryption key: dedicated key, legacy ciphertext still decrypts
# ---------------------------------------------------------------------------

class TestSecretEncryptionKeys:
    def test_production_without_dedicated_key_refuses_to_encrypt(self, monkeypatch):
        from api.secret_encryption import encrypt_secret

        _clear_env(monkeypatch)
        _prod_env(monkeypatch)
        monkeypatch.delenv("KUBESIGHT_SECRET_KEY")
        with pytest.raises(runtime_config.InsecureConfigurationError):
            encrypt_secret("hunter2")

    def test_rows_encrypted_with_the_jwt_key_survive_a_new_dedicated_key(self, monkeypatch):
        from api.secret_encryption import decrypt_secret, encrypt_secret

        _clear_env(monkeypatch)
        monkeypatch.setenv("JWT_SECRET_KEY", "old-jwt-key-used-for-encryption")
        legacy = encrypt_secret("bitbucket-app-password")

        monkeypatch.setenv("KUBESIGHT_SECRET_KEY", "brand-new-dedicated-key")
        assert decrypt_secret(legacy) == "bitbucket-app-password"
        fresh = encrypt_secret("x")
        # New ciphertext is written with the dedicated key only.
        monkeypatch.delenv("JWT_SECRET_KEY")
        assert decrypt_secret(fresh) == "x"

    def test_rows_encrypted_with_the_dev_default_still_decrypt(self, monkeypatch):
        from api.secret_encryption import decrypt_secret, encrypt_secret

        _clear_env(monkeypatch)
        monkeypatch.delenv("JWT_SECRET_KEY", raising=False)
        legacy = encrypt_secret("smtp-password")
        _prod_env(monkeypatch)
        assert decrypt_secret(legacy) == "smtp-password"

    def test_alert_routing_key_remains_primary_when_set(self, monkeypatch):
        from api.secret_encryption import decrypt_secret, encrypt_secret

        _clear_env(monkeypatch)
        monkeypatch.setenv("ALERT_ROUTING_SECRET_KEY", "routing-key")
        cipher = encrypt_secret("webhook")
        monkeypatch.delenv("JWT_SECRET_KEY", raising=False)
        assert decrypt_secret(cipher) == "webhook"

    def test_rotation_via_previous_keys(self, monkeypatch):
        from api.secret_encryption import decrypt_secret, encrypt_secret

        _clear_env(monkeypatch)
        monkeypatch.setenv("KUBESIGHT_SECRET_KEY", "key-one")
        cipher = encrypt_secret("value")
        monkeypatch.setenv("KUBESIGHT_SECRET_KEY", "key-two")
        assert decrypt_secret(cipher) == ""
        monkeypatch.setenv("KUBESIGHT_PREVIOUS_SECRET_KEYS", "key-one")
        assert decrypt_secret(cipher) == "value"


# ---------------------------------------------------------------------------
# 5. Database fallback
# ---------------------------------------------------------------------------

class TestDatabaseResolution:
    UNREACHABLE = "postgresql://kubesight:pw@127.0.0.1:1/kubesight"

    def test_unreachable_database_fails_hard_in_production(self, monkeypatch):
        from api import _resolve_database_url

        _clear_env(monkeypatch)
        _prod_env(monkeypatch, DATABASE_URL=self.UNREACHABLE)
        with pytest.raises(RuntimeError, match="unreachable"):
            _resolve_database_url()

    def test_error_does_not_leak_the_password(self, monkeypatch):
        from api import _resolve_database_url

        _clear_env(monkeypatch)
        _prod_env(monkeypatch, DATABASE_URL=self.UNREACHABLE)
        with pytest.raises(RuntimeError) as excinfo:
            _resolve_database_url()
        assert "pw" not in str(excinfo.value).replace("kubesight", "")

    def test_missing_database_url_refused_in_production(self, monkeypatch):
        from api import _resolve_database_url

        _clear_env(monkeypatch)
        _prod_env(monkeypatch)
        monkeypatch.delenv("DATABASE_URL", raising=False)
        with pytest.raises(RuntimeError, match="DATABASE_URL"):
            _resolve_database_url()

    def test_unreachable_database_falls_back_loudly_in_development(self, monkeypatch, caplog):
        from api import _resolve_database_url

        _clear_env(monkeypatch)
        monkeypatch.setenv("DATABASE_URL", self.UNREACHABLE)
        with caplog.at_level("ERROR", logger="kubesight.db"):
            assert _resolve_database_url() == "sqlite:///kubesight.db"
        assert "FALLING BACK TO LOCAL SQLITE" in caplog.text
        assert ":pw@" not in caplog.text

    def test_no_database_url_is_the_dev_sqlite_path(self, monkeypatch):
        from api import _resolve_database_url

        _clear_env(monkeypatch)
        monkeypatch.delenv("DATABASE_URL", raising=False)
        assert _resolve_database_url() == "sqlite:///kubesight.db"


# ---------------------------------------------------------------------------
# 1. API token routes go through require_permission
# ---------------------------------------------------------------------------

class TestApiTokenRoutes:
    def test_admin_can_manage_tokens(self, client, admin_token):
        created = client.post(
            "/api/auth/tokens", json={"name": "ci"}, headers=auth_headers(admin_token)
        )
        assert created.status_code == 201
        token_id = created.get_json()["data"]["id"]
        assert client.get("/api/auth/tokens", headers=auth_headers(admin_token)).status_code == 200
        revoked = client.delete(f"/api/auth/tokens/{token_id}", headers=auth_headers(admin_token))
        assert revoked.status_code == 200

    def test_user_without_permission_is_refused(self, client, viewer_token):
        headers = auth_headers(viewer_token)
        assert client.post("/api/auth/tokens", json={"name": "x"}, headers=headers).status_code == 403
        assert client.get("/api/auth/tokens", headers=headers).status_code == 403
        assert client.delete("/api/auth/tokens/1", headers=headers).status_code == 403

    def test_first_login_gate_applies(self, client, admin_token):
        admin = User.query.filter_by(username="admin").first()
        admin.first_login_completed = False
        db.session.commit()
        response = client.post(
            "/api/auth/tokens", json={"name": "skip-mfa"}, headers=auth_headers(admin_token)
        )
        assert response.status_code == 403
        assert "first-login" in response.get_json()["error"].lower()

    def test_anonymous_is_refused(self, client):
        assert client.get("/api/auth/tokens").status_code == 401


# ---------------------------------------------------------------------------
# 2. TOTP seeds encrypted at rest
# ---------------------------------------------------------------------------

def _viewer_role_id():
    from api.models import Role

    return Role.query.filter_by(name="viewer").first().id


def _create_user(client, admin_token, username):
    response = client.post(
        "/api/users",
        headers=auth_headers(admin_token),
        json={
            "username": username,
            "email": f"{username}@test.local",
            "roleId": _viewer_role_id(),
            "clusterAccess": [CLUSTER],
        },
    )
    assert response.status_code == 201, response.get_json()
    return response.get_json()["data"]


def _onboard(client, admin_token, username, password="StrongPass!2345"):
    created = _create_user(client, admin_token, username)
    login = client.post(
        "/api/auth/login", json={"username": username, "password": created["temporaryPassword"]}
    )
    onboarding = login.get_json()["data"]["onboardingToken"]
    client.post(
        "/api/auth/first-login/change-password",
        headers=auth_headers(onboarding),
        json={"newPassword": password},
    )
    secret = client.post(
        "/api/auth/first-login/totp/setup", headers=auth_headers(onboarding)
    ).get_json()["data"]["secret"]
    verified = client.post(
        "/api/auth/first-login/totp/verify",
        headers=auth_headers(onboarding),
        json={"code": pyotp.TOTP(secret).now()},
    )
    assert verified.status_code == 200, verified.get_json()
    return secret, verified.get_json()["data"]["token"]


class TestTotpEncryption:
    def test_seed_is_stored_encrypted_and_still_verifies(self, client, admin_token):
        secret, _token = _onboard(client, admin_token, "totpenc")
        stored = User.query.filter_by(username="totpenc").first().totp_secret
        assert stored != secret
        assert stored.startswith("gAAAAA")

        login = client.post(
            "/api/auth/login", json={"username": "totpenc", "password": "StrongPass!2345"}
        )
        mfa_token = login.get_json()["data"]["mfaToken"]
        ok = client.post(
            "/api/auth/mfa/verify",
            headers=auth_headers(mfa_token),
            json={"code": pyotp.TOTP(secret).now()},
        )
        assert ok.status_code == 200

    def test_legacy_plaintext_seed_still_verifies(self, client, admin_token):
        secret, _token = _onboard(client, admin_token, "totplegacy")
        user = User.query.filter_by(username="totplegacy").first()
        user.totp_secret = secret  # a row from before encryption
        db.session.commit()

        login = client.post(
            "/api/auth/login", json={"username": "totplegacy", "password": "StrongPass!2345"}
        )
        mfa_token = login.get_json()["data"]["mfaToken"]
        ok = client.post(
            "/api/auth/mfa/verify",
            headers=auth_headers(mfa_token),
            json={"code": pyotp.TOTP(secret).now()},
        )
        assert ok.status_code == 200

    def test_migration_encrypts_plaintext_rows_idempotently(self, client, admin_token):
        from api.migrate_rbac import _migrate_totp_secret_encryption
        from api.secret_encryption import decrypt_secret

        secret, _token = _onboard(client, admin_token, "totpmigrate")
        user = User.query.filter_by(username="totpmigrate").first()
        with db.engine.begin() as conn:
            conn.execute(
                text("UPDATE users SET totp_secret = :s WHERE id = :id"),
                {"s": secret, "id": user.id},
            )
        _migrate_totp_secret_encryption()
        first = db.session.execute(
            text("SELECT totp_secret FROM users WHERE id = :id"), {"id": user.id}
        ).scalar()
        assert first.startswith("gAAAAA")
        assert decrypt_secret(first) == secret

        _migrate_totp_secret_encryption()
        second = db.session.execute(
            text("SELECT totp_secret FROM users WHERE id = :id"), {"id": user.id}
        ).scalar()
        assert second == first

    def test_undecryptable_ciphertext_is_not_used_as_a_seed(self, client, admin_token):
        from api.services.auth_service import _totp_secret

        _onboard(client, admin_token, "totpbroken")
        user = User.query.filter_by(username="totpbroken").first()
        user.totp_secret = "gAAAAABnot-a-real-token"
        assert _totp_secret(user) is None


# ---------------------------------------------------------------------------
# 7. Session revocation via token_version
# ---------------------------------------------------------------------------

def _me(client, token):
    return client.get("/api/auth/me", headers=auth_headers(token)).status_code


class TestSessionRevocation:
    def test_logout_revokes_the_token(self, client, admin_token):
        assert _me(client, admin_token) == 200
        assert client.post("/api/auth/logout", headers=auth_headers(admin_token)).status_code == 200
        assert _me(client, admin_token) == 401

    def test_new_login_after_logout_works(self, client, admin_token):
        client.post("/api/auth/logout", headers=auth_headers(admin_token))
        again = client.post("/api/auth/login", json={"username": "admin", "password": "admin123"})
        assert _me(client, again.get_json()["data"]["token"]) == 200

    def test_api_tokens_survive_logout(self, client, admin_token):
        raw = client.post(
            "/api/auth/tokens", json={"name": "hermes"}, headers=auth_headers(admin_token)
        ).get_json()["data"]["token"]
        client.post("/api/auth/logout", headers=auth_headers(admin_token))
        assert _me(client, raw) == 200

    def test_logout_with_an_api_token_does_not_sign_the_owner_out(self, client, admin_token):
        raw = client.post(
            "/api/auth/tokens", json={"name": "script"}, headers=auth_headers(admin_token)
        ).get_json()["data"]["token"]
        assert client.post("/api/auth/logout", headers=auth_headers(raw)).status_code == 200
        assert _me(client, admin_token) == 200
        assert _me(client, raw) == 200

    def test_token_without_ver_claim_is_accepted_until_the_next_revocation(self, client):
        admin = User.query.filter_by(username="admin").first()
        now = datetime.now(timezone.utc)
        legacy = jwt.encode(
            {"sub": str(admin.id), "username": "admin", "purpose": "access",
             "iat": now, "exp": now + timedelta(hours=1)},
            auth_utils._jwt_secret(),
            algorithm="HS256",
        )
        assert _me(client, legacy) == 200
        client.post("/api/auth/logout", headers=auth_headers(legacy))
        assert _me(client, legacy) == 401

    @pytest.mark.parametrize(
        "action",
        ["force-password-reset", "reset-mfa", "lock", "disable", "resend-temporary-password"],
    )
    def test_admin_actions_revoke_the_users_sessions(self, client, admin_token, action):
        _secret, token = _onboard(client, admin_token, f"revoke{action.replace('-', '')}")
        assert _me(client, token) == 200
        user = User.query.filter(User.username.like("revoke%")).first()
        routes = {
            "force-password-reset": ("post", f"/api/users/{user.id}/force-password-reset"),
            "reset-mfa": ("post", f"/api/users/{user.id}/reset-mfa"),
            "lock": ("post", f"/api/users/{user.id}/lock"),
            "disable": ("delete", f"/api/users/{user.id}"),
            "resend-temporary-password": ("post", f"/api/users/{user.id}/resend-temporary-password"),
        }
        method, url = routes[action]
        response = getattr(client, method)(url, headers=auth_headers(admin_token))
        assert response.status_code == 200, (url, response.get_json())
        assert _me(client, token) == 401

    def test_admin_password_update_revokes(self, client, admin_token):
        _secret, token = _onboard(client, admin_token, "revokepw")
        user = User.query.filter_by(username="revokepw").first()
        response = client.put(
            f"/api/users/{user.id}",
            json={"password": "AnotherStrong!2345"},
            headers=auth_headers(admin_token),
        )
        assert response.status_code == 200, response.get_json()
        assert _me(client, token) == 401


# ---------------------------------------------------------------------------
# 8. Temporary password never echoed in production when SMTP fails
# ---------------------------------------------------------------------------

class TestTemporaryPasswordReveal:
    def test_development_still_reveals(self, client, admin_token):
        created = _create_user(client, admin_token, "tmpdev")
        assert created["temporaryPassword"]
        assert created["temporaryPasswordDeliveryFailed"] is True
        assert created["temporaryPasswordRevealed"] is True

    def test_production_withholds_the_password(self, client, admin_token, monkeypatch):
        import os

        # Keep the key the fixture token was signed with.
        _prod_env(monkeypatch, JWT_SECRET_KEY=os.environ["JWT_SECRET_KEY"])
        created = _create_user(client, admin_token, "tmpprod")
        assert "temporaryPassword" not in created
        assert created["temporaryPasswordEmailed"] is False
        assert created["temporaryPasswordDeliveryFailed"] is True
        assert created["temporaryPasswordRevealed"] is False
        assert "Resend" in created["temporaryPasswordHint"]

        user = User.query.filter_by(username="tmpprod").first()
        for route in ("resend-temporary-password", "force-password-reset"):
            data = client.post(
                f"/api/users/{user.id}/{route}", headers=auth_headers(admin_token)
            ).get_json()["data"]
            assert "temporaryPassword" not in data
            assert data["temporaryPasswordDeliveryFailed"] is True

    def test_production_reveals_with_explicit_opt_in(self, client, admin_token, monkeypatch):
        import os

        _prod_env(
            monkeypatch,
            JWT_SECRET_KEY=os.environ["JWT_SECRET_KEY"],
            ALLOW_TEMP_PASSWORD_REVEAL="true",
        )
        created = _create_user(client, admin_token, "tmpoptin")
        assert created["temporaryPassword"]
        assert created["temporaryPasswordRevealed"] is True


# ---------------------------------------------------------------------------
# 9. Secret values need secrets:reveal
# ---------------------------------------------------------------------------

LIVE_SECRET_YAML = """apiVersion: v1
kind: Secret
metadata:
  name: {name}
  namespace: payments
  annotations:
    kubectl.kubernetes.io/last-applied-configuration: '{{"data":{{"password":"c3VwZXJzZWNyZXQ="}}}}'
type: Opaque
data:
  password: c3VwZXJzZWNyZXQ=
  username: YWRtaW4=
stringData:
  token: plain-token
"""


class TestSecretRedaction:
    def test_redaction_keeps_keys_and_hides_values(self):
        from api.services.resource_actions_service import REDACTED_SECRET_VALUE, redact_secret_yaml

        redacted, keys = redact_secret_yaml(LIVE_SECRET_YAML.format(name="s"))
        assert "c3VwZXJzZWNyZXQ=" not in redacted
        assert "plain-token" not in redacted
        assert "YWRtaW4=" not in redacted
        assert sorted(keys) == ["password", "token", "username"]
        assert redacted.count(REDACTED_SECRET_VALUE) == 4  # 3 values + last-applied

    def test_redaction_handles_lists_and_unparseable_text(self):
        from api.services.resource_actions_service import redact_secret_yaml

        listing = "apiVersion: v1\nkind: List\nitems:\n- kind: Secret\n  data:\n    k: dmFsdWU=\n"
        redacted, keys = redact_secret_yaml(listing)
        assert "dmFsdWU=" not in redacted and keys == ["k"]
        broken, keys = redact_secret_yaml("data: [unclosed\n  password: c2VjcmV0")
        assert "c2VjcmV0" not in broken and keys == []

    def test_rbac_grants(self):
        from api.rbac_data import ROLE_DEFINITIONS

        assert "secrets:reveal" in ROLE_DEFINITIONS["admin"]["permissions"]
        assert "secrets:reveal" in ROLE_DEFINITIONS["cluster_admin"]["permissions"]
        assert "secrets:reveal" not in ROLE_DEFINITIONS["operator"]["permissions"]
        assert "secrets:reveal" not in ROLE_DEFINITIONS["viewer"]["permissions"]

    def _live(self, monkeypatch, calls):
        from api.cluster_access import ClusterAccess
        from api.services import resource_actions_service as svc

        monkeypatch.setattr(svc, "should_use_real_k8s", lambda _cid: True)
        monkeypatch.setattr(
            svc, "resolve_cluster_access", lambda cid: ClusterAccess(cluster_id=cid, context_name=cid)
        )

        def fake_kubectl(_access, args):
            calls.append(args)
            return LIVE_SECRET_YAML.format(name=args[2])

        monkeypatch.setattr(svc, "_run_for_access", fake_kubectl)

    def _yaml(self, client, token, name):
        response = client.get(
            f"/api/clusters/{CLUSTER}/namespaces/{NAMESPACE}/resources/secret/{name}/yaml",
            headers=auth_headers(token),
        )
        assert response.status_code == 200, response.get_json()
        return response.get_json()["data"]

    def test_viewer_gets_redacted_yaml(self, client, viewer_token, monkeypatch):
        calls = []
        self._live(monkeypatch, calls)
        data = self._yaml(client, viewer_token, "viewer-only-secret")
        assert data["valuesHidden"] is True
        assert data["hiddenKeys"] == ["password", "token", "username"]
        assert "c3VwZXJzZWNyZXQ=" not in data["yaml"]

    def test_admin_gets_values(self, client, admin_token, monkeypatch):
        calls = []
        self._live(monkeypatch, calls)
        data = self._yaml(client, admin_token, "admin-secret")
        assert data["valuesHidden"] is False
        assert "c3VwZXJzZWNyZXQ=" in data["yaml"]

    def test_cache_never_serves_values_across_privilege(
        self, client, admin_token, viewer_token, monkeypatch
    ):
        from api import ttl_cache
        from api.k8s_provider import _K8S_READ_CACHE

        calls = []
        self._live(monkeypatch, calls)
        # TESTING turns the read cache off; this test is about the cache.
        monkeypatch.setattr(ttl_cache, "caching_disabled", lambda: False)
        name = "shared-cache-secret"
        try:
            assert "c3VwZXJzZWNyZXQ=" in self._yaml(client, admin_token, name)["yaml"]
            assert "c3VwZXJzZWNyZXQ=" not in self._yaml(client, viewer_token, name)["yaml"]
            # The redacted copy is cached; the revealing one never is.
            assert "c3VwZXJzZWNyZXQ=" not in self._yaml(client, viewer_token, name)["yaml"]
            assert "c3VwZXJzZWNyZXQ=" in self._yaml(client, admin_token, name)["yaml"]
            assert len(calls) == 3
            cached = [
                value for key, (_exp, _stale, value) in _K8S_READ_CACHE._entries.items()
                if name in str(key)
            ]
            assert cached and all("c3VwZXJzZWNyZXQ=" not in str(value) for value in cached)
        finally:
            _K8S_READ_CACHE.invalidate(f"res:{CLUSTER}:{NAMESPACE}:")

    def test_mcp_resource_get_uses_the_same_redaction(self, client, viewer_token, monkeypatch):
        from tests.test_mcp_server import call_tool

        calls = []
        self._live(monkeypatch, calls)
        # The MCP endpoint takes a Bearer token like any other route.
        result = call_tool(
            client,
            viewer_token,
            "kubesight_resource_get",
            {"cluster": CLUSTER, "namespace": NAMESPACE, "kind": "secret",
             "name": "mcp-secret", "as": "yaml"},
        )
        raw = str(result)
        assert "c3VwZXJzZWNyZXQ=" not in raw
        assert "valuesHidden" in raw

    def test_applying_a_redacted_secret_is_refused(self):
        from api.services.deployment_service import validate_yaml
        from api.services.resource_actions_service import redact_secret_yaml

        redacted, _ = redact_secret_yaml(LIVE_SECRET_YAML.format(name="s"))
        _data, error, status = validate_yaml(redacted, NAMESPACE, preview_mode=True)
        assert status == 403
        assert "hidden placeholder" in error

    def test_describe_is_unchanged(self, client, viewer_token):
        response = client.get(
            f"/api/clusters/{CLUSTER}/namespaces/{NAMESPACE}/resources/secret/app-secret/describe",
            headers=auth_headers(viewer_token),
        )
        assert response.status_code == 200
