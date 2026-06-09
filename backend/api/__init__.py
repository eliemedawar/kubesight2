import logging
import os
import time

from dotenv import load_dotenv
from flask import Flask, request

load_dotenv()
from flask_cors import CORS
from sqlalchemy import create_engine
from sqlalchemy.exc import OperationalError

from .db import db
from .k8s_provider import is_real_mode_enabled
from .models import AppSettings, User
from .routes import register_blueprints
from .frontend_static import frontend_dist_available, register_frontend_static
from .response import success_response
from .migrate_rbac import run_migrations
from .seed import seed_defaults


def _is_production_env() -> bool:
    debug = os.getenv("FLASK_DEBUG", "true").strip().lower()
    if debug in {"1", "true", "yes", "on"}:
        return False
    for key in ("FLASK_ENV", "APP_ENV"):
        if os.getenv(key, "").strip().lower() == "production":
            return True
    return False


def _is_logs_fetch_path(path: str) -> bool:
    normalized = (path or "").rstrip("/")
    if normalized == "/api/logs":
        return True
    parts = normalized.split("/")
    return (
        len(parts) >= 10
        and parts[1:3] == ["api", "clusters"]
        and parts[4] == "namespaces"
        and parts[6] == "pods"
        and parts[8] == "containers"
        and parts[10] == "logs"
    )


class _SkipNoiseAccessLogFilter(logging.Filter):
    """Keep kubelet probes and log-viewer polling out of werkzeug access logs."""

    def filter(self, record: logging.LogRecord) -> bool:
        message = record.getMessage()
        if '"/health ' in message or '"/healthz ' in message:
            return False
        if '"/api/logs' in message:
            return False
        if "/containers/" in message and "/logs" in message and '"/api/clusters/' in message:
            return False
        return True


def _configure_access_log_filters() -> None:
    logging.getLogger("werkzeug").addFilter(_SkipNoiseAccessLogFilter())


def _configure_api_request_logging(app: Flask) -> None:
    """Log API requests in a concise format, excluding health probes."""
    api_logger = logging.getLogger("kubesight.api")

    @app.after_request
    def log_api_request(response):
        path = request.path or ""
        if not path.startswith("/api/"):
            return response
        if path in {"/health", "/healthz"} or _is_logs_fetch_path(path):
            return response
        duration_ms = 0
        if hasattr(request, "start_time"):
            duration_ms = int((time.perf_counter() - request.start_time) * 1000)
        api_logger.info(
            "%s %s %s %sms",
            request.method,
            path,
            response.status_code,
            duration_ms,
        )
        return response

    @app.before_request
    def mark_request_start():
        request.start_time = time.perf_counter()


def _configure_cors(app: Flask) -> None:
    raw = os.getenv("CORS_ORIGINS", "*").strip()
    if not raw or raw == "*":
        CORS(app)
        return
    origins = [origin.strip() for origin in raw.split(",") if origin.strip()]
    CORS(app, origins=origins or "*")


def create_app(config_object=None) -> Flask:
    app = Flask(__name__)
    is_testing = False

    if config_object is not None:
        app.config.from_object(config_object)
        is_testing = bool(app.config.get("TESTING"))
        database_url = app.config.get("SQLALCHEMY_DATABASE_URI") or "sqlite:///:memory:"
    else:
        database_url = os.getenv("DATABASE_URL", "sqlite:///kubesight.db")
        # Heroku-style URLs may use `postgres://`; SQLAlchemy expects `postgresql://`.
        if database_url.startswith("postgres://"):
            database_url = database_url.replace("postgres://", "postgresql://", 1)

        fallback_sqlite_url = "sqlite:///kubesight.db"
        if database_url != fallback_sqlite_url and not _is_production_env():
            try:
                with create_engine(database_url).connect():
                    pass
            except OperationalError:
                database_url = fallback_sqlite_url

        app.config["SQLALCHEMY_DATABASE_URI"] = database_url
        app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
        app.config["JWT_SECRET_KEY"] = os.getenv(
            "JWT_SECRET_KEY", "kubesight-dev-secret-change-me"
        )

    if "SQLALCHEMY_DATABASE_URI" not in app.config:
        app.config["SQLALCHEMY_DATABASE_URI"] = database_url
    if "JWT_SECRET_KEY" not in app.config:
        app.config["JWT_SECRET_KEY"] = os.getenv(
            "JWT_SECRET_KEY", "kubesight-dev-secret-change-me"
        )
    app.config.setdefault("SQLALCHEMY_TRACK_MODIFICATIONS", False)

    db.init_app(app)
    _configure_cors(app)
    _configure_access_log_filters()
    _configure_api_request_logging(app)

    register_blueprints(app)
    register_frontend_static(app)

    with app.app_context():
        if not is_testing:
            run_migrations()
            seed_defaults()

    @app.route("/health", methods=["GET"])
    def health():
        return success_response(
            {
                "status": "ok",
                "database": {
                    "users": User.query.count(),
                    "settingsRows": AppSettings.query.count(),
                },
                "kubernetesMode": "real" if is_real_mode_enabled() else "mock",
            }
        )

    if not frontend_dist_available():

        @app.route("/", methods=["GET"])
        def home():
            return success_response(
                {
                    "message": "Backend is running",
                    "ui": "Build frontend (cd frontend && npm run build) then open this URL in the browser.",
                }
            )

    return app
