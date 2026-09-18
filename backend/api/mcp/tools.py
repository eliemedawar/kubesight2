"""What an agent may ask KubeSight, and what it may change.

Most tools here read. Four of them write, and all four write exactly one kind
of thing: a service's pipeline. That boundary is drawn deliberately.

**Writes are the pipeline and nothing else.** An agent can rewrite what a build
runs; it cannot trigger a build, delete a service, write a secret or touch a
cluster. A pipeline edit is reviewable after the fact — it is stored, versioned,
audited, and a build snapshots the version it ran — which is what makes it the
one surface worth opening. Starting a build is not reversible, so it stays on
the ordinary API with a person pressing the button.

**Each tool declares the permission it needs**, and that permission is checked
against the calling token's user through the same access engine every HTTP route
uses. The write tools ask for ``ci_pipelines:edit``, which is the same
permission the editor screen requires — so a token minted without it can read
everything here and change nothing, and that is the whole gate. There is no
second, MCP-specific switch, because a permission system an administrator
cannot see in the roles screen is one they will forget exists.

**A write is never a blind overwrite.** The edit tools read the current stages
out of the database, change what was asked, and save the whole thing back
through ``pipelines.update_pipeline`` — the same validator the UI posts to. An
agent therefore cannot lose a field it did not know to send, which is what would
happen if it had to echo a pipeline it had only read a summary of.

**Answers come back twice**: a short human-readable summary and the full
structured payload. The summary is what a model reads when it is deciding what
to ask next; the structure is what it reads when it is answering precisely. A
tool that returned only prose would force the model to parse its own English,
and one that returned only JSON would make cheap orientation expensive.
"""

from __future__ import annotations

import re
from typing import Any, Callable, Dict, List, NamedTuple, Optional

from ..access_engine import user_has_permission
from ..db import db
from ..models_ci import CiBuild, CiRunner, CiService
from .protocol import ToolError

# name -> {"permission", "description", "schema", "run", "write", "destructive"}
_REGISTRY: Dict[str, Dict[str, Any]] = {}

MAX_ROWS = 100
MAX_LOG_LINES = 400
MAX_FILE_LINES = 1200
MAX_FILE_CHARS = 120_000
MAX_TREE_PATHS = 2_000


def tool(
    name: str,
    *,
    permission: str,
    description: str,
    schema: Optional[Dict[str, Any]] = None,
    write: bool = False,
    destructive: bool = False,
) -> Callable:
    """Register one tool, the permission it answers under, and whether it writes.

    ``write`` is not decoration: it becomes the ``readOnlyHint`` a client reads
    when it decides whether to confirm a call with a person first. A tool that
    mutated while claiming to be read-only would take that decision away from
    them, so the flag lives next to the function rather than in a list that can
    drift away from it.
    """

    def decorate(func: Callable) -> Callable:
        _REGISTRY[name] = {
            "permission": permission,
            "description": description,
            "schema": schema or {"type": "object", "properties": {}},
            "run": func,
            "write": bool(write),
            "destructive": bool(destructive),
        }
        return func

    return decorate


def _limit(arguments: Dict[str, Any], default: int = 25) -> int:
    try:
        return max(1, min(int(arguments.get("limit", default)), MAX_ROWS))
    except (TypeError, ValueError):
        return default


def _service_or_error(reference: Any) -> CiService:
    """A service by id or slug — agents have whichever is to hand."""
    raw = str(reference or "").strip()
    if not raw:
        raise ToolError("Name the service, by id or slug.")
    row = None
    if raw.isdigit():
        row = db.session.get(CiService, int(raw))
    if row is None:
        row = CiService.query.filter_by(slug=raw).first()
    if row is None:
        row = CiService.query.filter(db.func.lower(CiService.name) == raw.lower()).first()
    if row is None:
        known = [item.slug for item in CiService.query.limit(20).all()]
        raise ToolError(
            f"No service '{raw}'. Known services include: {', '.join(known) or 'none yet'}."
        )
    return row


# ---------------------------------------------------------------------------
# Orientation
# ---------------------------------------------------------------------------

@tool(
    "kubesight_overview",
    permission="ci_services:view",
    description=(
        "Start here. The state of the build floor in one call: how many services "
        "exist, which are failing or building right now, which need setup, and "
        "whether any runners are online to build them."
    ),
)
def _overview(_arguments: Dict[str, Any]) -> Dict[str, Any]:
    from ..services.ci import catalog

    items = catalog.list_services()
    summary = catalog.catalog_summary(items)
    runners = CiRunner.query.all()

    failing = [
        {
            "service": item["slug"],
            "build": (item.get("latestBuild") or {}).get("number"),
            "status": (item.get("latestBuild") or {}).get("status"),
        }
        for item in items
        if (item.get("latestBuild") or {}).get("status") in ("failed", "timeout")
    ]
    needs_setup = [
        item["slug"]
        for item in items
        if not (item.get("sourceConfigured") and item.get("pipelineConfigured"))
    ]
    return {
        "services": summary,
        "failing": failing,
        "needsSetup": needs_setup,
        "runners": [
            {
                "name": runner.name,
                "type": runner.runner_type,
                "status": runner.status,
                "enabled": bool(runner.enabled),
                "capabilities": sorted(runner.capabilities or []),
            }
            for runner in runners
        ],
        # Said explicitly because it is the most common reason a build sits
        # queued and the least obvious thing to go looking for.
        "onlineRunners": sum(1 for r in runners if r.enabled and r.status == "online"),
    }


