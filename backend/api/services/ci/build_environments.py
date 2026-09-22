"""The approved build images, in one place.

Before this module every template held its own ``os.getenv("CI_TEMPLATE_...")``
constant, which was fine while a human chose the stage image. It stops being
fine the moment something else proposes a pipeline: an image name is the one
field where "plausible" and "correct" look identical, and a generated pipeline
that names ``gradle:8-jdk21`` off the public Docker Hub would be rejected by a
cluster that has no route there — after the build was saved, approved, and run.

So the image is never named from outside. A caller states what it NEEDS —
"java 17, gradle" — and this module answers with an approved image and the
runner labels that image implies, or with nothing at all. Pipeline generation
asks for a capability; KubeSight decides what runs it.

Every entry resolves through an environment variable with the installation's
current value as its default, so this is a refactor and not a change: the
images the templates used yesterday are the images they use today, and another
installation still repoints one setting instead of editing a file.

Resolution is deliberately at CALL time, not import time. Templates read these
into module constants at their own import, which is what lets a test reload a
template module with different environment and see the difference.
"""

from __future__ import annotations

import os
from typing import Any, Dict, List, Optional, Tuple

# Mirrors of the values templates.py and default_pipelines.py held before this
# module existed. Changing a default here changes what a NEW pipeline is
# generated with; it never rewrites a pipeline that is already saved.
_REGISTRY_ENV = "CI_TEMPLATE_IMAGE_REGISTRY"
_DEFAULT_REGISTRY = "registry.areeba.com"

# key -> definition.
#
# ``provides`` is the capability vocabulary a caller matches against, and the
# same vocabulary the pipeline generator is handed. ``labels`` are the RUNNER
# labels an image implies — note that they are deliberately coarse ("java", not
# "java11"): the image pins the toolchain, so the runner only has to be able to
# run Linux containers. A finer label would exclude the built-in Kubernetes
# runner for no benefit, which is a mistake this codebase has already made once.
ENVIRONMENTS: Dict[str, Dict[str, Any]] = {
    "java-jdk11": {
        "label": "Java 11 (JDK)",
        "env": "CI_TEMPLATE_JDK_IMAGE",
        "default": "{registry}/adoptopenjdk/openjdk11:jdk-11.0.11_9-alpine-slim",
        "provides": {"java": "11"},
        "labels": ["linux", "java"],
        # A JDK rather than a JRE so it also compiles, and no build tool of its
        # own: the project's own wrapper supplies Maven or Gradle at the version
        # that project pins. That is what keeps the tool right when a project
        # upgrades without anyone editing KubeSight.
        "requiresWrapper": True,
        "notes": (
            "Carries no Maven or Gradle. The project's ./mvnw or ./gradlew "
            "supplies the build tool at the version the repository pins."
        ),
    },
    "gradle-8-jdk11": {
        "label": "Gradle 8 on JDK 11",
        "env": "CI_TEMPLATE_GRADLE_IMAGE",
        "default": "{registry}/gradle:8-jdk11",
        "provides": {"java": "11", "gradle": "8"},
        "labels": ["linux", "java"],
        "requiresWrapper": False,
        "notes": "For projects with no Gradle wrapper.",
    },
    "gradle-9-jdk25": {
        # What this installation's working pipelines actually build with. It was
        # missing from the catalog, which meant a proposal could not ask for the
        # image the site really uses and had to be corrected into an older one.
        "label": "Gradle 9 on JDK 25",
        "env": "CI_TEMPLATE_GRADLE_JDK25_IMAGE",
        "default": "{registry}/gradle:9.1.0-jdk25-alpine",
        "provides": {"java": "25", "gradle": "9"},
        "labels": ["linux", "java"],
        "requiresWrapper": False,
        "notes": (
            "Carries Gradle itself, so a project without a wrapper builds with "
            "`gradle` directly. Dependencies resolve through the internal Maven "
            "mirror via an init script."
        ),
    },
    "maven-3.9-jdk21": {
        "label": "Maven 3.9 on JDK 21",
        "env": "CI_TEMPLATE_MAVEN_IMAGE",
        "default": "{registry}/maven:3.9-eclipse-temurin-21",
        "provides": {"java": "21", "maven": "3.9"},
        "labels": ["linux", "java"],
        "requiresWrapper": False,
        "notes": "For projects with no Maven wrapper.",
    },
    "node-22": {
        "label": "Node.js 22",
        "env": "CI_TEMPLATE_NODE_IMAGE",
        "default": "node:22-alpine",
        "provides": {"node": "22"},
        "labels": ["linux", "node"],
    },
    "python-3.12": {
        "label": "Python 3.12",
        "env": "CI_TEMPLATE_PYTHON_IMAGE",
        "default": "python:3.12-slim",
        "provides": {"python": "3.12"},
        "labels": ["linux", "python"],
    },
    "sonar-scanner": {
        # Merge checks. The scanner CLI only — the SonarQube server is an
        # installation of its own that KubeSight connects to, never one it runs.
        "label": "SonarQube Scanner CLI",
        "env": "CI_TEMPLATE_SONAR_SCANNER_IMAGE",
        "default": "{registry}/sonarsource/sonar-scanner-cli:latest",
        "provides": {"sonar": "true"},
        "labels": ["linux"],
        "notes": (
            "Needs SONAR_HOST_URL and SONAR_TOKEN as CI secrets. The scanner "
            "uploads to the server; the merge check then reads the issue counts "
            "back over the web API."
        ),
    },
    "semgrep": {
        # Merge checks, and the server-free alternative to SonarQube. Semgrep
        # OSS is a binary that reads the checkout and exits — there is nothing
        # to run, keep up, or hold state in, which is the whole reason to pick
        # it over a scanner that needs a server.
        "label": "Semgrep (static analysis)",
        "env": "CI_TEMPLATE_SEMGREP_IMAGE",
        "default": "{registry}/semgrep/semgrep:latest",
        "provides": {"semgrep": "true"},
        "labels": ["linux"],
        "notes": (
            "Caches downloaded rulesets in $SEMGREP_CACHE_DIR. Telemetry is off. "
            "Point SEMGREP_RULES at a directory in the repository to run with no "
            "network at all."
        ),
    },
    "dependency-check": {
        "label": "OWASP Dependency-Check",
        "env": "CI_TEMPLATE_DEPENDENCY_CHECK_IMAGE",
        "default": "{registry}/owasp/dependency-check:latest",
        "provides": {"dependencyCheck": "true"},
        "labels": ["linux"],
        "notes": (
            "Keeps its NVD database in the build cache at $DC_DATA_DIR. The "
            "first run builds that database and is slow; later runs update it. "
            "Set NVD_API_KEY as a CI secret to avoid NVD rate limiting."
        ),
    },
    "android": {
        "label": "Android SDK",
        "env": "CI_TEMPLATE_ANDROID_IMAGE",
        # No default: this installation has no mirrored Android image, and
        # inventing one would produce a pipeline that cannot pull. An
        # unconfigured environment is reported as unconfigured.
        "default": "",
        "provides": {"android": "true", "java": "17"},
        "labels": ["linux", "android"],
    },
    "flutter": {
        "label": "Flutter SDK",
        "env": "CI_TEMPLATE_FLUTTER_IMAGE",
        "default": "",
        "provides": {"flutter": "true", "android": "true"},
        "labels": ["linux", "flutter"],
    },
    "macos-xcode": {
        "label": "macOS with Xcode",
        # Not an image at all: an iOS build runs on the machine, because that
        # is the only place Xcode exists. Recorded here so the generator has
        # one vocabulary for "where does this run" rather than two.
        "env": "",
        "default": "",
        "provides": {"xcode": "true", "swift": "true"},
        "labels": ["macos", "xcode"],
        "runnerType": "agent_macos",
        "imageless": True,
        "notes": "Runs directly on a registered Mac agent; no container image.",
    },
}

