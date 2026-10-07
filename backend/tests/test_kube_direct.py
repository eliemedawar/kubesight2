import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from api import kube_direct
from api.kube_direct import _load_endpoint, plan_for


@pytest.mark.parametrize(
    "args, path, query",
    [
        (["get", "pods", "-n", "payments", "-o", "json"], "/api/v1/namespaces/payments/pods", {}),
        (["get", "pods", "--all-namespaces", "-o", "json"], "/api/v1/pods", {}),
        (["get", "deployments", "-A", "-o", "json"], "/apis/apps/v1/deployments", {}),
        (["get", "nodes", "-o", "json"], "/api/v1/nodes", {}),
        (["get", "deploy", "api", "-n", "ns1", "-o", "json"], "/apis/apps/v1/namespaces/ns1/deployments/api", {}),
        (["get", "pods", "-n", "ns1", "-l", "app=web", "-o", "json"], "/api/v1/namespaces/ns1/pods", {"labelSelector": "app=web"}),
        (["get", "ingress", "-n", "ns1", "-o", "json"], "/apis/networking.k8s.io/v1/namespaces/ns1/ingresses", {}),
        (["get", "--raw", "/api/v1/nodes/n1/proxy/stats/summary"], "/api/v1/nodes/n1/proxy/stats/summary", {}),
        (["version", "-o", "json"], "/version", {}),
    ],
)
def test_plain_reads_map_to_api_paths(args, path, query):
    plan = plan_for(args)
    assert plan is not None
    assert plan.path == path
    assert plan.query == query


@pytest.mark.parametrize(
    "args",
    [
        ["get", "pods", "-o", "json"],  # no namespace: kubectl's default namespace applies
        ["get", "pods", "-n", "x", "-o", "wide"],
        ["get", "pods", "-n", "x"],
        ["get", "pods", "-n", "x", "-o", "jsonpath={.items}"],
        ["get", "deployments,services", "-n", "x", "-o", "json"],
        ["get", "deployment/api", "-n", "x", "-o", "json"],
        ["get", "widgets", "-n", "x", "-o", "json"],
        ["get", "pods", "-n", "x", "--field-selector", "status.phase=Running", "-o", "json"],
        ["top", "pods", "-A", "--no-headers"],
        ["logs", "p", "-n", "x"],
        ["delete", "pod", "p", "-n", "x"],
        ["config", "view", "--minify", "-o", "json"],
    ],
)
def test_everything_else_falls_back_to_kubectl(args):
    assert plan_for(args) is None


def _kubeconfig(cluster: dict, user: dict) -> str:
    return json.dumps(
        {
            "apiVersion": "v1",
            "kind": "Config",
            "clusters": [{"name": "c", "cluster": cluster}],
            "contexts": [{"name": "ctx", "context": {"cluster": "c", "user": "u"}}],
            "current-context": "ctx",
            "users": [{"name": "u", "user": user}],
        }
    )


def test_token_auth_endpoint():
    endpoint = _load_endpoint(_kubeconfig({"server": "https://k8s.example:6443", "insecure-skip-tls-verify": True}, {"token": "abc"}), "ctx")
    assert endpoint is not None
    assert endpoint.server == "https://k8s.example:6443"
    assert endpoint.headers["Authorization"] == "Bearer abc"


@pytest.mark.parametrize(
    "user",
    [
        {"exec": {"command": "aws", "args": ["eks", "get-token"]}},
        {"auth-provider": {"name": "oidc"}},
        {"username": "a", "password": "b"},
    ],
)
def test_plugin_and_basic_auth_stay_on_kubectl(user):
    assert _load_endpoint(_kubeconfig({"server": "https://k8s.example:6443"}, user), "ctx") is None


def test_proxy_stays_on_kubectl():
    assert _load_endpoint(_kubeconfig({"server": "https://k", "proxy-url": "http://proxy:3128"}, {"token": "t"}), "ctx") is None


def test_unknown_context_stays_on_kubectl():
    assert _load_endpoint(_kubeconfig({"server": "https://k"}, {"token": "t"}), "other") is None


class _FakeApi(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_GET(self):  # noqa: N802
        if self.path.split("?")[0] == "/api/v1/namespaces/ns1/pods":
            if "continue=2" in self.path:
                body = {"kind": "PodList", "metadata": {}, "items": [{"metadata": {"name": "p3"}}]}
            else:
                body = {"kind": "PodList", "metadata": {"continue": "2"}, "items": [{"metadata": {"name": "p1"}}, {"metadata": {"name": "p2"}}]}
            data = json.dumps(body).encode()
            self.send_response(200)
        else:
            data = json.dumps({"kind": "Status", "reason": "NotFound", "message": 'pods "x" not found', "code": 404}).encode()
            self.send_response(404)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


@pytest.fixture()
def fake_api(tmp_path, monkeypatch):
    server = HTTPServer(("127.0.0.1", 0), _FakeApi)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    path = tmp_path / "kc.yaml"
    path.write_text(_kubeconfig({"server": f"http://127.0.0.1:{server.server_port}"}, {"token": "t"}))
    monkeypatch.setenv("KUBESIGHT_DIRECT_API_READS", "true")
    kube_direct.forget_endpoints()
    yield str(path)
    server.shutdown()
    kube_direct.forget_endpoints()


def test_list_pages_are_joined_like_kubectl(fake_api):
    out = kube_direct.try_read(["get", "pods", "-n", "ns1", "-o", "json"], fake_api, None, 5)
    doc = json.loads(out)
    assert doc["kind"] == "List"
    assert [i["metadata"]["name"] for i in doc["items"]] == ["p1", "p2", "p3"]
    assert all(i["kind"] == "Pod" and i["apiVersion"] == "v1" for i in doc["items"])


def test_api_errors_read_like_kubectl(fake_api):
    with pytest.raises(kube_direct.DirectReadError) as caught:
        kube_direct.try_read(["get", "pod", "x", "-n", "ns1", "-o", "json"], fake_api, None, 5)
    assert "NotFound" in str(caught.value)
    assert caught.value.network is False


def test_disabled_by_default(monkeypatch, tmp_path):
    monkeypatch.delenv("KUBESIGHT_DIRECT_API_READS", raising=False)
    assert kube_direct.try_read(["get", "pods", "-n", "ns1", "-o", "json"], str(tmp_path / "x"), None, 5) is None
