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
    SKIPPED,
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
# Emitted by a stage that declined to run because an earlier one failed.
_SKIP_MARKER = "[kubesight-skip]"


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
# One shallow clone of the ref being built, rather than cloning the default
# branch and then fetching. --branch takes a tag as happily as a branch; a
# pinned commit sha is the case it cannot express, so that still falls back.
if [ -n "${KUBESIGHT_REVISION:-}" ] &&    git clone --no-tags --depth 1 --branch "$KUBESIGHT_REVISION"        "$KUBESIGHT_REPO_URL" /workspace/source 2>/dev/null; then
  cd /workspace/source
  echo "Checked out $KUBESIGHT_REVISION"
else
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
if [ "${KUBESIGHT_RESTORE:-0}" = "1" ]; then
python3 - <<'PYEOF'
# Rerun-from-a-stage: put the earlier build's artifacts back where they were,
# so the stages being skipped do not have to run again. Restoring is the whole
# point of the rerun, so unlike the commit report a failure here is fatal.
import json, os, sys, urllib.request

base = os.environ["KUBESIGHT_CALLBACK_URL"].rstrip("/") + "/builds/" + os.environ["KUBESIGHT_BUILD_ID"] + "/restore"
headers = {"Authorization": "Bearer " + os.environ["KUBESIGHT_CALLBACK_TOKEN"]}
listing = json.load(urllib.request.urlopen(urllib.request.Request(base, headers=headers), timeout=60))
items = (listing.get("data") or {}).get("items") or []
if not items:
    print("[kubesight] nothing to restore from the previous build.")
for item in items:
    rel = (item.get("sourcePath") or item.get("name") or "").lstrip("/")
    if not rel or ".." in rel.split("/"):
        print("[kubesight] skipped an artifact with an unusable path:", rel)
        continue
    target = os.path.join("/workspace/source", rel)
    os.makedirs(os.path.dirname(target) or "/workspace/source", exist_ok=True)
    req = urllib.request.Request(base + "/" + str(item["id"]), headers=headers)
    with urllib.request.urlopen(req, timeout=600) as response, open(target, "wb") as sink:
        while True:
            chunk = response.read(1024 * 1024)
            if not chunk:
                break
            sink.write(chunk)
    print("[kubesight] restored", rel, "(%d bytes)" % os.path.getsize(target))
PYEOF
fi
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
        # Where the file sat in the workspace. Recorded so a later build can
        # restore it to the same place instead of re-running the stage that
        # produced it.
        "sourcePath": os.path.relpath(path, "/workspace/source"),
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


_FAIL_FLAG = "/workspace/.kubesight/failed"


def _wrap_stage_script(body: str, *, continue_on_failure: bool) -> str:
    """Every stage exits 0 and reports its real code as a log marker.

    Kubernetes only starts a pod's main containers once EVERY initContainer has
    succeeded — so a stage that exits non-zero means the collector never runs
    and the artifacts of the stages that DID succeed are lost, exactly when
    they are most wanted. Exiting 0 keeps the pod walking to the collector.

    Sequential semantics are preserved by a flag on the shared workspace: the
    first failure writes it, and every later stage sees it and skips itself
    instead of running against a broken tree. The adapter turns the markers
    back into real per-stage statuses, so nothing reports success it did not
    earn. A continue-on-failure stage records its failure without writing the
    flag — that is what makes it "continue".
    """
    guard = (
        f'KS_FLAG={_FAIL_FLAG}\n'
        'mkdir -p /workspace/.kubesight 2>/dev/null || true\n'
        'if [ -e "$KS_FLAG" ]; then\n'
        '  echo "[kubesight] Skipped: an earlier stage failed."\n'
        f'  echo "{_SKIP_MARKER}"\n'
        "  exit 0\n"
        "fi\n"
    )
    record = "" if continue_on_failure else 'if [ "$EC" -ne 0 ]; then : > "$KS_FLAG"; fi\n'
    return (
        "set -u\nexport HOME=/tmp TMPDIR=/tmp\n"
        + guard
        + f"(\nset -e\n{body}\n)\nEC=$?\n"
        + record
        + f'echo "{_EXIT_MARKER} $EC"\nexit 0\n'
    )


