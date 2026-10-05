"""Pipelines that live outside any CI service.

A pipeline on the Pipelines page has two lives, and both are ordinary CI:

* **On its own.** It runs like a Jenkins job with no application behind it —
  its own Run button, builds, logs, schedules and secrets, and a repository
  only if it needs one. Nightly backups, a release train, a cleanup.
* **Attached to services.** A CI service can build with it instead of its own
  stages. Each service still builds its own repository, Dockerfile and
  registry, and its builds stay in its own history; only the stages,
  build inputs and post actions come from the shared pipeline. Edit it once,
  every service that uses it builds the new version from its next build on.

Storage: the pipeline's *home* is a ``CiService`` row of kind ``pipeline``,
and the shared pipeline is that home's default build pipeline. That is what
lets the first life reuse everything keyed by service — build numbering,
logs, caches, runner labels, schedules, secrets, artifacts — with no change on
the build path. The catalog, slug matching and every service picker list only
``kind='service'`` rows, so a home never shows up as an application.

An attached service points at it through its own pipeline row
(``CiPipeline.linked_pipeline_id``). That row's own stages are left exactly as
they were, so "stop using it" can put them back.

Secrets resolve service first, then the shared pipeline's home, then global:
a credential stored on the shared pipeline is available to every service that
uses it (which the UI says), and a service can override it by name.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from sqlalchemy import or_

from ...audit import log_audit
from ...db import db
from ...models_ci import APPLICATION_TYPES, CiBuild, CiPipeline, CiService
from . import pipelines as pipelines_service
from . import templates as templates_service
from .catalog import CatalogError, _clean, _unique_slug


class SharedPipelineError(ValueError):
    """A request about a shared pipeline was refused. Message is user-facing."""


START_FROM = ("empty", "template", "service")


# ---------------------------------------------------------------------------
# Lookups
# ---------------------------------------------------------------------------

def get_home(home_id: int) -> CiService:
    row = db.session.get(CiService, int(home_id))
    if row is None or not row.is_pipeline_home:
        raise LookupError("Pipeline not found.")
    return row


def shared_pipeline_of(home: CiService) -> Optional[CiPipeline]:
    """The pipeline a home holds — its default build pipeline."""
    return home.default_pipeline()


def home_of(pipeline: Optional[CiPipeline]) -> Optional[CiService]:
    """The home of a shared pipeline, or None when it is a service's own."""
    if pipeline is None:
        return None
    service = pipeline.service
    return service if service is not None and service.is_pipeline_home else None


def users_of(home: CiService) -> List[CiPipeline]:
    """Service pipelines that build with this home's pipeline."""
    shared = shared_pipeline_of(home)
    if shared is None:
        return []
    return (
        CiPipeline.query.filter_by(linked_pipeline_id=shared.id)
        .order_by(CiPipeline.service_id.asc(), CiPipeline.id.asc())
        .all()
    )


def shared_summary(pipeline: Optional[CiPipeline]) -> Optional[Dict[str, Any]]:
    """What a linked service pipeline uses, for the service's payloads."""
    if pipeline is None or not pipeline.linked_pipeline_id:
        return None
    shared = pipeline.linked_pipeline
    home = home_of(shared)
    if shared is None or home is None:
        return {
            "missing": True,
            "pipelineId": pipeline.linked_pipeline_id,
            "name": "a shared pipeline that no longer exists",
        }
    return {
        "missing": False,
        "id": home.id,
        "slug": home.slug,
        "name": home.name,
        "pipelineId": shared.id,
        "version": shared.version,
        "enabled": bool(shared.enabled) and home.status == "active",
        "stageCount": len(shared.stages),
        "updatedAt": _iso(shared.updated_at),
    }


# ---------------------------------------------------------------------------
# Reads
# ---------------------------------------------------------------------------

