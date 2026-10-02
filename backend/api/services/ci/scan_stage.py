"""The Scan stage: a stage whose script KubeSight writes, around one scanner.

A command stage runs whatever its author typed. A scan stage names a TOOL and a
policy, and KubeSight generates the rest — image, commands, report file, gate —
so a pipeline gets a security check without anybody having to know Trivy's
flags or where Dependency-Check keeps its database. Four tools:

* ``trivy_fs`` — Trivy over the checkout: vulnerable dependencies (lockfiles,
  jars), committed secrets, and optionally IaC misconfigurations. Gated like the
  image scan: a severity threshold, block or warn.
* ``semgrep`` — static analysis, gated by the EXISTING code scan quality gate
  (``code_scan``): the same shim, the same stdlib gate, the same report file and
  artifact name, so the build drawer's PDF report and Send dialog work on a scan
  stage without knowing it is one.
* ``dependency_check`` — OWASP Dependency-Check, on the same shared NVD database
  and behind the same lock as the merge check (``merge_checks.stages``), failing
  on a CVSS score at or above a number, or warning.
* ``syft`` — a software bill of materials of the source tree, kept as an
  ``sbom`` artifact. No gate: an SBOM is an inventory, not a verdict.

Generated in the ENGINE (``engine._build_execution``) rather than in a runner,
the way ``code_scan.wrap_commands`` is, so there is one script text however the
stage runs. Today that is the Kubernetes runner only: an agent may run without
the stage image (and so without the scanner), and uploads artifacts only after
a passing stage — a scan that failed its gate would lose the report that says
why. The engine skips a scan stage on such a runner, with the reason.

Every script ends the same way: the machine-readable report saved OUTSIDE the
checkout (``/workspace/.kubesight``, so a later image build never ships it), a
short readable summary in the log, then the verdict. The Kubernetes collector
picks the report up after the last stage, failed or not — a failed gate is
exactly when somebody reads it.

The SBOM is of the SOURCE only. Describing the image a build pushed would mean
pulling it back from the registry, and the only registry credential KubeSight
holds is the push credential, which is mounted into the one stage that pushes
and nowhere else. Handing push rights to a scanner container to read an image
is a trade this stage does not make; the image's own vulnerabilities are what
the image scan gate on the container image stage is for.
"""

from __future__ import annotations

import os
import re
import shlex
from typing import Any, Dict, List, Optional

from ...models_ci import IMAGE_SCAN_ON_FAIL, IMAGE_SCAN_SEVERITIES
from . import build_environments, code_scan

TOOLS = ("trivy_fs", "semgrep", "dependency_check", "syft")

TOOL_LABELS = {
    "trivy_fs": "Trivy filesystem",
    "semgrep": "Semgrep",
    "dependency_check": "Dependency-Check",
    "syft": "SBOM with Syft",
}

# Which catalog image each tool runs in (services/ci/build_environments.py).
ENVIRONMENT_KEYS = {
    "trivy_fs": "trivy",
    "semgrep": "semgrep",
    "dependency_check": "dependency-check",
    "syft": "syft",
}

TRIVY_SCANNERS = ("vuln", "secret", "misconfig")
DEFAULT_TRIVY_SCANNERS = ("vuln", "secret")
# Skipped by a Trivy filesystem scan unless the stage says otherwise. Lockfiles
# already say what node_modules holds, and scanning both counts every finding
# twice and takes minutes; .git is history, not what ships.
DEFAULT_SKIP_DIRS = ("**/node_modules", "**/.git")
SBOM_FORMATS = ("cyclonedx-json", "spdx-json")
# What a Syft stage describes. Only the source tree — see the module docstring
# for why "the image this build pushed" is not offered.
SBOM_TARGETS = ("source",)

DEFAULT_FAIL_ON_CVSS = 7.0
MAX_LIST = 20
MAX_ITEM = 200

# Rule sets and skip patterns reach a shell line. Quoted there too; this keeps
# them to characters a rule id, a registry pack, a path or a glob actually uses.
_RULE_RE = re.compile(r"^[A-Za-z0-9_./:@+=~-]+$")
_GLOB_RE = re.compile(r"^[A-Za-z0-9_./*@+~-]+$")
_URL_RE = re.compile(r"^https?://[^\s'\"`$\\]+$")
_SECRET_NAME_RE = re.compile(r"^[A-Za-z0-9_.-]{1,120}$")

# A distinctive exit code for "Trivy found something at or above the gate", so
# a Trivy that broke (exit 1) is never read as a verdict about the code.
_TRIVY_FOUND = 3

_ON = {"1", "true", "yes", "on"}


