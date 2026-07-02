"""End-to-end tests for the first-login onboarding + MFA authentication flow."""

from datetime import datetime, timedelta, timezone

import pyotp

from tests.conftest import auth_headers


def _viewer_role_id(client, admin_token):
    roles = client.get("/api/roles", headers=auth_headers(admin_token)).get_json()["data"]["items"]
    return next(r for r in roles if r["name"] == "viewer")["id"]


def _create_user(client, admin_token, username="newhire", email="newhire@test.local"):
    role_id = _viewer_role_id(client, admin_token)
    resp = client.post(
        "/api/users",
        headers=auth_headers(admin_token),
        json={
            "username": username,
            "email": email,
            "fullName": "New Hire",
            "roleId": role_id,
            "clusterAccess": ["prod-us-east"],
        },
    )
    assert resp.status_code == 201, resp.get_json()
    return resp.get_json()["data"]


def test_admin_create_user_generates_temp_password_no_manual_password(client, admin_token):
    data = _create_user(client, admin_token)
    # Admin never sets a password; SMTP is not configured under tests, so the
    # plaintext temporary password is returned for out-of-band delivery.
    assert data["mustChangePassword"] is True
    assert data["mfaEnabled"] is False
    assert data["firstLoginCompleted"] is False
    assert data["temporaryPasswordEmailed"] is False
    assert data.get("temporaryPassword")
    assert data["temporaryPasswordExpiresAt"]


def test_create_user_requires_email(client, admin_token):
    role_id = _viewer_role_id(client, admin_token)
    resp = client.post(
        "/api/users",
        headers=auth_headers(admin_token),
        json={"username": "noemail", "roleId": role_id},
    )
    assert resp.status_code == 400


def _full_onboarding(client, admin_token, username="newhire", email="newhire@test.local"):
    """Run a user through the entire first-login flow; return their access token."""
    created = _create_user(client, admin_token, username, email)
    temp_password = created["temporaryPassword"]

    # Step 1: log in with the temporary password -> onboarding stage.
    login = client.post("/api/auth/login", json={"username": username, "password": temp_password})
    assert login.status_code == 200
    login_data = login.get_json()["data"]
    assert login_data["stage"] == "password_change"
    onboarding_token = login_data["onboardingToken"]

    # The onboarding token must NOT unlock protected endpoints.
    assert client.get("/api/auth/me", headers=auth_headers(onboarding_token)).status_code in (401, 403)
    assert client.get("/api/users", headers=auth_headers(onboarding_token)).status_code in (401, 403)

    # Step 2: change the temporary password -> mfa_setup stage.
    change = client.post(
        "/api/auth/first-login/change-password",
        headers=auth_headers(onboarding_token),
        json={"newPassword": "BrandNewPass!234"},
    )
    assert change.status_code == 200
    assert change.get_json()["data"]["stage"] == "mfa_setup"

    # Step 3: set up TOTP -> secret + QR.
    setup = client.post("/api/auth/first-login/totp/setup", headers=auth_headers(onboarding_token))
    assert setup.status_code == 200
    setup_data = setup.get_json()["data"]
    secret = setup_data["secret"]
    assert setup_data["otpauthUri"].startswith("otpauth://totp/")
    assert setup_data["qrDataUri"].startswith("data:image/png;base64,")

    # Step 4: a wrong code is rejected.
    bad = client.post(
        "/api/auth/first-login/totp/verify",
        headers=auth_headers(onboarding_token),
        json={"code": "000000"},
    )
    assert bad.status_code == 400

    # Step 5: the correct code completes onboarding and logs the user in.
    code = pyotp.TOTP(secret).now()
    verify = client.post(
        "/api/auth/first-login/totp/verify",
        headers=auth_headers(onboarding_token),
        json={"code": code},
    )
    assert verify.status_code == 200
    verify_data = verify.get_json()["data"]
    assert verify_data["stage"] == "authenticated"
    assert verify_data["user"]["mfaEnabled"] is True
    assert verify_data["user"]["firstLoginCompleted"] is True
    return verify_data["token"], secret, temp_password


def test_first_login_full_flow(client, admin_token):
    token, _secret, _temp = _full_onboarding(client, admin_token)
    # The freshly issued access token works on protected endpoints.
    me = client.get("/api/auth/me", headers=auth_headers(token))
    assert me.status_code == 200
    assert me.get_json()["data"]["username"] == "newhire"


