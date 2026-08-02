"""Contract 4: browser sessions, refresh rotation, and cookie CSRF."""

from __future__ import annotations

import hashlib
import json

import pyotp

from api.auth_utils import decode_access_token
from api.models import AuditLog
from api.models_auth import AuthRefreshToken, AuthSession
from api.session_auth import (
    ACCESS_COOKIE,
    CSRF_COOKIE,
    CSRF_HEADER,
    INTERIM_COOKIE,
    REFRESH_COOKIE,
)
from tests.conftest import auth_headers


def _login(client, username="admin", password="admin123"):
    response = client.post(
        "/api/auth/login",
        json={"username": username, "password": password},
    )
    assert response.status_code == 200, response.get_json()
    return response


def _csrf(client) -> str:
    response = client.get("/api/auth/csrf")
    assert response.status_code == 200
    token = response.get_json()["data"]["csrfToken"]
    cookie = client.get_cookie(CSRF_COOKIE)
    assert cookie and cookie.value == token
    return token


def _refresh_cookie(client):
    return client.get_cookie(REFRESH_COOKIE, path="/api/auth")


def test_login_dual_accept_issues_hardened_cookies_and_bearer(client):
    response = _login(client)
    data = response.get_json()["data"]

    assert data["token"]
    access = client.get_cookie(ACCESS_COOKIE)
    refresh = _refresh_cookie(client)
    csrf = client.get_cookie(CSRF_COOKIE)
    assert access and access.secure and access.http_only
    assert refresh and refresh.secure and refresh.http_only
    assert csrf and csrf.secure and not csrf.http_only
    assert access.same_site == refresh.same_site == csrf.same_site == "Lax"


def test_cookie_access_authenticates_me_without_bearer(client):
    _login(client)

    response = client.get("/api/auth/me")

    assert response.status_code == 200
    assert response.get_json()["data"]["username"] == "admin"


def test_cookie_mutation_requires_matching_signed_csrf(client):
    _login(client)

    missing = client.post("/api/auth/logout")
    invalid = client.post(
        "/api/auth/logout", headers={CSRF_HEADER: "not-the-cookie"}
    )

    assert missing.status_code == 403
    assert invalid.status_code == 403
    assert CSRF_HEADER in missing.get_json()["error"]
    assert client.get("/api/auth/me").status_code == 200


def test_csrf_protects_cookie_mutations_outside_the_auth_blueprint(client):
    _login(client)

    missing = client.post("/api/auth/tokens", json={"name": "browser-token"})
    csrf = _csrf(client)
    accepted = client.post(
        "/api/auth/tokens",
        headers={CSRF_HEADER: csrf},
        json={"name": "browser-token"},
    )

    assert missing.status_code == 403
    assert accepted.status_code == 201


def test_bearer_mutation_remains_compatible_without_csrf(client):
    token = _login(client).get_json()["data"]["token"]

    response = client.post("/api/auth/logout", headers=auth_headers(token))

    assert response.status_code == 200


def test_logout_revokes_cookie_session_and_clears_cookies(client, app):
    _login(client)
    csrf = _csrf(client)

    response = client.post("/api/auth/logout", headers={CSRF_HEADER: csrf})

    assert response.status_code == 200
    assert client.get_cookie(ACCESS_COOKIE) is None
    assert _refresh_cookie(client) is None
    assert client.get("/api/auth/me").status_code == 401
    with app.app_context():
        session = AuthSession.query.one()
        assert session.revoked_at is not None
        assert session.revoke_reason == "logout"


def test_refresh_rotates_single_use_token_and_access_cookie(client, app):
    _login(client)
    csrf = _csrf(client)
    old_refresh = _refresh_cookie(client).value
    old_access = client.get_cookie(ACCESS_COOKIE).value

    response = client.post("/api/auth/refresh", headers={CSRF_HEADER: csrf})

    assert response.status_code == 200
    assert response.get_json()["data"]["user"]["username"] == "admin"
    assert _refresh_cookie(client).value != old_refresh
    assert client.get_cookie(ACCESS_COOKIE).value != old_access
    assert client.get("/api/auth/me").status_code == 200
    with app.app_context():
        tokens = AuthRefreshToken.query.order_by(AuthRefreshToken.created_at).all()
        assert len(tokens) == 2
        assert tokens[0].used_at is not None
        assert tokens[0].replaced_by_token_id == tokens[1].id


