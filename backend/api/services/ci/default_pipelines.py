"""Repository-aware fallback pipelines for services with no saved stages.

The provider returns ordinary KubeSight stage dictionaries.  ``pipelines``
normalizes those dictionaries into the same stage objects used by persisted
and Jenkins-imported pipelines; runners and the execution engine therefore do
not need a second path.

Repository inspection is deliberately small and deterministic.  It reads only
the handful of marker files relevant to the selected application type.  Every
generated command also performs the same cheap checks in the checked-out tree,
so a temporary source-host failure or a build pinned to a newer commit cannot
make the fallback choose the wrong tool.
"""

from __future__ import annotations

import json
import os
import posixpath
from copy import deepcopy
from typing import Any, Dict, Iterable, List, Optional

from . import source as source_port
from . import templates


_REGISTRY = os.getenv("CI_TEMPLATE_IMAGE_REGISTRY", "registry.areeba.com").rstrip("/")
_GRADLE_IMAGE = os.getenv("CI_TEMPLATE_GRADLE_IMAGE", f"{_REGISTRY}/gradle:8-jdk11")
_MAVEN_IMAGE = os.getenv(
    "CI_TEMPLATE_MAVEN_IMAGE", f"{_REGISTRY}/maven:3.9-eclipse-temurin-21"
)
_ANDROID_IMAGE = os.getenv("CI_TEMPLATE_ANDROID_IMAGE", "").strip()
_FLUTTER_IMAGE = os.getenv("CI_TEMPLATE_FLUTTER_IMAGE", "").strip()


_PROBES = {
    "java_gradle": ("gradlew", "build.gradle", "build.gradle.kts", "settings.gradle"),
    "java_maven": ("mvnw", "pom.xml"),
    "java": ("mvnw", "pom.xml"),
    "node": ("package.json", "package-lock.json", "yarn.lock", "pnpm-lock.yaml"),
    "python": ("pyproject.toml", "requirements.txt", "setup.py", "setup.cfg"),
    "android": (
        "gradlew",
        "settings.gradle",
        "settings.gradle.kts",
        "app/build.gradle",
        "app/build.gradle.kts",
    ),
    "flutter": ("pubspec.yaml",),
    "container": ("Dockerfile", "dockerfile"),
    "ios": ("Package.swift", "Podfile"),
}


def _type(value: str) -> str:
    value = (value or "").strip().lower()
    return "java_maven" if value == "java" else value


def _repo_path(service, path: str) -> str:
    root = str(getattr(service, "working_directory", "") or "").strip("/")
    return posixpath.join(root, path) if root else path


def inspect_repository(service, revision: str = "") -> Dict[str, Any]:
    """Best-effort marker-file inspection through the configured source port.

    Missing files are normal and provider failures never prevent a fallback
    pipeline from being shown.  The generated shell still verifies the actual
    checkout at execution time.
    """
    app_type = _type(getattr(service, "application_type", "generic"))
    paths = _PROBES.get(app_type, ())
    result: Dict[str, Any] = {
        "attempted": False,
        "files": {},
        "revision": revision or getattr(service, "default_branch", "main") or "main",
    }
    if not paths or not getattr(service, "source_ready", lambda: False)():
        return result

    result["attempted"] = True
    try:
        handler = source_port.get_provider(service.repository_provider)
        ref = handler.parse_repository_url(service.repository_url)
    except Exception:
        return result

    for path in paths:
        try:
            result["files"][path] = handler.read_file(
                ref,
                service.credential_profile,
                result["revision"],
                _repo_path(service, path),
            )
        except Exception:
            # A 404 means only that this signal is absent; an outage is handled
            # by the checkout-time detection embedded in the generated stage.
            continue
    return result


