"""Tests for the Service → Client egress topology (per-deployment reverse connectivity)."""

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
    # WAF (entrypoint) → Backend API (leaf / deployment → egress source).
    return {
        "nodes": [
            {"tempId": "n1", "name": "WAF", "type": "Security"},
            {"tempId": "n2", "name": "Backend API", "type": "Deployment"},
        ],
        "edges": [{"sourceTempId": "n1", "targetTempId": "n2", "protocol": "HTTPS"}],
    }


def _egress_nodes(client, token, client_id, service_id):
    res = client.get(
        f"/api/clients/{client_id}/services/{service_id}/egress-nodes",
        headers=auth_headers(token),
    )
    assert res.status_code == 200, res.get_json()
    return res.get_json()["data"]


# ---------------------------------------------------------------------------
# List egress nodes
# ---------------------------------------------------------------------------

class TestListEgressNodes:
    def test_requires_auth(self, client):
        assert client.get("/api/clients/1/services/1/egress-nodes").status_code == 401

    def test_missing_client_404(self, client, admin_token):
        res = client.get("/api/clients/9999/services/1/egress-nodes", headers=auth_headers(admin_token))
        assert res.status_code == 404

    def test_lists_leaf_deployments(self, client, admin_token):
        svc = _create_service(client, admin_token, topology=_topology_two_nodes())
        cl = _create_client(client, admin_token, service_ids=[svc["id"]])
        data = _egress_nodes(client, admin_token, cl["id"], svc["id"])
        names = [n["nodeName"] for n in data["items"]]
        # Only the leaf (Backend API) is an egress source; WAF is the entrypoint.
        assert "Backend API" in names
        assert "WAF" not in names
        assert all(n["connection"] is None for n in data["items"])

    def test_no_topology_reports_empty(self, client, admin_token):
        svc = _create_service(client, admin_token, name="No Topo Svc")
        cl = _create_client(client, admin_token, service_ids=[svc["id"]])
        data = _egress_nodes(client, admin_token, cl["id"], svc["id"])
        assert data["hasTopology"] is False
        assert data["count"] == 0


# ---------------------------------------------------------------------------
# Upsert egress connection
# ---------------------------------------------------------------------------

class TestUpsertEgress:
    def _first_node_ref(self, client, token, client_id, service_id):
        data = _egress_nodes(client, token, client_id, service_id)
        return data["items"][0]["nodeRef"]

    def test_create_egress_connection(self, client, admin_token):
        svc = _create_service(client, admin_token, topology=_topology_two_nodes())
        cl = _create_client(client, admin_token, service_ids=[svc["id"]])
        node_ref = self._first_node_ref(client, admin_token, cl["id"], svc["id"])
        res = client.post(
            f"/api/clients/{cl['id']}/services/{svc['id']}/egress/{node_ref}/connection",
            json={"sourceIp": "10.4.12.50", "destinationIp": "196.10.20.5", "transportType": "VPN"},
            headers=auth_headers(admin_token),
        )
        assert res.status_code == 201, res.get_json()
        data = res.get_json()["data"]
        assert data["sourceIp"] == "10.4.12.50"
        assert data["transportType"] == "VPN"
        assert data["nodeName"] == "Backend API"

    def test_update_returns_200(self, client, admin_token):
        svc = _create_service(client, admin_token, topology=_topology_two_nodes())
        cl = _create_client(client, admin_token, service_ids=[svc["id"]])
        node_ref = self._first_node_ref(client, admin_token, cl["id"], svc["id"])
        url = f"/api/clients/{cl['id']}/services/{svc['id']}/egress/{node_ref}/connection"
        client.post(url, json={"transportType": "VPN"}, headers=auth_headers(admin_token))
        res = client.post(url, json={"transportType": "MPLS"}, headers=auth_headers(admin_token))
        assert res.status_code == 200
        assert res.get_json()["data"]["transportType"] == "MPLS"

    def test_unknown_node_404(self, client, admin_token):
        svc = _create_service(client, admin_token, topology=_topology_two_nodes())
        cl = _create_client(client, admin_token, service_ids=[svc["id"]])
        res = client.post(
            f"/api/clients/{cl['id']}/services/{svc['id']}/egress/999999/connection",
            json={"transportType": "VPN"}, headers=auth_headers(admin_token),
        )
        assert res.status_code == 404

    def test_invalid_transport_rejected(self, client, admin_token):
        svc = _create_service(client, admin_token, topology=_topology_two_nodes())
        cl = _create_client(client, admin_token, service_ids=[svc["id"]])
        node_ref = self._first_node_ref(client, admin_token, cl["id"], svc["id"])
        res = client.post(
            f"/api/clients/{cl['id']}/services/{svc['id']}/egress/{node_ref}/connection",
            json={"transportType": "Carrier Pigeon"}, headers=auth_headers(admin_token),
        )
        assert res.status_code == 400

    def test_viewer_cannot_write(self, client, viewer_token, admin_token):
        svc = _create_service(client, admin_token, topology=_topology_two_nodes())
        cl = _create_client(client, admin_token, service_ids=[svc["id"]])
        node_ref = self._first_node_ref(client, admin_token, cl["id"], svc["id"])
        res = client.post(
            f"/api/clients/{cl['id']}/services/{svc['id']}/egress/{node_ref}/connection",
            json={"transportType": "VPN"}, headers=auth_headers(viewer_token),
        )
        assert res.status_code == 403


