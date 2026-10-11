"""The Approvals page: one inbox across every kind, history, the rules, approver groups.

What is locked here: each pending kind shows up, "waiting on me" is exactly
what that kind's own rule lets the viewer decide now (never their own request),
a vote through the item's action URL lands in that kind's own route, decided
items move to history, and the rules / approver groups read and write the same
settings the gates use.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from api.db import db
from api.models import (
    ChangeBundle,
    ClusterBuild,
    ClusterProvisionJob,
    DeploymentRequestSetting,
    Role,
    User,
)
from api.services import deployment_request_service
from tests.conftest import auth_headers
from tests.test_ticket_agent import agent, writes  # noqa: F401,E402
from tests.test_ci_approval_stage import (  # noqa: F401  (fixtures are used by name)
    _approval,
    _no_cluster_approvals,
    _run,
    _save,
    _stages,
    service,
)

CLUSTER = "prod-us-east"


def _get(client, token, path):
    response = client.get(path, headers=auth_headers(token))
    assert response.status_code == 200, response.get_json()
    return response.get_json()["data"]


def _inbox(client, token, scope="mine"):
    return _get(client, token, f"/api/approvals/inbox?scope={scope}")


def _user(username):
    return User.query.filter_by(username=username).first()


@pytest.fixture()
def admin2_token(client, app):
    admin = _user("admin")
    other = User(username="admin2", email="admin2@example.com", is_active=True, role_id=admin.role_id)
    other.password_hash = admin.password_hash
    other.first_login_completed = getattr(admin, "first_login_completed", True)
    db.session.add(other)
    db.session.commit()
    response = client.post("/api/auth/login", json={"username": "admin2", "password": "admin123"})
    return response.get_json()["data"]["token"]


def _request_by(user):
    row = DeploymentRequestSetting.query.first() or DeploymentRequestSetting(group_ids=[])
    row.recipients = ["admin@example.com", "admin2@example.com", "boss@example.com"]
    row.required_approvals = 1
    row.cluster_required_approvals = {}
    db.session.add(row)
    db.session.commit()
    now = datetime.now(timezone.utc)
    return deployment_request_service.create_request(
        user, CLUSTER, CLUSTER, "Release 4.2 to prod",
        window_start=(now + timedelta(hours=1)).isoformat(),
        window_end=(now + timedelta(hours=2)).isoformat(),
    )


def test_empty_inbox(client, admin_token):
    data = _inbox(client, admin_token)
    assert data["items"] == [] and data["counts"] == {"waitingOnMe": 0, "pending": 0}
    assert _get(client, admin_token, "/api/approvals/count") == {"waitingOnMe": 0}


def test_a_deployment_request_waits_on_others_not_its_requester(client, admin_token, admin2_token):
    admin = _user("admin")
    admin.email = "admin@example.com"
    db.session.commit()
    request = _request_by(admin)

    mine = _inbox(client, admin_token)
    assert mine["items"] == []  # your own request never waits on you
    everything = _inbox(client, admin_token, "all")
    [item] = everything["items"]
    assert item["kind"] == "deployment_request" and item["canDecide"] is False
    assert "another approver" in item["why"]

    [item] = _inbox(client, admin2_token)["items"]
    assert item["canDecide"] is True and item["title"] == f"Deploy to {CLUSTER}"
    assert item["approvals"] == 0 and item["required"] == 1
    # The vote goes to the kind's own route.
    response = client.post(item["actions"]["approve"], headers=auth_headers(admin2_token))
    assert response.status_code == 200, response.get_json()
    assert _inbox(client, admin2_token, "all")["items"] == []
    history = _get(client, admin2_token, "/api/approvals/history")["items"]
    assert history[0]["key"] == f"deployment_request:{request['id']}"
    assert history[0]["outcome"] == "approved"


def test_a_change_bundle_shows_its_changes(client, admin_token, admin2_token):
    bundle = ChangeBundle(
        requester_user_id=_user("admin2").id, status="pending_approval", note="Rotate the certs",
        required_approvals=1, total_recipients=2,
    )
    db.session.add(bundle)
    db.session.commit()
    [item] = _inbox(client, admin_token)["items"]
    assert item["kind"] == "change_bundle" and item["summary"] == "Rotate the certs"
    assert item["actions"]["rejectField"] == "reason"
    [own] = _inbox(client, admin2_token, "all")["items"]
    assert own["canDecide"] is False and "Your bundle" in own["why"]

    response = client.post(item["actions"]["reject"], json={"reason": "not today"},
                           headers=auth_headers(admin_token))
    assert response.status_code == 200, response.get_json()
    [done] = _get(client, admin_token, "/api/approvals/history?kind=change_bundle")["items"]
    assert done["outcome"] == "rejected"


def test_a_destroy_needs_the_second_person(client, admin_token, admin2_token):
    build = ClusterBuild(name="uat-09", status="vms_ready", machine_source="vmware", k8s_version="1.32.4")
    db.session.add(build)
    db.session.flush()
    job = ClusterProvisionJob(
        build_id=build.id, operation="destroy", status="awaiting_approval",
        requested_by="admin", requested_by_user_id=_user("admin").id, reason="Done testing",
        plan_summary_json={"resources": [
            {"kind": "vm", "action": "delete", "title": "uat-09-vm-1"},
        ]},
    )
    db.session.add(job)
    db.session.commit()
    [item] = _inbox(client, admin_token, "all")["items"]
    assert item["kind"] == "cluster_destroy" and item["canDecide"] is False
    assert "someone else" in item["why"]
    [item] = _inbox(client, admin2_token)["items"]
    assert item["canDecide"] is True and "uat-09-vm-1" in item["details"][0]
    assert item["actions"]["reject"].endswith(f"/provision/jobs/{job.id}/reject")


def test_a_ci_approval_stage_uses_its_own_approvers(app, client, admin_token, admin2_token, service):
    _no_cluster_approvals(app)
    assert _save(client, admin_token, service.pipeline_id, _stages(_approval(), deploy=False)).status_code == 200
    build_id = _run(app, client, admin_token, service.id)

    [item] = _inbox(client, admin_token, "all")["items"]
    assert item["kind"] == "ci_stage" and item["canDecide"] is False  # admin started the build
    assert item["summary"] == "Check the release notes before approving."
    [item] = _inbox(client, admin2_token)["items"]
    assert item["canDecide"] is True and item["required"] == 1
    assert item["href"].endswith(f"builds?build={build_id}")

    response = client.post(item["actions"]["approve"], json={"comment": "ok"}, headers=auth_headers(admin2_token))
    assert response.status_code == 200, response.get_json()
    assert _inbox(client, admin2_token, "all")["items"] == []
    [done] = _get(client, admin2_token, "/api/approvals/history?kind=ci_stage")["items"]
    assert done["outcome"] == "approved" and done["decidedBy"] == "admin2"


def test_the_viewer_sees_nothing_they_may_not(client, viewer_token, admin_token):
    admin = _user("admin")
    _request_by(admin)
    data = _inbox(client, viewer_token, "all")
    assert all(i["kind"] != "deployment_request" for i in data["items"])
    rules = _get(client, viewer_token, "/api/approvals/rules")
    assert "clusters" not in rules and "groups" not in rules


# ---------------------------------------------------------------------------
# Rules and approver groups
# ---------------------------------------------------------------------------

def test_rules_gather_every_setting(client, admin_token):
    rules = _get(client, admin_token, "/api/approvals/rules")
    assert rules["permissions"]["editClusters"] and rules["permissions"]["editHermes"]
    assert "Change bundles" in rules["clusters"]["gates"]
    assert any(row["id"] == CLUSTER for row in rules["clusters"]["clusters"])
    assert rules["hermes"]["minConfidence"] == "High"
    assert "second person" in rules["clusterBuilder"]["destroy"]
    assert "ciStages" in rules["elsewhere"] and "promotions" in rules["elsewhere"]


def test_rules_list_ci_approval_stages(app, client, admin_token, service):
    _save(client, admin_token, service.pipeline_id, _stages(_approval(minApprovals=2), deploy=False))
    [row] = _get(client, admin_token, "/api/approvals/rules")["elsewhere"]["ciStages"]
    assert row["service"] == "Payments Api" and row["stage"] == "Approve release"
    assert row["minApprovals"] == 2 and "anyone with" in row["approvers"]
    assert row["href"] == f"#/service-catalog/{service.id}/pipeline"


def test_approver_groups_as_people_roles_and_emails(client, admin_token, viewer_token):
    admin = _user("admin")
    admin.email = "admin@example.com"
    db.session.commit()
    role = Role.query.first()
    response = client.post("/api/approvals/groups", json={
        "name": "Release managers", "userIds": [admin.id], "roleIds": [role.id],
        "emails": ["boss@example.com"], "approvesDeploys": True,
    }, headers=auth_headers(admin_token))
    assert response.status_code == 201, response.get_json()
    group = response.get_json()["data"]
    assert [u["id"] for u in group["users"]] == [admin.id]
    assert [r["id"] for r in group["roles"]] == [role.id]
    assert group["emails"] == ["boss@example.com"] and group["approvesDeploys"] is True
    assert "boss@example.com" in group["resolvedEmails"]
    # The deploy gate now sends requests to this group.
    config = deployment_request_service.get_recipient_config()
    assert group["id"] in config["groupIds"]
    assert "boss@example.com" in config["resolvedRecipients"]

    response = client.put(f"/api/approvals/groups/{group['id']}", json={
        "emails": [], "roleIds": [], "approvesDeploys": False,
    }, headers=auth_headers(admin_token))
    assert response.status_code == 200, response.get_json()
    updated = response.get_json()["data"]
    assert updated["emails"] == [] and updated["roles"] == [] and len(updated["users"]) == 1
    assert group["id"] not in deployment_request_service.get_recipient_config()["groupIds"]

    refused = client.post("/api/approvals/groups", json={"name": "x"}, headers=auth_headers(viewer_token))
    assert refused.status_code == 403
    assert client.delete(f"/api/approvals/groups/{group['id']}",
                         headers=auth_headers(admin_token)).status_code == 200
    assert _get(client, admin_token, "/api/approvals/groups")["items"] == []


def test_a_group_keeps_its_slack_members(client, admin_token):
    from api.models import AlertRoutingReceiver, AlertRoutingReceiverGroup

    slack = AlertRoutingReceiver(name="ops-slack", receiver_type="slack", url="https://hooks.slack.com/x",
                                 enabled=True)
    group = AlertRoutingReceiverGroup(name="Ops", enabled=True)
    group.members = [slack]
    db.session.add_all([slack, group])
    db.session.commit()
    response = client.put(f"/api/approvals/groups/{group.id}", json={"emails": ["ops@example.com"]},
                          headers=auth_headers(admin_token))
    data = response.get_json()["data"]
    assert data["emails"] == ["ops@example.com"]
    assert [m["name"] for m in data["otherMembers"]] == ["ops-slack"]


def test_a_hermes_approval_is_in_the_inbox(client, admin_token, viewer_token, agent, writes):
    from tests.test_ticket_agent import _request, _ticket

    ticket = _ticket()
    _request(ticket.id)
    [item] = _inbox(client, admin_token)["items"]
    assert item["kind"] == "hermes" and item["requestedBy"] == "Hermes"
    assert "deploy payments-api v9.9.9 to payments" in item["title"]
    assert item["actions"]["rejectField"] == "note" and item["expiresAt"]
    response = client.post(item["actions"]["approve"], headers=auth_headers(admin_token))
    assert response.status_code == 200, response.get_json()
    [done] = _get(client, admin_token, "/api/approvals/history?kind=hermes")["items"]
    assert done["outcome"] == "approved" and done["decidedBy"] == "admin"
