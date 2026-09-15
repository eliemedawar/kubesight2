"""What the repository actually says, gathered without cloning it.

Application Intelligence clones into an isolated Kubernetes Job because it runs
Trivy and Semgrep over the whole tree. Working out that a project is Gradle 8.7
on Java 17 needs no such thing: it needs the file listing and about thirty
build descriptors, all of which the source port can read over the provider's
API in a few seconds. Reaching for the Job here would turn a twenty-second
interaction into a multi-minute one and make registering a service depend on
cluster health.

So this reads through the SAME source port the rest of CI uses — same
credential profile, same URL validation, same size caps — and produces three
things:

``tree``          every path, so structure is visible (modules, wrappers, where
                  the Dockerfile lives) without reading anything
``files``         the build descriptors, redacted and budgeted
``deterministic`` facts KubeSight worked out ITSELF from the tree

That last one matters more than it looks. A model handed only prose will
cheerfully assert a Gradle wrapper that is not there; a model handed
``hasGradleWrapper: false`` alongside the files has to contend with it. Every
deterministic fact is one fewer thing that can be imagined.

Nothing that is not a build descriptor is sent. Not source, not README, not
documentation — they are the highest-volume, lowest-signal, highest-injection
part of a repository, and none of them decide how it is built.
"""

from __future__ import annotations

import posixpath
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from ..application_intelligence_security import redact_text
from ..ci import source as source_port

# Budgets. The ceiling that matters is the model's context, not the API's: forty
# build descriptors is already more than any real project has.
MAX_FILES = 40
MAX_FILE_CHARS = 64_000
MAX_TOTAL_CHARS = 400_000
# How deep a per-module build file is still interesting. Beyond this it is a
# fixture or a sample, not a module anybody builds.
MAX_MODULE_DEPTH = 3


class EvidenceError(RuntimeError):
    """The repository could not be read. Message is user-facing."""


# Exact filenames worth reading, with the priority they are read in. Lower
# sorts first — when the budget runs out it must run out on the least useful
# file, not on whichever happened to sort last alphabetically.
_PRIORITY_FILES: Dict[str, int] = {
    # 0 — what the project IS. Never dropped.
    "pom.xml": 0,
    "build.gradle": 0,
    "build.gradle.kts": 0,
    "settings.gradle": 0,
    "settings.gradle.kts": 0,
    "package.json": 0,
    "pyproject.toml": 0,
    "requirements.txt": 0,
    "pubspec.yaml": 0,
    "package.swift": 0,
    "go.mod": 0,
    "composer.json": 0,
    "gemfile": 0,
    "cargo.toml": 0,
    # 1 — what version of the tooling.
    "gradle/wrapper/gradle-wrapper.properties": 1,
    ".mvn/wrapper/maven-wrapper.properties": 1,
    "gradle.properties": 1,
    "gradle/libs.versions.toml": 1,
    ".nvmrc": 1,
    ".node-version": 1,
    ".java-version": 1,
    ".sdkmanrc": 1,
    ".tool-versions": 1,
    ".python-version": 1,
    "setup.py": 1,
    "setup.cfg": 1,
    "pipfile": 1,
    # 2 — how it is containerised.
    "dockerfile": 2,
    "containerfile": 2,
    "docker-compose.yml": 2,
    "docker-compose.yaml": 2,
    "compose.yml": 2,
    "compose.yaml": 2,
    # 3 — how it is built today. A Jenkinsfile is the best evidence there is of
    # what this project's build actually needs, and CI already knows how to read
    # one — see services/ci/jenkinsfile.py.
    "jenkinsfile": 3,
    "bitbucket-pipelines.yml": 3,
    ".gitlab-ci.yml": 3,
    "azure-pipelines.yml": 3,
    "makefile": 3,
    # 4 — framework and runtime hints.
    "tsconfig.json": 4,
    "angular.json": 4,
    "nest-cli.json": 4,
    "manage.py": 4,
    "podfile": 4,
    "local.properties.sample": 4,
}

# Suffix matches, for files whose NAME is the project's.
_PRIORITY_SUFFIXES: Tuple[Tuple[str, int], ...] = (
    (".csproj", 0),
    (".sln", 1),
    ("/dockerfile", 2),
    (".dockerfile", 2),
)

# Prefix matches on the whole path.
_PRIORITY_PREFIXES: Tuple[Tuple[str, int], ...] = (
    (".github/workflows/", 3),
)

# Framework configuration whose STEM is what identifies it, at any extension.
_PRIORITY_STEMS: Tuple[Tuple[str, int], ...] = (
    ("next.config", 4),
    ("vite.config", 4),
    ("webpack.config", 4),
    ("application", 4),   # application.yml / application.properties
    ("build", 9),         # build.sbt and friends — last resort
)

