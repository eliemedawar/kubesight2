from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from api.alert_policy_catalog import SERVICE_ALERT_CLUSTER_ID
from api.db import db
from api.models import (
    AlertHistory,
    AlertPolicy,
    ApplicationService,
    ApplicationServiceDeployment,
    ApplicationServiceTopologyNode,
    TopologyComponent,
)
from api.services.alert_policy_evaluator import evaluate_policies_for_cluster
from api.services.topology_component_service import refresh_stale_component_healths
from tests.conftest import auth_headers

MOCK_HEALTH_PATH = "api.services.application_service_service._MOCK_DEPLOYMENT_HEALTH"


def _service_policy_payload(**overrides):
    payload = {
        "name": "Service Down",
        "alertType": "service",
        "severity": "critical",
        "serviceConfig": {"serviceId": "*", "triggerOn": "critical"},
        "showOnDashboard": True,
    }
    payload.update(overrides)
    return payload


def _make_service(name, deployments=()):
    svc = ApplicationService(name=name)
    db.session.add(svc)
    db.session.flush()
    for cluster_id, namespace, dep_name in deployments:
        db.session.add(
            ApplicationServiceDeployment(
                service_id=svc.id,
                cluster_id=cluster_id,
                namespace=namespace,
                deployment_name=dep_name,
            )
        )
    db.session.commit()
    return svc


def _reevaluate(policy_id):
    policy = AlertPolicy.query.get(policy_id)
    policy.last_evaluated_at = None
    db.session.commit()
    evaluate_policies_for_cluster(SERVICE_ALERT_CLUSTER_ID, persist=True)


def test_create_service_policy_sets_sentinel_cluster(client, admin_token):
    response = client.post(
        "/api/alert-policies",
        headers=auth_headers(admin_token),
        json=_service_policy_payload(),
    )
    assert response.status_code in (200, 201)
    created = response.get_json()["data"]
    assert created["alertType"] == "service"
    assert created["clusterId"] == SERVICE_ALERT_CLUSTER_ID
    assert created["serviceConfig"] == {"serviceId": "*", "triggerOn": "critical"}
    assert created["serviceName"] == "All services"


def test_service_policy_rejects_invalid_config(client, admin_token):
    bad_service = client.post(
        "/api/alert-policies",
        headers=auth_headers(admin_token),
        json=_service_policy_payload(serviceConfig={"serviceId": "abc"}),
    )
    assert bad_service.status_code == 400

    bad_trigger = client.post(
        "/api/alert-policies",
        headers=auth_headers(admin_token),
        json=_service_policy_payload(serviceConfig={"serviceId": "*", "triggerOn": "bogus"}),
    )
    assert bad_trigger.status_code == 400


def test_viewer_cannot_create_service_policy(client, viewer_token):
    response = client.post(
        "/api/alert-policies",
        headers=auth_headers(viewer_token),
        json=_service_policy_payload(),
    )
    assert response.status_code == 403


def test_catalog_lists_service_alert_options(client, admin_token, app):
    _make_service("Checkout", [("cluster-x", "ns1", "api")])
    response = client.get("/api/alert-policies/catalog", headers=auth_headers(admin_token))
    assert response.status_code == 200
    data = response.get_json()["data"]
    assert "service" in data["alertTypes"]
    assert any(level["key"] == "critical" for level in data["serviceAlertTriggerLevels"])
    assert any(svc["name"] == "Checkout" for svc in data["applicationServices"])


def test_service_policy_fires_and_resolves_for_down_workload(client, admin_token, app):
    svc = _make_service("Checkout", [("cluster-x", "ns1", "api")])

    down = {("cluster-x", "ns1", "api"): {"desired": 2, "available": 0, "ready": 0}}
    with patch.dict(MOCK_HEALTH_PATH, down):
        response = client.post(
            "/api/alert-policies",
            headers=auth_headers(admin_token),
            json=_service_policy_payload(
                serviceConfig={"serviceId": svc.id, "triggerOn": "critical"}
            ),
        )
        assert response.status_code in (200, 201)
        created = response.get_json()["data"]
        assert created["serviceName"] == "Checkout"
        assert created["lastResult"] == "met"

        row = AlertHistory.query.filter_by(policy_id=created["id"], status="active").one()
        assert row.alert_type == "service"
        assert row.cluster_id == "cluster-x"
        assert row.namespace == "ns1"
        assert row.resource_name == "api"
        assert "down" in (row.description or "")
        assert row.metric_snapshot["serviceName"] == "Checkout"

    # Workload recovered (mock health falls back to healthy defaults).
    _reevaluate(created["id"])
    row = AlertHistory.query.filter_by(policy_id=created["id"]).one()
    assert row.status == "resolved"
    assert row.resolved_at is not None

    policy = AlertPolicy.query.get(created["id"])
    assert policy.last_evaluation_result == "not_met"


