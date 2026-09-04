"""Kubernetes Job runner — ONE Job per build, stages as ordered initContainers.

Shape (the approved Phase 1–3 workspace mode):

    Job ci-b<buildId>-<slug>
      emptyDir /workspace  (shared by every stage)
      initContainers, in pipeline order:
        stage-0   checkout   (CI_WORKER_IMAGE — the backend image: has git+python)
        stage-1   command    (the stage's own image, e.g. maven:3.9)
        stage-2   buildctl   (container_image stages, client-only, see below)
        ...
      containers:
        collector            (CI_WORKER_IMAGE — uploads declared artifacts and
                              BuildKit image metadata back to KubeSight over the
                              per-build callback token)

Kubernetes runs initContainers strictly in order and stops at the first failure,
which is exactly sequential pipeline semantics. Per-stage status is read from
``pod.status.initContainerStatuses``; per-stage logs from ``kubectl logs -c``.

Security is the ``application_analysis_jobs.py`` recipe, unchanged in intent:
restricted securityContext (non-root 65532, no privilege escalation, read-only
root, all capabilities dropped), no ServiceAccount token, per-build Secret and
NetworkPolicy garbage-collected via ownerReference to the Job, TTL cleanup,
bounded resources. Build pods never get a Docker socket and are never
privileged.

Container images are built by **BuildKit as a remote client**: the stage runs
plain ``buildctl`` (no daemon, no relaxed seccomp — the *client* is just a gRPC
program) against the shared rootless ``buildkitd`` Deployment shipped in
``k8s/ci-buildkitd.yaml``. When ``CI_BUILDKIT_ADDR`` is not configured,
``container_image`` is simply not in this adapter's supported stage types and
the engine skips those stages with an honest explanation.

Credentials never enter argv: git auth travels as GIT_CONFIG_* environment
variables (git reads config from env; values don't appear in ``ps``), and
registry auth is a per-build docker config Secret mounted read-only.
"""

from __future__ import annotations

import base64
import json
import logging
import os
import re
import subprocess
from typing import Any, Dict, Iterator, List, Optional, Tuple

from .base import (
    FAILED,
    QUEUED,
    RUNNING,
    SUCCEEDED,
    TIMEOUT,
    ArtifactRef,
    LogChunk,
    RunnerError,
    RunnerHandle,
    StageExecution,
    StageRequirements,
)

logger = logging.getLogger(__name__)

_COF_ANNOTATION = "kubesight.io/continue-on-failure-stages"
_EXIT_MARKER = "[kubesight-exit]"


def _env(name: str, default: str) -> str:
    return os.getenv(name, default).strip() or default


def _namespace() -> str:
    return _env("CI_KUBERNETES_NAMESPACE", "kubesight-ci")


def _worker_image() -> str:
    # The backend image: ships git + python3, which is all checkout/collect need.
    return _env("CI_WORKER_IMAGE", os.getenv("APPLICATION_ANALYSIS_WORKER_IMAGE", "kubesight-backend:latest"))


def buildkit_addr() -> str:
    """Where the shared rootless buildkitd listens. Empty = image builds off."""
    return os.getenv("CI_BUILDKIT_ADDR", "").strip()


# ---------------------------------------------------------------------------
# kubectl transport (injectable so tests never need a cluster)
# ---------------------------------------------------------------------------

_kubectl_runner = None


def set_kubectl_runner(fn) -> None:
    """Test hook: ``fn(args: list[str], input_text: str|None) -> (rc, stdout, stderr)``."""
    global _kubectl_runner
    _kubectl_runner = fn


def _kubectl(args: List[str], input_text: Optional[str] = None, timeout: int = 30) -> Tuple[int, str, str]:
    if _kubectl_runner is not None:
        return _kubectl_runner(args, input_text)
    command = ["kubectl"]
    kubeconfig = os.getenv("K8S_KUBECONFIG", "").strip()
    if kubeconfig:
        command.extend(["--kubeconfig", kubeconfig])
    command.extend(args)
    completed = subprocess.run(
        command, input=input_text, text=True, capture_output=True, check=False, timeout=timeout
    )
    return completed.returncode, completed.stdout, completed.stderr


# ---------------------------------------------------------------------------
# Names and small helpers
# ---------------------------------------------------------------------------

def _dns(value: str, limit: int) -> str:
    safe = re.sub(r"[^a-z0-9-]+", "-", str(value or "").lower()).strip("-")
    return safe[:limit].rstrip("-") or "build"


def job_name_for(execution: StageExecution) -> str:
    # Build id makes the name unique forever; the slug keeps it readable.
    return f"ci-b{execution.build_id}-{_dns(execution.service_slug, 40)}"[:63].rstrip("-")


def _stage_container_name(position: int) -> str:
    return f"stage-{position}"


