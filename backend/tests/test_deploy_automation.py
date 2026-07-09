"""Deploy automation: Jenkins config CRUD + the per-ticket run state machine.

Runs execute against the mock cluster ("prod-us-east" / "payments" /
"payments-api" from mock_data). Registry + live-YAML reads are monkeypatched at
their module attributes — the service imports them function-locally, so the
patch is picked up at call time.
"""

import json
from datetime import datetime, timedelta, timezone

import pytest

from api.db import db
from api.models import (
    ChangeBundle,
    DeployAutomationRun,
    DeploymentRequestSetting,
    ZohoDeploymentSnapshot,
    ZohoInboundTicket,
)

from .conftest import auth_headers

CLUSTER = "prod-us-east"
NAMESPACE = "payments"
DEPLOYMENT = "payments-api"  # mock image ghcr.io/mock/payments:v2.8.1

LIVE_DEPLOYMENT_YAML = """apiVersion: apps/v1
kind: Deployment
metadata:
  name: payments-api
  namespace: payments
spec:
  replicas: 3
  template:
    spec:
      containers:
        - name: payments-api
          image: ghcr.io/mock/payments:v2.8.1
          resources:
            limits:
              cpu: "2"
"""


def _make_ticket(resolved=True, tag="v9.9.9"):
    snapshot = ZohoDeploymentSnapshot.query.filter_by(
        cluster_id=CLUSTER, namespace=NAMESPACE, deployment_name=DEPLOYMENT
    ).first()
    if snapshot is None:
        snapshot = ZohoDeploymentSnapshot(
            cluster_id=CLUSTER, namespace=NAMESPACE, deployment_name=DEPLOYMENT
        )
        db.session.add(snapshot)
        db.session.flush()
    ticket = ZohoInboundTicket(
        ticket_id=f"zt-{datetime.now(timezone.utc).timestamp()}",
        ticket_number="DR-9001",
        resolved=resolved,
        app_service_id=snapshot.id if resolved else None,
        tag=tag,
        received_at=datetime.now(timezone.utc),
    )
    db.session.add(ticket)
    db.session.commit()
    return ticket


def _set_cluster_approvals(count: int) -> None:
    row = DeploymentRequestSetting.query.first() or DeploymentRequestSetting()
    row.cluster_required_approvals = {CLUSTER: count}
    db.session.add(row)
    db.session.commit()


def _start(client, token, ticket_id, expect=201):
    response = client.post(
        "/api/zoho/automation/runs",
        json={"ticketRecordId": ticket_id},
        headers=auth_headers(token),
    )
    assert response.status_code == expect, response.get_json()
    return response.get_json()["data"] if expect == 201 else response.get_json()


def _step(run_payload, key):
    return next(s for s in run_payload["steps"] if s["key"] == key)


def test_jenkins_config_roundtrip(client, admin_token):
    # Partial save while disabled is fine; the token is write-only.
    response = client.put(
        "/api/zoho/jenkins",
        json={
            "baseUrl": "https://jenkins.example.com",
            "username": "bot",
            "apiToken": "s3cret",
            "buildToken": "trigger-t0ken",
        },
        headers=auth_headers(admin_token),
    )
    assert response.status_code == 200
    data = response.get_json()["data"]
    assert data["apiTokenConfigured"] is True
    assert data["buildTokenConfigured"] is True
    assert "s3cret" not in str(data) and "trigger-t0ken" not in str(data)

    # Enabling requires the router job path.
    response = client.put(
        "/api/zoho/jenkins", json={"enabled": True}, headers=auth_headers(admin_token)
    )
    assert response.status_code == 400
    assert "Router job path" in response.get_json()["error"]

    response = client.put(
        "/api/zoho/jenkins",
        json={"enabled": True, "routerJobPath": "folder/router", "autoRunTickets": True},
        headers=auth_headers(admin_token),
    )
    assert response.status_code == 200
    data = response.get_json()["data"]
    assert data["enabled"] is True and data["autoRunTickets"] is True


def test_start_run_validations(client, admin_token, app):
    unresolved = _make_ticket(resolved=False)
    body = _start(client, admin_token, unresolved.id, expect=400)
    assert "did not resolve" in body["error"]

    untagged = _make_ticket(resolved=True, tag="")
    body = _start(client, admin_token, untagged.id, expect=400)
    assert "tag" in body["error"]

    body = _start(client, admin_token, 999_999, expect=404)
    assert "not found" in body["error"]


