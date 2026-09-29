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
from .profiles import semgrep_rules

# The checkout every check runs against. Named the same as everywhere else in
# CI so the stage reads identically in the editor.
CHECKOUT_STAGE = "Checkout"

# JVM application types, and the build tool that knows their dependencies.
# Dependency-Check reads jars, not build.gradle or pom.xml, so for these the
# pipeline first asks Gradle/Maven to put the resolved jars in DEPS_DIR —
# without that step the scan reports zero and means nothing.
JAVA_TYPES = ("java_gradle", "java_maven", "java", "android")
_DEPS_TOOL = {
    "java_gradle": "gradle",
    "android": "gradle",
    "java_maven": "maven",
    "java": "maven",
}
_DEPS_ENV_KEY = {
    "java_gradle": "gradle-8-jdk11",
    "android": "android",
    "java_maven": "maven-3.9-jdk21",
    "java": "maven-3.9-jdk21",
}
DEPS_DIR = ".kubesight-deps"
DEPS_STAGE = "Resolve dependencies"
# Types whose dependency manifests Dependency-Check only reads with its
# "experimental" analyzers on: pip requirements, Pipfile, Poetry.
_DC_EXPERIMENTAL = ("python", "generic", "container")

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

    A project that has not set ESLint up (no dependency on it, no config) is
    reported as `skipped` rather than failing: that is a fact about the
    repository, not a tool that broke, and blocking every merge of a Node
    service that never adopted ESLint would make the gate useless there.

    Installed with the package manager the lockfile belongs to — ``npm ci``
    against a yarn.lock ignores the lock and installs different versions.
    """
    counted = "e+w" if count_warnings else "e"
    return [
        "if [ ! -f package.json ]; then",
        '  echo "No package.json here - ESLint has nothing to check."',
        f"  {_metric('eslint', 'skipped', problems=0)}",
        "  exit 0",
        "fi",
        "",
        "HAS_ESLINT=$(node -e \"const p=require('./package.json');"
        "const d=Object.assign({},p.dependencies,p.devDependencies);"
        "console.log(d.eslint||p.eslintConfig?'yes':'no')\")",
        'if [ "$HAS_ESLINT" != yes ] && ! ls eslint.config.* .eslintrc* >/dev/null 2>&1; then',
        '  echo "This project does not use ESLint (no dependency, no config) - skipped."',
        '  echo "Add eslint to devDependencies with a config to have it checked."',
        f"  {_metric('eslint', 'skipped', problems=0)}",
        "  exit 0",
        "fi",
        "",
        "INSTALL_EXIT=0",
        "if [ -f pnpm-lock.yaml ]; then",
        "  corepack enable >/dev/null 2>&1 || true",
        "  pnpm install --frozen-lockfile || INSTALL_EXIT=$?",
        "elif [ -f yarn.lock ]; then",
        "  corepack enable >/dev/null 2>&1 || true",
        "  yarn install --frozen-lockfile || yarn install --immutable || INSTALL_EXIT=$?",
        "elif [ -f package-lock.json ] || [ -f npm-shrinkwrap.json ]; then",
        "  npm ci --no-audit --no-fund || INSTALL_EXIT=$?",
        "else",
        "  npm install --no-audit --no-fund || INSTALL_EXIT=$?",
        "fi",
        'if [ "$INSTALL_EXIT" != 0 ]; then',
        '  echo "Installing the dependencies failed (exit $INSTALL_EXIT), so ESLint cannot run."',
        f"  {_metric('eslint', 'error', problems=0)}",
        "  exit 1",
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

def _semgrep_commands(min_severity: str, app_type: str = "") -> List[str]:
    """Scan the checkout, count the findings, print the number.

    One step, unlike SonarQube: Semgrep reads the working tree and writes a
    report, so there is no server to upload to, no analysis to wait for, and no
    web API to ask afterwards. That is the entire difference between the two as
    far as this gate is concerned — and it is why a site with no SonarQube can
    pick this and get the same verdict.

    The default rules follow the application type: ``p/default`` plus the
    language's own pack (``p/java`` for a Gradle service, ``p/javascript`` and
    ``p/typescript`` for Node). See ``profiles.semgrep_rules``.

    ``--metrics=off`` because a merge check must not phone home about somebody's
    private repository, and ``SEMGREP_RULES`` so an installation that wants no
    network at all can point it at rules committed in the repository instead of
    the hosted registry.
    """
    counted = _SEMGREP_BY_FLOOR.get(min_severity, _SEMGREP_BY_FLOOR["medium"])
    counted_literal = ",".join(f"'{level}'" for level in counted)
    return [
        '# Rules: registry packs for this application type by default. Set',
        '# SEMGREP_RULES to a path inside the repository (e.g. .semgrep/) to run',
        '# with no network at all. Several rule sets are separated by spaces.',
        f'DEFAULT_RULES="{semgrep_rules(app_type)}"',
        "# Rules committed to the repository win over the registry: they work",
        "# offline and are the ones the team actually agreed to.",
        "if [ -f .semgrep.yml ]; then DEFAULT_RULES=.semgrep.yml;"
        " elif [ -d .semgrep ]; then DEFAULT_RULES=.semgrep; fi",
        'RULES="${SEMGREP_RULES:-$DEFAULT_RULES}"',
        'echo "Scanning with rules: $RULES"',
        'CONFIG_ARGS=""',
        'for R in $RULES; do CONFIG_ARGS="$CONFIG_ARGS --config $R"; done',
        "",
        "# Exit 1 means findings - the quality gate's business. 2 and above means",
        "# Semgrep itself failed. Its own output is kept either way: a failure",
        "# with no explanation is what this stage used to print.",
        "SEMGREP_EXIT=0",
        "semgrep scan $CONFIG_ARGS \\",
        "  --json \\",
        "  --output semgrep-report.json \\",
        "  --metrics=off \\",
        "  --disable-version-check \\",
        # The target is named on purpose. The official image sets
        # SEMGREP_IN_DOCKER, and with no target Semgrep then insists the code
        # is mounted at /src — empty in a build pod — and dies with
        # "Detected Docker environment without a code volume".
        "  . \\",
        "  2> semgrep-stderr.log || SEMGREP_EXIT=$?",
        'echo "Semgrep exited with $SEMGREP_EXIT."',
        "",
        "if [ ! -s semgrep-report.json ]; then",
        '  echo "Semgrep produced no report. Its output:"',
        '  echo "------------------------------------------------------------"',
        "  tail -n 40 semgrep-stderr.log 2>/dev/null || true",
        '  echo "------------------------------------------------------------"',
        "  if [ \"$SEMGREP_EXIT\" = 127 ]; then",
        '    echo "Hint: semgrep is not installed in this image - check the semgrep build environment."',
        "  elif grep -q 'without a code volume' semgrep-stderr.log 2>/dev/null; then",
        '    echo "Hint: this stage runs an old script that does not name the folder to scan."',
        '    echo "Save the Merge Checks tab (or open a new pull request) to regenerate it."',
        # Specific network failures only: a bare "registry" or "resolve" also
        # matches Semgrep's own file names (config_resolver.py) in a traceback.
        "  elif grep -qiE 'HTTPSConnectionPool|ProxyError|ConnectionError|Name or service not known"
        "|Temporary failure in name resolution|timed out|CERTIFICATE_VERIFY_FAILED|SSLError'"
        " semgrep-stderr.log 2>/dev/null; then",
        '    echo "Hint: the rules could not be downloaded from the Semgrep registry ($RULES)."',
        '    echo "Allow the runner to reach https://semgrep.dev, or commit rules to the"',
        '    echo "repository as .semgrep.yml or .semgrep/ - they are used automatically."',
        "  elif grep -qi 'permission denied' semgrep-stderr.log 2>/dev/null; then",
        '    echo "Hint: Semgrep could not write somewhere (HOME=$HOME, cache=${SEMGREP_CACHE_DIR:-unset})."',
        "  fi",
        f"  {_metric('semgrep', 'error', problems=0)}",
        "  exit 1",
        "fi",
        "",
        "# A report can still carry errors (a rule that failed to parse, a file it",
        "# could not read). Shown, not counted - they are not findings.",
        'python3 -c "'
        "import json;"
        "e=json.load(open('semgrep-report.json')).get('errors') or [];"
        "[print('Semgrep warning:', (x.get('message') or str(x))[:300]) for x in e[:10]]"
        '" || true',
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

def _sonar_commands(min_severity: str, project_key: str, app_type: str = "") -> List[str]:
    """Scan, then ask the server how many open issues that produced.

    Two steps because SonarQube is a server, not a linter: the scanner uploads
    an analysis and exits, and the findings live on the server afterwards. The
    count therefore has to be read back over the web API, against the same
    project key the scan just wrote to.

    ``sonar.qualitygate.wait=true`` is deliberately NOT set. It would make the
    scanner apply SONARQUBE's gate and fail the stage on it, which would put two
    gates in the path with different numbers and no way to tell which one
    stopped a merge. KubeSight's gate is the one this feature is about.

    Java and Android sources are refused by the scanner unless it is told where
    compiled classes are (``sonar.java.binaries``). A merge check does not
    compile, so it points at any classes already present and otherwise at an
    empty directory - the analysis then runs on source alone. Jars fetched by
    the dependency stage are passed as libraries so types resolve.

    If the scanner fails, or the server has not finished processing within the
    wait, the stage reports an error rather than counting: the issue count on
    the server would then belong to the PREVIOUS analysis.
    """
    java = app_type in JAVA_TYPES
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
        'JAVA_ARGS=""',
        *(
            [
                "# Compiled classes if a previous step left any, else an empty dir.",
                'BIN=$(find . -type d \\( -path "*/build/classes" -o -path "*/target/classes" \\)'
                ' -not -path "./.git/*" 2>/dev/null | paste -sd, -)',
                'if [ -z "$BIN" ]; then mkdir -p .kubesight-no-classes; BIN=.kubesight-no-classes; fi',
                'JAVA_ARGS="-Dsonar.java.binaries=$BIN"',
                f'if ls {DEPS_DIR}/*.jar >/dev/null 2>&1; then',
                f'  JAVA_ARGS="$JAVA_ARGS -Dsonar.java.libraries={DEPS_DIR}/*.jar"',
                "fi",
            ]
            if java
            else []
        ),
        "rm -f .scannerwork/report-task.txt",
        "SCANNER_EXIT=0",
        "sonar-scanner \\",
        '  -Dsonar.projectKey="$PROJECT_KEY" \\',
        '  -Dsonar.host.url="$SONAR_HOST_URL" \\',
        '  -Dsonar.token="$SONAR_TOKEN" \\',
        '  -Dsonar.sources=. \\',
        '  -Dsonar.scm.provider=git \\',
        f"  -Dsonar.exclusions={DEPS_DIR}/** \\",
        "  $JAVA_ARGS \\",
        "  -Dsonar.qualitygate.wait=false || SCANNER_EXIT=$?",
        "",
        "if [ ! -f .scannerwork/report-task.txt ]; then",
        '  echo "The SonarQube scanner did not upload an analysis (exit $SCANNER_EXIT)."',
        f"  {_metric('sonar', 'error', problems=0)}",
        "  exit 1",
        "fi",
        "",
        "# The analysis is queued, not applied, when the scanner exits. Wait for",
        "# the server to finish processing it before counting, or the count is of",
        "# the PREVIOUS analysis - which would pass a merge on yesterday's code.",
        'CE_URL=$(sed -n "s/^ceTaskUrl=//p" .scannerwork/report-task.txt)',
        "ATTEMPT=0",
        "PROCESSED=no",
        'while [ -n "$CE_URL" ] && [ "$ATTEMPT" -lt 120 ]; do',
        '  CE=$(curl -s -u "$SONAR_TOKEN:" "$CE_URL" || true)',
        '  case "$CE" in',
        '    *\\"status\\":\\"SUCCESS\\"*) PROCESSED=yes; break ;;',
        '    *\\"status\\":\\"FAILED\\"*|*\\"status\\":\\"CANCELED\\"*)',
        '      echo "SonarQube could not process the analysis."',
        f"      {_metric('sonar', 'error', problems=0)}",
        "      exit 1 ;;",
        "  esac",
        "  ATTEMPT=$((ATTEMPT + 1))",
        "  sleep 5",
        "done",
        'if [ "$PROCESSED" != yes ]; then',
        '  echo "SonarQube had not finished processing the analysis after 10 minutes."',
        f"  {_metric('sonar', 'error', problems=0)}",
        "  exit 1",
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

def _dependency_check_commands(min_severity: str, app_type: str = "") -> List[str]:
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
    extra = [
        '  --exclude "**/node_modules/**" \\',
        '  --exclude "**/.git/**" \\',
    ]
    if app_type in _DC_EXPERIMENTAL:
        extra.append("  --enableExperimental \\")
    java_guard = (
        [
            f'if [ -z "$(find {DEPS_DIR} -type f \\( -name "*.jar" -o -name "*.aar" \\)'
            ' 2>/dev/null | head -n 1)" ]; then',
            f'  echo "Warning: no dependency jars in {DEPS_DIR} - the \'{DEPS_STAGE}\' stage'
            ' found none, so only jars committed to the repository are scanned."',
            "fi",
        ]
        if app_type in JAVA_TYPES
        else []
    )
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
        *java_guard,
        'NVD_ARGS=""',
        'if [ -n "${NVD_API_KEY:-}" ]; then',
        '  NVD_ARGS="--nvdApiKey $NVD_API_KEY"',
        "else",
        "  # Without a key NVD allows a handful of requests a minute; the first",
        "  # full download needs thousands. Slow down rather than be refused.",
        '  NVD_ARGS="--nvdApiDelay 8000"',
        '  echo "No NVD_API_KEY - the NVD download will be slow and may be refused."',
        "fi",
        "# A mirror of the NVD feed (e.g. an internal vulnz/Nexus mirror) for a",
        "# cluster that cannot reach services.nvd.nist.gov.",
        'if [ -n "${NVD_DATAFEED_URL:-}" ]; then',
        '  NVD_ARGS="--nvdDatafeed $NVD_DATAFEED_URL"',
        '  echo "Using the NVD mirror at $NVD_DATAFEED_URL"',
        "fi",
        "",
        "# Exits 1 when it finds anything at or above --failOnCVSS, which is not",
        "# what should fail this stage. The report is what is read; the output is",
        "# kept so a failure can be explained below.",
        "( /usr/share/dependency-check/bin/dependency-check.sh \\",
        '  --project "${KUBESIGHT_SERVICE_SLUG:-merge-check}" \\',
        "  --scan . \\",
        "  --format JSON \\",
        "  --out reports \\",
        '  --data "$DATA_DIR" \\',
        "  --failOnCVSS 11 \\",
        *extra,
        "  $NVD_ARGS 2>&1 || true ) | tee dependency-check.log",
        "dc_unlock",
        "trap - EXIT",
        "",
        "REPORT=reports/dependency-check-report.json",
        'if [ ! -s "$REPORT" ]; then',
        '  echo "Dependency-Check produced no report."',
        "  if grep -qE 'No documents exist|Error updating the NVD' dependency-check.log; then",
        '    echo "Cause: the vulnerability database in $DATA_DIR is empty and could not be downloaded."',
        '    if grep -qE "40[34]|429|rate limit" dependency-check.log; then',
        '      echo "NVD refused the requests (403/429)."',
        "    elif grep -qiE 'UnknownHost|Connect(ion)? (refused|timed out)|No route|SSL|PKIX' dependency-check.log; then",
        '      echo "The runner cannot reach services.nvd.nist.gov (network, proxy or TLS)."',
        "    fi",
        '    if [ -z "${NVD_API_KEY:-}" ]; then',
        '      echo "Fix: request a free key at https://nvd.nist.gov/developers/request-an-api-key"',
        '      echo "and add it as a CI secret named NVD_API_KEY on the service Settings tab."',
        "    fi",
        '    echo "No internet from the runner? Set NVD_DATAFEED_URL to an internal NVD mirror."',
        '    echo "The first successful run fills the shared cache; later scans take a minute or two."',
        "  fi",
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
    "dependency_check": [
        {"name": "NVD_API_KEY", "envVar": "NVD_API_KEY"},
        {"name": "NVD_DATAFEED_URL", "envVar": "NVD_DATAFEED_URL"},
    ],
}

def tool_secret_refs(tool: str) -> List[Dict[str, str]]:
    """Every secret this tool's stage can use, whether or not it exists yet."""
    return [dict(ref) for ref in _TOOL_SECRETS.get(tool, [])]


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


