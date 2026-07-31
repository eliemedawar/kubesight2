"""Credential-free build/test verifier for Build Verified analyses.

This process is launched in its own locked-down container. It receives only the
checked-out workspace, has no KubeSight/Hermes/Bitbucket credentials, and is
expected to run with networking disabled (local Docker) or tightly restricted
(Kubernetes).
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

REPORT_PATH = Path("/workspace/build-verification.json")
MAX_OUTPUT_CHARS = int(os.getenv("APPLICATION_BUILD_OUTPUT_MAX_CHARS", "12000"))
COMMAND_TIMEOUT = int(os.getenv("APPLICATION_BUILD_COMMAND_TIMEOUT_SECONDS", "600"))
MAX_COMMANDS = 6


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _command_specs(root: Path) -> list[tuple[str, list[str]]]:
    specs: list[tuple[str, list[str]]] = []
    if (root / "package.json").is_file():
        specs.extend(
            [
                ("JavaScript tests", ["npm", "test", "--if-present"]),
                ("JavaScript build", ["npm", "run", "build", "--if-present"]),
            ]
        )
    if (root / "pyproject.toml").is_file() or (root / "requirements.txt").is_file():
        specs.append(("Python syntax", ["python", "-m", "compileall", "-q", "."]))
        if any((root / name).exists() for name in ("tests", "test")):
            specs.append(("Python tests", ["python", "-m", "pytest", "-q"]))
    if (root / "go.mod").is_file():
        specs.append(("Go tests", ["go", "test", "./..."]))
    if (root / "pom.xml").is_file():
        specs.append(("Maven tests", ["mvn", "--batch-mode", "test"]))
    if (root / "gradlew").is_file():
        specs.append(("Gradle tests", ["./gradlew", "--no-daemon", "test"]))
    if list(root.glob("*.csproj")) or list(root.glob("*.sln")):
        specs.append(("dotnet tests", ["dotnet", "test", "--nologo"]))
    return specs[:MAX_COMMANDS]


def _bounded_output(value: str) -> str:
    value = value or ""
    if len(value) <= MAX_OUTPUT_CHARS:
        return value
    return value[:MAX_OUTPUT_CHARS] + "\n[output truncated]"


def verify(root: Path) -> dict[str, Any]:
    started_at = _now()
    commands = []
    specs = _command_specs(root)
    sanitized_env = {
        "PATH": "/usr/local/bin:/usr/bin:/bin",
        "HOME": "/tmp",
        "TMPDIR": "/tmp",
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "CI": "true",
        "NO_COLOR": "1",
        "PYTHONDONTWRITEBYTECODE": "1",
    }
    for label, command in specs:
        executable = command[0]
        if executable.startswith("./"):
            available = (root / executable[2:]).is_file()
        else:
            available = shutil.which(executable, path=sanitized_env["PATH"]) is not None
        if not available:
            commands.append(
                {
                    "label": label,
                    "command": command,
                    "status": "Unavailable",
                    "exitCode": None,
                    "output": f"{executable} is not installed in the build-verifier image.",
                }
            )
            continue
        try:
            completed = subprocess.run(
                command,
                cwd=root,
                env=sanitized_env,
                capture_output=True,
                text=True,
                timeout=COMMAND_TIMEOUT,
                check=False,
            )
            commands.append(
                {
                    "label": label,
                    "command": command,
                    "status": "Passed" if completed.returncode == 0 else "Failed",
                    "exitCode": completed.returncode,
                    "output": _bounded_output(
                        "\n".join(
                            part.strip()
                            for part in (completed.stdout, completed.stderr)
                            if part and part.strip()
                        )
                    ),
                }
            )
        except subprocess.TimeoutExpired as exc:
            commands.append(
                {
                    "label": label,
                    "command": command,
                    "status": "Timed Out",
                    "exitCode": None,
                    "output": _bounded_output(
                        (exc.stdout or "") + "\n" + (exc.stderr or "")
                    ),
                }
            )
    statuses = {item["status"] for item in commands}
    if not commands or statuses == {"Unavailable"}:
        status = "Unavailable"
    elif statuses & {"Failed", "Timed Out"}:
        status = "Failed"
    elif "Unavailable" in statuses:
        status = "Completed With Warnings"
    else:
        status = "Passed"
    return {
        "schemaVersion": 1,
        "status": status,
        "startedAt": started_at,
        "completedAt": _now(),
        "networkPolicy": os.getenv(
            "APPLICATION_BUILD_NETWORK_POLICY", "Deny all network access"
        ),
        "credentialExposure": "None",
        "commands": commands,
    }


def main() -> int:
    repository_root = Path("/workspace/repository")
    subdirectory = os.getenv("ANALYSIS_SUBDIRECTORY", "").strip()
    root = repository_root / subdirectory if subdirectory else repository_root
    if not root.is_dir() or repository_root.resolve() not in (
        root.resolve(),
        *root.resolve().parents,
    ):
        report = {
            "schemaVersion": 1,
            "status": "Failed",
            "startedAt": _now(),
            "completedAt": _now(),
            "networkPolicy": os.getenv(
                "APPLICATION_BUILD_NETWORK_POLICY", "Deny all network access"
            ),
            "credentialExposure": "None",
            "commands": [],
            "safeError": "The configured repository subdirectory was not found.",
        }
    else:
        report = verify(root)
    REPORT_PATH.write_text(json.dumps(report, indent=2), encoding="utf-8")
    # Verification failure is data, not an infrastructure failure. The trusted
    # analyzer persists it and creates the corresponding finding.
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
