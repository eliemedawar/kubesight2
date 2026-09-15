"""The KubeSight ↔ Hermes pipeline contract, and the gate it has to pass.

Two things this module refuses to do, both deliberate.

**It does not parse prose.** A response is one JSON object with exactly these
top-level keys or it is rejected. Reading a pipeline out of a paragraph would
make every model upgrade a silent behaviour change and every ambiguity a build
that runs something nobody wrote.

**It does not repair.** Unknown keys, wrong types and bad enums fail. That is
what makes the correction loop honest: the model is told precisely what was
wrong and produces something that passes, rather than KubeSight quietly
reshaping a response until it fits and nobody learning anything.

The contract is versioned independently of the Application Intelligence prompt
(``application-intelligence-v2``) because the two evolve for different reasons
and must not be able to break each other.
"""

from __future__ import annotations

import re
from typing import Any, Dict, List

from ...models_ci import REQUIRED_INPUT_KINDS

SCHEMA_VERSION = "1.0"
CONTRACT_ID = "kubesight.ci.pipeline-plan"
PROMPT_VERSION = "ci-pipeline-plan-v1"

TOP_LEVEL_KEYS = {"schemaVersion", "applicationProfile", "pipeline", "requiredInputs", "analysis"}
PIPELINE_KEYS = {"name", "description", "parameters", "stages"}
ANALYSIS_KEYS = {"warnings", "unknown", "notes"}
REQUIRED_INPUT_KEYS = {
    "name", "kind", "required", "label", "description", "reason",
    "usedByStages", "example",
}

MAX_STAGES = 40
MAX_REQUIRED_INPUTS = 30
MAX_LIST_TEXT = 40

# What a name has to be to become an environment variable or a secret key.
# Mirrors pipelines._PARAM_NAME_RE — a required input whose name a stage cannot
# reference is a question with no answer.
_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_SECRET_NAME_RE = re.compile(r"^[A-Za-z0-9_.\-]+$")


class ContractError(ValueError):
    """A response did not match the contract. Message is user-facing."""


def _require_object(value: Any, label: str) -> Dict[str, Any]:
    if not isinstance(value, dict):
        raise ContractError(f"Hermes returned a '{label}' that is not an object.")
    return value


def _require_list(value: Any, label: str) -> List[Any]:
    if not isinstance(value, list):
        raise ContractError(f"Hermes returned a '{label}' that is not a list.")
    return value


def _text_list(value: Any, label: str) -> List[str]:
    return [
        " ".join(str(item).split())[:500]
        for item in _require_list(value, label)[:MAX_LIST_TEXT]
        if str(item or "").strip()
    ]


def response_template() -> Dict[str, Any]:
    """The exact shape a response must have, sent WITH the request.

    Handing the model the empty result it has to fill in, rather than describing
    it, is what the Application Intelligence contract does and it is the single
    biggest reason that one holds its shape.
    """
    return {
        "schemaVersion": SCHEMA_VERSION,
        "applicationProfile": {
            "language": "",
            "languageVersion": "",
            "framework": "",
            "frameworkVersion": "",
            "buildSystem": "",
            "buildSystemVersion": "",
            "usesBuildWrapper": False,
            "packageManager": None,
            "packaging": "",
            "projectStructure": "single",
            "modules": [],
            "containerization": {"type": "none", "dockerfilePath": ""},
            "testsDetected": False,
            "testFramework": "",
            "artifactPaths": [],
            "platformTargets": [],
            "evidence": {},
            "unknown": [],
        },
        "pipeline": {
            "name": "default",
            "description": "",
            "parameters": [],
            "stages": [],
        },
        "requiredInputs": [],
        "analysis": {"warnings": [], "unknown": [], "notes": []},
    }


