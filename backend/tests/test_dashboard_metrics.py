from unittest.mock import patch

from api.cluster_access import ClusterAccess
from tests.conftest import auth_headers


def test_dashboard_missing_metrics_server(client, admin_token):
    unavailable = {
        "available": False,
        "title": "Metrics unavailable",
        "reason": "Metrics Server is not installed or accessible.",
        "helpText": "Install Metrics Server to enable utilization monitoring.",
    }
    with patch("api.services.dashboard_service.should_use_real_k8s", return_value=True):
        with patch("api.services.dashboard_service.resolve_cluster_access") as mock_access:
            mock_access.return_value = ClusterAccess(
                cluster_id="c1",
                context_name="c1",
                display_name="c1",
            )
            with patch("api.services.dashboard_service.cluster_overview_from_k8s") as mock_overview:
                mock_overview.return_value = {
                    "pods": {"running": 1, "pending": 0, "failed": 0},
                    "updatedAt": "2026-01-01T00:00:00+00:00",
                }
                with patch("api.k8s_provider.list_namespaces_from_k8s", return_value={"items": []}):
                    with patch("api.k8s_provider.list_alerts_for_access", return_value={"items": []}):
                        with patch("api.upgrade_provider.build_cluster_info", return_value={}):
                            with patch(
                                "api.k8s_metrics.cluster_utilization_metrics",
                                return_value=(unavailable, unavailable),
                            ):
                                response = client.get(
                                    "/api/dashboard/summary?clusterId=c1",
                                    headers=auth_headers(admin_token),
                                )
    assert response.status_code == 200
    data = response.get_json()["data"]
    assert data["cpuUsage"]["available"] is False
    assert data["memoryUsage"]["available"] is False
    assert "percent" not in data["cpuUsage"]
    assert data["cpuUsage"]["helpText"]