def _stage(
    name: str,
    commands: Iterable[str],
    *,
    image: str = "",
    labels: Optional[List[str]] = None,
    runner_type: str = "",
    artifacts: Optional[List[Dict[str, str]]] = None,
    timeout: int = 1800,
    env: Optional[Dict[str, str]] = None,
) -> Dict[str, Any]:
    return {
        "name": name,
        "stageType": "command",
        "runnerType": runner_type,
        "runnerLabels": list(labels or ["linux"]),
        "image": image,
        "commands": list(commands),
        "env": dict(env or {}),
        "artifacts": list(artifacts or []),
        "timeoutSeconds": timeout,
    }


def _checkout(*, labels: Optional[List[str]] = None, runner_type: str = "") -> Dict[str, Any]:
    return {
        "name": "Checkout",
        "stageType": "checkout",
        "runnerType": runner_type,
        "runnerLabels": list(labels or ["linux"]),
        "commands": [],
        "timeoutSeconds": 600,
    }


def _has(inspection: Dict[str, Any], path: str) -> bool:
    return path in (inspection.get("files") or {})


def _package_json(inspection: Dict[str, Any]) -> Dict[str, Any]:
    try:
        value = json.loads((inspection.get("files") or {}).get("package.json", "{}"))
        return value if isinstance(value, dict) else {}
    except (TypeError, ValueError):
        return {}


def _result(
    app_type: str,
    stages: List[Dict[str, Any]],
    command: str,
    inspection: Dict[str, Any],
    *,
    parameters: Optional[List[Dict[str, Any]]] = None,
    requires_customization: bool = False,
) -> Dict[str, Any]:
    label = templates.template_for(app_type)["label"]
    return {
        "name": "default",
        "description": f"KubeSight default pipeline for {label}.",
        "isDefault": True,
        "enabled": True,
        "parameters": deepcopy(parameters or []),
        "stages": stages,
        "metadata": {
            "source": "kubesight_default",
            "applicationType": app_type,
            "applicationTypeLabel": label,
            "detectedCommand": command,
            "detectedFiles": sorted((inspection.get("files") or {}).keys()),
            "inspectionAttempted": bool(inspection.get("attempted")),
            "requiresCustomization": requires_customization,
        },
    }


