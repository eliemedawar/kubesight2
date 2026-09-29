"""Linters for every language — ESLint's counterparts, in one table.

ESLint checks JavaScript for bugs and bad patterns with the rules the project
agreed to. Each language has a tool that does the same job, and this module is
where they live:

    Python         Ruff          (the project's pyproject/ruff.toml, else bug-only rules)
    Java           PMD           (the project's ruleset, else PMD's "quickstart")
    Kotlin         detekt        (the project's detekt.yml on top of the defaults)
    Swift          SwiftLint     (the project's .swiftlint.yml, else the defaults)
    Dart/Flutter   dart analyze  (the project's analysis_options.yaml)
    Dockerfiles    Hadolint      (the project's .hadolint.yaml)
    Shell scripts  ShellCheck    (the project's .shellcheckrc)

Every one follows the conventions of ``stages.py``: it never fails the stage on
findings, prints exactly one ``##kubesight-metric`` line on every path, counts
in its own image with what that image ships, and reports ``skipped`` — not
zero, not an error — when the repository has nothing in its language. That
last one is what lets a Python service carry Hadolint without a Dockerfile
costing anything but a line in the log.

Unlike ESLint, these run even when the project has no config of its own: each
tool's defaults are a sensible, conservative starting point, where ESLint has
no defaults to speak of.

What each counts as a problem is stated per tool (``counts``) and shown in the
panel next to its limit, because "3 problems" means nothing until you know
whether warnings were in it.
"""

from __future__ import annotations

from typing import Callable, Dict, List

from .metrics import SENTINEL

# Pinned so a merge check gives the same answer next month as today. A new
# version is a deliberate edit here, not something that happens on its own.
RUFF_VERSION = "0.16.9"
PMD_VERSION = "7.28.0"
DETEKT_VERSION = "1.23.8"

# Directories no linter should walk: dependencies, VCS internals, build output
# and the jars the dependency stage fetched.
_PRUNE = (
    "\\( -name .git -o -name node_modules -o -name .kubesight-deps"
    " -o -name build -o -name target -o -name dist -o -name .venv"
    " -o -name venv -o -name vendor -o -name Pods -o -name .dart_tool \\) -prune"
)


def _metric(tool: str, status: str, **pairs) -> str:
    parts = [f"tool={tool}", f"status={status}"]
    parts.extend(f"{key}={value}" for key, value in pairs.items())
    return f'echo "{SENTINEL} {" ".join(parts)}"'


def _find(names: str) -> str:
    """A find over the checkout, skipping dependency and build directories.

    ``names`` is the -name expression, e.g. ``-name '*.py'``.
    """
    return f"find . {_PRUNE} -o -type f \\( {names} \\) -print"


def _skip_unless_files(tool: str, names: str, what: str) -> List[str]:
    return [
        f"if [ -z \"$({_find(names)} | head -n 1)\" ]; then",
        f'  echo "No {what} in this repository - nothing for {tool} to check."',
        f"  {_metric(tool, 'skipped', problems=0)}",
        "  exit 0",
        "fi",
    ]


# A tool fetched once into the shared cache and reused by every build: PMD and
# detekt ship as Java archives, not images, and downloading them on every pull
# request would make the check as slow as the network. Downloaded to a temp
# name and renamed, so two builds fetching at once never see half a file.
_FETCH = [
    'TOOLS_DIR="${KUBESIGHT_SHARED_CACHE_DIR:-${KUBESIGHT_CACHE_DIR:-/tmp}}/tools"',
    'mkdir -p "$TOOLS_DIR"',
    "fetch() {",
    '  [ -s "$2" ] && return 0',
    '  echo "Downloading $1"',
    '  if command -v curl >/dev/null 2>&1; then curl -fsSL -o "$2.part.$$" "$1";',
    '  else wget -q -O "$2.part.$$" "$1"; fi && mv -f "$2.part.$$" "$2"',
    "}",
]


# ---------------------------------------------------------------------------
# Python — Ruff
# ---------------------------------------------------------------------------

