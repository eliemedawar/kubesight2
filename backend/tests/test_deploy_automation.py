"""Deploy automation: Jenkins config CRUD + the per-ticket run state machine.

Runs execute against the mock cluster ("prod-us-east" / "payments" /
"payments-api" from mock_data). Registry + live-YAML reads are monkeypatched at
their module attributes â€” the service imports them function-locally, so the
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


def _make_ticket(resolved=True, tag="v9.9.9", variable=None, value=None):
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
        variable_name=variable,
        variable_value=value,
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


def test_jenkins_registry_pin_roundtrip(client, admin_token, app):
    """The image-check registry pin must reference a real connection; null = auto."""
    response = client.put(
        "/api/zoho/jenkins",
        json={"registryConnectionId": 424242},
        headers=auth_headers(admin_token),
    )
    assert response.status_code == 400
    assert "registry" in response.get_json()["error"].lower()

    from api.services import registry_service

    with app.app_context():
        conn = registry_service.create_connection(
            {"name": "Nexus", "baseUrl": "10.1.1.5", "authMode": "none"}
        )
    response = client.put(
        "/api/zoho/jenkins",
        json={"registryConnectionId": conn["id"]},
        headers=auth_headers(admin_token),
    )
    assert response.status_code == 200
    assert response.get_json()["data"]["registryConnectionId"] == conn["id"]

    response = client.put(
        "/api/zoho/jenkins",
        json={"registryConnectionId": None},
        headers=auth_headers(admin_token),
    )
    assert response.status_code == 200
    assert response.get_json()["data"]["registryConnectionId"] is None


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
    """No linked registry (no_connection â‡’ build required) + Jenkins disabled â‡’ clear failure."""
    ticket = _make_ticket()
    run = _start(client, admin_token, ticket.id)
    assert run["status"] == "failed"
    assert "Jenkins is not configured" in run["error"]
    assert _step(run, "image_check")["status"] == "done"
    assert _step(run, "build")["status"] == "fail"


def test_run_bundle_path_when_cluster_requires_approval(client, admin_token, app, monkeypatch):
    """Image already in the registry â‡’ skip build/verify â‡’ Change Bundle handoff."""
    monkeypatch.setattr(
        "api.services.registry_service.check_image",
        lambda image, **kw: {"status": "found", "image": image},
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
    # Only the image changed â€” the live spec (replicas, resources) is preserved.
    assert "replicas: 3" in item.yaml_preview

    # Duplicate start for the same ticket/deployment is refused while active.
    body = _start(client, admin_token, ticket.id, expect=409)
    assert "already active" in body["error"]

    # Bundle completes (executor's job) â†’ the run lands on deployed.
    from api.services.deploy_automation_service import advance_runs

    bundle.status = "completed"
    db.session.commit()
    advance_runs()
    row = DeployAutomationRun.query.get(run["id"])
    assert row.status == "deployed"
    # Bundle completion also passes through the pod-health gate (instant in mock).
    pods = next(s for s in row.steps if s["key"] == "pods")
    assert pods["status"] == "done"


def test_ticket_writeback_on_deploy(client, admin_token, app, monkeypatch):
    """A finished run writes status + owner + comment back to its Desk ticket."""
    from api.models import ZohoIntegration
    from api.secret_encryption import encrypt_secret

    zi = ZohoIntegration.query.get(1) or ZohoIntegration(id=1)
    zi.enabled = True
    zi.org_id = "854214247"
    zi.client_id = "1000.abc"
    zi.refresh_token_encrypted = encrypt_secret("rt")
    zi.client_secret_encrypted = encrypt_secret("cs")
    zi.ticket_writeback_enabled = True
    zi.ticket_owner_email = "zagent@areeba.com"
    zi.ticket_status_deployed = "Closed"
    db.session.add(zi)
    db.session.commit()

    calls = {"update": [], "comment": []}
    monkeypatch.setattr(
        "api.services.registry_service.check_image",
        lambda image, **kw: {"status": "found", "image": image},
    )
    monkeypatch.setattr(
        "api.services.zoho_client.resolve_agent_id",
        lambda cfg, email: "999149000003619001",
    )
    monkeypatch.setattr(
        "api.services.zoho_client.update_ticket",
        lambda cfg, tid, fields: calls["update"].append((tid, fields)) or {},
    )
    monkeypatch.setattr(
        "api.services.zoho_client.add_ticket_comment",
        lambda cfg, tid, content, **kw: calls["comment"].append((tid, content)) or {},
    )
    _set_cluster_approvals(0)
    ticket = _make_ticket(tag="v8.0.0")

    run = _start(client, admin_token, ticket.id)
    assert run["status"] == "deployed"
    # Owner reassigned, deployed status set, comment posted â€” all on the Desk id.
    assert any(f.get("assigneeId") == "999149000003619001" for _, f in calls["update"])
    assert any(f.get("status") == "Closed" for _, f in calls["update"])
    assert any(f.get("resolution") for _, f in calls["update"])
    assert calls["comment"], "a comment should be posted"
    assert all(tid == ticket.ticket_id for tid, _ in calls["update"])


def test_ticket_writeback_off_makes_no_calls(client, admin_token, app, monkeypatch):
    """With write-back disabled (default), no Zoho ticket calls are made."""
    hit = []
    monkeypatch.setattr(
        "api.services.registry_service.check_image",
        lambda image, **kw: {"status": "found", "image": image},
    )
    monkeypatch.setattr(
        "api.services.zoho_client.update_ticket",
        lambda *a, **k: hit.append(1) or {},
    )
    _set_cluster_approvals(0)
    ticket = _make_ticket(tag="v8.0.1")
    run = _start(client, admin_token, ticket.id)
    assert run["status"] == "deployed"
    assert hit == []


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

    # Flip: global ON but the cluster is overridden to manual â€” nothing starts.
    row = JenkinsConnection.query.get(1)
    row.auto_run_tickets = True
    row.auto_run_clusters = {CLUSTER: "manual"}
    db.session.commit()

    before = DeployAutomationRun.query.count()
    ticket2 = _make_ticket(tag="v1.0.1")
    assert maybe_auto_run(ticket2.id) is None
    assert DeployAutomationRun.query.count() == before


def test_run_direct_path_when_no_approval_required(client, admin_token, app, monkeypatch):
    """Approval-free cluster â‡’ image applied immediately (mock mode), with the
    ticket's raw tag resolved through the image tag template."""
    monkeypatch.setattr(
        "api.services.registry_service.check_image",
        lambda image, **kw: {"status": "found", "image": image},
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
        lambda image, **kw: {"status": "not_found", "image": image},
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


def test_router_param_toggles(client, admin_token, app, monkeypatch):
    """Unticked router parameters are left out of buildWithParameters; saving
    with all three off is rejected."""
    captured = {}

    def fake_trigger(cfg, params):
        captured["params"] = params
        return "http://jenkins/queue/item/2"

    monkeypatch.setattr(
        "api.services.registry_service.check_image",
        lambda image, **kw: {"status": "not_found", "image": image},
    )
    monkeypatch.setattr("api.services.jenkins_client.trigger_build", fake_trigger)
    response = client.put(
        "/api/zoho/jenkins",
        json={
            "enabled": True,
            "baseUrl": "http://10.43.17.16:8080",
            "username": "admin",
            "apiToken": "api-t0ken",
            "routerJobPath": "georgio-testing",
            "sendParamNamespace": False,
            "sendParamTag": False,
        },
        headers=auth_headers(admin_token),
    )
    assert response.status_code == 200
    data = response.get_json()["data"]
    assert data["sendParamApp"] is True
    assert data["sendParamNamespace"] is False
    assert data["sendParamTag"] is False

    ticket = _make_ticket(tag="1.73.13")
    run = _start(client, admin_token, ticket.id)
    assert run["status"] == "building", run
    assert captured["params"] == {"APP": DEPLOYMENT}

    # All three off is a misconfiguration â€” rejected, config left untouched.
    response = client.put(
        "/api/zoho/jenkins",
        json={"sendParamApp": False},
        headers=auth_headers(admin_token),
    )
    assert response.status_code == 400
    assert "At least one router parameter" in response.get_json()["error"]
    response = client.get("/api/zoho/jenkins", headers=auth_headers(admin_token))
    assert response.get_json()["data"]["sendParamApp"] is True


def test_image_tag_template_resolution(app):
    from api.models import JenkinsConnection
    from api.services.deploy_automation_service import resolve_image_tag

    row = JenkinsConnection(id=1, image_tag_template="v{tag}-prod")
    assert resolve_image_tag(row, "1.72.1") == "v1.72.1-prod"
    # Operator already typed the full registry form (any case) â†’ used as-is.
    assert resolve_image_tag(row, "V1.72.5-prod") == "V1.72.5-prod"
    assert resolve_image_tag(row, "v2.0.0-prod") == "v2.0.0-prod"
    # No template configured â†’ raw passthrough.
    assert resolve_image_tag(None, "1.2.3") == "1.2.3"
    row.image_tag_template = "{tag}"
    assert resolve_image_tag(row, "1.2.3") == "1.2.3"


def test_rollout_failure_rolls_back_and_emails_admins(client, admin_token, app, monkeypatch):
    """Pods never come up â‡’ rollout undo + failed run + admin notification."""
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
    observedGeneration) must not mark the run deployed â€” otherwise the check
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
        # Fully "ready" â€” but the controller hasn't seen generation 7 yet.
        "stale": {
            "metadata": {"generation": 7},
            "spec": {"replicas": 3},
            "status": {
                "observedGeneration": 6,
                "replicas": 3,
                "readyReplicas": 3,
                "updatedReplicas": 3,
                "availableReplicas": 3,
            },
        },
        "fresh": {
            "metadata": {"generation": 7},
            "spec": {"replicas": 3},
            "status": {
                "observedGeneration": 7,
                "replicas": 3,
                "readyReplicas": 3,
                "updatedReplicas": 3,
                "availableReplicas": 3,
            },
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

    # The controller catches up â†’ the same counters now count.
    current["key"] = "fresh"
    svc.advance_runs()
    row = db.session.get(DeployAutomationRun, run.id)
    assert row.status == "deployed"


def test_rollout_watch_not_fooled_by_stuck_rolling_update(client, admin_token, app, monkeypatch):
    """A stuck rolling update reads ready=1 (the OLD pod, still serving) and
    updated=1 (the NEW pod, crashlooping) with desired=1: readyReplicas counts
    every ReplicaSet and updatedReplicas ignores readiness, so those two alone
    go green while the new pod crashloops. The run must only complete once the
    old pod is gone and the new one is available."""
    from api.models import JenkinsConnection
    from api.services import deploy_automation_service as svc

    db.session.add(JenkinsConnection(id=1))
    run = DeployAutomationRun(
        cluster_id=CLUSTER,
        namespace=NAMESPACE,
        deployment_name=DEPLOYMENT,
        image_tag="v6.6.6",
        status="verifying_rollout",
        steps=[],
        rollout_started_at=datetime.now(timezone.utc),
    )
    db.session.add(run)
    db.session.commit()

    payloads = {
        # Old-RS pod ready + new-RS pod crashlooping: 2 pods total, 1 ready.
        "stuck": {
            "metadata": {"generation": 2},
            "spec": {"replicas": 1},
            "status": {
                "observedGeneration": 2,
                "replicas": 2,
                "readyReplicas": 1,
                "updatedReplicas": 1,
                "availableReplicas": 1,
            },
        },
        # New pod recovered, old ReplicaSet scaled down.
        "rolled": {
            "metadata": {"generation": 2},
            "spec": {"replicas": 1},
            "status": {
                "observedGeneration": 2,
                "replicas": 1,
                "readyReplicas": 1,
                "updatedReplicas": 1,
                "availableReplicas": 1,
            },
        },
    }
    current = {"key": "stuck"}
    monkeypatch.setattr("api.k8s_provider.should_use_real_k8s", lambda cid=None: True)
    monkeypatch.setattr("api.k8s_provider.resolve_cluster_access", lambda cid: object())
    monkeypatch.setattr(
        "api.k8s_provider._run_for_access",
        lambda access, args: json.dumps(payloads[current["key"]]),
    )

    svc.advance_runs()
    row = db.session.get(DeployAutomationRun, run.id)
    assert row.status == "verifying_rollout", "the old pod's readiness must not complete the run"
    pods = next(s for s in row.steps if s["key"] == "pods")
    assert "old pod" in pods["detail"]

    current["key"] = "rolled"
    svc.advance_runs()
    row = db.session.get(DeployAutomationRun, run.id)
    assert row.status == "deployed"


def test_cancel_withdraws_pending_and_approved_bundles(client, admin_token, app, monkeypatch):
    """Cancelling a run must stop its Change Bundle too â€” a bundle that outlives
    the run would deploy later with no run watching the rollout."""
    monkeypatch.setattr(
        "api.services.registry_service.check_image",
        lambda image, **kw: {"status": "found", "image": image},
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

    # Bundle still pending approval â†’ rejected via the normal decide path.
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

    # Bundle already approved (executor hasn't started) â†’ withdrawn directly.
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
        lambda image, **kw: {"status": "found", "image": image},
    )
    _set_cluster_approvals(0)  # direct path, no bundle needed
    db.session.add(
        JenkinsConnection(id=1, auto_run_tickets=True, image_tag_template="{tag}")
    )
    db.session.commit()
    ticket = _make_ticket(tag="1.0.0")

    first = maybe_auto_run(ticket.id)
    assert first is not None
    # Second delivery of the SAME ticket â†’ no new run.
    second = maybe_auto_run(ticket.id)
    assert second is None
    assert DeployAutomationRun.query.filter_by(ticket_record_id=ticket.id).count() == 1


def test_manager_reject_is_authoritative(client, admin_token, app, monkeypatch):
    """One manager reject finalizes the bundle even when the recipient pool is
    larger than the required-approval count (the old quorum needed 2 declines)."""
    monkeypatch.setattr(
        "api.services.registry_service.check_image",
        lambda image, **kw: {"status": "found", "image": image},
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
        lambda image, **kw: {"status": "found", "image": image},
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


LIVE_ENV_DEPLOYMENT_YAML = """apiVersion: apps/v1
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
          env:
            - name: LOG_LEVEL
              value: info
            - name: DB_URL
              valueFrom:
                secretKeyRef:
                  name: db
                  key: url
"""


def test_variable_run_validations(client, admin_token, app):
    """A ticket carries exactly one change: tag XOR variable(+value)."""
    both = _make_ticket(tag="v1.0.0", variable="LOG_LEVEL", value="debug")
    body = _start(client, admin_token, both.id, expect=400)
    assert "exactly one" in body["error"]

    no_value = _make_ticket(tag="", variable="LOG_LEVEL", value="")
    body = _start(client, admin_token, no_value.id, expect=400)
    assert "no value" in body["error"]


def test_variable_run_direct_path(client, admin_token, app):
    """Approval-free cluster â‡’ the variable change applies immediately (mock
    mode): image gate first, then the variable check, no Jenkins build."""
    _set_cluster_approvals(0)
    ticket = _make_ticket(tag="", variable="LOG_LEVEL", value="debug")

    run = _start(client, admin_token, ticket.id)
    assert run["status"] == "deployed", run
    assert run["changeType"] == "env_var"
    assert run["variableName"] == "LOG_LEVEL" and run["variableValue"] == "debug"
    # Step 1 is the registry gate on the RUNNING image (no linked registry in
    # tests â‡’ passes with a "cannot verify" note), then the variable check.
    assert _step(run, "image_check")["status"] == "done"
    assert _step(run, "build")["status"] == "skip"
    assert _step(run, "verify")["status"] == "done"
    assert _step(run, "approval")["status"] == "skip"
    assert _step(run, "deploy")["status"] == "done"
    assert "LOG_LEVEL=debug" in _step(run, "deploy")["detail"]
    assert _step(run, "pods")["status"] == "done"
    assert run["bundleId"] is None


def test_variable_run_blocks_when_running_image_missing(client, admin_token, app, monkeypatch):
    """The image gate: a variable change restarts every pod, so a running image
    that has been pruned from the registry blocks the edit before anything is
    applied (no ImagePullBackOff surprise)."""
    live_deployment = {
        "metadata": {"generation": 1},
        "spec": {
            "replicas": 1,
            "template": {
                "spec": {
                    "containers": [
                        {
                            "name": "payments-api",
                            "image": "ghcr.io/mock/payments:v2.8.1",
                            "env": [{"name": "LOG_LEVEL", "value": "info"}],
                        }
                    ]
                }
            },
        },
        "status": {
            "observedGeneration": 1,
            "replicas": 1,
            "readyReplicas": 1,
            "updatedReplicas": 1,
            "availableReplicas": 1,
        },
    }
    monkeypatch.setattr("api.k8s_provider.should_use_real_k8s", lambda cid=None: True)
    monkeypatch.setattr("api.k8s_provider.resolve_cluster_access", lambda cid: object())
    monkeypatch.setattr(
        "api.k8s_provider._run_for_access", lambda access, args: json.dumps(live_deployment)
    )
    monkeypatch.setattr(
        "api.services.registry_service.check_image",
        lambda image, **kw: {"status": "not_found", "image": image},
    )
    kubectl_calls = []
    monkeypatch.setattr(
        "api.services.deployment_service._run_kubectl_for_cluster",
        lambda cid, args: kubectl_calls.append(args) or "",
    )
    _set_cluster_approvals(0)

    ticket = _make_ticket(tag="", variable="LOG_LEVEL", value="debug")
    run = _start(client, admin_token, ticket.id)
    assert run["status"] == "failed", run
    assert _step(run, "image_check")["status"] == "fail"
    assert "no longer in the registry" in run["error"]
    assert kubectl_calls == [], "the variable change must not be applied"


def test_variable_run_bundle_path(client, admin_token, app, monkeypatch):
    """Approval cluster â‡’ a Change Bundle carrying the live YAML with ONLY the
    variable's value edited (valueFrom refs + the rest of the spec untouched)."""
    monkeypatch.setattr(
        "api.services.resource_actions_service.get_resource_yaml",
        lambda user, cluster_id, namespace, kind, name: (
            {"yaml": LIVE_ENV_DEPLOYMENT_YAML},
            None,
            200,
        ),
    )
    _set_cluster_approvals(1)
    ticket = _make_ticket(tag="", variable="LOG_LEVEL", value="debug")

    run = _start(client, admin_token, ticket.id)
    assert run["status"] == "awaiting_approval", run
    assert run["bundleId"]
    bundle = ChangeBundle.query.get(run["bundleId"])
    assert bundle.status == "pending_approval"
    item = bundle.items[0]
    assert item.action_type == "edit_deployment"
    assert "value: debug" in item.yaml_preview
    assert "value: info" not in item.yaml_preview
    # Everything else is preserved: replicas, image, the secret reference.
    assert "replicas: 3" in item.yaml_preview
    assert "secretKeyRef" in item.yaml_preview
    assert "ghcr.io/mock/payments:v2.8.1" in item.yaml_preview
    assert "LOG_LEVEL=debug" in (bundle.note or "")


def test_variable_run_missing_var_fails_bundle_prep(client, admin_token, app, monkeypatch):
    """The live YAML has no such literal var â‡’ the bundle prep fails clearly."""
    monkeypatch.setattr(
        "api.services.resource_actions_service.get_resource_yaml",
        lambda user, cluster_id, namespace, kind, name: (
            {"yaml": LIVE_ENV_DEPLOYMENT_YAML},
            None,
            200,
        ),
    )
    _set_cluster_approvals(1)
    ticket = _make_ticket(tag="", variable="NOT_A_VAR", value="x")
    run = _start(client, admin_token, ticket.id)
    assert run["status"] == "failed"
    assert "NOT_A_VAR" in run["error"]


def test_variable_resolve_validates_against_live_spec(client, admin_token, app, monkeypatch):
    """Against a (fake) real cluster: valueFrom vars are refused, unknown vars
    list the changeable ones, and a literal var applies via `kubectl set env`
    targeting exactly the containers that define it."""
    live_deployment = {
        "metadata": {"generation": 1},
        "spec": {
            "replicas": 1,
            "template": {
                "spec": {
                    "containers": [
                        {
                            "name": "payments-api",
                            "image": "ghcr.io/mock/payments:v2.8.1",
                            "env": [
                                {"name": "LOG_LEVEL", "value": "info"},
                                {"name": "DB_URL", "valueFrom": {"secretKeyRef": {"name": "db", "key": "url"}}},
                            ],
                        },
                        {"name": "sidecar", "image": "ghcr.io/mock/sidecar:v1"},
                    ]
                }
            },
        },
        "status": {
            "observedGeneration": 1,
            "replicas": 1,
            "readyReplicas": 1,
            "updatedReplicas": 1,
            "availableReplicas": 1,
        },
    }
    monkeypatch.setattr("api.k8s_provider.should_use_real_k8s", lambda cid=None: True)
    monkeypatch.setattr("api.k8s_provider.resolve_cluster_access", lambda cid: object())
    monkeypatch.setattr(
        "api.k8s_provider._run_for_access", lambda access, args: json.dumps(live_deployment)
    )
    kubectl_calls = []
    monkeypatch.setattr(
        "api.services.deployment_service._run_kubectl_for_cluster",
        lambda cid, args: kubectl_calls.append((cid, args)) or "deployment.apps env updated",
    )
    _set_cluster_approvals(0)

    # valueFrom var â†’ refused with a pointer to the source.
    ticket = _make_ticket(tag="", variable="DB_URL", value="postgres://x")
    run = _start(client, admin_token, ticket.id)
    assert run["status"] == "failed"
    assert "valueFrom" in run["error"]

    # Unknown var â†’ failure lists the changeable variables.
    ticket = _make_ticket(tag="", variable="NOPE", value="1")
    run = _start(client, admin_token, ticket.id)
    assert run["status"] == "failed"
    assert "LOG_LEVEL" in run["error"]

    # Literal var â†’ applied, targeting only the defining container.
    ticket = _make_ticket(tag="", variable="LOG_LEVEL", value="debug")
    run = _start(client, admin_token, ticket.id)
    assert run["status"] == "deployed", run
    assert kubectl_calls, "kubectl set env should have been invoked"
    _, args = kubectl_calls[0]
    assert args[:2] == ["set", "env"]
    assert f"deployment/{DEPLOYMENT}" in args
    assert "LOG_LEVEL=debug" in args
    assert "-c" in args and args[args.index("-c") + 1] == "payments-api"


def test_variable_values_dedupe_case_insensitively(app):
    """Zoho compares picklist values case-insensitively â€” 'encryption_key' and
    'ENCRYPTION_KEY' must publish as ONE option (HTTP 400 'duplicate value'
    otherwise), and the Appâ†’Variable cascade must reference exactly the
    published spellings."""
    from api.services.zoho_sync_service import _app_to_variables, build_variable_values

    entries = [
        {"id": 1, "namespace": "ns1", "name": "svc-a", "label": "svc-a"},
        {"id": 2, "namespace": "ns2", "name": "svc-b", "label": "svc-b"},
    ]
    vars_by_ns = {
        "ns1": {"svc-a": ["ENCRYPTION_KEY", "log_level"]},
        "ns2": {"svc-b": ["encryption_key", "LOG_LEVEL", "EXTRA"]},
    }
    values = build_variable_values(entries, vars_by_ns)
    assert values[0] == "-None-"
    lowered = [v.lower() for v in values[1:]]
    assert sorted(lowered) == ["encryption_key", "extra", "log_level"]
    assert len(lowered) == len(set(lowered)), "no Zoho-level duplicates"

    mapping = _app_to_variables(entries, vars_by_ns)
    published = set(values)
    for child_values in mapping.values():
        assert set(child_values) <= published, "cascade children must be published spellings"
    # Both apps expose the SAME canonical spelling for the shared variable.
    shared_a = {v for v in mapping["svc-a"] if v.lower() == "encryption_key"}
    shared_b = {v for v in mapping["svc-b"] if v.lower() == "encryption_key"}
    assert shared_a == shared_b


def test_cascade_resync_rebuilds_chain_parent_first(app, monkeypatch):
    """Re-syncing with BOTH mappings already in Zoho must not trip the chain
    check: Zoho rejects creating Environmentâ†’Application while Application still
    parents Applicationâ†’Variable (HTTP 422 'invalid child Id'). The sync must
    delete every managed mapping first, then rebuild parent-first."""
    from api.db import db
    from api.models import ZohoIntegration
    from api.services import zoho_sync_service as svc
    from api.services.zoho_client import ZohoConfig, ZohoError

    ENV_F, APP_F, VAR_F = "111", "222", "333"
    row = ZohoIntegration.query.get(1) or ZohoIntegration(id=1)
    row.cascade_enabled = True
    row.sync_application = True
    row.sync_environment = True
    row.sync_variables = True
    row.app_field_id = APP_F
    row.environment_field_id = ENV_F
    row.variable_field_id = VAR_F
    db.session.add(row)
    db.session.commit()

    cfg = ZohoConfig(
        api_base="https://desk.example/api/v1",
        accounts_base="https://accounts.example",
        token_endpoint="https://accounts.example/oauth/v2/token",
        org_id="1",
        layout_id="L1",
        app_field_id=APP_F,
        client_id="c",
        client_secret="s",
        refresh_token="r",
        environment_field_id=ENV_F,
    )

    # State after a successful first sync: both cascade levels live in Zoho.
    store = {
        "m1": {"id": "m1", "parentId": ENV_F, "childId": APP_F},
        "m2": {"id": "m2", "parentId": APP_F, "childId": VAR_F},
    }
    created_order = []

    def fake_create(cfg_, body):
        # Zoho's chain validation (observed live): the new mapping's child must
        # not already be the parent of another mapping.
        if any(m["parentId"] == body["childId"] for m in store.values()):
            raise ZohoError(
                "POST dependency mapping failed (HTTP 422): "
                "Validation failed for the condition : invalid child Id",
                422,
            )
        new_id = f"new{len(created_order) + 1}"
        store[new_id] = {"id": new_id, "parentId": body["parentId"], "childId": body["childId"]}
        created_order.append((body["parentId"], body["childId"]))
        return {"id": new_id}

    monkeypatch.setattr(
        "api.services.zoho_client.list_dependency_mappings",
        lambda cfg_: {"data": list(store.values())},
    )
    monkeypatch.setattr(
        "api.services.zoho_client.delete_dependency_mapping",
        lambda cfg_, mid: store.pop(mid, None) or {},
    )
    monkeypatch.setattr("api.services.zoho_client.create_dependency_mapping", fake_create)

    entries = [{"id": 1, "namespace": "verto-sit", "name": "svc-a", "label": "svc-a"}]
    vars_by_ns = {"verto-sit": {"svc-a": ["LOG_LEVEL"]}}
    result = svc._maybe_sync_cascade(row, cfg, entries, vars_by_ns)

    assert result["status"] == "ok", result
    assert created_order == [(ENV_F, APP_F), (APP_F, VAR_F)], "must rebuild parent-first"
    pairs = {(m["parentId"], m["childId"]) for m in store.values()}
    assert pairs == {(ENV_F, APP_F), (APP_F, VAR_F)}
    assert row.dependency_mapping_id and row.variable_mapping_id


def test_variable_resolve_matches_case_insensitively(client, admin_token, app, monkeypatch):
    """A ticket carrying the merged picklist spelling still applies with the
    deployment's ACTUAL env-var name (env names are case-sensitive)."""
    live_deployment = {
        "metadata": {"generation": 1},
        "spec": {
            "replicas": 1,
            "template": {
                "spec": {
                    "containers": [
                        {
                            "name": "payments-api",
                            "image": "ghcr.io/mock/payments:v2.8.1",
                            "env": [{"name": "LOG_LEVEL", "value": "info"}],
                        }
                    ]
                }
            },
        },
        "status": {
            "observedGeneration": 1,
            "replicas": 1,
            "readyReplicas": 1,
            "updatedReplicas": 1,
            "availableReplicas": 1,
        },
    }
    monkeypatch.setattr("api.k8s_provider.should_use_real_k8s", lambda cid=None: True)
    monkeypatch.setattr("api.k8s_provider.resolve_cluster_access", lambda cid: object())
    monkeypatch.setattr(
        "api.k8s_provider._run_for_access", lambda access, args: json.dumps(live_deployment)
    )
    kubectl_calls = []
    monkeypatch.setattr(
        "api.services.deployment_service._run_kubectl_for_cluster",
        lambda cid, args: kubectl_calls.append(args) or "deployment.apps env updated",
    )
    _set_cluster_approvals(0)

    ticket = _make_ticket(tag="", variable="log_level", value="warn")
    run = _start(client, admin_token, ticket.id)
    assert run["status"] == "deployed", run
    # Applied with the live spelling, not the ticket's.
    assert run["variableName"] == "LOG_LEVEL"
    assert "LOG_LEVEL=warn" in kubectl_calls[0]


def test_resolve_inbound_parses_variable_change(client, admin_token, app):
    """The webhook parses cf_variable/cf_value, and flags tag+variable tickets."""
    from api.services.zoho_sync_service import resolve_inbound

    _make_ticket()  # ensures the snapshot exists
    result = resolve_inbound(
        {
            "ticketId": "vt-1",
            "ticketNumber": "DR-7001",
            "cf_application": DEPLOYMENT,
            "cf_environment": NAMESPACE,
            "cf_variable": "LOG_LEVEL",
            "cf_value": "debug",
        }
    )
    assert result["resolved"] is True
    assert result["variableName"] == "LOG_LEVEL"
    assert result["variableValue"] == "debug"
    assert result["error"] is None

    # The -None- picklist placeholder is not a variable.
    result = resolve_inbound(
        {
            "ticketId": "vt-2",
            "cf_application": DEPLOYMENT,
            "cf_environment": NAMESPACE,
            "cf_tag": "v1.2.3",
            "cf_variable": "-None-",
        }
    )
    assert result["error"] is None and result["variableName"] is None

    # Both a tag and a variable â†’ flagged, automation refuses to guess.
    result = resolve_inbound(
        {
            "ticketId": "vt-3",
            "cf_application": DEPLOYMENT,
            "cf_environment": NAMESPACE,
            "cf_tag": "v1.2.3",
            "cf_variable": "LOG_LEVEL",
            "cf_value": "debug",
        }
    )
    assert result["resolved"] is True
    assert "exactly one" in result["error"]

    # Variable without a value â†’ flagged too.
    result = resolve_inbound(
        {
            "ticketId": "vt-4",
            "cf_application": DEPLOYMENT,
            "cf_environment": NAMESPACE,
            "cf_variable": "LOG_LEVEL",
        }
    )
    assert "no Value" in result["error"]

    # No change at all (webhook payload missing the fields) â†’ flagged, so the
    # operator sees WHY nothing ran instead of a silent dash.
    result = resolve_inbound(
        {
            "ticketId": "vt-5",
            "cf_application": DEPLOYMENT,
            "cf_environment": NAMESPACE,
        }
    )
    assert result["resolved"] is True
    assert "carries no change" in result["error"]

    # The user's real field api name arrives as cf_env_variable â€” parsed too.
    result = resolve_inbound(
        {
            "ticketId": "vt-6",
            "cf_application": DEPLOYMENT,
            "cf_environment": NAMESPACE,
            "cf_env_variable": "LOG_LEVEL",
            "cf_value": "debug",
        }
    )
    assert result["variableName"] == "LOG_LEVEL"
    assert result["variableValue"] == "debug"
    assert result["error"] is None


def test_inbound_log_pruned_to_newest_10(client, admin_token, app):
    """Every webhook delivery trims the inbound log to the 10 newest tickets,
    deleting pruned tickets' finished runs â€” but never a ticket whose run is
    still active (write-back + duplicate-delivery guard need the row)."""
    from api.services.zoho_sync_service import resolve_inbound

    protected = _make_ticket(tag="v0.0.1")
    db.session.add(
        DeployAutomationRun(
            ticket_record_id=protected.id,
            cluster_id=CLUSTER,
            namespace=NAMESPACE,
            deployment_name=DEPLOYMENT,
            image_tag="v0.0.1",
            status="awaiting_approval",
            steps=[],
        )
    )
    finished = _make_ticket(tag="v0.0.2")
    db.session.add(
        DeployAutomationRun(
            ticket_record_id=finished.id,
            cluster_id=CLUSTER,
            namespace=NAMESPACE,
            deployment_name=DEPLOYMENT,
            image_tag="v0.0.2",
            status="deployed",
            steps=[],
        )
    )
    for i in range(12):
        db.session.add(
            ZohoInboundTicket(
                ticket_id=f"prune-fill-{i}",
                resolved=True,
                tag="v1.0.0",
                received_at=datetime.now(timezone.utc),
            )
        )
    db.session.commit()

    # The webhook path is the intake choke point â€” it triggers the prune.
    resolve_inbound(
        {
            "ticketId": "prune-new",
            "cf_application": DEPLOYMENT,
            "cf_environment": NAMESPACE,
            "cf_tag": "v2.0.0",
        }
    )

    remaining = {t.id for t in ZohoInboundTicket.query.all()}
    assert protected.id in remaining, "a ticket with an active run must survive"
    assert finished.id not in remaining
    # The 10 newest + the protected straggler.
    assert len(remaining) == 11
    # The pruned ticket's runs went with it; the active run is untouched.
    assert DeployAutomationRun.query.filter_by(ticket_record_id=finished.id).count() == 0
    assert DeployAutomationRun.query.filter_by(ticket_record_id=protected.id).count() == 1


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


# ---------------------------------------------------------------------------
# Custom (non-cluster) environments — free-text Environment/Application entries
# that route straight to a Jenkins job with an operator-defined parameter map.
# ---------------------------------------------------------------------------

CUSTOM_ENVS = [
    {
        "name": "POS-UAT",
        "applications": ["pos"],
        "jenkinsJobPath": "pos-deploy",
        "jenkinsParams": {
            "msName": "{app}",
            "repotag": "{tag}",
            "envi": "uat",
            "company": "{cf_company}",
            "debugProd": "{cf_debugProd}",
            "country": "{cf_country}",
        },
    }
]


def _set_custom_source(cluster_id=CLUSTER, namespaces=None, customs=CUSTOM_ENVS):
    from api.services.zoho_sync_service import set_source

    return set_source(cluster_id, namespaces or [NAMESPACE], None, customs)


def test_custom_environments_publish_values_and_cascade(app):
    """Custom env names join the Environment picklist, their apps join the
    Application picklist, and the cascade maps env → its apps."""
    from api.models import ZohoIntegration
    from api.services.zoho_sync_service import (
        CUSTOM_SOURCE_CLUSTER,
        _application_values,
        _namespace_to_labels,
        _source_entries,
        build_environment_values,
    )

    with app.app_context():
        _set_custom_source()
        row = ZohoIntegration.query.get(1)
        env_values = build_environment_values(row)
        assert NAMESPACE in env_values and "POS-UAT" in env_values

        entries = _source_entries(row)
        customs = [e for e in entries if e.get("custom")]
        assert [(e["namespace"], e["name"]) for e in customs] == [("POS-UAT", "pos")]
        assert "pos" in _application_values(entries)
        assert _namespace_to_labels(entries).get("POS-UAT") == ["pos"]

        # Snapshotted under the sentinel cluster for inbound resolution.
        snap = ZohoDeploymentSnapshot.query.filter_by(
            cluster_id=CUSTOM_SOURCE_CLUSTER, namespace="POS-UAT", deployment_name="pos"
        ).first()
        assert snap is not None


def test_custom_environment_name_collision_rejected(client, admin_token, app):
    """A custom env named like a selected namespace is refused (Zoho compares
    picklist values case-insensitively)."""
    response = client.put(
        "/api/zoho/source",
        json={
            "clusterId": CLUSTER,
            "namespaces": [NAMESPACE],
            "customEnvironments": [{"name": NAMESPACE.upper(), "applications": ["x"]}],
        },
        headers=auth_headers(admin_token),
    )
    assert response.status_code == 400
    assert "collides" in response.get_json()["error"]


def test_custom_environment_inbound_resolves(client, admin_token, app):
    """A ticket for a custom env resolves via the sentinel snapshot."""
    from api.services.zoho_sync_service import CUSTOM_SOURCE_CLUSTER, resolve_inbound

    with app.app_context():
        _set_custom_source()
        from api.models import ZohoIntegration
        from api.services.zoho_sync_service import _source_entries

        _source_entries(ZohoIntegration.query.get(1))  # snapshot the custom entries

    result = resolve_inbound(
        {
            "ticketId": "pos-1",
            "ticketNumber": "DR-8001",
            "cf_application": "pos",
            "cf_environment": "POS-UAT",
            "cf_tag": "2.4.0",
            "cf_company": "kozen",
            "cf_country": "Lebanon",
        }
    )
    assert result["resolved"] is True, result
    assert result["error"] is None
    assert result["namespace"] == "POS-UAT"
    assert result["clusterId"] == CUSTOM_SOURCE_CLUSTER


def _make_custom_ticket(tag="2.4.0", variable=None, value=None, payload=None):
    from api.services.zoho_sync_service import CUSTOM_SOURCE_CLUSTER

    snapshot = ZohoDeploymentSnapshot.query.filter_by(
        cluster_id=CUSTOM_SOURCE_CLUSTER, namespace="POS-UAT", deployment_name="pos"
    ).first()
    if snapshot is None:
        snapshot = ZohoDeploymentSnapshot(
            cluster_id=CUSTOM_SOURCE_CLUSTER, namespace="POS-UAT", deployment_name="pos"
        )
        db.session.add(snapshot)
        db.session.flush()
    ticket = ZohoInboundTicket(
        ticket_id=f"pos-{datetime.now(timezone.utc).timestamp()}",
        ticket_number="DR-8002",
        resolved=True,
        app_service_id=snapshot.id,
        tag=tag,
        variable_name=variable,
        variable_value=value,
        payload=payload,
        received_at=datetime.now(timezone.utc),
    )
    db.session.add(ticket)
    db.session.commit()
    return ticket


def test_custom_environment_run_routes_to_jenkins(client, admin_token, app, monkeypatch):
    """A custom-env run skips every cluster stage: it triggers the entry's OWN
    Jenkins job with the rendered parameter map ({app}/{tag}/fixed/{cf_*}) and
    completes as deployed on build success — no registry, kubectl or rollout."""
    captured = {}

    def fake_trigger(cfg, params):
        captured["job_path"] = cfg.router_job_path
        captured["params"] = params
        return "http://jenkins/queue/item/77"

    monkeypatch.setattr("api.services.jenkins_client.trigger_build", fake_trigger)
    monkeypatch.setattr(
        "api.services.jenkins_client.queue_state",
        lambda cfg, url: {"state": "building", "buildNumber": 12, "buildUrl": "http://jenkins/job/pos-deploy/12"},
    )
    monkeypatch.setattr(
        "api.services.jenkins_client.build_state",
        lambda cfg, url: {"building": False, "result": "SUCCESS", "durationMs": 1000, "url": url},
    )
    response = client.put(
        "/api/zoho/jenkins",
        json={
            "enabled": True,
            "baseUrl": "http://jenkins:8080",
            "username": "bot",
            "apiToken": "t0ken",
            "routerJobPath": "router",
        },
        headers=auth_headers(admin_token),
    )
    assert response.status_code == 200

    with app.app_context():
        _set_custom_source()
        ticket = _make_custom_ticket(
            payload={"cf": {"cf_company": "kozen", "cf_debugProd": True, "cf_country": "Lebanon"}}
        )
        ticket_id = ticket.id

    run = _start(client, admin_token, ticket_id)
    # The trigger went to the entry's own job with the rendered param map.
    assert captured["job_path"] == "pos-deploy"
    assert captured["params"] == {
        "msName": "pos",
        "repotag": "2.4.0",
        "envi": "uat",
        "company": "kozen",
        "debugProd": "true",
        "country": "Lebanon",
    }
    assert _step(run, "image_check")["status"] == "skip"

    # Poll ticks: queue → build success → deployed, with the cluster stages skipped.
    from api.services.deploy_automation_service import advance_runs

    with app.app_context():
        advance_runs()
        advance_runs()
        final = DeployAutomationRun.query.get(run["id"])
        assert final.status == "deployed", (final.status, final.error)
        steps = {s["key"]: s["status"] for s in final.steps}
        assert steps["verify"] == "skip"
        assert steps["approval"] == "skip"
        assert steps["deploy"] == "done"
        assert steps["pods"] == "done"


def test_custom_environment_run_defaults_to_router_contract(client, admin_token, app, monkeypatch):
    """No job path / params on the entry → the router job gets APP/TAG/NAMESPACE."""
    captured = {}

    def fake_trigger(cfg, params):
        captured["job_path"] = cfg.router_job_path
        captured["params"] = params
        return "http://jenkins/queue/item/78"

    monkeypatch.setattr("api.services.jenkins_client.trigger_build", fake_trigger)
    response = client.put(
        "/api/zoho/jenkins",
        json={
            "enabled": True,
            "baseUrl": "http://jenkins:8080",
            "username": "bot",
            "apiToken": "t0ken",
            "routerJobPath": "router",
        },
        headers=auth_headers(admin_token),
    )
    assert response.status_code == 200

    with app.app_context():
        _set_custom_source(customs=[{"name": "POS-UAT", "applications": ["pos"]}])
        ticket = _make_custom_ticket()
        ticket_id = ticket.id

    run = _start(client, admin_token, ticket_id)
    assert run["status"] == "building", run
    assert captured["job_path"] == "router"
    assert captured["params"] == {"APP": "pos", "TAG": "2.4.0", "NAMESPACE": "POS-UAT"}


def test_custom_environment_rejects_variable_change(client, admin_token, app):
    with app.app_context():
        _set_custom_source()
        ticket = _make_custom_ticket(tag="", variable="LOG_LEVEL", value="debug")
        ticket_id = ticket.id
    body = _start(client, admin_token, ticket_id, expect=400)
    assert "custom environment" in body["error"]
