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
    # Carried, because a build here genuinely needs them: the internal Nexus and
    # the registry are reachable only by explicit address, and an example that
    # dropped them would teach a pipeline that cannot resolve its dependencies.
    "hostAliases",
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


# The house pattern — a pipeline that is RUNNING here, not one reconstructed
# from a Jenkinsfile. Three stages, which is the first surprise: the Jenkins job
# it replaced had eight, and five of those were deploys, notifications and
# file-patching that a build has no business doing.
#
# Every detail below is a convention no repository reveals and no model guesses:
#
#   * dependencies come from the internal Maven mirror, reached through a Gradle
#     INIT SCRIPT written at build time — not from gradle.properties, and not
#     with the credentials inline. The init script reads them from the
#     environment, which is what secretRefs put there.
#   * the image carries Gradle itself, so the command is `gradle`, not
#     `./gradlew` — these projects do not all ship a wrapper.
#   * the JAR is normalised to app.jar, excluding -plain, -sources and -javadoc,
#     and failing loudly when there is none, so one Dockerfile serves every
#     service.
#   * `docker build` + `docker push` is NOT a command stage. It is what the
#     container_image stage is for, and a build pod has no Docker socket.
#   * the build needs host aliases to resolve the internal Nexus at all.
#
# Host addresses are deliberately not written here — they differ per site and
# arrive with the repository's own Jenkinsfile, or with the real saved pipelines
# this module prefers over this one.
_HOUSE_JAVA_GRADLE: Dict[str, Any] = {
    "name": "default",
    "parameters": [],
    "stages": [
        {
            "name": "Checkout",
            "stageType": "checkout",
            "runnerType": "kubernetes",
            "runnerLabels": ["linux"],
            "timeoutSeconds": 600,
        },
        {
            "name": "Build JAR",
            "stageType": "command",
            "buildEnvironment": "gradle-9-jdk25",
            "runnerType": "kubernetes",
            "runnerLabels": ["linux", "java"],
            # Named, never valued. The plaintext is injected as environment at
            # dispatch and masked out of the log; the init script below reads it
            # with System.getenv rather than embedding it.
            "secretRefs": [
                {"name": "NEXUS_USER", "envVar": "NEXUS_USER"},
                {"name": "NEXUS_PASSWORD", "envVar": "NEXUS_PASSWORD"},
            ],
            "commands": [
                'init="$KUBESIGHT_WORKSPACE/nexus-init.gradle"',
                "cat > \"$init\" <<'EOF'",
                "allprojects {",
                "  repositories {",
                "    maven {",
                '      url "https://registry.areeba.com:4443/repository/maven-public/"',
                "      credentials {",
                '        username System.getenv("NEXUS_USER")',
                '        password System.getenv("NEXUS_PASSWORD")',
                "      }",
                "    }",
                "  }",
                "}",
                "EOF",
                'gradle -I "$init" clean build -x test -x checkstyleMain '
                "-x checkstyleTest -x compileTestJava --stacktrace",
                "jar=$(ls -1 build/libs/*.jar 2>/dev/null | "
                "grep -Ev -- '-(plain|sources|javadoc)\\.jar$' | head -n 1) || true",
                'if [ -z "$jar" ]; then echo "No runnable jar in build/libs:"; '
                "ls -l build/libs || true; exit 1; fi",
                'mv "$jar" ./app.jar',
                'echo "Packaged $jar as app.jar"',
            ],
            "artifacts": [{"path": "app.jar", "type": "jar"}],
            "timeoutSeconds": 2400,
        },
        {
            "name": "Build Image",
            "stageType": "container_image",
            "runnerType": "kubernetes",
            "runnerLabels": ["linux"],
            "timeoutSeconds": 2400,
        },
    ],
}

_HOUSE_EXAMPLES: Dict[str, Dict[str, Any]] = {
    "java_gradle": _HOUSE_JAVA_GRADLE,
    "java_maven": _HOUSE_JAVA_GRADLE,
    "java": _HOUSE_JAVA_GRADLE,
}


# The inputs the house pipeline's secret references depend on. Shown WITH it,
# because the pairing is the lesson: a stage may reference a secret that does
# not exist yet precisely because the proposal asks the user for it in the same
# breath. A secretRefs entry with no matching requiredInput is a dangling
# reference, and seeing the two together is what prevents that.
_HOUSE_REQUIRED_INPUTS = [
    {
        "name": "NEXUS_USER",
        "kind": "secret",
        "required": True,
        "label": "Nexus username",
        "reason": "Dependencies resolve through the internal Maven mirror.",
    },
    {
        "name": "NEXUS_PASSWORD",
        "kind": "secret",
        "required": True,
        "label": "Nexus password",
        "reason": "Password or token for the Nexus reader account.",
    },
]


def _from_house(application_type: str) -> Dict[str, Any]:
    kit = _HOUSE_EXAMPLES[application_type]
    return {
        "source": (
            "a pipeline currently running in this KubeSight — the conventions "
            "this organisation's builds actually follow"
        ),
        "applicationType": application_type,
        "pipeline": kit,
        "requiredInputs": _HOUSE_REQUIRED_INPUTS,
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
