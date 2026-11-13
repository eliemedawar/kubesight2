"""OpenTofu in Kubernetes Jobs, as ``kubesight-provisioner`` (OKE workload identity).

For an Oracle Cloud build whose account signs in by OKE workload identity
(``clouds.oci_wi``), the backend never runs OpenTofu and never holds create or
delete rights in OCI (K3). Each OpenTofu step is a Job in the backend's own
namespace, running as the service account ``kubesight-provisioner`` (the only
identity OCI's policy lets create and delete), from the provisioner image by
digest and from the fixed template in ``job_manifest``. No user input reaches
its command: the Job's environment names a run id and a phase, and the Job
asks the backend for the rest (``run_spec``).

  plan   the Job runs ``tofu plan -out`` and sends back the plan file (opaque,
         encrypted when state encryption is on), its sha256, OpenTofu's text
         and the resource changes with sensitive values masked. The job then
         waits for a second person, signed in with MFA and never the
         requester, to approve that exact hash (K13).
  apply  the Job fetches the approved plan, checks its hash, applies that file
         only, and reports the cluster's facts (K22). The backend keeps state
         and plan as blobs it never opens.
  bind   on a created cluster: the Job binds the backend's subject to its
         named role and installs the LoadBalancer admission policy (K20, K21).

Jobs sign in to the backend with a projected service-account token (audience
``kubesight-backend``); the backend checks it with a TokenReview and accepts
only ``kubesight-provisioner`` from the pod of the step it is waiting for.

Every request, approval, plan, apply, destroy and facts report is one
structured line on stdout (K19), never with a token, key or state.
"""

from __future__ import annotations

import base64
import hashlib
import ipaddress
import json
import logging
import os
import queue
import re
import shutil
import subprocess
import tempfile
import threading
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Dict, List, Optional, Tuple

from ....db import db
from ....models import ClusterBuild, ClusterProvisionJob
from ....secret_encryption import decrypt_secret, encrypt_secret
from ..scrub import scrub

logger = logging.getLogger(__name__)

AUDIENCE = "kubesight-backend"
PHASES = ("plan", "apply", "bind")
RUNNER = "kubernetes"
_MAX_PLAN_BYTES = 64 * 1024 * 1024
_MAX_TEXT = 400_000
_POLL_S = 3.0
_REVIEW_TTL_S = 60.0
BACKEND_ROLE = "kubesight-backend"
LB_POLICY = "kubesight-internal-load-balancers"
_BUILTIN_ROLES = ("view", "edit", "admin", "cluster-admin")


class RemoteError(RuntimeError):
    pass


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _as_utc(value: Optional[datetime]) -> Optional[datetime]:
    if value is None:
        return None
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# Audit lines (K19)
# ---------------------------------------------------------------------------

_audit_sink: Optional[Callable[[Dict[str, Any]], None]] = None


def set_audit_sink(sink: Optional[Callable[[Dict[str, Any]], None]]) -> None:
    """Test seam: where audit lines go instead of stdout."""
    global _audit_sink
    _audit_sink = sink


_FORBIDDEN_AUDIT_KEYS = {"token", "key", "state", "plan", "planB64", "kubeconfig", "password", "secret"}


def audit(event: str, *, job: Optional[ClusterProvisionJob] = None, build: Optional[ClusterBuild] = None,
          **fields: Any) -> None:
    """One structured line for the platform's log store: who asked, who
    approved, the plan hash, the cluster OCID, the times and the result."""
    line: Dict[str, Any] = {"kubesight_audit": "oke-provisioning", "event": event,
                            "time": _utcnow().isoformat(timespec="seconds")}
    if job is not None:
        line.update({
            "jobId": job.id, "buildId": job.build_id, "operation": job.operation,
            "requestedBy": job.requested_by, "approvedBy": job.approved_by,
            "planSha256": job.plan_sha256, "approvedPlanSha256": job.approved_plan_sha256,
        })
    if build is not None:
        line["cluster"] = build.name
        facts = (build.cloud_facts_json or {}).get("cluster") or {}
        if facts.get("id"):
            line["clusterOcid"] = facts["id"]
    for key, value in fields.items():
        if key in _FORBIDDEN_AUDIT_KEYS:
            continue
        line[key] = value
    if _audit_sink is not None:
        _audit_sink(line)
        return
    print(json.dumps(line, sort_keys=True, default=str), flush=True)


# ---------------------------------------------------------------------------
# Which builds, and the install's settings
# ---------------------------------------------------------------------------

def is_remote(build: ClusterBuild) -> bool:
    if (build.machine_source or "") != "oci":
        return False
    from . import cloud_build

    try:
        row = cloud_build.account_row(build)
    except ValueError:
        return False
    return (row.settings_json or {}).get("authMode") == "workload_identity"


def is_remote_job(job: ClusterProvisionJob) -> bool:
    return (job.runner or "") == RUNNER


def settings() -> Dict[str, Any]:
    namespace = (os.getenv("KUBESIGHT_PROVISIONER_NAMESPACE") or os.getenv("CI_BACKEND_NAMESPACE")
                 or "kubesight").strip()
    return {
        "namespace": namespace,
        "serviceAccount": (os.getenv("KUBESIGHT_PROVISIONER_SERVICE_ACCOUNT") or "kubesight-provisioner").strip(),
        "image": (os.getenv("KUBESIGHT_PROVISIONER_IMAGE") or "").strip(),
        "pullSecret": (os.getenv("KUBESIGHT_PROVISIONER_PULL_SECRET") or "").strip(),
        "backendUrl": (os.getenv("KUBESIGHT_INTERNAL_URL") or "http://127.0.0.1:5000").rstrip("/"),
        "region": (os.getenv("OCI_RESOURCE_PRINCIPAL_REGION") or "").strip(),
        "deadlineSeconds": int(os.getenv("KUBESIGHT_PROVISIONER_DEADLINE_SECONDS") or 5400),
        "ttlSeconds": int(os.getenv("KUBESIGHT_PROVISIONER_TTL_SECONDS") or 600),
    }


