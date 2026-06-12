"""Automated cluster upgrades for kubeadm and cloud/minikube providers."""

from __future__ import annotations

import json
import os
import re
import subprocess
import time
import uuid
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List

from .cluster_access import ClusterAccess
from .k8s_provider import K8sCommandError
from .upgrade_jobs import create_job, run_job_async, update_job
from .upgrade_provider import UPGRADE_PLAN_STEPS, normalize_version, parse_k8s_version

RunKubectlFn = Callable[[ClusterAccess, List[str]], str]

DEFAULT_DEBUG_NAMESPACE = "default"
POD_WAIT_TIMEOUT_SECONDS = 1800
POD_POLL_INTERVAL_SECONDS = 3
NODE_EXECUTOR_IMAGE = "debian:bookworm-slim"

# Substrings that indicate the API server is temporarily unavailable (e.g. restarting
# during kubeadm upgrade apply). These are retried rather than treated as failures.
_TRANSIENT_API_ERROR_MARKERS = (
    "connectex",
    "connection refused",
    "dial tcp",
    "unable to connect to the server",
    "no connection could be made",
    "i/o timeout",
    "eof",
    "context deadline exceeded",
    "net/http: request canceled",
    "tls handshake timeout",
)


def _is_transient_api_error(exc: Exception) -> bool:
    lower = str(exc).lower()
    return any(marker in lower for marker in _TRANSIENT_API_ERROR_MARKERS)


def _package_version(target_version: str) -> str:
    return normalize_version(target_version).lstrip("v")


_APT_SHELL_PREAMBLE = """
export DEBIAN_FRONTEND=noninteractive
APT_OPTS="-o DPkg::Lock::Timeout=600 -o APT::Acquire::Retries=5"
_wait_for_apt() {
  local timeout=600 elapsed=0
  while pgrep -fx "apt-get|apt|dpkg|unattended-upgrade" >/dev/null 2>&1; do
    if [ "$elapsed" -ge "$timeout" ]; then
      echo "Timed out waiting for apt/dpkg (another package manager is still running)."
      exit 1
    fi
    echo "Waiting for apt/dpkg to finish (${elapsed}s)..."
    sleep 10
    elapsed=$((elapsed + 10))
  done
}
_apt() {
  local attempt=1
  local apt_out
  while [ "$attempt" -le 30 ]; do
    _wait_for_apt
    apt_out=$(apt-get $APT_OPTS "$@" 2>&1)
    if [ $? -eq 0 ]; then
      printf '%s\n' "$apt_out"
      return 0
    fi
    printf '%s\n' "$apt_out" >&2
    # Version-not-found and missing-package errors are permanent — retrying will
    # never help.  Fail immediately with a clear message.
    if printf '%s\n' "$apt_out" | grep -qiE \
        "version .+ (for .+ )?was not found|unable to locate package|no packages found|has no installation candidate"; then
      echo "ERROR: Package not available in the repository. Verify that the target" \
           "Kubernetes version has been released and that pkgs.k8s.io is reachable." >&2
      return 1
    fi
    echo "apt-get failed (attempt ${attempt}/30); retrying after lock wait..."
    sleep 10
    attempt=$((attempt + 1))
  done
  return 1
}
"""


def _shell_script_install_kubeadm_only(target_version: str) -> str:
    version = _package_version(target_version)
    return f"""set -e
K8S_VER="{version}"
{_APT_SHELL_PREAMBLE}
if command -v apt-get >/dev/null 2>&1; then
  apt-mark unhold kubeadm 2>/dev/null || true
  _apt update -qq
  _apt install -y -qq kubeadm="${{K8S_VER}}-*"
  apt-mark hold kubeadm 2>/dev/null || true
elif command -v dnf >/dev/null 2>&1; then
  dnf install -y "kubeadm-${{K8S_VER}}" || dnf install -y "kubeadm-${{K8S_VER}}-*"
elif command -v yum >/dev/null 2>&1; then
  yum install -y "kubeadm-${{K8S_VER}}" || yum install -y "kubeadm-${{K8S_VER}}-*"
else
  echo "Unsupported OS: install kubeadm ${{K8S_VER}} with apt, dnf, or yum before upgrading."
  exit 1
fi
kubeadm version -o short
"""