def _split_ref(external_ref: str) -> Tuple[str, str]:
    """``jobname#stage-N`` -> (jobname, container name)."""
    job, _, container = (external_ref or "").partition("#")
    return job, container or "stage-0"


def _secret_key(position: int, env_name: str) -> str:
    # Secret data keys must match [-._a-zA-Z0-9]+; env var names already do.
    return f"s{position}-{env_name}"


def _b64(value: str) -> str:
    return base64.b64encode(value.encode("utf-8")).decode("ascii")


# ---------------------------------------------------------------------------
# In-pod scripts
#
# Inline so they work with the backend image already deployed — no image
# rebuild is required to ship or fix them.
# ---------------------------------------------------------------------------

_CHECKOUT_SCRIPT = r"""set -eu
umask 077
export HOME=/tmp TMPDIR=/tmp
# Never fall back to an interactive prompt: without this a rejected credential
# surfaces as "could not read Username", which hides the actual 401.
export GIT_TERMINAL_PROMPT=0
# Git over HTTPS wants a fixed username per credential type -- an Atlassian API
# token authenticates as x-bitbucket-api-token-auth here, even though the same
# token pairs with the account email on the REST API. Keep this in step with
# application_checkout._git_username.
case "${KUBESIGHT_GIT_CREDENTIAL_TYPE:-}" in
  api_token) GIT_USER="x-bitbucket-api-token-auth" ;;
  *)         GIT_USER="x-token-auth" ;;
esac
# Auth as env-provided git config: never in argv, never in the remote URL.
AUTH="$(printf '%s:%s' "$GIT_USER" "$KUBESIGHT_GIT_TOKEN" | base64 | tr -d '\n')"
export GIT_CONFIG_COUNT=2
export GIT_CONFIG_KEY_0=http.extraHeader GIT_CONFIG_VALUE_0="Authorization: Basic $AUTH"
export GIT_CONFIG_KEY_1=safe.directory GIT_CONFIG_VALUE_1=/workspace/source
echo "Cloning $KUBESIGHT_REPO_URL"
git clone --no-tags --depth 50 "$KUBESIGHT_REPO_URL" /workspace/source
cd /workspace/source
if [ -n "${KUBESIGHT_REVISION:-}" ]; then
  echo "Checking out $KUBESIGHT_REVISION"
  if git fetch --no-tags --depth 50 origin "$KUBESIGHT_REVISION" 2>/dev/null; then
    git checkout --quiet FETCH_HEAD
  else
    git checkout --quiet "$KUBESIGHT_REVISION"
  fi
fi
COMMIT="$(git rev-parse HEAD)"
echo "HEAD is now at $COMMIT"
mkdir -p /workspace/.kubesight
printf '%s' "$COMMIT" > /workspace/.kubesight/commit
python3 - <<'PYEOF'
import json, os, urllib.request
url = os.environ["KUBESIGHT_CALLBACK_URL"].rstrip("/") + "/builds/" + os.environ["KUBESIGHT_BUILD_ID"] + "/meta"
body = json.dumps({"commitSha": open("/workspace/.kubesight/commit").read().strip()}).encode()
req = urllib.request.Request(url, data=body, method="POST", headers={
    "Authorization": "Bearer " + os.environ["KUBESIGHT_CALLBACK_TOKEN"],
    "Content-Type": "application/json",
})
try:
    urllib.request.urlopen(req, timeout=15)
except Exception as exc:  # Reporting the commit is best-effort, never fatal.
    print("[kubesight] commit report failed:", exc)
PYEOF
echo "Checkout complete."
"""

