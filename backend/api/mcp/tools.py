"""What an agent may ask KubeSight, and what it gets back.

Every tool here reads. None of them writes, and that is enforced structurally
rather than by convention: this module imports the serializers and the query
helpers, and nothing that mutates.

Two things shape the answers.

**Each tool declares the permission it needs**, and that permission is checked
against the calling token's user through the same access engine every HTTP route
uses. An agent holding a viewer's token gets a viewer's answers — there is no
path here that sees more than the person whose token it is.

**Answers come back twice**: a short human-readable summary and the full
structured payload. The summary is what a model reads when it is deciding what
to ask next; the structure is what it reads when it is answering precisely. A
tool that returned only prose would force the model to parse its own English,
and one that returned only JSON would make cheap orientation expensive.
"""

from __future__ import annotations

from typing import Any, Callable, Dict, List, Optional

from ..access_engine import user_has_permission
from ..db import db
from ..models_ci import CiBuild, CiRunner, CiService
from .protocol import ToolError

# name -> {"permission", "description", "schema", "run"}
_REGISTRY: Dict[str, Dict[str, Any]] = {}

MAX_ROWS = 100
MAX_LOG_LINES = 400


def tool(
    name: str, *, permission: str, description: str, schema: Optional[Dict[str, Any]] = None
) -> Callable:
    """Register one read-only tool and the permission it answers under."""

    def decorate(func: Callable) -> Callable:
        _REGISTRY[name] = {
            "permission": permission,
            "description": description,
            "schema": schema or {"type": "object", "properties": {}},
            "run": func,
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
                # Names only — a pipeline stores references, never values.
                "secretRefs": [ref.get("name") for ref in stage.get("secretRefs") or []],
                "hostAliases": stage.get("hostAliases"),
                "timeoutSeconds": stage.get("timeoutSeconds"),
                "enabled": stage.get("enabled"),
            }
            for stage in pipeline.get("stages") or []
        ],
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
            # Read-only and non-destructive, declared rather than implied — a
            # client that surfaces write tools differently can then do so.
            "annotations": {"readOnlyHint": True, "destructiveHint": False},
        }
        for name, entry in sorted(_REGISTRY.items())
    ]


def _summarise(name: str, payload: Any) -> str:
    """One line for a model deciding what to ask next."""
    if not isinstance(payload, dict):
        return f"{name}: done."
    for key in ("services", "builds", "runners", "artifacts", "stages", "environments"):
        if isinstance(payload.get(key), list):
            return f"{name}: {len(payload[key])} {key}."
    if "service" in payload and isinstance(payload["service"], dict):
        return f"{name}: {payload['service'].get('slug', 'service')}."
    return f"{name}: ok."


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

    payload = entry["run"](arguments or {})
    return {
        "content": [
            {"type": "text", "text": _summarise(name, payload)},
            {"type": "text", "text": json.dumps(payload, ensure_ascii=False, default=str)},
        ],
        "structuredContent": payload if isinstance(payload, dict) else {"result": payload},
    }
