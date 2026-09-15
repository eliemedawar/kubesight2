"""Starter kits per application type.

A registered service should be one click from a runnable pipeline, not a blank
stage editor. The application type is the one thing we ask for at registration,
so it is what everything predefinable hangs off:

    stages           the ordered pipeline
    parameters       what the Run Build dialog asks, wired to stage conditions
    dockerfile       the image recipe, for types whose pipeline builds an image
    expectedSecrets  the secret KEYS a build of this kind needs (never values)

These are ordinary definitions in the same shape the API accepts — an operator
can edit every field afterwards, and nothing in the engine treats a templated
stage differently from a hand-written one.

Two deliberate omissions:

* **No stage carries ``secretRefs``.** A reference to a secret that does not
  exist yet is rejected when the pipeline saves (``pipelines._secret_refs``),
  so a template that referenced one would make registering a service fail.
  ``expectedSecrets`` names them instead, and the Secrets panel offers them.
* **No Dockerfile where the repository owns one.** ``container`` exists
  precisely to build the repository's own Dockerfile, and the mobile types
  produce an APK/AAB/IPA rather than an image. Seeding a Dockerfile for those
  would override or invent something nobody asked for.

Stages that are not yet executable (``container_image`` without BuildKit) are
included where the application type implies them, so the pipeline reads as the
intended flow from day one. The engine skips a stage type it has no executor
for and says so in the stage log rather than failing the build.
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

# Run conditions are compared against the build's variables as strings, which is
# what a boolean parameter stores and what a stage's environment receives.
_UNLESS_SKIP_TESTS = {"variable": "SKIP_TESTS", "operator": "equals", "value": "false"}


def _flag(name: str, label: str, description: str, *, default: bool = False) -> Dict[str, Any]:
    """A boolean build parameter.

    Only ever defined alongside the stage condition that reads it — a parameter
    nothing consumes is a question with no consequence, which teaches people to
    ignore the dialog.
    """
    return {
        "name": name,
        "type": "boolean",
        "label": label,
        "description": description,
        "default": "true" if default else "false",
    }


_SKIP_TESTS = _flag(
    "SKIP_TESTS",
    "Skip tests",
    "Run the build without its test stage. The stage is closed as skipped, "
    "with the reason in its log.",
)


def _secret(key: str, description: str) -> Dict[str, str]:
    return {"key": key, "description": description}


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
    run_condition: Dict[str, str] | None = None,
) -> Dict[str, Any]:
    stage: Dict[str, Any] = {
        "name": name,
        "stageType": stage_type,
        "image": image,
        "runnerLabels": labels or ["linux"],
        "commands": commands,
        "artifacts": artifacts or [],
        "timeoutSeconds": timeout,
        "continueOnFailure": continue_on_failure,
    }
    if run_condition:
        stage["runCondition"] = dict(run_condition)
    return stage


# ---------------------------------------------------------------------------
# Dockerfiles
#
# Every one of these is a RUNTIME image, not a rebuild. The build context is the
# checkout, and by the time the container_image stage runs the earlier stages
# have already compiled into that same directory — so the Dockerfile copies what
# the pipeline produced instead of compiling a second time in a second place.
#
# They run as a non-root UID because the target clusters enforce restricted Pod
# Security; an image that only works as root is rejected at admission, long
# after the build went green.
# ---------------------------------------------------------------------------

_JAVA_DOCKERFILE = """\
# Runtime image for the JAR the Package stage produced.
# The build context is the checkout, so target/ is already populated.
FROM eclipse-temurin:21-jre-alpine

WORKDIR /app

