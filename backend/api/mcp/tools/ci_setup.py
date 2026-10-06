"""Setting CI up: registering a service, and pipelines outside any service.

The ``ci`` module answers about services that exist and edits their pipelines.
This one creates things, which is a different kind of write and worth keeping
apart:

* **A new CI service** is a new buildable application in the catalog. Creating
  one is cheap to undo (delete it in the UI) but easy to do twice — the slug
  becomes ``payments-2`` and nobody notices — so the tool refuses a second
  service with the same name or the same repository unless told the duplicate is
  intended.
* **A standalone pipeline** (the Pipelines page) belongs to no service. It runs
  on its own, and services can build with it. Once created it is addressed by
  its slug in every ``ci`` tool — ``kubesight_pipeline_get``,
  ``kubesight_pipeline_stage_add``, ``kubesight_build_run`` … — because it is
  stored as a service row of kind ``pipeline``.
* **Attaching** one to a service changes what that service builds from its next
  build on. The service's own stages are kept, and detaching can put them back.

Everything goes through the same service functions the UI posts to, so the
validation, audit trail and permissions are the UI's.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from ...access_engine import user_has_permission
from ...db import db
from ...models_ci import APPLICATION_TYPES, CiService
from ..protocol import ToolError
from .ci import _checked_fields, _pipeline_error, _service_or_error
from .registry import MAX_ROWS, _limit, tool as _register


def tool(name, **kwargs):
    kwargs.setdefault("domain", "ci")
    return _register(name, **kwargs)


_APP_TYPES = [item for item in APPLICATION_TYPES if item != "java"]


def _credential_id(reference: Any) -> Optional[int]:
    """A source credential by id or name; None when not given."""
    from ...models_application_intelligence import BitbucketCredentialProfile

    raw = str(reference or "").strip()
    if not raw:
        return None
    row = None
    if raw.isdigit():
        row = db.session.get(BitbucketCredentialProfile, int(raw))
    if row is None:
        row = BitbucketCredentialProfile.query.filter(
            db.func.lower(BitbucketCredentialProfile.name) == raw.lower()
        ).first()
    if row is None or not row.enabled:
        names = [item.name for item in BitbucketCredentialProfile.query.filter_by(enabled=True).all()]
        raise ToolError(
            f"No enabled source credential '{raw}'. Available: {', '.join(names) or 'none'} "
            "(kubesight_ci_credentials_list)."
        )
    return row.id


def _registry_id(reference: Any) -> Optional[int]:
    from ...models import RegistryConnection

    raw = str(reference or "").strip()
    if not raw:
        return None
    row = None
    if raw.isdigit():
        row = db.session.get(RegistryConnection, int(raw))
    if row is None:
        row = RegistryConnection.query.filter(db.func.lower(RegistryConnection.name) == raw.lower()).first()
    if row is None:
        raise ToolError(f"No image registry '{raw}' (kubesight_registries_list).")
    return row.id


def _repo_key(url: str) -> str:
    """A repository URL compared loosely: case, trailing slash and .git aside."""
    text = str(url or "").strip().rstrip("/").lower()
    return text[:-4] if text.endswith(".git") else text


def _home_or_error(reference: Any) -> CiService:
    """A standalone pipeline by id, slug or name."""
    raw = str(reference or "").strip()
    if not raw:
        raise ToolError("Name the pipeline, by id, slug or name.")
    query = CiService.query.filter(CiService.kind == "pipeline")
    row = None
    if raw.isdigit():
        row = query.filter(CiService.id == int(raw)).first()
    row = row or query.filter(CiService.slug == raw).first()
    row = row or query.filter(db.func.lower(CiService.name) == raw.lower()).first()
    if row is None:
        known = [item.slug for item in query.limit(20).all()]
        raise ToolError(
            f"No standalone pipeline '{raw}'. Known: {', '.join(known) or 'none yet'} "
            "(kubesight_shared_pipelines_list)."
        )
    return row


def _catalog_service(reference: Any) -> CiService:
    row = _service_or_error(reference)
    if row.is_pipeline_home:
        raise ToolError(
            f"'{row.slug}' is a standalone pipeline, not a CI service. Name the service that "
            "should build with it."
        )
    return row


def _service_brief(row: CiService) -> Dict[str, Any]:
    from ...services.ci import catalog

    readiness = catalog.readiness(row)
    return {
        "id": row.id,
        "slug": row.slug,
        "name": row.name,
        "applicationType": row.application_type,
        "status": row.status,
        "repository": (
            f"{row.repository_workspace}/{row.repository_name}" if row.repository_name else None
        ),
        "defaultBranch": row.default_branch,
        "readiness": readiness["checks"],
        "ready": readiness["ready"],
        "blockedReason": catalog.can_run_build(row),
    }


# ---------------------------------------------------------------------------
# What a new service can point at
# ---------------------------------------------------------------------------

@tool(
    "kubesight_ci_credentials_list",
    permission="ci_services:view",
    description=(
        "The source credentials (Bitbucket tokens/app passwords) a CI service can clone "
        "with — names and ids only, never a secret. Use one as `credential` in "
        "kubesight_service_create."
    ),
)
def _credentials_list(_arguments: Dict[str, Any]) -> Dict[str, Any]:
    from ...services.ci import catalog

    items = [
        {
            "id": item["id"],
            "name": item["name"],
            "provider": item["provider"],
            "principal": item.get("principal"),
            "readOnly": item.get("readOnly"),
        }
        for item in catalog.list_credential_profiles()
    ]
    return {"count": len(items), "credentials": items}


# ---------------------------------------------------------------------------
# Registering a CI service
# ---------------------------------------------------------------------------

@tool(
    "kubesight_service_create",
    permission="ci_services:create",
    description=(
        "Register a new CI service (an application KubeSight builds). Give it a name and an "
        "applicationType; with repositoryUrl + credential it can build at once, using the "
        "application type's starter pipeline (and starter Dockerfile when the type builds an "
        "image) until someone customises it. Refuses a second service with the same name or "
        "repository unless allowDuplicate is true. Say what you are about to create before "
        "calling this. Afterwards: kubesight_pipeline_get to see what it will run, "
        "kubesight_shared_pipeline_attach to build with a standalone pipeline instead."
    ),
    write=True,
    schema={
        "type": "object",
        "properties": {
            "name": {"type": "string", "description": "Display name, e.g. 'Payments API'. The slug is derived from it."},
            "applicationType": {"type": "string", "enum": _APP_TYPES},
            "description": {"type": "string"},
            "ownerTeam": {"type": "string"},
            "criticality": {"type": "string", "enum": ["low", "medium", "high", "critical"]},
            "repositoryUrl": {
                "type": "string",
                "description": "The repository's clone or browse URL. Optional; without it the service is registered but cannot build yet.",
            },
            "defaultBranch": {"type": "string", "description": "Defaults to main."},
            "workingDirectory": {"type": "string", "description": "Monorepo: the service's folder inside the repository."},
            "credential": {
                "type": "string",
                "description": "Source credential id or name (kubesight_ci_credentials_list). Needed with repositoryUrl; when only one is enabled it is used.",
            },
            "registry": {
                "type": "string",
                "description": "Image registry id or name the service's image stages push to (kubesight_registries_list).",
            },
            "allowDuplicate": {"type": "boolean", "description": "Create even if a service with this name or repository exists."},
        },
        "required": ["name", "applicationType"],
    },
)
def _service_create(arguments: Dict[str, Any], *, user=None) -> Dict[str, Any]:
    from ...models_application_intelligence import BitbucketCredentialProfile
    from ...services.ci import catalog

    name = str(arguments.get("name") or "").strip()
    if not name:
        raise ToolError("Give the service a name.")
    repository = str(arguments.get("repositoryUrl") or "").strip()
    working_directory = str(arguments.get("workingDirectory") or "").strip()

    if not arguments.get("allowDuplicate"):
        services = CiService.query.filter(
            db.or_(CiService.kind == "service", CiService.kind.is_(None))
        )
        same_name = services.filter(db.func.lower(CiService.name) == name.lower()).first()
        if same_name is not None:
            raise ToolError(
                f"A CI service named '{same_name.name}' already exists ('{same_name.slug}'). Use it, "
                "or pass allowDuplicate: true if a second one is really intended."
            )
        if repository:
            wanted = _repo_key(repository)
            for row in services.filter(CiService.repository_url.isnot(None)).all():
                same_repo = _repo_key(row.repository_url or "") == wanted
                if same_repo and (row.working_directory or "") == working_directory:
                    raise ToolError(
                        f"'{row.slug}' already builds {row.repository_url}"
                        + (f" ({working_directory})" if working_directory else "")
                        + ". Use it, or pass allowDuplicate: true."
                    )

    payload: Dict[str, Any] = {
        "name": name,
        "applicationType": str(arguments.get("applicationType") or "generic"),
    }
    for key in ("description", "ownerTeam", "criticality"):
        if arguments.get(key):
            payload[key] = arguments[key]
    registry = _registry_id(arguments.get("registry"))
    if registry:
        payload["registryConnectionId"] = registry
    credential_note = ""
    if repository:
        credential = _credential_id(arguments.get("credential"))
        if credential is None:
            enabled = BitbucketCredentialProfile.query.filter_by(enabled=True).all()
            if len(enabled) != 1:
                raise ToolError(
                    "Say which source credential clones this repository: "
                    + (", ".join(item.name for item in enabled) or "none is configured — add one under Integrations")
                    + "."
                )
            credential = enabled[0].id
            credential_note = f"Cloned with '{enabled[0].name}', the only enabled credential."
        payload.update(
            {
                "repositoryUrl": repository,
                "credentialProfileId": credential,
                "defaultBranch": str(arguments.get("defaultBranch") or "main"),
            }
        )
        if working_directory:
            payload["workingDirectory"] = working_directory

    try:
        created = catalog.create_service(payload, actor=user)
    except catalog.CatalogError as exc:
        raise ToolError(str(exc))
    row = db.session.get(CiService, int(created["id"]))
    brief = _service_brief(row)
    next_steps: List[str] = []
    if not row.source_ready():
        next_steps.append("Connect its repository (repositoryUrl + credential) before it can build.")
    if not row.registry_connection_id:
        next_steps.append("No image registry: image stages will be skipped until one is set in its Settings.")
    next_steps.append(
        "It builds with the starter pipeline for its type until customised — kubesight_pipeline_get shows it."
    )
    return {
        **brief,
        "created": True,
        "changed": f"registered the CI service '{row.slug}'",
        "note": credential_note,
        "nextSteps": next_steps,
    }


# ---------------------------------------------------------------------------
# Pipelines outside services
# ---------------------------------------------------------------------------

@tool(
    "kubesight_shared_pipelines_list",
    permission="ci_pipelines:view",
    description=(
        "Standalone pipelines (the Pipelines page): pipelines that belong to no CI service. "
        "Each runs on its own and can be shared — attached to services, which then build with "
        "its stages. Shows stages, which services use each, and the last run. Address one by "
        "its slug in any ci tool (kubesight_pipeline_get, kubesight_build_run, ...)."
    ),
    schema={
        "type": "object",
        "properties": {
            "search": {"type": "string"},
            "pipeline": {"type": "string", "description": "One pipeline (id/slug/name) — adds who uses it."},
            "limit": {"type": "integer", "minimum": 1, "maximum": MAX_ROWS},
        },
    },
)
def _shared_list(arguments: Dict[str, Any]) -> Dict[str, Any]:
    from ...services.ci import shared_pipelines

    if arguments.get("pipeline"):
        home = _home_or_error(arguments.get("pipeline"))
        item = shared_pipelines.home_to_dict(home, include_used_by=True)
        return {"pipeline": _home_brief(item, used_by=item.get("usedBy") or [])}
    items = shared_pipelines.list_homes(search=str(arguments.get("search") or ""))
    trimmed = [_home_brief(item) for item in items][: _limit(arguments, MAX_ROWS)]
    return {"count": len(trimmed), "pipelines": trimmed}


def _home_brief(item: Dict[str, Any], used_by: Optional[List[Dict[str, Any]]] = None) -> Dict[str, Any]:
    out = {
        "id": item["id"],
        "slug": item["slug"],
        "name": item["name"],
        "description": item.get("description"),
        "status": item.get("status"),
        "version": item.get("pipelineVersion"),
        "stages": item.get("stageNames") or [],
        "repository": (
            f"{item['repositoryWorkspace']}/{item['repositoryName']}" if item.get("repositoryName") else None
        ),
        "usedByCount": item.get("usedByCount", 0),
        "latestBuild": item.get("latestBuild"),
    }
    if used_by is not None:
        out["usedBy"] = [
            {"service": entry["serviceSlug"], "name": entry["serviceName"], "latestBuild": entry.get("latestBuild")}
            for entry in used_by
        ]
    return out


@tool(
    "kubesight_shared_pipeline_create",
    permission="ci_pipelines:edit",
    description=(
        "Create a standalone pipeline (the Pipelines page) — one that belongs to no CI "
        "service. It can run on its own (a nightly job, a cleanup, a release train; a "
        "repository is optional) and/or be attached to services with "
        "kubesight_shared_pipeline_attach. Start it empty, from an application type's starter "
        "(startFrom: template), or as a copy of a service's pipeline (startFrom: service); "
        "`stages` given here replace whatever it started with. Needs ci_services:create too. "
        "Afterwards use its slug with the ci tools: kubesight_pipeline_stage_add, "
        "kubesight_build_run."
    ),
    write=True,
    schema={
        "type": "object",
        "properties": {
            "name": {"type": "string"},
            "description": {"type": "string"},
            "startFrom": {"type": "string", "enum": ["empty", "template", "service"]},
            "applicationType": {
                "type": "string",
                "enum": _APP_TYPES,
                "description": "With startFrom: template — whose starter stages to begin with.",
            },
            "fromService": {"type": "string", "description": "With startFrom: service — the service (id/slug) to copy."},
            "stages": {
                "type": "array",
                "items": {"type": "object"},
                "description": "Optional full stage list, same shape as kubesight_pipeline_save.",
            },
            "parameters": {"type": "array", "description": "Optional build inputs."},
        },
        "required": ["name"],
    },
)
def _shared_create(arguments: Dict[str, Any], *, user=None) -> Dict[str, Any]:
    from ...services.ci import pipelines, shared_pipelines

    if user is not None and not user_has_permission(user, "ci_services:create"):
        raise ToolError(
            "Creating a standalone pipeline also needs the 'ci_services:create' permission, "
            "which this token does not have."
        )
    start = str(arguments.get("startFrom") or "empty")
    payload: Dict[str, Any] = {
        "name": arguments.get("name"),
        "description": arguments.get("description") or "",
        "applicationType": arguments.get("applicationType") or "generic",
        "startFrom": {"type": start},
    }
    if start == "template":
        payload["startFrom"]["applicationType"] = arguments.get("applicationType") or "generic"
    if start == "service":
        payload["startFrom"]["serviceId"] = _catalog_service(arguments.get("fromService")).id
    try:
        created = shared_pipelines.create_home(payload, actor=user)
    except (shared_pipelines.SharedPipelineError, pipelines.PipelineError) as exc:
        raise ToolError(str(exc))

    stages = arguments.get("stages")
    if isinstance(stages, list) and stages:
        from ...models_ci import CiPipeline

        row = db.session.get(CiPipeline, int(created["pipelineId"]))
        update: Dict[str, Any] = {"stages": [_checked_fields(stage, what="Each stage") for stage in stages]}
        if isinstance(arguments.get("parameters"), list):
            update["parameters"] = arguments["parameters"]
        try:
            pipelines.update_pipeline(row, update, actor=user)
        except pipelines.PipelineError as exc:
            raise ToolError(
                f"The pipeline '{created['slug']}' was created, but its stages were refused: "
                f"{_pipeline_error(exc)}. Fix them with kubesight_pipeline_save {{service: "
                f"'{created['slug']}'}}."
            )
    home = db.session.get(CiService, int(created["id"]))
    item = shared_pipelines.home_to_dict(home)
    return {
        **_home_brief(item),
        "created": True,
        "changed": f"created the standalone pipeline '{home.slug}' with {len(item.get('stageNames') or [])} stages",
        "blockedReason": _service_brief(home)["blockedReason"],
        "nextSteps": [
            f"Edit its stages with the pipeline tools, service '{home.slug}'.",
            f"Run it on its own: kubesight_build_run {{service: '{home.slug}'}}.",
            f"Let a service build with it: kubesight_shared_pipeline_attach {{pipeline: '{home.slug}', service: …}}.",
        ],
    }


@tool(
    "kubesight_shared_pipeline_attach",
    permission="ci_pipelines:edit",
    description=(
        "Make a CI service build with a standalone pipeline from its next build on. The "
        "service keeps its repository, Dockerfile, registry, secrets and build history; only "
        "the stages, build inputs and post actions come from the pipeline. Its own stages are "
        "kept for kubesight_shared_pipeline_detach. Confirm with the person first — it changes "
        "what that service builds."
    ),
    write=True,
    schema={
        "type": "object",
        "properties": {
            "service": {"type": "string", "description": "The CI service (id/slug)."},
            "pipeline": {"type": "string", "description": "The standalone pipeline (id/slug/name)."},
        },
        "required": ["service", "pipeline"],
    },
)
def _shared_attach(arguments: Dict[str, Any], *, user=None) -> Dict[str, Any]:
    from ...services.ci import shared_pipelines

    service = _catalog_service(arguments.get("service"))
    home = _home_or_error(arguments.get("pipeline"))
    try:
        result = shared_pipelines.attach(service, home, actor=user)
    except shared_pipelines.SharedPipelineError as exc:
        raise ToolError(str(exc))
    return {
        "service": service.slug,
        "pipeline": home.slug,
        "changed": f"{service.slug} now builds with '{home.name}' (version {home.default_pipeline().version})",
        "stages": [stage.get("name") for stage in result.get("stages") or []],
        "ownStagesKept": result.get("ownStageCount", 0),
    }


@tool(
    "kubesight_shared_pipeline_detach",
    permission="ci_pipelines:edit",
    description=(
        "Stop a CI service building with a standalone pipeline. mode 'restore' (default) puts "
        "back the service's own stages as they were (or the starter pipeline if it never had "
        "any); 'copy' copies the shared stages into the service so it can edit them, and later "
        "changes to the shared pipeline no longer reach it. Confirm with the person first."
    ),
    write=True,
    schema={
        "type": "object",
        "properties": {
            "service": {"type": "string"},
            "mode": {"type": "string", "enum": ["restore", "copy"]},
        },
        "required": ["service"],
    },
)
def _shared_detach(arguments: Dict[str, Any], *, user=None) -> Dict[str, Any]:
    from ...services.ci import pipelines, shared_pipelines

    service = _catalog_service(arguments.get("service"))
    mode = str(arguments.get("mode") or "restore")
    try:
        result = shared_pipelines.detach(service, mode=mode, actor=user)
    except (shared_pipelines.SharedPipelineError, pipelines.PipelineError) as exc:
        raise ToolError(str(exc))
    return {
        "service": service.slug,
        "changed": (
            "copied the shared stages in; the service owns them now"
            if mode == "copy"
            else "back to the service's own pipeline"
        ),
        "isGeneratedDefault": bool(result.get("isGeneratedDefault")),
        "stages": [stage.get("name") for stage in result.get("stages") or []],
    }
