"""Operational actions on inventory workloads (restart, scale, rollback, rollout history)."""

from __future__ import annotations

import re
from typing import Any, Callable, Dict, List, Optional, Tuple

from ..access_engine import can_access_namespace, can_access_resource, user_has_permission
from ..audit import log_audit
from ..k8s_provider import K8sCommandError, resolve_cluster_access, should_use_real_k8s
from ..models import User
from .deployment_service import _run_kubectl_for_cluster

RunKubectlFn = Callable[[str, List[str]], str]

MAX_REPLICAS = 50
SUPPORTED_WORKLOAD_TYPE = "deployment"

MOCK_ROLLOUT_HISTORY: Dict[Tuple[str, str, str], List[Dict[str, Any]]] = {
    ("prod-us-east", "payments", "payments-api"): [
        {"revision": 1, "changeCause": "<none>"},
        {"revision": 2, "changeCause": "kubectl set image deployment/payments-api payments=ghcr.io/mock/payments:v2.7.0"},
        {"revision": 3, "changeCause": "kubectl set image deployment/payments-api payments=ghcr.io/mock/payments:v2.8.1"},
    ],
    ("prod-us-east", "payments", "ledger-worker"): [
        {"revision": 1, "changeCause": "<none>"},
        {"revision": 2, "changeCause": "helm upgrade ledger-worker"},
    ],
    ("staging-eu-west", "sandbox", "demo-app"): [
        {"revision": 1, "changeCause": "<none>"},
        {"revision": 2, "changeCause": "kubectl apply -f demo-app.yaml"},
    ],
}


def _parse_action_body(body: Dict[str, Any]) -> Tuple[Optional[Dict[str, str]], Optional[str], int]:
    cluster_id = (body.get("clusterId") or body.get("cluster") or "").strip()
    namespace = (body.get("namespace") or "").strip()
    workload_type = (body.get("workloadType") or body.get("workload_type") or "").strip().lower()
    workload_name = (body.get("workloadName") or body.get("workload_name") or "").strip()

    if not cluster_id or not namespace or not workload_name:
        return None, "clusterId, namespace, and workloadName are required", 400
    if workload_type != SUPPORTED_WORKLOAD_TYPE:
        return None, f'Only workloadType "{SUPPORTED_WORKLOAD_TYPE}" is supported', 400

    return {
        "clusterId": cluster_id,
        "namespace": namespace,
        "workloadType": workload_type,
        "workloadName": workload_name,
    }, None, 200


def _check_deployment_action_access(
    user: Optional[User],
    cluster_id: str,
    namespace: str,
    workload_name: str,
    action: str,
) -> Optional[Tuple[str, int]]:
    if user and not user_has_permission(user, "apps:deploy"):
        log_audit(
            "unauthorized_deployment_attempt",
            actor=user,
            target_type="deployment",
            target_id=f"{cluster_id}/{namespace}/{workload_name}",
            details={"action": action},
        )
        return "Forbidden", 403

    if user and not can_access_namespace(user, cluster_id, namespace):
        log_audit(
            "unauthorized_deployment_attempt",
            actor=user,
            target_type="namespace",
            target_id=f"{cluster_id}/{namespace}",
            details={"action": action, "deployment": workload_name},
        )
        return "Forbidden", 403

    if user and not can_access_resource(user, cluster_id, namespace, "deployment", workload_name):
        log_audit(
            "unauthorized_deployment_attempt",
            actor=user,
            target_type="deployment",
            target_id=f"{cluster_id}/{namespace}/{workload_name}",
            details={"action": action},
        )
        return "Forbidden", 403

    return None


def _deployment_resource_name(workload_name: str) -> str:
    return f"deployment/{workload_name}"


def parse_rollout_history(output: str) -> Dict[str, Any]:
    lines = [line.rstrip() for line in output.splitlines()]
    deployment_line = ""
    revisions: List[Dict[str, Any]] = []
    in_table = False

    for line in lines:
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.startswith("deployment.apps/") or stripped.startswith("deployment/"):
            deployment_line = stripped
            continue
        if stripped.startswith("REVISION"):
            in_table = True
            continue
        if in_table:
            match = re.match(r"^(\d+)\s+(.*)$", stripped)
            if match:
                revisions.append({
                    "revision": int(match.group(1)),
                    "changeCause": match.group(2).strip(),
                })

    deployment_name = ""
    if deployment_line:
        deployment_name = deployment_line.split("/", 1)[-1].split()[0]

    return {
        "deployment": deployment_name,
        "revisions": revisions,
        "raw": output,
    }


