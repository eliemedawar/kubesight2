from unittest.mock import MagicMock, patch

import pytest

from api.cluster_access import ClusterAccess
from api.upgrade_provider import (
    analyze_version_skew,
    detect_cluster_provider,
    generate_upgrade_plan,
    get_provider_support,
    mock_precheck,
    normalize_version,
    parse_k8s_version,
    required_confirmation_text,
    run_extended_prechecks,
    run_upgrade_workflow,
    validate_target_version,
)
from tests.conftest import auth_headers


def _access(context="docker-desktop", cluster_id="docker-desktop"):
    return ClusterAccess(cluster_id=cluster_id, context_name=context)


def _mock_kubectl_responses(responses: dict):
    def run_kubectl(access, args):
        key = " ".join(args)
        if key in responses:
            result = responses[key]
            if isinstance(result, Exception):
                raise result
            return result
        raise RuntimeError(f"Unexpected kubectl args: {args}")

    return run_kubectl


# --- Unit tests: version parsing ---


def test_parse_k8s_version():
    assert parse_k8s_version("v1.34.3") == (1, 34, 3)
    assert parse_k8s_version("1.28.0") == (1, 28, 0)
    assert parse_k8s_version("") == (0, 0, 0)


def test_normalize_version():
    assert normalize_version("1.31.0") == "v1.31.0"
    assert normalize_version("v1.31.0") == "v1.31.0"


def test_validate_target_version_rejects_invalid():
    ok, err = validate_target_version("")
    assert not ok
    ok, err = validate_target_version("not-a-version")
    assert not ok
    ok, _ = validate_target_version("v1.31.0")
    assert ok


# --- Provider detection ---


def test_detect_docker_desktop_from_context():
    access = _access("docker-desktop")
    run = _mock_kubectl_responses(
        {
            "get nodes -o json": '{"items": [{"metadata": {"name": "docker-desktop"}, "status": {"nodeInfo": {"kubeletVersion": "v1.34.3"}, "conditions": [{"type": "Ready", "status": "True"}]}}]}',
            "config view --minify -o json": '{"clusters": [{"cluster": {"server": "https://127.0.0.1:6443"}}]}',
        }
    )
    assert detect_cluster_provider(access, run, version_data={}, node_items=[]) == "docker-desktop"


def test_detect_kind_from_context():
    access = _access("kind-test")
    run = MagicMock(return_value="{}")
    assert detect_cluster_provider(access, run, version_data={}, node_items=[]) == "kind"


def test_detect_eks_from_server_url():
    access = _access("prod")
    nodes = [{"metadata": {"name": "node-1", "labels": {}}, "status": {"conditions": []}}]
    run = _mock_kubectl_responses(
        {
            "config view --minify -o json": '{"clusters": [{"cluster": {"server": "https://ABC.gr7.us-east-1.eks.amazonaws.com"}}]}',
        }
    )
    assert detect_cluster_provider(access, run, version_data={}, node_items=nodes) == "eks"


# --- Provider support ---


def test_docker_desktop_unsupported_upgrade():
    support = get_provider_support("docker-desktop")
    assert support["upgradeSupported"] is False
    assert "Docker Desktop" in support["reason"]
    assert support["executionMode"] == "instructions"


def test_generate_upgrade_plan_manual_for_docker_desktop():
    support = get_provider_support("docker-desktop")
    plan = generate_upgrade_plan(support)
    assert plan["manualUpgradeRequired"] is True
    assert len(plan["steps"]) == 7
    assert plan["steps"][0]["name"] == "Validate cluster readiness"


# --- Version skew ---


def test_version_skew_healthy():
    nodes = [
        {
            "metadata": {"name": "n1"},
            "status": {"nodeInfo": {"kubeletVersion": "v1.34.3"}},
        },
        {
            "metadata": {"name": "n2"},
            "status": {"nodeInfo": {"kubeletVersion": "v1.34.3"}},
        },
    ]
    result = analyze_version_skew("v1.34.3", nodes)
    assert result["status"] == "healthy"
    assert not result["warnings"]


