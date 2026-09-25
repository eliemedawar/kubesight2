#!/usr/bin/env python3
"""KubeSight build agent.

Runs on a machine KubeSight cannot reach — a Mac mini that builds iOS, a VM with
a licensed toolchain, a box behind a firewall — and pulls work from KubeSight
rather than waiting to be pushed to.

    python3 kubesight-agent.py --url https://kubesight.example.com --token <TOKEN>

Deliberately one file with no dependencies beyond the Python standard library,
and written to Python 3.6 — the machines this runs on are somebody's laptop or a
long-lived build host, where "pip install" is often the step that does not
happen and the system python3 can be years old. 3.6 is what RHEL/CentOS 7
ships, and those are exactly the hosts an agent exists to reach.

What it does, in a loop:
    heartbeat  say it is alive and what it has installed
    claim      ask for one task; 204 means nothing to do
    run        execute in a per-build workspace directory, streaming output
    upload     send declared artifacts
    report     post the exit code

Everything a build prints is shown here as well as sent to KubeSight, framed by
a rule when a stage starts and its verdict when it ends. The terminal you
started the agent in is usually the first place somebody looks when a build on
this machine misbehaves, and a machine-specific failure — a missing tool, a
permission, a full disk — is often legible here before it is anywhere else.
``--quiet`` drops the output; the verdict is printed either way, because an
agent that shows a stage starting and never says how it ended has told you
something false.

About containers: an agent uses the machine as it is — that is the point, and
the only way an iOS build works at all. But on Linux, where a container is just
a process, a stage that declares an image runs *inside* that image when this
machine has docker or podman. The machine then needs no JDK, no Gradle and no
Node of its own, and the same pipeline produces the same build here as it does
in the cluster. Everything else (macOS, no runtime, no image on the stage) runs
directly on the machine exactly as before.

A stage can say so explicitly with KUBESIGHT_CONTAINER in its environment:
    auto    (default) in a container when this machine can, otherwise here
    always  refuse to run the stage outside a container
    never   always run on the machine itself
"""

import argparse
import base64
import glob
import json
import mimetypes
import os
import platform
import re
import shutil
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
import uuid
from typing import Any, Dict, List, Optional

VERSION = "1.2.0"

# Keep agent data off root's small filesystem by default. The directory remains
# configurable from KubeSight, --workspace, or KUBESIGHT_AGENT_WORKSPACE.
DEFAULT_WORKSPACE = os.getenv(
    "KUBESIGHT_AGENT_WORKSPACE", "/data/kubesight-agent"
).strip() or "/data/kubesight-agent"
WORKSPACE_MARKER = ".kubesight-agent-workspace"
DEFAULT_WORKSPACE_RETENTION_HOURS = 24.0

# Runtimes to look for, in order. Podman's CLI is compatible with the subset
# used here, and rootless podman maps the container user to the invoking user,
# which is the friendlier default for file ownership in the workspace.
CONTAINER_RUNTIMES = ("docker", "podman")
# Where a containerised stage sees its workspace. Deliberately the SAME paths
# the Kubernetes runner uses, so one pipeline's commands are correct on both.
CONTAINER_WORKSPACE = "/workspace"
CONTAINER_SOURCE = "/workspace/source"
CONTAINER_CACHE = "/cache"
CONTAINER_HOME = "/workspace/.home"
# Where a stage leaves values for the stages after it (see build_env_prelude).
CONTAINER_BUILD_ENV = "/workspace/.kubesight/build.env"
# Every container this agent starts carries this label, so it can clean up
# after a crash without touching anything else on the machine.
CONTAINER_LABEL = "kubesight.agent=1"
# How long an idle agent waits between claim attempts. Low because a claim is a
# single indexed lookup and the wait is exactly what somebody watching the
# Builds tab experiences as "the runner has not picked it up yet". KubeSight can
# lower it further for the whole fleet (CI_AGENT_POLL_SECONDS) — see the
# heartbeat reply — unless --poll pins it here.
DEFAULT_POLL_SECONDS = 2.0
# Right after finishing a task, ask again on this much shorter beat for a few
# seconds: the stage that follows is being queued as we report, so the next task
# is usually already there.
EAGER_POLL_SECONDS = 0.3
EAGER_WINDOW_SECONDS = 6.0

# 3.6 is the floor deliberately: it is what RHEL/CentOS 7 ships, and those are
# exactly the long-lived build hosts an agent exists to reach. Everything here
# stays inside 3.6 — no f-strings, no subprocess.run(capture_output=...), no
# `from __future__ import annotations`.
if sys.version_info < (3, 6):
    sys.stderr.write(
        "The KubeSight agent needs Python 3.6 or newer; this is %d.%d.\n"
        "Try an explicit interpreter (python3.6, python3.8) if one is installed.\n"
        % (sys.version_info[0], sys.version_info[1])
    )
    raise SystemExit(2)


# ---------------------------------------------------------------------------
# Transport
# ---------------------------------------------------------------------------

