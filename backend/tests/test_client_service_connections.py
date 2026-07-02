"""Tests for Client Service Access Topology (client-specific connectivity overlays)."""

from tests.conftest import auth_headers


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _create_service(client, token, name="Acquiring API", deployments=None, topology=None):
    payload = {"name": name, "deployments": deployments or [], "topology": topology or {"nodes": [], "edges": []}}
    res = client.post("/api/application-services", json=payload, headers=auth_headers(token))
    assert res.status_code == 201, res.get_json()
    return res.get_json()["data"]


def _create_client(client, token, name="Bank ABC", service_ids=None):
    payload = {"name": name, "serviceIds": service_ids or []}
    res = client.post("/api/clients", json=payload, headers=auth_headers(token))
    assert res.status_code == 201, res.get_json()
    return res.get_json()["data"]


def _topology_two_nodes():
    return {
        "nodes": [
            {"tempId": "n1", "name": "WAF", "type": "Security"},
            {"tempId": "n2", "name": "Backend API", "type": "Application"},
        ],
        "edges": [{"sourceTempId": "n1", "targetTempId": "n2", "protocol": "HTTPS"}],
    }


# ---------------------------------------------------------------------------
# Transport types
# ---------------------------------------------------------------------------

class TestTransportTypes:
    def test_list_transport_types(self, client, admin_token):
        res = client.get("/api/clients/transport-types", headers=auth_headers(admin_token))
        assert res.status_code == 200
        items = res.get_json()["data"]["items"]
        assert "VPN" in items and "Other" in items and "Direct Connect" in items


# ---------------------------------------------------------------------------
# List client services
# ---------------------------------------------------------------------------

class TestListClientServices:
    def test_list_requires_auth(self, client):
        assert client.get("/api/clients/1/services").status_code == 401

    def test_missing_client_404(self, client, admin_token):
        res = client.get("/api/clients/9999/services", headers=auth_headers(admin_token))
        assert res.status_code == 404

    def test_lists_linked_services_without_connection(self, client, admin_token):
        svc = _create_service(client, admin_token)
        cl = _create_client(client, admin_token, service_ids=[svc["id"]])
        res = client.get(f"/api/clients/{cl['id']}/services", headers=auth_headers(admin_token))
        assert res.status_code == 200
        data = res.get_json()["data"]
        assert data["count"] == 1
        item = data["items"][0]
        assert item["serviceId"] == svc["id"]
        assert item["connection"] is None


# ---------------------------------------------------------------------------
# Upsert connection
# ---------------------------------------------------------------------------

class TestUpsertConnection:
    def test_create_connection(self, client, admin_token):
        svc = _create_service(client, admin_token)
        cl = _create_client(client, admin_token, service_ids=[svc["id"]])
        payload = {
            "sourceIp": "196.10.20.5",
            "destinationIp": "10.4.12.50",
            "transportType": "VPN",
            "clusterId": "cluster-prod",
            "namespace": "production",
            "environment": "production",
            "status": "active",
        }
        res = client.post(
            f"/api/clients/{cl['id']}/services/{svc['id']}/connection",
            json=payload, headers=auth_headers(admin_token),
        )
        assert res.status_code == 201, res.get_json()
        data = res.get_json()["data"]
        assert data["sourceIp"] == "196.10.20.5"
        assert data["transportType"] == "VPN"
        assert data["isActive"] is True

    def test_update_existing_connection_returns_200(self, client, admin_token):
        svc = _create_service(client, admin_token)
        cl = _create_client(client, admin_token, service_ids=[svc["id"]])
        url = f"/api/clients/{cl['id']}/services/{svc['id']}/connection"
        client.post(url, json={"transportType": "VPN"}, headers=auth_headers(admin_token))
        res = client.post(url, json={"transportType": "MPLS"}, headers=auth_headers(admin_token))
        assert res.status_code == 200
        assert res.get_json()["data"]["transportType"] == "MPLS"

    def test_connection_creates_link_if_missing(self, client, admin_token):
        # Service not pre-linked to the client: configuring a connection links it.
        svc = _create_service(client, admin_token)
        cl = _create_client(client, admin_token, service_ids=[])
        res = client.post(
            f"/api/clients/{cl['id']}/services/{svc['id']}/connection",
            json={"transportType": "Internet"}, headers=auth_headers(admin_token),
        )
        assert res.status_code == 201
        listed = client.get(f"/api/clients/{cl['id']}/services", headers=auth_headers(admin_token)).get_json()["data"]
        assert listed["count"] == 1

    def test_invalid_transport_type_rejected(self, client, admin_token):
        svc = _create_service(client, admin_token)
        cl = _create_client(client, admin_token, service_ids=[svc["id"]])
        res = client.post(
            f"/api/clients/{cl['id']}/services/{svc['id']}/connection",
            json={"transportType": "Carrier Pigeon"}, headers=auth_headers(admin_token),
        )
        assert res.status_code == 400

    def test_other_requires_transport_name(self, client, admin_token):
        svc = _create_service(client, admin_token)
        cl = _create_client(client, admin_token, service_ids=[svc["id"]])
        url = f"/api/clients/{cl['id']}/services/{svc['id']}/connection"
        bad = client.post(url, json={"transportType": "Other"}, headers=auth_headers(admin_token))
        assert bad.status_code == 400
        ok = client.post(url, json={"transportType": "Other", "transportName": "Custom Link"}, headers=auth_headers(admin_token))
        assert ok.status_code == 201

    def test_viewer_cannot_write(self, client, viewer_token, admin_token):
        svc = _create_service(client, admin_token)
        cl = _create_client(client, admin_token, service_ids=[svc["id"]])
        res = client.post(
            f"/api/clients/{cl['id']}/services/{svc['id']}/connection",
            json={"transportType": "VPN"}, headers=auth_headers(viewer_token),
        )
        assert res.status_code == 403


