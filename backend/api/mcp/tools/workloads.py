"""What is running, and the four things an agent may do to it.

The reads are the application inventory — KubeSight's view of a cluster grouped
by application rather than by Kubernetes object, which is the view a question
like "what version of payments is in production" is actually asking about.

The writes are restart, scale, rollback and exec. They are here rather than
excluded because each is reversible and each already exists as a button a person
presses, behind ``apps:deploy`` and the same namespace access rules. What is
*not* here is delete: an agent that removes a workload leaves nothing to put
back, and no audit row makes that reversible.

Every write goes through the same service the UI posts to. That matters more
than it sounds: those services do their own access check, their own validation
and their own audit row, so a tool cannot accidentally be more permissive than
the button — and when an installation tightens the button, the tool tightens
with it.

``kubesight_pod_exec`` is the one tool in this server marked destructive. A
command inside a container can do anything the container can, KubeSight cannot
inspect it, and the honest thing is to say so in the annotation a client uses to
decide whether to ask a person first.
"""

from __future__ import annotations

from typing import Any, Dict

from ..protocol import ToolError
from .common import pick, require_namespace, resolve_cluster, take, unwrap
from .registry import MAX_ROWS, _limit, tool as _register


def tool(name, **kwargs):
    kwargs.setdefault("domain", "workloads")
    return _register(name, **kwargs)


def _user():
    from ...auth_utils import get_current_user

    try:
        return get_current_user()
    except Exception:
        return None


_INVENTORY_FIELDS = (
    "id", "applicationName", "clusterId", "clusterName", "namespace", "workloadType",
    "status", "image", "imageTag", "replicas", "readyReplicas", "desiredReplicas",
    "cpuUsage", "memoryUsage", "lastDeployedAt", "helmRelease", "owner",
)


@tool(
    "kubesight_inventory_list",
    permission="inventory:view",
    description=(
        "What is deployed, grouped by application rather than by Kubernetes "
        "object: name, cluster, namespace, image tag, replicas and status. This "
        "is the tool for 'what version is running where' and 'what is unhealthy'. "
        "Filter by cluster, namespace, name, status, workload type or image tag."
    ),
    schema={
        "type": "object",
        "properties": {
            "cluster": {"type": "string", "description": "Cluster id or name."},
            "namespace": {"type": "string"},
            "name": {"type": "string", "description": "Application name."},
            "status": {"type": "string"},
            "workloadType": {"type": "string", "description": "deployment, statefulset, daemonset…"},
            "imageTag": {"type": "string"},
            "search": {"type": "string"},
            "limit": {"type": "integer", "minimum": 1, "maximum": MAX_ROWS},
        },
    },
)
def _inventory_list(arguments: Dict[str, Any]) -> Dict[str, Any]:
    from ...services.inventory_service import list_inventory, summarize_inventory

    user = _user()
    filters = {}
    if arguments.get("cluster"):
        filters["cluster"] = resolve_cluster(user, arguments.get("cluster"))
    for key in ("namespace", "name", "status", "workloadType", "imageTag", "search"):
        value = str(arguments.get(key) or "").strip()
        if value:
            filters[key] = value

    items = unwrap(list_inventory(user, filters), what="inventory")
    items = items or []
    summary = summarize_inventory(items)
    trimmed = [pick(item, _INVENTORY_FIELDS) for item in take(items, _limit(arguments, MAX_ROWS))]
    return {"summary": summary, "totalMatching": len(items), "count": len(trimmed), "items": trimmed}


@tool(
    "kubesight_inventory_get",
    permission="inventory:view",
    description=(
        "One application in full: every Kubernetes object behind it, its "
        "containers and images, and its deployment history. Takes the 'id' from "
        "kubesight_inventory_list."
    ),
    schema={
        "type": "object",
        "properties": {
            "inventoryId": {
                "type": "string",
                "description": "The id from kubesight_inventory_list (cluster/namespace/name).",
            }
        },
        "required": ["inventoryId"],
    },
)
def _inventory_get(arguments: Dict[str, Any]) -> Dict[str, Any]:
    from ...services.inventory_service import get_inventory_detail

    inventory_id = str(arguments.get("inventoryId") or "").strip()
    if not inventory_id:
        raise ToolError("An inventoryId is required — take it from kubesight_inventory_list.")
    return unwrap(get_inventory_detail(_user(), inventory_id), what=inventory_id) or {}


