"""Operational commands for a KubeSight deployment.

These exist because production should not do any of this to itself on boot.
Bringing a database to head, reconciling permissions, and creating the first
administrator are deliberate steps an operator takes with a backup in hand --
not side effects of a pod restarting.

Deliberately does not call `create_app`. The app refuses to start in production
when the database is not at head, which would deadlock the one command whose job
is to fix that. A minimal Flask app with the same database URL is enough for
everything here.

    python manage.py status
    python manage.py migrate      # alembic upgrade head
    python manage.py reconcile    # idempotent data repair
    python manage.py seed         # default roles, permissions, demo users
    python manage.py upgrade      # migrate + reconcile, the usual release step

`upgrade` deliberately omits `seed`: it creates demo users, which is the last
thing a production upgrade should introduce.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

BACKEND_ROOT = Path(__file__).resolve().parent
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from flask import Flask  # noqa: E402

from api.db import db  # noqa: E402

# Imported for the side effect of registering tables on the metadata.
import api.models  # noqa: E402,F401
import api.models_application_intelligence  # noqa: E402,F401
import api.models_cluster_build  # noqa: E402,F401


def _database_url() -> str:
    url = (os.getenv("DATABASE_URL") or "sqlite:///kubesight.db").strip()
    if url.startswith("postgres://"):
        url = url.replace("postgres://", "postgresql://", 1)
    return url


def _app() -> Flask:
    app = Flask(__name__)
    app.config["SQLALCHEMY_DATABASE_URI"] = _database_url()
    app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
    db.init_app(app)
    return app


def cmd_status(_args: argparse.Namespace) -> int:
    from api.migrations import current_revision, head_revision, is_at_head

    url = _database_url()
    # Never print a URL with an embedded password.
    safe = url.split("@")[-1] if "@" in url else url
    print(f"database:  {safe}")
    print(f"current:   {current_revision()}")
    print(f"head:      {head_revision()}")
    print(f"at head:   {is_at_head()}")
    return 0 if is_at_head() else 1


def cmd_migrate(_args: argparse.Namespace) -> int:
    from api.migrations import is_at_head, upgrade_to_head

    upgrade_to_head(_database_url())
    print("at head" if is_at_head() else "STILL NOT AT HEAD")
    return 0 if is_at_head() else 1


def cmd_reconcile(_args: argparse.Namespace) -> int:
    from api.migrate_rbac import reconcile_data
    from api.migrations import is_at_head

    if not is_at_head():
        print(
            "refusing: database is not at head. Run `migrate` first -- "
            "reconciling against a stale schema is how you get a half-applied "
            "permission set.",
            file=sys.stderr,
        )
        return 1
    reconcile_data()
    print("reconciled")
    return 0


def cmd_seed(_args: argparse.Namespace) -> int:
    from api.seed import seed_defaults

    seed_defaults()
    print("seeded")
    return 0


def cmd_upgrade(args: argparse.Namespace) -> int:
    rc = cmd_migrate(args)
    if rc != 0:
        return rc
    return cmd_reconcile(args)


COMMANDS = {
    "status": cmd_status,
    "migrate": cmd_migrate,
    "reconcile": cmd_reconcile,
    "seed": cmd_seed,
    "upgrade": cmd_upgrade,
}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="manage.py", description=__doc__)
    parser.add_argument("command", choices=sorted(COMMANDS))
    args = parser.parse_args(argv)

    app = _app()
    with app.app_context():
        return COMMANDS[args.command](args)


if __name__ == "__main__":
    raise SystemExit(main())
