"""Tests for Clients CRUD, service assignment, status calculation, and RBAC."""

import pytest
from tests.conftest import auth_headers


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _create_service(client, token, name="Auth Service"):
    res = client.post(
        "/api/application-services",
        json={"name": name, "description": "", "deployments": []},
        headers=auth_headers(token),
    )
    assert res.status_code == 201
    return res.get_json()["data"]["id"]


def _create_client(http_client, token, name="Acme Corp", **kwargs):
    payload = {"name": name, **kwargs}
    return http_client.post("/api/clients", json=payload, headers=auth_headers(token))


# ---------------------------------------------------------------------------
# List
# ---------------------------------------------------------------------------

class TestListClients:
    def test_list_empty(self, client, admin_token):
        res = client.get("/api/clients", headers=auth_headers(admin_token))
        assert res.status_code == 200
        data = res.get_json()["data"]
        assert data["items"] == []
        assert data["count"] == 0

    def test_list_requires_auth(self, client):
        res = client.get("/api/clients")
        assert res.status_code == 401

    def test_list_viewer_can_view(self, client, viewer_token):
        res = client.get("/api/clients", headers=auth_headers(viewer_token))
        assert res.status_code == 200

    def test_list_returns_created_clients(self, client, admin_token):
        _create_client(client, admin_token, "Client A")
        _create_client(client, admin_token, "Client B")
        data = client.get("/api/clients", headers=auth_headers(admin_token)).get_json()["data"]
        assert data["count"] == 2
        names = {c["name"] for c in data["items"]}
        assert names == {"Client A", "Client B"}


# ---------------------------------------------------------------------------
# Create
# ---------------------------------------------------------------------------

class TestCreateClient:
    def test_create_minimal(self, client, admin_token):
        res = _create_client(client, admin_token, "Acme")
        assert res.status_code == 201
        data = res.get_json()["data"]
        assert data["name"] == "Acme"
        assert data["serviceCount"] == 0
        assert data["status"] == "unknown"
        assert "id" in data
        assert "createdAt" in data

    def test_create_with_contact_info(self, client, admin_token):
        res = _create_client(
            client, admin_token, "Beta",
            contactPerson="Jane Doe",
            email="jane@beta.com",
            phone="+1-555-0100",
            notes="VIP client",
        )
        assert res.status_code == 201
        data = res.get_json()["data"]
        assert data["contactPerson"] == "Jane Doe"
        assert data["email"] == "jane@beta.com"
        assert data["phone"] == "+1-555-0100"
        assert data["notes"] == "VIP client"

    def test_create_with_services(self, client, admin_token):
        sid = _create_service(client, admin_token)
        res = _create_client(client, admin_token, "Gamma", serviceIds=[sid])
        assert res.status_code == 201
        data = res.get_json()["data"]
        assert data["serviceCount"] == 1

    def test_create_duplicate_name_rejected(self, client, admin_token):
        _create_client(client, admin_token, "Acme")
        res = _create_client(client, admin_token, "Acme")
        assert res.status_code == 409

    def test_create_missing_name_rejected(self, client, admin_token):
        res = client.post(
            "/api/clients",
            json={"email": "test@test.com"},
            headers=auth_headers(admin_token),
        )
        assert res.status_code == 400

    def test_create_requires_permission(self, client, viewer_token):
        res = _create_client(client, viewer_token, "X")
        assert res.status_code == 403


# ---------------------------------------------------------------------------
# Get
# ---------------------------------------------------------------------------

class TestGetClient:
    def test_get_existing(self, client, admin_token):
        cid = _create_client(client, admin_token, "Delta").get_json()["data"]["id"]
        res = client.get(f"/api/clients/{cid}", headers=auth_headers(admin_token))
        assert res.status_code == 200
        assert res.get_json()["data"]["name"] == "Delta"

    def test_get_missing_returns_404(self, client, admin_token):
        res = client.get("/api/clients/99999", headers=auth_headers(admin_token))
        assert res.status_code == 404

    def test_get_viewer_can_read(self, client, admin_token, viewer_token):
        cid = _create_client(client, admin_token, "E").get_json()["data"]["id"]
        res = client.get(f"/api/clients/{cid}", headers=auth_headers(viewer_token))
        assert res.status_code == 200


# ---------------------------------------------------------------------------
# Update
# ---------------------------------------------------------------------------

class TestUpdateClient:
    def test_update_name_and_contact(self, client, admin_token):
        cid = _create_client(client, admin_token, "Old Name").get_json()["data"]["id"]
        res = client.put(
            f"/api/clients/{cid}",
            json={"name": "New Name", "contactPerson": "Bob"},
            headers=auth_headers(admin_token),
        )
        assert res.status_code == 200
        data = res.get_json()["data"]
        assert data["name"] == "New Name"
        assert data["contactPerson"] == "Bob"

    def test_update_replaces_services(self, client, admin_token):
        sid_a = _create_service(client, admin_token, "SvcA")
        sid_b = _create_service(client, admin_token, "SvcB")
        cid = _create_client(client, admin_token, "Client", serviceIds=[sid_a]).get_json()["data"]["id"]

        res = client.put(
            f"/api/clients/{cid}",
            json={"name": "Client", "serviceIds": [sid_b]},
            headers=auth_headers(admin_token),
        )
        assert res.status_code == 200
        data = res.get_json()["data"]
        assert data["serviceCount"] == 1
        assert data["services"][0]["id"] == sid_b

    def test_update_missing_returns_404(self, client, admin_token):
        res = client.put("/api/clients/99999", json={"name": "X"}, headers=auth_headers(admin_token))
        assert res.status_code == 404

    def test_update_requires_permission(self, client, admin_token, viewer_token):
        cid = _create_client(client, admin_token, "C").get_json()["data"]["id"]
        res = client.put(
            f"/api/clients/{cid}",
            json={"name": "C"},
            headers=auth_headers(viewer_token),
        )
        assert res.status_code == 403