def ruff_commands() -> List[str]:
    """``ruff check`` with the project's config, else the bug-only rules.

    Without a project config the rules are pinned to ``E4,E7,E9,F`` (pyflakes
    and pycodestyle's error codes): undefined names, unused imports, syntax
    errors — bugs, not style, which is why every finding counts. Pinned rather
    than left to Ruff's defaults because those grew to include modernisation
    rules (UP, I, BLE...) in 0.16: on this very repository that is 7,800
    findings instead of 221, and a gate nobody can pass is a gate nobody uses.
    """
    return [
        *_skip_unless_files("ruff", "-name '*.py'", "Python files"),
        f'pip install --quiet --disable-pip-version-check "ruff=={RUFF_VERSION}" || {{',
        '  echo "Could not install Ruff from the package index."',
        f"  {_metric('ruff', 'error', problems=0)}",
        "  exit 1",
        "}",
        'SELECT="--select E4,E7,E9,F"',
        "if [ -f ruff.toml ] || [ -f .ruff.toml ] || grep -qs '^\\[tool\\.ruff' pyproject.toml; then",
        '  SELECT=""',
        '  echo "Rules: the project\'s Ruff config"',
        "else",
        '  echo "Rules: E4,E7,E9,F (no Ruff config in the repository - bugs only)"',
        "fi",
        "ruff check . $SELECT --output-format json --output-file ruff-report.json --exit-zero",
        "if [ ! -f ruff-report.json ]; then",
        '  echo "Ruff produced no report."',
        f"  {_metric('ruff', 'error', problems=0)}",
        "  exit 1",
        "fi",
        'python3 -c "'
        "import json,collections;"
        "r=json.load(open('ruff-report.json'));"
        "top=collections.Counter(x.get('code') or '?' for x in r).most_common(5);"
        "[print('  %s x%d' % t) for t in top];"
        f"print('{SENTINEL} tool=ruff status=ok problems=%d files=%d'"
        " % (len(r), len({x.get('filename') for x in r})))"
        '"',
    ]


# ---------------------------------------------------------------------------
# Java — PMD
# ---------------------------------------------------------------------------

def pmd_commands() -> List[str]:
    """PMD over the Java sources with the project's ruleset, else "quickstart".

    quickstart is PMD's own "rules that are almost always right" set — the
    Java equivalent of ESLint's recommended config. A project with its own
    ruleset puts it at one of the paths below or points PMD_RULESET at it.
    Run through ``java -cp`` rather than PMD's launcher, which needs bash and
    the JDK images here are Alpine.
    """
    home = f"$TOOLS_DIR/pmd-bin-{PMD_VERSION}"
    url = (
        "https://github.com/pmd/pmd/releases/download/"
        f"pmd_releases%2F{PMD_VERSION}/pmd-dist-{PMD_VERSION}-bin.zip"
    )
    return [
        *_skip_unless_files("pmd", "-name '*.java'", "Java files"),
        *_FETCH,
        f'if [ ! -d "{home}/lib" ]; then',
        f'  fetch "{url}" "$TOOLS_DIR/pmd-{PMD_VERSION}.zip" || {{',
        '    echo "Could not download PMD."',
        f"    {_metric('pmd', 'error', problems=0)}",
        "    exit 1",
        "  }",
        '  UNPACK="$TOOLS_DIR/.pmd-unpack.$$"',
        f'  mkdir -p "$UNPACK" && unzip -q -o "$TOOLS_DIR/pmd-{PMD_VERSION}.zip" -d "$UNPACK"',
        f'  mv "$UNPACK/pmd-bin-{PMD_VERSION}" "{home}" 2>/dev/null || true',
        '  rm -rf "$UNPACK"',
        "fi",
        'RULESET="${PMD_RULESET:-}"',
        'for f in pmd-ruleset.xml .pmd/ruleset.xml config/pmd/ruleset.xml config/pmd/pmd.xml; do',
        '  if [ -z "$RULESET" ] && [ -f "$f" ]; then RULESET="$f"; fi',
        "done",
        'RULESET="${RULESET:-rulesets/java/quickstart.xml}"',
        'echo "Rules: $RULESET"',
        f'java -cp "{home}/lib/*" net.sourceforge.pmd.cli.PmdCli check \\',
        '  --dir . --rulesets "$RULESET" --format text --report-file pmd-report.txt \\',
        "  --no-progress --no-cache --no-fail-on-violation --no-fail-on-error \\",
        "  --exclude .kubesight-deps || PMD_EXIT=$?",
        "if [ ! -f pmd-report.txt ]; then",
        '  echo "PMD produced no report (exit ${PMD_EXIT:-?})."',
        f"  {_metric('pmd', 'error', problems=0)}",
        "  exit 1",
        "fi",
        "# One line per violation: file:line:\\tRule:\\tmessage",
        "N=$(grep -c . pmd-report.txt || true)",
        "head -n 20 pmd-report.txt",
        "FILES=$(cut -d: -f1 pmd-report.txt | sort -u | grep -c . || true)",
        f'echo "{SENTINEL} tool=pmd status=ok problems=$N files=$FILES"',
    ]


