from api.k8s_provider import (
    endpoints_targets_by_name,
    format_service_external_ip,
    format_service_target_pods,
    targets_from_endpoint,
)


def test_format_external_ip_load_balancer_ingress():
    svc = {
        "spec": {"type": "LoadBalancer"},
        "status": {"loadBalancer": {"ingress": [{"ip": "203.0.113.10"}, {"hostname": "lb.example.com"}]}},
    }
    assert format_service_external_ip(svc) == "203.0.113.10, lb.example.com"


def test_format_external_ip_load_balancer_pending():
    svc = {"spec": {"type": "LoadBalancer"}, "status": {"loadBalancer": {}}}
    assert format_service_external_ip(svc) == "pending"


def test_format_external_ip_external_ips_and_nodeport():
    svc = {
        "spec": {
            "type": "NodePort",
            "externalIPs": ["192.0.2.1"],
            "ports": [{"port": 80, "nodePort": 30080}],
        },
    }
    assert format_service_external_ip(svc) == "192.0.2.1"


def test_format_external_ip_nodeport_with_node_ips():
    svc = {
        "spec": {"type": "NodePort", "ports": [{"port": 80, "nodePort": 30080}, {"port": 443, "nodePort": 30443}]},
    }
    result = format_service_external_ip(svc, ["192.168.1.10", "192.168.1.11"])
    assert "192.168.1.10:30080" in result
    assert "192.168.1.11:30080" in result
    assert "192.168.1.10:30443" in result


def test_format_external_ip_external_name():
    svc = {"spec": {"type": "ExternalName", "externalName": "api.example.com"}}
    assert format_service_external_ip(svc) == "api.example.com"


def test_targets_from_endpoint_pod_names():
    ep = {
        "metadata": {"name": "web"},
        "subsets": [
            {
                "addresses": [
                    {"ip": "10.0.0.1", "targetRef": {"kind": "Pod", "name": "web-abc"}},
                    {"ip": "10.0.0.2", "targetRef": {"kind": "Pod", "name": "web-def"}},
                ],
                "notReadyAddresses": [{"ip": "10.0.0.3", "targetRef": {"kind": "Pod", "name": "web-ghi"}}],
            }
        ],
    }
    assert targets_from_endpoint(ep) == ["web-abc", "web-def", "web-ghi"]


def test_format_service_target_pods_from_endpoints_index():
    svc = {"metadata": {"name": "backend-service"}, "spec": {"type": "ClusterIP", "selector": {"app": "backend"}}}
    endpoints_by_name = {"backend-service": ["backend-1", "backend-2"]}
    assert format_service_target_pods(svc, endpoints_by_name) == "backend-1, backend-2"


def test_format_service_target_pods_external_name():
    svc = {"metadata": {"name": "ext"}, "spec": {"type": "ExternalName", "externalName": "db.example.com"}}
    assert format_service_target_pods(svc, {}) == "db.example.com"


def test_endpoints_targets_by_name_index():
    items = [
        {"metadata": {"name": "a"}, "subsets": [{"addresses": [{"targetRef": {"kind": "Pod", "name": "a-0"}}]}]},
        {"metadata": {"name": "b"}, "subsets": []},
    ]
    assert endpoints_targets_by_name(items) == {"a": ["a-0"]}