def encryption() -> Dict[str, Any]:
    """K15: OpenTofu's openbao key provider on OpenBao's Transit engine."""
    if (os.getenv("KUBESIGHT_TOFU_STATE_ENCRYPTION") or "").strip().lower() != "openbao":
        return {"enabled": False}
    ca_key = (os.getenv("KUBESIGHT_OPENBAO_CA_KEY") or "ca.crt").strip()
    return {
        "enabled": True,
        "address": (os.getenv("KUBESIGHT_OPENBAO_ADDRESS") or "https://openbao.openbao.svc:8200").strip(),
        "caConfigMap": (os.getenv("KUBESIGHT_OPENBAO_CA_CONFIGMAP") or "openbao-ca").strip(),
        "caFile": f"/etc/openbao-ca/{ca_key}",
        "caKey": ca_key,
        "role": (os.getenv("KUBESIGHT_OPENBAO_ROLE") or "kubesight-provisioner-state").strip(),
        "transitPath": (os.getenv("KUBESIGHT_OPENBAO_TRANSIT_PATH") or "/transit").strip(),
        "keyName": (os.getenv("KUBESIGHT_OPENBAO_KEY_NAME") or "kubesight-tofu-state").strip(),
    }


def approval_hours() -> float:
    try:
        return max(0.25, float(os.getenv("KUBESIGHT_PROVISION_APPROVAL_HOURS") or 24))
    except ValueError:
        return 24.0


def backend_role() -> str:
    """The backend's ClusterRole on a cluster it created. ``cluster-admin`` is
    allowed since 2026-11-13 (the platform owner agreed KubeSight manages the
    OKE clusters it creates as it does the vCenter ones, any Helm chart
    included); the default stays KubeSight's own limited role."""
    return (os.getenv("KUBESIGHT_OKE_BACKEND_ROLE") or BACKEND_ROLE).strip()


def full_rights() -> bool:
    return backend_role() == "cluster-admin"


def binding_name(role: Optional[str] = None) -> str:
    """One binding per role: a binding's roleRef never changes, so a new role
    is a new binding (the first clusters keep "kubesight-backend")."""
    role = role or backend_role()
    return "kubesight-backend" if role == BACKEND_ROLE else f"kubesight-backend-{role}"


# ---------------------------------------------------------------------------
# kubectl against the cluster KubeSight runs in
# ---------------------------------------------------------------------------

_kubectl_override: Optional[Callable[[List[str], Optional[str]], Tuple[int, str, str]]] = None


def set_kubectl(fn) -> None:
    """Test seam: ``fn(args, input_text) -> (rc, stdout, stderr)``."""
    global _kubectl_override
    _kubectl_override = fn


def _kubectl_base() -> List[str]:
    command = ["kubectl"]
    kubeconfig = os.getenv("K8S_KUBECONFIG", "").strip()
    if kubeconfig:
        command.extend(["--kubeconfig", kubeconfig])
    return command


def _kubectl(args: List[str], input_text: Optional[str] = None, timeout: int = 60) -> Tuple[int, str, str]:
    if _kubectl_override is not None:
        return _kubectl_override(args, input_text)
    completed = subprocess.run(
        _kubectl_base() + args, input=input_text, text=True, capture_output=True, check=False, timeout=timeout,
    )
    return completed.returncode, completed.stdout, completed.stderr


# ---------------------------------------------------------------------------
# The Job (the fixed template)
# ---------------------------------------------------------------------------

_NAME_RE = re.compile(r"^kubesight-provision-\d+-(plan|apply|bind)-\d+$")


def job_name(job_id: int, phase: str, attempt: int) -> str:
    return f"kubesight-provision-{int(job_id)}-{phase}-{int(attempt)}"


def job_manifest(job_id: int, phase: str, name: str) -> Dict[str, Any]:
    if phase not in PHASES or not _NAME_RE.fullmatch(name):
        raise RemoteError("Refusing to build a provisioner Job outside the fixed template.")
    cfg = settings()
    image = cfg["image"]
    if not re.fullmatch(r"[a-z0-9.\-:/_]+@sha256:[0-9a-f]{64}", image):
        raise RemoteError(
            "The provisioner image must be set by digest (KUBESIGHT_PROVISIONER_IMAGE = "
            "registry/kubesight-provisioner@sha256:...)."
        )
    enc = encryption()
    labels = {"app.kubernetes.io/name": "kubesight-provisioner", "kubesight.io/provision-job": str(int(job_id)),
              "kubesight.io/phase": phase}
    env = [
        {"name": "KUBESIGHT_BACKEND_URL", "value": cfg["backendUrl"]},
        {"name": "KUBESIGHT_RUN_ID", "value": str(int(job_id))},
        {"name": "KUBESIGHT_PHASE", "value": phase},
        {"name": "OCI_RESOURCE_PRINCIPAL_VERSION", "value": "2.2"},
        {"name": "OCI_RESOURCE_PRINCIPAL_REGION", "value": cfg["region"] or "me-dubai-1"},
        {"name": "OCI_CLI_AUTH", "value": "oke_workload_identity"},
        {"name": "TF_CLI_CONFIG_FILE", "value": "/etc/tofu/tofurc"},
        {"name": "TF_IN_AUTOMATION", "value": "1"},
        {"name": "HOME", "value": "/work"},
    ]
    mounts = [
        {"name": "work", "mountPath": "/work"},
        {"name": "tmp", "mountPath": "/tmp"},
        {"name": "backend-token", "mountPath": "/var/run/kubesight", "readOnly": True},
    ]
    volumes: List[Dict[str, Any]] = [
        {"name": "work", "emptyDir": {"sizeLimit": "1Gi"}},
        {"name": "tmp", "emptyDir": {"sizeLimit": "256Mi"}},
        {"name": "backend-token", "projected": {"sources": [{"serviceAccountToken": {
            "audience": AUDIENCE, "expirationSeconds": 600, "path": "token",
        }}]}},
    ]
    if enc["enabled"]:
        mounts.append({"name": "openbao-ca", "mountPath": "/etc/openbao-ca", "readOnly": True})
        volumes.append({"name": "openbao-ca", "configMap": {"name": enc["caConfigMap"]}})
    pod_spec: Dict[str, Any] = {
        "serviceAccountName": cfg["serviceAccount"],
        "automountServiceAccountToken": True,
        "restartPolicy": "Never",
        "enableServiceLinks": False,
        "securityContext": {
            "runAsNonRoot": True, "runAsUser": 10001, "runAsGroup": 10001, "fsGroup": 10001,
            "seccompProfile": {"type": "RuntimeDefault"},
        },
        "containers": [{
            "name": "tofu",
            "image": image,
            "imagePullPolicy": "IfNotPresent",
            "command": ["/usr/local/bin/kubesight-provision"],
            "env": env,
            "securityContext": {
                "allowPrivilegeEscalation": False, "readOnlyRootFilesystem": True,
                "capabilities": {"drop": ["ALL"]},
            },
            "resources": {"requests": {"cpu": "250m", "memory": "512Mi"}, "limits": {"memory": "1Gi"}},
            "volumeMounts": mounts,
        }],
        "volumes": volumes,
    }
    if cfg["pullSecret"]:
        pod_spec["imagePullSecrets"] = [{"name": cfg["pullSecret"]}]
    return {
        "apiVersion": "batch/v1",
        "kind": "Job",
        "metadata": {"name": name, "namespace": cfg["namespace"], "labels": dict(labels)},
        "spec": {
            "backoffLimit": 0,
            "activeDeadlineSeconds": cfg["deadlineSeconds"],
            "ttlSecondsAfterFinished": cfg["ttlSeconds"],
            "template": {"metadata": {"labels": dict(labels)}, "spec": pod_spec},
        },
    }