def _shell_script_install_kubelet_kubectl(target_version: str) -> str:
    version = _package_version(target_version)
    return f"""set -e
K8S_VER="{version}"
{_APT_SHELL_PREAMBLE}
if command -v apt-get >/dev/null 2>&1; then
  apt-mark unhold kubelet kubectl 2>/dev/null || true
  _apt install -y -qq kubelet="${{K8S_VER}}-*" kubectl="${{K8S_VER}}-*"
  apt-mark hold kubelet kubectl 2>/dev/null || true
elif command -v dnf >/dev/null 2>&1; then
  dnf install -y "kubelet-${{K8S_VER}}" "kubectl-${{K8S_VER}}" || \\
    dnf install -y "kubelet-${{K8S_VER}}-*" "kubectl-${{K8S_VER}}-*"
elif command -v yum >/dev/null 2>&1; then
  yum install -y "kubelet-${{K8S_VER}}" "kubectl-${{K8S_VER}}" || \\
    yum install -y "kubelet-${{K8S_VER}}-*" "kubectl-${{K8S_VER}}-*"
else
  echo "Unsupported OS: install kubelet and kubectl ${{K8S_VER}} before continuing."
  exit 1
fi
systemctl daemon-reload
systemctl restart kubelet
"""


def _shell_script_control_plane_upgrade(target_version: str) -> str:
    """Single on-node script: packages, kubeadm apply, kubelet — one apt session."""
    normalized = normalize_version(target_version)
    version = _package_version(target_version)
    return f"""set -e
K8S_VER="{version}"
TARGET="{normalized}"
{_APT_SHELL_PREAMBLE}
if command -v apt-get >/dev/null 2>&1; then
  apt-mark unhold kubeadm kubelet kubectl 2>/dev/null || true
  _apt update -qq
  _apt install -y -qq kubeadm="${{K8S_VER}}-*"
  apt-mark hold kubeadm 2>/dev/null || true
elif command -v dnf >/dev/null 2>&1; then
  dnf install -y "kubeadm-${{K8S_VER}}" || dnf install -y "kubeadm-${{K8S_VER}}-*"
elif command -v yum >/dev/null 2>&1; then
  yum install -y "kubeadm-${{K8S_VER}}" || yum install -y "kubeadm-${{K8S_VER}}-*"
else
  echo "Unsupported OS: install kubeadm ${{K8S_VER}} with apt, dnf, or yum before upgrading."
  exit 1
fi
kubeadm version -o short
kubeadm upgrade plan "$TARGET"
kubeadm upgrade apply -y "$TARGET"
if command -v apt-get >/dev/null 2>&1; then
  apt-mark unhold kubelet kubectl 2>/dev/null || true
  _apt install -y -qq kubelet="${{K8S_VER}}-*" kubectl="${{K8S_VER}}-*"
  apt-mark hold kubelet kubectl 2>/dev/null || true
elif command -v dnf >/dev/null 2>&1; then
  dnf install -y "kubelet-${{K8S_VER}}" "kubectl-${{K8S_VER}}" || \\
    dnf install -y "kubelet-${{K8S_VER}}-*" "kubectl-${{K8S_VER}}-*"
elif command -v yum >/dev/null 2>&1; then
  yum install -y "kubelet-${{K8S_VER}}" "kubectl-${{K8S_VER}}" || \\
    yum install -y "kubelet-${{K8S_VER}}-*" "kubectl-${{K8S_VER}}-*"
fi
systemctl daemon-reload
systemctl restart kubelet
"""


