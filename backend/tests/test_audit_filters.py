"""Audit-log list filters: from/to (UTC), actor and action, with the paging contract.

``hasMore`` is exact; ``total`` is exact once the end is reached and otherwise
counts only what matches the same filters.
"""

import csv
import io
from datetime import datetime, timedelta, timezone

from sqlalchemy import text

from api.db import db
from api.models import AuditLog, User

from .conftest import auth_headers

BASE = datetime(2026, 3, 10, 12, 0, tzinfo=timezone.utc)


def _seed():
    admin = User.query.filter_by(username="admin").first()
    rows = []
    # Five admin entries an hour apart (12:00 .. 16:00), three automation ones a day later.
    for index in range(5):
        rows.append(AuditLog(actor_user_id=admin.id, action="filter_probe_user", target_type="cluster",
                             target_id=f"c{index}", created_at=BASE + timedelta(hours=index)))
    for index in range(3):
        rows.append(AuditLog(actor_user_id=None, action="filter_probe_automation", target_type="cluster",
                             target_id=f"a{index}", created_at=BASE + timedelta(days=1, hours=index)))
    db.session.add_all(rows)
    db.session.commit()


def _page(client, token, query):
    resp = client.get(f"/api/audit-logs{query}", headers=auth_headers(token))
    assert resp.status_code == 200, resp.get_json()
    return resp.get_json()["data"]


def test_date_range_is_utc_from_inclusive_to_exclusive(client, admin_token):
    _seed()
    # 13:00 .. 15:00 UTC: the 13:00 and 14:00 entries, not 15:00.
    page = _page(client, admin_token, "?from=2026-03-10T13:00:00Z&to=2026-03-10T15:00:00Z")
    assert sorted(e["targetId"] for e in page["items"]) == ["c1", "c2"]
    assert page["hasMore"] is False and page["total"] == 2

    # A zone is honored (15:00+02:00 is 13:00 UTC); a naive value reads as UTC.
    page = _page(client, admin_token, "?from=2026-03-10T15:00:00%2B02:00&to=2026-03-10T14:00:00")
    assert [e["targetId"] for e in page["items"]] == ["c1"]

    # A bare date is that day's UTC midnight.
    page = _page(client, admin_token, "?from=2026-03-11&to=2026-03-12&action=filter_probe")
    assert sorted(e["targetId"] for e in page["items"]) == ["a0", "a1", "a2"]


def test_actor_filter_by_username_and_automation(client, admin_token):
    _seed()
    window = "&from=2026-03-01&to=2026-04-01"
    page = _page(client, admin_token, f"?actor=admin{window}")
    assert {e["action"] for e in page["items"]} == {"filter_probe_user"}
    assert page["total"] == 5

    page = _page(client, admin_token, f"?actor=KubeSight%20automation{window}")
    assert {e["targetId"] for e in page["items"]} == {"a0", "a1", "a2"}

    # Someone who never existed matches nothing (rather than everything).
    page = _page(client, admin_token, f"?actor=nobody-here{window}")
    assert page["items"] == [] and page["total"] == 0 and page["hasMore"] is False


def test_filtered_paging_keeps_the_contract(client, admin_token):
    _seed()
    query = "?actor=admin&from=2026-03-10&to=2026-03-11"
    first = _page(client, admin_token, f"{query}&limit=2")
    assert first["count"] == 2 and first["hasMore"] is True
    # Full page: the total counts the filtered rows, not the whole table.
    assert first["total"] == 5
    # Newest first.
    assert [e["targetId"] for e in first["items"]] == ["c4", "c3"]

    second = _page(client, admin_token, f"{query}&limit=2&offset=2")
    assert [e["targetId"] for e in second["items"]] == ["c2", "c1"] and second["hasMore"] is True

    last = _page(client, admin_token, f"{query}&limit=2&offset=4")
    assert [e["targetId"] for e in last["items"]] == ["c0"]
    assert last["hasMore"] is False and last["total"] == 5

    past = _page(client, admin_token, f"{query}&limit=2&offset=50")
    assert past["count"] == 0 and past["hasMore"] is False and past["total"] == 5


def test_bad_dates_are_rejected(client, admin_token):
    resp = client.get("/api/audit-logs?from=yesterday", headers=auth_headers(admin_token))
    assert resp.status_code == 400
    resp = client.get(
        "/api/audit-logs?from=2026-03-11&to=2026-03-10", headers=auth_headers(admin_token)
    )
    assert resp.status_code == 400
    resp = client.get("/api/audit-logs/export?to=nope", headers=auth_headers(admin_token))
    assert resp.status_code == 400


def test_export_honors_the_date_range(client, admin_token):
    _seed()
    resp = client.get(
        "/api/audit-logs/export?from=2026-03-11T12:00:00Z&to=2026-03-11T13:30:00Z",
        headers=auth_headers(admin_token),
    )
    assert resp.status_code == 200
    rows = list(csv.reader(io.StringIO(resp.get_data(as_text=True).lstrip("﻿"))))[1:]
    assert [(r[1], r[4]) for r in rows] == [("KubeSight automation", "a1"), ("KubeSight automation", "a0")]


def test_actor_choices_list_users_and_automation(client, admin_token, viewer_token):
    resp = client.get("/api/audit-logs/actors", headers=auth_headers(admin_token))
    assert resp.status_code == 200
    items = resp.get_json()["data"]["items"]
    assert items[0] == "KubeSight automation"
    assert "admin" in items and "viewer" in items
    assert client.get("/api/audit-logs/actors", headers=auth_headers(viewer_token)).status_code == 403


def test_filtered_queries_use_an_index(app):
    """SQLite's plan for each filter walks an index, never the whole table."""
    if db.engine.dialect.name != "sqlite":
        return
    cases = {
        "created_at >= :a AND created_at < :b": {"a": "2026-01-01", "b": "2026-02-01"},
        "actor_user_id = :u AND created_at >= :a": {"u": 1, "a": "2026-01-01"},
        "actor_user_id IS NULL AND created_at < :b": {"b": "2026-02-01"},
    }
    for where, params in cases.items():
        plan = db.session.execute(
            text(f"EXPLAIN QUERY PLAN SELECT * FROM audit_logs WHERE {where} "
                 "ORDER BY created_at DESC LIMIT 201"),
            params,
        ).fetchall()
        detail = " ".join(str(row[-1]) for row in plan)
        assert "INDEX" in detail.upper(), (where, detail)
        assert "SCAN audit_logs" not in detail or "USING" in detail.upper(), (where, detail)
