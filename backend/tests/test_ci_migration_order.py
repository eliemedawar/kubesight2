"""Migration ordering on a database that predates native CI.

The deployed failure this guards: ``db.create_all`` creates new tables but never
alters an existing one, so a database from before native CI has
``mobile_app_builds`` WITHOUT the ``ci_build_id`` column its model now maps.
``_backfill_build_signature_state`` queries that model early in
``run_migrations``; selecting a column the table lacks fails the statement, and
on PostgreSQL that aborts the whole transaction — so every later ORM step died
with ``InFailedSqlTransaction`` pointing at an unrelated migration.
"""

from __future__ import annotations

from sqlalchemy import inspect, text

from api.db import db
from api.migrate_rbac import run_migrations
from api.models import MobileAppBuild


def _columns(table: str) -> set:
    return {col["name"] for col in inspect(db.engine).get_columns(table)}


def test_run_migrations_survives_a_pre_ci_database(app):
    """Drop the CI column, then re-run migrations as a deploy would.

    Note this passes on SQLite even with the ordering wrong: SQLite keeps the
    transaction usable after a failed statement, so the cascade never happens.
    That dialect gap is why the deployed break was invisible here — the ordering
    and rollback assertions below are what actually guard it.
    """
    with app.app_context():
        with db.engine.begin() as conn:
            conn.execute(text("ALTER TABLE mobile_app_builds DROP COLUMN ci_build_id"))
        assert "ci_build_id" not in _columns("mobile_app_builds")

        run_migrations()

        # Re-added, and the early ORM backfill no longer trips over it.
        assert "ci_build_id" in _columns("mobile_app_builds")
        assert MobileAppBuild.query.all() == []


def test_ci_columns_precede_the_first_orm_query():
    """Ordering is the actual fix; assert it rather than trusting a comment."""
    import inspect as pyinspect

    from api import migrate_rbac

    body = pyinspect.getsource(migrate_rbac.run_migrations)
    steps = [
        line.strip().rstrip("()")
        for line in body.splitlines()
        if line.strip().startswith("_") and line.strip().endswith("()")
    ]
    assert steps.index("_migrate_ci_columns") < steps.index(
        "_backfill_build_signature_state"
    )


def test_backfill_rolls_back_a_swallowed_query_failure(app, monkeypatch):
    """The swallowed failure must leave the session clean, not poisoned.

    Independent of the ordering fix and of the dialect: whatever makes that
    query fail, PostgreSQL needs the rollback before the next ORM statement.
    """
    from api import migrate_rbac

    with app.app_context():
        calls = []
        monkeypatch.setattr(
            db.session, "rollback", lambda: calls.append("rollback"), raising=False
        )

        def _boom(*_args, **_kwargs):
            raise RuntimeError("column ci_build_id does not exist")

        monkeypatch.setattr(MobileAppBuild, "query", property(_boom), raising=False)
        migrate_rbac._backfill_build_signature_state()

        assert calls == ["rollback"]
