"""Idempotent migration from legacy User schema to RBAC."""

from __future__ import annotations

import logging
import os
import re
from urllib.parse import urlsplit, urlunsplit

from sqlalchemy import inspect, text

from .db import db
from .passwords import hash_password

logger = logging.getLogger(__name__)


def _table_columns(table_name: str) -> set:
    inspector = inspect(db.engine)
    if table_name not in inspector.get_table_names():
        return set()
    return {col["name"] for col in inspector.get_columns(table_name)}


def _portable_type(sql_type: str) -> str:
    """Translate SQLite-flavored column types in raw DDL for the active dialect.

    These migrators hand-write ``ALTER TABLE ... ADD COLUMN`` with literal type
    strings. SQLite is permissive (``DATETIME`` and integer boolean defaults are
    fine), but PostgreSQL is strict: it has no ``DATETIME`` type. The model uses
    ``DateTime(timezone=True)`` (rendered as ``TIMESTAMP WITH TIME ZONE`` by
    ``create_all``), so map ``DATETIME`` accordingly on non-SQLite backends.
    """
    if db.engine.dialect.name == "sqlite":
        return sql_type
    return re.sub(r"\bDATETIME\b", "TIMESTAMP WITH TIME ZONE", sql_type, flags=re.IGNORECASE)


def _add_column_if_missing(table_name: str, col: str, sql_type: str) -> None:
    cols = _table_columns(table_name)
    if col in cols:
        return
    with db.engine.begin() as conn:
        conn.execute(text(f"ALTER TABLE {table_name} ADD COLUMN {col} {_portable_type(sql_type)}"))


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


def _migrate_cluster_build_columns() -> None:
    """Columns added after Cluster Builder's initial release.

    ``db.create_all`` creates new tables but deliberately does not alter an
    existing one, so deployed databases need this small idempotent migration.
    """
    _add_column_if_missing("cluster_builds", "addons_json", "JSON")
    _add_column_if_missing("cluster_builds", "workloads_json", "JSON")
    _add_column_if_missing("cluster_builds", "growth_started_at", "DATETIME")
    _add_column_if_missing("cluster_builds", "build_seconds", "INTEGER")
    _add_column_if_missing("cluster_builds", "execution_user_id", "INTEGER")
    _add_column_if_missing("cluster_builds", "disk_check_path", "VARCHAR(255)")
    for col, sql_type in [
        ("last_test_at", "DATETIME"),
        ("last_test_status", "VARCHAR(16)"),
        ("last_test_message", "TEXT"),
    ]:
        _add_column_if_missing("ssh_connection_profiles", col, sql_type)


def _sanitize_legacy_build_profile_proxies() -> None:
    """Remove obsolete proxy URL credentials from existing profile rows.

    Forward-proxy authentication is intentionally unsupported because process
    environments are not a safe secret channel. Older versions accepted full
    URLs, so retain only a credential-free scheme/authority and discard any
    path, query, or fragment.
    """
    columns = _table_columns("build_profiles")
    proxy_columns = [
        column for column in ("http_proxy", "https_proxy") if column in columns
    ]
    if not proxy_columns:
        return

    def _safe_proxy(value):
        raw = str(value or "").strip()
        if not raw:
            return None
        if any(char.isspace() or ord(char) < 32 for char in raw):
            return None
        try:
            parsed = urlsplit(raw)
            if parsed.scheme not in ("http", "https") or not parsed.hostname:
                return None
            port = parsed.port
            if port is not None and not 1 <= port <= 65535:
                return None
            host = parsed.hostname
            if ":" in host and not host.startswith("["):
                host = f"[{host}]"
            netloc = host + (f":{port}" if port is not None else "")
            return urlunsplit((parsed.scheme, netloc, "", "", ""))
        except (TypeError, ValueError):
            return None

    select_columns = ", ".join(["id", *proxy_columns])
    with db.engine.begin() as connection:
        rows = connection.execute(
            text(f"SELECT {select_columns} FROM build_profiles")
        ).mappings().all()
        for row in rows:
            updates = {}
            for column in proxy_columns:
                safe_value = _safe_proxy(row[column])
                if safe_value != row[column]:
                    updates[column] = safe_value
            if not updates:
                continue
            assignments = ", ".join(
                f"{column} = :{column}" for column in updates
            )
            connection.execute(
                text(f"UPDATE build_profiles SET {assignments} WHERE id = :id"),
                {**updates, "id": row["id"]},
            )


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