# ---------------------------------------------------------------------------
# Services and pipelines
# ---------------------------------------------------------------------------

@tool(
    "kubesight_services_list",
    permission="ci_services:view",
    description=(
        "List services in the CI catalog, optionally filtered by a search term, "
        "status (active/paused/archived) or application type."
    ),
    schema={
        "type": "object",
        "properties": {
            "search": {"type": "string", "description": "Match name, slug, team or repository."},
            "status": {"type": "string", "enum": ["active", "paused", "archived", "all"]},
            "applicationType": {"type": "string"},
            "limit": {"type": "integer", "minimum": 1, "maximum": MAX_ROWS},
        },
    },
)
def _services_list(arguments: Dict[str, Any]) -> Dict[str, Any]:
    from ..services.ci import catalog

    items = catalog.list_services(
        search=str(arguments.get("search") or ""),
        status=str(arguments.get("status") or ""),
        application_type=str(arguments.get("applicationType") or ""),
    )
    trimmed = [
        {
            "id": item["id"],
            "slug": item["slug"],
            "name": item["name"],
            "applicationType": item["applicationType"],
            "status": item["status"],
            "ownerTeam": item.get("ownerTeam"),
            "repository": (
                f"{item['repositoryWorkspace']}/{item['repositoryName']}"
                if item.get("repositoryName")
                else None
            ),
            "defaultBranch": item.get("defaultBranch"),
            "sourceConfigured": item["sourceConfigured"],
            "pipelineConfigured": item["pipelineConfigured"],
            "latestBuild": item.get("latestBuild"),
        }
        for item in items
    ][: _limit(arguments, MAX_ROWS)]
    return {"count": len(trimmed), "totalInCatalog": len(items), "services": trimmed}


@tool(
    "kubesight_service_get",
    permission="ci_services:view",
    description=(
        "Everything about one service: identity, repository, detected application "
        "profile, readiness to build, the secrets it expects, and recent builds. "
        "Accepts an id or a slug."
    ),
    schema={
        "type": "object",
        "properties": {"service": {"type": "string", "description": "Service id or slug."}},
        "required": ["service"],
    },
)
def _service_get(arguments: Dict[str, Any]) -> Dict[str, Any]:
    from ..services.ci import catalog

    row = _service_or_error(arguments.get("service"))
    summary = catalog.service_summary(row)
    return {
        "service": summary["service"],
        "readiness": summary["readiness"],
        "expectedSecrets": summary["expectedSecrets"],
        "recentBuilds": summary["recentBuilds"],
        "stats": summary["stats"],
        # Why a build cannot start, when it cannot. The single most useful
        # sentence for "why will this not run".
        "blockedReason": catalog.can_run_build(row),
    }


@tool(
    "kubesight_pipeline_get",
    permission="ci_pipelines:view",
    description=(
        "The pipeline a service builds with: every stage in order, with the "
        "commands it runs, the image, the runner it needs, its artifacts and the "
        "secrets it references by name. Says whether the pipeline is saved or is "
        "KubeSight's unsaved default."
    ),
    schema={
        "type": "object",
        "properties": {"service": {"type": "string", "description": "Service id or slug."}},
        "required": ["service"],
    },
)
def _pipeline_get(arguments: Dict[str, Any]) -> Dict[str, Any]:
    from ..services.ci import pipelines

    row = _service_or_error(arguments.get("service"))
    items = pipelines.list_pipelines(row)
    if not items:
        raise ToolError(f"'{row.slug}' has no pipeline configured.")
    pipeline = items[0]
    return {
        "service": row.slug,
        "name": pipeline["name"],
        "version": pipeline.get("version"),
        "isGeneratedDefault": bool(pipeline.get("isGeneratedDefault")),
        "parameters": pipeline.get("parameters") or [],
        "stages": [
            {
                "position": stage.get("position"),
                "name": stage.get("name"),
                "stageType": stage.get("stageType"),
                "image": stage.get("image"),
                "runnerType": stage.get("runnerType"),
                "runnerLabels": stage.get("runnerLabels"),
                "commands": stage.get("commands"),
                "artifacts": stage.get("artifacts"),
                # Name and destination variable — never a value. The mapping
                # is here because an agent rewriting this stage has to be able
                # to put it back; the name alone would silently re-point a
                # secret at a variable of the same name on the next save.
                "secretRefs": [
                    {"name": ref.get("name"), "envVar": ref.get("envVar") or ref.get("name")}
                    for ref in stage.get("secretRefs") or []
                ],
                "hostAliases": stage.get("hostAliases"),
                "timeoutSeconds": stage.get("timeoutSeconds"),
                "enabled": stage.get("enabled"),
            }
            for stage in pipeline.get("stages") or []
        ],
    }


