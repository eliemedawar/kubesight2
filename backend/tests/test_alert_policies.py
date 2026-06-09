from datetime import datetime, timezone
from unittest.mock import patch

from api.db import db
from api.models import AlertPolicy
from api.services.alert_policy_evaluator import evaluate_policies_for_cluster
from tests.conftest import auth_headers

SAMPLE_POLICY = {
    "name": "High CPU",
    "clusterId": "prod-us-east",
    "description": "CPU threshold alert",
    "enabled": True,
    "severity": "warning",
    "conditionLogic": "any",
    "conditions": [{"metricKey": "cpu_usage_percent", "operator": ">", "threshold": 70}],
    "scope": {"type": "deployment", "namespace": "default", "resourceName": "*"},
    "showOnDashboard": True,
}


def test_policy_strips_legacy_outbound_notification_channels(client, admin_token):
    create = client.post(
        "/api/alert-policies",
        headers=auth_headers(admin_token),
        json={
            **SAMPLE_POLICY,
            "name": "Legacy Channels",
            "notificationChannels": [
                {"channel": "dashboard"},
                {"channel": "email"},
                {"channel": "slack"},
            ],
        },
    )
    assert create.status_code in (200, 201)
    created = create.get_json()["data"]
    assert created["showOnDashboard"] is True
    assert "notificationChannels" not in created

    update = client.put(
        f"/api/alert-policies/{created['id']}",
        headers=auth_headers(admin_token),
        json={"showOnDashboard": False},
    )
    assert update.status_code == 200
    assert update.get_json()["data"]["showOnDashboard"] is False

    client.delete(f"/api/alert-policies/{created['id']}", headers=auth_headers(admin_token))


def test_viewer_can_list_alert_policy_catalog(client, viewer_token):
    response = client.get("/api/alert-policies/catalog", headers=auth_headers(viewer_token))
    assert response.status_code == 200
    payload = response.get_json()
    assert payload["success"] is True
    assert "metrics" in payload["data"]
    assert any(metric["key"] == "cpu_usage_percent" for metric in payload["data"]["metrics"])


def test_viewer_cannot_create_alert_policy(client, viewer_token):
    response = client.post(
        "/api/alert-policies",
        headers=auth_headers(viewer_token),
        json=SAMPLE_POLICY,
    )
    assert response.status_code == 403


def test_admin_crud_alert_policy(client, admin_token):
    create = client.post(
        "/api/alert-policies",
        headers=auth_headers(admin_token),
        json=SAMPLE_POLICY,
    )
    assert create.status_code in (200, 201)
    created = create.get_json()["data"]
    policy_id = created["id"]
    assert created["name"] == "High CPU"
    assert created["clusterId"] == "prod-us-east"

    listing = client.get("/api/alert-policies", headers=auth_headers(admin_token))
    assert listing.status_code == 200
    items = listing.get_json()["data"]["items"]
    assert any(item["id"] == policy_id for item in items)

    update = client.put(
        f"/api/alert-policies/{policy_id}",
        headers=auth_headers(admin_token),
        json={**SAMPLE_POLICY, "name": "High CPU Updated", "severity": "critical"},
    )
    assert update.status_code == 200
    assert update.get_json()["data"]["name"] == "High CPU Updated"
    assert update.get_json()["data"]["severity"] == "critical"

    disable = client.patch(
        f"/api/alert-policies/{policy_id}/status",
        headers=auth_headers(admin_token),
        json={"enabled": False},
    )
    assert disable.status_code == 200
    assert disable.get_json()["data"]["enabled"] is False

    delete = client.delete(
        f"/api/alert-policies/{policy_id}",
        headers=auth_headers(admin_token),
    )
    assert delete.status_code == 200


