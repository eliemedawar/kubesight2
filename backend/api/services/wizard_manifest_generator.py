"""Generate Kubernetes manifests from Application Builder wizard form state."""

from __future__ import annotations

import base64
import json
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


def has_required_resource_requests(resources: Dict[str, Any]) -> bool:
    """HPA scales on resource utilization, so it can only work when both CPU and
    memory requests are explicitly defined. Without them Kubernetes has no
    reference point to compute utilization percentages.

    Returns True only when both ``cpuRequest`` and ``memoryRequest`` are present.
    """
    resources = resources or {}
    cpu_request = _coerce_str(resources.get("cpuRequest"))
    memory_request = _coerce_str(resources.get("memoryRequest"))
    return bool(cpu_request and memory_request)


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


def _volume_mount_name(vm: Dict[str, Any]) -> str:
    return str(vm.get("name") or vm.get("volumeName") or "").strip()


def _container_spec(
    container: Dict[str, Any],
    resources: Dict[str, Any],
    environment: Dict[str, Any],
    storage: Dict[str, Any],
    probes: Dict[str, Any],
) -> Dict[str, Any]:
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
        name = item.get("name")
        if not name:
            continue
        value_from = item.get("valueFrom") or {}
        kind = value_from.get("kind")
        ref_name = value_from.get("name")
        ref_key = value_from.get("key")
        if kind in ("configMap", "secret") and ref_name and ref_key:
            ref_field = "configMapKeyRef" if kind == "configMap" else "secretKeyRef"
            env_list.append({
                "name": name,
                "valueFrom": {ref_field: {"name": ref_name, "key": ref_key}},
            })
        else:
            env_list.append({"name": name, "value": str(item.get("value") or "")})
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
                **({"readOnly": True} if mount.get("readOnly") else {}),
            })
    for vm in environment.get("volumeMounts") or []:
        vol_name = _volume_mount_name(vm)
        if vm.get("mountPath") and vol_name:
            volume_mounts.append({
                "name": vol_name,
                "mountPath": vm["mountPath"],
                **({"subPath": vm["subPath"]} if vm.get("subPath") else {}),
            })
    for vm in storage.get("volumeMounts") or []:
        vol_name = _volume_mount_name(vm)
        if vm.get("mountPath") and vol_name:
            mount = {
                "name": vol_name,
                "mountPath": vm["mountPath"],
                **({"subPath": vm["subPath"]} if vm.get("subPath") else {}),
            }
            if vm.get("readOnly"):
                mount["readOnly"] = True
            volume_mounts.append(mount)
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


def _pvc_spec(
    pvc_cfg: Dict[str, Any],
    *,
    pvc_name: Optional[str] = None,
    volume_name: Optional[str] = None,
    storage_class: Optional[str] = None,
    force_storage_class: bool = False,
) -> Dict[str, Any]:
    spec: Dict[str, Any] = {
        "accessModes": [pvc_cfg.get("accessMode") or "ReadWriteOnce"],
        "resources": {"requests": {"storage": pvc_cfg.get("size") or "1Gi"}},
    }
    sc = storage_class if storage_class is not None else pvc_cfg.get("storageClass")
    if force_storage_class:
        spec["storageClassName"] = sc or ""
    elif sc:
        spec["storageClassName"] = sc
    if volume_name:
        spec["volumeName"] = volume_name
    return spec


def _build_persistent_volume(
    advanced: Dict[str, Any],
    pvc_cfg: Dict[str, Any],
    labels: Dict[str, str],
) -> Dict[str, Any]:
    pv_name = _sanitize_name(advanced.get("pvName") or f"{pvc_cfg.get('name') or 'data'}-pv")
    capacity = advanced.get("capacity") or pvc_cfg.get("size") or "1Gi"
    access_mode = pvc_cfg.get("accessMode") or "ReadWriteOnce"
    storage_class = ""
    reclaim = advanced.get("reclaimPolicy") or "Retain"
    storage_type = (advanced.get("storageType") or "hostPath").lower()

    spec: Dict[str, Any] = {
        "capacity": {"storage": capacity},
        "accessModes": [access_mode],
        "persistentVolumeReclaimPolicy": reclaim,
        "storageClassName": storage_class,
    }

    if storage_type == "hostpath":
        spec["hostPath"] = {"path": advanced.get("hostPath") or "/data", "type": ""}
    elif storage_type == "nfs":
        spec["nfs"] = {
            "server": advanced.get("nfsServer") or "",
            "path": advanced.get("nfsPath") or "/",
        }
    elif storage_type == "local":
        spec["local"] = {"path": advanced.get("localPath") or "/mnt/data"}
        node_name = (advanced.get("nodeName") or "").strip()
        if node_name:
            spec["nodeAffinity"] = {
                "required": {
                    "nodeSelectorTerms": [
                        {
                            "matchExpressions": [
                                {
                                    "key": "kubernetes.io/hostname",
                                    "operator": "In",
                                    "values": [node_name],
                                }
                            ]
                        }
                    ]
                }
            }

    return {
        "apiVersion": "v1",
        "kind": "PersistentVolume",
        "metadata": {"name": pv_name, "labels": labels},
        "spec": spec,
    }