# ---------------------------------------------------------------------------
# Editing a pipeline
# ---------------------------------------------------------------------------
#
# The only writes in this file, and they share one shape: read the stages that
# are there, change what was asked, hand the whole list to the ordinary
# validator, save. An agent never posts a pipeline it assembled from a summary,
# which is what makes a one-field edit safe — nothing it did not mention can be
# dropped.
#
# Three consequences worth knowing before reading the tools:
#
# * **A stage save is a full replace.** ``pipelines._apply_stages`` clears and
#   rebuilds, so stage ids are not stable across an edit. Builds snapshot their
#   pipeline, so history is unaffected — but an agent must not cache a stage id
#   across a write.
# * **A generated default is materialised on first edit**, exactly as the editor
#   screen does it. Editing "the pipeline" of a service that never saved one
#   turns KubeSight's suggestion into that service's own pipeline, and the
#   answer says so.
# * **Secrets are referenced, never written.** ``_secret_refs`` refuses a name
#   that is not already a secret of this service, so an agent cannot invent one;
#   creating secrets stays with a person.

# What a stage may carry. Anything else in a changes object is a typo, and
# normalize_stage would ignore it silently — so it is rejected here instead.
STAGE_FIELDS = {
    "name", "stageType", "runnerType", "runnerLabels", "image", "workingDirectory",
    "commands", "env", "secretRefs", "artifacts", "resources", "hostAliases",
    "runCondition", "imageScan", "timeoutSeconds", "continueOnFailure",
    "parallelGroup", "enabled",
}


_MISSING_SECRET_RE = re.compile(r"references secret '([^']+)'")


def _saved_secret_names(row) -> set:
    """Secret names the stored pipeline already references."""
    if row is None:
        return set()
    return {
        str(ref.get("name"))
        for stage in row.stages
        for ref in (stage.secret_refs or [])
        if isinstance(ref, dict)
    }


def _pipeline_error(exc: Exception, *, already_saved: Optional[set] = None) -> ToolError:
    """Validator messages are written for a person and name the stage — keep them.

    This is the correction loop: an agent that sent a bad stage is told exactly
    what was wrong and can send a good one, rather than being told the call
    failed.

    One case needs a sentence the validator cannot supply. Because every save is
    a full replace, a reference that was valid when it was saved and whose secret
    has since been deleted fails a save that never touched it — and the agent, on
    the face of the message, has no way to tell that it did not cause this and
    cannot fix it by sending different stages. Saying so is the difference
    between a dead end and a thing to go and ask somebody for.
    """
    message = str(exc) or "The pipeline was rejected."
    match = _MISSING_SECRET_RE.search(message)
    if match and match.group(1) in (already_saved or set()):
        message += (
            f" That reference was already in the saved pipeline — the secret has "
            f"been deleted since. Nothing was changed. Either re-create the secret "
            f"'{match.group(1)}', or drop the reference from the stage that holds it."
        )
    return ToolError(message)


class _Editable(NamedTuple):
    """The pipeline an edit is about to be applied to.

    ``stages`` and ``parameters`` are in the API's own camelCase shape — the same
    one ``pipelines.normalize_stage`` and ``pipelines._parameters`` read — so they
    can be edited and handed straight back without a translation step that could
    lose a field.
    """

    service: Any
    row: Any  # CiPipeline, or None when nothing is saved yet
    stages: List[Dict[str, Any]]
    parameters: List[Dict[str, Any]]
    generated: bool


def _editable_pipeline(reference: Any) -> "_Editable":
    """The service, its default pipeline row if it has one, and what is in it."""
    from ..models_ci import CiPipeline
    from ..services.ci import pipelines
    from ..services.ci.serializers import pipeline_stage_to_dict

    service = _service_or_error(reference)
    row = (
        CiPipeline.query.filter_by(service_id=service.id, is_default=True).first()
        or CiPipeline.query.filter_by(service_id=service.id)
        .order_by(CiPipeline.id.asc())
        .first()
    )
    if row is not None and row.stages:
        return _Editable(
            service,
            row,
            [pipeline_stage_to_dict(stage) for stage in row.stages],
            list(row.parameters or []),
            False,
        )

    # No saved stages: start from what KubeSight would run anyway, so an edit
    # against a generated default adds to it rather than replacing it with one
    # stage. Its parameters come along for the same reason — materialising the
    # default must not quietly drop the build inputs its Run Build dialog asks
    # for.
    generated = pipelines.list_pipelines(service)[0]
    return _Editable(
        service,
        row,
        list(generated.get("stages") or []),
        list(generated.get("parameters") or []),
        True,
    )


def _save_stages(
    target: "_Editable",
    stages: List[Dict[str, Any]],
    *,
    user,
    name: Optional[str] = None,
    description: Optional[str] = None,
    parameters: Any = None,
) -> Dict[str, Any]:
    """Persist a stage list through the same path the editor screen posts to."""
    from ..services.ci import pipelines

    row = target.row
    payload: Dict[str, Any] = {
        "name": name or (row.name if row is not None else "default"),
        "isDefault": True,
        "stages": stages,
        # Always sent, never left to a default: an update that omitted them
        # would keep whatever is stored, which is the wrong answer on the save
        # that materialises a generated default.
        "parameters": parameters if parameters is not None else target.parameters,
    }
    if description is not None:
        payload["description"] = description

    try:
        if row is None:
            return pipelines.create_pipeline(target.service, payload, actor=user)
        return pipelines.update_pipeline(row, payload, actor=user)
    except pipelines.PipelineError as exc:
        raise _pipeline_error(exc, already_saved=_saved_secret_names(row))


