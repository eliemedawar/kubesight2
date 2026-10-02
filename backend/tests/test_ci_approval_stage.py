"""The Approval stage: a build waits until enough of the right people say yes.

What is locked here:

* the configuration rules (somebody must be able to approve, the number of
  approvals must be reachable, named approvers must be real active users, no
  commands on a server stage) and the ordering rule (server stages last, an
  Approval may sit before a Deploy);
* the wait: the build runs, the stage sits in ``waiting_approval`` and lists
  can find it (``awaitingApproval``, ``status=awaiting_approval``);
* deciding: named users and/or ``ci_builds:approve`` holders only, never the
  person who started the build unless allowed, minimum approvals of DISTINCT
  people, one rejection fails the build, later stages start at once on
  approval and are skipped on rejection;
* time and cancellation: running out of time fails "not approved", cancelling
  closes it, a restart resumes with every decision kept;
* email to approvers is best effort and never fails the build;
* a ticket-driven run waits on the approval instead of misreading the build.
"""

from __future__ import annotations

from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from api.db import db
from api.models import DeployAutomationRun, DeploymentRequestSetting, Permission, Role, User
from api.models_application_intelligence import BitbucketCredentialProfile
from api.models_ci import CiBuild
from api.secret_encryption import encrypt_secret
from api.services.ci import approval_config
from tests.conftest import auth_headers


@pytest.fixture()
def service(app, client, admin_token):
    with app.app_context():
        credential = BitbucketCredentialProfile(
            name="ci-token",
            provider="bitbucket",
            credential_type="repository_access_token",
            secret_cipher=encrypt_secret("clone-token-value"),
            read_only=True,
            enabled=True,
        )
        db.session.add(credential)
        db.session.commit()
        credential_id = credential.id
    service_id = client.post(
        "/api/ci/services",
        json={"name": "Payments Api", "applicationType": "java"},
        headers=auth_headers(admin_token),
    ).get_json()["data"]["id"]
    client.put(
        f"/api/ci/services/{service_id}/source",
        json={
            "repositoryUrl": "https://bitbucket.org/areeba/payments-api",
            "defaultBranch": "main",
            "credentialProfileId": credential_id,
        },
        headers=auth_headers(admin_token),
    )
    pipeline_id = client.get(
        f"/api/ci/services/{service_id}/pipelines", headers=auth_headers(admin_token)
    ).get_json()["data"]["items"][0]["id"]
    return SimpleNamespace(id=service_id, pipeline_id=pipeline_id)


def _user_id(app, username):
    with app.app_context():
        return User.query.filter_by(username=username).first().id


def _approval(**overrides):
    config = {
        "instructions": "Check the release notes before approving.",
        "users": [],
        "anyoneWithPermission": True,
        "minApprovals": 1,
        "allowSelfApproval": False,
        "notify": False,
    }
    config.update(overrides)
    return config


def _deploy():
    return {
        "clusterId": "prod-us-east",
        "namespace": "payments",
        "deploymentName": "payments-api",
        "containerName": "",
        "image": "registry.local/payments:2.0.0",
        "createIfMissing": False,
        "manifest": "",
    }


def _stages(approval=None, *, deploy=True, extra_after=None, timeout=3600):
    stages = [
        {"name": "Checkout", "stageType": "checkout", "runnerLabels": ["mock"]},
        {"name": "Build", "stageType": "command", "commands": ["make"], "runnerLabels": ["mock"]},
        {"name": "Approve release", "stageType": "approval", "approval": approval or _approval(),
         "timeoutSeconds": timeout},
    ]
    if deploy:
        stages.append({"name": "Deploy", "stageType": "deploy", "deploy": _deploy()})
    if extra_after:
        stages.append(extra_after)
    return stages


def _save(client, token, pipeline_id, stages):
    return client.put(
        f"/api/ci/pipelines/{pipeline_id}",
        json={"parameters": [], "stages": stages},
        headers=auth_headers(token),
    )


def _no_cluster_approvals(app):
    with app.app_context():
        row = DeploymentRequestSetting.query.first()
        if row is None:
            row = DeploymentRequestSetting()
            db.session.add(row)
        row.required_approvals = 0
        db.session.commit()


