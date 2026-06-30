"""Read-only kubectl actions for Resources page (describe, YAML, rollout history)."""

from __future__ import annotations

from typing import Any, Dict, Optional, Tuple

from ..access_engine import can_access_namespace, can_access_resource, user_has_permission
from ..audit import log_audit
from ..k8s_provider import K8sCommandError, resolve_cluster_access, should_use_real_k8s
from ..models import User
from .deployment_service import _run_kubectl_for_cluster
from .inventory_actions_service import _mock_rollout_history, parse_rollout_history

KIND_ALIASES = {
    "pod": "pod",
    "pods": "pod",
    "deployment": "deployment",
    "deployments": "deployment",
    "replicaset": "replicaset",
    "replicasets": "replicaset",
    "statefulset": "statefulset",
    "statefulsets": "statefulset",
    "daemonset": "daemonset",
    "daemonsets": "daemonset",
    "job": "job",
    "jobs": "job",
    "cronjob": "cronjob",
    "cronjobs": "cronjob",
    "service": "service",
    "services": "service",
    "configmap": "configmap",
    "configmaps": "configmap",
    "secret": "secret",
    "secrets": "secret",
}

KIND_PERMISSION = {
    "pod": "pods:view",
    "deployment": "deployments:view",
    "replicaset": "replicasets:view",
    "statefulset": "statefulsets:view",
    "daemonset": "daemonsets:view",
    "job": "jobs:view",
    "cronjob": "cronjobs:view",
    "service": "services:view",
    # ConfigMaps and Secrets have no dedicated view permission; they are gated by
    # the page-level resources:view (and per-namespace access rules).
    "configmap": "resources:view",
    "secret": "resources:view",
}

# kubectl kind -> proper YAML kind casing for mock-mode YAML output.
_YAML_KIND_CASING = {
    "deployment": "Deployment",
    "replicaset": "ReplicaSet",
    "statefulset": "StatefulSet",
    "daemonset": "DaemonSet",
    "configmap": "ConfigMap",
    "secret": "Secret",
    "cronjob": "CronJob",
}

# Kinds that support the Resources "Restart" action. Pods are restarted by
# deletion (the owning controller recreates them); workloads use rollout restart.
RESTART_SUPPORTED_KINDS = {"pod", "deployment", "statefulset", "daemonset"}

# Permission gating writes from the Resources page (mirrors the deployment edit action).
RESTART_PERMISSION = "apps:deploy"

# Exec into a pod runs arbitrary commands in a container, so it is gated by the
# same write-level permission as restart.
EXEC_PERMISSION = "apps:deploy"

# Upper bound on a single exec command length (defensive — avoids unbounded argv).
_MAX_EXEC_COMMAND_LENGTH = 4000


def _normalize_kind(kind: str) -> Optional[str]:
    return KIND_ALIASES.get((kind or "").strip().lower())


def _check_resource_read_access(
    user: Optional[User],
    cluster_id: str,
    namespace: str,
    kind: str,
    name: str,
    action: str,
) -> Optional[Tuple[str, int]]:
    normalized = _normalize_kind(kind)
    if not normalized or not name.strip():
        return "Invalid resource kind or name", 400

    permission = KIND_PERMISSION[normalized]
    if user and not user_has_permission(user, permission) and not user_has_permission(user, "resources:view"):
        log_audit(
            "forbidden_access_attempt",
            actor=user,
            target_type=normalized,
            target_id=f"{cluster_id}/{namespace}/{name}",
            details={"action": action},
        )
        return "Forbidden", 403

    if user and not can_access_resource(user, cluster_id, namespace, normalized, name):
        log_audit(
            "forbidden_access_attempt",
            actor=user,
            target_type=normalized,
            target_id=f"{cluster_id}/{namespace}/{name}",
            details={"action": action},
        )
        return "Forbidden", 403

    return None


def _mock_describe(kind: str, namespace: str, name: str) -> str:
    return (
        f"Name:         {name}\n"
        f"Namespace:    {namespace}\n"
        f"Kind:         {kind}\n"
        f"Labels:       app={name}\n"
        f"Status:       Running (mock)\n"
        f"Events:       <none> (mock environment)\n"
    )


