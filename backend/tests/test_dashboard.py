from tests.conftest import auth_headers


def test_viewer_dashboard_respects_permissions(client, viewer_token):
    response = client.get(
        "/api/dashboard/summary?clusterId=prod-us-east",
        headers=auth_headers(viewer_token),
    )
    assert response.status_code == 200
    data = response.get_json()["data"]
    assert data["permissions"]["upgrades"] is False
    assert data["permissions"]["audit"] is False
    assert data["permissions"]["alerts"] is True
    assert data["permissions"]["users"] is False
    assert "myAccess" in data
    assert data["myAccess"]["hasAccessibleScope"] is True
    assert len(data["myAccess"]["clusters"]) >= 1
    assert "operationalEvents" in data
    assert "userActivity" in data
    assert data["userActivity"] == []


def test_operator_dashboard_includes_upgrade_data(client, operator_token):
    response = client.get(
        "/api/dashboard/summary?clusterId=prod-us-east",
        headers=auth_headers(operator_token),
    )
    assert response.status_code == 200
    data = response.get_json()["data"]
    assert data["permissions"]["upgrades"] is True
    assert data["upgradeStatus"]["currentVersion"]


def test_dashboard_my_access_includes_permissions(client, viewer_token):
    response = client.get(
        "/api/dashboard/summary?clusterId=prod-us-east",
        headers=auth_headers(viewer_token),
    )
    data = response.get_json()["data"]["myAccess"]
    permission_ids = {item["id"] for item in data["permissions"]}
    assert "view_alerts" in permission_ids
    assert "view_resources" in permission_ids
    assert "manage_users" not in permission_ids
    assert any("(all namespaces)" in line for line in data["clusters"])


def test_accessible_cluster_display_names_includes_current_live_cluster(app):
    with app.app_context():
        from api.models import User
        from api.services.dashboard_service import _build_my_access

        viewer = User.query.filter_by(username="viewer").first()
        access = _build_my_access(
            viewer,
            "docker-desktop",
            cluster={"id": "docker-desktop", "name": "Docker Desktop"},
        )
        assert access["hasAccessibleScope"] is True
        assert access["clusters"]


def test_admin_dashboard_summary_mock_mode(client, admin_token):
    clusters = client.get("/api/clusters", headers=auth_headers(admin_token)).get_json()
    cluster_id = clusters["data"]["items"][0]["id"]
    response = client.get(
        f"/api/dashboard/summary?clusterId={cluster_id}",
        headers=auth_headers(admin_token),
    )
    assert response.status_code == 200
    payload = response.get_json()
    assert payload["success"] is True
    data = payload["data"]
    assert data["clusterId"] == cluster_id
    assert data["health"]["status"] in {"healthy", "warning", "critical"}
    assert "pods" in data
    assert "alerts" in data
    assert "clusterInfo" in data
    assert "namespaces" in data
    assert "upgradeStatus" in data
    assert isinstance(data["namespaces"], list)
    assert "cpuUsage" in data
    assert "memoryUsage" in data
    assert "version" in data
    assert data["cpuUsage"]["available"] is True
    assert data["cpuUsage"]["percent"] is not None
    assert data["version"]["current"]
    assert "status" in data["version"]


def test_dashboard_mock_utilization_from_overview(client, admin_token):
    response = client.get(
        "/api/dashboard/summary?clusterId=prod-us-east",
        headers=auth_headers(admin_token),
    )
    data = response.get_json()["data"]
    cpu = data["cpuUsage"]
    memory = data["memoryUsage"]
    assert cpu["available"] is True
    assert cpu["percent"] > 0
    assert memory["available"] is True
    assert memory["percent"] > 0
    assert cpu["usedDisplay"]
    assert cpu["allocatableDisplay"]


def test_dashboard_version_status_in_response(client, admin_token):
    response = client.get(
        "/api/dashboard/summary?clusterId=prod-us-east",
        headers=auth_headers(admin_token),
    )
    version = response.get_json()["data"]["version"]
    assert version["current"] == "v1.30.2"
    assert "status" in version
    assert "statusLabel" in version
    assert "providerKey" in version or version.get("provider")


def test_dashboard_summary_critical_on_failed_pods(client, admin_token):
    response = client.get(
        "/api/dashboard/summary?clusterId=staging-eu-west",
        headers=auth_headers(admin_token),
    )
    assert response.status_code == 200
    data = response.get_json()["data"]
    assert data["pods"]["failed"] > 0
    assert data["health"]["status"] == "critical"


def test_dashboard_summary_namespace_health_from_alerts(client, admin_token):
    response = client.get(
        "/api/dashboard/summary?clusterId=prod-us-east",
        headers=auth_headers(admin_token),
    )
    assert response.status_code == 200
    data = response.get_json()["data"]
    payments = next((ns for ns in data["namespaces"] if ns["name"] == "payments"), None)
    assert payments is not None
    assert payments["status"] == "critical"
    assert data["alerts"]["critical"] >= 1


def test_dashboard_summary_requires_cluster_id(client, admin_token):
    response = client.get("/api/dashboard/summary", headers=auth_headers(admin_token))
    assert response.status_code == 400