def _shell_script_upgrade_worker_node(target_version: str) -> str:
    version = _package_version(target_version)
    return f"""set -e
K8S_VER="{version}"
{_APT_SHELL_PREAMBLE}
if command -v apt-get >/dev/null 2>&1; then
  apt-mark unhold kubeadm kubelet kubectl 2>/dev/null || true
  _apt update -qq
  _apt install -y -qq \\
    kubeadm="${{K8S_VER}}-*" kubelet="${{K8S_VER}}-*" kubectl="${{K8S_VER}}-*"
  apt-mark hold kubeadm kubelet kubectl 2>/dev/null || true
elif command -v dnf >/dev/null 2>&1; then
  dnf install -y "kubeadm-${{K8S_VER}}" "kubelet-${{K8S_VER}}" "kubectl-${{K8S_VER}}" || \\
    dnf install -y "kubeadm-${{K8S_VER}}-*" "kubelet-${{K8S_VER}}-*" "kubectl-${{K8S_VER}}-*"
elif command -v yum >/dev/null 2>&1; then
  yum install -y "kubeadm-${{K8S_VER}}" "kubelet-${{K8S_VER}}" "kubectl-${{K8S_VER}}" || \\
    yum install -y "kubeadm-${{K8S_VER}}-*" "kubelet-${{K8S_VER}}-*" "kubectl-${{K8S_VER}}-*"
else
  echo "Unsupported OS: install kubeadm/kubelet/kubectl ${{K8S_VER}} on the worker first."
  exit 1
fi
kubeadm upgrade node
systemctl daemon-reload
systemctl restart kubelet
"""


def _is_control_plane_node(labels: Dict[str, Any]) -> bool:
    # Role labels are often present with an empty string value — use key membership.
    return (
        "node-role.kubernetes.io/control-plane" in labels
        or "node-role.kubernetes.io/master" in labels
    )


def _control_plane_nodes(node_items: List[Dict[str, Any]]) -> List[str]:
    names: List[str] = []
    for node in node_items:
        labels = node.get("metadata", {}).get("labels", {}) or {}
        if _is_control_plane_node(labels):
            names.append(node.get("metadata", {}).get("name", ""))
    if names:
        return [name for name in names if name]
    if len(node_items) == 1:
        return [node_items[0].get("metadata", {}).get("name", "")]
    return []


def _worker_nodes(node_items: List[Dict[str, Any]], control_planes: List[str]) -> List[str]:
    cp_set = set(control_planes)
    return [
        node.get("metadata", {}).get("name", "")
        for node in node_items
        if node.get("metadata", {}).get("name") not in cp_set
    ]


def _step_states() -> List[Dict[str, Any]]:
    return [
        {"step": index + 1, "name": name, "status": "pending", "message": ""}
        for index, name in enumerate(UPGRADE_PLAN_STEPS)
    ]


def _mark_step(steps: List[Dict[str, Any]], index: int, status: str, message: str) -> None:
    if 0 <= index < len(steps):
        steps[index]["status"] = status
        steps[index]["message"] = message


def _run_upgrade_kubectl(
    access: ClusterAccess,
    run_kubectl: RunKubectlFn,
    args: List[str],
) -> str:
    """Run kubectl without inheriting a kubeconfig default namespace (e.g. kubesight)."""
    if "-n" not in args and "--namespace" not in args:
        return run_kubectl(access, ["--namespace", "default", *args])
    return run_kubectl(access, args)


def _kubectl_command(access: ClusterAccess, args: List[str]) -> List[str]:
    command = ["kubectl"]
    if access.kubeconfig_path:
        command += ["--kubeconfig", access.kubeconfig_path]
    if access.context_name:
        command += ["--context", access.context_name]
    command += args
    return command


def _kubectl_apply_manifest(access: ClusterAccess, manifest: Dict[str, Any]) -> str:
    env = os.environ.copy()
    if access.kubeconfig_path:
        env["KUBECONFIG"] = access.kubeconfig_path
    elif not env.get("KUBECONFIG") and env.get("K8S_KUBECONFIG"):
        env["KUBECONFIG"] = env["K8S_KUBECONFIG"]

    completed = subprocess.run(
        _kubectl_command(access, ["apply", "-f", "-"]),
        input=json.dumps(manifest),
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
        env=env,
    )
    combined = "\n".join(part for part in (completed.stdout, completed.stderr) if part).strip()
    if completed.returncode != 0:
        raise RuntimeError(combined or "Failed to apply upgrade job manifest.")
    return combined


