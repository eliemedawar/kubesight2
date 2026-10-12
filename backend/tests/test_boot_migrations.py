"""Boot-time migration cost: reflection is cached, one-time fix-ups run once."""

from __future__ import annotations

from sqlalchemy import event, inspect, text

from api import migrate_rbac
from api.db import db
from api.migrate_rbac import run_migrations


def _count_statements(fn) -> list:
    statements: list = []

    def hook(conn, cursor, statement, parameters, context, executemany):
        statements.append(statement)

    event.listen(db.engine, "before_cursor_execute", hook)
    try:
        fn()
    finally:
        event.remove(db.engine, "before_cursor_execute", hook)
    return statements


def test_a_steady_state_boot_lists_the_tables_a_handful_of_times(app):
    with app.app_context():
        statements = _count_statements(run_migrations)
        table_listings = [s for s in statements if "sqlite_master WHERE type='table'" in s]
        # Was one listing per _add_column_if_missing call (~340 per boot).
        assert len(table_listings) <= 6, len(table_listings)


def test_cached_columns_follow_the_alters_a_pass_makes(app):
    with app.app_context():
        with db.engine.begin() as conn:
            conn.execute(text("ALTER TABLE users DROP COLUMN lock_count_24h"))
        with migrate_rbac._cached_schema():
            assert "lock_count_24h" not in migrate_rbac._table_columns("users")
            migrate_rbac._add_column_if_missing("users", "lock_count_24h", "INTEGER DEFAULT 0")
            assert "lock_count_24h" in migrate_rbac._table_columns("users")
            # A second call sees the column without reflecting again.
            statements = _count_statements(
                lambda: migrate_rbac._add_column_if_missing(
                    "users", "lock_count_24h", "INTEGER DEFAULT 0"
                )
            )
            assert statements == []
        assert "lock_count_24h" in {c["name"] for c in inspect(db.engine).get_columns("users")}
        # Outside a pass the helpers reflect live again.
        assert migrate_rbac._schema_cache is None


def test_signature_backfill_runs_once_per_installation(app, monkeypatch):
    with app.app_context():
        calls = []
        monkeypatch.setattr(
            migrate_rbac, "_backfill_build_signature_state", lambda: calls.append(1) or True
        )
        # The fixture's boot already recorded the marker.
        assert migrate_rbac._marker_done("mobile_signature_backfill_v1")
        run_migrations()
        assert calls == []

        with db.engine.begin() as conn:
            conn.execute(text("DELETE FROM kubesight_migration_markers"))
        run_migrations()
        run_migrations()
        assert calls == [1]


def test_a_failed_backfill_is_retried_next_boot(app, monkeypatch):
    with app.app_context():
        with db.engine.begin() as conn:
            conn.execute(text("DELETE FROM kubesight_migration_markers"))
        calls = []
        monkeypatch.setattr(
            migrate_rbac, "_backfill_build_signature_state", lambda: calls.append(1) or False
        )
        run_migrations()
        run_migrations()
        assert calls == [1, 1]
        assert not migrate_rbac._marker_done("mobile_signature_backfill_v1")


def test_legacy_grants_become_rules_only_for_users_without_rules(app):
    from api.models import AccessRule, Role, User, UserClusterAccess, UserNamespaceAccess

    with app.app_context():
        role = Role.query.filter_by(name="viewer").first()
        legacy = User(username="legacy", email="legacy@x.io", full_name="L", role=role)
        ruled = User(username="ruled", email="ruled@x.io", full_name="R", role=role)
        db.session.add_all([legacy, ruled])
        db.session.flush()
        db.session.add_all(
            [
                UserClusterAccess(user_id=legacy.id, cluster_id="c1", can_view=True),
                UserNamespaceAccess(user_id=legacy.id, cluster_id="c1", namespace="n1", can_view=True),
                UserClusterAccess(user_id=ruled.id, cluster_id="c2", can_view=True),
                AccessRule(
                    user_id=ruled.id,
                    cluster_id="c9",
                    resource_type="cluster",
                    permission_key="clusters:view",
                    effect="allow",
                ),
            ]
        )
        db.session.commit()

        migrate_rbac._migrate_legacy_access_rules()

        legacy_rules = {
            (r.cluster_id, r.namespace, r.permission_key)
            for r in AccessRule.query.filter_by(user_id=legacy.id)
        }
        assert legacy_rules == {
            ("c1", None, "clusters:view"),
            ("c1", "n1", "namespaces:view"),
            ("c1", "n1", "resources:view"),
        }
        ruled_rules = {r.cluster_id for r in AccessRule.query.filter_by(user_id=ruled.id)}
        assert ruled_rules == {"c9"}
