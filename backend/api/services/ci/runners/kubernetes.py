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

Kubernetes runs initContainers strictly in order, which is sequential pipeline
semantics; every stage exits 0 and reports its real outcome as a log marker so
the pod always reaches its collector (see ``_wrap_stage_script``). A parallel
group is the exception: its members run side by side as native sidecars, with a
barrier container after them (see "Parallel groups" below). Per-stage status is
read from ``pod.status.initContainerStatuses`` and the markers; per-stage logs
from ``kubectl logs -c``.

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
import contextlib
import json
import logging
import os
import re
import shlex
import socket
import subprocess
import threading
import time
import zlib
from typing import Any, Dict, Iterator, List, Optional, Tuple

from .. import build_environments, build_inputs, cache_layout, code_scan, parallel_groups, scan_stage
from .. import resources as ci_resources
from .base import (
    CANCELLED,
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


# Blanking one of these variables cannot mean "no value": _env falls back to the
# default the moment it sees an empty string. A resource that should be left off
# the manifest entirely therefore needs a word for it.
_OFF = {"off", "none", "no", "0", "false", "unlimited"}


def _is_off(value: str) -> bool:
    return value.strip().lower() in _OFF


def _namespace() -> str:
    return _env("CI_KUBERNETES_NAMESPACE", "kubesight-ci")


def _worker_image() -> str:
    # The backend image: ships git + python3, which is all checkout/collect need.
    return _env("CI_WORKER_IMAGE", os.getenv("APPLICATION_ANALYSIS_WORKER_IMAGE", "kubesight-backend:latest"))


def worker_image() -> str:
    """Exposed for the cache maintenance jobs, which run the same image."""
    return _worker_image()


def buildkit_addr() -> str:
    """Where the shared rootless buildkitd listens. Empty = image builds off."""
    return os.getenv("CI_BUILDKIT_ADDR", "").strip()


# ---------------------------------------------------------------------------
# BuildKit failover
#
# One Service address is one point of failure that no readiness probe can see:
# buildkitd's probe dials 127.0.0.1, so a pod on a node whose pod network is
# broken (firewalld back after a reboot, a sick calico-node) stays Ready while
# every build pod on every OTHER node times out dialling it.
#
# So when CI_BUILDKIT_POOL names a headless Service over the buildkitd pods,
# each image stage gets every Ready builder's own address and uses the first
# one that answers FROM THAT BUILD POD. The list is resolved here, when the Job
# is created: the backend runs in-cluster, so a plain DNS lookup of a headless
# Service returns one address per Ready pod, with no RBAC and no parsing of
# nslookup output in a shell. CI_BUILDKIT_ADDR stays on the end of the list, so
# a builder that moved between Job creation and the stage is still reachable.
#
# The order is rotated by service, not shuffled: each service keeps landing on
# the same builder while it is healthy, and that builder's layer cache stays
# warm for it.
# ---------------------------------------------------------------------------

_buildkit_resolver = None


def set_buildkit_resolver(fn) -> None:
    """Test hook: ``fn(host: str) -> list[str]`` of IP addresses."""
    global _buildkit_resolver
    _buildkit_resolver = fn


def _resolve_host(host: str) -> List[str]:
    if _buildkit_resolver is not None:
        return list(_buildkit_resolver(host))
    infos = socket.getaddrinfo(host, None, type=socket.SOCK_STREAM)
    return [info[4][0] for info in infos]


def buildkit_pool_host() -> str:
    """Headless Service host over every buildkitd pod. Empty = no failover."""
    return os.getenv("CI_BUILDKIT_POOL", "").strip()


def buildkit_candidates(service_slug: str) -> List[str]:
    """Builder addresses an image stage tries, in order.

    Just ``[CI_BUILDKIT_ADDR]`` when no pool is configured or it resolves to
    nothing, which keeps the stage script exactly what it was before failover.
    """
    primary = buildkit_addr()
    host = buildkit_pool_host()
    if not primary or not host:
        return [primary] if primary else []
    port_match = re.search(r":(\d+)$", primary)
    port = port_match.group(1) if port_match else "1234"
    try:
        ips = sorted(set(_resolve_host(host)))
    except (OSError, UnicodeError) as exc:
        logger.warning("Could not resolve BuildKit pool %s (%s); using %s only", host, exc, primary)
        return [primary]
    if not ips:
        return [primary]
    start = zlib.crc32(str(service_slug or "").encode("utf-8")) % len(ips)
    ordered = ips[start:] + ips[:start]
    addrs = [f"tcp://[{ip}]:{port}" if ":" in ip else f"tcp://{ip}:{port}" for ip in ordered]
    return addrs + [primary]


def _buildkit_select_script(candidates: List[str]) -> str:
    """Shell that sets $KS_BUILDKIT_ADDR to the first builder that answers.

    ``debug workers`` is the cheapest call that proves the whole path: TCP
    through every NetworkPolicy, the gRPC handshake, and a daemon with a worker.
    ``timeout`` bounds it when present, because a dial into a black-holed node
    otherwise waits for as long as the kernel keeps retrying the SYN.
    """
    words = " ".join(_q(addr) for addr in candidates)
    return (
        'KS_BUILDKIT_ADDR=""\n'
        'KS_BK_LIMIT=""\n'
        "if command -v timeout >/dev/null 2>&1; then KS_BK_LIMIT=\"timeout 20\"; fi\n"
        f"for KS_BK_TRY in {words}; do\n"
        '  if $KS_BK_LIMIT buildctl --addr "$KS_BK_TRY" --timeout 10 debug workers >/dev/null 2>&1; then\n'
        '    KS_BUILDKIT_ADDR="$KS_BK_TRY"\n'
        "    break\n"
        "  fi\n"
        '  echo "[kubesight] BuildKit at $KS_BK_TRY did not answer from this node; trying the next builder" >&2\n'
        "done\n"
        'if [ -z "$KS_BUILDKIT_ADDR" ]; then\n'
        f'  echo "[kubesight] No BuildKit builder answered ({len(candidates)} tried). Check that the buildkitd pods are Ready and that this node can reach pods on theirs." >&2\n'
        "  exit 1\n"
        "fi\n"
        'echo "[kubesight] BuildKit: $KS_BUILDKIT_ADDR"\n'
    )


# ---------------------------------------------------------------------------
# kubectl transport (injectable so tests never need a cluster)
# ---------------------------------------------------------------------------

_kubectl_runner = None


def set_kubectl_runner(fn) -> None:
    """Test hook: ``fn(args: list[str], input_text: str|None) -> (rc, stdout, stderr)``."""
    global _kubectl_runner
    _kubectl_runner = fn


def kubectl(args: List[str], input_text: Optional[str] = None, timeout: int = 30):
    """The runner's own kubectl, for the cache operations in services/ci/cache.py.

    Public on purpose: those operations must go through the same transport a
    build does, so they honour K8S_KUBECONFIG and the test hook above rather
    than opening a second, differently-configured path to the cluster.
    """
    return _kubectl(args, input_text=input_text, timeout=timeout)


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
      # A webhook may carry an abbreviated SHA, which cannot be fetched as a
      # remote ref. The default-branch clone does not contain PR-only commits.
      # Fetch the source branch to obtain its objects, then resolve the PINNED
      # revision locally: the branch may have advanced since the webhook.
      if ! git rev-parse --verify --end-of-options "${KUBESIGHT_REVISION}^{commit}" >/dev/null 2>&1 &&
         [ -n "${KUBESIGHT_BRANCH:-}" ]; then
        echo "Fetching source branch $KUBESIGHT_BRANCH to resolve $KUBESIGHT_REVISION"
        git fetch --no-tags --depth 50 origin "refs/heads/$KUBESIGHT_BRANCH"
      fi
      if RESOLVED_COMMIT=$(git rev-parse --verify --end-of-options "${KUBESIGHT_REVISION}^{commit}" 2>/dev/null); then
        git checkout --quiet --detach "$RESOLVED_COMMIT"
      else
        echo "Cannot resolve requested commit $KUBESIGHT_REVISION in the fetched repository. Check that the commit is available on the source branch, or use its full SHA." >&2
        exit 1
      fi
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
echo "Checkout complete."
"""

_COLLECTOR_SCRIPT = r"""
import glob, io, json, os, shutil, sys, urllib.request, uuid

CALLBACK = os.environ["KUBESIGHT_CALLBACK_URL"].rstrip("/")
TOKEN = os.environ["KUBESIGHT_CALLBACK_TOKEN"]
BUILD_ID = os.environ["KUBESIGHT_BUILD_ID"]
MAX_BYTES = int(os.environ.get("KUBESIGHT_MAX_ARTIFACT_BYTES", str(512 * 1024 * 1024)))
specs = json.loads(os.environ.get("KUBESIGHT_ARTIFACTS", "[]"))
images = json.loads(os.environ.get("KUBESIGHT_IMAGES", "[]"))
failures = 0


def release_cache_slot():
    # Every stage of this build is done, so the cache slot it leased goes back
    # now rather than when the lease goes stale - another build of this service
    # may be waiting to land on slot 0. A failure here costs nothing but that.
    cache_dir = os.environ.get("KUBESIGHT_CACHE_DIR", "")
    if not cache_dir:
        return
    leases = os.path.join(cache_dir, ".leases")
    try:
        names = os.listdir(leases)
    except OSError:
        return
    for name in names:
        path = os.path.join(leases, name)
        try:
            with open(os.path.join(path, "owner")) as handle:
                owner = handle.read().strip()
        except OSError:
            continue
        if owner == BUILD_ID:
            shutil.rmtree(path, ignore_errors=True)
            print("[kubesight] released cache slot", name)


release_cache_slot()


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
        # Where the file sat in the workspace, so the artifact record says
        # what the pipeline actually matched and not just the glob.
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
# Written by a continue-on-failure stage that failed; read only by post-action
# cleanup containers (see _post_action_containers).
_SOFT_FAIL_FLAG = "/workspace/.kubesight/failed-continued"

# Values one stage hands the next. A stage appends ``NAME=value`` lines to
# $KUBESIGHT_ENV; every later stage sources the file before running, so a
# version read out of package.json in stage 3 is an ordinary variable in stage
# 7. This is what replaces a Jenkins ``script { version = sh(...) }`` binding,
# which only worked because every stage shared one Groovy interpreter.
_BUILD_ENV_FILE = "/workspace/.kubesight/build.env"

# Sourced by every stage, written by any. Guarded with [ -s ] rather than
# [ -f ] so an empty file left by a stage that exported nothing does not fail
# under `set -e`, and sourced BEFORE the stage's own commands so a stage can
# override an inherited value simply by assigning it.
_LOAD_BUILD_ENV = (
    f'export KUBESIGHT_ENV={_BUILD_ENV_FILE}\n'
    f'if [ -s "$KUBESIGHT_ENV" ]; then . "$KUBESIGHT_ENV"; fi\n'
)


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
    # A continue-on-failure stage still fails the BUILD, so it leaves a flag of
    # its own — one no stage reads, only the post-action cleanup containers,
    # which run "on failure" for it exactly as the engine would decide.
    record = (
        f'if [ "$EC" -ne 0 ]; then : > {_SOFT_FAIL_FLAG}; fi\n'
        if continue_on_failure
        else 'if [ "$EC" -ne 0 ]; then : > "$KS_FLAG"; fi\n'
    )
    return (
        "set -u\nexport HOME=/tmp TMPDIR=/tmp\n"
        + guard
        + _cache_prep()
        + f"(\nset -e\n{_LOAD_BUILD_ENV}{body}\n)\nEC=$?\n"
        + record
        + f'echo "{_EXIT_MARKER} $EC"\nexit 0\n'
    )


def _cache_prep() -> str:
    """Make this service's cache subtree exist, before the stage's own commands.

    Outside the ``set -e`` subshell on purpose: a cache that cannot be written
    is a slow build, not a failed one — it warns on stderr and the stage runs
    cold.

    On every stage rather than in one setup container, because the layout has to
    be right for whichever stage runs first, a build's stages are initContainers
    with no guaranteed predecessor, and ``mkdir -p`` over an existing tree costs
    milliseconds. It also quietly repairs a cache somebody emptied by hand.

    Expands nothing at manifest time: the script reads $KUBESIGHT_CACHE_DIR,
    which ``_plain_env`` has already set — to "" when caching is off, which
    makes the whole block a no-op.
    """
    return cache_layout.prep_script(
        gradle_init=not _is_off(_env("CI_CACHE_GRADLE_INIT", "1")),
        shared=cache_shared_tools() if cache_enabled() else (),
        slots=_int_env("CI_CACHE_SLOTS", cache_layout.DEFAULT_SLOTS),
        stale_seconds=_int_env("CI_CACHE_LEASE_STALE_SECONDS", cache_layout.DEFAULT_LEASE_STALE_SECONDS),
    )


def _int_env(name: str, default: int) -> int:
    try:
        return max(1, int(_env(name, str(default))))
    except ValueError:
        return default


def _node_modules_cached(execution: StageExecution) -> bool:
    """Whether this stage keeps its node_modules between builds.

    Only a stage that runs an install - later stages already find node_modules
    in the shared workspace - and only with the cache on, so a cache-off
    build's script carries none of it. ``CI_CACHE_NODE_MODULES=0`` switches it
    off for the installation; a stage's own ``KUBESIGHT_NODE_MODULES_CACHE=0``
    for that one stage.
    """
    return (
        cache_enabled()
        and not _is_off(_env("CI_CACHE_NODE_MODULES", "1"))
        and cache_layout.runs_node_install(execution.commands)
    )


def _node_modules_keep() -> int:
    """How many lockfiles' node_modules a service keeps; the oldest-used go."""
    try:
        return max(1, int(_env("CI_CACHE_NODE_MODULES_KEEP", str(cache_layout.NODE_MODULES_KEEP))))
    except ValueError:
        return cache_layout.NODE_MODULES_KEEP


def _command_stage_body(execution: StageExecution) -> str:
    """A command (or scan) stage's own shell, before any wrapper."""
    workdir = "/workspace/source"
    if execution.working_directory:
        workdir = f"/workspace/source/{execution.working_directory}"
    commands = "\n".join(execution.commands or ["true"])
    if _node_modules_cached(execution):
        commands = cache_layout.node_modules_wrap(
            commands,
            image=execution.image or "",
            workdir=execution.working_directory or "",
            keep=_node_modules_keep(),
            stage=execution.stage_name or "",
        )
    return f"cd {_q(workdir)}\n{commands}"


def _command_stage_script(execution: StageExecution) -> str:
    return _wrap_stage_script(
        _command_stage_body(execution),
        continue_on_failure=bool(execution.continue_on_failure),
    )


# ---------------------------------------------------------------------------
# Parallel groups — members side by side in the build's one pod
#
# A pod runs its initContainers strictly one after another, which is what made
# the pipeline sequential. A NATIVE SIDECAR — an initContainer with
# ``restartPolicy: Always`` — is the exception: kubelet starts it in order, but
# moves on to the next initContainer as soon as it is running instead of
# waiting for it to exit. So a group's members are sidecars, started back to
# back and running together, and a plain initContainer after them — the
# BARRIER, in the worker image — is what waits:
#
#     stage-1 checkout            (initContainer)
#     stage-2 lint     ┐
#     stage-3 test     ├ sidecars: start together, run together
#     stage-4 sonar    ┘
#     barrier-2                   (initContainer: waits for 2, 3 and 4)
#     stage-5 image               (initContainer: after the group)
#     post-N …                    (post actions, owned by their own block)
#     collector                   (main container)
#
# Every member writes its result to ``done-<position>`` on the shared
# workspace. The barrier waits for all of them — each within its own timeout —
# writes the fail flag when a member that does not continue on failure failed,
# prints one verdict line per member, and exits; the stages after it then skip
# or run exactly as they do after any other stage.
#
# A sidecar must NOT exit (kubelet restarts it), so a finished member parks in
# ``sleep`` — or ``tail -f /dev/null`` in an image without it — until the pod
# ends, when kubelet stops the sidecars after the collector. An image with
# neither just exits: kubelet restarts it under its usual backoff, and the
# restarted container sees its done file and only reprints the result. Noisy,
# never wrong.
#
# Native sidecars need Kubernetes 1.29 or newer (the SidecarContainers feature,
# on by default from 1.29 and GA in 1.33). ``parallel_capability`` checks the
# cluster version — or ``CI_PARALLEL_STAGES`` overrides the check — and a
# build on an older cluster lays its members out as ordinary initContainers,
# one after another, with the same done files, barrier and failure semantics.
#
# Sidecars hold their resource REQUESTS for the pod's whole life, finished or
# not: see ``resources.effective_pod_requests``, whose total the Job carries in
# an annotation and the barrier prints.
# ---------------------------------------------------------------------------

_STATE_DIR = "/workspace/.kubesight"
_GROUPS_ANNOTATION = "kubesight.io/parallel-groups"
_REQUESTS_ANNOTATION = "kubesight.io/effective-requests"
# The barrier's per-member verdict: "[kubesight-member] <position> <outcome>",
# outcome one of ok | failed | skip | timeout | cancelled.
_MEMBER_VERDICT = "[kubesight-member]"
# A member that its own `timeout` wrapper stopped.
_TIMEOUT_MARKER = "[kubesight-timeout]"
_STAGE_SCRIPT_ENV = "KUBESIGHT_STAGE_SCRIPT"
_BARRIER_PREFIX = "barrier-"
_BARRIER_POLL_SECONDS = 2
_STAGE_CONTAINER_RE = re.compile(r"^stage-(\d+)$")


def barrier_container_name(first_position: int) -> str:
    return f"{_BARRIER_PREFIX}{first_position}"


def plan_groups(plan: List[StageExecution]) -> List[List[StageExecution]]:
    """Runs of two or more consecutive plan entries sharing a parallel group.

    Read off the PLAN, not the pipeline: the plan holds only the stages that
    will run, so a member skipped by its run condition is simply not in it —
    and a group left with one runnable member is no group, just a stage.
    """
    runs: List[List[StageExecution]] = []
    current: List[StageExecution] = []
    for execution in plan:
        key = (execution.parallel_group or "").strip().lower()
        if key and execution.stage_type != "checkout" and current and (
            (current[-1].parallel_group or "").strip().lower() == key
        ):
            current.append(execution)
            continue
        if len(current) > 1:
            runs.append(current)
        current = [execution] if key and execution.stage_type != "checkout" else []
    if len(current) > 1:
        runs.append(current)
    return runs


def _plan_parallel_mode(plan: List[StageExecution]) -> str:
    for execution in plan:
        if execution.parallel_mode:
            return execution.parallel_mode
    return ""


def _parallel_reason(plan: List[StageExecution]) -> str:
    for execution in plan:
        if execution.parallel_reason:
            return execution.parallel_reason
    return ""


def _member_body(body: str) -> str:
    """The member's own commands as one script for ``sh -c``: the same
    ``set -eu`` and build-variable loading the sequential wrapper's subshell
    gives a stage."""
    return f"set -eu\n{_LOAD_BUILD_ENV}{body}\n"


def member_stage_script(
    execution: StageExecution,
    *,
    group: List[StageExecution],
    parallel: bool,
    reason: str = "",
    state_dir: str = _STATE_DIR,
    with_cache_prep: bool = True,
) -> str:
    """The wrapper every member of a group runs; its commands arrive in
    ``$KUBESIGHT_STAGE_SCRIPT``.

    The same contract as :func:`_wrap_stage_script` — exit markers, a skip when
    an earlier stage failed — plus what running beside siblings needs: a done
    file for the barrier, a failure recorded for the GROUP rather than written
    straight to the fail flag (a sibling that has not started yet must not
    read it as "an earlier stage failed"), and, as a sidecar, its own timeout
    and staying alive once finished. ``state_dir`` exists so a test can run the
    real script against a temporary directory.
    """
    position = execution.position
    first = group[0].position
    name = execution.parallel_group or "group"
    siblings = [item.stage_name for item in group if item.position != position]
    fail_fast = any(item.parallel_fail_fast for item in group)
    timeout = max(1, int(execution.timeout_seconds or 1800))
    cof = bool(execution.continue_on_failure)

    if parallel:
        rest = (
            "ks_rest() {\n"
            # A test hook, and an escape hatch for an image where a lingering
            # sidecar is unwanted: exiting only costs a restart (see below).
            '  if [ "${KUBESIGHT_KEEPALIVE:-1}" = "0" ]; then exit 0; fi\n'
            "  trap 'exit 0' TERM INT HUP\n"
            "  if command -v sleep >/dev/null 2>&1; then\n"
            "    while :; do sleep 3600 & wait $!; done\n"
            "  elif command -v tail >/dev/null 2>&1; then\n"
            "    tail -f /dev/null & wait $!\n"
            "  fi\n"
            '  echo "[kubesight] This image has neither sleep nor tail, so the finished stage\'s '
            'container exits and Kubernetes restarts it. The restart only reprints the result."\n'
            "  exit 0\n"
            "}\n"
        )
        intro = (
            "echo "
            + _q(f"[kubesight] Parallel group '{name}': runs at the same time as {', '.join(siblings)}.")
            + "\n"
        )
        runner = (
            'KS_TO=""\n'
            # GNU and busybox ≥1.30 spell it this way; an older busybox does
            # not, and then the barrier's deadline is the timeout.
            "if timeout -s TERM 5 true >/dev/null 2>&1; then\n"
            f'  KS_TO="timeout -s TERM {timeout}"\n'
            "fi\n"
            # A missing script is a failure, never an empty success.
            f'$KS_TO sh -c "${{{_STAGE_SCRIPT_ENV}:-exit 97}}"\n'
            "EC=$?\n"
            'if [ -n "$KS_TO" ] && [ "$EC" -eq 124 ]; then\n'
            f'  echo "[kubesight] Stopped: this stage exceeded its {timeout}s timeout." >&2\n'
            "  KS_RESULT=timeout\n"
            "else\n"
            "  KS_RESULT=$EC\n"
            "fi\n"
        )
    else:
        rest = "ks_rest() { exit 0; }\n"
        intro = f"echo {_q(parallel_groups.sequential_notice(name, reason or 'the runner cannot run them side by side.'))}\n"
        runner = f'sh -c "${{{_STAGE_SCRIPT_ENV}:-exit 97}}"\nKS_RESULT=$?\n'

    group_flag = f"$KS_STATE/group-{first}-failed"
    record = (
        'if [ "$KS_RESULT" != "0" ]; then\n'
        + (
            # Fails the BUILD all the same; post actions read this flag.
            '  : > "$KS_STATE/failed-continued"\n'
            if cof
            else f'  : > "{group_flag}"\n'
        )
        + "fi\n"
    )
    fail_fast_guard = (
        (
            f'if [ -e "{group_flag}" ]; then\n'
            '  echo "[kubesight] Skipped: another stage in this group failed, and the group stops at its first failure."\n'
            "  ks_finish skip\n"
            "fi\n"
        )
        if fail_fast
        else ""
    )
    return (
        "set -u\nexport HOME=/tmp TMPDIR=/tmp\n"
        f"KS_STATE={_q(state_dir)}\n"
        f"KS_POS={position}\n"
        'mkdir -p "$KS_STATE" 2>/dev/null || true\n'
        + rest
        + "ks_report() {\n"
        '  case "$1" in\n'
        f'    skip) echo "{_SKIP_MARKER}" ;;\n'
        f'    timeout) echo "{_TIMEOUT_MARKER}" ;;\n'
        f'    *) echo "{_EXIT_MARKER} $1" ;;\n'
        "  esac\n"
        "}\n"
        "ks_finish() {\n"
        "  printf '%s\\n' \"$1\" > \"$KS_STATE/done-$KS_POS\"\n"
        '  ks_report "$1"\n'
        "  ks_rest\n"
        "}\n"
        # Restarted after finishing (a sidecar that could not stay alive):
        # say the result again, never run the stage twice.
        'if [ -s "$KS_STATE/done-$KS_POS" ]; then\n'
        '  read -r KS_DONE < "$KS_STATE/done-$KS_POS" || KS_DONE=""\n'
        '  echo "[kubesight] Kubernetes restarted this stage\'s container after it had finished; it is not run again."\n'
        '  ks_report "${KS_DONE:-1}"\n'
        "  ks_rest\n"
        "fi\n"
        # Restarted part-way through — killed for memory, say. Running it again
        # would repeat side effects behind everybody's back; it failed.
        'if [ -e "$KS_STATE/started-$KS_POS" ]; then\n'
        '  echo "[kubesight] This stage\'s container stopped before the stage finished (out of memory, or killed) and Kubernetes restarted it. It is not run twice: the stage failed."\n'
        + ('  : > "$KS_STATE/failed-continued"\n' if cof else f'  : > "{group_flag}"\n')
        + "  ks_finish 137\n"
        "fi\n"
        ': > "$KS_STATE/started-$KS_POS"\n'
        'if [ -e "$KS_STATE/failed" ]; then\n'
        '  echo "[kubesight] Skipped: an earlier stage failed."\n'
        "  ks_finish skip\n"
        "fi\n"
        + fail_fast_guard
        + intro
        + (_cache_prep() if with_cache_prep else "")
        + runner
        + record
        + 'ks_finish "$KS_RESULT"\n'
    )


def barrier_script(
    group: List[StageExecution],
    *,
    state_dir: str = _STATE_DIR,
    poll_seconds: int = _BARRIER_POLL_SECONDS,
    grace_seconds: int = parallel_groups.POD_TIMEOUT_GRACE_SECONDS,
    reserved: str = "",
) -> str:
    """The plain initContainer that holds the pod until the whole group is done.

    Waits for every member's done file, each within its own timeout (plus a
    grace that lets the member's own ``timeout`` record the result first),
    prints ``[kubesight-member] <position> <outcome>`` for each, and writes the
    fail flag when a member that does not continue on failure failed — so the
    stages after the group skip exactly as they do after any failed stage. A
    fail-fast group stops waiting at the first such failure; the members still
    running are stopped with the pod.
    """
    name = group[0].parallel_group or "group"
    members = " ".join(
        f"{item.position}:{max(1, int(item.timeout_seconds or 1800))}:{1 if item.continue_on_failure else 0}"
        for item in group
    )
    fail_fast = 1 if any(item.parallel_fail_fast for item in group) else 0
    names = ", ".join(item.stage_name for item in group)
    lines = [
        "set -u",
        f"KS_STATE={_q(state_dir)}",
        f"KS_POLL={int(poll_seconds)}",
        f"KS_GRACE={int(grace_seconds)}",
        f"KS_FAIL_FAST={fail_fast}",
        f'KS_PENDING="{members}"',
        'mkdir -p "$KS_STATE" 2>/dev/null || true',
        "echo " + _q(f"[kubesight] Parallel group '{name}': waiting for {names}."),
    ]
    if reserved:
        lines.append(
            f"echo {_q('[kubesight] While it runs, this build pod requests ' + reserved + ': each stage of the group holds its requests until the build ends.')}"
        )
    lines += [
        "KS_FAILED=0",
        "KS_SOFT=0",
        'ks_verdict() { echo "' + _MEMBER_VERDICT + ' $1 $2"; }',
        'ks_bad() { if [ "$1" = "1" ]; then KS_SOFT=1; else KS_FAILED=1; fi; }',
        "KS_START=$(date +%s)",
        'while [ -n "$KS_PENDING" ]; do',
        "  KS_ELAPSED=$(( $(date +%s) - KS_START ))",
        '  KS_NEXT=""',
        "  for KS_M in $KS_PENDING; do",
        "    KS_POS=${KS_M%%:*}",
        "    KS_REST=${KS_M#*:}",
        "    KS_LIMIT=${KS_REST%%:*}",
        "    KS_COF=${KS_REST#*:}",
        '    if [ -s "$KS_STATE/done-$KS_POS" ]; then',
        '      read -r KS_RES < "$KS_STATE/done-$KS_POS" || KS_RES=""',
        '      case "$KS_RES" in',
        '        0) ks_verdict "$KS_POS" ok ;;',
        '        skip) ks_verdict "$KS_POS" skip ;;',
        '        timeout) ks_verdict "$KS_POS" timeout; ks_bad "$KS_COF" ;;',
        '        *) ks_verdict "$KS_POS" failed; ks_bad "$KS_COF" ;;',
        "      esac",
        '    elif [ "$KS_ELAPSED" -ge $(( KS_LIMIT + KS_GRACE )) ]; then',
        '      : > "$KS_STATE/timeout-$KS_POS"',
        '      echo "[kubesight] Stage $KS_POS ran past its ${KS_LIMIT}s timeout; the group stops waiting for it."',
        '      ks_verdict "$KS_POS" timeout',
        '      ks_bad "$KS_COF"',
        "    else",
        '      KS_NEXT="$KS_NEXT $KS_M"',
        "    fi",
        "  done",
        "  KS_PENDING=${KS_NEXT# }",
        '  if [ "$KS_FAIL_FAST" = "1" ] && [ "$KS_FAILED" = "1" ] && [ -n "$KS_PENDING" ]; then',
        '    echo "[kubesight] A stage in this group failed, and the group stops at its first failure. The stages still running are stopped with the build pod."',
        "    for KS_M in $KS_PENDING; do",
        "      KS_POS=${KS_M%%:*}",
        '      : > "$KS_STATE/cancelled-$KS_POS"',
        '      ks_verdict "$KS_POS" cancelled',
        "    done",
        '    KS_PENDING=""',
        "  fi",
        '  if [ -n "$KS_PENDING" ]; then sleep "$KS_POLL"; fi',
        "done",
        'if [ "$KS_FAILED" = "1" ]; then',
        '  : > "$KS_STATE/failed"',
        '  echo "[kubesight] The group failed, so the stages after it are skipped."',
        "fi",
        'if [ "$KS_SOFT" = "1" ]; then : > "$KS_STATE/failed-continued"; fi',
        "echo " + _q(f"[kubesight] Parallel group '{name}' finished."),
        "exit 0",
    ]
    return "\n".join(lines) + "\n"


def _barrier_container(group: List[StageExecution], reserved: str) -> Dict[str, Any]:
    return {
        "name": barrier_container_name(group[0].position),
        "image": _worker_image(),
        "imagePullPolicy": _env("CI_IMAGE_PULL_POLICY", "IfNotPresent"),
        "command": ["/bin/sh", "-c", barrier_script(group, reserved=reserved)],
        "env": [{"name": "HOME", "value": "/tmp"}, {"name": "TMPDIR", "value": "/tmp"}],
        "securityContext": dict(_SECURITY_CONTEXT),
        # It only needs the workspace's state directory, but the mounts are
        # every stage's so an operator inspecting the pod sees one shape.
        "volumeMounts": _mounts(),
        "resources": {
            "requests": {"cpu": "10m", "memory": "16Mi"},
            "limits": {"cpu": "100m", "memory": "64Mi"},
        },
    }


def _lay_out_groups(
    plan: List[StageExecution],
    init_containers: List[Dict[str, Any]],
    bodies: Dict[int, str],
) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    """``(containers, annotations)`` with every group's members and barrier.

    ``init_containers`` holds one container per plan entry, in plan order, as
    the stage loop built them; members already carry the member wrapper and
    ``bodies`` their commands. This adds the commands to each member's
    environment, makes members sidecars when the build runs groups in
    parallel, and puts the barrier after each group's last member.
    """
    groups = plan_groups(plan)
    if not groups:
        return init_containers, {}
    parallel = _plan_parallel_mode(plan) == parallel_groups.PARALLEL
    by_name = {container["name"]: container for container in init_containers}
    reserved = ""
    if parallel:
        members = {_stage_container_name(member.position) for group in groups for member in group}
        totals = ci_resources.effective_pod_requests(
            [(container.get("resources") or {}, container["name"] in members) for container in init_containers],
            # The collector, whose requests are fixed (see build_job_resources).
            [{"requests": dict(_DEFAULT_REQUESTS)}],
        )
        reserved = ci_resources.describe_requests(totals)
    barrier_after: Dict[str, Dict[str, Any]] = {}
    for group in groups:
        for member in group:
            container = by_name[_stage_container_name(member.position)]
            container["env"] = list(container.get("env") or []) + [
                {"name": _STAGE_SCRIPT_ENV, "value": _member_body(bodies.get(member.position, "true"))}
            ]
            if parallel:
                container["restartPolicy"] = "Always"
        barrier_after[_stage_container_name(group[-1].position)] = _barrier_container(group, reserved)
    laid_out: List[Dict[str, Any]] = []
    for container in init_containers:
        laid_out.append(container)
        if container["name"] in barrier_after:
            laid_out.append(barrier_after[container["name"]])
    annotations = {
        _GROUPS_ANNOTATION: json.dumps(
            {
                "mode": parallel_groups.PARALLEL if parallel else parallel_groups.SEQUENTIAL,
                "groups": [[member.position for member in group] for group in groups],
            },
            separators=(",", ":"),
        )
    }
    if reserved:
        annotations[_REQUESTS_ANNOTATION] = reserved
    return laid_out, annotations


def _stage_command_script(
    execution: StageExecution,
    body: str,
    plan: List[StageExecution],
    bodies: Dict[int, str],
) -> str:
    """The script a stage container runs: the ordinary wrapper, or — for a
    member of a group — the member wrapper, with ``body`` kept aside for its
    environment (see :func:`_lay_out_groups`)."""
    for group in plan_groups(plan):
        if any(member is execution for member in group):
            bodies[execution.position] = body
            return member_stage_script(
                execution,
                group=group,
                parallel=_plan_parallel_mode(plan) == parallel_groups.PARALLEL,
                reason=_parallel_reason(plan),
            )
    return _wrap_stage_script(body, continue_on_failure=bool(execution.continue_on_failure))


# ---------------------------------------------------------------------------
# Cluster version — the gate for native sidecars
# ---------------------------------------------------------------------------

_SIDECAR_MIN_VERSION = (1, 29)
_VERSION_TTL_SECONDS = float(os.getenv("CI_CLUSTER_VERSION_TTL_SECONDS", "600"))
_VERSION_FAILURE_TTL_SECONDS = 60.0
_version_cache: Dict[str, Any] = {"value": None, "error": "", "at": None}


def reset_cluster_version_cache() -> None:
    """Test hook, and what an operator's cluster upgrade waits out otherwise."""
    _version_cache.update({"value": None, "error": "", "at": None})


def cluster_version() -> Tuple[Optional[Tuple[int, int, str]], str]:
    """``((major, minor, text), "")`` for the build cluster, or ``(None, why)``.

    Read through the runner's own kubectl — the same transport and kubeconfig a
    build uses — and cached, because it is asked on every build with a group
    and on every pipeline edit that has one. A failed read is cached for a
    minute only, so a cluster that was briefly unreachable is asked again soon.
    """
    import time as _time

    now = _time.monotonic()
    fetched = _version_cache.get("at")
    if fetched is not None:
        ttl = _VERSION_TTL_SECONDS if _version_cache.get("value") else _VERSION_FAILURE_TTL_SECONDS
        if now - fetched < ttl:
            return _version_cache.get("value"), _version_cache.get("error") or ""
    value: Optional[Tuple[int, int, str]] = None
    error = ""
    try:
        rc, out, err = _kubectl(["version", "-o", "json"], timeout=10)
        data = json.loads(out) if out.strip() else {}
        server = data.get("serverVersion") or {}
        major = int(re.sub(r"\D", "", str(server.get("major") or "")) or 0)
        minor = int(re.sub(r"\D", "", str(server.get("minor") or "")) or 0)
        if major:
            text = str(server.get("gitVersion") or f"v{major}.{minor}")
            value = (major, minor, text)
        else:
            detail = (err or "").strip().splitlines()
            error = detail[-1] if detail else "the cluster did not report a server version"
            if rc == 0 and not detail:
                error = "the cluster did not report a server version"
    except (ValueError, TypeError, OSError, subprocess.SubprocessError) as exc:
        error = str(exc) or exc.__class__.__name__
    _version_cache.update({"value": value, "error": error, "at": now})
    return value, error


def parallel_stage_capability() -> Tuple[bool, str]:
    """Whether this installation's cluster can run group members side by side."""
    mode = parallel_groups.env_mode()
    if mode == "off":
        return False, parallel_groups.OFF_REASON
    if mode == "on":
        return True, (
            "Parallel stages are forced on (CI_PARALLEL_STAGES=on); the cluster version is "
            "not checked, so it must support native sidecar containers."
        )
    version, error = cluster_version()
    if version is None:
        return False, (
            f"KubeSight could not read the cluster's Kubernetes version ({error}), so it does "
            "not assume native sidecar containers. Set CI_PARALLEL_STAGES=on if the cluster "
            "is 1.29 or newer."
        )
    major, minor, text = version
    if (major, minor) < _SIDECAR_MIN_VERSION:
        return False, (
            f"the build cluster runs Kubernetes {text}, and running stages side by side needs "
            "native sidecar containers, which Kubernetes turns on by default from 1.29."
        )
    return True, f"Kubernetes {text} runs native sidecar containers."


INLINE_DOCKERFILE_DIR = "/kubesight-dockerfile"


def _q(value: Any) -> str:
    """One shell word. Leaves an already-safe value byte-for-byte unchanged."""
    return shlex.quote(str(value))


def _checked_registry(registry: Dict[str, Any]) -> Dict[str, Any]:
    """Re-check every registry value the image script splices in.

    The trigger and the pipeline save already hold these to their grammar;
    this is the last line, for a snapshot that outlived those checks. The
    image stage is the one container with the push credentials mounted, so a
    value that is not what it claims to be fails the stage rather than being
    tidied into something nobody asked for.
    """
    problems = [
        build_inputs.registry_host_problem(registry.get("host") or ""),
        build_inputs.image_name_problem(registry.get("repository") or ""),
        build_inputs.image_tag_problem(
            registry.get("tag") or "", allow_template=bool(registry.get("tagIsTemplate"))
        ),
        build_inputs.dockerfile_path_problem(registry.get("dockerfile") or "Dockerfile"),
    ]
    for problem in problems:
        if problem:
            raise RunnerError(problem)
    return registry


def _image_ref_word(registry: Dict[str, Any], prefix: str = "", suffix: str = "") -> str:
    """``prefix + host/repo:tag + suffix`` as one shell word.

    A templated tag is left as ``$KS_TAG`` - unquoted, exactly as before - for
    the pod's shell to finish. That is safe: the resolver forces it through
    ``tr`` to the tag alphabet, so its expansion cannot split or glob.
    """
    base = f"{prefix}{registry['host']}/{registry['repository']}:"
    if registry.get("tagIsTemplate"):
        return _q(base) + "$KS_TAG" + (_q(suffix) if suffix else "")
    return _q(f"{base}{registry['tag']}{suffix}")


def _buildctl_output(registry: Dict[str, Any]) -> str:
    """Where buildkitd sends the finished image: straight to the registry."""
    suffix = ",push=true"
    if registry.get("verifyTls") is False:
        suffix += ",registry.insecure=true"
    return _image_ref_word(registry, "type=image,name=", suffix)


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
    return f"--opt {_q('add-hosts=' + ','.join(pairs))} " if pairs else ""


_ON = ("1", "true", "yes", "on")

# Set by the local-cache prelude; empty when that cache is off or has nothing in
# it yet. Deliberately UNQUOTED where it is used, so an empty value contributes
# no argument at all rather than an empty one buildctl would reject.
_LOCAL_CACHE_VAR = "KS_BK_IMPORT"


def _buildkit_cache_ref(execution: StageExecution) -> str:
    """The registry tag the layer cache is pushed to.

    ``CI_BUILDKIT_CACHE_REPO`` collects every service's cache under one
    repository (``registry.example.com/cache/<slug>:buildcache``), which is what
    you want where the image repositories themselves are governed and an extra
    tag in them would be noticed. Without it the cache sits beside the image it
    belongs to, which is the behaviour this had before.
    """
    registry = execution.registry or {}
    repo = _env("CI_BUILDKIT_CACHE_REPO", "").strip().rstrip("/")
    if repo:
        return f"{repo}/{_dns(execution.service_slug, 63)}:buildcache"
    return f"{registry.get('host', '')}/{registry.get('repository', '')}:buildcache"


def _buildctl_registry_cache(execution: StageExecution) -> str:
    """Layer cache kept in a registry.

    buildkitd's own cache is an emptyDir: warm while that pod lives, gone when
    it restarts, and invisible to a second builder. Pushing the cache to a
    ``:buildcache`` tag makes it survive both, and is the only option that
    still works when the builder moves to another node. Off unless asked for,
    because it writes an extra tag into somebody's registry.
    """
    if _env("CI_BUILDKIT_REGISTRY_CACHE", "0").lower() not in _ON:
        return ""
    ref = _buildkit_cache_ref(execution)
    registry = execution.registry or {}
    insecure = ",registry.insecure=true" if registry.get("verifyTls") is False else ""
    return (
        f"--import-cache {_q(f'type=registry,ref={ref}{insecure}')} "
        # mode=max caches intermediate layers too, which is what makes a
        # dependency-heavy build cheap on the second run.
        f"--export-cache {_q(f'type=registry,ref={ref},mode=max{insecure}')} "
    )


def _buildctl_local_cache(execution: StageExecution) -> Tuple[str, str]:
    """``(prelude, flags)`` for a layer cache kept on the cache volume.

    buildctl resolves ``type=local`` on the CLIENT side and streams it over the
    session, so the directory is this pod's — the one on the PersistentVolume.
    That is what makes this need no change to buildkitd at all.

    It is also why this is safe on the NFS cache volume, while buildkitd's OWN
    store is not: the snapshotter there needs overlayfs and stays an emptyDir,
    whereas a local cache export is ordinary blob files and an index.json.

    The import has to be conditional: buildctl fails the build outright on a
    ``src`` with no ``index.json``, which is exactly an empty cache on the first
    run. So the prelude looks, and the flag is a shell variable that expands to
    nothing when there is nothing to import.

    Off unless asked for. A ``mode=max`` local export rewrites the full cache
    every build and prunes nothing, so it grows until somebody empties it —
    fine on a volume sized for it, a slow disk-full on one that is not.
    """
    if _env("CI_BUILDKIT_LOCAL_CACHE", "0").lower() not in _ON:
        return "", ""
    if not cache_base_path(execution.service_slug):
        return "", ""  # asked for, but there is no volume to put it on
    prelude = (
        f'{_LOCAL_CACHE_VAR}=""\n'
        'if [ -s "$BUILDKIT_CACHE_DIR/index.json" ]; then\n'
        f'  {_LOCAL_CACHE_VAR}="--import-cache type=local,src=$BUILDKIT_CACHE_DIR"\n'
        "else\n"
        '  echo "[kubesight] No BuildKit layer cache yet; this image builds cold."\n'
        "fi\n"
    )
    flags = (
        f"${_LOCAL_CACHE_VAR} "
        "--export-cache type=local,dest=$BUILDKIT_CACHE_DIR,mode=max "
    )
    return prelude, flags


def _buildctl_cache(execution: StageExecution) -> Tuple[str, str]:
    """``(prelude, flags)`` for every layer cache this stage should use.

    Both kinds can be on at once, and buildctl accepts repeated
    ``--import-cache``: the registry copy survives the builder moving node, the
    local copy is far cheaper to read when the build lands back on its volume.
    """
    prelude, local = _buildctl_local_cache(execution)
    return prelude, _buildctl_registry_cache(execution) + local


# Resolves a templated tag inside the build pod, after $KUBESIGHT_ENV has been
# sourced. tr rather than sed: it is in the buildkit client image, and a
# character class is exactly the rule being applied. The empty check matters
# because an unset variable expands to nothing under `set -u`-free expansion,
# and pushing ``repo:V-7`` instead of ``repo:V1.2.3-7`` is silent corruption.
_TAG_RESOLVE_TEMPLATE = """KS_TAG="{template}"
KS_TAG=$(printf '%s' "$KS_TAG" | tr -c 'A-Za-z0-9._-' '-' | cut -c1-100)
if [ -z "$KS_TAG" ]; then
  echo "[kubesight] The image tag template resolved to nothing. Did the stage that exports it run?" >&2
  exit 1
fi
echo "[kubesight] Image tag resolved to $KS_TAG"
"""


def _image_ref_prelude(registry: Dict[str, Any]) -> str:
    """Shell that finishes a templated tag, or nothing when the tag is literal.

    ``IMAGE_TAG=V${APP_VERSION}-${KUBESIGHT_BUILD_NUMBER}`` cannot be resolved
    when the Job is created: APP_VERSION does not exist until a stage reads it
    out of package.json and writes it to $KUBESIGHT_ENV. So the tag travels to
    the pod as a template and the pod's own shell finishes it.
    """
    if not registry.get("tagIsTemplate"):
        return ""
    # Lands inside double quotes so its ${...} expands; the template grammar has
    # nothing that could close the quotes or start a command substitution.
    problem = build_inputs.image_tag_problem(registry["tag"], allow_template=True)
    if problem:
        raise RunnerError(problem)
    return _TAG_RESOLVE_TEMPLATE.format(template=registry["tag"])


def _buildctl_args(
    execution: StageExecution,
    meta_file: str,
    *,
    output: Optional[str] = None,
    with_prelude: bool = True,
) -> str:
    """The buildctl invocation for a container_image stage.

    ``output`` overrides where buildkitd sends the result — the scanned path
    asks for a local archive instead of a registry push, which is the whole
    mechanism behind the gate. ``with_prelude`` is False when the caller has
    already emitted the tag/cache prelude itself, because that prelude resolves
    ``$KS_TAG`` and the scanned script needs the resolved tag before the build
    in order to name the archive and the push after it.
    """
    registry = _checked_registry(execution.registry or {})
    cache_prelude, cache_flags = _buildctl_cache(execution)
    # The cache prelude runs BEFORE the tag prelude only because neither reads
    # the other; keeping the order fixed keeps the generated script diffable.
    prelude = (cache_prelude + _image_ref_prelude(registry)) if with_prelude else ""
    # Several builders: pick one that answers from THIS pod, right before the
    # build, after the tag and cache preludes (neither talks to buildkitd).
    candidates = buildkit_candidates(execution.service_slug)
    if len(candidates) > 1:
        prelude += _buildkit_select_script(candidates)
        addr_word = '"$KS_BUILDKIT_ADDR"'
    else:
        addr_word = _q(buildkit_addr())
    output_word = output or _buildctl_output(registry)
    context = "/workspace/source"
    if execution.working_directory:
        context = f"/workspace/source/{execution.working_directory}"
    dockerfile = registry.get("dockerfile") or "Dockerfile"
    dockerfile_dir = os.path.dirname(dockerfile) or "."
    if registry.get("dockerfileContent"):
        # buildctl takes the context and the Dockerfile as SEPARATE locals, so
        # an inline Dockerfile needs no copy into the context: point the
        # dockerfile local at the mounted file and leave the context alone.
        return prelude + (
            f"buildctl --addr {addr_word} build "
            f"--frontend dockerfile.v0 "
            f"--local {_q(f'context={context}')} "
            f"--local dockerfile={INLINE_DOCKERFILE_DIR} "
            f"--opt filename=Dockerfile "
            f"{_buildctl_add_hosts(execution)}"
            f"{cache_flags}"
            f"--output {output_word} "
            f"--metadata-file {_q(meta_file)}"
        )
    return prelude + (
        f"buildctl --addr {addr_word} build "
        f"--frontend dockerfile.v0 "
        f"--local {_q(f'context={context}')} "
        f"--local {_q(f'dockerfile={context}/{dockerfile_dir}')} "
        f"--opt {_q(f'filename={os.path.basename(dockerfile)}')} "
        f"{_buildctl_add_hosts(execution)}"
        f"{cache_flags}"
        f"--output {output_word} "
        f"--metadata-file {_q(meta_file)}"
    )


# ---------------------------------------------------------------------------
# Image scanning — build, scan, push, in ONE stage
#
# Without a gate a container_image stage is a single buildctl call that builds
# and pushes in one motion: by the time anything could look at the image, it is
# already in the registry and already pullable. Scanning has to interrupt that,
# and the only place to interrupt it is inside the stage.
#
# So a scanned stage stops buildkitd from pushing at all. The result comes back
# as a docker archive on the shared workspace, the scanner reads the archive,
# and only a passing verdict reaches the ``crane push`` on the next line. The
# registry never sees an image that failed its own gate — not under a temporary
# tag, not for a moment. That is the property a quarantine-tag design cannot
# offer, and it is the reason for the extra copy through the workspace.
#
# The cost is real and worth stating: the image travels buildkitd -> pod ->
# registry instead of buildkitd -> registry, so a large image spends an extra
# minute or two in transfer and needs room for the archive on the build pod's
# ephemeral storage. Nothing caps that by default; where an installation or a
# service HAS set a cap, resources.ephemeral_limit raises it for exactly these
# stages rather than letting the gate turn into an eviction.
#
# All three tools live in ONE image because a stage is one container. That is
# not a workaround — it is what keeps "one stage = one initContainer = one
# status = one log" true, which is the property this whole adapter is built on.
# A scan as its own stage would have been cheaper to build and impossible to
# rely on: it could be reordered, disabled or deleted while the push it was
# meant to guard carried on.
# ---------------------------------------------------------------------------

# Worst first. A threshold means "this severity and everything above it".
_SEVERITY_ORDER = ("critical", "high", "medium", "low")


def image_tools_image() -> str:
    """The image carrying buildctl + trivy + crane.

    Defaulted rather than required so a fresh installation has something that
    resolves, and pointed at the same internal registry the build templates
    use. If it is not there the pod fails to pull, which is visible; if it is
    there but missing a tool, the guard below says which one. Neither failure
    mode can end with an unscanned image in the registry.
    """
    configured = _env("CI_IMAGE_TOOLS_IMAGE", "").strip()
    if configured:
        return configured
    return f"{build_environments.registry()}/kubesight-ci-imagetools:v1"


def scanning_requested(execution: StageExecution) -> bool:
    """Whether this stage's image must pass a scan before it may be pushed."""
    scan = execution.image_scan
    return bool(isinstance(scan, dict) and scan.get("enabled") is not False)


def _gated_severities(threshold: str) -> str:
    """The severities the gate counts, as Trivy spells them."""
    threshold = str(threshold or "critical").lower()
    if threshold not in _SEVERITY_ORDER:
        threshold = "critical"
    cut = _SEVERITY_ORDER.index(threshold) + 1
    return ",".join(item.upper() for item in _SEVERITY_ORDER[:cut])


def image_archive_path(position: int) -> str:
    return f"/workspace/.kubesight/image-{position}.tar"


def scan_report_path(position: int) -> str:
    return f"/workspace/.kubesight/scan-{position}.json"


def trivy_cache_dir(execution: StageExecution) -> str:
    """Where Trivy keeps its vulnerability database.

    On the build cache volume when there is one, because the database is tens
    of megabytes and re-downloading it every build is the difference between a
    scan that costs seconds and one that costs minutes — and on a cluster with
    no route to the public database host, the difference between a scan and no
    scan at all. /tmp otherwise: correct, just cold every time.
    """
    base = cache_base_path(execution.service_slug)
    return f"{base}/trivy" if base else "/tmp/trivy-cache"


_TOOL_GUARD = """for KS_TOOL in buildctl trivy crane; do
  if ! command -v "$KS_TOOL" >/dev/null 2>&1; then
    echo "[kubesight] This stage scans the image before pushing it, which needs" >&2
    echo "[kubesight] buildctl, trivy and crane in one image. '$KS_TOOL' is not in" >&2
    echo "[kubesight] "{tools_image} >&2
    echo "[kubesight] Build and mirror Dockerfile.ci-imagetools, then point" >&2
    echo "[kubesight] CI_IMAGE_TOOLS_IMAGE at it. Nothing was built and nothing" >&2
    echo "[kubesight] was pushed: an unscanned image is never the fallback." >&2
    exit 1
  fi
done
"""


def _scan_and_push_script(execution: StageExecution, meta_file: str) -> str:
    """build -> scan -> push, as one shell script for one container.

    Ordered so that every exit before the push leaves the registry untouched.
    ``set -e`` is already in force from :func:`_wrap_stage_script`, so an
    unhandled failure anywhere above stops short of the push by construction
    rather than by a check somebody has to remember to write.
    """
    registry = _checked_registry(execution.registry or {})
    scan = execution.image_scan or {}
    position = execution.position
    archive = _q(image_archive_path(position))
    report = _q(scan_report_path(position))

    # One shell word for the argv; the echo lines read $KS_IMAGE_REF instead.
    image_ref = _image_ref_word(registry)
    insecure = " --insecure" if registry.get("verifyTls") is False else ""

    # Both echoed into the script: held to their closed sets here as well as on
    # save, so a hand-edited snapshot cannot put text into those lines.
    threshold = str(scan.get("threshold") or "critical").lower()
    if threshold not in _SEVERITY_ORDER:
        threshold = "critical"
    on_fail = str(scan.get("onFail") or "block").lower()
    if on_fail not in ("block", "warn"):
        on_fail = "block"
    gated = _gated_severities(threshold)
    ignore_unfixed = " --ignore-unfixed" if scan.get("ignoreUnfixed") else ""

    db_repo = _env("CI_TRIVY_DB_REPOSITORY", "").strip()
    db_flag = f" --db-repository {_q(db_repo)}" if db_repo else ""

    # The prelude is emitted here rather than left inside _buildctl_args because
    # the archive name, the scan and the push all need $KS_TAG, and a templated
    # tag must resolve exactly once for all three to agree.
    cache_prelude, _ = _buildctl_cache(execution)
    prelude = cache_prelude + _image_ref_prelude(registry)

    # type=docker rather than type=oci: crane pushes a docker archive and Trivy
    # reads one, so a single file serves both and no conversion step sits
    # between what was scanned and what is pushed.
    build = _buildctl_args(
        execution,
        f"/workspace/.kubesight/buildkit-meta-{position}.json",
        output=_image_ref_word(
            registry, "type=docker,name=", f",dest={image_archive_path(position)}"
        ),
        with_prelude=False,
    )

    if on_fail == "block":
        verdict = (
            '  echo "[kubesight] Scan BLOCKED the push: findings at or above '
            f'{threshold.upper()}. Nothing was pushed. The full report is on this '
            'build as an artifact." >&2\n'
            "  exit 1\n"
        )
    else:
        verdict = (
            '  echo "[kubesight] Scan found findings at or above '
            f'{threshold.upper()}. This gate is set to warn, so the push '
            'continues. The full report is on this build as an artifact." >&2\n'
        )

    return (
        "mkdir -p /workspace/.kubesight\n"
        + _TOOL_GUARD.format(tools_image=_q(image_tools_image()))
        + prelude
        + f"KS_IMAGE_REF={image_ref}\n"
        + 'echo "[kubesight] == build == $KS_IMAGE_REF"\n'
        + build
        + "\n"
        + f'echo "[kubesight] == scan == trivy, gate at {threshold.upper()} ({on_fail})"\n'
        # One full-severity pass writes the report; the gate is then applied to
        # the SAVED report by `trivy convert`. So the image is unpacked and
        # scanned exactly once, and the artifact always holds every finding —
        # including the ones below the threshold, which are what somebody reads
        # when deciding whether to tighten it.
        + (
            f"trivy image --input {archive} --format json --output {report} "
            f"--severity CRITICAL,HIGH,MEDIUM,LOW --scanners vuln --no-progress"
            f"{ignore_unfixed}{db_flag}\n"
        )
        + "set +e\n"
        + f"trivy convert --format table --severity {gated} --exit-code 1 {report}\n"
        + "KS_SCAN_RC=$?\n"
        + "set -e\n"
        + 'if [ "$KS_SCAN_RC" -ne 0 ]; then\n'
        + verdict
        + "fi\n"
        + 'echo "[kubesight] == push == $KS_IMAGE_REF"\n'
        + f"crane push{insecure} {archive} {image_ref}\n"
        # The digest comes from the registry rather than from the build, so the
        # artifact record names the manifest that is actually pullable.
        + f"KS_DIGEST=$(crane digest{insecure} {image_ref})\n"
        + (
            "printf '{\"image.name\":\"%s\",\"containerimage.digest\":\"%s\"}\\n' "
            f'"$KS_IMAGE_REF" "$KS_DIGEST" > {_q(meta_file)}\n'
        )
        # A multi-gigabyte archive on a shared emptyDir would otherwise sit
        # there for the rest of the build, against the same workspace size limit
        # every later stage has to fit inside.
        + f"rm -f {archive}\n"
        + 'echo "[kubesight] Pushed $KS_IMAGE_REF ($KS_DIGEST)"\n'
    )


def image_stage_script(execution: StageExecution, meta_file: str) -> str:
    """The body of a container_image stage, gated or not.

    An ungated stage produces exactly the script it produced before scanning
    existed — byte for byte — so turning the gate off is a true rollback rather
    than a second code path that happens to look similar.
    """
    if not scanning_requested(execution):
        return "mkdir -p /workspace/.kubesight\n" + _buildctl_args(execution, meta_file)
    return _scan_and_push_script(execution, meta_file)


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

# Why a stage container can sit in "waiting" forever. Pull failures can be a
# passing registry hiccup, so they get CI_IMAGE_PULL_GRACE_SECONDS; a name that
# cannot be a valid image never will be.
_PULL_REASONS = frozenset({"ErrImagePull", "ImagePullBackOff", "InvalidImageName", "ErrImageNeverPull"})
_START_FAILURES = frozenset(
    {"ErrImagePull", "ImagePullBackOff", "CreateContainerConfigError", "CreateContainerError"}
)
_START_FAILURES_AT_ONCE = frozenset({"InvalidImageName", "ErrImageNeverPull"})
# When each stuck container was first seen, and the log lines explaining the
# ones given up on - read once by drain_logs. In memory: a restarted backend
# only restarts the grace.
_start_trouble_since: Dict[str, float] = {}
_start_failure_notes: Dict[str, List[str]] = {}
# Jobs deleted because a stage could not start: the stages after it read as
# skipped, not as "deleted out from under us".
_abandoned_jobs: Dict[str, float] = {}


def _scanning(execution: StageExecution) -> bool:
    return execution.stage_type == "container_image" and scanning_requested(execution)


def _stage_resources(execution: StageExecution) -> Dict[str, Any]:
    """CPU, memory and ephemeral storage for one stage container.

    ``execution.resources`` is already the merged view — the stage's own values
    over the service's Build resources (see ``engine._build_execution``). What is
    left for this function is the installation default underneath, and turning
    "off" into an absent key rather than a literal value. The decisions live in
    ``services/ci/resources.py``; this only shapes them into a manifest.
    """
    chosen = execution.resources or {}
    defaults = ci_resources.installation_defaults()
    limits = {
        "cpu": chosen.get("cpu") or defaults["cpu"],
        "memory": chosen.get("memory") or defaults["memory"],
    }
    # "off" is honoured for CPU and memory as well, since a user who asks for no
    # limit has a reason — a compile that is throttled to uselessness by a CPU
    # cap is the usual one. Worth knowing before choosing it: an unlimited
    # container can be OOM-killed only after it has already pushed the NODE into
    # memory pressure, which takes its neighbours with it. The requests below
    # stay either way, so the scheduler still reserves a floor.
    for key in ("cpu", "memory"):
        if _is_off(limits[key]):
            limits.pop(key)

    requests = dict(_DEFAULT_REQUESTS)

    ephemeral_limit = ci_resources.ephemeral_limit(chosen, scanning=_scanning(execution))
    if not _is_off(ephemeral_limit):
        limits["ephemeral-storage"] = ephemeral_limit

    ephemeral_request = ci_resources.ephemeral_request(ephemeral_limit)
    if not _is_off(ephemeral_request):
        requests["ephemeral-storage"] = ephemeral_request

    return {"requests": requests, "limits": limits}


def _workspace_medium(plan: Optional[List[StageExecution]] = None) -> Dict[str, Any]:
    """The /workspace emptyDir, sized to whatever the plan's stages were granted.

    Its sizeLimit is a ceiling kubelet enforces by EVICTING the pod, independent
    of the per-container ephemeral-storage limit — so it has to honour "off" too
    (or removing the limits still leaves a cap in place) and it has to rise with
    an explicit limit (or a stage granted 8Gi still dies at the default, and the
    eviction reads as the pod disappearing halfway through a build for no stated
    reason). Both rules live in ``services/ci/resources.py``.
    """
    stages = list(plan or [])
    size = ci_resources.workspace_size_limit(
        # What the stages were EXPLICITLY granted, not what they resolved to. A
        # stage that simply inherits the open default says nothing about the
        # workspace — an installation is free to leave containers uncapped and
        # still cap the shared volume, and usually should.
        [(item.resources or {}).get("ephemeralStorage") for item in stages],
        scanning=any(_scanning(item) for item in stages),
    )
    return {} if _is_off(size) else {"sizeLimit": size}


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
#
# A cluster with no provisioner has no class to name, and there the only way to
# get a cache is a PersistentVolume made by hand. CI_CACHE_CLAIM_NAME points at
# that volume's claim (see k8s/ci-cache-volume.yaml): one claim shared by every
# service, each service confined to its own subtree of it.
# ---------------------------------------------------------------------------

# The layout itself — mount paths, per-service directory, tool variables —
# lives in ../cache_layout.py, shared verbatim with the maintenance Jobs in
# ../cache.py so the two can never disagree about a path.
CACHE_MOUNT_PATH = cache_layout.CACHE_MOUNT_PATH
LEGACY_CACHE_MOUNT_PATH = cache_layout.LEGACY_MOUNT_PATH

# uid/gid the stage containers run as. The cache volume is handed to them
# through this group — see the fsGroup note on the Job below.
CACHE_FS_GROUP = cache_layout.CACHE_FS_GROUP


# One build's cache settings, read once. A Job manifest asks "is the cache on?"
# a dozen times - volumes, every container's mounts, env and prep, fsGroup - and
# each answer used to be its own database read. One read failing half way
# through (and falling back to the environment) gave a Job whose containers
# mount a "cache" volume the pod does not declare, or a cache directory on a
# volume that is not there. Pinned, every part of one manifest agrees.
_pinned_cache = threading.local()


@contextlib.contextmanager
def cache_settings_pinned():
    """Read the cache settings once for everything inside this block.

    Re-entrant: an inner block reuses the outer one's answer, so the claim
    check in ``_create_job`` and the manifest it then builds cannot disagree.
    """
    if getattr(_pinned_cache, "value", None) is not None:
        yield
        return
    _pinned_cache.value = {"runtime": _read_cache_runtime()}
    try:
        yield
    finally:
        _pinned_cache.value = None


def _cache_runtime() -> Dict[str, str]:
    pinned = getattr(_pinned_cache, "value", None)
    if pinned is not None:
        return pinned["runtime"]
    return _read_cache_runtime()


def _read_cache_runtime() -> Dict[str, str]:
    """What to cache into: what an operator saved in the UI, or the
    environment when nothing has been saved.

    Imported inside the function rather than at module scope because
    services/ci/cache.py reads THIS module for its kubectl transport; a
    top-level import either way closes the loop. A manifest must still be
    buildable when the database is unreachable, so any failure falls back to
    the variables.
    """
    try:
        from .. import cache as cache_settings

        return cache_settings.runtime_config()
    except Exception:  # pragma: no cover - depends on app/db state
        logger.debug("cache settings unavailable; using the environment")
        return {
            "claimName": os.getenv("CI_CACHE_CLAIM_NAME", "").strip(),
            "storageClass": os.getenv("CI_CACHE_STORAGE_CLASS", "").strip(),
        }


def cache_storage_class() -> str:
    return _cache_runtime()["storageClass"]


def cache_claim_override() -> str:
    """A claim the operator created by hand (or from the UI), shared by every
    service.

    Takes precedence over a storage class: naming an existing claim is the more
    specific instruction, and KubeSight then never tries to create, resize or
    otherwise touch the volume behind it.
    """
    return _cache_runtime()["claimName"]


def cache_enabled() -> bool:
    return bool(cache_claim_override() or cache_storage_class())


def cache_claim_name(service_slug: str) -> str:
    return cache_claim_override() or f"ci-cache-{_dns(service_slug, 50)}"


def cache_base_path(service_slug: str) -> str:
    """``$KUBESIGHT_CACHE_DIR`` for this service, "" when caching is off.

    Always a per-service subtree, in BOTH storage modes. One hand-made volume
    holds every service, so each needs its own: two services sharing a Gradle or
    Maven directory would fight over the same lock files. A per-service claim is
    isolated by the claim already — it gets the same subtree anyway, so that one
    rule explains the layout everywhere, and so ``cache.py`` can empty one
    service's cache by path without knowing which mode produced it.
    """
    if not cache_enabled():
        return ""
    return cache_layout.service_cache_dir(service_slug, CACHE_MOUNT_PATH)


def cache_shared_tools() -> tuple:
    """Which tools cache into the subtree every service shares.

    Only on the one hand-made claim: a per-service claim (storage-class mode)
    has no volume in common with any other service, so there is nothing to
    share and everything stays in the service's own subtree. Saved in the UI,
    else ``CI_CACHE_SHARED``, else the default set.
    """
    if not cache_claim_override():
        return ()
    pinned = getattr(_pinned_cache, "value", None)
    if pinned is not None and "shared" in pinned:
        return pinned["shared"]
    try:
        from .. import cache as cache_settings

        shared = cache_settings.shared_tools()
    except Exception:  # pragma: no cover - depends on app/db state
        shared = cache_layout.parse_shared(os.getenv("CI_CACHE_SHARED"))
    if pinned is not None:
        pinned["shared"] = shared
    return shared


def cache_shared_path() -> str:
    """``$KUBESIGHT_SHARED_CACHE_DIR``, "" when nothing is shared."""
    if not cache_enabled() or not cache_shared_tools():
        return ""
    return cache_layout.shared_cache_dir(CACHE_MOUNT_PATH)


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
    """Every stage container's mounts, including both cache paths.

    The cache claim is mounted TWICE: at /kubesight-cache, which is what
    $KUBESIGHT_CACHE_DIR and every injected tool variable point at, and again at
    /cache, which is where it used to live. The same volume, so the same bytes —
    a pipeline that still hardcodes /cache/<slug> keeps its warm cache instead of
    quietly starting cold. Nothing KubeSight generates emits /cache any more, so
    the second mount can be dropped once no pipeline mentions it.
    """
    mounts = [
        {"name": "workspace", "mountPath": "/workspace"},
        {"name": "tmp", "mountPath": "/tmp"},
    ]
    if cache_enabled():
        mounts.append({"name": "cache", "mountPath": CACHE_MOUNT_PATH})
        mounts.append({"name": "cache", "mountPath": LEGACY_CACHE_MOUNT_PATH})
    return mounts


def _tool_cache_env(base: str, shared_base: str = "", shared=()) -> Dict[str, str]:
    """Every build tool's cache variable, from the shared layout.

    Kept as a thin wrapper rather than inlined at the call site because the
    agent runner and the maintenance Jobs need the identical mapping, and a
    second copy of it is a second thing to forget to update.
    """
    return cache_layout.tool_env(base, shared_base, shared)


def _plain_env(execution: StageExecution, extra: Dict[str, str]) -> List[Dict[str, Any]]:
    cache_base = cache_base_path(execution.service_slug)
    shared_base = cache_shared_path() if cache_base else ""
    env = {
        "HOME": "/tmp",
        "TMPDIR": "/tmp",
        "PYTHONDONTWRITEBYTECODE": "1",
        "KUBESIGHT_BUILD_ID": str(execution.build_id),
        "KUBESIGHT_BUILD_NUMBER": str(execution.build_number),
        "KUBESIGHT_SERVICE": execution.service_slug,
        # The same value under the name the cache paths are built from. Stage
        # scripts name their scan project and their cache subdirectory with it,
        # and "$KUBESIGHT_SERVICE_SLUG" reads as what it is where
        # "$KUBESIGHT_SERVICE" could be a display name.
        "KUBESIGHT_SERVICE_SLUG": execution.service_slug,
        "KUBESIGHT_BRANCH": execution.branch or "",
        "KUBESIGHT_COMMIT": execution.commit_sha or "",
        # Where this build's files are, named rather than assumed. A pipeline
        # that hardcodes /workspace is a pipeline that only runs on Kubernetes:
        # an agent puts the same build under its own directory, and the same
        # stage has to work on both.
        "KUBESIGHT_WORKSPACE": "/workspace",
        "KUBESIGHT_SOURCE": "/workspace/source",
        # Both empty when no cache volume is configured, so a pipeline can use
        # them unconditionally and simply get a cold build where there is none.
        # _tool_cache_env sets them again to the same value when there IS one;
        # they are declared here so the "off" case still defines the names.
        "KUBESIGHT_CACHE_DIR": cache_base,
        "KUBESIGHT_CACHE": cache_base,
        # "" unless some tools cache into the subtree every service shares.
        "KUBESIGHT_SHARED_CACHE_DIR": shared_base,
        **_tool_cache_env(cache_base, shared_base, cache_shared_tools() if shared_base else ()),
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
    with cache_settings_pinned():
        return _build_job_resources(first)


def _build_job_resources(first: StageExecution) -> List[Dict[str, Any]]:
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
    # Parallel group members' own commands, by position: they travel in the
    # member's environment rather than inline (see member_stage_script).
    member_bodies: Dict[int, str] = {}

    for execution in plan:
        if execution.continue_on_failure:
            cof_positions.append(execution.position)
        if execution.stage_type == "command" and code_scan.armed(execution.code_scan):
            # Kept whether the gate passed or failed - the collector runs after
            # a failed stage too, and a failed gate is when the report is read.
            artifact_specs.append(code_scan.artifact_spec(execution.position))
        if execution.stage_type == "scan":
            # The same, for the report (or SBOM) a scan stage writes. A Semgrep
            # scan stage's spec IS code_scan's, so the PDF finds it by name.
            artifact_specs.extend(scan_stage.artifact_specs(execution.scan, execution.position))
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
                        "KUBESIGHT_BRANCH": execution.branch or "",
                    },
                )
                + callback_env
                + _secret_env(secret_name, execution),
            }
        elif execution.stage_type == "container_image":
            meta_file = f"/workspace/.kubesight/image-meta-{execution.position}.json"
            registry = execution.registry or {}
            scanned = scanning_requested(execution)
            image_specs.append(
                {
                    "stagePosition": execution.position,
                    "name": registry.get("repository", first.service_slug),
                    "uri": f"{registry.get('host','')}/{registry.get('repository','')}:{registry.get('tag','')}",
                }
            )
            if scanned:
                # Declared here rather than from the stage's own `artifacts`,
                # which the loop above deliberately ignores for image stages.
                # An absolute path so it resolves outside /workspace/source —
                # the report describes the image, not the checkout.
                artifact_specs.append(
                    {
                        "path": scan_report_path(execution.position),
                        "type": "scan-report",
                        "name": f"{registry.get('repository') or first.service_slug}-scan",
                        "workdir": "",
                        "stagePosition": execution.position,
                    }
                )
            container = {
                **base,
                # A scanned stage needs buildctl, the scanner and the pusher in
                # the same container, because it is one stage and a stage is one
                # container. An unscanned one keeps the plain client image it
                # has always used.
                "image": (
                    image_tools_image()
                    if scanned
                    else _env("CI_BUILDKIT_CLIENT_IMAGE", "moby/buildkit:v0.23.2")
                ),
                "command": [
                    "/bin/sh",
                    "-c",
                    _stage_command_script(
                        execution, image_stage_script(execution, meta_file), plan, member_bodies
                    ),
                ],
                "env": _plain_env(
                    execution,
                    {"DOCKER_CONFIG": "/kubesight-docker"}
                    if not scanned
                    else {
                        "DOCKER_CONFIG": "/kubesight-docker",
                        # Trivy writes its database and its own scratch space
                        # here. Both must be somewhere writable, because the
                        # root filesystem is read-only for every stage.
                        "TRIVY_CACHE_DIR": trivy_cache_dir(execution),
                        "TRIVY_TEMP_DIR": "/tmp",
                        # An air-gapped installation mirrors the database and
                        # points CI_TRIVY_DB_REPOSITORY at the mirror; skipping
                        # the update then avoids a doomed reach for the public
                        # host on every build.
                        "TRIVY_SKIP_DB_UPDATE": (
                            "true" if _env("CI_TRIVY_SKIP_DB_UPDATE", "0").lower() in _ON else "false"
                        ),
                    },
                )
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
        elif execution.stage_type == "scan":
            # A command stage whose script and image KubeSight chose: the same
            # container, cache mounts and secrets. Two differences. The shell
            # is found on PATH rather than at /bin/sh, because the Syft image
            # (anchore/syft:debug) carries busybox at /busybox and has no
            # /bin/sh at all. And Trivy is pointed at the same database
            # directory as the image scan gate, so it is downloaded once.
            tool = (execution.scan or {}).get("tool")
            container = {
                **base,
                "image": execution.image or _env("CI_DEFAULT_STAGE_IMAGE", "debian:bookworm-slim"),
                "command": [
                    "sh",
                    "-c",
                    _stage_command_script(execution, _command_stage_body(execution), plan, member_bodies),
                ],
                "env": _plain_env(
                    execution,
                    {"TRIVY_CACHE_DIR": trivy_cache_dir(execution), "TRIVY_TEMP_DIR": "/tmp"}
                    if tool == "trivy_fs"
                    else {},
                )
                + _secret_env(secret_name, execution),
            }
        else:  # command
            container = {
                **base,
                "image": execution.image or _env("CI_DEFAULT_STAGE_IMAGE", "debian:bookworm-slim"),
                "command": [
                    "/bin/sh",
                    "-c",
                    _stage_command_script(execution, _command_stage_body(execution), plan, member_bodies),
                ],
                "env": _plain_env(execution, {}) + _secret_env(secret_name, execution),
            }
        init_containers.append(container)

    # -- parallel groups: members become sidecars (or stay in line on a cluster
    # without them) and each group gets its barrier. Still among the STAGE
    # containers, so everything below — post actions, collector — comes after
    # every group has finished.
    init_containers, group_annotations = _lay_out_groups(plan, init_containers, member_bodies)

    # -- post actions: cleanup commands (services/ci/post_actions.py) --------
    # AFTER every stage container (anything a stage needs in front of it
    # belongs above this block) and BEFORE the collector. Each runs whatever
    # the fail flag says, decides by it, and exits 0 so the collector still
    # uploads. Kept to these lines on purpose: the stage containers above can
    # change shape without touching them.
    post_containers, post_timeout_seconds = _post_action_containers(first, secret_name, secret_data)
    init_containers.extend(post_containers)

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
            # Only to release the build's cache slot (see the script).
            {"name": "KUBESIGHT_CACHE_DIR", "value": cache_base_path(first.service_slug)},
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
        {"name": "workspace", "emptyDir": _workspace_medium(plan)},
        {"name": "tmp", "emptyDir": {"sizeLimit": "512Mi"}},
    ]
    if cache_enabled():
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

    total_timeout = (
        sum(int(execution.timeout_seconds or 1800) for execution in plan) + 900 + post_timeout_seconds
    )
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
                        _COF_ANNOTATION: ",".join(str(p) for p in cof_positions),
                        # Which stage containers are a parallel group, and
                        # whether they run side by side — what `poll` reads
                        # to know a member is not waited on like a stage.
                        **group_annotations,
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
                        # A volume arrives owned by root, and stage containers
                        # run as uid 65532 with a read-only root filesystem and
                        # no capability to chown it — so hand it over by group
                        # instead, or every build fails writing to /cache.
                        # OnRootMismatch keeps that to the first pod rather
                        # than re-walking a full cache on every build.
                        **(
                            {
                                "fsGroup": CACHE_FS_GROUP,
                                "fsGroupChangePolicy": "OnRootMismatch",
                            }
                            if cache_enabled()
                            else {}
                        ),
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
# Post actions — cleanup commands after the stages
#
# One ``post-N`` initContainer per cleanup (services/ci/post_actions.py), after
# every stage and before the collector. Not stages: the engine never advances
# them as such, and :meth:`KubernetesJobRunnerAdapter._is_last_stage` looks
# past them, so the last STAGE still waits for the whole pod as before.
#
# Each container runs whatever the fail flag says — that is the point of a
# cleanup — and decides by it: ``success`` runs when no stage failed,
# ``failure`` when one did (a continue-on-failure stage's own flag counts, as
# it fails the build), ``always`` either way. It is bounded by its own timeout
# inside the pod, and always exits 0, so a failed cleanup never stops the
# collector uploading what the stages produced.
# ---------------------------------------------------------------------------