def list_homes(*, search: str = "") -> List[Dict[str, Any]]:
    query = CiService.query.filter(CiService.kind == "pipeline")
    term = _clean(search, 120)
    if term:
        like = f"%{term.lower()}%"
        query = query.filter(
            or_(
                db.func.lower(CiService.name).like(like),
                db.func.lower(CiService.slug).like(like),
                db.func.lower(db.func.coalesce(CiService.description, "")).like(like),
            )
        )
    rows = query.order_by(CiService.name.asc()).all()
    return [home_to_dict(row) for row in rows]


def list_summary(items: List[Dict[str, Any]]) -> Dict[str, int]:
    latest = [item.get("latestBuild") for item in items]
    return {
        "total": len(items),
        "shared": sum(1 for item in items if item.get("usedByCount")),
        "running": sum(1 for b in latest if b and b.get("status") in ("running", "queued")),
        "failing": sum(1 for b in latest if b and b.get("status") in ("failed", "timeout")),
    }


def home_to_dict(home: CiService, *, include_used_by: bool = False) -> Dict[str, Any]:
    from .serializers import build_summary, service_to_dict

    recent = (
        CiBuild.query.filter_by(service_id=home.id)
        .order_by(CiBuild.number.desc())
        .limit(10)
        .all()
    )
    data = service_to_dict(
        home,
        latest_build=recent[0] if recent else None,
        recent_statuses=[b.status for b in recent],
    )
    shared = shared_pipeline_of(home)
    users = users_of(home)
    stages = list(shared.stages) if shared else []
    data.update(
        {
            "pipelineVersion": shared.version if shared else None,
            "stageNames": [stage.name for stage in stages],
            "parameterCount": len(pipelines_service.parameter_definitions(shared)) if shared else 0,
            "usedByCount": len(users),
            "latestBuild": build_summary(recent[0]) if recent else None,
        }
    )
    if include_used_by:
        data["usedBy"] = [_user_to_dict(row) for row in users]
    return data


def _user_to_dict(pipeline: CiPipeline) -> Dict[str, Any]:
    from .serializers import build_summary

    service = pipeline.service
    latest = (
        CiBuild.query.filter_by(service_id=service.id)
        .order_by(CiBuild.number.desc())
        .first()
    )
    return {
        "serviceId": service.id,
        "serviceName": service.name,
        "serviceSlug": service.slug,
        "serviceStatus": service.status,
        "applicationType": service.application_type,
        "pipelineId": pipeline.id,
        "pipelineName": pipeline.name,
        "isDefault": bool(pipeline.is_default),
        "latestBuild": build_summary(latest) if latest else None,
    }


# ---------------------------------------------------------------------------
# Writes
# ---------------------------------------------------------------------------

def _starting_payload(payload: Dict[str, Any], home: CiService, actor) -> Dict[str, Any]:
    """The stages/parameters/post actions a new pipeline starts with."""
    start = payload.get("startFrom") if isinstance(payload.get("startFrom"), dict) else {}
    mode = _clean(start.get("type"), 16).lower() or "empty"
    if mode not in START_FROM:
        raise SharedPipelineError(f"'{mode}' is not a way to start a pipeline.")
    if mode == "template":
        kind = _clean(start.get("applicationType"), 32) or home.application_type
        if kind not in APPLICATION_TYPES:
            raise SharedPipelineError(f"Unknown application type '{kind}'.")
        template = templates_service.default_pipeline_payload(kind)
        return {"stages": template["stages"], "parameters": template["parameters"]}
    if mode == "service":
        try:
            source = db.session.get(CiService, int(start.get("serviceId")))
        except (TypeError, ValueError):
            source = None
        if source is None or source.is_pipeline_home:
            raise SharedPipelineError("Pick the CI service whose pipeline to copy.")
        return copy_payload_of(source)
    return {"stages": [], "parameters": []}


