"""First-boot settings a deployment (the Helm chart) hands the seed.

KUBESIGHT_INITIAL_ADMIN_PASSWORD, KUBESIGHT_SEED_DEMO_USERS and
KUBESIGHT_HERMES_MCP_TOKEN only change what an empty database is seeded with;
an installation that sets none of them is seeded exactly as before.
"""

from hashlib import sha256

from api.db import db
from api.models import ApiToken, User
from api.passwords import verify_password
from api.seed import DEPLOYMENT_HERMES_TOKEN_NAME, seed_defaults
from tests.conftest import _reset_database

TOKEN_A = "ksa_" + "a" * 40
TOKEN_B = "ksa_" + "b" * 40


def _hermes_tokens():
    hermes = User.query.filter_by(username="hermes-agent").one()
    return ApiToken.query.filter_by(user_id=hermes.id).order_by(ApiToken.id).all()


def test_initial_admin_password_replaces_the_default_and_forces_onboarding(app, client, monkeypatch):
    monkeypatch.setenv("KUBESIGHT_INITIAL_ADMIN_PASSWORD", "Start-Here-9472")
    _reset_database()

    admin = User.query.filter_by(username="admin").one()
    assert verify_password("Start-Here-9472", admin.password_hash)
    assert not verify_password("admin123", admin.password_hash)
    assert admin.must_change_password is True
    assert admin.first_login_completed is False

    response = client.post("/api/auth/login", json={"username": "admin", "password": "Start-Here-9472"})
    assert response.status_code == 200
    data = response.get_json()["data"]
    assert data["stage"] == "password_change"
    assert "token" not in data
    assert client.post("/api/auth/login", json={"username": "admin", "password": "admin123"}).status_code == 401


def test_initial_admin_password_never_touches_an_existing_admin(app, monkeypatch):
    monkeypatch.setenv("KUBESIGHT_INITIAL_ADMIN_PASSWORD", "Start-Here-9472")
    seed_defaults()
    db.session.commit()

    admin = User.query.filter_by(username="admin").one()
    assert verify_password("admin123", admin.password_hash)
    assert admin.must_change_password is False


def test_demo_users_can_be_left_out(app, monkeypatch):
    monkeypatch.setenv("KUBESIGHT_SEED_DEMO_USERS", "false")
    _reset_database()

    assert User.query.filter_by(username="viewer").first() is None
    assert User.query.filter_by(username="operator").first() is None
    assert User.query.filter_by(username="admin").first() is not None
    hermes = User.query.filter_by(username="hermes-agent").one()
    assert hermes.is_service_account is True


def test_demo_users_are_seeded_by_default(app):
    assert User.query.filter_by(username="viewer").first() is not None
    assert User.query.filter_by(username="operator").first() is not None


def test_deployment_token_is_registered_for_hermes_once(app, monkeypatch):
    monkeypatch.setenv("KUBESIGHT_HERMES_MCP_TOKEN", TOKEN_A)
    _reset_database()
    seed_defaults()
    db.session.commit()

    tokens = _hermes_tokens()
    assert len(tokens) == 1
    assert tokens[0].token_hash == sha256(TOKEN_A.encode()).hexdigest()
    assert tokens[0].name == DEPLOYMENT_HERMES_TOKEN_NAME
    assert tokens[0].token_prefix == TOKEN_A[:12]
    assert tokens[0].is_active is True


def test_deployment_token_authenticates_as_hermes(app, client, monkeypatch):
    monkeypatch.setenv("KUBESIGHT_HERMES_MCP_TOKEN", TOKEN_A)
    _reset_database()

    response = client.get("/api/auth/me", headers={"Authorization": f"Bearer {TOKEN_A}"})
    assert response.status_code == 200
    assert response.get_json()["data"]["username"] == "hermes-agent"


def test_a_new_deployment_token_retires_the_old_one(app, monkeypatch):
    monkeypatch.setenv("KUBESIGHT_HERMES_MCP_TOKEN", TOKEN_A)
    _reset_database()
    monkeypatch.setenv("KUBESIGHT_HERMES_MCP_TOKEN", TOKEN_B)
    seed_defaults()
    db.session.commit()

    by_hash = {t.token_hash: t for t in _hermes_tokens()}
    assert by_hash[sha256(TOKEN_A.encode()).hexdigest()].is_active is False
    assert by_hash[sha256(TOKEN_B.encode()).hexdigest()].is_active is True


def test_a_revoked_deployment_token_stays_revoked(app, monkeypatch):
    monkeypatch.setenv("KUBESIGHT_HERMES_MCP_TOKEN", TOKEN_A)
    _reset_database()
    token = _hermes_tokens()[0]
    token.is_active = False
    db.session.commit()

    seed_defaults()
    db.session.commit()

    tokens = _hermes_tokens()
    assert len(tokens) == 1
    assert tokens[0].is_active is False


def test_a_malformed_deployment_token_is_ignored(app, monkeypatch):
    monkeypatch.setenv("KUBESIGHT_HERMES_MCP_TOKEN", "not-a-ksa-token")
    _reset_database()

    assert _hermes_tokens() == []
