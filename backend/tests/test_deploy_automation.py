"""Deploy automation: Jenkins config CRUD + the per-ticket run state machine.

Runs execute against the mock cluster ("prod-us-east" / "payments" /
"payments-api" from mock_data). Registry + live-YAML reads are monkeypatched at
their module attributes — the service imports them function-locally, so the
patch is picked up at call time.
"""

from datetime import datetime, timezone

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
        json={"baseUrl": "https://jenkins.example.com", "username": "bot", "apiToken": "s3cret"},
        headers=auth_headers(admin_token),
    )
    assert response.status_code == 200
    data = response.get_json()["data"]
    assert data["apiTokenConfigured"] is True
    assert "apiToken" not in data and "s3cret" not in str(data)

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


def test_run_direct_path_when_no_approval_required(client, admin_token, app, monkeypatch):
    """Approval-free cluster ⇒ image applied immediately (mock mode)."""
    monkeypatch.setattr(
        "api.services.registry_service.check_image",
        lambda image: {"status": "found", "image": image},
    )
    _set_cluster_approvals(0)
    ticket = _make_ticket(tag="v8.0.0")

    run = _start(client, admin_token, ticket.id)
    assert run["status"] == "deployed", run
    assert _step(run, "approval")["status"] == "skip"
    assert _step(run, "deploy")["status"] == "done"
    assert "ghcr.io/mock/payments:v8.0.0" in _step(run, "deploy")["detail"]
    assert run["bundleId"] is None


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