# ---------------------------------------------------------------------------
# Launchers: Kubernetes, or in-process for demos and tests
# ---------------------------------------------------------------------------

class KubernetesLauncher:
    mode = "kubernetes"

    def launch(self, name: str, manifest: Dict[str, Any], *, job_id: int, phase: str) -> None:
        rc, _, err = _kubectl(["create", "-f", "-", "-o", "name"], json.dumps(manifest))
        if rc != 0:
            raise RemoteError(f"Kubernetes refused the provisioner Job {name}: {scrub(err.strip())[:600]}")

    def state(self, name: str) -> Tuple[str, str]:
        ns = settings()["namespace"]
        rc, out, err = _kubectl(["-n", ns, "get", "job", name, "-o", "json"])
        if rc != 0:
            if "NotFound" in err or "not found" in err:
                return "missing", ""
            return "unknown", scrub(err.strip())[:300]
        try:
            status = (json.loads(out) or {}).get("status") or {}
        except ValueError:
            return "unknown", "unreadable Job status"
        for condition in status.get("conditions") or []:
            if condition.get("status") != "True":
                continue
            if condition.get("type") == "Complete":
                return "succeeded", ""
            if condition.get("type") == "Failed":
                return "failed", condition.get("message") or condition.get("reason") or "the Job failed"
        if status.get("succeeded"):
            return "succeeded", ""
        if status.get("failed"):
            return "failed", "the Job's pod failed"
        return "active", ""

    def follow(self, name: str, on_line: Callable[[str], None], stop: threading.Event) -> threading.Thread:
        """Stream the Job's log (kubectl logs -f), resuming after a drop."""
        ns = settings()["namespace"]

        def _run() -> None:
            since: Optional[str] = None
            while not stop.is_set():
                args = ["-n", ns, "logs", f"job/{name}", "-f", "--timestamps", "--pod-running-timeout=10m"]
                if since:
                    args.append(f"--since-time={since}")
                try:
                    process = subprocess.Popen(
                        _kubectl_base() + args, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                        text=True, encoding="utf-8", errors="replace", bufsize=1,
                    )
                except OSError:
                    return
                for raw in process.stdout:  # type: ignore[union-attr]
                    if raw.startswith("Error from server") and "is waiting to start" in raw:
                        continue  # the pod is not running yet; the next try reads it
                    stamp, _, line = raw.rstrip("\n").partition(" ")
                    if since and stamp <= since:
                        continue
                    if re.match(r"^\d{4}-\d{2}-\d{2}T", stamp):
                        since = stamp
                        on_line(line)
                    else:
                        on_line(raw.rstrip("\n"))
                    if stop.is_set():
                        break
                try:
                    process.kill()
                except OSError:
                    pass
                stop.wait(3)

        thread = threading.Thread(target=_run, name=f"{name}-log", daemon=True)
        thread.start()
        return thread

    def delete(self, name: str) -> None:
        ns = settings()["namespace"]
        _kubectl(["-n", ns, "delete", "job", name, "--ignore-not-found", "--wait=false",
                  "--cascade=foreground"])


class SimulatedLauncher:
    """Runs a phase in-process against the simulated OpenTofu engine, through
    the same receivers the real Job posts to. For demo mode and tests."""

    mode = "simulated"

    def __init__(self) -> None:
        self.states: Dict[str, Tuple[str, str]] = {}
        self.lines: Dict[str, List[str]] = {}

    def launch(self, name: str, manifest: Dict[str, Any], *, job_id: int, phase: str) -> None:
        self.lines[name] = []
        try:
            simulate_phase(job_id, phase, self.lines[name].append)
            self.states[name] = ("succeeded", "")
        except Exception as exc:  # noqa: BLE001 — a simulated Job fails like a real one
            self.lines[name].append(f"Error: {exc}")
            try:
                job = db.session.get(ClusterProvisionJob, job_id)
                if job is not None:
                    record_failed(job, {"phase": phase, "message": str(exc)})
            except Exception:  # noqa: BLE001
                db.session.rollback()
            self.states[name] = ("failed", str(exc))

    def state(self, name: str) -> Tuple[str, str]:
        return self.states.get(name, ("missing", ""))

    def follow(self, name: str, on_line, stop: threading.Event) -> threading.Thread:
        def _run() -> None:
            for line in self.lines.get(name, []):
                on_line(line)

        thread = threading.Thread(target=_run, daemon=True)
        thread.start()
        return thread

    def delete(self, name: str) -> None:
        self.states.pop(name, None)


_launcher_override = None
_simulated = SimulatedLauncher()


def set_launcher(launcher) -> None:
    """Test seam."""
    global _launcher_override
    _launcher_override = launcher


def launcher():
    if _launcher_override is not None:
        return _launcher_override
    from . import inventory

    return _simulated if inventory.simulation_enabled() else KubernetesLauncher()


# ---------------------------------------------------------------------------
# Running one phase
# ---------------------------------------------------------------------------

def _remote_progress(job: ClusterProvisionJob) -> Dict[str, Any]:
    return dict((job.progress_json or {}).get("remote") or {})


def _results(job: ClusterProvisionJob) -> Dict[str, Any]:
    return dict((job.config_json or {}).get("remote") or {})


def _set_results(job: ClusterProvisionJob, phase: str, result: Dict[str, Any]) -> None:
    config = dict(job.config_json or {})
    remote = dict(config.get("remote") or {})
    entry = dict(remote.get(phase) or {})
    entry.update(result)
    remote[phase] = entry
    config["remote"] = remote
    job.config_json = config