def _wait_for_job_completion(
    access: ClusterAccess,
    run_kubectl: RunKubectlFn,
    job_name: str,
    *,
    timeout_seconds: int = POD_WAIT_TIMEOUT_SECONDS,
) -> str:
    deadline = time.time() + timeout_seconds
    while time.time() < deadline:
        try:
            job_output = _run_upgrade_kubectl(
                access,
                run_kubectl,
                ["get", "job", job_name, "-n", DEFAULT_DEBUG_NAMESPACE, "-o", "json"],
            )
        except Exception as exc:
            err_lower = str(exc).lower()
            # The API server restarts during kubeadm upgrade apply — wait for it to
            # come back up instead of treating the transient disconnect as a failure.
            if _is_transient_api_error(exc):
                time.sleep(POD_POLL_INTERVAL_SECONDS)
                continue
            # Job was garbage-collected by Kubernetes after ttlSecondsAfterFinished
            # elapsed while the API server was restarting.  The job completed (TTL only
            # starts on completion); let _verify_server_version decide if it succeeded.
            if "not found" in err_lower or "notfound" in err_lower.replace(" ", ""):
                return ""
            raise
        job = json.loads(job_output)
        status = job.get("status", {})
        if status.get("succeeded"):
            pods_output = _run_upgrade_kubectl(
                access,
                run_kubectl,
                [
                    "get",
                    "pods",
                    "-n",
                    DEFAULT_DEBUG_NAMESPACE,
                    "-l",
                    f"job-name={job_name}",
                    "-o",
                    "json",
                ],
            )
            items = json.loads(pods_output).get("items", [])
            if not items:
                raise RuntimeError(f"Upgrade job {job_name} succeeded but no pod was found.")
            pod = items[0]
            pod_name = pod.get("metadata", {}).get("name", job_name)
            for container_status in pod.get("status", {}).get("containerStatuses", []) or []:
                terminated = container_status.get("state", {}).get("terminated") or {}
                exit_code = terminated.get("exitCode")
                if exit_code not in (None, 0):
                    logs = _run_upgrade_kubectl(
                        access,
                        run_kubectl,
                        ["logs", pod_name, "-n", DEFAULT_DEBUG_NAMESPACE],
                    )
                    raise RuntimeError(
                        logs[:1000]
                        or f"Remote command failed on node (exit {exit_code})."
                    )
            return _run_upgrade_kubectl(
                access,
                run_kubectl,
                ["logs", pod_name, "-n", DEFAULT_DEBUG_NAMESPACE],
            )
        if status.get("failed"):
            pods_output = _run_upgrade_kubectl(
                access,
                run_kubectl,
                [
                    "get",
                    "pods",
                    "-n",
                    DEFAULT_DEBUG_NAMESPACE,
                    "-l",
                    f"job-name={job_name}",
                    "-o",
                    "json",
                ],
            )
            items = json.loads(pods_output).get("items", [])
            pod_name = items[0].get("metadata", {}).get("name", job_name) if items else job_name
            try:
                logs = _run_upgrade_kubectl(
                    access,
                    run_kubectl,
                    ["logs", pod_name, "-n", DEFAULT_DEBUG_NAMESPACE],
                )
            except K8sCommandError:
                logs = ""
            raise RuntimeError(logs[:1000] or f"Upgrade job {job_name} failed.")
        time.sleep(POD_POLL_INTERVAL_SECONDS)
    raise RuntimeError(f"Timed out waiting for upgrade job {job_name}.")


def _delete_upgrade_job(
    access: ClusterAccess,
    run_kubectl: RunKubectlFn,
    job_name: str,
) -> None:
    try:
        _run_upgrade_kubectl(
            access,
            run_kubectl,
            ["delete", "job", job_name, "-n", DEFAULT_DEBUG_NAMESPACE, "--ignore-not-found", "--wait=false"],
        )
    except K8sCommandError:
        pass