def _advance(app, build_id, passes=40):
    from api.services.ci import engine
    from api.services.ci.runners import mock as mock_runner

    original = mock_runner._STAGE_SECONDS
    mock_runner._STAGE_SECONDS = 0.0
    try:
        with app.app_context():
            for _ in range(passes):
                engine.advance_ci_builds()
                build = db.session.get(CiBuild, build_id)
                if build.status not in ("queued", "running"):
                    break
                waiting = next((s for s in build.stages if s.stage_type == "approval"), None)
                if waiting is not None and waiting.status == "running":
                    break
    finally:
        mock_runner._STAGE_SECONDS = original


def _run(app, client, token, service_id):
    build_id = client.post(
        f"/api/ci/services/{service_id}/builds", json={}, headers=auth_headers(token)
    ).get_json()["data"]["id"]
    _advance(app, build_id)
    return build_id


def _build(client, token, build_id):
    return client.get(f"/api/ci/builds/{build_id}", headers=auth_headers(token)).get_json()["data"]


def _approval_stage(data):
    return next(s for s in data["stages"] if s["stageType"] == "approval")


def _decide(client, token, build_id, stage_id, action="approve", comment=""):
    return client.post(
        f"/api/ci/builds/{build_id}/stages/{stage_id}/{action}",
        json={"comment": comment},
        headers=auth_headers(token),
    )


def _second_operator(app):
    """Another person holding ci_builds:approve, to reach two approvals."""
    with app.app_context():
        operator = User.query.filter_by(username="operator").first()
        other = User(username="op2", email="op2@example.com", is_active=True, role_id=operator.role_id)
        other.password_hash = operator.password_hash
        db.session.add(other)
        db.session.commit()
        return other.id


def _login(client, username, password):
    response = client.post("/api/auth/login", json={"username": username, "password": password})
    return response.get_json()["data"]["token"]


# ---------------------------------------------------------------------------
# Configuration rules
# ---------------------------------------------------------------------------

def test_somebody_must_be_able_to_approve():
    with pytest.raises(approval_config.ApprovalConfigError, match="nobody who may approve"):
        approval_config.normalize({"users": [], "anyoneWithPermission": False}, "approval", "Gate")


def test_the_number_of_approvals_must_be_reachable():
    with pytest.raises(approval_config.ApprovalConfigError, match="could never pass"):
        approval_config.normalize(
            {"users": [1], "anyoneWithPermission": False, "minApprovals": 2},
            "approval", "Gate", known_users={1: "alice"},
        )
    with pytest.raises(approval_config.ApprovalConfigError, match="between 1 and 10"):
        approval_config.normalize({"anyoneWithPermission": True, "minApprovals": 0}, "approval", "Gate")


def test_defaults_are_one_approval_and_no_self_approval():
    config = approval_config.normalize({"anyoneWithPermission": True}, "approval", "Gate")
    assert config["minApprovals"] == 1
    assert config["allowSelfApproval"] is False
    assert config["notify"] is False


def test_only_an_approval_stage_carries_approvers():
    assert approval_config.normalize(None, "command", "Build") is None
    with pytest.raises(approval_config.ApprovalConfigError, match="asks nobody"):
        approval_config.normalize(_approval(), "command", "Build")


def test_named_approvers_must_be_active_users(app, client, admin_token, service):
    response = _save(client, admin_token, service.pipeline_id, _stages(_approval(users=[9999], anyoneWithPermission=False)))
    assert response.status_code == 400
    assert "not an active KubeSight user" in response.get_json()["error"]


def test_named_approvers_are_stored_with_their_names(app, client, admin_token, service):
    operator = _user_id(app, "operator")
    data = _save(
        client, admin_token, service.pipeline_id, _stages(_approval(users=[operator], anyoneWithPermission=False))
    ).get_json()["data"]
    assert data["stages"][2]["approval"]["users"] == [{"id": operator, "username": "operator"}]


def test_an_approval_stage_runs_no_commands(client, admin_token, service):
    stages = _stages()
    stages[2]["commands"] = ["echo approve"]
    response = _save(client, admin_token, service.pipeline_id, stages)
    assert response.status_code == 400
    assert "no image or commands" in response.get_json()["error"]


