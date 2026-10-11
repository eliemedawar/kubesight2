"""Regression tests for the Kubernetes read-path performance pass.

Shared cached payloads must never be changed in place by one reader, cache
keys must not over-match on invalidation, failures must not be cached, and
the read paths share one kubectl call where they used to each run their own.
"""

from unittest.mock import patch

from tests.conftest import auth_headers


def test_cluster_list_filter_does_not_mutate_shared_payload(client, viewer_token):
    shared = {
        "items": [{"id": "a", "name": "a"}, {"id": "b", "name": "b"}],
        "count": 2,
    }
    with patch("api.routes.clusters._list_clusters_payload", return_value=shared), patch(
        "api.routes.clusters.filter_clusters_for_user",
        side_effect=lambda user, items: [i for i in items if i["id"] == "a"],
    ):
        response = client.get("/api/clusters", headers=auth_headers(viewer_token))
    assert response.status_code == 200
    assert [i["id"] for i in response.get_json()["data"]["items"]] == ["a"]
    # The cache entry other readers get is untouched.
    assert shared["count"] == 2
    assert [i["id"] for i in shared["items"]] == ["a", "b"]


def test_rollout_reads_do_not_invalidate_caches(app):
    from api.cluster_access import ClusterAccess
    from api.services import deployment_service

    access = ClusterAccess("c1", "ctx", None, "c1", False)
    with patch.object(deployment_service, "resolve_cluster_access", return_value=access), patch(
        "api.k8s_provider._run_for_access", return_value="ok"
    ), patch("api.k8s_provider.invalidate_namespace_resources_cache") as inv:
        deployment_service._run_kubectl_for_cluster(
            "c1", ["rollout", "history", "deployment/x", "-n", "ns"]
        )
        deployment_service._run_kubectl_for_cluster(
            "c1", ["rollout", "status", "deployment/x", "-n", "ns"]
        )
        assert inv.call_count == 0
        deployment_service._run_kubectl_for_cluster(
            "c1", ["rollout", "restart", "deployment/x", "-n", "ns"]
        )
        assert inv.call_count == 1