# ---------------------------------------------------------------------------
# Composed topology
# ---------------------------------------------------------------------------

class TestComposedTopology:
    def test_topology_prepends_client_and_transport(self, client, admin_token):
        svc = _create_service(client, admin_token, topology=_topology_two_nodes())
        cl = _create_client(client, admin_token, service_ids=[svc["id"]])
        client.post(
            f"/api/clients/{cl['id']}/services/{svc['id']}/connection",
            json={"sourceIp": "196.10.20.5", "destinationIp": "10.4.12.50", "transportType": "VPN"},
            headers=auth_headers(admin_token),
        )
        res = client.get(f"/api/clients/{cl['id']}/services/{svc['id']}/topology", headers=auth_headers(admin_token))
        assert res.status_code == 200
        topo = res.get_json()["data"]["topology"]
        names = [n["name"] for n in topo["nodes"]]
        assert "Bank ABC" in names  # client node
        assert "VPN" in names       # transport node
        assert "WAF" in names       # existing service topology preserved
        # An edge should connect the transport node to the service entrypoint (WAF).
        waf = next(n for n in topo["nodes"] if n["name"] == "WAF")
        transport = next(n for n in topo["nodes"] if n["name"] == "VPN")
        assert any(
            str(e["sourceNodeId"]) == str(transport["id"]) and str(e["targetNodeId"]) == str(waf["id"])
            for e in topo["edges"]
        )

    def test_topology_without_service_topology_still_shows_chain(self, client, admin_token):
        svc = _create_service(client, admin_token, name="No Topo Svc")
        cl = _create_client(client, admin_token, service_ids=[svc["id"]])
        # No connection configured at all → IPs show as "Not configured".
        res = client.get(f"/api/clients/{cl['id']}/services/{svc['id']}/topology", headers=auth_headers(admin_token))
        assert res.status_code == 200
        topo = res.get_json()["data"]["topology"]
        names = [n["name"] for n in topo["nodes"]]
        assert "Bank ABC" in names
        assert "No Topo Svc" in names  # synthetic service node
        assert "Not configured" in " ".join(n.get("description", "") for n in topo["nodes"])


# ---------------------------------------------------------------------------
# Delete / deactivate
# ---------------------------------------------------------------------------

class TestDeleteConnection:
    def test_delete_removes_overlay_keeps_link(self, client, admin_token):
        svc = _create_service(client, admin_token)
        cl = _create_client(client, admin_token, service_ids=[svc["id"]])
        url = f"/api/clients/{cl['id']}/services/{svc['id']}/connection"
        client.post(url, json={"transportType": "VPN"}, headers=auth_headers(admin_token))
        res = client.delete(url, headers=auth_headers(admin_token))
        assert res.status_code == 200
        assert res.get_json()["data"]["deleted"] is True
        # Service link remains; connection is gone.
        listed = client.get(f"/api/clients/{cl['id']}/services", headers=auth_headers(admin_token)).get_json()["data"]
        assert listed["count"] == 1
        assert listed["items"][0]["connection"] is None

    def test_deactivate_keeps_row(self, client, admin_token):
        svc = _create_service(client, admin_token)
        cl = _create_client(client, admin_token, service_ids=[svc["id"]])
        url = f"/api/clients/{cl['id']}/services/{svc['id']}/connection"
        client.post(url, json={"transportType": "VPN"}, headers=auth_headers(admin_token))
        res = client.delete(url + "?deactivate=true", headers=auth_headers(admin_token))
        assert res.status_code == 200
        assert res.get_json()["data"]["deactivated"] is True
        listed = client.get(f"/api/clients/{cl['id']}/services", headers=auth_headers(admin_token)).get_json()["data"]
        conn = listed["items"][0]["connection"]
        assert conn is not None
        assert conn["isActive"] is False
        assert conn["status"] == "inactive"

    def test_delete_missing_404(self, client, admin_token):
        svc = _create_service(client, admin_token)
        cl = _create_client(client, admin_token, service_ids=[svc["id"]])
        res = client.delete(
            f"/api/clients/{cl['id']}/services/{svc['id']}/connection",
            headers=auth_headers(admin_token),
        )
        assert res.status_code == 404