# Literal images an operator explicitly permits in a generated pipeline, as a
# comma-separated list of prefixes. Empty by default: the catalog is the
# supported route, and this exists so an installation with a mirror of its own
# is not forced to add a catalog entry for a one-off stage.
_ALLOWED_PREFIX_ENV = "CI_ALLOWED_IMAGE_PREFIXES"


def registry() -> str:
    return os.getenv(_REGISTRY_ENV, _DEFAULT_REGISTRY).rstrip("/")


def image(key: str) -> str:
    """The approved image for one environment key, or "" when it has none.

    "" is a real answer, not a failure: ``macos-xcode`` is imageless by nature
    and ``android``/``flutter`` are unconfigured until an installation points
    them somewhere. A stage with no image runs on whatever the runner provides,
    which is exactly right for an agent and exactly wrong silently on
    Kubernetes — which is why :func:`resolve` reports ``configured``.
    """
    entry = ENVIRONMENTS.get(str(key or "").strip())
    if entry is None:
        return ""
    env_name = entry.get("env") or ""
    default = str(entry.get("default") or "").format(registry=registry())
    value = os.getenv(env_name, default) if env_name else default
    return str(value or "").strip()


def resolve(key: str) -> Optional[Dict[str, Any]]:
    """Everything a stage needs from one environment key, or None if unknown."""
    clean = str(key or "").strip()
    entry = ENVIRONMENTS.get(clean)
    if entry is None:
        return None
    resolved_image = image(clean)
    return {
        "key": clean,
        "label": entry["label"],
        "image": resolved_image,
        "labels": list(entry.get("labels") or ["linux"]),
        "runnerType": entry.get("runnerType") or "",
        "provides": dict(entry.get("provides") or {}),
        "imageless": bool(entry.get("imageless")),
        "requiresWrapper": bool(entry.get("requiresWrapper")),
        "notes": entry.get("notes", ""),
        # False means "this installation has not pointed the key anywhere".
        # A generated pipeline may still reference it; the validator warns.
        "configured": bool(resolved_image) or bool(entry.get("imageless")),
    }


