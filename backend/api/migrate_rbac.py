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


def _prune_obsolete_permissions() -> None:
    """Delete permission rows whose key is no longer defined in rbac_data.

    Permission keys are fully owned by ``rbac_data.PERMISSIONS`` (there is no UI to
    invent new ones), so any DB permission not in that list is a leftover from a
    removed feature (e.g. the old ``network:view``). Pruning keeps the Roles editor
    catalog clean and prevents stale keys leaking into an "Other" group. Idempotent.
    """
    from .models import Permission, Role
    from .rbac_data import ALL_PERMISSION_KEYS

    valid = set(ALL_PERMISSION_KEYS)
    obsolete = [perm for perm in Permission.query.all() if perm.key not in valid]
    if not obsolete:
        return

    obsolete_ids = {perm.id for perm in obsolete}
    # Detach the obsolete permissions from every role first (association rows).
    for role in Role.query.all():
        kept = [perm for perm in role.permissions if perm.id not in obsolete_ids]
        if len(kept) != len(role.permissions):
            role.permissions = kept
    for perm in obsolete:
        db.session.delete(perm)
    db.session.commit()


def _sync_role_permissions() -> None:
    """Ensure every role has all permissions defined for it in ROLE_DEFINITIONS.

    This is idempotent: it only ever adds missing permissions, never removes existing ones.
    Called on every startup so that new permissions added to rbac_data.py automatically
    propagate to existing deployments without manual DB surgery.
    """
    from .models import Role, Permission
    from .rbac_data import ROLE_DEFINITIONS

    for role_name, defn in ROLE_DEFINITIONS.items():
        role = Role.query.filter_by(name=role_name).first()
        if not role:
            continue
        current_keys = {p.key for p in role.permissions}
        needed_keys = set(defn["permissions"]) - current_keys
        if not needed_keys:
            continue
        new_perms = Permission.query.filter(Permission.key.in_(needed_keys)).all()
        for perm in new_perms:
            role.permissions.append(perm)
    db.session.commit()


def _migrate_app_service_deployments() -> None:
    _add_column_if_missing(
        "application_service_deployments",
        "resource_kind",
        "VARCHAR(20) NOT NULL DEFAULT 'deployment'",
    )
    # Optional DR counterpart columns for each linked component.
    for col, sql_type in [
        ("dr_cluster_id", "VARCHAR(120)"),
        ("dr_namespace", "VARCHAR(253)"),
        ("dr_resource_name", "VARCHAR(253)"),
        ("dr_resource_kind", "VARCHAR(20)"),
    ]:
        _add_column_if_missing("application_service_deployments", col, sql_type)


def _migrate_app_service_topology_positions() -> None:
    _add_column_if_missing("application_service_topology_nodes", "position_x", "FLOAT")
    _add_column_if_missing("application_service_topology_nodes", "position_y", "FLOAT")
    # Optional reference to a predefined TopologyComponent.
    _add_column_if_missing("application_service_topology_nodes", "component_id", "INTEGER")


def _migrate_topology_components() -> None:
    """Forward-compatible column adds for the topology_components table.

    The table itself is created by ``db.create_all()``; this only backfills
    columns added after it first shipped. Idempotent and safe on a fresh DB.
    """
    if "topology_components" not in inspect(db.engine).get_table_names():
        return
    for col, sql_type in [
        ("category", "VARCHAR(80)"),
        ("description", "TEXT"),
        ("check_type", "VARCHAR(16) NOT NULL DEFAULT 'none'"),
        ("health_check_url", "VARCHAR(512)"),
        ("tcp_host", "VARCHAR(253)"),
        ("tcp_port", "INTEGER"),
        ("webhook_token", "VARCHAR(64)"),
        ("heartbeat_interval_seconds", "INTEGER DEFAULT 300"),
        ("last_heartbeat_at", "DATETIME"),
        ("last_status", "VARCHAR(16)"),
        ("last_message", "TEXT"),
        ("last_checked_at", "DATETIME"),
        ("created_by", "INTEGER"),
    ]:
        _add_column_if_missing("topology_components", col, sql_type)


def _migrate_app_service_topology_edge_meta() -> None:
    _add_column_if_missing("application_service_topology_edges", "protocol", "VARCHAR(20)")
    _add_column_if_missing("application_service_topology_edges", "scope", "VARCHAR(20)")
    _add_column_if_missing("application_service_topology_edges", "description", "TEXT")


def _migrate_deployment_request_columns() -> None:
    table_names = inspect(db.engine).get_table_names()
    if "deployment_requests" in table_names:
        _add_column_if_missing("deployment_requests", "required_approvals", "INTEGER DEFAULT 1")
        _add_column_if_missing("deployment_requests", "total_recipients", "INTEGER DEFAULT 1")
        _add_column_if_missing("deployment_requests", "requested_window_start", "DATETIME")
        _add_column_if_missing("deployment_requests", "requested_window_end", "DATETIME")
        _add_column_if_missing("deployment_requests", "requested_window_timezone", "VARCHAR(64)")
    if "deployment_request_settings" in table_names:
        _add_column_if_missing("deployment_request_settings", "group_ids", "JSON")
        _add_column_if_missing("deployment_request_settings", "required_approvals", "INTEGER DEFAULT 1")
        _add_column_if_missing("deployment_request_settings", "cluster_required_approvals", "JSON")