_COLLECTOR_SCRIPT = r"""
import glob, io, json, os, sys, urllib.request, uuid

CALLBACK = os.environ["KUBESIGHT_CALLBACK_URL"].rstrip("/")
TOKEN = os.environ["KUBESIGHT_CALLBACK_TOKEN"]
BUILD_ID = os.environ["KUBESIGHT_BUILD_ID"]
MAX_BYTES = int(os.environ.get("KUBESIGHT_MAX_ARTIFACT_BYTES", str(512 * 1024 * 1024)))
specs = json.loads(os.environ.get("KUBESIGHT_ARTIFACTS", "[]"))
images = json.loads(os.environ.get("KUBESIGHT_IMAGES", "[]"))
failures = 0


def request(url, data, headers):
    req = urllib.request.Request(url, data=data, method="POST", headers=headers)
    with urllib.request.urlopen(req, timeout=300) as resp:
        resp.read()


def post_file(path, spec):
    boundary = uuid.uuid4().hex
    body = io.BytesIO()
    fields = {
        "name": spec.get("name") or os.path.basename(path),
        "type": spec.get("type") or "binary",
        "stagePosition": str(spec.get("stagePosition", "")),
        "declaredPath": spec.get("path", ""),
    }
    for key, value in fields.items():
        body.write(("--%s\r\nContent-Disposition: form-data; name=\"%s\"\r\n\r\n%s\r\n" % (boundary, key, value)).encode())
    body.write(("--%s\r\nContent-Disposition: form-data; name=\"file\"; filename=\"%s\"\r\n"
                "Content-Type: application/octet-stream\r\n\r\n" % (boundary, os.path.basename(path))).encode())
    with open(path, "rb") as handle:
        body.write(handle.read())
    body.write(("\r\n--%s--\r\n" % boundary).encode())
    request(
        CALLBACK + "/builds/" + BUILD_ID + "/artifacts",
        body.getvalue(),
        {"Authorization": "Bearer " + TOKEN, "Content-Type": "multipart/form-data; boundary=" + boundary},
    )


for spec in specs:
    base = os.path.join("/workspace/source", spec.get("workdir") or "")
    pattern = os.path.join(base, spec.get("path", ""))
    matches = [p for p in glob.glob(pattern, recursive=True) if os.path.isfile(p)]
    if not matches:
        print("[kubesight] no files matched artifact pattern:", spec.get("path"))
        continue
    for path in matches:
        size = os.path.getsize(path)
        if size > MAX_BYTES:
            print("[kubesight] artifact too large, skipped:", path, size)
            failures += 1
            continue
        try:
            post_file(path, spec)
            print("[kubesight] uploaded", path, "(%d bytes)" % size)
        except Exception as exc:
            print("[kubesight] upload failed for", path, ":", exc)
            failures += 1

for image in images:
    meta_path = "/workspace/.kubesight/image-meta-%s.json" % image.get("stagePosition")
    if not os.path.exists(meta_path):
        continue
    try:
        meta = json.load(open(meta_path))
        payload = json.dumps({
            "name": image.get("name") or "",
            "type": "container-image",
            "uri": meta.get("image.name") or image.get("uri") or "",
            "digest": meta.get("containerimage.digest") or "",
            "stagePosition": image.get("stagePosition"),
            "metadata": {"buildkit": True},
        }).encode()
        request(
            CALLBACK + "/builds/" + BUILD_ID + "/artifacts",
            payload,
            {"Authorization": "Bearer " + TOKEN, "Content-Type": "application/json"},
        )
        print("[kubesight] recorded image", meta.get("image.name"), meta.get("containerimage.digest"))
    except Exception as exc:
        print("[kubesight] image record failed:", exc)
        failures += 1

if failures:
    # A build must not report success while its declared outputs are missing.
    sys.exit(1)
print("[kubesight] artifact collection complete")
"""


def _command_stage_script(execution: StageExecution) -> str:
    workdir = "/workspace/source"
    if execution.working_directory:
        workdir = f"/workspace/source/{execution.working_directory}"
    commands = "\n".join(execution.commands or ["true"])
    if execution.continue_on_failure:
        # Real exit code goes to the log as a marker; the container exits 0 so
        # Kubernetes lets the next initContainer run. The adapter reads the
        # marker back and reports the stage as FAILED — the pipeline continues
        # but the truth is preserved.
        return (
            "set -u\nexport HOME=/tmp TMPDIR=/tmp\n"
            f"cd {workdir}\n"
            f"(\nset -e\n{commands}\n)\nEC=$?\n"
            f"echo \"{_EXIT_MARKER} $EC\"\nexit 0\n"
        )
    return f"set -eu\nexport HOME=/tmp TMPDIR=/tmp\ncd {workdir}\n{commands}\n"


def _buildctl_args(execution: StageExecution, meta_file: str) -> str:
    registry = execution.registry or {}
    image_ref = f"{registry['host']}/{registry['repository']}:{registry['tag']}"
    context = "/workspace/source"
    if execution.working_directory:
        context = f"/workspace/source/{execution.working_directory}"
    dockerfile = registry.get("dockerfile") or "Dockerfile"
    dockerfile_dir = os.path.dirname(dockerfile) or "."
    output = f"type=image,name={image_ref},push=true"
    if registry.get("verifyTls") is False:
        output += ",registry.insecure=true"
    return (
        f"buildctl --addr {buildkit_addr()} build "
        f"--frontend dockerfile.v0 "
        f"--local context={context} "
        f"--local dockerfile={context}/{dockerfile_dir} "
        f"--opt filename={os.path.basename(dockerfile)} "
        f"--output {output} "
        f"--metadata-file {meta_file}"
    )


# ---------------------------------------------------------------------------
# Manifest builder (pure — unit-testable without a cluster)
# ---------------------------------------------------------------------------