def _saved_summary(target: "_Editable", saved: Dict[str, Any], change: str) -> Dict[str, Any]:
    """What a write answers with: what changed, and the pipeline that resulted."""
    return {
        "service": target.service.slug,
        "changed": change,
        "pipelineId": saved.get("id"),
        "name": saved.get("name"),
        "version": saved.get("version"),
        # Said plainly, because it is a bigger change than the edit itself: this
        # service now has a pipeline of its own and will stop tracking the
        # generated default.
        "materialisedGeneratedDefault": bool(target.generated),
        "stages": [
            {
                "position": stage.get("position"),
                "name": stage.get("name"),
                "stageType": stage.get("stageType"),
                "image": stage.get("image"),
                "commands": stage.get("commands"),
                "enabled": stage.get("enabled"),
            }
            for stage in saved.get("stages") or []
        ],
        "note": (
            "Builds already running or finished are unaffected — each one runs a "
            "snapshot of the pipeline as it was when it started."
        ),
    }


def _stage_index(stages: List[Dict[str, Any]], selector: Any) -> int:
    """Find one stage by name or 1-based position, or say what the names are."""
    raw = str(selector if selector is not None else "").strip()
    if not raw:
        raise ToolError("Name the stage to change, by name or 1-based position.")
    if raw.isdigit():
        position = int(raw)
        if 1 <= position <= len(stages):
            return position - 1
        raise ToolError(
            f"This pipeline has {len(stages)} stages, so there is no stage {position}."
        )
    for index, stage in enumerate(stages):
        if str(stage.get("name") or "").lower() == raw.lower():
            return index
    names = ", ".join(str(stage.get("name")) for stage in stages) or "none"
    raise ToolError(f"No stage '{raw}'. This pipeline's stages are: {names}.")


def _checked_fields(payload: Any, *, what: str) -> Dict[str, Any]:
    if not isinstance(payload, dict) or not payload:
        raise ToolError(f"{what} must be an object with at least one field.")
    unknown = sorted(set(payload) - STAGE_FIELDS)
    if unknown:
        raise ToolError(
            f"A stage has no field {', '.join(unknown)}. Valid fields: "
            + ", ".join(sorted(STAGE_FIELDS))
            + "."
        )
    return dict(payload)


_STAGE_SCHEMA = {
    "type": "object",
    "description": (
        "A stage. name is required; stageType is checkout, command or "
        "container_image (default command); a command stage needs commands. "
        "Other fields: image, runnerType, runnerLabels, workingDirectory, env, "
        "secretRefs [{name, envVar}], artifacts [{path, type, name}], "
        "hostAliases, runCondition, resources, timeoutSeconds, "
        "continueOnFailure, parallelGroup, enabled. On a container_image stage, "
        "imageScan {enabled, scanner: trivy, threshold: critical|high|medium|low, "
        "onFail: block|warn, ignoreUnfixed} gates the push on a vulnerability "
        "scan of the image the stage just built — enabled=true means a "
        "finding at or above threshold stops the image reaching the registry."
    ),
    "properties": {
        "name": {"type": "string"},
        "stageType": {"type": "string", "enum": ["checkout", "command", "container_image"]},
        "image": {"type": "string"},
        "commands": {"type": "array", "items": {"type": "string"}},
        "runnerLabels": {"type": "array", "items": {"type": "string"}},
        "env": {"type": "object"},
        "imageScan": {
            "type": "object",
            "description": (
                "container_image stages only. Gates the push on a scan of the "
                "image just built."
            ),
            "properties": {
                "enabled": {"type": "boolean"},
                "scanner": {"type": "string", "enum": ["trivy"]},
                "threshold": {
                    "type": "string",
                    "enum": ["critical", "high", "medium", "low"],
                },
                "onFail": {"type": "string", "enum": ["block", "warn"]},
                "ignoreUnfixed": {"type": "boolean"},
            },
        },
        "enabled": {"type": "boolean"},
    },
    "required": ["name"],
}


@tool(
    "kubesight_pipeline_stage_update",
    permission="ci_pipelines:edit",
    description=(
        "Change fields on ONE stage of a service's pipeline and save. Only the "
        "fields in changes are touched; everything else on that stage is kept, "
        "so you do not need to read the pipeline back first. Identify the stage "
        "by name or 1-based position. Use this for 'add a flag to the build "
        "command', 'bump the image', 'disable the test stage'."
    ),
    write=True,
    schema={
        "type": "object",
        "properties": {
            "service": {"type": "string", "description": "Service id or slug."},
            "stage": {"type": "string", "description": "Stage name or 1-based position."},
            "changes": {
                "type": "object",
                "description": (
                    "Fields to set. Any stage field is allowed, including name to "
                    "rename. A list or object REPLACES the current value — to add "
                    "one command, send the full command list."
                ),
            },
        },
        "required": ["service", "stage", "changes"],
    },
)
def _pipeline_stage_update(arguments: Dict[str, Any], *, user=None) -> Dict[str, Any]:
    target = _editable_pipeline(arguments.get("service"))
    if not target.stages:
        raise ToolError(
            f"'{target.service.slug}' has no pipeline stages yet. Use "
            "kubesight_pipeline_stage_add or kubesight_pipeline_save first."
        )

    stages = target.stages
    index = _stage_index(stages, arguments.get("stage"))
    changes = _checked_fields(arguments.get("changes"), what="changes")
    before = str(stages[index].get("name"))
    stages[index] = {**stages[index], **changes}

    saved = _save_stages(target, stages, user=user)
    fields = ", ".join(sorted(changes))
    return _saved_summary(target, saved, f"stage '{before}': {fields}")


