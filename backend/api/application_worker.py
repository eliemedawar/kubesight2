"""Application Intelligence worker entry point.

This module is executed only inside the isolated Kubernetes Job. It never runs
repository code; deterministic scanners inspect files and Hermes receives a
bounded, redacted evidence package.
"""

from __future__ import annotations

import json
import os
import re
import shutil
from pathlib import Path
from urllib.request import Request, urlopen

from .services.application_intelligence_discovery import SCANNERS, discover_repository
from .services.application_intelligence_hermes import HermesError, analyze
from .services.application_intelligence_security import redact_text, safe_error

RELEVANT_SUFFIXES = {
    ".py",
    ".js",
    ".jsx",
    ".ts",
    ".tsx",
    ".java",
    ".go",
    ".cs",
    ".php",
    ".rb",
    ".yaml",
    ".yml",
    ".json",
    ".toml",
    ".properties",
    ".gradle",
    ".xml",
}
RELEVANT_NAMES = {
    "Dockerfile",
    ".dockerignore",
    "Gemfile",
    "Jenkinsfile",
    "requirements.txt",
    "package.json",
    "go.mod",
    "pom.xml",
}
BUILD_REPORT_PATH = Path("/workspace/build-verification.json")
BUILD_STATUSES = {"Passed", "Completed With Warnings", "Failed", "Unavailable"}


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


def _post(kind: str, payload: dict) -> None:
    base = os.getenv("KUBESIGHT_ANALYSIS_CALLBACK_URL", "").rstrip("/")
    analysis_id = os.getenv("ANALYSIS_ID", "")
    token = _secret_value("KUBESIGHT_ANALYSIS_CALLBACK_TOKEN")
    body = json.dumps(payload).encode("utf-8")
    request = Request(
        f"{base}/{analysis_id}/{kind}",
        data=body,
        method="POST",
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
    )
    with urlopen(request, timeout=30):
        pass


# Path signals that make a source file worth spending the evidence budget on.
# Hermes only ever sees a slice of a real repository, so the slice must be the
# part that carries architecture, integration, and security meaning rather than
# whichever files happen to sort first.
SOURCE_RELEVANCE_RANK = (
    (re.compile(r"(application|main|program|startup|bootstrap)\.[a-z]+$", re.I), 0),
    (re.compile(r"(config|configuration|properties)", re.I), 1),
    (re.compile(r"(security|auth|oauth|jwt|credential)", re.I), 1),
    (re.compile(r"(client|apiclient|feign|gateway|producer|consumer|listener)", re.I), 2),
    (re.compile(r"(controller|resource|handler|route|endpoint)", re.I), 3),
    (re.compile(r"(service|usecase|manager)", re.I), 4),
    (re.compile(r"(repository|dao|entity|model|dto|domain)", re.I), 6),
    (re.compile(r"(test|spec|mock|fixture)", re.I), 9),
)


def _source_rank(relative: str) -> int:
    name = relative.rsplit("/", 1)[-1]
    for pattern, rank in SOURCE_RELEVANCE_RANK:
        if pattern.search(name):
            return rank
    return 5


def _selected_file_evidence(root: Path, discovery: dict) -> tuple[list[dict], dict]:
    """Choose the file slice sent to Hermes, and report how much it covers.

    Returns the selection plus a coverage summary. A model reading 120 of 900
    files cannot see what it was not given, so the size of that gap is part of
    the result rather than a silent implementation detail.
    """
    mode = os.getenv("ANALYSIS_MODE", "Quick")
    # Quick stays deliberately small for latency. Deep and Build Verified use
    # a much wider bounded slice so ordinary service repositories are normally
    # reviewed in full; byte limits still protect the Hermes request size.
    default_file_limit = "40" if mode == "Quick" else "500"
    mode_file_limit = (
        "APPLICATION_ANALYSIS_HERMES_QUICK_FILE_LIMIT"
        if mode == "Quick"
        else "APPLICATION_ANALYSIS_HERMES_DEEP_FILE_LIMIT"
    )
    max_files = int(
        os.getenv(
            mode_file_limit,
            os.getenv("APPLICATION_ANALYSIS_HERMES_FILE_LIMIT", default_file_limit),
        )
    )
    max_file_bytes = int(os.getenv("APPLICATION_ANALYSIS_HERMES_FILE_BYTES", "65536"))
    max_total_bytes = int(
        os.getenv("APPLICATION_ANALYSIS_HERMES_CONTENT_BYTES", "2000000")
    )
    total_bytes = 0
    selected = []
    truncated = 0
    # Configuration files carry the connection evidence (broker addresses,
    # datasource URLs, ports). They sort after source directories, so without an
    # explicit priority they lose the file budget to source files and the model
    # is left reporting "not stated" for values the repository does contain.
    priority = set(
        discovery.get("configuration_files", [])
        + discovery.get("dependency_manifests", [])
        + discovery.get("dockerfiles", [])
        + discovery.get("compose_files", [])
        + discovery.get("kubernetes_manifests", [])
        + discovery.get("api_specs", [])
        + discovery.get("ci_files", [])
    )
    eligible = [
        item
        for item in discovery.get("file_tree", [])
        if (
            Path(item.get("path", "")).name in RELEVANT_NAMES
            or Path(item.get("path", "")).suffix.lower() in RELEVANT_SUFFIXES
        )
    ]
    candidates = sorted(
        eligible,
        key=lambda item: (
            item.get("path") not in priority,
            _source_rank(item.get("path", "")),
            item.get("path", ""),
        ),
    )
    for item in candidates:
        relative = item.get("path", "")
        path = root / relative
        if len(selected) >= max_files:
            break
        if item.get("size", 0) > max_file_bytes:
            continue
        try:
            raw = path.read_bytes()
            if b"\x00" in raw:
                continue
            content = raw.decode("utf-8", errors="replace")
        except OSError:
            continue
        if total_bytes + len(raw) > max_total_bytes:
            continue
        if len(content) > max_file_bytes:
            truncated += 1
        selected.append({"path": relative, "content": redact_text(content, max_chars=max_file_bytes)})
        total_bytes += len(raw)
    coverage = {
        "selectedFiles": len(selected),
        "eligibleFiles": len(eligible),
        "repositoryFiles": len(discovery.get("file_tree", [])),
        "truncatedFiles": truncated,
        "fileLimit": max_files,
        "bytesSent": total_bytes,
        "analysisMode": mode,
    }
    return selected, coverage