def _migrate_service_alert_columns() -> None:
    if "alert_policies" in inspect(db.engine).get_table_names():
        _add_column_if_missing("alert_policies", "service_config", "JSON")


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


def _migrate_renamed_permissions() -> None:
    """Carry role grants across a permission KEY rename, before pruning drops the old one.

    ``_prune_obsolete_permissions`` deletes any permission row missing from
    ``rbac_data.PERMISSIONS``, which silently strips the grant from every role
    that held it. When a key is renamed rather than retired (``zoho:*`` became
    ``ticketing:*`` when the tab grew a second provider), the grant has to be
    re-pointed first. Idempotent: a role that already holds the new key is left
    alone, and a deployment with no old rows is a no-op.
    """
    from .models import Permission, Role

    renames = {
        "zoho:view": "ticketing:view",
        "zoho:manage": "ticketing:manage",
    }
    old_rows = Permission.query.filter(Permission.key.in_(renames.keys())).all()
    if not old_rows:
        return

    # Migrations run BEFORE ``seed_defaults`` creates the new Permission rows, and
    # the prune below would drop the old ones in this same pass — so the
    # replacements have to exist now. ``_seed_permissions`` later fills in the
    # canonical description on these rows.
    from .rbac_data import PERMISSIONS

    descriptions = dict(PERMISSIONS)
    new_by_key = {
        perm.key: perm
        for perm in Permission.query.filter(Permission.key.in_(renames.values())).all()
    }
    for new_key in renames.values():
        if new_key not in new_by_key:
            perm = Permission(key=new_key, description=descriptions.get(new_key, ""))
            db.session.add(perm)
            new_by_key[new_key] = perm
    db.session.flush()

    carried = 0
    for role in Role.query.all():
        held = {perm.key for perm in role.permissions}
        for old in old_rows:
            replacement = new_by_key.get(renames[old.key])
            if old.key in held and replacement is not None and replacement.key not in held:
                role.permissions.append(replacement)
                held.add(replacement.key)
                carried += 1
    db.session.commit()
    if carried:
        logger.info("Carried %s role grant(s) from zoho:* to ticketing:*.", carried)


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
        _add_column_if_missing(
            "deployment_request_settings", "rollout_timeout_minutes", "INTEGER DEFAULT 15"
        )
        _add_column_if_missing(
            "deployment_request_settings", "rollback_on_failure", "BOOLEAN DEFAULT true"
        )


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
            ("supports_external", "BOOLEAN DEFAULT false"),
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


def _migrate_client_service_connections() -> None:
    """Forward-compatible column adds for the client_service_connections table.

    The table itself is created by ``db.create_all()``; this only backfills
    columns added after it first shipped, mirroring the other migrators.
    Idempotent and safe on a fresh database.
    """
    if "client_service_connections" not in inspect(db.engine).get_table_names():
        return
    for col, sql_type in [
        ("source_ip", "VARCHAR(64)"),
        ("destination_ip", "VARCHAR(64)"),
        ("netted_source_ip", "VARCHAR(64)"),
        ("netted_destination_ip", "VARCHAR(64)"),
        ("transport_type", "VARCHAR(32)"),
        ("transport_name", "VARCHAR(255)"),
        ("transport_notes", "TEXT"),
        ("cluster_id", "VARCHAR(120)"),
        ("namespace", "VARCHAR(253)"),
        ("environment", "VARCHAR(64)"),
        ("component_refs", "TEXT"),
        ("direction", "VARCHAR(16) DEFAULT 'inbound'"),
        ("status", "VARCHAR(32) DEFAULT 'active'"),
        ("is_active", "BOOLEAN DEFAULT true"),
    ]:
        _add_column_if_missing("client_service_connections", col, sql_type)