def _run_on_node(
    access: ClusterAccess,
    run_kubectl: RunKubectlFn,
    node_name: str,
    shell_command: str,
) -> str:
    job_name = f"kubesight-upg-{uuid.uuid4().hex[:8]}"
    manifest = {
        "apiVersion": "batch/v1",
        "kind": "Job",
        "metadata": {
            "name": job_name,
            "namespace": DEFAULT_DEBUG_NAMESPACE,
            "labels": {"app.kubernetes.io/name": "kubesight-upgrade"},
        },
        "spec": {
            "ttlSecondsAfterFinished": 600,
            "backoffLimit": 0,
            "template": {
                "metadata": {"labels": {"job-name": job_name, "app.kubernetes.io/name": "kubesight-upgrade"}},
                "spec": {
                    "nodeName": node_name,
                    "hostPID": True,
                    "hostNetwork": True,
                    "dnsPolicy": "ClusterFirstWithHostNet",
                    "restartPolicy": "Never",
                    # Control-plane nodes carry NoSchedule taints; tolerate them so the
                    # upgrade Job pod is admitted by the kubelet on those nodes.
                    "tolerations": [
                        {
                            "key": "node-role.kubernetes.io/control-plane",
                            "operator": "Exists",
                            "effect": "NoSchedule",
                        },
                        {
                            "key": "node-role.kubernetes.io/master",
                            "operator": "Exists",
                            "effect": "NoSchedule",
                        },
                    ],
                    "containers": [
                        {
                            "name": "executor",
                            "image": NODE_EXECUTOR_IMAGE,
                            "command": ["chroot", "/host", "sh", "-c", shell_command],
                            "securityContext": {"privileged": True},
                            "volumeMounts": [{"name": "host-root", "mountPath": "/host"}],
                        }
                    ],
                    "volumes": [{"name": "host-root", "hostPath": {"path": "/", "type": "Directory"}}],
                },
            },
        },
    }
    try:
        _kubectl_apply_manifest(access, manifest)
        output = _wait_for_job_completion(access, run_kubectl, job_name)
    finally:
        _delete_upgrade_job(access, run_kubectl, job_name)

    output = output.strip()
    lowered = output.lower()
    failure_markers = (
        "error execution phase",
        "can't upgrade",
        "cannot upgrade",
        "precheck failed",
        "component status is",
        "unable to locate package",
        "no package kubeadm",
        "unsupported os:",
        "such an upgrade is not supported",
        "could not get lock",
        "unable to lock directory",
        "timed out waiting for apt",
        "was not found",
        "has no installation candidate",
        "package not available in the repository",
    )
    if any(marker in lowered for marker in failure_markers):
        raise RuntimeError(output[:1000] or f"Remote command failed on node {node_name}")
    return output


def _read_server_version(access: ClusterAccess, run_kubectl: RunKubectlFn) -> str:
    version_output = _run_upgrade_kubectl(access, run_kubectl, ["version", "-o", "json"])
    return json.loads(version_output).get("serverVersion", {}).get("gitVersion") or "unknown"


def _verify_server_version(
    access: ClusterAccess,
    run_kubectl: RunKubectlFn,
    target_version: str,
    *,
    previous_version: str,
) -> str:
    # After kubeadm upgrade apply the API server restarts; retry for up to 3 minutes
    # so a still-warming-up server doesn't look like a verification failure.
    deadline = time.time() + 180
    while True:
        try:
            server_version = _read_server_version(access, run_kubectl)
            break
        except Exception as exc:
            if _is_transient_api_error(exc) and time.time() < deadline:
                time.sleep(5)
                continue
            raise
    if parse_k8s_version(server_version) <= parse_k8s_version(previous_version):
        raise RuntimeError(
            f"Post-upgrade verification failed: API server is still {server_version}. "
            f"kubeadm commands may not have executed on the node. "
            f"Run the upgrade manually on the control plane: "
            f"kubeadm upgrade apply {normalize_version(target_version)}"
        )
    target_tuple = parse_k8s_version(target_version)
    server_tuple = parse_k8s_version(server_version)
    if server_tuple[:2] < target_tuple[:2]:
        raise RuntimeError(
            f"Post-upgrade verification failed: API server is {server_version}, "
            f"expected at least {normalize_version(target_version)}."
        )
    return server_version


def _run_cli(command: List[str], timeout: int = 600) -> str:
    completed = subprocess.run(
        command,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout or "").strip()
        raise RuntimeError(detail or f"Command failed: {' '.join(command)}")
    return (completed.stdout or "").strip()