def test_version_skew_warning_on_mismatch():
    nodes = [
        {
            "metadata": {"name": "n1"},
            "status": {"nodeInfo": {"kubeletVersion": "v1.33.0"}},
        },
    ]
    result = analyze_version_skew("v1.34.3", nodes)
    assert result["status"] == "warning"
    assert result["warnings"]


# --- Precheck execution with mocked kubectl ---


def test_precheck_execution_mock_kubectl():
    access = _access()
    version_json = (
        '{"serverVersion": {"gitVersion": "v1.34.3"}, "clientVersion": {"gitVersion": "v1.34.3"}}'
    )
    nodes_json = (
        '{"items": [{"metadata": {"name": "docker-desktop"}, "status": {"nodeInfo": {"kubeletVersion": "v1.34.3"}, '
        '"conditions": [{"type": "Ready", "status": "True"}]}}]}'
    )
    run = _mock_kubectl_responses(
        {
            "version -o json": version_json,
            "get nodes -o json": nodes_json,
            "get pods -n kube-system -o json": '{"items": []}',
            "top nodes --no-headers": "docker-desktop   10m   20%",
            "get pvc --all-namespaces -o json": '{"items": []}',
            "get storageclass -o json": '{"items": [{"metadata": {"name": "standard", "annotations": {"storageclass.kubernetes.io/is-default-class": "true"}}}]}',
            "get pods --all-namespaces -o json": '{"items": []}',
            "get pdb --all-namespaces -o json": '{"items": []}',
            "get apiservices -o json": '{"items": []}',
            "config view --minify -o json": '{"clusters": [{"cluster": {"server": "https://127.0.0.1:6443"}}]}',
        }
    )
    result = run_extended_prechecks(access, "v1.35.0", run)
    assert result["canUpgrade"] is True
    assert len(result["checks"]) >= 15
    assert result["provider"]["provider"] == "docker-desktop"
    assert result["provider"]["upgradeSupported"] is False


# --- Upgrade workflow ---


def test_upgrade_workflow_docker_desktop_manual_only():
    access = _access()
    version_json = '{"serverVersion": {"gitVersion": "v1.34.3"}}'
    nodes_json = (
        '{"items": [{"metadata": {"name": "docker-desktop"}, "status": {"nodeInfo": {"kubeletVersion": "v1.34.3"}, '
        '"conditions": [{"type": "Ready", "status": "True"}]}}]}'
    )
    run = _mock_kubectl_responses(
        {
            "version -o json": version_json,
            "get nodes -o json": nodes_json,
            "get pods -n kube-system -o json": '{"items": []}',
            "top nodes --no-headers": "ok",
            "get pvc --all-namespaces -o json": '{"items": []}',
            "get storageclass -o json": '{"items": []}',
            "get pods --all-namespaces -o json": '{"items": []}',
            "get pdb --all-namespaces -o json": '{"items": []}',
            "get apiservices -o json": '{"items": []}',
            "config view --minify -o json": '{"clusters": []}',
        }
    )
    result = run_upgrade_workflow(access, "v1.35.0", run)
    assert result["status"] == "manual_required"
    assert result["executionSupported"] is False
    assert "Docker Desktop" in result["message"] or "Manual" in result.get("provider", {}).get("reason", "")


def test_kubeadm_requires_confirmation():
    access = _access("kubeadm-cluster", "kubeadm-cluster")
    version_json = '{"serverVersion": {"gitVersion": "v1.30.0"}}'
    nodes_json = (
        '{"items": [{"metadata": {"name": "cp", "labels": {"node-role.kubernetes.io/control-plane": ""}}, '
        '"status": {"nodeInfo": {"kubeletVersion": "v1.30.0"}, "conditions": [{"type": "Ready", "status": "True"}]}}]}'
    )
    run = _mock_kubectl_responses(
        {
            "version -o json": version_json,
            "get nodes -o json": nodes_json,
            "get pods -n kube-system -o json": '{"items": []}',
            "top nodes --no-headers": "ok",
            "get pvc --all-namespaces -o json": '{"items": []}',
            "get storageclass -o json": '{"items": []}',
            "get pods --all-namespaces -o json": '{"items": []}',
            "get pdb --all-namespaces -o json": '{"items": []}',
            "get apiservices -o json": '{"items": []}',
            "config view --minify -o json": '{"clusters": []}',
        }
    )
    result = run_upgrade_workflow(access, "v1.31.0", run)
    assert result["status"] == "confirmation_required"
    expected = required_confirmation_text("kubeadm", "kubeadm-cluster", "v1.31.0")
    confirmed = run_upgrade_workflow(access, "v1.31.0", run, confirmation=expected)
    assert confirmed["status"] == "manual_required"
    assert confirmed["executionSupported"] is False


