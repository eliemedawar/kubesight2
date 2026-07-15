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

    def test_netted_ips_optional_round_trip(self, client, admin_token):
        svc = _create_service(client, admin_token)
        cl = _create_client(client, admin_token, service_ids=[svc["id"]])
        url = f"/api/clients/{cl['id']}/services/{svc['id']}/connection"
        # Omitted → serialized as empty strings.
        res = client.post(url, json={"transportType": "VPN"}, headers=auth_headers(admin_token))
        data = res.get_json()["data"]
        assert data["nettedSourceIp"] == ""
        assert data["nettedDestinationIp"] == ""
        # Provided → stored and returned.
        res = client.post(
            url,
            json={"sourceIp": "10.4.24.129", "nettedSourceIp": "196.10.20.99",
                  "nettedDestinationIp": "172.16.0.5", "transportType": "VPN"},
            headers=auth_headers(admin_token),
        )
        data = res.get_json()["data"]
        assert data["nettedSourceIp"] == "196.10.20.99"
        assert data["nettedDestinationIp"] == "172.16.0.5"

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
    def test_topology_prepends_client_and_tunnel(self, client, admin_token):
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
        assert "WAF" in names       # existing service topology preserved
        assert "VPN" not in names   # transport is a tunnel edge, not a node
        # A tunnel edge connects the client straight to the entrypoint (WAF),
        # labeled with the transport type and the end IPs.
        waf = next(n for n in topo["nodes"] if n["name"] == "WAF")
        client_node = next(n for n in topo["nodes"] if n["name"] == "Bank ABC")
        tunnel = next(e for e in topo["edges"] if e.get("kind") == "tunnel")
        assert tunnel["transportType"] == "VPN"
        assert str(tunnel["sourceNodeId"]) == str(client_node["id"])
        assert str(tunnel["targetNodeId"]) == str(waf["id"])
        assert tunnel["sourceLabel"] == "Source 196.10.20.5"
        assert tunnel["targetLabel"] == "Destination 10.4.12.50"

    def _topo_with_direction(self, client, admin_token, direction):
        svc = _create_service(client, admin_token, topology=_topology_two_nodes())
        cl = _create_client(client, admin_token, service_ids=[svc["id"]])
        client.post(
            f"/api/clients/{cl['id']}/services/{svc['id']}/connection",
            json={"sourceIp": "196.10.20.5", "destinationIp": "10.4.12.50",
                  "transportType": "VPN", "direction": direction},
            headers=auth_headers(admin_token),
        )
        res = client.get(f"/api/clients/{cl['id']}/services/{svc['id']}/topology", headers=auth_headers(admin_token))
        assert res.status_code == 200, res.get_json()
        return res.get_json()["data"]

    def _ids(self, topo):
        client_node = next(n for n in topo["nodes"] if n["name"] == "Bank ABC")["id"]
        waf = next(n for n in topo["nodes"] if n["name"] == "WAF")["id"]
        return str(client_node), str(waf)

    def _tunnel(self, topo):
        return next(e for e in topo["edges"] if e.get("kind") == "tunnel")

    def test_direction_defaults_inbound(self, client, admin_token):
        data = self._topo_with_direction(client, admin_token, "")
        assert data["connection"]["direction"] == "inbound"
        client_node, waf = self._ids(data["topology"])
        tunnel = self._tunnel(data["topology"])
        # inbound: tunnel drawn client → service (WAF).
        assert str(tunnel["sourceNodeId"]) == client_node
        assert str(tunnel["targetNodeId"]) == waf
        assert tunnel["bidirectional"] is False

    def test_direction_outbound_reverses_link(self, client, admin_token):
        data = self._topo_with_direction(client, admin_token, "outbound")
        client_node, waf = self._ids(data["topology"])
        tunnel = self._tunnel(data["topology"])
        # outbound: tunnel drawn service (WAF) → client; the Source IP label
        # sits at the service end and the Destination IP label at the client.
        assert str(tunnel["sourceNodeId"]) == waf
        assert str(tunnel["targetNodeId"]) == client_node
        assert tunnel["bidirectional"] is False
        assert tunnel["sourceLabel"].startswith("Source ")
        assert tunnel["targetLabel"].startswith("Destination ")

    def test_direction_both_is_bidirectional(self, client, admin_token):
        data = self._topo_with_direction(client, admin_token, "both")
        client_node, waf = self._ids(data["topology"])
        tunnel = self._tunnel(data["topology"])
        assert str(tunnel["sourceNodeId"]) == client_node
        assert str(tunnel["targetNodeId"]) == waf
        assert tunnel["bidirectional"] is True

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
        assert "Not configured" in " ".join(e.get("description", "") for e in topo["edges"])


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