def _mock_yaml(kind: str, namespace: str, name: str) -> str:
    yaml_kind = _YAML_KIND_CASING.get(kind, kind.capitalize())
    if kind == "configmap":
        return (
            f"apiVersion: v1\n"
            f"kind: ConfigMap\n"
            f"metadata:\n"
            f"  name: {name}\n"
            f"  namespace: {namespace}\n"
            f"data:\n"
            f"  example.key: example-value\n"
        )
    if kind == "secret":
        return (
            f"apiVersion: v1\n"
            f"kind: Secret\n"
            f"metadata:\n"
            f"  name: {name}\n"
            f"  namespace: {namespace}\n"
            f"type: Opaque\n"
            f"data: {{}}\n"
        )
    return (
        f"apiVersion: v1\n"
        f"kind: {yaml_kind}\n"
        f"metadata:\n"
        f"  name: {name}\n"
        f"  namespace: {namespace}\n"
        f"spec: {{}}\n"
        f"status: {{}}\n"
    )


def get_resource_describe(
    user: Optional[User],
    cluster_id: str,
    namespace: str,
    kind: str,
    name: str,
) -> Tuple[Optional[Dict[str, Any]], Optional[str], int]:
    denied = _check_resource_read_access(user, cluster_id, namespace, kind, name, "describe")
    if denied:
        return None, denied[0], denied[1]

    normalized = _normalize_kind(kind)
    assert normalized

    if not should_use_real_k8s(cluster_id):
        output = _mock_describe(normalized, namespace, name)
        return {
            "clusterId": cluster_id,
            "namespace": namespace,
            "kind": normalized,
            "name": name,
            "output": output,
            "mode": "mock",
        }, None, 200

    access = resolve_cluster_access(cluster_id)
    if not access:
        return None, "Cluster not found", 404

    try:
        output = _run_kubectl_for_cluster(
            cluster_id,
            ["describe", normalized, name, "-n", namespace],
        )
        log_audit(
            "resource_describe_viewed",
            actor=user,
            target_type=normalized,
            target_id=f"{cluster_id}/{namespace}/{name}",
        )
        return {
            "clusterId": cluster_id,
            "namespace": namespace,
            "kind": normalized,
            "name": name,
            "output": output,
        }, None, 200
    except K8sCommandError as exc:
        return None, str(exc), 503


def get_resource_yaml(
    user: Optional[User],
    cluster_id: str,
    namespace: str,
    kind: str,
    name: str,
) -> Tuple[Optional[Dict[str, Any]], Optional[str], int]:
    denied = _check_resource_read_access(user, cluster_id, namespace, kind, name, "yaml")
    if denied:
        return None, denied[0], denied[1]

    normalized = _normalize_kind(kind)
    assert normalized

    if not should_use_real_k8s(cluster_id):
        yaml_content = _mock_yaml(normalized, namespace, name)
        return {
            "clusterId": cluster_id,
            "namespace": namespace,
            "kind": normalized,
            "name": name,
            "yaml": yaml_content,
            "mode": "mock",
        }, None, 200

    access = resolve_cluster_access(cluster_id)
    if not access:
        return None, "Cluster not found", 404

    try:
        yaml_content = _run_kubectl_for_cluster(
            cluster_id,
            ["get", normalized, name, "-n", namespace, "-o", "yaml"],
        )
        log_audit(
            "resource_yaml_viewed",
            actor=user,
            target_type=normalized,
            target_id=f"{cluster_id}/{namespace}/{name}",
        )
        return {
            "clusterId": cluster_id,
            "namespace": namespace,
            "kind": normalized,
            "name": name,
            "yaml": yaml_content,
        }, None, 200
    except K8sCommandError as exc:
        return None, str(exc), 503


