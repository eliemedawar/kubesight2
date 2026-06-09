"""Generate Kubernetes manifests from Application Builder wizard form state."""

from __future__ import annotations

import base64
import re
from typing import Any, Dict, List, Optional, Tuple

import yaml

K8S_NAME_RE = re.compile(r"^[a-z0-9]([-a-z0-9]*[a-z0-9])?$")
WORKLOAD_KINDS = {"Deployment", "StatefulSet", "DaemonSet", "Job", "CronJob"}
STANDALONE_KINDS = {"Service", "ConfigMap", "Secret", "PersistentVolumeClaim", "HorizontalPodAutoscaler", "Ingress"}


def _coerce_str(value: Any, default: str = "") -> str:
    if value is None:
        return default
    if isinstance(value, dict):
        for key in ("name", "namespace", "value", "id"):
            nested = value.get(key)
            if nested is not None and not isinstance(nested, (dict, list)):
                return str(nested).strip()
        return default
    return str(value).strip()


def _sanitize_name(name: str) -> str:
    cleaned = name.strip().lower().replace("_", "-")
    return "".join(ch for ch in cleaned if ch.isalnum() or ch == "-")[:63].strip("-") or "app"


def validate_k8s_name(name: str) -> Optional[str]:
    if not name:
        return "Name is required"
    if len(name) > 63:
        return "Name must be 63 characters or fewer"
    if not K8S_NAME_RE.match(name):
        return "Name must use lowercase letters, numbers, and hyphens (Kubernetes DNS-1123)"
    return None


def _labels(basics: Dict[str, Any], app_name: str) -> Dict[str, str]:
    labels = {"app": app_name, "app.kubernetes.io/name": app_name}
    for key, value in (basics.get("labels") or {}).items():
        if key and value is not None:
            labels[str(key)] = str(value)
    return labels


def _annotations(basics: Dict[str, Any]) -> Dict[str, str]:
    return {str(k): str(v) for k, v in (basics.get("annotations") or {}).items() if k}


def _container_spec(container: Dict[str, Any], resources: Dict[str, Any], environment: Dict[str, Any], probes: Dict[str, Any]) -> Dict[str, Any]:
    image = (container.get("image") or "").strip()
    tag = (container.get("tag") or "latest").strip()
    if image and ":" not in image.rsplit("/", 1)[-1]:
        image = f"{image}:{tag}"

    spec: Dict[str, Any] = {
        "name": _sanitize_name(container.get("name") or "main"),
        "image": image,
        "imagePullPolicy": container.get("pullPolicy") or "IfNotPresent",
    }

    ports = container.get("ports") or []
    if ports:
        spec["ports"] = [{"containerPort": int(p)} for p in ports if p]

    command = container.get("command") or []
    if command:
        spec["command"] = command if isinstance(command, list) else [command]
    args = container.get("args") or []
    if args:
        spec["args"] = args if isinstance(args, list) else [args]
    if container.get("workingDir"):
        spec["workingDir"] = container["workingDir"]

    spec["resources"] = {
        "requests": {
            "cpu": resources.get("cpuRequest") or "100m",
            "memory": resources.get("memoryRequest") or "128Mi",
        },
        "limits": {
            "cpu": resources.get("cpuLimit") or "500m",
            "memory": resources.get("memoryLimit") or "512Mi",
        },
    }

    env_list: List[Dict[str, Any]] = []
    for item in environment.get("envVars") or []:
        if item.get("name"):
            env_list.append({"name": item["name"], "value": str(item.get("value") or "")})
    for ref in environment.get("configMapRefs") or []:
        cm_name = ref.get("name")
        if not cm_name:
            continue
        for key in ref.get("keys") or []:
            env_list.append({
                "name": key,
                "valueFrom": {"configMapKeyRef": {"name": cm_name, "key": key}},
            })
    for ref in environment.get("secretRefs") or []:
        sec_name = ref.get("name")
        if not sec_name:
            continue
        for key in ref.get("keys") or []:
            env_list.append({
                "name": key,
                "valueFrom": {"secretKeyRef": {"name": sec_name, "key": key}},
            })
    if env_list:
        spec["env"] = env_list

    volume_mounts = []
    for mount in environment.get("mountedFiles") or []:
        if mount.get("mountPath"):
            volume_mounts.append({
                "name": mount.get("volumeName") or mount.get("name") or "config-vol",
                "mountPath": mount["mountPath"],
                **({"subPath": mount["subPath"]} if mount.get("subPath") else {}),
            })
    for vm in environment.get("volumeMounts") or []:
        if vm.get("mountPath") and vm.get("volumeName"):
            volume_mounts.append({
                "name": vm["volumeName"],
                "mountPath": vm["mountPath"],
                **({"subPath": vm["subPath"]} if vm.get("subPath") else {}),
            })
    if volume_mounts:
        spec["volumeMounts"] = volume_mounts

    for probe_key, probe_field in [("readiness", "readinessProbe"), ("liveness", "livenessProbe"), ("startup", "startupProbe")]:
        probe = probes.get(probe_key) or {}
        if not probe.get("enabled"):
            continue
        built = _build_probe(probe)
        if built:
            spec[probe_field] = built

    return spec