def test_run_fails_when_build_needed_but_jenkins_off(client, admin_token, app):
    """No linked registry (no_connection ⇒ build required) + Jenkins disabled ⇒ clear failure."""
    ticket = _make_ticket()
    run = _start(client, admin_token, ticket.id)
    assert run["status"] == "failed"
    assert "Jenkins is not configured" in run["error"]
    assert _step(run, "image_check")["status"] == "done"
    assert _step(run, "build")["status"] == "fail"


def test_run_bundle_path_when_cluster_requires_approval(client, admin_token, app, monkeypatch):
    """Image already in the registry ⇒ skip build/verify ⇒ Change Bundle handoff."""
    monkeypatch.setattr(
        "api.services.registry_service.check_image",
        lambda image: {"status": "found", "image": image},
    )
    monkeypatch.setattr(
        "api.services.resource_actions_service.get_resource_yaml",
        lambda user, cluster_id, namespace, kind, name: (
            {"yaml": LIVE_DEPLOYMENT_YAML},
            None,
            200,
        ),
    )
    _set_cluster_approvals(1)
    ticket = _make_ticket(tag="v9.9.9")

    run = _start(client, admin_token, ticket.id)
    assert run["status"] == "awaiting_approval", run
    assert _step(run, "build")["status"] == "skip"
    assert _step(run, "verify")["status"] == "skip"
    assert run["bundleId"]

    bundle = ChangeBundle.query.get(run["bundleId"])
    assert bundle.status == "pending_approval"
    assert len(bundle.items) == 1
    item = bundle.items[0]
    assert item.action_type == "edit_deployment"
    assert "ghcr.io/mock/payments:v9.9.9" in item.yaml_preview
    # Only the image changed — the live spec (replicas, resources) is preserved.
    assert "replicas: 3" in item.yaml_preview

    # Duplicate start for the same ticket/deployment is refused while active.
    body = _start(client, admin_token, ticket.id, expect=409)
    assert "already active" in body["error"]

    # Bundle completes (executor's job) → the run lands on deployed.
    from api.services.deploy_automation_service import advance_runs

    bundle.status = "completed"
    db.session.commit()
    advance_runs()
    row = DeployAutomationRun.query.get(run["id"])
    assert row.status == "deployed"
    # Bundle completion also passes through the pod-health gate (instant in mock).
    pods = next(s for s in row.steps if s["key"] == "pods")
    assert pods["status"] == "done"


def test_auto_run_respects_per_cluster_overrides(client, admin_token, app):
    from api.models import JenkinsConnection
    from api.services.deploy_automation_service import maybe_auto_run

    # Global default OFF, but this cluster is overridden to auto-start.
    row = JenkinsConnection(id=1, auto_run_tickets=False, auto_run_clusters={CLUSTER: "auto"})
    db.session.add(row)
    db.session.commit()

    ticket = _make_ticket(tag="v1.0.0")
    started = maybe_auto_run(ticket.id)
    assert started is not None and started["auto"] is True

    # Flip: global ON but the cluster is overridden to manual — nothing starts.
    row = JenkinsConnection.query.get(1)
    row.auto_run_tickets = True
    row.auto_run_clusters = {CLUSTER: "manual"}
    db.session.commit()

    before = DeployAutomationRun.query.count()
    ticket2 = _make_ticket(tag="v1.0.1")
    assert maybe_auto_run(ticket2.id) is None
    assert DeployAutomationRun.query.count() == before


def test_run_direct_path_when_no_approval_required(client, admin_token, app, monkeypatch):
    """Approval-free cluster ⇒ image applied immediately (mock mode), with the
    ticket's raw tag resolved through the image tag template."""
    monkeypatch.setattr(
        "api.services.registry_service.check_image",
        lambda image: {"status": "found", "image": image},
    )
    _set_cluster_approvals(0)
    response = client.put(
        "/api/zoho/jenkins",
        json={"imageTagTemplate": "v{tag}-prod"},
        headers=auth_headers(admin_token),
    )
    assert response.status_code == 200
    ticket = _make_ticket(tag="8.0.0")

    run = _start(client, admin_token, ticket.id)
    assert run["status"] == "deployed", run
    assert run["imageTag"] == "v8.0.0-prod"
    assert run["ticketTag"] == "8.0.0"
    assert _step(run, "approval")["status"] == "skip"
    assert _step(run, "deploy")["status"] == "done"
    assert "ghcr.io/mock/payments:v8.0.0-prod" in _step(run, "deploy")["detail"]
    # Completion requires the pod-health step to pass (instant in mock mode).
    assert _step(run, "pods")["status"] == "done"
    assert run["bundleId"] is None


