"""The CI half of KubeSight: what an agent may ask about a build, and change.

Most tools here read. Seven write, and they are two different kinds of thing
with two different risk profiles, which is worth keeping straight.

**Four of them edit a pipeline**, and a pipeline edit is reviewable after the
fact: it is stored, versioned, audited, and a build snapshots the version it
ran, so an edit somebody disagrees with can be read afterwards and put back.

**Three of them run a build** — trigger, cancel, retry. Those are not reversible
in the same way: a build pushes images and can deploy. They are here because
``ci_builds:run`` is a permission an installation grants deliberately, and a
token minted without it makes these tools cease to exist for that agent rather
than exist and always refuse. An agent that can queue a build should say which
service and which branch before it does, and should not report the build as
finished — it returns queued and nothing here waits.

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
from typing import Any, Dict, List, NamedTuple, Optional

from ...db import db
from ...models_ci import CiBuild, CiRunner, CiService
from ..protocol import ToolError
from .registry import (
    MAX_FILE_CHARS,
    MAX_FILE_LINES,
    MAX_LOG_LINES,
    MAX_ROWS,
    MAX_TREE_PATHS,
    _limit,
    tool as _register,
)


def tool(name, **kwargs):
    """Every tool in this module belongs to the ``ci`` domain."""
    kwargs.setdefault("domain", "ci")
    return _register(name, **kwargs)


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
    from ...services.ci import catalog

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
    from ...services.ci import catalog

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
    from ...services.ci import catalog

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
    from ...services.ci import pipelines

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
    from ...models_ci import CiPipeline
    from ...services.ci import pipelines
    from ...services.ci.serializers import pipeline_stage_to_dict

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
    from ...services.ci import pipelines

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
    from ...services.ci import catalog
    from ...services.ci.source import SourceError

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
    from ...services.ci import catalog
    from ...services.ci.source import SourceError

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
    from ...services.ci import catalog
    from ...services.ci.source import SourceError

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
    from ...services.ci import engine
    from ...services.ci.serializers import build_summary

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
    from ...services.ci.serializers import build_to_dict

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
    from ...services.ci import engine, logs

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


@tool(
    "kubesight_build_failure",
    permission="ci_builds:view",
    description=(
        "Why a build failed, in one call: the stage that failed and the tail of "
        "its log. Name a buildId, or name a service to get its most recent "
        "failed build. This is the tool for 'why did the build break' and 'why "
        "did my pull request get blocked' - kubesight_build_logs is for reading "
        "a specific stage you already have the id of."
    ),
    schema={
        "type": "object",
        "properties": {
            "buildId": {"type": "integer", "description": "The build to explain."},
            "service": {
                "type": "string",
                "description": (
                    "Instead of buildId: this service's most recent failed build."
                ),
            },
            "tail": {
                "type": "integer",
                "description": f"Log lines per failed stage (default 120, max {MAX_LOG_LINES}).",
            },
        },
    },
)
def _build_failure(arguments: Dict[str, Any]) -> Dict[str, Any]:
    """The failing stage and its output, without the two-step.

    Every investigation goes build -> which stage -> that stage's log, and an
    agent doing it by hand spends two round trips discovering something the
    database already knows. Worse, it has to guess which stage to read when
    several ran, and the interesting one is rarely the last.

    Bounded on purpose: the TAIL of each failed stage, not the whole log. A
    build log runs to tens of thousands of lines and the reason is nearly always
    at the end; handing a model the whole thing buries the answer it came for.
    ``kubesight_build_logs`` is still there when the tail is not enough.
    """
    from ...services.ci import logs

    build = None
    if arguments.get("buildId") is not None:
        try:
            build_id = int(arguments["buildId"])
        except (TypeError, ValueError):
            raise ToolError("buildId must be a number.")
        build = db.session.get(CiBuild, build_id)
        if build is None:
            raise ToolError(f"No build {build_id}.")
    elif arguments.get("service"):
        service = _service_or_error(arguments.get("service"))
        build = (
            CiBuild.query.filter(
                CiBuild.service_id == service.id,
                CiBuild.status.in_(("failed", "timeout")),
            )
            .order_by(CiBuild.number.desc())
            .first()
        )
        if build is None:
            return {
                "service": service.slug,
                "failed": False,
                "message": f"No failed build on record for '{service.slug}'.",
            }
    else:
        raise ToolError("Name a buildId or a service.")

    try:
        tail = max(1, min(int(arguments.get("tail", 120)), MAX_LOG_LINES))
    except (TypeError, ValueError):
        tail = 120

    stages = sorted(build.stages, key=lambda item: item.position)
    failed = [stage for stage in stages if stage.status in ("failed", "timeout")]
    if not failed and build.status in ("failed", "timeout"):
        # The build failed without any stage owning it — a runner that went
        # away, a dispatch that never happened. `build.error` is the only
        # account of it, and saying "no failed stage" without it is useless.
        return {
            "build": build.id,
            "number": build.number,
            "service": build.service.slug if build.service else None,
            "status": build.status,
            "failedStages": [],
            "error": build.error,
            "message": (
                "No stage reported the failure; the build itself did. The error "
                "above is the whole account of it."
            ),
        }

    explained = []
    for stage in failed:
        payload = logs.read(stage.id, after_seq=0, limit=MAX_LOG_LINES * 4)
        lines = [line["content"] for line in payload.get("lines") or []]
        explained.append(
            {
                "id": stage.id,
                "name": stage.name,
                "status": stage.status,
                "exitCode": stage.exit_code,
                "error": stage.error,
                "truncated": len(lines) > tail,
                "tail": lines[-tail:],
            }
        )

    return {
        "build": build.id,
        "number": build.number,
        "service": build.service.slug if build.service else None,
        "status": build.status,
        "branch": build.branch,
        "commit": (build.commit_sha or "")[:12],
        "trigger": build.trigger_type,
        "error": build.error,
        "failedStages": explained,
        "skipped": [
            stage.name for stage in stages if stage.status == "skipped"
        ],
    }


# ---------------------------------------------------------------------------
# Running one
# ---------------------------------------------------------------------------

# Triggering a build is the one CI write that is not a pipeline edit, and it is
# not reversible the way an edit is — a build pushes images and can deploy. It
# is here because a token can be minted without ``ci_builds:run`` and then this
# tool does not exist for that agent at all, which is a cleaner switch than a
# tool that exists and always refuses.
#
# Three habits the description leans on, because getting them wrong is how an
# agent runs the wrong thing:
#   - a build is QUEUED, not finished. These tools return immediately.
#   - a retry re-runs the original's coordinates, not today's HEAD.
#   - cancelling a running build is a request the runner honours on the next
#     tick, so the status will not be "cancelled" the moment this returns.

def _build_or_error(reference: Any):
    from ...services.ci import engine as engine_service

    raw = str(reference or "").strip()
    if not raw.isdigit():
        raise ToolError("Name the build by its id, as kubesight_builds_list returns it.")
    try:
        return engine_service.get_build(int(raw))
    except LookupError:
        raise ToolError(f"No build with id {raw}.")


@tool(
    "kubesight_build_run",
    permission="ci_builds:run",
    description=(
        "Queue a build of a service. Returns as soon as it is queued — the build "
        "has not run yet, and nothing here waits for it; poll kubesight_build_get "
        "for the outcome. Defaults to the service's default branch."
    ),
    write=True,
    schema={
        "type": "object",
        "properties": {
            "service": {"type": "string", "description": "Service id or slug."},
            "branch": {"type": "string", "description": "Defaults to the service's default branch."},
            "commitSha": {"type": "string"},
            "refType": {"type": "string", "enum": ["branch", "tag"]},
            "variables": {
                "type": "object",
                "description": "Per-build environment overrides, applied to every stage.",
            },
        },
        "required": ["service"],
    },
)
def _build_run(arguments: Dict[str, Any], *, user=None) -> Dict[str, Any]:
    from ...services.ci import catalog, engine as engine_service

    row = _service_or_error(arguments.get("service"))
    blocked = catalog.can_run_build(row)
    if blocked:
        # The catalog's own sentence, which names the missing piece. Refusing
        # here rather than letting the engine fail keeps the reason readable.
        raise ToolError(f"'{row.slug}' cannot build: {blocked}")
    variables = arguments.get("variables")
    try:
        data = engine_service.trigger_build(
            row,
            branch=str(arguments.get("branch") or "") or None,
            commit_sha=str(arguments.get("commitSha") or "") or None,
            trigger_type="manual",
            actor=user,
            variables=variables if isinstance(variables, dict) else None,
            ref_type=str(arguments.get("refType") or "") or None,
        )
    except Exception as exc:
        raise ToolError(f"Could not queue a build of '{row.slug}': {exc}")
    return {
        "changed": f"queued build #{data.get('number')} of {row.slug}",
        "queued": True,
        "build": data,
    }


@tool(
    "kubesight_build_cancel",
    permission="ci_builds:cancel",
    description=(
        "Cancel a queued or running build. A queued build stops immediately; a "
        "running one is flagged and its runner is told on the next tick, so it "
        "will still read as running for a moment after this returns."
    ),
    write=True,
    schema={
        "type": "object",
        "properties": {"buildId": {"type": "integer"}},
        "required": ["buildId"],
    },
)
def _build_cancel(arguments: Dict[str, Any], *, user=None) -> Dict[str, Any]:
    from ...services.ci import engine as engine_service

    row = _build_or_error(arguments.get("buildId"))
    try:
        data = engine_service.cancel_build(row, actor=user)
    except Exception as exc:
        raise ToolError(str(exc))
    return {"changed": f"cancelled build #{row.number}", "build": data}


@tool(
    "kubesight_build_retry",
    permission="ci_builds:retry",
    description=(
        "Queue a new build with the same coordinates as a finished one — the "
        "same branch, commit and trigger variables. This re-runs the ORIGINAL "
        "commit, not the branch's current head; to build the latest, use "
        "kubesight_build_run."
    ),
    write=True,
    schema={
        "type": "object",
        "properties": {"buildId": {"type": "integer"}},
        "required": ["buildId"],
    },
)
def _build_retry(arguments: Dict[str, Any], *, user=None) -> Dict[str, Any]:
    from ...services.ci import engine as engine_service

    row = _build_or_error(arguments.get("buildId"))
    try:
        data = engine_service.retry_build(row, actor=user)
    except Exception as exc:
        raise ToolError(str(exc))
    return {
        "changed": f"queued build #{data.get('number')} retrying #{row.number}",
        "build": data,
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
    from ...services.ci.serializers import runner_to_dict

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
    from ...services.ci import build_environments

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
    from ...models_ci import CiArtifact
    from ...services.ci.serializers import artifact_to_dict

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
# Merge checks — the gate between a pull request and a merge
# ---------------------------------------------------------------------------
#
# Read tools plus one write. The write moves a NUMBER, not code: it changes what
# is allowed to be merged across every service that inherits it, which is why it
# is gated on its own permission and why the description tells the agent to say
# the current value before changing it.
#
# Deliberately absent: anything that delivers or re-delivers a verdict, and
# anything that turns a service's checks off. An agent relaxing a gate to get a
# merge through is the exact failure this feature exists to prevent, so
# switching merge checks off stays a human action in the UI.


@tool(
    "kubesight_merge_checks_status",
    permission="ci_merge_checks:view",
    description=(
        "Whether a service's merge gate is on, what it runs, the limits it "
        "enforces, and - asked of Bitbucket - whether a failed check would "
        "actually stop a merge. Start here for 'why did my PR get blocked' and "
        "'is the gate even working'."
    ),
    schema={
        "type": "object",
        "properties": {
            "service": {"type": "string", "description": "Service id or slug."},
            "checkEnforcement": {
                "type": "boolean",
                "description": (
                    "Also ask Bitbucket whether a branch restriction requires "
                    "passing builds. Costs a round trip; default true."
                ),
            },
        },
        "required": ["service"],
    },
)
def _merge_checks_status(arguments: Dict[str, Any]) -> Dict[str, Any]:
    from ...services.ci import merge_checks

    row = _service_or_error(arguments.get("service"))
    config = merge_checks.config_payload(row)
    gate = config.get("effectiveGate") or {}
    payload: Dict[str, Any] = {
        "service": row.slug,
        "enabled": config.get("enabled"),
        "checks": config.get("tools"),
        "events": config.get("events"),
        "targetBranches": config.get("targetBranches") or ["(every branch)"],
        "gate": {
            key: value for key, value in gate.items() if key not in ("sources", "mode")
        },
        "gateMode": gate.get("mode"),
        "statusKey": config.get("statusKey"),
        "webhookUrl": config.get("webhookUrl") or config.get("webhookPath"),
        "canReportVerdict": config.get("canReportVerdict"),
        "editedScripts": [
            item["tool"]
            for item in config.get("checkScripts") or []
            if item.get("customized")
        ],
    }
    if arguments.get("checkEnforcement") is not False:
        # The single most useful field: from inside KubeSight, a gate that only
        # reports looks identical to one that actually blocks.
        payload["enforcement"] = merge_checks.merge_enforcement(row)
    return payload


@tool(
    "kubesight_merge_checks_history",
    permission="ci_merge_checks:view",
    description=(
        "Recent pull requests this service's gate judged: the verdict, how many "
        "problems each check reported, and whether the verdict reached Bitbucket. "
        "Use for 'what blocked PR 142' and 'is anything failing to report'."
    ),
    schema={
        "type": "object",
        "properties": {
            "service": {"type": "string", "description": "Service id or slug."},
            "pullRequest": {
                "type": "string",
                "description": "Narrow to one pull request id.",
            },
            "blockedOnly": {"type": "boolean", "description": "Only blocked merges."},
            "limit": {"type": "integer", "minimum": 1, "maximum": MAX_ROWS},
        },
        "required": ["service"],
    },
)
def _merge_checks_history(arguments: Dict[str, Any]) -> Dict[str, Any]:
    from ...services.ci import merge_checks

    row = _service_or_error(arguments.get("service"))
    items = merge_checks.list_checks(row, limit=_limit(arguments))

    wanted = str(arguments.get("pullRequest") or "").strip()
    if wanted:
        items = [item for item in items if str(item.get("pullRequestId")) == wanted]
    if arguments.get("blockedOnly"):
        items = [item for item in items if item.get("verdict") == "blocked"]

    return {
        "service": row.slug,
        "count": len(items),
        "checks": [
            {
                "pullRequest": item["pullRequestId"],
                "title": item["title"],
                "author": item["author"],
                "into": item["destinationBranch"],
                "commit": item["shortSha"],
                "verdict": item["verdict"],
                "state": item["state"],
                "problems": item["totalProblems"],
                "limit": (item.get("gate") or {}).get("maxTotalProblems"),
                # Per check, so "which tool blocked it" is answerable without a
                # second call. A check that did not run says so rather than
                # reporting zero problems.
                "byCheck": {
                    name: {
                        "status": report.get("status"),
                        "problems": report.get("problems"),
                    }
                    for name, report in (item.get("metrics") or {}).items()
                    if isinstance(report, dict)
                },
                "reasons": item["reasons"],
                "reportedToBitbucket": item["deliveryState"],
                "deliveryError": item["deliveryError"],
                "buildId": item["buildId"],
                "url": item["pullRequestUrl"],
                "at": item["createdAt"],
            }
            for item in items
        ],
    }


@tool(
    "kubesight_merge_check_policy_get",
    permission="ci_merge_checks:view",
    description=(
        "The installation-wide quality gate every service inherits unless it "
        "overrides it: the total problem limit, the per-check limits, and the "
        "severity floors."
    ),
)
def _merge_check_policy_get(_arguments: Dict[str, Any]) -> Dict[str, Any]:
    from ...services.ci.merge_checks import policy as policy_service

    row = policy_service.get_policy()
    resolved = policy_service.resolve_gate(None, row)
    return {
        "configured": policy_service.gate_payload(row),
        "effective": {
            key: value
            for key, value in resolved.items()
            if key not in ("sources", "mode")
        },
        "enabledByDefault": bool(row.enabled_by_default),
        "note": (
            "A null limit is no limit. A service whose gate mode is 'override' "
            "ignores these entirely rather than merging field by field."
        ),
    }


@tool(
    "kubesight_merge_check_policy_set",
    permission="ci_merge_checks:manage",
    description=(
        "Change the installation-wide quality gate. This changes what may be "
        "merged across EVERY service that inherits it, so read the current value "
        "with kubesight_merge_check_policy_get and say what it is and what it "
        "would become before calling this. Send null for a field to mean 'no "
        "limit'. It cannot turn a service's checks off."
    ),
    write=True,
    schema={
        "type": "object",
        "properties": {
            "maxTotalProblems": {
                "type": ["integer", "null"],
                "minimum": 0,
                "description": (
                    "Findings across all checks added together. This many passes; "
                    "one more blocks the merge."
                ),
            },
            "maxEslintProblems": {"type": ["integer", "null"], "minimum": 0},
            "maxSemgrepProblems": {"type": ["integer", "null"], "minimum": 0},
            "maxSonarProblems": {"type": ["integer", "null"], "minimum": 0},
            "maxDependencyProblems": {"type": ["integer", "null"], "minimum": 0},
            "eslintCountWarnings": {"type": "boolean"},
            "semgrepMinSeverity": {"type": "string"},
            "sonarMinSeverity": {"type": "string"},
            "dependencyMinSeverity": {"type": "string"},
            "blockOnToolError": {
                "type": "boolean",
                "description": "Whether a check that could not run blocks the merge.",
            },
        },
    },
)
def _merge_check_policy_set(arguments: Dict[str, Any], *, user=None) -> Dict[str, Any]:
    from ...audit import log_audit
    from ...services.ci.merge_checks import policy as policy_service

    row = policy_service.get_policy()
    before = policy_service.gate_payload(row)
    try:
        policy_service.apply_gate_fields(row, arguments)
    except policy_service.PolicyError as exc:
        raise ToolError(str(exc)) from exc
    row.updated_by_user_id = getattr(user, "id", None)
    db.session.add(row)
    db.session.commit()

    after = policy_service.gate_payload(row)
    changed = {
        key: {"from": before[key], "to": after[key]}
        for key in after
        if before.get(key) != after.get(key)
    }
    log_audit(
        "ci_merge_check_policy_saved",
        actor=user,
        target_type="ci_merge_check_policy",
        target_id="1",
        details={"changed": changed, "via": "mcp"},
    )
    return {
        "changed": changed or "nothing",
        "gate": after,
        "appliesTo": "every service whose gate mode is 'inherit'",
    }