@tool(
    "kubesight_rollout_history",
    permission="apps:deploy",
    description=(
        "A workload's rollout revisions, newest first, with the image each one "
        "carried. Read this before rolling back — it is how you name the revision "
        "you mean rather than guessing one."
    ),
    schema={
        "type": "object",
        "properties": {
            "cluster": {"type": "string"},
            "namespace": {"type": "string"},
            "workload": {"type": "string", "description": "The deployment name."},
        },
        "required": ["cluster", "namespace", "workload"],
    },
)
def _rollout_history(arguments: Dict[str, Any]) -> Dict[str, Any]:
    from ...services.inventory_actions_service import get_rollout_history

    user = _user()
    cluster_id = resolve_cluster(user, arguments.get("cluster"))
    namespace = require_namespace(user, cluster_id, arguments.get("namespace"))
    workload = str(arguments.get("workload") or "").strip()
    if not workload:
        raise ToolError("Name the workload.")
    return unwrap(
        get_rollout_history(user, cluster_id, namespace, workload), what=workload
    ) or {}


def _where(body: Dict[str, Any]) -> str:
    """cluster/namespace/workload, for a summary line that is unambiguous.

    A write's one-line summary is what a model reads back when it is deciding
    whether the write landed. "restarted payments" in a fleet with three
    payments deployments is not an answer, so the summary always carries where.
    """
    return f"{body['workloadName']} in {body['clusterId']}/{body['namespace']}"


def _action_body(user, arguments: Dict[str, Any]) -> Dict[str, Any]:
    """The body the action services expect, with the cluster already resolved."""
    cluster_id = resolve_cluster(user, arguments.get("cluster"))
    namespace = require_namespace(user, cluster_id, arguments.get("namespace"))
    workload = str(arguments.get("workload") or "").strip()
    if not workload:
        raise ToolError("Name the workload.")
    # These tools act on deployments only, and the service refuses a body
    # that does not say so.
    return {
        "clusterId": cluster_id,
        "namespace": namespace,
        "workloadType": "deployment",
        "workloadName": workload,
    }


@tool(
    "kubesight_workload_restart",
    permission="apps:deploy",
    description=(
        "Roll a deployment: `kubectl rollout restart`. Pods are replaced one at "
        "a time under the deployment's own strategy; nothing about the spec "
        "changes. The usual fix for a pod holding a stale config or a bad "
        "connection pool."
    ),
    approval=(
        "On a cluster configured to require approvals this fails unless the "
        "token's user has a live approved deployment request — check "
        "kubesight_deploy_eligibility first."
    ),
    write=True,
    schema={
        "type": "object",
        "properties": {
            "cluster": {"type": "string"},
            "namespace": {"type": "string"},
            "workload": {"type": "string"},
        },
        "required": ["cluster", "namespace", "workload"],
    },
)
def _workload_restart(arguments: Dict[str, Any], *, user=None) -> Dict[str, Any]:
    from ...services.inventory_actions_service import restart_deployment

    body = _action_body(user, arguments)
    data = unwrap(restart_deployment(user, body), what=body["workloadName"]) or {}
    return {"changed": f"restarted {_where(body)}", **data}


@tool(
    "kubesight_workload_scale",
    permission="apps:deploy",
    description=(
        "Set a deployment's replica count. Scaling to 0 stops the application "
        "without deleting it — say so plainly when you do it, because to anyone "
        "watching it looks exactly like an outage."
    ),
    approval=(
        "On a cluster configured to require approvals this fails unless the "
        "token's user has a live approved deployment request — check "
        "kubesight_deploy_eligibility first."
    ),
    write=True,
    schema={
        "type": "object",
        "properties": {
            "cluster": {"type": "string"},
            "namespace": {"type": "string"},
            "workload": {"type": "string"},
            "replicas": {"type": "integer", "minimum": 0},
        },
        "required": ["cluster", "namespace", "workload", "replicas"],
    },
)
def _workload_scale(arguments: Dict[str, Any], *, user=None) -> Dict[str, Any]:
    from ...services.inventory_actions_service import scale_deployment

    body = _action_body(user, arguments)
    if arguments.get("replicas") is None:
        raise ToolError("'replicas' is required.")
    body["replicas"] = arguments.get("replicas")
    data = unwrap(scale_deployment(user, body), what=body["workloadName"]) or {}
    return {"changed": f"scaled {_where(body)} to {body['replicas']} replicas", **data}