def run_phase(job_id: int, phase: str, on_line: Callable[[str], None], timeout_s: int) -> Dict[str, Any]:
    """Launch (or reattach to) the Job for ``phase`` and wait for it. Returns
    what the Job reported; raises RemoteError when it did not succeed.

    After a backend restart the worker comes back here: a Job still running is
    followed, one that finished while KubeSight was away is read from what it
    reported, and only a step with no Job at all is launched (never an apply
    twice)."""
    job = db.session.get(ClusterProvisionJob, job_id)
    remote = _remote_progress(job)
    entry = dict(remote.get(phase) or {})
    run = launcher()
    if phase == "apply" and entry.get("finished"):
        # An apply Job ran already (KubeSight restarted after it finished):
        # what it reported stands. An apply is never launched twice.
        result = _results(job).get("apply") or {}
        if result.get("result") == "ok":
            on_line(f"The apply Job {entry.get('k8sJob')} had already finished.")
            return result
        raise RemoteError(
            f"The apply Job {entry.get('k8sJob')} already ran and did not succeed: "
            f"{result.get('message') or entry.get('jobState') or 'no report'}. Make a new plan."
        )
    name = entry.get("k8sJob") if not entry.get("finished") else None
    reported = (_results(job).get(phase) or {}).get("result") if name else None
    state, detail = run.state(name) if name else ("missing", "")

    if name and reported in ("ok", "failed"):
        on_line(f"The provisioner Job {name} reported while KubeSight was away.")
        state = "succeeded" if reported == "ok" else "failed"
    elif name and state in ("succeeded", "failed"):
        on_line(f"The provisioner Job {name} finished while KubeSight was away.")
    elif name and state in ("active", "unknown"):
        on_line(f"Following the provisioner Job {name}, still running.")
        state, detail = _wait(run, name, on_line, timeout_s)
    else:
        if name and phase == "apply":
            raise RemoteError(
                f"The apply Job {name} is gone and never reported. Check the cluster in OCI, "
                "then make a new plan (it needs a new approval)."
            )
        attempt = int(entry.get("attempt") or 0) + 1
        name = job_name(job.id, phase, attempt)
        progress = dict(job.progress_json or {})
        remote[phase] = {"k8sJob": name, "attempt": attempt, "launchedAt": _utcnow().isoformat()}
        remote["phase"] = phase
        progress["remote"] = remote
        job.progress_json = progress
        _set_results(job, phase, {"result": None, "message": None})
        db.session.commit()
        on_line(f"Starting the provisioner Job {name} (as {settings()['serviceAccount']}).")
        audit("job-launched", job=job, phase=phase, k8sJob=name, runner=run.mode)
        run.launch(name, job_manifest(job.id, phase, name), job_id=job.id, phase=phase)
        state, detail = _wait(run, name, on_line, timeout_s)

    db.session.expire_all()
    job = db.session.get(ClusterProvisionJob, job_id)
    progress = dict(job.progress_json or {})
    remote = _remote_progress(job)
    entry = dict(remote.get(phase) or {})
    entry.update({"finished": True, "finishedAt": _utcnow().isoformat(), "jobState": state})
    remote[phase] = entry
    progress["remote"] = remote
    job.progress_json = progress
    db.session.commit()
    result = _results(job).get(phase) or {}
    if state != "succeeded" or result.get("result") != "ok":
        message = result.get("message") or detail or "the provisioner Job did not report back"
        raise RemoteError(f"The provisioner Job {name} failed: {message}")
    return result


def _wait(run, name: str, on_line: Callable[[str], None], timeout_s: int) -> Tuple[str, str]:
    """Follow the Job until it ends. The log follower runs on its own thread
    and only QUEUES lines: ``on_line`` writes the job row through the worker's
    session, so it is called here, on the worker thread, never from the
    follower (a session touched from two threads kept a connection checked out
    for a whole apply; the database pooler closed it, and the job then failed
    with "SSL connection has been closed unexpectedly" though OKE had built
    the cluster)."""
    lines: "queue.Queue[str]" = queue.Queue()

    def drain() -> None:
        while True:
            try:
                line = lines.get_nowait()
            except queue.Empty:
                return
            on_line(line)

    stop = threading.Event()
    follower = run.follow(name, lines.put, stop)
    deadline = time.monotonic() + timeout_s
    try:
        while True:
            drain()
            state, detail = run.state(name)
            if state in ("succeeded", "failed"):
                return state, detail
            if state == "missing":
                return "failed", "the Job disappeared"
            if time.monotonic() > deadline:
                run.delete(name)
                return "failed", f"it ran longer than {timeout_s // 60} minutes and was stopped"
            time.sleep(_POLL_S)
    finally:
        stop.set()
        follower.join(timeout=5)
        drain()


# ---------------------------------------------------------------------------
# Signing in to the backend (TokenReview)
# ---------------------------------------------------------------------------

_reviewer: Optional[Callable[[str], Optional[Dict[str, Any]]]] = None
_review_cache: Dict[str, Tuple[float, Optional[Dict[str, Any]]]] = {}
_review_lock = threading.Lock()


def set_token_reviewer(fn) -> None:
    """Test seam: ``fn(token) -> {"username", "podName", "audiences"} | None``."""
    global _reviewer
    _reviewer = fn
    _review_cache.clear()


def _review(token: str) -> Optional[Dict[str, Any]]:
    if _reviewer is not None:
        return _reviewer(token)
    body = {"apiVersion": "authentication.k8s.io/v1", "kind": "TokenReview",
            "spec": {"token": token, "audiences": [AUDIENCE]}}
    # The token goes through stdin only, never a command-line argument.
    # --validate=false: kubectl's client-side check looks TokenReview up among
    # the CRDs, and the backend may not list CRDs (AI cluster, 2026-11-13). The
    # API server still validates the body; KubeSight builds it itself.
    rc, out, err = _kubectl(["create", "--validate=false", "-f", "-", "-o", "json"], json.dumps(body))
    if rc != 0:
        logger.warning("TokenReview failed: %s", scrub(err.strip())[:300])
        return None
    try:
        status = (json.loads(out) or {}).get("status") or {}
    except ValueError:
        return None
    if not status.get("authenticated"):
        return None
    user = status.get("user") or {}
    extra = user.get("extra") or {}
    pods = extra.get("authentication.kubernetes.io/pod-name") or []
    return {"username": user.get("username") or "", "podName": pods[0] if pods else "",
            "audiences": status.get("audiences") or []}


def review(token: str) -> Optional[Dict[str, Any]]:
    if not token or len(token) > 8192:
        return None
    key = hashlib.sha256(token.encode("utf-8")).hexdigest()
    now = time.monotonic()
    with _review_lock:
        cached = _review_cache.get(key)
        if cached and now - cached[0] < _REVIEW_TTL_S:
            return cached[1]
    info = _review(token)
    with _review_lock:
        if len(_review_cache) > 512:
            _review_cache.clear()
        _review_cache[key] = (now, info)
    return info


