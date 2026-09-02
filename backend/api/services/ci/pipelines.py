"""Pipeline definitions: CRUD, validation, and template instantiation.

A pipeline is saved as a whole — name, flags, and the complete ordered stage
list in one request — matching how ``service_blueprint_service`` saves a
blueprint with its components. Per-stage endpoints would make reordering a
multi-request transaction the UI would have to get right on every drag.

Validation is strict about the two things that can hurt: a stage may not
reference a secret that does not exist, and a stage's declared runner type must
be one KubeSight knows. Everything else is normalized rather than rejected.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

from ...audit import log_audit
from ...db import db
from ...models_ci import (
    RUNNER_TYPES,
    STAGE_TYPES,
    CiPipeline,
    CiPipelineStage,
    CiSecret,
    CiService,
)
from . import templates
from .serializers import pipeline_to_dict

MAX_STAGES = 40
MAX_COMMANDS_PER_STAGE = 100
MAX_COMMAND_CHARS = 4000
MIN_TIMEOUT_SECONDS = 30
MAX_TIMEOUT_SECONDS = 24 * 3600


class PipelineError(ValueError):
    """A pipeline definition was rejected. Message is user-facing."""


def _clean(value: Any, limit: int) -> str:
    return " ".join(str(value or "").split())[:limit]


def _string_list(value: Any, *, limit: int, item_limit: int) -> List[str]:
    if isinstance(value, str):
        items = [line for line in value.splitlines()]
    elif isinstance(value, (list, tuple)):
        items = list(value)
    else:
        items = []
    out: List[str] = []
    for item in items[:limit]:
        text = str(item or "").strip()
        if text:
            out.append(text[:item_limit])
    return out


def _label_list(value: Any) -> List[str]:
    seen: List[str] = []
    for item in _string_list(value, limit=20, item_limit=64):
        label = item.strip().lower()
        if label and label not in seen:
            seen.append(label)
    return seen


def _env_map(value: Any) -> Dict[str, str]:
    if not isinstance(value, dict):
        return {}
    out: Dict[str, str] = {}
    for key, raw in list(value.items())[:100]:
        name = _clean(key, 128)
        if name:
            out[name] = str(raw if raw is not None else "")[:4000]
    return out


def _secret_refs(value: Any, known_keys: set) -> List[Dict[str, str]]:
    """Normalize ``[{name, envVar}]`` and reject unknown secret names.

    Failing here rather than at build time means a pipeline that references a
    deleted secret is caught while someone is looking at the editor.
    """
    if not isinstance(value, (list, tuple)):
        return []
    out: List[Dict[str, str]] = []
    for item in value[:50]:
        if isinstance(item, str):
            item = {"name": item}
        if not isinstance(item, dict):
            continue
        name = _clean(item.get("name"), 120)
        if not name:
            continue
        if name not in known_keys:
            raise PipelineError(
                f"Stage references secret '{name}', which is not defined for this service."
            )
        out.append({"name": name, "envVar": _clean(item.get("envVar"), 128) or name})
    return out


def _artifact_specs(value: Any) -> List[Dict[str, Any]]:
    if not isinstance(value, (list, tuple)):
        return []
    out: List[Dict[str, Any]] = []
    for item in value[:20]:
        if not isinstance(item, dict):
            continue
        path = _clean(item.get("path"), 512)
        if not path:
            continue
        entry: Dict[str, Any] = {"path": path, "type": _clean(item.get("type"), 32) or "binary"}
        name = _clean(item.get("name"), 255)
        if name:
            entry["name"] = name
        out.append(entry)
    return out


def _resources(value: Any) -> Optional[Dict[str, str]]:
    if not isinstance(value, dict):
        return None
    out = {}
    for key in ("cpu", "memory", "ephemeralStorage"):
        raw = _clean(value.get(key), 32)
        if raw:
            out[key] = raw
    return out or None


def _known_secret_keys(service_id: int) -> set:
    rows = CiSecret.query.filter(
        db.or_(CiSecret.service_id == service_id, CiSecret.scope == "global")
    ).all()
    return {row.key for row in rows}


def normalize_stage(payload: Dict[str, Any], position: int, known_keys: set) -> Dict[str, Any]:
    """Validate and normalize one stage payload into model kwargs."""
    name = _clean(payload.get("name"), 120)
    if not name:
        raise PipelineError(f"Stage {position + 1} needs a name.")

    stage_type = _clean(payload.get("stageType"), 32).lower() or "command"
    if stage_type not in STAGE_TYPES:
        raise PipelineError(
            f"Stage '{name}' has an unknown type '{stage_type}'. "
            f"Supported: {', '.join(STAGE_TYPES)}."
        )

    runner_type = _clean(payload.get("runnerType"), 24).lower() or None
    if runner_type and runner_type not in RUNNER_TYPES:
        raise PipelineError(
            f"Stage '{name}' targets an unknown runner type '{runner_type}'."
        )

    commands = _string_list(
        payload.get("commands"), limit=MAX_COMMANDS_PER_STAGE, item_limit=MAX_COMMAND_CHARS
    )
    if stage_type == "command" and not commands:
        raise PipelineError(f"Stage '{name}' is a command stage but has no commands.")

    timeout = payload.get("timeoutSeconds")
    try:
        timeout = int(timeout) if timeout not in (None, "") else 1800
    except (TypeError, ValueError):
        raise PipelineError(f"Stage '{name}' has an invalid timeout.")
    if not MIN_TIMEOUT_SECONDS <= timeout <= MAX_TIMEOUT_SECONDS:
        raise PipelineError(
            f"Stage '{name}' timeout must be between {MIN_TIMEOUT_SECONDS} seconds "
            f"and {MAX_TIMEOUT_SECONDS // 3600} hours."
        )

    return {
        "position": position,
        "name": name,
        "stage_type": stage_type,
        "runner_type": runner_type,
        "runner_labels": _label_list(payload.get("runnerLabels")),
        "image": _clean(payload.get("image"), 512) or None,
        "working_directory": _clean(payload.get("workingDirectory"), 512) or None,
        "commands": commands,
        "env": _env_map(payload.get("env")),
        "secret_refs": _secret_refs(payload.get("secretRefs"), known_keys),
        "artifacts": _artifact_specs(payload.get("artifacts")),
        "resources": _resources(payload.get("resources")),
        "timeout_seconds": timeout,
        "continue_on_failure": bool(payload.get("continueOnFailure")),
        "parallel_group": _clean(payload.get("parallelGroup"), 64) or None,
        "enabled": payload.get("enabled") is not False,
    }


def _apply_stages(pipeline: CiPipeline, stage_payloads: List[Dict[str, Any]]) -> None:
    if len(stage_payloads) > MAX_STAGES:
        raise PipelineError(f"A pipeline may not exceed {MAX_STAGES} stages.")
    known_keys = _known_secret_keys(pipeline.service_id)
    normalized = [
        normalize_stage(payload if isinstance(payload, dict) else {}, index, known_keys)
        for index, payload in enumerate(stage_payloads)
    ]
    names = [stage["name"].lower() for stage in normalized]
    duplicates = {name for name in names if names.count(name) > 1}
    if duplicates:
        raise PipelineError(
            f"Stage names must be unique: {', '.join(sorted(duplicates))} is repeated."
        )

    # Full replace. Stage ids are not stable across a save, which is why builds
    # snapshot their pipeline rather than pointing at live stage rows.
    pipeline.stages.clear()
    db.session.flush()
    for kwargs in normalized:
        pipeline.stages.append(CiPipelineStage(**kwargs))


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def list_pipelines(service_id: int) -> List[Dict[str, Any]]:
    rows = (
        CiPipeline.query.filter_by(service_id=service_id)
        .order_by(CiPipeline.is_default.desc(), CiPipeline.id.asc())
        .all()
    )
    return [pipeline_to_dict(row) for row in rows]


def get_pipeline(pipeline_id: int) -> CiPipeline:
    row = db.session.get(CiPipeline, int(pipeline_id))
    if row is None:
        raise LookupError("Pipeline not found.")
    return row


def create_pipeline(
    service: CiService, payload: Dict[str, Any], *, actor=None
) -> Dict[str, Any]:
    name = _clean(payload.get("name"), 120) or "default"
    if CiPipeline.query.filter_by(service_id=service.id, name=name).first():
        raise PipelineError(f"This service already has a pipeline named '{name}'.")

    is_default = payload.get("isDefault")
    pipeline = CiPipeline(
        service_id=service.id,
        name=name,
        description=_clean(payload.get("description"), 2000) or None,
        # The first pipeline is the default whatever the payload says, so a
        # service can never end up with pipelines and no default.
        is_default=bool(is_default) or not service.pipelines,
        enabled=payload.get("enabled") is not False,
        created_by_user_id=getattr(actor, "id", None),
    )
    db.session.add(pipeline)
    db.session.flush()
    _apply_stages(pipeline, payload.get("stages") or [])
    if pipeline.is_default:
        _demote_other_defaults(service.id, pipeline.id)
    db.session.commit()

    log_audit(
        "ci_pipeline_saved",
        actor=actor,
        target_type="ci_pipeline",
        target_id=str(pipeline.id),
        details={
            "service": service.slug,
            "pipeline": pipeline.name,
            "stageCount": len(pipeline.stages),
            "created": True,
        },
    )
    return pipeline_to_dict(pipeline)


def update_pipeline(
    pipeline: CiPipeline, payload: Dict[str, Any], *, actor=None
) -> Dict[str, Any]:
    name = _clean(payload.get("name"), 120) or pipeline.name
    clash = (
        CiPipeline.query.filter_by(service_id=pipeline.service_id, name=name)
        .filter(CiPipeline.id != pipeline.id)
        .first()
    )
    if clash:
        raise PipelineError(f"This service already has a pipeline named '{name}'.")

    pipeline.name = name
    if "description" in payload:
        pipeline.description = _clean(payload.get("description"), 2000) or None
    if "enabled" in payload:
        pipeline.enabled = bool(payload.get("enabled"))
    if payload.get("isDefault"):
        pipeline.is_default = True

    if "stages" in payload:
        _apply_stages(pipeline, payload.get("stages") or [])
    # Bumped on every save so a build's snapshot records which revision ran.
    pipeline.version = int(pipeline.version or 1) + 1
    pipeline.updated_at = datetime.now(timezone.utc)
    if pipeline.is_default:
        _demote_other_defaults(pipeline.service_id, pipeline.id)
    db.session.commit()

    log_audit(
        "ci_pipeline_saved",
        actor=actor,
        target_type="ci_pipeline",
        target_id=str(pipeline.id),
        details={
            "service": pipeline.service.slug if pipeline.service else None,
            "pipeline": pipeline.name,
            "stageCount": len(pipeline.stages),
            "version": pipeline.version,
        },
    )
    return pipeline_to_dict(pipeline)


def delete_pipeline(pipeline: CiPipeline, *, actor=None) -> None:
    service_id = pipeline.service_id
    was_default = pipeline.is_default
    name = pipeline.name
    db.session.delete(pipeline)
    db.session.flush()
    if was_default:
        # Never leave a service with pipelines but no default.
        replacement = (
            CiPipeline.query.filter_by(service_id=service_id)
            .order_by(CiPipeline.id.asc())
            .first()
        )
        if replacement:
            replacement.is_default = True
            db.session.add(replacement)
    db.session.commit()
    log_audit(
        "ci_pipeline_deleted",
        actor=actor,
        target_type="ci_pipeline",
        target_id=str(pipeline.id),
        details={"pipeline": name, "serviceId": service_id},
    )


def create_from_template(
    service: CiService, application_type: Optional[str] = None, *, actor=None
) -> Dict[str, Any]:
    payload = templates.default_pipeline_payload(
        application_type or service.application_type
    )
    existing = CiPipeline.query.filter_by(
        service_id=service.id, name=payload["name"]
    ).first()
    if existing:
        return update_pipeline(existing, payload, actor=actor)
    return create_pipeline(service, payload, actor=actor)


def _demote_other_defaults(service_id: int, keep_id: int) -> None:
    CiPipeline.query.filter(
        CiPipeline.service_id == service_id,
        CiPipeline.id != keep_id,
        CiPipeline.is_default.is_(True),
    ).update({"is_default": False}, synchronize_session=False)


def resolve_for_build(
    service: CiService, pipeline_id: Optional[int] = None
) -> Tuple[CiPipeline, List[CiPipelineStage]]:
    """The pipeline a Run Build should execute, with its runnable stages."""
    if pipeline_id:
        pipeline = db.session.get(CiPipeline, int(pipeline_id))
        if pipeline is None or pipeline.service_id != service.id:
            raise PipelineError("That pipeline does not belong to this service.")
    else:
        pipeline = service.default_pipeline()
    if pipeline is None:
        raise PipelineError("This service has no pipeline configured.")
    if not pipeline.enabled:
        raise PipelineError(f"Pipeline '{pipeline.name}' is disabled.")
    stages = [stage for stage in pipeline.stages if stage.enabled]
    if not stages:
        raise PipelineError(f"Pipeline '{pipeline.name}' has no enabled stages.")
    return pipeline, stages
