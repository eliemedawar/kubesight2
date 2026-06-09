"""Verify admin, operator, and viewer roles against key API permissions."""

from tests.conftest import auth_headers


def _login(client, username, password):
    response = client.post("/api/auth/login", json={"username": username, "password": password})
    assert response.status_code == 200
    return response.get_json()["data"]["token"]


def _me(client, token):
    response = client.get("/api/auth/me", headers=auth_headers(token))
    assert response.status_code == 200
    return response.get_json()["data"]


def test_admin_has_full_permissions(client, admin_token):
    profile = _me(client, admin_token)
    assert profile["isAdmin"] is True
    assert profile["role"] == "admin"
    assert "users:view" in profile["permissions"]
    assert "settings:manage" in profile["permissions"]
    assert "alerts:manage" in profile["permissions"]


def test_viewer_permissions_and_denials(client, viewer_token):
    profile = _me(client, viewer_token)
    assert profile["role"] == "viewer"
    assert "alerts:view" in profile["permissions"]
    assert "upgrades:precheck" not in profile["permissions"]
    assert "users:view" not in profile["permissions"]
    assert "settings:view" not in profile["permissions"]

    assert client.get("/api/users", headers=auth_headers(viewer_token)).status_code == 403
    assert client.get("/api/settings", headers=auth_headers(viewer_token)).status_code == 403
    assert (
        client.post(
            "/api/alerts/notifications/email/test",
            headers=auth_headers(viewer_token),
        ).status_code
        == 403
    )


def test_operator_permissions_and_denials(client, operator_token):
    profile = _me(client, operator_token)
    assert profile["role"] == "operator"
    assert "alerts:view" in profile["permissions"]
    assert "alerts:manage" in profile["permissions"]
    assert "upgrades:precheck" in profile["permissions"]
    assert "upgrades:start" not in profile["permissions"]
    assert "users:view" not in profile["permissions"]
    assert "settings:view" not in profile["permissions"]

    assert client.get("/api/clusters", headers=auth_headers(operator_token)).status_code == 200
    assert client.get("/api/users", headers=auth_headers(operator_token)).status_code == 403
    assert client.get("/api/settings", headers=auth_headers(operator_token)).status_code == 403


def test_viewer_can_view_alerts_and_clusters(client, viewer_token):
    clusters = client.get("/api/clusters", headers=auth_headers(viewer_token))
    assert clusters.status_code == 200
    assert clusters.get_json()["data"]["items"]

    alerts = client.get("/api/alerts", headers=auth_headers(viewer_token))
    assert alerts.status_code == 200


def test_operator_can_run_upgrade_precheck(client, operator_token):
    response = client.post(
        "/api/upgrades/precheck",
        headers=auth_headers(operator_token),
        json={"clusterId": "prod-us-east", "targetVersion": "v1.31.0"},
    )
    assert response.status_code == 200


def test_viewer_cannot_run_upgrade_precheck(client, viewer_token):
    response = client.post(
        "/api/upgrades/precheck",
        headers=auth_headers(viewer_token),
        json={"clusterId": "prod-us-east", "targetVersion": "v1.31.0"},
    )
    assert response.status_code == 403
