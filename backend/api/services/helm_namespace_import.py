"""Turn a live cluster namespace into a reusable Helm chart (helmify-style).

The "Cluster Namespace" source of the Import Helm Chart dialog reads what is
actually running in a namespace, drops everything the cluster generated for
itself, and hands the survivors to the same manifest-to-chart converter the
YAML, Git and archive sources already use — so images, replicas, env values,
ports, hosts and Secret keys become configurable chart values for free.

Discovery (the preview list) and the import itself share one snapshot function,
so what the operator unticks in the preview is exactly what the import leaves
out. Secrets keep their key names but never their values: the converter blanks
them and turns each key into a required field, which is why a chart built from a
production namespace can be stored without storing a single credential.
"""

from __future__ import annotations

import json
from copy import deepcopy
from typing import Any, Dict, List, Optional, Sequence, Tuple

import yaml

from .helm_chart_template_service import (
    ChartTemplateError,
    MAX_IMPORT_BYTES,
    _clean_text,
    _convert_manifest_files,
    _persist,
    _slugify,
)

# kubectl resource names read for an import, in the order they should appear in
# the preview. Deliberately curated: cluster-generated kinds (pods, replicasets,
# endpoints, events) never belong in a chart, and an allowlist keeps the kubectl
# argument list closed.
NAMESPACE_IMPORT_RESOURCES: Tuple[str, ...] = (
    "deployments",
    "statefulsets",
    "daemonsets",
    "cronjobs",
    "services",
    "ingresses",
    "configmaps",
    "secrets",
    "persistentvolumeclaims",
    "serviceaccounts",
    "roles",
    "rolebindings",
    "horizontalpodautoscalers",
    "poddisruptionbudgets",
    "networkpolicies",
)

# Sort weight for the preview so workloads lead and RBAC/policy trails.
_KIND_ORDER = {
    "Deployment": 0,
    "StatefulSet": 1,
    "DaemonSet": 2,
    "CronJob": 3,
    "Service": 4,
    "Ingress": 5,
    "ConfigMap": 6,
    "Secret": 7,
    "PersistentVolumeClaim": 8,
    "ServiceAccount": 9,
    "Role": 10,
    "RoleBinding": 11,
    "HorizontalPodAutoscaler": 12,
    "PodDisruptionBudget": 13,
    "NetworkPolicy": 14,
}

MAX_NAMESPACE_RESOURCES = 250

# Annotations written by the cluster, a controller, or a previous Helm release.
# Carrying them into a chart either breaks the install (release ownership) or
# pins the chart to the namespace it came from.
_ANNOTATION_PREFIXES = (
    "autoscaling.alpha.kubernetes.io/",
    "cni.projectcalico.org/",
    "control-plane.alpha.kubernetes.io/",
    "endpoints.kubernetes.io/",
    "kubectl.kubernetes.io/",
    "meta.helm.sh/",
    "pv.kubernetes.io/",
    "volume.beta.kubernetes.io/",
    "volume.kubernetes.io/",
)
_ANNOTATION_KEYS = {
    "deployment.kubernetes.io/revision",
    "kubernetes.io/service-account.uid",
}
# Labels a previous Helm release stamped on the object.
_LABEL_KEYS = {"helm.sh/chart", "app.kubernetes.io/version"}

_METADATA_KEYS = {
    "creationTimestamp",
    "deletionGracePeriodSeconds",
    "deletionTimestamp",
    "finalizers",
    "generation",
    "managedFields",
    "namespace",
    "ownerReferences",
    "resourceVersion",
    "selfLink",
    "uid",
}

# API-server defaults. Dropped only when the value still equals the default, so
# a pod spec that deliberately sets something else keeps it.
_POD_SPEC_DEFAULTS = {
    "dnsPolicy": "ClusterFirst",
    "restartPolicy": "Always",
    "schedulerName": "default-scheduler",
    "terminationGracePeriodSeconds": 30,
    "securityContext": {},
    "deprecatedServiceAccount": None,
}
_CONTAINER_DEFAULTS = {
    "terminationMessagePath": "/dev/termination-log",
    "terminationMessagePolicy": "File",
}