def test_temporary_password_single_use_after_change(client, admin_token):
    created = _create_user(client, admin_token, "singleuse", "singleuse@test.local")
    temp_password = created["temporaryPassword"]
    login = client.post("/api/auth/login", json={"username": "singleuse", "password": temp_password})
    onboarding_token = login.get_json()["data"]["onboardingToken"]
    client.post(
        "/api/auth/first-login/change-password",
        headers=auth_headers(onboarding_token),
        json={"newPassword": "PermanentPass!99"},
    )
    # The temporary password no longer works once it has been replaced.
    again = client.post("/api/auth/login", json={"username": "singleuse", "password": temp_password})
    assert again.status_code == 401


def test_normal_login_requires_mfa_after_enrolment(client, admin_token):
    _token, secret, _temp = _full_onboarding(client, admin_token, "mfauser", "mfauser@test.local")

    # Password alone yields an MFA challenge, not an access token.
    login = client.post("/api/auth/login", json={"username": "mfauser", "password": "BrandNewPass!234"})
    assert login.status_code == 200
    data = login.get_json()["data"]
    assert data["stage"] == "mfa_challenge"
    mfa_token = data["mfaToken"]

    # Wrong code rejected; correct code logs in.
    assert (
        client.post("/api/auth/mfa/verify", headers=auth_headers(mfa_token), json={"code": "000000"}).status_code
        == 400
    )
    code = pyotp.TOTP(secret).now()
    verify = client.post("/api/auth/mfa/verify", headers=auth_headers(mfa_token), json={"code": code})
    assert verify.status_code == 200
    assert verify.get_json()["data"]["stage"] == "authenticated"


def test_expired_temporary_password_blocks_login(client, admin_token):
    from api.db import db
    from api.models import User

    created = _create_user(client, admin_token, "expired", "expired@test.local")
    temp_password = created["temporaryPassword"]
    user = User.query.filter_by(username="expired").first()
    user.temporary_password_expires_at = datetime.now(timezone.utc) - timedelta(hours=1)
    db.session.commit()

    resp = client.post("/api/auth/login", json={"username": "expired", "password": temp_password})
    assert resp.status_code == 403
    assert "expired" in resp.get_json()["error"].lower()


def test_account_lockout_after_repeated_failures(client, admin_token):
    _create_user(client, admin_token, "locky", "locky@test.local")
    # Default lockout threshold is 5 failed attempts.
    for _ in range(5):
        client.post("/api/auth/login", json={"username": "locky", "password": "wrong-password"})
    resp = client.post("/api/auth/login", json={"username": "locky", "password": "wrong-password"})
    assert resp.status_code == 423
    assert "locked" in resp.get_json()["error"].lower()


def test_admin_resend_temporary_password(client, admin_token):
    created = _create_user(client, admin_token, "resend", "resend@test.local")
    user_id = created["id"]
    resp = client.post(
        f"/api/users/{user_id}/resend-temporary-password",
        headers=auth_headers(admin_token),
    )
    assert resp.status_code == 200
    data = resp.get_json()["data"]
    assert data.get("temporaryPassword")
    # The re-issued temporary password lets the user start onboarding again.
    login = client.post(
        "/api/auth/login",
        json={"username": "resend", "password": data["temporaryPassword"]},
    )
    assert login.status_code == 200
    assert login.get_json()["data"]["stage"] == "password_change"


def test_admin_reset_mfa_forces_reenrolment(client, admin_token):
    _token, _secret, _temp = _full_onboarding(client, admin_token, "resetmfa", "resetmfa@test.local")
    from api.models import User

    user_id = User.query.filter_by(username="resetmfa").first().id
    resp = client.post(f"/api/users/{user_id}/reset-mfa", headers=auth_headers(admin_token))
    assert resp.status_code == 200

    # After reset, a normal password login routes back into MFA setup.
    login = client.post("/api/auth/login", json={"username": "resetmfa", "password": "BrandNewPass!234"})
    assert login.status_code == 200
    assert login.get_json()["data"]["stage"] == "mfa_setup"


def test_login_by_email_identifier(client, admin_token):
    _token, secret, _temp = _full_onboarding(client, admin_token, "emaillogin", "emaillogin@test.local")
    login = client.post(
        "/api/auth/login",
        json={"username": "emaillogin@test.local", "password": "BrandNewPass!234"},
    )
    assert login.status_code == 200
    assert login.get_json()["data"]["stage"] == "mfa_challenge"
