"""Tests for the expanded onboarding security policy: password rules, MFA
lockout, hybrid admin-unlock escalation, and admin account actions."""

from datetime import datetime, timedelta, timezone

import pyotp

from tests.conftest import auth_headers


def _viewer_role_id(client, admin_token):
    roles = client.get("/api/roles", headers=auth_headers(admin_token)).get_json()["data"]["items"]
    return next(r for r in roles if r["name"] == "viewer")["id"]


def _create_user(client, admin_token, username, email):
    resp = client.post(
        "/api/users",
        headers=auth_headers(admin_token),
        json={
            "username": username,
            "email": email,
            "roleId": _viewer_role_id(client, admin_token),
            "clusterAccess": ["prod-us-east"],
        },
    )
    assert resp.status_code == 201, resp.get_json()
    return resp.get_json()["data"]


def _start_onboarding(client, username, temp_password):
    login = client.post("/api/auth/login", json={"username": username, "password": temp_password})
    assert login.status_code == 200
    return login.get_json()["data"]["onboardingToken"]


def test_password_policy_rejects_weak_passwords(client, admin_token):
    created = _create_user(client, admin_token, "weakpw", "weakpw@test.local")
    token = _start_onboarding(client, "weakpw", created["temporaryPassword"])
    for weak in ["short1!A", "alllowercase123!", "NOLOWERCASE123!", "NoNumber!!!!", "NoSpecial123ABC"]:
        resp = client.post(
            "/api/auth/first-login/change-password",
            headers=auth_headers(token),
            json={"newPassword": weak},
        )
        assert resp.status_code == 400, weak


def test_password_change_rejects_same_as_temporary(client, admin_token):
    created = _create_user(client, admin_token, "sametemp", "sametemp@test.local")
    temp = created["temporaryPassword"]
    token = _start_onboarding(client, "sametemp", temp)
    resp = client.post(
        "/api/auth/first-login/change-password",
        headers=auth_headers(token),
        json={"newPassword": temp},
    )
    # Random temp passwords already satisfy the policy, so this must be rejected
    # specifically for matching the temporary password.
    assert resp.status_code == 400


def _fully_onboard(client, admin_token, username, email):
    created = _create_user(client, admin_token, username, email)
    token = _start_onboarding(client, username, created["temporaryPassword"])
    client.post(
        "/api/auth/first-login/change-password",
        headers=auth_headers(token),
        json={"newPassword": "StrongPass!2345"},
    )
    secret = client.post("/api/auth/first-login/totp/setup", headers=auth_headers(token)).get_json()["data"]["secret"]
    client.post(
        "/api/auth/first-login/totp/verify",
        headers=auth_headers(token),
        json={"code": pyotp.TOTP(secret).now()},
    )
    return secret


def test_mfa_lockout_after_five_wrong_codes(client, admin_token):
    secret = _fully_onboard(client, admin_token, "mfalock", "mfalock@test.local")
    login = client.post("/api/auth/login", json={"username": "mfalock", "password": "StrongPass!2345"})
    mfa_token = login.get_json()["data"]["mfaToken"]
    for _ in range(4):
        r = client.post("/api/auth/mfa/verify", headers=auth_headers(mfa_token), json={"code": "000000"})
        assert r.status_code == 400
    # 5th wrong code locks the account.
    r = client.post("/api/auth/mfa/verify", headers=auth_headers(mfa_token), json={"code": "000000"})
    assert r.status_code == 423
    assert "verification codes" in r.get_json()["error"].lower()
    # Even a correct password now returns the lock.
    again = client.post("/api/auth/login", json={"username": "mfalock", "password": "StrongPass!2345"})
    assert again.status_code == 423


def test_admin_unlock_restores_access(client, admin_token):
    _fully_onboard(client, admin_token, "unlockme", "unlockme@test.local")
    for _ in range(5):
        client.post("/api/auth/login", json={"username": "unlockme", "password": "wrong-pass"})
    locked = client.post("/api/auth/login", json={"username": "unlockme", "password": "StrongPass!2345"})
    assert locked.status_code == 423

    from api.models import User

    uid = User.query.filter_by(username="unlockme").first().id
    unlock = client.post(f"/api/users/{uid}/unlock", headers=auth_headers(admin_token))
    assert unlock.status_code == 200

    ok = client.post("/api/auth/login", json={"username": "unlockme", "password": "StrongPass!2345"})
    assert ok.status_code == 200
    assert ok.get_json()["data"]["stage"] == "mfa_challenge"