POST_CONTAINER_PREFIX = "post-"


def post_container_name(position: int) -> str:
    from .. import post_actions

    return f"{POST_CONTAINER_PREFIX}{max(0, int(position) - post_actions.POSITION_BASE)}"


def _container_name_for(execution: StageExecution) -> str:
    if execution.stage_type == "post":
        return post_container_name(execution.position)
    return _stage_container_name(execution.position)


def post_container_script(
    execution: StageExecution, *, root: str = "/workspace", tmp: str = "/tmp"
) -> str:
    """The whole shell of one cleanup container.

    ``root``/``tmp`` exist so a test can run the real script against a
    temporary directory; the manifest always uses the defaults.
    """
    from .. import post_actions

    when = execution.post_when if execution.post_when in post_actions.CLEANUP_WHEN_VALUES else "always"
    index = max(0, int(execution.position) - post_actions.POSITION_BASE)
    state = f"{root}/.kubesight"
    workdir = f"{root}/source" + (f"/{execution.working_directory}" if execution.working_directory else "")
    try:
        timeout = max(1, int(execution.timeout_seconds or post_actions.DEFAULT_CLEANUP_TIMEOUT))
    except (TypeError, ValueError):
        timeout = post_actions.DEFAULT_CLEANUP_TIMEOUT
    commands = "\n".join(execution.commands or ["true"])
    script_file = f"{tmp}/kubesight-post-{index}.sh"
    # A quoted heredoc writes the commands verbatim; the delimiter only has to
    # be a line the commands do not contain.
    delimiter = f"KS_POST_{index}_EOF"
    while delimiter in commands:
        delimiter += "_"
    gate = ""
    if when == "success":
        gate = (
            'if [ "$KUBESIGHT_STAGES_RESULT" != "success" ]; then\n'
            '  echo "[kubesight] Skipped: this cleanup runs when the stages succeed, and one of them failed."\n'
            f'  echo "{_SKIP_MARKER}"\n'
            "  exit 0\n"
            "fi\n"
        )
    elif when == "failure":
        gate = (
            'if [ "$KUBESIGHT_STAGES_RESULT" != "failure" ]; then\n'
            '  echo "[kubesight] Skipped: this cleanup runs when a stage fails, and every stage succeeded."\n'
            f'  echo "{_SKIP_MARKER}"\n'
            "  exit 0\n"
            "fi\n"
        )
    return (
        "set -u\n"
        f"export HOME={_q(tmp)} TMPDIR={_q(tmp)}\n"
        f"KS_STATE={_q(state)}\n"
        'mkdir -p "$KS_STATE" 2>/dev/null || true\n'
        'if [ -e "$KS_STATE/failed" ] || [ -e "$KS_STATE/failed-continued" ]; then\n'
        "  KUBESIGHT_STAGES_RESULT=failure\n"
        "else\n"
        "  KUBESIGHT_STAGES_RESULT=success\n"
        "fi\n"
        "export KUBESIGHT_STAGES_RESULT\n"
        f'echo "[kubesight] Post action ({when}): the stages ended in $KUBESIGHT_STAGES_RESULT."\n'
        + gate
        + f"if [ -d {_q(workdir)} ]; then\n"
        f"  cd {_q(workdir)}\n"
        "else\n"
        '  echo "[kubesight] The source directory does not exist (did the checkout run?); '
        'cleaning up from the workspace root."\n'
        f"  cd {_q(root)} 2>/dev/null || cd /\n"
        "fi\n"
        f"cat > {_q(script_file)} <<'{delimiter}'\n"
        f"export KUBESIGHT_ENV={_q(state + '/build.env')}\n"
        'if [ -s "$KUBESIGHT_ENV" ]; then . "$KUBESIGHT_ENV"; fi\n'
        f"{commands}\n"
        f"{delimiter}\n"
        "if command -v timeout >/dev/null 2>&1; then\n"
        f"  timeout {timeout} sh -e {_q(script_file)}\n"
        "else\n"
        f"  sh -e {_q(script_file)}\n"
        "fi\n"
        "EC=$?\n"
        'if [ "$EC" -eq 124 ]; then\n'
        f'  echo "[kubesight] The cleanup exceeded its {timeout}s timeout and was stopped." >&2\n'
        "fi\n"
        f'echo "{_EXIT_MARKER} $EC"\n'
        "exit 0\n"
    )