def _migrate_client_service_egress_connections() -> None:
    """Forward-compatible column adds for the client_service_egress_connections table.

    The table itself is created by ``db.create_all()``; this only backfills
    columns added after it first shipped, mirroring the other migrators.
    Idempotent and safe on a fresh database.
    """
    if "client_service_egress_connections" not in inspect(db.engine).get_table_names():
        return
    for col, sql_type in [
        ("node_ref", "VARCHAR(120)"),
        ("node_name", "VARCHAR(253)"),
        ("source_ip", "VARCHAR(64)"),
        ("destination_ip", "VARCHAR(64)"),
        ("transport_type", "VARCHAR(32)"),
        ("transport_name", "VARCHAR(255)"),
        ("transport_notes", "TEXT"),
        ("direction", "VARCHAR(16) DEFAULT 'outbound'"),
        ("status", "VARCHAR(32) DEFAULT 'active'"),
        ("is_active", "BOOLEAN DEFAULT true"),
    ]:
        _add_column_if_missing("client_service_egress_connections", col, sql_type)


def _seed_builtin_ci_runners() -> None:
    """Ensure the runners KubeSight manages itself exist.

    ``mock`` executes pipelines without touching a cluster — it backs mock mode
    and the test suite. ``kubernetes`` is the real in-cluster Job executor and
    ships disabled until its adapter lands, so a build can never silently
    dispatch to a runner that cannot run it.
    """
    if "ci_runners" not in inspect(db.engine).get_table_names():
        return
    from .models_ci import CiRunner

    builtins = [
        {
            "name": "kubesight-mock",
            "runner_type": "mock",
            "description": "Simulated executor. Runs pipelines without a cluster.",
            "status": "online",
            "enabled": True,
            "os": "linux",
            "arch": "amd64",
            "labels": ["mock"],
            "capabilities": [
                "mock", "linux", "java", "java21", "node", "python",
                "docker", "android", "generic",
            ],
            "max_concurrent": 4,
        },
        {
            "name": "kubesight-kubernetes",
            "runner_type": "kubernetes",
            "description": (
                "Ephemeral Kubernetes Job executor: one Job per build, stages as "
                "ordered initContainers on a shared workspace. Apply k8s/ci-runner.yaml, "
                "then enable this runner."
            ),
            "status": "offline",
            "enabled": False,
            "os": "linux",
            "arch": "amd64",
            "labels": ["kubernetes"],
            # A Kubernetes runner satisfies any Linux toolchain label because the
            # stage's own image brings the tools; only macOS-bound labels are
            # genuinely out of reach.
            "capabilities": [
                "linux", "kubernetes", "docker", "java", "java17", "java21",
                "node", "python", "android", "flutter", "generic",
            ],
            "max_concurrent": 4,
        },
    ]
    changed = False
    for spec in builtins:
        row = CiRunner.query.filter_by(name=spec["name"]).first()
        if row is not None:
            # Operators own enabled/capacity after first creation, but default
            # capabilities are union-merged so shipped rows learn new ones.
            if not row.is_builtin:
                row.is_builtin = True
                changed = True
            merged = sorted(set(row.capabilities or []) | set(spec["capabilities"]))
            if merged != sorted(row.capabilities or []):
                row.capabilities = merged
                db.session.add(row)
                changed = True
            continue
        db.session.add(CiRunner(is_builtin=True, **spec))
        changed = True
    if changed:
        db.session.commit()