def test_router_trigger_contract(client, admin_token, app, monkeypatch):
    """The router gets exactly APP/TAG/NAMESPACE with the RAW ticket tag; the
    client appends the job-level token; the run's own tag is template-resolved."""
    captured = {}

    def fake_trigger(cfg, params):
        captured["params"] = params
        captured["build_token"] = cfg.build_token
        return "http://jenkins/queue/item/1"

    monkeypatch.setattr(
        "api.services.registry_service.check_image",
        lambda image: {"status": "not_found", "image": image},
    )
    monkeypatch.setattr("api.services.jenkins_client.trigger_build", fake_trigger)
    response = client.put(
        "/api/zoho/jenkins",
        json={
            "enabled": True,
            "baseUrl": "http://10.43.17.16:8080",
            "username": "admin",
            "apiToken": "api-t0ken",
            "buildToken": "kubesightareeba",
            "routerJobPath": "georgio-testing",
            "imageTagTemplate": "v{tag}-prod",
        },
        headers=auth_headers(admin_token),
    )
    assert response.status_code == 200

    ticket = _make_ticket(tag="1.73.13")
    run = _start(client, admin_token, ticket.id)
    assert run["status"] == "building", run
    # APP is the K8s deployment name; TAG is the raw ticket tag.
    assert captured["params"] == {
        "APP": DEPLOYMENT,
        "TAG": "1.73.13",
        "NAMESPACE": NAMESPACE,
    }
    assert captured["build_token"] == "kubesightareeba"
    # Registry/deploy side uses the templated tag.
    assert run["imageTag"] == "v1.73.13-prod"
    assert run["ticketTag"] == "1.73.13"


def test_image_tag_template_resolution(app):
    from api.models import JenkinsConnection
    from api.services.deploy_automation_service import resolve_image_tag

    row = JenkinsConnection(id=1, image_tag_template="v{tag}-prod")
    assert resolve_image_tag(row, "1.72.1") == "v1.72.1-prod"
    # Operator already typed the full registry form (any case) → used as-is.
    assert resolve_image_tag(row, "V1.72.5-prod") == "V1.72.5-prod"
    assert resolve_image_tag(row, "v2.0.0-prod") == "v2.0.0-prod"
    # No template configured → raw passthrough.
    assert resolve_image_tag(None, "1.2.3") == "1.2.3"
    row.image_tag_template = "{tag}"
    assert resolve_image_tag(row, "1.2.3") == "1.2.3"


def test_rollout_failure_rolls_back_and_emails_admins(client, admin_token, app, monkeypatch):
    """Pods never come up ⇒ rollout undo + failed run + admin notification."""
    from api.models import JenkinsConnection, User
    from api.services import deploy_automation_service as svc

    admin = User.query.filter_by(username="admin").first()
    admin.email = "admins@areeba.com"
    db.session.add(JenkinsConnection(id=1, rollback_on_failure=True))
    run = DeployAutomationRun(
        cluster_id=CLUSTER,
        namespace=NAMESPACE,
        deployment_name=DEPLOYMENT,
        image_tag="v9.9.9-prod",
        ticket_tag="9.9.9",
        ticket_number="DR-9002",
        status="verifying_rollout",
        steps=[],
        rollout_started_at=datetime.now(timezone.utc) - timedelta(minutes=60),
    )
    db.session.add(run)
    db.session.commit()

    # Pretend the cluster is real and permanently reports 1/3 ready.
    monkeypatch.setattr("api.k8s_provider.should_use_real_k8s", lambda cid=None: True)
    monkeypatch.setattr("api.k8s_provider.resolve_cluster_access", lambda cid: object())
    monkeypatch.setattr(
        "api.k8s_provider._run_for_access",
        lambda access, args: json.dumps(
            {"spec": {"replicas": 3}, "status": {"readyReplicas": 1, "updatedReplicas": 3}}
        ),
    )
    undo_calls = []
    monkeypatch.setattr(
        "api.services.deployment_service._run_kubectl_for_cluster",
        lambda cid, args: undo_calls.append((cid, args)) or "deployment.apps rolled back",
    )
    sent = []
    monkeypatch.setattr("api.email_delivery.smtp_is_configured", lambda: True)
    monkeypatch.setattr(
        "api.email_delivery.send_email",
        lambda to, subject, body, **kw: sent.append((to, subject, body)),
    )

    svc.advance_runs()

    row = db.session.get(DeployAutomationRun, run.id)
    assert row.status == "failed"
    assert "Rolled back to the previous version" in row.error
    assert undo_calls and undo_calls[0][1][:2] == ["rollout", "undo"]
    assert undo_calls[0][1][2] == f"deployment/{DEPLOYMENT}"
    assert sent, "admins should be emailed on rollout failure"
    assert sent[0][0] == "admins@areeba.com"
    assert "rollout failed" in sent[0][1]
    assert "DR-9002" in sent[0][1]