def authenticate(job: ClusterProvisionJob, token: str, phase: Optional[str] = None) -> Optional[str]:
    """None when ``token`` is the provisioner Job's own for this job's current
    phase; otherwise why not (never shown in detail to the caller)."""
    if not is_remote_job(job):
        return "not a provisioner job"
    # What costs nothing first: a TokenReview runs only while a step waits.
    remote = _remote_progress(job)
    current = remote.get("phase")
    if phase is not None and phase != current:
        return f"the backend waits for {current}, not {phase}"
    entry = remote.get(current) or {}
    name = entry.get("k8sJob") or ""
    if not name or entry.get("finished"):
        return "no step is waiting"
    expected = {"plan": "planning", "apply": "applying", "bind": "connecting"}.get(current)
    if job.status != expected:
        return f"the job is {job.status}"
    info = review(token)
    if not info:
        return "the token is not valid"
    cfg = settings()
    if info.get("username") != f"system:serviceaccount:{cfg['namespace']}:{cfg['serviceAccount']}":
        return "not the provisioner's service account"
    if AUDIENCE not in (info.get("audiences") or []):
        return "wrong audience"
    if not (info.get("podName") or "").startswith(name + "-"):
        return "not the pod of the step the backend waits for"
    return None


# ---------------------------------------------------------------------------
# What a Job asks for, and what it reports
# ---------------------------------------------------------------------------

def approval_problem(job: ClusterProvisionJob) -> Optional[str]:
    """Why ``job``'s saved plan may not be applied now, or None."""
    if not job.plan_sha256 or not job.plan_cipher:
        return "There is no saved plan."
    if not job.approved_plan_sha256 or not job.approved_by_user_id:
        return "Nobody has approved this plan."
    if job.approved_plan_sha256 != job.plan_sha256:
        return "The approved hash is not the saved plan's hash."
    own = bool(
        (job.requested_by_user_id and job.approved_by_user_id == job.requested_by_user_id)
        or (not job.requested_by_user_id and job.requested_by and job.approved_by == job.requested_by)
    )
    # An admin's own approval counts only while the switch is still on and the
    # approval was recorded as one (service.approve_plan_hash checked the admin).
    if own and not (admin_self_approve() and self_approved(job)):
        return "The person who asked cannot approve."
    approved = _as_utc(job.approved_at)
    if approved is None:
        return "Nobody has approved this plan."
    planned = planned_at(job) or approved
    if _utcnow() - planned > timedelta(hours=approval_hours()):
        return "The approval has expired. Plan again."
    return None


def apply_reported_ok(job: ClusterProvisionJob) -> bool:
    """The provisioner Job applied the plan and said so (its facts were kept)."""
    return is_remote_job(job) and (_results(job).get("apply") or {}).get("result") == "ok"


def admin_self_approve() -> bool:
    """KUBESIGHT_PROVISION_ADMIN_SELF_APPROVE: an admin (with MFA) may approve
    a plan they asked for themselves. Off by default: it sets aside K13's
    "never the requester"; every such approval is audited and named in the
    platform owner's email."""
    return (os.getenv("KUBESIGHT_PROVISION_ADMIN_SELF_APPROVE") or "").strip().lower() in ("1", "true", "yes", "on")


def self_approved(job: ClusterProvisionJob) -> bool:
    return bool(((job.config_json or {}).get("remote") or {}).get("selfApproved"))


def mark_self_approved(job: ClusterProvisionJob) -> None:
    config = dict(job.config_json or {})
    config["remote"] = {**(config.get("remote") or {}), "selfApproved": True}
    job.config_json = config


def planned_at(job: ClusterProvisionJob) -> Optional[datetime]:
    """When the saved plan was made (what an approval's age counts from)."""
    stamp = ((((job.config_json or {}).get("remote") or {}).get("plan")) or {}).get("at")
    try:
        return _as_utc(datetime.fromisoformat(stamp)) if stamp else _as_utc(job.created_at)
    except (TypeError, ValueError):
        return _as_utc(job.created_at)


def require_encryption() -> Optional[str]:
    """K15: from the first real build, state and plans are encrypted. None
    when that holds (or a test/demo explicitly allows plain state)."""
    if encryption().get("enabled"):
        return None
    if (os.getenv("KUBESIGHT_ALLOW_PLAIN_STATE") or "").strip().lower() in ("1", "true", "yes"):
        return None
    return ("State encryption (K15) is not on: OpenBao's state key must be live and "
            "oke.stateEncryption.enabled set before KubeSight creates OKE clusters.")


def run_spec(job: ClusterProvisionJob, phase: str) -> Dict[str, Any]:
    build = db.session.get(ClusterBuild, job.build_id)
    enc = {k: v for k, v in encryption().items() if k not in ("caConfigMap", "caKey")}
    if enc.get("enabled"):
        from . import state_store

        document = state_store.read_state_document(job.build_id)
        if document and not state_store.is_encrypted(document):
            # Written before encryption was on: read once, written back encrypted.
            enc["migrateFromPlain"] = True
    spec: Dict[str, Any] = {
        "phase": phase, "runId": job.id, "buildId": job.build_id, "operation": job.operation,
        "encryption": enc,
    }
    if phase in ("plan", "apply"):
        config = (job.config_json or {}).get("tofu")
        if not config:
            raise RemoteError("This job has no OpenTofu configuration.")
        spec["config"] = config
    if phase == "apply":
        problem = approval_problem(job)
        if problem:
            audit("apply-refused", job=job, build=build, reason=problem)
            raise PermissionError(problem)
        plan = base64.b64decode(decrypt_secret(job.plan_cipher))
        if hashlib.sha256(plan).hexdigest() != job.approved_plan_sha256:
            audit("apply-refused", job=job, build=build, reason="stored plan does not match its hash")
            raise PermissionError("The stored plan does not match the approved hash.")
        spec["planB64"] = base64.b64encode(plan).decode("ascii")
        spec["approvedPlanSha256"] = job.approved_plan_sha256
    if phase == "bind":
        bind = (_results(job).get("bind") or {}).get("spec")
        if not bind:
            raise RemoteError("Nothing to bind.")
        spec["bind"] = bind
    return spec