def _migrate_ci_columns() -> None:
    """Columns native CI adds to tables it did not create.

    The ``ci_*`` tables themselves come from ``db.create_all()``. These three
    are additions to pre-existing tables, so a deployed database needs them
    backfilled. Idempotent and safe on a fresh database.
    """
    existing = set(inspect(db.engine).get_table_names())
    if "bitbucket_credential_profiles" in existing:
        # Shared credential store: CI and Application Intelligence both read it.
        _add_column_if_missing(
            "bitbucket_credential_profiles",
            "provider",
            "VARCHAR(32) DEFAULT 'bitbucket'",
        )
        with db.engine.begin() as conn:
            conn.execute(
                text(
                    "UPDATE bitbucket_credential_profiles "
                    "SET provider = 'bitbucket' WHERE provider IS NULL"
                )
            )
    if "intelligence_applications" in existing:
        _add_column_if_missing("intelligence_applications", "ci_service_id", "INTEGER")
    if "application_deployment_versions" in existing:
        # Closes the commit -> build -> artifact -> deployment trace.
        _add_column_if_missing("application_deployment_versions", "ci_build_id", "INTEGER")
        _add_column_if_missing(
            "application_deployment_versions", "ci_artifact_id", "INTEGER"
        )
    # Jenkins retirement: the automation and Mobile Applications can build on
    # native CI instead of a Jenkins job.
    if "deploy_automation_runs" in existing:
        _add_column_if_missing("deploy_automation_runs", "ci_build_id", "INTEGER")
    if "mobile_applications" in existing:
        _add_column_if_missing("mobile_applications", "ci_service_id", "INTEGER")
    if "mobile_app_builds" in existing:
        _add_column_if_missing("mobile_app_builds", "ci_build_id", "INTEGER")
    if "ci_pipeline_stages" in existing:
        # Added after the table shipped: db.create_all() will not alter an
        # existing table, so a deployed database needs this backfilled. Existing
        # rows read as NULL, which the serializer renders as an empty list.
        _add_column_if_missing("ci_pipeline_stages", "host_aliases", "TEXT")


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
        ("image_hosts", "TEXT"),
        ("auth_mode", "VARCHAR(16) DEFAULT 'basic'"),
        ("verify_tls", "BOOLEAN DEFAULT true"),
        ("ca_cert", "TEXT"),
        ("enforcement", "VARCHAR(8) DEFAULT 'block'"),
        ("enabled", "BOOLEAN DEFAULT true"),
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


def _migrate_user_onboarding_columns() -> None:
    """Add first-login / MFA columns to the users table (idempotent).

    Existing rows must NOT be forced through onboarding, so ``first_login_completed``
    defaults to 1 (true) — every pre-existing account keeps logging in normally.
    Only accounts created after this migration (via the admin create-user flow)
    are provisioned with ``first_login_completed = 0``.
    """
    if "users" not in inspect(db.engine).get_table_names():
        return
    # NB: boolean columns use the SQL boolean literals ``false``/``true`` rather
    # than ``0``/``1``. PostgreSQL rejects an integer default on a BOOLEAN column
    # (DatatypeMismatch); SQLite (3.23+) accepts the keyword form too, so this is
    # portable across both backends.
    for col, sql_type in [
        ("last_login_ip", "VARCHAR(64)"),
        ("must_change_password", "BOOLEAN DEFAULT false"),
        ("temporary_password_expires_at", "DATETIME"),
        ("temporary_password_used", "BOOLEAN DEFAULT false"),
        ("mfa_enabled", "BOOLEAN DEFAULT false"),
        ("totp_secret", "VARCHAR(64)"),
        ("first_login_completed", "BOOLEAN DEFAULT true"),
        ("failed_login_attempts", "INTEGER DEFAULT 0"),
        ("mfa_failed_attempts", "INTEGER DEFAULT 0"),
        ("last_failed_login_at", "DATETIME"),
        ("locked_until", "DATETIME"),
        ("lock_reason", "VARCHAR(64)"),
        ("lock_count_24h", "INTEGER DEFAULT 0"),
        ("requires_admin_unlock", "BOOLEAN DEFAULT false"),
        ("created_by_admin_id", "INTEGER"),
        ("is_service_account", "BOOLEAN DEFAULT false"),
        ("interactive_login_enabled", "BOOLEAN DEFAULT true"),
    ]:
        _add_column_if_missing("users", col, sql_type)


