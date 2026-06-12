from unittest.mock import MagicMock, patch

import pytest

from api.cluster_access import ClusterAccess
from api.upgrade_provider import (
    analyze_version_skew,
    build_target_version_options,
    build_version_info,
    detect_cluster_provider,
    generate_upgrade_plan,
    get_provider_support,
    kubeadm_minor_jump_blocked,
    mock_precheck,
    normalize_version,
    parse_k8s_version,
    recommended_kubeadm_target,
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


@patch("api.upgrade_provider._fetch_latest_patch_for_minor", return_value="v1.33.4")
def test_recommended_kubeadm_target(mock_fetch):
    assert recommended_kubeadm_target("v1.32.13") == "v1.33.4"
    mock_fetch.assert_called_once_with(1, 33)


def test_kubeadm_minor_jump_blocked():
    message = kubeadm_minor_jump_blocked("v1.32.13", "v1.36.1")
    assert message is not None
    assert "one minor version" in message
    assert kubeadm_minor_jump_blocked("v1.32.13", "v1.33.0") is None


@patch("api.upgrade_provider._fetch_latest_patch_for_minor", return_value="v1.33.4")
def test_build_target_version_options_for_kubeadm(mock_fetch):
    options = build_target_version_options("v1.32.13", "v1.36.1", "kubeadm")
    assert options[0]["value"] == "v1.33.4"
    assert options[0]["recommended"] is True
    assert any(option["value"] == "v1.36.1" for option in options)


@patch("api.upgrade_provider.recommended_kubeadm_target", return_value="v1.33.4")
@patch("api.upgrade_provider._fetch_latest_k8s_version", return_value="v1.36.1")
def test_build_version_info_includes_recommended_target(mock_latest, mock_recommended):
    info = build_version_info("v1.32.13", "v1.36.1", get_provider_support("kubeadm"))
    assert info["recommendedTarget"] == "v1.33.4"
    assert info["targetOptions"][0]["value"] == "v1.33.4"


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


def test_detect_kind_from_provider_id():
    access = _access("docker-desktop")
    nodes = [
        {
            "metadata": {"name": "desktop-control-plane", "labels": {}},
            "spec": {"providerID": "kind://docker/desktop/desktop-control-plane"},
            "status": {"conditions": []},
        }
    ]
    run = MagicMock(return_value="{}")
    assert detect_cluster_provider(access, run, version_data={}, node_items=nodes) == "kind"


def test_run_upgrade_kubectl_forces_default_namespace():
    from api.upgrade_executor import _run_upgrade_kubectl

    access = _access()
    captured = []

    def run_kubectl(_access, args):
        captured.append(args)
        return "ok"

    _run_upgrade_kubectl(access, run_kubectl, ["cordon", "worker-1"])
    assert captured[0][:2] == ["--namespace", "default"]


def test_verify_server_version_rejects_unchanged_version():
    from api.upgrade_executor import _verify_server_version

    access = _access("kubeadm-cluster", "kubeadm-cluster")
    run = _mock_kubectl_responses(
        {
            "--namespace default version -o json": '{"serverVersion": {"gitVersion": "v1.32.13"}}',
        }
    )
    with pytest.raises(RuntimeError, match="Post-upgrade verification failed"):
        _verify_server_version(
            access,
            run,
            "v1.33.4",
            previous_version="v1.32.13",
        )


def test_kubeadm_package_install_scripts_include_target_version():
    from api.upgrade_executor import (
        _shell_script_control_plane_upgrade,
        _shell_script_install_kubeadm_only,
        _shell_script_install_kubelet_kubectl,
        _shell_script_upgrade_worker_node,
    )

    kubeadm_script = _shell_script_install_kubeadm_only("v1.33.0")
    kubelet_script = _shell_script_install_kubelet_kubectl("v1.33.0")
    worker_script = _shell_script_upgrade_worker_node("v1.33.0")
    assert 'K8S_VER="1.33.0"' in kubeadm_script
    assert "_apt install" in kubeadm_script
    assert "kubeadm upgrade node" in worker_script
    assert "systemctl restart kubelet" in kubelet_script

    cp_script = _shell_script_control_plane_upgrade("v1.33.0")
    assert "kubeadm upgrade apply" in cp_script
    assert "_wait_for_apt" in cp_script
    assert "DPkg::Lock::Timeout=600" in cp_script


def test_fetch_latest_patch_for_minor_skips_prerelease(monkeypatch):
    from api.upgrade_provider import _fetch_latest_patch_for_minor

    monkeypatch.setattr(
        "api.upgrade_provider._fetch_release_text",
        lambda url: "v1.33.0-rc.1" if "latest-1.33" in url else None,
    )
    monkeypatch.setattr("api.upgrade_provider._release_binary_exists", lambda version: version == "v1.33.0")
    assert _fetch_latest_patch_for_minor(1, 33) == "v1.33.0"


def test_control_plane_nodes_with_empty_label_value():
    from api.upgrade_executor import _control_plane_nodes

    nodes = [
        {
            "metadata": {
                "name": "desktop-control-plane",
                "labels": {"node-role.kubernetes.io/control-plane": ""},
            }
        }
    ]
    assert _control_plane_nodes(nodes) == ["desktop-control-plane"]


def test_detect_docker_desktop_from_api_server_url():
    access = _access("docker-desktop")
    nodes = [
        {
            "metadata": {"name": "desktop-control-plane", "labels": {}},
            "status": {
                "nodeInfo": {"kubeletVersion": "v1.34.3"},
                "conditions": [{"type": "Ready", "status": "True"}],
            },
        }
    ]
    run = _mock_kubectl_responses(
        {
            "config view --minify -o json": (
                '{"clusters": [{"cluster": {"server": "https://kubernetes.docker.internal:6443"}}]}'
            ),
        }
    )
    assert detect_cluster_provider(access, run, version_data={}, node_items=nodes) == "docker-desktop"


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


def test_kubeadm_auto_upgrade_starts_job(monkeypatch):
    monkeypatch.setenv("KUBESIGHT_AUTO_UPGRADE", "true")
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
    expected = required_confirmation_text("kubeadm", "kubeadm-cluster", "v1.31.0")
    confirmed = run_upgrade_workflow(access, "v1.31.0", run, confirmation=expected)
    assert confirmed["status"] == "running"
    assert confirmed["executionSupported"] is True
    assert confirmed.get("jobId") or confirmed.get("upgradeId")


def test_kubeadm_without_auto_upgrade_returns_manual_plan(monkeypatch):
    monkeypatch.delenv("KUBESIGHT_AUTO_UPGRADE", raising=False)
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
    assert result["status"] == "manual_required"
    assert result["executionSupported"] is False


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
