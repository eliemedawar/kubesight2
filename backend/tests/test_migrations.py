"""Alembic guardrails.

The value of migrations is not that they exist, it is that they still describe
the models. These tests fail the moment the two drift, which is the only way a
schema change reaches production having been reviewed rather than discovered.

They deliberately use their own SQLite files rather than the `app` fixture:
these assert things about the schema definition, not about a running app, and
the in-memory database that fixture builds is torn down per test.
"""

from __future__ import annotations

import pathlib
import re

import pytest
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine

from api.db import db
from api.migrations import alembic_config

BACKEND_ROOT = pathlib.Path(__file__).resolve().parents[1]


@pytest.fixture()
def sqlite_url(tmp_path):
    return f"sqlite:///{(tmp_path / 'migrations.db').as_posix()}"


def _diff_against_models(url: str):
    engine = create_engine(url)
    try:
        with engine.connect() as connection:
            context = MigrationContext.configure(
                connection,
                opts={"compare_type": True, "render_as_batch": True},
            )
            return compare_metadata(context, db.metadata)
    finally:
        engine.dispose()


def test_single_head():
    """Two heads mean two people generated a revision from the same parent.

    Caught here rather than at `upgrade head`, which fails on a production box
    with a message nobody wants to read for the first time during a deploy.
    """
    script = ScriptDirectory.from_config(alembic_config())
    heads = script.get_heads()
    assert len(heads) == 1, f"expected one head, found {heads}"


def test_migrations_reproduce_the_models(sqlite_url):
    """Upgrading an empty database produces exactly what the models describe."""
    command.upgrade(alembic_config(sqlite_url), "head")

    diff = _diff_against_models(sqlite_url)
    assert diff == [], (
        "migrations and models disagree -- run "
        "`alembic revision --autogenerate` and commit the result:\n"
        + "\n".join(str(d) for d in diff)
    )


def test_every_model_module_is_imported_by_env(sqlite_url):
    """A model module missing from env.py is invisible to autogenerate.

    Worse than invisible: its tables look like tables nobody declared, so the
    next revision proposes dropping them. This compares the modules on disk
    against the ones env.py imports, so adding `models_jobs.py` or
    `models_auth.py` without wiring it up fails here instead of in a migration
    that quietly deletes data.
    """
    env = (BACKEND_ROOT / "alembic" / "env.py").read_text(encoding="utf-8")
    imported = set(re.findall(r"^import (api\.models[a-z_]*)", env, re.M))

    on_disk = {
        f"api.{path.stem}"
        for path in (BACKEND_ROOT / "api").glob("models*.py")
    }

    missing = on_disk - imported
    assert not missing, (
        f"model modules not imported by alembic/env.py: {sorted(missing)}. "
        "Add them or autogenerate will propose dropping their tables."
    )


def test_upgrade_is_reversible_to_base(sqlite_url):
    """Downgrade is exercised, not merely written.

    A down path that has never run is decoration, and the moment it matters is
    a failed upgrade on a customer's database.
    """
    config = alembic_config(sqlite_url)
    command.upgrade(config, "head")
    command.downgrade(config, "base")

    engine = create_engine(sqlite_url)
    try:
        remaining = [
            t for t in db.inspect(engine).get_table_names() if t != "alembic_version"
        ]
    finally:
        engine.dispose()

    assert remaining == [], f"downgrade left tables behind: {remaining}"