def _migrate_application_intelligence_columns() -> None:
    """Forward-compatible Phase 1 Application Intelligence columns.

    New installations receive all tables through ``db.create_all``. These
    idempotent additions protect deployments that started an earlier build of
    Phase 1 before progress, worker authentication, or warning fields existed.
    """
    if "application_analyses" not in inspect(db.engine).get_table_names():
        return
    for col, sql_type in [
        ("progress_percent", "INTEGER DEFAULT 0"),
        ("current_stage", "VARCHAR(64)"),
        ("worker_job_name", "VARCHAR(253)"),
        ("worker_callback_token_hash", "VARCHAR(64)"),
        ("scanner_runs", "JSON"),
        ("warnings", "JSON"),
        ("source_coverage", "JSON"),
    ]:
        _add_column_if_missing("application_analyses", col, sql_type)
    # Per-finding source evidence. Earlier builds discarded the observation
    # Hermes cited, which left findings unverifiable in the UI. Legacy score
    # columns on application_analyses are intentionally left in place and
    # unmapped: they held model-invented numbers that are no longer published.
    if "application_findings" in inspect(db.engine).get_table_names():
        _add_column_if_missing("application_findings", "evidence", "TEXT")


def _migrate_zoho_integration_columns() -> None:
    """Forward-compatible column adds for the Zoho integration config (single-row).

    The table is created by ``db.create_all()``; this backfills the Environment
    picklist columns added when the dropdown was split into deployments + namespaces.
    Must not recreate the table — it holds the encrypted OAuth secrets.
    """
    if "zoho_integration" in inspect(db.engine).get_table_names():
        _add_column_if_missing("zoho_integration", "environment_field_id", "VARCHAR(64)")
        _add_column_if_missing(
            "zoho_integration", "environment_field_api_name", "VARCHAR(120) DEFAULT 'cf_environment'"
        )
        _add_column_if_missing("zoho_integration", "sync_application", "BOOLEAN DEFAULT true")
        _add_column_if_missing("zoho_integration", "sync_environment", "BOOLEAN DEFAULT true")
        # Live-cluster dropdown source (namespaces picked from a cluster) + the
        # Application<-Environment cascade added when the source moved off AppServices.
        _add_column_if_missing("zoho_integration", "source_cluster_id", "VARCHAR(120)")
        _add_column_if_missing("zoho_integration", "selected_namespaces", "TEXT")
        _add_column_if_missing("zoho_integration", "selected_deployments", "TEXT")
        _add_column_if_missing("zoho_integration", "cascade_enabled", "BOOLEAN DEFAULT true")
        _add_column_if_missing("zoho_integration", "dependency_mapping_id", "VARCHAR(64)")
        _add_column_if_missing("zoho_integration", "last_dependency_status", "VARCHAR(16)")
        _add_column_if_missing("zoho_integration", "last_dependency_message", "TEXT")
        # Ticket write-back (deploy automation → Desk ticket status/comment/owner).
        _add_column_if_missing("zoho_integration", "ticket_writeback_enabled", "BOOLEAN DEFAULT false")
        _add_column_if_missing("zoho_integration", "ticket_status_started", "VARCHAR(120) DEFAULT 'Open'")
        _add_column_if_missing("zoho_integration", "ticket_status_deployed", "VARCHAR(120) DEFAULT 'Closed'")
        _add_column_if_missing("zoho_integration", "ticket_status_failed", "VARCHAR(120) DEFAULT 'Failed'")
        _add_column_if_missing("zoho_integration", "ticket_status_cancelled", "VARCHAR(120) DEFAULT 'Canceled'")
        _add_column_if_missing(
            "zoho_integration", "ticket_owner_email", "VARCHAR(255) DEFAULT 'zagent@areeba.com'"
        )
        # Variable-change automation (Variable picklist + Value field + App->Variable cascade).
        _add_column_if_missing("zoho_integration", "variable_field_id", "VARCHAR(64)")
        _add_column_if_missing(
            "zoho_integration", "variable_field_api_name", "VARCHAR(120) DEFAULT 'cf_variable'"
        )
        _add_column_if_missing(
            "zoho_integration", "value_field_api_name", "VARCHAR(120) DEFAULT 'cf_value'"
        )
        _add_column_if_missing("zoho_integration", "sync_variables", "BOOLEAN DEFAULT false")
        _add_column_if_missing("zoho_integration", "variable_mapping_id", "VARCHAR(64)")
        # Custom (non-cluster) Environment entries with their Jenkins routing.
        _add_column_if_missing("zoho_integration", "custom_environments", "TEXT")
        # Per-namespace/per-deployment Jenkins job overrides for cluster targets.
        _add_column_if_missing("zoho_integration", "job_overrides", "TEXT")
    if "zoho_inbound_tickets" in inspect(db.engine).get_table_names():
        _add_column_if_missing("zoho_inbound_tickets", "variable_name", "TEXT")
        _add_column_if_missing("zoho_inbound_tickets", "variable_value", "TEXT")