@tool(
    "kubesight_pipeline_stage_add",
    permission="ci_pipelines:edit",
    description=(
        "Insert a new stage into a service's pipeline and save. Appends at the "
        "end unless 'after' names an existing stage. The existing stages are "
        "kept as they are. There is no dependency graph — order is the only "
        "relationship between stages."
    ),
    write=True,
    schema={
        "type": "object",
        "properties": {
            "service": {"type": "string", "description": "Service id or slug."},
            "stage": _STAGE_SCHEMA,
            "after": {
                "type": "string",
                "description": (
                    "Insert directly after this stage (name or 1-based position). "
                    "Omit to append at the end; 'start' to put it first."
                ),
            },
        },
        "required": ["service", "stage"],
    },
)
def _pipeline_stage_add(arguments: Dict[str, Any], *, user=None) -> Dict[str, Any]:
    target = _editable_pipeline(arguments.get("service"))
    stage = _checked_fields(arguments.get("stage"), what="stage")
    if not str(stage.get("name") or "").strip():
        raise ToolError("The new stage needs a name.")

    stages = target.stages
    after = str(arguments.get("after") or "").strip()
    if not after:
        at = len(stages)
    elif after.lower() == "start":
        at = 0
    else:
        at = _stage_index(stages, after) + 1
    stages.insert(at, stage)

    saved = _save_stages(target, stages, user=user)
    return _saved_summary(
        target, saved, f"added stage '{stage['name']}' at position {at + 1}"
    )


@tool(
    "kubesight_pipeline_stage_remove",
    permission="ci_pipelines:edit",
    description=(
        "Delete one stage from a service's pipeline and save. To stop a stage "
        "running without losing what it did, prefer setting enabled:false with "
        "kubesight_pipeline_stage_update — a removed stage is not recoverable "
        "from KubeSight."
    ),
    write=True,
    destructive=True,
    schema={
        "type": "object",
        "properties": {
            "service": {"type": "string", "description": "Service id or slug."},
            "stage": {"type": "string", "description": "Stage name or 1-based position."},
        },
        "required": ["service", "stage"],
    },
)
def _pipeline_stage_remove(arguments: Dict[str, Any], *, user=None) -> Dict[str, Any]:
    target = _editable_pipeline(arguments.get("service"))
    index = _stage_index(target.stages, arguments.get("stage"))
    removed = str(target.stages[index].get("name"))
    remaining = [
        stage for position, stage in enumerate(target.stages) if position != index
    ]
    if not remaining:
        raise ToolError(
            f"'{removed}' is the only stage — removing it would leave a pipeline "
            "that builds nothing. Disable it instead, or replace the pipeline "
            "with kubesight_pipeline_save."
        )

    saved = _save_stages(target, remaining, user=user)
    return _saved_summary(target, saved, f"removed stage '{removed}'")


@tool(
    "kubesight_pipeline_save",
    permission="ci_pipelines:edit",
    description=(
        "REPLACE a service's whole pipeline with the stages given, and save. "
        "Every existing stage is discarded — for a change to one stage use "
        "kubesight_pipeline_stage_update instead, which cannot lose the fields "
        "you did not send. Images should come from kubesight_build_environments; "
        "secretRefs may only name secrets the service already has."
    ),
    write=True,
    destructive=True,
    schema={
        "type": "object",
        "properties": {
            "service": {"type": "string", "description": "Service id or slug."},
            "stages": {
                "type": "array",
                "items": _STAGE_SCHEMA,
                "description": "The complete stage list, in execution order.",
            },
            "name": {
                "type": "string",
                "description": "Pipeline name. Defaults to the current one.",
            },
            "description": {"type": "string"},
            "parameters": {
                "type": "array",
                "description": "Build inputs. Omit to keep the current ones.",
            },
        },
        "required": ["service", "stages"],
    },
)
def _pipeline_save(arguments: Dict[str, Any], *, user=None) -> Dict[str, Any]:
    target = _editable_pipeline(arguments.get("service"))

    stages = arguments.get("stages")
    if not isinstance(stages, list) or not stages:
        raise ToolError(
            "stages must be a non-empty list. A pipeline with no stages builds nothing."
        )
    checked = [_checked_fields(stage, what="Each stage") for stage in stages]

    saved = _save_stages(
        target,
        checked,
        user=user,
        name=str(arguments.get("name") or "") or None,
        description=arguments.get("description"),
        parameters=arguments.get("parameters"),
    )
    return _saved_summary(
        target, saved, f"replaced the pipeline with {len(checked)} stages"
    )


