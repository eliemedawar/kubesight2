"""Where things run: clusters, nodes, namespaces, resources, events, topology.

Everything here reads. Changing a cluster is not in this module and not in this
server — adding, editing or deleting a cluster connection is a person's job in
the Clusters tab, because it is the one action that can make every other tool
here answer about the wrong place.

Two rules the whole module obeys:

**Permission is not access.** ``resources:view`` says a user may read resources;
the access engine says which clusters and namespaces they may read them in. Both
are checked, in that order, exactly as the HTTP routes check them — and the
payloads are then put through the same ``filter_namespace_resources`` the routes
use, so a rule that hides one deployment hides it here too.

**Listings are trimmed to the fields an answer needs.** A raw namespace listing
is tens of thousands of tokens of managed fields and annotations, and the answer
to "is anything broken in payments" is four of them. A tool that returned
everything would be read once and then avoided.
"""

from __future__ import annotations

from typing import Any, Dict, List

from ...access_engine import (
    filter_namespace_events,
    filter_namespace_resources,
    filter_namespaces_for_user,
    is_admin,
)
from ..protocol import ToolError
from .common import (
    cluster_access_or_error,
    pick,
    require_namespace,
    resolve_cluster,
    take,
    unwrap,
    visible_clusters,
)
from .registry import MAX_ROWS, _limit, tool as _register


def tool(name, **kwargs):
    kwargs.setdefault("domain", "clusters")
    return _register(name, **kwargs)


def _user():
    """The token's user, for the read tools the registry does not hand one to.

    Read tools deliberately do not receive ``user`` — that is what stops one
    acting on somebody's behalf by accident. But access filtering needs it, and
    it is the same user either way, so the ones that filter fetch it themselves
    from the request the way every service in this backend does.
    """
    from ...auth_utils import get_current_user

    try:
        return get_current_user()
    except Exception:
        return None


_CLUSTER_FIELDS = ("id", "name", "provider", "version", "status", "region", "nodeCount")


@tool(
    "kubesight_clusters_list",
    permission="clusters:view",
    description=(
        "Every cluster this token can see, with provider, version, node count "
        "and status. Start here for anything cluster-shaped — other cluster "
        "tools take the id or the name this returns."
    ),
)
def _clusters_list(_arguments: Dict[str, Any]) -> Dict[str, Any]:
    items = [pick(item, _CLUSTER_FIELDS) for item in visible_clusters(_user())]
    return {"count": len(items), "clusters": items}


@tool(
    "kubesight_cluster_overview",
    permission="overview:view",
    description=(
        "One cluster's health in a single call: node and pod counts, capacity, "
        "version, and what is not Ready. The orientation call before drilling in."
    ),
    schema={
        "type": "object",
        "properties": {"cluster": {"type": "string", "description": "Cluster id or name."}},
        "required": ["cluster"],
    },
)
def _cluster_overview(arguments: Dict[str, Any]) -> Dict[str, Any]:
    from ...k8s_provider import K8sCommandError, cluster_overview_from_k8s

    user = _user()
    cluster_id = resolve_cluster(user, arguments.get("cluster"))
    access = cluster_access_or_error(cluster_id)
    if access:
        try:
            return {"clusterId": cluster_id, **cluster_overview_from_k8s(access)}
        except K8sCommandError as exc:
            raise ToolError(f"Failed to load the overview for '{cluster_id}': {exc}")

    from ...mock_data import CLUSTER_OVERVIEWS

    overview = CLUSTER_OVERVIEWS.get(cluster_id)
    if not overview:
        raise ToolError(f"No overview for cluster '{cluster_id}'.")
    return {"clusterId": cluster_id, **overview}


@tool(
    "kubesight_cluster_nodes",
    permission="clusters:view",
    description=(
        "The machines in a cluster: name, role, status, version and capacity. "
        "Read this when pods are unschedulable or a node is suspected."
    ),
    schema={
        "type": "object",
        "properties": {"cluster": {"type": "string"}},
        "required": ["cluster"],
    },
)
def _cluster_nodes(arguments: Dict[str, Any]) -> Dict[str, Any]:
    from ...k8s_provider import K8sCommandError, list_nodes_from_k8s

    user = _user()
    cluster_id = resolve_cluster(user, arguments.get("cluster"))
    access = cluster_access_or_error(cluster_id)
    if access:
        try:
            items = list_nodes_from_k8s(access)
        except K8sCommandError as exc:
            raise ToolError(f"Failed to load nodes for '{cluster_id}': {exc}")
    else:
        from ...mock_data import CLUSTER_NODES

        items = CLUSTER_NODES.get(cluster_id) or []
    return {"clusterId": cluster_id, "count": len(items), "nodes": items}


