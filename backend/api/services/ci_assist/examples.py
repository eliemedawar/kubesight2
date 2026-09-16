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

    # And always one curated kit, so there is a canonical reference even when
    # the real ones are unusual.
    canonical = preferred_type if preferred_type in templates.TEMPLATES else "java_gradle"
    examples.append(_from_template(canonical))

    return redact_structure(examples)