def _build_verification() -> tuple[dict | None, str | None]:
    if os.getenv("ANALYSIS_MODE", "Quick") != "Build Verified":
        return None, None
    try:
        payload = json.loads(BUILD_REPORT_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None, "Build verification did not produce a valid report."
    if (
        not isinstance(payload, dict)
        or payload.get("schemaVersion") != 1
        or payload.get("status") not in BUILD_STATUSES
        or not isinstance(payload.get("commands"), list)
        or len(payload["commands"]) > 6
    ):
        return None, "Build verification report failed validation."
    clean_commands = []
    for item in payload["commands"]:
        if not isinstance(item, dict) or item.get("status") not in {
            "Passed",
            "Failed",
            "Timed Out",
            "Unavailable",
        }:
            return None, "Build verification report failed validation."
        command = item.get("command")
        if not isinstance(command, list) or any(not isinstance(value, str) for value in command):
            return None, "Build verification report failed validation."
        clean_commands.append(
            {
                "label": str(item.get("label") or "")[:120],
                "command": [value[:200] for value in command[:12]],
                "status": item["status"],
                "exitCode": item.get("exitCode") if isinstance(item.get("exitCode"), int) else None,
                "output": redact_text(str(item.get("output") or ""), max_chars=12000),
            }
        )
    return (
        {
            "schemaVersion": 1,
            "status": payload["status"],
            "startedAt": str(payload.get("startedAt") or "")[:64] or None,
            "completedAt": str(payload.get("completedAt") or "")[:64] or None,
            "networkPolicy": (
                payload.get("networkPolicy")
                if payload.get("networkPolicy")
                in {
                    "Deny all network access",
                    "Controlled proxy unavailable to build process",
                }
                else "Unknown"
            ),
            "credentialExposure": "None",
            "commands": clean_commands,
        },
        None,
    )


def main() -> int:
    repository_root = Path("/workspace/repository")
    subdirectory = os.getenv("ANALYSIS_SUBDIRECTORY", "").strip()
    analysis_mode = os.getenv("ANALYSIS_MODE", "Quick")
    root = repository_root / subdirectory if subdirectory else repository_root
    warnings = []
    scanner_runs = []
    cleanup_status = "Pending"
    try:
        if not root.is_dir() or repository_root.resolve() not in (root.resolve(), *root.resolve().parents):
            raise ValueError("The configured repository subdirectory was not found.")
        _post("event", {"status": "Discovering", "progressPercent": 20})
        discovery = discover_repository(root)
        build_verification, build_warning = _build_verification()
        if build_warning:
            warnings.append(build_warning)
        if build_verification:
            _post("event", {"status": "Building", "progressPercent": 28})
            if build_verification["status"] != "Passed":
                warnings.append(
                    f"Build verification status: {build_verification['status']}."
                )

        _post("event", {"status": "Scanning", "progressPercent": 35})
        normalized_findings = []
        for index, scanner in enumerate(SCANNERS):
            _post(
                "event",
                {
                    "status": "Scanning",
                    "progressPercent": 35 + index * 10,
                    "scanner": scanner.name,
                    "scannerEvent": "started",
                },
            )
            run = scanner.run(root, timeout_seconds=180 if analysis_mode == "Quick" else 600)
            scanner_runs.append(run)
            normalized_findings.extend(run.pop("findings", []))
            if run.get("warning"):
                warnings.append(run["warning"])
            _post(
                "event",
                {
                    "status": "Scanning",
                    "progressPercent": 43 + index * 10,
                    "scanner": scanner.name,
                    "scannerEvent": "completed",
                    "exitStatus": run.get("exitStatus"),
                },
            )

        selected_files, evidence_coverage = _selected_file_evidence(root, discovery)
        evidence = {
            "repository": {
                "analysis_mode": analysis_mode,
                "commit_sha": (
                    (repository_root / ".git" / "HEAD").read_text(errors="ignore")[:200]
                    if (repository_root / ".git" / "HEAD").is_file()
                    else None
                )
            },
            "discovery": discovery,
            "selected_files": selected_files,
            "scanner_findings": normalized_findings,
            "scanner_runs": scanner_runs,
        }
        _post("event", {"status": "Analyzing", "progressPercent": 72})
        result, model, prompt_version = analyze(evidence)
        deterministic_apis = discovery.get("api_inventory") or []
        hermes_apis = {}
        for item in result.get("api_inventory", []):
            if not isinstance(item, dict):
                continue
            hermes_apis[(
                str(item.get("method") or "").upper(),
                str(item.get("path") or item.get("route") or ""),
                str(item.get("file") or ""),
            )] = item
        for item in deterministic_apis:
            key = (
                str(item.get("method") or "").upper(),
                str(item.get("path") or ""),
                str(item.get("file") or ""),
            )
            existing = hermes_apis.get(key)
            if existing is None:
                result["api_inventory"].append(item)
                hermes_apis[key] = item
            elif item.get("direction"):
                # Direction is decided by a file-level marker, not by the model.
                existing["direction"] = item["direction"]

        # Deterministic connections outrank the model on wire facts: they are
        # read straight from configuration. Backfill protocol/port/endpoint onto
        # a dependency the model already named, and add the ones it missed.
        result.setdefault("communications", [])
        # Keep one consistent name for the analyzed service within a run. The
        # trusted side resolves whatever this is onto the application node.
        application_name = next(
            (
                str(entry["source"]).strip()
                for entry in result["communications"]
                if isinstance(entry, dict) and str(entry.get("source") or "").strip()
            ),
            "this service",
        )
        for item in discovery.get("connections") or []:
            destination = str(item.get("destination") or "").strip()
            if not destination:
                continue
            existing = next(
                (
                    entry
                    for entry in result["communications"]
                    if isinstance(entry, dict)
                    and destination.lower() in str(entry.get("destination") or "").lower()
                ),
                None,
            )
            if existing is None:
                result["communications"].append(
                    {
                        "source": application_name,
                        "destination": destination,
                        "destination_type": item.get("destination_type") or "External service",
                        "protocol": item.get("protocol"),
                        "port": item.get("port"),
                        "endpoint": item.get("endpoint"),
                        "configuration_key": item.get("configuration_key"),
                        # The connection is dialled outbound either way; whether
                        # the service publishes or consumes is a separate fact,
                        # read from the client usage rather than from the model.
                        "messaging_role": item.get("messaging_role"),
                        "topics": item.get("topics"),
                        "direction": "Outbound",
                        "required": True,
                        "confidence": "Confirmed",
                        "evidence_state": "Configuration Declared",
                        "evidence": item.get("evidence"),
                        "file": item.get("file"),
                        "line": item.get("line"),
                    }
                )
                continue
            for field in ("protocol", "port", "endpoint", "configuration_key"):
                if item.get(field) and not existing.get(field):
                    existing[field] = item[field]
            # Whether the service publishes or consumes, and the topics it names,
            # are read from the client usage in source. They replace whatever the
            # model said about the same broker rather than backfilling it.
            for field in ("messaging_role", "topics"):
                if item.get(field):
                    existing[field] = item[field]
            if item.get("evidence") and not existing.get("evidence"):
                existing["evidence"] = item["evidence"]
            # Configuration is stronger evidence than source inference.
            existing["confidence"] = "Confirmed"
            existing["evidence_state"] = "Configuration Declared"
        if build_verification:
            result.setdefault("application_profile", {})[
                "build_verification"
            ] = build_verification
        _post("event", {"status": "Generating Report", "progressPercent": 90})
        _post(
            "result",
            {
                "result": result,
                "scannerRuns": scanner_runs,
                "scannerFindings": normalized_findings,
                "dependencies": [
                    item for item in normalized_findings if item.get("type") == "dependency"
                ],
                "buildVerification": build_verification,
                "evidenceCoverage": evidence_coverage,
                "warnings": warnings,
                "hermesModel": model,
                "hermesPromptVersion": prompt_version,
                "workspaceCleanupStatus": cleanup_status,
            },
        )
        return 0
    except HermesError as exc:
        _post(
            "event",
            {
                "status": "Failed",
                "failureStage": "Analyzing",
                "safeErrorMessage": safe_error(exc, "Hermes analysis failed safely."),
            },
        )
        return 1
    except Exception as exc:
        _post(
            "event",
            {
                "status": "Failed",
                "failureStage": "Scanning",
                "safeErrorMessage": safe_error(exc),
            },
        )
        return 1
    finally:
        try:
            shutil.rmtree(repository_root)
            cleanup_status = "Completed"
        except OSError:
            cleanup_status = "Failed"
        try:
            _post("cleanup", {"workspaceCleanupStatus": cleanup_status})
        except Exception:
            pass


if __name__ == "__main__":
    raise SystemExit(main())