def test_nothing_a_runner_executes_may_follow_an_approval(client, admin_token, service):
    response = _save(
        client, admin_token, service.pipeline_id,
        _stages(deploy=False, extra_after={"name": "Smoke test", "stageType": "command", "commands": ["curl x"]}),
    )
    assert response.status_code == 400
    error = response.get_json()["error"]
    assert "Smoke test" in error and "Approval stage 'Approve release'" in error and "last" in error


def test_an_approval_may_sit_before_a_deploy(client, admin_token, service):
    response = _save(client, admin_token, service.pipeline_id, _stages())
    assert response.status_code == 200, response.get_json()


# ---------------------------------------------------------------------------
# Waiting and deciding
# ---------------------------------------------------------------------------

def test_the_build_waits_and_lists_can_find_it(app, client, admin_token, service):
    _save(client, admin_token, service.pipeline_id, _stages())
    build_id = _run(app, client, admin_token, service.id)

    data = _build(client, admin_token, build_id)
    assert data["status"] == "running"
    stage = _approval_stage(data)
    assert stage["status"] == "running"
    assert stage["runnerName"] is None
    assert stage["approval"]["phase"] == "waiting_approval"
    assert stage["approval"]["instructions"] == "Check the release notes before approving."
    assert stage["approval"]["startedBy"]["username"] == "admin"
    assert data["awaitingApproval"]["stageName"] == "Approve release"
    assert data["awaitingApproval"]["required"] == 1
    # The Deploy after it has not started.
    assert data["stages"][3]["status"] == "pending"

    listed = client.get(
        "/api/ci/builds?status=awaiting_approval", headers=auth_headers(admin_token)
    ).get_json()["data"]
    assert [item["id"] for item in listed["items"]] == [build_id]
    assert listed["items"][0]["awaitingApproval"]["approvals"] == 0


def test_the_person_who_started_the_build_cannot_approve_it(app, client, admin_token, service):
    _save(client, admin_token, service.pipeline_id, _stages())
    build_id = _run(app, client, admin_token, service.id)
    stage = _approval_stage(_build(client, admin_token, build_id))
    assert stage["approval"]["viewer"]["canApprove"] is False
    assert "You started this build" in stage["approval"]["viewer"]["reason"]

    response = _decide(client, admin_token, build_id, stage["id"])
    assert response.status_code == 403
    assert "You started this build" in response.get_json()["error"]
    assert _build(client, admin_token, build_id)["status"] == "running"


def test_self_approval_when_the_stage_allows_it(app, client, admin_token, service):
    _no_cluster_approvals(app)
    _save(client, admin_token, service.pipeline_id, _stages(_approval(allowSelfApproval=True)))
    build_id = _run(app, client, admin_token, service.id)
    stage = _approval_stage(_build(client, admin_token, build_id))
    assert _decide(client, admin_token, build_id, stage["id"]).status_code == 200
    _advance(app, build_id)
    assert _build(client, admin_token, build_id)["status"] == "success"


def test_someone_without_the_permission_cannot_approve(app, client, admin_token, viewer_token, service):
    _save(client, admin_token, service.pipeline_id, _stages())
    build_id = _run(app, client, admin_token, service.id)
    stage = _approval_stage(_build(client, viewer_token, build_id))
    assert stage["approval"]["viewer"]["canApprove"] is False
    response = _decide(client, viewer_token, build_id, stage["id"])
    assert response.status_code == 403
    assert "Only anyone with the ci_builds:approve permission" in response.get_json()["error"]


def test_a_named_approver_needs_no_permission(app, client, admin_token, viewer_token, service):
    _no_cluster_approvals(app)
    viewer = _user_id(app, "viewer")
    _save(client, admin_token, service.pipeline_id, _stages(_approval(users=[viewer], anyoneWithPermission=False)))
    build_id = _run(app, client, admin_token, service.id)
    stage = _approval_stage(_build(client, viewer_token, build_id))
    assert stage["approval"]["viewer"]["canApprove"] is True
    assert _decide(client, viewer_token, build_id, stage["id"], comment="looks good").status_code == 200
    _advance(app, build_id)
    data = _build(client, admin_token, build_id)
    assert data["status"] == "success"
    decision = _approval_stage(data)["approval"]["decisions"][0]
    assert decision["username"] == "viewer" and decision["comment"] == "looks good"


