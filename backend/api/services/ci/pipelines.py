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

import ipaddress
import re
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


PARAMETER_TYPES = ("text", "choice", "boolean", "dynamic_choice")
# What a dynamic_choice can be filled from. Resolved server-side at run time so
# the Run Build dialog receives a ready list rather than discovering how to
# build one.
PARAMETER_SOURCES = ("branches", "tags", "branches_and_tags")
MAX_PARAMETERS = 25
MAX_CHOICES = 100
# Values become environment variables for every stage, so a name has to be one.
_PARAM_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def _parameter_choices(value: Any, name: str) -> List[str]:
    if isinstance(value, str):
        items = value.splitlines()
    elif isinstance(value, (list, tuple)):
        items = list(value)
    else:
        items = []
    cleaned: List[str] = []
    for item in items[:MAX_CHOICES]:
        text = str(item or "").strip()[:255]
        if text and text not in cleaned:
            cleaned.append(text)
    if not cleaned:
        raise PipelineError(f"Parameter '{name}' is a choice but lists no options.")
    return cleaned


def _parameters(value: Any) -> List[Dict[str, Any]]:
    """What a person is asked before a build starts.

    Rejected rather than repaired when malformed: a parameter whose definition
    is wrong produces a build configured differently from what was intended,
    which is worse than being told to fix the definition.
    """
    if value in (None, "", [], {}):
        return []
    if not isinstance(value, (list, tuple)):
        raise PipelineError("Build parameters must be a list.")
    if len(value) > MAX_PARAMETERS:
        raise PipelineError(f"A pipeline may not define more than {MAX_PARAMETERS} parameters.")

    out: List[Dict[str, Any]] = []
    seen = set()
    for entry in value:
        if not isinstance(entry, dict):
            raise PipelineError("Each build parameter must be an object.")
        name = _clean(entry.get("name"), 128)
        if not name:
            raise PipelineError("Every build parameter needs a name.")
        if not _PARAM_NAME_RE.match(name):
            raise PipelineError(
                f"Parameter name '{name}' is not usable as an environment variable "
                "— letters, digits and underscores only, not starting with a digit."
            )
        if name in seen:
            raise PipelineError(f"Build parameter '{name}' is defined twice.")
        seen.add(name)

        param_type = _clean(entry.get("type"), 24).lower() or "text"
        if param_type not in PARAMETER_TYPES:
            raise PipelineError(
                f"Parameter '{name}' has unknown type '{param_type}'. "
                f"Use one of: {', '.join(PARAMETER_TYPES)}."
            )

        param: Dict[str, Any] = {
            "name": name,
            "type": param_type,
            "label": _clean(entry.get("label"), 160) or name,
            "description": _clean(entry.get("description"), 500) or "",
            "required": bool(entry.get("required")),
        }

        if param_type == "boolean":
            # Stored as the strings a shell sees, since that is what a stage gets.
            param["default"] = "true" if _truthy(entry.get("default")) else "false"
            param["required"] = False  # A checkbox always has a value.
        elif param_type == "choice":
            param["choices"] = _parameter_choices(entry.get("choices"), name)
            default = _clean(entry.get("default"), 255)
            if default and default not in param["choices"]:
                raise PipelineError(
                    f"Parameter '{name}' defaults to '{default}', which is not one of its options."
                )
            param["default"] = default or param["choices"][0]
        elif param_type == "dynamic_choice":
            source = _clean(entry.get("source"), 32).lower() or "branches"
            if source not in PARAMETER_SOURCES:
                raise PipelineError(
                    f"Parameter '{name}' has unknown source '{source}'. "
                    f"Use one of: {', '.join(PARAMETER_SOURCES)}."
                )
            param["source"] = source
            # No validation against the live list: it is resolved at run time and
            # a repository that is briefly unreachable must not invalidate a
            # saved pipeline.
            param["default"] = _clean(entry.get("default"), 255)
        else:
            param["default"] = str(entry.get("default") or "")[:4000]

        out.append(param)
    return out


def _truthy(value: Any) -> bool:
    if isinstance(value, str):
        return value.strip().lower() in ("1", "true", "yes", "on")
    return bool(value)