def test_consumed_refresh_token_reuse_revokes_entire_family(client, app):
    _login(client)
    csrf = _csrf(client)
    consumed_refresh = _refresh_cookie(client).value
    assert client.post(
        "/api/auth/refresh", headers={CSRF_HEADER: csrf}
    ).status_code == 200
    current_csrf = client.get_cookie(CSRF_COOKIE).value
    client.set_cookie(
        REFRESH_COOKIE, consumed_refresh, path="/api/auth", secure=True
    )

    replay = client.post(
        "/api/auth/refresh", headers={CSRF_HEADER: current_csrf}
    )

    assert replay.status_code == 401
    assert "reuse" in replay.get_json()["error"].lower()
    with app.app_context():
        session = AuthSession.query.one()
        assert session.revoked_at is not None
        assert session.revoke_reason == "refresh_token_reuse"
        assert all(
            token.revoked_at is not None for token in AuthRefreshToken.query.all()
        )


def test_session_lifecycle_is_audited_without_token_material(client, app):
    _login(client)
    csrf = _csrf(client)
    consumed_refresh = _refresh_cookie(client).value
    assert client.post(
        "/api/auth/refresh", headers={CSRF_HEADER: csrf}
    ).status_code == 200
    current_csrf = client.get_cookie(CSRF_COOKIE).value
    client.set_cookie(
        REFRESH_COOKIE, consumed_refresh, path="/api/auth", secure=True
    )
    assert client.post(
        "/api/auth/refresh", headers={CSRF_HEADER: current_csrf}
    ).status_code == 401

    with app.app_context():
        entries = AuditLog.query.filter(
            AuditLog.action.in_(
                {
                    "session_created",
                    "session_refreshed",
                    "refresh_token_reuse_detected",
                }
            )
        ).all()
        assert {entry.action for entry in entries} == {
            "session_created",
            "session_refreshed",
            "refresh_token_reuse_detected",
        }
        serialized = json.dumps(
            [
                {
                    "action": entry.action,
                    "target": entry.target_id,
                    "details": entry.details,
                }
                for entry in entries
            ]
        )
        assert consumed_refresh not in serialized
        assert hashlib.sha256(consumed_refresh.encode("utf-8")).hexdigest() not in serialized


def test_unknown_refresh_token_does_not_revoke_an_unrelated_session(app):
    first = app.test_client()
    second = app.test_client()
    _login(first)
    _login(second)
    csrf = _csrf(first)
    first.set_cookie(
        REFRESH_COOKIE, "unrelated-random-token", path="/api/auth", secure=True
    )

    response = first.post(
        "/api/auth/refresh", headers={CSRF_HEADER: csrf}
    )

    assert response.status_code == 401
    assert second.get("/api/auth/me").status_code == 200
    with app.app_context():
        assert AuthSession.query.filter_by(revoked_at=None).count() == 2


def test_refresh_tokens_are_only_stored_as_hashes(client, app):
    _login(client)
    raw_refresh = _refresh_cookie(client).value

    with app.app_context():
        stored = AuthRefreshToken.query.one()
        assert stored.token_hash == hashlib.sha256(
            raw_refresh.encode("utf-8")
        ).hexdigest()
        assert raw_refresh not in stored.token_hash


def test_sessions_are_listable_without_token_material(app):
    first = app.test_client()
    second = app.test_client()
    _login(first)
    _login(second)

    response = first.get("/api/auth/sessions")

    assert response.status_code == 200
    items = response.get_json()["data"]["items"]
    assert len(items) == 2
    assert sum(item["current"] for item in items) == 1
    serialized = response.get_data(as_text=True).lower()
    assert "token_hash" not in serialized
    assert "refreshtoken" not in serialized


def test_one_session_can_revoke_another(app):
    first = app.test_client()
    second = app.test_client()
    _login(first)
    _login(second)
    items = first.get("/api/auth/sessions").get_json()["data"]["items"]
    other = next(item for item in items if not item["current"])
    csrf = _csrf(first)

    response = first.delete(
        f"/api/auth/sessions/{other['id']}", headers={CSRF_HEADER: csrf}
    )

    assert response.status_code == 200
    assert first.get("/api/auth/me").status_code == 200
    assert second.get("/api/auth/me").status_code == 401