def _build_probe(probe: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    probe_type = probe.get("type") or "http"
    initial_delay = int(probe.get("initialDelaySeconds") or 5)
    period = int(probe.get("periodSeconds") or 10)
    base = {"initialDelaySeconds": initial_delay, "periodSeconds": period}

    if probe_type == "http":
        port = int(probe.get("port") or 80)
        path = probe.get("path") or "/"
        return {**base, "httpGet": {"path": path, "port": port}}
    if probe_type == "tcp":
        port = int(probe.get("port") or 80)
        return {**base, "tcpSocket": {"port": port}}
    if probe_type == "command":
        cmd = probe.get("command")
        if not cmd:
            return None
        command = cmd if isinstance(cmd, list) else [cmd]
        return {**base, "exec": {"command": command}}
    return None


def _pod_template(
    app_name: str,
    namespace: str,
    labels: Dict[str, str],
    annotations: Dict[str, str],
    containers: List[Dict[str, Any]],
    resources: Dict[str, Any],
    environment: Dict[str, Any],
    storage: Dict[str, Any],
    probes: Dict[str, Any],
) -> Dict[str, Any]:
    container_specs = [
        _container_spec(c, resources, environment, probes) for c in containers
    ]
    volumes = []
    for mount in environment.get("mountedFiles") or []:
        vol_name = mount.get("volumeName") or mount.get("name") or "config-vol"
        if mount.get("configMap"):
            volumes.append({
                "name": vol_name,
                "configMap": {"name": mount["configMap"]},
            })
        elif mount.get("secret"):
            volumes.append({
                "name": vol_name,
                "secret": {"secretName": mount["secret"]},
            })
    for vm in storage.get("volumeMounts") or []:
        vol_name = vm.get("volumeName") or "data"
        pvc_name = vm.get("pvcName") or storage.get("existingPvc") or (storage.get("newPvc") or {}).get("name")
        if pvc_name and not any(v["name"] == vol_name for v in volumes):
            volumes.append({"name": vol_name, "persistentVolumeClaim": {"claimName": pvc_name}})

    pod_spec: Dict[str, Any] = {"containers": container_specs}
    if volumes:
        pod_spec["volumes"] = volumes

    meta: Dict[str, Any] = {"labels": labels}
    if annotations:
        meta["annotations"] = annotations

    return {
        "metadata": meta,
        "spec": pod_spec,
    }


def _workload_document(
    kind: str,
    app_name: str,
    namespace: str,
    labels: Dict[str, str],
    annotations: Dict[str, str],
    scaling: Dict[str, Any],
    pod_template: Dict[str, Any],
) -> Dict[str, Any]:
    meta: Dict[str, Any] = {"name": app_name, "namespace": namespace, "labels": labels}
    if annotations:
        meta["annotations"] = annotations

    if kind == "Deployment":
        return {
            "apiVersion": "apps/v1",
            "kind": "Deployment",
            "metadata": meta,
            "spec": {
                "replicas": int(scaling.get("replicas") or 1),
                "selector": {"matchLabels": labels},
                "template": pod_template,
            },
        }
    if kind == "StatefulSet":
        return {
            "apiVersion": "apps/v1",
            "kind": "StatefulSet",
            "metadata": meta,
            "spec": {
                "replicas": int(scaling.get("replicas") or 1),
                "selector": {"matchLabels": labels},
                "serviceName": f"{app_name}-headless",
                "template": pod_template,
            },
        }
    if kind == "DaemonSet":
        return {
            "apiVersion": "apps/v1",
            "kind": "DaemonSet",
            "metadata": meta,
            "spec": {
                "selector": {"matchLabels": labels},
                "template": pod_template,
            },
        }
    if kind == "Job":
        return {
            "apiVersion": "batch/v1",
            "kind": "Job",
            "metadata": meta,
            "spec": {
                "template": {
                    **pod_template,
                    "spec": {**pod_template["spec"], "restartPolicy": "Never"},
                },
            },
        }
    if kind == "CronJob":
        schedule = scaling.get("cronSchedule") or "0 * * * *"
        return {
            "apiVersion": "batch/v1",
            "kind": "CronJob",
            "metadata": meta,
            "spec": {
                "schedule": schedule,
                "jobTemplate": {
                    "spec": {
                        "template": {
                            **pod_template,
                            "spec": {**pod_template["spec"], "restartPolicy": "OnFailure"},
                        },
                    },
                },
            },
        }
    raise ValueError(f"Unsupported workload kind: {kind}")


def generate_wizard_manifests(payload: Dict[str, Any]) -> Tuple[str, Dict[str, Any], Optional[str]]:
    """Return (yaml_string, summary, error)."""
    basics = payload.get("basics") or {}
    app_name = _sanitize_name(basics.get("appName") or basics.get("app_name") or "app")
    name_err = validate_k8s_name(app_name)
    if name_err:
        return "", {}, name_err

    namespace = _coerce_str(basics.get("namespace"), "default") or "default"
    if not namespace:
        return "", {}, "namespace is required"

    workload_type = payload.get("workloadType") or "Deployment"
    labels = _labels(basics, app_name)
    annotations = _annotations(basics)
    containers = payload.get("containers") or []
    if workload_type in WORKLOAD_KINDS and not containers:
        return "", {}, "At least one container is required"

    for container in containers:
        if not (container.get("image") or "").strip():
            return "", {}, "Container image is required"

    resources = payload.get("resources") or {}
    environment = payload.get("environment") or {}
    storage = payload.get("storage") or {}
    networking = payload.get("networking") or {}
    health_checks = payload.get("healthChecks") or {}
    scaling = payload.get("scaling") or {}

    documents: List[Dict[str, Any]] = []

    if workload_type in WORKLOAD_KINDS:
        pod_template = _pod_template(
            app_name, namespace, labels, annotations, containers, resources, environment, storage, health_checks
        )
        documents.append(_workload_document(workload_type, app_name, namespace, labels, annotations, scaling, pod_template))
    elif workload_type == "ConfigMap":
        data = environment.get("configMapData") or {}
        if not data:
            for item in environment.get("envVars") or []:
                if item.get("name"):
                    data[item["name"]] = str(item.get("value") or "")
        documents.append({
            "apiVersion": "v1",
            "kind": "ConfigMap",
            "metadata": {"name": app_name, "namespace": namespace, "labels": labels},
            "data": {str(k): str(v) for k, v in data.items()},
        })
    elif workload_type == "Secret":
        data = environment.get("secretData") or {}
        documents.append({
            "apiVersion": "v1",
            "kind": "Secret",
            "metadata": {"name": app_name, "namespace": namespace, "labels": labels},
            "type": "Opaque",
            "stringData": {str(k): str(v) for k, v in data.items()},
        })
    elif workload_type == "PersistentVolumeClaim":
        new_pvc = storage.get("newPvc") or {}
        documents.append({
            "apiVersion": "v1",
            "kind": "PersistentVolumeClaim",
            "metadata": {"name": app_name, "namespace": namespace, "labels": labels},
            "spec": {
                "accessModes": [new_pvc.get("accessMode") or "ReadWriteOnce"],
                "resources": {"requests": {"storage": new_pvc.get("size") or "1Gi"}},
                **({"storageClassName": new_pvc["storageClass"]} if new_pvc.get("storageClass") else {}),
            },
        })
    elif workload_type == "Service":
        svc = networking.get("service") or {}
        documents.append({
            "apiVersion": "v1",
            "kind": "Service",
            "metadata": {"name": app_name, "namespace": namespace, "labels": labels},
            "spec": {
                "type": svc.get("type") or "ClusterIP",
                "selector": labels,
                "ports": [{
                    "port": int(svc.get("port") or 80),
                    "targetPort": int(svc.get("targetPort") or svc.get("port") or 80),
                    "protocol": svc.get("protocol") or "TCP",
                }],
            },
        })

    pvc_mode = storage.get("pvcMode") or "none"
    if pvc_mode == "new":
        new_pvc = storage.get("newPvc") or {}
        pvc_name = _sanitize_name(new_pvc.get("name") or f"{app_name}-pvc")
        if not any(d.get("kind") == "PersistentVolumeClaim" and d["metadata"]["name"] == pvc_name for d in documents):
            documents.append({
                "apiVersion": "v1",
                "kind": "PersistentVolumeClaim",
                "metadata": {"name": pvc_name, "namespace": namespace, "labels": labels},
                "spec": {
                    "accessModes": [new_pvc.get("accessMode") or "ReadWriteOnce"],
                    "resources": {"requests": {"storage": new_pvc.get("size") or "1Gi"}},
                    **({"storageClassName": new_pvc["storageClass"]} if new_pvc.get("storageClass") else {}),
                },
            })

    svc_cfg = networking.get("service") or {}
    if svc_cfg.get("enabled") and workload_type in WORKLOAD_KINDS:
        svc_name = _sanitize_name(svc_cfg.get("name") or f"{app_name}-service")
        documents.append({
            "apiVersion": "v1",
            "kind": "Service",
            "metadata": {"name": svc_name, "namespace": namespace, "labels": labels},
            "spec": {
                "type": svc_cfg.get("type") or "ClusterIP",
                "selector": labels,
                "ports": [{
                    "port": int(svc_cfg.get("port") or 80),
                    "targetPort": int(svc_cfg.get("targetPort") or svc_cfg.get("port") or 80),
                    "protocol": svc_cfg.get("protocol") or "TCP",
                }],
            },
        })

    ing_cfg = networking.get("ingress") or {}
    if ing_cfg.get("enabled"):
        ing_name = _sanitize_name(ing_cfg.get("name") or f"{app_name}-ingress")
        svc_port = int((networking.get("service") or {}).get("port") or 80)
        rules = [{
            "host": ing_cfg.get("host") or f"{app_name}.local",
            "http": {
                "paths": [{
                    "path": ing_cfg.get("path") or "/",
                    "pathType": "Prefix",
                    "backend": {
                        "service": {
                            "name": _sanitize_name(svc_cfg.get("name") or f"{app_name}-service"),
                            "port": {"number": svc_port},
                        },
                    },
                }],
            },
        }]
        ing_doc: Dict[str, Any] = {
            "apiVersion": "networking.k8s.io/v1",
            "kind": "Ingress",
            "metadata": {"name": ing_name, "namespace": namespace, "labels": labels},
            "spec": {"rules": rules},
        }
        if ing_cfg.get("tlsEnabled") and ing_cfg.get("tlsSecret"):
            ing_doc["spec"]["tls"] = [{"hosts": [ing_cfg.get("host")], "secretName": ing_cfg["tlsSecret"]}]
        documents.append(ing_doc)

    hpa_cfg = scaling.get("hpa") or {}
    if hpa_cfg.get("enabled") and workload_type in ("Deployment", "StatefulSet"):
        metrics = []
        if hpa_cfg.get("cpuThreshold"):
            metrics.append({
                "type": "Resource",
                "resource": {"name": "cpu", "target": {"type": "Utilization", "averageUtilization": int(hpa_cfg["cpuThreshold"])}},
            })
        if hpa_cfg.get("memoryThreshold"):
            metrics.append({
                "type": "Resource",
                "resource": {"name": "memory", "target": {"type": "Utilization", "averageUtilization": int(hpa_cfg["memoryThreshold"])}},
            })
        documents.append({
            "apiVersion": "autoscaling/v2",
            "kind": "HorizontalPodAutoscaler",
            "metadata": {"name": f"{app_name}-hpa", "namespace": namespace, "labels": labels},
            "spec": {
                "scaleTargetRef": {
                    "apiVersion": "apps/v1",
                    "kind": workload_type,
                    "name": app_name,
                },
                "minReplicas": int(hpa_cfg.get("minReplicas") or 1),
                "maxReplicas": int(hpa_cfg.get("maxReplicas") or 5),
                "metrics": metrics or [{
                    "type": "Resource",
                    "resource": {"name": "cpu", "target": {"type": "Utilization", "averageUtilization": 80}},
                }],
            },
        })

    if not documents:
        return "", {}, "No resources to generate"

    yaml_docs = "\n---\n".join(
        yaml.dump(doc, default_flow_style=False, sort_keys=False) for doc in documents
    )
    summary = {
        "appName": app_name,
        "namespace": namespace,
        "workloadType": workload_type,
        "resources": [
            {"kind": doc["kind"], "name": doc["metadata"]["name"], "namespace": namespace}
            for doc in documents
        ],
    }
    return yaml_docs, summary, None