def _merge_checks():
    """The merge check modules this stage shares its shell with.

    Imported when a script is generated, not at import time: the merge_checks
    package imports the engine and the pipeline service, both of which import
    this module, so a top-level import would close the loop.
    """
    from .merge_checks import profiles, stages

    return profiles, stages


class ScanConfigError(ValueError):
    pass


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

def default_config(tool: str) -> Dict[str, Any]:
    """What a stage gets when this tool is picked. Mirrored in the editor."""
    if tool == "trivy_fs":
        return {
            "tool": tool,
            "scanners": list(DEFAULT_TRIVY_SCANNERS),
            "threshold": "critical",
            "onFail": "block",
            "ignoreUnfixed": False,
            "skipDirs": list(DEFAULT_SKIP_DIRS),
        }
    if tool == "semgrep":
        return {"tool": tool, "rules": []}
    if tool == "dependency_check":
        return {
            "tool": tool,
            "failOnCvss": DEFAULT_FAIL_ON_CVSS,
            "onFail": "block",
            "nvdApiKeySecret": "",
            "nvdDatafeedUrl": "",
        }
    if tool == "syft":
        return {"tool": tool, "format": SBOM_FORMATS[0], "target": "source"}
    raise ScanConfigError(f"Unknown scan tool '{tool}'.")


def configured(config: Any) -> bool:
    """Whether a stored scan stage names a tool this version can run.

    False for every scan stage saved before scan stages had an executor — those
    carry no config at all, still load, and are skipped with a reason.
    """
    return isinstance(config, dict) and config.get("tool") in TOOLS


def _tools_sentence() -> str:
    return ", ".join(f"{TOOL_LABELS[tool]} ({tool})" for tool in TOOLS)


def _clean_list(value: Any, pattern: re.Pattern, what: str, stage_name: str) -> List[str]:
    if value in (None, ""):
        return []
    if isinstance(value, str):
        value = re.split(r"[\s,]+", value)
    if not isinstance(value, (list, tuple)):
        raise ScanConfigError(f"Stage '{stage_name}': {what} must be a list.")
    out: List[str] = []
    for item in value:
        text = str(item or "").strip()
        if not text:
            continue
        if len(text) > MAX_ITEM or not pattern.match(text):
            raise ScanConfigError(f"Stage '{stage_name}': '{text[:80]}' is not a valid {what[:-1]}.")
        if text not in out:
            out.append(text)
    if len(out) > MAX_LIST:
        raise ScanConfigError(f"Stage '{stage_name}': at most {MAX_LIST} {what}.")
    return out


def _choice(value: Any, allowed, default: str, what: str, stage_name: str) -> str:
    text = str(value or "").strip().lower() or default
    if text not in allowed:
        raise ScanConfigError(
            f"Stage '{stage_name}': {what} must be one of {', '.join(allowed)}, not '{text[:32]}'."
        )
    return text


