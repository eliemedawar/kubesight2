from unittest.mock import patch

from api.k8s_provider import K8sCommandError
from api.services.topology_service import (
    build_cluster_topology,
    build_namespace_topology,
)
from tests.conftest import auth_headers


def _edge_pairs(topology):
    return {
        (edge["sourceNodeId"], edge["targetNodeId"])
        for edge in topology["edges"]
    }


def test_cluster_root_includes_namespace_workload_health():
    topology = build_cluster_topology(
        "demo",
        "Demo",
        [{"name": "payments", "pods": 2, "status": "active"}],
        [{"name": "worker-1", "status": "Ready"}],
        [{"name": "payments-api", "namespace": "payments", "status": "CrashLoopBackOff"}],
    )

    by_id = {node["id"]: node for node in topology["nodes"]}
    assert by_id["cluster"]["componentStatus"] == "unhealthy"
    assert by_id["ns:payments"]["componentStatus"] == "unhealthy"
    assert by_id["node:worker-1"]["componentStatus"] == "healthy"


def test_namespace_topology_uses_only_real_traffic_edges_and_resolves_selectors():
    topology = build_namespace_topology(
        "payments",
        [
            {
                "name": "api-abc",
                "status": "Running",
                "labels": {"app": "api"},
                "node": "worker-1",
                "ready": "1/1",
            },
            {
                "name": "orphan",
                "status": "CrashLoopBackOff",
                "labels": {"app": "other"},
            },
        ],
        [
            {
                "name": "api",
                "type": "ClusterIP",
                "ports": "80",
                "selector": {"app": "api"},
            }
        ],
        [
            {
                "name": "api-ingress",
                "host": "api.example.com",
                "path": "/",
                "backendService": "api",
            }
        ],
    )

    by_id = {node["id"]: node for node in topology["nodes"]}
    edges = _edge_pairs(topology)

    assert "namespace:payments" not in by_id
    assert by_id["svc:api"]["componentStatus"] == "healthy"
    assert ("ing:api-ingress", "svc:api") in edges
    assert ("svc:api", "pod:api-abc") in edges
    assert all("pod:orphan" not in pair for pair in edges)
    assert by_id["pod:orphan"]["componentStatus"] == "unhealthy"


def test_endpoint_ips_are_not_mistaken_for_pod_names():
    topology = build_namespace_topology(
        "demo",
        [{"name": "api-123", "status": "Running"}],
        [{"name": "api", "targetPods": "10.1.2.3"}],
        [],
    )

    assert ("svc:api", "pod:api-123") not in _edge_pairs(topology)
    assert topology["edges"] == []


def test_empty_namespace_returns_an_empty_graph():
    assert build_namespace_topology("empty", [], [], []) == {
        "nodes": [],
        "edges": [],
    }


def test_cluster_topology_returns_partial_graph_when_nodes_fail(client, admin_token):
    access = object()
    with (
        patch(
            "api.routes.clusters._resolve_cluster_access_or_error",
            return_value=(access, None),
        ),
        patch(
            "api.k8s_provider.list_namespace_counts_from_k8s",
            return_value={"items": [{"name": "payments", "pods": 2, "status": "active"}]},
        ),
        patch(
            "api.routes.clusters.list_nodes_from_k8s",
            side_effect=K8sCommandError("node API timed out"),
        ),
        patch(
            "api.routes.clusters.cluster_pod_issues_from_k8s",
            return_value={"pods": []},
        ),
    ):
        response = client.get(
            "/api/clusters/prod-us-east/topology",
            headers=auth_headers(admin_token),
        )

    assert response.status_code == 200
    data = response.get_json()["data"]
    assert data["partial"] is True
    assert any("Nodes could not be loaded" in warning for warning in data["warnings"])
    assert any(node["id"] == "ns:payments" for node in data["topology"]["nodes"])


def test_namespace_topology_returns_available_resource_groups(client, admin_token):
    access = object()

    def resources(_access, _namespace, list_key):
        if list_key == "services":
            raise K8sCommandError("services forbidden")
        if list_key == "pods":
            return {"pods": [{"name": "api-1", "status": "Running"}]}
        return {"ingress": []}

    with (
        patch(
            "api.routes.clusters._resolve_cluster_access_or_error",
            return_value=(access, None),
        ),
        patch(
            "api.routes.clusters.namespace_resource_list_from_k8s",
            side_effect=resources,
        ),
    ):
        response = client.get(
            "/api/clusters/prod-us-east/namespaces/payments/topology",
            headers=auth_headers(admin_token),
        )

    assert response.status_code == 200
    data = response.get_json()["data"]
    assert data["partial"] is True
    assert any(node["id"] == "pod:api-1" for node in data["topology"]["nodes"])
    assert data["topology"]["edges"] == []
