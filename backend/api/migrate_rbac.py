"""Idempotent migration from legacy User schema to RBAC."""

from __future__ import annotations

from sqlalchemy import inspect, text

from .db import db
from .passwords import hash_password


def _table_columns(table_name: str) -> set:
    inspector = inspect(db.engine)
    if table_name not in inspector.get_table_names():
        return set()
    return {col["name"] for col in inspector.get_columns(table_name)}


def _add_column_if_missing(table_name: str, col: str, sql_type: str) -> None:
    cols = _table_columns(table_name)
    if col in cols:
        return
    with db.engine.begin() as conn:
        conn.execute(text(f"ALTER TABLE {table_name} ADD COLUMN {col} {sql_type}"))


def _drop_column_if_exists(table_name: str, col: str) -> None:
    cols = _table_columns(table_name)
    if col not in cols:
        return
    try:
        with db.engine.begin() as conn:
            conn.execute(text(f"ALTER TABLE {table_name} DROP COLUMN {col}"))
    except Exception:
        # Older SQLite builds may not support DROP COLUMN; leave column in place.
        pass


def _drop_obsolete_user_columns() -> None:
    """Remove pre-RBAC columns after password_hash migration (fixes NOT NULL password)."""
    user_cols = _table_columns("users")
    if "password_hash" not in user_cols:
        return
    for col in ("password", "display_name", "roles"):
        _drop_column_if_exists("users", col)


def _migrate_legacy_users() -> None:
    user_cols = _table_columns("users")
    if not user_cols:
        return

    if "password" in user_cols and "password_hash" not in user_cols:
        _add_column_if_missing("users", "password_hash", "VARCHAR(255)")

    for col, sql_type in [
        ("email", "VARCHAR(255)"),
        ("full_name", "VARCHAR(255)"),
        ("role_id", "INTEGER"),
        ("is_active", "INTEGER"),
        ("created_at", "DATETIME"),
        ("updated_at", "DATETIME"),
        ("last_login_at", "DATETIME"),
    ]:
        _add_column_if_missing("users", col, sql_type)

    user_cols = _table_columns("users")

    if "password" in user_cols and "password_hash" in user_cols:
        from .models import Role

        rows = db.session.execute(
            text("SELECT id, username, password, display_name, roles FROM users")
        ).mappings().all()
        for row in rows:
            role_name = "viewer"
            roles_json = row.get("roles")
            if isinstance(roles_json, list) and "admin" in roles_json:
                role_name = "admin"
            role = Role.query.filter_by(name=role_name).first()
            updates = {
                "password_hash": hash_password(row["password"] or ""),
                "full_name": row.get("display_name") or row["username"],
                "email": f"{row['username']}@kubesight.local",
                "is_active": 1,
                "role_id": role.id if role else None,
            }
            set_clause = ", ".join(f"{key} = :{key}" for key in updates)
            db.session.execute(
                text(f"UPDATE users SET {set_clause} WHERE id = :id"),
                {**updates, "id": row["id"]},
            )
        db.session.commit()

    user_cols = _table_columns("users")
    if "display_name" in user_cols and "full_name" in user_cols:
        db.session.execute(
            text(
                "UPDATE users SET full_name = display_name "
                "WHERE (full_name IS NULL OR full_name = '') AND display_name IS NOT NULL"
            )
        )
        db.session.commit()

    if "role_id" in user_cols:
        from .models import Role, User

        for user in User.query.all():
            if user.role_id:
                continue
            role_name = "admin" if user.username == "admin" else "viewer"
            role = Role.query.filter_by(name=role_name).first()
            if role:
                user.role_id = role.id
        db.session.commit()

    _drop_obsolete_user_columns()


def _migrate_clusters_table() -> None:
    if "clusters" not in inspect(db.engine).get_table_names():
        return
    for col, sql_type in [
        ("connection_method", "VARCHAR(32)"),
        ("authentication_type", "VARCHAR(32)"),
        ("skip_tls_verify", "INTEGER"),
        ("connection_timeout_seconds", "INTEGER"),
    ]:
        _add_column_if_missing("clusters", col, sql_type)


def _migrate_alert_policy_evaluation_columns() -> None:
    if "alert_policies" not in inspect(db.engine).get_table_names():
        return
    _add_column_if_missing("alert_policies", "evaluation_interval_seconds", "INTEGER DEFAULT 300")
    _add_column_if_missing("alert_policies", "last_evaluated_at", "DATETIME")
    _add_column_if_missing("alert_policies", "last_evaluation_result", "VARCHAR(16)")
    _add_column_if_missing("alert_policies", "last_measured_value", "VARCHAR(255)")
    _add_column_if_missing("alert_policies", "last_threshold", "VARCHAR(64)")
    _add_column_if_missing("alert_policies", "last_evaluation_error", "TEXT")


def _migrate_alert_delivery_log_group_column() -> None:
    if "alert_delivery_logs" not in inspect(db.engine).get_table_names():
        return
    _add_column_if_missing("alert_delivery_logs", "group_name", "VARCHAR(120)")


def _migrate_app_catalog_helm_columns() -> None:
    if "app_catalog_entries" not in inspect(db.engine).get_table_names():
        return
    for col, sql_type in [
        ("release_name", "VARCHAR(253)"),
        ("chart_name", "VARCHAR(253)"),
        ("chart_version", "VARCHAR(64)"),
        ("app_version", "VARCHAR(64)"),
        ("helm_revision", "INTEGER"),
    ]:
        _add_column_if_missing("app_catalog_entries", col, sql_type)


def _migrate_log_alert_columns() -> None:
    if "alert_policies" in inspect(db.engine).get_table_names():
        _add_column_if_missing("alert_policies", "alert_type", "VARCHAR(16) DEFAULT 'metric'")
        _add_column_if_missing("alert_policies", "log_config", "JSON")
    if "alert_history" in inspect(db.engine).get_table_names():
        _add_column_if_missing("alert_history", "alert_type", "VARCHAR(16) DEFAULT 'metric'")
        _add_column_if_missing("alert_history", "log_snapshot", "JSON")
    if "alert_delivery_logs" in inspect(db.engine).get_table_names():
        _add_column_if_missing("alert_delivery_logs", "matched_pattern", "VARCHAR(512)")
        _add_column_if_missing("alert_delivery_logs", "pod_name", "VARCHAR(253)")
        _add_column_if_missing("alert_delivery_logs", "log_snippet", "TEXT")


def run_migrations() -> None:
    db.create_all()
    _migrate_clusters_table()
    _migrate_app_catalog_helm_columns()
    _migrate_alert_policy_evaluation_columns()
    _migrate_alert_delivery_log_group_column()
    _migrate_log_alert_columns()
    _migrate_legacy_users()
    from .access_rules import migrate_all_users_legacy_rules
    from .migrate_alert_routing import run_alert_routing_migrations

    migrate_all_users_legacy_rules()
    run_alert_routing_migrations()