class Client:
    def __init__(self, url: str, token: str, verify_tls: bool = True):
        self.base = url.rstrip("/") + "/api/ci/agent"
        self.token = token
        self.context = None
        if not verify_tls:
            import ssl

            self.context = ssl._create_unverified_context()

    def _request(self, path: str, data: Optional[bytes], headers: Dict[str, str],
                 method: str = "POST", timeout: int = 60):
        request = urllib.request.Request(
            self.base + path, data=data, method=method,
            headers={"Authorization": "Bearer " + self.token, **headers},
        )
        return urllib.request.urlopen(request, timeout=timeout, context=self.context)

    def post_json(self, path: str, payload: Dict[str, Any], timeout: int = 60):
        body = json.dumps(payload).encode("utf-8")
        try:
            with self._request(path, body, {"Content-Type": "application/json"},
                               timeout=timeout) as response:
                if response.status == 204:
                    return None
                text = response.read().decode("utf-8", "replace")
                return json.loads(text).get("data") if text else None
        except urllib.error.HTTPError as exc:
            if exc.code == 204:
                return None
            detail = exc.read().decode("utf-8", "replace")[:400]
            raise RuntimeError("%s %s: %s" % (exc.code, path, detail)) from None

    def post_file(self, path: str, fields: Dict[str, str], file_path: str, timeout: int = 900):
        """Multipart upload, hand-rolled to avoid a dependency."""
        boundary = uuid.uuid4().hex
        buffer = bytearray()
        for key, value in fields.items():
            buffer += ("--%s\r\nContent-Disposition: form-data; name=\"%s\"\r\n\r\n%s\r\n"
                       % (boundary, key, value)).encode("utf-8")
        filename = os.path.basename(file_path)
        content_type = mimetypes.guess_type(filename)[0] or "application/octet-stream"
        buffer += ("--%s\r\nContent-Disposition: form-data; name=\"file\"; filename=\"%s\"\r\n"
                   "Content-Type: %s\r\n\r\n" % (boundary, filename, content_type)).encode("utf-8")
        with open(file_path, "rb") as handle:
            buffer += handle.read()
        buffer += ("\r\n--%s--\r\n" % boundary).encode("utf-8")
        with self._request(
            path, bytes(buffer),
            {"Content-Type": "multipart/form-data; boundary=" + boundary},
            timeout=timeout,
        ) as response:
            response.read()


# ---------------------------------------------------------------------------
# What this machine can do
# ---------------------------------------------------------------------------

_runtime_cache = None          # type: Optional[str]
_containers_disabled = False   # --no-container
_runtime_pinned = ""           # --runtime docker|podman
_echo_output = True            # --quiet turns this off


def echo(text: str, stream: str = "stdout") -> None:
    """Write one line to the terminal this agent is running in.

    Build output goes to KubeSight either way; this is the copy for whoever is
    watching the machine. Encoding is handled rather than assumed: a build host
    with LANG=C gives Python an ASCII stdout, and an em dash or a tick in a
    build's own output would then raise UnicodeEncodeError out of the middle of
    the streaming loop and take the stage down with it. A log line must never
    be able to do that, so anything unprintable here is replaced and the line
    still appears.
    """
    handle = sys.stderr if stream == "stderr" else sys.stdout
    try:
        handle.write(text + "\n")
    except UnicodeEncodeError:
        encoding = getattr(handle, "encoding", None) or "ascii"
        handle.write(text.encode(encoding, "replace").decode(encoding, "replace") + "\n")
    except Exception:
        return  # A closed or broken stdout is not a reason to fail a build.
    try:
        handle.flush()
    except Exception:
        pass


def container_runtime() -> str:
    """"docker", "podman", or "" when this machine has neither usable.

    Probed once: the answer cannot change without the agent being restarted,
    and every stage would otherwise pay for the lookup. ``--runtime`` pins the
    choice, so a machine that gets podman installed later does not quietly
    change how its builds run.
    """
    global _runtime_cache
    if _containers_disabled:
        return ""
    if _runtime_cache is None:
        _runtime_cache = ""
        # Linux only. A container on macOS is a Linux VM, which is precisely
        # what an iOS build cannot use, and silently running a mac stage in
        # Linux would be worse than not containerising at all.
        if platform.system().lower() == "linux":
            candidates = (_runtime_pinned,) if _runtime_pinned else CONTAINER_RUNTIMES
            for name in candidates:
                if shutil.which(name) is None:
                    continue
                try:
                    probe = subprocess.run(
                        [name, "version", "--format", "{{.Client.Version}}"],
                        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                        timeout=20, check=False,
                    )
                except Exception:
                    continue
                # A runtime whose daemon is unreachable is not a runtime: the
                # client exists but every run would fail.
                if probe.returncode == 0:
                    _runtime_cache = name
                    break
    return _runtime_cache


def prune_containers(shipper=None) -> None:
    """Remove containers this agent left behind, e.g. after being killed."""
    runtime = container_runtime()
    if not runtime:
        return
    try:
        listed = subprocess.run(
            [runtime, "ps", "-aq", "--filter", "label=" + CONTAINER_LABEL],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=30, check=False,
        )
        ids = [line.strip() for line in (listed.stdout or b"").decode().splitlines() if line.strip()]
        if not ids:
            return
        subprocess.run([runtime, "rm", "-f"] + ids,
                       stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=60, check=False)
        message = "[agent] removed %d leftover container(s)" % len(ids)
        print(message)
        if shipper is not None:
            shipper.add(message)
    except Exception as exc:
        print("[agent] container cleanup failed: %s" % exc, file=sys.stderr)