def _mock_rollout_history(cluster_id: str, namespace: str, workload_name: str) -> Dict[str, Any]:
    key = (cluster_id, namespace, workload_name)
    revisions = MOCK_ROLLOUT_HISTORY.get(key, [{"revision": 1, "changeCause": "<none>"}])
    raw_lines = [
        f"deployment.apps/{workload_name}",
        "REVISION  CHANGE-CAUSE",
    ]
    for rev in revisions:
        raw_lines.append(f"{rev['revision']:<9} {rev['changeCause']}")
    raw = "\n".join(raw_lines)
    return {
        "deployment": workload_name,
        "revisions": revisions,
        "raw": raw,
        "clusterId": cluster_id,
        "namespace": namespace,
    }


def get_rollout_history(
    user: Optional[User],
    cluster_id: str,
    namespace: str,
    workload_name: str,
    *,
    run_kubectl: Optional[RunKubectlFn] = None,
) -> Tuple[Optional[Dict[str, Any]], Optional[str], int]:
    denied = _check_deployment_action_access(user, cluster_id, namespace, workload_name, "rollout-history")
    if denied:
        return None, denied[0], denied[1]

    if not should_use_real_k8s(cluster_id):
        data = _mock_rollout_history(cluster_id, namespace, workload_name)
        log_audit(
            "deployment_rollout_history_viewed",
            actor=user,
            target_type="deployment",
            target_id=f"{cluster_id}/{namespace}/{workload_name}",
            details={"cluster": cluster_id, "namespace": namespace, "result": "success", "mode": "mock"},
        )
        return data, None, 200

    access = resolve_cluster_access(cluster_id)
    if not access:
        return None, "Cluster not found", 404

    try:
        runner = run_kubectl or _run_kubectl_for_cluster
        output = runner(
            cluster_id,
            ["rollout", "history", _deployment_resource_name(workload_name), "-n", namespace],
        )
        parsed = parse_rollout_history(output)
        parsed["clusterId"] = cluster_id
        parsed["namespace"] = namespace
        log_audit(
            "deployment_rollout_history_viewed",
            actor=user,
            target_type="deployment",
            target_id=f"{cluster_id}/{namespace}/{workload_name}",
            details={"cluster": cluster_id, "namespace": namespace, "result": "success"},
        )
        return parsed, None, 200
    except K8sCommandError as exc:
        log_audit(
            "deployment_action_failed",
            actor=user,
            target_type="deployment",
            target_id=f"{cluster_id}/{namespace}/{workload_name}",
            details={"action": "rollout-history", "error": str(exc), "result": "failed"},
        )
        return None, str(exc), 503


def restart_deployment(
    user: Optional[User],
    body: Dict[str, Any],
    *,
    run_kubectl: Optional[RunKubectlFn] = None,
) -> Tuple[Optional[Dict[str, Any]], Optional[str], int]:
    params, err, code = _parse_action_body(body)
    if err:
        return None, err, code

    cluster_id = params["clusterId"]
    namespace = params["namespace"]
    workload_name = params["workloadName"]

    denied = _check_deployment_action_access(user, cluster_id, namespace, workload_name, "restart")
    if denied:
        return None, denied[0], denied[1]

    if not should_use_real_k8s(cluster_id):
        output = f'deployment.apps/{workload_name} restarted'
        data = {
            "restarted": True,
            "clusterId": cluster_id,
            "namespace": namespace,
            "workloadName": workload_name,
            "workloadType": SUPPORTED_WORKLOAD_TYPE,
            "output": output,
        }
        log_audit(
            "deployment_restarted",
            actor=user,
            target_type="deployment",
            target_id=f"{cluster_id}/{namespace}/{workload_name}",
            details={**data, "result": "success", "mode": "mock"},
        )
        return data, None, 200

    try:
        runner = run_kubectl or _run_kubectl_for_cluster
        output = runner(
            cluster_id,
            ["rollout", "restart", _deployment_resource_name(workload_name), "-n", namespace],
        )
        data = {
            "restarted": True,
            "clusterId": cluster_id,
            "namespace": namespace,
            "workloadName": workload_name,
            "workloadType": SUPPORTED_WORKLOAD_TYPE,
            "output": output.strip(),
        }
        log_audit(
            "deployment_restarted",
            actor=user,
            target_type="deployment",
            target_id=f"{cluster_id}/{namespace}/{workload_name}",
            details={**data, "result": "success"},
        )
        return data, None, 200
    except K8sCommandError as exc:
        log_audit(
            "deployment_action_failed",
            actor=user,
            target_type="deployment",
            target_id=f"{cluster_id}/{namespace}/{workload_name}",
            details={"action": "restart", "error": str(exc), "result": "failed"},
        )
        return None, str(exc), 503