# ---------------------------------------------------------------------------
# The source a service builds from
# ---------------------------------------------------------------------------
#
# Read straight from the host over its API — no clone, no workspace, no build
# pod. That is why these are cheap enough for an agent to browse with, and it is
# also their limit: they see what the repository holds at a revision, not what a
# build produced from it.
#
# Everything goes through the service's own stored credential. No tool here
# takes a repository URL or a token as an argument, so an agent cannot aim
# KubeSight's credentials at a repository nobody registered.


def _source_error(exc: Exception) -> ToolError:
    """Source failures are already written for a person — pass them through.

    ``SourceError`` and ``CatalogError`` messages name the credential profile or
    the missing setting. Replacing them with something generic would hide the
    one sentence that says what to fix.
    """
    return ToolError(str(exc) or "The repository could not be read.")


@tool(
    "kubesight_repo_revisions",
    permission="ci_services:view",
    description=(
        "The branches, tags and recent commits of a service's repository. Use "
        "this to find the revision to read code at, or to check that a branch a "
        "build named still exists."
    ),
    schema={
        "type": "object",
        "properties": {
            "service": {"type": "string", "description": "Service id or slug."},
            "kinds": {
                "type": "array",
                "items": {"type": "string", "enum": ["branch", "tag", "commit"]},
                "description": "Narrows the fetch. Defaults to all three.",
            },
            "limit": {"type": "integer", "minimum": 1, "maximum": MAX_ROWS},
        },
        "required": ["service"],
    },
)
def _repo_revisions(arguments: Dict[str, Any]) -> Dict[str, Any]:
    from ..services.ci import catalog
    from ..services.ci.source import SourceError

    row = _service_or_error(arguments.get("service"))
    requested = arguments.get("kinds")
    if isinstance(requested, str):
        requested = [requested]
    kinds = tuple(
        kind for kind in (requested or []) if kind in ("branch", "tag", "commit")
    )
    try:
        payload = catalog.list_branches(row, kinds=kinds)
    except (catalog.CatalogError, SourceError, ValueError) as exc:
        raise _source_error(exc)

    items = payload["items"][: _limit(arguments, MAX_ROWS)]
    return {
        "service": row.slug,
        "repository": f"{row.repository_workspace}/{row.repository_name}",
        "defaultBranch": payload.get("defaultBranch"),
        "count": len(items),
        "total": payload.get("count"),
        "revisions": items,
    }


@tool(
    "kubesight_repo_tree",
    permission="ci_services:view",
    description=(
        "Every file path in a service's repository at one revision — the shape "
        "of the project, without cloning it. Narrow with pathPrefix before "
        "reading files. If truncated is true the listing hit a ceiling, so an "
        "absent path means 'not seen', not 'not there'."
    ),
    schema={
        "type": "object",
        "properties": {
            "service": {"type": "string", "description": "Service id or slug."},
            "revision": {
                "type": "string",
                "description": (
                    "Branch, tag or commit. Defaults to the service's default branch."
                ),
            },
            "pathPrefix": {
                "type": "string",
                "description": "Only paths under this directory, e.g. 'src/main'.",
            },
            "limit": {"type": "integer", "minimum": 1, "maximum": MAX_TREE_PATHS},
        },
        "required": ["service"],
    },
)
def _repo_tree(arguments: Dict[str, Any]) -> Dict[str, Any]:
    from ..services.ci import catalog
    from ..services.ci.source import SourceError

    row = _service_or_error(arguments.get("service"))
    try:
        limit = max(1, min(int(arguments.get("limit", 500)), MAX_TREE_PATHS))
    except (TypeError, ValueError):
        limit = 500
    try:
        payload = catalog.list_source_tree(
            row,
            revision=str(arguments.get("revision") or ""),
            path_prefix=str(arguments.get("pathPrefix") or ""),
            limit=limit,
        )
    except (catalog.CatalogError, SourceError, ValueError) as exc:
        raise _source_error(exc)
    payload["service"] = row.slug
    return payload


@tool(
    "kubesight_repo_file",
    permission="ci_services:view",
    description=(
        "One file's text from a service's repository at a revision — the actual "
        "code, a Dockerfile, a pom.xml, a Jenkinsfile. Paths are relative to the "
        "service's working directory. Use startLine/endLine on a long file "
        "rather than reading all of it."
    ),
    schema={
        "type": "object",
        "properties": {
            "service": {"type": "string", "description": "Service id or slug."},
            "path": {
                "type": "string",
                "description": (
                    "Repository path, e.g. 'build.gradle' or 'src/main/App.java'."
                ),
            },
            "revision": {
                "type": "string",
                "description": (
                    "Branch, tag or commit. Defaults to the service's default branch."
                ),
            },
            "startLine": {
                "type": "integer",
                "minimum": 1,
                "description": "1-based, inclusive.",
            },
            "endLine": {
                "type": "integer",
                "minimum": 1,
                "description": "1-based, inclusive.",
            },
        },
        "required": ["service", "path"],
    },
)
def _repo_file(arguments: Dict[str, Any]) -> Dict[str, Any]:
    from ..services.ci import catalog
    from ..services.ci.source import SourceError

    row = _service_or_error(arguments.get("service"))
    try:
        payload = catalog.read_source_file(
            row,
            str(arguments.get("path") or ""),
            revision=str(arguments.get("revision") or ""),
        )
    except (catalog.CatalogError, SourceError, ValueError) as exc:
        raise _source_error(exc)

    lines = (payload.get("content") or "").splitlines()
    total = len(lines)

    def _bound(key: str, fallback: int) -> int:
        try:
            return max(1, int(arguments[key]))
        except (KeyError, TypeError, ValueError):
            return fallback

    start = _bound("startLine", 1)
    end = _bound("endLine", start + MAX_FILE_LINES - 1)
    # A window wider than the ceiling is honoured up to the ceiling rather than
    # refused: an agent asking for a whole file should get as much of it as
    # fits, and be told that is what happened.
    end = min(end, start + MAX_FILE_LINES - 1, total)
    window = lines[start - 1 : end] if start <= total else []

    text = "\n".join(window)
    truncated = bool(window) and (start > 1 or end < total)
    if len(text) > MAX_FILE_CHARS:
        text = text[:MAX_FILE_CHARS]
        truncated = True

    return {
        "service": row.slug,
        "path": payload["path"],
        "revision": payload["revision"],
        "totalLines": total,
        "startLine": start if window else 0,
        "endLine": end if window else 0,
        "truncated": truncated,
        "content": text,
    }


