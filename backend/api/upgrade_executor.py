"""Automated cluster upgrades for kubeadm and minikube.

A kubeadm run, in order:

0. Safety net — a workload baseline (ready pods) and, unless switched off, an
   etcd snapshot taken inside the first control plane's etcd pod.
1. The first control plane — drain (when there is somewhere for its pods to
   go), packages + ``kubeadm upgrade apply`` + kubelet in one on-node Job.
2. Every other control plane, one at a time — ``kubeadm upgrade node``.
3. Workers in waves of ``waveSize`` — cordon, drain, upgrade, uncordon, then
   wait until each node is Ready on the new kubelet. At most ``waveSize``
   workers are out of service at once.
4. Verify — API server and kubelet versions, ready pods against the baseline.

Each node's progress (``nodes[].phases``) and a run log (``log``) are written
to the job as they happen, so the live view can draw them. Between nodes the
run honours Pause / Stop (``control`` in the stored job, settable from any
process) and the end of its maintenance window (it pauses there).

A node already on the target is skipped, so starting the same upgrade again
after a stop or failure resumes where it left off.

The API server goes away during the run — ``kubeadm upgrade apply`` restarts
it, and with one control plane there is no other. Every kubectl call retries
through that (``_retry_transient``), and a step that still fails only because
the API was gone does not end the run: it waits for the API to answer and
continues from the first node not on the target (``AUTO_RESUME_ATTEMPTS``).
"""

from __future__ import annotations

import inspect
import json
import logging
import os
import re
import shlex
import subprocess
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional, Tuple

from .cluster_access import ClusterAccess
from .k8s_provider import K8sCommandError
from .upgrade_jobs import LOG_LIMIT, create_job, get_control, get_job, mutate_job, run_job_async, update_job
from .upgrade_provider import UPGRADE_PLAN_STEPS, normalize_version, parse_k8s_version

RunKubectlFn = Callable[..., str]

logger = logging.getLogger(__name__)

DEFAULT_DEBUG_NAMESPACE = "default"
POD_WAIT_TIMEOUT_SECONDS = 1800
POD_POLL_INTERVAL_SECONDS = 3
NODE_EXECUTOR_IMAGE = "debian:bookworm-slim"
NODE_READY_TIMEOUT_SECONDS = int(os.getenv("UPGRADE_NODE_READY_TIMEOUT_SECONDS", "600"))
PAUSE_POLL_SECONDS = 5
# How long one kubectl call keeps retrying a dropped connection to the API
# server (kubeadm restarts it; a node VM's network can blip) before the run
# gives up on it.
TRANSIENT_RETRY_SECONDS = int(os.getenv("UPGRADE_TRANSIENT_RETRY_SECONDS", "180"))
TRANSIENT_RETRY_INTERVAL_SECONDS = 5
# When a step still fails because the API server was gone, the run waits up to
# this long for it to answer, then carries on — at most this many times.
API_RECOVERY_SECONDS = int(os.getenv("UPGRADE_API_RECOVERY_SECONDS", "900"))
AUTO_RESUME_ATTEMPTS = int(os.getenv("UPGRADE_AUTO_RESUME_ATTEMPTS", "5"))
# Pre-staging downloads every node's packages and images at once before the
# first node is cordoned. It is best effort: a node that does not finish in time
# downloads what it needs during its own upgrade, as it would without it.
PRESTAGE_TIMEOUT_SECONDS = int(os.getenv("UPGRADE_PRESTAGE_TIMEOUT_SECONDS", "600"))
PRESTAGE_PARALLELISM = 10
# The on-node upgrade Job ends with `systemctl restart kubelet` on the node it
# runs on, and the new kubelet can end that Job's own pod: the node is upgraded
# but the Job reads as failed, which used to stop the run after every node. So
# the script leaves a marker once every step before the restart has passed, and
# a failed Job whose node holds the marker counts as done.
UPGRADE_MARK_DIR = "/var/lib/kubesight/upgrade"
_MARK_FOUND = "KS_UPGRADE_MARK_FOUND"

DEFAULT_OPTIONS: Dict[str, Any] = {
    "waveSize": 1,
    "drainTimeoutMinutes": 10,
    "etcdSnapshot": True,
    "pauseBetweenWaves": False,
}

NODE_PHASES = ["cordon", "drain", "upgrade", "uncordon", "ready"]

# Substrings that indicate the API server is temporarily unavailable (e.g. restarting
# during kubeadm upgrade apply). These are retried rather than treated as failures.
#
# The API server could not be reached at all: anything may be retried, drain too.
_CONNECTION_ERROR_MARKERS = (
    "connectex",
    "connection refused",
    "dial tcp",
    "unable to connect to the server",
    "no connection could be made",
    "no route to host",
    "connection reset by peer",
    "broken pipe",
    "use of closed network connection",
    "http2: client connection lost",
    "tls handshake timeout",
    # KubeSight's own breaker (k8s_provider): a recent call timed out, so calls
    # fail fast for a while. This is what a run saw right after the control
    # plane restarted, and it used to end the run.
    "cluster api is unreachable",
)
# It answered slowly or half-way (restarting, etcd leader change). Not retried
# for a drain: a drain that times out is usually a PodDisruptionBudget.
_SLOW_API_ERROR_MARKERS = (
    "i/o timeout",
    "eof",
    "context deadline exceeded",
    "client.timeout exceeded",
    "net/http: request canceled",
    "service unavailable",
    "the server is currently unable to handle the request",
    "the server was unable to return a response",
    "etcdserver:",
    "kubectl command timed out after",
)
_TRANSIENT_API_ERROR_MARKERS = _CONNECTION_ERROR_MARKERS + _SLOW_API_ERROR_MARKERS


def _is_transient_api_error(exc: Exception, *, connection_only: bool = False) -> bool:
    lower = str(exc).lower()
    markers = _CONNECTION_ERROR_MARKERS if connection_only else _TRANSIENT_API_ERROR_MARKERS
    return any(marker in lower for marker in markers)


def _retry_transient(call: Callable[[], str], *, connection_only: bool = False) -> str:
    """``call()``, retried while the API server is unreachable — up to
    ``TRANSIENT_RETRY_SECONDS``, then the last error is raised."""
    deadline = time.time() + TRANSIENT_RETRY_SECONDS
    while True:
        try:
            return call()
        except Exception as exc:
            if not _is_transient_api_error(exc, connection_only=connection_only) or time.time() >= deadline:
                raise
        time.sleep(TRANSIENT_RETRY_INTERVAL_SECONDS)


class NodeCommandFailed(RuntimeError):
    """The on-node upgrade Job ran and failed: retrying will not help."""


class DrainFailed(RuntimeError):
    """A drain did not finish (usually a PodDisruptionBudget)."""


def _resumable(exc: BaseException) -> bool:
    """Whether the run may pick itself up after ``exc``: only when the API
    server was gone, never after a node really failed."""
    seen: Optional[BaseException] = exc
    while seen is not None:
        if isinstance(seen, (NodeCommandFailed, DrainFailed, RunStopped)):
            return False
        seen = seen.__cause__
    return isinstance(exc, Exception) and _is_transient_api_error(exc)


def _kubectl_verb(args: List[str]) -> str:
    skip = False
    for arg in args:
        if skip:
            skip = False
            continue
        if arg in ("-n", "--namespace"):
            skip = True
            continue
        if not str(arg).startswith("-"):
            return str(arg)
    return ""


def _package_version(target_version: str) -> str:
    return normalize_version(target_version).lstrip("v")


