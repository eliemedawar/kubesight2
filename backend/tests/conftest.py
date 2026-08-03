import os
import sys

import pytest

BACKEND_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if BACKEND_ROOT not in sys.path:
    sys.path.insert(0, BACKEND_ROOT)

from api.testing_config import TestingConfig, apply_test_environment, resolve_test_database_uri

# Safe test env before create_app (and load_dotenv) runs.
TEST_DATABASE_URI = apply_test_environment()

from api import create_app
from api.auth_utils import auth_required_enabled
from api.db import db
from api.migrate_rbac import run_migrations
from api.seed import seed_defaults


def pytest_configure(config):
    """Show which database backend tests will use."""
    uri = resolve_test_database_uri()
    if uri.startswith("sqlite"):
        config._kubesight_test_db = "sqlite (in-memory)"
    else:
        # Mask credentials in log output.
        safe = uri.split("@")[-1] if "@" in uri else uri
        config._kubesight_test_db = f"postgresql ({safe})"


def pytest_report_header(config):
    backend = getattr(config, "_kubesight_test_db", resolve_test_database_uri())
    return f"KubeSight test database: {backend}"


def _reset_database():
    """Drop and recreate schema; seed defaults. Order-independent clean slate."""
    db.session.remove()
    db.drop_all()
    db.create_all()
    run_migrations()
    seed_defaults()
    db.session.commit()


@pytest.fixture()
def app():
    os.environ["AUTH_REQUIRED"] = "true"
    application = create_app(TestingConfig)
    assert application.config["TESTING"] is True
    assert auth_required_enabled()
    assert application.config["SQLALCHEMY_DATABASE_URI"] == TEST_DATABASE_URI

    with application.app_context():
        _reset_database()
        try:
            yield application
        finally:
            db.session.remove()
            db.drop_all()


@pytest.fixture()
def client(app):
    return app.test_client()


def _login_token(app, username: str, password: str) -> str:
    """Create a bearer token without authenticating the test's shared client."""
    with app.test_client() as login_client:
        response = login_client.post(
            "/api/auth/login",
            json={"username": username, "password": password},
        )
    assert response.status_code == 200
    payload = response.get_json()
    assert payload["success"] is True
    assert payload["data"]["user"]["username"] == username
    return payload["data"]["token"]


@pytest.fixture()
def admin_token(app):
    return _login_token(app, "admin", "admin123")


@pytest.fixture()
def viewer_token(app):
    return _login_token(app, "viewer", "viewer123")


@pytest.fixture()
def operator_token(app):
    return _login_token(app, "operator", "operator123")


def auth_headers(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}