# Directories that never hold evidence and always hold volume.
_IGNORED_SEGMENTS = frozenset(
    {
        ".git", "node_modules", "vendor", "target", "build", "dist", "out",
        "bin", "obj", "coverage", ".gradle", ".idea", ".venv", "venv",
        "__pycache__", "Pods", "DerivedData", ".terraform",
    }
)

# Files whose NAME promises credentials. Never read, whatever the redactor
# would have done with them — not reading is a stronger guarantee than redacting.
_NEVER_READ = frozenset(
    {
        "local.properties", ".env", ".npmrc", ".netrc", "settings.xml",
        "credentials", "id_rsa", "keystore.properties", "secrets.yml",
        "secrets.yaml", "gradle.properties.local",
    }
)


@dataclass
class Evidence:
    """Everything a proposal is allowed to be based on."""

    revision: str
    working_directory: str = ""
    tree: List[str] = field(default_factory=list)
    files: List[Dict[str, str]] = field(default_factory=list)
    deterministic: Dict[str, Any] = field(default_factory=dict)
    coverage: Dict[str, Any] = field(default_factory=dict)

    def as_payload(self) -> Dict[str, Any]:
        return {
            "repository": {
                "revision": self.revision,
                "workingDirectory": self.working_directory,
            },
            "tree": self.tree,
            "files": self.files,
            "deterministic": self.deterministic,
        }


def _relative(path: str, root: str) -> Optional[str]:
    """A repository path expressed relative to the service's own directory.

    A monorepo service points at one subdirectory, and everything it is told
    must be about that subdirectory — otherwise a proposal for the payments
    service is built from the shipping service's build.gradle.
    """
    if not root:
        return path
    prefix = f"{root.strip('/')}/"
    return path[len(prefix):] if path.startswith(prefix) else None


def _is_ignored(path: str) -> bool:
    return any(segment in _IGNORED_SEGMENTS for segment in path.split("/")[:-1])


def _priority(path: str) -> Optional[int]:
    """How badly this file is wanted, or None if it is not wanted at all."""
    lowered = path.lower()
    name = lowered.rsplit("/", 1)[-1]

    if name in _NEVER_READ:
        return None
    if lowered in _PRIORITY_FILES:
        return _PRIORITY_FILES[lowered]
    if name in _PRIORITY_FILES:
        # A build descriptor inside a module is still one, just less urgent the
        # deeper it sits — that is what makes multi-module projects legible
        # without reading fifty poms.
        depth = lowered.count("/")
        if depth > MAX_MODULE_DEPTH:
            return None
        return _PRIORITY_FILES[name] + (2 if depth else 0)
    for suffix, score in _PRIORITY_SUFFIXES:
        if lowered.endswith(suffix):
            return score + (2 if lowered.count("/") else 0)
    for prefix, score in _PRIORITY_PREFIXES:
        if lowered.startswith(prefix):
            return score
    stem = name.rsplit(".", 1)[0] if "." in name else name
    for candidate, score in _PRIORITY_STEMS:
        if stem == candidate or stem.startswith(f"{candidate}-"):
            if lowered.rsplit(".", 1)[-1] in ("yml", "yaml", "properties", "json", "js", "ts", "mjs", "sbt"):
                return score + (1 if lowered.count("/") > 2 else 0)
    return None