def _migrate_ticketing_tables() -> None:
    """Make the Zoho-only integration multi-provider (Zoho + Jira).

    Idempotent steps:

    1. Stamp ``provider`` on the tables Zoho used to own outright. Existing rows
       predate Jira, so the backfill is unconditionally ``'zoho'``.
    2. Seed :class:`~api.models.TicketingDeployConfig` — the deploy surface —
       from the live Zoho row, ONCE. The columns moved verbatim (same names, same
       JSON encodings), so this is a copy.
    3. Key that table by provider and give every provider its own row. The deploy
       source was briefly one shared record; splitting it means the pre-existing
       row becomes Zoho's and is CLONED to the other providers, so both tabs keep
       publishing exactly what they published before the split. Only the split
       needs the clone — a provider added later has no history to inherit and
       starts with an empty selection.

    Nothing is dropped from ``zoho_integration``: the old columns stay as a
    readable record of where the values came from, and as the fallback the
    seeder reads on a fresh database.
    """
    tables = inspect(db.engine).get_table_names()

    for table in ("zoho_inbound_tickets", "zoho_field_bindings", "zoho_layout_snapshots"):
        if table not in tables:
            continue
        fresh = "provider" not in _table_columns(table)
        _add_column_if_missing(table, "provider", "VARCHAR(16) DEFAULT 'zoho'")
        if fresh:
            # A DEFAULT on ADD COLUMN backfills on SQLite but the explicit UPDATE
            # keeps PostgreSQL and any NULL-tolerant path consistent.
            with db.engine.begin() as conn:
                conn.execute(text(f"UPDATE {table} SET provider = 'zoho' WHERE provider IS NULL"))

    if "ticketing_deploy_config" not in tables:
        return

    # --- The source becomes per-provider -----------------------------------
    fresh_provider = "provider" not in _table_columns("ticketing_deploy_config")
    _add_column_if_missing("ticketing_deploy_config", "provider", "VARCHAR(16) DEFAULT 'zoho'")
    with db.engine.begin() as conn:
        if fresh_provider:
            conn.execute(
                text(
                    "UPDATE ticketing_deploy_config SET provider = 'zoho' "
                    "WHERE provider IS NULL OR provider = ''"
                )
            )
        # Enforced as an index rather than a constraint: SQLite cannot ALTER one
        # in, and on PostgreSQL the UNIQUE constraint create_all makes on a fresh
        # database IS an index of this name, so IF NOT EXISTS covers both.
        conn.execute(
            text(
                "CREATE UNIQUE INDEX IF NOT EXISTS uq_ticketing_deploy_provider "
                "ON ticketing_deploy_config (provider)"
            )
        )

    legacy = None
    if "zoho_integration" in tables:
        with db.engine.begin() as conn:
            seeded = conn.execute(
                text("SELECT COUNT(*) FROM ticketing_deploy_config WHERE provider = 'zoho'")
            ).scalar()
            if not seeded:
                legacy = conn.execute(
                    text(
                        "SELECT source_cluster_id, selected_namespaces, selected_deployments, "
                        "custom_environments, job_overrides FROM zoho_integration WHERE id = 1"
                    )
                ).first()
                conn.execute(
                    text(
                        "INSERT INTO ticketing_deploy_config "
                        "(provider, source_cluster_id, selected_namespaces, selected_deployments, "
                        " custom_environments, job_overrides, created_at, updated_at) "
                        "VALUES ('zoho', :cluster, :namespaces, :deployments, :custom, :overrides, "
                        " CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
                    ),
                    {
                        "cluster": legacy[0] if legacy else None,
                        "namespaces": legacy[1] if legacy else None,
                        "deployments": legacy[2] if legacy else None,
                        "custom": legacy[3] if legacy else None,
                        "overrides": legacy[4] if legacy else None,
                    },
                )
    if legacy and any(legacy):
        logger.info("Seeded the Zoho ticketing deploy source from the Zoho integration row.")

    # Clone the (formerly shared) selection to any provider still missing a row.
    for other in ("jira",):
        with db.engine.begin() as conn:
            if conn.execute(
                text("SELECT COUNT(*) FROM ticketing_deploy_config WHERE provider = :p"),
                {"p": other},
            ).scalar():
                continue
            copied = conn.execute(
                text(
                    "INSERT INTO ticketing_deploy_config "
                    "(provider, source_cluster_id, selected_namespaces, selected_deployments, "
                    " custom_environments, job_overrides, created_at, updated_at) "
                    "SELECT :p, source_cluster_id, selected_namespaces, selected_deployments, "
                    " custom_environments, job_overrides, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP "
                    "FROM ticketing_deploy_config WHERE provider = 'zoho'"
                ),
                {"p": other},
            ).rowcount
            # Zero means there was no Zoho row to inherit from (a fresh install);
            # the provider's row is then created empty on first read.
            if copied:
                logger.info(
                    "Split the ticketing deploy source: cloned the shared selection to %s.", other
                )


