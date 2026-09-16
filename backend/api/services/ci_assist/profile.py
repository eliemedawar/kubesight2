"""What an application IS, separated from how KubeSight files it.

``CiService.application_type`` conflates several different questions into one
string: ``java_gradle`` answers "which language", "which build tool" and "which
starter kit" at once, and answers none of them in enough detail to build with.
It stays — everything reads it, and a service registered two years ago still
has one — but it stops being the *source* of the answer and becomes something
DERIVED from this profile.

The separation matters most where it is least visible. A profile can say
"Java 17, Spring Boot 3.3.2, Gradle 8.7, wrapper present" and derive
``java_gradle``; the derived type picks the icon and the fallback, while the
detail picks the build image, the commands, and what to warn about. Collapsing
those back into one string is what made "which JDK does this build with?"
unanswerable before.

Two rules the whole module is built around:

**Nothing is invented.** A value KubeSight could not establish is absent and
named in ``unknown``, not guessed at. An absent version is a question for the
user; a wrong one is a build that compiles against the wrong runtime and says
nothing.

**Confidence is a word, not a number.** Application Intelligence removed
model-chosen scores for good reason — a model asked for 0.96 will produce 0.96.
Each detected value instead carries where it was read from and how firmly, in
the same vocabulary findings already use.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Optional

from ..application_intelligence_security import validate_relative_path

SCHEMA_VERSION = "1.0"

# Deliberately open-ended at the edges: `other` exists so an honest answer is
# always available, and a language nobody anticipated does not force a wrong one.
LANGUAGES = (
    "java", "kotlin", "scala", "groovy",
    "javascript", "typescript",
    "python", "go", "csharp", "php", "ruby", "rust",
    "dart", "swift", "objective-c",
    "shell", "other",
)
BUILD_SYSTEMS = (
    "gradle", "maven", "ant",
    "npm", "yarn", "pnpm",
    "pip", "poetry", "pipenv", "setuptools",
    "go", "dotnet", "composer", "bundler", "cargo",
    "flutter", "xcodebuild", "swiftpm",
    "make", "docker", "none", "other",
)
PACKAGE_MANAGERS = (
    "npm", "yarn", "pnpm", "pip", "poetry", "pipenv",
    "cocoapods", "swiftpm", "composer", "bundler", "cargo", "go", "other",
)
PACKAGING = (
    "jar", "war", "ear", "apk", "aab", "ipa",
    "wheel", "sdist", "tarball", "zip",
    "static-site", "bundle", "binary", "container-image", "none", "other",
)
PROJECT_STRUCTURES = ("single", "multi-module", "monorepo")
CONTAINERIZATION_TYPES = ("dockerfile", "containerfile", "buildpack", "none")
PLATFORM_TARGETS = ("android", "ios", "web", "desktop", "server")
# Same vocabulary the Application Intelligence findings use, minus
# "Informational" — a detected build value is either evidenced or it is unknown.
CONFIDENCES = ("Confirmed", "High", "Medium", "Low")

# Other names for things KubeSight already has a word for.
#
# "executable jar" is what a Spring Boot fat jar genuinely is, and refusing it
# because the enum says "jar" loses a correct answer over a synonym — the same
# mistake as refusing `type` for `stageType`. These are vocabulary, not
# capability: every one maps onto a value that was already allowed.
SYNONYMS: Dict[str, Dict[str, str]] = {
    "language": {
        "js": "javascript", "node": "javascript", "nodejs": "javascript",
        "node.js": "javascript", "javascript/typescript": "javascript",
        "ts": "typescript",
        "py": "python", "python3": "python",
        "c#": "csharp", "dotnet": "csharp", ".net": "csharp",
        "golang": "go",
        "objc": "objective-c", "objective c": "objective-c",
        "jvm": "java", "java/kotlin": "java", "kotlin/java": "kotlin",
        "bash": "shell", "sh": "shell",
    },
    "buildSystem": {
        "gradlew": "gradle", "gradle wrapper": "gradle", "gradle build": "gradle",
        "mvn": "maven", "mvnw": "maven", "maven wrapper": "maven",
        "npm scripts": "npm", "yarn berry": "yarn",
        "requirements.txt": "pip", "pip/requirements": "pip",
        "setup.py": "setuptools",
        "go modules": "go", "go build": "go",
        "msbuild": "dotnet", "dotnet cli": "dotnet",
        "xcode": "xcodebuild", "swift package manager": "swiftpm", "spm": "swiftpm",
        "dockerfile": "docker", "docker build": "docker",
        "makefile": "make",
        "n/a": "none", "not applicable": "none", "unknown": "",
    },
    "packageManager": {
        "npm scripts": "npm", "pip3": "pip", "pods": "cocoapods",
        "swift package manager": "swiftpm", "spm": "swiftpm",
    },
    "packaging": {
        # The one that started this.
        "executable jar": "jar", "fat jar": "jar", "uber jar": "jar",
        "uber-jar": "jar", "shaded jar": "jar", "runnable jar": "jar",
        "spring boot jar": "jar", "boot jar": "jar", "bootjar": "jar",
        "jar file": "jar", "war file": "war",
        "docker image": "container-image", "container": "container-image",
        "oci image": "container-image", "image": "container-image",
        "static site": "static-site", "static": "static-site", "spa": "static-site",
        "python wheel": "wheel", "npm package": "bundle", "node module": "bundle",
        "android app bundle": "aab", "app bundle": "aab", "apk file": "apk",
        "executable": "binary", "binary executable": "binary",
        "tar": "tarball", "tar.gz": "tarball", "tgz": "tarball",
        "n/a": "none", "not applicable": "none",
    },
    "projectStructure": {
        "multi module": "multi-module", "multimodule": "multi-module",
        "multi-project": "multi-module", "mono-repo": "monorepo",
        "single module": "single", "single-module": "single",
    },
    "containerization type": {
        "docker": "dockerfile", "container": "dockerfile",
        "podman": "containerfile", "cnb": "buildpack", "buildpacks": "buildpack",
        "n/a": "none", "not applicable": "none",
    },
}

# Fields a user may override. Everything else is either derived or evidence.
OVERRIDABLE = (
    "language", "languageVersion",
    "framework", "frameworkVersion",
    "buildSystem", "buildSystemVersion", "usesBuildWrapper",
    "packageManager", "packaging", "projectStructure",
    "testsDetected",
)

MAX_MODULES = 50
MAX_ARTIFACT_PATHS = 20


class ProfileError(ValueError):
    """An application profile was rejected. Message is user-facing."""


def _text(value: Any, limit: int = 120) -> str:
    return " ".join(str(value or "").split())[:limit]


def _enum(
    value: Any,
    allowed: Iterable[str],
    field: str,
    *,
    required: bool = False,
    strict: bool = True,
    notes: Optional[List[str]] = None,
) -> str:
    cleaned = _text(value, 40).lower()
    if not cleaned:
        if required:
            raise ProfileError(f"The application profile needs a {field}.")
        return ""
    if cleaned in allowed:
        return cleaned

    # A different word for something KubeSight already has.
    mapped = SYNONYMS.get(field, {}).get(cleaned)
    if mapped is not None:
        if mapped and notes is not None:
            notes.append(f"Read {field} '{cleaned}' as '{mapped}'.")
        return mapped if mapped in allowed else ""

    if strict:
        raise ProfileError(
            f"'{cleaned}' is not a {field} KubeSight recognises. "
            f"Use one of: {', '.join(allowed)}."
        )

    # Tolerant path (anything a model produced): an unrecognised value is
    # recorded as "other" where the vocabulary has one and dropped where it does
    # not, and either way it is SAID. Failing the whole analysis because one
    # field used an unfamiliar word throws away everything else that was right.
    fallback = "other" if "other" in allowed else ""
    if notes is not None:
        notes.append(
            f"'{cleaned}' is not a {field} KubeSight recognises"
            + (f"; recorded as '{fallback}'." if fallback else "; left unset.")
        )
    return fallback


def _version(value: Any, *, strict: bool = True, notes: Optional[List[str]] = None) -> str:
    """A version as written, not as parsed.

    Kept verbatim because '17', '1.8', '3.3.2' and '8.7-rc-2' are all real and
    all mean something to the tool that reads them. Only the characters a
    version cannot contain are refused — and outside strict mode a value like
    "17 (LTS)" is trimmed to the part that is a version rather than thrown away.
    """
    cleaned = _text(value, 40)
    if not cleaned:
        return ""
    if all(char.isalnum() or char in "._-+" for char in cleaned):
        return cleaned
    if strict:
        raise ProfileError(f"'{cleaned}' is not a usable version string.")

    trimmed = "".join(
        char for char in cleaned.split()[0] if char.isalnum() or char in "._-+"
    )
    if notes is not None:
        notes.append(
            f"Read version '{cleaned}' as '{trimmed}'." if trimmed
            else f"'{cleaned}' is not a usable version; left unset."
        )
    return trimmed


def _string_list(value: Any, limit: int, item_limit: int = 255) -> List[str]:
    if isinstance(value, str):
        items = [value]
    elif isinstance(value, (list, tuple)):
        items = list(value)
    else:
        return []
    out: List[str] = []
    for item in items[:limit]:
        text = _text(item, item_limit)
        if text and text not in out:
            out.append(text)
    return out


def _evidence(value: Any) -> Dict[str, Dict[str, str]]:
    """Per-field provenance: what was read, from where, how firmly.

    Malformed entries are dropped rather than rejected. Evidence is what makes
    the answer trustworthy, but a badly-shaped citation is not a reason to throw
    away a correct detection — the field simply shows as unattributed.
    """
    if not isinstance(value, dict):
        return {}
    out: Dict[str, Dict[str, str]] = {}
    for field, entry in list(value.items())[:40]:
        if not isinstance(entry, dict):
            continue
        confidence = _text(entry.get("confidence"), 20)
        out[_text(field, 60)] = {
            "value": _text(entry.get("value"), 200),
            "confidence": confidence if confidence in CONFIDENCES else "Medium",
            "source": _text(entry.get("source"), 512),
            "detail": _text(entry.get("detail"), 400),
        }
    return out


def _containerization(
    value: Any, *, strict: bool = True, notes: Optional[List[str]] = None
) -> Dict[str, str]:
    if not isinstance(value, dict):
        return {"type": "none", "dockerfilePath": ""}
    kind = _enum(
        value.get("type"),
        CONTAINERIZATION_TYPES,
        "containerization type",
        strict=strict,
        notes=notes,
    )
    raw = _text(value.get("dockerfilePath"), 512)
    # Validated BEFORE any tidying: stripping a leading slash first would turn
    # "/etc/passwd" into the perfectly valid "etc/passwd" and accept it, which
    # is the difference between rejecting bad input and quietly rewriting it
    # into different input nobody asked for.
    try:
        path = validate_relative_path(raw, "The Dockerfile path") or ""
    except ValueError as exc:
        raise ProfileError(str(exc)) from exc
    return {"type": kind or "none", "dockerfilePath": path}


def normalize(
    payload: Any, *, source: str = "hermes", strict: Optional[bool] = None
) -> Dict[str, Any]:
    """Validate an application profile into the shape KubeSight stores.

    Two audiences, two standards, and the difference is deliberate.

    A profile a PERSON typed is validated strictly: they are at a form, a typo
    is worth telling them about, and "Jva" should not silently become "other".

    A profile a MODEL produced is normalized tolerantly. "executable jar" is
    exactly what a Spring Boot fat jar is, and refusing it because the
    vocabulary says "jar" throws away a correct reading of the repository over
    a synonym. Known synonyms map onto the value they mean; anything genuinely
    unrecognised is recorded as "other" and NAMED in ``notes`` rather than
    guessed at or fatal.

    ``strict`` defaults to whether the profile came from a person.
    """
    if not isinstance(payload, dict):
        raise ProfileError("The application profile must be an object.")

    if strict is None:
        strict = source == "manual"
    notes: List[str] = []

    language = _enum(
        payload.get("language"), LANGUAGES, "language", strict=strict, notes=notes
    )
    build_system = _enum(
        payload.get("buildSystem"), BUILD_SYSTEMS, "buildSystem", strict=strict, notes=notes
    )

    profile: Dict[str, Any] = {
        "schemaVersion": SCHEMA_VERSION,
        "language": language,
        "languageVersion": _version(
            payload.get("languageVersion"), strict=strict, notes=notes
        ),
        "framework": _text(payload.get("framework"), 80).lower(),
        "frameworkVersion": _version(
            payload.get("frameworkVersion"), strict=strict, notes=notes
        ),
        "buildSystem": build_system,
        "buildSystemVersion": _version(
            payload.get("buildSystemVersion"), strict=strict, notes=notes
        ),
        "usesBuildWrapper": bool(payload.get("usesBuildWrapper")),
        "packageManager": _enum(
            payload.get("packageManager"),
            PACKAGE_MANAGERS,
            "packageManager",
            strict=strict,
            notes=notes,
        )
        or None,
        "packaging": _enum(
            payload.get("packaging"), PACKAGING, "packaging", strict=strict, notes=notes
        ),
        "projectStructure": _enum(
            payload.get("projectStructure"),
            PROJECT_STRUCTURES,
            "projectStructure",
            strict=strict,
            notes=notes,
        )
        or "single",
        "modules": _string_list(payload.get("modules"), MAX_MODULES),
        "containerization": _containerization(
            payload.get("containerization"), strict=strict, notes=notes
        ),
        "testsDetected": bool(payload.get("testsDetected")),
        "testFramework": _text(payload.get("testFramework"), 60).lower(),
        "artifactPaths": _string_list(payload.get("artifactPaths"), MAX_ARTIFACT_PATHS, 512),
        "platformTargets": [
            item
            for item in _string_list(payload.get("platformTargets"), 6, 40)
            if item.lower() in PLATFORM_TARGETS
        ],
        "evidence": _evidence(payload.get("evidence")),
        "unknown": _string_list(payload.get("unknown"), 30, 60),
        "overrides": payload.get("overrides") if isinstance(payload.get("overrides"), dict) else {},
        "source": source if source in ("hermes", "manual", "derived") else "hermes",
        # Every value that was read as something other than what arrived. Shown
        # next to the profile, so a reading KubeSight adjusted is visible rather
        # than quietly different from what the model said.
        "notes": notes[:20],
    }
    profile["derivedApplicationType"] = derive_application_type(profile)
    return profile


def derive_application_type(profile: Dict[str, Any]) -> str:
    """The existing ``application_type`` discriminator, from the detail.

    Order matters and is not alphabetical: a Kotlin Android app is Gradle AND
    Java-family AND Android, and calling it ``java_gradle`` would give it a JAR
    pipeline. The most specific true answer wins.
    """
    language = str(profile.get("language") or "").lower()
    build_system = str(profile.get("buildSystem") or "").lower()
    targets = {str(item).lower() for item in profile.get("platformTargets") or []}
    framework = str(profile.get("framework") or "").lower()
    packaging = str(profile.get("packaging") or "").lower()
    containerized = (profile.get("containerization") or {}).get("type") in (
        "dockerfile",
        "containerfile",
    )

    if language == "dart" or build_system == "flutter" or framework == "flutter":
        return "flutter"
    if language in ("swift", "objective-c") or build_system in ("xcodebuild", "swiftpm"):
        return "ios"
    if "ios" in targets and "android" not in targets:
        return "ios"
    if "android" in targets or packaging in ("apk", "aab"):
        return "android"
    if language in ("java", "kotlin", "scala", "groovy"):
        if build_system == "gradle":
            return "java_gradle"
        if build_system in ("maven", "ant"):
            return "java_maven"
        # A JVM project whose build system is genuinely unknown is not silently
        # filed as Maven — that would hand it ./mvnw commands it cannot run.
        return "container" if containerized else "generic"
    if language in ("javascript", "typescript") or build_system in ("npm", "yarn", "pnpm"):
        return "node"
    if language == "python" or build_system in ("pip", "poetry", "pipenv", "setuptools"):
        return "python"
    if containerized:
        return "container"
    return "generic"


def summary(profile: Optional[Dict[str, Any]]) -> str:
    """One line a person can read, for a card or an audit entry."""
    if not profile:
        return "Not analyzed"
    parts: List[str] = []
    language = profile.get("language")
    if language:
        parts.append(
            f"{language.title()} {profile['languageVersion']}".strip()
            if profile.get("languageVersion")
            else language.title()
        )
    if profile.get("framework"):
        framework = profile["framework"]
        parts.append(
            f"{framework} {profile['frameworkVersion']}".strip()
            if profile.get("frameworkVersion")
            else framework
        )
    if profile.get("buildSystem"):
        build = profile["buildSystem"]
        parts.append(
            f"{build} {profile['buildSystemVersion']}".strip()
            if profile.get("buildSystemVersion")
            else build
        )
    return " · ".join(parts) or "Unclassified"


def apply_overrides(
    profile: Dict[str, Any], overrides: Dict[str, Any], *, actor=None
) -> Dict[str, Any]:
    """Replace detected values with what a person says, and record that.

    The record is the point. A user-set Java version has to stay user-set
    through a regenerate, or the next analysis quietly reverts the correction
    and the user has to make it again — which is how people stop trusting a
    feature.
    """
    if not isinstance(overrides, dict):
        raise ProfileError("Overrides must be an object.")
    unknown = sorted(set(overrides) - set(OVERRIDABLE))
    if unknown:
        raise ProfileError(
            f"These fields cannot be overridden: {', '.join(unknown)}. "
            f"Overridable: {', '.join(OVERRIDABLE)}."
        )

    merged = dict(profile or {})
    recorded = dict(merged.get("overrides") or {})
    now = datetime.now(timezone.utc).isoformat()
    for field, value in overrides.items():
        previous = merged.get(field)
        if previous == value:
            continue
        recorded[field] = {
            "was": previous,
            "by": getattr(actor, "id", None),
            "at": now,
        }
        merged[field] = value

    merged["overrides"] = recorded
    # Re-validated as a whole: an override is user input, and a hand-typed
    # "Jva" must fail here rather than reach a build image lookup.
    revalidated = normalize(merged, source="manual" if recorded else merged.get("source", "hermes"))
    revalidated["overrides"] = recorded
    # An overridden field is no longer unknown, whatever the analysis thought.
    revalidated["unknown"] = [
        item for item in revalidated.get("unknown") or [] if item not in recorded
    ]
    return revalidated


def is_overridden(profile: Optional[Dict[str, Any]], field: str) -> bool:
    return bool((profile or {}).get("overrides", {}).get(field))
