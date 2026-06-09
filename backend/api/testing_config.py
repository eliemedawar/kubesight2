"""Flask configuration for pytest — never used by production app.py."""

from __future__ import annotations

import os
import sys

DEFAULT_SQLITE_TEST_URI = "sqlite:///:memory:"
DEFAULT_TEST_JWT_SECRET = "test-secret-key-for-pytest-only-32chars"


def normalize_database_url(url: str) -> str:
    """Normalize postgres:// to postgresql:// for SQLAlchemy."""
    cleaned = (url or "").strip()
    if cleaned.startswith("postgres://"):
        return cleaned.replace("postgres://", "postgresql://", 1)
    return cleaned


def resolve_test_database_uri() -> str:
    """
    Test DB selection:
    - TEST_DATABASE_URL if set (PostgreSQL or other)
    - else in-memory SQLite
    """
    test_url = os.getenv("TEST_DATABASE_URL", "").strip()
    if test_url:
        return normalize_database_url(test_url)
    return DEFAULT_SQLITE_TEST_URI


def refuse_production_tests() -> None:
    """Abort pytest if the environment looks like production."""
    for env_key in ("FLASK_ENV", "APP_ENV"):
        value = os.getenv(env_key, "").strip().lower()
        if value == "production":
            print(
                f"Refusing to run tests: {env_key}=production. "
                "Unset it or use a non-production environment.",
                file=sys.stderr,
            )
            raise SystemExit(1)

    database_url = os.getenv("DATABASE_URL", "").strip().lower()
    test_url = os.getenv("TEST_DATABASE_URL", "").strip()
    if not test_url and database_url and "kubesight_test" not in database_url:
        if any(
            marker in database_url
            for marker in ("prod", "production", "kubesight_prod")
        ):
            print(
                "Refusing to run tests: DATABASE_URL looks like production and "
                "TEST_DATABASE_URL is not set.",
                file=sys.stderr,
            )
            raise SystemExit(1)


def apply_test_environment() -> str:
    """
    Force safe test env vars before create_app / load_dotenv.
    Returns the resolved test database URI.
    """
    refuse_production_tests()
    uri = resolve_test_database_uri()
    os.environ["K8S_REAL_MODE"] = "false"
    os.environ["AUTH_REQUIRED"] = "true"
    os.environ["JWT_SECRET_KEY"] = os.getenv("JWT_SECRET_KEY", DEFAULT_TEST_JWT_SECRET)
    # Never let production DATABASE_URL reach the app during tests.
    os.environ["DATABASE_URL"] = uri
    return uri


class TestingConfig:
    TESTING = True
    SQLALCHEMY_TRACK_MODIFICATIONS = False
    SQLALCHEMY_DATABASE_URI = resolve_test_database_uri()
    JWT_SECRET_KEY = os.getenv("JWT_SECRET_KEY", DEFAULT_TEST_JWT_SECRET)
    # Documented for operators; k8s_provider reads K8S_REAL_MODE from os.environ.
    K8S_REAL_MODE = "false"
