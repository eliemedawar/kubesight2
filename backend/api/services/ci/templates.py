"""Starter pipelines per application type.

A registered service should be one click from a runnable pipeline, not a blank
stage editor. These are ordinary pipeline definitions in the same shape the API
accepts — an operator can edit every field afterwards, and nothing in the engine
treats a templated stage differently from a hand-written one.

Stages that are not yet executable (``container_image``) are included where the
application type implies them, so the pipeline reads as the intended flow from
day one. The engine skips a stage type it has no executor for and says so in the
stage log rather than failing the build.
"""

from __future__ import annotations

from copy import deepcopy
from typing import Any, Dict, List

_CHECKOUT = {
    "name": "Checkout",
    "stageType": "checkout",
    "runnerLabels": ["linux"],
    "commands": [],
    "timeoutSeconds": 600,
}


def _stage(
    name: str,
    commands: List[str],
    *,
    image: str = "",
    labels: List[str] | None = None,
    stage_type: str = "command",
    artifacts: List[Dict[str, Any]] | None = None,
    timeout: int = 1800,
    continue_on_failure: bool = False,
) -> Dict[str, Any]:
    return {
        "name": name,
        "stageType": stage_type,
        "image": image,
        "runnerLabels": labels or ["linux"],
        "commands": commands,
        "artifacts": artifacts or [],
        "timeoutSeconds": timeout,
        "continueOnFailure": continue_on_failure,
    }


TEMPLATES: Dict[str, Dict[str, Any]] = {
    "java": {
        "label": "Java / Maven",
        "description": "Compile, test, package a JAR, then build a container image.",
        "stages": [
            _CHECKOUT,
            _stage("Build", ["mvn -B -DskipTests clean package"],
                   image="maven:3.9-eclipse-temurin-21", labels=["linux", "java21"]),
            _stage("Unit Tests", ["mvn -B test"],
                   image="maven:3.9-eclipse-temurin-21", labels=["linux", "java21"],
                   artifacts=[{"path": "target/surefire-reports/*.xml", "type": "test-report"}]),
            _stage("Package", ["ls -1 target/*.jar"],
                   image="maven:3.9-eclipse-temurin-21", labels=["linux", "java21"],
                   artifacts=[{"path": "target/*.jar", "type": "jar"}]),
            _stage("Build Image", [], stage_type="container_image", labels=["linux"]),
        ],
    },
    "node": {
        "label": "Node.js",
        "description": "Install, test, build, then build a container image.",
        "stages": [
            _CHECKOUT,
            _stage("Install", ["npm ci"], image="node:22-alpine", labels=["linux", "node"]),
            _stage("Unit Tests", ["npm test --if-present"],
                   image="node:22-alpine", labels=["linux", "node"]),
            _stage("Build", ["npm run build --if-present"],
                   image="node:22-alpine", labels=["linux", "node"],
                   artifacts=[{"path": "dist/**", "type": "zip"}]),
            _stage("Build Image", [], stage_type="container_image", labels=["linux"]),
        ],
    },
    "python": {
        "label": "Python",
        "description": "Install dependencies, run pytest, then build a container image.",
        "stages": [
            _CHECKOUT,
            _stage("Install", ["pip install --no-cache-dir -r requirements.txt"],
                   image="python:3.12-slim", labels=["linux", "python"]),
            _stage("Unit Tests", ["pytest -q"],
                   image="python:3.12-slim", labels=["linux", "python"],
                   artifacts=[{"path": "junit.xml", "type": "test-report"}]),
            _stage("Build Image", [], stage_type="container_image", labels=["linux"]),
        ],
    },
    "container": {
        "label": "Container application",
        "description": "Check out the repository and build a container image.",
        "stages": [
            _CHECKOUT,
            _stage("Build Image", [], stage_type="container_image", labels=["linux"]),
        ],
    },
    "android": {
        "label": "Android",
        "description": "Gradle dependencies, tests, then an APK and an AAB.",
        "stages": [
            _CHECKOUT,
            _stage("Dependencies", ["./gradlew --no-daemon dependencies"],
                   labels=["linux", "android"], timeout=2400),
            _stage("Unit Tests", ["./gradlew --no-daemon test"],
                   labels=["linux", "android"], timeout=2400),
            _stage("Assemble", ["./gradlew --no-daemon assembleRelease bundleRelease"],
                   labels=["linux", "android"], timeout=3600,
                   artifacts=[
                       {"path": "app/build/outputs/apk/release/*.apk", "type": "apk"},
                       {"path": "app/build/outputs/bundle/release/*.aab", "type": "aab"},
                   ]),
        ],
    },
    "ios": {
        "label": "iOS",
        "description": "Runs on a macOS runner: dependencies, tests, archive, export IPA.",
        "stages": [
            _CHECKOUT,
            _stage("Dependencies", ["pod install --repo-update"],
                   labels=["macos", "xcode"], timeout=2400),
            _stage("Unit Tests", ["xcodebuild test -scheme App -destination 'generic/platform=iOS Simulator'"],
                   labels=["macos", "xcode"], timeout=3600),
            _stage("Archive", ["xcodebuild archive -scheme App -archivePath build/App.xcarchive"],
                   labels=["macos", "xcode"], timeout=3600),
            _stage("Export IPA",
                   ["xcodebuild -exportArchive -archivePath build/App.xcarchive "
                    "-exportPath build/ipa -exportOptionsPlist ExportOptions.plist"],
                   labels=["macos", "xcode"], timeout=1800,
                   artifacts=[{"path": "build/ipa/*.ipa", "type": "ipa"}]),
        ],
    },
    "flutter": {
        "label": "Flutter",
        "description": "Pub get, analyze, test, then build an APK.",
        "stages": [
            _CHECKOUT,
            _stage("Dependencies", ["flutter pub get"], labels=["linux", "flutter"]),
            _stage("Analyze", ["flutter analyze"], labels=["linux", "flutter"],
                   continue_on_failure=True),
            _stage("Unit Tests", ["flutter test"], labels=["linux", "flutter"]),
            _stage("Build APK", ["flutter build apk --release"],
                   labels=["linux", "flutter"], timeout=3600,
                   artifacts=[{"path": "build/app/outputs/flutter-apk/*.apk", "type": "apk"}]),
        ],
    },
    "generic": {
        "label": "Generic",
        "description": "Check out and run a build script.",
        "stages": [
            _CHECKOUT,
            _stage("Build", ["./build.sh"], labels=["linux"]),
        ],
    },
}


def list_templates() -> List[Dict[str, Any]]:
    """Template summaries for the picker."""
    return [
        {
            "applicationType": key,
            "label": value["label"],
            "description": value["description"],
            "stageNames": [stage["name"] for stage in value["stages"]],
        }
        for key, value in TEMPLATES.items()
    ]


def template_for(application_type: str) -> Dict[str, Any]:
    """The full definition for one application type, falling back to generic."""
    return TEMPLATES.get((application_type or "").strip().lower(), TEMPLATES["generic"])


def default_pipeline_payload(application_type: str) -> Dict[str, Any]:
    """A ready-to-save pipeline payload for a newly registered service."""
    template = template_for(application_type)
    return {
        "name": "default",
        "description": template["description"],
        "isDefault": True,
        "enabled": True,
        # Deep-copied: the stage dicts hold nested lists, so a shallow copy
        # would let one service's edits mutate the module-level template every
        # future service is built from.
        "stages": deepcopy(template["stages"]),
    }