def record_plan(job: ClusterProvisionJob, payload: Dict[str, Any]) -> None:
    from .jobs import summarize_plan

    try:
        plan = base64.b64decode(str(payload.get("planB64") or ""), validate=True)
    except (ValueError, TypeError) as exc:
        raise ValueError("planB64 is not base64.") from exc
    if not plan or len(plan) > _MAX_PLAN_BYTES:
        raise ValueError("The plan file is empty or too large.")
    digest = hashlib.sha256(plan).hexdigest()
    if digest != str(payload.get("planSha256") or "").lower():
        raise ValueError("planSha256 is not the plan file's sha256.")
    document = payload.get("plan") if isinstance(payload.get("plan"), dict) else {}
    summary = summarize_plan(document, job.operation)
    summary["checks"] = list((job.config_json or {}).get("checks") or [])
    summary["planSha256"] = digest
    summary["encrypted"] = bool(payload.get("encrypted"))
    job.plan_summary_json = summary
    job.plan_text = scrub(str(payload.get("planText") or ""))[:_MAX_TEXT]
    job.plan_cipher = encrypt_secret(base64.b64encode(plan).decode("ascii"))
    job.plan_sha256 = digest
    _set_results(job, "plan", {"result": "ok", "planSha256": digest, "at": _utcnow().isoformat()})
    db.session.commit()
    audit("plan", job=job, build=db.session.get(ClusterBuild, job.build_id), result="planned",
          add=summary.get("add"), change=summary.get("change"), destroy=summary.get("destroy"))


_FACT_KEYS = ("resourceCount", "cluster", "nodePools", "nsgs")


def _clean_facts(raw: Any) -> Dict[str, Any]:
    """Only the fixed list (K22); anything else the Job might send is dropped."""
    raw = raw if isinstance(raw, dict) else {}
    out: Dict[str, Any] = {"resourceCount": int(raw.get("resourceCount") or 0)}
    cluster = raw.get("cluster") if isinstance(raw.get("cluster"), dict) else None
    if cluster:
        endpoint = cluster.get("privateEndpoint") if isinstance(cluster.get("privateEndpoint"), dict) else {}
        address = str(endpoint.get("address") or "")
        try:
            address = str(ipaddress.ip_address(address)) if address else ""
        except ValueError:
            address = ""
        out["cluster"] = {
            "id": str(cluster.get("id") or "")[:255] or None,
            "name": str(cluster.get("name") or "")[:255] or None,
            "kubernetesVersion": str(cluster.get("kubernetesVersion") or "")[:32] or None,
            "type": str(cluster.get("type") or "")[:32] or None,
            "state": str(cluster.get("state") or "")[:32] or None,
            "privateEndpoint": {"address": address or None,
                                "port": int(endpoint.get("port") or 0) or None},
            "cniType": str(cluster.get("cniType") or "")[:32] or None,
            "endpointSubnetId": str(cluster.get("endpointSubnetId") or "")[:255] or None,
            "serviceLbSubnetIds": [str(s)[:255] for s in cluster.get("serviceLbSubnetIds") or []][:4],
        }
    else:
        out["cluster"] = None
    pools = []
    for pool in raw.get("nodePools") or []:
        if not isinstance(pool, dict):
            continue
        pools.append({
            "id": str(pool.get("id") or "")[:255] or None,
            "name": str(pool.get("name") or "")[:255] or None,
            "kubernetesVersion": str(pool.get("kubernetesVersion") or "")[:32] or None,
            "shape": str(pool.get("shape") or "")[:64] or None,
            "ocpus": pool.get("ocpus"), "memoryGb": pool.get("memoryGb"),
            "size": int(pool.get("size") or 0), "nodeCount": int(pool.get("nodeCount") or 0),
            "nodes": [
                {"id": str(n.get("id") or "")[:255] or None, "name": str(n.get("name") or "")[:255] or None,
                 "privateIp": str(n.get("privateIp") or "")[:64] or None,
                 "state": str(n.get("state") or "")[:32] or None}
                for n in (pool.get("nodes") or [])[:200] if isinstance(n, dict)
            ],
        })
    out["nodePools"] = pools[:8]
    out["nsgs"] = [
        {"id": str(n.get("id") or "")[:255] or None, "name": str(n.get("name") or "")[:255] or None}
        for n in (raw.get("nsgs") or [])[:8] if isinstance(n, dict)
    ]
    return out


def record_facts(job: ClusterProvisionJob, payload: Dict[str, Any]) -> None:
    from . import state_store

    build = db.session.get(ClusterBuild, job.build_id)
    result = str(payload.get("result") or "")
    digest = str(payload.get("planSha256") or "")
    if digest and digest != job.approved_plan_sha256:
        raise ValueError("The facts are for another plan.")
    if isinstance(payload.get("facts"), dict):
        facts = _clean_facts(payload["facts"])
        facts["reportedAt"] = _utcnow().isoformat()
        facts["jobId"] = job.id
        build.cloud_facts_json = facts
        state_store.set_resource_count(build.id, facts["resourceCount"])
    ok = result == "succeeded"
    _set_results(job, "apply", {
        "result": "ok" if ok else "failed",
        "message": None if ok else (payload.get("factsError") or "OpenTofu's apply failed"),
        "at": _utcnow().isoformat(),
    })
    db.session.commit()
    audit("facts", job=job, build=build, result=result,
          resourceCount=(build.cloud_facts_json or {}).get("resourceCount"))


def record_errored_state(job: ClusterProvisionJob, payload: Dict[str, Any]) -> None:
    raw = str(payload.get("stateB64") or "")
    if not raw or len(raw) > _MAX_PLAN_BYTES * 2:
        raise ValueError("stateB64 is empty or too large.")
    base64.b64decode(raw, validate=True)
    config = dict(job.config_json or {})
    config["erroredStateCipher"] = encrypt_secret(raw)
    job.config_json = config
    db.session.commit()
    audit("errored-state-kept", job=job, build=db.session.get(ClusterBuild, job.build_id))


def record_bound(job: ClusterProvisionJob, payload: Dict[str, Any]) -> None:
    ok = bool(payload.get("ok"))
    _set_results(job, "bind", {
        "result": "ok" if ok else "failed",
        "message": None if ok else scrub(str(payload.get("output") or ""))[-1500:],
        "provisionerSubject": str(payload.get("provisionerSubject") or "")[:255] or None,
        "at": _utcnow().isoformat(),
    })
    db.session.commit()


def record_failed(job: ClusterProvisionJob, payload: Dict[str, Any]) -> None:
    phase = str(payload.get("phase") or "")
    if phase not in PHASES:
        raise ValueError("phase must be plan, apply or bind.")
    _set_results(job, phase, {"result": "failed", "message": scrub(str(payload.get("message") or ""))[:4000]})
    db.session.commit()
    audit("job-failed", job=job, build=db.session.get(ClusterBuild, job.build_id), phase=phase)


# ---------------------------------------------------------------------------
# The simulated Job (demo mode, tests)
# ---------------------------------------------------------------------------

