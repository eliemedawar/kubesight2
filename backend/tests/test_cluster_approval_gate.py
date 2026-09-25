"""The per-cluster approval rule applies to everyone.

Clusters → Configure recipients sets how many approvals a cluster needs before
anything is changed in it. That number is the rule for admins and for an agent
holding a token (Hermes over MCP) exactly as for anybody else, and it covers
every write path — YAML apply, Helm, and workload restart/scale/rollback — not
only the Deploy screen. The one way through without a request is configuring
the cluster to 0.
"""

from datetime import datetime, timedelta, timezone

import pytest

from api.db import db
from api.models import DeploymentRequest, DeploymentRequestSetting, User
from api.services import deployment_request_service as svc
from tests.conftest import auth_headers
from tests.test_mcp_server import call_tool

CLUSTER = "prod-us-east"
NAMESPACE = "payments"
ACTION_BODY = {
    "clusterId": CLUSTER,
    "namespace": NAMESPACE,
    "workloadType": "deployment",
    "workloadName": "payments-api",
}


def _require(count: int) -> None:
    row = DeploymentRequestSetting.query.first() or DeploymentRequestSetting(
        recipients=[], group_ids=[]
    )
    row.required_approvals = 1
    row.cluster_required_approvals = {CLUSTER: count}
    db.session.add(row)
    db.session.commit()


def _admin() -> User:
    return User.query.filter_by(username="admin").first()


def _approved_request_for(user: User) -> DeploymentRequest:
    now = datetime.now(timezone.utc)
    req = DeploymentRequest(
        requester_id=user.id,
        cluster_id=CLUSTER,
        cluster_name=CLUSTER,
        message="ship it",
        status="approved",
        required_approvals=1,
        total_recipients=2,
        requested_window_start=now - timedelta(minutes=5),
        requested_window_end=now + timedelta(hours=2),
    )
    db.session.add(req)
    db.session.commit()
    return req


def test_admin_eligibility_reports_the_rule(app):
    _require(2)
    info = svc.deploy_eligibility(_admin(), CLUSTER)
    assert info["approvalRequired"] is True
    assert info["eligible"] is False
    assert info["requiredApprovals"] == 2


def test_cluster_at_zero_needs_no_request(app):
    _require(0)
    info = svc.deploy_eligibility(_admin(), CLUSTER)
    assert info["approvalRequired"] is False
    assert info["eligible"] is True


@pytest.mark.parametrize(
    "path,body",
    [
        ("/api/inventory/actions/restart", ACTION_BODY),
        ("/api/inventory/actions/scale", {**ACTION_BODY, "replicas": 3}),
        ("/api/inventory/actions/rollback", ACTION_BODY),
    ],
)
def test_admin_workload_actions_are_gated(client, admin_token, path, body):
    _require(1)
    response = client.post(path, headers=auth_headers(admin_token), json=body)
    assert response.status_code == 403
    assert "approved deployment request" in response.get_json()["error"]


def test_admin_workload_action_allowed_with_an_approved_request(client, admin_token):
    _require(1)
    _approved_request_for(_admin())
    response = client.post(
        "/api/inventory/actions/restart", headers=auth_headers(admin_token), json=ACTION_BODY
    )
    assert response.status_code == 200


def test_admin_helm_rollback_and_uninstall_are_gated(app):
    from api.services.helm_service import rollback_release, uninstall_release

    _require(1)
    admin = _admin()
    for call in (
        lambda: rollback_release(admin, CLUSTER, NAMESPACE, "demo", run_helm_fn=_never),
        lambda: uninstall_release(admin, CLUSTER, NAMESPACE, "demo", run_helm_fn=_never),
    ):
        data, err, status = call()
        assert status == 403 and data is None
        assert "approved deployment request" in err


def _never(*_args, **_kwargs):
    raise AssertionError("helm must not run without an approval")


def test_admin_yaml_apply_is_gated(app):
    from api.services.deployment_service import apply_yaml

    _require(1)
    manifest = "apiVersion: v1\nkind: ConfigMap\nmetadata:\n  name: x\n"
    data, err, status = apply_yaml(_admin(), CLUSTER, NAMESPACE, manifest, "")
    assert status == 403 and data is None
    assert "approved deployment request" in err


def test_mcp_writes_are_gated_for_an_admin_token(client, admin_token):
    """Hermes holds a token like anybody else, so it gets the same answer."""
    _require(1)
    eligibility = call_tool(
        client, admin_token, "kubesight_deploy_eligibility", {"cluster": CLUSTER}
    )["structuredContent"]
    assert eligibility["eligible"] is False

    result = call_tool(
        client,
        admin_token,
        "kubesight_workload_restart",
        {"cluster": CLUSTER, "namespace": NAMESPACE, "workload": "payments-api"},
    )
    assert result["isError"] is True
    assert "approved deployment request" in result["content"][0]["text"]


def _pending_request_by(user: User) -> DeploymentRequest:
    now = datetime.now(timezone.utc)
    req = DeploymentRequest(
        requester_id=user.id,
        cluster_id=CLUSTER,
        cluster_name=CLUSTER,
        message="please",
        status="pending",
        required_approvals=1,
        total_recipients=2,
        requested_window_start=now + timedelta(hours=1),
        requested_window_end=now + timedelta(hours=3),
    )
    db.session.add(req)
    db.session.commit()
    return req


def test_requester_cannot_approve_their_own_request_in_app(client, admin_token):
    req = _pending_request_by(_admin())
    response = client.post(
        f"/api/deployment-requests/{req.id}/approve", headers=auth_headers(admin_token)
    )
    assert response.status_code == 403
    db.session.refresh(req)
    assert req.status == "pending"


def test_requester_cannot_approve_their_own_request_by_email_link(app):
    admin = _admin()
    admin.email = "admin@example.com"
    db.session.commit()
    req = _pending_request_by(admin)
    with pytest.raises(svc.DeploymentRequestError) as exc:
        svc.record_vote(req.id, "approve", voter_email="ADMIN@example.com")
    assert exc.value.status_code == 403
    db.session.refresh(req)
    assert req.status == "pending"


def test_requester_is_left_out_of_their_own_approver_pool(app):
    admin = _admin()
    admin.email = "admin@example.com"
    row = DeploymentRequestSetting.query.first() or DeploymentRequestSetting(group_ids=[])
    row.recipients = ["admin@example.com", "boss@example.com"]
    row.required_approvals = 1
    row.cluster_required_approvals = {}
    db.session.add(row)
    db.session.commit()

    now = datetime.now(timezone.utc)
    data = svc.create_request(
        admin,
        CLUSTER,
        CLUSTER,
        "change",
        window_start=(now + timedelta(hours=1)).isoformat(),
        window_end=(now + timedelta(hours=2)).isoformat(),
    )
    assert data["totalRecipients"] == 1
    assert data["status"] == "pending"