def copy_payload_of(service: CiService) -> Dict[str, Any]:
    """A service's build pipeline as a save payload — what it builds with today,
    including the generated default when nothing was ever saved."""
    from .serializers import pipeline_stage_to_dict

    row = service.default_pipeline()
    effective = row.effective() if row is not None else None
    if effective is not None and effective.stages:
        stages = [pipeline_stage_to_dict(stage) for stage in effective.stages]
        return {
            "stages": [_as_payload(stage) for stage in stages],
            "parameters": pipelines_service.parameter_definitions(effective),
            "postActions": list(effective.post_actions or []),
        }
    try:
        generated, _ = pipelines_service.resolve_for_build(service)
    except pipelines_service.PipelineError as exc:
        raise SharedPipelineError(f"{service.name} has no pipeline to copy: {exc}")
    return {
        "stages": [_as_payload(pipeline_stage_to_dict(stage)) for stage in generated.stages],
        "parameters": list(generated.parameters or []),
    }


def _as_payload(stage: Dict[str, Any]) -> Dict[str, Any]:
    stage = dict(stage)
    stage.pop("id", None)
    stage.pop("pipelineStageId", None)
    deploy = stage.get("deploy")
    if isinstance(deploy, dict):
        # The authority stamp is never carried across: whoever saves the copy
        # is the one it deploys as (pipelines._stamp_deploy_authority).
        stage["deploy"] = {k: v for k, v in deploy.items() if k != "authorizedBy"}
    upload = stage.get("storeUpload")
    if isinstance(upload, dict):
        stage["storeUpload"] = {k: v for k, v in upload.items() if k != "authorizedBy"}
    return stage


def _missing_secret_names(stages: List[Dict[str, Any]], known: set) -> List[str]:
    names = set()
    for stage in stages:
        for ref in stage.get("secretRefs") or []:
            name = ref.get("name") if isinstance(ref, dict) else ref
            if name and name not in known:
                names.add(str(name))
    return sorted(names)


def create_home(payload: Dict[str, Any], *, actor=None) -> Dict[str, Any]:
    name = _clean(payload.get("name"), 160)
    if not name:
        raise SharedPipelineError("Give the pipeline a name.")
    application_type = _clean(payload.get("applicationType"), 32) or "generic"
    if application_type not in APPLICATION_TYPES:
        raise SharedPipelineError(f"Unknown application type '{application_type}'.")

    home = CiService(
        kind="pipeline",
        name=name,
        slug=_unique_slug(payload.get("slug") or name),
        description=_clean(payload.get("description"), 2000) or None,
        owner_team=_clean(payload.get("ownerTeam"), 255) or None,
        application_type=application_type,
        status="active",
        default_branch=_clean(payload.get("defaultBranch"), 255) or "main",
        created_by_user_id=getattr(actor, "id", None),
    )
    db.session.add(home)
    db.session.flush()
    try:
        start = _starting_payload(payload, home, actor)
        missing = _missing_secret_names(
            start.get("stages") or [], pipelines_service._known_secret_keys(home.id)
        )
        if missing:
            raise SharedPipelineError(
                "The pipeline being copied uses secrets this new pipeline does not have yet: "
                f"{', '.join(missing)}. Create it empty or from a starter, add the secrets "
                "under its Settings, then copy the stages in — or make them global secrets."
            )
        shared = CiPipeline(
            service_id=home.id,
            name="default",
            description=None,
            purpose="build",
            is_default=True,
            enabled=True,
            parameters=pipelines_service._parameters(start.get("parameters")),
            created_by_user_id=getattr(actor, "id", None),
        )
        db.session.add(shared)
        db.session.flush()
        pipelines_service._apply_stages(shared, start.get("stages") or [], actor=actor)
        if start.get("postActions"):
            shared.post_actions = pipelines_service._post_actions(start["postActions"], home.id)
    except (SharedPipelineError, pipelines_service.PipelineError, CatalogError):
        db.session.rollback()
        raise
    db.session.commit()
    log_audit(
        "ci_shared_pipeline_created",
        actor=actor,
        target_type="ci_service",
        target_id=str(home.id),
        details={"pipeline": home.slug, "stageCount": len(shared.stages)},
    )
    return home_to_dict(home, include_used_by=True)