def build_workspace(root: str, workspace_ref: Any) -> str:
    """Return one direct child of ``root``, refusing path traversal.

    Workspace names normally come from KubeSight as ``service-buildNumber``.
    Treating that value as an arbitrary path would let a malformed task make
    cleanup remove something outside the agent's data directory.
    """
    root_path = os.path.realpath(os.path.abspath(root))
    name = str(workspace_ref or "build").strip()
    if not name or os.path.isabs(name):
        raise RuntimeError("Invalid build workspace name: %s" % name)
    candidate = os.path.realpath(os.path.join(root_path, name))
    if os.path.dirname(candidate) != root_path:
        raise RuntimeError("Build workspace must be directly under %s" % root_path)
    return candidate


def mark_workspace(workspace: str) -> None:
    """Mark/touch an agent-owned build directory for crash cleanup."""
    marker = os.path.join(workspace, WORKSPACE_MARKER)
    with open(marker, "a"):
        os.utime(marker, None)


def remove_build_workspace(root: str, workspace_ref: Any, shipper=None) -> bool:
    """Remove exactly one completed build workspace, never the root/cache."""
    try:
        workspace = build_workspace(root, workspace_ref)
        if not os.path.lexists(workspace):
            return False
        if os.path.islink(workspace):
            raise RuntimeError("refusing to clean a symlink")
        shutil.rmtree(workspace)
        message = "[agent] cleaned completed workspace %s" % workspace
        print(message)
        if shipper is not None:
            shipper.add(message)
        return True
    except Exception as exc:
        print("[agent] workspace cleanup failed for %s: %s" % (workspace_ref, exc),
              file=sys.stderr)
        return False


def cleanup_stale_workspaces(root: str, max_age_hours: float = 24.0) -> int:
    """Remove marked workspaces left by a crash or lost final response.

    Only direct children carrying this agent's marker are eligible. The shared
    ``.cache`` and any unrelated directory under /data are never considered.
    Markers are touched at both ends of every stage, so a long build is not
    mistaken for an abandoned one between stages.
    """
    if max_age_hours <= 0:
        return 0
    cutoff = time.time() - max_age_hours * 3600.0
    removed = 0
    try:
        names = os.listdir(root)
    except OSError as exc:
        print("[agent] cannot scan workspaces in %s: %s" % (root, exc), file=sys.stderr)
        return 0
    for name in names:
        try:
            workspace = build_workspace(root, name)
            marker = os.path.join(workspace, WORKSPACE_MARKER)
            if (
                name == ".cache"
                or os.path.islink(workspace)
                or not os.path.isfile(marker)
                or os.path.getmtime(marker) > cutoff
            ):
                continue
            if remove_build_workspace(root, name):
                removed += 1
        except (OSError, RuntimeError):
            continue
    return removed


def detect_capabilities() -> List[str]:
    """Tools actually present, not tools somebody typed into a form.

    Reported on every heartbeat so the list follows the machine: uninstall Xcode
    and stages requiring it stop being routed here, instead of failing on it.
    """
    # Report the platform this actually is. Anything else lets a machine claim
    # work it cannot do — a Windows host advertising "macos" would be handed an
    # iOS build and fail it at the first xcodebuild.
    system = platform.system().lower()
    found = [{"linux": "linux", "darwin": "macos", "windows": "windows"}.get(system, system)]
    probes = {
        "git": ["git", "--version"],
        "java": ["java", "-version"],
        "maven": ["mvn", "-v"],
        "gradle": ["gradle", "-v"],
        "node": ["node", "--version"],
        "python3": ["python3", "--version"],
        "docker": ["docker", "--version"],
        "podman": ["podman", "--version"],
        "xcode": ["xcodebuild", "-version"],
        "fastlane": ["fastlane", "--version"],
    }
    for name, command in probes.items():
        if shutil.which(command[0]) is None:
            continue
        try:
            subprocess.run(
                command, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                timeout=20, check=False,
            )
            found.append(name)
        except Exception:
            pass
    machine = platform.machine().lower()
    if machine:
        found.append("arm64" if machine in ("arm64", "aarch64") else machine)
    # Reported as a capability of its own so a stage that must be containerised
    # can be routed here by label, rather than discovering the machine's
    # toolchain the hard way.
    if container_runtime():
        found.append("container")
    return found


# ---------------------------------------------------------------------------
# Running one task
# ---------------------------------------------------------------------------

