from unittest.mock import patch

import pytest

from api.db import db
from api.models import AlertRoutingReceiver, AlertRoutingSmtp
from tests.conftest import auth_headers


def test_viewer_cannot_access_alert_routing(client, viewer_token):
    response = client.get("/api/alert-routing/smtp", headers=auth_headers(viewer_token))
    assert response.status_code == 403


def test_admin_can_read_and_save_smtp(client, admin_token):
    get_resp = client.get("/api/alert-routing/smtp", headers=auth_headers(admin_token))
    assert get_resp.status_code == 200
    payload = get_resp.get_json()
    assert payload["success"] is True
    assert "passwordConfigured" in payload["data"]
    assert "password" not in payload["data"]

    save_resp = client.post(
        "/api/alert-routing/smtp",
        headers=auth_headers(admin_token),
        json={
            "host": "smtp.test.local",
            "port": 587,
            "username": "alerts",
            "password": "secret-pass",
            "fromEmail": "alerts@kubesight.test",
            "fromName": "KubeSight",
            "useTls": True,
            "useSsl": False,
        },
    )
    assert save_resp.status_code == 200
    saved = save_resp.get_json()["data"]
    assert saved["host"] == "smtp.test.local"
    assert saved["passwordConfigured"] is True
    assert saved["configured"] is True

    row = AlertRoutingSmtp.query.first()
    assert row.password_encrypted
    assert row.password_encrypted != "secret-pass"


def test_receiver_crud_and_policy_dispatch(client, admin_token):
    email_resp = client.post(
        "/api/alert-routing/receivers",
        headers=auth_headers(admin_token),
        json={
            "name": "Admin Team",
            "type": "email",
            "emailAddress": "admin@company.com",
            "enabled": True,
        },
    )
    assert email_resp.status_code == 201
    email_id = email_resp.get_json()["data"]["id"]

    webhook_resp = client.post(
        "/api/alert-routing/receivers",
        headers=auth_headers(admin_token),
        json={
            "name": "Slack Prod",
            "type": "slack",
            "url": "https://hooks.slack.com/services/T00/B00/XXX",
            "enabled": True,
        },
    )
    assert webhook_resp.status_code == 201
    webhook_id = webhook_resp.get_json()["data"]["id"]

    list_resp = client.get("/api/alert-routing/receivers", headers=auth_headers(admin_token))
    assert len(list_resp.get_json()["data"]["items"]) == 2
    assert "assignedPolicyNames" in list_resp.get_json()["data"]["items"][0]

    policy_resp = client.post(
        "/api/alert-policies",
        headers=auth_headers(admin_token),
        json={
            "name": "High CPU",
            "clusterId": "prod-us-east",
            "severity": "critical",
            "conditionLogic": "any",
            "conditions": [{"metricKey": "cpu_usage_percent", "operator": ">", "threshold": 70}],
            "scope": {"type": "cluster"},
            "showOnDashboard": True,
            "receiverIds": [email_id, webhook_id],
        },
    )
    assert policy_resp.status_code in (200, 201)
    policy_id = policy_resp.get_json()["data"]["id"]

    with patch("api.services.alert_routing_service.smtp_is_configured", return_value=True), patch(
        "api.services.alert_routing_service.send_alert_email"
    ) as send_email, patch("api.services.alert_routing_service._post_webhook") as post_webhook:
        from api.services.alert_routing_service import dispatch_policy_alert_notifications

        alert = {
            "id": "history-1",
            "policyId": policy_id,
            "policyName": "High CPU",
            "severity": "critical",
            "status": "firing",
            "clusterId": "prod-us-east",
            "namespace": "production",
            "resourceType": "pod",
            "title": "High CPU",
        }
        summary = dispatch_policy_alert_notifications(alert)
        assert summary["sent"] == 2
        send_email.assert_called_once()
        assert post_webhook.call_count == 1

        summary_repeat = dispatch_policy_alert_notifications(alert)
        assert summary_repeat["skipped"] == 2
        assert summary_repeat["sent"] == 0

    logs_resp = client.get("/api/alert-routing/delivery-logs", headers=auth_headers(admin_token))
    logs = logs_resp.get_json()["data"]["items"]
    assert any(log["policyName"] == "High CPU" for log in logs)
    assert any(log["alertName"] == "High CPU" for log in logs)

    client.delete(f"/api/alert-policies/{policy_id}", headers=auth_headers(admin_token))
    client.delete(f"/api/alert-routing/receivers/{email_id}", headers=auth_headers(admin_token))
    client.delete(f"/api/alert-routing/receivers/{webhook_id}", headers=auth_headers(admin_token))


def test_webhook_receiver_validation(client, admin_token):
    bad_resp = client.post(
        "/api/alert-routing/receivers",
        headers=auth_headers(admin_token),
        json={
            "name": "Bad Slack",
            "type": "slack",
            "url": "https://example.com/hook",
            "enabled": True,
        },
    )
    assert bad_resp.status_code == 400


def test_delete_receiver(client, admin_token):
    receiver = AlertRoutingReceiver(
        name="Temp",
        receiver_type="email",
        email_address="temp@test.com",
        enabled=True,
    )
    db.session.add(receiver)
    db.session.commit()

    delete_resp = client.delete(
        f"/api/alert-routing/receivers/{receiver.id}",
        headers=auth_headers(admin_token),
    )
    assert delete_resp.status_code == 200
    assert AlertRoutingReceiver.query.get(receiver.id) is None


def test_routing_rules_endpoint_removed(client, admin_token):
    response = client.get("/api/alert-routing/rules", headers=auth_headers(admin_token))
    assert response.status_code == 404


def test_deleted_receiver_not_recreated_on_startup_migration(client, admin_token, app):
    from api.migrate_alert_routing import migrate_settings_routing_to_policy_receivers
    from api.models import AppSettings

    settings = AppSettings.query.first()
    assert settings is not None
    settings.notifications = {
        "alerts": True,
        "upgrades": True,
        "routing": {
            "email": {"enabled": True, "address": "legacy@company.com"},
            "slack": {"enabled": False, "webhookUrl": ""},
            "webhook": {"enabled": False, "url": ""},
        },
        "routingReceiversMigrated": False,
        "routingRulesMigrated": True,
    }
    db.session.commit()

    migrate_settings_routing_to_policy_receivers()
    assert AlertRoutingReceiver.query.filter_by(email_address="legacy@company.com").count() == 1

    receiver = AlertRoutingReceiver.query.filter_by(email_address="legacy@company.com").first()
    db.session.delete(receiver)
    db.session.commit()
    assert AlertRoutingReceiver.query.filter_by(email_address="legacy@company.com").count() == 0

    migrate_settings_routing_to_policy_receivers()
    assert AlertRoutingReceiver.query.filter_by(email_address="legacy@company.com").count() == 0