def _command_lines(value: Any) -> List[str]:
    """Stage commands, kept as the author wrote them.

    These lines are joined back into one shell script, so unlike a list of
    labels they are whitespace-sensitive: a heredoc that writes a Dockerfile or
    a properties file breaks if interior blank lines vanish or leading
    indentation is stripped. Only trailing whitespace is removed, and only
    leading/trailing blank lines are dropped — an entirely empty list still
    fails the "command stage with no commands" check above.
    """
    if isinstance(value, str):
        items = value.splitlines()
    elif isinstance(value, (list, tuple)):
        items = list(value)
    else:
        items = []
    lines = [str(item or "").rstrip()[:MAX_COMMAND_CHARS] for item in items[:MAX_COMMANDS_PER_STAGE]]
    while lines and not lines[0].strip():
        lines.pop(0)
    while lines and not lines[-1].strip():
        lines.pop()
    return lines


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


_HOSTNAME_RE = re.compile(
    r"^(?=.{1,253}$)[A-Za-z0-9]([A-Za-z0-9-]{0,61}[A-Za-z0-9])?"
    r"(\.[A-Za-z0-9]([A-Za-z0-9-]{0,61}[A-Za-z0-9])?)*$"
)

MAX_HOST_ALIASES = 20
MAX_HOSTNAMES_PER_ALIAS = 10


def _host_aliases(value: Any, stage_name: str) -> List[Dict[str, Any]]:
    """Extra /etc/hosts entries for the build pod.

    Accepts either the text form the editor shows (``ip=host,host`` per line)
    or the structured form the API round-trips (``[{"ip", "hostnames"}]``), so
    a saved stage reloads and re-saves unchanged.

    A bad entry raises rather than being dropped: silently ignoring a typo'd
    mapping would surface much later as a connect timeout inside a build tool,
    which is exactly the failure this field exists to prevent.
    """
    if value in (None, "", [], {}):
        return []

    entries: List[Any] = []
    if isinstance(value, str):
        entries = value.splitlines()
    elif isinstance(value, (list, tuple)):
        entries = list(value)
    else:
        raise PipelineError(f"Stage '{stage_name}' has malformed host aliases.")

    merged: List[Dict[str, Any]] = []
    by_ip: Dict[str, Dict[str, Any]] = {}

    for entry in entries[: MAX_HOST_ALIASES * MAX_HOSTNAMES_PER_ALIAS]:
        if isinstance(entry, dict):
            raw_ip = _clean(entry.get("ip"), 64)
            raw_names = entry.get("hostnames")
            names = (
                [str(name) for name in raw_names]
                if isinstance(raw_names, (list, tuple))
                else str(raw_names or "").split(",")
            )
        else:
            line = str(entry or "").strip()
            if not line:
                continue  # blank lines are formatting, not entries
            if "=" not in line:
                raise PipelineError(
                    f"Stage '{stage_name}' has a host alias without '=': '{line}'. "
                    "Use ip=hostname, for example 10.10.10.20=nexus.areeba.com."
                )
            raw_ip, _, raw_names = line.partition("=")
            raw_ip = raw_ip.strip()
            names = raw_names.split(",")

        if not raw_ip and not any(name.strip() for name in names):
            continue
        try:
            ip = str(ipaddress.ip_address(raw_ip))
        except ValueError:
            raise PipelineError(
                f"Stage '{stage_name}' has an invalid host alias IP address: '{raw_ip}'."
            )

        cleaned: List[str] = []
        for name in names:
            name = name.strip()
            if not name:
                continue
            if not _HOSTNAME_RE.match(name):
                raise PipelineError(
                    f"Stage '{stage_name}' has an invalid host alias hostname: '{name}'."
                )
            if name not in cleaned:
                cleaned.append(name)
        if not cleaned:
            raise PipelineError(
                f"Stage '{stage_name}' has a host alias for {ip} with no hostname."
            )

        # One entry per IP: Kubernetes accepts duplicates, but merging keeps the
        # generated pod spec readable and the round-trip stable.
        existing = by_ip.get(ip)
        if existing is None:
            existing = {"ip": ip, "hostnames": []}
            by_ip[ip] = existing
            merged.append(existing)
        for name in cleaned:
            if name not in existing["hostnames"]:
                existing["hostnames"].append(name)
        del existing["hostnames"][MAX_HOSTNAMES_PER_ALIAS:]

    if len(merged) > MAX_HOST_ALIASES:
        raise PipelineError(
            f"Stage '{stage_name}' may not define more than {MAX_HOST_ALIASES} host aliases."
        )
    return merged


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

    commands = _command_lines(payload.get("commands"))
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
        "host_aliases": _host_aliases(payload.get("hostAliases"), name),
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

    if "parameters" in payload:
        pipeline.parameters = _parameters(payload.get("parameters"))

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



