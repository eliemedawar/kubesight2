"""Turning an approved proposal into ordinary KubeSight configuration.

This is where the feature ends. After it runs there is a ``CiPipeline`` with
stages, a ``CiService`` with an application profile, and some ``CiSecret`` rows
— all of them exactly the shape a person would have produced by hand, none of
them marked as having come from a model, and nothing anywhere that a build has
to consult before it can run. Deleting this entire package afterwards would not
stop a single service building.

Three things it is careful about:

**It re-validates.** The stored draft is not trusted, because the user may have
edited it in the review screen and because it has been sitting in a row since
it was generated. Whatever is being saved goes through the validator again, on
the way in, every time.

**Secrets are written before the pipeline.** A stage may not reference a secret
that does not exist — ``pipelines._secret_refs`` refuses it — so the values the
user just typed have to be rows before the pipeline that reads them is saved.
That ordering is load-bearing, not incidental.

**A value the user typed is never read back.** Secrets go into the encrypted
store through the ordinary service and are never returned, logged, echoed into
an audit entry, or sent anywhere near Hermes again.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

from ...audit import log_audit
from ...db import db
from ...models import RegistryConnection
from ...models_ci import CiPipeline, CiRepositoryAnalysis, CiSecret, CiService
from ..ci import generated
from ..ci import pipelines as pipelines_service
from ..ci import secrets as secrets_service
from . import profile as profile_module


class AcceptError(ValueError):
    """A proposal could not be accepted. Message is user-facing."""


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _existing_secret_keys(service_id: int) -> set:
    rows = CiSecret.query.filter(
        db.or_(CiSecret.service_id == service_id, CiSecret.scope == "global")
    ).all()
    return {row.key for row in rows}


def _partition_inputs(
    required: List[Dict[str, Any]], values: Dict[str, Any], known_secrets: set
) -> Tuple[List[Dict[str, Any]], Dict[str, str], Dict[str, str], Optional[int]]:
    """Sort the answers into what each one becomes, and refuse what is missing.

    Returns ``(parameters, secret_values, parameter_values, registry_id)``.
    """
    parameters: List[Dict[str, Any]] = []
    secret_values: Dict[str, str] = {}
    parameter_values: Dict[str, str] = {}
    registry_id: Optional[int] = None
    missing: List[str] = []

    for item in required or []:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "").strip()
        kind = str(item.get("kind") or "").strip()
        if not name:
            continue
        raw = values.get(name)
        supplied = "" if raw is None else str(raw)

        if kind == "secret":
            if supplied:
                secret_values[name] = supplied
            elif name in known_secrets:
                # Already set for this service, or inherited from a global. The
                # user is not asked to retype a secret they already have.
                pass
            elif item.get("required") is not False:
                missing.append(item.get("label") or name)
            continue

        if kind == "registry":
            if supplied:
                try:
                    registry_id = int(supplied)
                except (TypeError, ValueError):
                    raise AcceptError(
                        f"'{item.get('label') or name}' must be a registry connection."
                    )
            elif item.get("required") is not False:
                missing.append(item.get("label") or name)
            continue

        # A parameter. It becomes a build input with the supplied value as its
        # default, so it is visible and overridable in Run Build rather than
        # baked invisibly into a stage.
        if not supplied and item.get("required") is not False:
            missing.append(item.get("label") or name)
            continue
        parameter_values[name] = supplied
        parameters.append(
            {
                "name": name,
                "type": "text",
                "label": item.get("label") or name,
                "description": item.get("description") or "",
                "default": supplied,
                "required": bool(item.get("required") is not False),
            }
        )

    if missing:
        raise AcceptError(
            "These still need a value before the service can be created: "
            + ", ".join(sorted(missing))
            + "."
        )
    return parameters, secret_values, parameter_values, registry_id


def _merge_parameters(
    proposed: List[Dict[str, Any]], from_inputs: List[Dict[str, Any]]
) -> List[Dict[str, Any]]:
    """The pipeline's own parameters, plus the ones the answers created.

    A name defined on both sides keeps the pipeline's definition and takes the
    supplied value as its default: the pipeline knows it is a choice or a
    boolean, the answer knows what it should be.
    """
    merged: List[Dict[str, Any]] = []
    supplied = {str(item["name"]): item for item in from_inputs}
    for param in proposed or []:
        if not isinstance(param, dict):
            continue
        name = str(param.get("name") or "")
        answer = supplied.pop(name, None)
        if answer is not None and answer.get("default"):
            param = {**param, "default": answer["default"]}
        merged.append(param)
    merged.extend(supplied.values())
    return merged


def accept(
    analysis: CiRepositoryAnalysis,
    payload: Dict[str, Any],
    *,
    actor=None,
) -> Dict[str, Any]:
    """Save an approved proposal as native configuration.

    ``payload`` may carry a ``pipeline`` the user edited in the review screen
    and an ``applicationProfile`` they corrected; both replace the stored draft
    and both are validated as if they had arrived from anywhere else.
    """
    service: CiService = analysis.service
    if service is None:
        raise AcceptError("The service this analysis belongs to no longer exists.")
    if analysis.state not in ("analyzed", "partial"):
        raise AcceptError(
            "This analysis has not produced a pipeline to accept. Run it again, or "
            "configure the service manually."
        )

    proposal = payload.get("pipeline") or analysis.generated_pipeline
    if not isinstance(proposal, dict) or not proposal.get("stages"):
        raise AcceptError("There is no pipeline to accept.")

    required = analysis.required_inputs or []
    values = payload.get("inputs") if isinstance(payload.get("inputs"), dict) else {}

    # --- Everything that can be refused, before anything is written --------
    known_secrets = _existing_secret_keys(service.id)
    parameters, secret_values, _parameter_values, registry_id = _partition_inputs(
        required, values, known_secrets
    )

    if registry_id is not None:
        registry = db.session.get(RegistryConnection, registry_id)
        if registry is None or not registry.enabled:
            raise AcceptError("Select an enabled registry connection.")

    raw_profile = payload.get("applicationProfile") or analysis.application_profile
    try:
        resolved_profile = (
            profile_module.normalize(
                raw_profile, source=(raw_profile or {}).get("source", "hermes")
            )
            if raw_profile
            else None
        )
    except profile_module.ProfileError as exc:
        raise AcceptError(str(exc)) from exc

    # The validator runs against the secrets that WILL exist, which is why the
    # declared inputs travel with it: a stage referencing NEXUS_PASSWORD is
    # valid precisely because the user is supplying NEXUS_PASSWORD right now.
    verdict = generated.validate(service, proposal, declared_inputs=required)
    if not verdict["valid"]:
        raise AcceptError(
            "This pipeline cannot be saved: "
            + " ".join(item["message"] for item in verdict["errors"][:3])
        )

    # --- Writes -----------------------------------------------------------
    # Secrets first: a stage may not reference one that does not exist yet.
    created_secrets: List[str] = []
    for key, value in secret_values.items():
        if key in known_secrets:
            existing = CiSecret.query.filter_by(service_id=service.id, key=key).first()
            if existing is not None:
                secrets_service.update_secret(existing, {"value": value}, actor=actor)
                continue
        secrets_service.create_secret(
            {
                "key": key,
                "value": value,
                "description": next(
                    (
                        str(item.get("description") or "")
                        for item in required
                        if str(item.get("name")) == key
                    ),
                    "",
                ),
            },
            service_id=service.id,
            actor=actor,
        )
        created_secrets.append(key)

    if registry_id is not None:
        service.registry_connection_id = registry_id

    pipeline_payload = {
        **verdict["pipeline"],
        "parameters": _merge_parameters(verdict["pipeline"].get("parameters") or [], parameters),
    }

    existing_pipeline: Optional[CiPipeline] = service.default_pipeline()
    if existing_pipeline is not None:
        saved = pipelines_service.update_pipeline(
            existing_pipeline, pipeline_payload, actor=actor
        )
    else:
        saved = pipelines_service.create_pipeline(service, pipeline_payload, actor=actor)

    if resolved_profile:
        service.application_profile = resolved_profile
        service.profile_source = resolved_profile.get("source") or "hermes"
        # The discriminator everything else already reads, brought in line with
        # what the repository actually is. It changes the fallback pipeline and
        # the starter kit, never a saved stage.
        derived = resolved_profile.get("derivedApplicationType")
        if derived:
            service.application_type = derived
    service.analysis_state = "analyzed"
    service.updated_at = _now()
    db.session.add(service)

    analysis.pipeline_state = "accepted"
    analysis.generated_pipeline = pipeline_payload
    analysis.application_profile = resolved_profile or analysis.application_profile
    db.session.add(analysis)
    db.session.commit()

    log_audit(
        "ci_generated_pipeline_accepted",
        actor=actor,
        target_type="ci_service",
        target_id=str(service.id),
        details={
            "service": service.slug,
            "analysisId": analysis.id,
            "pipelineId": saved.get("id"),
            "stageCount": len(pipeline_payload.get("stages") or []),
            "applicationType": service.application_type,
            "profileSource": service.profile_source,
            # Keys only. A value never reaches an audit row.
            "secretsCreated": sorted(created_secrets),
            "registryLinked": bool(registry_id),
            "editedBeforeAccepting": bool(payload.get("pipeline")),
        },
    )
    return {"pipeline": saved, "service": service}