_SECURITY_CONTEXT = {
    "allowPrivilegeEscalation": False,
    "readOnlyRootFilesystem": True,
    "runAsNonRoot": True,
    "runAsUser": 65532,
    "runAsGroup": 65532,
    "capabilities": {"drop": ["ALL"]},
}

_DEFAULT_REQUESTS = {"cpu": "100m", "memory": "256Mi"}


def _stage_resources(execution: StageExecution) -> Dict[str, Any]:
    limits = {
        "cpu": (execution.resources or {}).get("cpu") or _env("CI_STAGE_CPU_LIMIT", "2"),
        "memory": (execution.resources or {}).get("memory") or _env("CI_STAGE_MEMORY_LIMIT", "4Gi"),
        "ephemeral-storage": (execution.resources or {}).get("ephemeralStorage")
        or _env("CI_STAGE_EPHEMERAL_LIMIT", "2Gi"),
    }
    # The scheduler matches REQUESTS. A limit with no request makes Kubernetes
    # default the request to the limit, so the ephemeral-storage cap above would
    # silently demand its full size on every node — unschedulable on hosts with
    # small root disks. Request a modest floor explicitly and let the limit cap.
    requests = dict(_DEFAULT_REQUESTS)
    requests["ephemeral-storage"] = _env("CI_STAGE_EPHEMERAL_REQUEST", "256Mi")
    return {"requests": requests, "limits": limits}


def _mounts() -> List[Dict[str, str]]:
    return [
        {"name": "workspace", "mountPath": "/workspace"},
        {"name": "tmp", "mountPath": "/tmp"},
    ]


def _plain_env(execution: StageExecution, extra: Dict[str, str]) -> List[Dict[str, Any]]:
    env = {
        "HOME": "/tmp",
        "TMPDIR": "/tmp",
        "PYTHONDONTWRITEBYTECODE": "1",
        "KUBESIGHT_BUILD_ID": str(execution.build_id),
        "KUBESIGHT_BUILD_NUMBER": str(execution.build_number),
        "KUBESIGHT_SERVICE": execution.service_slug,
        "KUBESIGHT_BRANCH": execution.branch or "",
        "KUBESIGHT_COMMIT": execution.commit_sha or "",
        **(execution.env or {}),
        **extra,
    }
    return [{"name": key, "value": str(value)} for key, value in sorted(env.items())]


def _secret_env(secret_name: str, execution: StageExecution) -> List[Dict[str, Any]]:
    return [
        {
            "name": env_name,
            "valueFrom": {
                "secretKeyRef": {
                    "name": secret_name,
                    "key": _secret_key(execution.position, env_name),
                }
            },
        }
        for env_name in sorted(execution.secrets or {})
    ]


