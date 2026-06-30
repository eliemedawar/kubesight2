"""Live Kubernetes resource pickers for the Deploy From Blueprint wizard.

Every endpoint degrades gracefully: outside real-k8s mode (or on a transient
kubectl error) it returns an empty list with ``live: False`` and HTTP 200 so the
wizard can fall back to generated names / manual entry instead of hard-failing.
RBAC is enforced the same way as the App Services picker.
"""

from __future__ import annotations

import json
from typing import Any, Dict, List, Optional, Tuple

# kubectl resource for each picker kind (whitelisted to avoid arbitrary args).
NAMESPACED_KINDS = {
    "deployments": "deployments",
    "statefulsets": "statefulsets",
    "daemonsets": "daemonsets",
    "cronjobs": "cronjobs",
    "services": "services",
    "secrets": "secrets",
    "configmaps": "configmaps",
    "pvcs": "persistentvolumeclaims",
}
CLUSTER_KINDS = {
    "storageclasses": "storageclasses",
    "ingressclasses": "ingressclasses",
}

# Optional secret.type filters for the secret picker.
SECRET_TYPE_FILTERS = {
    "tls": "kubernetes.io/tls",
    "dockerconfig": "kubernetes.io/dockerconfigjson",
}


def _empty(extra: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    return {"items": [], "count": 0, "live": False, **(extra or {})}


def _check_access(user, cluster_id: str, namespace: Optional[str] = None) -> Optional[str]:
    from ..access_engine import can_access_cluster, can_access_namespace

    if not user:
        return None
    if not can_access_cluster(user, cluster_id):
        return "Forbidden"
    if namespace and not can_access_namespace(user, cluster_id, namespace):
        return "Forbidden"
    return None


def list_namespaces(cluster_id: str, user=None) -> Tuple[Optional[Dict[str, Any]], Optional[str], int]:
    from ..k8s_provider import (
        K8sCommandError, _run_for_access, resolve_cluster_access, should_use_real_k8s,
    )

    if not cluster_id:
        return None, "clusterId is required.", 400
    forbidden = _check_access(user, cluster_id)
    if forbidden:
        return None, forbidden, 403
    if not should_use_real_k8s(cluster_id):
        return _empty(), None, 200
    access = resolve_cluster_access(cluster_id)
    if not access:
        return _empty(), None, 200
    try:
        output = _run_for_access(access, ["get", "namespaces", "-o", "json"])
        items = json.loads(output).get("items", [])
        names = sorted(
            i.get("metadata", {}).get("name", "")
            for i in items if i.get("metadata", {}).get("name")
        )
        return {"items": names, "count": len(names), "live": True}, None, 200
    except (K8sCommandError, Exception):
        return _empty(), None, 200


def list_namespaced_resources(
    cluster_id: str,
    namespace: str,
    kind: str,
    user=None,
    secret_type: Optional[str] = None,
) -> Tuple[Optional[Dict[str, Any]], Optional[str], int]:
    from ..k8s_provider import (
        K8sCommandError, _run_for_access, resolve_cluster_access, should_use_real_k8s,
    )

    resource = NAMESPACED_KINDS.get((kind or "").lower())
    if not resource:
        return None, f"Unsupported kind '{kind}'.", 400
    if not cluster_id or not namespace:
        return None, "clusterId and namespace are required.", 400
    forbidden = _check_access(user, cluster_id, namespace)
    if forbidden:
        return None, forbidden, 403
    if not should_use_real_k8s(cluster_id):
        return _empty(), None, 200
    access = resolve_cluster_access(cluster_id)
    if not access:
        return _empty(), None, 200
    try:
        output = _run_for_access(access, ["get", resource, "-n", namespace, "-o", "json"])
        items = json.loads(output).get("items", [])
        type_filter = SECRET_TYPE_FILTERS.get((secret_type or "").lower()) if resource == "secrets" else None
        names: List[str] = []
        for item in items:
            name = item.get("metadata", {}).get("name")
            if not name:
                continue
            if type_filter and item.get("type") != type_filter:
                continue
            names.append(name)
        names.sort()
        return {"items": names, "count": len(names), "live": True}, None, 200
    except (K8sCommandError, Exception):
        return _empty(), None, 200


def list_cluster_resources(
    cluster_id: str,
    kind: str,
    user=None,
) -> Tuple[Optional[Dict[str, Any]], Optional[str], int]:
    from ..k8s_provider import (
        K8sCommandError, _run_for_access, resolve_cluster_access, should_use_real_k8s,
    )

    resource = CLUSTER_KINDS.get((kind or "").lower())
    if not resource:
        return None, f"Unsupported kind '{kind}'.", 400
    if not cluster_id:
        return None, "clusterId is required.", 400
    forbidden = _check_access(user, cluster_id)
    if forbidden:
        return None, forbidden, 403
    if not should_use_real_k8s(cluster_id):
        return _empty(), None, 200
    access = resolve_cluster_access(cluster_id)
    if not access:
        return _empty(), None, 200
    try:
        output = _run_for_access(access, ["get", resource, "-o", "json"])
        items = json.loads(output).get("items", [])
        names = sorted(
            i.get("metadata", {}).get("name", "")
            for i in items if i.get("metadata", {}).get("name")
        )
        return {"items": names, "count": len(names), "live": True}, None, 200
    except (K8sCommandError, Exception):
        return _empty(), None, 200
