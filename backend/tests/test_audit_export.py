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