def build_job_resources(first: StageExecution) -> List[Dict[str, Any]]:
    """Secret + NetworkPolicy + Job for one build. ``first.plan`` is required."""
    plan = first.plan or []
    if not plan:
        raise RunnerError("The Kubernetes runner needs the full build plan.")

    namespace = _namespace()
    job_name = job_name_for(first)
    secret_name = f"{job_name}-secrets"
    callback_url = first.callback_url
    labels = {
        "app.kubernetes.io/name": "kubesight-ci",
        "kubesight.io/build-id": str(first.build_id),
        "kubesight.io/service": _dns(first.service_slug, 63),
    }

    # -- Per-build secret: every stage's secret env + callback token + registry
    secret_data: Dict[str, str] = {"callback-token": _b64(first.callback_token)}
    for execution in plan:
        for env_name, value in (execution.secrets or {}).items():
            secret_data[_secret_key(execution.position, env_name)] = _b64(value)

    docker_config_needed = False
    for execution in plan:
        if execution.stage_type == "container_image" and execution.registry:
            registry = execution.registry
            auth = _b64(f"{registry.get('username', '')}:{registry.get('password', '')}")
            docker_config = json.dumps({"auths": {registry["host"]: {"auth": auth}}})
            secret_data["docker-config"] = _b64(docker_config)
            docker_config_needed = True

    secret = {
        "apiVersion": "v1",
        "kind": "Secret",
        "metadata": {"name": secret_name, "namespace": namespace, "labels": labels},
        "type": "Opaque",
        "data": secret_data,
    }

    callback_env = [
        {"name": "KUBESIGHT_CALLBACK_URL", "value": callback_url},
        {
            "name": "KUBESIGHT_CALLBACK_TOKEN",
            "valueFrom": {"secretKeyRef": {"name": secret_name, "key": "callback-token"}},
        },
    ]

    # -- initContainers, one per stage, in pipeline order
    init_containers: List[Dict[str, Any]] = []
    cof_positions: List[int] = []
    artifact_specs: List[Dict[str, Any]] = []
    image_specs: List[Dict[str, Any]] = []

    for execution in plan:
        if execution.continue_on_failure:
            cof_positions.append(execution.position)
        for spec in execution.artifacts or []:
            if isinstance(spec, dict) and spec.get("path") and execution.stage_type != "container_image":
                artifact_specs.append(
                    {
                        **spec,
                        "workdir": execution.working_directory or "",
                        "stagePosition": execution.position,
                    }
                )

        base = {
            "name": _stage_container_name(execution.position),
            "imagePullPolicy": _env("CI_IMAGE_PULL_POLICY", "IfNotPresent"),
            "securityContext": dict(_SECURITY_CONTEXT),
            "volumeMounts": _mounts(),
            "resources": _stage_resources(execution),
        }

        if execution.stage_type == "checkout":
            container = {
                **base,
                "image": _worker_image(),
                "command": ["/bin/sh", "-c", _CHECKOUT_SCRIPT],
                "env": _plain_env(
                    execution,
                    {
                        "KUBESIGHT_REPO_URL": execution.repository_url or "",
                        "KUBESIGHT_REVISION": execution.commit_sha or execution.branch or "",
                    },
                )
                + callback_env
                + _secret_env(secret_name, execution),
            }
        elif execution.stage_type == "container_image":
            meta_file = f"/workspace/.kubesight/image-meta-{execution.position}.json"
            registry = execution.registry or {}
            image_specs.append(
                {
                    "stagePosition": execution.position,
                    "name": registry.get("repository", first.service_slug),
                    "uri": f"{registry.get('host','')}/{registry.get('repository','')}:{registry.get('tag','')}",
                }
            )
            container = {
                **base,
                "image": _env("CI_BUILDKIT_CLIENT_IMAGE", "moby/buildkit:v0.23.2"),
                "command": [
                    "/bin/sh",
                    "-c",
                    "mkdir -p /workspace/.kubesight && " + _buildctl_args(execution, meta_file),
                ],
                "env": _plain_env(execution, {"DOCKER_CONFIG": "/kubesight-docker"})
                + _secret_env(secret_name, execution),
                "volumeMounts": _mounts()
                + [{"name": "docker-config", "mountPath": "/kubesight-docker", "readOnly": True}],
            }
        else:  # command
            container = {
                **base,
                "image": execution.image or _env("CI_DEFAULT_STAGE_IMAGE", "debian:bookworm-slim"),
                "command": ["/bin/sh", "-c", _command_stage_script(execution)],
                "env": _plain_env(execution, {}) + _secret_env(secret_name, execution),
            }
        init_containers.append(container)

    # -- collector: the only main container; uploads artifacts, then the Job
    # completes. Its failure fails the Job — a build must not pass with its
    # declared outputs missing.
    collector = {
        "name": "collector",
        "image": _worker_image(),
        "imagePullPolicy": _env("CI_IMAGE_PULL_POLICY", "IfNotPresent"),
        "command": ["python3", "-c", _COLLECTOR_SCRIPT],
        "env": [
            {"name": "HOME", "value": "/tmp"},
            {"name": "TMPDIR", "value": "/tmp"},
            {"name": "KUBESIGHT_BUILD_ID", "value": str(first.build_id)},
            {"name": "KUBESIGHT_ARTIFACTS", "value": json.dumps(artifact_specs)},
            {"name": "KUBESIGHT_IMAGES", "value": json.dumps(image_specs)},
            {
                "name": "KUBESIGHT_MAX_ARTIFACT_BYTES",
                "value": str(int(_env("CI_MAX_ARTIFACT_MB", "512")) * 1024 * 1024),
            },
        ]
        + callback_env,
        "securityContext": dict(_SECURITY_CONTEXT),
        "volumeMounts": _mounts(),
        "resources": {"requests": dict(_DEFAULT_REQUESTS), "limits": {"cpu": "1", "memory": "1Gi"}},
    }

    volumes = [
        {"name": "workspace", "emptyDir": {"sizeLimit": _env("CI_WORKSPACE_SIZE_LIMIT", "5Gi")}},
        {"name": "tmp", "emptyDir": {"sizeLimit": "512Mi"}},
    ]
    if docker_config_needed:
        volumes.append(
            {
                "name": "docker-config",
                "secret": {
                    "secretName": secret_name,
                    "items": [{"key": "docker-config", "path": "config.json"}],
                },
            }
        )

    total_timeout = sum(int(execution.timeout_seconds or 1800) for execution in plan) + 900
    host_aliases = _merged_host_aliases(plan)

    job = {
        "apiVersion": "batch/v1",
        "kind": "Job",
        "metadata": {"name": job_name, "namespace": namespace, "labels": labels},
        "spec": {
            # No pod-level retry: a retried pod would re-run completed stages.
            # Retrying a build is a KubeSight action that makes a new build.
            "backoffLimit": 0,
            "activeDeadlineSeconds": total_timeout,
            "ttlSecondsAfterFinished": int(_env("CI_JOB_TTL_SECONDS", "1800")),
            "template": {
                "metadata": {
                    "labels": labels,
                    "annotations": {
                        _COF_ANNOTATION: ",".join(str(p) for p in cof_positions)
                    },
                },
                "spec": {
                    "restartPolicy": "Never",
                    "serviceAccountName": _env("CI_SERVICE_ACCOUNT", "kubesight-ci-build"),
                    "automountServiceAccountToken": False,
                    "enableServiceLinks": False,
                    # Kubelet writes these into /etc/hosts before any container
                    # starts, so a stage's commands never have to patch a file
                    # they cannot write (the root filesystem is read-only).
                    **({"hostAliases": host_aliases} if host_aliases else {}),
                    "securityContext": {
                        "runAsNonRoot": True,
                        "seccompProfile": {"type": "RuntimeDefault"},
                    },
                    "volumes": volumes,
                    "initContainers": init_containers,
                    "containers": [collector],
                },
            },
        },
    }

    network_policy = _network_policy(job_name, namespace, labels, plan)
    return [secret, network_policy, job]


