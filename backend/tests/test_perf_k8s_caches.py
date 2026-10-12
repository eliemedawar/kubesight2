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


# --- Helm: cached version probe + release list, timeouts ----------------------


def test_helm_release_list_is_shared_and_dropped_on_change():
    from api.cluster_access import ClusterAccess
    from api.services import helm_service as h

    h._HELM_CACHE.invalidate()
    access = ClusterAccess("hc1", "ctx", None, "hc1", False)
    calls = []

    def fake_run(_access, args, **_kw):
        calls.append(args)
        return '[{"name": "r1", "namespace": "ns"}]'

    with patch.object(h, "_helm_version_output", return_value="v3.15.0"), patch.object(
        h, "_resolve_access", return_value=access
    ), patch.object(h, "run_helm", side_effect=fake_run):
        assert [r["name"] for r in h.list_releases("hc1")] == ["r1"]
        assert [r["name"] for r in h.list_releases("hc1")] == ["r1"]
        assert len(calls) == 1
        h.invalidate_helm_release_cache("hc1")
        h.list_releases("hc1")
        assert len(calls) == 2
    h._HELM_CACHE.invalidate()


def test_helm_list_failure_is_not_cached():
    from api.cluster_access import ClusterAccess
    from api.services import helm_service as h

    h._HELM_CACHE.invalidate()
    access = ClusterAccess("hc2", "ctx", None, "hc2", False)
    outputs = [h.HelmCommandError("boom"), '[{"name": "r1", "namespace": "ns"}]']

    def fake_run(_access, args, **_kw):
        value = outputs.pop(0)
        if isinstance(value, Exception):
            raise value
        return value

    with patch.object(h, "_helm_version_output", return_value="v3"), patch.object(
        h, "_resolve_access", return_value=access
    ), patch.object(h, "run_helm", side_effect=fake_run):
        assert h.list_releases("hc2") == []
        assert [r["name"] for r in h.list_releases("hc2")] == ["r1"]
    h._HELM_CACHE.invalidate()


def test_helm_subprocess_timeouts():
    from api.services import helm_service as h

    assert h._helm_subprocess_timeout(["list", "-A"]) == h._HELM_READ_TIMEOUT_SECONDS
    assert h._helm_subprocess_timeout(["upgrade", "--install", "r", "c"]) == h._HELM_WRITE_TIMEOUT_SECONDS
    assert h._helm_subprocess_timeout(["upgrade", "r", "c", "--timeout", "10m0s"]) == 660
    assert h._helm_subprocess_timeout(["install", "r", "c", "--timeout=90s"]) == 150


def test_helm_version_probe_runs_once():
    from api.services import helm_service as h

    h._HELM_CACHE.invalidate()

    class Done:
        returncode = 0
        stdout = "v3.15.0+g1"
        stderr = ""

    with patch.object(h.subprocess, "run", return_value=Done()) as run:
        assert h.is_helm_installed()
        assert h.check_helm_available()["version"] == "v3.15.0+g1"
        assert run.call_count == 1
    h._HELM_CACHE.invalidate()