def validate_response(payload: Any) -> Dict[str, Any]:
    """Accept a response, or say exactly why not.

    The message ends up in two places: the analysis row a person reads, and the
    repair prompt the model reads. Both need it to name the problem rather than
    describe a category of problem.
    """
    if not isinstance(payload, dict):
        raise ContractError("Hermes returned something that is not an object.")

    keys = set(payload)
    if keys != TOP_LEVEL_KEYS:
        missing = sorted(TOP_LEVEL_KEYS - keys)
        unknown = sorted(keys - TOP_LEVEL_KEYS)
        detail = []
        if missing:
            detail.append(f"missing: {', '.join(missing)}")
        if unknown:
            detail.append(f"unexpected: {', '.join(unknown)}")
        raise ContractError("Hermes response does not match the contract (" + "; ".join(detail) + ").")

    version = str(payload.get("schemaVersion") or "").strip()
    if version.split(".")[0] != SCHEMA_VERSION.split(".")[0]:
        raise ContractError(
            f"Hermes answered with contract version '{version}'; this KubeSight "
            f"speaks {SCHEMA_VERSION}."
        )

    profile = _require_object(payload["applicationProfile"], "applicationProfile")

    pipeline = _require_object(payload["pipeline"], "pipeline")
    unknown_pipeline = sorted(set(pipeline) - PIPELINE_KEYS)
    if unknown_pipeline:
        raise ContractError(
            "The proposed pipeline carries fields the contract has no place for: "
            f"{', '.join(unknown_pipeline)}."
        )
    stages = _require_list(pipeline.get("stages"), "pipeline.stages")
    if not stages:
        raise ContractError("Hermes proposed a pipeline with no stages.")
    if len(stages) > MAX_STAGES:
        raise ContractError(
            f"Hermes proposed {len(stages)} stages; the limit is {MAX_STAGES}."
        )
    for index, stage in enumerate(stages):
        if not isinstance(stage, dict):
            raise ContractError(f"Proposed stage {index + 1} is not an object.")
        if not str(stage.get("name") or "").strip():
            raise ContractError(f"Proposed stage {index + 1} has no name.")
    _require_list(pipeline.get("parameters", []), "pipeline.parameters")

    required_inputs = _require_list(payload["requiredInputs"], "requiredInputs")
    if len(required_inputs) > MAX_REQUIRED_INPUTS:
        raise ContractError(
            f"Hermes asked for {len(required_inputs)} inputs; the limit is "
            f"{MAX_REQUIRED_INPUTS}."
        )

    analysis = _require_object(payload["analysis"], "analysis")
    unknown_analysis = sorted(set(analysis) - ANALYSIS_KEYS)
    if unknown_analysis:
        raise ContractError(
            f"The analysis block carries unexpected fields: {', '.join(unknown_analysis)}."
        )

    return {
        "schemaVersion": SCHEMA_VERSION,
        "applicationProfile": profile,
        "pipeline": {
            "name": " ".join(str(pipeline.get("name") or "default").split())[:120] or "default",
            "description": " ".join(str(pipeline.get("description") or "").split())[:2000],
            "parameters": pipeline.get("parameters") or [],
            "stages": stages,
        },
        "requiredInputs": normalize_required_inputs(required_inputs),
        "analysis": {
            "warnings": _text_list(analysis.get("warnings", []), "analysis.warnings"),
            "unknown": _text_list(analysis.get("unknown", []), "analysis.unknown"),
            "notes": _text_list(analysis.get("notes", []), "analysis.notes"),
        },
    }


def normalize_required_inputs(value: Any) -> List[Dict[str, Any]]:
    """The questions a proposal still needs answered, in one shape.

    A malformed entry is dropped rather than fatal: an unusable question is
    noise, but throwing away an otherwise-valid pipeline because one of its
    prompts had a bad label would be worse. A question that genuinely matters
    and goes missing resurfaces as a validation error on the stage that needed
    it — ``undeclared_secret_ref`` exists precisely for that.
    """
    out: List[Dict[str, Any]] = []
    seen = set()
    for entry in value or []:
        if not isinstance(entry, dict):
            continue
        unknown = set(entry) - REQUIRED_INPUT_KEYS
        if unknown:
            continue
        name = " ".join(str(entry.get("name") or "").split())[:120]
        kind = str(entry.get("kind") or "").strip().lower()
        if not name or kind not in REQUIRED_INPUT_KINDS:
            continue
        if kind == "secret" and not _SECRET_NAME_RE.match(name):
            continue
        if kind == "parameter" and not _NAME_RE.match(name):
            continue
        if name in seen:
            continue
        seen.add(name)
        out.append(
            {
                "name": name,
                "kind": kind,
                "required": entry.get("required") is not False,
                "label": " ".join(str(entry.get("label") or name).split())[:160],
                "description": " ".join(str(entry.get("description") or "").split())[:500],
                "reason": " ".join(str(entry.get("reason") or "").split())[:500],
                "usedByStages": [
                    " ".join(str(item).split())[:120]
                    for item in (entry.get("usedByStages") or [])[:MAX_STAGES]
                    if str(item or "").strip()
                ],
                "example": " ".join(str(entry.get("example") or "").split())[:200],
            }
        )
    return out