def _merged_host_aliases(plan: List[StageExecution]) -> List[Dict[str, Any]]:
    """Every stage's host aliases, as ONE pod-level ``hostAliases`` list.

    Kubernetes writes /etc/hosts per POD, and a build is one pod whose stages
    are initContainers, so aliases cannot be scoped to a single stage: entries
    from every stage are merged and all stages resolve all of them. The editor
    says so. Merging by IP keeps the spec readable and the ordering stable.
    """
    merged: List[Dict[str, Any]] = []
    by_ip: Dict[str, Dict[str, Any]] = {}
    for execution in plan:
        for alias in execution.host_aliases or []:
            ip = str((alias or {}).get("ip") or "").strip()
            hostnames = [
                str(name).strip()
                for name in (alias or {}).get("hostnames") or []
                if str(name).strip()
            ]
            if not ip or not hostnames:
                continue
            entry = by_ip.get(ip)
            if entry is None:
                entry = {"ip": ip, "hostnames": []}
                by_ip[ip] = entry
                merged.append(entry)
            for name in hostnames:
                if name not in entry["hostnames"]:
                    entry["hostnames"].append(name)
    return merged


def _extra_egress_ports() -> List[int]:
    """Operator-declared TCP ports build pods may reach, from
    ``CI_EXTRA_EGRESS_PORTS`` (comma-separated). Unparseable entries are
    ignored rather than failing a build over a typo in configuration."""
    ports: List[int] = []
    for chunk in os.getenv("CI_EXTRA_EGRESS_PORTS", "").split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        try:
            port = int(chunk)
        except ValueError:
            logger.warning("Ignoring non-numeric CI_EXTRA_EGRESS_PORTS entry %r", chunk)
            continue
        if 1 <= port <= 65535 and port not in ports:
            ports.append(port)
    return ports


def _network_policy(
    job_name: str, namespace: str, labels: Dict[str, str], plan: List[StageExecution]
) -> Dict[str, Any]:
    egress: List[Dict[str, Any]] = [
        {  # DNS
            "to": [
                {
                    "namespaceSelector": {
                        "matchLabels": {"kubernetes.io/metadata.name": "kube-system"}
                    }
                }
            ],
            "ports": [{"protocol": "UDP", "port": 53}, {"protocol": "TCP", "port": 53}],
        },
        {  # Callback to the backend
            "to": [
                {
                    "namespaceSelector": {
                        "matchLabels": {
                            "kubernetes.io/metadata.name": _env("CI_BACKEND_NAMESPACE", "kubesight")
                        }
                    }
                }
            ],
            "ports": [{"protocol": "TCP", "port": int(_env("CI_BACKEND_PORT", "5000"))}],
        },
        {  # Git host + package registries. Production hardening: replace with a
           # controlled egress proxy or CNI FQDN policy, as Application
           # Intelligence documents for its own workers.
            "ports": [{"protocol": "TCP", "port": 443}]
        },
    ]
    addr = buildkit_addr()
    if addr:
        port_match = re.search(r":(\d+)$", addr)
        egress.append(
            {
                "to": [
                    {
                        "namespaceSelector": {
                            "matchLabels": {
                                "kubernetes.io/metadata.name": _env(
                                    "CI_BUILDKIT_NAMESPACE", "kubesight-buildkit"
                                )
                            }
                        }
                    }
                ],
                "ports": [
                    {"protocol": "TCP", "port": int(port_match.group(1)) if port_match else 1234}
                ],
            }
        )
    for execution in plan:
        registry = execution.registry or {}
        port = registry.get("port")
        if port and port != 443:
            egress.append({"ports": [{"protocol": "TCP", "port": int(port)}]})
    # Dependency repositories on non-standard ports: a self-hosted Nexus often
    # serves Maven/npm on its own port, distinct from the container registry's.
    # Nothing else can infer them, and a blocked port fails as a connect
    # TIMEOUT deep inside the build tool rather than anything obviously
    # network-shaped, so make them declarable.
    for port in _extra_egress_ports():
        egress.append({"ports": [{"protocol": "TCP", "port": port}]})

    return {
        "apiVersion": "networking.k8s.io/v1",
        "kind": "NetworkPolicy",
        "metadata": {"name": job_name, "namespace": namespace, "labels": labels},
        "spec": {
            "podSelector": {"matchLabels": {"kubesight.io/build-id": labels["kubesight.io/build-id"]}},
            "policyTypes": ["Ingress", "Egress"],
            "ingress": [],
            "egress": egress,
        },
    }