# Assumes the build produces exactly one JAR under target/. If yours also
# produces a sources or javadoc JAR, name the one you want instead of the glob.
COPY target/*.jar /app/app.jar

# Restricted Pod Security rejects containers that need root.
USER 65532:65532

EXPOSE 8080
ENTRYPOINT ["java", "-jar", "/app/app.jar"]
"""

_NODE_DOCKERFILE = """\
# Runtime image for what the Build stage produced.
# dist/ and node_modules/ are already in the checkout by this point, so nothing
# is fetched here — the image builds with no access to a package registry.
FROM node:22-alpine

WORKDIR /app
ENV NODE_ENV=production

COPY package.json ./
COPY node_modules/ ./node_modules/
COPY dist/ ./dist/

# node_modules carries dev dependencies too, because the Install stage needed
# them to build and test. To slim the image, add `npm prune --omit=dev` as the
# last command of the Build stage.
USER node

EXPOSE 3000
CMD ["node", "dist/main.js"]
"""

_PYTHON_DOCKERFILE = """\
# Runtime image for the checked-out application.
# Unlike the JVM and Node templates this one installs its dependencies here:
# the Install stage put them in that container's site-packages, not in the
# workspace, so there is nothing to copy across.
FROM python:3.12-slim

WORKDIR /app
ENV PYTHONDONTWRITEBYTECODE=1 \\
    PYTHONUNBUFFERED=1

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Restricted Pod Security rejects containers that need root.
USER 65532:65532

EXPOSE 8000
CMD ["python", "-m", "app"]
"""


TEMPLATES: Dict[str, Dict[str, Any]] = {
    "java": {
        "label": "Java / Maven",
        "description": "Compile, test, package a JAR, then build a container image.",
        "dockerfile": _JAVA_DOCKERFILE,
        "parameters": [_SKIP_TESTS],
        "expectedSecrets": [
            _secret(
                "NEXUS_USERNAME",
                "Reader account for the Maven mirror, when the build resolves "
                "dependencies through Nexus rather than Maven Central.",
            ),
            _secret("NEXUS_PASSWORD", "Password or token for NEXUS_USERNAME."),
        ],
        "stages": [
            _CHECKOUT,
            _stage("Build", ["mvn -B -DskipTests clean package"],
                   image="maven:3.9-eclipse-temurin-21", labels=["linux", "java21"]),
            _stage("Unit Tests", ["mvn -B test"],
                   image="maven:3.9-eclipse-temurin-21", labels=["linux", "java21"],
                   artifacts=[{"path": "target/surefire-reports/*.xml", "type": "test-report"}],
                   run_condition=_UNLESS_SKIP_TESTS),
            _stage("Package", ["ls -1 target/*.jar"],
                   image="maven:3.9-eclipse-temurin-21", labels=["linux", "java21"],
                   artifacts=[{"path": "target/*.jar", "type": "jar"}]),
            _stage("Build Image", [], stage_type="container_image", labels=["linux"]),
        ],
    },
    "node": {
        "label": "Node.js",
        "description": "Install, test, build, then build a container image.",
        "dockerfile": _NODE_DOCKERFILE,
        "parameters": [_SKIP_TESTS],
        "expectedSecrets": [
            _secret(
                "NPMRC",
                "The .npmrc body, when packages come from a private registry. "
                "Write it out in a stage before Install.",
            ),
        ],
        "stages": [
            _CHECKOUT,
            _stage("Install", ["npm ci"], image="node:22-alpine", labels=["linux", "node"]),
            _stage("Unit Tests", ["npm test --if-present"],
                   image="node:22-alpine", labels=["linux", "node"],
                   run_condition=_UNLESS_SKIP_TESTS),
            _stage("Build", ["npm run build --if-present"],
                   image="node:22-alpine", labels=["linux", "node"],
                   artifacts=[{"path": "dist/**", "type": "zip"}]),
            _stage("Build Image", [], stage_type="container_image", labels=["linux"]),
        ],
    },
    "python": {
        "label": "Python",
        "description": "Install dependencies, run pytest, then build a container image.",
        "dockerfile": _PYTHON_DOCKERFILE,
        "parameters": [_SKIP_TESTS],
        "expectedSecrets": [
            _secret(
                "PIP_INDEX_URL",
                "Full index URL including credentials, when packages come from "
                "a private index rather than PyPI.",
            ),
        ],
        "stages": [
            _CHECKOUT,
            _stage("Install", ["pip install --no-cache-dir -r requirements.txt"],
                   image="python:3.12-slim", labels=["linux", "python"]),
            _stage("Unit Tests", ["pytest -q"],
                   image="python:3.12-slim", labels=["linux", "python"],
                   artifacts=[{"path": "junit.xml", "type": "test-report"}],
                   run_condition=_UNLESS_SKIP_TESTS),
            _stage("Build Image", [], stage_type="container_image", labels=["linux"]),
        ],
    },
    "container": {
        "label": "Container application",
        "description": "Check out the repository and build a container image.",
        # No Dockerfile: this type exists to build the one in the repository.
        # Seeding an inline recipe here would silently take its place.
        "dockerfile": "",
        "parameters": [],
        "expectedSecrets": [],
        "stages": [
            _CHECKOUT,
            _stage("Build Image", [], stage_type="container_image", labels=["linux"]),
        ],
    },
    "android": {
        "label": "Android",
        "description": "Gradle dependencies, tests, then an APK and an AAB.",
        # Produces an APK/AAB, not an image.
        "dockerfile": "",
        "parameters": [
            _SKIP_TESTS,
            _flag("BUILD_APK", "Build APK", "Assemble the release APK.", default=True),
            _flag("BUILD_AAB", "Build AAB", "Bundle the release AAB for Play.", default=True),
        ],
        "expectedSecrets": [
            _secret(
                "ANDROID_KEYSTORE_B64",
                "base64 of the release keystore (`base64 -w0 app.keystore`).",
            ),
            _secret("ANDROID_STORE_PASS", "Keystore password."),
            _secret("ANDROID_KEY_ALIAS", "Signing key alias inside the keystore."),
            _secret("ANDROID_KEY_PASS", "Password for that key alias."),
        ],
        "stages": [
            _CHECKOUT,
            _stage("Dependencies", ["./gradlew --no-daemon dependencies"],
                   labels=["linux", "android"], timeout=2400),
            _stage("Unit Tests", ["./gradlew --no-daemon test"],
                   labels=["linux", "android"], timeout=2400,
                   run_condition=_UNLESS_SKIP_TESTS),
            # Split so each half can be turned off from the Run Build dialog —
            # one Gradle invocation doing both could only be skipped wholesale.
            _stage("Assemble APK", ["./gradlew --no-daemon assembleRelease"],
                   labels=["linux", "android"], timeout=3600,
                   artifacts=[{"path": "app/build/outputs/apk/release/*.apk", "type": "apk"}],
                   run_condition={"variable": "BUILD_APK", "operator": "equals", "value": "true"}),
            _stage("Assemble AAB", ["./gradlew --no-daemon bundleRelease"],
                   labels=["linux", "android"], timeout=3600,
                   artifacts=[{"path": "app/build/outputs/bundle/release/*.aab", "type": "aab"}],
                   run_condition={"variable": "BUILD_AAB", "operator": "equals", "value": "true"}),
        ],
    },
    "ios": {
        "label": "iOS",
        "description": "Runs on a macOS runner: dependencies, tests, archive, export IPA.",
        # Produces an IPA, not an image.
        "dockerfile": "",
        "parameters": [_SKIP_TESTS],
        "expectedSecrets": [
            _secret("IOS_P12_B64", "base64 of the signing certificate (.p12)."),
            _secret("IOS_P12_PASSWORD", "Password for that .p12."),
            _secret("IOS_PROVISIONING_PROFILE_B64", "base64 of the .mobileprovision."),
        ],
        "stages": [
            _CHECKOUT,
            _stage("Dependencies", ["pod install --repo-update"],
                   labels=["macos", "xcode"], timeout=2400),
            _stage("Unit Tests", ["xcodebuild test -scheme App -destination 'generic/platform=iOS Simulator'"],
                   labels=["macos", "xcode"], timeout=3600,
                   run_condition=_UNLESS_SKIP_TESTS),
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
        # Produces an APK, not an image.
        "dockerfile": "",
        "parameters": [_SKIP_TESTS],
        "expectedSecrets": [
            _secret("ANDROID_KEYSTORE_B64", "base64 of the release keystore, for a signed APK."),
            _secret("ANDROID_STORE_PASS", "Keystore password."),
            _secret("ANDROID_KEY_ALIAS", "Signing key alias inside the keystore."),
            _secret("ANDROID_KEY_PASS", "Password for that key alias."),
        ],
        "stages": [
            _CHECKOUT,
            _stage("Dependencies", ["flutter pub get"], labels=["linux", "flutter"]),
            _stage("Analyze", ["flutter analyze"], labels=["linux", "flutter"],
                   continue_on_failure=True),
            _stage("Unit Tests", ["flutter test"], labels=["linux", "flutter"],
                   run_condition=_UNLESS_SKIP_TESTS),
            _stage("Build APK", ["flutter build apk --release"],
                   labels=["linux", "flutter"], timeout=3600,
                   artifacts=[{"path": "build/app/outputs/flutter-apk/*.apk", "type": "apk"}]),
        ],
    },
    "generic": {
        "label": "Generic",
        "description": "Check out and run a build script.",
        # Nothing is known about what this builds, so nothing is predefined
        # beyond the one stage.
        "dockerfile": "",
        "parameters": [],
        "expectedSecrets": [],
        "stages": [
            _CHECKOUT,
            _stage("Build", ["./build.sh"], labels=["linux"]),
        ],
    },
}


def list_templates() -> List[Dict[str, Any]]:
    """Template summaries for the picker.

    Carries the full Dockerfile body: there are eight types and the largest
    recipe is under a kilobyte, so shipping them with the list saves the
    Dockerfile tab a round trip for every type the user previews.
    """
    return [
        {
            "applicationType": key,
            "label": value["label"],
            "description": value["description"],
            "stageNames": [stage["name"] for stage in value["stages"]],
            "dockerfile": value.get("dockerfile", ""),
            "parameters": deepcopy(value.get("parameters") or []),
            "expectedSecrets": deepcopy(value.get("expectedSecrets") or []),
        }
        for key, value in TEMPLATES.items()
    ]


def template_for(application_type: str) -> Dict[str, Any]:
    """The full definition for one application type, falling back to generic."""
    return TEMPLATES.get((application_type or "").strip().lower(), TEMPLATES["generic"])


def dockerfile_for(application_type: str) -> str:
    """The starter image recipe, or "" for a type that predefines none."""
    return template_for(application_type).get("dockerfile", "") or ""


def parameters_for(application_type: str) -> List[Dict[str, Any]]:
    return deepcopy(template_for(application_type).get("parameters") or [])


def expected_secrets_for(application_type: str) -> List[Dict[str, str]]:
    """The secret keys a build of this kind needs.

    Declarative on purpose: these are never written as rows. A secret with no
    value cannot be created (``secrets.create_secret`` requires one), and a
    blank placeholder would hand a stage an empty environment variable that
    looks set. The Secrets panel lists these as "expected" and offers to add
    each one properly.
    """
    return deepcopy(template_for(application_type).get("expectedSecrets") or [])


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
        "parameters": deepcopy(template.get("parameters") or []),
    }