def assert_deletable(home: CiService) -> None:
    """A shared pipeline in use cannot be deleted: its services would quietly
    fall back to some other pipeline on their next build."""
    users = users_of(home)
    if users:
        names = ", ".join(sorted({row.service.name for row in users}))
        raise CatalogError(
            f"{len(users)} service{'s build' if len(users) != 1 else ' builds'} with this pipeline "
            f"({names}). Stop using it on each of them first."
        )


def attach(service: CiService, home: CiService, *, actor=None) -> Dict[str, Any]:
    """Make ``service`` build with ``home``'s pipeline from its next build on."""
    if service.is_pipeline_home:
        raise SharedPipelineError(
            "A pipeline on the Pipelines page cannot use another one — copy its stages instead."
        )
    shared = shared_pipeline_of(home)
    if shared is None:
        raise SharedPipelineError(f"'{home.name}' has no pipeline yet.")
    if not shared.stages:
        raise SharedPipelineError(
            f"'{home.name}' has no stages yet, so a build with it would do nothing. Add some first."
        )

    row = service.default_pipeline()
    if row is None:
        row = CiPipeline(
            service_id=service.id,
            name="default",
            purpose="build",
            is_default=True,
            enabled=True,
            parameters=[],
            created_by_user_id=getattr(actor, "id", None),
        )
        db.session.add(row)
        db.session.flush()
    previous = row.linked_pipeline_id
    row.linked_pipeline_id = shared.id
    row.version = int(row.version or 1) + 1
    row.updated_at = datetime.now(timezone.utc)
    db.session.add(row)
    db.session.commit()
    log_audit(
        "ci_shared_pipeline_attached",
        actor=actor,
        target_type="ci_service",
        target_id=str(service.id),
        details={
            "service": service.slug,
            "pipeline": home.slug,
            "replaced": previous,
        },
    )
    return pipelines_service.list_pipelines(service)[0]


def detach(service: CiService, *, mode: str = "restore", actor=None) -> Dict[str, Any]:
    """Stop building with the shared pipeline.

    ``restore`` goes back to the service's own stages as they were before it was
    attached (or the generated default, if it never had any). ``copy`` replaces
    them with the shared pipeline's current stages, which the service then owns
    and may edit.
    """
    from .serializers import pipeline_stage_to_dict

    row = next((p for p in service.build_pipelines() if p.linked_pipeline_id), None)
    if row is None:
        raise SharedPipelineError("This service does not use a shared pipeline.")
    shared = row.linked_pipeline
    home = home_of(shared)
    mode = (mode or "restore").strip().lower()
    if mode not in ("restore", "copy"):
        raise SharedPipelineError("Choose restore or copy.")

    if mode == "copy":
        if shared is None:
            raise SharedPipelineError(
                "The shared pipeline no longer exists, so there is nothing to copy."
            )
        stages = [_as_payload(pipeline_stage_to_dict(stage)) for stage in shared.stages]
        missing = _missing_secret_names(
            stages, pipelines_service._known_secret_keys(service.id)
        )
        if missing:
            raise SharedPipelineError(
                f"The shared pipeline uses secrets this service does not have: {', '.join(missing)}. "
                "Add them under Settings → Secrets first (the copy would otherwise lose them), "
                "or go back to this service's own pipeline instead."
            )
        try:
            pipelines_service._apply_stages(row, stages, actor=actor)
            row.parameters = pipelines_service._parameters(
                pipelines_service.parameter_definitions(shared)
            )
            row.post_actions = pipelines_service._post_actions(
                list(shared.post_actions or []), service.id
            )
        except pipelines_service.PipelineError:
            db.session.rollback()
            raise

    row.linked_pipeline_id = None
    row.version = int(row.version or 1) + 1
    row.updated_at = datetime.now(timezone.utc)
    db.session.add(row)
    db.session.commit()
    log_audit(
        "ci_shared_pipeline_detached",
        actor=actor,
        target_type="ci_service",
        target_id=str(service.id),
        details={
            "service": service.slug,
            "pipeline": home.slug if home else None,
            "mode": mode,
        },
    )
    return pipelines_service.list_pipelines(service)[0]


def _iso(value) -> Optional[str]:
    from .serializers import _iso as iso

    return iso(value)
