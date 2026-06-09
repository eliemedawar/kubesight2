"""Tests for log-based alert policies."""

from unittest.mock import patch

from api.db import db
from api.k8s_logs import find_log_matches
from api.models import AlertHistory, LogAlertSeen
from api.services.alert_policy_evaluator import evaluate_policies_for_cluster, list_active_policy_alerts
from tests.conftest import auth_headers

LOG_POLICY = {
    "name": "Backend Error Logs",
    "clusterId": "prod-us-east",
    "description": "Detect ERROR lines in pod logs",
    "enabled": True,
    "alertType": "log",
    "severity": "critical",
    "logConfig": {
        "matchType": "contains",
        "pattern": "ERROR",
        "caseSensitive": False,
        "logWindowSeconds": 60,
        "contextLinesBefore": 5,
        "contextLinesAfter": 5,
        "maxLines": 20,
    },
    "scope": {"type": "deployment", "namespace": "default", "resourceName": "kubesight-backend"},
    "showOnDashboard": True,
}


def test_catalog_includes_log_alert_options(client, admin_token):
    response = client.get("/api/alert-policies/catalog", headers=auth_headers(admin_token))
    assert response.status_code == 200
    data = response.get_json()["data"]
    assert "log" in data["alertTypes"]
    assert data["defaultAlertType"] == "metric"
    assert data["logMatchTypes"] == ["contains", "regex"]
    assert len(data["logWindowOptions"]) == 4


def test_create_log_alert_policy(client, admin_token):
    response = client.post(
        "/api/alert-policies",
        headers=auth_headers(admin_token),
        json=LOG_POLICY,
    )
    assert response.status_code in (200, 201)
    created = response.get_json()["data"]
    assert created["alertType"] == "log"
    assert created["logConfig"]["pattern"] == "ERROR"
    assert created["conditions"] == []

    client.delete(f"/api/alert-policies/{created['id']}", headers=auth_headers(admin_token))


def test_log_policy_requires_pattern(client, admin_token):
    response = client.post(
        "/api/alert-policies",
        headers=auth_headers(admin_token),
        json={**LOG_POLICY, "name": "Missing Pattern", "logConfig": {**LOG_POLICY["logConfig"], "pattern": ""}},
    )
    assert response.status_code == 400


def test_find_log_matches_with_comma_separated_contains():
    log_text = "\n".join(
        [
            "2026-06-09T15:12:03Z Connecting to database...",
            "2026-06-09T15:12:04Z connection timeout while dialing upstream",
            "2026-06-09T15:12:05Z Retrying connection...",
        ]
    )
    matches = find_log_matches(
        log_text,
        match_type="contains",
        pattern="ERROR, Exception, failed, timeout",
        case_sensitive=False,
        context_before=0,
        context_after=0,
        max_lines=20,
    )
    assert len(matches) == 1
    assert "timeout" in matches[0]["matchingMessage"].lower()


def test_find_log_matches_with_context():
    log_text = "\n".join(
        [
            "2026-06-09T15:12:03Z Connecting to database...",
            "2026-06-09T15:12:04Z ERROR Database connection failed",
            "psycopg2.OperationalError: could not connect to server",
            "Connection refused",
            "2026-06-09T15:12:05Z Retrying connection...",
        ]
    )
    matches = find_log_matches(
        log_text,
        match_type="contains",
        pattern="ERROR",
        case_sensitive=False,
        context_before=1,
        context_after=2,
        max_lines=20,
    )
    assert len(matches) == 1
    assert "ERROR Database connection failed" in matches[0]["logSnippet"]
    assert len(matches[0]["logLines"]) >= 2


@patch("api.services.alert_routing_service.send_alert_email")
@patch("api.services.alert_routing_service.smtp_is_configured", return_value=True)
def test_log_policy_evaluation_and_dedup(mock_smtp_ready, mock_send_email, client, admin_token):
    receiver = client.post(
        "/api/alert-routing/receivers",
        headers=auth_headers(admin_token),
        json={"name": "Log Email", "type": "email", "emailAddress": "ops@example.com"},
    )
    receiver_id = receiver.get_json()["data"]["id"]

    create = client.post(
        "/api/alert-policies",
        headers=auth_headers(admin_token),
        json={**LOG_POLICY, "receiverIds": [receiver_id]},
    )
    policy_id = create.get_json()["data"]["id"]

    evaluate_policies_for_cluster("prod-us-east", persist=True)
    db.session.commit()

    active = list_active_policy_alerts(cluster_id="prod-us-east")
    log_alerts = [item for item in active if item.get("alertType") == "log"]
    assert len(log_alerts) >= 1
    assert log_alerts[0]["matchedPattern"] == "ERROR"
    assert log_alerts[0]["logSnippet"]
    assert mock_send_email.called

    seen_count = LogAlertSeen.query.filter_by(policy_id=policy_id).count()
    assert seen_count >= 1

    mock_send_email.reset_mock()
    evaluate_policies_for_cluster("prod-us-east", persist=True)
    assert mock_send_email.call_count == 0

    client.delete(f"/api/alert-policies/{policy_id}", headers=auth_headers(admin_token))
    client.delete(f"/api/alert-routing/receivers/{receiver_id}", headers=auth_headers(admin_token))


def test_log_alert_history_payload(client, admin_token):
    create = client.post(
        "/api/alert-policies",
        headers=auth_headers(admin_token),
        json=LOG_POLICY,
    )
    policy_id = create.get_json()["data"]["id"]
    evaluate_policies_for_cluster("prod-us-east", persist=True)

    history = client.get(
        "/api/alert-policies/history?cluster=prod-us-east",
        headers=auth_headers(admin_token),
    )
    items = history.get_json()["data"]["items"]
    log_items = [item for item in items if item.get("alertType") == "log"]
    assert log_items
    assert log_items[0]["logSnippet"]

    client.delete(f"/api/alert-policies/{policy_id}", headers=auth_headers(admin_token))
