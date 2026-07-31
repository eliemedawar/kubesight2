"""Deterministic repository discovery and scanner adapter normalization."""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .application_intelligence_security import EXCLUDED_DIRECTORIES, redact_structure, safe_error

MAX_FILES = int(os.getenv("APPLICATION_ANALYSIS_MAX_FILES", "12000"))
MAX_FILE_BYTES = int(os.getenv("APPLICATION_ANALYSIS_MAX_FILE_BYTES", "1048576"))
MAX_REPOSITORY_BYTES = int(os.getenv("APPLICATION_ANALYSIS_MAX_REPOSITORY_BYTES", "524288000"))

ECOSYSTEM_MARKERS = {
    "pom.xml": ("Java", "Maven", "Spring Boot"),
    "build.gradle": ("Java", "Gradle", None),
    "build.gradle.kts": ("Kotlin", "Gradle", None),
    "package.json": ("JavaScript/TypeScript", "npm", None),
    "requirements.txt": ("Python", "pip", None),
    "pyproject.toml": ("Python", "Python packaging", None),
    "go.mod": ("Go", "Go modules", None),
    "*.csproj": (".NET", "MSBuild", None),
    "composer.json": ("PHP", "Composer", None),
    "Gemfile": ("Ruby", "Bundler", None),
}
SPECIAL_FILES = {
    "dockerfiles": ("Dockerfile",),
    "compose_files": ("docker-compose.yml", "docker-compose.yaml", "compose.yml", "compose.yaml"),
    "ci_files": ("Jenkinsfile", "bitbucket-pipelines.yml", ".gitlab-ci.yml"),
    "api_specs": ("openapi.yaml", "openapi.yml", "openapi.json", "swagger.yaml", "swagger.json"),
}

API_ROUTE_PATTERNS = (
    (
        "Flask/FastAPI",
        re.compile(
            r"@(?:app|router|blueprint|bp)\.(?P<method>get|post|put|patch|delete|options|head)"
            r"\(\s*[\"'](?P<path>/[^\"']*)[\"']",
            re.IGNORECASE,
        ),
    ),
    (
        "Express",
        re.compile(
            r"(?:app|router)\.(?P<method>get|post|put|patch|delete|options|head)"
            r"\(\s*[\"'`](?P<path>/[^\"'`]*)[\"'`]",
            re.IGNORECASE,
        ),
    ),
    (
        "Spring",
        re.compile(
            r"@(?P<method>Get|Post|Put|Patch|Delete)Mapping"
            r"\(\s*(?:value\s*=\s*)?[\"'](?P<path>/[^\"']*)[\"']",
        ),
    ),
    (
        "ASP.NET",
        re.compile(
            r"\.Map(?P<method>Get|Post|Put|Patch|Delete)"
            r"\(\s*[\"'](?P<path>/[^\"']*)[\"']",
        ),
    ),
    (
        "Go net/http",
        re.compile(
            r"(?:http\.)?HandleFunc\(\s*[\"'](?P<path>/[^\"']*)[\"']",
        ),
    ),
)


# A Spring Feign client interface declares the routes it *calls* with the same
# annotations a controller uses to declare the routes it *serves*. Without this
# distinction an outbound dependency is reported as an endpoint the service
# exposes, which overstates its attack surface.
OUTBOUND_CLIENT_MARKERS = (
    re.compile(r"@FeignClient\b"),
    re.compile(r"@HttpExchange\b"),
    re.compile(r"@RegisterRestClient\b"),
)


def _iso_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _route_direction(content: str) -> str:
    """Whether a source file declares served routes or consumed ones."""
    if any(marker.search(content) for marker in OUTBOUND_CLIENT_MARKERS):
        return "Outbound"
    return "Inbound"


