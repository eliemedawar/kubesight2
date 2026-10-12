"""The sidebar badge and the history pages, without building the whole inbox.

Locked here: the badge count equals the inbox's "waiting on me" for every kind
and every viewer, its short cache forgets as soon as something approvable
changes, and history pages walk backwards with the ``before`` cursor.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import event

from api.db import db
from api.models import (
    ChangeBundle,
    ChangeBundleItem,
    ChangeBundleVote,
    ClusterBuild,
    ClusterProvisionJob,
    DeploymentRequest,
    DeploymentRequestVote,
    User,
)
from api.services import approvals_service
from tests.conftest import auth_headers
from tests.test_approvals import _get, _inbox, _user, admin2_token  # noqa: F401
from tests.test_ticket_agent import agent, writes  # noqa: F401
from tests.test_ci_approval_stage import (  # noqa: F401  (fixtures are used by name)
    _approval,
    _no_cluster_approvals,
    _run,
    _save,
    _stages,
    service,
)


def _count(client, token):
    return _get(client, token, "/api/approvals/count")["waitingOnMe"]


def _seed_everything():
    admin, admin2 = _user("admin"), _user("admin2")
    admin.email, admin2.email = "admin@example.com", "admin2@example.com"
    now = datetime.now(timezone.utc)
    for i, owner in enumerate([admin, admin2, admin2, None]):
        row = DeploymentRequest(requester_id=owner.id if owner else None, cluster_id="prod-us-east",
                                cluster_name="prod", message=f"r{i}", status="pending",
                                created_at=now - timedelta(minutes=i))
        db.session.add(row)
        db.session.flush()
        if i == 2:  # admin already voted on this one
            db.session.add(DeploymentRequestVote(request_id=row.id, voter_email="ADMIN@example.com",
                                                 decision="decline"))
    for i, owner in enumerate([admin, admin2, admin2, admin2, None]):
        bundle = ChangeBundle(requester_user_id=owner.id if owner else None, status="pending_approval",
                              required_approvals=2, total_recipients=3, created_at=now - timedelta(minutes=i))
        db.session.add(bundle)
        db.session.flush()
        db.session.add(ChangeBundleItem(bundle_id=bundle.id, position=0, action_type="apply_yaml",
                                        cluster_id="prod-us-east", namespace="pay", resource_name="api",
                                        new_payload_json={"execution": {"mode": "apply"}}, yaml_preview="x"))
        if i == 2:  # admin approved: no longer waits on admin
            db.session.add(ChangeBundleVote(bundle_id=bundle.id, voter_email="admin@example.com",
                                            decision="approve"))
        if i == 3:  # admin declined: a decline does not count as "already approved"
            db.session.add(ChangeBundleVote(bundle_id=bundle.id, voter_email="admin@example.com",
                                            decision="decline"))
    build = ClusterBuild(name="uat-09", status="vms_ready", machine_source="vmware", k8s_version="1.32.4")
    db.session.add(build)
    db.session.flush()
    for owner in (admin, admin2, None):
        db.session.add(ClusterProvisionJob(build_id=build.id, operation="destroy", status="awaiting_approval",
                                           requested_by="x", requested_by_user_id=owner.id if owner else None,
                                           plan_cipher="secret" * 100, plan_text="t" * 1000))
    # A destroy whose build is gone never shows.
    db.session.add(ClusterProvisionJob(build_id=build.id + 999, operation="destroy",
                                       status="awaiting_approval", requested_by="x"))
    db.session.commit()


def test_the_badge_equals_the_inbox_for_every_viewer(client, admin_token, admin2_token, viewer_token):
    _seed_everything()
    for token in (admin_token, admin2_token, viewer_token):
        mine = _inbox(client, token)
        assert _count(client, token) == mine["counts"]["waitingOnMe"] == len(mine["items"])
    # admin: 3 requests not theirs minus 1 voted; 4 bundles not theirs minus 1 approved;
    # all 3 destroys (an admin needs no second person, so their own counts too).
    assert _count(client, admin_token) == 2 + 3 + 3
    assert _count(client, viewer_token) == 0


def test_the_badge_counts_ci_stages_like_the_inbox(app, client, admin_token, admin2_token, service):
    _no_cluster_approvals(app)
    assert _save(client, admin_token, service.pipeline_id, _stages(_approval(), deploy=False)).status_code == 200
    _run(app, client, admin_token, service.id)
    assert _count(client, admin_token) == len(_inbox(client, admin_token)["items"]) == 0
    assert _count(client, admin2_token) == len(_inbox(client, admin2_token)["items"]) == 1


def test_the_badge_counts_hermes_like_the_inbox(client, admin_token, viewer_token, agent, writes):
    from tests.test_ticket_agent import _request, _ticket

    _request(_ticket().id)
    assert _count(client, admin_token) == len(_inbox(client, admin_token)["items"]) == 1
    assert _count(client, viewer_token) == 0


def test_the_badge_does_not_serialize_the_inbox(app, client, admin_token, admin2_token):
    _seed_everything()
    seen = []

    def hook(conn, cursor, statement, *args):
        seen.append(statement)

    event.listen(db.engine, "before_cursor_execute", hook)
    try:
        _count(client, admin_token)
    finally:
        event.remove(db.engine, "before_cursor_execute", hook)
    assert not any("change_bundle_items" in s for s in seen)
    assert len(seen) < 25, len(seen)


def test_the_badge_cache_forgets_on_a_new_approval(app, client, admin_token, admin2_token):
    app.config["APPROVALS_COUNT_CACHE"] = True
    approvals_service.invalidate_waiting_counts()
    try:
        assert _count(client, admin_token) == 0
        bundle = ChangeBundle(requester_user_id=_user("admin2").id, status="pending_approval")
        db.session.add(bundle)
        db.session.commit()  # an ORM write invalidates at once
        assert _count(client, admin_token) == 1
        # A write that bypasses the ORM is only seen once the cache expires.
        db.session.execute(db.text("UPDATE change_bundles SET status='rejected'"))
        db.session.commit()
        assert _count(client, admin_token) == 1
        approvals_service.invalidate_waiting_counts()
        assert _count(client, admin_token) == 0
    finally:
        app.config.pop("APPROVALS_COUNT_CACHE", None)
        approvals_service.invalidate_waiting_counts()


def test_history_pages_with_the_before_cursor(client, admin_token):
    base = datetime(2026, 10, 1, 12, 0, tzinfo=timezone.utc)
    admin = _user("admin")
    for i in range(7):
        db.session.add(DeploymentRequest(requester_id=admin.id, cluster_id="c", cluster_name="c", message="m",
                                         status="approved", created_at=base - timedelta(days=1),
                                         decided_at=base - timedelta(hours=i)))
        db.session.add(ChangeBundle(requester_user_id=admin.id, status="completed",
                                    approved_at=base - timedelta(hours=i, minutes=30),
                                    updated_at=base + timedelta(days=1)))
    db.session.commit()
    seen, before = [], None
    while True:
        path = "/api/approvals/history?limit=4" + (f"&before={before}" if before else "")
        page = _get(client, admin_token, path.replace("+", "%2B"))
        seen += page["items"]
        before = page["nextBefore"]
        if not before:
            break
    assert len(seen) == 14 and len({i["key"] for i in seen}) == 14
    moments = [i["decidedAt"] for i in seen]
    assert moments == sorted(moments, reverse=True)
    assert seen[0]["kind"] == "deployment_request" and seen[1]["kind"] == "change_bundle"


def test_history_loads_users_once_and_no_payloads(client, admin_token):
    users = []
    for n in range(6):
        u = User(username=f"h{n}", email=f"h{n}@x.com", is_active=True, role_id=_user("admin").role_id)
        db.session.add(u)
        users.append(u)
    db.session.flush()
    for n, u in enumerate(users):
        db.session.add(DeploymentRequest(requester_id=u.id, decided_by_user_id=users[-1 - n].id,
                                         cluster_id="c", cluster_name="c", message="m", status="approved"))
        bundle = ChangeBundle(requester_user_id=u.id, approved_by_user_id=users[-1 - n].id, status="completed")
        db.session.add(bundle)
        db.session.flush()
        db.session.add(ChangeBundleItem(bundle_id=bundle.id, position=0, action_type="apply_yaml",
                                        cluster_id="c", new_payload_json={"big": "x" * 1000}, yaml_preview="y"))
    db.session.commit()
    db.session.expunge_all()
    seen = []

    def hook(conn, cursor, statement, *args):
        seen.append(statement)

    event.listen(db.engine, "before_cursor_execute", hook)
    try:
        items = _get(client, admin_token, "/api/approvals/history")["items"]
    finally:
        event.remove(db.engine, "before_cursor_execute", hook)
    assert len(items) == 12
    assert not any("new_payload_json" in s for s in seen)
    assert len(seen) < 30, len(seen)