def test_alert_policy_scope_validation(client, admin_token):
    missing_namespace = client.post(
        "/api/alert-policies",
        headers=auth_headers(admin_token),
        json={
            **SAMPLE_POLICY,
            "name": "Missing Namespace",
            "scope": {"type": "deployment", "namespace": "", "resourceName": "*"},
        },
    )
    assert missing_namespace.status_code == 400

    legacy_cluster = client.post(
        "/api/alert-policies",
        headers=auth_headers(admin_token),
        json={
            **SAMPLE_POLICY,
            "name": "Legacy Cluster Scope",
            "scope": {"type": "cluster"},
        },
    )
    assert legacy_cluster.status_code in (200, 201)
    created = legacy_cluster.get_json()["data"]
    assert created["scope"]["type"] == "deployment"
    assert created["scope"]["namespace"] == "default"
    assert created["scope"]["resourceName"] == "*"

    client.delete(f"/api/alert-policies/{created['id']}", headers=auth_headers(admin_token))


def test_alert_policy_validation_rejects_empty_conditions(client, admin_token):
    response = client.post(
        "/api/alert-policies",
        headers=auth_headers(admin_token),
        json={**SAMPLE_POLICY, "conditions": []},
    )
    assert response.status_code == 400


def test_evaluate_policy_creates_history_and_merges_into_alerts(client, admin_token):
    create = client.post(
        "/api/alert-policies",
        headers=auth_headers(admin_token),
        json={**SAMPLE_POLICY, "name": "Eval Policy", "conditions": [{"metricKey": "cpu_usage_percent", "operator": ">", "threshold": 1}]},
    )
    assert create.status_code in (200, 201)
    policy_id = create.get_json()["data"]["id"]

    evaluate = client.post(
        "/api/alert-policies/evaluate",
        headers=auth_headers(admin_token),
        json={"clusterId": "prod-us-east"},
    )
    assert evaluate.status_code == 200
    evaluated = evaluate.get_json()["data"]["items"]
    assert isinstance(evaluated, list)

    history = client.get(
        "/api/alert-policies/history?cluster=prod-us-east",
        headers=auth_headers(admin_token),
    )
    assert history.status_code == 200
    history_items = history.get_json()["data"]["items"]
    assert any(item.get("policyId") == policy_id for item in history_items)

    alerts = client.get(
        "/api/alerts?cluster=prod-us-east",
        headers=auth_headers(admin_token),
    )
    assert alerts.status_code == 200
    alert_items = alerts.get_json()["data"]["items"]
    assert any(item.get("source") == "alert_policy" for item in alert_items)

    stats = client.get(
        "/api/alert-policies/stats?cluster=prod-us-east",
        headers=auth_headers(admin_token),
    )
    assert stats.status_code == 200
    stats_data = stats.get_json()["data"]
    assert "activeTotal" in stats_data
    assert "topTriggeredPolicies" in stats_data

    listing = client.get("/api/alert-policies", headers=auth_headers(admin_token))
    evaluated_policy = next(item for item in listing.get_json()["data"]["items"] if item["id"] == policy_id)
    assert evaluated_policy["lastEvaluatedAt"] is not None
    assert evaluated_policy["lastResult"] == "met"
    assert evaluated_policy["lastMeasuredValue"] is not None
    assert evaluated_policy["lastThreshold"] is not None

    client.delete(f"/api/alert-policies/{policy_id}", headers=auth_headers(admin_token))


def test_deployment_scope_cpu_evaluation_records_debug_fields(client, admin_token):
    create = client.post(
        "/api/alert-policies",
        headers=auth_headers(admin_token),
        json={
            **SAMPLE_POLICY,
            "name": "Deployment CPU",
            "clusterId": "docker-desktop",
            "scope": {"type": "deployment", "namespace": "kubesight", "resourceName": "kubesight-backend"},
            "conditions": [{"metricKey": "cpu_usage_percent", "operator": "<", "threshold": 100}],
        },
    )
    assert create.status_code in (200, 201)
    created = create.get_json()["data"]
    assert created["lastEvaluatedAt"] is not None
    assert created["lastResult"] == "met"
    assert created["lastMeasuredValue"] is not None
    assert "CPU" in (created["lastMeasuredValue"] or "")

    client.delete(f"/api/alert-policies/{created['id']}", headers=auth_headers(admin_token))


