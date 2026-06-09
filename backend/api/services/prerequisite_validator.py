"""Validate deployment prerequisites before applying wizard manifests."""

from __future__ import annotations

import json
import re
from typing import Any, Dict, List, Optional, Tuple

from ..k8s_metrics import metrics_server_available
from ..k8s_provider import K8sCommandError, resolve_cluster_access, _run_for_access
from ..models import User
from ..access_engine import can_access_namespace
from .wizard_manifest_generator import generate_wizard_manifests

IMAGE_RE = re.compile(r"^([a-z0-9]+([._-][a-z0-9]+)*(/[a-z0-9]+([._-][a-z0-9]+)*)*)(:[\w][\w.-]{0,127})?(@sha256:[a-f0-9]{64})?$", re.IGNORECASE)


def _check(status: str, category: str, message: str, detail: str = "") -> Dict[str, Any]:
    return {"status": status, "category": category, "message": message, "detail": detail}


def _resource_exists(access, kind: str, name: str, namespace: str) -> bool:
    try:
        _run_for_access(access, ["get", kind, name, "-n", namespace])
        return True
    except K8sCommandError:
        return False


def _namespace_exists(access, namespace: str) -> bool:
    try:
        _run_for_access(access, ["get", "namespace", namespace])
        return True
    except K8sCommandError:
        return False


def _ingress_controller_available(access) -> Tuple[bool, str]:
    try:
        output = _run_for_access(access, ["get", "pods", "-A", "-l", "app.kubernetes.io/name=ingress-nginx", "-o", "json"])
        items = json.loads(output).get("items") or []
        if items:
            return True, "nginx ingress controller detected"
        output = _run_for_access(access, ["get", "ingressclass", "-o", "json"])
        classes = json.loads(output).get("items") or []
        if classes:
            names = ", ".join(c.get("metadata", {}).get("name", "") for c in classes)
            return True, f"IngressClass resources found: {names}"
        return False, "No IngressClass or ingress-nginx pods found"
    except K8sCommandError as exc:
        return False, str(exc)


def _storage_class_exists(access, name: str) -> bool:
    try:
        _run_for_access(access, ["get", "storageclass", name])
        return True
    except K8sCommandError:
        return False


def _app_name_exists(access, namespace: str, app_name: str, workload_type: str) -> bool:
    kind_map = {
        "Deployment": "deployment",
        "StatefulSet": "statefulset",
        "DaemonSet": "daemonset",
        "Job": "job",
        "CronJob": "cronjob",
        "Service": "service",
        "ConfigMap": "configmap",
        "Secret": "secret",
        "PersistentVolumeClaim": "pvc",
        "HorizontalPodAutoscaler": "hpa",
        "Ingress": "ingress",
    }
    kind = kind_map.get(workload_type, "deployment")
    return _resource_exists(access, kind, app_name, namespace)


