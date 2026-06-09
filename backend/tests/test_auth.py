from api.passwords import hash_password, verify_password
from tests.conftest import auth_headers


def test_password_hashing_roundtrip():
    hashed = hash_password("secret-pass")
    assert hashed != "secret-pass"
    assert verify_password("secret-pass", hashed)
    assert not verify_password("wrong", hashed)


def test_login_success(client, admin_token):
    assert admin_token


def test_login_failure(client):
    response = client.post(
        "/api/auth/login",
        json={"username": "admin", "password": "wrong-password"},
    )
    assert response.status_code == 401
    payload = response.get_json()
    assert payload["success"] is False


def test_me_requires_auth(client):
    response = client.get("/api/auth/me")
    assert response.status_code in (401, 403)


def test_me_with_token(client, admin_token):
    response = client.get("/api/auth/me", headers=auth_headers(admin_token))
    assert response.status_code == 200
    payload = response.get_json()
    assert payload["success"] is True
    assert payload["data"]["username"] == "admin"
    assert payload["data"]["isAdmin"] is True