def _gradle_deps_commands() -> List[str]:
    """Copy every resolvable runtime classpath into DEPS_DIR, via an init script.

    An init script rather than a task in the project: the repository is not
    ours to edit, and an init script applies to every project of a multi-module
    build. Android has no ``runtimeClasspath``; its variants do, hence the
    release/debug names. A configuration that fails to resolve is reported and
    skipped so one odd module does not empty the whole scan.
    """
    return [
        "if [ -x ./gradlew ]; then GRADLE=./gradlew;",
        "elif [ -f build.gradle ] || [ -f build.gradle.kts ] || [ -f settings.gradle ]"
        " || [ -f settings.gradle.kts ]; then GRADLE=gradle;",
        'else echo "No Gradle build here - nothing to resolve."; exit 0; fi',
        f'mkdir -p "{DEPS_DIR}"',
        "cat > /tmp/kubesight-deps.gradle <<'KUBESIGHT_EOF'",
        "allprojects { p ->",
        "  p.tasks.register('kubesightCopyDeps') { t ->",
        "    t.doLast {",
        "      ['runtimeClasspath', 'releaseRuntimeClasspath', 'debugRuntimeClasspath'].each { n ->",
        "        def c = p.configurations.findByName(n)",
        "        if (c != null && c.canBeResolved) {",
        "          try {",
        f"            p.copy {{ from c; into new File(p.rootDir, '{DEPS_DIR}') }}",
        "          } catch (Exception e) {",
        "            println \"kubesight: could not resolve ${p.path}:${n}: ${e.message}\"",
        "          }",
        "        }",
        "      }",
        "    }",
        "  }",
        "}",
        "KUBESIGHT_EOF",
        "$GRADLE -I /tmp/kubesight-deps.gradle kubesightCopyDeps \\",
        "  --no-daemon --no-configuration-cache --continue -q \\",
        '  || echo "Gradle could not resolve every dependency - scanning what it did."',
    ]