def test_policy_save_runs_immediate_evaluation(client, admin_token):
    create = client.post(
        "/api/alert-policies",
        headers=auth_headers(admin_token),
        json={
            **SAMPLE_POLICY,
            "name": "Immediate Eval",
            "conditions": [{"metricKey": "cpu_usage_percent", "operator": "<", "threshold": 100}],
            "scope": {"type": "pod", "namespace": "default", "resourceName": "payments-api-84b5d5"},
        },
    )
    assert create.status_code in (200, 201)
    created = create.get_json()["data"]
    assert created["lastEvaluatedAt"] is not None
    assert created["lastResult"] in {"met", "not_met", "error"}
    assert created["lastMeasuredValue"] is not None

    update = client.put(
        f"/api/alert-policies/{created['id']}",
        headers=auth_headers(admin_token),
        json={
            "conditions": [{"metricKey": "memory_usage_percent", "operator": "<", "threshold": 100}],
        },
    )
    assert update.status_code == 200
    updated = update.get_json()["data"]
    assert updated["lastEvaluatedAt"] is not None
    assert "Memory" in (updated["lastMeasuredValue"] or "")

    client.delete(f"/api/alert-policies/{created['id']}", headers=auth_headers(admin_token))


def test_pod_scope_memory_evaluation_records_debug_fields(client, admin_token):
    create = client.post(
        "/api/alert-policies",
        headers=auth_headers(admin_token),
        json={
            **SAMPLE_POLICY,
            "name": "Pod Memory",
            "scope": {"type": "pod", "namespace": "default", "resourceName": "payments-api-84b5d5"},
            "conditions": [{"metricKey": "memory_usage_percent", "operator": ">", "threshold": 1}],
        },
    )
    assert create.status_code in (200, 201)
    policy_id = create.get_json()["data"]["id"]

    evaluate = client.post(
        "/api/alert-policies/evaluate",
        headers=auth_headers(admin_token),
        json={"clusterId": "prod-us-east"},
    )
    assert evaluate.status_code == 200

    listing = client.get("/api/alert-policies", headers=auth_headers(admin_token))
    evaluated_policy = next(item for item in listing.get_json()["data"]["items"] if item["id"] == policy_id)
    assert evaluated_policy["lastEvaluatedAt"] is not None
    assert evaluated_policy["lastResult"] == "met"
    assert evaluated_policy["lastMeasuredValue"] is not None

    client.delete(f"/api/alert-policies/{policy_id}", headers=auth_headers(admin_token))


def test_policy_evaluation_debug_fields_not_met(client, admin_token):
    create = client.post(
        "/api/alert-policies",
        headers=auth_headers(admin_token),
        json={
            **SAMPLE_POLICY,
            "name": "Never Fires",
            "conditions": [{"metricKey": "cpu_usage_percent", "operator": ">", "threshold": 999}],
        },
    )
    assert create.status_code in (200, 201)
    policy_id = create.get_json()["data"]["id"]

    evaluate = client.post(
        "/api/alert-policies/evaluate",
        headers=auth_headers(admin_token),
        json={"clusterId": "prod-us-east"},
    )
    assert evaluate.status_code == 200

    listing = client.get("/api/alert-policies", headers=auth_headers(admin_token))
    evaluated_policy = next(item for item in listing.get_json()["data"]["items"] if item["id"] == policy_id)
    assert evaluated_policy["lastEvaluatedAt"] is not None
    assert evaluated_policy["lastResult"] == "not_met"
    assert "CPU" in (evaluated_policy["lastMeasuredValue"] or "")
    assert evaluated_policy["lastThreshold"] == "> 999"

    client.delete(f"/api/alert-policies/{policy_id}", headers=auth_headers(admin_token))