def resolve_default_pipeline(
    application_type: str, inspection: Optional[Dict[str, Any]] = None
) -> Dict[str, Any]:
    """Return a native pipeline payload plus display-only detection metadata."""
    app_type = _type(application_type)
    inspection = inspection or {"attempted": False, "files": {}}
    attempted = bool(inspection.get("attempted"))

    if app_type == "java_gradle":
        wrapper = _has(inspection, "gradlew")
        if wrapper:
            command = "./gradlew --no-daemon clean build"
            image = templates._JDK_IMAGE
        elif attempted:
            command = "gradle --no-daemon clean build"
            image = _GRADLE_IMAGE
        else:
            command = (
                "if [ -x ./gradlew ]; then ./gradlew --no-daemon clean build; "
                "else gradle --no-daemon clean build; fi"
            )
            image = _GRADLE_IMAGE
        return _result(
            app_type,
            [
                _checkout(),
                _stage(
                    "Build",
                    [command],
                    image=image,
                    # The image pins the JDK; the runner only needs to support
                    # Java workloads. Requiring ``java11`` would exclude the
                    # built-in Kubernetes runner, which advertises ``java``.
                    labels=["linux", "java"],
                    artifacts=[{"path": "build/libs/*.jar", "type": "jar"}],
                    timeout=2400,
                ),
            ],
            command,
            inspection,
        )

    if app_type == "java_maven":
        wrapper = _has(inspection, "mvnw")
        if wrapper:
            command = "./mvnw -B clean package"
            image = templates._JDK_IMAGE
        elif attempted:
            command = "mvn -B clean package"
            image = _MAVEN_IMAGE
        else:
            command = (
                "if [ -x ./mvnw ]; then ./mvnw -B clean package; "
                "else mvn -B clean package; fi"
            )
            image = _MAVEN_IMAGE
        return _result(
            app_type,
            [
                _checkout(),
                _stage(
                    "Build",
                    [command],
                    image=image,
                    labels=["linux", "java"],
                    artifacts=[
                        {"path": "target/*.jar", "type": "jar"},
                        {"path": "target/*.war", "type": "war"},
                    ],
                    timeout=2400,
                ),
            ],
            command,
            inspection,
        )

    if app_type == "node":
        package = _package_json(inspection)
        has_build = isinstance(package.get("scripts"), dict) and bool(
            package["scripts"].get("build")
        )
        if _has(inspection, "pnpm-lock.yaml"):
            manager = "pnpm"
            install = "corepack pnpm install --frozen-lockfile"
            build = "corepack pnpm run build" if has_build else 'echo "No build script; install completed."'
        elif _has(inspection, "yarn.lock"):
            manager = "yarn"
            install = "corepack yarn install --frozen-lockfile"
            build = "corepack yarn build" if has_build else 'echo "No build script; install completed."'
        elif attempted:
            manager = "npm"
            install = "npm ci" if _has(inspection, "package-lock.json") else "npm install"
            build = "npm run build" if has_build else "npm run build --if-present"
        else:
            manager = "lockfile detection"
            install = (
                "if [ -f pnpm-lock.yaml ]; then corepack pnpm install --frozen-lockfile; "
                "elif [ -f yarn.lock ]; then corepack yarn install --frozen-lockfile; "
                "elif [ -f package-lock.json ]; then npm ci; else npm install; fi"
            )
            build = (
                "if node -e \"const p=require('./package.json'); "
                "process.exit(p.scripts&&p.scripts.build?0:1)\"; then "
                "if [ -f pnpm-lock.yaml ]; then corepack pnpm run build; "
                "elif [ -f yarn.lock ]; then corepack yarn run build; "
                "else npm run build; fi; "
                "else echo 'No build script; install completed.'; fi"
            )
        return _result(
            app_type,
            [
                _checkout(),
                _stage("Install", [install], image=templates._NODE_IMAGE, labels=["linux", "node"]),
                _stage(
                    "Build",
                    [build],
                    image=templates._NODE_IMAGE,
                    labels=["linux", "node"],
                    artifacts=[
                        {"path": "dist/**", "type": "zip"},
                        {"path": "build/**", "type": "zip"},
                    ],
                ),
            ],
            f"{manager}: {install}; {build}",
            inspection,
        )

    if app_type == "python":
        if _has(inspection, "requirements.txt"):
            install = "python -m pip install --no-cache-dir -r requirements.txt"
        elif _has(inspection, "pyproject.toml") or _has(inspection, "setup.py"):
            install = "python -m pip install --no-cache-dir ."
        else:
            install = (
                "if [ -f requirements.txt ]; then python -m pip install --no-cache-dir -r requirements.txt; "
                "elif [ -f pyproject.toml ] || [ -f setup.py ]; then python -m pip install --no-cache-dir .; "
                "else echo 'No Python dependency manifest found.'; fi"
            )
        package = (
            "if [ -f pyproject.toml ] || [ -f setup.py ]; then "
            "python -m pip wheel --no-deps --wheel-dir dist .; "
            "else python -m compileall -q .; fi"
        )
        return _result(
            app_type,
            [
                _checkout(),
                _stage("Install", [install], image=templates._PYTHON_IMAGE, labels=["linux", "python"]),
                _stage(
                    "Package",
                    [package],
                    image=templates._PYTHON_IMAGE,
                    labels=["linux", "python"],
                    artifacts=[{"path": "dist/*", "type": "binary"}],
                ),
            ],
            f"{install}; {package}",
            inspection,
        )

    if app_type == "android":
        tool = "./gradlew" if _has(inspection, "gradlew") else "gradle"
        if not attempted:
            tool = '$(if [ -x ./gradlew ]; then printf ./gradlew; else printf gradle; fi)'
        command = f"{tool} --no-daemon assembleDebug bundleDebug"
        return _result(
            app_type,
            [
                _checkout(labels=["linux", "android"]),
                _stage(
                    "Build Android",
                    [command],
                    image=_ANDROID_IMAGE,
                    labels=["linux", "android"],
                    artifacts=[
                        {"path": "**/build/outputs/apk/debug/*.apk", "type": "apk"},
                        {"path": "**/build/outputs/bundle/debug/*.aab", "type": "aab"},
                    ],
                    timeout=3600,
                ),
            ],
            command,
            inspection,
        )

    if app_type == "flutter":
        command = "flutter pub get && flutter build apk --debug"
        return _result(
            app_type,
            [
                _checkout(labels=["linux", "flutter"]),
                _stage(
                    "Build Flutter",
                    ["flutter pub get", "flutter build apk --debug"],
                    image=_FLUTTER_IMAGE,
                    labels=["linux", "flutter"],
                    artifacts=[{"path": "build/app/outputs/flutter-apk/*.apk", "type": "apk"}],
                    timeout=3600,
                ),
            ],
            command,
            inspection,
        )

    if app_type == "container":
        dockerfile = "dockerfile" if _has(inspection, "dockerfile") else "Dockerfile"
        return _result(
            app_type,
            [
                _checkout(),
                {
                    "name": "Build Image",
                    "stageType": "container_image",
                    "runnerLabels": ["linux"],
                    "commands": [],
                    "env": {"DOCKERFILE_PATH": dockerfile},
                    "timeoutSeconds": 2400,
                },
            ],
            f"Build container image from {dockerfile}",
            inspection,
        )

    if app_type == "ios":
        command = (
            "workspace=$(find . -maxdepth 2 -name '*.xcworkspace' -print -quit); "
            "project=$(find . -maxdepth 2 -name '*.xcodeproj' -print -quit); "
            "if [ -n \"$workspace\" ]; then name=$(basename \"$workspace\" .xcworkspace); "
            "xcodebuild -workspace \"$workspace\" -scheme \"${IOS_SCHEME:-$name}\" "
            "-configuration Debug -sdk iphonesimulator -derivedDataPath build/DerivedData "
            "CODE_SIGNING_ALLOWED=NO build; "
            "elif [ -n \"$project\" ]; then name=$(basename \"$project\" .xcodeproj); "
            "xcodebuild -project \"$project\" -scheme \"${IOS_SCHEME:-$name}\" "
            "-configuration Debug -sdk iphonesimulator -derivedDataPath build/DerivedData "
            "CODE_SIGNING_ALLOWED=NO build; "
            "else echo 'No Xcode workspace or project found.'; exit 1; fi"
        )
        parameters = [{
            "name": "IOS_SCHEME",
            "type": "text",
            "label": "Xcode scheme",
            "description": "Optional; defaults to the detected workspace or project name.",
            "default": "",
            "required": False,
        }]
        return _result(
            app_type,
            [
                _checkout(labels=["macos", "xcode"], runner_type="agent_macos"),
                _stage(
                    "Build iOS",
                    [command],
                    labels=["macos", "xcode"],
                    runner_type="agent_macos",
                    artifacts=[{"path": "build/**/*.app", "type": "binary"}],
                    timeout=3600,
                ),
            ],
            "Detect .xcworkspace/.xcodeproj and run an unsigned simulator build",
            inspection,
            parameters=parameters,
        )

    return _result(
        "generic",
        [],
        "No command is guessed for Custom services",
        inspection,
        requires_customization=True,
    )


def for_service(service, revision: str = "") -> Dict[str, Any]:
    inspection = inspect_repository(service, revision)
    return resolve_default_pipeline(service.application_type, inspection)


def is_available(application_type: str) -> bool:
    return _type(application_type) != "generic"


def stage_count(application_type: str) -> int:
    return len(resolve_default_pipeline(application_type)["stages"])
