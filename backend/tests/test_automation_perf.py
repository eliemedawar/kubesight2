"""Deploy automation + deployment requests: fewer writes, probes and queries."""

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import event

from api.db import db
from api.models import ChangeBundle, DeployAutomationRun, DeploymentRequest, User
from api.services import deploy_automation_service as svc
from api.services import jenkins_client


def _run(**kwargs):
    values = dict(
        cluster_id="prod-us-east",
        namespace="payments",
        deployment_name="payments-api",
        image_repo="registry.example.com/team/payments-api",
        image_tag="1.0.0",
        status="queued",
    )
    values.update(kwargs)
    run = DeployAutomationRun(**values)
    db.session.add(run)
    db.session.commit()
    return run


# ── A9: a waiting run is not rewritten every tick ───────────────────────────


def test_set_step_is_a_noop_when_nothing_changed(app):
    run = _run()
    svc._set_step(run, "build", "run", "queued in Jenkins: waiting for executor")
    db.session.commit()
    steps_before = run.steps
    at_before = next(s for s in run.steps if s["key"] == "build")["at"]

    svc._set_step(run, "build", "run", "queued in Jenkins: waiting for executor")
    assert run.steps is steps_before  # not reassigned -> no UPDATE
    assert run not in db.session.dirty

    svc._set_step(run, "build", "run", "router build #7 running")
    step = next(s for s in run.steps if s["key"] == "build")
    assert step["detail"] == "router build #7 running"
    assert step["at"] is not None and step["at"] >= at_before


# ── A2: Jenkins poll cadence + unreachable host skipped for the pass ────────


def test_jenkins_build_is_polled_at_most_every_30_seconds(app, monkeypatch):
    monkeypatch.setattr(svc, "_CADENCE_IN_TESTS", True)
    run = _run(status="building")
    svc._last_poll.pop(run.id, None)
    assert svc._jenkins_poll_due(run) is True
    assert svc._jenkins_poll_due(run) is False
    svc._last_poll[run.id] -= svc.JENKINS_POLL_SECONDS + 1
    assert svc._jenkins_poll_due(run) is True
    svc._forget_poll_times(set())
    assert run.id not in svc._last_poll


def test_dead_jenkins_is_asked_once_per_pass(app, monkeypatch):
    jrow = svc.get_or_create_jenkins()
    jrow.base_url = "https://jenkins.example.com"
    db.session.commit()
    now = datetime.now(timezone.utc)
    first = _run(status="building", jenkins_queue_url="q/1", build_triggered_at=now)
    second = _run(
        status="building", deployment_name="ledger", jenkins_queue_url="q/2", build_triggered_at=now
    )
    calls = []

    def _timeout(cfg, url):
        calls.append(url)
        raise jenkins_client.JenkinsError("timed out")

    monkeypatch.setattr(jenkins_client, "queue_state", _timeout)
    svc.advance_runs()
    assert calls == ["q/1"]  # the second run did not wait on the same dead server
    db.session.expire_all()
    assert db.session.get(DeployAutomationRun, first.id).retry_count == 1
    assert db.session.get(DeployAutomationRun, second.id).retry_count == 0

    # Next pass: asked again (the skip only lasts one pass).
    svc.advance_runs()
    assert calls == ["q/1", "q/1"]


def test_unreachable_registry_is_asked_once_per_pass(app, monkeypatch):
    calls = []

    def _unreachable(image, **kwargs):
        calls.append(image)
        return {"status": "unreachable", "message": "timed out"}

    monkeypatch.setattr("api.services.registry_service.check_image", _unreachable)
    _run(status="checking_image")
    _run(status="checking_image", deployment_name="ledger", image_repo="registry.example.com/team/ledger")
    svc.advance_runs()
    assert len(calls) == 1


# ── A7: run list in a fixed number of queries ───────────────────────────────


def test_list_runs_reads_bundle_status_in_one_query(app):
    bundles = [ChangeBundle(note=f"b{i}", status="pending_approval") for i in range(3)]
    db.session.add_all(bundles)
    db.session.commit()
    for i, bundle in enumerate(bundles):
        _run(deployment_name=f"app-{i}", bundle_id=bundle.id, status="awaiting_approval")
    _run(deployment_name="orphan", bundle_id=999999, status="awaiting_approval")

    statements = []

    def _listener(conn, cursor, statement, params, context, executemany):
        statements.append(statement)

    event.listen(db.engine, "before_cursor_execute", _listener)
    try:
        items = svc.list_runs(50)
    finally:
        event.remove(db.engine, "before_cursor_execute", _listener)
    by_name = {item["deploymentName"]: item for item in items}
    assert by_name["app-0"]["bundleStatus"] == "pending_approval"
    assert by_name["orphan"]["bundleStatus"] == "deleted"
    bundle_reads = [s for s in statements if "change_bundles" in s]
    assert len(bundle_reads) == 1
    assert "change_bundle_items" not in bundle_reads[0]


# ── A10: admins found without loading every user ────────────────────────────


def test_admin_emails_by_role(app):
    admin = User.query.filter_by(username="admin").first()
    admin.email = "root@example.com"
    db.session.commit()
    assert "root@example.com" in svc._admin_emails()
    admin.is_active = False
    db.session.commit()
    assert "root@example.com" not in svc._admin_emails()