def test_a_named_only_stage_refuses_permission_holders(app, client, admin_token, operator_token, service):
    viewer = _user_id(app, "viewer")
    _save(client, admin_token, service.pipeline_id, _stages(_approval(users=[viewer], anyoneWithPermission=False)))
    build_id = _run(app, client, admin_token, service.id)
    stage = _approval_stage(_build(client, admin_token, build_id))
    response = _decide(client, operator_token, build_id, stage["id"])
    assert response.status_code == 403
    assert "Only viewer may answer" in response.get_json()["error"]


def test_approval_starts_the_stages_after_it(app, client, admin_token, operator_token, service):
    _no_cluster_approvals(app)
    _save(client, admin_token, service.pipeline_id, _stages())
    build_id = _run(app, client, admin_token, service.id)
    stage = _approval_stage(_build(client, admin_token, build_id))

    response = _decide(client, operator_token, build_id, stage["id"], comment="ship it")
    assert response.status_code == 200, response.get_json()
    data = response.get_json()["data"]
    approval = _approval_stage(data)
    assert approval["status"] == "success"
    assert approval["approval"]["outcome"] == "approved"
    # Started in the same request, not at the next tick.
    assert data["stages"][3]["status"] in ("running", "success")
    _advance(app, build_id)
    data = _build(client, admin_token, build_id)
    assert data["status"] == "success", data
    assert data["awaitingApproval"] is None

    from api.models import AuditLog

    with app.app_context():
        actions = {row.action for row in AuditLog.query.all()}
    assert {"ci_build_approval_requested", "ci_build_stage_approved", "ci_build_approval_granted"} <= actions


def test_minimum_approvals_need_distinct_people(app, client, admin_token, operator_token, service):
    _no_cluster_approvals(app)
    _second_operator(app)
    _save(client, admin_token, service.pipeline_id, _stages(_approval(minApprovals=2)))
    build_id = _run(app, client, admin_token, service.id)
    stage = _approval_stage(_build(client, admin_token, build_id))

    assert _decide(client, operator_token, build_id, stage["id"]).status_code == 200
    data = _build(client, admin_token, build_id)
    assert data["status"] == "running"
    assert data["awaitingApproval"]["approvals"] == 1

    again = _decide(client, operator_token, build_id, stage["id"])
    assert again.status_code == 409 and "already approved" in again.get_json()["error"]

    other_token = _login(client, "op2", "operator123")
    assert _decide(client, other_token, build_id, stage["id"]).status_code == 200
    _advance(app, build_id)
    data = _build(client, admin_token, build_id)
    assert data["status"] == "success"
    assert _approval_stage(data)["approval"]["message"] == "Approved by operator, op2."


def test_a_rejection_fails_the_build_and_nothing_after_it_runs(app, client, admin_token, operator_token, service):
    _save(client, admin_token, service.pipeline_id, _stages())
    build_id = _run(app, client, admin_token, service.id)
    stage = _approval_stage(_build(client, admin_token, build_id))

    response = _decide(client, operator_token, build_id, stage["id"], action="reject", comment="not this week")
    assert response.status_code == 200
    _advance(app, build_id)
    data = _build(client, admin_token, build_id)
    assert data["status"] == "failed"
    approval = _approval_stage(data)
    assert approval["status"] == "failed"
    assert "Rejected by operator: not this week" in approval["error"]
    assert approval["approval"]["outcome"] == "rejected"
    assert data["stages"][3]["status"] == "skipped"
    assert _decide(client, operator_token, build_id, stage["id"]).status_code == 409


def test_running_out_of_time_fails_not_approved(app, client, admin_token, service):
    from api.services.ci import engine

    _save(client, admin_token, service.pipeline_id, _stages(timeout=60))
    build_id = _run(app, client, admin_token, service.id)
    with app.app_context():
        stage = next(s for s in db.session.get(CiBuild, build_id).stages if s.stage_type == "approval")
        stage.started_at = stage.started_at - timedelta(seconds=120)
        db.session.commit()
        engine.advance_ci_builds()
    data = _build(client, admin_token, build_id)
    assert data["status"] == "timeout"
    approval = _approval_stage(data)
    assert approval["status"] == "timeout"
    assert "Not approved within the stage's 1 min limit (0 of 1 approval)" in approval["error"]
    assert approval["approval"]["outcome"] == "timed_out"
    assert data["stages"][3]["status"] == "skipped"


