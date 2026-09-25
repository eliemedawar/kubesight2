"""The per-cluster approval rule applies to everyone.

Clusters → Configure recipients sets how many approvals a cluster needs before
anything is changed in it. That number is the rule for admins and for an agent
holding a token (Hermes over MCP) exactly as for anybody else, and it covers
every write path — YAML apply, Helm, and workload restart/scale/rollback — not
only the Deploy screen. The one way through without a request is configuring
the cluster to 0.

A gated YAML apply, restart, scale or rollback is not refused: it is sent for
approval as a one-item change bundle (202, ``pendingApproval``) and the bundle
executor applies it once another approver approves. Helm has no bundle form and
is still refused.
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
def test_admin_workload_actions_are_sent_for_approval(client, admin_token, path, body):
    from api.models import ChangeBundle

    _require(1)
    response = client.post(path, headers=auth_headers(admin_token), json=body)
    assert response.status_code == 202, response.get_json()
    data = response.get_json()["data"]
    assert data["pendingApproval"] is True and data["applied"] is False
    bundle = ChangeBundle.query.get(data["bundleId"])
    assert bundle.status == "pending_approval"
    assert bundle.requester_user_id == _admin().id
    assert [i.resource_name for i in bundle.items] == ["payments-api"]


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


MANIFEST = "apiVersion: v1\nkind: ConfigMap\nmetadata:\n  name: x\ndata:\n  a: b\n"


def _operator_approver() -> User:
    approver = User.query.filter_by(username="operator").first()
    approver.email = "approver@example.com"
    db.session.commit()
    return approver


def test_admin_yaml_apply_is_sent_for_approval(app):
    from api.models import ChangeBundle
    from api.services.deployment_service import apply_yaml

    _require(1)
    data, err, status = apply_yaml(_admin(), CLUSTER, NAMESPACE, MANIFEST, "")
    assert err is None and status == 202
    assert data["pendingApproval"] is True
    bundle = ChangeBundle.query.get(data["bundleId"])
    assert bundle.status == "pending_approval"
    assert bundle.items[0].action_type == "apply_yaml"
    assert "kind: ConfigMap" in bundle.items[0].yaml_preview


def test_invalid_yaml_is_refused_not_queued(app):
    from api.models import ChangeBundle
    from api.services.deployment_service import apply_yaml

    _require(1)
    data, _err, status = apply_yaml(_admin(), CLUSTER, NAMESPACE, "not: [valid", "")
    assert data is None and status >= 400
    assert ChangeBundle.query.count() == 0


def test_queued_change_is_applied_automatically_once_approved(app):
    """The whole point: edit, wait for the approval, and it happens by itself."""
    from api.models import ChangeBundle
    from api.services.change_bundle_executor import process_due_bundles
    from api.services.change_bundle_service import decide_bundle
    from api.services.deployment_service import apply_yaml

    _require(1)
    data, _err, _status = apply_yaml(_admin(), CLUSTER, NAMESPACE, MANIFEST, "")
    bundle = ChangeBundle.query.get(data["bundleId"])

    # Nothing runs while it waits.
    process_due_bundles(now=datetime.now(timezone.utc) + timedelta(minutes=5))
    db.session.refresh(bundle)
    assert bundle.status == "pending_approval"

    decide_bundle(bundle.id, "approve", actor=_operator_approver())
    db.session.refresh(bundle)
    assert bundle.status == "approved"

    # The scheduler's next tick applies it.
    result = process_due_bundles()
    db.session.refresh(bundle)
    assert result["executed"] == 1
    assert bundle.status == "completed"
    assert bundle.items[0].status == "succeeded"


def test_queued_restart_and_rollback_execute(app):
    from api.models import ChangeBundle
    from api.services.change_bundle_executor import process_due_bundles
    from api.services.change_bundle_service import decide_bundle
    from api.services.inventory_actions_service import restart_deployment, rollback_deployment

    _require(1)
    approver = _operator_approver()
    ids = []
    for call in (restart_deployment, rollback_deployment):
        data, _err, status = call(_admin(), {**ACTION_BODY, "revision": 2})
        assert status == 202
        ids.append(data["bundleId"])
        decide_bundle(data["bundleId"], "approve", actor=approver)
    process_due_bundles()
    modes = []
    for bundle_id in ids:
        bundle = ChangeBundle.query.get(bundle_id)
        assert bundle.status == "completed", bundle.items[0].execution_result
        modes.append(bundle.items[0].execution_result["mode"])
    assert modes == ["restart", "rollback"]


def test_mcp_writes_are_sent_for_approval_for_an_admin_token(client, admin_token):
    """Hermes holds a token like anybody else, so its change waits for approval too."""
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
    assert not result.get("isError"), result
    assert "NOT applied yet" in result["content"][0]["text"]
    assert result["structuredContent"]["pendingApproval"] is True

    applied = call_tool(
        client,
        admin_token,
        "kubesight_deploy_apply",
        {"cluster": CLUSTER, "namespace": NAMESPACE, "yaml": MANIFEST},
    )
    assert not applied.get("isError"), applied
    assert applied["structuredContent"]["pendingApproval"] is True


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


def _pending_bundle_by(user: User):
    from api.models import ChangeBundle

    bundle = ChangeBundle(
        requester_user_id=user.id,
        status="pending_approval",
        required_approvals=1,
        total_recipients=2,
        requested_start_time=datetime.now(timezone.utc) + timedelta(hours=1),
        requested_end_time=datetime.now(timezone.utc) + timedelta(hours=3),
    )
    db.session.add(bundle)
    db.session.commit()
    return bundle


def test_requester_cannot_approve_their_own_change_bundle(app):
    """Change bundles are where approval-gated automation and ticket deploys go."""
    from api.services import change_bundle_service as bundles

    admin = _admin()
    admin.email = "admin@example.com"
    db.session.commit()
    bundle = _pending_bundle_by(admin)
    with pytest.raises(bundles.ChangeBundleError) as exc:
        bundles.decide_bundle(bundle.id, "approve", actor=admin)
    assert exc.value.status_code == 403
    with pytest.raises(bundles.ChangeBundleError):
        bundles.record_vote(bundle.id, "approve", voter_email="admin@example.com")
    db.session.refresh(bundle)
    assert bundle.status == "pending_approval"


def test_requester_may_still_decline_their_own_request(app):
    req = _pending_request_by(_admin())
    data = svc.decide_request(req.id, "decline", actor=_admin())
    assert data["declines"] == 1 or data["status"] == "declined"