@tool(
    "kubesight_storage_classes",
    permission="clusters:view",
    description=(
        "The StorageClasses a cluster offers, and which is default. The answer "
        "to a PVC stuck Pending is usually here — a cluster with no default "
        "StorageClass leaves every unqualified claim unbound."
    ),
    schema={
        "type": "object",
        "properties": {"cluster": {"type": "string"}},
        "required": ["cluster"],
    },
)
def _storage_classes(arguments: Dict[str, Any]) -> Dict[str, Any]:
    from ...k8s_provider import K8sCommandError, list_storage_classes_from_k8s

    user = _user()
    cluster_id = resolve_cluster(user, arguments.get("cluster"))
    access = cluster_access_or_error(cluster_id)
    if access:
        try:
            items = list_storage_classes_from_k8s(access)
        except K8sCommandError as exc:
            raise ToolError(f"Failed to load storage classes for '{cluster_id}': {exc}")
    else:
        from ...mock_data import STORAGE_CLASSES

        items = STORAGE_CLASSES.get(cluster_id) or []
    return {"clusterId": cluster_id, "count": len(items), "items": items}


@tool(
    "kubesight_namespaces_list",
    permission="namespaces:view",
    description=(
        "The namespaces in a cluster that this token can see, with pod, "
        "deployment and service counts."
    ),
    schema={
        "type": "object",
        "properties": {
            "cluster": {"type": "string"},
            "search": {"type": "string", "description": "Match the namespace name."},
            "limit": {"type": "integer", "minimum": 1, "maximum": MAX_ROWS},
        },
        "required": ["cluster"],
    },
)
def _namespaces_list(arguments: Dict[str, Any]) -> Dict[str, Any]:
    from ...k8s_provider import K8sCommandError, list_namespaces_from_k8s

    user = _user()
    cluster_id = resolve_cluster(user, arguments.get("cluster"))
    access = cluster_access_or_error(cluster_id)
    if access:
        try:
            items = list(list_namespaces_from_k8s(access).get("items") or [])
        except K8sCommandError as exc:
            raise ToolError(f"Failed to load namespaces for '{cluster_id}': {exc}")
    else:
        from ...mock_data import NAMESPACES

        items = list(NAMESPACES.get(cluster_id) or [])

    if user:
        items = filter_namespaces_for_user(user, cluster_id, items)
    search = str(arguments.get("search") or "").strip().lower()
    if search:
        items = [item for item in items if search in str(item.get("name", "")).lower()]
    total = len(items)
    items = take(items, _limit(arguments, MAX_ROWS))
    return {
        "clusterId": cluster_id,
        "count": len(items),
        "totalVisible": total,
        "namespaces": items,
    }


# The kinds a namespace listing can be narrowed to. The same tuple the HTTP
# route validates against, imported rather than retyped so a kind added to one
# is never missing from the other.
def _resource_kinds() -> tuple:
    from ...k8s_provider import NAMESPACE_RESOURCE_LIST_KEYS

    return NAMESPACE_RESOURCE_LIST_KEYS