_SKIP_SECRET_TYPES = {"kubernetes.io/service-account-token", "helm.sh/release.v1"}


class NamespaceImportError(ValueError):
    pass


def _drop_defaults(target: Any, defaults: Dict[str, Any]) -> None:
    if not isinstance(target, dict):
        return
    for key, default in defaults.items():
        if key not in target:
            continue
        if default is None or target[key] == default:
            target.pop(key, None)


def _clean_metadata(doc: Dict[str, Any]) -> None:
    metadata = doc.get("metadata")
    if not isinstance(metadata, dict):
        return
    for key in _METADATA_KEYS:
        metadata.pop(key, None)
    annotations = metadata.get("annotations")
    if isinstance(annotations, dict):
        for key in list(annotations):
            if key in _ANNOTATION_KEYS or key.startswith(_ANNOTATION_PREFIXES):
                annotations.pop(key, None)
        if not annotations:
            metadata.pop("annotations", None)
    labels = metadata.get("labels")
    if isinstance(labels, dict):
        for key in list(labels):
            if key in _LABEL_KEYS:
                labels.pop(key, None)
        if labels.get("app.kubernetes.io/managed-by") == "Helm":
            labels.pop("app.kubernetes.io/managed-by", None)
        if not labels:
            metadata.pop("labels", None)


