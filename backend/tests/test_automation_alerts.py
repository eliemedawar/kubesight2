"""Automation alerts: policy CRUD for the event-driven type, alert firing when
a deploy automation run fails, and auto-resolution when a later run for the
same target deploys successfully."""

from datetime import datetime, timezone

from api.alert_policy_catalog import AUTOMATION_ALERT_CLUSTER_ID
from api.db import db
from api.models import AlertHistory, AlertPolicy, DeployAutomationRun
from api.services import deploy_automation_service as das

from .conftest import auth_headers

CLUSTER = "prod-us-east"
NAMESPACE = "payments"
DEPLOYMENT = "payments-api"


def _create_policy(client, token, **overrides):
    payload = {
        "name": "Deploy failures",
        "alertType": "automation",
        "severity": "critical",
        **overrides,
    }
    response = client.post("/api/alert-policies", json=payload, headers=auth_headers(token))
    assert response.status_code == 201, response.get_json()
    return response.get_json()["data"]


def _make_run(status="building", ticket_number="DR-9001"):
    run = DeployAutomationRun(
        ticket_number=ticket_number,
        cluster_id=CLUSTER,
        namespace=NAMESPACE,
        deployment_name=DEPLOYMENT,
        container_name=DEPLOYMENT,
        image_repo="ghcr.io/mock/payments",
        image_tag="v9.9.9",
        status=status,
        created_at=datetime.now(timezone.utc),
    )
    db.session.add(run)
    db.session.commit()
    return run


def test_automation_policy_uses_sentinel_cluster(client, admin_token):
    policy = _create_policy(client, admin_token)
    assert policy["alertType"] == "automation"
    assert policy["clusterId"] == AUTOMATION_ALERT_CLUSTER_ID
    # No cluster selection is required for the event-driven type.
    row = AlertPolicy.query.get(policy["id"])
    assert row.cluster_id == AUTOMATION_ALERT_CLUSTER_ID
    assert row.conditions == []


def test_run_failure_fires_alert_and_success_resolves(client, admin_token):
    policy = _create_policy(client, admin_token)

    run = _make_run()
    das._fail(run, "build", "router build failed")
    db.session.commit()

    rows = AlertHistory.query.filter_by(alert_type="automation").all()
    assert len(rows) == 1
    alert = rows[0]
    assert alert.status == "active"
    assert alert.policy_id == policy["id"]
    assert alert.cluster_id == CLUSTER
    assert alert.namespace == NAMESPACE
    assert alert.resource_name == DEPLOYMENT
    assert alert.severity == "critical"
    assert "DR-9001" in alert.title
    assert alert.metric_snapshot["failedStep"] == "build"
    assert "router build failed" in alert.description

    # The failed run's alert shows up in the merged alerts feed.
    response = client.get("/api/alerts", headers=auth_headers(admin_token))
    assert response.status_code == 200
    items = response.get_json()["data"]["items"]
    automation_items = [item for item in items if item.get("alertType") == "automation"]
    assert len(automation_items) == 1
    assert automation_items[0]["ticketNumber"] == "DR-9001"
    assert automation_items[0]["status"] == "firing"

    # A later run deploying the same target successfully resolves the alert.
    retry = _make_run(status="verifying_rollout", ticket_number="DR-9001")
    das._complete_deployed(retry, "1/1 ready")
    db.session.commit()

    db.session.refresh(alert)
    assert alert.status == "resolved"
    assert alert.resolved_at is not None


def test_disabled_policy_does_not_fire(client, admin_token):
    policy = _create_policy(client, admin_token)
    response = client.patch(
        f"/api/alert-policies/{policy['id']}/status",
        json={"enabled": False},
        headers=auth_headers(admin_token),
    )
    assert response.status_code == 200

    run = _make_run()
    das._fail(run, "verify", "image never appeared in the registry")
    db.session.commit()

    assert AlertHistory.query.filter_by(alert_type="automation").count() == 0


def test_no_policy_means_no_alert(client, admin_token):
    run = _make_run()
    das._fail(run, "image_check", "registry unreachable")
    db.session.commit()

    assert AlertHistory.query.filter_by(alert_type="automation").count() == 0
