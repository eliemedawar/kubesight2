"""Tests for inventory deployment operational actions."""

from unittest.mock import patch

from tests.conftest import auth_headers

ACTION_BODY = {
    "clusterId": "prod-us-east",
    "namespace": "payments",
    "workloadType": "deployment",
    "workloadName": "payments-api",
}

MOCK_HISTORY_OUTPUT = """deployment.apps/payments-api
REVISION  CHANGE-CAUSE
1         <none>
2         kubectl set image
3         kubectl apply
"""


def test_admin_restart_deployment_mock(client, admin_token):
    response = client.post(
        "/api/inventory/actions/restart",
        headers=auth_headers(admin_token),
        json=ACTION_BODY,
    )
    assert response.status_code == 200
    data = response.get_json()["data"]
    assert data["restarted"] is True
    assert data["workloadName"] == "payments-api"
    assert "restarted" in data["output"]


def test_admin_scale_deployment_mock(client, admin_token):
    response = client.post(
        "/api/inventory/actions/scale",
        headers=auth_headers(admin_token),
        json={**ACTION_BODY, "replicas": 5},
    )
    assert response.status_code == 200
    data = response.get_json()["data"]
    assert data["scaled"] is True
    assert data["replicas"] == 5


def test_admin_rollback_deployment_mock(client, admin_token):
    response = client.post(
        "/api/inventory/actions/rollback",
        headers=auth_headers(admin_token),
        json={**ACTION_BODY, "revision": 2},
    )
    assert response.status_code == 200
    data = response.get_json()["data"]
    assert data["rolledBack"] is True
    assert data["revision"] == 2


def test_rollout_history_mock(client, admin_token):
    response = client.get(
        "/api/inventory/actions/rollout-history",
        headers=auth_headers(admin_token),
        query_string={
            "clusterId": "prod-us-east",
            "namespace": "payments",
            "workloadName": "payments-api",
        },
    )
    assert response.status_code == 200
    data = response.get_json()["data"]
    assert data["deployment"] == "payments-api"
    assert len(data["revisions"]) >= 2
    assert data["revisions"][0]["revision"] == 1


def test_scale_replicas_validation(client, admin_token):
    bad = client.post(
        "/api/inventory/actions/scale",
        headers=auth_headers(admin_token),
        json={**ACTION_BODY, "replicas": 100},
    )
    assert bad.status_code == 400

    missing = client.post(
        "/api/inventory/actions/scale",
        headers=auth_headers(admin_token),
        json=ACTION_BODY,
    )
    assert missing.status_code == 400


def test_unsupported_workload_type(client, admin_token):
    response = client.post(
        "/api/inventory/actions/restart",
        headers=auth_headers(admin_token),
        json={**ACTION_BODY, "workloadType": "statefulset"},
    )
    assert response.status_code == 400


def test_viewer_cannot_restart(client, viewer_token):
    response = client.post(
        "/api/inventory/actions/restart",
        headers=auth_headers(viewer_token),
        json=ACTION_BODY,
    )
    assert response.status_code == 403


def test_operator_cannot_restart(client, operator_token):
    response = client.post(
        "/api/inventory/actions/restart",
        headers=auth_headers(operator_token),
        json=ACTION_BODY,
    )
    assert response.status_code == 403


def test_operator_cannot_scale(client, operator_token):
    response = client.post(
        "/api/inventory/actions/scale",
        headers=auth_headers(operator_token),
        json={**ACTION_BODY, "replicas": 3},
    )
    assert response.status_code == 403


def test_operator_cannot_rollback(client, operator_token):
    response = client.post(
        "/api/inventory/actions/rollback",
        headers=auth_headers(operator_token),
        json=ACTION_BODY,
    )
    assert response.status_code == 403


def test_viewer_cannot_view_rollout_history(client, viewer_token):
    response = client.get(
        "/api/inventory/actions/rollout-history",
        headers=auth_headers(viewer_token),
        query_string={
            "clusterId": "prod-us-east",
            "namespace": "payments",
            "workloadName": "payments-api",
        },
    )
    assert response.status_code == 403


