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


# --- TTLCache: max_age, exact invalidation (no app context = caching on) ------


def test_ttl_cache_max_age_reuses_young_entry_and_recomputes_old_one():


    from api.ttl_cache import TTLCache

    cache = TTLCache("t")
    calls = []

    def compute():
        calls.append(1)
        return len(calls)

    assert cache.get_or_compute("k", 60, compute, max_age=4) == 1
    assert cache.get_or_compute("k", 60, compute, max_age=4) == 1
    assert len(calls) == 1
    # Pretend the entry was stored 5 s ago: a max_age=4 poller recomputes,
    # a plain reader keeps the (still fresh) entry.
    with cache._lock:
        f, s, v, stored = cache._entries["k"]
        cache._entries["k"] = (f, s, v, stored - 5)
    assert cache.get_or_compute("k", 60, compute) == 1
    assert cache.get_or_compute("k", 60, compute, max_age=4) == 2
    assert cache.age("k") is not None and cache.age("k") < 1



def test_ttl_cache_exact_invalidation_does_not_overmatch():
    from api.ttl_cache import TTLCache

    cache = TTLCache("t")
    cache.set("nslist:custom-1", 1, 60)
    cache.set("nslist:custom-10", 10, 60)
    cache.invalidate("nslist:custom-1", exact=True)
    assert cache.get("nslist:custom-1") is None
    assert cache.get("nslist:custom-10") == 10


def test_namespace_invalidation_keeps_other_clusters():
    from api import k8s_provider as p

    p._K8S_READ_CACHE.set("nslist:custom-1", "a", 60)
    p._K8S_READ_CACHE.set("nslist:custom-10", "b", 60)
    p._K8S_READ_CACHE.set("res:custom-10:ns:pods", "c", 60)
    p.invalidate_namespace_resources_cache("custom-1", "ns")
    assert p._K8S_READ_CACHE.get("nslist:custom-1") is None
    assert p._K8S_READ_CACHE.get("nslist:custom-10") == "b"
    assert p._K8S_READ_CACHE.get("res:custom-10:ns:pods") == "c"
    p._K8S_READ_CACHE.invalidate()


def test_failed_namespace_list_is_not_cached():
    from api import k8s_provider as p
    from api.cluster_access import ClusterAccess

    p._K8S_READ_CACHE.invalidate()
    access = ClusterAccess("cfail", "ctx", None, "cfail", False)
    outputs = [p.K8sCommandError("i/o timeout"), '{"items": [{"metadata": {"name": "d1"}}]}']

    def run(_access, args, timeout=None):
        value = outputs.pop(0)
        if isinstance(value, Exception):
            raise value
        return value

    with patch.object(p, "_run_for_access", side_effect=run):
        first = p.namespace_resource_list_from_k8s(access, "ns", "deployments")
        assert first["deployments"] == []  # degraded exactly as before ...
        second = p.namespace_resource_list_from_k8s(access, "ns", "deployments")
        # ... but not cached: the next read goes back to the cluster.
        assert [d["name"] for d in second["deployments"]] == ["d1"]
    p._K8S_READ_CACHE.invalidate()


def test_forbidden_namespace_list_is_cached_as_empty():
    from api import k8s_provider as p
    from api.cluster_access import ClusterAccess

    p._K8S_READ_CACHE.invalidate()
    access = ClusterAccess("cforb", "ctx", None, "cforb", False)
    with patch.object(
        p, "_run_for_access", side_effect=p.K8sCommandError("secrets is forbidden: User x")
    ) as run:
        assert p.namespace_resource_list_from_k8s(access, "ns", "secrets")["secrets"] == []
        assert p.namespace_resource_list_from_k8s(access, "ns", "secrets")["secrets"] == []
        assert run.call_count == 1
    p._K8S_READ_CACHE.invalidate()