@tool(
    "kubesight_namespace_resources",
    permission="resources:view",
    description=(
        "What is in a namespace. Without 'kind' it returns every kind at once "
        "(pods, deployments, services, configmaps, ingress and the rest), which "
        "is large — name a kind when you know which one you want. Secret values "
        "are never included, only names."
    ),
    schema={
        "type": "object",
        "properties": {
            "cluster": {"type": "string"},
            "namespace": {"type": "string"},
            "kind": {
                "type": "string",
                "description": (
                    "One of pods, deployments, replicasets, statefulsets, daemonsets, "
                    "jobs, cronjobs, services, configmaps, secrets, ingress."
                ),
            },
            "limit": {"type": "integer", "minimum": 1, "maximum": MAX_ROWS},
        },
        "required": ["cluster", "namespace"],
    },
)
def _namespace_resources(arguments: Dict[str, Any]) -> Dict[str, Any]:
    from ...k8s_provider import (
        K8sCommandError,
        namespace_resource_list_from_k8s,
        namespace_resources_from_k8s,
    )

    user = _user()
    cluster_id = resolve_cluster(user, arguments.get("cluster"))
    namespace = require_namespace(user, cluster_id, arguments.get("namespace"))
    kind = str(arguments.get("kind") or "").strip().lower()
    kinds = _resource_kinds()
    if kind and kind not in kinds:
        raise ToolError(f"'{kind}' is not a kind KubeSight lists. Try one of: {', '.join(kinds)}.")

    access = cluster_access_or_error(cluster_id)
    if access:
        try:
            if kind:
                resources = namespace_resource_list_from_k8s(access, namespace, kind)
            else:
                resources = namespace_resources_from_k8s(access, namespace)
        except K8sCommandError as exc:
            raise ToolError(f"Failed to read '{cluster_id}/{namespace}': {exc}")
    else:
        from ...mock_data import NAMESPACES, NAMESPACE_RESOURCES

        if NAMESPACES.get(cluster_id) is None:
            raise ToolError(f"No cluster '{cluster_id}'.")
        found = (NAMESPACE_RESOURCES.get(cluster_id) or {}).get(namespace) or {}
        resources = (
            {"namespace": namespace, kind: found.get(kind) or []}
            if kind
            else {"namespace": namespace, **{key: found.get(key) or [] for key in kinds}}
        )

    if user:
        resources = filter_namespace_resources(user, cluster_id, resources)

    limit = _limit(arguments, MAX_ROWS)
    trimmed = {"clusterId": cluster_id, "namespace": namespace}
    counts = {}
    for key, value in resources.items():
        if isinstance(value, list):
            counts[key] = len(value)
            trimmed[key] = take(value, limit)
        elif key != "namespace":
            trimmed[key] = value
    trimmed["counts"] = counts
    return trimmed


@tool(
    "kubesight_resource_get",
    permission="resources:view",
    description=(
        "One resource in full, as either 'describe' output or its YAML. Describe "
        "is what to read for a pod that will not start — it carries the events "
        "and the container statuses. YAML is what to read to see spec as applied."
    ),
    schema={
        "type": "object",
        "properties": {
            "cluster": {"type": "string"},
            "namespace": {"type": "string"},
            "kind": {"type": "string", "description": "pod, deployment, service, configmap, …"},
            "name": {"type": "string"},
            "as": {"type": "string", "enum": ["describe", "yaml"], "default": "describe"},
        },
        "required": ["cluster", "namespace", "kind", "name"],
    },
)
def _resource_get(arguments: Dict[str, Any]) -> Dict[str, Any]:
    from ...services.resource_actions_service import get_resource_describe, get_resource_yaml

    user = _user()
    cluster_id = resolve_cluster(user, arguments.get("cluster"))
    namespace = require_namespace(user, cluster_id, arguments.get("namespace"))
    kind = str(arguments.get("kind") or "").strip()
    name = str(arguments.get("name") or "").strip()
    if not kind or not name:
        raise ToolError("Both 'kind' and 'name' are required.")
    want = str(arguments.get("as") or "describe").strip().lower()
    fetch = get_resource_yaml if want == "yaml" else get_resource_describe
    data = unwrap(
        fetch(user, cluster_id, namespace, kind, name),
        what=f"{kind}/{name}",
    )
    return {"clusterId": cluster_id, "namespace": namespace, "kind": kind, "name": name, **(data or {})}


