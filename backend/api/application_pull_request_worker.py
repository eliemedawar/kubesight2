"""Isolated Bitbucket pull-request preparation and publication worker."""

from __future__ import annotations

import base64
import json
import os
import re
import stat
import subprocess
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

WORKSPACE = Path("/workspace/repository")
PREPARED_REPORT = Path("/workspace/pr-prepared.json")
BUILD_REPORT = Path("/workspace/build-verification.json")


def _secret_value(name: str) -> str:
    value = os.getenv(name, "")
    if value:
        return value
    path = os.getenv(f"{name}_FILE", "")
    try:
        return Path(path).read_text(encoding="utf-8").strip() if path else ""
    except OSError:
        return ""


def _bundle() -> dict:
    path = os.getenv("KUBESIGHT_PR_REQUEST_FILE", "/run/kubesight-pr/pr-request.json")
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    if (
        not isinstance(payload, dict)
        or not isinstance(payload.get("patches"), list)
        or not 1 <= len(payload["patches"]) <= 50
    ):
        raise ValueError("The pull-request patch bundle is invalid.")
    return payload


def _callback(payload: dict) -> None:
    base = os.getenv("KUBESIGHT_PR_CALLBACK_URL", "").rstrip("/")
    request_id = os.getenv("PULL_REQUEST_ID", "")
    token = _secret_value("KUBESIGHT_PR_CALLBACK_TOKEN")
    if not base or not request_id or not token:
        return
    request = Request(
        f"{base}/{request_id}/result",
        data=json.dumps(payload).encode("utf-8"),
        method="POST",
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
    )
    try:
        with urlopen(request, timeout=30):
            pass
    except Exception:
        pass


def _git_environment(token: str, credential_type: str) -> tuple[dict, Path]:
    username = (
        "x-bitbucket-api-token-auth"
        if credential_type == "api_token"
        else "x-token-auth"
    )
    askpass = Path("/tmp/kubesight-pr-git-askpass")
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
        "KUBESIGHT_GIT_USERNAME": username,
    }
    if credential_type == "api_token":
        encoded = base64.b64encode(f"{username}:{token}".encode()).decode()
        env.update(
            {
                "GIT_CONFIG_COUNT": "1",
                "GIT_CONFIG_KEY_0": "http.extraHeader",
                "GIT_CONFIG_VALUE_0": f"Authorization: Basic {encoded}",
            }
        )
    return env, askpass


def _run(command: list[str], *, env: dict, timeout: int = 300) -> str:
    completed = subprocess.run(
        command,
        cwd=WORKSPACE if WORKSPACE.exists() else Path("/workspace"),
        env=env,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )
    if completed.returncode != 0:
        raise RuntimeError("A guarded Git operation failed.")
    return (completed.stdout or "").strip()


def prepare() -> int:
    token = _secret_value("BITBUCKET_WRITE_TOKEN")
    credential_type = os.getenv("BITBUCKET_CREDENTIAL_TYPE", "oauth")
    bundle = _bundle()
    env, askpass = _git_environment(token, credential_type)
    try:
        WORKSPACE.mkdir(parents=True, exist_ok=True)
        _run(["git", "init", str(WORKSPACE)], env=env, timeout=30)
        _run(
            ["git", "remote", "add", "origin", bundle["repositoryUrl"]],
            env=env,
            timeout=30,
        )
        _run(
            [
                "git",
                "-c",
                "credential.helper=",
                "fetch",
                "--no-tags",
                "--depth",
                "1",
                "origin",
                bundle["commitSha"],
            ],
            env=env,
        )
        _run(["git", "checkout", "--detach", "FETCH_HEAD"], env=env, timeout=60)
        for index, patch in enumerate(bundle["patches"]):
            content = str(patch.get("content") or "")
            if not content or len(content.encode("utf-8")) > 250_000:
                raise ValueError("A selected finding patch is invalid.")
            patch_path = Path(f"/tmp/kubesight-finding-{index}.patch")
            patch_path.write_text(content, encoding="utf-8")
            _run(["git", "apply", "--check", "--whitespace=error-all", str(patch_path)], env=env)
            _run(["git", "apply", "--whitespace=error-all", str(patch_path)], env=env)
            patch_path.unlink(missing_ok=True)
        _run(["git", "config", "user.name", "KubeSight hermes-agent"], env=env)
        _run(["git", "config", "user.email", "hermes-agent@kubesight.local"], env=env)
        _run(["git", "add", "--all"], env=env)
        _run(["git", "commit", "-m", bundle["commitMessage"]], env=env)
        commit_sha = _run(["git", "rev-parse", "HEAD"], env=env, timeout=15)
        changed = _run(
            ["git", "diff-tree", "--no-commit-id", "--name-only", "-r", "HEAD"],
            env=env,
            timeout=15,
        ).splitlines()
        PREPARED_REPORT.write_text(
            json.dumps(
                {"status": "Prepared", "commitSha": commit_sha, "changedFiles": changed},
                indent=2,
            ),
            encoding="utf-8",
        )
        return 0
    except Exception:
        _callback(
            {
                "status": "Failed",
                "safeErrorMessage": (
                    "The selected suggestions could not be applied cleanly to the "
                    "analyzed commit."
                ),
            }
        )
        return 1
    finally:
        askpass.unlink(missing_ok=True)