def normalize(
    value: Any,
    stage_type: str,
    stage_name: str,
    known_secret_keys: Optional[set] = None,
) -> Optional[Dict[str, Any]]:
    """A scan stage's scanner and policy, or None on any other stage.

    Refused rather than dropped off a non-scan stage, like every other per-kind
    setting: a scanner that silently vanished on save is one somebody still
    believes runs. And refused when a scan stage names no tool — the stage would
    otherwise save, and every build would skip it.
    """
    if stage_type != "scan":
        if value in (None, "", {}):
            return None
        raise ScanConfigError(
            f"Stage '{stage_name}' is a {stage_type} stage. Scanner settings go on a scan stage."
        )
    if value in (None, "", {}):
        raise ScanConfigError(
            f"Stage '{stage_name}' is a scan stage but names no scanner, so it would have "
            f"nothing to run. Choose one: {_tools_sentence()}."
        )
    if not isinstance(value, dict):
        raise ScanConfigError(f"Stage '{stage_name}' has an invalid scan configuration.")
    tool = str(value.get("tool") or "").strip().lower()
    if not tool:
        raise ScanConfigError(
            f"Stage '{stage_name}' is a scan stage but names no scanner. Choose one: "
            f"{_tools_sentence()}."
        )
    if tool not in TOOLS:
        raise ScanConfigError(
            f"Stage '{stage_name}': '{tool[:32]}' is not a scanner KubeSight runs. "
            f"Choose one: {_tools_sentence()}."
        )

    if tool == "trivy_fs":
        scanners = _clean_list(value.get("scanners"), re.compile(r"^[a-z]+$"), "scanners", stage_name)
        unknown = [item for item in scanners if item not in TRIVY_SCANNERS]
        if unknown:
            raise ScanConfigError(
                f"Stage '{stage_name}': Trivy scans for {', '.join(TRIVY_SCANNERS)}, "
                f"not '{unknown[0][:32]}'."
            )
        skip = value.get("skipDirs")
        return {
            "tool": tool,
            # Canonical order, so the same choice always writes the same line.
            "scanners": [item for item in TRIVY_SCANNERS if item in scanners]
            or list(DEFAULT_TRIVY_SCANNERS),
            "threshold": _choice(
                value.get("threshold"), IMAGE_SCAN_SEVERITIES, "critical", "the severity threshold", stage_name
            ),
            "onFail": _choice(value.get("onFail"), IMAGE_SCAN_ON_FAIL, "block", "what a finding does", stage_name),
            "ignoreUnfixed": bool(value.get("ignoreUnfixed")),
            # Absent means the defaults; an empty list is "skip nothing".
            "skipDirs": list(DEFAULT_SKIP_DIRS)
            if skip is None
            else _clean_list(skip, _GLOB_RE, "skip patterns", stage_name),
        }

    if tool == "semgrep":
        return {
            "tool": tool,
            # Empty is "automatic": the packs for the service's application type,
            # resolved when the build runs, so changing the type changes the scan.
            "rules": _clean_list(value.get("rules"), _RULE_RE, "rule sets", stage_name),
        }

    if tool == "dependency_check":
        raw = value.get("failOnCvss", DEFAULT_FAIL_ON_CVSS)
        try:
            score = round(float(raw if raw not in (None, "") else DEFAULT_FAIL_ON_CVSS), 1)
        except (TypeError, ValueError):
            raise ScanConfigError(f"Stage '{stage_name}': the CVSS score to fail on must be a number.")
        if not 0.0 <= score <= 10.0:
            raise ScanConfigError(f"Stage '{stage_name}': the CVSS score to fail on must be from 0 to 10.")
        secret = str(value.get("nvdApiKeySecret") or "").strip()
        if secret:
            if not _SECRET_NAME_RE.match(secret):
                raise ScanConfigError(f"Stage '{stage_name}': '{secret[:80]}' is not a secret name.")
            if known_secret_keys is not None and secret not in known_secret_keys:
                raise ScanConfigError(
                    f"Stage '{stage_name}' reads the NVD API key from secret '{secret}', which is "
                    "not defined for this service. Add it on the Settings tab, or leave the key empty."
                )
        mirror = str(value.get("nvdDatafeedUrl") or "").strip()
        if mirror and (len(mirror) > 500 or not _URL_RE.match(mirror)):
            raise ScanConfigError(
                f"Stage '{stage_name}': the NVD data mirror must be an http(s) URL."
            )
        return {
            "tool": tool,
            "failOnCvss": score,
            "onFail": _choice(value.get("onFail"), IMAGE_SCAN_ON_FAIL, "block", "what a finding does", stage_name),
            "nvdApiKeySecret": secret,
            "nvdDatafeedUrl": mirror,
        }

    # syft
    target = str(value.get("target") or "source").strip().lower()
    if target == "image":
        raise ScanConfigError(
            f"Stage '{stage_name}': an SBOM of the pushed image is not available. Reading the image "
            "back would need the registry's push credential in a scanner container; KubeSight "
            "keeps that credential in the stage that pushes. The SBOM describes the source tree."
        )
    return {
        "tool": tool,
        "format": _choice(value.get("format"), SBOM_FORMATS, SBOM_FORMATS[0], "the SBOM format", stage_name),
        "target": _choice(target, SBOM_TARGETS, "source", "the SBOM target", stage_name),
    }


def gate_for(config: Dict[str, Any], code_scan_config: Any, stage_name: str) -> Optional[Dict[str, Any]]:
    """The code scan quality gate a scan stage runs under, or None.

    A Semgrep scan stage always has one — that is what makes it a scan and not
    a log — so a missing gate is the default and a parked one is switched back
    on. Any other tool refuses it: the gate counts Semgrep's findings, and on a
    Trivy stage it would count nothing and fail every build.
    """
    if config.get("tool") != "semgrep":
        if code_scan_config:
            raise ScanConfigError(
                f"Stage '{stage_name}' scans with {TOOL_LABELS[config['tool']]}. The quality gate "
                "counts Semgrep findings, so it only goes on a Semgrep scan stage; this tool has "
                "its own gate in the scan settings."
            )
        return None
    gate = dict(code_scan_config) if isinstance(code_scan_config, dict) else code_scan.default_config()
    gate["enabled"] = True
    return gate


def secret_refs(config: Dict[str, Any]) -> List[Dict[str, str]]:
    """The secrets a scan stage reads, under the names its script expects.

    Dependency-Check only. The key's secret is the one the stage names, else
    ``NVD_API_KEY`` — the name the merge check uses — so a service that set it up
    once for merge checks gets it here too. A secret that does not exist simply
    resolves to nothing, and the script says it is running without a key.
    """
    if not configured(config) or config.get("tool") != "dependency_check":
        return []
    return [
        {"name": config.get("nvdApiKeySecret") or "NVD_API_KEY", "envVar": "NVD_API_KEY"},
        {"name": "NVD_DATAFEED_URL", "envVar": "NVD_DATAFEED_URL"},
    ]