def test_restart_kubectl_invocation(client, admin_token):
    with patch(
        "api.services.inventory_actions_service._run_kubectl_for_cluster",
        return_value="deployment.apps/payments-api restarted",
    ) as mock_run:
        with patch("api.services.inventory_actions_service.should_use_real_k8s", return_value=True):
            with patch("api.services.inventory_actions_service.resolve_cluster_access") as mock_access:
                mock_access.return_value = object()
                response = client.post(
                    "/api/inventory/actions/restart",
                    headers=auth_headers(admin_token),
                    json=ACTION_BODY,
                )
    assert response.status_code == 200
    mock_run.assert_called_once()
    args = mock_run.call_args[0][1]
    assert args[:3] == ["rollout", "restart", "deployment/payments-api"]
    assert "-n" in args and "payments" in args


def test_scale_kubectl_invocation(client, admin_token):
    with patch(
        "api.services.inventory_actions_service._run_kubectl_for_cluster",
        return_value="deployment.apps/payments-api scaled",
    ) as mock_run:
        with patch("api.services.inventory_actions_service.should_use_real_k8s", return_value=True):
            with patch("api.services.inventory_actions_service.resolve_cluster_access") as mock_access:
                mock_access.return_value = object()
                response = client.post(
                    "/api/inventory/actions/scale",
                    headers=auth_headers(admin_token),
                    json={**ACTION_BODY, "replicas": 8},
                )
    assert response.status_code == 200
    args = mock_run.call_args[0][1]
    assert args[0] == "scale"
    assert "deployment/payments-api" in args
    assert "--replicas=8" in args


def test_rollback_with_revision_kubectl(client, admin_token):
    with patch(
        "api.services.inventory_actions_service._run_kubectl_for_cluster",
        return_value="deployment.apps/payments-api rolled back",
    ) as mock_run:
        with patch("api.services.inventory_actions_service.should_use_real_k8s", return_value=True):
            with patch("api.services.inventory_actions_service.resolve_cluster_access") as mock_access:
                mock_access.return_value = object()
                response = client.post(
                    "/api/inventory/actions/rollback",
                    headers=auth_headers(admin_token),
                    json={**ACTION_BODY, "revision": 2},
                )
    assert response.status_code == 200
    args = mock_run.call_args[0][1]
    assert "--to-revision=2" in args


def test_rollout_history_parsing(client, admin_token):
    with patch(
        "api.services.inventory_actions_service._run_kubectl_for_cluster",
        return_value=MOCK_HISTORY_OUTPUT,
    ):
        with patch("api.services.inventory_actions_service.should_use_real_k8s", return_value=True):
            with patch("api.services.inventory_actions_service.resolve_cluster_access") as mock_access:
                mock_access.return_value = object()
                response = client.get(
                    "/api/inventory/actions/rollout-history",
                    headers=auth_headers(admin_token),
                    query_string={
                        "clusterId": "prod-us-east",
                        "namespace": "payments",
                        "workloadName": "payments-api",
                    },
                )
    assert response.status_code == 200
    revisions = response.get_json()["data"]["revisions"]
    assert len(revisions) == 3
    assert revisions[2]["revision"] == 3


def test_action_blocked_without_namespace_access(client, admin_token):
    with patch("api.services.inventory_actions_service.can_access_namespace", return_value=False):
        response = client.post(
            "/api/inventory/actions/restart",
            headers=auth_headers(admin_token),
            json=ACTION_BODY,
        )
    assert response.status_code == 403


def test_parse_rollout_history_unit():
    from api.services.inventory_actions_service import parse_rollout_history

    parsed = parse_rollout_history(MOCK_HISTORY_OUTPUT)
    assert parsed["deployment"] == "payments-api"
    assert len(parsed["revisions"]) == 3
    assert parsed["revisions"][1]["changeCause"] == "kubectl set image"
