"""Tests for RESTful pod logs endpoints."""

from unittest.mock import patch

from tests.conftest import auth_headers

PODS_URL = "/api/clusters/prod-us-east/namespaces/payments/pods"
POD_NAME = "payments-api-84b5d5"
CONTAINER_NAME = "payments"
CONTAINERS_URL = f"/api/clusters/prod-us-east/namespaces/payments/pods/{POD_NAME}/containers"
LOGS_URL = (
    f"/api/clusters/prod-us-east/namespaces/payments/pods/{POD_NAME}/containers/{CONTAINER_NAME}/logs"
)


def test_list_namespace_pods_for_logs_mock_mode(client, admin_token):
    response = client.get(PODS_URL, headers=auth_headers(admin_token))
    assert response.status_code == 200
    data = response.get_json()["data"]
    assert data["namespace"] == "payments"
    assert data["count"] >= 1
    assert all("canViewLogs" in pod for pod in data["items"])


def test_list_pod_containers_mock_mode(client, admin_token):
    response = client.get(CONTAINERS_URL, headers=auth_headers(admin_token))
    assert response.status_code == 200
    data = response.get_json()["data"]
    assert data["pod"] == POD_NAME
    assert data["count"] >= 1


def test_container_logs_default_time_range(client, admin_token):
    response = client.get(
        LOGS_URL,
        query_string={"sinceSeconds": "900", "tailLines": "200", "timestamps": "true"},
        headers=auth_headers(admin_token),
    )
    assert response.status_code == 200
    data = response.get_json()["data"]
    assert data["query"]["sinceSeconds"] == 900
    assert data["query"]["tailLines"] == 200
    assert data["lines"]


def test_container_logs_rejects_follow(client, admin_token):
    response = client.get(
        LOGS_URL,
        query_string={"follow": "true"},
        headers=auth_headers(admin_token),
    )
    assert response.status_code == 400
    assert "follow" in response.get_json()["error"].lower()


@patch("api.services.logs_service.pod_logs_from_k8s")
@patch("api.services.logs_service.resolve_cluster_access")
@patch("api.services.logs_service.should_use_real_k8s", return_value=True)
def test_container_logs_passes_tail_lines(
    _mock_real,
    mock_resolve,
    mock_pod_logs,
    client,
    admin_token,
):
    mock_resolve.return_value = object()
    mock_pod_logs.return_value = {"query": {}, "stream": "snapshot", "lines": ["line"]}
    response = client.get(
        LOGS_URL,
        query_string={"tailLines": "500", "timestamps": "true"},
        headers=auth_headers(admin_token),
    )
    assert response.status_code == 200
    mock_pod_logs.assert_called_once()
    assert mock_pod_logs.call_args.kwargs["tail_lines"] == 500


def test_list_pods_not_found_cluster(client, admin_token):
    response = client.get(
        "/api/clusters/unknown-cluster/namespaces/default/pods",
        headers=auth_headers(admin_token),
    )
    assert response.status_code == 404