def image_for(config: Dict[str, Any]) -> str:
    """The catalog image the tool runs in — never one a pipeline names.

    An operator repoints it (``CI_TEMPLATE_*_IMAGE``) for a mirror. A stage
    cannot, so a scan stage cannot be turned into "run this image of mine with
    the service's NVD key in its environment".
    """
    return build_environments.image(ENVIRONMENT_KEYS.get(str(config.get("tool")), ""))


def tool_catalog(application_type: str = "") -> List[Dict[str, Any]]:
    """What the editor shows for each tool: image, whether it is configured,
    what the stage produces, and the default rules for this service."""
    out = []
    for tool in TOOLS:
        environment = build_environments.resolve(ENVIRONMENT_KEYS[tool]) or {}
        entry = build_environments.ENVIRONMENTS.get(ENVIRONMENT_KEYS[tool]) or {}
        out.append(
            {
                "tool": tool,
                "label": TOOL_LABELS[tool],
                "image": environment.get("image") or "",
                "imageVariable": entry.get("env") or "",
                "configured": bool(environment.get("configured")),
                "produces": produces(default_config(tool), 0),
            }
        )
        if tool == "semgrep":
            out[-1]["defaultRules"] = _merge_checks()[0].semgrep_rules(
                str(application_type or "generic").strip().lower() or "generic"
            ).split()
    return out


# ---------------------------------------------------------------------------
# Reports and artifacts
# ---------------------------------------------------------------------------

_SBOM_SUFFIX = {"cyclonedx-json": "cdx.json", "spdx-json": "spdx.json"}


def report_file(config: Dict[str, Any], position: int) -> str:
    """The report's name inside the workspace's .kubesight directory."""
    tool = config.get("tool")
    position = int(position)
    if tool == "semgrep":
        return code_scan.report_file(position)
    if tool == "syft":
        return f"sbom-{position}.{_SBOM_SUFFIX.get(config.get('format'), 'cdx.json')}"
    return f"scan-{position}-{'trivy' if tool == 'trivy_fs' else 'dependency-check'}.json"


def report_artifact_name(config: Dict[str, Any], position: int) -> str:
    tool = config.get("tool")
    stage_number = int(position) + 1
    if tool == "semgrep":
        return code_scan.report_artifact_name(position)
    if tool == "syft":
        return f"sbom-stage-{stage_number}.{_SBOM_SUFFIX.get(config.get('format'), 'cdx.json')}"
    return f"{'trivy-fs' if tool == 'trivy_fs' else 'dependency-check'}-stage-{stage_number}.json"


def produces(config: Dict[str, Any], position: int) -> List[Dict[str, str]]:
    """What the stage leaves on the build, by artifact name and type."""
    if not configured(config):
        return []
    return [
        {
            "name": report_artifact_name(config, position),
            "type": "sbom" if config.get("tool") == "syft" else "scan-report",
        }
    ]


def artifact_specs(config: Any, position: int) -> List[Dict[str, Any]]:
    """The Kubernetes collector's specs for this stage's report.

    Absolute paths, outside /workspace/source, so the collector finds them
    whatever the working directory and a later image build never sends them to
    BuildKit. A Semgrep stage's report is code_scan's own spec, so the PDF and
    Send dialog find it by the name they already look for.
    """
    if not configured(config):
        return []
    if config.get("tool") == "semgrep":
        return [code_scan.artifact_spec(position)]
    [item] = produces(config, position)
    return [
        {
            "path": f"/workspace/.kubesight/{report_file(config, position)}",
            "type": item["type"],
            "name": item["name"],
            "workdir": "",
            "stagePosition": int(position),
        }
    ]


# ---------------------------------------------------------------------------
# The generated scripts
# ---------------------------------------------------------------------------

def _q(value: Any) -> str:
    return shlex.quote(str(value))


def _report_prelude(variable: str, file_name: str) -> str:
    return (
        f'{variable}="${{KUBESIGHT_WORKSPACE:-/workspace}}/.kubesight/{file_name}"\n'
        f'mkdir -p "$(dirname "${variable}")"\n'
        f'rm -f "${variable}"\n'
    )


_IMAGE_NAMES = {"trivy_fs": "Trivy", "semgrep": "Semgrep", "dependency_check": "Dependency-Check", "syft": "Syft"}


