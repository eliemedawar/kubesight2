"""Tests for Application Builder wizard manifest generation."""

from api.services.wizard_manifest_generator import generate_wizard_manifests, validate_k8s_name


def test_validate_k8s_name():
    assert validate_k8s_name("my-app") is None
    assert validate_k8s_name("My_App") is not None


def test_generate_deployment_with_service():
    payload = {
        "basics": {"appName": "nginx-demo", "namespace": "default"},
        "workloadType": "Deployment",
        "containers": [{"name": "nginx", "image": "nginx", "tag": "latest", "ports": [80]}],
        "resources": {"cpuRequest": "100m", "cpuLimit": "500m", "memoryRequest": "128Mi", "memoryLimit": "256Mi"},
        "networking": {"service": {"enabled": True, "port": 80, "targetPort": 80}},
        "scaling": {"replicas": 2},
    }
    yaml_text, summary, error = generate_wizard_manifests(payload)
    assert error is None
    assert "kind: Deployment" in yaml_text
    assert "kind: Service" in yaml_text
    assert summary["appName"] == "nginx-demo"
    assert len(summary["resources"]) >= 2