def test_global_logout_revokes_every_session(app):
    first = app.test_client()
    second = app.test_client()
    _login(first)
    _login(second)
    csrf = _csrf(first)

    response = first.post(
        "/api/auth/sessions/revoke-all", headers={CSRF_HEADER: csrf}
    )

    assert response.status_code == 200
    assert response.get_json()["data"]["revoked"] == 2
    assert first.get("/api/auth/me").status_code == 401
    assert second.get("/api/auth/me").status_code == 401


def test_access_and_refresh_durations_are_configurable(client, app, monkeypatch):
    monkeypatch.setenv("AUTH_ACCESS_SESSION_MINUTES", "7")
    monkeypatch.setenv("AUTH_REFRESH_SESSION_DAYS", "11")

    _login(client)
    access_payload = decode_access_token(client.get_cookie(ACCESS_COOKIE).value)

    assert 419 <= access_payload["exp"] - access_payload["iat"] <= 421
    with app.app_context():
        session = AuthSession.query.one()
        duration = session.refresh_expires_at - session.created_at
        assert timedelta_days(duration) == 11


def timedelta_days(value) -> int:
    return round(value.total_seconds() / 86400)


def test_logout_can_revoke_with_refresh_cookie_after_access_cookie_is_gone(
    client, app
):
    _login(client)
    csrf = _csrf(client)
    client.delete_cookie(ACCESS_COOKIE, path="/")

    response = client.post("/api/auth/logout", headers={CSRF_HEADER: csrf})

    assert response.status_code == 200
    with app.app_context():
        assert AuthSession.query.one().revoked_at is not None


def test_cookie_only_first_login_and_totp_flow(client, admin_token):
    roles = client.get(
        "/api/roles", headers=auth_headers(admin_token)
    ).get_json()["data"]["items"]
    viewer_role_id = next(role["id"] for role in roles if role["name"] == "viewer")
    created = client.post(
        "/api/users",
        headers=auth_headers(admin_token),
        json={
            "username": "cookie-newhire",
            "email": "cookie-newhire@test.local",
            "roleId": viewer_role_id,
            "clusterAccess": ["prod-us-east"],
        },
    ).get_json()["data"]

    login = _login(
        client, "cookie-newhire", created["temporaryPassword"]
    )
    assert login.get_json()["data"]["stage"] == "password_change"
    assert client.get_cookie(INTERIM_COOKIE, path="/api/auth").http_only
    csrf = _csrf(client)

    changed = client.post(
        "/api/auth/first-login/change-password",
        headers={CSRF_HEADER: csrf},
        json={"newPassword": "CookieOnlyPass!234"},
    )
    assert changed.status_code == 200
    setup = client.post(
        "/api/auth/first-login/totp/setup", headers={CSRF_HEADER: csrf}
    )
    secret = setup.get_json()["data"]["secret"]
    verified = client.post(
        "/api/auth/first-login/totp/verify",
        headers={CSRF_HEADER: csrf},
        json={"code": pyotp.TOTP(secret).now()},
    )

    assert verified.status_code == 200
    assert verified.get_json()["data"]["stage"] == "authenticated"
    assert client.get_cookie(ACCESS_COOKIE).http_only
    assert client.get_cookie(INTERIM_COOKIE, path="/api/auth") is None
    assert client.get("/api/auth/me").status_code == 200

    logout_csrf = _csrf(client)
    assert client.post(
        "/api/auth/logout", headers={CSRF_HEADER: logout_csrf}
    ).status_code == 200
    challenge = _login(client, "cookie-newhire", "CookieOnlyPass!234")
    assert challenge.get_json()["data"]["stage"] == "mfa_challenge"
    assert client.get_cookie(INTERIM_COOKIE, path="/api/auth").http_only
    mfa_csrf = _csrf(client)
    completed = client.post(
        "/api/auth/mfa/verify",
        headers={CSRF_HEADER: mfa_csrf},
        json={"code": pyotp.TOTP(secret).now()},
    )

    assert completed.status_code == 200
    assert completed.get_json()["data"]["stage"] == "authenticated"
    assert client.get_cookie(ACCESS_COOKIE).http_only
    assert client.get_cookie(INTERIM_COOKIE, path="/api/auth") is None