def _migrate_change_bundle_columns() -> None:
    """Forward-compatible column adds for change bundles (tables come from create_all)."""
    table_names = inspect(db.engine).get_table_names()
    if "change_bundles" in table_names:
        _add_column_if_missing("change_bundles", "stop_on_failure", "INTEGER DEFAULT 1")
        _add_column_if_missing("change_bundles", "execution_started_at", "DATETIME")
        _add_column_if_missing("change_bundles", "execution_finished_at", "DATETIME")
        _add_column_if_missing("change_bundles", "rejection_reason", "TEXT")
        _add_column_if_missing("change_bundles", "requested_window_timezone", "VARCHAR(64)")
    if "change_bundle_items" in table_names:
        _add_column_if_missing("change_bundle_items", "cluster_name", "VARCHAR(255)")
        _add_column_if_missing("change_bundle_items", "validation_message", "TEXT")
        _add_column_if_missing("change_bundle_items", "execution_result", "JSON")


def _migrate_service_catalog_columns() -> None:
    """Forward-compatible column adds for the Service Catalog tables.

    The tables themselves are created by ``db.create_all()``; this only backfills
    columns added after a table first shipped, mirroring the other migrators.
    Idempotent and safe on a fresh database.
    """
    table_names = inspect(db.engine).get_table_names()
    if "service_blueprint_components" in table_names:
        for col, sql_type in [
            ("supports_external", "BOOLEAN DEFAULT 0"),
            ("default_template_id", "VARCHAR(120)"),
            ("default_port", "INTEGER"),
            ("default_resources", "JSON"),
            ("default_health", "JSON"),
            ("default_hpa", "JSON"),
            ("position_x", "FLOAT"),
            ("position_y", "FLOAT"),
            ("position", "INTEGER DEFAULT 0"),
        ]:
            _add_column_if_missing("service_blueprint_components", col, sql_type)
    if "app_services" in table_names:
        for col, sql_type in [
            ("slug", "VARCHAR(180)"),
            ("description", "TEXT"),
            ("created_by_user_id", "INTEGER"),
            ("application_service_id", "INTEGER"),
        ]:
            _add_column_if_missing("app_services", col, sql_type)
    if "app_service_component_mappings" in table_names:
        for col, sql_type in [
            ("component_name", "VARCHAR(120)"),
            ("component_role", "VARCHAR(120)"),
            ("generated_name", "VARCHAR(253)"),
            ("labels", "JSON"),
            ("config", "JSON"),
        ]:
            _add_column_if_missing("app_service_component_mappings", col, sql_type)


def _migrate_registry_connection_columns() -> None:
    """Forward-compatible column adds for the registry_connections table.

    The table itself is created by ``db.create_all()``; this only backfills
    columns added after it first shipped, mirroring the other migrators.
    Idempotent and safe on a fresh database.
    """
    if "registry_connections" not in inspect(db.engine).get_table_names():
        return
    for col, sql_type in [
        ("registry_type", "VARCHAR(32) DEFAULT 'nexus'"),
        ("auth_mode", "VARCHAR(16) DEFAULT 'basic'"),
        ("verify_tls", "BOOLEAN DEFAULT 1"),
        ("ca_cert", "TEXT"),
        ("enforcement", "VARCHAR(8) DEFAULT 'block'"),
        ("enabled", "BOOLEAN DEFAULT 1"),
        ("last_test_at", "DATETIME"),
        ("last_test_status", "VARCHAR(16)"),
        ("last_test_message", "TEXT"),
    ]:
        _add_column_if_missing("registry_connections", col, sql_type)


def _migrate_alert_routing_user_receivers() -> None:
    """Add user/role linkage to receivers and migrate static emails to users."""
    if "alert_routing_receivers" not in inspect(db.engine).get_table_names():
        return
    _add_column_if_missing("alert_routing_receivers", "user_id", "INTEGER")
    _add_column_if_missing("alert_routing_receivers", "role_id", "INTEGER")

    # Backfill: link existing static-email receivers to a matching active user by
    # email and promote them to 'user' receivers. Unmatched ones stay as legacy.
    from .models import AlertRoutingReceiver, User

    pending = (
        AlertRoutingReceiver.query.filter(
            AlertRoutingReceiver.receiver_type == "email",
            AlertRoutingReceiver.user_id.is_(None),
            AlertRoutingReceiver.email_address.isnot(None),
        ).all()
    )
    changed = False
    for receiver in pending:
        address = (receiver.email_address or "").strip().lower()
        if not address:
            continue
        user = User.query.filter(db.func.lower(User.email) == address).first()
        if user:
            receiver.user_id = user.id
            receiver.receiver_type = "user"
            changed = True
    if changed:
        db.session.commit()


def run_migrations() -> None:
    db.create_all()
    _migrate_deployment_request_columns()
    _migrate_change_bundle_columns()
    _migrate_alert_routing_user_receivers()
    _migrate_clusters_table()
    _migrate_app_catalog_helm_columns()
    _migrate_alert_policy_evaluation_columns()
    _migrate_alert_delivery_log_group_column()
    _migrate_log_alert_columns()
    _migrate_legacy_users()
    _migrate_app_service_deployments()
    _migrate_app_service_topology_positions()
    _migrate_app_service_topology_edge_meta()
    _migrate_topology_components()
    _migrate_service_catalog_columns()
    _migrate_registry_connection_columns()
    from .access_rules import migrate_all_users_legacy_rules
    from .migrate_alert_routing import run_alert_routing_migrations

    migrate_all_users_legacy_rules()
    run_alert_routing_migrations()
    _sync_role_permissions()
    _prune_obsolete_permissions()