# ---------------------------------------------------------------------------
# Builds
# ---------------------------------------------------------------------------

@tool(
    "kubesight_builds_list",
    permission="ci_builds:view",
    description=(
        "Recent builds, newest first. Filter by service and/or status "
        "(queued, running, success, failed, cancelled, timeout)."
    ),
    schema={
        "type": "object",
        "properties": {
            "service": {"type": "string", "description": "Service id or slug."},
            "status": {"type": "string"},
            "limit": {"type": "integer", "minimum": 1, "maximum": MAX_ROWS},
        },
    },
)
def _builds_list(arguments: Dict[str, Any]) -> Dict[str, Any]:
    from ..services.ci import engine
    from ..services.ci.serializers import build_summary

    service_id = None
    if arguments.get("service"):
        service_id = _service_or_error(arguments["service"]).id

    rows, total = engine.list_builds(
        service_id=service_id,
        status=str(arguments.get("status") or "") or None,
        limit=_limit(arguments),
    )
    return {
        "count": len(rows),
        "total": total,
        "builds": [
            {**build_summary(row), "service": row.service.slug if row.service else None}
            for row in rows
        ],
    }


@tool(
    "kubesight_build_get",
    permission="ci_builds:view",
    description=(
        "One build in full: status, the branch and commit it ran on, how long it "
        "took, and every stage with its own status, exit code and error. Use this "
        "to find WHICH stage failed, then kubesight_build_logs to see why."
    ),
    schema={
        "type": "object",
        "properties": {"buildId": {"type": "integer"}},
        "required": ["buildId"],
    },
)
def _build_get(arguments: Dict[str, Any]) -> Dict[str, Any]:
    from ..services.ci.serializers import build_to_dict

    try:
        build_id = int(arguments.get("buildId"))
    except (TypeError, ValueError):
        raise ToolError("buildId must be a number.")
    row = db.session.get(CiBuild, build_id)
    if row is None:
        raise ToolError(f"No build {build_id}.")

    payload = build_to_dict(row)
    payload["service"] = row.service.slug if row.service else None
    failed = [
        stage
        for stage in payload.get("stages") or []
        if stage.get("status") in ("failed", "timeout")
    ]
    # Pointed at directly: "which stage failed" is the question every
    # investigation starts with, and making it derivable rather than stated
    # costs a round trip every time.
    payload["failedStages"] = [
        {"id": stage["id"], "name": stage["name"], "error": stage.get("error")}
        for stage in failed
    ]
    return payload


@tool(
    "kubesight_build_logs",
    permission="ci_builds:view",
    description=(
        "The output of one build stage. Secret values are masked before they are "
        "ever stored, so this is safe to read in full. Get the stage id from "
        "kubesight_build_get."
    ),
    schema={
        "type": "object",
        "properties": {
            "buildId": {"type": "integer"},
            "stageId": {"type": "integer"},
            "tail": {
                "type": "integer",
                "description": f"Last N lines (default 200, max {MAX_LOG_LINES}).",
            },
        },
        "required": ["buildId", "stageId"],
    },
)
def _build_logs(arguments: Dict[str, Any]) -> Dict[str, Any]:
    from ..services.ci import engine, logs

    try:
        build_id = int(arguments.get("buildId"))
        stage_id = int(arguments.get("stageId"))
    except (TypeError, ValueError):
        raise ToolError("buildId and stageId must be numbers.")

    build = db.session.get(CiBuild, build_id)
    if build is None:
        raise ToolError(f"No build {build_id}.")
    try:
        stage = engine.get_build_stage(build, stage_id)
    except LookupError:
        raise ToolError(f"Build {build_id} has no stage {stage_id}.")

    try:
        tail = max(1, min(int(arguments.get("tail", 200)), MAX_LOG_LINES))
    except (TypeError, ValueError):
        tail = 200

    payload = logs.read(stage_id, after_seq=0, limit=MAX_LOG_LINES * 4)
    lines = [line["content"] for line in payload.get("lines") or []]
    return {
        "build": build_id,
        "stage": {"id": stage.id, "name": stage.name, "status": stage.status,
                  "exitCode": stage.exit_code, "error": stage.error},
        "truncated": len(lines) > tail,
        "lines": lines[-tail:],
    }


