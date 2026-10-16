"""Reading where an installed app answers: Ingress hosts and the IP behind them."""

from __future__ import annotations

import pytest

from api import k8s_provider
from api.services.cluster_build import app_ingress


def _ingress(name, release, namespace="shop", hosts=("shop.example.local",), status_ip=None, tls=()):
    return {
        "metadata": {"name": name, "annotations": {
            "meta.helm.sh/release-name": release, "meta.helm.sh/release-namespace": namespace}},
        "spec": {
            "ingressClassName": "nginx",
            "rules": [{"host": h, "http": {"paths": [{"path": "/", "pathType": "Prefix"}]}} for h in hosts],
            "tls": [{"hosts": list(tls)}] if tls else [],
        },
        "status": {"loadBalancer": {"ingress": [{"ip": status_ip}]}} if status_ip else {},
    }


def _node(name, ip, ready=True, control_plane=False):
    labels = {"node-role.kubernetes.io/control-plane": ""} if control_plane else {}
    return {
        "metadata": {"name": name, "labels": labels},
        "status": {"addresses": [{"type": "InternalIP", "address": ip}],
                   "conditions": [{"type": "Ready", "status": "True" if ready else "False"}]},
    }


NODEPORT_SERVICE = {"spec": {"type": "NodePort", "ports": [
    {"name": "http", "port": 80, "nodePort": 30080},
    {"name": "https", "port": 443, "nodePort": 30443}]}}


@pytest.fixture()
def cluster(monkeypatch):
    state = {
        "ingresses": [],
        "service": NODEPORT_SERVICE,
        "nodes": [_node("cp-1", "10.0.0.10", control_plane=True), _node("w-1", "10.0.0.21"),
                  _node("w-2", "10.0.0.22"), _node("w-3", "10.0.0.23", ready=False)],
    }
    monkeypatch.setattr(k8s_provider, "resolve_cluster_access", lambda cluster_id: object())

    def fake_get(access, args):
        if "ingress" in args:
            return {"items": state["ingresses"]}
        if "service" in args:
            if state["service"] is None:
                raise k8s_provider.K8sCommandError("not found")
            return state["service"]
        if "nodes" in args:
            return {"items": state["nodes"]}
        raise AssertionError(args)

    monkeypatch.setattr(app_ingress, "_get", fake_get)
    return state


def test_nodeport_controller_gives_ready_workers_and_the_port(cluster):
    cluster["ingresses"] = [_ingress("web", "web"), _ingress("other", "someone-else")]
    found, problem = app_ingress.release_ingresses("custom-1", [("web", "shop")])
    assert problem is None
    assert len(found) == 1
    item = found[0]
    assert item["hosts"] == ["shop.example.local"]
    assert item["addresses"] == ["10.0.0.21", "10.0.0.22"]
    assert item["via"] == "nodePort"
    assert item["urls"] == ["http://shop.example.local:30080/"]
    text = "\n".join(app_ingress.lines(found))
    assert "point DNS shop.example.local at 10.0.0.21, 10.0.0.22" in text


def test_ingress_status_wins_and_tls_hosts_get_https(cluster):
    cluster["ingresses"] = [_ingress("web", "web", status_ip="10.0.0.50", tls=("shop.example.local",))]
    found, _ = app_ingress.release_ingresses("custom-1", [("web", "shop")])
    assert found[0]["addresses"] == ["10.0.0.50"] and found[0]["via"] == "ingress"
    assert found[0]["urls"] == ["https://shop.example.local/"]


def test_loadbalancer_controller(cluster):
    cluster["ingresses"] = [_ingress("web", "web")]
    cluster["service"] = {"spec": {"type": "LoadBalancer", "ports": []},
                          "status": {"loadBalancer": {"ingress": [{"ip": "10.0.0.60"}]}}}
    found, _ = app_ingress.release_ingresses("custom-1", [("web", "shop")])
    assert found[0]["addresses"] == ["10.0.0.60"] and found[0]["via"] == "loadBalancer"
    assert found[0]["urls"] == ["http://shop.example.local/"]


def test_no_host_uses_the_address_and_no_controller_means_no_address(cluster):
    cluster["ingresses"] = [_ingress("web", "web", hosts=())]
    cluster["service"] = None
    found, problem = app_ingress.release_ingresses("custom-1", [("web", "shop")])
    assert problem is None
    assert found[0]["addresses"] == [] and found[0]["urls"] == []
    assert "no host name" in app_ingress.lines(found)[0]


def test_a_read_failure_is_a_note_not_an_error(cluster, monkeypatch):
    def boom(access, args):
        raise RuntimeError("connection refused")

    monkeypatch.setattr(app_ingress, "_get", boom)
    found, problem = app_ingress.release_ingresses("custom-1", [("web", "shop")])
    assert found == [] and "connection refused" in problem


def test_unreachable_cluster(monkeypatch):
    monkeypatch.setattr(k8s_provider, "resolve_cluster_access", lambda cluster_id: None)
    found, problem = app_ingress.release_ingresses("custom-9", [("web", "shop")])
    assert found == [] and "cannot reach" in problem