def _deterministic_facts(paths: List[str]) -> Dict[str, Any]:
    """What KubeSight can state without asking anybody.

    Everything here is a set-membership test over the file listing, which makes
    it the part of the evidence that cannot be wrong. A model's claim that
    contradicts one of these is a claim to disbelieve.
    """
    lowered = {path.lower() for path in paths}
    names = {path.lower().rsplit("/", 1)[-1] for path in paths}

    def any_named(*candidates: str) -> bool:
        return any(name in names for name in candidates)

    dockerfiles = sorted(
        path
        for path in paths
        if path.lower().rsplit("/", 1)[-1] == "dockerfile"
        or path.lower().endswith(".dockerfile")
        or path.lower().rsplit("/", 1)[-1].startswith("dockerfile.")
    )
    gradle_modules = sorted(
        {
            posixpath.dirname(path)
            for path in paths
            if path.lower().rsplit("/", 1)[-1] in ("build.gradle", "build.gradle.kts")
            and posixpath.dirname(path)
        }
    )
    maven_modules = sorted(
        {
            posixpath.dirname(path)
            for path in paths
            if path.lower().rsplit("/", 1)[-1] == "pom.xml" and posixpath.dirname(path)
        }
    )
    return {
        "fileCount": len(paths),
        "hasGradleWrapper": "gradlew" in lowered
        or "gradle/wrapper/gradle-wrapper.properties" in lowered,
        "hasMavenWrapper": "mvnw" in lowered
        or ".mvn/wrapper/maven-wrapper.properties" in lowered,
        "hasGradleBuild": any_named("build.gradle", "build.gradle.kts"),
        "hasMavenPom": "pom.xml" in names,
        "hasPackageJson": "package.json" in names,
        "lockfiles": sorted(
            name
            for name in ("package-lock.json", "yarn.lock", "pnpm-lock.yaml", "poetry.lock", "pipfile.lock", "gemfile.lock", "cargo.lock", "pubspec.lock")
            if name in names
        ),
        "dockerfiles": dockerfiles[:20],
        "gradleModules": gradle_modules[:30],
        "mavenModules": maven_modules[:30],
        "hasAndroidAppModule": any(
            path.lower().startswith("app/build.gradle") for path in paths
        ),
        "xcodeProjects": sorted(
            {
                path.split("/")[0] if "/" in path else path
                for path in paths
                if ".xcodeproj/" in path or ".xcworkspace/" in path
            }
        )[:10],
        "hasPubspec": "pubspec.yaml" in names,
        # Test presence is a directory fact, not an opinion. It decides whether
        # a test stage is generated at all.
        "testDirectories": sorted(
            {
                segment
                for path in paths
                for segment in (path.rsplit("/", 1)[0],)
                if "/test/" in f"/{path}" or path.startswith("test/") or "/tests/" in f"/{path}" or path.startswith("tests/") or "/__tests__/" in f"/{path}" or "src/test" in path
            }
        )[:20],
        "ciFiles": sorted(
            path
            for path in paths
            if path.lower().rsplit("/", 1)[-1]
            in ("jenkinsfile", "bitbucket-pipelines.yml", ".gitlab-ci.yml", "azure-pipelines.yml")
            or path.lower().startswith(".github/workflows/")
        )[:20],
    }


def collect(service, revision: str = "") -> Evidence:
    """Read one repository into the evidence a proposal may be based on.

    Raises :class:`EvidenceError` with a message meant for a person — this runs
    behind a button, and "Bitbucket rejected this credential" is the whole
    answer somebody needs.
    """
    if not service.source_ready():
        raise EvidenceError(
            "Connect a repository and credential before analyzing this service."
        )

    chosen = str(revision or "").strip() or service.default_branch or "main"
    root = str(service.working_directory or "").strip("/")

    try:
        handler = source_port.get_provider(service.repository_provider)
        ref = handler.parse_repository_url(service.repository_url)
    except Exception as exc:
        raise EvidenceError(str(exc) or "The repository could not be addressed.") from exc

    lister = getattr(handler, "list_tree", None)
    if lister is None:
        raise EvidenceError(
            f"The {service.repository_provider} source provider cannot list a "
            "repository tree, which analysis needs."
        )
    try:
        listing = lister(ref, service.credential_profile, chosen)
    except source_port.SourceError as exc:
        raise EvidenceError(str(exc)) from exc

    scoped: List[str] = []
    for path in listing.paths:
        relative = _relative(path, root)
        if relative and not _is_ignored(relative):
            scoped.append(relative)

    if not scoped:
        raise EvidenceError(
            f"Nothing was found at '{root or '/'}' on {chosen}. Check the branch and "
            "the service's working directory."
        )

    wanted = sorted(
        ((score, path) for path in scoped for score in (_priority(path),) if score is not None),
        key=lambda item: (item[0], item[1].count("/"), item[1]),
    )

    files: List[Dict[str, str]] = []
    total = 0
    skipped: List[str] = []
    for _score, path in wanted:
        if len(files) >= MAX_FILES or total >= MAX_TOTAL_CHARS:
            skipped.append(path)
            continue
        full = posixpath.join(root, path) if root else path
        try:
            body = handler.read_file(ref, service.credential_profile, chosen, full)
        except Exception:
            # A file in the listing that will not read is normal — a symlink, a
            # submodule, a binary. It is one missing signal, not a failure.
            continue
        # Redacted on the way in, not on the way out: nothing downstream of this
        # point should ever hold the unredacted text.
        body = redact_text(body, max_chars=MAX_FILE_CHARS)
        if len(body) > MAX_FILE_CHARS:
            body = body[:MAX_FILE_CHARS]
        files.append({"path": path, "content": body})
        total += len(body)

    return Evidence(
        revision=chosen,
        working_directory=root,
        # The tree travels whole where it fits: structure is cheap and is what
        # stops "there is probably a wrapper" being a plausible answer.
        tree=scoped[:5000],
        files=files,
        deterministic=_deterministic_facts(scoped),
        coverage={
            "treeTruncated": bool(listing.truncated),
            "filesInTree": len(scoped),
            "filesConsidered": len(wanted),
            "filesRead": len(files),
            "filesSkippedForBudget": len(skipped),
            "charactersRead": total,
            "revision": chosen,
            "workingDirectory": root,
        },
    )
