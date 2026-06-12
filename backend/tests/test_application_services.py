"""Tests for Application Services CRUD, health, RBAC, and deployment management."""

import pytest
from tests.conftest import auth_headers


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _create_service(client, token, name="Auth Service", description="", deployments=None):
    payload = {"name": name, "description": description, "deployments": deployments or []}
    return client.post("/api/application-services", json=payload, headers=auth_headers(token))


def _sample_deployment(cluster="cluster-a", namespace="default", name="my-deploy"):
    return {"clusterId": cluster, "namespace": namespace, "deploymentName": name}


# ---------------------------------------------------------------------------
# List
# ---------------------------------------------------------------------------

class TestListServices:
    def test_list_empty(self, client, admin_token):
        res = client.get("/api/application-services", headers=auth_headers(admin_token))
        assert res.status_code == 200
        data = res.get_json()["data"]
        assert data["items"] == []
        assert data["count"] == 0

    def test_list_requires_auth(self, client):
        res = client.get("/api/application-services")
        assert res.status_code == 401

    def test_list_viewer_can_view(self, client, viewer_token):
        res = client.get("/api/application-services", headers=auth_headers(viewer_token))
        assert res.status_code == 200

    def test_list_returns_created_services(self, client, admin_token):
        _create_service(client, admin_token, "Svc A")
        _create_service(client, admin_token, "Svc B")
        res = client.get("/api/application-services", headers=auth_headers(admin_token))
        assert res.status_code == 200
        data = res.get_json()["data"]
        assert data["count"] == 2
        names = {s["name"] for s in data["items"]}
        assert names == {"Svc A", "Svc B"}


# ---------------------------------------------------------------------------
# Create
# ---------------------------------------------------------------------------

class TestCreateService:
    def test_create_minimal(self, client, admin_token):
        res = _create_service(client, admin_token, "Billing Service")
        assert res.status_code == 201
        data = res.get_json()["data"]
        assert data["name"] == "Billing Service"
        assert data["deploymentCount"] == 0
        assert data["health"] == "unknown"
        assert "id" in data
        assert "createdAt" in data

    def test_create_with_description(self, client, admin_token):
        res = _create_service(client, admin_token, "Svc", description="My description")
        assert res.status_code == 201
        assert res.get_json()["data"]["description"] == "My description"

    def test_create_with_deployments(self, client, admin_token):
        deps = [
            _sample_deployment("cluster-a", "default", "api"),
            _sample_deployment("cluster-b", "staging", "api"),
        ]
        res = _create_service(client, admin_token, "Multi-Cluster Svc", deployments=deps)
        assert res.status_code == 201
        data = res.get_json()["data"]
        assert data["deploymentCount"] == 2

    def test_create_deduplicates_deployments(self, client, admin_token):
        dep = _sample_deployment("cluster-a", "default", "api")
        res = _create_service(client, admin_token, "Svc", deployments=[dep, dep])
        assert res.status_code == 201
        assert res.get_json()["data"]["deploymentCount"] == 1

    def test_create_duplicate_name_rejected(self, client, admin_token):
        _create_service(client, admin_token, "Auth")
        res = _create_service(client, admin_token, "Auth")
        assert res.status_code == 409

    def test_create_missing_name_rejected(self, client, admin_token):
        res = client.post(
            "/api/application-services",
            json={"description": "no name"},
            headers=auth_headers(admin_token),
        )
        assert res.status_code == 400

    def test_create_requires_permission(self, client, viewer_token):
        res = _create_service(client, viewer_token, "X")
        assert res.status_code == 403


# ---------------------------------------------------------------------------
# Get
# ---------------------------------------------------------------------------

class TestGetService:
    def test_get_existing(self, client, admin_token):
        create_res = _create_service(client, admin_token, "Payment Svc")
        sid = create_res.get_json()["data"]["id"]
        res = client.get(f"/api/application-services/{sid}", headers=auth_headers(admin_token))
        assert res.status_code == 200
        assert res.get_json()["data"]["name"] == "Payment Svc"

    def test_get_missing_returns_404(self, client, admin_token):
        res = client.get("/api/application-services/99999", headers=auth_headers(admin_token))
        assert res.status_code == 404

    def test_get_viewer_can_read(self, client, admin_token, viewer_token):
        sid = _create_service(client, admin_token, "S").get_json()["data"]["id"]
        res = client.get(f"/api/application-services/{sid}", headers=auth_headers(viewer_token))
        assert res.status_code == 200


# ---------------------------------------------------------------------------
# Update
# ---------------------------------------------------------------------------

class TestUpdateService:
    def test_update_name_and_description(self, client, admin_token):
        sid = _create_service(client, admin_token, "Old Name").get_json()["data"]["id"]
        res = client.put(
            f"/api/application-services/{sid}",
            json={"name": "New Name", "description": "Updated", "deployments": []},
            headers=auth_headers(admin_token),
        )
        assert res.status_code == 200
        data = res.get_json()["data"]
        assert data["name"] == "New Name"
        assert data["description"] == "Updated"

    def test_update_replaces_deployments(self, client, admin_token):
        dep_a = _sample_deployment("c1", "ns1", "dep-a")
        dep_b = _sample_deployment("c2", "ns2", "dep-b")
        sid = _create_service(client, admin_token, "Svc", deployments=[dep_a]).get_json()["data"]["id"]

        res = client.put(
            f"/api/application-services/{sid}",
            json={"name": "Svc", "deployments": [dep_b]},
            headers=auth_headers(admin_token),
        )
        assert res.status_code == 200
        deps = res.get_json()["data"]["deployments"]
        assert len(deps) == 1
        assert deps[0]["deploymentName"] == "dep-b"

    def test_update_missing_returns_404(self, client, admin_token):
        res = client.put(
            "/api/application-services/99999",
            json={"name": "X", "deployments": []},
            headers=auth_headers(admin_token),
        )
        assert res.status_code == 404

    def test_update_requires_permission(self, client, admin_token, viewer_token):
        sid = _create_service(client, admin_token, "S").get_json()["data"]["id"]
        res = client.put(
            f"/api/application-services/{sid}",
            json={"name": "S", "deployments": []},
            headers=auth_headers(viewer_token),
        )
        assert res.status_code == 403


