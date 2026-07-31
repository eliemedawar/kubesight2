"""Read-only Bitbucket checkout entry point used only by the Job init container."""

from __future__ import annotations

import base64
import json
import os
import shutil
import stat
import subprocess
from pathlib import Path
from urllib.request import Request, urlopen


def _secret_value(name: str) -> str:
    value = os.getenv(name, "")
    if value:
        return value
    secret_file = os.getenv(f"{name}_FILE", "")
    if not secret_file:
        return ""
    try:
        return Path(secret_file).read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def _git_username(credential_type: str) -> str:
    return (
        "x-bitbucket-api-token-auth"
        if credential_type == "api_token"
        else "x-token-auth"
    )


def _callback(status: str = "", *, kind: str = "event", **extra) -> None:
    base = os.getenv("KUBESIGHT_ANALYSIS_CALLBACK_URL", "").rstrip("/")
    analysis_id = os.getenv("ANALYSIS_ID", "")
    token = _secret_value("KUBESIGHT_ANALYSIS_CALLBACK_TOKEN")
    if not base or not analysis_id or not token:
        return
    payload = {**extra}
    if status:
        payload["status"] = status
    body = json.dumps(payload).encode("utf-8")
    request = Request(
        f"{base}/{analysis_id}/{kind}",
        data=body,
        method="POST",
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
    )
    try:
        with urlopen(request, timeout=10):
            pass
    except Exception:
        pass


def main() -> int:
    repository_url = os.getenv("ANALYSIS_REPOSITORY_URL", "")
    revision = os.getenv("ANALYSIS_REVISION", "main") or "main"
    token = _secret_value("ANALYSIS_REPOSITORY_TOKEN")
    credential_type = os.getenv(
        "ANALYSIS_REPOSITORY_CREDENTIAL_TYPE", "repository_access_token"
    )
    git_username = _git_username(credential_type)
    workspace = Path("/workspace/repository")
    askpass = Path("/tmp/kubesight-git-askpass")
    checkout_completed = False
    askpass.write_text(
        "#!/bin/sh\n"
        'case "$1" in\n'
        '  *Username*) printf "%s" "$KUBESIGHT_GIT_USERNAME" ;;\n'
        '  *) printf "%s" "$KUBESIGHT_GIT_TOKEN" ;;\n'
        "esac\n",
        encoding="utf-8",
    )
    askpass.chmod(stat.S_IRUSR | stat.S_IWUSR | stat.S_IXUSR)
    env = {
        **os.environ,
        "GIT_ASKPASS": str(askpass),
        "GIT_TERMINAL_PROMPT": "0",
        "KUBESIGHT_GIT_TOKEN": token,
        "KUBESIGHT_GIT_USERNAME": git_username,
    }
    if credential_type == "api_token":
        basic_auth = base64.b64encode(
            f"{git_username}:{token}".encode("utf-8")
        ).decode("ascii")
        # Use Git's environment-based config so the credential is neither in
        # the repository URL nor visible in the git process command line.
        env.update(
            {
                "GIT_CONFIG_COUNT": "1",
                "GIT_CONFIG_KEY_0": "http.extraHeader",
                "GIT_CONFIG_VALUE_0": f"Authorization: Basic {basic_auth}",
            }
        )
    _callback("Cloning", progressPercent=5)
    try:
        workspace.mkdir(parents=True, exist_ok=True)
        initialized = subprocess.run(
            ["git", "init", str(workspace)],
            env=env,
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        if initialized.returncode != 0:
            raise RuntimeError("Repository workspace could not be initialized.")
        subprocess.run(
            ["git", "-C", str(workspace), "remote", "add", "origin", repository_url],
            env=env,
            capture_output=True,
            text=True,
            timeout=15,
            check=True,
        )
        completed = subprocess.run(
            [
                "git",
                "-C",
                str(workspace),
                "-c",
                "credential.helper=",
                "fetch",
                "--no-tags",
                "--depth",
                "1",
                "origin",
                revision,
            ],
            env=env,
            capture_output=True,
            text=True,
            timeout=int(os.getenv("APPLICATION_ANALYSIS_CLONE_TIMEOUT_SECONDS", "300")),
            check=False,
        )
        if completed.returncode != 0:
            # Do not send git stderr: providers may echo credential-bearing URLs.
            _callback(
                "Failed",
                failureStage="Cloning",
                safeErrorMessage="Repository checkout failed. Verify the URL, revision, and credential profile.",
            )
            return 1
        checkout = subprocess.run(
            ["git", "-C", str(workspace), "checkout", "--detach", "FETCH_HEAD"],
            env=env,
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
        if checkout.returncode != 0:
            _callback(
                "Failed",
                failureStage="Cloning",
                safeErrorMessage="The requested branch, tag, or commit could not be checked out.",
            )
            return 1
        sha = subprocess.run(
            ["git", "-C", str(workspace), "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            timeout=15,
            check=True,
        ).stdout.strip()
        checkout_completed = True
        _callback("Discovering", progressPercent=15, commitSha=sha)
        return 0
    except subprocess.TimeoutExpired:
        _callback(
            "Failed",
            failureStage="Cloning",
            safeErrorMessage="Repository checkout timed out.",
        )
        return 1
    except Exception:
        _callback(
            "Failed",
            failureStage="Cloning",
            safeErrorMessage="Repository checkout failed safely.",
        )
        return 1
    finally:
        os.environ.pop("ANALYSIS_REPOSITORY_TOKEN", None)
        os.environ.pop("KUBESIGHT_GIT_TOKEN", None)
        os.environ.pop("KUBESIGHT_GIT_USERNAME", None)
        try:
            askpass.unlink(missing_ok=True)
        except OSError:
            pass
        if not checkout_completed:
            cleanup_status = "Completed"
            try:
                if workspace.exists():
                    shutil.rmtree(workspace)
            except OSError:
                cleanup_status = "Failed"
            _callback(
                kind="cleanup", workspaceCleanupStatus=cleanup_status
            )


if __name__ == "__main__":
    raise SystemExit(main())