def test_three_locks_escalate_to_admin_unlock(client, admin_token):
    from api.db import db
    from api.models import User

    _create_user(client, admin_token, "escalate", "escalate@test.local")
    user = User.query.filter_by(username="escalate").first()
    for _round in range(3):
        for _ in range(5):
            client.post("/api/auth/login", json={"username": "escalate", "password": "wrong-pass"})
        # Expire the temporary lock so the next round of failures can lock again.
        user = User.query.filter_by(username="escalate").first()
        user.locked_until = datetime.now(timezone.utc) - timedelta(minutes=1)
        db.session.commit()

    user = User.query.filter_by(username="escalate").first()
    assert user.requires_admin_unlock is True

    # Status is surfaced to admins and the lock message points at the admin.
    users = client.get("/api/users", headers=auth_headers(admin_token)).get_json()["data"]["items"]
    row = next(u for u in users if u["username"] == "escalate")
    assert row["accountStatus"] == "admin_locked"

    resp = client.post("/api/auth/login", json={"username": "escalate", "password": "wrong-pass"})
    assert resp.status_code == 423
    assert "administrator" in resp.get_json()["error"].lower()


def test_manual_admin_lock_blocks_login(client, admin_token):
    _fully_onboard(client, admin_token, "manuallock", "manuallock@test.local")
    from api.models import User

    uid = User.query.filter_by(username="manuallock").first().id
    lock = client.post(f"/api/users/{uid}/lock", headers=auth_headers(admin_token))
    assert lock.status_code == 200

    resp = client.post("/api/auth/login", json={"username": "manuallock", "password": "StrongPass!2345"})
    assert resp.status_code == 423
    assert "administrator" in resp.get_json()["error"].lower()


def test_force_password_reset_keeps_mfa(client, admin_token):
    secret = _fully_onboard(client, admin_token, "forcereset", "forcereset@test.local")
    from api.models import User

    uid = User.query.filter_by(username="forcereset").first().id
    resp = client.post(f"/api/users/{uid}/force-password-reset", headers=auth_headers(admin_token))
    assert resp.status_code == 200
    new_temp = resp.get_json()["data"]["temporaryPassword"]

    # Login with the new temp password -> must change password again.
    login = client.post("/api/auth/login", json={"username": "forcereset", "password": new_temp})
    assert login.status_code == 200
    token = login.get_json()["data"]["onboardingToken"]
    assert login.get_json()["data"]["stage"] == "password_change"

    change = client.post(
        "/api/auth/first-login/change-password",
        headers=auth_headers(token),
        json={"newPassword": "AnotherPass!678"},
    )
    # MFA was preserved, so the next step is to verify the existing authenticator.
    assert change.get_json()["data"]["stage"] == "mfa"
    verify = client.post(
        "/api/auth/first-login/totp/verify",
        headers=auth_headers(token),
        json={"code": pyotp.TOTP(secret).now()},
    )
    assert verify.status_code == 200
    assert verify.get_json()["data"]["stage"] == "authenticated"


def test_disable_then_enable_account(client, admin_token):
    created = _create_user(client, admin_token, "toggler", "toggler@test.local")
    uid = created["id"]
    assert client.delete(f"/api/users/{uid}", headers=auth_headers(admin_token)).status_code == 200
    # Disabled account cannot log in.
    login = client.post("/api/auth/login", json={"username": "toggler", "password": "whatever"})
    assert login.status_code == 403
    # Re-enable.
    enable = client.post(f"/api/users/{uid}/enable", headers=auth_headers(admin_token))
    assert enable.status_code == 200
    assert enable.get_json()["data"]["isActive"] is True


def test_totp_secret_never_exposed_after_enrolment(client, admin_token):
    _fully_onboard(client, admin_token, "nosecret", "nosecret@test.local")
    users = client.get("/api/users", headers=auth_headers(admin_token)).get_json()["data"]["items"]
    row = next(u for u in users if u["username"] == "nosecret")
    assert "totpSecret" not in row
    assert "totp_secret" not in row
    assert row["mfaEnabled"] is True