class LogShipper:
    """Batches output and posts it a few times a second.

    Batched because a chatty build would otherwise be one HTTP request per line;
    time-bounded because a person watching the build should see it move.

    It also echoes each line to this machine's terminal as it arrives, which is
    the *un*batched copy: somebody watching the agent should see a build move
    line by line, and — when the network to KubeSight is the thing that broke —
    should still see it at all. Batching there is an optimisation for HTTP;
    there is nothing to optimise for a local write.
    """

    def __init__(self, client: Client, task_id: int, claim_token: str):
        self.client, self.task_id, self.claim_token = client, task_id, claim_token
        self.lines: List[Dict[str, str]] = []
        self.lock = threading.Lock()
        self.stop_event = threading.Event()
        self.thread = threading.Thread(target=self._loop, daemon=True)
        self.thread.start()

    def add(self, content: str, stream: str = "stdout") -> None:
        line = content.rstrip("\n")
        with self.lock:
            self.lines.append({"content": line, "stream": stream})
        if _echo_output:
            echo(line, stream)

    def _flush(self) -> None:
        with self.lock:
            batch, self.lines = self.lines, []
        if not batch:
            return
        try:
            self.client.post_json(
                "/tasks/%d/logs" % self.task_id,
                {"claimToken": self.claim_token, "lines": batch},
            )
        except Exception as exc:  # Losing a log line must not fail the build.
            print("[agent] log upload failed: %s" % exc, file=sys.stderr)

    def _loop(self) -> None:
        while not self.stop_event.wait(1.0):
            self._flush()

    def close(self) -> None:
        self.stop_event.set()
        self.thread.join(timeout=5)
        self._flush()


def git_username(credential_type: str) -> str:
    # Matches the in-cluster checkout: an Atlassian API token clones as
    # x-bitbucket-api-token-auth even though it pairs with an email elsewhere.
    return "x-bitbucket-api-token-auth" if credential_type == "api_token" else "x-token-auth"


def run_checkout(task: Dict[str, Any], workspace: str, shipper: LogShipper) -> int:
    spec = task.get("checkout") or {}
    url, revision = spec.get("url") or "", spec.get("revision") or ""
    source = os.path.join(workspace, "source")
    if os.path.isdir(source):
        shutil.rmtree(source, ignore_errors=True)

    auth = base64.b64encode(
        ("%s:%s" % (git_username(spec.get("credentialType") or ""), spec.get("token") or "")).encode()
    ).decode()
    env = {
        **os.environ,
        # Credentials as git config in the environment: never in argv, so they
        # cannot be read out of the process list on a shared machine.
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_CONFIG_COUNT": "1",
        "GIT_CONFIG_KEY_0": "http.extraHeader",
        "GIT_CONFIG_VALUE_0": "Authorization: Basic " + auth,
    }
    shipper.add("Cloning %s" % url)
    code = stream(["git", "clone", "--no-tags", "--depth", "1", "--branch", revision, url, source],
                  workspace, env, shipper, quiet_fail=True) if revision else 1
    if code != 0:
        # A pinned commit cannot be expressed as --branch, so fall back.
        code = stream(["git", "clone", "--no-tags", "--depth", "50", url, source],
                      workspace, env, shipper)
        if code == 0 and revision:
            code = stream(["git", "checkout", "--quiet", revision], source, env, shipper)
    if code == 0:
        shipper.add("Checkout complete.")
    return code


def stream(command: List[str], cwd: str, env: Dict[str, str], shipper: LogShipper,
           quiet_fail: bool = False, timeout: Optional[int] = None) -> int:
    """Run a command, shipping its output as it appears."""
    try:
        process = subprocess.Popen(
            command, cwd=cwd, env=env, stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT, universal_newlines=True, bufsize=1,
        )
    except FileNotFoundError:
        if not quiet_fail:
            shipper.add("command not found: %s" % command[0], "stderr")
        return 127
    assert process.stdout is not None
    for line in process.stdout:
        shipper.add(line, "stdout")
    try:
        return process.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        # Without this the timed-out process keeps running and keeps holding
        # the workspace — and for a container, keeps burning the machine.
        try:
            process.kill()
        except Exception:
            pass
        raise


# Where the server's node_modules wrapper keeps its archives. The agent's cache
# is ONE folder for every service, so the archives get a directory per service
# under it - the server sends the name already made safe (cacheSlug), and it is
# checked again here because it becomes a path on this machine.
NODE_MODULES_DIR = "node-modules"
_CACHE_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}$")


def node_modules_dir(cache: str, task: Dict[str, Any]) -> str:
    """``$KUBESIGHT_NODE_MODULES_DIR`` for this task, or "" to leave it unset."""
    slug = str(task.get("cacheSlug") or "")
    if not _CACHE_SLUG_RE.match(slug):
        return ""
    return cache.rstrip("/") + "/" + NODE_MODULES_DIR + "/" + slug


def container_mode(task: Dict[str, Any]) -> str:
    """auto (default), always, or never — from the stage's own environment."""
    raw = str((task.get("env") or {}).get("KUBESIGHT_CONTAINER", "auto")).strip().lower()
    return raw if raw in ("auto", "always", "never") else "auto"


