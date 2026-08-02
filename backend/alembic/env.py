"""Alembic environment.

Two things this file is careful about.

**One source of truth for the URL.** It is read from DATABASE_URL exactly as
`create_app` reads it, including the `postgres://` normalisation. A migration
run that can silently target a different database than the application is worse
than no migrations at all.

**Every model module must be imported.** SQLAlchemy only knows about tables
whose classes have been imported, so a module missing from the list below is
invisible to autogenerate -- and its tables would be proposed for deletion on
the next revision. Adding a `models_*.py` means adding it here.
"""

from __future__ import annotations

import os
import sys
from logging.config import fileConfig
from pathlib import Path

from alembic import context
from sqlalchemy import engine_from_config, pool

# `backend/` on the path, so `api.*` imports resolve when alembic is invoked
# from anywhere.
BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from api.db import db  # noqa: E402

# Import for the side effect of registering tables on db.metadata. Nothing here
# is referenced directly, hence the noqa.
import api.models  # noqa: E402,F401
import api.models_application_intelligence  # noqa: E402,F401
import api.models_cluster_build  # noqa: E402,F401

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = db.metadata


def _database_url() -> str:
    """Resolve the URL the way create_app does.

    ALEMBIC_DATABASE_URL wins so a migration can be pointed at a copy without
    disturbing DATABASE_URL for everything else running on the box.
    """
    url = (os.getenv("ALEMBIC_DATABASE_URL") or os.getenv("DATABASE_URL") or "").strip()
    if not url:
        url = "sqlite:///kubesight.db"
    if url.startswith("postgres://"):
        url = url.replace("postgres://", "postgresql://", 1)
    return url


def run_migrations_offline() -> None:
    context.configure(
        url=_database_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
        compare_server_default=True,
        render_as_batch=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    section = config.get_section(config.config_ini_section, {})
    section["sqlalchemy.url"] = _database_url()

    connectable = engine_from_config(
        section,
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            compare_type=True,
            compare_server_default=True,
            # SQLite cannot ALTER most things in place; batch mode rebuilds the
            # table instead. Harmless on PostgreSQL, and it keeps one migration
            # working on both, which the test suite depends on.
            render_as_batch=True,
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
