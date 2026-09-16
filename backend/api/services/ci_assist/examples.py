"""Complete pipelines that already work here, sent as worked examples.

Everything else in the request describes the shape of a KubeSight pipeline —
the rules, the vocabulary, an empty template to fill in. None of it shows one.
A model given a schema and no instance has to infer the conventions, and the
round trips that follow are it discovering them one rejection at a time:
``type`` for ``stageType``, a label no machine has, an image named directly.

So it is shown real ones instead. Two sources, in order of preference:

**This installation's own saved pipelines.** The strongest possible signal:
they are known to run here, on these runners, with these images, using this
site's conventions. A pipeline somebody wrote and has been building with for
months teaches more than any amount of prose about what "good" looks like.

**The starter kits** (``services/ci/templates``). Deterministic, always
present, and designed to be exemplary. They are the fallback for a fresh
installation with nothing saved yet, and one is always included as a canonical
reference even when real pipelines exist.

Only the pipeline STRUCTURE travels. Secret values cannot appear — a pipeline
stores references, never values — and the redactor runs over the result anyway.
"""

from __future__ import annotations

from typing import Any, Dict, List

from ...models_ci import CiPipeline, CiService
from ..application_intelligence_security import redact_structure
from ..ci import templates
from ..ci.serializers import pipeline_stage_to_dict

# Two real ones is enough to show the shape without crowding out the evidence.
MAX_REAL_EXAMPLES = 2
MAX_STAGES_PER_EXAMPLE = 8

# Fields an example should demonstrate. Deliberately the same set a proposal is
# allowed to use — an example carrying a field the model may not set would be
# teaching it to fail.
_EXAMPLE_STAGE_KEYS = (
    "name",
    "stageType",
    "runnerType",
    "runnerLabels",
    "workingDirectory",
    "commands",
    "env",
    "secretRefs",
    "artifacts",
    "runCondition",
    "timeoutSeconds",
    "continueOnFailure",
)


def _stage_for_example(stage: Dict[str, Any]) -> Dict[str, Any]:
    """One stage reduced to what is worth copying."""
    out: Dict[str, Any] = {}
    for key in _EXAMPLE_STAGE_KEYS:
        value = stage.get(key)
        if value in (None, "", [], {}, False):
            continue
        out[key] = value
    # An example must not show an image literal: a proposal may not set one, and
    # copying the field is exactly the mistake this file exists to prevent. The
    # environment key that WOULD have produced it is shown instead.
    out.pop("image", None)
    if stage.get("image"):
        out["buildEnvironment"] = _environment_for_image(stage["image"])
    return out


def _environment_for_image(image: str) -> str:
    """Which catalog key would have produced this image, if any."""
    from ..ci import build_environments

    for item in build_environments.catalog():
        if item["image"] and item["image"] == image:
            return item["key"]
    return ""


def _from_saved(pipeline: CiPipeline, service: CiService) -> Dict[str, Any]:
    stages = [
        _stage_for_example(pipeline_stage_to_dict(stage))
        for stage in sorted(pipeline.stages, key=lambda s: s.position)
        if stage.enabled
    ][:MAX_STAGES_PER_EXAMPLE]
    return {
        "source": "a pipeline already running in this KubeSight",
        "applicationType": service.application_type,
        "pipeline": {
            "name": "default",
            "parameters": [
                item for item in (pipeline.parameters or []) if isinstance(item, dict)
            ][:5],
            "stages": stages,
        },
    }


