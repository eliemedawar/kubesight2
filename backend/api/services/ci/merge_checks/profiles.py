"""Which checks fit which application — the "Automatic" tool selection.

A merge check that cannot apply to a repository is worse than no check: ESLint
on a Gradle service reports `skipped`, and a row of stages that did nothing
reads as evidence that something was looked at. So by default the tools are
chosen from the service's ``application_type`` — the same discriminator the
generated build pipeline, templates and icons already read — instead of every
service getting all four.

The rules, deliberately few:

* **A linter for the language**, ESLint's counterpart: ESLint for Node, Ruff
  for Python, PMD for Java (plus detekt for Kotlin on Gradle and Android),
  SwiftLint for iOS, ``dart analyze`` for Flutter.
* **Hadolint and ShellCheck** on every server-side type, since those ship
  Dockerfiles and scripts. Each reports `skipped` when the repository has none,
  so carrying them costs a line in the log, not a false result.
* **One code analyser, never two.** SonarQube when this service can reach one
  (both ``SONAR_HOST_URL`` and ``SONAR_TOKEN`` are available to it), Semgrep
  otherwise. They answer the same question; running both doubles the time and
  counts the same finding twice against the total.
* **Dependency-Check** wherever it has a dependency format to read. Not for
  Flutter or iOS, whose manifests (pubspec, Podfile, SwiftPM) it does not
  analyse — there it would report zero and mean nothing.

"Custom" mode keeps whatever the operator ticked; nothing here overrides it.
"""

from __future__ import annotations

from typing import Dict, Iterable, List, Optional, Tuple

from ....models_merge_checks import MERGE_CHECK_TOOLS

TOOLS_MODES = ("auto", "custom")

_SONAR_SECRETS = ("SONAR_HOST_URL", "SONAR_TOKEN")

TYPE_LABELS = {
    "container": "Container",
    "java_maven": "Java (Maven)",
    "java_gradle": "Java (Gradle)",
    "java": "Java (Maven)",
    "node": "Node.js",
    "python": "Python",
    "android": "Android",
    "ios": "iOS",
    "flutter": "Flutter",
    "generic": "Generic",
}

# Semgrep registry packs per application type, on top of p/default. The
# language pack is what makes a Java scan look for Java problems specifically
# rather than only the cross-language defaults. Only packs that exist in the
# registry are named — an unknown pack fails the whole scan.
_SEMGREP_PACKS = {
    "java_maven": ("p/default", "p/java"),
    "java_gradle": ("p/default", "p/java"),
    "java": ("p/default", "p/java"),
    "android": ("p/default", "p/java", "p/kotlin"),
    "node": ("p/default", "p/javascript", "p/typescript"),
    "python": ("p/default", "p/python"),
    "container": ("p/default", "p/dockerfile"),
    "ios": ("p/default", "p/swift"),
}

_NO_DEPENDENCY_CHECK = {"ios", "flutter"}

# The language linter(s) per type. Order does not matter; MERGE_CHECK_TOOLS
# decides the stage order.
_LANGUAGE_LINTERS = {
    "node": ("eslint",),
    "python": ("ruff",),
    "java_maven": ("pmd",),
    "java": ("pmd",),
    "java_gradle": ("pmd", "detekt"),
    "android": ("pmd", "detekt"),
    "ios": ("swiftlint",),
    "flutter": ("dart_analyze",),
}
# Types that ship server-side: they carry Dockerfiles and shell scripts.
_SERVER_TYPES = {"node", "python", "java_maven", "java", "java_gradle", "container", "generic"}

_LINTER_REASON = {
    "eslint": "lints the JavaScript/TypeScript with the project's own ESLint and config",
    "ruff": "lints the Python (the project's Ruff config, else Ruff's bug-only defaults)",
    "pmd": "lints the Java (the project's ruleset, else PMD quickstart)",
    "detekt": "lints any Kotlin; skipped when there is none",
    "swiftlint": "lints the Swift",
    "dart_analyze": "runs dart analyze with the project's analysis_options.yaml",
    "hadolint": "lints the Dockerfiles; skipped when there are none",
    "shellcheck": "lints the shell scripts; skipped when there are none",
}


def application_type(service) -> str:
    value = str(getattr(service, "application_type", "") or "generic").strip().lower()
    return value or "generic"


def type_label(app_type: str) -> str:
    return TYPE_LABELS.get(app_type, app_type or "Generic")


def semgrep_rules(app_type: str) -> str:
    """The default rule set for this type, space separated."""
    return " ".join(_SEMGREP_PACKS.get(app_type, ("p/default",)))


def sonar_available(known_secret_keys: Optional[Iterable[str]]) -> bool:
    keys = set(known_secret_keys or ())
    return all(name in keys for name in _SONAR_SECRETS)


def recommended_tools(
    app_type: str, known_secret_keys: Optional[Iterable[str]] = None
) -> Tuple[List[str], Dict[str, str]]:
    """The tools for this type, and a one-line reason for each tool's inclusion
    or exclusion — the panel shows the reason so "why is ESLint off?" has an
    answer on the page."""
    use_sonar = sonar_available(known_secret_keys)
    reasons: Dict[str, str] = {}
    chosen: List[str] = []
    label = type_label(app_type)

    linters = set(_LANGUAGE_LINTERS.get(app_type, ()))
    if app_type in _SERVER_TYPES:
        linters |= {"hadolint", "shellcheck"}
    for tool, why in _LINTER_REASON.items():
        if tool in linters:
            chosen.append(tool)
            reasons[tool] = f"{label}: {why}."
        else:
            reasons[tool] = f"Off: not a {label} language."
    if "hadolint" not in linters:
        reasons["hadolint"] = f"Off: {label} apps do not ship a Dockerfile."
        reasons["shellcheck"] = f"Off: {label} apps do not ship shell scripts."

    if use_sonar:
        chosen.append("sonar")
        reasons["sonar"] = "SONAR_HOST_URL and SONAR_TOKEN are set for this service."
        reasons["semgrep"] = "Off: SonarQube is configured and does the same job."
    else:
        chosen.append("semgrep")
        reasons["semgrep"] = f"Scans with {semgrep_rules(app_type)}."
        reasons["sonar"] = "Off: add SONAR_HOST_URL and SONAR_TOKEN secrets to use it instead of Semgrep."

    if app_type in _NO_DEPENDENCY_CHECK:
        reasons["dependency_check"] = f"Off: Dependency-Check cannot read {label} dependency manifests."
    else:
        chosen.append("dependency_check")
        reasons["dependency_check"] = "Scans the dependencies for known CVEs."

    ordered = [tool for tool in MERGE_CHECK_TOOLS if tool in chosen]
    return ordered, reasons
