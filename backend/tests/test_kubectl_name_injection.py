"""kubectl flag injection through user-supplied Kubernetes names (H3).

kubectl parses flags even after positional arguments, so a pod "name" like
``--server=https://evil`` would send the cluster bearer token to an attacker,
``--all`` on a restart would delete every pod. Every entry point must refuse
such names with a 400 (or a tool error) and never spawn kubectl with them.

The kubectl subprocess is replaced by a recorder in real mode, so these tests
prove both halves: the refusal, and that no argv containing the name was run.
"""

from __future__ import annotations

import subprocess
from contextlib import ExitStack
from unittest.mock import patch
from urllib.parse import quote

import pytest

from api.cluster_access import ClusterAccess
from api.k8s_names import (
    K8sNameError,
    unsafe_helm_arg,
    unsafe_kubectl_arg,
    validate_container_name,
    validate_namespace,
    validate_resource_name,
)
from tests.conftest import auth_headers

CLUSTER = "prod-us-east"
NAMESPACE = "payments"
POD = "payments-api-84b5d5"
CONTAINER = "payments"

BAD_NAMES = ["--server=https://evil", "--server=evil.example:443", "-A", "--all", "a b", "x;rm", "UPPER"]


def _refused_path(response, bad: str) -> bool:
    """A path segment with '/' in it (%2F decodes) no longer matches the route
    at all (404, still nothing run); every other bad name is a clean 400."""
    return response.status_code == (404 if "/" in bad else 400)

ACCESS = ClusterAccess(cluster_id=CLUSTER, context_name="ctx", kubeconfig_path=None)


class KubectlRecorder:
    """Stands in for subprocess.run / Popen and records every kubectl argv."""

    def __init__(self, stdout: str = "{}"):
        self.calls = []
        self.stdout = stdout

    def run(self, command, *args, **kwargs):
        self.calls.append(list(command))
        return subprocess.CompletedProcess(command, 0, stdout=self.stdout, stderr="")

    def popen(self, command, *args, **kwargs):  # pragma: no cover - must not be reached
        self.calls.append(list(command))
        raise AssertionError("kubectl logs -f must not be spawned in these tests")

    def saw(self, value: str) -> bool:
        return any(value in call for call in self.calls)


@pytest.fixture()
def kubectl():
    """Real mode everywhere relevant, with kubectl replaced by a recorder."""
    recorder = KubectlRecorder()
    real = lambda *_a, **_k: True  # noqa: E731
    resolve = lambda *_a, **_k: ACCESS  # noqa: E731
    targets = {
        "api.services.logs_service.should_use_real_k8s": real,
        "api.services.logs_service.resolve_cluster_access": resolve,
        "api.services.resource_actions_service.should_use_real_k8s": real,
        "api.services.resource_actions_service.resolve_cluster_access": resolve,
        "api.services.inventory_actions_service.should_use_real_k8s": real,
        "api.services.inventory_actions_service.resolve_cluster_access": resolve,
        "api.services.deployment_service.resolve_cluster_access": resolve,
        "api.k8s_provider.subprocess.run": recorder.run,
        "api.k8s_provider.subprocess.Popen": recorder.popen,
    }
    with ExitStack() as stack:
        for target, value in targets.items():
            stack.enter_context(patch(target, value))
        yield recorder


def _seg(value: str) -> str:
    return quote(value, safe="")


# ---------------------------------------------------------------------------
# The validator
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("bad", BAD_NAMES + ["", " ", "-x", "a..", ".a", "a/b", "x\n"])
def test_validator_refuses_bad_names(bad):
    with pytest.raises(K8sNameError):
        validate_resource_name(bad)
    with pytest.raises(K8sNameError):
        validate_namespace(bad)
    with pytest.raises(K8sNameError):
        validate_container_name(bad)


