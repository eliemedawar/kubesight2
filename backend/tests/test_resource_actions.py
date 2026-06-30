"""Tests for Resources-page restart action (pods + workloads)."""

from tests.conftest import auth_headers

CLUSTER = "prod-us-east"
NAMESPACE = "payments"


def _restart_url(kind: str, name: str) -> str:
    return f"/api/clusters/{CLUSTER}/namespaces/{NAMESPACE}/resources/{kind}/{name}/restart"


def test_admin_restart_pod_mock(client, admin_token):
    response = client.post(
        _restart_url("pod", "payments-api-abc123"),
        headers=auth_headers(admin_token),
    )
    assert response.status_code == 200
    data = response.get_json()["data"]
    assert data["restarted"] is True
    assert data["kind"] == "pod"
    assert "deleted" in data["output"]


def test_admin_rollout_restart_deployment_mock(client, admin_token):
    response = client.post(
        _restart_url("deployment", "payments-api"),
        headers=auth_headers(admin_token),
    )
    assert response.status_code == 200
    data = response.get_json()["data"]
    assert data["restarted"] is True
    assert data["kind"] == "deployment"
    assert "restarted" in data["output"]


def test_restart_unsupported_kind_rejected(client, admin_token):
    response = client.post(
        _restart_url("service", "payments-api"),
        headers=auth_headers(admin_token),
    )
    assert response.status_code == 400


def _yaml_url(kind: str, name: str) -> str:
    return f"/api/clusters/{CLUSTER}/namespaces/{NAMESPACE}/resources/{kind}/{name}/yaml"


def test_admin_configmap_yaml_mock(client, admin_token):
    response = client.get(_yaml_url("configmap", "app-config"), headers=auth_headers(admin_token))
    assert response.status_code == 200
    data = response.get_json()["data"]
    assert data["kind"] == "configmap"
    assert "kind: ConfigMap" in data["yaml"]


def test_admin_secret_yaml_mock(client, admin_token):
    response = client.get(_yaml_url("secret", "app-secret"), headers=auth_headers(admin_token))
    assert response.status_code == 200
    data = response.get_json()["data"]
    assert data["kind"] == "secret"
    assert "kind: Secret" in data["yaml"]
