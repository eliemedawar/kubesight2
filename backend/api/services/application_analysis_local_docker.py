"""Isolated local-Docker launcher for Application Intelligence development."""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import threading
from pathlib import Path

_cancelled: set[str] = set()
_cancel_lock = threading.Lock()


class LocalDockerLaunchError(RuntimeError):
    pass


def _docker(*args: str, timeout: int = 30, input_text: str | None = None):
    try:
        return subprocess.run(
            ["docker", *args],
            input=input_text,
            text=True,
            capture_output=True,
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise LocalDockerLaunchError(
            "The local Docker analysis runtime is unavailable."
        ) from exc


def _require_resource(kind: str, name: str) -> None:
    result = _docker(kind, "inspect", name)
    if result.returncode != 0:
        label = "image" if kind == "image" else "network"
        raise LocalDockerLaunchError(
            f"The local analysis Docker {label} '{name}' is not available."
        )


def _require_worker_runtime(image: str) -> None:
    """Reject stale worker images before they can orphan a queued analysis."""
    result = _docker(
        "run",
        "--rm",
        "--entrypoint",
        "python",
        image,
        "-c",
        "import api.application_checkout; import api.application_worker",
        timeout=60,
    )
    if result.returncode != 0:
        raise LocalDockerLaunchError(
            f"The local analysis Docker image '{image}' is incompatible. "
            "Rebuild it from backend/Dockerfile.application-worker-local."
        )


def _write_secret(directory: Path, name: str, value: str) -> None:
    path = directory / name
    path.write_text(value, encoding="utf-8")
    try:
        path.chmod(0o600)
    except OSError:
        pass


def _cleanup_when_finished(container_name: str, secret_dir: Path) -> None:
    try:
        _docker(
            "wait",
            container_name,
            timeout=int(
                os.getenv("APPLICATION_ANALYSIS_DEADLINE_SECONDS", "1800")
            )
            + 300,
        )
    except LocalDockerLaunchError:
        pass
    finally:
        try:
            _docker("rm", "-f", container_name, timeout=30)
        except LocalDockerLaunchError:
            pass
        shutil.rmtree(secret_dir, ignore_errors=True)


def _is_cancelled(root_name: str) -> bool:
    with _cancel_lock:
        return root_name in _cancelled


def _run_build_verified_pipeline(
    *,
    root_name: str,
    image: str,
    network: str,
    workspace_dir: Path,
    secret_dir: Path,
    analysis_id: int,
    analysis_mode: str,
    repository_url: str,
    revision: str,
    subdirectory: str,
    repository_credential_type: str,
    repository_principal: str,
    callback_url: str,
    hermes_url: str,
) -> None:
    checkout_secrets = secret_dir / "checkout"
    analyzer_secrets = secret_dir / "analyzer"
    common_limits = [
        "--read-only",
        "--cap-drop",
        "ALL",
        "--security-opt",
        "no-new-privileges:true",
        "--pids-limit",
        "256",
        "--cpus",
        os.getenv("APPLICATION_ANALYSIS_CPU_LIMIT", "2"),
        "--memory",
        os.getenv("APPLICATION_ANALYSIS_MEMORY_LIMIT", "4g"),
        "--user",
        "65532:65532",
        "--tmpfs",
        "/tmp:rw,noexec,nosuid,nodev,size=256m,uid=65532,gid=65532",
        "--mount",
        f"type=bind,source={workspace_dir},target=/workspace",
        "-e",
        "HOME=/tmp",
        "-e",
        "TMPDIR=/tmp",
        "-e",
        "PYTHONDONTWRITEBYTECODE=1",
        "-e",
        f"ANALYSIS_ID={analysis_id}",
        "-e",
        f"ANALYSIS_MODE={analysis_mode}",
        "-e",
        f"ANALYSIS_SUBDIRECTORY={subdirectory}",
    ]
    try:
        checkout_name = f"{root_name}-checkout"
        checkout = _docker(
            "run",
            "--name",
            checkout_name,
            "--label",
            f"kubesight.io/analysis-id={analysis_id}",
            "--network",
            network,
            "--add-host",
            "host.docker.internal:host-gateway",
            *common_limits,
            "--mount",
            f"type=bind,source={checkout_secrets},target=/run/kubesight-secrets,readonly",
            "-e",
            f"ANALYSIS_REPOSITORY_URL={repository_url}",
            "-e",
            f"ANALYSIS_REVISION={revision}",
            "-e",
            f"ANALYSIS_REPOSITORY_CREDENTIAL_TYPE={repository_credential_type}",
            "-e",
            f"ANALYSIS_REPOSITORY_PRINCIPAL={repository_principal}",
            "-e",
            "ANALYSIS_REPOSITORY_TOKEN_FILE=/run/kubesight-secrets/repository-token",
            "-e",
            f"KUBESIGHT_ANALYSIS_CALLBACK_URL={callback_url}",
            "-e",
            "KUBESIGHT_ANALYSIS_CALLBACK_TOKEN_FILE=/run/kubesight-secrets/callback-token",
            "--entrypoint",
            "python",
            image,
            "-m",
            "api.application_checkout",
            timeout=int(os.getenv("APPLICATION_ANALYSIS_CLONE_TIMEOUT_SECONDS", "300"))
            + 90,
        )
        _docker("rm", "-f", checkout_name)
        if checkout.returncode != 0 or _is_cancelled(root_name):
            return

        build_name = f"{root_name}-build"
        _docker(
            "run",
            "--name",
            build_name,
            "--label",
            f"kubesight.io/analysis-id={analysis_id}",
            "--network",
            "none",
            *common_limits,
            "--entrypoint",
            "python",
            image,
            "-m",
            "api.application_build_verifier",
            timeout=int(os.getenv("APPLICATION_ANALYSIS_DEADLINE_SECONDS", "1800")),
        )
        _docker("rm", "-f", build_name)
        if _is_cancelled(root_name):
            return

        analyzer_name = f"{root_name}-analyzer"
        _docker(
            "run",
            "--name",
            analyzer_name,
            "--label",
            f"kubesight.io/analysis-id={analysis_id}",
            "--network",
            network,
            "--add-host",
            "host.docker.internal:host-gateway",
            *common_limits,
            "--mount",
            f"type=bind,source={analyzer_secrets},target=/run/kubesight-secrets,readonly",
            "-e",
            f"KUBESIGHT_ANALYSIS_CALLBACK_URL={callback_url}",
            "-e",
            "KUBESIGHT_ANALYSIS_CALLBACK_TOKEN_FILE=/run/kubesight-secrets/callback-token",
            "-e",
            f"HERMES_API_URL={hermes_url}",
            "-e",
            "HERMES_API_TOKEN_FILE=/run/kubesight-secrets/hermes-token",
            "-e",
            "HERMES_ALLOW_HTTP_HOSTS=kubesight-hermes-local",
            "-e",
            f"HERMES_APPLICATION_MODEL={os.getenv('HERMES_APPLICATION_MODEL', 'hermes-agent')}",
            "-e",
            "APPLICATION_ANALYSIS_EXECUTION_MODE=local_docker",
            "--entrypoint",
            "python",
            image,
            "-m",
            "api.application_worker",
            timeout=int(os.getenv("APPLICATION_ANALYSIS_DEADLINE_SECONDS", "1800"))
            + 120,
        )
        _docker("rm", "-f", analyzer_name)
    finally:
        for suffix in ("checkout", "build", "analyzer"):
            try:
                _docker("rm", "-f", f"{root_name}-{suffix}", timeout=30)
            except LocalDockerLaunchError:
                pass
        shutil.rmtree(secret_dir, ignore_errors=True)
        shutil.rmtree(workspace_dir, ignore_errors=True)
        with _cancel_lock:
            _cancelled.discard(root_name)


def _launch_build_verified(
    *,
    analysis_id: int,
    image: str,
    network: str,
    callback_url: str,
    hermes_url: str,
    hermes_token: str,
    repository_url: str,
    revision: str,
    subdirectory: str,
    repository_token: str,
    repository_credential_type: str,
    repository_principal: str,
    callback_token: str,
    analysis_mode: str,
) -> str:
    data_root = Path(
        os.getenv(
            "APPLICATION_ANALYSIS_LOCAL_SECRET_DIR",
            str(Path(__file__).resolve().parents[2] / "data" / "application-analysis-local"),
        )
    ).resolve()
    data_root.mkdir(parents=True, exist_ok=True)
    secret_dir = Path(
        tempfile.mkdtemp(prefix=f"analysis-{analysis_id}-secrets-", dir=data_root)
    ).resolve()
    workspace_dir = Path(
        tempfile.mkdtemp(prefix=f"analysis-{analysis_id}-workspace-", dir=data_root)
    ).resolve()
    checkout_secrets = secret_dir / "checkout"
    analyzer_secrets = secret_dir / "analyzer"
    checkout_secrets.mkdir()
    analyzer_secrets.mkdir()
    _write_secret(checkout_secrets, "repository-token", repository_token)
    _write_secret(checkout_secrets, "callback-token", callback_token)
    _write_secret(analyzer_secrets, "callback-token", callback_token)
    _write_secret(analyzer_secrets, "hermes-token", hermes_token)
    root_name = f"ks-app-analysis-local-{analysis_id}"
    threading.Thread(
        target=_run_build_verified_pipeline,
        kwargs={
            "root_name": root_name,
            "image": image,
            "network": network,
            "workspace_dir": workspace_dir,
            "secret_dir": secret_dir,
            "analysis_id": analysis_id,
            "analysis_mode": analysis_mode,
            "repository_url": repository_url,
            "revision": revision,
            "subdirectory": subdirectory,
            "repository_credential_type": repository_credential_type,
            "repository_principal": repository_principal,
            "callback_url": callback_url,
            "hermes_url": hermes_url,
        },
        daemon=True,
        name=f"application-build-verified-{analysis_id}",
    ).start()
    return root_name


def launch(
    *,
    analysis_id: int,
    repository_url: str,
    revision: str,
    subdirectory: str,
    repository_token: str,
    repository_credential_type: str,
    repository_principal: str,
    callback_token: str,
    analysis_mode: str,
) -> str:
    image = os.getenv(
        "APPLICATION_ANALYSIS_LOCAL_DOCKER_IMAGE",
        "kubesight-application-worker:local",
    ).strip()
    network = os.getenv(
        "APPLICATION_ANALYSIS_LOCAL_DOCKER_NETWORK",
        "kubesigh_1_default",
    ).strip()
    callback_url = os.getenv(
        "APPLICATION_ANALYSIS_LOCAL_CALLBACK_URL",
        "http://host.docker.internal:5000/api/application-analysis-worker",
    ).strip()
    hermes_url = os.getenv(
        "APPLICATION_ANALYSIS_LOCAL_HERMES_URL",
        "http://kubesight-hermes-local:8642/v1/chat/completions",
    ).strip()
    hermes_token = os.getenv("HERMES_API_TOKEN", "").strip()
    if not hermes_token:
        raise LocalDockerLaunchError("Hermes is not configured for local analysis.")
    _require_resource("image", image)
    _require_resource("network", network)
    _require_worker_runtime(image)
    if analysis_mode == "Build Verified":
        return _launch_build_verified(
            analysis_id=analysis_id,
            image=image,
            network=network,
            callback_url=callback_url,
            hermes_url=hermes_url,
            hermes_token=hermes_token,
            repository_url=repository_url,
            revision=revision,
            subdirectory=subdirectory,
            repository_token=repository_token,
            repository_credential_type=repository_credential_type,
            repository_principal=repository_principal,
            callback_token=callback_token,
            analysis_mode=analysis_mode,
        )

    secret_root = Path(
        os.getenv(
            "APPLICATION_ANALYSIS_LOCAL_SECRET_DIR",
            str(Path(__file__).resolve().parents[2] / "data" / "application-analysis-local"),
        )
    ).resolve()
    secret_root.mkdir(parents=True, exist_ok=True)
    secret_dir = Path(
        tempfile.mkdtemp(prefix=f"analysis-{analysis_id}-", dir=secret_root)
    ).resolve()
    _write_secret(secret_dir, "repository-token", repository_token)
    _write_secret(secret_dir, "callback-token", callback_token)
    _write_secret(secret_dir, "hermes-token", hermes_token)

    container_name = f"ks-app-analysis-local-{analysis_id}"
    command = [
        "run",
        "-d",
        "--name",
        container_name,
        "--label",
        f"kubesight.io/analysis-id={analysis_id}",
        "--network",
        network,
        "--add-host",
        "host.docker.internal:host-gateway",
        "--read-only",
        "--cap-drop",
        "ALL",
        "--security-opt",
        "no-new-privileges:true",
        "--pids-limit",
        "256",
        "--cpus",
        os.getenv("APPLICATION_ANALYSIS_CPU_LIMIT", "2"),
        "--memory",
        os.getenv("APPLICATION_ANALYSIS_MEMORY_LIMIT", "4g"),
        "--user",
        "65532:65532",
        "--tmpfs",
        "/tmp:rw,noexec,nosuid,nodev,size=256m,uid=65532,gid=65532",
        "--tmpfs",
        "/workspace:rw,nosuid,nodev,size=2g,uid=65532,gid=65532",
        "--mount",
        f"type=bind,source={secret_dir},target=/run/kubesight-secrets,readonly",
        "-e",
        "HOME=/tmp",
        "-e",
        "TMPDIR=/tmp",
        "-e",
        "PYTHONDONTWRITEBYTECODE=1",
        "-e",
        f"ANALYSIS_ID={analysis_id}",
        "-e",
        f"ANALYSIS_MODE={analysis_mode}",
        "-e",
        f"ANALYSIS_REPOSITORY_URL={repository_url}",
        "-e",
        f"ANALYSIS_REVISION={revision}",
        "-e",
        f"ANALYSIS_SUBDIRECTORY={subdirectory}",
        "-e",
        f"ANALYSIS_REPOSITORY_CREDENTIAL_TYPE={repository_credential_type}",
        "-e",
        f"ANALYSIS_REPOSITORY_PRINCIPAL={repository_principal}",
        "-e",
        "ANALYSIS_REPOSITORY_TOKEN_FILE=/run/kubesight-secrets/repository-token",
        "-e",
        f"KUBESIGHT_ANALYSIS_CALLBACK_URL={callback_url}",
        "-e",
        "KUBESIGHT_ANALYSIS_CALLBACK_TOKEN_FILE=/run/kubesight-secrets/callback-token",
        "-e",
        f"HERMES_API_URL={hermes_url}",
        "-e",
        "HERMES_API_TOKEN_FILE=/run/kubesight-secrets/hermes-token",
        "-e",
        "HERMES_ALLOW_HTTP_HOSTS=kubesight-hermes-local",
        "-e",
        f"HERMES_APPLICATION_MODEL={os.getenv('HERMES_APPLICATION_MODEL', 'hermes-agent')}",
        "-e",
        "APPLICATION_ANALYSIS_EXECUTION_MODE=local_docker",
        "--entrypoint",
        "/bin/sh",
        image,
        "-c",
        "python -m api.application_checkout && python -m api.application_worker",
    ]
    result = _docker(*command, timeout=60)
    if result.returncode != 0:
        shutil.rmtree(secret_dir, ignore_errors=True)
        raise LocalDockerLaunchError(
            "The isolated local Docker analysis could not be started."
        )
    threading.Thread(
        target=_cleanup_when_finished,
        args=(container_name, secret_dir),
        daemon=True,
        name=f"application-analysis-{analysis_id}-cleanup",
    ).start()
    return container_name


def cancel(container_name: str) -> None:
    if not container_name.startswith("ks-app-analysis-local-"):
        return
    with _cancel_lock:
        _cancelled.add(container_name)
    _docker("rm", "-f", container_name, timeout=30)
    for suffix in ("checkout", "build", "analyzer"):
        _docker("rm", "-f", f"{container_name}-{suffix}", timeout=30)