# ---------------------------------------------------------------------------
# The fleet and what it can run
# ---------------------------------------------------------------------------

@tool(
    "kubesight_runners_list",
    permission="ci_runners:view",
    description=(
        "The build runners: type, status, what each one can run, and how loaded "
        "it is. A stage runs on a runner whose capabilities cover ALL of that "
        "stage's labels — which is the usual reason a build sits queued."
    ),
)
def _runners_list(_arguments: Dict[str, Any]) -> Dict[str, Any]:
    from ..services.ci.serializers import runner_to_dict

    rows = CiRunner.query.order_by(CiRunner.name.asc()).all()
    return {
        "count": len(rows),
        "runners": [runner_to_dict(row) for row in rows],
        "matching": (
            "A stage is eligible for a runner when the runner's capabilities are a "
            "superset of the stage's runnerLabels."
        ),
    }


@tool(
    "kubesight_build_environments",
    permission="ci_pipelines:view",
    description=(
        "The approved build images a pipeline may use, and what each provides "
        "(language and version, build tool). A pipeline stage names one of these "
        "rather than an arbitrary image."
    ),
)
def _build_environments(_arguments: Dict[str, Any]) -> Dict[str, Any]:
    from ..services.ci import build_environments

    return {"environments": build_environments.catalog()}


@tool(
    "kubesight_artifacts_list",
    permission="ci_artifacts:view",
    description="Artifacts a service has produced — images, JARs, APKs, reports.",
    schema={
        "type": "object",
        "properties": {
            "service": {"type": "string", "description": "Service id or slug."},
            "limit": {"type": "integer", "minimum": 1, "maximum": MAX_ROWS},
        },
        "required": ["service"],
    },
)
def _artifacts_list(arguments: Dict[str, Any]) -> Dict[str, Any]:
    from ..models_ci import CiArtifact
    from ..services.ci.serializers import artifact_to_dict

    row = _service_or_error(arguments.get("service"))
    rows = (
        CiArtifact.query.filter_by(service_id=row.id)
        .order_by(CiArtifact.created_at.desc(), CiArtifact.id.desc())
        .limit(_limit(arguments))
        .all()
    )
    return {
        "service": row.slug,
        "count": len(rows),
        "artifacts": [artifact_to_dict(item) for item in rows],
    }


# ---------------------------------------------------------------------------
# Dispatch
# ---------------------------------------------------------------------------

def definitions() -> List[Dict[str, Any]]:
    """The tool list, in MCP's shape."""
    return [
        {
            "name": name,
            "description": entry["description"],
            "inputSchema": entry["schema"],
            # Declared per tool rather than assumed for the surface: a client
            # that asks a person before a write can only do that if the tools
            # that write say so.
            "annotations": {
                "readOnlyHint": not entry["write"],
                "destructiveHint": bool(entry["destructive"]),
            },
        }
        for name, entry in sorted(_REGISTRY.items())
    ]


def _summarise(name: str, payload: Any) -> str:
    """One line for a model deciding what to ask next."""
    if not isinstance(payload, dict):
        return f"{name}: done."
    # A write says what it did before it says how big the result is: a model
    # that reads "12 stages" after an edit cannot tell whether the edit landed.
    if payload.get("changed"):
        return f"{name}: saved — {payload['changed']}."
    for key in ("paths", "revisions", "services", "builds", "runners", "artifacts",
                "stages", "environments"):
        if isinstance(payload.get(key), list):
            return f"{name}: {len(payload[key])} {key}."
    if payload.get("content") is not None and payload.get("path"):
        return f"{name}: {payload['path']} ({payload.get('totalLines', 0)} lines)."
    if "service" in payload and isinstance(payload["service"], dict):
        return f"{name}: {payload['service'].get('slug', 'service')}."
    return f"{name}: ok."


def is_write(name: str) -> bool:
    """Whether a tool changes anything. Read by the route, for the audit row."""
    entry = _REGISTRY.get(name)
    return bool(entry and entry["write"])


def call(name: str, arguments: Dict[str, Any], *, user) -> Dict[str, Any]:
    """Run one tool as ``user``, or refuse with a reason they can act on."""
    import json

    entry = _REGISTRY.get(name)
    if entry is None:
        raise ToolError(
            f"KubeSight has no tool '{name}'. Available: "
            + ", ".join(sorted(_REGISTRY))
        )

    # The same permission the equivalent HTTP route requires. An agent never
    # sees more than the person whose token it is holding.
    if user is not None and not user_has_permission(user, entry["permission"]):
        raise ToolError(
            f"'{name}' needs the '{entry['permission']}' permission, which this "
            "token does not have."
        )

    # A write is attributed: the pipeline service records who saved it, and
    # "who" is the token holder, never KubeSight. Read tools do not receive the
    # user at all, so one cannot start acting on their behalf by accident.
    if entry["write"]:
        payload = entry["run"](arguments or {}, user=user)
    else:
        payload = entry["run"](arguments or {})
    return {
        "content": [
            {"type": "text", "text": _summarise(name, payload)},
            {"type": "text", "text": json.dumps(payload, ensure_ascii=False, default=str)},
        ],
        "structuredContent": payload if isinstance(payload, dict) else {"result": payload},
    }