def container_tool_env(cache: str) -> Dict[str, str]:
    """Point the usual build tools at the mounted cache.

    The same idea as the Kubernetes runner's cache injection, and the reason a
    containerised stage is not slower than a host one: without this, every
    build starts with an empty ~/.gradle inside a fresh container.
    """
    return {
        "MAVEN_OPTS": "-Dmaven.repo.local=" + cache + "/maven",
        "GRADLE_USER_HOME": cache + "/gradle",
        # Gradle has no environment variable for its build cache directory, so
        # on Kubernetes KubeSight writes an init script that reads this one. An
        # agent build gets the variable but not the init script, so a pipeline
        # that wants a shared build cache here passes -I itself. Named anyway,
        # so the same stage text is valid on both runners.
        "GRADLE_BUILD_CACHE_DIR": cache + "/gradle-build-cache",
        "DC_DATA_DIR": cache + "/dependency-check-data",
        "SEMGREP_CACHE_DIR": cache + "/semgrep",
        "SEMGREP_VERSION_CACHE_PATH": cache + "/semgrep/version",
        "BUILDKIT_CACHE_DIR": cache + "/buildkit",
        "npm_config_cache": cache + "/npm",
        "YARN_CACHE_FOLDER": cache + "/yarn",
        "PIP_CACHE_DIR": cache + "/pip",
        "GOMODCACHE": cache + "/go/mod",
        "GOCACHE": cache + "/go/build",
        "XDG_CACHE_HOME": cache + "/xdg",
    }


def build_env_prelude(path):
    """Shell that makes an earlier stage's exports available to this one.

    A stage appends ``NAME=value`` lines to $KUBESIGHT_ENV; every later stage
    sources the file first. The Kubernetes runner does exactly the same thing at
    /workspace/.kubesight/build.env, which is what lets one pipeline compute a
    version once and use it in five stages on either runner.
    """
    return (
        'export KUBESIGHT_ENV="%s"\n'
        'mkdir -p "$(dirname "$KUBESIGHT_ENV")" 2>/dev/null || true\n'
        'if [ -s "$KUBESIGHT_ENV" ]; then . "$KUBESIGHT_ENV"; fi\n'
    ) % path


def container_command(runtime, task, workspace, cache, script, uid_gid=None,
                      selinux=True):
    """The argv for running one stage inside its image.

    Secrets are passed as ``-e NAME`` with NO value: the runtime reads them
    from this process's own environment, so they never appear in argv where
    any user on the machine could read them with ps — the same rule the git
    token follows.
    """
    name = "kubesight-task-%s" % task.get("taskId")
    workdir = CONTAINER_SOURCE
    if task.get("workingDirectory"):
        workdir = CONTAINER_SOURCE + "/" + str(task["workingDirectory"]).strip("/")

    # :z lets SELinux hosts (RHEL, Fedora) read the bind mount at all; without
    # it every command in the container fails with Permission denied.
    suffix = ":z" if selinux else ""
    argv = [
        runtime, "run", "--rm",
        "--name", name,
        "--label", CONTAINER_LABEL,
        "--label", "kubesight.build=%s" % task.get("buildId"),
        "-v", "%s:%s%s" % (workspace, CONTAINER_WORKSPACE, suffix),
        "-v", "%s:%s%s" % (cache, CONTAINER_CACHE, suffix),
        "-w", workdir,
    ]
    if uid_gid:
        # Files land owned by the agent's user, so the next stage — and the
        # cleanup — can still touch them.
        argv += ["--user", uid_gid]

    # Non-secret, and useful in a log: these are the paths the pipeline text
    # refers to, identical to the Kubernetes runner's.
    fixed = {
        "HOME": CONTAINER_HOME,
        "TMPDIR": "/tmp",
        "KUBESIGHT_WORKSPACE": CONTAINER_WORKSPACE,
        "KUBESIGHT_SOURCE": CONTAINER_SOURCE,
        "KUBESIGHT_CACHE": CONTAINER_CACHE,
        # The name the Kubernetes runner leads with. Same value; a stage script
        # written against either one runs unchanged on both runners.
        "KUBESIGHT_CACHE_DIR": CONTAINER_CACHE,
        "KUBESIGHT_ENV": CONTAINER_BUILD_ENV,
    }
    fixed.update(container_tool_env(CONTAINER_CACHE))
    nm_dir = node_modules_dir(CONTAINER_CACHE, task)
    if nm_dir:
        fixed["KUBESIGHT_NODE_MODULES_DIR"] = nm_dir
    stage_env = {str(k): str(v) for k, v in (task.get("env") or {}).items()}
    for key in sorted(fixed):
        # A stage that sets one of these itself wins, exactly as on Kubernetes.
        if key not in stage_env:
            argv += ["-e", "%s=%s" % (key, fixed[key])]
    for key in sorted(stage_env):
        argv += ["-e", key]

    for alias in task.get("hostAliases") or []:
        ip = str((alias or {}).get("ip") or "").strip()
        for host in (alias or {}).get("hostnames") or []:
            if ip and host:
                argv += ["--add-host", "%s:%s" % (str(host).strip(), ip)]

    resources = task.get("resources") or {}
    if resources.get("memory"):
        argv += ["--memory", str(resources["memory"])]
    if resources.get("cpu"):
        argv += ["--cpus", str(resources["cpu"])]

    argv += [str(task.get("image") or ""), "/bin/sh", "-e", "-c", script]
    return argv


def container_user(runtime: str) -> Optional[str]:
    """The --user to run a stage as, or None to leave it to the runtime.

    Docker: this process's own uid, so files in the workspace come back owned
    by the agent rather than by root.

    Rootless podman: None, deliberately. There, the container's root IS the
    invoking user through the user namespace, so ownership is already right —
    and forcing --user 1000:1000 maps into the *subuid* range instead, leaving
    files the agent's own user cannot delete.
    """
    if not hasattr(os, "getuid"):
        return None
    if runtime == "podman" and os.getuid() != 0:
        return None
    return "%d:%d" % (os.getuid(), os.getgid())