def _command_stage_script(execution: StageExecution) -> str:
    workdir = "/workspace/source"
    if execution.working_directory:
        workdir = f"/workspace/source/{execution.working_directory}"
    commands = "\n".join(execution.commands or ["true"])
    return _wrap_stage_script(
        f"cd {workdir}\n{commands}",
        continue_on_failure=bool(execution.continue_on_failure),
    )


INLINE_DOCKERFILE_DIR = "/kubesight-dockerfile"


def _buildctl_output(registry: Dict[str, Any], image_ref: str) -> str:
    """Where buildkitd sends the finished image: straight to the registry."""
    output = f"type=image,name={image_ref},push=true"
    if registry.get("verifyTls") is False:
        output += ",registry.insecure=true"
    return output


def _buildctl_add_hosts(execution: StageExecution) -> str:
    """Host aliases for the RUN steps inside the image build.

    Pod-level hostAliases cannot help here: they apply to the build pod, while
    the Dockerfile's RUN steps execute inside buildkitd, in another pod. This
    passes the same mappings to the frontend so a RUN that reaches an internal
    host resolves it.

    It does NOT affect where the image is pulled from or pushed to — buildkitd
    resolves the registry host itself, before any frontend option applies. That
    is an operator concern: either address the registry by IP in its connection,
    or give the buildkitd Deployment its own hostAliases.
    """
    pairs = []
    for alias in execution.host_aliases or []:
        if not isinstance(alias, dict):
            continue
        ip = str(alias.get("ip") or "").strip()
        names = alias.get("hostnames")
        if not ip or not isinstance(names, (list, tuple)):
            continue
        for name in names:
            name = str(name).strip()
            if name:
                pairs.append(f"{name}={ip}")
    return f"--opt add-hosts={','.join(pairs)} " if pairs else ""


def _buildctl_cache(registry: Dict[str, Any]) -> str:
    """Layer cache kept in the registry beside the image.

    buildkitd's own cache is an emptyDir: warm while that pod lives, gone when
    it restarts, and invisible to a second builder. Pushing the cache to a
    ``:buildcache`` tag makes it survive both. Off unless asked for, because it
    writes an extra tag into somebody's registry.
    """
    if _env("CI_BUILDKIT_REGISTRY_CACHE", "0") not in ("1", "true", "yes"):
        return ""
    ref = f"{registry['host']}/{registry['repository']}:buildcache"
    insecure = ",registry.insecure=true" if registry.get("verifyTls") is False else ""
    return (
        f"--import-cache type=registry,ref={ref}{insecure} "
        # mode=max caches intermediate layers too, which is what makes a
        # dependency-heavy build cheap on the second run.
        f"--export-cache type=registry,ref={ref},mode=max{insecure} "
    )