# ---------------------------------------------------------------------------
# Kotlin — detekt
# ---------------------------------------------------------------------------

def detekt_commands() -> List[str]:
    """detekt over the Kotlin sources, the project's detekt.yml on top of the
    defaults when it has one."""
    jar = f"$TOOLS_DIR/detekt-cli-{DETEKT_VERSION}-all.jar"
    url = (
        "https://github.com/detekt/detekt/releases/download/"
        f"v{DETEKT_VERSION}/detekt-cli-{DETEKT_VERSION}-all.jar"
    )
    return [
        *_skip_unless_files("detekt", "-name '*.kt'", "Kotlin files"),
        *_FETCH,
        f'fetch "{url}" "{jar}" || {{',
        '  echo "Could not download detekt."',
        f"  {_metric('detekt', 'error', problems=0)}",
        "  exit 1",
        "}",
        'CONFIG_ARGS=""',
        "for f in detekt.yml config/detekt/detekt.yml .detekt.yml; do",
        '  if [ -z "$CONFIG_ARGS" ] && [ -f "$f" ]; then',
        '    CONFIG_ARGS="--config $f --build-upon-default-config"',
        '    echo "Config: $f (on top of the defaults)"',
        "  fi",
        "done",
        "rm -f detekt-report.txt",
        "# Exits 2 when it finds more than its own maxIssues - the gate decides.",
        f'java -jar "{jar}" --input . $CONFIG_ARGS \\',
        "  --excludes '**/build/**,**/.kubesight-deps/**,**/node_modules/**' \\",
        "  --report txt:detekt-report.txt || DETEKT_EXIT=$?",
        "if [ ! -f detekt-report.txt ]; then",
        '  echo "detekt produced no report (exit ${DETEKT_EXIT:-?})."',
        f"  {_metric('detekt', 'error', problems=0)}",
        "  exit 1",
        "fi",
        "N=$(grep -c . detekt-report.txt || true)",
        "head -n 20 detekt-report.txt",
        f'echo "{SENTINEL} tool=detekt status=ok problems=$N"',
    ]


# ---------------------------------------------------------------------------
# Swift — SwiftLint
# ---------------------------------------------------------------------------

def swiftlint_commands() -> List[str]:
    """SwiftLint; counts errors. Its warnings are mostly style (line length,
    naming), the same reason ESLint warnings are not counted by default."""
    return [
        *_skip_unless_files("swiftlint", "-name '*.swift'", "Swift files"),
        "swiftlint lint --reporter json --quiet > swiftlint-report.json || SL_EXIT=$?",
        "if [ ! -s swiftlint-report.json ]; then",
        '  echo "SwiftLint produced no report (exit ${SL_EXIT:-?})."',
        f"  {_metric('swiftlint', 'error', problems=0)}",
        "  exit 1",
        "fi",
        "E=$(grep -oE '\"severity\" *: *\"Error\"' swiftlint-report.json | wc -l | tr -d ' ')",
        "W=$(grep -oE '\"severity\" *: *\"Warning\"' swiftlint-report.json | wc -l | tr -d ' ')",
        f'echo "{SENTINEL} tool=swiftlint status=ok problems=$E errors=$E warnings=$W"',
    ]


