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

from .. import build_environments, cache_layout
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
    record = "" if continue_on_failure else 'if [ "$EC" -ne 0 ]; then : > "$KS_FLAG"; fi\n'
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
        gradle_init=not _is_off(_env("CI_CACHE_GRADLE_INIT", "1"))
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
        f"--import-cache type=registry,ref={ref}{insecure} "
        # mode=max caches intermediate layers too, which is what makes a
        # dependency-heavy build cheap on the second run.
        f"--export-cache type=registry,ref={ref},mode=max{insecure} "
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
    registry = execution.registry or {}
    cache_prelude, cache_flags = _buildctl_cache(execution)
    # The cache prelude runs BEFORE the tag prelude only because neither reads
    # the other; keeping the order fixed keeps the generated script diffable.
    prelude = (cache_prelude + _image_ref_prelude(registry)) if with_prelude else ""
    tag = '$KS_TAG' if registry.get("tagIsTemplate") else registry["tag"]
    image_ref = f"{registry['host']}/{registry['repository']}:{tag}"
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
            f"buildctl --addr {buildkit_addr()} build "
            f"--frontend dockerfile.v0 "
            f"--local context={context} "
            f"--local dockerfile={INLINE_DOCKERFILE_DIR} "
            f"--opt filename=Dockerfile "
            f"{_buildctl_add_hosts(execution)}"
            f"{cache_flags}"
            f"--output {output or _buildctl_output(registry, image_ref)} "
            f"--metadata-file {meta_file}"
        )
    return prelude + (
        f"buildctl --addr {buildkit_addr()} build "
        f"--frontend dockerfile.v0 "
        f"--local context={context} "
        f"--local dockerfile={context}/{dockerfile_dir} "
        f"--opt filename={os.path.basename(dockerfile)} "
        f"{_buildctl_add_hosts(execution)}"
        f"{cache_flags}"
        f"--output {output or _buildctl_output(registry, image_ref)} "
        f"--metadata-file {meta_file}"
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
# ephemeral storage (see _stage_resources, which raises the default for exactly
# these stages).
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
    echo "[kubesight] {tools_image}" >&2
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
    registry = execution.registry or {}
    scan = execution.image_scan or {}
    position = execution.position
    archive = image_archive_path(position)
    report = scan_report_path(position)

    tag = "$KS_TAG" if registry.get("tagIsTemplate") else registry["tag"]
    image_ref = f"{registry['host']}/{registry['repository']}:{tag}"
    insecure = " --insecure" if registry.get("verifyTls") is False else ""

    threshold = str(scan.get("threshold") or "critical").lower()
    on_fail = str(scan.get("onFail") or "block").lower()
    gated = _gated_severities(threshold)
    ignore_unfixed = " --ignore-unfixed" if scan.get("ignoreUnfixed") else ""

    db_repo = _env("CI_TRIVY_DB_REPOSITORY", "").strip()
    db_flag = f" --db-repository {db_repo}" if db_repo else ""

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
        output=f"type=docker,name={image_ref},dest={archive}",
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
        + _TOOL_GUARD.format(tools_image=image_tools_image())
        + prelude
        + f'echo "[kubesight] == build == {image_ref}"\n'
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
        + f'echo "[kubesight] == push == {image_ref}"\n'
        + f"crane push{insecure} {archive} {image_ref}\n"
        # The digest comes from the registry rather than from the build, so the
        # artifact record names the manifest that is actually pullable.
        + f"KS_DIGEST=$(crane digest{insecure} {image_ref})\n"
        + (
            "printf '{\"image.name\":\"%s\",\"containerimage.digest\":\"%s\"}\\n' "
            f'"{image_ref}" "$KS_DIGEST" > {meta_file}\n'
        )
        # A multi-gigabyte archive on a shared emptyDir would otherwise sit
        # there for the rest of the build, against the same workspace size limit
        # every later stage has to fit inside.
        + f"rm -f {archive}\n"
        + f'echo "[kubesight] Pushed {image_ref} ($KS_DIGEST)"\n'
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


def _stage_resources(execution: StageExecution) -> Dict[str, Any]:
    limits = {
        "cpu": (execution.resources or {}).get("cpu") or _env("CI_STAGE_CPU_LIMIT", "2"),
        "memory": (execution.resources or {}).get("memory") or _env("CI_STAGE_MEMORY_LIMIT", "4Gi"),
    }
    requests = dict(_DEFAULT_REQUESTS)

    # The scheduler matches REQUESTS. A limit with no request makes Kubernetes
    # default the request to the limit, so the ephemeral-storage cap below would
    # silently demand its full size on every node — unschedulable on hosts with
    # small root disks. Request a modest floor explicitly and let the limit cap.
    #
    # Either half can be set to "off" for a cluster that would rather not account
    # for build disk at all. Dropping the limit lets a runaway build fill the
    # node; dropping the request makes this pod the first thing kubelet evicts
    # when some other tenant fills it, because eviction ranks by usage above
    # request. Off on both is a deliberate "the node has disk to spare", not a
    # fix for a node that is already tight.
    # A scanned image stage holds the whole image as an uncompressed archive on
    # the workspace between the build and the push, which the 2Gi that suits a
    # compile stage does not fit. Raised only for those stages, and still
    # overridable per stage — a limit that silently applied everywhere would
    # make every other stage unschedulable on a small node for no reason.
    default_ephemeral = (
        _env("CI_IMAGE_SCAN_EPHEMERAL_LIMIT", "8Gi")
        if execution.stage_type == "container_image" and scanning_requested(execution)
        else _env("CI_STAGE_EPHEMERAL_LIMIT", "2Gi")
    )
    ephemeral_limit = (execution.resources or {}).get("ephemeralStorage") or default_ephemeral
    if not _is_off(ephemeral_limit):
        limits["ephemeral-storage"] = ephemeral_limit

    ephemeral_request = _env("CI_STAGE_EPHEMERAL_REQUEST", "256Mi")
    if not _is_off(ephemeral_request):
        requests["ephemeral-storage"] = ephemeral_request

    return {"requests": requests, "limits": limits}



def _workspace_medium(plan: Optional[List[StageExecution]] = None) -> Dict[str, Any]:
    """The /workspace emptyDir. Its sizeLimit is a ceiling kubelet enforces by
    evicting the pod, independent of the per-container ephemeral-storage limit —
    so "off" has to be honoured here too, or removing the limits above still
    leaves a cap in place.

    A scanned image stage parks the whole image here as an archive between the
    build and the push, so the default that fits a checkout plus build output
    does not fit it. Raising the container's ephemeral limit alone would not
    help: kubelet evicts on whichever ceiling is hit first, and the eviction
    reads as the pod dying for no stated reason halfway through a build.
    """
    scanned = any(
        item.stage_type == "container_image" and scanning_requested(item)
        for item in (plan or [])
    )
    size = (
        _env("CI_IMAGE_SCAN_WORKSPACE_SIZE_LIMIT", "8Gi")
        if scanned
        else _env("CI_WORKSPACE_SIZE_LIMIT", "5Gi")
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


def _cache_runtime() -> Dict[str, str]:
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


def _tool_cache_env(base: str) -> Dict[str, str]:
    """Every build tool's cache variable, from the shared layout.

    Kept as a thin wrapper rather than inlined at the call site because the
    agent runner and the maintenance Jobs need the identical mapping, and a
    second copy of it is a second thing to forget to update.
    """
    return cache_layout.tool_env(base)


def _plain_env(execution: StageExecution, extra: Dict[str, str]) -> List[Dict[str, Any]]:
    cache_base = cache_base_path(execution.service_slug)
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
        **_tool_cache_env(cache_base),
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
                    _wrap_stage_script(
                        image_stage_script(execution, meta_file),
                        continue_on_failure=bool(execution.continue_on_failure),
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
