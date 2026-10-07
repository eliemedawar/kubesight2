"""GET /api/clusters cache: one rebuild at a time, stale served while refreshing."""

import threading
import time
from unittest.mock import patch

import api.k8s_provider as provider
from api.k8s_provider import invalidate_cluster_list_cache, list_clusters_from_k8s


def _cache_on():
    return patch("api.k8s_provider._cluster_list_cache_disabled", return_value=False)


class _no_discovery:
    """No kubeconfig contexts and no discovered clusters: custom clusters only."""

    def __enter__(self):
        self._patches = [
            patch("api.k8s_provider._discovered_clusters_from_k8s", return_value=[]),
            patch("api.k8s_provider.is_real_mode_enabled", return_value=False),
            patch("api.k8s_provider._kubectl_has_contexts", return_value=False),
        ]
        for p in self._patches:
            p.start()
        return self

    def __exit__(self, *exc):
        for p in reversed(self._patches):
            p.stop()
        return False


def test_concurrent_cold_callers_share_one_build(app):
    invalidate_cluster_list_cache()
    calls = []
    release = threading.Event()

    def slow_build():
        calls.append(1)
        release.wait(5)
        return [{"id": "custom-1", "name": "one"}]

    with _cache_on(), _no_discovery(), patch("api.k8s_provider._custom_clusters_as_items", side_effect=slow_build):
        results = []

        def call():
            results.append(list_clusters_from_k8s())

        threads = [threading.Thread(target=call) for _ in range(8)]
        for t in threads:
            t.start()
        time.sleep(0.3)
        release.set()
        for t in threads:
            t.join(5)

    assert len(calls) == 1
    assert len(results) == 8 and all(r["count"] == 1 for r in results)
    invalidate_cluster_list_cache()


def test_expired_list_is_served_while_one_refresh_runs(app):
    invalidate_cluster_list_cache()
    with _cache_on(), _no_discovery(), \
            patch("api.k8s_provider._custom_clusters_as_items", return_value=[{"id": "custom-1", "name": "old"}]):
        assert list_clusters_from_k8s()["items"][0]["name"] == "old"

    # Expire it, then make the background refresh slow and observable.
    with provider._cluster_list_cache_lock:
        provider._cluster_list_cache["expires_at"] = 0.0
    refreshed = threading.Event()
    gate = threading.Event()
    builds = []

    def new_items(_snapshots):
        builds.append(1)
        gate.wait(5)
        refreshed.set()
        return [{"id": "custom-1", "name": "new"}]

    with _cache_on(), _no_discovery(), \
            patch("api.k8s_provider._custom_cluster_snapshots", return_value=[]), \
            patch("api.k8s_provider._custom_cluster_items_from_snapshots", side_effect=new_items):
        first = list_clusters_from_k8s()
        second = list_clusters_from_k8s()
        assert first["items"][0]["name"] == "old"
        assert second["items"][0]["name"] == "old"
        gate.set()
        assert refreshed.wait(5)
        deadline = time.time() + 5
        while time.time() < deadline and list_clusters_from_k8s()["items"][0]["name"] != "new":
            time.sleep(0.05)
        assert list_clusters_from_k8s()["items"][0]["name"] == "new"
    assert len(builds) == 1
    invalidate_cluster_list_cache()


def test_refresh_started_before_invalidation_does_not_land(app):
    invalidate_cluster_list_cache()
    with _cache_on(), _no_discovery(), \
            patch("api.k8s_provider._custom_clusters_as_items", return_value=[{"id": "custom-9", "name": "deleted"}]):
        list_clusters_from_k8s()
    generation = provider._cluster_list_cache["generation"]
    invalidate_cluster_list_cache()
    provider._store_cluster_list({"items": [{"id": "custom-9"}], "count": 1}, generation)
    assert provider._cluster_list_cache["payload"] is None
