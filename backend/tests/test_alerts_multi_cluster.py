"""All-cluster alert list: one cluster failing no longer hides the others."""

from unittest.mock import patch

import pytest

from api.cluster_access import ClusterAccess
from api.k8s_provider import K8sCommandError, list_alerts_for_clusters, list_alerts_from_k8s


def _access(cid):
    return ClusterAccess(cluster_id=cid, context_name=cid, kubeconfig_path=None, display_name=cid, is_custom=True)


def _scan(access, cid):
    if cid == "custom-2":
        raise K8sCommandError("Cluster API is unreachable")
    return {"items": [{"id": f"{cid}:a", "clusterId": cid}], "metadata": {"hasLiveAlertsSource": True}}


def test_failing_cluster_maps_to_none(app):
    with app.app_context(), \
            patch("api.k8s_provider.resolve_cluster_access", side_effect=_access), \
            patch("api.k8s_provider.list_alerts_for_access", side_effect=_scan):
        results = list_alerts_for_clusters(["custom-1", "custom-2", "custom-3"])
    assert results["custom-2"] is None
    assert results["custom-1"]["items"][0]["clusterId"] == "custom-1"


def test_all_cluster_view_keeps_reachable_clusters(app):
    clusters = {"items": [{"id": "custom-1"}, {"id": "custom-2"}, {"id": "custom-3"}]}
    with app.app_context(), \
            patch("api.k8s_provider.list_clusters_from_k8s", return_value=clusters), \
            patch("api.k8s_provider.resolve_cluster_access", side_effect=_access), \
            patch("api.k8s_provider.list_alerts_for_access", side_effect=_scan):
        payload = list_alerts_from_k8s(None)
    assert {i["clusterId"] for i in payload["items"]} == {"custom-1", "custom-3"}
    assert payload["metadata"]["unavailableClusters"] == ["custom-2"]


def test_all_cluster_view_still_fails_when_every_cluster_does(app):
    clusters = {"items": [{"id": "custom-2"}]}
    with app.app_context(), \
            patch("api.k8s_provider.list_clusters_from_k8s", return_value=clusters), \
            patch("api.k8s_provider.resolve_cluster_access", side_effect=_access), \
            patch("api.k8s_provider.list_alerts_for_access", side_effect=_scan):
        with pytest.raises(K8sCommandError):
            list_alerts_from_k8s(None)