def _migrate_deploy_automation_columns() -> None:
    """Column adds for the deploy-automation tables (created 2026-07-08).

    The post-deploy pod-health step added a rollout timeout on the Jenkins
    config and a rollout-start anchor on runs; both tables may already exist.
    """
    tables = inspect(db.engine).get_table_names()
    if "jenkins_connection" in tables:
        _add_column_if_missing("jenkins_connection", "rollout_timeout_minutes", "INTEGER DEFAULT 15")
        _add_column_if_missing("jenkins_connection", "auto_run_clusters", "JSON")
        _add_column_if_missing(
            "jenkins_connection", "image_tag_template", "VARCHAR(120) DEFAULT '{tag}'"
        )
        _add_column_if_missing("jenkins_connection", "build_token_encrypted", "TEXT")
        _add_column_if_missing("jenkins_connection", "rollback_on_failure", "BOOLEAN DEFAULT true")
        # Operator-pinned registry for the automation's image checks (2026-07-13).
        _add_column_if_missing("jenkins_connection", "registry_connection_id", "INTEGER")
        # Per-parameter router contract toggles (2026-07-13).
        _add_column_if_missing("jenkins_connection", "send_param_app", "BOOLEAN DEFAULT true")
        _add_column_if_missing("jenkins_connection", "send_param_namespace", "BOOLEAN DEFAULT true")
        _add_column_if_missing("jenkins_connection", "send_param_tag", "BOOLEAN DEFAULT true")
        # The original 10 min queue timeout failed builds that were merely waiting
        # behind an in-progress build; the default is now 30. Only rows still on
        # the old default are moved — an operator-chosen value is left alone.
        if "queue_timeout_minutes" in _table_columns("jenkins_connection"):
            db.session.execute(
                text("UPDATE jenkins_connection SET queue_timeout_minutes = 30 "
                     "WHERE queue_timeout_minutes = 10")
            )
            db.session.commit()
    if "deploy_automation_runs" in tables:
        _add_column_if_missing("deploy_automation_runs", "rollout_started_at", "DATETIME")
        _add_column_if_missing("deploy_automation_runs", "ticket_tag", "VARCHAR(200)")
        # Variable-change runs (change_type "env_var").
        _add_column_if_missing(
            "deploy_automation_runs", "change_type", "VARCHAR(16) DEFAULT 'image'"
        )
        _add_column_if_missing("deploy_automation_runs", "variable_name", "TEXT")
        _add_column_if_missing("deploy_automation_runs", "variable_value", "TEXT")