def test_a_decision_after_the_window_closed_is_refused(app, client, admin_token, operator_token, service):
    _save(client, admin_token, service.pipeline_id, _stages(timeout=60))
    build_id = _run(app, client, admin_token, service.id)
    with app.app_context():
        stage = next(s for s in db.session.get(CiBuild, build_id).stages if s.stage_type == "approval")
        stage.started_at = stage.started_at - timedelta(seconds=120)
        db.session.commit()
        stage_id = stage.id
    response = _decide(client, operator_token, build_id, stage_id)
    assert response.status_code == 409 and "has closed" in response.get_json()["error"]


def test_cancelling_while_waiting_cancels(app, client, admin_token, service):
    from api.services.ci import engine

    _save(client, admin_token, service.pipeline_id, _stages())
    build_id = _run(app, client, admin_token, service.id)
    client.post(f"/api/ci/builds/{build_id}/cancel", headers=auth_headers(admin_token))
    with app.app_context():
        engine.advance_ci_builds()
    data = _build(client, admin_token, build_id)
    assert data["status"] == "cancelled"
    approval = _approval_stage(data)
    assert approval["status"] == "cancelled"
    assert approval["approval"]["outcome"] == "cancelled"
    assert data["stages"][3]["status"] == "skipped"


def test_a_restart_resumes_with_every_decision_kept(app, client, admin_token, operator_token, service):
    """Everything lives on the stage row: a new session (a restarted backend)
    sees the approval given before it and keeps waiting for the second."""
    _no_cluster_approvals(app)
    _second_operator(app)
    _save(client, admin_token, service.pipeline_id, _stages(_approval(minApprovals=2)))
    build_id = _run(app, client, admin_token, service.id)
    stage = _approval_stage(_build(client, admin_token, build_id))
    _decide(client, operator_token, build_id, stage["id"])

    with app.app_context():
        db.session.remove()  # Nothing held in memory survives.
    _advance(app, build_id, passes=5)
    data = _build(client, admin_token, build_id)
    assert data["status"] == "running"
    assert [d["username"] for d in _approval_stage(data)["approval"]["decisions"]] == ["operator"]

    _decide(client, _login(client, "op2", "operator123"), build_id, stage["id"])
    _advance(app, build_id)
    assert _build(client, admin_token, build_id)["status"] == "success"


def test_an_earlier_failure_means_nobody_is_asked(app, client, admin_token, service):
    from api.services.ci.runners import base as runner_base

    _save(client, admin_token, service.pipeline_id, _stages())
    adapter = runner_base.get_adapter("mock")
    original_poll = adapter.poll
    adapter.poll = lambda handle: runner_base.FAILED
    try:
        build_id = _run(app, client, admin_token, service.id)
    finally:
        adapter.poll = original_poll
    data = _build(client, admin_token, build_id)
    assert data["status"] == "failed"
    assert _approval_stage(data)["status"] == "skipped"


def test_a_whole_build_runner_gets_no_container_for_an_approval(app, client, admin_token, service):
    from api.services.ci import engine

    _save(client, admin_token, service.pipeline_id, _stages())
    build_id = client.post(
        f"/api/ci/services/{service.id}/builds", json={}, headers=auth_headers(admin_token)
    ).get_json()["data"]["id"]
    adapter = SimpleNamespace(supported_stage_types=lambda: {"checkout", "command"})
    with app.app_context():
        build = db.session.get(CiBuild, build_id)
        with patch.object(engine, "_build_execution", side_effect=lambda b, s, d, **k: s.name):
            assert engine._build_plan(build, adapter, "token") == ["Checkout", "Build"]


# ---------------------------------------------------------------------------
# Notification
# ---------------------------------------------------------------------------