def test_rollout_watch_ignores_stale_status(client, admin_token, app, monkeypatch):
    """Counters from before the controller observed the new spec (stale
    observedGeneration) must not mark the run deployed — otherwise the check
    that runs milliseconds after `kubectl set image` reads the OLD template's
    3/3-ready status and goes green before the rollout even starts."""
    from api.models import JenkinsConnection
    from api.services import deploy_automation_service as svc

    db.session.add(JenkinsConnection(id=1))
    run = DeployAutomationRun(
        cluster_id=CLUSTER,
        namespace=NAMESPACE,
        deployment_name=DEPLOYMENT,
        image_tag="v5.5.5",
        status="verifying_rollout",
        steps=[],
        rollout_started_at=datetime.now(timezone.utc),
    )
    db.session.add(run)
    db.session.commit()

    payloads = {
        # Fully "ready" — but the controller hasn't seen generation 7 yet.
        "stale": {
            "metadata": {"generation": 7},
            "spec": {"replicas": 3},
            "status": {"observedGeneration": 6, "readyReplicas": 3, "updatedReplicas": 3},
        },
        "fresh": {
            "metadata": {"generation": 7},
            "spec": {"replicas": 3},
            "status": {"observedGeneration": 7, "readyReplicas": 3, "updatedReplicas": 3},
        },
    }
    current = {"key": "stale"}
    monkeypatch.setattr("api.k8s_provider.should_use_real_k8s", lambda cid=None: True)
    monkeypatch.setattr("api.k8s_provider.resolve_cluster_access", lambda cid: object())
    monkeypatch.setattr(
        "api.k8s_provider._run_for_access",
        lambda access, args: json.dumps(payloads[current["key"]]),
    )

    svc.advance_runs()
    row = db.session.get(DeployAutomationRun, run.id)
    assert row.status == "verifying_rollout", "stale status must not complete the run"
    pods = next(s for s in row.steps if s["key"] == "pods")
    assert "observe" in pods["detail"]

    # The controller catches up → the same counters now count.
    current["key"] = "fresh"
    svc.advance_runs()
    row = db.session.get(DeployAutomationRun, run.id)
    assert row.status == "deployed"


def test_cancel_withdraws_pending_and_approved_bundles(client, admin_token, app, monkeypatch):
    """Cancelling a run must stop its Change Bundle too — a bundle that outlives
    the run would deploy later with no run watching the rollout."""
    monkeypatch.setattr(
        "api.services.registry_service.check_image",
        lambda image: {"status": "found", "image": image},
    )
    monkeypatch.setattr(
        "api.services.resource_actions_service.get_resource_yaml",
        lambda user, cluster_id, namespace, kind, name: (
            {"yaml": LIVE_DEPLOYMENT_YAML},
            None,
            200,
        ),
    )
    _set_cluster_approvals(1)

    # Bundle still pending approval → rejected via the normal decide path.
    ticket = _make_ticket(tag="v4.4.4")
    run = _start(client, admin_token, ticket.id)
    assert run["status"] == "awaiting_approval" and run["bundleId"]
    response = client.post(
        f"/api/zoho/automation/runs/{run['id']}/cancel", headers=auth_headers(admin_token)
    )
    assert response.status_code == 200
    data = response.get_json()["data"]
    assert data["status"] == "cancelled"
    assert f"bundle #{run['bundleId']}" in data["error"].lower()
    bundle = ChangeBundle.query.get(run["bundleId"])
    assert bundle.status == "rejected"
    assert "cancelled" in (bundle.rejection_reason or "").lower()

    # Bundle already approved (executor hasn't started) → withdrawn directly.
    ticket2 = _make_ticket(tag="v4.4.5")
    run2 = _start(client, admin_token, ticket2.id)
    bundle2 = ChangeBundle.query.get(run2["bundleId"])
    bundle2.status = "approved"
    db.session.commit()
    response = client.post(
        f"/api/zoho/automation/runs/{run2['id']}/cancel", headers=auth_headers(admin_token)
    )
    assert response.status_code == 200
    data = response.get_json()["data"]
    assert data["status"] == "cancelled"
    assert "withdrawn" in data["error"]
    bundle2 = ChangeBundle.query.get(run2["bundleId"])
    assert bundle2.status == "rejected"