def test_validator_accepts_real_names():
    assert validate_resource_name("payment-service-7d9f8b6c4-x2k9p") == "payment-service-7d9f8b6c4-x2k9p"
    assert validate_resource_name("my.dotted.name") == "my.dotted.name"
    assert validate_resource_name("a" * 253)
    assert validate_namespace("kube-system") == "kube-system"
    assert validate_container_name("payments") == "payments"
    with pytest.raises(K8sNameError):
        validate_resource_name("a" * 254)
    with pytest.raises(K8sNameError):
        validate_namespace("a" * 64)
    with pytest.raises(K8sNameError):
        validate_namespace("dotted.ns")  # labels have no dots


@pytest.mark.parametrize(
    "args",
    [
        ["logs", "--server=https://evil", "-n", "ns"],
        ["logs", "pod", "-n", "ns", "-s", "https://evil"],
        ["get", "pod", "x", "--kubeconfig=/tmp/k"],
        ["get", "pod", "x", "--as=system:admin"],
        ["get", "pod", "x", "--token", "t"],
        ["delete", "pod", "--all", "-n", "ns"],
        ["delete", "pod", "x", "-A"],
        ["get", "pods", "-n", "--server=https://evil"],
        ["get", "pods", "--namespace=Bad_NS"],
        ["logs", "pod", "-n", "ns", "-c", "--as=x"],
        ["set", "env", "deployment/x", "--server=https://evil=1", "-n", "ns"],
    ],
)
def test_runner_guard_refuses_injected_flags(args):
    assert unsafe_kubectl_arg(args) is not None


@pytest.mark.parametrize(
    "args",
    [
        ["get", "pods", "-A", "-o", "json"],
        ["get", "pods", "--all-namespaces", "-o", "json"],
        ["logs", "pod-1", "-n", "ns", "--tail=200", "-c", "app", "--since-time", "2026-01-01T00:00:00Z"],
        ["exec", "pod-1", "-n", "ns", "-c", "app", "--", "sh", "-c", "echo --server=x -s"],
        ["rollout", "restart", "deployment/api", "-n", "ns"],
        ["set", "env", "deployment/api", "FOO=bar", "-n", "ns", "-c", "a,b"],
        ["--namespace", "default", "get", "pods"],
        ["apply", "-f", "-"],
    ],
)
def test_runner_guard_allows_legitimate_argv(args):
    assert unsafe_kubectl_arg(args) is None


def test_helm_guard():
    assert unsafe_helm_arg(["uninstall", "--kube-apiserver=https://evil", "--namespace", "ns"])
    assert unsafe_helm_arg(["status", "rel", "-n", "--kube-token=x"])
    assert unsafe_helm_arg(["install", "r", "c", "--post-renderer", "/bin/sh"])
    assert unsafe_helm_arg(["template", "r", "c", "--kube-version", "1.30", "-n", "ns"]) is None


def test_run_kubectl_refuses_before_spawning(kubectl):
    from api.k8s_provider import K8sCommandError, _run_for_access

    with pytest.raises(K8sCommandError):
        _run_for_access(ACCESS, ["logs", "--server=https://evil", "-n", NAMESPACE])
    assert kubectl.calls == []


# ---------------------------------------------------------------------------
# Entry points: logs
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("bad", BAD_NAMES)
def test_legacy_logs_query_refuses_bad_pod(client, admin_token, kubectl, bad):
    response = client.get(
        "/api/logs",
        query_string={"cluster": CLUSTER, "namespace": NAMESPACE, "pod": bad},
        headers=auth_headers(admin_token),
    )
    assert response.status_code == 400, response.get_data(as_text=True)
    assert "Invalid pod name" in response.get_json()["error"]
    assert kubectl.calls == []


@pytest.mark.parametrize("bad", BAD_NAMES)
def test_legacy_logs_query_refuses_bad_container(client, admin_token, kubectl, bad):
    response = client.get(
        "/api/logs",
        query_string={"cluster": CLUSTER, "namespace": NAMESPACE, "pod": POD, "container": bad},
        headers=auth_headers(admin_token),
    )
    assert response.status_code == 400
    assert kubectl.calls == []


@pytest.mark.parametrize("bad", BAD_NAMES)
def test_logs_path_refuses_bad_pod(client, admin_token, kubectl, bad):
    url = f"/api/clusters/{CLUSTER}/namespaces/{NAMESPACE}/pods/{_seg(bad)}/containers/{CONTAINER}/logs"
    response = client.get(url, headers=auth_headers(admin_token))
    assert _refused_path(response, bad), response.get_data(as_text=True)
    assert kubectl.calls == []


