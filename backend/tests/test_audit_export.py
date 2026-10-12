"""Audit-log CSV export: the 'KubeSight automation' actor label + filters."""

import csv
import io

from api.db import db
from api.models import AuditLog, User

from .conftest import auth_headers


def _seed():
    admin = User.query.filter_by(username="admin").first()
    db.session.add_all(
        [
            AuditLog(
                actor_user_id=admin.id,
                action="login_success",
                target_type="user",
                target_id=str(admin.id),
                details={"ip": "10.0.0.1"},
            ),
            AuditLog(
                actor_user_id=None,  # system / automation action
                action="automation_run_deployed",
                target_type="deploy_automation_run",
                target_id="21",
                details={"clusterId": "kubesight-prod", "ticket": "TM-8430"},
            ),
            AuditLog(
                actor_user_id=None,
                action="automation_build_triggered",
                target_type="deploy_automation_run",
                target_id="21",
                details={"clusterId": "verto-sit", "ticket": "TM-8431"},
            ),
        ]
    )
    db.session.commit()


def _rows(client, token, query=""):
    resp = client.get(f"/api/audit-logs/export{query}", headers=auth_headers(token))
    assert resp.status_code == 200
    assert "text/csv" in resp.headers["Content-Type"]
    assert "attachment" in resp.headers["Content-Disposition"]
    text = resp.get_data(as_text=True).lstrip("﻿")
    return list(csv.reader(io.StringIO(text)))


def test_export_labels_and_filters(client, admin_token, app):
    _seed()

    # Unfiltered: header + at least our seeded rows (the login fixture also logs
    # an entry), automation actor labeled.
    rows = _rows(client, admin_token)
    assert rows[0] == ["Date/Time (UTC)", "Actor", "Action", "Target Type", "Target ID", "Details"]
    body = rows[1:]
    assert len(body) >= 3
    actors = {r[1] for r in body}
    assert "KubeSight automation" in actors
    assert "admin" in actors

    # Filter by the automation actor → only the two system rows.
    rows = _rows(client, admin_token, "?actor=KubeSight%20automation")
    body = rows[1:]
    assert len(body) == 2
    assert all(r[1] == "KubeSight automation" for r in body)

    # Filter by action substring.
    rows = _rows(client, admin_token, "?action=build_triggered")
    body = rows[1:]
    assert len(body) == 1
    assert body[0][2] == "automation_build_triggered"

    # Filter by cluster (derived from details.clusterId).
    rows = _rows(client, admin_token, "?cluster=kubesight-prod")
    body = rows[1:]
    assert len(body) == 1
    assert body[0][2] == "automation_run_deployed"


def test_export_requires_permission(client, viewer_token, app):
    _seed()
    resp = client.get("/api/audit-logs/export", headers=auth_headers(viewer_token))
    assert resp.status_code == 403


def test_audit_list_reports_more_without_counting_short_pages(client, admin_token):
    from api.db import db
    from api.models import AuditLog

    for index in range(5):
        db.session.add(AuditLog(action="paging_probe", target_type="cluster", target_id=str(index)))
    db.session.commit()
    total_rows = AuditLog.query.count()

    resp = client.get("/api/audit-logs?limit=2", headers=auth_headers(admin_token))
    page = resp.get_json()["data"]
    assert page["count"] == 2
    assert page["hasMore"] is True
    assert page["total"] == total_rows

    resp = client.get(
        f"/api/audit-logs?limit=2&offset={total_rows - 1}", headers=auth_headers(admin_token)
    )
    tail = resp.get_json()["data"]
    assert tail["count"] == 1
    assert tail["hasMore"] is False
    assert tail["total"] == total_rows


def test_mcp_audit_listing_names_actors(app, admin_token):
    from api.services.audit_service import list_audit_logs

    result = list_audit_logs(limit=5)
    assert result["count"] >= 1
    assert any(item.get("actorUsername") == "admin" for item in result["items"])


def test_audit_list_past_the_end_still_reports_the_total(client, admin_token):
    from api.models import AuditLog

    total_rows = AuditLog.query.count()
    resp = client.get("/api/audit-logs?limit=5&offset=100000", headers=auth_headers(admin_token))
    page = resp.get_json()["data"]
    assert page["count"] == 0
    assert page["hasMore"] is False
    assert page["total"] == total_rows