def _pod_template(doc: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    spec = doc.get("spec")
    if not isinstance(spec, dict):
        return None
    if doc.get("kind") == "CronJob":
        job = ((spec.get("jobTemplate") or {}).get("spec") or {})
        template = job.get("template")
    else:
        template = spec.get("template")
    return template if isinstance(template, dict) else None


def _scrub(doc: Dict[str, Any]) -> Dict[str, Any]:
    """Strip everything the cluster added so the object is portable again."""
    cleaned = deepcopy(doc)
    cleaned.pop("status", None)
    _clean_metadata(cleaned)
    kind = str(cleaned.get("kind") or "")
    spec = cleaned.get("spec") if isinstance(cleaned.get("spec"), dict) else {}

    template = _pod_template(cleaned)
    if template is not None:
        _clean_metadata(template)
        pod_spec = template.get("spec")
        if isinstance(pod_spec, dict):
            _drop_defaults(pod_spec, _POD_SPEC_DEFAULTS)
            for group in ("containers", "initContainers"):
                for container in pod_spec.get(group) or []:
                    _drop_defaults(container, _CONTAINER_DEFAULTS)

    if kind == "Service":
        for field in ("clusterIP", "clusterIPs", "healthCheckNodePort", "ipFamilies",
                      "ipFamilyPolicy"):
            spec.pop(field, None)
        for port in spec.get("ports") or []:
            if isinstance(port, dict):
                port.pop("nodePort", None)
    elif kind == "PersistentVolumeClaim":
        spec.pop("volumeName", None)
    elif kind == "ServiceAccount":
        # Auto-generated token references; recreated by the cluster on install.
        cleaned.pop("secrets", None)
    elif kind == "CronJob":
        spec.pop("lastScheduleTime", None)

    return cleaned


def _owner_label(doc: Dict[str, Any]) -> str:
    owners = (doc.get("metadata") or {}).get("ownerReferences") or []
    for owner in owners:
        if isinstance(owner, dict) and owner.get("kind"):
            return f"{owner.get('kind')}/{owner.get('name') or '?'}"
    return ""


def _helm_release(doc: Dict[str, Any]) -> str:
    metadata = doc.get("metadata") or {}
    annotations = metadata.get("annotations") or {}
    labels = metadata.get("labels") or {}
    release = annotations.get("meta.helm.sh/release-name")
    if release:
        return str(release)
    if labels.get("app.kubernetes.io/managed-by") == "Helm":
        return str(labels.get("app.kubernetes.io/instance") or "an existing release")
    return ""


def _skip_reason(doc: Dict[str, Any]) -> str:
    """Why this live object must not become part of the chart (empty = keep)."""
    kind = str(doc.get("kind") or "")
    name = str((doc.get("metadata") or {}).get("name") or "")

    owner = _owner_label(doc)
    if owner:
        return f"Created and managed by {owner}"
    release = _helm_release(doc)
    if release:
        return f"Already part of Helm release {release}"
    if kind == "ServiceAccount" and name == "default":
        return "Cluster-provided default ServiceAccount"
    if kind == "ConfigMap" and name == "kube-root-ca.crt":
        return "Cluster-injected CA bundle"
    if kind == "Secret":
        secret_type = str(doc.get("type") or "")
        if secret_type in _SKIP_SECRET_TYPES:
            return f"Cluster-managed Secret ({secret_type})"
    return ""


def _mock_namespace_objects(namespace: str) -> List[Dict[str, Any]]:
    """A representative namespace for mock mode (demos and local verification)."""
    app = _slugify(namespace) or "sample-app"
    return [
        {
            "apiVersion": "apps/v1",
            "kind": "Deployment",
            "metadata": {
                "name": f"{app}-api",
                "namespace": namespace,
                "labels": {"app": f"{app}-api"},
                "annotations": {"deployment.kubernetes.io/revision": "4"},
                "uid": "00000000-0000-0000-0000-000000000001",
                "resourceVersion": "184223",
                "creationTimestamp": "2026-01-04T09:11:00Z",
            },
            "spec": {
                "replicas": 3,
                "selector": {"matchLabels": {"app": f"{app}-api"}},
                "template": {
                    "metadata": {
                        "labels": {"app": f"{app}-api"},
                        "creationTimestamp": None,
                    },
                    "spec": {
                        "restartPolicy": "Always",
                        "dnsPolicy": "ClusterFirst",
                        "schedulerName": "default-scheduler",
                        "terminationGracePeriodSeconds": 30,
                        "containers": [
                            {
                                "name": "api",
                                "image": f"ghcr.io/mock/{app}:v2.8.1",
                                "terminationMessagePath": "/dev/termination-log",
                                "terminationMessagePolicy": "File",
                                "ports": [{"containerPort": 8080}],
                                "env": [
                                    {"name": "LOG_LEVEL", "value": "info"},
                                    {"name": "DB_PASSWORD", "value": "s3cr3t-in-cluster"},
                                ],
                                "resources": {
                                    "requests": {"cpu": "250m", "memory": "256Mi"},
                                    "limits": {"cpu": "500m", "memory": "512Mi"},
                                },
                            }
                        ],
                    },
                },
            },
            "status": {"readyReplicas": 3},
        },
        {
            "apiVersion": "v1",
            "kind": "Service",
            "metadata": {"name": f"{app}-api", "namespace": namespace},
            "spec": {
                "type": "ClusterIP",
                "clusterIP": "10.96.44.19",
                "clusterIPs": ["10.96.44.19"],
                "ipFamilies": ["IPv4"],
                "selector": {"app": f"{app}-api"},
                "ports": [{"name": "http", "port": 80, "targetPort": 8080}],
            },
            "status": {"loadBalancer": {}},
        },
        {
            "apiVersion": "networking.k8s.io/v1",
            "kind": "Ingress",
            "metadata": {"name": f"{app}-api", "namespace": namespace},
            "spec": {
                "rules": [
                    {
                        "host": f"{app}.example.com",
                        "http": {
                            "paths": [
                                {
                                    "path": "/",
                                    "pathType": "Prefix",
                                    "backend": {
                                        "service": {
                                            "name": f"{app}-api",
                                            "port": {"number": 80},
                                        }
                                    },
                                }
                            ]
                        },
                    }
                ]
            },
        },
        {
            "apiVersion": "v1",
            "kind": "ConfigMap",
            "metadata": {"name": f"{app}-config", "namespace": namespace},
            "data": {"app.properties": "timeout=30\nretries=3\n"},
        },
        {
            "apiVersion": "v1",
            "kind": "Secret",
            "metadata": {"name": f"{app}-credentials", "namespace": namespace},
            "type": "Opaque",
            "data": {"DB_PASSWORD": "czNjcjN0", "API_TOKEN": "dG9rZW4="},
        },
        {
            "apiVersion": "v1",
            "kind": "PersistentVolumeClaim",
            "metadata": {"name": f"{app}-data", "namespace": namespace},
            "spec": {
                "accessModes": ["ReadWriteOnce"],
                "resources": {"requests": {"storage": "20Gi"}},
                "volumeName": "pvc-9f2c1e77-mock",
            },
        },
        # Deliberately skipped by the scrub rules, so the preview shows why.
        {
            "apiVersion": "v1",
            "kind": "ServiceAccount",
            "metadata": {"name": "default", "namespace": namespace},
        },
        {
            "apiVersion": "v1",
            "kind": "ConfigMap",
            "metadata": {"name": "kube-root-ca.crt", "namespace": namespace},
            "data": {"ca.crt": "-----BEGIN CERTIFICATE-----"},
        },
        {
            "apiVersion": "apps/v1",
            "kind": "Deployment",
            "metadata": {
                "name": f"{app}-legacy",
                "namespace": namespace,
                "labels": {"app.kubernetes.io/managed-by": "Helm"},
                "annotations": {"meta.helm.sh/release-name": f"{app}-legacy"},
            },
            "spec": {"replicas": 1, "template": {"spec": {"containers": []}}},
        },
    ]


def _fetch_namespace_objects(access, namespace: str) -> Tuple[List[Dict[str, Any]], List[str]]:
    """One kubectl read for every supported kind, falling back per kind."""
    from ..k8s_provider import K8sCommandError, _run_for_access

    warnings: List[str] = []
    joined = ",".join(NAMESPACE_IMPORT_RESOURCES)
    try:
        output = _run_for_access(
            access, ["get", joined, "-n", namespace, "-o", "json", "--ignore-not-found"]
        )
        return list(json.loads(output or "{}").get("items") or []), warnings
    except (K8sCommandError, json.JSONDecodeError):
        # An older API server without one of these kinds fails the whole batch,
        # so fall back to reading each kind on its own and note what was missing.
        items: List[Dict[str, Any]] = []
        for resource in NAMESPACE_IMPORT_RESOURCES:
            try:
                output = _run_for_access(
                    access,
                    ["get", resource, "-n", namespace, "-o", "json", "--ignore-not-found"],
                )
                items.extend(json.loads(output or "{}").get("items") or [])
            except (K8sCommandError, json.JSONDecodeError):
                warnings.append(f"{resource} could not be read from this cluster and were skipped.")
        return items, warnings


def _snapshot(
    cluster_id: str, namespace: str, user=None
) -> Tuple[List[Dict[str, Any]], List[str]]:
    """Every candidate object in the namespace, scrubbed and marked keep/skip."""
    from ..access_engine import can_access_cluster, can_access_namespace
    from ..k8s_provider import resolve_cluster_access, should_use_real_k8s

    if not cluster_id or not namespace:
        raise NamespaceImportError("Select a cluster and a namespace first.")
    if user and not can_access_cluster(user, cluster_id):
        raise PermissionError("Forbidden")
    if user and not can_access_namespace(user, cluster_id, namespace):
        raise PermissionError("Forbidden")

    if should_use_real_k8s(cluster_id):
        access = resolve_cluster_access(cluster_id)
        if not access:
            raise NamespaceImportError("Cluster not found or no kubeconfig is stored for it.")
        raw, warnings = _fetch_namespace_objects(access, namespace)
    else:
        raw, warnings = _mock_namespace_objects(namespace), []

    entries: List[Dict[str, Any]] = []
    for doc in raw:
        if not isinstance(doc, dict) or not doc.get("kind"):
            continue
        kind = str(doc.get("kind"))
        name = str((doc.get("metadata") or {}).get("name") or "")
        if not name:
            continue
        reason = _skip_reason(doc)
        entries.append(
            {
                "kind": kind,
                "name": name,
                "apiVersion": str(doc.get("apiVersion") or ""),
                "skipped": reason,
                "doc": None if reason else _scrub(doc),
            }
        )

    entries.sort(key=lambda item: (_KIND_ORDER.get(item["kind"], 99), item["name"]))
    if len(entries) > MAX_NAMESPACE_RESOURCES:
        warnings.append(
            f"The namespace holds more than {MAX_NAMESPACE_RESOURCES} objects; only the "
            f"first {MAX_NAMESPACE_RESOURCES} are offered for import."
        )
        entries = entries[:MAX_NAMESPACE_RESOURCES]
    return entries, warnings


def _public_entries(entries: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
    return [
        {key: value for key, value in entry.items() if key != "doc"}
        for entry in entries
    ]


def discover_namespace_resources(
    payload: Dict[str, Any], user=None
) -> Tuple[Optional[Dict[str, Any]], Optional[str], int]:
    """Preview list for the dialog: what would be imported, and what is skipped."""
    cluster_id = _clean_text(payload.get("clusterId") or payload.get("cluster"), 200)
    namespace = _clean_text(payload.get("namespace"), 253)
    try:
        entries, warnings = _snapshot(cluster_id, namespace, user=user)
    except PermissionError:
        return None, "Forbidden", 403
    except NamespaceImportError as exc:
        return None, str(exc), 400

    importable = [entry for entry in entries if not entry["skipped"]]
    if not importable:
        warnings.append("Nothing in this namespace can be turned into a chart.")
    return (
        {
            "clusterId": cluster_id,
            "namespace": namespace,
            "resources": _public_entries(entries),
            "importableCount": len(importable),
            "skippedCount": len(entries) - len(importable),
            "warnings": warnings,
        },
        None,
        200,
    )


def _selected_keys(payload: Dict[str, Any]) -> Optional[set]:
    raw = payload.get("resources")
    if not isinstance(raw, list):
        return None
    keys = set()
    for item in raw:
        if isinstance(item, dict) and item.get("kind") and item.get("name"):
            keys.add((str(item["kind"]), str(item["name"])))
    return keys


def import_namespace_chart(
    payload: Dict[str, Any], actor_user_id: Optional[int] = None, user=None
) -> Tuple[Optional[Dict[str, Any]], Optional[str], int]:
    cluster_id = _clean_text(payload.get("clusterId") or payload.get("cluster"), 200)
    namespace = _clean_text(payload.get("namespace"), 253)
    try:
        entries, warnings = _snapshot(cluster_id, namespace, user=user)
    except PermissionError:
        return None, "Forbidden", 403
    except NamespaceImportError as exc:
        return None, str(exc), 400

    selected = _selected_keys(payload)
    source_files: List[Tuple[str, str]] = []
    total_bytes = 0
    for entry in entries:
        if entry["skipped"] or not entry["doc"]:
            continue
        if selected is not None and (entry["kind"], entry["name"]) not in selected:
            continue
        content = yaml.safe_dump(entry["doc"], sort_keys=False, default_flow_style=False)
        total_bytes += len(content.encode("utf-8"))
        if total_bytes > MAX_IMPORT_BYTES:
            return None, "The selected resources exceed the 10 MiB import limit.", 400
        source_files.append(
            (f"{_slugify(entry['kind'])}-{_slugify(entry['name'])}.yaml", content)
        )

    if not source_files:
        return None, "Select at least one resource to import.", 400

    try:
        name, files, values, variables, convert_warnings, count = _convert_manifest_files(
            source_files, requested_name=_clean_text(payload.get("name"), 120)
        )
        data = _persist(
            name=name,
            description=_clean_text(payload.get("description"), 500)
            or f"Captured from namespace {namespace}",
            version="0.1.0",
            app_version="1.0.0",
            source_type="namespace",
            source_ref=f"{cluster_id}/{namespace}",
            files=files,
            values=values,
            variables=variables,
            warnings=warnings + convert_warnings,
            resource_count=count,
            actor_user_id=actor_user_id,
        )
        return data, None, 201
    except ChartTemplateError as exc:
        return None, str(exc), 400
