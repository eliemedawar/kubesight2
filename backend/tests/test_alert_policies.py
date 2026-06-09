from tests.conftest import auth_headers

SAMPLE_POLICY = {
    "name": "High CPU",
    "clusterId": "prod-us-east",
    "description": "CPU threshold alert",
    "enabled": True,
    "severity": "warning",
    "conditionLogic": "any",
    "conditions": [{"metricKey": "cpu_usage_percent", "operator": ">", "threshold": 70}],
    "scope": {"type": "cluster"},
    "notificationChannels": [{"channel": "dashboard"}],
}


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