def catalog(*, configured_only: bool = False) -> List[Dict[str, Any]]:
    """The whole catalog, for the Hermes capability block and the UI."""
    items = [resolve(key) for key in ENVIRONMENTS]
    return [
        item
        for item in items
        if item is not None and (not configured_only or item["configured"])
    ]


def _version_distance(wanted: str, offered: str) -> Optional[int]:
    """How far apart two version strings are on their major number.

    None when either side is not a version at all, which is the honest answer
    for ``{"android": "true"}``.
    """
    try:
        return abs(int(str(wanted).split(".")[0]) - int(str(offered).split(".")[0]))
    except (TypeError, ValueError):
        return None


def best_for(
    *,
    language: str = "",
    language_version: str = "",
    build_system: str = "",
    platform: str = "",
) -> Tuple[Optional[str], str]:
    """The approved environment closest to what a project needs.

    Returns ``(key, warning)``. The warning is non-empty when the match is
    approximate — the honest case this installation is actually in today, where
    the only JDK image is Java 11 and a repository may well target 17. Saying so
    is the point: a silent downgrade produces a green build that compiled
    against the wrong runtime, and nobody finds out until it starts.
    """
    platform = str(platform or "").strip().lower()
    if platform == "ios":
        return "macos-xcode", ""
    if platform == "android":
        return "android", ""
    if platform == "flutter":
        return "flutter", ""

    language = str(language or "").strip().lower()
    build_system = str(build_system or "").strip().lower()

    if language in ("java", "kotlin", "scala", "groovy"):
        candidates = [
            key
            for key in ("java-jdk11", "gradle-8-jdk11", "maven-3.9-jdk21")
            if resolve(key)["configured"]
        ]
        # The build tool is a hard filter, not a preference. Offering a Gradle
        # project the Maven image because its JDK happens to be closer would be
        # recommending an image that cannot build it — and the version-distance
        # sort below will happily do exactly that if the tool is left as a
        # tie-break.
        if build_system in ("gradle", "maven"):
            other = "maven" if build_system == "gradle" else "gradle"
            candidates = [
                key
                for key in candidates
                if other not in resolve(key)["provides"]
            ]
        if not candidates:
            return None, ""
        if not language_version:
            # No version to match on: the JDK-only image is the safest default,
            # because the project's own wrapper supplies the build tool at the
            # version the repository pins.
            return candidates[0], ""

        scored = []
        for key in candidates:
            environment = resolve(key)
            offered = environment["provides"].get("java", "")
            distance = _version_distance(language_version, offered)
            scored.append(
                (
                    distance if distance is not None else 99,
                    # Tie-break towards the wrapper-neutral JDK image: it serves
                    # both tools and lets the repository pin its own.
                    0 if environment["requiresWrapper"] else 1,
                    key,
                    offered,
                )
            )
        scored.sort(key=lambda item: (item[0], item[1]))
        distance, _wrapper, key, offered = scored[0]
        if distance == 0:
            return key, ""
        return key, (
            f"This project targets Java {language_version}, but the closest approved "
            f"build environment provides Java {offered}. Add an approved Java "
            f"{language_version} image to the build environment catalog "
            f"(CI_TEMPLATE_JDK_IMAGE) before relying on this pipeline."
        )

    if language in ("javascript", "typescript", "node", "nodejs"):
        return ("node-22", "") if resolve("node-22")["configured"] else (None, "")
    if language == "python":
        return ("python-3.12", "") if resolve("python-3.12")["configured"] else (None, "")
    if language in ("swift", "objective-c", "objectivec"):
        return "macos-xcode", ""
    if language == "dart":
        return "flutter", ""
    return None, ""


def allowed_image_prefixes() -> List[str]:
    return [
        item.strip()
        for item in os.getenv(_ALLOWED_PREFIX_ENV, "").split(",")
        if item.strip()
    ]


def is_permitted_image(value: str) -> bool:
    """Whether a literal image may appear in a GENERATED pipeline.

    Hand-written pipelines are unaffected — a person naming an image is making
    a decision, and this is not a place to second-guess them. This governs only
    what a proposal is allowed to contain.
    """
    candidate = str(value or "").strip()
    if not candidate:
        return True
    if candidate in {item["image"] for item in catalog() if item["image"]}:
        return True
    return any(candidate.startswith(prefix) for prefix in allowed_image_prefixes())
