"""Which checks fit which application — the "Automatic" tool selection.

A merge check that cannot apply to a repository is worse than no check: ESLint
on a Gradle service reports `skipped`, and a row of stages that did nothing
reads as evidence that something was looked at. So by default the tools are
chosen from the service's ``application_type`` — the same discriminator the
generated build pipeline, templates and icons already read — instead of every
service getting all four.

The rules, deliberately few:

* **ESLint** only where there is JavaScript to lint: Node services.
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
}

_NO_DEPENDENCY_CHECK = {"ios", "flutter"}


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

    if app_type == "node":
        chosen.append("eslint")
        reasons["eslint"] = f"{label} — lints the JavaScript/TypeScript."
    else:
        reasons["eslint"] = f"Off: {label} has no JavaScript to lint."

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