def scale_deployment(
    user: Optional[User],
    body: Dict[str, Any],
    *,
    run_kubectl: Optional[RunKubectlFn] = None,
) -> Tuple[Optional[Dict[str, Any]], Optional[str], int]:
    params, err, code = _parse_action_body(body)
    if err:
        return None, err, code

    replicas_raw = body.get("replicas")
    if replicas_raw is None:
        return None, "replicas is required", 400
    try:
        replicas = int(replicas_raw)
    except (TypeError, ValueError):
        return None, "replicas must be an integer", 400
    if replicas < 0 or replicas > MAX_REPLICAS:
        return None, f"replicas must be between 0 and {MAX_REPLICAS}", 400

    cluster_id = params["clusterId"]
    namespace = params["namespace"]
    workload_name = params["workloadName"]

    denied = _check_deployment_action_access(user, cluster_id, namespace, workload_name, "scale")
    if denied:
        return None, denied[0], denied[1]

    if not should_use_real_k8s(cluster_id):
        output = f'deployment.apps/{workload_name} scaled'
        data = {
            "scaled": True,
            "replicas": replicas,
            "clusterId": cluster_id,
            "namespace": namespace,
            "workloadName": workload_name,
            "workloadType": SUPPORTED_WORKLOAD_TYPE,
            "output": output,
        }
        log_audit(
            "deployment_scaled",
            actor=user,
            target_type="deployment",
            target_id=f"{cluster_id}/{namespace}/{workload_name}",
            details={**data, "result": "success", "mode": "mock"},
        )
        return data, None, 200

    try:
        runner = run_kubectl or _run_kubectl_for_cluster
        output = runner(
            cluster_id,
            [
                "scale",
                _deployment_resource_name(workload_name),
                f"--replicas={replicas}",
                "-n",
                namespace,
            ],
        )
        data = {
            "scaled": True,
            "replicas": replicas,
            "clusterId": cluster_id,
            "namespace": namespace,
            "workloadName": workload_name,
            "workloadType": SUPPORTED_WORKLOAD_TYPE,
            "output": output.strip(),
        }
        log_audit(
            "deployment_scaled",
            actor=user,
            target_type="deployment",
            target_id=f"{cluster_id}/{namespace}/{workload_name}",
            details={**data, "result": "success"},
        )
        return data, None, 200
    except K8sCommandError as exc:
        log_audit(
            "deployment_action_failed",
            actor=user,
            target_type="deployment",
            target_id=f"{cluster_id}/{namespace}/{workload_name}",
            details={"action": "scale", "replicas": replicas, "error": str(exc), "result": "failed"},
        )
        return None, str(exc), 503


def rollback_deployment(
    user: Optional[User],
    body: Dict[str, Any],
    *,
    run_kubectl: Optional[RunKubectlFn] = None,
) -> Tuple[Optional[Dict[str, Any]], Optional[str], int]:
    params, err, code = _parse_action_body(body)
    if err:
        return None, err, code

    cluster_id = params["clusterId"]
    namespace = params["namespace"]
    workload_name = params["workloadName"]
    revision = body.get("revision")

    if revision is not None:
        try:
            revision = int(revision)
        except (TypeError, ValueError):
            return None, "revision must be an integer", 400
        if revision < 1:
            return None, "revision must be at least 1", 400

    denied = _check_deployment_action_access(user, cluster_id, namespace, workload_name, "rollback")
    if denied:
        return None, denied[0], denied[1]

    if not should_use_real_k8s(cluster_id):
        output = f'deployment.apps/{workload_name} rolled back'
        data = {
            "rolledBack": True,
            "revision": revision,
            "clusterId": cluster_id,
            "namespace": namespace,
            "workloadName": workload_name,
            "workloadType": SUPPORTED_WORKLOAD_TYPE,
            "output": output,
        }
        log_audit(
            "deployment_rolled_back",
            actor=user,
            target_type="deployment",
            target_id=f"{cluster_id}/{namespace}/{workload_name}",
            details={**data, "result": "success", "mode": "mock"},
        )
        return data, None, 200

    try:
        args = ["rollout", "undo", _deployment_resource_name(workload_name), "-n", namespace]
        if revision is not None:
            args.append(f"--to-revision={revision}")

        runner = run_kubectl or _run_kubectl_for_cluster
        output = runner(cluster_id, args)
        data = {
            "rolledBack": True,
            "revision": revision,
            "clusterId": cluster_id,
            "namespace": namespace,
            "workloadName": workload_name,
            "workloadType": SUPPORTED_WORKLOAD_TYPE,
            "output": output.strip(),
        }
        log_audit(
            "deployment_rolled_back",
            actor=user,
            target_type="deployment",
            target_id=f"{cluster_id}/{namespace}/{workload_name}",
            details={**data, "result": "success"},
        )
        return data, None, 200
    except K8sCommandError as exc:
        log_audit(
            "deployment_action_failed",
            actor=user,
            target_type="deployment",
            target_id=f"{cluster_id}/{namespace}/{workload_name}",
            details={"action": "rollback", "revision": revision, "error": str(exc), "result": "failed"},
        )
        return None, str(exc), 503