def test_required_confirmation_text_format():
    text = required_confirmation_text("kubeadm", "docker-desktop", "v1.35.0")
    assert text == "UPGRADE docker-desktop TO v1.35.0"


# --- API permission tests ---


def test_viewer_cannot_run_precheck(client, viewer_token):
    response = client.post(
        "/api/upgrades/precheck",
        headers=auth_headers(viewer_token),
        json={"clusterId": "prod-us-east", "targetVersion": "v1.31.0"},
    )
    assert response.status_code == 403


def test_operator_cannot_start_upgrade(client, operator_token):
    clusters = client.get("/api/clusters", headers=auth_headers(operator_token)).get_json()
    cluster_id = clusters["data"]["items"][0]["id"]
    response = client.post(
        "/api/upgrades/start",
        headers=auth_headers(operator_token),
        json={"clusterId": cluster_id, "targetVersion": "v1.31.0"},
    )
    assert response.status_code == 403


def test_admin_precheck_mock_mode(client, admin_token):
    clusters = client.get("/api/clusters", headers=auth_headers(admin_token)).get_json()
    cluster_id = clusters["data"]["items"][0]["id"]
    response = client.post(
        "/api/upgrades/precheck",
        headers=auth_headers(admin_token),
        json={"clusterId": cluster_id, "targetVersion": "v1.31.0"},
    )
    assert response.status_code == 200
    payload = response.get_json()
    assert payload["success"] is True
    data = payload["data"]
    assert "checks" in data
    assert len(data["checks"]) >= 15
    assert data["provider"]["provider"] == "docker-desktop"
    assert data["provider"]["upgradeSupported"] is False
    assert "upgradePlan" in data


def test_admin_upgrade_info_mock_mode(client, admin_token):
    clusters = client.get("/api/clusters", headers=auth_headers(admin_token)).get_json()
    cluster_id = clusters["data"]["items"][0]["id"]
    response = client.get(
        f"/api/upgrades/info?clusterId={cluster_id}&targetVersion=v1.31.0",
        headers=auth_headers(admin_token),
    )
    assert response.status_code == 200
    data = response.get_json()["data"]
    assert "clusterInfo" in data
    assert "versionSkew" in data
    assert data["clusterInfo"]["providerDisplay"] == "Docker Desktop"


def test_admin_start_returns_manual_required(client, admin_token):
    clusters = client.get("/api/clusters", headers=auth_headers(admin_token)).get_json()
    cluster_id = clusters["data"]["items"][0]["id"]
    response = client.post(
        "/api/upgrades/start",
        headers=auth_headers(admin_token),
        json={"clusterId": cluster_id, "targetVersion": "v1.31.0"},
    )
    assert response.status_code == 200
    data = response.get_json()["data"]
    assert data["status"] == "manual_required"
    assert data["executionSupported"] is False


def test_invalid_target_version_rejected(client, admin_token):
    clusters = client.get("/api/clusters", headers=auth_headers(admin_token)).get_json()
    cluster_id = clusters["data"]["items"][0]["id"]
    response = client.post(
        "/api/upgrades/precheck",
        headers=auth_headers(admin_token),
        json={"clusterId": cluster_id, "targetVersion": "bad-version"},
    )
    assert response.status_code == 400


def test_mock_precheck_includes_extended_checks():
    result = mock_precheck("docker-desktop", "v1.31.0", "docker-desktop")
    assert result["canUpgrade"] is True
    assert len(result["checks"]) == 15