def _facts_from_raw_state(document: Dict[str, Any]) -> Dict[str, Any]:
    """The simulated engine keeps a plain state; read the facts the way the
    Job reads ``tofu show -json``."""
    facts: Dict[str, Any] = {"resourceCount": 0, "cluster": None, "nodePools": [], "nsgs": []}
    for resource in document.get("resources") or []:
        if resource.get("mode") != "managed":
            continue
        for instance in resource.get("instances") or []:
            facts["resourceCount"] += 1
            values = instance.get("attributes") or {}
            rtype = resource.get("type")
            if rtype == "oci_containerengine_cluster":
                endpoint = str(((values.get("endpoints") or [{}])[0] or {}).get("private_endpoint") or "")
                host, _, port = endpoint.rpartition(":")
                facts["cluster"] = {
                    "id": values.get("id"), "name": values.get("name"),
                    "kubernetesVersion": values.get("kubernetes_version"), "type": values.get("type"),
                    "state": "ACTIVE", "privateEndpoint": {"address": host or None,
                                                         "port": int(port) if port.isdigit() else None},
                    "cniType": "FLANNEL_OVERLAY",
                }
            elif rtype == "oci_containerengine_node_pool":
                facts["nodePools"].append({
                    "id": values.get("id"), "name": values.get("name"), "shape": values.get("node_shape"),
                    "size": int(values.get("size") or 0), "nodeCount": int(values.get("size") or 0),
                    "kubernetesVersion": values.get("kubernetes_version"), "nodes": [],
                })
            elif rtype == "oci_core_network_security_group":
                facts["nsgs"].append({"id": values.get("id"), "name": values.get("display_name")})
    return facts


def simulate_phase(job_id: int, phase: str, on_line: Callable[[str], None]) -> None:
    from . import state_store, tofu_runner

    job = db.session.get(ClusterProvisionJob, job_id)
    spec = run_spec(job, phase)
    if phase == "bind":
        record_bound(job, {"ok": True, "provisionerSubject": "simulated-provisioner", "output": "simulated"})
        return
    engine = tofu_runner.SimulatedEngine()
    workdir = tempfile.mkdtemp(prefix=f"ks-sim-{job_id}-")
    try:
        with open(os.path.join(workdir, "main.tf.json"), "w", encoding="utf-8") as handle:
            json.dump(spec["config"], handle)
        env = {"KUBESIGHT_BUILD_ID": str(job.build_id), "KUBESIGHT_JOB_ID": str(job.id)}
        engine.run(workdir, ["init"], env, on_line)
        if phase == "plan":
            args = ["plan", "-out=job.tfplan"] + (["-destroy"] if job.operation == "destroy" else [])
            if engine.run(workdir, args, env, on_line):
                raise RemoteError("simulated plan failed")
            shown: List[str] = []
            engine.run(workdir, ["show", "-json", "job.tfplan"], env, shown.append)
            text: List[str] = []
            engine.run(workdir, ["show", "job.tfplan"], env, text.append)
            with open(os.path.join(workdir, "job.tfplan"), "rb") as handle:
                plan = handle.read()
            record_plan(job, {
                "planB64": base64.b64encode(plan).decode("ascii"),
                "planSha256": hashlib.sha256(plan).hexdigest(),
                "planText": "\n".join(text), "plan": json.loads(shown[-1]), "encrypted": False,
            })
            return
        plan = base64.b64decode(spec["planB64"])
        if hashlib.sha256(plan).hexdigest() != spec["approvedPlanSha256"]:
            raise RemoteError("hash mismatch")
        with open(os.path.join(workdir, "job.tfplan"), "wb") as handle:
            handle.write(plan)
        code = engine.run(workdir, ["apply", "job.tfplan"], env, on_line)
        facts = _facts_from_raw_state(state_store.read_state_document(job.build_id) or {})
        record_facts(job, {"result": "succeeded" if code == 0 else "failed",
                           "planSha256": spec["approvedPlanSha256"], "facts": facts})
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


# ---------------------------------------------------------------------------
# What the created cluster gets (K20, K21)
# ---------------------------------------------------------------------------

def backend_cluster_role() -> Dict[str, Any]:
    """What KubeSight does on a cluster it manages, and nothing more. No
    admission objects (so the K21 policy stays), no cluster-wide RBAC writes,
    no impersonation, no escalate or bind."""
    write = ["create", "update", "patch", "delete"]
    return {
        "apiVersion": "rbac.authorization.k8s.io/v1",
        "kind": "ClusterRole",
        "metadata": {"name": BACKEND_ROLE, "labels": {"app.kubernetes.io/managed-by": "kubesight"}},
        "rules": [
            {"apiGroups": ["*"], "resources": ["*"], "verbs": ["get", "list", "watch"]},
            {"apiGroups": [""], "resources": [
                "pods", "services", "endpoints", "configmaps", "secrets", "serviceaccounts",
                "persistentvolumeclaims", "namespaces", "resourcequotas", "limitranges", "events",
            ], "verbs": write},
            {"apiGroups": [""], "resources": ["pods/exec", "pods/portforward", "pods/eviction"],
             "verbs": ["create"]},
            {"apiGroups": [""], "resources": ["nodes"], "verbs": ["patch"]},
            {"apiGroups": ["apps"], "resources": [
                "deployments", "replicasets", "statefulsets", "daemonsets",
                "deployments/scale", "statefulsets/scale", "replicasets/scale",
            ], "verbs": write},
            {"apiGroups": ["batch"], "resources": ["jobs", "cronjobs"], "verbs": write},
            {"apiGroups": ["networking.k8s.io"], "resources": ["ingresses", "networkpolicies"], "verbs": write},
            {"apiGroups": ["autoscaling"], "resources": ["horizontalpodautoscalers"], "verbs": write},
            {"apiGroups": ["policy"], "resources": ["poddisruptionbudgets"], "verbs": write},
            {"apiGroups": ["rbac.authorization.k8s.io"], "resources": ["roles", "rolebindings"],
             "verbs": write},
        ],
    }