def test_active_alert_dispatches_after_receiver_added(client, admin_token):
    create = client.post(
        "/api/alert-policies",
        headers=auth_headers(admin_token),
        json={
            **SAMPLE_POLICY,
            "name": "Late Receiver",
            "conditions": [{"metricKey": "cpu_usage_percent", "operator": "<", "threshold": 100}],
        },
    )
    assert create.status_code in (200, 201)
    policy_id = create.get_json()["data"]["id"]

    email_resp = client.post(
        "/api/alert-routing/receivers",
        headers=auth_headers(admin_token),
        json={
            "name": "Late Email",
            "type": "email",
            "emailAddress": "late@company.com",
            "enabled": True,
        },
    )
    assert email_resp.status_code == 201
    email_id = email_resp.get_json()["data"]["id"]

    with patch("api.services.alert_routing_service.smtp_is_configured", return_value=True), patch(
        "api.services.alert_routing_service.send_alert_email"
    ) as send_email:
        update = client.put(
            f"/api/alert-policies/{policy_id}",
            headers=auth_headers(admin_token),
            json={"receiverIds": [email_id]},
        )
        assert update.status_code == 200
        assert send_email.call_count >= 1

    client.delete(f"/api/alert-routing/receivers/{email_id}", headers=auth_headers(admin_token))
    client.delete(f"/api/alert-policies/{policy_id}", headers=auth_headers(admin_token))


def test_policy_receiver_assignment_and_dispatch(client, admin_token):
    email_resp = client.post(
        "/api/alert-routing/receivers",
        headers=auth_headers(admin_token),
        json={
            "name": "DevOps Team Email",
            "type": "email",
            "emailAddress": "devops@company.com",
            "enabled": True,
        },
    )
    assert email_resp.status_code == 201
    email_id = email_resp.get_json()["data"]["id"]

    slack_resp = client.post(
        "/api/alert-routing/receivers",
        headers=auth_headers(admin_token),
        json={
            "name": "Production Slack",
            "type": "slack",
            "url": "https://hooks.slack.com/services/T00/B00/XXX",
            "enabled": True,
        },
    )
    assert slack_resp.status_code == 201
    slack_id = slack_resp.get_json()["data"]["id"]

    create = client.post(
        "/api/alert-policies",
        headers=auth_headers(admin_token),
        json={
            **SAMPLE_POLICY,
            "name": "High CPU",
            "receiverIds": [email_id, slack_id],
        },
    )
    assert create.status_code in (200, 201)
    policy = create.get_json()["data"]
    assert set(policy["receiverIds"]) == {email_id, slack_id}
    assert "DevOps Team Email" in policy["receiverNames"]
    assert "Production Slack" in policy["receiverNames"]

    catalog = client.get("/api/alert-policies/catalog", headers=auth_headers(admin_token))
    assert catalog.status_code == 200
    catalog_receivers = catalog.get_json()["data"]["receivers"]
    assert any(item["id"] == email_id for item in catalog_receivers)

    receivers = client.get("/api/alert-routing/receivers", headers=auth_headers(admin_token))
    receiver_items = receivers.get_json()["data"]["items"]
    email_row = next(item for item in receiver_items if item["id"] == email_id)
    assert "High CPU" in email_row["assignedPolicyNames"]

    with patch("api.services.alert_routing_service.smtp_is_configured", return_value=True), patch(
        "api.services.alert_routing_service.send_alert_email"
    ) as send_email, patch("api.services.alert_routing_service._post_webhook") as post_webhook:
        from api.services.alert_routing_service import dispatch_policy_alert_notifications

        alert = {
            "id": "history-99",
            "policyId": policy["id"],
            "severity": "warning",
            "status": "firing",
            "clusterId": "prod-us-east",
            "namespace": "default",
            "title": "High CPU",
        }
        summary = dispatch_policy_alert_notifications(alert)
        assert summary["sent"] == 2
        send_email.assert_called_once()
        assert post_webhook.call_count == 1

    client.delete(f"/api/alert-policies/{policy['id']}", headers=auth_headers(admin_token))
    client.delete(f"/api/alert-routing/receivers/{email_id}", headers=auth_headers(admin_token))
    client.delete(f"/api/alert-routing/receivers/{slack_id}", headers=auth_headers(admin_token))