# ---------------------------------------------------------------------------
# Dart / Flutter — dart analyze
# ---------------------------------------------------------------------------

def dart_analyze_commands() -> List[str]:
    """``dart analyze`` after fetching packages; counts errors and warnings.

    Packages first because an unresolved import is itself an error, and every
    file would report one. Flutter projects need ``flutter pub get`` — plain
    ``dart pub get`` cannot resolve the Flutter SDK packages.
    """
    return [
        "if [ ! -f pubspec.yaml ]; then",
        '  echo "No pubspec.yaml here - nothing for dart analyze to check."',
        f"  {_metric('dart_analyze', 'skipped', problems=0)}",
        "  exit 0",
        "fi",
        "# The SDK in the image is owned by another user; git refuses it otherwise.",
        "git config --global --add safe.directory '*' 2>/dev/null || true",
        "if grep -q 'sdk: *flutter' pubspec.yaml; then PUB='flutter pub get'; else PUB='dart pub get'; fi",
        "$PUB || {",
        '  echo "Fetching the packages failed, so the analysis would only report missing imports."',
        f"  {_metric('dart_analyze', 'error', problems=0)}",
        "  exit 1",
        "}",
        "dart analyze --format=machine . > dart-analyze.txt 2>&1 || true",
        "E=$(grep -c '^ERROR|' dart-analyze.txt || true)",
        "W=$(grep -c '^WARNING|' dart-analyze.txt || true)",
        "I=$(grep -c '^INFO|' dart-analyze.txt || true)",
        "grep -E '^(ERROR|WARNING)\\|' dart-analyze.txt | head -n 20 || true",
        f'echo "{SENTINEL} tool=dart_analyze status=ok problems=$((E + W))'
        ' errors=$E warnings=$W info=$I"',
    ]


# ---------------------------------------------------------------------------
# Dockerfiles — Hadolint
# ---------------------------------------------------------------------------

def hadolint_commands() -> List[str]:
    """Hadolint over every Dockerfile; counts errors and warnings (info and
    style are advice). Reads .hadolint.yaml from the repository root."""
    names = "-name Dockerfile -o -name 'Dockerfile.*' -o -name '*.Dockerfile' -o -name '*.dockerfile'"
    return [
        *_skip_unless_files("hadolint", names, "Dockerfiles"),
        f"{_find(names)} > .kubesight-dockerfiles",
        'echo "Checking $(wc -l < .kubesight-dockerfiles | tr -d " ") Dockerfile(s)."',
        "tr '\\n' '\\0' < .kubesight-dockerfiles | xargs -0 hadolint --no-fail --no-color -f json"
        " > hadolint-report.json || HL_EXIT=$?",
        "if [ ! -f hadolint-report.json ]; then",
        '  echo "Hadolint produced no report (exit ${HL_EXIT:-?})."',
        f"  {_metric('hadolint', 'error', problems=0)}",
        "  exit 1",
        "fi",
        "E=$(grep -oE '\"level\" *: *\"error\"' hadolint-report.json | wc -l | tr -d ' ')",
        "W=$(grep -oE '\"level\" *: *\"warning\"' hadolint-report.json | wc -l | tr -d ' ')",
        # Human-readable too: the JSON is one line and unreadable in a log.
        "tr '\\n' '\\0' < .kubesight-dockerfiles | xargs -0 hadolint --no-fail --no-color"
        " -t warning 2>/dev/null | head -n 20 || true",
        f'echo "{SENTINEL} tool=hadolint status=ok problems=$((E + W)) errors=$E warnings=$W"',
    ]


