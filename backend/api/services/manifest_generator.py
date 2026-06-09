"""Generate Kubernetes manifests from Docker image deployment form."""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

import yaml


def _sanitize_name(name: str) -> str:
    cleaned = name.strip().lower().replace("_", "-")
    return "".join(ch for ch in cleaned if ch.isalnum() or ch == "-")[:63].strip("-") or "app"


def generate_manifests(payload: Dict[str, Any]) -> Tuple[str, Dict[str, Any], Optional[str]]:
    """Return (yaml_string, parsed_resources_summary, error)."""
    app_name = _sanitize_name(payload.get("appName") or payload.get("app_name") or "app")
    namespace = (payload.get("namespace") or "default").strip()
    image = (payload.get("dockerImage") or payload.get("docker_image") or "").strip()
    tag = (payload.get("imageTag") or payload.get("image_tag") or "latest").strip()
    replicas = int(payload.get("replicas") or 1)
    container_port = int(payload.get("containerPort") or payload.get("container_port") or 8080)
    service_type = (payload.get("serviceType") or payload.get("service_type") or "ClusterIP").strip()
    env_vars = payload.get("environmentVariables") or payload.get("environment_variables") or {}
    cpu_request = (payload.get("cpuRequest") or payload.get("cpu_request") or "100m").strip()
    cpu_limit = (payload.get("cpuLimit") or payload.get("cpu_limit") or "500m").strip()
    memory_request = (payload.get("memoryRequest") or payload.get("memory_request") or "128Mi").strip()
    memory_limit = (payload.get("memoryLimit") or payload.get("memory_limit") or "512Mi").strip()

    if not image:
        return "", {}, "dockerImage is required"
    if replicas < 0:
        return "", {}, "replicas must be >= 0"

    full_image = f"{image}:{tag}" if ":" not in image.rsplit("/", 1)[-1] else image
    if tag and ":" not in image.rsplit("/", 1)[-1]:
        full_image = f"{image}:{tag}"

    labels = {"app": app_name, "app.kubernetes.io/name": app_name}

    deployment = {
        "apiVersion": "apps/v1",
        "kind": "Deployment",
        "metadata": {"name": app_name, "namespace": namespace, "labels": labels},
        "spec": {
            "replicas": replicas,
            "selector": {"matchLabels": labels},
            "template": {
                "metadata": {"labels": labels},
                "spec": {
                    "containers": [
                        {
                            "name": app_name,
                            "image": full_image,
                            "ports": [{"containerPort": container_port}],
                            "resources": {
                                "requests": {"cpu": cpu_request, "memory": memory_request},
                                "limits": {"cpu": cpu_limit, "memory": memory_limit},
                            },
                        }
                    ]
                },
            },
        },
    }

    if env_vars:
        env_list = [{"name": k, "value": str(v)} for k, v in env_vars.items()]
        deployment["spec"]["template"]["spec"]["containers"][0]["env"] = env_list

    service = {
        "apiVersion": "v1",
        "kind": "Service",
        "metadata": {"name": f"{app_name}-service", "namespace": namespace, "labels": labels},
        "spec": {
            "type": service_type,
            "selector": labels,
            "ports": [{"port": container_port, "targetPort": container_port, "protocol": "TCP"}],
        },
    }

    documents: List[Dict[str, Any]] = [deployment, service]

    if env_vars:
        config_map = {
            "apiVersion": "v1",
            "kind": "ConfigMap",
            "metadata": {"name": f"{app_name}-config", "namespace": namespace, "labels": labels},
            "data": {k: str(v) for k, v in env_vars.items()},
        }
        documents.append(config_map)

    yaml_docs = "\n---\n".join(yaml.dump(doc, default_flow_style=False, sort_keys=False) for doc in documents)

    summary = {
        "appName": app_name,
        "namespace": namespace,
        "resources": [
            {"kind": doc["kind"], "name": doc["metadata"]["name"], "namespace": namespace}
            for doc in documents
        ],
    }
    return yaml_docs, summary, None