# ── A11: overdue / active windows filtered in SQL ───────────────────────────


def test_overdue_requests_declined_and_active_approval_window(app):
    from api.services import deployment_request_service as drs

    admin = User.query.filter_by(username="admin").first()
    now = datetime.now(timezone.utc)
    overdue = DeploymentRequest(
        requester_id=admin.id, cluster_id="c1", cluster_name="c1", message="m", status="pending",
        requested_window_start=now - timedelta(minutes=5), requested_window_end=now + timedelta(hours=1),
    )
    future = DeploymentRequest(
        requester_id=admin.id, cluster_id="c1", cluster_name="c1", message="m", status="pending",
        requested_window_start=now + timedelta(hours=1), requested_window_end=now + timedelta(hours=2),
    )
    expired = DeploymentRequest(
        requester_id=admin.id, cluster_id="c2", cluster_name="c2", message="m", status="approved",
        requested_window_start=now - timedelta(hours=3), requested_window_end=now - timedelta(hours=1),
    )
    live = DeploymentRequest(
        requester_id=admin.id, cluster_id="c2", cluster_name="c2", message="m", status="approved",
        requested_window_start=now - timedelta(hours=1), requested_window_end=now + timedelta(hours=1),
    )
    db.session.add_all([overdue, future, expired, live])
    db.session.commit()

    assert drs.auto_decline_overdue_requests() == 1
    assert db.session.get(DeploymentRequest, overdue.id).status == "declined"
    assert db.session.get(DeploymentRequest, future.id).status == "pending"
    assert drs._active_approval(admin, "c2").id == live.id
    assert drs._active_approval(admin, "c1") is None


# ── A4: approver emails leave the request thread ────────────────────────────


def test_management_emails_are_queued_off_the_request_thread(app, monkeypatch):
    import threading

    from api.services import deployment_request_service as drs

    app.config["TESTING"] = False
    sent = []
    done = threading.Event()

    def _send(to, subject, body, **kwargs):
        sent.append(to)
        if len(sent) == 2:
            done.set()

    monkeypatch.setattr(drs, "smtp_is_configured", lambda: True)
    monkeypatch.setattr(drs, "send_email", _send)
    monkeypatch.setattr(drs, "_build_email", lambda req, name, voter_email="": ("s", "b", "<p>h</p>"))
    req = DeploymentRequest(cluster_id="c1", cluster_name="c1", message="m", status="pending")
    db.session.add(req)
    db.session.commit()
    try:
        result = drs._notify_management(req, "Elie", ["a@x.io", "b@x.io"])
    finally:
        app.config["TESTING"] = True
    assert result["status"] == "queued" and result["sent"] == 2
    assert done.wait(5)
    assert sorted(sent) == ["a@x.io", "b@x.io"]


# ── MCP-3 / MCP-small: API token use and the MCP endpoint ───────────────────


def _api_token(user_id):
    import secrets
    from hashlib import sha256

    from api.models import ApiToken

    raw = "ksa_" + secrets.token_hex(20)
    row = ApiToken(
        user_id=user_id,
        name="agent",
        token_prefix=raw[:12],
        token_hash=sha256(raw.encode()).hexdigest(),
        is_active=True,
    )
    db.session.add(row)
    db.session.commit()
    return raw, row.id


def _aware_dt(value):
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value


def test_api_token_use_is_recorded_at_most_every_five_minutes(app, client):
    from api.models import ApiToken

    admin = User.query.filter_by(username="admin").first()
    raw, token_id = _api_token(admin.id)
    headers = {"Authorization": f"Bearer {raw}"}

    assert client.get("/api/auth/me", headers=headers).status_code == 200
    db.session.expire_all()
    first = db.session.get(ApiToken, token_id).last_used_at
    assert first is not None

    assert client.get("/api/auth/me", headers=headers).status_code == 200
    db.session.expire_all()
    assert db.session.get(ApiToken, token_id).last_used_at == first

    row = db.session.get(ApiToken, token_id)
    row.last_used_at = datetime.now(timezone.utc) - timedelta(minutes=6)
    db.session.commit()
    assert client.get("/api/auth/me", headers=headers).status_code == 200
    db.session.expire_all()
    last = _aware_dt(db.session.get(ApiToken, token_id).last_used_at)
    assert datetime.now(timezone.utc) - last < timedelta(minutes=1)


def test_mcp_tools_call_does_not_build_the_catalog(app, client, monkeypatch):
    from api.mcp import tools

    admin = User.query.filter_by(username="admin").first()
    raw, _ = _api_token(admin.id)
    headers = {"Authorization": f"Bearer {raw}"}
    built = []
    real = tools.definitions
    monkeypatch.setattr(tools, "definitions", lambda **kw: built.append(1) or real(**kw))

    ping = client.post(
        "/api/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "ping"}, headers=headers
    )
    assert ping.status_code == 200 and built == []
    listed = client.post(
        "/api/mcp", json={"jsonrpc": "2.0", "id": 2, "method": "tools/list"}, headers=headers
    )
    assert listed.status_code == 200 and built == [1]
    assert listed.get_json()["result"]["tools"]
