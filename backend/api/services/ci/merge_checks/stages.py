"""The merge check pipeline: what each tool runs, and how it reports a number.

This module produces ordinary KubeSight stage dictionaries — the same shape
``default_pipelines`` emits and ``pipelines.normalize_stage`` validates. That is
the whole design: a merge check is an ORDINARY BUILD of an ordinary pipeline,
so it inherits runners, caches, secret masking, logs, cancellation, retries and
restart safety without a second execution path to keep correct. The only thing
special about it is that something reads the log afterwards.

Because it is an ordinary pipeline, it is also editable. The stages generated
here are a starting point that works for a conventional repository; a project
with an unusual layout changes the commands and keeps the gate. The one thing
an edit must preserve is the ``##kubesight-metric`` line — without it the tool
reports `missing`, which the gate reads as "not checked", not as "clean".

Three conventions hold across all three scripts:

*The tool never fails the stage on findings.* Findings are the gate's business,
not the stage's. A stage fails only when the tool could not run or produced no
report, because that is a different fact and the gate treats it differently.

*Every stage prints exactly one metric line, on every path.* Including the paths
where it decided there was nothing to do — a skipped check says so rather than
vanishing.

*Counting happens in the tool's own image.* Each image has its own idea of what
utilities exist; the counting in each script uses only what that image ships.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from .. import build_environments
from ....models_merge_checks import MERGE_CHECK_TOOLS, TOOL_LABELS
from .metrics import SENTINEL, STAGE_TOOL_ENV

# The checkout every check runs against. Named the same as everywhere else in
# CI so the stage reads identically in the editor.
CHECKOUT_STAGE = "Checkout"

_STAGE_NAMES = {
    "eslint": "ESLint",
    "semgrep": "Semgrep scan",
    "sonar": "SonarQube scan",
    "dependency_check": "Dependency-Check",
}

# Severity floors, worst first, as each tool spells them.
#
# SonarQube has five levels and the gate has five names, but they do not line up
# one to one: Sonar's MAJOR is the level most installations treat as "must fix",
# so both `high` and `medium` map to it. Mapping `medium` down to MINOR instead
# would count every naming convention in the repository as a merge blocker.
_SONAR_SEVERITIES = ("BLOCKER", "CRITICAL", "MAJOR", "MINOR", "INFO")
_SONAR_BY_FLOOR = {
    "critical": ("BLOCKER", "CRITICAL"),
    "high": ("BLOCKER", "CRITICAL", "MAJOR"),
    "medium": ("BLOCKER", "CRITICAL", "MAJOR"),
    "low": ("BLOCKER", "CRITICAL", "MAJOR", "MINOR"),
    "info": _SONAR_SEVERITIES,
}
# Semgrep has three levels — ERROR, WARNING, INFO — against the gate's five.
# INFO is informational by the tool's own definition, so it only counts at the
# bottom two floors; anything else would make a note about a naming convention
# block a merge.
_SEMGREP_BY_FLOOR = {
    "critical": ("ERROR",),
    "high": ("ERROR",),
    "medium": ("ERROR", "WARNING"),
    "low": ("ERROR", "WARNING", "INFO"),
    "info": ("ERROR", "WARNING", "INFO"),
}
_DC_BY_FLOOR = {
    "critical": ("CRITICAL",),
    "high": ("CRITICAL", "HIGH"),
    "medium": ("CRITICAL", "HIGH", "MEDIUM"),
    "low": ("CRITICAL", "HIGH", "MEDIUM", "LOW"),
    "info": ("CRITICAL", "HIGH", "MEDIUM", "LOW", "INFO"),
}


def stage_name_for(tool: str) -> str:
    return _STAGE_NAMES.get(tool, TOOL_LABELS.get(tool, tool))


def tool_label(tool: str) -> str:
    return TOOL_LABELS.get(tool, tool)


def _metric(tool: str, status: str, **pairs: Any) -> str:
    """One `echo` of a metric line, assembled so the scripts stay readable."""
    parts = [f"tool={tool}", f"status={status}"]
    parts.extend(f"{key}={value}" for key, value in pairs.items())
    return f'echo "{SENTINEL} {" ".join(parts)}"'


# ---------------------------------------------------------------------------
# ESLint
# ---------------------------------------------------------------------------

def _eslint_commands(count_warnings: bool) -> List[str]:
    """Install, lint to JSON, count with the Node that is already there.

    ``npx --no-install`` on purpose: it uses the ESLint the project depends on,
    at the version and with the config the project pins. Fetching a floating
    ESLint from the network would lint the repository against rules its authors
    never agreed to, and would do it differently next week.
    """
    counted = "e+w" if count_warnings else "e"
    return [
        "if [ ! -f package.json ]; then",
        '  echo "No package.json here - ESLint has nothing to check."',
        f"  {_metric('eslint', 'skipped', problems=0)}",
        "  exit 0",
        "fi",
        "",
        "if [ -f package-lock.json ]; then",
        "  npm ci --no-audit --no-fund",
        "else",
        "  npm install --no-audit --no-fund",
        "fi",
        "",
        "# ESLint exits non-zero when it finds anything. Findings are the quality",
        "# gate's business, so the exit code is ignored here and the REPORT is",
        "# what decides whether this stage worked.",
        "npx --no-install eslint . --format json --output-file eslint-report.json || true",
        "",
        "if [ ! -s eslint-report.json ]; then",
        '  echo "ESLint produced no report - is eslint a dependency of this project?"',
        f"  {_metric('eslint', 'error', problems=0)}",
        "  exit 1",
        "fi",
        "",
        "node -e \"const r=require('./eslint-report.json');"
        "let e=0,w=0,f=0;"
        "for(const x of r){e+=x.errorCount||0;w+=x.warningCount||0;"
        "if((x.errorCount||0)+(x.warningCount||0)>0)f++;}"
        f"const c={counted};"
        f"console.log('{SENTINEL} tool=eslint status=ok problems='+c"
        "+' errors='+e+' warnings='+w+' files='+f);\"",
    ]


# ---------------------------------------------------------------------------
# Semgrep — the same job as SonarQube, with nothing to operate
# ---------------------------------------------------------------------------

def _semgrep_commands(min_severity: str) -> List[str]:
    """Scan the checkout, count the findings, print the number.

    One step, unlike SonarQube: Semgrep reads the working tree and writes a
    report, so there is no server to upload to, no analysis to wait for, and no
    web API to ask afterwards. That is the entire difference between the two as
    far as this gate is concerned — and it is why a site with no SonarQube can
    pick this and get the same verdict.

    ``--metrics=off`` because a merge check must not phone home about somebody's
    private repository, and ``SEMGREP_RULES`` so an installation that wants no
    network at all can point it at rules committed in the repository instead of
    the hosted registry.
    """
    counted = _SEMGREP_BY_FLOOR.get(min_severity, _SEMGREP_BY_FLOOR["medium"])
    counted_literal = ",".join(f"'{level}'" for level in counted)
    return [
        '# Rules: a registry pack by default. Set SEMGREP_RULES to a path inside',
        '# the repository (e.g. .semgrep/) to run with no network at all.',
        'RULES="${SEMGREP_RULES:-p/default}"',
        'echo "Scanning with rules: $RULES"',
        "",
        "# Exits non-zero when it finds anything. Findings are the quality gate's",
        "# business, so the report is what decides whether this stage worked.",
        "semgrep scan \\",
        '  --config "$RULES" \\',
        "  --json \\",
        "  --output semgrep-report.json \\",
        "  --metrics=off \\",
        "  --quiet || true",
        "",
        "if [ ! -s semgrep-report.json ]; then",
        '  echo "Semgrep produced no report."',
        f"  {_metric('semgrep', 'error', problems=0)}",
        "  exit 1",
        "fi",
        "",
        # Counted in Python because the Semgrep image is a Python one — the
        # convention every script here follows: count in whatever the tool's own
        # image already ships, never by adding an interpreter to it.
        'python3 -c "'
        "import json;"
        "d=json.load(open('semgrep-report.json'));"
        "r=d.get('results') or [];"
        "sev=lambda x:((x.get('extra') or {}).get('severity') or 'WARNING').upper();"
        "n=lambda k:sum(1 for x in r if sev(x)==k);"
        f"counted=sum(1 for x in r if sev(x) in ({counted_literal},));"
        f"print('{SENTINEL} tool=semgrep status=ok problems=%d errors=%d "
        "warnings=%d info=%d' % (counted, n('ERROR'), n('WARNING'), n('INFO')))"
        '"',
    ]


# ---------------------------------------------------------------------------
# SonarQube
# ---------------------------------------------------------------------------

def _sonar_commands(min_severity: str, project_key: str) -> List[str]:
    """Scan, then ask the server how many open issues that produced.

    Two steps because SonarQube is a server, not a linter: the scanner uploads
    an analysis and exits, and the findings live on the server afterwards. The
    count therefore has to be read back over the web API, against the same
    project key the scan just wrote to.

    ``sonar.qualitygate.wait=true`` is deliberately NOT set. It would make the
    scanner apply SONARQUBE's gate and fail the stage on it, which would put two
    gates in the path with different numbers and no way to tell which one
    stopped a merge. KubeSight's gate is the one this feature is about.
    """
    severities = ",".join(_SONAR_BY_FLOOR.get(min_severity, _SONAR_BY_FLOOR["medium"]))
    return [
        'if [ -z "${SONAR_HOST_URL:-}" ] || [ -z "${SONAR_TOKEN:-}" ]; then',
        '  echo "SONAR_HOST_URL and SONAR_TOKEN are not set - add them as CI secrets"',
        '  echo "on the service Settings tab and reference them from this stage."',
        f"  {_metric('sonar', 'error', problems=0)}",
        "  exit 1",
        "fi",
        "",
        f'PROJECT_KEY="${{SONAR_PROJECT_KEY:-{project_key}}}"',
        "sonar-scanner \\",
        '  -Dsonar.projectKey="$PROJECT_KEY" \\',
        '  -Dsonar.host.url="$SONAR_HOST_URL" \\',
        '  -Dsonar.token="$SONAR_TOKEN" \\',
        '  -Dsonar.sources=. \\',
        '  -Dsonar.scm.provider=git \\',
        "  -Dsonar.qualitygate.wait=false || true",
        "",
        "# The analysis is queued, not applied, when the scanner exits. Wait for",
        "# the server to finish processing it before counting, or the count is of",
        "# the PREVIOUS analysis - which would pass a merge on yesterday's code.",
        'if [ -f .scannerwork/report-task.txt ]; then',
        '  CE_URL=$(sed -n "s/^ceTaskUrl=//p" .scannerwork/report-task.txt)',
        "  ATTEMPT=0",
        '  while [ -n "$CE_URL" ] && [ "$ATTEMPT" -lt 60 ]; do',
        '    CE=$(curl -s -u "$SONAR_TOKEN:" "$CE_URL" || true)',
        '    case "$CE" in',
        '      *\\"status\\":\\"SUCCESS\\"*) break ;;',
        '      *\\"status\\":\\"FAILED\\"*|*\\"status\\":\\"CANCELED\\"*)',
        '        echo "SonarQube could not process the analysis."',
        f"        {_metric('sonar', 'error', problems=0)}",
        "        exit 1 ;;",
        "    esac",
        "    ATTEMPT=$((ATTEMPT + 1))",
        "    sleep 5",
        "  done",
        "fi",
        "",
        'ISSUES=$(curl -s -u "$SONAR_TOKEN:" \\',
        '  "$SONAR_HOST_URL/api/issues/search?componentKeys=$PROJECT_KEY'
        f'&resolved=false&severities={severities}&ps=1" || true)',
        'TOTAL=$(echo "$ISSUES" | sed -n "s/.*\\"total\\"[ ]*:[ ]*\\([0-9]*\\).*/\\\\1/p" | head -n 1)',
        'if [ -z "$TOTAL" ]; then',
        '  echo "SonarQube did not answer with an issue count:"',
        '  echo "$ISSUES" | head -c 500',
        f"  {_metric('sonar', 'error', problems=0)}",
        "  exit 1",
        "fi",
        f'echo "{SENTINEL} tool=sonar status=ok problems=$TOTAL severities={severities}"',
    ]


# ---------------------------------------------------------------------------
# OWASP Dependency-Check
# ---------------------------------------------------------------------------

def _dependency_check_commands(min_severity: str) -> List[str]:
    """Scan the dependency tree, count findings at or above the floor.

    Counted with grep rather than a JSON parser because the Dependency-Check
    image carries a JRE and shell utilities and no guarantee of node or python.
    It counts ``"severity" : "HIGH"`` occurrences in the report, which is the
    field the tool writes once per vulnerability.

    The NVD database lives in the build cache (``$DC_DATA_DIR``), so the first
    run on a cold cache downloads it and takes many minutes. That is a property
    of the tool, and the honest thing is to let it happen once rather than to
    disable the update and scan against an empty database.

    That database is usually SHARED by every service (``_shared/`` in the
    cache), and its embedded H2 file does not survive two processes updating
    it at once. So a scan takes a lock first: a directory, because ``mkdir`` is
    atomic on NFS where ``flock`` is not reliably, kept fresh by a heartbeat so
    a lock whose pod was killed goes stale after ten minutes instead of blocking
    every scan forever. Scans queue behind each other; with a warm database
    each one is a minute or two.
    """
    floors = _DC_BY_FLOOR.get(min_severity, _DC_BY_FLOOR["high"])
    pattern = "|".join(floors)
    return [
        'DATA_DIR="${DC_DATA_DIR:-/tmp/dependency-check-data}"',
        'mkdir -p "$DATA_DIR" reports',
        "",
        "# One scan at a time per NVD database - see the stage's docstring.",
        'DC_LOCK="$DATA_DIR/.kubesight-scan.lock"',
        "DC_WAITED=0",
        'until mkdir "$DC_LOCK" 2>/dev/null; do',
        '  if [ -n "$(find "$DC_LOCK" -maxdepth 0 -mmin +10 2>/dev/null)" ]; then',
        '    echo "Breaking a stale Dependency-Check lock ($(cat "$DC_LOCK/owner" 2>/dev/null))."',
        '    rm -rf "$DC_LOCK"',
        "    continue",
        "  fi",
        '  if [ $((DC_WAITED % 60)) -eq 0 ]; then',
        '    echo "Waiting for another Dependency-Check scan ($(cat "$DC_LOCK/owner" 2>/dev/null))' \
        ' to finish with the NVD database..."',
        "  fi",
        "  sleep 5",
        "  DC_WAITED=$((DC_WAITED + 5))",
        "done",
        'echo "${KUBESIGHT_SERVICE_SLUG:-?} build ${KUBESIGHT_BUILD_NUMBER:-?}" > "$DC_LOCK/owner"',
        '( while sleep 60; do touch "$DC_LOCK" 2>/dev/null || exit 0; done ) &',
        "DC_HEARTBEAT=$!",
        # `|| true`: the stage runs under set -e, and a heartbeat that already
        # exited must not fail the scan on its way out.
        'dc_unlock() { kill "$DC_HEARTBEAT" 2>/dev/null || true; rm -rf "$DC_LOCK"; }',
        "trap dc_unlock EXIT",
        "",
        'NVD_ARGS=""',
        'if [ -n "${NVD_API_KEY:-}" ]; then NVD_ARGS="--nvdApiKey $NVD_API_KEY"; fi',
        "",
        "# Exits 1 when it finds anything at or above --failOnCVSS, which is not",
        "# what should fail this stage. The report is what is read.",
        "/usr/share/dependency-check/bin/dependency-check.sh \\",
        '  --project "${KUBESIGHT_SERVICE_SLUG:-merge-check}" \\',
        "  --scan . \\",
        "  --format JSON \\",
        "  --out reports \\",
        '  --data "$DATA_DIR" \\',
        "  --failOnCVSS 11 \\",
        "  $NVD_ARGS || true",
        "dc_unlock",
        "trap - EXIT",
        "",
        "REPORT=reports/dependency-check-report.json",
        'if [ ! -s "$REPORT" ]; then',
        '  echo "Dependency-Check produced no report."',
        f"  {_metric('dependency_check', 'error', problems=0)}",
        "  exit 1",
        "fi",
        "",
        "count() {",
        '  grep -o "\\"severity\\"[^,]*\\"$1\\"" "$REPORT" 2>/dev/null | wc -l | tr -d " "',
        "}",
        "CRIT=$(count CRITICAL)",
        "HIGH=$(count HIGH)",
        "MED=$(count MEDIUM)",
        "LOW=$(count LOW)",
        f'COUNTED=$(grep -Eo "\\"severity\\"[^,]*\\"({pattern})\\"" "$REPORT" 2>/dev/null'
        ' | wc -l | tr -d " ")',
        f'echo "{SENTINEL} tool=dependency_check status=ok problems=$COUNTED'
        ' critical=$CRIT high=$HIGH medium=$MED low=$LOW"',
    ]


# ---------------------------------------------------------------------------
# Assembly
# ---------------------------------------------------------------------------

_TOOL_ENV_KEY = {
    "eslint": "node-22",
    "semgrep": "semgrep",
    "sonar": "sonar-scanner",
    "dependency_check": "dependency-check",
}

_TOOL_SECRETS = {
    "sonar": [
        {"name": "SONAR_HOST_URL", "envVar": "SONAR_HOST_URL"},
        {"name": "SONAR_TOKEN", "envVar": "SONAR_TOKEN"},
    ],
    "dependency_check": [{"name": "NVD_API_KEY", "envVar": "NVD_API_KEY"}],
}

_TOOL_ARTIFACTS = {
    "eslint": [{"path": "eslint-report.json", "type": "test-report", "name": "eslint"}],
    "semgrep": [
        {"path": "semgrep-report.json", "type": "scan-report", "name": "semgrep"}
    ],
    "dependency_check": [
        {
            "path": "reports/dependency-check-report.json",
            "type": "scan-report",
            "name": "dependency-check",
        }
    ],
}

_TOOL_TIMEOUTS = {
    "eslint": 1200,
    "semgrep": 1800,
    "sonar": 2400,
    "dependency_check": 3600,
}


def generated_commands(tool: str, gate: Dict[str, Any], *, service_slug: str = "") -> List[str]:
    """The default script for one tool — what "Reset to the default" restores."""
    if tool == "eslint":
        return _eslint_commands(bool(gate.get("eslintCountWarnings")))
    if tool == "semgrep":
        return _semgrep_commands(str(gate.get("semgrepMinSeverity") or "medium"))
    if tool == "sonar":
        return _sonar_commands(
            str(gate.get("sonarMinSeverity") or "medium"), service_slug or "merge-check"
        )
    if tool == "dependency_check":
        return _dependency_check_commands(str(gate.get("dependencyMinSeverity") or "high"))
    raise ValueError(f"Unknown merge check tool: {tool}")


def check_stage(
    tool: str,
    gate: Dict[str, Any],
    *,
    service_slug: str = "",
    known_secret_keys: Optional[set] = None,
    custom_commands: Optional[List[str]] = None,
) -> Dict[str, Any]:
    """One tool's stage, wired to report against ``gate``.

    The gate's severity floors are baked into the COMMANDS rather than passed as
    environment: what a tool counts is part of what the stage does, and a
    reviewer reading the stage should be able to see which severities it is
    looking at without cross-referencing a settings page.

    ``custom_commands`` replaces the generated script wholesale. Everything else
    about the stage — image, labels, secret references, artifacts, timeout — is
    still supplied here, so an edited script keeps the wiring it needs and the
    person editing only has to think about the commands.
    """
    if custom_commands:
        commands = list(custom_commands)
    elif tool == "eslint":
        commands = _eslint_commands(bool(gate.get("eslintCountWarnings")))
    elif tool in ("semgrep", "sonar", "dependency_check"):
        commands = generated_commands(tool, gate, service_slug=service_slug)
    else:
        raise ValueError(f"Unknown merge check tool: {tool}")

    environment = build_environments.resolve(_TOOL_ENV_KEY[tool]) or {}
    # Only reference secrets this service actually has. A secretRef to a key
    # that does not exist is refused by the pipeline validator, which would make
    # "enable merge checks" fail on a service that has not configured Sonar yet
    # — the stage itself already reports that case in words.
    refs = [
        ref
        for ref in _TOOL_SECRETS.get(tool, [])
        if known_secret_keys is None or ref["name"] in known_secret_keys
    ]
    return {
        "name": stage_name_for(tool),
        "stageType": "command",
        "runnerType": environment.get("runnerType") or "",
        "runnerLabels": list(environment.get("labels") or ["linux"]),
        "image": environment.get("image") or "",
        "commands": commands,
        "env": {
            STAGE_TOOL_ENV: tool,
            "KUBESIGHT_SERVICE_SLUG": service_slug or "",
        },
        "secretRefs": refs,
        "artifacts": list(_TOOL_ARTIFACTS.get(tool, [])),
        # Every check runs, even after one of them fails. A pull request whose
        # ESLint stage died should still come back with its dependency findings
        # — telling somebody about one problem at a time is how a two-minute
        # review becomes four round trips.
        "continueOnFailure": True,
        "timeoutSeconds": _TOOL_TIMEOUTS.get(tool, 1800),
        "enabled": True,
    }


def build_stages(
    tools: List[str],
    gate: Dict[str, Any],
    *,
    service_slug: str = "",
    known_secret_keys: Optional[set] = None,
    custom_commands: Optional[Dict[str, List[str]]] = None,
) -> List[Dict[str, Any]]:
    """Checkout plus one stage per selected tool, in canonical order."""
    ordered = [tool for tool in MERGE_CHECK_TOOLS if tool in set(tools or ())]
    stages: List[Dict[str, Any]] = [
        {
            "name": CHECKOUT_STAGE,
            "stageType": "checkout",
            "runnerLabels": ["linux"],
            "commands": [],
            "timeoutSeconds": 600,
        }
    ]
    overrides = custom_commands or {}
    stages.extend(
        check_stage(
            tool,
            gate,
            service_slug=service_slug,
            known_secret_keys=known_secret_keys,
            custom_commands=overrides.get(tool),
        )
        for tool in ordered
    )
    return stages


def unconfigured_environments(tools: List[str]) -> List[Dict[str, str]]:
    """Tools whose build image this installation has not pointed anywhere.

    Surfaced before the gate is switched on rather than discovered when the
    first pull request arrives and the pod cannot pull.
    """
    missing = []
    for tool in tools or ():
        key = _TOOL_ENV_KEY.get(tool)
        environment = build_environments.resolve(key) if key else None
        if environment and not environment["configured"]:
            missing.append({"tool": tool, "environment": key, "label": tool_label(tool)})
    return missing