def pull_image(runtime: str, image: str, shipper: LogShipper) -> None:
    """Pull only when the image is absent, and say so.

    An unexplained sixty-second pause at the start of a stage reads as a hung
    build, so the pull is announced rather than hidden inside `run`.
    """
    present = subprocess.run(
        [runtime, "image", "inspect", image],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False,
    )
    if present.returncode == 0:
        return
    shipper.add("[agent] pulling %s" % image)
    stream([runtime, "pull", image], None, dict(os.environ), shipper)


def run_task(client: Client, task: Dict[str, Any], root: str) -> int:
    task_id, claim = task["taskId"], task["claimToken"]
    workspace_ref = str(task.get("workspace") or "build")
    workspace = build_workspace(root, workspace_ref)
    os.makedirs(workspace, exist_ok=True)
    mark_workspace(workspace)
    shipper = LogShipper(client, task_id, claim)
    exit_code, error = 1, None
    started = time.time()
    cleanup_workspace = False

    try:
        if task.get("stageType") == "checkout":
            exit_code = run_checkout(task, workspace, shipper)
        else:
            cwd = os.path.join(workspace, "source")
            if task.get("workingDirectory"):
                cwd = os.path.join(cwd, task["workingDirectory"])
            if not os.path.isdir(cwd):
                # Nothing checked out: say what is wrong rather than failing on
                # the first command with a confusing "no such file".
                shipper.add("No checkout at %s - did the checkout stage run here?" % cwd, "stderr")
                raise RuntimeError("workspace missing")
            env = {
                **os.environ,
                # The same names the Kubernetes runner exports, pointing at this
                # machine's directories, so one pipeline runs on either.
                "KUBESIGHT_WORKSPACE": workspace,
                "KUBESIGHT_SOURCE": os.path.join(workspace, "source"),
            }
            # A stage running on the machine itself (a Mac, or Linux without
            # docker/podman) keeps node_modules under the same cache folder a
            # container would mount. Set before the stage's own Environment, so
            # a stage can still point it elsewhere.
            host_nm_dir = node_modules_dir(os.path.join(root, ".cache"), task)
            if host_nm_dir:
                env["KUBESIGHT_NODE_MODULES_DIR"] = host_nm_dir
            env.update({str(k): str(v) for k, v in (task.get("env") or {}).items()})
            build_env = os.path.join(workspace, ".kubesight", "build.env")
            env["KUBESIGHT_ENV"] = build_env
            # Prefixed rather than appended: a stage overrides an inherited
            # value by assigning it, which only works if the file is read first.
            script = build_env_prelude(
                CONTAINER_BUILD_ENV if str(task.get("image") or "").strip() else build_env
            ) + "\n".join(task.get("commands") or ["true"])
            timeout = int(task.get("timeoutSeconds") or 1800)
            image = str(task.get("image") or "").strip()
            mode = container_mode(task)
            runtime = "" if mode == "never" else container_runtime()

            if image and runtime and mode != "never":
                cache = os.path.join(root, ".cache")
                home = os.path.join(workspace, ".home")
                for path in (cache, home):
                    os.makedirs(path, exist_ok=True)
                uid_gid = container_user(runtime)
                shipper.add("[agent] running in %s (%s)" % (image, runtime))
                pull_image(runtime, image, shipper)
                argv = container_command(
                    runtime, task, workspace, cache, script, uid_gid=uid_gid
                )
                try:
                    exit_code = stream(argv, None, env, shipper, timeout=timeout)
                finally:
                    # The container outlives a killed client, so removing it by
                    # name is what actually ends the stage's work.
                    subprocess.run(
                        [runtime, "rm", "-f", "kubesight-task-%s" % task_id],
                        stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False,
                    )
            elif image and mode == "always":
                raise RuntimeError(
                    "This stage requires a container (KUBESIGHT_CONTAINER=always) and this "
                    "machine has no usable docker or podman."
                )
            else:
                if image:
                    # Never silent: the build that just ran is not the build the
                    # image would have produced.
                    shipper.add(
                        "[agent] stage image %s ignored: no container runtime here, so the "
                        "machine's own tools are used" % image
                    )
                exit_code = stream(
                    ["/bin/sh", "-e", "-c", script], cwd, env, shipper, timeout=timeout
                )
        if exit_code == 0:
            upload_artifacts(client, task, workspace, shipper)
    except subprocess.TimeoutExpired:
        exit_code, error = 124, "The stage exceeded its timeout."
        shipper.add(error, "stderr")
    except Exception as exc:
        exit_code, error = 1, str(exc)
        shipper.add("[agent] %s" % exc, "stderr")
    finally:
        # A fresh timestamp keeps a multi-stage build safe from the stale sweep
        # while the server queues its next stage.
        try:
            mark_workspace(workspace)
        except OSError as exc:
            print("[agent] could not update workspace marker: %s" % exc, file=sys.stderr)
        shipper.close()
        try:
            response = client.post_json(
                "/tasks/%d/result" % task_id,
                {"claimToken": claim, "exitCode": exit_code, "error": error},
            )
            cleanup_workspace = bool((response or {}).get("cleanupWorkspace"))
        except Exception as exc:
            print("[agent] result post failed: %s" % exc, file=sys.stderr)
        # Printed even under --quiet: the outcome of a stage is the one line
        # nobody watching this machine can do without, and a failure that is
        # only visible in a browser is a failure this terminal lied about.
        echo(
            "[agent] build #%s stage '%s' %s"
            % (
                task.get("buildNumber"),
                task.get("stageName"),
                _outcome(exit_code, error, time.time() - started),
            ),
            "stdout" if exit_code == 0 else "stderr",
        )
        # The server only says yes once every stage is terminal. Artifacts and
        # the result are already safely uploaded before anything is removed.
        if cleanup_workspace:
            remove_build_workspace(root, workspace_ref)
    return exit_code