@tool(
    "kubesight_pod_issues",
    permission="resources:view",
    description=(
        "Every pod with a problem status across every namespace this token can "
        "see — CrashLoopBackOff, ImagePullBackOff, Pending, Evicted and the rest. "
        "One call answers 'is anything broken in this cluster'."
    ),
    schema={
        "type": "object",
        "properties": {
            "cluster": {"type": "string"},
            "limit": {"type": "integer", "minimum": 1, "maximum": MAX_ROWS},
        },
        "required": ["cluster"],
    },
)
def _pod_issues(arguments: Dict[str, Any]) -> Dict[str, Any]:
    from ...access_engine import can_access_namespace
    from ...k8s_provider import K8sCommandError, cluster_pod_issues_from_k8s, is_issue_pod_status

    user = _user()
    cluster_id = resolve_cluster(user, arguments.get("cluster"))
    access = cluster_access_or_error(cluster_id)
    if access:
        try:
            payload = cluster_pod_issues_from_k8s(access)
        except K8sCommandError as exc:
            raise ToolError(f"Failed to scan '{cluster_id}' for pod issues: {exc}")
        pods = list(payload.get("pods") or [])
    else:
        from ...mock_data import NAMESPACE_RESOURCES

        pods = []
        for namespace, resources in (NAMESPACE_RESOURCES.get(cluster_id) or {}).items():
            pods.extend(
                {**pod, "namespace": pod.get("namespace") or namespace}
                for pod in (resources.get("pods") or [])
                if is_issue_pod_status(pod.get("status"))
            )

    if user and not is_admin(user):
        visible: List[Dict[str, Any]] = []
        by_namespace: Dict[str, List[Dict[str, Any]]] = {}
        for pod in pods:
            by_namespace.setdefault(pod.get("namespace") or "", []).append(pod)
        for namespace, group in by_namespace.items():
            if not namespace or not can_access_namespace(user, cluster_id, namespace):
                continue
            visible.extend(
                filter_namespace_resources(
                    user, cluster_id, {"namespace": namespace, "pods": group}
                ).get("pods", [])
            )
        pods = visible

    pods.sort(key=lambda item: ((item.get("namespace") or ""), (item.get("name") or "")))
    total = len(pods)
    pods = take(pods, _limit(arguments, MAX_ROWS))
    return {"clusterId": cluster_id, "totalIssues": total, "count": len(pods), "pods": pods}


@tool(
    "kubesight_namespace_events",
    permission="resources:view",
    description=(
        "Kubernetes events for a namespace, newest first, optionally narrowed to "
        "one object. Events are where the reason lives: a pod that will not "
        "schedule, an image that will not pull, a probe that keeps failing."
    ),
    schema={
        "type": "object",
        "properties": {
            "cluster": {"type": "string"},
            "namespace": {"type": "string"},
            "kind": {"type": "string", "description": "Narrow to one object's kind, e.g. Pod."},
            "name": {"type": "string", "description": "Narrow to one object's name."},
            "limit": {"type": "integer", "minimum": 1, "maximum": MAX_ROWS},
        },
        "required": ["cluster", "namespace"],
    },
)
def _namespace_events(arguments: Dict[str, Any]) -> Dict[str, Any]:
    from ...k8s_provider import K8sCommandError, namespace_events_from_k8s

    user = _user()
    cluster_id = resolve_cluster(user, arguments.get("cluster"))
    namespace = require_namespace(user, cluster_id, arguments.get("namespace"))
    limit = _limit(arguments, MAX_ROWS)
    kind = str(arguments.get("kind") or "").strip() or None
    name = str(arguments.get("name") or "").strip() or None

    access = cluster_access_or_error(cluster_id)
    if access:
        try:
            payload = namespace_events_from_k8s(
                access, namespace, involved_kind=kind, involved_name=name, limit=limit
            )
        except K8sCommandError as exc:
            raise ToolError(f"Failed to load events for '{cluster_id}/{namespace}': {exc}")
    else:
        from ...mock_data import NAMESPACE_EVENTS

        items = list((NAMESPACE_EVENTS.get(cluster_id) or {}).get(namespace) or [])
        if kind:
            items = [e for e in items if str(e.get("kind", "")).lower() == kind.lower()]
        if name:
            items = [e for e in items if str(e.get("name", "")).lower() == name.lower()]
        payload = {"namespace": namespace, "items": items[:limit], "count": len(items[:limit])}

    if user:
        payload = filter_namespace_events(user, cluster_id, namespace, payload)
    items = take(payload.get("items"), limit)
    return {"clusterId": cluster_id, "namespace": namespace, "count": len(items), "events": items}