# The house pattern, taken from the Jenkins jobs this installation actually
# runs. Everything here is a convention a model cannot infer from a repository
# and gets wrong every time until it is shown:
#
#   * the version is not in the build file — it is composed from
#     version.properties and has to reach later stages through $KUBESIGHT_ENV
#   * the build is patched before it runs (a settings.gradle fragment, a
#     gradle.properties for the internal mirror), from build inputs
#   * the JAR is renamed to app.jar so one Dockerfile serves every service
#   * `docker build` + `docker push` is NOT a command stage — it is what the
#     container_image stage is for
#   * deploys, notifications and promotion are not part of a build
#
# Deliberately free of site-specific addresses: the host alias a particular
# repository needs arrives with its own Jenkinsfile in evidence.existingPipeline.
_HOUSE_JAVA_GRADLE: Dict[str, Any] = {
    "name": "default",
    "parameters": [
        {"name": "SKIP_TESTS", "type": "boolean", "label": "Skip tests", "default": "false"},
        {
            "name": "GRADLE_PROPERTIES",
            "type": "multiline",
            "label": "gradle.properties",
            "description": "Credentials and mirror settings for the internal Gradle repository.",
            "default": "",
        },
    ],
    "stages": [
        {"name": "Checkout", "stageType": "checkout", "runnerLabels": ["linux"],
         "timeoutSeconds": 600},
        {
            "name": "Prepare Build",
            "stageType": "command",
            "buildEnvironment": "java-jdk11",
            "runnerLabels": ["linux", "java"],
            "commands": [
                "# The version is composed from version.properties, not read from",
                "# the build file, and later stages need it — so it is exported.",
                "major=$(grep '^major=' version.properties | cut -d= -f2)",
                "minor=$(grep '^minor=' version.properties | cut -d= -f2)",
                "patch=$(grep '^patch=' version.properties | cut -d= -f2)",
                "suffix=$(grep '^suffix=' version.properties | cut -d= -f2 || true)",
                'if [ -n "$suffix" ]; then VERSION="$major.$minor.$patch-$suffix"; '
                'else VERSION="$major.$minor.$patch"; fi',
                'printf "VERSION=%s\\n" "$VERSION" >> "$KUBESIGHT_ENV"',
                'echo "Building version $VERSION"',
                "# Settings the internal mirror needs, supplied as a build input.",
                'printf "%s\\n" "${GRADLE_PROPERTIES}" > "$KUBESIGHT_WORKSPACE/gradle.properties"',
            ],
            "timeoutSeconds": 600,
        },
        {
            "name": "Build",
            "stageType": "command",
            "buildEnvironment": "java-jdk11",
            "runnerLabels": ["linux", "java"],
            "env": {"GRADLE_USER_HOME": "$KUBESIGHT_WORKSPACE/.gradle"},
            "commands": [
                'mkdir -p "$GRADLE_USER_HOME"',
                'cp "$KUBESIGHT_WORKSPACE/gradle.properties" "$GRADLE_USER_HOME/gradle.properties"',
                "./gradlew --no-daemon clean build -x test -x checkstyleMain "
                "-x checkstyleTest -x compileTestJava --stacktrace",
            ],
            "timeoutSeconds": 2400,
        },
        {
            "name": "Unit Tests",
            "stageType": "command",
            "buildEnvironment": "java-jdk11",
            "runnerLabels": ["linux", "java"],
            "commands": ["./gradlew --no-daemon test"],
            "artifacts": [{"path": "build/test-results/test/*.xml", "type": "test-report"}],
            "runCondition": {"variable": "SKIP_TESTS", "operator": "equals", "value": "false"},
            "timeoutSeconds": 2400,
        },
        {
            "name": "Package JAR",
            "stageType": "command",
            "buildEnvironment": "java-jdk11",
            "runnerLabels": ["linux", "java"],
            "commands": [
                "# One canonical app.jar, so a single Dockerfile serves every service.",
                "jar=$(ls -1 build/libs/*.jar | grep -v -- '-plain[.]jar$' | head -1)",
                'test -n "$jar" || { echo "no JAR under build/libs"; exit 1; }',
                'cp "$jar" app.jar',
            ],
            "artifacts": [{"path": "app.jar", "type": "jar"}],
            "timeoutSeconds": 600,
        },
        {
            "name": "Build Container Image",
            "stageType": "container_image",
            "runnerType": "kubernetes",
            "runnerLabels": ["linux"],
            # docker build / docker push never appear as commands: a build pod
            # has no Docker socket. BuildKit does this, and the tag can use the
            # VERSION the Prepare Build stage exported.
            "env": {"IMAGE_TAG": "V${VERSION}-prod"},
            "timeoutSeconds": 2400,
        },
    ],
}

_HOUSE_EXAMPLES: Dict[str, Dict[str, Any]] = {
    "java_gradle": _HOUSE_JAVA_GRADLE,
    "java_maven": _HOUSE_JAVA_GRADLE,
    "java": _HOUSE_JAVA_GRADLE,
}


def _from_house(application_type: str) -> Dict[str, Any]:
    kit = _HOUSE_EXAMPLES[application_type]
    return {
        "source": (
            "how this organisation builds a service — the conventions its "
            "existing Jenkins jobs follow, expressed in KubeSight's model"
        ),
        "applicationType": application_type,
        "pipeline": kit,
    }


def _from_template(application_type: str) -> Dict[str, Any]:
    kit = templates.template_for(application_type)
    stages = [
        _stage_for_example(dict(stage))
        for stage in kit["stages"][:MAX_STAGES_PER_EXAMPLE]
    ]
    return {
        "source": "the KubeSight starter kit for this application type",
        "applicationType": application_type,
        "pipeline": {
            "name": "default",
            "parameters": templates.parameters_for(application_type)[:5],
            "stages": stages,
        },
    }


def worked_examples(preferred_type: str = "") -> List[Dict[str, Any]]:
    """Pipelines to show alongside the rules.

    ``preferred_type`` biases the selection towards the kind of project being
    analysed when one is already suspected — a Gradle example is worth more to
    a Gradle repository than a Python one — without ever being the only example,
    because the shape matters more than the stack.
    """
    examples: List[Dict[str, Any]] = []

    # Real pipelines first, most recently edited, preferring the same kind of
    # application. A pipeline with no saved stages is the unsaved default and
    # teaches nothing.
    query = (
        CiPipeline.query.join(CiService, CiPipeline.service_id == CiService.id)
        .filter(CiService.status == "active")
        .order_by(CiPipeline.updated_at.desc())
    )
    candidates = [row for row in query.limit(25).all() if row.stages]
    if preferred_type:
        candidates.sort(
            key=lambda row: 0 if row.service and row.service.application_type == preferred_type else 1
        )
    seen_types = set()
    for row in candidates:
        if len(examples) >= MAX_REAL_EXAMPLES:
            break
        service = row.service
        if service is None or service.application_type in seen_types:
            continue
        seen_types.add(service.application_type)
        examples.append(_from_saved(row, service))

    # The house pattern for this kind of project, where one exists. This is the
    # example that carries the conventions — the version composed from
    # version.properties, the JAR normalised to app.jar, `docker build` being a
    # container_image stage rather than a command — none of which is inferable
    # from the repository and all of which is got wrong without it.
    if preferred_type in _HOUSE_EXAMPLES:
        examples.append(_from_house(preferred_type))
    else:
        # And otherwise one curated kit, so there is always a canonical
        # reference even for a stack with no house pattern yet.
        canonical = preferred_type if preferred_type in templates.TEMPLATES else "java_gradle"
        examples.append(_from_template(canonical))

    return redact_structure(examples)