def normalize_options(raw: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """Clamp a plan's run options to what the executor supports."""
    raw = raw or {}
    out = dict(DEFAULT_OPTIONS)
    try:
        out["waveSize"] = max(1, min(int(raw.get("waveSize", out["waveSize"])), 10))
    except (TypeError, ValueError):
        pass
    try:
        out["drainTimeoutMinutes"] = max(1, min(int(raw.get("drainTimeoutMinutes", out["drainTimeoutMinutes"])), 120))
    except (TypeError, ValueError):
        pass
    if "etcdSnapshot" in raw:
        out["etcdSnapshot"] = bool(raw.get("etcdSnapshot"))
    if "pauseBetweenWaves" in raw:
        out["pauseBetweenWaves"] = bool(raw.get("pauseBetweenWaves"))
    return out


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
      printf '%s\\n' "$apt_out"
      return 0
    fi
    printf '%s\\n' "$apt_out" >&2
    # Version-not-found and missing-package errors are permanent — retrying will
    # never help.  Fail immediately with a clear message.
    if printf '%s\\n' "$apt_out" | grep -qiE \\
        "version .+ (for .+ )?was not found|unable to locate package|no packages found|has no installation candidate"; then
      echo "ERROR: Package not available in the repository. Verify that the target" \\
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


_SATELLITE_KUBE_REPO_SCRIPT = r"""# Satellite publishes one repository per Kubernetes minor (k8s-packages-v1_34,
# ...) and the activation key enables whichever it was set up with. Turn on
# this minor's and the other minors' off, so dnf sees exactly this version.
if command -v subscription-manager >/dev/null 2>&1 && subscription-manager identity >/dev/null 2>&1; then
  KS_KUBE_REPOS=$(subscription-manager repos --list 2>/dev/null | awk '
    /^Repo ID:/ { id = $3 }
    /^Repo Name:/ { sub(/^Repo Name:[ \t]*/, ""); print id "\t" $0 }' \
    | grep -iE 'k8s|kube' | grep -E '[vV-][0-9]+[._][0-9]+([^0-9]|$)' || true)
  KS_KUBE_WANT=$(printf '%s\n' "$KS_KUBE_REPOS" | grep -E '@WANT@' | cut -f1 | head -n 1)
  if [ -n "$KS_KUBE_WANT" ]; then
    KS_KUBE_ARGS="--enable=$KS_KUBE_WANT"
    for KS_KUBE_OTHER in $(printf '%s\n' "$KS_KUBE_REPOS" | grep -vE '@WANT@' | cut -f1); do
      KS_KUBE_ARGS="$KS_KUBE_ARGS --disable=$KS_KUBE_OTHER"
    done
    if ! subscription-manager repos $KS_KUBE_ARGS; then
      echo "Could not switch this host to Satellite repository $KS_KUBE_WANT for Kubernetes @MINOR@." >&2
      exit 1
    fi
    echo "Kubernetes @MINOR@ packages come from Satellite repository $KS_KUBE_WANT."
  elif [ -n "$KS_KUBE_REPOS" ]; then
    echo "Satellite offers this host no Kubernetes @MINOR@ repository; it has:" $(printf '%s\n' "$KS_KUBE_REPOS" | cut -f1)
  fi
fi
"""


def satellite_kube_repo_script(k8s_version: str) -> str:
    """Shell that enables the Satellite repository for ``k8s_version``'s minor
    and disables the other minors' — a no-op on a host not registered with
    Satellite, or when Satellite has no repository named for that minor."""
    parts = k8s_version.lstrip("v").split(".")
    major, minor = parts[0], parts[1] if len(parts) > 1 else "0"
    return (
        _SATELLITE_KUBE_REPO_SCRIPT
        .replace("@WANT@", f"[vV-]{major}[._]{minor}([^0-9]|$)")
        .replace("@MINOR@", f"{major}.{minor}")
    )


def _shell_script_install_kubeadm_only(target_version: str) -> str:
    version = _package_version(target_version)
    return f"""set -e
K8S_VER="{version}"
{_APT_SHELL_PREAMBLE}
{satellite_kube_repo_script(version)}
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
{satellite_kube_repo_script(version)}
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


def _done_marker(job_id: str, node: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]", "-", f"{job_id}-{node}")


def _mark_done_script(done_marker: Optional[str]) -> str:
    """Shell that leaves ``done_marker`` on the node (before the kubelet restart)."""
    if not done_marker:
        return ""
    path = shlex.quote(f"{UPGRADE_MARK_DIR}/{done_marker}")
    return f"mkdir -p {shlex.quote(UPGRADE_MARK_DIR)}\necho \"$K8S_VER\" > {path}\n"


def _shell_script_control_plane_upgrade(target_version: str, done_marker: Optional[str] = None) -> str:
    """Single on-node script: packages, kubeadm apply, kubelet — one apt session."""
    normalized = normalize_version(target_version)
    version = _package_version(target_version)
    return f"""set -e
K8S_VER="{version}"
TARGET="{normalized}"
{_APT_SHELL_PREAMBLE}
{satellite_kube_repo_script(version)}
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
{_mark_done_script(done_marker)}systemctl daemon-reload
systemctl restart kubelet
"""


def _shell_script_upgrade_worker_node(target_version: str, done_marker: Optional[str] = None) -> str:
    """Packages + ``kubeadm upgrade node`` + kubelet: every node but the first
    control plane (``kubeadm upgrade node`` also upgrades a secondary control
    plane's static pods)."""
    version = _package_version(target_version)
    return f"""set -e
K8S_VER="{version}"
{_APT_SHELL_PREAMBLE}
{satellite_kube_repo_script(version)}
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
{_mark_done_script(done_marker)}systemctl daemon-reload
systemctl restart kubelet
"""


def _shell_script_prestage(target_version: str, images: List[str]) -> str:
    """Download the target's packages and pull its images without installing or
    restarting anything, so a node's cordoned window holds only the install."""
    version = _package_version(target_version)
    pulls = "\n".join(
        f"  crictl pull {shlex.quote(image)} >/dev/null 2>&1 && echo \"Pulled \"{shlex.quote(image)} || "
        f"echo \"Could not pre-pull \"{shlex.quote(image)}\"; the upgrade pulls it itself.\""
        for image in images
    )
    return f"""set -e
K8S_VER="{version}"
{_APT_SHELL_PREAMBLE}
{satellite_kube_repo_script(version)}
if command -v apt-get >/dev/null 2>&1; then
  _apt update -qq
  _apt install -y -qq --download-only --allow-change-held-packages \\
    kubeadm="${{K8S_VER}}-*" kubelet="${{K8S_VER}}-*" kubectl="${{K8S_VER}}-*"
elif command -v dnf >/dev/null 2>&1; then
  dnf install -y --downloadonly "kubeadm-${{K8S_VER}}" "kubelet-${{K8S_VER}}" "kubectl-${{K8S_VER}}" || \\
    dnf install -y --downloadonly "kubeadm-${{K8S_VER}}-*" "kubelet-${{K8S_VER}}-*" "kubectl-${{K8S_VER}}-*"
elif command -v yum >/dev/null 2>&1; then
  yum install -y --downloadonly "kubeadm-${{K8S_VER}}" "kubelet-${{K8S_VER}}" "kubectl-${{K8S_VER}}" || \\
    yum install -y --downloadonly "kubeadm-${{K8S_VER}}-*" "kubelet-${{K8S_VER}}-*" "kubectl-${{K8S_VER}}-*"
fi
echo "Packages for ${{K8S_VER}} downloaded."
if command -v crictl >/dev/null 2>&1; then
  :
{pulls}
fi
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


_TIMEOUT_AWARE: Dict[int, bool] = {}


def _accepts_timeout(run_kubectl: RunKubectlFn) -> bool:
    key = id(run_kubectl)
    if key not in _TIMEOUT_AWARE:
        try:
            _TIMEOUT_AWARE[key] = "timeout" in inspect.signature(run_kubectl).parameters
        except (TypeError, ValueError):
            _TIMEOUT_AWARE[key] = False
    return _TIMEOUT_AWARE[key]


def _run_upgrade_kubectl(
    access: ClusterAccess,
    run_kubectl: RunKubectlFn,
    args: List[str],
    *,
    timeout: Optional[int] = None,
) -> str:
    """Run kubectl without inheriting a kubeconfig default namespace (e.g. kubesight).

    ``timeout`` (seconds) is the process kill timeout; long verbs like
    ``drain`` must pass one, or the runner's 30 s default kills them first.

    A dropped connection is retried rather than ending the run: every verb
    the executor sends (get, cordon, drain, uncordon, logs, exec) is safe to
    repeat."""
    if "-n" not in args and "--namespace" not in args:
        args = ["--namespace", "default", *args]
    connection_only = _kubectl_verb(args) == "drain"
    if timeout and _accepts_timeout(run_kubectl):
        return _retry_transient(lambda: run_kubectl(access, args, timeout=timeout), connection_only=connection_only)
    return _retry_transient(lambda: run_kubectl(access, args), connection_only=connection_only)


def _kubectl_command(
    access: ClusterAccess, args: List[str], kubeconfig_path: Optional[str] = None
) -> List[str]:
    """``kubeconfig_path`` is the materialized (decrypted) path when given."""
    path = kubeconfig_path if kubeconfig_path is not None else access.kubeconfig_path
    command = ["kubectl"]
    if path:
        command += ["--kubeconfig", path]
    if access.context_name:
        command += ["--context", access.context_name]
    from .k8s_provider import _refuse_unsafe_kubectl_args

    _refuse_unsafe_kubectl_args(args)
    command += args
    return command


def _kubectl_apply_manifest(access: ClusterAccess, manifest: Dict[str, Any]) -> str:
    from .kubeconfig_vault import materialized_kubeconfig

    # The decrypted copy lives only for this one kubectl call.
    with materialized_kubeconfig(access.kubeconfig_path) as kubeconfig_path:
        env = os.environ.copy()
        if kubeconfig_path:
            env["KUBECONFIG"] = kubeconfig_path
        elif not env.get("KUBECONFIG") and env.get("K8S_KUBECONFIG"):
            env["KUBECONFIG"] = env["K8S_KUBECONFIG"]

        try:
            completed = subprocess.run(
                _kubectl_command(access, ["apply", "-f", "-"], kubeconfig_path or ""),
                input=json.dumps(manifest),
                capture_output=True,
                text=True,
                timeout=120,
                check=False,
                env=env,
            )
        except subprocess.TimeoutExpired as exc:
            raise RuntimeError("kubectl command timed out after 120s: apply") from exc
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
                ["get", "pods", "-n", DEFAULT_DEBUG_NAMESPACE, "-l", f"job-name={job_name}", "-o", "json"],
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
                        access, run_kubectl, ["logs", pod_name, "-n", DEFAULT_DEBUG_NAMESPACE]
                    )
                    raise NodeCommandFailed(logs[:1000] or f"Remote command failed on node (exit {exit_code}).")
            return _run_upgrade_kubectl(access, run_kubectl, ["logs", pod_name, "-n", DEFAULT_DEBUG_NAMESPACE])
        if status.get("failed"):
            pods_output = _run_upgrade_kubectl(
                access,
                run_kubectl,
                ["get", "pods", "-n", DEFAULT_DEBUG_NAMESPACE, "-l", f"job-name={job_name}", "-o", "json"],
            )
            items = json.loads(pods_output).get("items", [])
            pod_name = items[0].get("metadata", {}).get("name", job_name) if items else job_name
            try:
                logs = _run_upgrade_kubectl(access, run_kubectl, ["logs", pod_name, "-n", DEFAULT_DEBUG_NAMESPACE])
            except K8sCommandError:
                logs = ""
            raise NodeCommandFailed(logs[:1000] or f"Upgrade job {job_name} failed.")
        time.sleep(POD_POLL_INTERVAL_SECONDS)
    raise RuntimeError(f"Timed out waiting for upgrade job {job_name}.")


def _delete_upgrade_job(access: ClusterAccess, run_kubectl: RunKubectlFn, job_name: str) -> None:
    try:
        _run_upgrade_kubectl(
            access,
            run_kubectl,
            ["delete", "job", job_name, "-n", DEFAULT_DEBUG_NAMESPACE, "--ignore-not-found", "--wait=false"],
        )
    except Exception:  # noqa: BLE001 — cleanup must not hide the job's own result; its TTL removes it
        pass


def _run_on_node(
    access: ClusterAccess,
    run_kubectl: RunKubectlFn,
    node_name: str,
    shell_command: str,
    *,
    timeout_seconds: int = POD_WAIT_TIMEOUT_SECONDS,
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
                    # Control-plane nodes carry NoSchedule taints, and a drained node
                    # carries the unschedulable taint; tolerate them so the kubelet
                    # admits the Job pod there.
                    "tolerations": [
                        {"key": "node-role.kubernetes.io/control-plane", "operator": "Exists", "effect": "NoSchedule"},
                        {"key": "node-role.kubernetes.io/master", "operator": "Exists", "effect": "NoSchedule"},
                        {"key": "node.kubernetes.io/unschedulable", "operator": "Exists", "effect": "NoSchedule"},
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
        _retry_transient(lambda: _kubectl_apply_manifest(access, manifest))
        output = _wait_for_job_completion(access, run_kubectl, job_name, timeout_seconds=timeout_seconds)
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
        raise NodeCommandFailed(output[:1000] or f"Remote command failed on node {node_name}")
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
    completed = subprocess.run(command, capture_output=True, text=True, timeout=timeout, check=False)
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout or "").strip()
        raise RuntimeError(detail or f"Command failed: {' '.join(command)}")
    return (completed.stdout or "").strip()


def _worker_kubelet_version(worker_name: str, node_items: List[Dict[str, Any]]) -> str:
    node = next((n for n in node_items if n.get("metadata", {}).get("name") == worker_name), None)
    return (node or {}).get("status", {}).get("nodeInfo", {}).get("kubeletVersion") or "unknown"


# ---------------------------------------------------------------------------
# Run tracking: per-node phases, the log, pause / stop / window checkpoints
# ---------------------------------------------------------------------------

class RunStopped(Exception):
    """Stop was pressed; the run ends at a safe point."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _parse_iso(value: Any) -> Optional[datetime]:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


class RunTracker:
    """Writes what a run is doing into its job, a few times per node."""

    def __init__(self, job_id: str, *, window_end: Optional[str] = None, sleep: Callable[[float], None] = time.sleep):
        self.job_id = job_id
        self.window_end = _parse_iso(window_end)
        self._sleep = sleep

    # -- writing -------------------------------------------------------------
    def save(self, **fields: Any) -> None:
        update_job(self.job_id, **fields)

    def log(self, node: Optional[str], message: str) -> None:
        def _add(job: Dict[str, Any]) -> None:
            lines = job.setdefault("log", [])
            lines.append({"at": _now(), "node": node, "message": message})
            if len(lines) > LOG_LIMIT:
                del lines[: len(lines) - LOG_LIMIT]

        mutate_job(self.job_id, _add)

    def set_nodes(self, nodes: List[Dict[str, Any]]) -> None:
        update_job(self.job_id, nodes=nodes)

    def node(self, name: str, *, phase: Optional[str] = None, status: Optional[str] = None,
             state: Optional[str] = None, message: Optional[str] = None, **fields: Any) -> None:
        """``phase`` + ``status``: one phase's state (pending/active/done/failed/
        skipped). ``state`` (or ``status`` without a phase): the node's overall state."""
        def _change(job: Dict[str, Any]) -> None:
            for node in job.get("nodes") or []:
                if node.get("name") != name:
                    continue
                overall = state
                if phase:
                    node.setdefault("phases", {})[phase] = status or "active"
                    if (status or "active") == "active":
                        overall = overall or "active"
                elif status:
                    overall = overall or status
                if overall:
                    node["status"] = overall
                    if overall == "active" and not node.get("startedAt"):
                        node["startedAt"] = _now()
                if message is not None:
                    node["message"] = message
                node.update(fields)
                break

        mutate_job(self.job_id, _change)

    def stage(self, key: str, status: str, message: str = "") -> None:
        def _change(job: Dict[str, Any]) -> None:
            for stage in job.get("stages") or []:
                if stage.get("key") == key:
                    stage["status"] = status
                    if message:
                        stage["message"] = message
                    if status == "active" and not stage.get("startedAt"):
                        stage["startedAt"] = _now()
                    if status in ("done", "failed", "skipped"):
                        stage["finishedAt"] = _now()
            job["currentStage"] = key if status == "active" else job.get("currentStage")

        mutate_job(self.job_id, _change)

    # -- checkpoints ----------------------------------------------------------
    def checkpoint(self, where: str, *, force_pause: bool = False) -> None:
        """Between nodes: honour Stop, Pause and the end of the window."""
        control = get_control(self.job_id)
        if control.get("stop"):
            raise RunStopped(f"Stopped before {where}.")
        reason = None
        if control.get("pause"):
            reason = f"Paused before {where}."
        elif force_pause:
            reason = f"Paused before {where}: waiting for a go-ahead after the wave."
            from .upgrade_jobs import change_stored

            change_stored(self.job_id, lambda job: job.setdefault("control", {}).update(pause=True))
        elif self.window_end and datetime.now(timezone.utc) >= self.window_end:
            reason = f"The maintenance window ended; paused before {where}."
            from .upgrade_jobs import change_stored

            change_stored(self.job_id, lambda job: job.setdefault("control", {}).update(pause=True))
        if not reason:
            return
        self.save(status="paused", message=reason, pausedAt=_now())
        self.log(None, reason)
        while True:
            self._sleep(PAUSE_POLL_SECONDS)
            control = get_control(self.job_id)
            if control.get("stop"):
                raise RunStopped(f"Stopped while paused before {where}.")
            if not control.get("pause"):
                break
        self.save(status="running", message="Upgrade in progress.", pausedAt=None)
        self.log(None, "Resumed.")


def initial_nodes(node_items: List[Dict[str, Any]], control_planes: List[str], workers: List[str],
                  wave_size: int, pods_by_node: Optional[Dict[str, int]] = None) -> List[Dict[str, Any]]:
    """The run's node list in execution order, each with its wave and phases."""
    pods_by_node = pods_by_node or {}
    versions = {n.get("metadata", {}).get("name"): _worker_kubelet_version(n.get("metadata", {}).get("name"), node_items)
                for n in node_items}
    rows = []
    for index, name in enumerate(control_planes):
        rows.append({
            "name": name,
            "role": "control-plane",
            "order": index,
            "first": index == 0,
            "wave": None,
            "pods": pods_by_node.get(name, 0),
            "versionBefore": versions.get(name),
            "versionAfter": None,
            "phases": {phase: "pending" for phase in NODE_PHASES},
            "status": "pending",
            "message": "",
        })
    for index, name in enumerate(workers):
        rows.append({
            "name": name,
            "role": "worker",
            "order": len(control_planes) + index,
            "first": False,
            "wave": index // wave_size + 1,
            "pods": pods_by_node.get(name, 0),
            "versionBefore": versions.get(name),
            "versionAfter": None,
            "phases": {phase: "pending" for phase in NODE_PHASES},
            "status": "pending",
            "message": "",
        })
    return rows


def initial_stages(options: Dict[str, Any], control_planes: List[str], workers: List[str]) -> List[Dict[str, Any]]:
    waves = (len(workers) + options["waveSize"] - 1) // options["waveSize"] if workers else 0
    return [
        {"key": "safety", "label": "Safety net", "status": "pending",
         "message": "etcd snapshot and workload baseline" if options["etcdSnapshot"] else "Workload baseline"},
        {"key": "control-plane", "label": "Control plane", "status": "pending",
         "message": f"{len(control_planes)} control plane(s), one at a time"},
        {"key": "workers", "label": "Workers", "status": "pending" if workers else "skipped",
         "message": f"{len(workers)} worker(s) in {waves} wave(s) of {options['waveSize']}" if workers else "No workers"},
        {"key": "verify", "label": "Verify", "status": "pending", "message": "Versions and workloads against the baseline"},
    ]


# ---------------------------------------------------------------------------
# kubeadm
# ---------------------------------------------------------------------------

def _get_json(access: ClusterAccess, run_kubectl: RunKubectlFn, args: List[str]) -> Dict[str, Any]:
    return json.loads(_run_upgrade_kubectl(access, run_kubectl, args))


def _pods_snapshot(access: ClusterAccess, run_kubectl: RunKubectlFn) -> Optional[Dict[str, Any]]:
    from .upgrade_checks import workload_baseline

    try:
        pods = _get_json(access, run_kubectl, ["get", "pods", "--all-namespaces", "-o", "json"]).get("items", [])
    except Exception:  # noqa: BLE001 — a baseline is a nicety, not a gate
        return None
    baseline = workload_baseline(pods)
    baseline["at"] = _now()
    return baseline


def _etcd_snapshot(access: ClusterAccess, run_kubectl: RunKubectlFn, node: str) -> Dict[str, Any]:
    """``etcdctl snapshot save`` inside the node's etcd static pod. The file lands
    in the pod's data directory, i.e. ``/var/lib/etcd`` on the node."""
    pods = _get_json(access, run_kubectl, ["get", "pods", "-n", "kube-system", "-l", "component=etcd", "-o", "json"])
    pod = next(
        (p for p in pods.get("items", []) if p.get("spec", {}).get("nodeName") == node),
        (pods.get("items") or [None])[0],
    )
    if not pod:
        return {"status": "skipped", "message": "No etcd pod in kube-system (external etcd?): take a snapshot yourself."}
    pod_name = pod["metadata"]["name"]
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    path = f"/var/lib/etcd/kubesight-pre-upgrade-{stamp}.db"
    _run_upgrade_kubectl(
        access,
        run_kubectl,
        [
            "exec", "-n", "kube-system", pod_name, "--",
            "etcdctl",
            "--endpoints=https://127.0.0.1:2379",
            "--cacert=/etc/kubernetes/pki/etcd/ca.crt",
            "--cert=/etc/kubernetes/pki/etcd/server.crt",
            "--key=/etc/kubernetes/pki/etcd/server.key",
            "snapshot", "save", path,
        ],
        timeout=300,
    )
    return {
        "status": "done",
        "node": pod.get("spec", {}).get("nodeName") or node,
        "path": path,
        "message": f"Saved to {path} on {pod.get('spec', {}).get('nodeName') or node}.",
        "restore": (
            "On the control plane: stop the kube-apiserver and etcd static pods, then "
            f"etcdutl snapshot restore {path} --data-dir /var/lib/etcd-restore and point "
            "the etcd manifest at the restored data dir."
        ),
    }


_NOT_SATELLITE_MARK = "KS_NOT_SATELLITE"


def _capsule_target(access: ClusterAccess, run_kubectl: RunKubectlFn, tracker: "RunTracker",
                    node: str, target: str) -> str:
    """The version to install: ``target``, unless ``node`` is registered with
    Satellite and its capsule lacks it — then the newest patch of the same minor
    the capsule carries (v1.35.7 asked, v1.35.4 on the capsule: v1.35.4).
    Anything else keeps ``target``, and the install says what is missing."""
    version = _package_version(target)
    minor = ".".join(version.split(".")[:2])
    script = f"""if ! command -v subscription-manager >/dev/null 2>&1 || ! subscription-manager identity >/dev/null 2>&1; then
  echo {_NOT_SATELLITE_MARK}
  exit 0
fi
{satellite_kube_repo_script(version)}
dnf -q --showduplicates --disableexcludes=all list kubeadm kubelet kubectl 2>/dev/null || true
"""
    try:
        output = _run_on_node(access, run_kubectl, node, script, timeout_seconds=PRESTAGE_TIMEOUT_SECONDS)
    except Exception as exc:  # noqa: BLE001 — the install itself reports a missing version
        tracker.log(node, f"Could not list the Kubernetes versions on the Satellite capsule; installing {target}. "
                          f"({str(exc)[:200]})")
        return target
    if _NOT_SATELLITE_MARK in output:
        return target
    from .services.cluster_build import k8s_versions

    on_capsule = k8s_versions.capsule_versions(output)
    chosen = k8s_versions.newest_patch_of(on_capsule, version)
    if chosen is None:
        carried = ", ".join(on_capsule[:6]) or "no kubeadm/kubelet/kubectl"
        tracker.log(node, f"The Satellite capsule carries no Kubernetes {minor} ({carried}); sync it into "
                          "Satellite, or the install stops on the missing packages.")
        return target
    chosen = normalize_version(chosen)
    if chosen == target:
        tracker.log(node, f"The Satellite capsule carries {target}.")
    else:
        tracker.log(node, f"Installing {chosen}: the newest {minor} patch the Satellite capsule carries "
                          f"({target} is not there).")
    return chosen


_RELEASE_TAG_RE = re.compile(r"^v\d+\.\d+\.\d+$")


def _prestage_images(access: ClusterAccess, run_kubectl: RunKubectlFn, target: str) -> Tuple[List[str], List[str]]:
    """(control-plane images, every node's images) at ``target``, from the
    registry the API server runs from now. kubeadm keeps etcd's and CoreDNS's
    tags in its own tables, so those are left for ``upgrade apply`` to pull."""
    if not _RELEASE_TAG_RE.match(target):
        return [], []
    pods = _get_json(access, run_kubectl, ["get", "pods", "-n", "kube-system", "-l", "component=kube-apiserver", "-o", "json"])
    containers = next(iter(pods.get("items") or []), {}).get("spec", {}).get("containers") or []
    image = (containers[0].get("image") if containers else "") or ""
    name = image.split("@", 1)[0]
    if ":" in name.rsplit("/", 1)[-1]:
        name = name.rsplit(":", 1)[0]
    if "/" not in name:
        return [], []
    registry = name.rsplit("/", 1)[0]
    proxy = [f"{registry}/kube-proxy:{target}"]
    control_plane = [
        f"{registry}/{component}:{target}"
        for component in ("kube-apiserver", "kube-controller-manager", "kube-scheduler")
    ]
    return control_plane + proxy, proxy


def _prestage(access: ClusterAccess, run_kubectl: RunKubectlFn, tracker: "RunTracker",
              nodes: List[Tuple[str, bool]], target: str) -> None:
    """Every node at once: download the target's packages and pull its images.
    ``nodes`` holds (name, is_control_plane). Never fails the run."""
    if not nodes:
        return
    try:
        cp_images, node_images = _prestage_images(access, run_kubectl, target)
    except Exception as exc:  # noqa: BLE001 — packages are still worth fetching
        cp_images, node_images = [], []
        tracker.log(None, f"Images not pre-pulled: could not read the API server's image ({str(exc)[:200]}).")
    tracker.log(None, f"Pre-staging {target} packages and images on {len(nodes)} node(s) before the first is cordoned.")

    def _one(name: str, is_cp: bool) -> None:
        script = _shell_script_prestage(target, cp_images if is_cp else node_images)
        try:
            _run_on_node(access, run_kubectl, name, script, timeout_seconds=PRESTAGE_TIMEOUT_SECONDS)
            tracker.log(name, "Packages downloaded and images pre-pulled.")
        except Exception as exc:  # noqa: BLE001 — its own upgrade downloads them
            tracker.log(name, f"Pre-staging did not finish; the upgrade downloads what it needs itself. "
                              f"({str(exc)[:200]})")

    with ThreadPoolExecutor(max_workers=PRESTAGE_PARALLELISM) as pool:
        list(pool.map(lambda node: _one(*node), nodes))


def _upgraded_despite_job(access: ClusterAccess, run_kubectl: RunKubectlFn, node: str, done_marker: str) -> bool:
    """Whether ``node`` holds the marker its upgrade script leaves once every
    step passed — read (and removed) by a fresh Job once the kubelet is back."""
    path = shlex.quote(f"{UPGRADE_MARK_DIR}/{done_marker}")
    script = f"if [ -f {path} ]; then rm -f {path}; echo {_MARK_FOUND}; fi"
    try:
        output = _run_on_node(access, run_kubectl, node, script, timeout_seconds=NODE_READY_TIMEOUT_SECONDS)
    except Exception:  # noqa: BLE001 — no answer: the Job's own failure stands
        logger.debug("Could not read the upgrade marker on %s", node, exc_info=True)
        return False
    return _MARK_FOUND in output


def _wait_node_ready(access: ClusterAccess, run_kubectl: RunKubectlFn, node: str, target: str) -> str:
    deadline = time.time() + NODE_READY_TIMEOUT_SECONDS
    last = "unknown"
    while time.time() < deadline:
        try:
            item = _get_json(access, run_kubectl, ["get", "node", node, "-o", "json"])
            last = item.get("status", {}).get("nodeInfo", {}).get("kubeletVersion") or "unknown"
            ready = any(
                c.get("type") == "Ready" and c.get("status") == "True"
                for c in item.get("status", {}).get("conditions", []) or []
            )
            if ready and parse_k8s_version(last)[:2] >= parse_k8s_version(target)[:2]:
                return last
        except Exception as exc:  # noqa: BLE001
            if not _is_transient_api_error(exc):
                raise
        time.sleep(POD_POLL_INTERVAL_SECONDS)
    raise RuntimeError(f"{node} did not come back Ready on {normalize_version(target)} within "
                       f"{NODE_READY_TIMEOUT_SECONDS // 60} min (kubelet reports {last}).")


def _drain(access: ClusterAccess, run_kubectl: RunKubectlFn, node: str, minutes: int) -> None:
    _run_upgrade_kubectl(
        access,
        run_kubectl,
        ["drain", node, "--ignore-daemonsets", "--delete-emptydir-data", f"--timeout={minutes}m"],
        timeout=minutes * 60 + 60,
    )


def _wait_for_api(access: ClusterAccess, run_kubectl: RunKubectlFn, job_id: str) -> bool:
    """Until the API server answers. False when Stop was pressed meanwhile."""
    deadline = time.time() + API_RECOVERY_SECONDS
    while True:
        if get_control(job_id).get("stop"):
            return False
        try:
            _read_server_version(access, run_kubectl)
            return True
        except Exception as exc:  # noqa: BLE001
            if not _is_transient_api_error(exc) or time.time() >= deadline:
                raise RuntimeError(
                    f"The API server did not answer for {API_RECOVERY_SECONDS // 60} min after the upgrade "
                    f"lost it, so the run stopped. Start the upgrade again once the cluster answers: nodes "
                    f"already on the target are skipped. Detail: {str(exc)[:300]}"
                ) from exc
        time.sleep(TRANSIENT_RETRY_INTERVAL_SECONDS)


def _left_cordoned_by_earlier_run(job_id: str, cluster_id: str) -> set:
    """Nodes the cluster's previous upgrade run cordoned and never uncordoned —
    only when that run failed or was stopped. A node a person cordoned is not
    in it, so it is left alone."""
    from .upgrade_jobs import _with_app, list_jobs

    def _previous() -> Optional[Dict[str, Any]]:
        for job in list_jobs(cluster_ids=[cluster_id], statuses=("completed", "failed", "stopped"), limit=5):
            if job.get("jobId") != job_id:
                return job
        return None

    try:
        previous = _with_app(job_id, _previous)
    except Exception:  # noqa: BLE001 — a nicety; the verify step still names cordoned nodes
        logger.debug("Could not read the previous upgrade run", exc_info=True)
        return set()
    if not previous or previous.get("status") not in ("failed", "stopped"):
        return set()
    names = set(previous.get("cordonedNodes") or [])
    for node in previous.get("nodes") or []:
        phases = node.get("phases") or {}
        if phases.get("cordon") == "done" and phases.get("uncordon") != "done":
            names.add(node.get("name"))
    return names


def _execute_kubeadm_upgrade(
    access: ClusterAccess,
    target_version: str,
    run_kubectl: RunKubectlFn,
    job_id: str,
    *,
    options: Optional[Dict[str, Any]] = None,
    window_end: Optional[str] = None,
) -> None:
    """The kubeadm run. When an attempt ends only because the API server was
    gone, it waits for the API and runs again: nodes already on the target are
    skipped, so it carries on with the next one (the CP done, the worker next)."""
    options = normalize_options(options)
    tracker = RunTracker(job_id, window_end=window_end)
    cordoned: List[str] = []
    attempt = 0
    while True:
        try:
            _kubeadm_attempt(access, target_version, run_kubectl, job_id, tracker, options,
                             cordoned=cordoned, resume=attempt > 0)
            return
        except Exception as exc:
            if attempt >= AUTO_RESUME_ATTEMPTS or not _resumable(exc):
                raise
            attempt += 1
            tracker.log(None, f"Lost the API server: {str(exc)[:200]}. Continuing automatically once it "
                              f"answers (attempt {attempt + 1} of {AUTO_RESUME_ATTEMPTS + 1}).")
            tracker.save(status="running", message="Waiting for the API server to come back, then continuing.")
        if not _wait_for_api(access, run_kubectl, job_id):
            tracker.log(None, "Stopped while waiting for the API server.")
            update_job(job_id, status="stopped", cordonedNodes=list(cordoned), finishedAt=_now(),
                       message="Stopped while waiting for the API server. Nodes already done keep the new "
                               "version; check cordoned nodes before starting again.")
            return
        tracker.log(None, "API server is back: continuing with the nodes not on the target yet.")


def _kubeadm_attempt(
    access: ClusterAccess,
    target_version: str,
    run_kubectl: RunKubectlFn,
    job_id: str,
    tracker: RunTracker,
    options: Dict[str, Any],
    *,
    cordoned: List[str],
    resume: bool,
) -> None:
    """One pass over the cluster. ``resume``: a pass after the API server came
    back — the first pass's baseline, snapshot, target and finished nodes are kept."""
    normalized = normalize_version(target_version)
    prior = (get_job(job_id) or {}) if resume else {}
    # A first pass that lost the API before it wrote its plan left nothing to keep.
    resume = resume and bool(prior.get("nodes"))
    if resume and prior.get("requestedVersion") and prior.get("targetVersion"):
        normalized = normalize_version(prior["targetVersion"])  # the capsule's patch, chosen by the first pass
    target_tuple = parse_k8s_version(normalized)
    steps = prior.get("steps") or _step_states()

    node_items = _get_json(access, run_kubectl, ["get", "nodes", "-o", "json"]).get("items", [])
    control_planes = _control_plane_nodes(node_items)
    workers = _worker_nodes(node_items, control_planes)
    if not control_planes:
        raise RuntimeError("No control plane node found for kubeadm upgrade.")
    server_now = _read_server_version(access, run_kubectl)

    def at_target(name: str) -> bool:
        return parse_k8s_version(_worker_kubelet_version(name, node_items)) >= target_tuple

    prior_nodes = {n.get("name"): n for n in prior.get("nodes") or []}
    if resume:
        previous_version = prior.get("versionBefore") or server_now
        baseline = prior.get("baseline")
        nodes = initial_nodes(node_items, control_planes, workers, options["waveSize"],
                              (baseline or {}).get("podsByNode"))
        for index, row in enumerate(nodes):
            old = prior_nodes.get(row["name"])
            if old and old.get("status") == "done":
                nodes[index] = old
            elif old and old.get("versionBefore"):
                row["versionBefore"] = old["versionBefore"]
        tracker.save(nodes=nodes, steps=steps, error=None)
        # A node upgraded just before the API dropped may still be cordoned.
        for name in list(cordoned):
            if at_target(name):
                _run_upgrade_kubectl(access, run_kubectl, ["uncordon", name])
                cordoned.remove(name)
                tracker.log(name, f"uncordon node/{name} (it was upgraded before the API server dropped)")
    else:
        previous_version = server_now
        baseline = _pods_snapshot(access, run_kubectl)
        nodes = initial_nodes(node_items, control_planes, workers, options["waveSize"],
                              (baseline or {}).get("podsByNode"))
        tracker.save(
            steps=steps, activeStep=0, options=options, nodes=nodes,
            stages=initial_stages(options, control_planes, workers),
            baseline=baseline, versionBefore=previous_version, startedAt=_now(),
        )
        tracker.log(None, f"Upgrading {access.cluster_id} from {previous_version} to {normalized}.")
        capsule_version = _capsule_target(access, run_kubectl, tracker, control_planes[0], normalized)
        if capsule_version != normalized:
            tracker.save(targetVersion=capsule_version, requestedVersion=normalized)
            normalized = capsule_version
            target_tuple = parse_k8s_version(normalized)
        # An earlier run that ended badly may have left a node it had already
        # upgraded cordoned. Skipping it as "on the target" would leave it so.
        left = _left_cordoned_by_earlier_run(job_id, access.cluster_id)
        for item in node_items:
            name = item.get("metadata", {}).get("name") or ""
            if name in left and item.get("spec", {}).get("unschedulable") and at_target(name):
                _run_upgrade_kubectl(access, run_kubectl, ["uncordon", name])
                item.setdefault("spec", {})["unschedulable"] = False
                tracker.log(name, f"uncordon node/{name} (an earlier run upgraded it and left it cordoned)")

    # -- 0. safety net --------------------------------------------------------
    tracker.stage("safety", "active")
    _mark_step(steps, 0, "completed", "Cluster readiness validated.")
    _mark_step(steps, 1, "completed", f"Target version {normalized} validated.")
    cp_done = parse_k8s_version(server_now) >= target_tuple
    if (prior.get("snapshot") or {}).get("status") == "done":
        pass  # taken by the first pass
    elif options["etcdSnapshot"] and not cp_done:
        tracker.log(control_planes[0], "Taking an etcd snapshot.")
        try:
            snapshot = _etcd_snapshot(access, run_kubectl, control_planes[0])
        except Exception as exc:  # noqa: BLE001
            tracker.save(snapshot={"status": "failed", "message": str(exc)[:500]})
            tracker.stage("safety", "failed", f"etcd snapshot failed: {str(exc)[:200]}")
            raise RuntimeError(
                f"etcd snapshot failed, so nothing was changed: {str(exc)[:400]} "
                "Fix it, or plan again with the snapshot switched off."
            ) from exc
        tracker.save(snapshot=snapshot)
        tracker.log(control_planes[0], snapshot["message"])
    elif cp_done:
        tracker.save(snapshot={"status": "skipped", "message": "Control plane already on the target (resuming)."})
    else:
        tracker.save(snapshot={"status": "skipped", "message": "Switched off in the plan."})
    if baseline and not resume:
        tracker.log(None, f"Baseline: {baseline['readyPods']} ready pods.")
    tracker.stage("safety", "done", "Snapshot and baseline taken." if options["etcdSnapshot"] else "Baseline taken.")
    tracker.save(steps=steps, activeStep=1)

    schedulable_elsewhere = len(node_items) > 1
    # The API server is on the target: from here a failure leaves a cluster to
    # finish (re-run the same target), not one to roll back.
    control_plane_upgraded = cp_done

    def upgrade_node(name: str, script: str, *, drain: bool) -> None:
        """cordon → drain → on-node upgrade → uncordon → Ready. A drain timeout
        uncordons the node (nothing changed on it); a failed upgrade leaves it
        cordoned for a person to look at."""
        tracker.node(name, status="active")
        tracker.node(name, phase="cordon", status="active", message="Cordoning")
        _run_upgrade_kubectl(access, run_kubectl, ["cordon", name])
        if name not in cordoned:
            cordoned.append(name)
        tracker.node(name, phase="cordon", status="done")
        tracker.log(name, f"cordon node/{name}")
        if drain:
            pods = next((n.get("pods") for n in nodes if n["name"] == name), 0)
            tracker.node(name, phase="drain", status="active",
                         message=f"Draining {pods} pod(s), up to {options['drainTimeoutMinutes']} min")
            tracker.log(name, f"drain: evicting pods, timeout {options['drainTimeoutMinutes']}m")
            try:
                _drain(access, run_kubectl, name, options["drainTimeoutMinutes"])
            except Exception as exc:
                try:
                    _run_upgrade_kubectl(access, run_kubectl, ["uncordon", name])
                    cordoned.remove(name)
                except Exception:  # noqa: BLE001
                    pass
                tracker.node(name, phase="drain", status="failed", state="failed",
                             message="Drain did not finish; uncordoned, nothing changed on the node.")
                tracker.log(name, f"drain failed: {str(exc)[:300]}")
                raise DrainFailed(
                    f"Draining {name} did not finish within {options['drainTimeoutMinutes']} min, so the run "
                    f"stopped. {name} was uncordoned and nothing changed on it. Usually a PodDisruptionBudget "
                    f"allows no evictions — re-run the readiness checks. Detail: {str(exc)[:300]}"
                ) from exc
            tracker.node(name, phase="drain", status="done")
            tracker.log(name, "drain complete")
        else:
            tracker.node(name, phase="drain", status="skipped")
            tracker.log(name, "drain skipped: no other node for its pods")
        tracker.node(name, phase="upgrade", status="active", message="Upgrading packages and kubeadm")
        tracker.log(name, "on-node upgrade job started")
        try:
            _run_on_node(access, run_kubectl, name, script)
        except Exception as exc:
            # An API outage is the auto-resume's to handle; anything else may
            # only be the kubelet restart ending the Job's own pod.
            if _resumable(exc) or not _upgraded_despite_job(access, run_kubectl, name, _done_marker(job_id, name)):
                tracker.node(name, phase="upgrade", status="failed", state="failed",
                             message="Upgrade failed on the node; left cordoned for inspection.")
                tracker.log(name, f"upgrade failed: {str(exc)[:300]}")
                raise
            tracker.log(name, f"The upgrade job reported a failure ({str(exc)[:200]}), but the node finished "
                              "every step before the kubelet restart: carrying on.")
        tracker.node(name, phase="upgrade", status="done")
        tracker.log(name, "kubeadm and kubelet upgraded")
        tracker.node(name, phase="uncordon", status="active", message="Uncordoning")
        _run_upgrade_kubectl(access, run_kubectl, ["uncordon", name])
        if name in cordoned:
            cordoned.remove(name)
        tracker.node(name, phase="uncordon", status="done")
        tracker.node(name, phase="ready", status="active", message="Waiting for Ready")
        version = _wait_node_ready(access, run_kubectl, name, normalized)
        tracker.node(name, phase="ready", status="done", state="done", versionAfter=version,
                     message=f"Ready on {version}", finishedAt=_now())
        tracker.log(name, f"Ready · kubelet {version}")

    def skip_node(name: str) -> None:
        version = _worker_kubelet_version(name, node_items)
        old = prior_nodes.get(name) or {}
        if old.get("status") == "done":
            return
        if old.get("status") in ("active", "failed"):
            # The first pass upgraded it, then lost the API before seeing it Ready.
            for phase in NODE_PHASES:
                tracker.node(name, phase=phase, status="done")
            tracker.node(name, state="done", versionAfter=version, finishedAt=_now(),
                         message=f"On {version} (confirmed after the API server came back)")
            return
        tracker.node(name, state="skipped", message="Already on the target", versionAfter=version)
        for phase in NODE_PHASES:
            tracker.node(name, phase=phase, status="skipped")

    try:
        # -- 1. pre-stage: downloads off every node's cordoned window ------------
        tracker.checkpoint("pre-staging")
        if not resume:  # the first pass already downloaded what it could
            _prestage(
                access, run_kubectl, tracker,
                [(cp, True) for index, cp in enumerate(control_planes)
                 if not (at_target(cp) and (index > 0 or cp_done))]
                + [(w, False) for w in workers if not at_target(w)],
                normalized,
            )

        # -- 2 + 3. control planes ---------------------------------------------
        tracker.stage("control-plane", "active")
        for index, cp in enumerate(control_planes):
            if at_target(cp) and (index > 0 or cp_done):
                skip_node(cp)
                continue
            tracker.checkpoint(cp)
            if index == 0 and not cp_done:
                upgrade_node(cp, _shell_script_control_plane_upgrade(normalized, _done_marker(job_id, cp)),
                             drain=schedulable_elsewhere)
                server_version = _verify_server_version(access, run_kubectl, normalized, previous_version=previous_version)
                control_plane_upgraded = True
                tracker.log(cp, f"API server reports {server_version}")
            else:
                upgrade_node(cp, _shell_script_upgrade_worker_node(normalized, _done_marker(job_id, cp)),
                             drain=schedulable_elsewhere)
        tracker.stage("control-plane", "done", f"{len(control_planes)} control plane(s) on {normalized}.")
        _mark_step(steps, 2, "completed", "Control planes upgraded one at a time.")
        _mark_step(steps, 3, "completed", f"Control plane on {normalized}.")
        tracker.save(steps=steps, activeStep=3)

        # -- 4. workers in waves ----------------------------------------------------
        if workers:
            tracker.stage("workers", "active")
            pending = [w for w in workers if not at_target(w)]
            for w in workers:
                if w not in pending:
                    skip_node(w)
            size = options["waveSize"]
            waves = [pending[i:i + size] for i in range(0, len(pending), size)]
            for number, wave in enumerate(waves, start=1):
                tracker.checkpoint(f"wave {number} ({', '.join(wave)})",
                                   force_pause=options["pauseBetweenWaves"] and number > 1)
                tracker.log(None, f"Wave {number} of {len(waves)}: {', '.join(wave)}")
                if len(wave) == 1:
                    upgrade_node(wave[0], _shell_script_upgrade_worker_node(normalized, _done_marker(job_id, wave[0])),
                                 drain=True)
                    continue
                errors: List[BaseException] = []

                def _one(name: str) -> None:
                    try:
                        upgrade_node(name, _shell_script_upgrade_worker_node(normalized, _done_marker(job_id, name)), drain=True)
                    except BaseException as exc:  # noqa: BLE001 — collected and re-raised below
                        errors.append(exc)

                threads = [threading.Thread(target=_one, args=(n,), daemon=True) for n in wave]
                for thread in threads:
                    thread.start()
                for thread in threads:
                    thread.join()
                if errors:
                    raise errors[0]
            tracker.stage("workers", "done", f"{len(pending)} worker(s) upgraded in {len(waves)} wave(s).")
        _mark_step(steps, 4, "completed", f"Upgraded {len(workers)} worker node(s) in waves of {options['waveSize']}.")
        tracker.save(steps=steps, activeStep=4)
    except RunStopped as stop:
        for name in list(cordoned):
            try:
                _run_upgrade_kubectl(access, run_kubectl, ["uncordon", name])
            except Exception:  # noqa: BLE001
                pass
        tracker.log(None, f"{stop} Nodes already done stay on {normalized}; the rest stay on their version.")
        update_job(job_id, status="stopped", message=str(stop) + " Nodes already done keep the new version.",
                   finishedAt=_now())
        return
    except Exception as exc:
        message = str(exc)
        if 'namespaces "kubesight" not found' in message:
            message = (
                f"{message} The cluster kubeconfig may set a default namespace (e.g. kubesight) "
                "that does not exist on this cluster."
            )
        if not control_plane_upgraded:
            # The failure may have come after kubeadm apply but before the
            # check: one direct look, no retries, so the failure is not delayed.
            try:
                version_output = run_kubectl(access, ["--namespace", "default", "version", "-o", "json"])
                server_now = json.loads(version_output).get("serverVersion", {}).get("gitVersion") or ""
                control_plane_upgraded = parse_k8s_version(server_now) >= target_tuple
            except Exception:  # noqa: BLE001
                pass
        if control_plane_upgraded:
            message = (
                f"{message} The control plane is already on {normalized}: start the upgrade to "
                f"{normalized} again to finish the remaining workers (nodes already on it are skipped)."
            )
        update_job(job_id, cordonedNodes=list(cordoned))
        raise RuntimeError(message) from exc

    # -- 4. verify ---------------------------------------------------------------
    tracker.stage("verify", "active")
    final_version = _read_server_version(access, run_kubectl)
    after = _pods_snapshot(access, run_kubectl)
    try:
        final_nodes = _get_json(access, run_kubectl, ["get", "nodes", "-o", "json"]).get("items", [])
    except Exception:  # noqa: BLE001
        final_nodes = []
    behind = [
        n.get("metadata", {}).get("name") for n in final_nodes
        if parse_k8s_version(n.get("status", {}).get("nodeInfo", {}).get("kubeletVersion") or "")[:2] < target_tuple[:2]
    ]
    verdict = "clean"
    notes = []
    if behind:
        verdict = "warning"
        notes.append(f"Still behind: {', '.join(behind)}.")
    still_cordoned = [n.get("metadata", {}).get("name") for n in final_nodes if n.get("spec", {}).get("unschedulable")]
    if still_cordoned:
        verdict = "warning"
        notes.append(f"Still cordoned: {', '.join(still_cordoned)} (kubectl uncordon when it should take pods).")
    if baseline and after and after["readyPods"] < baseline["readyPods"]:
        verdict = "warning"
        notes.append(f"{after['readyPods']} ready pods now vs {baseline['readyPods']} before.")
    tracker.stage("verify", "done" if verdict == "clean" else "warning",
                  "Matches the baseline." if verdict == "clean" else " ".join(notes))
    _mark_step(steps, 5, "completed", "Workloads compared with the baseline.")
    _mark_step(steps, 6, "completed", f"API server reports {final_version}.")
    tracker.log(None, f"Verify: API server {final_version}; " + ("matches the baseline." if verdict == "clean" else " ".join(notes)))
    update_job(
        job_id,
        status="completed",
        message=f"Cluster upgraded to {final_version}." + ("" if verdict == "clean" else " " + " ".join(notes)),
        steps=steps,
        activeStep=6,
        versionAfter=final_version,
        after=after,
        verdict=verdict,
        finishedAt=_now(),
    )
    _record_built_version(job_id, access.cluster_id, final_version)


def _record_built_version(job_id: str, cluster_id: str, server_version: str) -> None:
    """A cluster the Cluster Builder made: its build now reads the new version."""
    from .services.cluster_build.onboard import record_cluster_version
    from .upgrade_jobs import _with_app

    def _record() -> None:
        from .db import db

        try:
            record_cluster_version(cluster_id, server_version)
        except Exception:  # noqa: BLE001 — the upgrade itself is done
            db.session.rollback()
            logger.warning("Could not record %s on the build of %s", server_version, cluster_id, exc_info=True)

    _with_app(job_id, _record)


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


def validate_automated_target(provider: str, current_version: str, target_version: str) -> None:
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
    if provider not in {"kubeadm", "minikube"}:
        raise ValueError(
            f"Automatic upgrade for {provider} requires provider-specific configuration "
            "(cluster name, resource group, etc.). Use kubeadm or minikube clusters."
        )


def launch(
    access: ClusterAccess,
    job_id: str,
    *,
    provider: str,
    target_version: str,
    run_kubectl: RunKubectlFn,
    options: Optional[Dict[str, Any]] = None,
    window_end: Optional[str] = None,
) -> None:
    """Run an adopted/created job on a background thread."""
    if provider == "kubeadm":
        def _worker() -> None:
            _execute_kubeadm_upgrade(access, target_version, run_kubectl, job_id, options=options, window_end=window_end)
    elif provider == "minikube":
        def _worker() -> None:
            _execute_minikube_upgrade(target_version, job_id)
    else:
        raise ValueError(f"Automatic upgrade is not supported for provider {provider}.")
    run_job_async(job_id, _worker)


def start_automated_upgrade(
    access: ClusterAccess,
    *,
    provider: str,
    target_version: str,
    current_version: str,
    run_kubectl: RunKubectlFn,
    precheck: Dict[str, Any],
    options: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    validate_automated_target(provider, current_version, target_version)
    job = create_job(
        cluster_id=access.cluster_id,
        target_version=normalize_version(target_version),
        provider=provider,
        steps=_step_states(),
        extra={"currentVersion": current_version, "mode": "automatic", "options": normalize_options(options)},
    )
    job_id = job["jobId"]
    launch(access, job_id, provider=provider, target_version=target_version, run_kubectl=run_kubectl, options=options)
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