def _maven_deps_commands() -> List[str]:
    """``dependency:copy-dependencies`` for the whole reactor into DEPS_DIR.

    Tried without compiling first. A multi-module build whose modules depend on
    each other cannot copy a sibling that was never packaged, so the second
    attempt packages the reactor (tests skipped) — slower, only paid when
    needed.
    """
    return [
        "if [ -x ./mvnw ]; then MVN=./mvnw;",
        "elif [ -f pom.xml ]; then MVN=mvn;",
        'else echo "No Maven build here - nothing to resolve."; exit 0; fi',
        f'OUT="$PWD/{DEPS_DIR}"',
        'mkdir -p "$OUT"',
        "copy_deps() {",
        '  $MVN -B -q "$@" dependency:copy-dependencies \\',
        '    "-DoutputDirectory=$OUT" -DincludeScope=runtime',
        "}",
        "copy_deps || {",
        '  echo "Retrying after packaging the modules - a module depends on a sibling"',
        '  echo "that has not been built yet (the errors above are expected then)."',
        "  copy_deps -Dmaven.test.skip=true package",
        '} || echo "Maven could not resolve every dependency - scanning what it did."',
    ]


def dependency_stage(app_type: str, image: str = "") -> Optional[Dict[str, Any]]:
    """The stage that gives Dependency-Check (and SonarQube) jars to read.

    Only for JVM types. Never fails the build: missing jars are reported by the
    Dependency-Check stage in words, and a merge must not be blocked because a
    helper step, rather than a check, had trouble.
    """
    tool = _DEPS_TOOL.get(app_type)
    if tool is None:
        return None
    environment = build_environments.resolve(_DEPS_ENV_KEY[app_type]) or {}
    body = _gradle_deps_commands() if tool == "gradle" else _maven_deps_commands()
    return {
        "name": DEPS_STAGE,
        "stageType": "command",
        "runnerType": environment.get("runnerType") or "",
        "runnerLabels": list(environment.get("labels") or ["linux"]),
        "image": image or environment.get("image") or "",
        "commands": [
            *body,
            f'COUNT=$(find "{DEPS_DIR}" -type f \\( -name "*.jar" -o -name "*.aar" \\)'
            ' 2>/dev/null | wc -l | tr -d " ")',
            f'echo "$COUNT dependency files in {DEPS_DIR}."',
            "exit 0",
        ],
        "env": {},
        "secretRefs": [],
        "artifacts": [],
        "continueOnFailure": True,
        "timeoutSeconds": 1800,
        "enabled": True,
    }