@pytest.mark.parametrize("bad", BAD_NAMES)
def test_logs_stream_refuses_bad_container(client, admin_token, kubectl, bad):
    url = f"/api/clusters/{CLUSTER}/namespaces/{NAMESPACE}/pods/{POD}/containers/{_seg(bad)}/logs/stream"
    response = client.get(url, headers=auth_headers(admin_token))
    assert _refused_path(response, bad), response.get_data(as_text=True)
    assert kubectl.calls == []


@pytest.mark.parametrize("bad", BAD_NAMES)
def test_namespace_segment_is_refused(client, admin_token, kubectl, bad):
    url = f"/api/clusters/{CLUSTER}/namespaces/{_seg(bad)}/pods"
    response = client.get(url, headers=auth_headers(admin_token))
    assert _refused_path(response, bad), response.get_data(as_text=True)
    assert kubectl.calls == []


def test_stream_provider_refuses_before_spawning(kubectl):
    from api.k8s_provider import K8sCommandError, stream_pod_log_lines

    with pytest.raises(K8sCommandError):
        next(stream_pod_log_lines(access=ACCESS, namespace=NAMESPACE, pod="--all", container=None))
    assert kubectl.calls == []


def test_a_normal_pod_still_gets_its_logs(client, admin_token, kubectl):
    kubectl.stdout = "2026-09-27T10:00:00Z hello\n"
    url = f"/api/clusters/{CLUSTER}/namespaces/{NAMESPACE}/pods/{POD}/containers/{CONTAINER}/logs"
    response = client.get(url, headers=auth_headers(admin_token))
    assert response.status_code == 200, response.get_data(as_text=True)
    assert len(kubectl.calls) == 1
    argv = kubectl.calls[0]
    assert argv[0] == "kubectl"
    assert "logs" in argv and POD in argv and CONTAINER in argv
    assert not any(token.startswith("--server") for token in argv)


# ---------------------------------------------------------------------------
# Entry points: resources (restart / exec / describe / yaml / rollout history)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("bad", BAD_NAMES)
def test_restart_refuses_bad_name(client, admin_token, kubectl, bad):
    url = f"/api/clusters/{CLUSTER}/namespaces/{NAMESPACE}/resources/pod/{_seg(bad)}/restart"
    response = client.post(url, headers=auth_headers(admin_token))
    assert _refused_path(response, bad), response.get_data(as_text=True)
    assert kubectl.calls == []


@pytest.mark.parametrize("bad", BAD_NAMES)
def test_exec_refuses_bad_pod(client, admin_token, kubectl, bad):
    url = f"/api/clusters/{CLUSTER}/namespaces/{NAMESPACE}/pods/{_seg(bad)}/exec"
    response = client.post(url, json={"command": "id"}, headers=auth_headers(admin_token))
    assert _refused_path(response, bad), response.get_data(as_text=True)
    assert kubectl.calls == []


@pytest.mark.parametrize("bad", BAD_NAMES)
def test_exec_refuses_bad_container(client, admin_token, kubectl, bad):
    url = f"/api/clusters/{CLUSTER}/namespaces/{NAMESPACE}/pods/{POD}/exec"
    response = client.post(
        url, json={"command": "id", "container": bad}, headers=auth_headers(admin_token)
    )
    assert response.status_code == 400
    assert kubectl.calls == []


@pytest.mark.parametrize("view", ["describe", "yaml"])
@pytest.mark.parametrize("bad", BAD_NAMES)
def test_describe_and_yaml_refuse_bad_name(client, admin_token, kubectl, bad, view):
    url = f"/api/clusters/{CLUSTER}/namespaces/{NAMESPACE}/resources/deployment/{_seg(bad)}/{view}"
    response = client.get(url, headers=auth_headers(admin_token))
    assert _refused_path(response, bad), response.get_data(as_text=True)
    assert kubectl.calls == []


