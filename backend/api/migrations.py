"""Alembic access for the application.

Schema changes are applied by Alembic, not by `db.create_all()` plus the
hand-written `ALTER TABLE` statements in `migrate_rbac.py`. Those ran on every
boot, could not be reviewed as a unit, had no down path, and gave no way to ask
"is this database the shape this code expects?" -- which is exactly the question
a production start needs answered before it serves traffic.

`is_at_head()` is that question, and it is what the production startup guard
calls.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Optional

from alembic import command
from alembic.config import Config
from alembic.runtime.migration import MigrationContext
from alembic.script import ScriptDirectory

from .db import db

BACKEND_ROOT = Path(__file__).resolve().parents[1]
ALEMBIC_INI = BACKEND_ROOT / "alembic.ini"


def alembic_config(database_url: Optional[str] = None) -> Config:
    config = Config(str(ALEMBIC_INI))
    config.set_main_option("script_location", str(BACKEND_ROOT / "alembic"))
    if database_url:
        # env.py reads this rather than the ini, so the app and alembic can
        # never disagree about which database they mean.
        os.environ["ALEMBIC_DATABASE_URL"] = database_url
    return config


def head_revision() -> Optional[str]:
    script = ScriptDirectory.from_config(alembic_config())
    return script.get_current_head()


def current_revision() -> Optional[str]:
    """The revision this database is stamped at, or None if never stamped.

    None means either a brand-new database or one built by the legacy path
    before Alembic existed. The two are told apart by whether any tables are
    present, which `is_legacy_unstamped` answers.
    """
    with db.engine.connect() as connection:
        return MigrationContext.configure(connection).get_current_revision()


def is_at_head() -> bool:
    return current_revision() is not None and current_revision() == head_revision()


def is_legacy_unstamped() -> bool:
    """A populated database with no alembic_version table.

    The baseline revision reproduces exactly what the legacy path built --
    verified by autogenerating against a legacy-built database and getting no
    operations -- so these are stamped, never upgraded. Running the baseline
    against them would try to create tables that already exist.
    """
    if current_revision() is not None:
        return False
    tables = db.inspect(db.engine).get_table_names()
    return bool([t for t in tables if t != "alembic_version"])


def stamp_head(database_url: Optional[str] = None) -> None:
    command.stamp(alembic_config(database_url), "head")


def upgrade_to_head(database_url: Optional[str] = None) -> None:
    """Bring the database to head, stamping legacy databases rather than
    replaying a baseline whose tables they already have."""
    if is_legacy_unstamped():
        stamp_head(database_url)
        return
    command.upgrade(alembic_config(database_url), "head")