def _post_action_containers(
    first: StageExecution, secret_name: str, secret_data: Dict[str, str]
) -> Tuple[List[Dict[str, Any]], int]:
    """``(containers, seconds)``: the cleanup initContainers for ``first.post_plan``
    and what they add to the Job's deadline. Their secrets join the build's
    Secret under their own keys (positions from 1000, never a stage's)."""
    containers: List[Dict[str, Any]] = []
    budget = 0
    for execution in first.post_plan or []:
        for env_name, value in (execution.secrets or {}).items():
            secret_data[_secret_key(execution.position, env_name)] = _b64(value)
        containers.append(
            {
                "name": post_container_name(execution.position),
                "image": execution.image or _env("CI_DEFAULT_STAGE_IMAGE", "debian:bookworm-slim"),
                "imagePullPolicy": _env("CI_IMAGE_PULL_POLICY", "IfNotPresent"),
                "securityContext": dict(_SECURITY_CONTEXT),
                "volumeMounts": _mounts(),
                "resources": _stage_resources(execution),
                "command": ["/bin/sh", "-c", post_container_script(execution)],
                "env": _plain_env(execution, {"KUBESIGHT_POST_WHEN": execution.post_when or "always"})
                + _secret_env(secret_name, execution),
            }
        )
        try:
            budget += int(execution.timeout_seconds or 600) + 60
        except (TypeError, ValueError):
            budget += 660
    return containers, budget


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
        # A scan stage is a command stage with a generated script, so it needs
        # nothing a command stage does not.
        supported = {"checkout", "command", "scan"}
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

    def parallel_capability(self) -> Tuple[bool, str]:
        """Native sidecars or not: see the parallel groups notes above."""
        return parallel_stage_capability()

    # -- lifecycle -----------------------------------------------------------

    def start(self, execution: StageExecution) -> RunnerHandle:
        job_name = job_name_for(execution)
        # A cleanup (stage_type "post") attaches to its post-N container.
        ref = f"{job_name}#{_container_name_for(execution)}"
        if execution.plan:
            self._create_job(execution)
        # Later stages: the Job is already running their container in order (or
        # side by side, for a parallel group) — starting them is just attaching
        # to the right container.
        return RunnerHandle(runner_id=0, external_ref=ref)

    def _create_job(self, execution: StageExecution) -> None:
        with cache_settings_pinned():
            if cache_claim_override():
                self._require_cache_claim()
            elif cache_storage_class():
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

    def _require_cache_claim(self) -> None:
        """Check the hand-made claim is there before a pod is told to mount it.

        Same reasoning as _ensure_cache_claim: the Job mounts the claim by
        name, and a missing one leaves the pod Pending until its deadline with
        nothing in the build log explaining why.
        """
        name = cache_claim_override()
        rc, _, _ = _kubectl(["get", "pvc", name, "-n", _namespace(), "-o", "name"], timeout=20)
        if rc != 0:
            raise RunnerError(
                f"CI_CACHE_CLAIM_NAME points at a claim that does not exist in "
                f"{_namespace()}: {name} — apply k8s/ci-cache-volume.yaml, or clear "
                "CI_CACHE_CLAIM_NAME to build without a cache."
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
            f"cd {_q(path)} 2>/dev/null || {{ echo '__KS_NO_PATH__'; exit 0; }}\n"
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
        return self._status_of(job, pod, job_name, container, {})

    def poll_many(self, handles: List[RunnerHandle]) -> Dict[str, str]:
        """A parallel group's members in one observation: the Job and its pod
        are read once per build, a barrier's verdicts once per group."""
        statuses: Dict[str, str] = {}
        reads: Dict[str, Tuple[Optional[dict], Optional[dict]]] = {}
        verdicts: Dict[str, Dict[int, str]] = {}
        for handle in handles:
            job_name, container = _split_ref(handle.external_ref)
            if job_name not in reads:
                reads[job_name] = self._read_job_and_pod(job_name)
            job, pod = reads[job_name]
            statuses[handle.external_ref] = self._status_of(job, pod, job_name, container, verdicts)
        return statuses

    def _status_of(
        self,
        job: Optional[dict],
        pod: Optional[dict],
        job_name: str,
        container: str,
        verdicts: Dict[str, Dict[int, str]],
    ) -> str:
        if job is None:
            if job_name in _abandoned_jobs:
                return SKIPPED  # An earlier stage could not start; see its log.
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

        if self._runs_as_sidecar(pod, container):
            return self._member_status(job_name, container, pod, job_status, deadline_exceeded, verdicts)

        status = self._container_status(pod, container)
        if status is None:
            return QUEUED
        if self._cannot_start(job_name, container, status):
            return FAILED

        terminated = (status.get("state") or {}).get("terminated")
        if terminated is not None:
            if int(terminated.get("exitCode") or 0) != 0:
                return TIMEOUT if deadline_exceeded else FAILED
            # Exit 0 is now what EVERY stage does, so the real outcome lives in
            # a log marker. Read it before deciding anything.
            marker = self._exit_marker(job_name, container)
            if marker == "skip":
                return SKIPPED
            if marker == "timeout":
                return TIMEOUT
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

    def _cannot_start(self, job_name: str, container: str, status: dict) -> bool:
        """Whether this stage's container will never start, so the stage fails
        now instead of sitting "running" until the Job's deadline.

        A container kubelet cannot pull or create stays in ``waiting`` for good -
        a tag the registry does not have, a layer the mirror lost - and nothing
        in the pod ever terminates to say so. Pull errors get a short grace,
        because one registry hiccup clears on kubelet's next try; a name that
        can never resolve fails at once. When it gives up it says why in the
        stage's log and deletes the Job, which would otherwise hold its node
        resources retrying the pull until the deadline.
        """
        waiting = (status.get("state") or {}).get("waiting") or {}
        reason = str(waiting.get("reason") or "")
        key = f"{job_name}#{container}"
        if reason not in _START_FAILURES and reason not in _START_FAILURES_AT_ONCE:
            _start_trouble_since.pop(key, None)
            return False
        now = time.monotonic()
        if len(_start_trouble_since) > 500:
            _start_trouble_since.clear()
        first_seen = _start_trouble_since.setdefault(key, now)
        try:
            grace = max(0, int(_env("CI_IMAGE_PULL_GRACE_SECONDS", "60")))
        except ValueError:
            grace = 60
        if reason not in _START_FAILURES_AT_ONCE and now - first_seen < grace:
            return False
        _start_trouble_since.pop(key, None)
        image = str(status.get("image") or "")
        detail = str(waiting.get("message") or "").strip()
        if len(_start_failure_notes) > 500:
            _start_failure_notes.clear()
        _start_failure_notes[key] = [
            f"[kubesight] This stage never started: Kubernetes reports {reason} for {image or 'its image'}.",
            *([f"[kubesight]   {detail}"] if detail else []),
            "[kubesight] "
            + (
                "Check the stage's image name and tag, and that the registry has every layer of it "
                "(a mirror that lost a layer answers 'not found' for a tag it lists)."
                if reason in _PULL_REASONS
                else "A Secret or ConfigMap the container needs is missing, or its settings are invalid."
            ),
        ]
        logger.warning("CI stage %s cannot start (%s): %s", key, reason, detail[:500])
        if len(_abandoned_jobs) > 500:
            _abandoned_jobs.clear()
        _abandoned_jobs[job_name] = now
        _kubectl(
            ["delete", "job", job_name, "-n", _namespace(), "--ignore-not-found=true", "--wait=false"],
            timeout=30,
        )
        return True

    def _container_status(self, pod: dict, container: str) -> Optional[dict]:
        for status in (pod.get("status") or {}).get("initContainerStatuses") or []:
            if status.get("name") == container:
                return status
        return None

    def _is_last_stage(self, pod: dict, container: str) -> bool:
        """Whether this stage is in the build's LAST step, and so must wait for
        the whole pod (post actions + collector) before it may report success.

        The last step is the last stage container — or every member of a
        parallel group that ends the pipeline, since any of them can finish
        last. Post-action cleanup containers (post-N) and group barriers come
        after the stages but are not stages, so only ``stage-N`` names count.
        """
        init = (pod.get("spec") or {}).get("initContainers") or []
        stages = [
            str(c.get("name") or "") for c in init if _STAGE_CONTAINER_RE.match(str(c.get("name") or ""))
        ]
        if not stages:
            return False
        if stages[-1] == container:
            return True
        group = self._group_of(pod, container)
        return bool(group) and _stage_container_name(group[-1]) == stages[-1]

    @staticmethod
    def _stage_container_is_last(pod: dict, container: str) -> bool:
        """The one container the collector's output is attached to: the last
        ``stage-N``, even when the build ends with a group of several."""
        init = (pod.get("spec") or {}).get("initContainers") or []
        stages = [
            str(c.get("name") or "") for c in init if _STAGE_CONTAINER_RE.match(str(c.get("name") or ""))
        ]
        return bool(stages) and stages[-1] == container

    @staticmethod
    def _collector_started(pod: dict) -> bool:
        for status in (pod.get("status") or {}).get("containerStatuses") or []:
            if status.get("name") == "collector":
                state = status.get("state") or {}
                return "running" in state or "terminated" in state
        return False

    @staticmethod
    def _group_layout(pod: dict) -> Dict[str, Any]:
        raw = ((pod.get("metadata") or {}).get("annotations") or {}).get(_GROUPS_ANNOTATION)
        if not raw:
            return {}
        try:
            data = json.loads(raw)
        except (TypeError, ValueError):
            return {}
        return data if isinstance(data, dict) else {}

    def _group_of(self, pod: dict, container: str) -> Optional[List[int]]:
        match = _STAGE_CONTAINER_RE.match(container or "")
        if not match:
            return None
        position = int(match.group(1))
        for group in self._group_layout(pod).get("groups") or []:
            if isinstance(group, list) and position in group:
                return [int(item) for item in group]
        return None

    def _runs_as_sidecar(self, pod: dict, container: str) -> bool:
        """A member of a group laid out side by side. Such a container never
        terminates while the build runs, so it is read off its done marker and
        its barrier, never off its container state."""
        return (
            self._group_layout(pod).get("mode") == parallel_groups.PARALLEL
            and self._group_of(pod, container) is not None
        )

    def _member_status(
        self,
        job_name: str,
        container: str,
        pod: dict,
        job_status: dict,
        deadline_exceeded: bool,
        verdicts: Dict[str, Dict[int, str]],
    ) -> str:
        group = self._group_of(pod, container) or []
        position = int(_STAGE_CONTAINER_RE.match(container).group(1))
        status = self._container_status(pod, container)
        if status is not None and self._cannot_start(job_name, container, status):
            return FAILED
        state = (status or {}).get("state") or {}
        started = bool(
            "running" in state or "terminated" in state or int((status or {}).get("restartCount") or 0)
        )

        outcome: Optional[str] = None
        # The barrier's word is final: it is what decided the fail flag, and
        # it is the only one that knows about a member it stopped waiting for.
        barrier = barrier_container_name(group[0]) if group else ""
        barrier_state = (self._container_status(pod, barrier) or {}).get("state") or {}
        if barrier and "terminated" in barrier_state:
            if barrier not in verdicts:
                verdicts[barrier] = self._barrier_verdicts(job_name, barrier)
            outcome = verdicts[barrier].get(position)
        if outcome is None and started:
            outcome = self._exit_marker(job_name, container, default=None)

        if outcome is None:
            if deadline_exceeded:
                return TIMEOUT
            if int(job_status.get("failed") or 0) > 0:
                return FAILED
            return RUNNING if started else QUEUED
        if outcome == "skip":
            return SKIPPED
        if outcome == "timeout":
            return TIMEOUT
        if outcome == "cancelled":
            return CANCELLED
        last = self._is_last_stage(pod, container)
        if outcome == "failed":
            # As for any stage: a failure in the build's last step still waits
            # for the collector, or its artifacts would never be recorded.
            if last and not self._job_finished(job_status):
                return RUNNING
            return FAILED
        if last:
            if int(job_status.get("succeeded") or 0) > 0:
                return SUCCEEDED
            if deadline_exceeded:
                return TIMEOUT
            if int(job_status.get("failed") or 0) > 0:
                return FAILED
            return RUNNING
        return SUCCEEDED

    def _barrier_verdicts(self, job_name: str, barrier: str) -> Dict[int, str]:
        rc, out, _ = _kubectl(
            ["logs", f"job/{job_name}", "-c", barrier, "-n", _namespace(), "--tail", "200"],
            timeout=20,
        )
        found: Dict[int, str] = {}
        if rc != 0:
            return found
        for line in out.splitlines():
            if not line.startswith(_MEMBER_VERDICT):
                continue
            parts = line[len(_MEMBER_VERDICT):].split()
            if len(parts) == 2 and parts[0].isdigit():
                found[int(parts[0])] = parts[1]
        return found

    def _exit_marker(self, job_name: str, container: str, default: Any = "ok") -> Any:
        """What a stage's own log says about how it ended.

        Every stage exits 0 so the pod reaches the collector, so the container's
        exit code no longer carries the outcome — the marker does. Returns
        "skip", "failed", "timeout" (a group member its own timeout stopped),
        or "ok" — also when no marker is found, which is the pre-wrapper shape
        and means the exit code already told the truth. A group member asks for
        ``default=None`` instead: it is still running when it has no marker.
        """
        rc, out, _ = _kubectl(
            [
                "logs", f"job/{job_name}", "-c", container, "-n", _namespace(),
                "--tail", "5",
            ],
            timeout=20,
        )
        if rc != 0:
            return default
        for line in out.splitlines():
            if line.startswith(_SKIP_MARKER):
                return "skip"
            if line.startswith(_TIMEOUT_MARKER):
                return "timeout"
            if line.startswith(_EXIT_MARKER):
                code = line.replace(_EXIT_MARKER, "").strip()
                return "ok" if code in ("", "0") else "failed"
        return default

    @staticmethod
    def _job_finished(job_status: dict) -> bool:
        return bool(int(job_status.get("succeeded") or 0) or int(job_status.get("failed") or 0))

    def drain_logs(self, handle: RunnerHandle, after_seq: int) -> Iterator[LogChunk]:
        job_name, container = _split_ref(handle.external_ref)
        # A stage that never started has no log of its own; the reason poll
        # found is its whole log.
        note = _start_failure_notes.pop(f"{job_name}#{container}", None)
        if note:
            for index, content in enumerate(note, start=1):
                if index > after_seq:
                    yield LogChunk(seq=index, content=content)
            return
        lines = self._container_log_lines(job_name, container)
        # The collector's output belongs to the last stage's log — it is the
        # only window into artifact upload problems.
        if lines is not None:
            _, pod = self._read_job_and_pod(job_name)
            if pod is not None and self._is_last_stage(pod, container) and self._stage_container_is_last(pod, container):
                status = self._container_status(pod, container)
                # A group member never terminates while the pod runs (it is a
                # sidecar), so for one the collector having started is the
                # sign the stage's own output is complete.
                if status and (
                    (status.get("state") or {}).get("terminated") or self._collector_started(pod)
                ):
                    collector_lines = self._container_log_lines(job_name, "collector") or []
                    lines = lines + [f"[collector] {line}" for line in collector_lines]
        for index, content in enumerate(lines or [], start=1):
            if index > after_seq:
                yield LogChunk(seq=index, content=content)

    def _container_log_lines(self, job_name: str, container: str) -> Optional[List[str]]:
        """The stage's whole output so far, as lines.

        Read in full rather than tailed because a line's sequence number IS its
        position from the start — that is what makes the reader resumable. The
        byte cap is the price: ``--limit-bytes`` counts from the beginning, so a
        stage whose output exceeds it stops appearing to produce anything new
        rather than losing its oldest lines. Generous by default, configurable,
        and it says when it bites instead of going quiet.
        """
        limit = int(_env("CI_LOG_LIMIT_BYTES", "16000000"))
        rc, out, stderr = _kubectl(
            [
                "logs", f"job/{job_name}", "-c", container, "-n", _namespace(),
                "--limit-bytes", str(limit),
            ],
            timeout=30,
        )
        if rc != 0:
            return None  # Container is still waiting; no logs yet.
        lines = out.splitlines()
        if len(out.encode("utf-8", "ignore")) >= limit:
            lines.append(
                f"[kubesight] Output passed {limit} bytes — later lines are not shown. "
                "Raise CI_LOG_LIMIT_BYTES, or have the stage print less."
            )
        return lines

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