def _authorization(token: str, credential_type: str, principal: str) -> str:
    if credential_type == "api_token":
        encoded = base64.b64encode(f"{principal}:{token}".encode()).decode()
        return f"Basic {encoded}"
    return f"Bearer {token}"


def publish() -> int:
    token = _secret_value("BITBUCKET_WRITE_TOKEN")
    credential_type = os.getenv("BITBUCKET_CREDENTIAL_TYPE", "oauth")
    principal = os.getenv("BITBUCKET_PRINCIPAL", "")
    bundle = _bundle()
    env, askpass = _git_environment(token, credential_type)
    try:
        prepared = json.loads(PREPARED_REPORT.read_text(encoding="utf-8"))
        verification = json.loads(BUILD_REPORT.read_text(encoding="utf-8"))
        if prepared.get("status") != "Prepared":
            raise ValueError("The pull request was not prepared.")
        if verification.get("status") in {"Failed", "Unavailable"}:
            _callback(
                {
                    "status": "Validation Failed",
                    "validationSummary": verification,
                    "safeErrorMessage": (
                        "The proposed changes were not pushed because isolated "
                        "build verification did not pass."
                    ),
                }
            )
            return 1
        branch = str(bundle["branchName"])
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._/-]{0,199}", branch):
            raise ValueError("The pull-request branch is invalid.")
        _run(
            [
                "git",
                "-c",
                "credential.helper=",
                "push",
                "origin",
                f"HEAD:refs/heads/{branch}",
            ],
            env=env,
        )
        repository = bundle["repositoryRef"]
        request = Request(
            f"https://api.bitbucket.org/2.0/repositories/{repository}/pullrequests",
            data=json.dumps(
                {
                    "title": bundle["title"],
                    "description": bundle["description"],
                    "source": {"branch": {"name": branch}},
                    "destination": {
                        "branch": {"name": bundle["destinationBranch"]}
                    },
                    "close_source_branch": True,
                }
            ).encode("utf-8"),
            method="POST",
            headers={
                "Authorization": _authorization(token, credential_type, principal),
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
        )
        with urlopen(request, timeout=60) as response:
            provider = json.loads(response.read(1_000_001))
        _callback(
            {
                "status": "Created",
                "providerPullRequestId": str(provider.get("id") or ""),
                "providerUrl": (
                    (provider.get("links") or {}).get("html") or {}
                ).get("href"),
                "validationSummary": verification,
                "changedFiles": prepared.get("changedFiles") or [],
            }
        )
        return 0
    except (HTTPError, Exception):
        _callback(
            {
                "status": "Failed",
                "safeErrorMessage": (
                    "Bitbucket pull-request creation failed. No change was pushed "
                    "to the default branch."
                ),
            }
        )
        return 1
    finally:
        askpass.unlink(missing_ok=True)


def main() -> int:
    action = os.getenv("KUBESIGHT_PR_ACTION", "prepare").strip().lower()
    return publish() if action == "publish" else prepare()


if __name__ == "__main__":
    raise SystemExit(main())