@tool(
    "kubesight_workload_rollback",
    permission="apps:deploy",
    description=(
        "Undo a deployment's last rollout, or go back to a named revision from "
        "kubesight_rollout_history. Read the history first: 'the previous one' "
        "is not always the one that worked."
    ),
    approval=(
        "On a cluster configured to require approvals this fails unless the "
        "token's user has a live approved deployment request — check "
        "kubesight_deploy_eligibility first."
    ),
    write=True,
    schema={
        "type": "object",
        "properties": {
            "cluster": {"type": "string"},
            "namespace": {"type": "string"},
            "workload": {"type": "string"},
            "revision": {
                "type": "integer",
                "minimum": 1,
                "description": "Omit to undo the last rollout.",
            },
        },
        "required": ["cluster", "namespace", "workload"],
    },
)
def _workload_rollback(arguments: Dict[str, Any], *, user=None) -> Dict[str, Any]:
    from ...services.inventory_actions_service import rollback_deployment

    body = _action_body(user, arguments)
    if arguments.get("revision") is not None:
        body["revision"] = arguments.get("revision")
    target = f"to revision {body['revision']}" if "revision" in body else "to the previous revision"
    data = unwrap(rollback_deployment(user, body), what=body["workloadName"]) or {}
    return {"changed": f"rolled back {_where(body)} {target}", **data}


@tool(
    "kubesight_resource_restart",
    permission="apps:deploy",
    description=(
        "Restart something that is not a deployment: a single pod (deleted so "
        "its controller recreates it), a statefulset or a daemonset. For a "
        "deployment use kubesight_workload_restart."
    ),
    approval=(
        "On a cluster configured to require approvals this fails unless the "
        "token's user has a live approved deployment request — check "
        "kubesight_deploy_eligibility first."
    ),
    write=True,
    schema={
        "type": "object",
        "properties": {
            "cluster": {"type": "string"},
            "namespace": {"type": "string"},
            "kind": {"type": "string", "description": "pod, statefulset or daemonset."},
            "name": {"type": "string"},
        },
        "required": ["cluster", "namespace", "kind", "name"],
    },
)
def _resource_restart(arguments: Dict[str, Any], *, user=None) -> Dict[str, Any]:
    from ...services.resource_actions_service import restart_resource

    cluster_id = resolve_cluster(user, arguments.get("cluster"))
    namespace = require_namespace(user, cluster_id, arguments.get("namespace"))
    kind = str(arguments.get("kind") or "").strip()
    name = str(arguments.get("name") or "").strip()
    if not kind or not name:
        raise ToolError("Both 'kind' and 'name' are required.")
    data = unwrap(
        restart_resource(user, cluster_id, namespace, kind, name), what=f"{kind}/{name}"
    ) or {}
    return {"changed": f"restarted {kind}/{name} in {cluster_id}/{namespace}", **data}


@tool(
    "kubesight_pod_exec",
    permission="apps:deploy",
    description=(
        "Run one shell command inside a pod's container and return its output. "
        "The command runs with the container's own privileges and KubeSight "
        "cannot tell what it will do, so keep it to reading — checking a file, a "
        "process, a DNS lookup, a port. Say what you are about to run before you "
        "run it."
    ),
    write=True,
    destructive=True,
    schema={
        "type": "object",
        "properties": {
            "cluster": {"type": "string"},
            "namespace": {"type": "string"},
            "pod": {"type": "string"},
            "command": {"type": "string", "description": "Run via `sh -c`."},
            "container": {"type": "string", "description": "Required for a multi-container pod."},
        },
        "required": ["cluster", "namespace", "pod", "command"],
    },
)
def _pod_exec(arguments: Dict[str, Any], *, user=None) -> Dict[str, Any]:
    from ...services.resource_actions_service import exec_in_pod

    cluster_id = resolve_cluster(user, arguments.get("cluster"))
    namespace = require_namespace(user, cluster_id, arguments.get("namespace"))
    pod = str(arguments.get("pod") or "").strip()
    command = str(arguments.get("command") or "").strip()
    if not pod or not command:
        raise ToolError("Both 'pod' and 'command' are required.")
    container = str(arguments.get("container") or "").strip() or None
    data = unwrap(
        exec_in_pod(user, cluster_id, namespace, pod, command, container), what=pod
    ) or {}
    return {"changed": f"ran a command in {cluster_id}/{namespace}/{pod}", **data}