def _append_pvc_documents(
    documents: List[Dict[str, Any]],
    storage: Dict[str, Any],
    namespace: str,
    labels: Dict[str, str],
    *,
    default_name: str,
) -> None:
    advanced = storage.get("advanced") or {}
    manual_pv = bool(advanced.get("createManualPv"))
    new_pvc = storage.get("newPvc") or {}
    pvc_name = _sanitize_name(new_pvc.get("name") or default_name)
    if any(d.get("kind") == "PersistentVolumeClaim" and d["metadata"]["name"] == pvc_name for d in documents):
        return

    pv_name = None
    if manual_pv:
        pv_doc = _build_persistent_volume(advanced, new_pvc, labels)
        pv_name = pv_doc["metadata"]["name"]
        documents.append(pv_doc)

    documents.append({
        "apiVersion": "v1",
        "kind": "PersistentVolumeClaim",
        "metadata": {"name": pvc_name, "namespace": namespace, "labels": labels},
        "spec": _pvc_spec(
            new_pvc,
            volume_name=pv_name,
            storage_class="" if manual_pv else None,
            force_storage_class=manual_pv,
        ),
    })


def _provisioned_config_documents(
    environment: Dict[str, Any],
    namespace: str,
    labels: Dict[str, str],
) -> List[Dict[str, Any]]:
    """Build ConfigMap/Secret documents the resolver asked us to create.

    These come from "Create ConfigMap"/"Create Secret" env sources (and dependency
    wiring). The matching env entries already reference them via ``valueFrom``.
    """
    docs: List[Dict[str, Any]] = []
    for cm in environment.get("provisionedConfigMaps") or []:
        name = _sanitize_name(cm.get("name") or "config")
        data = {str(k): str(v) for k, v in (cm.get("data") or {}).items() if k}
        # Binary values arrive already base64-encoded; the kubelet decodes them.
        binary_data = {str(k): str(v) for k, v in (cm.get("binaryData") or {}).items() if k}
        if not data and not binary_data:
            continue
        doc: Dict[str, Any] = {
            "apiVersion": "v1",
            "kind": "ConfigMap",
            "metadata": {"name": name, "namespace": namespace, "labels": labels},
        }
        if data:
            doc["data"] = data
        if binary_data:
            doc["binaryData"] = binary_data
        docs.append(doc)
    for sec in environment.get("provisionedSecrets") or []:
        name = _sanitize_name(sec.get("name") or "secret")
        data = {str(k): str(v) for k, v in (sec.get("stringData") or {}).items() if k}
        # Binary values are base64 already and go straight into the Secret's ``data``.
        binary_data = {str(k): str(v) for k, v in (sec.get("data") or {}).items() if k}
        if not data and not binary_data:
            continue
        doc = {
            "apiVersion": "v1",
            "kind": "Secret",
            "metadata": {"name": name, "namespace": namespace, "labels": labels},
            "type": "Opaque",
        }
        if data:
            doc["stringData"] = data
        if binary_data:
            doc["data"] = binary_data
        docs.append(doc)
    for sec in environment.get("provisionedDockerSecrets") or []:
        name = _sanitize_name(sec.get("name") or "registry")
        registry = (sec.get("registry") or "https://index.docker.io/v1/").strip()
        username = sec.get("username") or ""
        password = sec.get("password") or ""
        if not username and not password:
            continue
        auth = base64.b64encode(f"{username}:{password}".encode()).decode()
        entry: Dict[str, Any] = {"username": username, "password": password, "auth": auth}
        if sec.get("email"):
            entry["email"] = sec["email"]
        dockercfg = json.dumps({"auths": {registry: entry}})
        docs.append({
            "apiVersion": "v1",
            "kind": "Secret",
            "metadata": {"name": name, "namespace": namespace, "labels": labels},
            "type": "kubernetes.io/dockerconfigjson",
            "stringData": {".dockerconfigjson": dockercfg},
        })
    for sec in environment.get("provisionedTlsSecrets") or []:
        name = _sanitize_name(sec.get("name") or "tls")
        cert = sec.get("cert") or ""
        key = sec.get("key") or ""
        if not cert or not key:
            continue
        docs.append({
            "apiVersion": "v1",
            "kind": "Secret",
            "metadata": {"name": name, "namespace": namespace, "labels": labels},
            "type": "kubernetes.io/tls",
            "stringData": {"tls.crt": cert, "tls.key": key},
        })
    return docs