def parameter_definitions(pipeline: CiPipeline) -> List[Dict[str, Any]]:
    """The stored definitions, unresolved — what a snapshot keeps."""
    from .serializers import _json_list

    return [p for p in _json_list(pipeline.parameters) if isinstance(p, dict)]


def resolve_parameters(service: CiService, pipeline: CiPipeline) -> List[Dict[str, Any]]:
    """The pipeline's parameters with dynamic choices filled in.

    Resolution happens here, server-side, so the Run Build dialog receives a
    ready list rather than learning how to build one. A repository that cannot
    be reached yields an empty ``choices`` and an ``error`` the dialog shows —
    the parameter stays usable by typing, because a listing outage should not
    stop a release.
    """
    from .serializers import _json_list

    resolved: List[Dict[str, Any]] = []
    listing: Optional[List[Dict[str, str]]] = None
    listing_error = ""

    for param in _json_list(pipeline.parameters):
        if not isinstance(param, dict):
            continue
        param = dict(param)
        if param.get("type") == "dynamic_choice":
            if listing is None:
                listing, listing_error = _repository_refs(service)
            source = param.get("source") or "branches"
            wanted = (
                ("branch", "tag")
                if source == "branches_and_tags"
                else ("tag",) if source == "tags" else ("branch",)
            )
            param["choices"] = [item["value"] for item in listing if item["type"] in wanted]
            if listing_error:
                param["error"] = listing_error
        resolved.append(param)
    return resolved


def _repository_refs(service: CiService) -> Tuple[List[Dict[str, str]], str]:
    """Branches and tags, or an empty list and why not."""
    if not service.source_ready():
        return [], "The service's source is not configured yet."
    try:
        from . import source as source_port

        handler = source_port.get_provider(service.repository_provider)
        ref = handler.parse_repository_url(service.repository_url)
        items = handler.list_revisions(ref, service.credential_profile)
        return [
            {"value": item.value, "type": item.kind}
            for item in items
            if getattr(item, "value", "")
        ], ""
    except Exception as exc:  # A listing failure must not block a build.
        return [], str(exc) or "The repository could not be listed."


def validate_parameter_values(
    pipeline: CiPipeline, values: Optional[Dict[str, Any]]
) -> Dict[str, str]:
    """Submitted values checked against the pipeline's own definitions.

    Returns the accepted values, which the caller passes as the build's
    ``variables``. Rejects rather than drops: a build that silently ignored a
    parameter would run differently from what was asked for, and say nothing.
    """
    from .serializers import _json_list

    definitions = [p for p in _json_list(pipeline.parameters) if isinstance(p, dict)]
    submitted = {str(k): v for k, v in (values or {}).items()}
    known = {str(p.get("name")) for p in definitions}

    unknown = [name for name in submitted if name not in known]
    if unknown and definitions:
        # Only complain when the pipeline HAS parameters: automation still
        # passes free-form variables to pipelines that declare none.
        raise PipelineError(
            f"This pipeline has no parameter named '{sorted(unknown)[0]}'."
        )

    accepted: Dict[str, str] = {}
    for param in definitions:
        name = str(param.get("name") or "")
        if not name:
            continue
        param_type = param.get("type") or "text"
        raw = submitted.get(name, None)

        if param_type == "boolean":
            accepted[name] = "true" if _truthy(
                param.get("default") if raw is None else raw
            ) else "false"
            continue

        value = str(param.get("default") or "" if raw is None else raw).strip()
        if not value:
            if param.get("required"):
                raise PipelineError(f"'{param.get('label') or name}' is required.")
            accepted[name] = ""
            continue

        if param_type == "choice":
            choices = [str(c) for c in (param.get("choices") or [])]
            if value not in choices:
                raise PipelineError(
                    f"'{param.get('label') or name}' must be one of: {', '.join(choices)}."
                )
        # dynamic_choice is deliberately not checked against the live list: it
        # is a convenience, and a ref created seconds ago must still be usable.
        accepted[name] = value[:4000]

    # Values a pipeline without parameters was given still travel through.
    if not definitions:
        return {str(k): str(v)[:4000] for k, v in submitted.items()}
    return accepted


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