def test_unknown_kind_is_refused(client, admin_token, kubectl):
    url = f"/api/clusters/{CLUSTER}/namespaces/{NAMESPACE}/resources/{_seg('--all')}/x/describe"
    response = client.get(url, headers=auth_headers(admin_token))
    assert response.status_code == 400
    assert kubectl.calls == []


@pytest.mark.parametrize("bad", BAD_NAMES)
def test_rollout_history_path_refuses_bad_name(client, admin_token, kubectl, bad):
    url = f"/api/clusters/{CLUSTER}/namespaces/{NAMESPACE}/deployments/{_seg(bad)}/rollout-history"
    response = client.get(url, headers=auth_headers(admin_token))
    assert _refused_path(response, bad), response.get_data(as_text=True)
    assert kubectl.calls == []


@pytest.mark.parametrize("bad", BAD_NAMES)
def test_inventory_actions_refuse_bad_workload(client, admin_token, kubectl, bad):
    body = {"clusterId": CLUSTER, "namespace": NAMESPACE, "workloadType": "deployment", "workloadName": bad}
    for path, extra in (("restart", {}), ("scale", {"replicas": 2}), ("rollback", {})):
        response = client.post(
            f"/api/inventory/actions/{path}", json={**body, **extra}, headers=auth_headers(admin_token)
        )
        assert response.status_code == 400, (path, response.get_data(as_text=True))
    response = client.get(
        "/api/inventory/actions/rollout-history",
        query_string={"clusterId": CLUSTER, "namespace": NAMESPACE, "workloadName": bad},
        headers=auth_headers(admin_token),
    )
    assert response.status_code == 400
    assert kubectl.calls == []


def test_a_normal_describe_still_runs(client, admin_token, kubectl):
    kubectl.stdout = "Name: payments-api\n"
    url = f"/api/clusters/{CLUSTER}/namespaces/{NAMESPACE}/resources/deployment/payments-api/describe"
    response = client.get(url, headers=auth_headers(admin_token))
    assert response.status_code == 200, response.get_data(as_text=True)
    assert any("describe" in call and "payments-api" in call for call in kubectl.calls)


def test_a_normal_exec_still_runs(client, admin_token, kubectl):
    kubectl.stdout = "uid=0(root)\n"
    url = f"/api/clusters/{CLUSTER}/namespaces/{NAMESPACE}/pods/{POD}/exec"
    response = client.post(
        url, json={"command": "id", "container": CONTAINER}, headers=auth_headers(admin_token)
    )
    assert response.status_code == 200, response.get_data(as_text=True)
    argv = kubectl.calls[-1]
    assert argv[argv.index("exec") + 1] == POD
    assert argv[argv.index("--") + 1:] == ["sh", "-c", "id"]


# ---------------------------------------------------------------------------
# Entry points: MCP tools
# ---------------------------------------------------------------------------

def _call_tool(client, token, name, arguments):
    response = client.post(
        "/api/mcp",
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": name, "arguments": arguments},
        },
        headers=auth_headers(token),
    )
    assert response.status_code == 200, response.get_data(as_text=True)
    return response.get_json()["result"]


@pytest.mark.parametrize("bad", BAD_NAMES)
def test_mcp_tools_refuse_bad_names(client, admin_token, kubectl, bad):
    where = {"cluster": CLUSTER, "namespace": NAMESPACE}
    calls = [
        ("kubesight_pod_logs", {**where, "pod": bad}),
        ("kubesight_pod_logs", {**where, "pod": POD, "container": bad}),
        ("kubesight_resource_restart", {**where, "kind": "pod", "name": bad}),
        ("kubesight_pod_exec", {**where, "pod": bad, "command": "id"}),
        ("kubesight_resource_get", {**where, "kind": "deployment", "name": bad}),
        ("kubesight_workload_restart", {**where, "workload": bad}),
        ("kubesight_rollout_history", {**where, "workload": bad}),
    ]
    for tool, arguments in calls:
        result = _call_tool(client, admin_token, tool, arguments)
        assert result.get("isError") is True, (tool, result)
        assert "Invalid" in result["content"][0]["text"], (tool, result)
    assert kubectl.calls == []
    assert not kubectl.saw(bad)