# ---------------------------------------------------------------------------
# Delete
# ---------------------------------------------------------------------------

class TestDeleteService:
    def test_delete_existing(self, client, admin_token):
        sid = _create_service(client, admin_token, "ToDelete").get_json()["data"]["id"]
        res = client.delete(f"/api/application-services/{sid}", headers=auth_headers(admin_token))
        assert res.status_code == 200
        assert res.get_json()["data"]["deleted"] is True
        # Confirm gone
        get_res = client.get(f"/api/application-services/{sid}", headers=auth_headers(admin_token))
        assert get_res.status_code == 404

    def test_delete_missing_returns_404(self, client, admin_token):
        res = client.delete("/api/application-services/99999", headers=auth_headers(admin_token))
        assert res.status_code == 404

    def test_delete_requires_permission(self, client, admin_token, viewer_token):
        sid = _create_service(client, admin_token, "S").get_json()["data"]["id"]
        res = client.delete(f"/api/application-services/{sid}", headers=auth_headers(viewer_token))
        assert res.status_code == 403


# ---------------------------------------------------------------------------
# Multi-cluster deployment support
# ---------------------------------------------------------------------------

class TestMultiClusterDeployments:
    def test_deployments_across_multiple_clusters(self, client, admin_token):
        deps = [
            _sample_deployment("cluster-prod", "production", "auth-api"),
            _sample_deployment("cluster-prod", "production", "auth-worker"),
            _sample_deployment("cluster-staging", "staging", "auth-api"),
            _sample_deployment("cluster-dev", "development", "auth-api"),
        ]
        res = _create_service(client, admin_token, "Auth", deployments=deps)
        assert res.status_code == 201
        data = res.get_json()["data"]
        assert data["deploymentCount"] == 4
        cluster_ids = {d["clusterId"] for d in data["deployments"]}
        assert cluster_ids == {"cluster-prod", "cluster-staging", "cluster-dev"}

    def test_deployments_across_multiple_namespaces(self, client, admin_token):
        deps = [
            _sample_deployment("cluster-a", "ns-1", "svc"),
            _sample_deployment("cluster-a", "ns-2", "svc"),
            _sample_deployment("cluster-a", "ns-3", "svc"),
        ]
        res = _create_service(client, admin_token, "Multi-NS", deployments=deps)
        assert res.status_code == 201
        namespaces = {d["namespace"] for d in res.get_json()["data"]["deployments"]}
        assert namespaces == {"ns-1", "ns-2", "ns-3"}


# ---------------------------------------------------------------------------
# Health calculation
# ---------------------------------------------------------------------------

class TestHealthCalculation:
    def test_health_unknown_when_no_deployments(self, client, admin_token):
        sid = _create_service(client, admin_token, "Empty").get_json()["data"]["id"]
        res = client.get(f"/api/application-services/{sid}", headers=auth_headers(admin_token))
        assert res.get_json()["data"]["health"] == "unknown"

    def test_list_includes_health_field(self, client, admin_token):
        _create_service(client, admin_token, "S1")
        res = client.get("/api/application-services", headers=auth_headers(admin_token))
        for item in res.get_json()["data"]["items"]:
            assert "health" in item

    def test_health_field_valid_values(self, client, admin_token):
        _create_service(client, admin_token, "S2")
        items = client.get("/api/application-services", headers=auth_headers(admin_token)).get_json()["data"]["items"]
        for item in items:
            assert item["health"] in ("healthy", "warning", "critical", "unknown")


# ---------------------------------------------------------------------------
# Empty state
# ---------------------------------------------------------------------------

class TestEmptyState:
    def test_list_empty_returns_zero_count(self, client, admin_token):
        res = client.get("/api/application-services", headers=auth_headers(admin_token))
        assert res.get_json()["data"]["count"] == 0


# ---------------------------------------------------------------------------
# Deployment picker
# ---------------------------------------------------------------------------

class TestDeploymentPicker:
    def test_picker_requires_auth(self, client):
        res = client.get("/api/application-services/picker/deployments?clusterId=c&namespace=n")
        assert res.status_code == 401

    def test_picker_requires_create_or_update_permission(self, client, viewer_token):
        res = client.get(
            "/api/application-services/picker/deployments?clusterId=c&namespace=n",
            headers=auth_headers(viewer_token),
        )
        assert res.status_code == 403

    def test_picker_returns_list(self, client, admin_token):
        res = client.get(
            "/api/application-services/picker/deployments?clusterId=cluster-prod&namespace=production",
            headers=auth_headers(admin_token),
        )
        assert res.status_code == 200
        data = res.get_json()["data"]
        assert "items" in data
        assert isinstance(data["items"], list)