def _missing_tool(binary: str, tool: str, image: str) -> str:
    """Said before anything runs, because 'command not found' halfway through a
    generated script reads as KubeSight's bug rather than an unmirrored image."""
    variable = (build_environments.ENVIRONMENTS.get(ENVIRONMENT_KEYS[tool]) or {}).get("env", "")
    return (
        f'  echo "[kubesight] Scan FAILED: {binary} is not in this stage\'s image"'
        f" {_q(image or '(none set)')} >&2\n"
        f'  echo "[kubesight] Mirror the {_IMAGE_NAMES[tool]} image to the cluster\'s registry'
        f' and point {variable} at it. Nothing was scanned." >&2\n'
        "  exit 1\n"
    )


def _on_path_check(binary: str) -> List[str]:
    """``KS_FOUND`` set when ``binary`` is a file on PATH.

    Not ``command -v``: by the time this runs, the quality gate's shim has
    defined a shell FUNCTION called semgrep, and ``command -v`` reports that.
    """
    return [
        'KS_FOUND=""',
        'KS_IFS="$IFS"',
        "IFS=:",
        f'for KS_D in $PATH; do if [ -x "$KS_D/{binary}" ]; then KS_FOUND=1; break; fi; done',
        'IFS="$KS_IFS"',
    ]


def _gated_severities(threshold: str) -> str:
    cut = IMAGE_SCAN_SEVERITIES.index(threshold) + 1 if threshold in IMAGE_SCAN_SEVERITIES else 1
    return ",".join(item.upper() for item in IMAGE_SCAN_SEVERITIES[:cut])


def _trivy_script(config: Dict[str, Any], position: int, image: str) -> str:
    """Trivy over the working directory: one full report, then the gate on it.

    The same two steps as the image gate (``runners/kubernetes._scan_and_push``):
    one pass writes every finding at every severity to the report, and the gate
    is ``trivy convert`` over the SAVED report — so the artifact always holds the
    findings below the threshold too, which is what somebody reads when deciding
    whether to tighten it, and the tree is scanned once.
    """
    threshold = config.get("threshold") if config.get("threshold") in IMAGE_SCAN_SEVERITIES else "critical"
    on_fail = config.get("onFail") if config.get("onFail") in IMAGE_SCAN_ON_FAIL else "block"
    scanners = ",".join(item for item in TRIVY_SCANNERS if item in (config.get("scanners") or ())) or "vuln,secret"
    gated = _gated_severities(threshold)
    flags = ""
    for pattern in config.get("skipDirs") or ():
        flags += f" --skip-dirs {_q(pattern)}"
    if config.get("ignoreUnfixed") and "vuln" in scanners:
        flags += " --ignore-unfixed"
    db_repo = os.getenv("CI_TRIVY_DB_REPOSITORY", "").strip()
    if db_repo:
        flags += f" --db-repository {_q(db_repo)}"
    skip_update = os.getenv("CI_TRIVY_SKIP_DB_UPDATE", "0").strip().lower() in _ON

    if on_fail == "block":
        verdict = (
            f'  echo "[kubesight] Scan FAILED: findings at or above {threshold.upper()}. The full'
            ' report is on this build as an artifact." >&2\n'
            "  exit 1\n"
        )
    else:
        verdict = (
            f'  echo "[kubesight] Scan found findings at or above {threshold.upper()}. This gate is'
            ' set to warn, so the stage passes. The full report is on this build as an artifact."\n'
        )

    return (
        "# -- KubeSight scan: Trivy filesystem --\n"
        + _report_prelude("KS_SCAN_REPORT", report_file(config, position))
        + "if ! command -v trivy >/dev/null 2>&1; then\n"
        + _missing_tool("trivy", "trivy_fs", image)
        + "fi\n"
        # The same database directory as the image scan gate, so a service that
        # scans its image and its source downloads the database once.
        + 'if [ -n "${KUBESIGHT_CACHE_DIR:-}" ]; then\n'
        + '  : "${TRIVY_CACHE_DIR:=$KUBESIGHT_CACHE_DIR/trivy}"\n'
        + "else\n"
        + '  : "${TRIVY_CACHE_DIR:=/tmp/trivy-cache}"\n'
        + "fi\n"
        + 'export TRIVY_CACHE_DIR\n'
        + ("export TRIVY_SKIP_DB_UPDATE=true\n" if skip_update else "")
        + f'echo "[kubesight] == scan == Trivy filesystem ({scanners}), gate at {threshold.upper()} ({on_fail})"\n'
        + "KS_TRIVY_RC=0\n"
        + (
            f'trivy fs --format json --output "$KS_SCAN_REPORT" --scanners {scanners} '
            f"--severity CRITICAL,HIGH,MEDIUM,LOW --no-progress{flags} . || KS_TRIVY_RC=$?\n"
        )
        + 'if [ "$KS_TRIVY_RC" -ne 0 ] || [ ! -s "$KS_SCAN_REPORT" ]; then\n'
        + '  echo "[kubesight] Scan FAILED: Trivy did not finish (exit $KS_TRIVY_RC), so nothing was checked." >&2\n'
        + '  echo "[kubesight] A first run downloads the vulnerability database: a cluster with no route to" >&2\n'
        + '  echo "[kubesight] ghcr.io needs CI_TRIVY_DB_REPOSITORY pointed at a mirror." >&2\n'
        + '  exit "$([ "$KS_TRIVY_RC" -ne 0 ] && echo "$KS_TRIVY_RC" || echo 1)"\n'
        + "fi\n"
        + 'ks_count() { grep -o "\\"Severity\\": *\\"$1\\"" "$KS_SCAN_REPORT" 2>/dev/null | wc -l | tr -d " "; }\n'
        + 'echo "[kubesight] Findings: CRITICAL $(ks_count CRITICAL), HIGH $(ks_count HIGH),'
        + ' MEDIUM $(ks_count MEDIUM), LOW $(ks_count LOW)."\n'
        + "KS_GATE_RC=0\n"
        + f'trivy convert --format table --severity {gated} --exit-code {_TRIVY_FOUND} "$KS_SCAN_REPORT" || KS_GATE_RC=$?\n'
        + f'if [ "$KS_GATE_RC" -eq {_TRIVY_FOUND} ]; then\n'
        + verdict
        + 'elif [ "$KS_GATE_RC" -ne 0 ]; then\n'
        + '  echo "[kubesight] Scan FAILED: Trivy could not read back its own report (exit $KS_GATE_RC)." >&2\n'
        + "  exit 1\n"
        + "else\n"
        + f'  echo "[kubesight] Scan passed: nothing at or above {threshold.upper()}."\n'
        + "fi\n"
    )