def _discover_api_inventory(root: Path, files: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Extract bounded, deterministic HTTP route evidence without executing code."""
    inventory: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str]] = set()
    max_routes = int(os.getenv("APPLICATION_ANALYSIS_MAX_API_ROUTES", "1000"))
    source_suffixes = {".py", ".js", ".jsx", ".ts", ".tsx", ".java", ".cs", ".go"}
    for item in files:
        relative = str(item.get("path") or "")
        path = root / relative
        if (
            not item.get("eligible")
            or path.suffix.lower() not in source_suffixes
            or len(inventory) >= max_routes
        ):
            continue
        try:
            content = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        direction = _route_direction(content)
        for framework, pattern in API_ROUTE_PATTERNS:
            for match in pattern.finditer(content):
                route = match.group("path")
                method = (
                    match.groupdict().get("method") or "ANY"
                ).replace("Mapping", "").upper()
                key = (method, route, relative)
                if key in seen:
                    continue
                seen.add(key)
                inventory.append(
                    {
                        "method": method,
                        "path": route,
                        "direction": direction,
                        "framework": framework,
                        "file": relative,
                        "line": content.count("\n", 0, match.start()) + 1,
                        "confidence": "Confirmed",
                        "evidence_state": "Source Inferred",
                        "source": "deterministic",
                    }
                )
                if len(inventory) >= max_routes:
                    break
            if len(inventory) >= max_routes:
                break
    return inventory


def discover_repository(root: Path) -> dict[str, Any]:
    root = root.resolve()
    files: list[dict[str, Any]] = []
    total_bytes = 0
    detected: list[dict[str, Any]] = []
    special = {key: [] for key in SPECIAL_FILES}
    manifests: list[str] = []
    kubernetes_files: list[str] = []
    helm_charts: list[str] = []

    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [
            name
            for name in sorted(dirnames)
            if name not in EXCLUDED_DIRECTORIES and not name.startswith(".terraform")
        ]
        for filename in sorted(filenames):
            path = Path(dirpath) / filename
            try:
                if path.is_symlink():
                    continue
                size = path.stat().st_size
            except OSError:
                continue
            total_bytes += size
            if total_bytes > MAX_REPOSITORY_BYTES:
                raise ValueError("Repository is larger than the configured analysis limit.")
            relative = path.relative_to(root).as_posix()
            if len(files) >= MAX_FILES:
                raise ValueError("Repository contains more files than the configured analysis limit.")
            files.append({"path": relative, "size": size, "eligible": size <= MAX_FILE_BYTES})

            for marker, technology in ECOSYSTEM_MARKERS.items():
                matched = filename.endswith(".csproj") if marker == "*.csproj" else filename == marker
                if matched:
                    manifests.append(relative)
                    detected.append(
                        {
                            "language": technology[0],
                            "buildSystem": technology[1],
                            "frameworkHint": technology[2],
                            "evidence": relative,
                        }
                    )
            for key, names in SPECIAL_FILES.items():
                if filename in names or (key == "dockerfiles" and filename.startswith("Dockerfile")):
                    special[key].append(relative)
            lower = relative.lower()
            if filename == "Chart.yaml":
                helm_charts.append(str(Path(relative).parent.as_posix()))
            if (
                lower.startswith(("k8s/", "kubernetes/", "manifests/", "deploy/"))
                and path.suffix.lower() in {".yaml", ".yml"}
            ):
                kubernetes_files.append(relative)

    return {
        "file_tree": files,
        "repository_size_bytes": total_bytes,
        "detected_technology": detected,
        "dependency_manifests": manifests,
        "kubernetes_manifests": kubernetes_files,
        "helm_charts": helm_charts,
        "api_inventory": _discover_api_inventory(root, files),
        **special,
    }


class ScannerAdapter:
    name = "base"
    executable = ""

    def command(self, root: Path) -> list[str]:
        raise NotImplementedError

    def normalize(self, output: str) -> list[dict[str, Any]]:
        return []

    def run(self, root: Path, timeout_seconds: int = 600) -> dict[str, Any]:
        started = _iso_now()
        if not shutil.which(self.executable):
            return {
                "name": self.name,
                "version": None,
                "startedAt": started,
                "completedAt": _iso_now(),
                "exitStatus": "unavailable",
                "warning": f"{self.name} is not installed in the analysis image.",
                "findings": [],
            }
        try:
            version = subprocess.run(
                [self.executable, "--version"],
                cwd=root,
                capture_output=True,
                text=True,
                timeout=15,
                check=False,
            )
            completed = subprocess.run(
                self.command(root),
                cwd=root,
                capture_output=True,
                text=True,
                timeout=timeout_seconds,
                check=False,
            )
            output = completed.stdout or "{}"
            output_bytes = len(output.encode("utf-8", errors="replace"))
            if output_bytes > int(
                os.getenv("APPLICATION_ANALYSIS_SCANNER_OUTPUT_MAX_BYTES", "20000000")
            ):
                raise ValueError(f"{self.name} output exceeded the configured limit.")
            findings = self.normalize(output)
            return {
                "name": self.name,
                "version": (version.stdout or version.stderr or "").strip()[:200],
                "startedAt": started,
                "completedAt": _iso_now(),
                "exitStatus": completed.returncode,
                "warning": safe_error(RuntimeError(completed.stderr)) if completed.returncode > 1 else None,
                "findings": redact_structure(findings),
            }
        except Exception as exc:
            return {
                "name": self.name,
                "version": None,
                "startedAt": started,
                "completedAt": _iso_now(),
                "exitStatus": "failed",
                "warning": safe_error(exc, f"{self.name} failed safely."),
                "findings": [],
            }


class SemgrepAdapter(ScannerAdapter):
    name = "Semgrep"
    executable = "semgrep"

    def command(self, root: Path) -> list[str]:
        return ["semgrep", "scan", "--config", "auto", "--json", "--metrics=off", str(root)]

    def normalize(self, output: str) -> list[dict[str, Any]]:
        data = json.loads(output or "{}")
        return [
            {
                "scanner": self.name,
                "ruleId": item.get("check_id"),
                "severity": str(item.get("extra", {}).get("severity") or "WARNING").title(),
                "title": item.get("extra", {}).get("message") or item.get("check_id"),
                "file": item.get("path"),
                "startLine": item.get("start", {}).get("line"),
                "endLine": item.get("end", {}).get("line"),
            }
            for item in data.get("results", [])
        ]


class TrivyAdapter(ScannerAdapter):
    name = "Trivy"
    executable = "trivy"

    def command(self, root: Path) -> list[str]:
        return ["trivy", "fs", "--format", "json", "--scanners", "vuln,secret,misconfig", str(root)]

    def normalize(self, output: str) -> list[dict[str, Any]]:
        data = json.loads(output or "{}")
        items = []
        for result in data.get("Results", []):
            for vuln in result.get("Vulnerabilities") or []:
                items.append(
                    {
                        "scanner": self.name,
                        "ruleId": vuln.get("VulnerabilityID"),
                        "severity": str(vuln.get("Severity") or "UNKNOWN").title(),
                        "title": vuln.get("Title") or vuln.get("VulnerabilityID"),
                        "file": result.get("Target"),
                        "package": vuln.get("PkgName"),
                        "version": vuln.get("InstalledVersion"),
                    }
                )
            for secret in result.get("Secrets") or []:
                items.append(
                    {
                        "scanner": self.name,
                        "ruleId": secret.get("RuleID"),
                        "severity": str(secret.get("Severity") or "HIGH").title(),
                        "title": secret.get("Title") or "Potential secret",
                        "file": result.get("Target"),
                        "startLine": secret.get("StartLine"),
                        "endLine": secret.get("EndLine"),
                    }
                )
        return items


class HadolintAdapter(ScannerAdapter):
    name = "Hadolint"
    executable = "hadolint"

    def command(self, root: Path) -> list[str]:
        dockerfiles = [path for path in root.rglob("Dockerfile*") if path.is_file()]
        return ["hadolint", "--format", "json", *[str(path) for path in dockerfiles]]

    def normalize(self, output: str) -> list[dict[str, Any]]:
        return [
            {
                "scanner": self.name,
                "ruleId": item.get("code"),
                "severity": str(item.get("level") or "warning").title(),
                "title": item.get("message"),
                "file": item.get("file"),
                "startLine": item.get("line"),
            }
            for item in json.loads(output or "[]")
        ]


class SyftAdapter(ScannerAdapter):
    name = "Syft"
    executable = "syft"

    def command(self, root: Path) -> list[str]:
        return ["syft", f"dir:{root}", "-o", "json"]

    def normalize(self, output: str) -> list[dict[str, Any]]:
        data = json.loads(output or "{}")
        return [
            {
                "scanner": self.name,
                "type": "dependency",
                "name": item.get("name"),
                "version": item.get("version"),
                "ecosystem": item.get("type"),
                "licenses": [
                    license_item.get("value")
                    for license_item in item.get("licenses") or []
                    if isinstance(license_item, dict)
                ],
                "source": (item.get("locations") or [{}])[0].get("path"),
            }
            for item in data.get("artifacts", [])
        ]


SCANNERS = (SemgrepAdapter(), TrivyAdapter(), SyftAdapter(), HadolintAdapter())