def get_deployment_rollout_history(
    user: Optional[User],
    cluster_id: str,
    namespace: str,
    deployment_name: str,
) -> Tuple[Optional[Dict[str, Any]], Optional[str], int]:
    denied = _check_resource_read_access(user, cluster_id, namespace, "deployment", deployment_name, "rollout-history")
    if denied:
        return None, denied[0], denied[1]

    if not should_use_real_k8s(cluster_id):
        data = _mock_rollout_history(cluster_id, namespace, deployment_name)
        data["clusterId"] = cluster_id
        data["namespace"] = namespace
        return data, None, 200

    access = resolve_cluster_access(cluster_id)
    if not access:
        return None, "Cluster not found", 404

    try:
        output = _run_kubectl_for_cluster(
            cluster_id,
            ["rollout", "history", f"deployment/{deployment_name}", "-n", namespace],
        )
        parsed = parse_rollout_history(output)
        parsed["clusterId"] = cluster_id
        parsed["namespace"] = namespace
        log_audit(
            "deployment_rollout_history_viewed",
            actor=user,
            target_type="deployment",
            target_id=f"{cluster_id}/{namespace}/{deployment_name}",
            details={"source": "resources"},
        )
        return parsed, None, 200
    except K8sCommandError as exc:
        return None, str(exc), 503


def _check_resource_restart_access(
    user: Optional[User],
    cluster_id: str,
    namespace: str,
    normalized: str,
    name: str,
) -> Optional[Tuple[str, int]]:
    if user and not user_has_permission(user, RESTART_PERMISSION):
        log_audit(
            "unauthorized_resource_action",
            actor=user,
            target_type=normalized,
            target_id=f"{cluster_id}/{namespace}/{name}",
            details={"action": "restart"},
        )
        return "Forbidden", 403

    if user and not can_access_namespace(user, cluster_id, namespace):
        log_audit(
            "unauthorized_resource_action",
            actor=user,
            target_type="namespace",
            target_id=f"{cluster_id}/{namespace}",
            details={"action": "restart", "resource": name},
        )
        return "Forbidden", 403

    if user and not can_access_resource(user, cluster_id, namespace, normalized, name):
        log_audit(
            "unauthorized_resource_action",
            actor=user,
            target_type=normalized,
            target_id=f"{cluster_id}/{namespace}/{name}",
            details={"action": "restart"},
        )
        return "Forbidden", 403

    return None


def restart_resource(
    user: Optional[User],
    cluster_id: str,
    namespace: str,
    kind: str,
    name: str,
) -> Tuple[Optional[Dict[str, Any]], Optional[str], int]:
    """Restart a pod (delete & recreate) or a workload (rollout restart)."""
    normalized = _normalize_kind(kind)
    if not normalized or not name.strip():
        return None, "Invalid resource kind or name", 400
    if normalized not in RESTART_SUPPORTED_KINDS:
        return None, f"Restart is not supported for {normalized}", 400

    denied = _check_resource_restart_access(user, cluster_id, namespace, normalized, name)
    if denied:
        return None, denied[0], denied[1]

    if normalized == "pod":
        args = ["delete", "pod", name, "-n", namespace]
        mock_output = f"pod/{name} deleted"
    else:
        args = ["rollout", "restart", f"{normalized}/{name}", "-n", namespace]
        mock_output = f"{normalized}.apps/{name} restarted"

    if not should_use_real_k8s(cluster_id):
        data = {
            "restarted": True,
            "clusterId": cluster_id,
            "namespace": namespace,
            "kind": normalized,
            "name": name,
            "output": mock_output,
            "mode": "mock",
        }
        log_audit(
            "resource_restarted",
            actor=user,
            target_type=normalized,
            target_id=f"{cluster_id}/{namespace}/{name}",
            details={**data, "result": "success"},
        )
        return data, None, 200

    access = resolve_cluster_access(cluster_id)
    if not access:
        return None, "Cluster not found", 404

    try:
        output = _run_kubectl_for_cluster(cluster_id, args)
        data = {
            "restarted": True,
            "clusterId": cluster_id,
            "namespace": namespace,
            "kind": normalized,
            "name": name,
            "output": output.strip(),
        }
        log_audit(
            "resource_restarted",
            actor=user,
            target_type=normalized,
            target_id=f"{cluster_id}/{namespace}/{name}",
            details={**data, "result": "success"},
        )
        return data, None, 200
    except K8sCommandError as exc:
        log_audit(
            "resource_action_failed",
            actor=user,
            target_type=normalized,
            target_id=f"{cluster_id}/{namespace}/{name}",
            details={"action": "restart", "error": str(exc), "result": "failed"},
        )
        return None, str(exc), 503


