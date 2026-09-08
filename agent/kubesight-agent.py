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

What it does NOT do: containerise anything. A stage's "container image" is
ignored here — the point of an agent is to use the machine as it is, which is
the only way an iOS build can work at all.
"""

import argparse
import base64
import glob
import json
import mimetypes
import os
import platform
import shutil
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
import uuid
from typing import Any, Dict, List, Optional

VERSION = "1.0.0"
DEFAULT_POLL_SECONDS = 5

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
    return found


# ---------------------------------------------------------------------------
# Running one task
# ---------------------------------------------------------------------------

class LogShipper:
    """Batches output and posts it a few times a second.

    Batched because a chatty build would otherwise be one HTTP request per line;
    time-bounded because a person watching the build should see it move.
    """

    def __init__(self, client: Client, task_id: int, claim_token: str):
        self.client, self.task_id, self.claim_token = client, task_id, claim_token
        self.lines: List[Dict[str, str]] = []
        self.lock = threading.Lock()
        self.stop_event = threading.Event()
        self.thread = threading.Thread(target=self._loop, daemon=True)
        self.thread.start()

    def add(self, content: str, stream: str = "stdout") -> None:
        with self.lock:
            self.lines.append({"content": content.rstrip("\n"), "stream": stream})

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
    return process.wait(timeout=timeout)


def run_task(client: Client, task: Dict[str, Any], root: str) -> None:
    task_id, claim = task["taskId"], task["claimToken"]
    workspace = os.path.join(root, str(task.get("workspace") or "build"))
    os.makedirs(workspace, exist_ok=True)
    shipper = LogShipper(client, task_id, claim)
    exit_code, error = 1, None

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
                shipper.add("No checkout at %s — did the checkout stage run here?" % cwd, "stderr")
                raise RuntimeError("workspace missing")
            env = {**os.environ, **{str(k): str(v) for k, v in (task.get("env") or {}).items()}}
            script = "\n".join(task.get("commands") or ["true"])
            exit_code = stream(
                ["/bin/sh", "-e", "-c", script], cwd, env, shipper,
                timeout=int(task.get("timeoutSeconds") or 1800),
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
        shipper.close()
        try:
            client.post_json(
                "/tasks/%d/result" % task_id,
                {"claimToken": claim, "exitCode": exit_code, "error": error},
            )
        except Exception as exc:
            print("[agent] result post failed: %s" % exc, file=sys.stderr)


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
                             "in KubeSight; without either, ~/kubesight-agent")
    parser.add_argument("--poll", type=float, default=DEFAULT_POLL_SECONDS,
                        help="Seconds between claim attempts when idle")
    parser.add_argument("--insecure", action="store_true",
                        help="Skip TLS verification (for a self-signed KubeSight)")
    args = parser.parse_args()

    if not args.token:
        print("An agent token is required: --token or KUBESIGHT_AGENT_TOKEN", file=sys.stderr)
        return 2

    client = Client(args.url, args.token, verify_tls=not args.insecure)
    # A path given here always wins: the person at the machine knows its disks,
    # and being overruled remotely by a typo would be worse than useless.
    pinned = bool(args.workspace)
    workspace = args.workspace or os.path.expanduser("~/kubesight-agent")
    workspace_error = ""
    os.makedirs(workspace, exist_ok=True)
    capabilities = detect_capabilities()
    print("[agent] %s, capabilities: %s" % (platform.node(), ", ".join(capabilities)))
    print("[agent] workspace: %s%s" % (workspace, " (from --workspace)" if pinned else ""))

    identity = {
        "hostname": platform.node(),
        "os": platform.system().lower(),
        "osVersion": platform.release(),
        "arch": platform.machine(),
        "version": VERSION,
        "capabilities": capabilities,
    }

    last_heartbeat = 0.0
    while True:
        try:
            now = time.time()
            accepting = True
            if now - last_heartbeat > 30:
                state = client.post_json(
                    "/heartbeat", {**identity, "workspaceError": workspace_error}
                ) or {}
                last_heartbeat = now
                accepting = bool(state.get("accepting", True))
                if not accepting:
                    print("[agent] not accepting work (%s)" % state.get("status"))

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
                    except Exception as exc:
                        # Keep building where we are and say why, so the runner
                        # shows the problem instead of silently using elsewhere.
                        workspace_error = "Cannot use %s: %s" % (wanted, exc)
                        print("[agent] %s" % workspace_error, file=sys.stderr)

            task = client.post_json("/claim", {}) if accepting else None
            if task:
                print("[agent] running build #%s stage '%s'"
                      % (task.get("buildNumber"), task.get("stageName")))
                run_task(client, task, workspace)
                continue  # Ask again immediately: a pipeline's next stage may be waiting.
        except KeyboardInterrupt:
            print("[agent] stopping")
            return 0
        except Exception as exc:
            # Never exit on a transient failure: an agent that dies when the
            # network blips has to be restarted by a person.
            print("[agent] %s" % exc, file=sys.stderr)
            time.sleep(min(30, args.poll * 4))
            continue
        time.sleep(args.poll)


if __name__ == "__main__":
    sys.exit(main())