def _semgrep_commands(config: Dict[str, Any], application_type: str) -> List[str]:
    """The scan itself. ``code_scan.wrap_commands`` adds the report file and the
    gate around it — the gate never sees a difference from a hand-written one.

    Automatic rules follow the merge check exactly (the type's packs, rules
    committed to the repository win); rules chosen on the stage are used as
    chosen. ``SEMGREP_RULES`` in the stage's variables overrides either.
    """
    profiles, merge_check_stages = _merge_checks()
    chosen = list(config.get("rules") or [])
    rules = " ".join(chosen) or profiles.semgrep_rules(
        str(application_type or "generic").strip().lower() or "generic"
    )
    return [
        "# -- KubeSight scan: Semgrep --",
        *_on_path_check("semgrep"),
        'if [ -z "$KS_FOUND" ]; then',
        *_missing_tool("semgrep", "semgrep", build_environments.image("semgrep")).rstrip("\n").split("\n"),
        "fi",
        *merge_check_stages.semgrep_rules_lines(rules, repository_rules_win=not chosen),
        'echo "[kubesight] == scan == Semgrep"',
        # The target is named: the official image sets SEMGREP_IN_DOCKER and,
        # without one, insists the code is mounted at /src. --metrics=off
        # because a build must not report on a private repository.
        "semgrep scan $CONFIG_ARGS --metrics=off --disable-version-check .",
    ]


