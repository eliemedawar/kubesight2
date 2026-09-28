"""Read-only kubectl actions for Resources page (describe, YAML, rollout history)."""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

import yaml

from ..access_engine import can_access_namespace, can_access_resource, user_has_permission
from ..audit import log_audit
from ..k8s_provider import (
    K8sCommandError,
    cached_namespace_read,
    resolve_cluster_access,
    should_use_real_k8s,
)
from ..k8s_provider import _run_for_access
from ..k8s_names import name_error
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
    "ingress": "ingress",
    "ingresses": "ingress",
    "ing": "ingress",
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
    # ConfigMaps, Secrets and Ingresses have no dedicated view permission; they are
    # gated by the page-level resources:view (and per-namespace access rules).
    "configmap": "resources:view",
    "secret": "resources:view",
    "ingress": "resources:view",
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
    "ingress": "Ingress",
}

# Kinds that support the Resources "Restart" action. Pods are restarted by
# deletion (the owning controller recreates them); workloads use rollout restart.
RESTART_SUPPORTED_KINDS = {"pod", "deployment", "statefulset", "daemonset"}
_KIND_LABELS = {"pod": "Pod", "deployment": "Deployment", "statefulset": "StatefulSet", "daemonset": "DaemonSet"}

# Permission gating writes from the Resources page (mirrors the deployment edit action).
RESTART_PERMISSION = "apps:deploy"

# Exec into a pod runs arbitrary commands in a container, so it is gated by the
# same write-level permission as restart.
EXEC_PERMISSION = "apps:deploy"

# Upper bound on a single exec command length (defensive — avoids unbounded argv).
_MAX_EXEC_COMMAND_LENGTH = 4000

# Reading a Secret's VALUES is its own permission. resources:view lets a user see
# that a Secret exists, its type and its key names — enough to debug a missing
# env var — but not the credentials in it. Without secrets:reveal, YAML comes
# back with every data/stringData value replaced by this marker. `kubectl
# describe secret` never prints values (only byte counts), so describe needs no
# redaction.
SECRET_REVEAL_PERMISSION = "secrets:reveal"
REDACTED_SECRET_VALUE = "<hidden: secrets:reveal required>"
_SECRET_VALUE_SECTIONS = ("data", "stringData")
# kubectl copies the whole applied manifest — values included — into this
# annotation, so it leaks exactly what the data section was redacted for.
_LAST_APPLIED_ANNOTATION = "kubectl.kubernetes.io/last-applied-configuration"


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
    invalid = name_error(namespace=namespace, names=((name, "resource name"),))
    if invalid:
        return invalid, 400

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


def can_reveal_secret_values(user: Optional[User]) -> bool:
    """Whether ``user`` may read Secret values. ``None`` is an internal caller."""
    return user is None or user_has_permission(user, SECRET_REVEAL_PERMISSION)


def _redact_secret_doc(doc: Dict[str, Any], hidden_keys: List[str]) -> None:
    for section in _SECRET_VALUE_SECTIONS:
        values = doc.get(section)
        if isinstance(values, dict):
            for key in values:
                values[key] = REDACTED_SECRET_VALUE
                hidden_keys.append(str(key))
        elif values:
            # Not a mapping (malformed): replace it whole rather than guess.
            doc[section] = REDACTED_SECRET_VALUE
    annotations = (doc.get("metadata") or {}).get("annotations")
    if isinstance(annotations, dict) and _LAST_APPLIED_ANNOTATION in annotations:
        annotations[_LAST_APPLIED_ANNOTATION] = REDACTED_SECRET_VALUE