def test_degraded_trigger_fires_on_partially_available_workload(client, admin_token, app):
    svc = _make_service("Billing", [("cluster-x", "ns1", "worker")])

    degraded = {("cluster-x", "ns1", "worker"): {"desired": 2, "available": 1, "ready": 1}}
    with patch.dict(MOCK_HEALTH_PATH, degraded):
        critical_only = client.post(
            "/api/alert-policies",
            headers=auth_headers(admin_token),
            json=_service_policy_payload(
                name="Critical only",
                serviceConfig={"serviceId": svc.id, "triggerOn": "critical"},
            ),
        )
        assert critical_only.status_code in (200, 201)
        critical_id = critical_only.get_json()["data"]["id"]
        assert AlertHistory.query.filter_by(policy_id=critical_id, status="active").count() == 0

        on_degraded = client.post(
            "/api/alert-policies",
            headers=auth_headers(admin_token),
            json=_service_policy_payload(
                name="Degraded or down",
                serviceConfig={"serviceId": svc.id, "triggerOn": "degraded"},
            ),
        )
        assert on_degraded.status_code in (200, 201)
        degraded_id = on_degraded.get_json()["data"]["id"]
        row = AlertHistory.query.filter_by(policy_id=degraded_id, status="active").one()
        assert "degraded" in (row.description or "")


def test_service_policy_fires_and_resolves_for_unhealthy_component(client, admin_token, app):
    svc = _make_service("Edge", [])
    component = TopologyComponent(
        name="WAF",
        check_type="none",
        last_status="unhealthy",
        last_message="HTTP 503",
    )
    db.session.add(component)
    db.session.flush()
    db.session.add(
        ApplicationServiceTopologyNode(service_id=svc.id, name="WAF", component_id=component.id)
    )
    db.session.commit()

    response = client.post(
        "/api/alert-policies",
        headers=auth_headers(admin_token),
        json=_service_policy_payload(
            serviceConfig={"serviceId": svc.id, "triggerOn": "critical"}
        ),
    )
    assert response.status_code in (200, 201)
    policy_id = response.get_json()["data"]["id"]

    row = AlertHistory.query.filter_by(policy_id=policy_id, status="active").one()
    assert row.resource_type == "component"
    assert row.resource_name == "WAF"
    assert row.cluster_id == SERVICE_ALERT_CLUSTER_ID
    assert "unhealthy" in (row.description or "")

    component.last_status = "healthy"
    db.session.commit()
    _reevaluate(policy_id)
    row = AlertHistory.query.filter_by(policy_id=policy_id).one()
    assert row.status == "resolved"


def test_unknown_component_status_never_fires(client, admin_token, app):
    svc = _make_service("Quiet", [])
    component = TopologyComponent(name="Router", check_type="none", last_status=None)
    db.session.add(component)
    db.session.flush()
    db.session.add(
        ApplicationServiceTopologyNode(service_id=svc.id, name="Router", component_id=component.id)
    )
    db.session.commit()

    response = client.post(
        "/api/alert-policies",
        headers=auth_headers(admin_token),
        json=_service_policy_payload(
            serviceConfig={"serviceId": svc.id, "triggerOn": "degraded"}
        ),
    )
    assert response.status_code in (200, 201)
    policy_id = response.get_json()["data"]["id"]
    assert AlertHistory.query.filter_by(policy_id=policy_id, status="active").count() == 0


def test_switching_service_policy_to_metric_requires_cluster(client, admin_token, app):
    create = client.post(
        "/api/alert-policies",
        headers=auth_headers(admin_token),
        json=_service_policy_payload(),
    )
    assert create.status_code in (200, 201)
    policy_id = create.get_json()["data"]["id"]

    missing_cluster = client.put(
        f"/api/alert-policies/{policy_id}",
        headers=auth_headers(admin_token),
        json={
            "alertType": "metric",
            "conditions": [{"metricKey": "cpu_usage_percent", "operator": ">", "threshold": 70}],
            "scope": {"type": "deployment", "namespace": "default", "resourceName": "*"},
        },
    )
    assert missing_cluster.status_code == 400

    with_cluster = client.put(
        f"/api/alert-policies/{policy_id}",
        headers=auth_headers(admin_token),
        json={
            "alertType": "metric",
            "clusterId": "prod-us-east",
            "conditions": [{"metricKey": "cpu_usage_percent", "operator": ">", "threshold": 70}],
            "scope": {"type": "deployment", "namespace": "default", "resourceName": "*"},
        },
    )
    assert with_cluster.status_code == 200
    updated = with_cluster.get_json()["data"]
    assert updated["clusterId"] == "prod-us-east"
    assert updated["alertType"] == "metric"


def test_refresh_stale_component_healths_checks_webhook_components(app):
    now = datetime.now(timezone.utc)
    fresh_heartbeat = TopologyComponent(
        name="Heartbeat OK",
        check_type="webhook",
        heartbeat_interval_seconds=300,
        last_heartbeat_at=now,
    )
    silent_heartbeat = TopologyComponent(
        name="Heartbeat Lost",
        check_type="webhook",
        heartbeat_interval_seconds=60,
        last_heartbeat_at=now - timedelta(seconds=600),
    )
    unchecked = TopologyComponent(name="No Check", check_type="none")
    db.session.add_all([fresh_heartbeat, silent_heartbeat, unchecked])
    db.session.commit()

    checked = refresh_stale_component_healths()
    assert checked == 2
    assert fresh_heartbeat.last_status == "healthy"
    assert silent_heartbeat.last_status == "unhealthy"
    assert fresh_heartbeat.last_checked_at is not None
    assert unchecked.last_checked_at is None

    # A second pass within the freshness window re-checks nothing.
    assert refresh_stale_component_healths() == 0
    # Unless staleness is forced to zero.
    assert refresh_stale_component_healths(older_than_seconds=0) == 2