def _service_port_specs(svc_cfg: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Build a Service ``ports`` list from a service config.

    Supports the multi-port shape the Deploy Wizard's Service Exposure step emits
    (``ports`` is a list of ``{name, protocol, port, targetPort, nodePort}``) and
    falls back to the legacy single-port fields (``port``/``targetPort``/``protocol``)
    so older payloads and template defaults keep working.
    """
    svc_type = svc_cfg.get("type") or "ClusterIP"
    raw_ports = svc_cfg.get("ports")
    if not isinstance(raw_ports, list) or not raw_ports:
        port = int(svc_cfg.get("port") or 80)
        raw_ports = [{
            "port": port,
            "targetPort": svc_cfg.get("targetPort") or port,
            "protocol": svc_cfg.get("protocol") or "TCP",
        }]

    specs: List[Dict[str, Any]] = []
    for entry in raw_ports:
        if not isinstance(entry, dict):
            continue
        try:
            port = int(entry.get("port") or entry.get("targetPort") or 80)
        except (TypeError, ValueError):
            continue
        try:
            target_port = int(entry.get("targetPort") or port)
        except (TypeError, ValueError):
            target_port = port
        protocol = (entry.get("protocol") or "TCP").upper()
        spec: Dict[str, Any] = {
            "name": str(entry.get("name") or f"port-{port}"),
            "protocol": protocol if protocol in ("TCP", "UDP", "SCTP") else "TCP",
            "port": port,
            "targetPort": target_port,
        }
        # nodePort is only meaningful for NodePort / LoadBalancer services.
        node_port = entry.get("nodePort")
        if node_port not in (None, "") and svc_type in ("NodePort", "LoadBalancer"):
            try:
                spec["nodePort"] = int(node_port)
            except (TypeError, ValueError):
                pass
        specs.append(spec)
    return specs


def _primary_service_port(svc_cfg: Dict[str, Any]) -> int:
    """The Service port an Ingress backend should target (first declared port)."""
    specs = _service_port_specs(svc_cfg)
    if specs:
        return specs[0]["port"]
    return int(svc_cfg.get("port") or 80)


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
        _container_spec(c, resources, environment, storage, probes) for c in containers
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
        vol_name = _volume_mount_name(vm) or "data"
        pvc_name = vm.get("pvcName") or storage.get("existingPvc") or (storage.get("newPvc") or {}).get("name")
        if pvc_name and vol_name and not any(v["name"] == vol_name for v in volumes):
            volumes.append({"name": vol_name, "persistentVolumeClaim": {"claimName": pvc_name}})

    pod_spec: Dict[str, Any] = {"containers": container_specs}
    if volumes:
        pod_spec["volumes"] = volumes

    # Pull secrets are pod-level; collect the distinct names referenced by containers.
    pull_secrets: List[str] = []
    for container in containers:
        name = (container.get("imagePullSecret") or "").strip()
        if name and name not in pull_secrets:
            pull_secrets.append(name)
    if pull_secrets:
        pod_spec["imagePullSecrets"] = [{"name": _sanitize_name(n)} for n in pull_secrets]

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

    # ConfigMaps/Secrets created from "Create ConfigMap/Secret" sources and
    # dependency wiring are emitted first so the workload can reference them.
    documents.extend(_provisioned_config_documents(environment, namespace, labels))

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
        pvc_storage = {**storage, "newPvc": {**(storage.get("newPvc") or {}), "name": app_name}}
        _append_pvc_documents(documents, pvc_storage, namespace, labels, default_name=app_name)
    elif workload_type == "Service":
        svc = networking.get("service") or {}
        documents.append({
            "apiVersion": "v1",
            "kind": "Service",
            "metadata": {"name": app_name, "namespace": namespace, "labels": labels},
            "spec": {
                "type": svc.get("type") or "ClusterIP",
                "selector": labels,
                "ports": _service_port_specs(svc),
            },
        })

    pvc_mode = storage.get("pvcMode") or "none"
    if pvc_mode == "new":
        _append_pvc_documents(documents, storage, namespace, labels, default_name=f"{app_name}-pvc")

    svc_cfg = networking.get("service") or {}
    if svc_cfg.get("enabled") and workload_type in WORKLOAD_KINDS:
        svc_name = _sanitize_name(svc_cfg.get("name") or f"{app_name}-service")
        # The selector pins to the workload's pod template labels so routing
        # always lands on this deployment's pods.
        documents.append({
            "apiVersion": "v1",
            "kind": "Service",
            "metadata": {"name": svc_name, "namespace": namespace, "labels": labels},
            "spec": {
                "type": svc_cfg.get("type") or "ClusterIP",
                "selector": labels,
                "ports": _service_port_specs(svc_cfg),
            },
        })

    ing_cfg = networking.get("ingress") or {}
    if ing_cfg.get("enabled"):
        ing_name = _sanitize_name(ing_cfg.get("name") or f"{app_name}-ingress")
        svc_port = _primary_service_port(networking.get("service") or {})
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
    hpa_enabled = bool(hpa_cfg.get("enabled")) and workload_type in ("Deployment", "StatefulSet")
    hpa_disabled_reason: Optional[str] = None
    if hpa_enabled and not has_required_resource_requests(resources):
        # HPA needs CPU + memory requests to compute utilization. Force it off
        # rather than emit an HPA that can never scale correctly.
        hpa_enabled = False
        hpa_disabled_reason = (
            "HPA was disabled automatically: CPU and memory requests must be "
            "defined for autoscaling to work."
        )
    if hpa_enabled:
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
    if hpa_disabled_reason:
        summary["warnings"] = [hpa_disabled_reason]
    return yaml_docs, summary, None