def load_balancer_policy() -> List[Dict[str, Any]]:
    """K21 on the created cluster: a LoadBalancer Service without exactly the
    platform's annotations is refused, whoever applies it."""
    from .clouds.oci_wi import LB_ANNOTATIONS

    checks = " && ".join(
        f"('{key}' in object.metadata.annotations && object.metadata.annotations['{key}'] == '{value}')"
        for key, value in LB_ANNOTATIONS.items()
    )
    expression = f"object.spec.type != 'LoadBalancer' || (has(object.metadata.annotations) && {checks})"
    listed = ", ".join(f"{k}: \"{v}\"" for k, v in LB_ANNOTATIONS.items())
    return [
        {
            "apiVersion": "admissionregistration.k8s.io/v1",
            "kind": "ValidatingAdmissionPolicy",
            "metadata": {"name": LB_POLICY, "labels": {"app.kubernetes.io/managed-by": "kubesight"}},
            "spec": {
                "failurePolicy": "Fail",
                "matchConstraints": {"resourceRules": [{
                    "apiGroups": [""], "apiVersions": ["v1"], "operations": ["CREATE", "UPDATE"],
                    "resources": ["services"],
                }]},
                "validations": [{
                    "expression": expression,
                    "message": f"A LoadBalancer Service here must be internal and flexible: {listed}",
                }],
            },
        },
        {
            "apiVersion": "admissionregistration.k8s.io/v1",
            "kind": "ValidatingAdmissionPolicyBinding",
            "metadata": {"name": LB_POLICY, "labels": {"app.kubernetes.io/managed-by": "kubesight"}},
            "spec": {"policyName": LB_POLICY, "validationActions": ["Deny"]},
        },
    ]


BACKEND_LIMITS = "kubesight-backend-limits"


def backend_limits_policy(subject: str) -> List[Dict[str, Any]]:
    """What the backend's role alone cannot say: it never writes in
    kube-system and never mints a service-account token Secret, so its
    rights cannot be turned into a privileged account's."""
    expression = (
        "request.namespace != 'kube-system' && !(request.kind.kind == 'Secret' && object != null && "
        "has(object.type) && object.type == 'kubernetes.io/service-account-token')"
    )
    return [
        {
            "apiVersion": "admissionregistration.k8s.io/v1",
            "kind": "ValidatingAdmissionPolicy",
            "metadata": {"name": BACKEND_LIMITS, "labels": {"app.kubernetes.io/managed-by": "kubesight"}},
            "spec": {
                "failurePolicy": "Fail",
                "matchConstraints": {"resourceRules": [{
                    "apiGroups": ["*"], "apiVersions": ["*"], "operations": ["CREATE", "UPDATE", "DELETE"],
                    "resources": ["*"],
                }]},
                "matchConditions": [{"name": "the-backend", "expression": f"request.userInfo.username == '{subject}'"}],
                "validations": [{
                    "expression": expression,
                    "message": "KubeSight does not change kube-system or make service-account tokens here.",
                }],
            },
        },
        {
            "apiVersion": "admissionregistration.k8s.io/v1",
            "kind": "ValidatingAdmissionPolicyBinding",
            "metadata": {"name": BACKEND_LIMITS, "labels": {"app.kubernetes.io/managed-by": "kubesight"}},
            "spec": {"policyName": BACKEND_LIMITS, "validationActions": ["Deny"]},
        },
    ]


def bind_manifests(subject: str) -> List[Dict[str, Any]]:
    if not subject or len(subject) > 512 or not re.fullmatch(r"[A-Za-z0-9._:@/+=-]+", subject):
        raise RemoteError("The backend's subject on the cluster is unknown or not a plain name.")
    role = backend_role()
    items: List[Dict[str, Any]] = []
    if role == BACKEND_ROLE:
        items.append(backend_cluster_role())
    elif role not in _BUILTIN_ROLES:
        raise RemoteError(f"Unknown role for the backend: {role}.")
    items.append({
        "apiVersion": "rbac.authorization.k8s.io/v1",
        "kind": "ClusterRoleBinding",
        "metadata": {"name": binding_name(role), "labels": {"app.kubernetes.io/managed-by": "kubesight"}},
        "roleRef": {"apiGroup": "rbac.authorization.k8s.io", "kind": "ClusterRole", "name": role},
        "subjects": [{"apiGroup": "rbac.authorization.k8s.io", "kind": "User", "name": subject}],
    })
    items.extend(load_balancer_policy())
    if role != "cluster-admin":
        # With full rights (charts in kube-system, cluster-wide objects) the
        # backend is not fenced; the K21 load-balancer rule still stands.
        items.extend(backend_limits_policy(subject))
    return items


def exec_kubeconfig(name: str, server: str, ca_data: str, cluster_id: str, region: str) -> str:
    """K20: no token in the file. kubectl (and Helm) run kubesight-oke-token,
    which makes a short-lived token by workload identity each time."""
    import yaml

    return yaml.safe_dump({
        "apiVersion": "v1", "kind": "Config",
        "clusters": [{"name": name, "cluster": {"server": server, "certificate-authority-data": ca_data}}],
        "users": [{"name": "kubesight-backend", "user": {"exec": {
            "apiVersion": "client.authentication.k8s.io/v1beta1",
            "command": "kubesight-oke-token",
            "args": ["--cluster-id", cluster_id, "--region", region],
            "interactiveMode": "Never",
            "provideClusterInfo": False,
        }}}],
        "contexts": [{"name": name, "context": {"cluster": name, "user": "kubesight-backend"}}],
        "current-context": name,
    }, sort_keys=False)


# ---------------------------------------------------------------------------
# Emails to the platform owner on every apply and destroy (K13)
# ---------------------------------------------------------------------------

def notify(job: ClusterProvisionJob, build: ClusterBuild, outcome: str) -> None:
    recipients = (os.getenv("KUBESIGHT_PROVISION_NOTIFY_EMAILS") or "").strip()
    if not recipients:
        return
    facts = (build.cloud_facts_json or {}).get("cluster") or {}
    verb = "destroy" if job.operation == "destroy" else ("node pool change" if job.operation == "grow" else "apply")
    body = "\n".join([
        f"KubeSight {verb} on {build.name}: {outcome}.",
        "",
        f"Requested by: {job.requested_by or '-'}",
        f"Approved by:  {job.approved_by or '-'} at {job.approved_at.isoformat() if job.approved_at else '-'}"
        + ("  (SELF-APPROVED by an admin: no second person)" if self_approved(job) else ""),
        f"Plan sha256:  {job.approved_plan_sha256 or '-'}",
        f"Cluster OCID: {facts.get('id') or '-'}",
        f"Job:          #{job.id} ({job.operation})",
        f"Time:         {_utcnow().isoformat(timespec='seconds')}",
    ])
    try:
        from ....email_delivery import send_email

        send_email(recipients, f"[KubeSight] OKE {verb} {outcome}: {build.name}", body)
    except Exception as exc:  # noqa: BLE001 — an email never fails the job
        logger.warning("Could not email the platform owner about job %s: %s", job.id, exc)
        audit("notify-failed", job=job, build=build, error=str(exc)[:200])