def _migrate_mobile_app_columns() -> None:
    """Column adds for the Mobile Applications tables (created 2026-07-15).

    ``create_all`` makes the tables themselves; columns added after that first
    release go here so existing databases pick them up.
    """
    tables = inspect(db.engine).get_table_names()
    if "mobile_applications" not in tables:
        return
    if "mobile_app_builds" in tables:
        # Signature probe at ingest, gating publish on shielded binaries
        # (2026-07-20). Existing rows come in as "unknown" and are then probed
        # once by _backfill_build_signature_state below.
        _add_column_if_missing(
            "mobile_app_builds", "signature_state", "VARCHAR(16) DEFAULT 'unknown'"
        )
        # Signed output linked back to the shielded build it came from.
        _add_column_if_missing("mobile_app_builds", "parent_build_id", "INTEGER")
    # Per-platform re-signing setup (2026-07-20).
    _add_column_if_missing("mobile_applications", "resign_config", "JSON")


def _backfill_build_signature_state() -> None:
    """Probe binaries that predate the signature check, once.

    Without this, every build ingested before the probe existed stays "unknown"
    forever — which never blocks a publish, but also never offers a re-sign, so
    a shielded binary already in the store looks fine and cannot be signed.

    Cheap despite the file sizes: reading a zip's central directory seeks to the
    end of the file rather than scanning it. Rows whose binary is missing or
    unreadable are left alone and simply stay "unknown".
    """
    from .models import MobileAppBuild

    if "mobile_app_builds" not in inspect(db.engine).get_table_names():
        return
    try:
        pending = (
            MobileAppBuild.query.filter(
                MobileAppBuild.status == "available",
                MobileAppBuild.signature_state == "unknown",
            )
            .limit(500)
            .all()
        )
    except Exception:  # column not there yet on a very old DB
        # PostgreSQL aborts the whole transaction on a failed statement, so
        # returning without a rollback would fail every later ORM query in this
        # session with InFailedSqlTransaction — far from the real cause.
        db.session.rollback()
        return
    if not pending:
        return

    from .services import binary_signature
    from .services.mobile_app_service import binary_path

    changed = 0
    for build in pending:
        path = binary_path(build)
        if not path or not os.path.isfile(path):
            continue
        state = binary_signature.detect_safe(path, build.artifact_type or "")
        if state != "unknown":
            build.signature_state = state
            changed += 1
    if changed:
        db.session.commit()
        logger.info("Backfilled signature_state for %s mobile build(s)", changed)


def run_migrations() -> None:
    db.create_all()
    # DDL for columns added to pre-existing tables must run before ANY step that
    # issues an ORM query: a mapped column missing from the table makes the whole
    # SELECT fail, and on PostgreSQL that aborts the session's transaction.
    _migrate_ci_columns()
    _migrate_cluster_build_columns()
    _sanitize_legacy_build_profile_proxies()
    _migrate_zoho_integration_columns()
    _migrate_ticketing_tables()
    _migrate_deploy_automation_columns()
    _migrate_mobile_app_columns()
    _backfill_build_signature_state()
    _migrate_user_onboarding_columns()
    _migrate_application_intelligence_columns()
    _migrate_deployment_request_columns()
    _migrate_change_bundle_columns()
    _migrate_alert_routing_user_receivers()
    _migrate_clusters_table()
    _migrate_app_catalog_helm_columns()
    _migrate_alert_policy_evaluation_columns()
    _migrate_alert_delivery_log_group_column()
    _migrate_log_alert_columns()
    _migrate_service_alert_columns()
    _migrate_legacy_users()
    _migrate_app_service_deployments()
    _migrate_app_service_topology_positions()
    _migrate_app_service_topology_edge_meta()
    _migrate_topology_components()
    _migrate_service_catalog_columns()
    _migrate_client_service_connections()
    _migrate_client_service_egress_connections()
    _migrate_registry_connection_columns()
    _seed_builtin_ci_runners()
    from .access_rules import migrate_all_users_legacy_rules
    from .migrate_alert_routing import run_alert_routing_migrations

    migrate_all_users_legacy_rules()
    run_alert_routing_migrations()
    _sync_role_permissions()
    # Must run BEFORE the prune: it reads the old rows the prune is about to drop.
    _migrate_renamed_permissions()
    _prune_obsolete_permissions()