def redact_secret_yaml(yaml_content: str) -> Tuple[str, List[str]]:
    """Replace every Secret value in ``yaml_content``; keep keys and metadata.

    Handles single documents, multi-document streams and ``kind: List``. Returns
    ``(redacted_yaml, hidden_key_names)``. Text that cannot be parsed is NOT
    passed through — a parse failure must not become a leak.
    """
    try:
        documents = [doc for doc in yaml.safe_load_all(yaml_content or "") if doc is not None]
    except yaml.YAMLError:
        return "# Secret values hidden (the YAML could not be parsed for redaction).\n", []

    hidden: List[str] = []
    for doc in documents:
        if not isinstance(doc, dict):
            continue
        candidates = [doc]
        if isinstance(doc.get("items"), list):
            candidates.extend(item for item in doc["items"] if isinstance(item, dict))
        for candidate in candidates:
            if candidate.get("kind") == "Secret":
                _redact_secret_doc(candidate, hidden)
    dumped = "---\n".join(
        yaml.safe_dump(doc, default_flow_style=False, sort_keys=False) for doc in documents
    )
    return dumped, hidden


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
    if kind == "ingress":
        return (
            f"apiVersion: networking.k8s.io/v1\n"
            f"kind: Ingress\n"
            f"metadata:\n"
            f"  name: {name}\n"
            f"  namespace: {namespace}\n"
            f"spec:\n"
            f"  rules:\n"
            f"    - host: {name}.example.com\n"
            f"      http:\n"
            f"        paths:\n"
            f"          - path: /\n"
            f"            pathType: Prefix\n"
            f"            backend:\n"
            f"              service:\n"
            f"                name: {name}\n"
            f"                port:\n"
            f"                  number: 80\n"
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
        # Read-only kubectl call — cache under the namespace-read prefix so
        # reopening the same resource is instant and concurrent identical
        # requests single-flight one kubectl process. Mutation invalidation
        # (deploy/restart/scale) already clears the `res:{cluster}:{ns}:` prefix.
        output = cached_namespace_read(
            access,
            namespace,
            f"describe:{normalized}:{name}",
            lambda: _run_for_access(access, ["describe", normalized, name, "-n", namespace]),
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

    is_secret = normalized == "secret"
    reveal = is_secret and can_reveal_secret_values(user)

    def _secret_fields(hidden_keys: List[str]) -> Dict[str, Any]:
        if not is_secret:
            return {}
        return {
            "valuesHidden": not reveal,
            "hiddenKeys": sorted(set(hidden_keys)),
            "revealPermission": SECRET_REVEAL_PERMISSION,
        }

    if not should_use_real_k8s(cluster_id):
        yaml_content = _mock_yaml(normalized, namespace, name)
        hidden: List[str] = []
        if is_secret and not reveal:
            yaml_content, hidden = redact_secret_yaml(yaml_content)
        return {
            "clusterId": cluster_id,
            "namespace": namespace,
            "kind": normalized,
            "name": name,
            "yaml": yaml_content,
            "mode": "mock",
            **_secret_fields(hidden),
        }, None, 200

    access = resolve_cluster_access(cluster_id)
    if not access:
        return None, "Cluster not found", 404

    def _fetch() -> str:
        return _run_for_access(access, ["get", normalized, name, "-n", namespace, "-o", "yaml"])

    try:
        hidden = []
        if not is_secret:
            # Read-only kubectl call — cached (see get_resource_describe rationale).
            yaml_content = cached_namespace_read(access, namespace, f"yaml:{normalized}:{name}", _fetch)
        elif reveal:
            # Never cached: the plaintext is fetched for this request only and is
            # not kept in a process-wide cache another request could be served from.
            yaml_content = _fetch()
        else:
            # Only the REDACTED form is cached, under its own key, so the cache
            # can never hand values to a user without secrets:reveal.
            yaml_content = cached_namespace_read(
                access,
                namespace,
                f"yaml:secret:{name}:redacted",
                lambda: redact_secret_yaml(_fetch())[0],
            )
            hidden = redact_secret_yaml(yaml_content)[1]
        log_audit(
            "secret_values_revealed" if reveal else "resource_yaml_viewed",
            actor=user,
            target_type=normalized,
            target_id=f"{cluster_id}/{namespace}/{name}",
            details={"valuesHidden": not reveal} if is_secret else None,
        )
        return {
            "clusterId": cluster_id,
            "namespace": namespace,
            "kind": normalized,
            "name": name,
            "yaml": yaml_content,
            **_secret_fields(hidden),
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
    invalid = name_error(namespace=namespace, names=((name, "resource name"),))
    if invalid:
        return None, invalid, 400

    denied = _check_resource_restart_access(user, cluster_id, namespace, normalized, name)
    if denied:
        return None, denied[0], denied[1]

    # The cluster's approval rule: without a live approved request the restart is
    # sent for approval and carried out automatically once approved (202).
    # A call without a user is held to the rule too (refused, not queued).
    from .change_bundle_service import gate_or_queue

    queued = gate_or_queue(
        user,
        cluster_id,
        bundle_payload={
            "actionType": "restart_workload",
            "namespace": namespace,
            "resourceKind": _KIND_LABELS.get(normalized, normalized),
            "resourceName": name,
        },
        what=f"restart {normalized}/{name}",
        action="restart",
        target_type=normalized,
        target_id=f"{cluster_id}/{namespace}/{name}",
    )
    if queued is not None:
        return queued

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
    invalid = name_error(
        namespace=namespace,
        names=((pod_name, "pod name"),),
        containers=((container, "container name"),),
    )
    if invalid:
        return None, invalid, 400

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