def generated_commands(
    tool: str, gate: Dict[str, Any], *, service_slug: str = "", app_type: str = ""
) -> List[str]:
    """The default script for one tool — what "Reset to the default" restores."""
    if tool == "eslint":
        return _eslint_commands(bool(gate.get("eslintCountWarnings")))
    if tool == "semgrep":
        return _semgrep_commands(str(gate.get("semgrepMinSeverity") or "medium"), app_type)
    if tool == "sonar":
        return _sonar_commands(
            str(gate.get("sonarMinSeverity") or "medium"),
            service_slug or "merge-check",
            app_type,
        )
    if tool == "dependency_check":
        return _dependency_check_commands(
            str(gate.get("dependencyMinSeverity") or "high"), app_type
        )
    raise ValueError(f"Unknown merge check tool: {tool}")


def check_stage(
    tool: str,
    gate: Dict[str, Any],
    *,
    service_slug: str = "",
    known_secret_keys: Optional[set] = None,
    custom_commands: Optional[List[str]] = None,
    app_type: str = "",
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
        commands = generated_commands(tool, gate, service_slug=service_slug, app_type=app_type)
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
    app_type: str = "",
    deps_image: str = "",
) -> List[Dict[str, Any]]:
    """Checkout, the JVM dependency step when a check needs jars, then one
    stage per selected tool, in canonical order.

    ``deps_image`` is the image the service's own build uses for Gradle/Maven,
    when known: resolving with the JDK and build tool the project is built with
    avoids "this project needs Java 17" failures from a generic image.
    """
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
    if {"dependency_check", "sonar"} & set(ordered):
        deps = dependency_stage(app_type, deps_image)
        if deps is not None:
            stages.append(deps)
    overrides = custom_commands or {}
    stages.extend(
        check_stage(
            tool,
            gate,
            service_slug=service_slug,
            known_secret_keys=known_secret_keys,
            custom_commands=overrides.get(tool),
            app_type=app_type,
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