# ---------------------------------------------------------------------------
# Composed reverse topology
# ---------------------------------------------------------------------------

class TestEgressTopology:
    def test_reverse_chain_ends_at_client(self, client, admin_token):
        svc = _create_service(client, admin_token, topology=_topology_two_nodes())
        cl = _create_client(client, admin_token, service_ids=[svc["id"]])
        node_ref = TestUpsertEgress()._first_node_ref(client, admin_token, cl["id"], svc["id"])
        client.post(
            f"/api/clients/{cl['id']}/services/{svc['id']}/egress/{node_ref}/connection",
            json={"sourceIp": "10.4.12.50", "destinationIp": "196.10.20.5", "transportType": "VPN"},
            headers=auth_headers(admin_token),
        )
        res = client.get(
            f"/api/clients/{cl['id']}/services/{svc['id']}/egress/{node_ref}/topology",
            headers=auth_headers(admin_token),
        )
        assert res.status_code == 200, res.get_json()
        topo = res.get_json()["data"]["topology"]
        names = [n["name"] for n in topo["nodes"]]
        assert "Bank ABC" in names   # client is the terminal node
        assert "VPN" in names        # transport node
        assert "Backend API" in names and "WAF" in names  # service topology preserved

        # The chosen deployment is flagged as the origin.
        origin = next(n for n in topo["nodes"] if n["name"] == "Backend API")
        assert origin.get("overlay") == "origin"

        # Edges reversed: Backend API → WAF (originally WAF → Backend API).
        waf = next(n for n in topo["nodes"] if n["name"] == "WAF")
        assert any(
            str(e["sourceNodeId"]) == str(origin["id"]) and str(e["targetNodeId"]) == str(waf["id"])
            for e in topo["edges"]
        )
        # Transport feeds the client node.
        transport = next(n for n in topo["nodes"] if n["name"] == "VPN")
        client_node = next(n for n in topo["nodes"] if n["name"] == "Bank ABC")
        assert any(
            str(e["sourceNodeId"]) == str(transport["id"]) and str(e["targetNodeId"]) == str(client_node["id"])
            for e in topo["edges"]
        )

    def test_topology_without_connection_shows_not_configured(self, client, admin_token):
        svc = _create_service(client, admin_token, topology=_topology_two_nodes())
        cl = _create_client(client, admin_token, service_ids=[svc["id"]])
        node_ref = TestUpsertEgress()._first_node_ref(client, admin_token, cl["id"], svc["id"])
        res = client.get(
            f"/api/clients/{cl['id']}/services/{svc['id']}/egress/{node_ref}/topology",
            headers=auth_headers(admin_token),
        )
        assert res.status_code == 200
        topo = res.get_json()["data"]["topology"]
        assert "Not configured" in " ".join((n.get("description") or "") for n in topo["nodes"])


# ---------------------------------------------------------------------------
# Delete / deactivate
# ---------------------------------------------------------------------------

class TestDeleteEgress:
    def _configured_node_ref(self, client, token, client_id, service_id):
        node_ref = TestUpsertEgress()._first_node_ref(client, token, client_id, service_id)
        client.post(
            f"/api/clients/{client_id}/services/{service_id}/egress/{node_ref}/connection",
            json={"transportType": "VPN"}, headers=auth_headers(token),
        )
        return node_ref

    def test_delete_removes_egress(self, client, admin_token):
        svc = _create_service(client, admin_token, topology=_topology_two_nodes())
        cl = _create_client(client, admin_token, service_ids=[svc["id"]])
        node_ref = self._configured_node_ref(client, admin_token, cl["id"], svc["id"])
        url = f"/api/clients/{cl['id']}/services/{svc['id']}/egress/{node_ref}/connection"
        res = client.delete(url, headers=auth_headers(admin_token))
        assert res.status_code == 200
        assert res.get_json()["data"]["deleted"] is True
        data = _egress_nodes(client, admin_token, cl["id"], svc["id"])
        node = next(n for n in data["items"] if n["nodeRef"] == node_ref)
        assert node["connection"] is None

    def test_deactivate_keeps_row(self, client, admin_token):
        svc = _create_service(client, admin_token, topology=_topology_two_nodes())
        cl = _create_client(client, admin_token, service_ids=[svc["id"]])
        node_ref = self._configured_node_ref(client, admin_token, cl["id"], svc["id"])
        url = f"/api/clients/{cl['id']}/services/{svc['id']}/egress/{node_ref}/connection?deactivate=true"
        res = client.delete(url, headers=auth_headers(admin_token))
        assert res.status_code == 200
        assert res.get_json()["data"]["deactivated"] is True
        data = _egress_nodes(client, admin_token, cl["id"], svc["id"])
        node = next(n for n in data["items"] if n["nodeRef"] == node_ref)
        assert node["connection"] is not None
        assert node["connection"]["isActive"] is False

    def test_delete_missing_404(self, client, admin_token):
        svc = _create_service(client, admin_token, topology=_topology_two_nodes())
        cl = _create_client(client, admin_token, service_ids=[svc["id"]])
        node_ref = TestUpsertEgress()._first_node_ref(client, admin_token, cl["id"], svc["id"])
        res = client.delete(
            f"/api/clients/{cl['id']}/services/{svc['id']}/egress/{node_ref}/connection",
            headers=auth_headers(admin_token),
        )
        assert res.status_code == 404