# ---------------------------------------------------------------------------
# Adapter
# ---------------------------------------------------------------------------

class KubernetesJobRunnerAdapter:
    """Drives one Job per build over kubectl. See the module docstring."""

    runner_type = "kubernetes"

    # -- capabilities --------------------------------------------------------

    def supported_stage_types(self) -> set:
        supported = {"checkout", "command"}
        if buildkit_addr():
            supported.add("container_image")
        return supported

    def skip_reason(self, stage_type: str) -> Optional[str]:
        if stage_type == "container_image" and not buildkit_addr():
            return (
                "Container image builds need the BuildKit service "
                "(deploy k8s/ci-buildkitd.yaml and set CI_BUILDKIT_ADDR)."
            )
        return None

    def can_run(self, requirements: StageRequirements) -> bool:
        return requirements.runner_type in (None, self.runner_type)

    # -- lifecycle -----------------------------------------------------------

    def start(self, execution: StageExecution) -> RunnerHandle:
        job_name = job_name_for(execution)
        ref = f"{job_name}#{_stage_container_name(execution.position)}"
        if execution.plan:
            self._create_job(execution)
        # Later stages: the Job is already running their container in order —
        # starting them is just attaching to the right container.
        return RunnerHandle(runner_id=0, external_ref=ref)

    def _create_job(self, execution: StageExecution) -> None:
        resources = build_job_resources(execution)
        manifest = json.dumps({"apiVersion": "v1", "kind": "List", "items": resources})
        rc, _, stderr = _kubectl(["apply", "-f", "-"], input_text=manifest, timeout=60)
        if rc != 0:
            logger.error("CI job apply failed: %s", stderr[-2000:])
            raise RunnerError("The build job could not be scheduled on the cluster.")
        self._attach_owner_refs(resources)

    def _attach_owner_refs(self, resources: List[Dict[str, Any]]) -> None:
        """Point the Secret and NetworkPolicy at the Job so Kubernetes GC
        removes them when the TTL controller deletes the finished Job."""
        job = next(item for item in resources if item["kind"] == "Job")
        namespace = job["metadata"]["namespace"]
        rc, uid, _ = _kubectl(
            ["get", "job", job["metadata"]["name"], "-n", namespace, "-o", "jsonpath={.metadata.uid}"],
            timeout=15,
        )
        uid = uid.strip()
        if rc != 0 or not uid:
            return
        patch = json.dumps(
            {
                "metadata": {
                    "ownerReferences": [
                        {
                            "apiVersion": "batch/v1",
                            "kind": "Job",
                            "name": job["metadata"]["name"],
                            "uid": uid,
                            "controller": True,
                            "blockOwnerDeletion": True,
                        }
                    ]
                }
            }
        )
        for item in resources:
            if item["kind"] == "Job":
                continue
            _kubectl(
                [
                    "patch", item["kind"].lower(), item["metadata"]["name"],
                    "-n", namespace, "--type=merge", "-p", patch,
                ],
                timeout=15,
            )

    # -- observation ---------------------------------------------------------

    def _read_job_and_pod(self, job_name: str) -> Tuple[Optional[dict], Optional[dict]]:
        namespace = _namespace()
        rc, out, _ = _kubectl(["get", "job", job_name, "-n", namespace, "-o", "json"], timeout=20)
        job = json.loads(out) if rc == 0 and out.strip() else None
        rc, out, _ = _kubectl(
            ["get", "pods", "-n", namespace, "-l", f"job-name={job_name}", "-o", "json"], timeout=20
        )
        pod = None
        if rc == 0 and out.strip():
            items = json.loads(out).get("items") or []
            if items:
                pod = sorted(items, key=lambda p: p["metadata"].get("creationTimestamp") or "")[-1]
        return job, pod

    def poll(self, handle: RunnerHandle) -> str:
        job_name, container = _split_ref(handle.external_ref)
        job, pod = self._read_job_and_pod(job_name)
        if job is None:
            return FAILED  # Deleted out from under us — the reaper's case.

        job_status = job.get("status") or {}
        deadline_exceeded = any(
            (c.get("type") == "Failed" and c.get("reason") == "DeadlineExceeded")
            for c in job_status.get("conditions") or []
        )

        if pod is None:
            if deadline_exceeded:
                return TIMEOUT
            if int(job_status.get("failed") or 0) > 0:
                return FAILED
            return QUEUED  # Pod not scheduled yet.

        status = self._container_status(pod, container)
        if status is None:
            return QUEUED

        terminated = (status.get("state") or {}).get("terminated")
        if terminated is not None:
            if int(terminated.get("exitCode") or 0) != 0:
                return TIMEOUT if deadline_exceeded else FAILED
            # Exit 0 — but a continue-on-failure wrapper hides the real code in
            # a log marker, and the LAST stage must also wait for the collector.
            if self._cof_failed(pod, job_name, container):
                return FAILED
            if self._is_last_stage(pod, container):
                if int(job_status.get("succeeded") or 0) > 0:
                    return SUCCEEDED
                if deadline_exceeded:
                    return TIMEOUT
                if int(job_status.get("failed") or 0) > 0:
                    return FAILED  # Collector failed: outputs are missing.
                return RUNNING  # Collector still uploading.
            return SUCCEEDED

        if "running" in (status.get("state") or {}):
            return TIMEOUT if deadline_exceeded else RUNNING
        if deadline_exceeded:
            return TIMEOUT
        if int(job_status.get("failed") or 0) > 0:
            return FAILED  # An earlier container failed; this one never ran.
        return QUEUED

    def _container_status(self, pod: dict, container: str) -> Optional[dict]:
        for status in (pod.get("status") or {}).get("initContainerStatuses") or []:
            if status.get("name") == container:
                return status
        return None

    def _is_last_stage(self, pod: dict, container: str) -> bool:
        init = (pod.get("spec") or {}).get("initContainers") or []
        return bool(init) and init[-1].get("name") == container

    def _cof_failed(self, pod: dict, job_name: str, container: str) -> bool:
        annotations = (pod.get("metadata") or {}).get("annotations") or {}
        cof = {p for p in (annotations.get(_COF_ANNOTATION) or "").split(",") if p}
        position = container.rsplit("-", 1)[-1]
        if position not in cof:
            return False
        rc, out, _ = _kubectl(
            [
                "logs", f"job/{job_name}", "-c", container, "-n", _namespace(),
                "--tail", "5",
            ],
            timeout=20,
        )
        if rc != 0:
            return False
        for line in out.splitlines():
            if line.startswith(_EXIT_MARKER):
                code = line.replace(_EXIT_MARKER, "").strip()
                return code not in ("", "0")
        return False

    def drain_logs(self, handle: RunnerHandle, after_seq: int) -> Iterator[LogChunk]:
        job_name, container = _split_ref(handle.external_ref)
        lines = self._container_log_lines(job_name, container)
        # The collector's output belongs to the last stage's log — it is the
        # only window into artifact upload problems.
        if lines is not None:
            _, pod = self._read_job_and_pod(job_name)
            if pod is not None and self._is_last_stage(pod, container):
                status = self._container_status(pod, container)
                if status and (status.get("state") or {}).get("terminated"):
                    collector_lines = self._container_log_lines(job_name, "collector") or []
                    lines = lines + [f"[collector] {line}" for line in collector_lines]
        for index, content in enumerate(lines or [], start=1):
            if index > after_seq:
                yield LogChunk(seq=index, content=content)

    def _container_log_lines(self, job_name: str, container: str) -> Optional[List[str]]:
        rc, out, stderr = _kubectl(
            [
                "logs", f"job/{job_name}", "-c", container, "-n", _namespace(),
                "--limit-bytes", "2000000",
            ],
            timeout=30,
        )
        if rc != 0:
            return None  # Container is still waiting; no logs yet.
        return out.splitlines()

    def collect_artifacts(self, handle: RunnerHandle) -> List[ArtifactRef]:
        # Artifacts arrive through the worker callback (the collector container
        # uploads them); there is nothing to pull from here.
        return []

    def cancel(self, handle: RunnerHandle) -> None:
        job_name, _ = _split_ref(handle.external_ref)
        _kubectl(
            [
                "delete", "job", job_name, "-n", _namespace(),
                "--ignore-not-found=true", "--wait=false",
            ],
            timeout=30,
        )

    def cleanup(self, handle: RunnerHandle) -> None:
        # Per-stage cleanup must NOT delete the shared Job mid-build. The Job's
        # TTL removes it after completion, and ownerReferences GC the Secret and
        # NetworkPolicy with it; cancel() handles the explicit path.
        return None