def _worker_kubelet_version(worker_name: str, node_items: List[Dict[str, Any]]) -> str:
    node = next((n for n in node_items if n.get("metadata", {}).get("name") == worker_name), None)
    return (node or {}).get("status", {}).get("nodeInfo", {}).get("kubeletVersion") or "unknown"


def _execute_kubeadm_upgrade(
    access: ClusterAccess,
    target_version: str,
    run_kubectl: RunKubectlFn,
    job_id: str,
) -> None:
    normalized = normalize_version(target_version)
    steps = _step_states()
    update_job(job_id, steps=steps, activeStep=0)

    nodes_output = _run_upgrade_kubectl(access, run_kubectl, ["get", "nodes", "-o", "json"])
    node_items = json.loads(nodes_output).get("items", [])
    control_planes = _control_plane_nodes(node_items)
    workers = _worker_nodes(node_items, control_planes)
    if not control_planes:
        raise RuntimeError("No control plane node found for kubeadm upgrade.")

    previous_version = _read_server_version(access, run_kubectl)

    # Detect a partial upgrade: control plane already at target but some workers lag.
    cp_already_done = parse_k8s_version(previous_version) >= parse_k8s_version(normalized)

    _mark_step(steps, 0, "completed", "Cluster readiness validated.")
    _mark_step(steps, 1, "completed", f"Target version {normalized} validated.")
    update_job(job_id, steps=steps, activeStep=1)

    cordoned_workers: List[str] = []
    try:
        if cp_already_done:
            # Control plane was upgraded in a previous run; resume at worker upgrade.
            pending = [
                w for w in workers
                if parse_k8s_version(_worker_kubelet_version(w, node_items)) < parse_k8s_version(normalized)
            ]
            _mark_step(steps, 2, "completed", f"Resuming partial upgrade; {len(pending)} worker(s) still need upgrading.")
            _mark_step(steps, 3, "completed", f"Control plane already at {previous_version}; skipping.")
            update_job(job_id, steps=steps, activeStep=3)
        else:
            # Fresh upgrade: drain all workers before touching the control plane.
            for worker in workers:
                _run_upgrade_kubectl(access, run_kubectl, ["cordon", worker])
                cordoned_workers.append(worker)
                _run_upgrade_kubectl(
                    access,
                    run_kubectl,
                    [
                        "drain", worker,
                        "--ignore-daemonsets",
                        "--delete-emptydir-data",
                        "--timeout=300s",
                    ],
                )

            _mark_step(steps, 2, "completed", f"Drained {len(workers)} worker node(s).")
            update_job(job_id, steps=steps, activeStep=2)

            cp_node = control_planes[0]
            _run_on_node(
                access,
                run_kubectl,
                cp_node,
                _shell_script_control_plane_upgrade(normalized),
            )

            server_version = _verify_server_version(
                access,
                run_kubectl,
                normalized,
                previous_version=previous_version,
            )

            _mark_step(
                steps,
                3,
                "completed",
                f"Control plane upgraded on {cp_node} ({previous_version} -> {server_version}).",
            )
            update_job(job_id, steps=steps, activeStep=3)

        for worker in workers:
            # Skip workers that are already at the target kubelet version.
            worker_ver = _worker_kubelet_version(worker, node_items)
            if parse_k8s_version(worker_ver) >= parse_k8s_version(normalized):
                try:
                    _run_upgrade_kubectl(access, run_kubectl, ["uncordon", worker])
                except Exception:
                    pass
                continue

            # Cordon + drain if not already done in this run (resume path).
            if worker not in cordoned_workers:
                _run_upgrade_kubectl(access, run_kubectl, ["cordon", worker])
                cordoned_workers.append(worker)
                _run_upgrade_kubectl(
                    access,
                    run_kubectl,
                    [
                        "drain", worker,
                        "--ignore-daemonsets",
                        "--delete-emptydir-data",
                        "--timeout=300s",
                    ],
                )

            _run_on_node(
                access,
                run_kubectl,
                worker,
                _shell_script_upgrade_worker_node(normalized),
            )
            _run_upgrade_kubectl(access, run_kubectl, ["uncordon", worker])
            if worker in cordoned_workers:
                cordoned_workers.remove(worker)

    except Exception as exc:
        for worker in cordoned_workers:
            try:
                _run_upgrade_kubectl(access, run_kubectl, ["uncordon", worker])
            except Exception:
                pass
        message = str(exc)
        if 'namespaces "kubesight" not found' in message or "namespace" in message.lower():
            message = (
                f"{message} The cluster kubeconfig may set a default namespace (e.g. kubesight) "
                "that does not exist on this cluster."
            )
        raise RuntimeError(message) from exc

    final_version = _read_server_version(access, run_kubectl)
    upgraded_count = sum(
        1 for w in workers
        if parse_k8s_version(_worker_kubelet_version(w, node_items)) >= parse_k8s_version(normalized)
    )
    _mark_step(steps, 4, "completed", f"Upgraded {upgraded_count}/{len(workers)} worker node(s).")
    _mark_step(steps, 5, "completed", "Workloads validated.")
    _mark_step(steps, 6, "completed", f"API server reports {final_version}.")
    update_job(
        job_id,
        status="completed",
        message=f"Cluster upgraded to {final_version}.",
        steps=steps,
        activeStep=6,
        finishedAt=datetime.now(timezone.utc).isoformat(),
    )