def _dependency_check_script(config: Dict[str, Any], position: int, image: str, application_type: str) -> str:
    """Dependency-Check over the working directory, gated on the highest CVSS.

    The scan, the database lock and the NVD arguments are the merge check's own
    shell (``merge_checks.stages``): the database is shared by every service,
    and two lock implementations would be two processes each sure it holds the
    H2 file. What differs is the verdict. Dependency-Check's own exit code is
    not used — it means "over --failOnCVSS" in one version and "an analyzer
    failed" in another — so the scan runs with ``--failOnCVSS 11`` (never) and
    the gate reads the report: the stage fails when any score, CVSS v2, v3 or
    v4, is at or above the number. A finding NVD has not scored yet has no
    number and is listed in the report without counting.
    """
    _, merge_check_stages = _merge_checks()
    threshold = float(config.get("failOnCvss", DEFAULT_FAIL_ON_CVSS))
    shown = f"{threshold:.1f}"
    on_fail = config.get("onFail") if config.get("onFail") in IMAGE_SCAN_ON_FAIL else "block"
    app_type = str(application_type or "generic").strip().lower() or "generic"
    mirror = str(config.get("nvdDatafeedUrl") or "")
    log_file = f'"${{KUBESIGHT_WORKSPACE:-/workspace}}/.kubesight/dependency-check-{int(position)}.log"'
    out_dir = f'"${{KUBESIGHT_WORKSPACE:-/workspace}}/.kubesight/dependency-check-{int(position)}"'

    extra = ' --exclude "**/node_modules/**" --exclude "**/.git/**"'
    if app_type in merge_check_stages.DC_EXPERIMENTAL_TYPES:
        extra += " --enableExperimental"

    java_note = ""
    if app_type in merge_check_stages.JAVA_TYPES:
        # Dependency-Check reads jars, not build.gradle or pom.xml.
        java_note = (
            'if [ -z "$(find . -type f \\( -name "*.jar" -o -name "*.war" -o -name "*.aar" \\)'
            ' -not -path "./.git/*" 2>/dev/null | head -n 1)" ]; then\n'
            '  echo "[kubesight] Warning: no jars here yet. Dependency-Check reads built jars, not'
            ' the build file - put this stage after the stage that builds."\n'
            "fi\n"
        )

    if on_fail == "block":
        verdict = (
            f'  echo "[kubesight] Scan FAILED: a vulnerability scores CVSS $KS_MAX, at or above {shown}.'
            ' The full report is on this build as an artifact." >&2\n'
            "  exit 1\n"
        )
    else:
        verdict = (
            f'  echo "[kubesight] Scan found a vulnerability scoring CVSS $KS_MAX, at or above {shown}.'
            ' This gate is set to warn, so the stage passes. The full report is on this build as an artifact."\n'
        )

    hints = "\n".join(merge_check_stages.dependency_check_failure_hints(log='"$KS_DC_LOG"'))
    scores = (
        "grep -Eo '\"(baseScore|score)\" *: *[0-9]+(\\.[0-9]+)?' \"$KS_SCAN_REPORT\" 2>/dev/null"
        " | sed 's/.*: *//'"
    )
    return (
        "# -- KubeSight scan: OWASP Dependency-Check --\n"
        + _report_prelude("KS_SCAN_REPORT", report_file(config, position))
        + f"KS_DC_LOG={log_file}\n"
        + f"KS_DC_OUT={out_dir}\n"
        + 'rm -rf "$KS_DC_OUT"\n'
        + 'mkdir -p "$KS_DC_OUT"\n'
        + "KS_DC_BIN=/usr/share/dependency-check/bin/dependency-check.sh\n"
        + 'if [ ! -x "$KS_DC_BIN" ]; then\n'
        + '  KS_DC_BIN="$(command -v dependency-check.sh 2>/dev/null || command -v dependency-check 2>/dev/null || true)"\n'
        + "fi\n"
        + 'if [ -z "$KS_DC_BIN" ]; then\n'
        + _missing_tool("dependency-check", "dependency_check", image)
        + "fi\n"
        + (
            # A mirror named on the stage, unless a NVD_DATAFEED_URL secret or
            # variable already says where - the more specific setting wins.
            f'if [ -z "${{NVD_DATAFEED_URL:-}}" ]; then NVD_DATAFEED_URL={_q(mirror)}; fi\n'
            if mirror
            else ""
        )
        + 'DATA_DIR="${DC_DATA_DIR:-/tmp/dependency-check-data}"\n'
        + 'mkdir -p "$DATA_DIR"\n'
        + java_note
        + "\n".join(merge_check_stages.dependency_check_lock_lines())
        + "\n"
        + "\n".join(merge_check_stages.dependency_check_nvd_lines())
        + "\n"
        + f'echo "[kubesight] == scan == Dependency-Check, fails on CVSS {shown} or higher ({on_fail})"\n'
        + '( "$KS_DC_BIN" \\\n'
        + '  --project "${KUBESIGHT_SERVICE_SLUG:-scan}" \\\n'
        + "  --scan . \\\n"
        + "  --format JSON \\\n"
        + '  --out "$KS_DC_OUT" \\\n'
        + '  --data "$DATA_DIR" \\\n'
        + f"  --failOnCVSS 11{extra} \\\n"
        + '  $NVD_ARGS 2>&1 || true ) | tee "$KS_DC_LOG"\n'
        + "dc_unlock\n"
        + "trap - EXIT\n"
        + 'if [ ! -s "$KS_DC_OUT/dependency-check-report.json" ]; then\n'
        + '  echo "[kubesight] Scan FAILED: Dependency-Check produced no report, so nothing was checked." >&2\n'
        + hints
        + "\n  exit 1\n"
        + "fi\n"
        + 'mv "$KS_DC_OUT/dependency-check-report.json" "$KS_SCAN_REPORT"\n'
        + f"KS_MAX=$({scores} | awk 'BEGIN {{ m = 0 }} {{ if ($1 + 0 > m) m = $1 + 0 }} END {{ printf \"%.1f\", m }}')\n"
        + f"KS_OVER=$({scores} | awk -v t={shown} '$1 + 0 >= t {{ c++ }} END {{ print c + 0 }}')\n"
        + "KS_VULNS=$(grep -Eo '\"name\" *: *\"(CVE|GHSA)-[^\"]*\"' \"$KS_SCAN_REPORT\" 2>/dev/null"
        + " | sed 's/.*: *//' | sort -u | wc -l | tr -d ' ')\n"
        + 'echo "[kubesight] $KS_VULNS distinct vulnerabilities; highest CVSS $KS_MAX;'
        + f' $KS_OVER score(s) at or above {shown}."\n'
        # Counted, not compared against the maximum: with nothing scored the
        # maximum reads 0.0, and a threshold of 0 would fail a clean scan.
        + 'if [ "${KS_OVER:-0}" -gt 0 ]; then\n'
        + verdict
        + "else\n"
        + f'  echo "[kubesight] Scan passed: nothing scores CVSS {shown} or higher."\n'
        + "fi\n"
    )