# ---------------------------------------------------------------------------
# Shell scripts — ShellCheck
# ---------------------------------------------------------------------------

def shellcheck_commands() -> List[str]:
    """ShellCheck over every *.sh; counts errors and warnings (info and style
    are advice). Reads .shellcheckrc from the repository."""
    names = "-name '*.sh' -o -name '*.bash'"
    return [
        *_skip_unless_files("shellcheck", names, "shell scripts"),
        f"{_find(names)} > .kubesight-scripts",
        'echo "Checking $(wc -l < .kubesight-scripts | tr -d " ") script(s)."',
        "tr '\\n' '\\0' < .kubesight-scripts | xargs -0 shellcheck -f json1 -S warning"
        " > shellcheck-report.json || true",
        "if [ ! -f shellcheck-report.json ]; then",
        '  echo "ShellCheck produced no report."',
        f"  {_metric('shellcheck', 'error', problems=0)}",
        "  exit 1",
        "fi",
        "E=$(grep -oE '\"level\" *: *\"error\"' shellcheck-report.json | wc -l | tr -d ' ')",
        "W=$(grep -oE '\"level\" *: *\"warning\"' shellcheck-report.json | wc -l | tr -d ' ')",
        "tr '\\n' '\\0' < .kubesight-scripts | xargs -0 shellcheck -f gcc -S warning"
        " 2>/dev/null | head -n 20 || true",
        f'echo "{SENTINEL} tool=shellcheck status=ok problems=$((E + W)) errors=$E warnings=$W"',
    ]


# ---------------------------------------------------------------------------
# The table the rest of merge checks reads
# ---------------------------------------------------------------------------

LINTERS: Dict[str, Dict[str, object]] = {
    "ruff": {
        "label": "Ruff",
        "stage": "Ruff (Python lint)",
        "env": "python-3.12",
        "commands": ruff_commands,
        "artifact": "ruff-report.json",
        "timeout": 900,
        "counts": "Every finding. Ruff's defaults are bugs, not style.",
    },
    "pmd": {
        "label": "PMD",
        "stage": "PMD (Java lint)",
        "env": "java-jdk11",
        "commands": pmd_commands,
        "artifact": "pmd-report.txt",
        "timeout": 1200,
        "counts": "Every violation of the project's ruleset, else PMD quickstart.",
    },
    "detekt": {
        "label": "detekt",
        "stage": "detekt (Kotlin lint)",
        "env": "java-jdk11",
        "commands": detekt_commands,
        "artifact": "detekt-report.txt",
        "timeout": 1200,
        "counts": "Every issue detekt reports with the project's config.",
    },
    "swiftlint": {
        "label": "SwiftLint",
        "stage": "SwiftLint",
        "env": "swiftlint",
        "commands": swiftlint_commands,
        "artifact": "swiftlint-report.json",
        "timeout": 900,
        "counts": "Errors. Warnings are reported, not counted.",
    },
    "dart_analyze": {
        "label": "Dart analyze",
        "stage": "Dart analyze",
        "env": "flutter-analyze",
        "commands": dart_analyze_commands,
        "artifact": "dart-analyze.txt",
        "timeout": 1800,
        "counts": "Errors and warnings. Infos are reported, not counted.",
    },
    "hadolint": {
        "label": "Hadolint",
        "stage": "Hadolint (Dockerfile lint)",
        "env": "hadolint",
        "commands": hadolint_commands,
        "artifact": "hadolint-report.json",
        "timeout": 600,
        "counts": "Errors and warnings. Info and style are not counted.",
    },
    "shellcheck": {
        "label": "ShellCheck",
        "stage": "ShellCheck (shell lint)",
        "env": "shellcheck",
        "commands": shellcheck_commands,
        "artifact": "shellcheck-report.json",
        "timeout": 600,
        "counts": "Errors and warnings. Info and style are not counted.",
    },
}


def commands_for(tool: str) -> List[str]:
    builder: Callable[[], List[str]] = LINTERS[tool]["commands"]  # type: ignore[assignment]
    return builder()
