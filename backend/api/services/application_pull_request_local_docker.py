"""Local Docker launcher for the guarded Phase 2 pull-request workflow."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import threading
from pathlib import Path


class LocalPullRequestLaunchError(RuntimeError):
    pass


def _docker(*args: str, timeout: int = 30):
    try:
        return subprocess.run(
            ["docker", *args],
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise LocalPullRequestLaunchError(
            "The local Docker pull-request runtime is unavailable."
        ) from exc


def _write(path: Path, value: str) -> None:
    path.write_text(value, encoding="utf-8")
    try:
        path.chmod(0o600)
    except OSError:
        pass


def _pipeline(
    *,
    root_name: str,
    image: str,
    network: str,
    workspace: Path,
    secret_root: Path,
    pull_request_id: int,
    credential_type: str,
    principal: str,
    subdirectory: str,
    callback_url: str,
) -> None:
    common = [
        "--read-only",
        "--cap-drop",
        "ALL",
        "--security-opt",
        "no-new-privileges:true",
        "--pids-limit",
        "256",
        "--cpus",
        os.getenv("APPLICATION_BUILD_CPU_LIMIT", "2"),
        "--memory",
        os.getenv("APPLICATION_BUILD_MEMORY_LIMIT", "4g"),
        "--user",
        "65532:65532",
        "--tmpfs",
        "/tmp:rw,noexec,nosuid,nodev,size=256m,uid=65532,gid=65532",
        "--mount",
        f"type=bind,source={workspace},target=/workspace",
        "-e",
        "HOME=/tmp",
        "-e",
        "TMPDIR=/tmp",
        "-e",
        "PYTHONDONTWRITEBYTECODE=1",
        "-e",
        f"PULL_REQUEST_ID={pull_request_id}",
        "-e",
        f"BITBUCKET_CREDENTIAL_TYPE={credential_type}",
        "-e",
        f"BITBUCKET_PRINCIPAL={principal}",
    ]
    try:
        for action in ("prepare", "publish"):
            name = f"{root_name}-{action}"
            secrets = secret_root / action
            result = _docker(
                "run",
                "--name",
                name,
                "--label",
                f"kubesight.io/pull-request-id={pull_request_id}",
                "--network",
                network,
                "--add-host",
                "host.docker.internal:host-gateway",
                *common,
                "--mount",
                f"type=bind,source={secrets},target=/run/kubesight-pr,readonly",
                "-e",
                f"KUBESIGHT_PR_ACTION={action}",
                "-e",
                "BITBUCKET_WRITE_TOKEN_FILE=/run/kubesight-pr/write-token",
                "-e",
                "KUBESIGHT_PR_CALLBACK_TOKEN_FILE=/run/kubesight-pr/callback-token",
                "-e",
                "KUBESIGHT_PR_REQUEST_FILE=/run/kubesight-pr/pr-request.json",
                "-e",
                f"KUBESIGHT_PR_CALLBACK_URL={callback_url}",
                "--entrypoint",
                "python",
                image,
                "-m",
                "api.application_pull_request_worker",
                timeout=int(os.getenv("APPLICATION_ANALYSIS_DEADLINE_SECONDS", "1800")),
            )
            _docker("rm", "-f", name)
            if result.returncode != 0:
                return
            if action == "prepare":
                build_name = f"{root_name}-build"
                _docker(
                    "run",
                    "--name",
                    build_name,
                    "--label",
                    f"kubesight.io/pull-request-id={pull_request_id}",
                    "--network",
                    "none",
                    *common,
                    "-e",
                    "ANALYSIS_MODE=Build Verified",
                    "-e",
                    f"ANALYSIS_SUBDIRECTORY={subdirectory}",
                    "--entrypoint",
                    "python",
                    image,
                    "-m",
                    "api.application_build_verifier",
                    timeout=int(
                        os.getenv("APPLICATION_ANALYSIS_DEADLINE_SECONDS", "1800")
                    ),
                )
                _docker("rm", "-f", build_name)
    finally:
        for suffix in ("prepare", "build", "publish"):
            _docker("rm", "-f", f"{root_name}-{suffix}")
        shutil.rmtree(secret_root, ignore_errors=True)
        shutil.rmtree(workspace, ignore_errors=True)


def launch(
    *,
    pull_request_id: int,
    bundle: dict,
    write_token: str,
    credential_type: str,
    principal: str,
    callback_token: str,
    subdirectory: str,
) -> str:
    image = os.getenv(
        "APPLICATION_ANALYSIS_LOCAL_DOCKER_IMAGE",
        "kubesight-application-worker:local",
    ).strip()
    network = os.getenv(
        "APPLICATION_ANALYSIS_LOCAL_DOCKER_NETWORK", "kubesigh_1_default"
    ).strip()
    callback_url = os.getenv(
        "APPLICATION_PULL_REQUEST_LOCAL_CALLBACK_URL",
        "http://host.docker.internal:5000/api/application-pull-request-worker",
    ).strip()
    if _docker("image", "inspect", image).returncode != 0:
        raise LocalPullRequestLaunchError(
            f"The local analysis Docker image '{image}' is not available."
        )
    if _docker("network", "inspect", network).returncode != 0:
        raise LocalPullRequestLaunchError(
            f"The local analysis Docker network '{network}' is not available."
        )
    root = Path(
        os.getenv(
            "APPLICATION_ANALYSIS_LOCAL_SECRET_DIR",
            str(Path(__file__).resolve().parents[2] / "data" / "application-analysis-local"),
        )
    ).resolve()
    root.mkdir(parents=True, exist_ok=True)
    secrets = Path(
        tempfile.mkdtemp(prefix=f"pr-{pull_request_id}-secrets-", dir=root)
    ).resolve()
    workspace = Path(
        tempfile.mkdtemp(prefix=f"pr-{pull_request_id}-workspace-", dir=root)
    ).resolve()
    for action in ("prepare", "publish"):
        directory = secrets / action
        directory.mkdir()
        _write(directory / "write-token", write_token)
        _write(directory / "callback-token", callback_token)
        _write(directory / "pr-request.json", json.dumps(bundle))
    root_name = f"ks-app-pr-local-{pull_request_id}"
    threading.Thread(
        target=_pipeline,
        kwargs={
            "root_name": root_name,
            "image": image,
            "network": network,
            "workspace": workspace,
            "secret_root": secrets,
            "pull_request_id": pull_request_id,
            "credential_type": credential_type,
            "principal": principal,
            "subdirectory": subdirectory,
            "callback_url": callback_url,
        },
        daemon=True,
        name=f"application-pull-request-{pull_request_id}",
    ).start()
    return root_name


def cancel(root_name: str) -> None:
    if not root_name.startswith("ks-app-pr-local-"):
        return
    for suffix in ("prepare", "build", "publish"):
        _docker("rm", "-f", f"{root_name}-{suffix}")