def _syft_script(config: Dict[str, Any], position: int, image: str) -> str:
    """An SBOM of the working directory. Passes when one was written.

    Run in ``anchore/syft:debug``, whose binary sits at /syft rather than on
    PATH, hence the lookup. Packages are counted by their purl, which both
    formats carry once per package.
    """
    fmt = config.get("format") if config.get("format") in SBOM_FORMATS else SBOM_FORMATS[0]
    count = (
        "grep -o '\"purl\"' \"$KS_SBOM\""
        if fmt == "cyclonedx-json"
        else "grep -Eo '\"referenceType\" *: *\"purl\"' \"$KS_SBOM\""
    )
    label = "CycloneDX JSON" if fmt == "cyclonedx-json" else "SPDX JSON"
    return (
        "# -- KubeSight scan: SBOM with Syft --\n"
        + _report_prelude("KS_SBOM", report_file(config, position))
        + 'KS_SYFT="$(command -v syft 2>/dev/null || true)"\n'
        + 'if [ -z "$KS_SYFT" ] && [ -x /syft ]; then KS_SYFT=/syft; fi\n'
        + 'if [ -z "$KS_SYFT" ]; then\n'
        + _missing_tool("syft", "syft", image)
        + "fi\n"
        + "export SYFT_CHECK_FOR_APP_UPDATE=false\n"
        + f'echo "[kubesight] == sbom == Syft, {label}, of the source tree"\n'
        + f'"$KS_SYFT" dir:. -o {fmt}="$KS_SBOM"\n'
        + 'if [ ! -s "$KS_SBOM" ]; then\n'
        + '  echo "[kubesight] SBOM FAILED: Syft wrote nothing." >&2\n'
        + "  exit 1\n"
        + "fi\n"
        + f"KS_PACKAGES=$({count} 2>/dev/null | wc -l | tr -d ' ')\n"
        + f'echo "[kubesight] SBOM written: $KS_PACKAGES packages. Kept on this build as'
        + f' {report_artifact_name(config, position)}."\n'
    )


def commands(
    config: Dict[str, Any],
    *,
    position: int,
    application_type: str = "",
    gate: Optional[Dict[str, Any]] = None,
) -> List[str]:
    """The stage's whole script, as ONE line for the same heredoc reason as
    ``code_scan.wrap_commands``: the runners join lines with newlines, and the
    Semgrep gate's heredoc has to reach them intact."""
    tool = config.get("tool")
    image = image_for(config)
    if tool == "trivy_fs":
        return [_trivy_script(config, position, image)]
    if tool == "dependency_check":
        return [_dependency_check_script(config, position, image, application_type)]
    if tool == "syft":
        return [_syft_script(config, position, image)]
    if tool == "semgrep":
        return code_scan.wrap_commands(
            _semgrep_commands(config, application_type),
            gate or code_scan.default_config(),
            position,
        )
    raise ScanConfigError(f"Unknown scan tool '{tool}'.")


def summary(config: Any) -> str:
    """One line for the build drawer and the MCP answer."""
    if not configured(config):
        return "No scanner chosen"
    tool = config["tool"]
    if tool == "trivy_fs":
        return (
            f"Trivy filesystem ({', '.join(config.get('scanners') or DEFAULT_TRIVY_SCANNERS)}), "
            f"{config.get('onFail', 'block')} at {str(config.get('threshold') or 'critical').upper()}"
        )
    if tool == "dependency_check":
        return f"Dependency-Check, {config.get('onFail', 'block')} at CVSS {float(config.get('failOnCvss', 7.0)):.1f}"
    if tool == "syft":
        return f"SBOM with Syft ({config.get('format', SBOM_FORMATS[0])})"
    return "Semgrep, with the code scan quality gate"