def _execute_minikube_upgrade(target_version: str, job_id: str) -> None:
    normalized = normalize_version(target_version).lstrip("v")
    steps = _step_states()
    update_job(job_id, steps=steps, activeStep=0)
    _run_cli(["minikube", "stop"], timeout=300)
    _mark_step(steps, 0, "completed", "Minikube stopped.")
    _run_cli(["minikube", "start", f"--kubernetes-version={normalized}"], timeout=900)
    for index in range(1, len(steps)):
        _mark_step(steps, index, "completed", "Minikube cluster restarted with new version.")
    update_job(
        job_id,
        status="completed",
        message=f"Minikube upgraded to v{normalized}.",
        steps=steps,
        activeStep=len(steps) - 1,
        finishedAt=datetime.now(timezone.utc).isoformat(),
    )


def start_automated_upgrade(
    access: ClusterAccess,
    *,
    provider: str,
    target_version: str,
    current_version: str,
    run_kubectl: RunKubectlFn,
    precheck: Dict[str, Any],
) -> Dict[str, Any]:
    current_tuple = parse_k8s_version(current_version)
    target_tuple = parse_k8s_version(target_version)
    if current_tuple > target_tuple:
        raise ValueError(
            f"Target version {normalize_version(target_version)} must be newer than "
            f"current version {current_version}."
        )
    if provider == "kubeadm" and target_tuple[1] - current_tuple[1] > 1:
        raise ValueError(
            f"kubeadm supports upgrading one minor version at a time. "
            f"Upgrade {current_version} to v{current_tuple[0]}.{current_tuple[1] + 1}.x first, "
            f"not directly to {normalize_version(target_version)}."
        )

    if provider in {"docker-desktop", "kind"}:
        raise ValueError(f"Automatic upgrade is not supported for provider {provider}.")

    job = create_job(
        cluster_id=access.cluster_id,
        target_version=normalize_version(target_version),
        provider=provider,
        steps=_step_states(),
    )
    job_id = job["jobId"]

    if provider == "kubeadm":

        def _worker() -> None:
            _execute_kubeadm_upgrade(access, target_version, run_kubectl, job_id)

        run_job_async(job_id, _worker)
    elif provider == "minikube":

        def _worker() -> None:
            _execute_minikube_upgrade(target_version, job_id)

        run_job_async(job_id, _worker)
    else:
        raise ValueError(
            f"Automatic upgrade for {provider} requires provider-specific configuration "
            "(cluster name, resource group, etc.). Use kubeadm or minikube clusters."
        )

    return {
        **job,
        "upgradeId": job_id,
        "status": "running",
        "message": "Automated upgrade started.",
        "checks": precheck.get("checks", []),
        "provider": precheck.get("provider", {}),
        "upgradePlan": precheck.get("upgradePlan"),
        "executionSupported": True,
        "currentVersion": current_version,
    }