def _buildctl_args(execution: StageExecution, meta_file: str) -> str:
    registry = execution.registry or {}
    image_ref = f"{registry['host']}/{registry['repository']}:{registry['tag']}"
    context = "/workspace/source"
    if execution.working_directory:
        context = f"/workspace/source/{execution.working_directory}"
    dockerfile = registry.get("dockerfile") or "Dockerfile"
    dockerfile_dir = os.path.dirname(dockerfile) or "."
    if registry.get("dockerfileContent"):
        # buildctl takes the context and the Dockerfile as SEPARATE locals, so
        # an inline Dockerfile needs no copy into the context: point the
        # dockerfile local at the mounted file and leave the context alone.
        return (
            f"buildctl --addr {buildkit_addr()} build "
            f"--frontend dockerfile.v0 "
            f"--local context={context} "
            f"--local dockerfile={INLINE_DOCKERFILE_DIR} "
            f"--opt filename=Dockerfile "
            f"{_buildctl_add_hosts(execution)}"
            f"{_buildctl_cache(registry)}"
            f"--output {_buildctl_output(registry, image_ref)} "
            f"--metadata-file {meta_file}"
        )
    return (
        f"buildctl --addr {buildkit_addr()} build "
        f"--frontend dockerfile.v0 "
        f"--local context={context} "
        f"--local dockerfile={context}/{dockerfile_dir} "
        f"--opt filename={os.path.basename(dockerfile)} "
        f"{_buildctl_add_hosts(execution)}"
        f"{_buildctl_cache(registry)}"
        f"--output {_buildctl_output(registry, image_ref)} "
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



# ---------------------------------------------------------------------------
# Dependency cache
#
# Without one, every build re-downloads its whole dependency graph: a Gradle or
# Maven project spends minutes doing it, on a workspace that is then deleted.
# The cache is a per-SERVICE PersistentVolumeClaim mounted at /cache, so a
# service's builds warm each other's while different services stay isolated.
#
# Per-service and ReadWriteOnce on purpose: build tools lock their cache
# directory, and lock semantics over shared network storage are exactly where
# they misbehave. A service's own builds are serialised by its
# maxConcurrentBuilds, so one pod holds the volume at a time.
#
# Off unless CI_CACHE_STORAGE_CLASS names a class: nothing should start
# demanding storage on a cluster that has none to give.
# ---------------------------------------------------------------------------

CACHE_MOUNT_PATH = "/cache"


def cache_storage_class() -> str:
    return os.getenv("CI_CACHE_STORAGE_CLASS", "").strip()


def cache_claim_name(service_slug: str) -> str:
    return f"ci-cache-{_dns(service_slug, 50)}"


def cache_claim(service_slug: str) -> Dict[str, Any]:
    """The per-service cache PVC. Applied separately from the Job and never
    given an ownerReference — it must outlive the build that created it."""
    return {
        "apiVersion": "v1",
        "kind": "PersistentVolumeClaim",
        "metadata": {
            "name": cache_claim_name(service_slug),
            "namespace": _namespace(),
            "labels": {
                "app.kubernetes.io/name": "kubesight-ci",
                "kubesight.io/cache-for": _dns(service_slug, 63),
            },
        },
        "spec": {
            "accessModes": ["ReadWriteOnce"],
            "storageClassName": cache_storage_class(),
            "resources": {"requests": {"storage": _env("CI_CACHE_SIZE", "10Gi")}},
        },
    }


def _mounts() -> List[Dict[str, str]]:
    mounts = [
        {"name": "workspace", "mountPath": "/workspace"},
        {"name": "tmp", "mountPath": "/tmp"},
    ]
    if cache_storage_class():
        mounts.append({"name": "cache", "mountPath": CACHE_MOUNT_PATH})
    return mounts


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
        # Empty when no cache volume is configured, so a pipeline can use it
        # unconditionally and simply get a cold build where there is none.
        "KUBESIGHT_CACHE": CACHE_MOUNT_PATH if cache_storage_class() else "",
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
    inline_dockerfile = ""
    for execution in plan:
        if execution.stage_type == "container_image" and execution.registry:
            registry = execution.registry
            auth = _b64(f"{registry.get('username', '')}:{registry.get('password', '')}")
            docker_config = json.dumps({"auths": {registry["host"]: {"auth": auth}}})
            secret_data["docker-config"] = _b64(docker_config)
            docker_config_needed = True
            # An inline Dockerfile rides in the per-build Secret and is mounted
            # read-only beside the build context. It is NOT written into the
            # workspace: the checkout stays exactly as the repository has it, so
            # building never mutates the source a later stage might read.
            if registry.get("dockerfileContent"):
                inline_dockerfile = registry["dockerfileContent"]
                secret_data["inline-dockerfile"] = _b64(inline_dockerfile)

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
                "command": [
                    "/bin/sh",
                    "-c",
                    _wrap_stage_script(_CHECKOUT_SCRIPT, continue_on_failure=False),
                ],
                "env": _plain_env(
                    execution,
                    {
                        "KUBESIGHT_REPO_URL": execution.repository_url or "",
                        "KUBESIGHT_REVISION": execution.commit_sha or execution.branch or "",
                        # Set when this build reruns from a later stage: the
                        # checkout also restores the earlier build's artifacts.
                        "KUBESIGHT_RESTORE": "1" if execution.restore_artifacts else "0",
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
                    _wrap_stage_script(
                        "mkdir -p /workspace/.kubesight\n"
                        + _buildctl_args(execution, meta_file),
                        continue_on_failure=bool(execution.continue_on_failure),
                    ),
                ],
                "env": _plain_env(execution, {"DOCKER_CONFIG": "/kubesight-docker"})
                + _secret_env(secret_name, execution),
                "volumeMounts": _mounts()
                + [{"name": "docker-config", "mountPath": "/kubesight-docker", "readOnly": True}]
                + (
                    [
                        {
                            "name": "inline-dockerfile",
                            "mountPath": INLINE_DOCKERFILE_DIR,
                            "readOnly": True,
                        }
                    ]
                    if (execution.registry or {}).get("dockerfileContent")
                    else []
                ),
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
    if cache_storage_class():
        volumes.append(
            {
                "name": "cache",
                "persistentVolumeClaim": {"claimName": cache_claim_name(first.service_slug)},
            }
        )
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
    if inline_dockerfile:
        # Read-only, beside the context rather than inside it: the checkout is
        # left exactly as the repository has it.
        volumes.append(
            {
                "name": "inline-dockerfile",
                "secret": {
                    "secretName": secret_name,
                    "items": [{"key": "inline-dockerfile", "path": "Dockerfile"}],
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
            if not isinstance(alias, dict):
                continue  # never let a malformed snapshot break a whole build
            ip = str(alias.get("ip") or "").strip()
            raw_names = alias.get("hostnames")
            hostnames = [
                str(name).strip()
                for name in (raw_names if isinstance(raw_names, (list, tuple)) else [])
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
    # One Job per build: every stage's container exists from the start, and
    # the pod must be allowed to reach its collector even after a failure.
    runs_whole_build = True

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
        if cache_storage_class():
            self._ensure_cache_claim(execution.service_slug)
        resources = build_job_resources(execution)
        manifest = json.dumps({"apiVersion": "v1", "kind": "List", "items": resources})
        rc, _, stderr = _kubectl(["apply", "-f", "-"], input_text=manifest, timeout=60)
        if rc != 0:
            logger.error("CI job apply failed: %s", stderr[-2000:])
            raise RunnerError("The build job could not be scheduled on the cluster.")
        self._attach_owner_refs(resources)

    def _ensure_cache_claim(self, service_slug: str) -> None:
        """Create the service's cache volume once, and only once.

        Create-if-missing rather than apply: a bound PVC has immutable fields,
        so re-applying it on every build would start failing the moment the
        configured size changed.

        A claim that cannot be created is fatal for this build, deliberately.
        The Job below mounts it by name, so continuing would leave a pod Pending
        until its deadline with nothing explaining why — a clear failure now is
        kinder than a silent twenty-minute one later.
        """
        name = cache_claim_name(service_slug)
        rc, _, _ = _kubectl(
            ["get", "pvc", name, "-n", _namespace(), "-o", "name"], timeout=20
        )
        if rc == 0:
            return
        rc, _, stderr = _kubectl(
            ["apply", "-f", "-"], input_text=json.dumps(cache_claim(service_slug)), timeout=30
        )
        if rc != 0:
            detail = (stderr or "").strip().splitlines()
            logger.error("CI cache claim failed: %s", (stderr or "")[-2000:])
            raise RunnerError(
                "The build cache volume could not be created: "
                + (detail[-1] if detail else "unknown error")
                + " — clear CI_CACHE_STORAGE_CLASS to build without a cache."
            )

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

    # -- workspace inspection ------------------------------------------------

    def list_workspace(self, handle: RunnerHandle, path: str) -> List[Dict[str, Any]]:
        """One directory of the live build workspace: names, sizes, types.

        Deliberately a listing and not a reader. A workspace routinely holds
        credentials a stage wrote for its own use (a gradle.properties, a
        kubeconfig), so serving file CONTENT through the API would turn "view
        the build" into "read the build's secrets". Names and sizes answer the
        question this exists for — did the previous stage produce the file the
        next one expects, and is it empty?

        Only works while a container of this build is running; kubectl exec has
        nothing to attach to otherwise, and the emptyDir is gone once the pod is
        removed. The caller turns that into an explanation.
        """
        job_name, container = _split_ref(handle.external_ref)
        _, pod = self._read_job_and_pod(job_name)
        if pod is None:
            raise RunnerError("The build pod is no longer there.")
        pod_name = pod["metadata"]["name"]

        # Portable across the images stages run on (debian, alpine, distroless
        # is excluded by needing a shell at all): no find -printf, no stat.
        # Field 5 of `ls -ldn` is the size on both GNU coreutils and busybox.
        script = (
            f"cd '{path}' 2>/dev/null || {{ echo '__KS_NO_PATH__'; exit 0; }}\n"
            "for e in * .*; do\n"
            '  [ "$e" = "." ] && continue\n'
            '  [ "$e" = ".." ] && continue\n'
            '  [ -e "$e" ] || continue\n'
            '  if [ -d "$e" ]; then t=dir; else t=file; fi\n'
            '  set -- $(ls -ldn "$e" 2>/dev/null)\n'
            '  printf "%s\\t%s\\t%s\\n" "$t" "${5:-0}" "$e"\n'
            "done\n"
        )
        rc, out, err = _kubectl(
            ["exec", pod_name, "-n", _namespace(), "-c", container, "--", "/bin/sh", "-c", script],
            timeout=20,
        )
        if rc != 0:
            detail = (err or "").strip().splitlines()
            hint = detail[-1] if detail else "kubectl exec failed."
            raise RunnerError(hint)
        if "__KS_NO_PATH__" in out:
            raise RunnerError(f"{path} does not exist in the workspace.")

        entries: List[Dict[str, Any]] = []
        for line in out.splitlines():
            parts = line.split("\t")
            if len(parts) != 3:
                continue
            kind, size, name = parts
            try:
                size_bytes = int(size)
            except ValueError:
                size_bytes = 0
            entries.append({"name": name, "type": kind, "size": size_bytes})
        entries.sort(key=lambda item: (item["type"] != "dir", item["name"].lower()))
        return entries

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
            # Exit 0 is now what EVERY stage does, so the real outcome lives in
            # a log marker. Read it before deciding anything.
            marker = self._exit_marker(job_name, container)
            if marker == "skip":
                return SKIPPED
            if marker == "failed":
                # The last stage still has to wait for the collector, otherwise
                # a failed final stage would end the build before its artifacts
                # were uploaded — which is the whole point of exiting 0.
                if self._is_last_stage(pod, container) and not self._job_finished(job_status):
                    return RUNNING
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

    def _exit_marker(self, job_name: str, container: str) -> str:
        """What a stage's own log says about how it ended.

        Every stage exits 0 so the pod reaches the collector, so the container's
        exit code no longer carries the outcome — the marker does. Returns
        "skip", "failed", or "ok" (also when no marker is found, which is the
        pre-wrapper shape and means the exit code already told the truth).
        """
        rc, out, _ = _kubectl(
            [
                "logs", f"job/{job_name}", "-c", container, "-n", _namespace(),
                "--tail", "5",
            ],
            timeout=20,
        )
        if rc != 0:
            return "ok"
        for line in out.splitlines():
            if line.startswith(_SKIP_MARKER):
                return "skip"
            if line.startswith(_EXIT_MARKER):
                code = line.replace(_EXIT_MARKER, "").strip()
                return "ok" if code in ("", "0") else "failed"
        return "ok"

    @staticmethod
    def _job_finished(job_status: dict) -> bool:
        return bool(int(job_status.get("succeeded") or 0) or int(job_status.get("failed") or 0))

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
