"""Bring workloads from an existing cluster into one KubeSight builds.

The builder can copy a whole namespace, or individual workloads, out of any
cluster KubeSight already manages and apply them to the cluster it just built
(phase ``workloads``, after ``addons``).

Three rules shape this module:

* **A copy has to be able to run.** A picked Deployment travels with everything
  its pod spec references — ConfigMaps, Secrets, PVCs, its ServiceAccount, the
  Services that select it and the Ingresses that route to those Services — so
  the copy starts on its own rather than landing as a Pending pod.
* **Nothing cluster-assigned comes along.** Live objects carry identity and
  scheduling state (uid, resourceVersion, clusterIP, nodePort, bound
  volumeName, status). Copying those either fails apply or silently ties the
  copy to the source cluster, so :func:`_clean` strips them.
* **A missing image is a warning, never a block.** The image check answers "is
  this in the registry you picked?" and the answer is reported per workload;
  the operator decides whether to drop those workloads or bring them anyway.
  Registry enforcement (which blocks deploys elsewhere in KubeSight) is
  deliberately not applied here — this is a copy of what already runs.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional, Tuple

from ...models import ClusterBuild, RegistryConnection
from ...secret_encryption import decrypt_secret
from .. import registry_client
from . import storage

# Individually pickable workload kinds, in apply order.
WORKLOAD_KINDS: Dict[str, str] = {
    "Deployment": "deployments",
    "StatefulSet": "statefulsets",
    "DaemonSet": "daemonsets",
    "CronJob": "cronjobs",
}

# Everything a workload can depend on that is safe to copy. Kept to a
# whitelist: an unknown kind is more likely to be a controller-owned artifact
# (ReplicaSet, Job, EndpointSlice) than something worth carrying over.
SUPPORT_KINDS: Dict[str, str] = {
    "ServiceAccount": "serviceaccounts",
    "Secret": "secrets",
    "ConfigMap": "configmaps",
    "PersistentVolumeClaim": "persistentvolumeclaims",
    "Service": "services",
    "Ingress": "ingresses",
}

# Apply order: identity and data before the things that mount them, workloads
# before the Ingresses that route to their Services.
APPLY_ORDER = [
    "Namespace",
    "ServiceAccount",
    "Secret",
    "ConfigMap",
    "PersistentVolumeClaim",
    "Service",
    *WORKLOAD_KINDS,
    "Ingress",
]

# Secrets that exist only because of the source cluster. A service-account
# token is minted per cluster (and is useless in another one), and a Helm
# release record would make the new cluster look like it owns a release it
# never installed.
_SKIP_SECRET_TYPES = {
    "kubernetes.io/service-account-token",
    "helm.sh/release.v1",
}

# ConfigMaps every namespace already has from its own kube-root CA.
_SKIP_CONFIGMAP_NAMES = {"kube-root-ca.crt"}

# Namespaces whose contents belong to the cluster, not to an application.
SYSTEM_NAMESPACES = {
    "kube-system",
    "kube-public",
    "kube-node-lease",
    "local-path-storage",
}

# A selection large enough to be a mistake rather than an intent.
MAX_ITEMS = 200

# Image-check statuses (mirrors registry_client's vocabulary plus "skipped").
FOUND = registry_client.FOUND
NOT_FOUND = registry_client.NOT_FOUND
UNREACHABLE = registry_client.UNREACHABLE
NOT_CHECKED = "not_checked"


def _count(number: int, noun: str, plural: str = "") -> str:
    """"1 workload" / "3 workloads" — the page says these out loud."""
    return f"{number} {noun if number == 1 else (plural or noun + 's')}"


class WorkloadSourceError(RuntimeError):
    """The source cluster could not be read."""


# ---------------------------------------------------------------------------
# Access + source cluster reads
# ---------------------------------------------------------------------------

def _check_access(user, cluster_id: str, namespace: Optional[str] = None) -> None:
    from ...access_engine import can_access_cluster, can_access_namespace

    if not user:
        return
    if not can_access_cluster(user, cluster_id):
        raise PermissionError("You do not have access to that cluster.")
    if namespace and not can_access_namespace(user, cluster_id, namespace):
        raise PermissionError("You do not have access to that namespace.")


def _kubectl_json(cluster_id: str, args: List[str], *, timeout: int = 60) -> Dict[str, Any]:
    """Run a read-only kubectl against a cluster KubeSight manages."""
    from ...k8s_provider import (
        K8sCommandError,
        _run_for_access,
        resolve_cluster_access,
        should_use_real_k8s,
    )

    if not should_use_real_k8s(cluster_id):
        raise WorkloadSourceError(
            f"Cluster '{cluster_id}' is not connected to a live Kubernetes API, "
            "so its workloads cannot be read."
        )
    access = resolve_cluster_access(cluster_id)
    if not access:
        raise WorkloadSourceError(f"Cluster '{cluster_id}' is no longer registered.")
    try:
        output = _run_for_access(access, args, timeout=timeout)
    except K8sCommandError as exc:
        raise WorkloadSourceError(str(exc)) from exc
    try:
        return json.loads(output or "{}")
    except json.JSONDecodeError as exc:
        raise WorkloadSourceError(
            f"Unexpected output from the source cluster: {exc}"
        ) from exc


def list_sources(user=None) -> Dict[str, Any]:
    """Clusters that can act as a workload source.

    Only clusters with a live API are usable, so the unusable ones are returned
    too (flagged) rather than hidden — "my cluster is missing from the list" is
    worse than "my cluster is listed as not connected".
    """
    from ...k8s_provider import should_use_real_k8s

    items: List[Dict[str, Any]] = []
    raw: List[Dict[str, Any]] = []
    try:
        from ...k8s_provider import _custom_clusters_as_items, list_clusters_from_k8s

        if should_use_real_k8s():
            try:
                raw += list(list_clusters_from_k8s().get("items") or [])
            except Exception:  # noqa: BLE001 — a broken kubeconfig must not empty the list
                pass
        else:
            # Mock mode: list the demo clusters too, flagged as not connected,
            # so the picker explains itself instead of looking broken.
            from ...mock_data import CLUSTERS

            raw += list(CLUSTERS or [])
        try:
            raw += _custom_clusters_as_items()
        except Exception:  # noqa: BLE001
            pass
    except Exception:  # noqa: BLE001
        raw = []

    if user:
        from ...access_engine import filter_clusters_for_user

        raw = filter_clusters_for_user(user, raw)

    seen = set()
    for item in raw:
        cluster_id = str(item.get("id") or "")
        if not cluster_id or cluster_id in seen:
            continue
        seen.add(cluster_id)
        items.append({
            "id": cluster_id,
            "name": item.get("name") or cluster_id,
            "live": bool(should_use_real_k8s(cluster_id)),
        })
    items.sort(key=lambda row: (not row["live"], row["name"].lower()))
    return {"items": items, "count": len(items)}


def list_namespaces(cluster_id: str, user=None) -> Dict[str, Any]:
    """Namespaces of the source cluster with a per-kind workload count.

    One list call covers every kind, so opening the picker costs a single
    round-trip rather than one per namespace.
    """
    _check_access(user, cluster_id)
    resources = ",".join(WORKLOAD_KINDS.values())
    payload = _kubectl_json(
        cluster_id, ["get", resources, "--all-namespaces", "-o", "json"]
    )
    counts: Dict[str, Dict[str, int]] = {}
    for item in payload.get("items") or []:
        namespace = (item.get("metadata") or {}).get("namespace") or ""
        kind = item.get("kind") or ""
        if not namespace or kind not in WORKLOAD_KINDS:
            continue
        counts.setdefault(namespace, {})
        counts[namespace][kind] = counts[namespace].get(kind, 0) + 1

    from ...access_engine import can_access_namespace

    items = []
    for namespace in sorted(counts):
        if user and not can_access_namespace(user, cluster_id, namespace):
            continue
        per_kind = counts[namespace]
        items.append({
            "name": namespace,
            "counts": per_kind,
            "total": sum(per_kind.values()),
            "system": namespace in SYSTEM_NAMESPACES,
        })
    return {"items": items, "count": len(items)}


def list_workloads(cluster_id: str, namespace: str, user=None) -> Dict[str, Any]:
    """Every pickable workload in one namespace, with the images it runs."""
    _check_access(user, cluster_id, namespace)
    resources = ",".join(WORKLOAD_KINDS.values())
    payload = _kubectl_json(
        cluster_id, ["get", resources, "-n", namespace, "-o", "json"]
    )
    items = []
    for doc in payload.get("items") or []:
        kind = doc.get("kind") or ""
        name = (doc.get("metadata") or {}).get("name") or ""
        if kind not in WORKLOAD_KINDS or not name:
            continue
        spec = doc.get("spec") or {}
        items.append({
            "kind": kind,
            "name": name,
            "namespace": namespace,
            "replicas": spec.get("replicas"),
            "schedule": spec.get("schedule"),
            "images": images_of(doc),
        })
    items.sort(key=lambda row: (APPLY_ORDER.index(row["kind"]), row["name"]))
    return {"items": items, "count": len(items)}


# ---------------------------------------------------------------------------
# Selection
# ---------------------------------------------------------------------------

def normalize_selection(payload: Any) -> Optional[Dict[str, Any]]:
    """Validate a workload selection into the shape stored on the build.

    Returns ``None`` for "bring nothing", which is what clearing the step does.
    """
    if not payload:
        return None
    if not isinstance(payload, dict):
        raise ValueError("workloads must be an object.")

    cluster_id = str(payload.get("sourceClusterId") or "").strip()
    raw_items = payload.get("items")
    if raw_items is None:
        raw_items = []
    if not isinstance(raw_items, list):
        raise ValueError("workloads.items must be a list.")
    if not cluster_id:
        if raw_items:
            raise ValueError("workloads.sourceClusterId is required.")
        return None
    if len(raw_items) > MAX_ITEMS:
        raise ValueError(
            f"Select at most {MAX_ITEMS} namespaces/workloads to copy "
            f"(got {len(raw_items)})."
        )

    items: List[Dict[str, Any]] = []
    seen = set()
    for entry in raw_items:
        if not isinstance(entry, dict):
            raise ValueError("Each workload selection must be an object.")
        namespace = str(entry.get("namespace") or "").strip()
        kind = str(entry.get("kind") or "Namespace").strip()
        name = str(entry.get("name") or "").strip()
        if not namespace:
            raise ValueError("Each workload selection needs a namespace.")
        if kind == "Namespace":
            name = ""
        elif kind in WORKLOAD_KINDS:
            if not name:
                raise ValueError(f"A {kind} selection needs a name.")
        else:
            raise ValueError(
                f"Cannot copy kind '{kind}'. Pick a whole namespace or one of: "
                f"{', '.join(WORKLOAD_KINDS)}."
            )
        key = (namespace, kind, name)
        if key in seen:
            continue
        seen.add(key)
        items.append({"namespace": namespace, "kind": kind, "name": name})

    if not items:
        return None

    # A whole-namespace selection subsumes individual picks in that namespace.
    whole = {item["namespace"] for item in items if item["kind"] == "Namespace"}
    items = [
        item for item in items
        if item["kind"] == "Namespace" or item["namespace"] not in whole
    ]
    items.sort(key=lambda item: (item["namespace"], item["kind"], item["name"]))

    selection: Dict[str, Any] = {
        "sourceClusterId": cluster_id,
        "sourceClusterName": str(payload.get("sourceClusterName") or "").strip(),
        "items": items,
    }
    registry_id = payload.get("registryConnectionId")
    if registry_id:
        selection["registryConnectionId"] = int(registry_id)
    ack = payload.get("imageAck")
    if isinstance(ack, dict):
        selection["imageAck"] = ack
    storage_answers = storage.normalize(payload.get("storage"))
    if storage_answers:
        selection["storage"] = storage_answers
    return selection


def replace_selection(existing: Any, payload: Any) -> Optional[Dict[str, Any]]:
    """A new selection, carrying forward what has already been applied.

    Copying workloads can happen more than once against the same cluster (day
    two), so the record of what previously landed survives replacing the
    selection — otherwise the receipt would forget the first copy.
    """
    selection = normalize_selection(payload)
    history = list((existing or {}).get("applied") or []) if isinstance(existing, dict) else []
    if selection is None:
        return {"items": [], "applied": history} if history else None
    if history:
        selection["applied"] = history
    return selection


def selection_of(build: ClusterBuild) -> Optional[Dict[str, Any]]:
    data = build.workloads_json
    return data if isinstance(data, dict) and data.get("items") else None


def summarize(selection: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """Counts for the serializer, cheap enough for the builds list."""
    items = list((selection or {}).get("items") or [])
    applied = list((selection or {}).get("applied") or [])
    return {
        "sourceClusterId": (selection or {}).get("sourceClusterId") or "",
        "sourceClusterName": (selection or {}).get("sourceClusterName") or "",
        "namespaceCount": len({item["namespace"] for item in items}),
        "wholeNamespaces": sorted(
            item["namespace"] for item in items if item.get("kind") == "Namespace"
        ),
        "workloadCount": sum(1 for item in items if item.get("kind") != "Namespace"),
        "itemCount": len(items),
        "appliedRuns": len(applied),
        "lastApplied": applied[-1] if applied else None,
    }


# ---------------------------------------------------------------------------
# Export: read, resolve references, sanitize
# ---------------------------------------------------------------------------

def images_of(doc: Dict[str, Any]) -> List[str]:
    """Every container image a workload document references."""
    images: List[str] = []
    for pod_spec in _pod_specs(doc):
        for field_name in ("initContainers", "containers", "ephemeralContainers"):
            for container in pod_spec.get(field_name) or []:
                image = str((container or {}).get("image") or "").strip()
                if image and image not in images:
                    images.append(image)
    return images


def _pod_specs(doc: Dict[str, Any]) -> List[Dict[str, Any]]:
    spec = doc.get("spec")
    if not isinstance(spec, dict):
        return []
    template = spec.get("template")
    if isinstance(template, dict) and isinstance(template.get("spec"), dict):
        return [template["spec"]]
    job_template = spec.get("jobTemplate")  # CronJob
    if isinstance(job_template, dict):
        inner = (job_template.get("spec") or {}).get("template") or {}
        if isinstance(inner.get("spec"), dict):
            return [inner["spec"]]
    return []


def _pod_template_labels(doc: Dict[str, Any]) -> Dict[str, str]:
    spec = doc.get("spec") or {}
    template = spec.get("template")
    if not isinstance(template, dict):
        job_template = spec.get("jobTemplate") or {}
        template = (job_template.get("spec") or {}).get("template") or {}
    labels = (template.get("metadata") or {}).get("labels")
    if not isinstance(labels, dict):
        return {}
    return {str(key): str(value) for key, value in labels.items()}


_DROP_METADATA = (
    "uid", "resourceVersion", "generation", "creationTimestamp", "selfLink",
    "managedFields", "ownerReferences", "finalizers", "deletionTimestamp",
    "deletionGracePeriodSeconds",
)
_DROP_ANNOTATIONS = (
    "kubectl.kubernetes.io/last-applied-configuration",
    "deployment.kubernetes.io/revision",
    "autoscaling.alpha.kubernetes.io/conditions",
)
_DROP_ANNOTATION_PREFIXES = (
    "pv.kubernetes.io/",
    "volume.beta.kubernetes.io/",
    "volume.kubernetes.io/",
    "kubernetes.io/psp",
)


def _clean(doc: Dict[str, Any]) -> Dict[str, Any]:
    """Strip everything that belongs to the source cluster, not to the object.

    What survives is what a human would have written: apiVersion, kind,
    metadata identity, and spec — with the fields Kubernetes assigns removed so
    the new cluster assigns its own.
    """
    out = {
        "apiVersion": doc.get("apiVersion"),
        "kind": doc.get("kind"),
        "metadata": dict(doc.get("metadata") or {}),
    }
    metadata = out["metadata"]
    for key in _DROP_METADATA:
        metadata.pop(key, None)
    annotations = {
        key: value for key, value in (metadata.get("annotations") or {}).items()
        if key not in _DROP_ANNOTATIONS
        and not key.startswith(_DROP_ANNOTATION_PREFIXES)
    }
    if annotations:
        metadata["annotations"] = annotations
    else:
        metadata.pop("annotations", None)

    kind = doc.get("kind") or ""
    spec = doc.get("spec")
    if isinstance(spec, dict):
        spec = json.loads(json.dumps(spec))  # cheap deep copy
        if kind == "Service":
            # Identity assigned by the source cluster's service CIDR, and node
            # ports allocated from its own range: both are reassigned on apply.
            for key in ("clusterIP", "clusterIPs", "healthCheckNodePort"):
                spec.pop(key, None)
            for port in spec.get("ports") or []:
                if isinstance(port, dict):
                    port.pop("nodePort", None)
        elif kind == "PersistentVolumeClaim":
            # Bound to a PV that only exists over there.
            spec.pop("volumeName", None)
        out["spec"] = spec

    # Top-level payload keys worth carrying. Deliberately NOT a ServiceAccount's
    # `secrets`: those name per-cluster token secrets the new cluster mints for
    # itself, and copying the names would point at objects that never arrive.
    for key in ("data", "stringData", "binaryData", "type", "rules", "subjects", "roleRef"):
        if key in doc:
            out[key] = doc[key]
    return {key: value for key, value in out.items() if value is not None}


@dataclass
class Export:
    """One resolved copy plan: what to apply, and what to say about it."""

    documents: List[Dict[str, Any]] = field(default_factory=list)
    workloads: List[Dict[str, Any]] = field(default_factory=list)
    namespaces: List[str] = field(default_factory=list)
    support: Dict[str, int] = field(default_factory=dict)
    warnings: List[str] = field(default_factory=list)
    missing: List[str] = field(default_factory=list)

    @property
    def claims(self) -> List[Dict[str, Any]]:
        """The copied PersistentVolumeClaims. ``storage`` decides what backs
        each one, so it needs the documents, not just a count."""
        return [
            doc for doc in self.documents
            if doc.get("kind") == "PersistentVolumeClaim"
        ]

    @property
    def images(self) -> List[str]:
        out: List[str] = []
        for workload in self.workloads:
            for image in workload.get("images") or []:
                if image not in out:
                    out.append(image)
        return out


class _Collector:
    """Accumulates documents once each, keyed by (kind, namespace, name)."""

    def __init__(self) -> None:
        self.docs: Dict[Tuple[str, str, str], Dict[str, Any]] = {}

    def add(self, doc: Dict[str, Any]) -> bool:
        kind = doc.get("kind") or ""
        metadata = doc.get("metadata") or {}
        key = (kind, metadata.get("namespace") or "", metadata.get("name") or "")
        if key in self.docs:
            return False
        self.docs[key] = _clean(doc)
        return True

    def has(self, kind: str, namespace: str, name: str) -> bool:
        return (kind, namespace, name) in self.docs

    def ordered(self) -> List[Dict[str, Any]]:
        def rank(key: Tuple[str, str, str]) -> Tuple[int, str, str]:
            kind = key[0]
            return (
                APPLY_ORDER.index(kind) if kind in APPLY_ORDER else len(APPLY_ORDER),
                key[1],
                key[2],
            )
        return [self.docs[key] for key in sorted(self.docs, key=rank)]

    def counts(self) -> Dict[str, int]:
        out: Dict[str, int] = {}
        for kind, _, _ in self.docs:
            out[kind] = out.get(kind, 0) + 1
        return out


def _skip_support(doc: Dict[str, Any]) -> bool:
    kind = doc.get("kind") or ""
    name = (doc.get("metadata") or {}).get("name") or ""
    if kind == "Secret" and str(doc.get("type") or "") in _SKIP_SECRET_TYPES:
        return True
    if kind == "ConfigMap" and name in _SKIP_CONFIGMAP_NAMES:
        return True
    if kind == "ServiceAccount" and name == "default":
        # Every namespace already has one; copying it fights the API server.
        return True
    return False


def _fetch_namespace_support(cluster_id: str, namespace: str) -> List[Dict[str, Any]]:
    payload = _kubectl_json(
        cluster_id,
        ["get", ",".join(SUPPORT_KINDS.values()), "-n", namespace, "-o", "json"],
    )
    return list(payload.get("items") or [])


def _referenced_names(doc: Dict[str, Any]) -> Dict[str, set]:
    """Every ConfigMap/Secret/PVC/ServiceAccount a pod spec names."""
    wanted = {"ConfigMap": set(), "Secret": set(), "PersistentVolumeClaim": set(),
              "ServiceAccount": set()}
    for pod_spec in _pod_specs(doc):
        account = pod_spec.get("serviceAccountName") or pod_spec.get("serviceAccount")
        if account:
            wanted["ServiceAccount"].add(str(account))
        for pull in pod_spec.get("imagePullSecrets") or []:
            if isinstance(pull, dict) and pull.get("name"):
                wanted["Secret"].add(str(pull["name"]))
        for volume in pod_spec.get("volumes") or []:
            if not isinstance(volume, dict):
                continue
            if (volume.get("configMap") or {}).get("name"):
                wanted["ConfigMap"].add(str(volume["configMap"]["name"]))
            if (volume.get("secret") or {}).get("secretName"):
                wanted["Secret"].add(str(volume["secret"]["secretName"]))
            claim = (volume.get("persistentVolumeClaim") or {}).get("claimName")
            if claim:
                wanted["PersistentVolumeClaim"].add(str(claim))
            for source in (volume.get("projected") or {}).get("sources") or []:
                if not isinstance(source, dict):
                    continue
                if (source.get("configMap") or {}).get("name"):
                    wanted["ConfigMap"].add(str(source["configMap"]["name"]))
                if (source.get("secret") or {}).get("name"):
                    wanted["Secret"].add(str(source["secret"]["name"]))
        for field_name in ("initContainers", "containers", "ephemeralContainers"):
            for container in pod_spec.get(field_name) or []:
                if not isinstance(container, dict):
                    continue
                for source in container.get("envFrom") or []:
                    if (source.get("configMapRef") or {}).get("name"):
                        wanted["ConfigMap"].add(str(source["configMapRef"]["name"]))
                    if (source.get("secretRef") or {}).get("name"):
                        wanted["Secret"].add(str(source["secretRef"]["name"]))
                for entry in container.get("env") or []:
                    value_from = (entry or {}).get("valueFrom") or {}
                    if (value_from.get("configMapKeyRef") or {}).get("name"):
                        wanted["ConfigMap"].add(str(value_from["configMapKeyRef"]["name"]))
                    if (value_from.get("secretKeyRef") or {}).get("name"):
                        wanted["Secret"].add(str(value_from["secretKeyRef"]["name"]))
    return wanted


def _selects(selector: Dict[str, Any], labels: Dict[str, str]) -> bool:
    if not selector or not labels:
        return False
    return all(str(labels.get(key)) == str(value) for key, value in selector.items())


def _ingress_service_names(doc: Dict[str, Any]) -> set:
    names = set()
    spec = doc.get("spec") or {}
    default = (spec.get("defaultBackend") or {}).get("service") or {}
    if default.get("name"):
        names.add(str(default["name"]))
    for rule in spec.get("rules") or []:
        for path in ((rule or {}).get("http") or {}).get("paths") or []:
            service = ((path or {}).get("backend") or {}).get("service") or {}
            if service.get("name"):
                names.add(str(service["name"]))
    return names


def _namespace_doc(name: str) -> Dict[str, Any]:
    return {"apiVersion": "v1", "kind": "Namespace", "metadata": {"name": name}}


def export_selection(
    selection: Dict[str, Any], user=None, *, cluster_id: Optional[str] = None
) -> Export:
    """Read the selection out of the source cluster and resolve what it needs.

    Raises :class:`WorkloadSourceError` when the source cluster cannot be read —
    the caller decides whether that is a preflight warning or a phase failure.
    """
    source = cluster_id or str(selection.get("sourceClusterId") or "")
    if not source:
        raise WorkloadSourceError("No source cluster is selected.")
    _check_access(user, source)

    items = list(selection.get("items") or [])
    namespaces = sorted({item["namespace"] for item in items})
    export = Export(namespaces=namespaces)
    collector = _Collector()
    for namespace in namespaces:
        _check_access(user, source, namespace)
        collector.add(_namespace_doc(namespace))

    # One read of each namespace's support objects, reused by every workload in
    # it — a namespace with 20 Deployments must not mean 20 Secret lists.
    support_cache: Dict[str, List[Dict[str, Any]]] = {}

    def support(namespace: str) -> List[Dict[str, Any]]:
        if namespace not in support_cache:
            support_cache[namespace] = _fetch_namespace_support(source, namespace)
        return support_cache[namespace]

    whole_namespaces = [item["namespace"] for item in items if item["kind"] == "Namespace"]
    picked = [item for item in items if item["kind"] != "Namespace"]

    for namespace in whole_namespaces:
        payload = _kubectl_json(
            source,
            ["get", ",".join(WORKLOAD_KINDS.values()), "-n", namespace, "-o", "json"],
        )
        found = list(payload.get("items") or [])
        if not found:
            export.warnings.append(
                f"Namespace {namespace} has no Deployments, StatefulSets, "
                "DaemonSets or CronJobs — only its configuration is copied."
            )
        for doc in found:
            if (doc.get("metadata") or {}).get("ownerReferences"):
                continue  # controller-owned (a Job made by a CronJob)
            collector.add(doc)
            export.workloads.append({
                "namespace": namespace,
                "kind": doc.get("kind") or "",
                "name": (doc.get("metadata") or {}).get("name") or "",
                "images": images_of(doc),
            })
        for doc in support(namespace):
            if not _skip_support(doc):
                collector.add(doc)

    for item in picked:
        namespace, kind, name = item["namespace"], item["kind"], item["name"]
        resource = WORKLOAD_KINDS[kind]
        try:
            doc = _kubectl_json(
                source, ["get", resource, name, "-n", namespace, "-o", "json"]
            )
        except WorkloadSourceError as exc:
            if "NotFound" in str(exc) or "not found" in str(exc).lower():
                export.missing.append(f"{namespace}/{kind} {name}")
                continue
            raise
        if not doc.get("kind"):
            export.missing.append(f"{namespace}/{kind} {name}")
            continue
        collector.add(doc)
        export.workloads.append({
            "namespace": namespace,
            "kind": kind,
            "name": name,
            "images": images_of(doc),
        })

        wanted = _referenced_names(doc)
        labels = _pod_template_labels(doc)
        services: List[str] = []
        for support_doc in support(namespace):
            support_kind = support_doc.get("kind") or ""
            support_name = (support_doc.get("metadata") or {}).get("name") or ""
            if _skip_support(support_doc):
                continue
            if support_kind in wanted and support_name in wanted[support_kind]:
                collector.add(support_doc)
            elif support_kind == "Service" and _selects(
                (support_doc.get("spec") or {}).get("selector") or {}, labels
            ):
                collector.add(support_doc)
                services.append(support_name)
        if services:
            for support_doc in support(namespace):
                if (support_doc.get("kind") == "Ingress"
                        and _ingress_service_names(support_doc) & set(services)):
                    collector.add(support_doc)

        for support_kind, names in wanted.items():
            for wanted_name in sorted(names):
                if support_kind == "ServiceAccount" and wanted_name == "default":
                    continue
                if not collector.has(support_kind, namespace, wanted_name):
                    export.warnings.append(
                        f"{namespace}/{name} references {support_kind} "
                        f"'{wanted_name}', which no longer exists in the source "
                        "cluster — the copy will not start until you create it."
                    )

    export.documents = collector.ordered()
    export.support = {
        kind: count for kind, count in collector.counts().items()
        if kind in SUPPORT_KINDS
    }
    # The PVC caveat is deliberately NOT stated here any more: whether those
    # claims have somewhere to bind is a question `storage` answers per claim,
    # and a blanket "they will stay Pending" would be wrong once it has.
    if export.support.get("Service"):
        export.warnings.append(
            "Cluster IPs and node ports are not copied; the new cluster "
            "allocates its own, so anything hard-coded to the old addresses "
            "needs repointing."
        )
    return export


def apply_documents(
    export: Export, claim_plans: Optional[List[storage.ClaimPlan]] = None
) -> List[Dict[str, Any]]:
    """The export's documents with the storage decisions folded in.

    PersistentVolumes are prepended (they are cluster-scoped, so they land in
    their own bucket which sorts first) and every copied claim is repointed at
    whatever will back it.
    """
    if not claim_plans:
        return list(export.documents)
    by_key = {plan.key: plan for plan in claim_plans}
    volumes = [
        storage.pv_document(plan) for plan in claim_plans if plan.authors_pv
    ]
    out: List[Dict[str, Any]] = list(volumes)
    for doc in export.documents:
        if doc.get("kind") == "PersistentVolumeClaim":
            metadata = doc.get("metadata") or {}
            plan = by_key.get(storage.claim_key(
                str(metadata.get("namespace") or ""), str(metadata.get("name") or "")
            ))
            out.append(storage.rewrite_claim(doc, plan) if plan else doc)
        else:
            out.append(doc)
    return out


def group_by_namespace(
    documents: List[Dict[str, Any]]
) -> List[Tuple[str, List[Dict[str, Any]]]]:
    """Documents bucketed per namespace, each bucket still in apply order.

    One file per namespace keeps a single failure legible ("payments failed")
    and lets the Namespace object lead its own contents — ``kubectl apply``
    honours document order within a file.
    """
    buckets: Dict[str, List[Dict[str, Any]]] = {}
    for doc in documents:
        metadata = doc.get("metadata") or {}
        namespace = (
            metadata.get("name") if doc.get("kind") == "Namespace"
            else metadata.get("namespace") or ""
        )
        buckets.setdefault(str(namespace), []).append(doc)
    return [(namespace, buckets[namespace]) for namespace in sorted(buckets)]


def to_yaml(documents: List[Dict[str, Any]]) -> str:
    """A multi-document manifest. Never logged — it can carry Secret data."""
    import yaml

    return yaml.safe_dump_all(documents, default_flow_style=False, sort_keys=False)


# ---------------------------------------------------------------------------
# Image availability against the chosen registry
# ---------------------------------------------------------------------------

def _registry(registry_id: Optional[int]) -> Optional[RegistryConnection]:
    if not registry_id:
        return None
    row = RegistryConnection.query.filter_by(id=int(registry_id)).first()
    return row


def check_images(
    registry_id: Optional[int], images: Iterable[str]
) -> Tuple[List[Dict[str, Any]], Dict[str, str]]:
    """Is each image present in the registry the operator picked?

    Unlike the deploy-time gate, this asks the *selected* registry about the
    image's repository path even when the image reference names a different
    host: the question here is "can the new cluster pull this from my
    registry?", and an image still pointing at docker.io is exactly the case
    worth reporting.

    Returns ``(checks, by_image)`` where ``by_image`` maps image → status.
    """
    connection = _registry(registry_id)
    checks: List[Dict[str, Any]] = []
    by_image: Dict[str, str] = {}
    seen = set()
    password = decrypt_secret(connection.password_encrypted or "") if connection else ""
    for image in images:
        reference = str(image or "").strip()
        if not reference or reference in seen:
            continue
        seen.add(reference)
        if connection is None:
            checks.append({
                "image": reference,
                "status": NOT_CHECKED,
                "message": "No registry selected, so image availability was not checked.",
                "registry": "",
            })
            by_image[reference] = NOT_CHECKED
            continue
        parsed = registry_client.parse_image_reference(reference)
        if parsed is None:
            checks.append({
                "image": reference,
                "status": NOT_CHECKED,
                "message": "Not a valid image reference.",
                "registry": connection.name or "",
            })
            by_image[reference] = NOT_CHECKED
            continue
        status, message = registry_client.check_manifest(
            connection.base_url,
            parsed.repository,
            parsed.reference,
            username=connection.username or "",
            password=password,
            verify_tls=bool(connection.verify_tls),
            ca_cert=connection.ca_cert,
        )
        checks.append({
            "image": reference,
            "status": status,
            "message": message,
            "registry": connection.name or registry_client.registry_host_of(connection.base_url),
        })
        by_image[reference] = status
    return checks, by_image


def registry_options() -> List[Dict[str, Any]]:
    """Linked registries the workloads step can check against."""
    rows = (
        RegistryConnection.query.filter(RegistryConnection.enabled.is_(True))
        .order_by(RegistryConnection.id.asc())
        .all()
    )
    return [
        {
            "id": row.id,
            "name": row.name or registry_client.registry_host_of(row.base_url),
            "host": registry_client.registry_host_of(row.base_url),
        }
        for row in rows
    ]


def _workload_status(images: List[str], by_image: Dict[str, str]) -> str:
    statuses = {by_image.get(image, NOT_CHECKED) for image in images}
    if not statuses:
        return "no_images"
    if NOT_FOUND in statuses:
        return "missing"
    if UNREACHABLE in statuses:
        return "unreachable"
    if statuses == {FOUND}:
        return "ok"
    return "not_checked"


def plan(selection: Dict[str, Any], user=None) -> Dict[str, Any]:
    """The whole copy, priced: what comes over and which images are missing.

    Never blocking. ``missingWorkloads`` is the list the UI turns into "these
    have no image in <registry> — remove them, or bring them anyway".
    """
    return _plan(selection, user=user)[0]


def _plan(
    selection: Dict[str, Any], user=None
) -> Tuple[Dict[str, Any], List[storage.ClaimPlan]]:
    """The plan payload plus the resolved claim plans behind it.

    Preflight needs the objects (to probe NFS and judge each decision) and the
    API needs the JSON; deriving both from one export keeps it to a single read
    of the source cluster.
    """
    normalized = normalize_selection(selection)
    if normalized is None:
        return ({
            "sourceClusterId": "", "workloads": [], "namespaces": [], "support": {},
            "warnings": [], "missing": [], "imageChecks": [], "missingWorkloads": [],
            "storage": {"answers": {"default": storage.NONE}, "claims": [],
                        "summary": storage.summarize([]), "errors": []},
            "counts": {"workloads": 0, "documents": 0, "images": 0, "missingImages": 0},
        }, [])
    export = export_selection(normalized, user=user)
    registry_id = normalized.get("registryConnectionId")
    checks, by_image = check_images(registry_id, export.images)

    workloads = []
    for workload in export.workloads:
        images = workload.get("images") or []
        workloads.append({
            **workload,
            "imageStatus": _workload_status(images, by_image),
            "missingImages": [
                image for image in images if by_image.get(image) == NOT_FOUND
            ],
        })
    missing_workloads = [w for w in workloads if w["imageStatus"] == "missing"]
    claim_plans = resolve_storage(normalized, export)
    return ({
        "sourceClusterId": normalized["sourceClusterId"],
        "sourceClusterName": normalized.get("sourceClusterName") or "",
        "registryConnectionId": registry_id,
        "namespaces": export.namespaces,
        "workloads": workloads,
        "support": export.support,
        "warnings": export.warnings,
        "missing": export.missing,
        "imageChecks": checks,
        "storage": {
            "answers": normalized.get("storage") or {"default": storage.NONE},
            "claims": [plan.as_dict() for plan in claim_plans],
            "summary": storage.summarize(claim_plans),
            "errors": storage.errors(claim_plans),
        },
        "missingWorkloads": [
            {"namespace": w["namespace"], "kind": w["kind"], "name": w["name"],
             "missingImages": w["missingImages"]}
            for w in missing_workloads
        ],
        "counts": {
            "workloads": len(workloads),
            "documents": len(export.documents),
            "images": len(export.images),
            "missingImages": sum(
                1 for check in checks if check["status"] == NOT_FOUND
            ),
            "claims": len(claim_plans),
            "volumesToCreate": storage.summarize(claim_plans)["volumesToCreate"],
        },
    }, claim_plans)


# ---------------------------------------------------------------------------
# Storage for the copied claims
# ---------------------------------------------------------------------------

def resolve_storage(
    selection: Dict[str, Any], export: Export
) -> List[storage.ClaimPlan]:
    """Per-claim storage plans for an export.

    The source cluster's volumes are read whenever there are claims, not only
    when something is set to reuse: the picker needs to know *whether* reuse is
    possible before the user can choose it. A source that refuses the read
    (cluster-scoped RBAC) degrades to "reuse unavailable, here is why" rather
    than failing the copy.
    """
    claim_docs = export.claims
    if not claim_docs:
        return []
    answers = selection.get("storage") or {}
    source_cluster = str(selection.get("sourceClusterId") or "")

    source_index: Dict[str, Dict[str, Any]] = {}
    try:
        source_index = storage.source_volume_index(_kubectl_json, source_cluster)
    except Exception:  # noqa: BLE001 — advisory; drives the "reusable" column only
        source_index = {}

    # Writers are only worth asking about where reuse is actually chosen — one
    # pod list per namespace, and only those namespaces.
    reuse_namespaces = {
        (doc.get("metadata") or {}).get("namespace") or ""
        for doc in claim_docs
        if storage.decision_for(
            answers,
            storage.claim_key(
                (doc.get("metadata") or {}).get("namespace") or "",
                (doc.get("metadata") or {}).get("name") or "",
            ),
        )["source"] == storage.REUSE
    }
    writer_index: Dict[str, List[str]] = {}
    for namespace in sorted(n for n in reuse_namespaces if n):
        writer_index.update(
            storage.source_writers(_kubectl_json, source_cluster, namespace)
        )

    return storage.resolve(
        claim_docs,
        answers,
        source_index=source_index,
        consumer_index=storage.consumers(export.documents),
        writer_index=writer_index,
    )


# ---------------------------------------------------------------------------
# Preflight
# ---------------------------------------------------------------------------

def preflight_checks(build: ClusterBuild, *, probe=None, probe_targets=None) -> List[Dict[str, Any]]:
    """Workload-copy checks, all acknowledgeable.

    Nothing here fails a build. Copying workloads is an add-on to building a
    cluster, and a cluster that stands with a warning beats one that never gets
    built because a source namespace lost a Deployment this morning.
    """
    selection = selection_of(build)
    if selection is None:
        return []

    def check(check_id, label, status, detail, hint=""):
        return {"id": check_id, "label": label, "status": status,
                "detail": detail, "hint": hint}

    source = selection.get("sourceClusterName") or selection["sourceClusterId"]
    claim_plans: List[storage.ClaimPlan] = []
    try:
        result, claim_plans = _plan(selection)
    except PermissionError as exc:
        return [check(
            "workload_source", "Workload source readable", "warn", str(exc),
            "Ask for access to that cluster, or clear the workloads step.",
        )]
    except WorkloadSourceError as exc:
        return [check(
            "workload_source", "Workload source readable", "warn",
            f"{source} could not be read: {exc}",
            "The workloads phase re-reads the source when it runs and fails the "
            "build if it is still unreachable. Fix the connection, or clear the "
            "workloads step to build the cluster without them.",
        )]

    checks = [check(
        "workload_source", "Workload source readable", "pass",
        f"{_count(result['counts']['workloads'], 'workload')} across "
        f"{_count(len(result['namespaces']), 'namespace')} from {source}, plus "
        f"{_count(sum(result['support'].values()), 'configuration object')}.",
    )]
    if result["missing"]:
        checks.append(check(
            "workload_missing", "Selected workloads still exist", "warn",
            f"{_count(len(result['missing']), 'selection')} "
            + ("is" if len(result["missing"]) == 1 else "are")
            + f" gone from the source: {', '.join(result['missing'][:5])}.",
            "They are skipped. Re-open the workloads step to pick their "
            "replacements.",
        ))
    missing_images = result["counts"]["missingImages"]
    if missing_images:
        names = ", ".join(
            f"{w['namespace']}/{w['name']}" for w in result["missingWorkloads"][:5]
        )
        checks.append(check(
            "workload_images", "Images available in the chosen registry", "warn",
            f"{_count(missing_images, 'image')} "
            + ("is" if missing_images == 1 else "are")
            + " not in the selected registry — affects "
            + f"{_count(len(result['missingWorkloads']), 'workload')}: {names}.",
            "Push the images, remove those workloads in the workloads step, or "
            "acknowledge this and bring them anyway — their pods will stay in "
            "ImagePullBackOff until the images exist.",
        ))
    elif result["counts"]["images"] and not selection.get("registryConnectionId"):
        checks.append(check(
            "workload_images", "Images available in the chosen registry", "warn",
            "No registry was selected, so image availability was not checked.",
            "Pick a linked registry in the workloads step to check every image "
            "before the cluster is built.",
        ))
    # Where the copied volume claims land. The one place a workload-copy check
    # is allowed to *fail* rather than warn — see storage's module docstring.
    checks.extend(storage.preflight_checks(
        claim_plans, selection.get("storage"),
        probe=probe, probe_targets=probe_targets,
    ))
    # Stable ids: the UI groups checks by id, and a hash of the text would
    # change between processes and split one finding into two rows.
    for index, warning in enumerate(result["warnings"][:4]):
        checks.append(check(
            f"workload_note_{index + 1}", "Workload copy note", "warn", warning,
        ))
    return checks