def test_list_runs_tolerates_bad_limit(client, admin_token, app):
    response = client.get(
        "/api/zoho/automation/runs?limit=abc", headers=auth_headers(admin_token)
    )
    assert response.status_code == 200
    assert isinstance(response.get_json()["data"]["items"], list)


def test_auto_run_is_idempotent_per_ticket(client, admin_token, app, monkeypatch):
    """A re-delivered webhook for the same ticket must not spawn a second run."""
    from api.models import JenkinsConnection
    from api.services.deploy_automation_service import maybe_auto_run

    monkeypatch.setattr(
        "api.services.registry_service.check_image",
        lambda image: {"status": "found", "image": image},
    )
    _set_cluster_approvals(0)  # direct path, no bundle needed
    db.session.add(
        JenkinsConnection(id=1, auto_run_tickets=True, image_tag_template="{tag}")
    )
    db.session.commit()
    ticket = _make_ticket(tag="1.0.0")

    first = maybe_auto_run(ticket.id)
    assert first is not None
    # Second delivery of the SAME ticket → no new run.
    second = maybe_auto_run(ticket.id)
    assert second is None
    assert DeployAutomationRun.query.filter_by(ticket_record_id=ticket.id).count() == 1


def test_manager_reject_is_authoritative(client, admin_token, app, monkeypatch):
    """One manager reject finalizes the bundle even when the recipient pool is
    larger than the required-approval count (the old quorum needed 2 declines)."""
    monkeypatch.setattr(
        "api.services.registry_service.check_image",
        lambda image: {"status": "found", "image": image},
    )
    monkeypatch.setattr(
        "api.services.resource_actions_service.get_resource_yaml",
        lambda user, cluster_id, namespace, kind, name: (
            {"yaml": LIVE_DEPLOYMENT_YAML},
            None,
            200,
        ),
    )
    _set_cluster_approvals(1)
    ticket = _make_ticket(tag="v2.2.2")
    run = _start(client, admin_token, ticket.id)
    bundle_id = run["bundleId"]
    assert bundle_id

    response = client.post(
        f"/api/change-bundles/{bundle_id}/reject",
        json={"reason": "not now"},
        headers=auth_headers(admin_token),
    )
    assert response.status_code == 200, response.get_json()
    assert response.get_json()["data"]["status"] == "rejected"

    # The run mirrors the rejection on the next tick.
    from api.services.deploy_automation_service import advance_runs

    advance_runs()
    row = db.session.get(DeployAutomationRun, run["id"])
    assert row.status == "failed"
    assert "rejected" in (row.error or "").lower()


def test_automation_bundle_requester_label(client, admin_token, app, monkeypatch):
    monkeypatch.setattr(
        "api.services.registry_service.check_image",
        lambda image: {"status": "found", "image": image},
    )
    monkeypatch.setattr(
        "api.services.resource_actions_service.get_resource_yaml",
        lambda user, cluster_id, namespace, kind, name: (
            {"yaml": LIVE_DEPLOYMENT_YAML},
            None,
            200,
        ),
    )
    _set_cluster_approvals(1)
    ticket = _make_ticket(tag="v3.3.3")
    run = _start(client, admin_token, ticket.id)
    response = client.get(
        f"/api/change-bundles/{run['bundleId']}", headers=auth_headers(admin_token)
    )
    assert response.status_code == 200
    assert response.get_json()["data"]["requesterName"] == "KubeSight automation"


def test_viewer_cannot_start_or_configure(client, viewer_token, app):
    ticket = _make_ticket()
    response = client.post(
        "/api/zoho/automation/runs",
        json={"ticketRecordId": ticket.id},
        headers=auth_headers(viewer_token),
    )
    assert response.status_code == 403
    response = client.put(
        "/api/zoho/jenkins", json={"enabled": False}, headers=auth_headers(viewer_token)
    )
    assert response.status_code == 403
