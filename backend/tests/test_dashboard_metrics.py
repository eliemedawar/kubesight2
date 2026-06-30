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
            with patch("api.services.dashboard_service._load_real_k8s_dashboard_data") as mock_load:
                mock_load.return_value = (
                    {
                        "pods": {"running": 1, "pending": 0, "failed": 0},
                        "updatedAt": "2026-01-01T00:00:00+00:00",
                    },
                    [],
                    {},
                    [],
                    unavailable,
                    unavailable,
                    None,
                    None,
                    "unknown",
                    False,
                    [],
                )
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