# ---------------------------------------------------------------------------
# Delete
# ---------------------------------------------------------------------------

class TestDeleteClient:
    def test_delete_existing(self, client, admin_token):
        cid = _create_client(client, admin_token, "ToDelete").get_json()["data"]["id"]
        res = client.delete(f"/api/clients/{cid}", headers=auth_headers(admin_token))
        assert res.status_code == 200
        assert res.get_json()["data"]["deleted"] is True
        get_res = client.get(f"/api/clients/{cid}", headers=auth_headers(admin_token))
        assert get_res.status_code == 404

    def test_delete_missing_returns_404(self, client, admin_token):
        res = client.delete("/api/clients/99999", headers=auth_headers(admin_token))
        assert res.status_code == 404

    def test_delete_requires_permission(self, client, admin_token, viewer_token):
        cid = _create_client(client, admin_token, "C").get_json()["data"]["id"]
        res = client.delete(f"/api/clients/{cid}", headers=auth_headers(viewer_token))
        assert res.status_code == 403


# ---------------------------------------------------------------------------
# Service assignment
# ---------------------------------------------------------------------------

class TestServiceAssignment:
    def test_assign_multiple_services(self, client, admin_token):
        sid1 = _create_service(client, admin_token, "Svc1")
        sid2 = _create_service(client, admin_token, "Svc2")
        sid3 = _create_service(client, admin_token, "Svc3")
        cid = _create_client(client, admin_token, "Client", serviceIds=[sid1, sid2, sid3]).get_json()["data"]["id"]
        data = client.get(f"/api/clients/{cid}", headers=auth_headers(admin_token)).get_json()["data"]
        assert data["serviceCount"] == 3

    def test_remove_all_services(self, client, admin_token):
        sid = _create_service(client, admin_token)
        cid = _create_client(client, admin_token, "C", serviceIds=[sid]).get_json()["data"]["id"]
        client.put(f"/api/clients/{cid}", json={"name": "C", "serviceIds": []}, headers=auth_headers(admin_token))
        data = client.get(f"/api/clients/{cid}", headers=auth_headers(admin_token)).get_json()["data"]
        assert data["serviceCount"] == 0

    def test_assign_nonexistent_service_ignored(self, client, admin_token):
        cid = _create_client(client, admin_token, "C", serviceIds=[99999]).get_json()["data"]["id"]
        data = client.get(f"/api/clients/{cid}", headers=auth_headers(admin_token)).get_json()["data"]
        assert data["serviceCount"] == 0


# ---------------------------------------------------------------------------
# Status calculation
# ---------------------------------------------------------------------------

class TestClientStatusCalculation:
    def test_status_unknown_when_no_services(self, client, admin_token):
        cid = _create_client(client, admin_token, "Empty").get_json()["data"]["id"]
        data = client.get(f"/api/clients/{cid}", headers=auth_headers(admin_token)).get_json()["data"]
        assert data["status"] == "unknown"

    def test_status_included_in_list(self, client, admin_token):
        _create_client(client, admin_token, "C1")
        items = client.get("/api/clients", headers=auth_headers(admin_token)).get_json()["data"]["items"]
        for item in items:
            assert "status" in item
            assert item["status"] in ("healthy", "warning", "critical", "unknown")

    def test_services_included_in_detail(self, client, admin_token):
        sid = _create_service(client, admin_token)
        cid = _create_client(client, admin_token, "C", serviceIds=[sid]).get_json()["data"]["id"]
        data = client.get(f"/api/clients/{cid}", headers=auth_headers(admin_token)).get_json()["data"]
        assert len(data["services"]) == 1
        assert data["services"][0]["id"] == sid


# ---------------------------------------------------------------------------
# RBAC enforcement
# ---------------------------------------------------------------------------

class TestClientRBAC:
    def test_viewer_cannot_create(self, client, viewer_token):
        assert _create_client(client, viewer_token, "X").status_code == 403

    def test_viewer_cannot_update(self, client, admin_token, viewer_token):
        cid = _create_client(client, admin_token, "C").get_json()["data"]["id"]
        res = client.put(f"/api/clients/{cid}", json={"name": "C"}, headers=auth_headers(viewer_token))
        assert res.status_code == 403

    def test_viewer_cannot_delete(self, client, admin_token, viewer_token):
        cid = _create_client(client, admin_token, "C").get_json()["data"]["id"]
        res = client.delete(f"/api/clients/{cid}", headers=auth_headers(viewer_token))
        assert res.status_code == 403

    def test_operator_can_create_and_update(self, client, operator_token):
        res = _create_client(client, operator_token, "Operator Client")
        assert res.status_code == 201
        cid = res.get_json()["data"]["id"]
        update_res = client.put(
            f"/api/clients/{cid}",
            json={"name": "Operator Client Updated"},
            headers=auth_headers(operator_token),
        )
        assert update_res.status_code == 200