@tool(
    "kubesight_topology",
    permission="resources:view",
    description=(
        "The shape of a cluster as a graph. Without a namespace it returns the "
        "cluster level — namespaces and nodes as hubs. With one, the pods and "
        "services inside it and the edges between them."
    ),
    schema={
        "type": "object",
        "properties": {
            "cluster": {"type": "string"},
            "namespace": {"type": "string", "description": "Omit for the cluster level."},
        },
        "required": ["cluster"],
    },
)
def _topology(arguments: Dict[str, Any]) -> Dict[str, Any]:
    from ...services.topology_service import build_cluster_topology, build_namespace_topology

    user = _user()
    cluster_id = resolve_cluster(user, arguments.get("cluster"))
    namespace = str(arguments.get("namespace") or "").strip()
    access = cluster_access_or_error(cluster_id)
    # A topology is an overview, so a partial one is still worth having: one
    # unavailable API group should not cost the whole graph. Whatever could not
    # be read is named in ``warnings`` rather than silently missing, because a
    # graph with a piece quietly absent reads as a graph that says the piece is
    # not there.
    warnings: List[str] = []

    if namespace:
        namespace = require_namespace(user, cluster_id, namespace)
        pods, services, ingresses = _namespace_topology_inputs(
            access, cluster_id, namespace, warnings
        )
        if user and not is_admin(user):
            visible = filter_namespace_resources(
                user,
                cluster_id,
                {"namespace": namespace, "pods": pods, "services": services, "ingress": ingresses},
            )
            pods = visible.get("pods", [])
            services = visible.get("services", [])
            ingresses = visible.get("ingress", [])
        topology = build_namespace_topology(namespace, pods, services, ingresses)
        level = "namespace"
    else:
        namespaces, nodes, issue_pods = _cluster_topology_inputs(access, cluster_id, warnings)
        if user:
            namespaces = filter_namespaces_for_user(user, cluster_id, namespaces)
        from .common import cluster_name

        topology = build_cluster_topology(
            cluster_id, cluster_name(cluster_id), namespaces, nodes, issue_pods
        )
        level = "cluster"

    return {
        "clusterId": cluster_id,
        "namespace": namespace or None,
        "level": level,
        "topology": topology,
        "warnings": warnings,
        "partial": bool(warnings),
    }


def _cluster_topology_inputs(access, cluster_id: str, warnings: List[str]):
    from ...k8s_provider import (
        K8sCommandError,
        cluster_pod_issues_from_k8s,
        is_issue_pod_status,
        list_namespace_counts_from_k8s,
        list_nodes_from_k8s,
    )

    if access:
        def read(label, fetch, default):
            try:
                return fetch()
            except (K8sCommandError, ValueError, TypeError) as exc:
                warnings.append(f"{label} could not be loaded: {exc}")
                return default

        return (
            read("Namespaces", lambda: list_namespace_counts_from_k8s(access).get("items", []), []),
            read("Nodes", lambda: list_nodes_from_k8s(access), []),
            read("Pod health", lambda: cluster_pod_issues_from_k8s(access).get("pods", []), []),
        )

    from ...mock_data import CLUSTER_NODES, NAMESPACES, NAMESPACE_RESOURCES

    namespaces = NAMESPACES.get(cluster_id)
    if namespaces is None:
        raise ToolError(f"No cluster '{cluster_id}'.")
    issue_pods = [
        {**pod, "namespace": pod.get("namespace") or ns_name}
        for ns_name, resources in (NAMESPACE_RESOURCES.get(cluster_id) or {}).items()
        for pod in (resources.get("pods") or [])
        if is_issue_pod_status(pod.get("status"))
    ]
    return namespaces, (CLUSTER_NODES.get(cluster_id) or []), issue_pods


def _namespace_topology_inputs(access, cluster_id: str, namespace: str, warnings: List[str]):
    from ...k8s_provider import K8sCommandError, namespace_resource_list_from_k8s

    if access:
        def read(key):
            try:
                return namespace_resource_list_from_k8s(access, namespace, key).get(key, [])
            except (K8sCommandError, ValueError, TypeError) as exc:
                warnings.append(f"{key.title()} could not be loaded: {exc}")
                return []

        return read("pods"), read("services"), read("ingress")

    from ...mock_data import NAMESPACE_RESOURCES

    resources = (NAMESPACE_RESOURCES.get(cluster_id) or {}).get(namespace) or {}
    return (
        resources.get("pods") or [],
        resources.get("services") or [],
        resources.get("ingress") or [],
    )