def _outcome(exit_code: int, error: Optional[str], seconds: float) -> str:
    """The one-line verdict on a finished stage.

    The exit code is named rather than shown alone: "exit 127" is a fact, "the
    command does not exist on this machine" is the answer, and on an agent —
    where the toolchain is whatever somebody installed — that distinction is
    usually the whole debugging session.
    """
    took = "%.0fs" % seconds if seconds < 90 else "%.1fm" % (seconds / 60.0)
    if exit_code == 0:
        return "succeeded in %s" % took
    hint = {
        124: "timed out",
        126: "command found but not executable",
        127: "command not found on this machine",
        137: "killed (SIGKILL - out of memory?)",
        143: "terminated (SIGTERM)",
    }.get(exit_code, "")
    detail = " - %s" % error if error else (" - %s" % hint if hint else "")
    return "FAILED after %s (exit %s)%s" % (took, exit_code, detail)


def upload_artifacts(client: Client, task: Dict[str, Any], workspace: str,
                     shipper: LogShipper) -> None:
    source = os.path.join(workspace, "source")
    for spec in task.get("artifacts") or []:
        pattern = os.path.join(source, spec.get("workdir") or "", spec.get("path") or "")
        matches = [p for p in glob.glob(pattern, recursive=True) if os.path.isfile(p)]
        if not matches:
            shipper.add("[agent] no files matched artifact pattern: %s" % spec.get("path"))
            continue
        for path in matches:
            try:
                client.post_file(
                    "/tasks/%d/artifacts" % task["taskId"],
                    {
                        "claimToken": task["claimToken"],
                        "name": os.path.basename(path),
                        "type": spec.get("type") or "binary",
                        "declaredPath": spec.get("path") or "",
                        "sourcePath": os.path.relpath(path, source),
                    },
                    path,
                )
                shipper.add("[agent] uploaded %s (%d bytes)" % (path, os.path.getsize(path)))
            except Exception as exc:
                shipper.add("[agent] artifact upload failed for %s: %s" % (path, exc), "stderr")


# ---------------------------------------------------------------------------
# The loop
# ---------------------------------------------------------------------------

