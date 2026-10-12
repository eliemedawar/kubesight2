"""Model-declared indexes reach databases whose tables already exist.

``db.create_all`` creates whole tables only, so an index added to a model after
its table shipped never reached a deployed database. ``create_missing_indexes``
runs at the end of every boot's migrations and creates exactly those.
"""

from __future__ import annotations

from sqlalchemy import Column, Index, Integer, MetaData, String, Table, text

from api.db import db
from api.migrate_rbac import create_missing_indexes, run_migrations


def _index_names() -> set:
    with db.engine.connect() as conn:
        return {
            row[0]
            for row in conn.execute(text("SELECT name FROM sqlite_master WHERE type = 'index'"))
        }


def _index_sql(name: str) -> str:
    with db.engine.connect() as conn:
        return conn.execute(
            text("SELECT sql FROM sqlite_master WHERE type = 'index' AND name = :n"), {"n": name}
        ).scalar() or ""


def test_declared_index_missing_from_the_database_is_created(app):
    with app.app_context():
        assert "ix_access_rule_user_cluster" in _index_names()
        with db.engine.begin() as conn:
            conn.execute(text("DROP INDEX ix_access_rule_user_cluster"))
            conn.execute(text("DROP INDEX ix_access_rules_user_id"))
        assert "ix_access_rule_user_cluster" not in _index_names()

        created = create_missing_indexes()

        assert set(created) == {"ix_access_rule_user_cluster", "ix_access_rules_user_id"}
        assert {"ix_access_rule_user_cluster", "ix_access_rules_user_id"} <= _index_names()


def test_existing_indexes_are_untouched_and_the_step_is_idempotent(app):
    with app.app_context():
        before = _index_names()
        assert create_missing_indexes() == []
        assert create_missing_indexes() == []
        assert _index_names() == before


def test_run_migrations_creates_missing_indexes(app):
    with app.app_context():
        with db.engine.begin() as conn:
            conn.execute(text("DROP INDEX ix_access_rule_user_cluster"))
        run_migrations()
        assert "ix_access_rule_user_cluster" in _index_names()


def test_partial_index_keeps_its_where_clause(app):
    with app.app_context():
        metadata = MetaData()
        table = Table(
            "zz_index_probe",
            metadata,
            Column("id", Integer, primary_key=True),
            Column("state", String(16)),
            Column("owner", String(16)),
        )
        with db.engine.begin() as conn:
            conn.execute(
                text("CREATE TABLE zz_index_probe (id INTEGER PRIMARY KEY, state VARCHAR(16), owner VARCHAR(16))")
            )
        Index(
            "ix_zz_probe_pending",
            table.c.owner,
            postgresql_where=table.c.state == "pending",
            sqlite_where=table.c.state == "pending",
        )
        Index("ix_zz_probe_owner_state", table.c.owner, table.c.state, unique=True)
        try:
            created = create_missing_indexes(metadata)
            assert set(created) == {"ix_zz_probe_pending", "ix_zz_probe_owner_state"}
            assert "WHERE state = 'pending'" in _index_sql("ix_zz_probe_pending")
            assert "UNIQUE" in _index_sql("ix_zz_probe_owner_state").upper()
            assert create_missing_indexes(metadata) == []
        finally:
            with db.engine.begin() as conn:
                conn.execute(text("DROP TABLE zz_index_probe"))


def test_a_failing_index_is_logged_and_boot_continues(app, caplog):
    with app.app_context():
        metadata = MetaData()
        table = Table(
            "zz_dupe_probe",
            metadata,
            Column("id", Integer, primary_key=True),
            Column("name", String(16)),
            Column("other", String(16)),
        )
        with db.engine.begin() as conn:
            conn.execute(text("CREATE TABLE zz_dupe_probe (id INTEGER PRIMARY KEY, name VARCHAR(16), other VARCHAR(16))"))
            conn.execute(text("INSERT INTO zz_dupe_probe (name) VALUES ('a'), ('a')"))
        Index("ix_zz_dupe_name", table.c.name, unique=True)
        Index("ix_zz_dupe_other", table.c.other)
        try:
            created = create_missing_indexes(metadata)
            # The unique index cannot be built over duplicates; the next one still is.
            assert created == ["ix_zz_dupe_other"]
            assert "ix_zz_dupe_name" in caplog.text
        finally:
            with db.engine.begin() as conn:
                conn.execute(text("DROP TABLE zz_dupe_probe"))


def test_index_step_can_be_switched_off(app, monkeypatch):
    with app.app_context():
        with db.engine.begin() as conn:
            conn.execute(text("DROP INDEX ix_access_rule_user_cluster"))
        monkeypatch.setenv("DB_INDEX_BOOT_MODE", "off")
        assert create_missing_indexes() == []
        assert "ix_access_rule_user_cluster" not in _index_names()


def test_postgres_ddl_is_concurrent_and_keeps_the_partial_where():
    from sqlalchemy.dialects import postgresql

    from api.migrate_rbac import index_ddl

    metadata = MetaData()
    table = Table("t", metadata, Column("id", Integer, primary_key=True), Column("s", String(8)))
    partial = Index("ix_t_s_live", table.c.s, unique=True, postgresql_where=table.c.s == "live")
    plain = Index("ix_t_s", table.c.s)
    dialect = postgresql.dialect()
    assert index_ddl(partial, dialect, concurrently=True) == (
        "CREATE UNIQUE INDEX CONCURRENTLY IF NOT EXISTS ix_t_s_live ON t (s) WHERE s = 'live'"
    )
    assert index_ddl(plain, dialect, concurrently=True) == (
        "CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_t_s ON t (s)"
    )
    assert index_ddl(plain, dialect) == "CREATE INDEX IF NOT EXISTS ix_t_s ON t (s)"