def _mock_exec_output(command: str, container: Optional[str]) -> str:
    """Best-effort simulated output for common commands in mock mode."""
    cmd = command.strip()
    first = cmd.split()[0] if cmd else ""
    target = f" (container {container})" if container else ""
    if first in {"pwd"}:
        return "/"
    if first in {"whoami", "id"}:
        return "root" if first == "whoami" else "uid=0(root) gid=0(root) groups=0(root)"
    if first in {"hostname"}:
        return "mock-pod"
    if first in {"ls", "dir"}:
        return "bin\nboot\ndev\netc\nhome\nlib\nproc\nroot\nsys\ntmp\nusr\nvar"
    if first in {"env", "printenv"}:
        return "PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin\nHOME=/root\nKUBERNETES_SERVICE_HOST=10.96.0.1"
    if first in {"echo"}:
        return cmd[len("echo"):].strip()
    if first in {"cat"}:
        return f"(mock) contents of {cmd[len('cat'):].strip() or 'file'} not available in mock mode"
    return f"(mock environment{target}) command executed: {cmd}"


def exec_in_pod(
    user: Optional[User],
    cluster_id: str,
    namespace: str,
    pod_name: str,
    command: str,
    container: Optional[str] = None,
) -> Tuple[Optional[Dict[str, Any]], Optional[str], int]:
    """Run a single command inside a pod container (``kubectl exec -- sh -c``)."""
    if not pod_name.strip():
        return None, "Invalid pod name", 400

    command = (command or "").strip()
    if not command:
        return None, "Command is required", 400
    if len(command) > _MAX_EXEC_COMMAND_LENGTH:
        return None, f"Command exceeds {_MAX_EXEC_COMMAND_LENGTH} characters", 400

    container = (container or "").strip() or None

    # Reuse the restart access checks: exec is a write-level pod action.
    denied = _check_resource_restart_access(user, cluster_id, namespace, "pod", pod_name)
    if denied:
        return None, denied[0], denied[1]

    if not should_use_real_k8s(cluster_id):
        data = {
            "clusterId": cluster_id,
            "namespace": namespace,
            "pod": pod_name,
            "container": container,
            "command": command,
            "output": _mock_exec_output(command, container),
            "mode": "mock",
        }
        log_audit(
            "pod_exec",
            actor=user,
            target_type="pod",
            target_id=f"{cluster_id}/{namespace}/{pod_name}",
            details={"command": command, "container": container, "result": "success"},
        )
        return data, None, 200

    access = resolve_cluster_access(cluster_id)
    if not access:
        return None, "Cluster not found", 404

    args = ["exec", pod_name, "-n", namespace]
    if container:
        args += ["-c", container]
    args += ["--", "sh", "-c", command]

    try:
        output = _run_kubectl_for_cluster(cluster_id, args)
        log_audit(
            "pod_exec",
            actor=user,
            target_type="pod",
            target_id=f"{cluster_id}/{namespace}/{pod_name}",
            details={"command": command, "container": container, "result": "success"},
        )
        return {
            "clusterId": cluster_id,
            "namespace": namespace,
            "pod": pod_name,
            "container": container,
            "command": command,
            "output": output,
        }, None, 200
    except K8sCommandError as exc:
        log_audit(
            "resource_action_failed",
            actor=user,
            target_type="pod",
            target_id=f"{cluster_id}/{namespace}/{pod_name}",
            details={"action": "exec", "command": command, "error": str(exc), "result": "failed"},
        )
        # kubectl exec surfaces non-zero command exit codes as command errors; show
        # the captured stderr/stdout to the user rather than a generic failure.
        return None, str(exc), 400