def main() -> int:
    parser = argparse.ArgumentParser(description="KubeSight build agent")
    parser.add_argument("--url", required=True, help="KubeSight base URL")
    parser.add_argument("--token", default=os.getenv("KUBESIGHT_AGENT_TOKEN", ""),
                        help="Agent token (or set KUBESIGHT_AGENT_TOKEN)")
    parser.add_argument("--workspace", default=None,
                        help="Where builds are checked out. Overrides the path set "
                             "in KubeSight; without either, /data/kubesight-agent")
    parser.add_argument(
        "--workspace-retention-hours",
        type=float,
        default=os.getenv("KUBESIGHT_AGENT_WORKSPACE_RETENTION_HOURS", "24"),
        help="Delete marked workspaces left by crashes after this many hours; "
             "0 disables the stale-workspace sweep (default: 24)",
    )
    parser.add_argument("--poll", type=float, default=None,
                        help="Seconds between claim attempts when idle. Pins the "
                             "interval; without it KubeSight sets the fleet's")
    parser.add_argument("--insecure", action="store_true",
                        help="Skip TLS verification (for a self-signed KubeSight)")
    parser.add_argument("--quiet", action="store_true",
                        help="Do not echo build output to this terminal. The outcome of "
                             "each stage is still printed, pass or fail")
    parser.add_argument("--no-container", action="store_true",
                        help="Never run a stage in a container, even when it declares an "
                             "image and this machine has docker or podman")
    parser.add_argument("--runtime", choices=CONTAINER_RUNTIMES, default=None,
                        help="Use this container runtime and no other. Without it, docker "
                             "is preferred and podman is the fallback")
    args = parser.parse_args()

    if not args.token:
        print("An agent token is required: --token or KUBESIGHT_AGENT_TOKEN", file=sys.stderr)
        return 2

    client = Client(args.url, args.token, verify_tls=not args.insecure)
    # A path given here always wins: the person at the machine knows its disks,
    # and being overruled remotely by a typo would be worse than useless.
    pinned = bool(args.workspace)
    workspace = args.workspace or DEFAULT_WORKSPACE
    workspace_error = ""
    os.makedirs(workspace, exist_ok=True)
    cleanup_stale_workspaces(workspace, max(0.0, args.workspace_retention_hours))
    global _containers_disabled, _runtime_pinned, _echo_output
    _containers_disabled = bool(args.no_container)
    _runtime_pinned = args.runtime or ""
    _echo_output = not args.quiet
    capabilities = detect_capabilities()
    print("[agent] %s, capabilities: %s" % (platform.node(), ", ".join(capabilities)))
    runtime = container_runtime()
    if runtime:
        print("[agent] stages that declare an image will run in %s%s"
              % (runtime, " (pinned)" if _runtime_pinned else ""))
        # Anything left from a previous life is this agent's to clean up.
        prune_containers()
    elif args.no_container:
        print("[agent] containers disabled (--no-container)")
    elif _runtime_pinned:
        print("[agent] %s was pinned with --runtime but cannot be used here (is the daemon "
              "running, and is this user allowed to reach it?): stages run with this "
              "machine's own tools" % _runtime_pinned, file=sys.stderr)
    else:
        print("[agent] no container runtime: stages run with this machine's own tools")
    print("[agent] workspace: %s%s" % (workspace, " (from --workspace)" if pinned else ""))
    if not _echo_output:
        print("[agent] build output is not echoed here (--quiet); read it in KubeSight")

    identity = {
        "hostname": platform.node(),
        "os": platform.system().lower(),
        "osVersion": platform.release(),
        "arch": platform.machine(),
        "version": VERSION,
        "capabilities": capabilities,
    }

    # A value on the command line wins; otherwise KubeSight tells us on each
    # heartbeat, so the fleet's responsiveness is one setting on the server
    # rather than an argument on every machine.
    pinned_poll = args.poll is not None
    poll = float(args.poll) if pinned_poll else DEFAULT_POLL_SECONDS
    heartbeat_every = 30.0
    last_heartbeat = 0.0
    eager_until = 0.0
    while True:
        try:
            now = time.time()
            accepting = True
            if now - last_heartbeat > heartbeat_every:
                state = client.post_json(
                    "/heartbeat", {**identity, "workspaceError": workspace_error}
                ) or {}
                last_heartbeat = now
                accepting = bool(state.get("accepting", True))
                if not pinned_poll:
                    try:
                        wanted_poll = float(state.get("pollSeconds") or 0)
                        if wanted_poll > 0:
                            poll = max(0.2, min(60.0, wanted_poll))
                    except (TypeError, ValueError):
                        pass
                try:
                    heartbeat_every = max(5.0, float(state.get("heartbeatSeconds") or 30))
                except (TypeError, ValueError):
                    heartbeat_every = 30.0
                if not accepting:
                    print("[agent] not accepting work (%s)" % state.get("status"))

                # Normal completions clean immediately. This bounded sweep is
                # the recovery path for a killed agent or lost result response.
                cleanup_stale_workspaces(
                    workspace, max(0.0, args.workspace_retention_hours)
                )

                # Applied between tasks, never underneath one that is running.
                wanted = str(state.get("workspaceRoot") or "").strip()
                if wanted and not pinned and wanted != workspace:
                    try:
                        os.makedirs(wanted, exist_ok=True)
                        # Prove it is usable rather than merely present: a path
                        # that exists but cannot be written to fails every build
                        # with a confusing error much later.
                        probe = os.path.join(wanted, ".kubesight-write-test")
                        with open(probe, "w") as handle:
                            handle.write("ok")
                        os.remove(probe)
                        workspace, workspace_error = wanted, ""
                        print("[agent] workspace set by KubeSight: %s" % workspace)
                        cleanup_stale_workspaces(
                            workspace, max(0.0, args.workspace_retention_hours)
                        )
                    except Exception as exc:
                        # Keep building where we are and say why, so the runner
                        # shows the problem instead of silently using elsewhere.
                        workspace_error = "Cannot use %s: %s" % (wanted, exc)
                        print("[agent] %s" % workspace_error, file=sys.stderr)

            task = client.post_json("/claim", {}) if accepting else None
            if task:
                # A rule above the output and the verdict below it: without the
                # pair, a long stage's output runs into the next stage's and
                # neither can be told apart afterwards.
                echo("[agent] %s build #%s stage '%s' %s"
                     % ("-" * 8, task.get("buildNumber"), task.get("stageName"), "-" * 8))
                run_task(client, task, workspace)
                # Ask again immediately, then keep asking on a fast beat for a
                # few seconds: reporting the result is what starts the next
                # stage, so its task lands within milliseconds of this point.
                eager_until = time.time() + EAGER_WINDOW_SECONDS
                continue
        except KeyboardInterrupt:
            print("[agent] stopping")
            return 0
        except Exception as exc:
            # Never exit on a transient failure: an agent that dies when the
            # network blips has to be restarted by a person.
            print("[agent] %s" % exc, file=sys.stderr)
            time.sleep(min(30.0, max(1.0, poll * 4)))
            continue
        time.sleep(EAGER_POLL_SECONDS if time.time() < eager_until else poll)


if __name__ == "__main__":
    sys.exit(main())