# ---------------------------------------------------------------------------
# Component selection (which service component(s) the connection attaches to)
# ---------------------------------------------------------------------------

class TestComponentSelection:
    def _node_id(self, svc, name):
        return next(n["id"] for n in svc["topology"]["nodes"] if n["name"] == name)

    def _post_conn(self, client, token, cl, svc, **payload):
        return client.post(
            f"/api/clients/{cl['id']}/services/{svc['id']}/connection",
            json=payload, headers=auth_headers(token),
        )

    def _topology(self, client, token, cl, svc):
        res = client.get(f"/api/clients/{cl['id']}/services/{svc['id']}/topology", headers=auth_headers(token))
        assert res.status_code == 200, res.get_json()
        return res.get_json()["data"]["topology"]

    def _tunnels(self, topo):
        return [e for e in topo["edges"] if e.get("kind") == "tunnel"]

    def test_list_services_includes_components(self, client, admin_token):
        svc = _create_service(client, admin_token, topology=_topology_two_nodes())
        cl = _create_client(client, admin_token, service_ids=[svc["id"]])
        res = client.get(f"/api/clients/{cl['id']}/services", headers=auth_headers(admin_token))
        item = res.get_json()["data"]["items"][0]
        assert {c["name"] for c in item["components"]} == {"WAF", "Backend API"}

    def test_connection_stores_component_refs(self, client, admin_token):
        svc = _create_service(client, admin_token, topology=_topology_two_nodes())
        cl = _create_client(client, admin_token, service_ids=[svc["id"]])
        backend = self._node_id(svc, "Backend API")
        res = self._post_conn(
            client, admin_token, cl, svc, transportType="VPN",
            componentRefs=[{"ref": str(backend), "sourceIp": "10.0.0.1", "destinationIp": "10.0.0.2"}],
        )
        assert res.status_code == 201, res.get_json()
        refs = res.get_json()["data"]["componentRefs"]
        assert [r["ref"] for r in refs] == [str(backend)]
        assert refs[0]["name"] == "Backend API"
        assert refs[0]["sourceIp"] == "10.0.0.1"
        assert refs[0]["destinationIp"] == "10.0.0.2"

    def test_component_refs_store_netted_ips(self, client, admin_token):
        svc = _create_service(client, admin_token, topology=_topology_two_nodes())
        cl = _create_client(client, admin_token, service_ids=[svc["id"]])
        backend = self._node_id(svc, "Backend API")
        res = self._post_conn(
            client, admin_token, cl, svc, transportType="VPN",
            componentRefs=[{
                "ref": str(backend), "sourceIp": "10.0.0.1", "destinationIp": "10.0.0.2",
                "nettedSourceIp": "196.10.20.99", "nettedDestinationIp": "172.16.0.5",
            }],
        )
        assert res.status_code == 201, res.get_json()
        ref = res.get_json()["data"]["componentRefs"][0]
        assert ref["nettedSourceIp"] == "196.10.20.99"
        assert ref["nettedDestinationIp"] == "172.16.0.5"
        # The tunnel edge surfaces the netted addresses: description summary +
        # a second "NAT …" label line under the matching end's IP.
        topo = self._topology(client, admin_token, cl, svc)
        tunnel = self._tunnels(topo)[0]
        assert "NAT source: 196.10.20.99" in tunnel["description"]
        assert "NAT destination: 172.16.0.5" in tunnel["description"]
        assert tunnel["sourceLabel"] == "Source 10.0.0.1\nNAT 196.10.20.99"
        assert tunnel["targetLabel"] == "Destination 10.0.0.2\nNAT 172.16.0.5"

    def test_netted_ips_omitted_stay_out_of_description(self, client, admin_token):
        svc = _create_service(client, admin_token, topology=_topology_two_nodes())
        cl = _create_client(client, admin_token, service_ids=[svc["id"]])
        self._post_conn(client, admin_token, cl, svc, transportType="VPN",
                        sourceIp="10.0.0.1", destinationIp="10.0.0.2")
        topo = self._topology(client, admin_token, cl, svc)
        tunnel = self._tunnels(topo)[0]
        assert "NAT" not in tunnel["description"]
        assert "NAT" not in tunnel["sourceLabel"]
        assert "NAT" not in tunnel["targetLabel"]

    def test_invalid_component_ref_rejected(self, client, admin_token):
        svc = _create_service(client, admin_token, topology=_topology_two_nodes())
        cl = _create_client(client, admin_token, service_ids=[svc["id"]])
        res = self._post_conn(client, admin_token, cl, svc, transportType="VPN", componentRefs=["99999"])
        assert res.status_code == 400

    def test_topology_attaches_to_selected_component_only(self, client, admin_token):
        svc = _create_service(client, admin_token, topology=_topology_two_nodes())
        cl = _create_client(client, admin_token, service_ids=[svc["id"]])
        backend = self._node_id(svc, "Backend API")
        waf = self._node_id(svc, "WAF")
        self._post_conn(client, admin_token, cl, svc, transportType="VPN", direction="inbound", componentRefs=[str(backend)])
        topo = self._topology(client, admin_token, cl, svc)
        tunnels = self._tunnels(topo)
        # inbound: tunnel → selected Backend API, NOT the WAF entrypoint.
        assert [str(t["targetNodeId"]) for t in tunnels] == [str(backend)]
        assert str(waf) not in {str(t["targetNodeId"]) for t in tunnels}

    def test_topology_attaches_to_multiple_components(self, client, admin_token):
        svc = _create_service(client, admin_token, topology=_topology_two_nodes())
        cl = _create_client(client, admin_token, service_ids=[svc["id"]])
        backend = self._node_id(svc, "Backend API")
        waf = self._node_id(svc, "WAF")
        self._post_conn(client, admin_token, cl, svc, transportType="VPN", direction="inbound",
                        componentRefs=[str(backend), str(waf)])
        topo = self._topology(client, admin_token, cl, svc)
        # One tunnel per component: parallel client → component tunnels, not a
        # single shared hop.
        tunnels = self._tunnels(topo)
        assert len(tunnels) == 2
        client_node = next(n for n in topo["nodes"] if n["name"] == "Bank ABC")["id"]
        assert all(str(t["sourceNodeId"]) == str(client_node) for t in tunnels)
        assert {str(t["targetNodeId"]) for t in tunnels} == {str(backend), str(waf)}

    def test_per_component_ips_on_own_tunnel(self, client, admin_token):
        svc = _create_service(client, admin_token, topology=_topology_two_nodes())
        cl = _create_client(client, admin_token, service_ids=[svc["id"]])
        backend = self._node_id(svc, "Backend API")
        waf = self._node_id(svc, "WAF")
        self._post_conn(
            client, admin_token, cl, svc, transportType="VPN", direction="inbound",
            componentRefs=[
                {"ref": str(backend), "sourceIp": "10.0.0.1", "destinationIp": "10.0.0.2"},
                {"ref": str(waf), "sourceIp": "10.1.1.1", "destinationIp": "10.1.1.2"},
            ],
        )
        topo = self._topology(client, admin_token, cl, svc)
        tunnels = self._tunnels(topo)
        # Each component's tunnel carries that component's own IPs.
        bt = next(t for t in tunnels if str(t["targetNodeId"]) == str(backend))
        assert "10.0.0.1" in bt["sourceLabel"] and "10.0.0.2" in bt["targetLabel"]
        wt = next(t for t in tunnels if str(t["targetNodeId"]) == str(waf))
        assert "10.1.1.1" in wt["sourceLabel"] and "10.1.1.2" in wt["targetLabel"]

    def test_topology_falls_back_to_entrypoint_without_components(self, client, admin_token):
        svc = _create_service(client, admin_token, topology=_topology_two_nodes())
        cl = _create_client(client, admin_token, service_ids=[svc["id"]])
        waf = self._node_id(svc, "WAF")
        self._post_conn(client, admin_token, cl, svc, transportType="VPN")
        topo = self._topology(client, admin_token, cl, svc)
        # No components selected → attaches to the WAF entrypoint (legacy behavior).
        tunnels = self._tunnels(topo)
        assert [str(t["targetNodeId"]) for t in tunnels] == [str(waf)]

    def test_connection_survives_service_topology_edit(self, client, admin_token):
        """Editing the service topology must not orphan the connection's refs.

        The editor sends existing nodes back as tempId "node-<id>"; the save
        updates those rows in place so their ids (and every client
        connection's component selection) survive the edit.
        """
        svc = _create_service(client, admin_token, topology=_topology_two_nodes())
        cl = _create_client(client, admin_token, service_ids=[svc["id"]])
        backend = self._node_id(svc, "Backend API")
        self._post_conn(
            client, admin_token, cl, svc, transportType="VPN",
            componentRefs=[{"ref": str(backend), "sourceIp": "10.0.0.1", "destinationIp": "10.0.0.2"}],
        )
        # Re-save the service the way the editor does: existing nodes carry
        # their id in tempId, plus a rename and a brand-new node.
        res = client.put(
            f"/api/application-services/{svc['id']}",
            json={
                "name": svc["name"],
                "deployments": [],
                "topology": {
                    "nodes": [
                        {"tempId": f"node-{n['id']}",
                         "name": "Backend API v2" if n["name"] == "Backend API" else n["name"],
                         "type": n.get("type") or ""}
                        for n in svc["topology"]["nodes"]
                    ] + [{"tempId": "tmp-new", "name": "Cache", "type": "Datastore"}],
                    "edges": [],
                },
            },
            headers=auth_headers(admin_token),
        )
        assert res.status_code == 200, res.get_json()
        updated = res.get_json()["data"]
        # The edited node kept its id (just renamed).
        assert str(self._node_id(updated, "Backend API v2")) == str(backend)
        # The connection still points at it, with its per-component IPs.
        listed = client.get(f"/api/clients/{cl['id']}/services", headers=auth_headers(admin_token)).get_json()["data"]
        refs = listed["items"][0]["connection"]["componentRefs"]
        assert [r["ref"] for r in refs] == [str(backend)]
        assert refs[0]["sourceIp"] == "10.0.0.1"
        topo = self._topology(client, admin_token, cl, svc)
        assert [str(t["targetNodeId"]) for t in self._tunnels(topo)] == [str(backend)]

    def test_orphaned_refs_heal_by_component_name(self, client, admin_token):
        """Connections broken by pre-fix topology saves re-attach by name.

        Saving with unrecognized tempIds recreates every node with fresh ids
        (the legacy behavior that orphaned refs); the next read should
        re-point the stored refs to the same-named live nodes and persist it.
        """
        svc = _create_service(client, admin_token, topology=_topology_two_nodes())
        cl = _create_client(client, admin_token, service_ids=[svc["id"]])
        backend = self._node_id(svc, "Backend API")
        self._post_conn(
            client, admin_token, cl, svc, transportType="VPN",
            componentRefs=[{"ref": str(backend), "sourceIp": "10.0.0.1", "destinationIp": "10.0.0.2"}],
        )
        res = client.put(
            f"/api/application-services/{svc['id']}",
            json={
                "name": svc["name"],
                "deployments": [],
                "topology": {
                    "nodes": [
                        {"tempId": "a1", "name": "WAF", "type": "Security"},
                        {"tempId": "a2", "name": "Backend API", "type": "Application"},
                    ],
                    "edges": [],
                },
            },
            headers=auth_headers(admin_token),
        )
        assert res.status_code == 200, res.get_json()
        new_backend = self._node_id(res.get_json()["data"], "Backend API")
        assert str(new_backend) != str(backend)  # ids really did change
        # Reading the client services heals the refs onto the new node id.
        listed = client.get(f"/api/clients/{cl['id']}/services", headers=auth_headers(admin_token)).get_json()["data"]
        refs = listed["items"][0]["connection"]["componentRefs"]
        assert [r["ref"] for r in refs] == [str(new_backend)]
        assert refs[0]["name"] == "Backend API"
        assert refs[0]["sourceIp"] == "10.0.0.1"
        topo = self._topology(client, admin_token, cl, svc)
        assert [str(t["targetNodeId"]) for t in self._tunnels(topo)] == [str(new_backend)]

    def test_status_only_update_keeps_component_refs(self, client, admin_token):
        svc = _create_service(client, admin_token, topology=_topology_two_nodes())
        cl = _create_client(client, admin_token, service_ids=[svc["id"]])
        backend = self._node_id(svc, "Backend API")
        self._post_conn(client, admin_token, cl, svc, transportType="VPN", componentRefs=[str(backend)])
        # A partial update that omits componentRefs must not clear the selection.
        res = self._post_conn(client, admin_token, cl, svc, transportType="VPN", status="degraded")
        refs = res.get_json()["data"]["componentRefs"]
        assert [r["ref"] for r in refs] == [str(backend)]