def test_approvers_are_emailed_but_not_the_person_who_started_it(app, client, admin_token, service):
    sent = []
    _save(client, admin_token, service.pipeline_id, _stages(_approval(notify=True)))
    with patch("api.email_delivery.send_email", side_effect=lambda to, subject, body, **k: sent.append((to, subject, body))):
        build_id = _run(app, client, admin_token, service.id)
    recipients = {to for to, _, _ in sent}
    assert "operator@kubesight.local" in recipients
    assert "admin@kubesight.local" not in recipients  # Started the build.
    assert "viewer@kubesight.local" not in recipients  # Cannot approve.
    assert all("waiting for your approval" in subject for _, subject, _ in sent)
    assert "Check the release notes" in sent[0][2]
    notified = _approval_stage(_build(client, admin_token, build_id))["approval"]["notified"]
    assert "operator" in notified["recipients"]


def test_mail_that_fails_never_fails_the_build(app, client, admin_token, service):
    from api.email_delivery import EmailDeliveryError

    _save(client, admin_token, service.pipeline_id, _stages(_approval(notify=True)))
    with patch("api.email_delivery.send_email", side_effect=EmailDeliveryError("SMTP is not configured.")):
        build_id = _run(app, client, admin_token, service.id)
    data = _build(client, admin_token, build_id)
    assert data["status"] == "running"
    assert _approval_stage(data)["approval"]["phase"] == "waiting_approval"


# ---------------------------------------------------------------------------
# Tickets: a run waits on the approval
# ---------------------------------------------------------------------------

def test_a_ticket_run_waits_on_the_approval_and_fails_at_approval_when_rejected(
    app, client, admin_token, operator_token, service
):
    from api.services import deploy_automation_service as automation

    _save(client, admin_token, service.pipeline_id, _stages())
    build_id = _run(app, client, admin_token, service.id)
    with app.app_context():
        run = DeployAutomationRun(
            cluster_id="prod-us-east", namespace="payments", deployment_name="payments-api",
            image_repo="registry.local/payments", image_tag="2.0.0", ticket_number="DR-11",
            status="building", change_type="image", ci_build_id=build_id,
        )
        db.session.add(run)
        db.session.commit()
        run_id = run.id
        automation._do_poll_native_build(run)
        db.session.commit()
        assert run.status == "building"
        steps = {s["key"]: s for s in run.steps}
        assert steps["build"]["status"] == "run"
        assert "waiting for approval (0 of 1)" in steps["build"]["detail"]

    stage = _approval_stage(_build(client, admin_token, build_id))
    _decide(client, operator_token, build_id, stage["id"], action="reject", comment="freeze")
    _advance(app, build_id)
    with app.app_context():
        run = db.session.get(DeployAutomationRun, run_id)
        automation._do_poll_native_build(run)
        db.session.commit()
        assert run.status == "failed"
        steps = {s["key"]: s for s in run.steps}
        assert steps["approval"]["status"] == "fail"
        assert "Rejected by operator: freeze" in steps["approval"]["detail"]


# ---------------------------------------------------------------------------
# Permission
# ---------------------------------------------------------------------------

def test_roles_that_retry_builds_can_approve_them(app):
    from api.rbac_data import ROLE_DEFINITIONS

    for name in ("operator", "cluster_admin"):
        permissions = ROLE_DEFINITIONS[name]["permissions"]
        assert "ci_builds:retry" in permissions and "ci_builds:approve" in permissions
    assert "ci_builds:approve" not in ROLE_DEFINITIONS["viewer"]["permissions"]


def test_the_migration_grants_approve_to_custom_roles_that_retry(app):
    from api.migrate_rbac import _grant_ci_approve_to_build_retriers

    with app.app_context():
        existing = Permission.query.filter_by(key="ci_builds:approve").one()
        retry = Permission.query.filter_by(key="ci_builds:retry").one()
        custom = Role(name="release-team", description="custom")
        custom.permissions = [retry]
        db.session.add(custom)
        # Pretend this installation predates the permission.
        for role in Role.query.all():
            role.permissions = [p for p in role.permissions if p.key != "ci_builds:approve"]
        db.session.delete(existing)
        db.session.commit()

        _grant_ci_approve_to_build_retriers()
        custom = Role.query.filter_by(name="release-team").one()
        assert "ci_builds:approve" in {p.key for p in custom.permissions}

        # Once only: removing it again sticks across the next start.
        custom.permissions = [p for p in custom.permissions if p.key != "ci_builds:approve"]
        db.session.commit()
        _grant_ci_approve_to_build_retriers()
        assert "ci_builds:approve" not in {p.key for p in Role.query.filter_by(name="release-team").one().permissions}
