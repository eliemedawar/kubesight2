"""Inventory catalog merge: one query for every row, same matches as before."""

from sqlalchemy import event

from api.db import db
from api.models import AppCatalogEntry
from api.services.app_catalog_service import get_entry_for_inventory
from api.services.inventory_service import _merge_catalog_metadata


def _rows(n):
    return [
        {"cluster": "c1", "clusterId": "c1", "namespace": f"ns{i % 5}", "name": f"app-{i}",
         "workloadNames": [f"app-{i}-wl"], "source": "Discovered"}
        for i in range(n)
    ]


def _seed(n):
    for i in range(n):
        # Half match by display name, half by workload name only.
        db.session.add(AppCatalogEntry(
            cluster_id="c1", namespace=f"ns{i % 5}",
            display_name=f"app-{i}" if i % 2 == 0 else f"registered-{i}",
            workload_name=f"app-{i}-wl", owner_team=f"team-{i}", source="Registered", is_active=True,
        ))
    db.session.commit()


def _count_statements(fn):
    statements = []

    def before(conn, cursor, statement, parameters, context, executemany):
        statements.append(statement)

    event.listen(db.engine, "before_cursor_execute", before)
    try:
        result = fn()
    finally:
        event.remove(db.engine, "before_cursor_execute", before)
    return result, len(statements)


def test_merge_runs_one_query_however_many_rows(app):
    with app.app_context():
        _seed(60)
        _, small = _count_statements(lambda: _merge_catalog_metadata(_rows(10)))
        _, large = _count_statements(lambda: _merge_catalog_metadata(_rows(60)))
        assert small == large == 1


def test_merge_matches_like_the_single_row_lookup(app):
    with app.app_context():
        _seed(20)
        rows = _rows(20)
        merged = {r["name"]: r for r in _merge_catalog_metadata([dict(r) for r in rows])}
        for row in rows:
            expected = get_entry_for_inventory("c1", row["namespace"], row["name"], row["workloadNames"][0])
            assert merged[row["name"]].get("catalogEntryId") == (expected.id if expected else None)
            if expected:
                assert merged[row["name"]]["ownerTeam"] == expected.owner_team