def test_policy_evaluation_interval_defaults_and_validation(client, admin_token):
    create = client.post(
        "/api/alert-policies",
        headers=auth_headers(admin_token),
        json=SAMPLE_POLICY,
    )
    assert create.status_code in (200, 201)
    created = create.get_json()["data"]
    assert created["evaluationIntervalSeconds"] == 300
    assert created["evaluationIntervalLabel"] == "Every 5 min"

    catalog = client.get("/api/alert-policies/catalog", headers=auth_headers(admin_token))
    assert catalog.status_code == 200
    assert len(catalog.get_json()["data"]["evaluationIntervals"]) == 6

    bad = client.post(
        "/api/alert-policies",
        headers=auth_headers(admin_token),
        json={**SAMPLE_POLICY, "name": "Bad Interval", "evaluationIntervalSeconds": 120},
    )
    assert bad.status_code == 400

    update = client.put(
        f"/api/alert-policies/{created['id']}",
        headers=auth_headers(admin_token),
        json={"evaluationIntervalSeconds": 3600},
    )
    assert update.status_code == 200
    assert update.get_json()["data"]["evaluationIntervalSeconds"] == 3600
    assert update.get_json()["data"]["evaluationIntervalLabel"] == "Every 1 hour"

    client.delete(f"/api/alert-policies/{created['id']}", headers=auth_headers(admin_token))


def test_scheduler_evaluates_due_policies_without_user(client, admin_token):
    create = client.post(
        "/api/alert-policies",
        headers=auth_headers(admin_token),
        json={
            **SAMPLE_POLICY,
            "name": "Scheduler Policy",
            "conditions": [{"metricKey": "cpu_usage_percent", "operator": "<", "threshold": 100}],
            "evaluationIntervalSeconds": 60,
        },
    )
    assert create.status_code in (200, 201)
    policy_id = create.get_json()["data"]["id"]

    from datetime import datetime, timedelta, timezone

    from api.models import AlertPolicy
    from api.services.alert_policy_evaluator import evaluate_all_enabled_policies

    policy = AlertPolicy.query.get(policy_id)
    policy.last_evaluated_at = datetime.now(timezone.utc) - timedelta(seconds=61)
    db.session.commit()

    evaluate_all_enabled_policies(persist=True)

    policy = AlertPolicy.query.get(policy_id)
    assert policy.last_evaluated_at is not None
    assert policy.last_evaluation_result == "met"

    client.delete(f"/api/alert-policies/{policy_id}", headers=auth_headers(admin_token))


def test_policy_evaluation_skips_until_interval_elapsed(client, admin_token):
    create = client.post(
        "/api/alert-policies",
        headers=auth_headers(admin_token),
        json={
            **SAMPLE_POLICY,
            "name": "Interval Policy",
            "conditions": [{"metricKey": "cpu_usage_percent", "operator": ">", "threshold": 1}],
            "evaluationIntervalSeconds": 3600,
        },
    )
    assert create.status_code in (200, 201)
    created = create.get_json()["data"]
    policy_id = created["id"]
    assert created["lastEvaluatedAt"] is not None

    first = evaluate_policies_for_cluster("prod-us-east", persist=True)
    assert first == []

    policy = AlertPolicy.query.get(policy_id)
    assert policy.last_evaluated_at is not None
    policy.last_evaluated_at = datetime.now(timezone.utc)
    db.session.commit()

    second = evaluate_policies_for_cluster("prod-us-east", persist=True)
    assert second == []

    client.delete(f"/api/alert-policies/{policy_id}", headers=auth_headers(admin_token))


def test_dashboard_includes_alert_policy_stats(client, admin_token):
    response = client.get(
        "/api/dashboard/summary?clusterId=prod-us-east",
        headers=auth_headers(admin_token),
    )
    assert response.status_code == 200
    data = response.get_json()["data"]
    assert "alertPolicies" in data
    assert "activeTotal" in data["alertPolicies"]