def validate_prerequisites(
    user: Optional[User],
    payload: Dict[str, Any],
) -> Tuple[Optional[Dict[str, Any]], Optional[str], int]:
    basics = payload.get("basics") or {}
    cluster_id = basics.get("clusterId") or basics.get("cluster") or ""
    namespace = (basics.get("namespace") or "").strip()
    app_name = (basics.get("appName") or "").strip()

    if not cluster_id or not namespace:
        return None, "clusterId and namespace are required", 400

    if user and not can_access_namespace(user, cluster_id, namespace):
        return None, "Forbidden", 403

    access = resolve_cluster_access(cluster_id)
    if not access:
        return None, "Cluster not found", 404

    checks: List[Dict[str, Any]] = []
    yaml_content, summary, gen_error = generate_wizard_manifests(payload)
    if gen_error:
        checks.append(_check("failed", "Generation", gen_error))
        return {"checks": checks, "passed": 0, "warnings": 0, "failed": len(checks), "yaml": ""}, None, 200

    workload_type = payload.get("workloadType") or "Deployment"

    if _namespace_exists(access, namespace):
        checks.append(_check("passed", "Namespace", f"Namespace '{namespace}' exists"))
    else:
        checks.append(_check("failed", "Namespace", f"Namespace '{namespace}' does not exist", "Create the namespace first or enable namespace creation"))

    if app_name and _app_name_exists(access, namespace, summary.get("appName", app_name), workload_type):
        checks.append(_check("warning", "Name", f"Resource '{summary.get('appName')}' may already exist in namespace", "Apply will update existing resources"))
    elif app_name:
        checks.append(_check("passed", "Name", "Application name is available"))

    for container in payload.get("containers") or []:
        image = (container.get("image") or "").strip()
        tag = (container.get("tag") or "latest").strip()
        full = image if ":" in image.rsplit("/", 1)[-1] else f"{image}:{tag}"
        if image and IMAGE_RE.match(full):
            checks.append(_check("passed", "Image", f"Image format valid: {full}"))
        elif image:
            checks.append(_check("warning", "Image", f"Image format may be invalid: {full}", "Verify image reference before deploy"))

    storage = payload.get("storage") or {}
    if storage.get("pvcMode") == "existing":
        pvc_name = storage.get("existingPvc") or ""
        if pvc_name and _resource_exists(access, "pvc", pvc_name, namespace):
            checks.append(_check("passed", "Storage", f"PVC '{pvc_name}' exists"))
        elif pvc_name:
            checks.append(_check("failed", "Storage", f"PVC '{pvc_name}' not found"))
    elif storage.get("pvcMode") == "new":
        sc = (storage.get("newPvc") or {}).get("storageClass") or ""
        if sc:
            if _storage_class_exists(access, sc):
                checks.append(_check("passed", "Storage", f"StorageClass '{sc}' exists"))
            else:
                checks.append(_check("failed", "Storage", f"StorageClass '{sc}' not found"))
        else:
            checks.append(_check("warning", "Storage", "No StorageClass specified", "Cluster default will be used if available"))

    environment = payload.get("environment") or {}
    for ref in environment.get("configMapRefs") or []:
        cm = ref.get("name")
        if cm:
            if _resource_exists(access, "configmap", cm, namespace):
                checks.append(_check("passed", "ConfigMaps", f"ConfigMap '{cm}' exists"))
            else:
                checks.append(_check("failed", "ConfigMaps", f"ConfigMap '{cm}' not found"))

    for ref in environment.get("secretRefs") or []:
        sec = ref.get("name")
        if sec:
            if _resource_exists(access, "secret", sec, namespace):
                checks.append(_check("passed", "Secrets", f"Secret '{sec}' exists"))
            else:
                checks.append(_check("failed", "Secrets", f"Secret '{sec}' not found"))

    networking = payload.get("networking") or {}
    if (networking.get("ingress") or {}).get("enabled"):
        available, detail = _ingress_controller_available(access)
        if available:
            checks.append(_check("passed", "Networking", "Ingress controller available", detail))
        else:
            checks.append(_check("warning", "Networking", "Ingress controller not detected", detail))

    svc = networking.get("service") or {}
    if svc.get("enabled"):
        port = int(svc.get("port") or 0)
        target = int(svc.get("targetPort") or port)
        if 1 <= port <= 65535 and 1 <= target <= 65535:
            checks.append(_check("passed", "Networking", f"Service ports valid ({port} → {target})"))
        else:
            checks.append(_check("failed", "Networking", "Service ports must be between 1 and 65535"))

    hpa = (payload.get("scaling") or {}).get("hpa") or {}
    if hpa.get("enabled"):
        if metrics_server_available(access):
            checks.append(_check("passed", "Scaling", "Metrics Server is available for HPA"))
        else:
            checks.append(_check("warning", "Scaling", "Metrics Server not available", "HPA may not function until Metrics Server is installed"))

    passed = sum(1 for c in checks if c["status"] == "passed")
    warnings = sum(1 for c in checks if c["status"] == "warning")
    failed = sum(1 for c in checks if c["status"] == "failed")

    return {
        "checks": checks,
        "passed": passed,
        "warnings": warnings,
        "failed": failed,
        "yaml": yaml_content,
        "summary": summary,
    }, None, 200
