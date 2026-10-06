"""camelCase serialization for the CI API.

One rule, enforced here rather than trusted at each call site: a secret *value*
never appears in a serialized payload. ``ci_secrets`` rows serialize to their
key and metadata; credential profiles serialize to their name and type.
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any, Dict, List, Optional

from ...models_ci import (
    CiArtifact,
    CiBuild,
    CiBuildStage,
    CiPipeline,
    CiPipelineStage,
    CiRunner,
    CiSecret,
    CiService,
)
from . import resources as ci_resources
from . import test_reports


def _iso(value: Optional[datetime]) -> Optional[str]:
    return value.isoformat() if value else None


def _json_list(value: Any) -> List[Any]:
    """A JSON list column, whatever the database handed back.

    A db.JSON attribute normally arrives as a list. It arrives as a *string* when
    the underlying column is text — which happens on PostgreSQL if a migration
    added the column with the wrong type, since psycopg2 only parses values from
    a real json column. ``list()`` over that string would silently produce one
    entry per character, so decode it instead of trusting the type.
    """
    if isinstance(value, str):
        try:
            value = json.loads(value or "[]")
        except ValueError:
            return []
    if isinstance(value, (list, tuple)):
        return list(value)
    return []


# ---------------------------------------------------------------------------
# Services
# ---------------------------------------------------------------------------

def service_to_dict(
    row: CiService,
    *,
    latest_build: Optional[CiBuild] = None,
    latest_artifact: Optional[CiArtifact] = None,
    recent_statuses: Optional[List[str]] = None,
    include_counts: bool = False,
) -> Dict[str, Any]:
    from . import default_pipelines

    pipeline = row.default_pipeline()
    linked = bool(pipeline is not None and pipeline.linked_pipeline_id)
    # What a build actually runs: a shared pipeline's stages when the service
    # uses one (shared_pipelines.py), else its own.
    runs = pipeline.effective() if pipeline is not None else None
    saved_stage_count = len(runs.stages) if runs is not None else 0
    is_home = row.is_pipeline_home
    uses_generated_default = (
        not linked
        and not is_home
        and saved_stage_count == 0
        and default_pipelines.is_available(row.application_type)
    )
    effective_stage_count = (
        default_pipelines.stage_count(row.application_type)
        if uses_generated_default
        else saved_stage_count
    )
    data: Dict[str, Any] = {
        "id": row.id,
        "name": row.name,
        "slug": row.slug,
        "description": row.description,
        "ownerTeam": row.owner_team,
        "criticality": row.criticality,
        "applicationType": row.application_type,
        "status": row.status,
        # 'service' (CI Services catalog) or 'pipeline' (the Pipelines page).
        "kind": "pipeline" if is_home else "service",
        "repositoryProvider": row.repository_provider,
        "repositoryUrl": row.repository_url,
        "repositoryWorkspace": row.repository_workspace,
        "repositoryName": row.repository_name,
        "defaultBranch": row.default_branch,
        "workingDirectory": row.working_directory,
        "credentialProfileId": row.credential_profile_id,
        "credentialProfileName": (
            row.credential_profile.name if row.credential_profile else None
        ),
        "registryConnectionId": row.registry_connection_id,
        "blueprintId": row.blueprint_id,
        "intelligenceApplicationId": row.intelligence_application_id,
        "catalogEntryId": row.catalog_entry_id,
        "maxConcurrentBuilds": row.max_concurrent_builds,
        # What this service's build stages may use, and what the installation
        # would give them if it said nothing. The defaults travel with the row so
        # the Settings card can name what "Default" currently resolves to instead
        # of printing a number this file hardcoded and an operator has since
        # changed.
        "buildResources": row.build_resources or {},
        "buildResourceDefaults": ci_resources.installation_defaults(),
        # Assisted configuration. Null on every service registered before it
        # existed, which the UI renders as "not analyzed" — the same thing it
        # showed before there was anything to say.
        "applicationProfile": row.application_profile or None,
        "profileSource": row.profile_source,
        "analysisState": row.analysis_state or "not_analyzed",
        "sourceConfigured": row.source_ready(),
        "pipelineConfigured": bool(saved_stage_count or uses_generated_default),
        "usingDefaultPipeline": uses_generated_default,
        "pipelineId": pipeline.id if pipeline else None,
        "pipelineStageCount": effective_stage_count,
        "sharedPipeline": _shared_pipeline_of(pipeline) if linked else None,
        "deploymentLinkCount": len(row.deployment_links) if not is_home else 0,
        # Pipelines page: how many services build with this one.
        "usedByCount": (
            CiPipeline.query.filter_by(linked_pipeline_id=pipeline.id).count()
            if is_home and pipeline is not None
            else 0
        ),
        "createdAt": _iso(row.created_at),
        "updatedAt": _iso(row.updated_at),
    }
    data["latestBuild"] = build_summary(latest_build) if latest_build else None
    data["latestArtifact"] = artifact_to_dict(latest_artifact) if latest_artifact else None
    # Newest-first statuses of the last builds — the card sparkline.
    data["recentBuildStatuses"] = list(recent_statuses or [])
    if include_counts:
        data["buildCount"] = row.builds.count()
        data["artifactCount"] = row.artifacts.count()
        # Detail view only: a Dockerfile is a document, and shipping one per
        # card would bloat every catalog listing.
        data["dockerfile"] = row.dockerfile or ""
    data["hasInlineDockerfile"] = bool(row.dockerfile)
    return data


def _shared_pipeline_of(pipeline) -> Optional[Dict[str, Any]]:
    from .shared_pipelines import shared_summary

    return shared_summary(pipeline)


# ---------------------------------------------------------------------------
# Pipelines
# ---------------------------------------------------------------------------

def pipeline_stage_to_dict(row: CiPipelineStage) -> Dict[str, Any]:
    return {
        "id": row.id,
        "position": row.position,
        "name": row.name,
        "stageType": row.stage_type,
        "runnerType": row.runner_type,
        "runnerLabels": list(row.runner_labels or []),
        "image": row.image,
        "workingDirectory": row.working_directory,
        "commands": list(row.commands or []),
        "env": dict(row.env or {}),
        "secretRefs": list(row.secret_refs or []),
        "artifacts": list(row.artifacts or []),
        "resources": row.resources or {},
        # NULL on stages saved before host aliases existed — read as none set.
        "hostAliases": _json_list(row.host_aliases),
        # NULL means "always runs"; the editor renders that as no condition.
        "runCondition": row.run_condition if isinstance(row.run_condition, dict) else None,
        # NULL means nobody has configured a scan for this image, which the
        # editor shows as "not configured" rather than as "off" — they are
        # different answers to "was this image scanned?".
        "imageScan": row.image_scan if isinstance(row.image_scan, dict) else None,
        # Deploy stages only. Generated pipelines are SimpleNamespace stand-ins
        # without the attribute, so it is read defensively.
        "deploy": (
            getattr(row, "deploy", None)
            if isinstance(getattr(row, "deploy", None), dict)
            else None
        ),
        # Command stages only; NULL is "no quality gate". Read defensively for
        # the same SimpleNamespace reason as `deploy`.
        "codeScan": (
            getattr(row, "code_scan", None)
            if isinstance(getattr(row, "code_scan", None), dict)
            else None
        ),
        # Scan stages only: the scanner and its options. NULL on a scan stage
        # saved before the kind had an executor, which the engine skips.
        "scan": (
            getattr(row, "scan", None)
            if isinstance(getattr(row, "scan", None), dict)
            else None
        ),
        # Approval stages only: who may approve, how many, the message.
        "approval": (
            getattr(row, "approval", None)
            if isinstance(getattr(row, "approval", None), dict)
            else None
        ),
        # App store upload stages only: app, store, target, which file, and
        # the administrator it publishes as.
        "storeUpload": (
            getattr(row, "store_upload", None)
            if isinstance(getattr(row, "store_upload", None), dict)
            else None
        ),
        "timeoutSeconds": row.timeout_seconds,
        "continueOnFailure": bool(row.continue_on_failure),
        # Consecutive stages sharing a name run at the same time — see
        # services/ci/parallel_groups.py. None on a stage that runs on its own.
        # Read defensively: generated pipelines are SimpleNamespace stand-ins.
        "parallelGroup": getattr(row, "parallel_group", None) or None,
        "parallelFailFast": bool(getattr(row, "parallel_fail_fast", False)),
        "enabled": bool(row.enabled),
    }


def pipeline_to_dict(row: CiPipeline, *, with_stages: bool = True) -> Dict[str, Any]:
    data = {
        "id": row.id,
        "serviceId": row.service_id,
        "name": row.name,
        "description": row.description,
        "isDefault": bool(row.is_default),
        "enabled": bool(row.enabled),
        # 'build' | 'merge_check'. Generated pipelines are SimpleNamespace
        # stand-ins with no column, so this is read defensively.
        "purpose": getattr(row, "purpose", None) or "build",
        "version": row.version,
        "parameters": _json_list(row.parameters),
        # What happens when a build ends — see services/ci/post_actions.py.
        # A generated default shows the ones saved on the row it stands in for.
        "postActions": _post_actions_of(row),
        "linkedPipelineId": getattr(row, "linked_pipeline_id", None),
        "stageCount": len(row.stages),
        "createdAt": _iso(row.created_at),
        "updatedAt": _iso(row.updated_at),
    }
    if with_stages:
        data["stages"] = [pipeline_stage_to_dict(stage) for stage in row.stages]
    return data


def _post_actions_of(row: Any) -> List[Dict[str, Any]]:
    from . import post_actions

    return post_actions.of_pipeline(row)


def stage_definition(row: CiPipelineStage) -> Dict[str, Any]:
    """The snapshot form stored on a build. Same shape as the API stage, minus
    the database id, so a build renders identically after its stage is deleted."""
    data = pipeline_stage_to_dict(row)
    data["pipelineStageId"] = data.pop("id")
    return data


# ---------------------------------------------------------------------------
# Builds
# ---------------------------------------------------------------------------

def build_stage_to_dict(row: CiBuildStage) -> Dict[str, Any]:
    return {
        "id": row.id,
        "buildId": row.build_id,
        "pipelineStageId": row.pipeline_stage_id,
        "position": row.position,
        "name": row.name,
        "stageType": row.stage_type,
        "status": row.status,
        "attempt": row.attempt,
        "runnerId": row.runner_id,
        "runnerName": row.runner.name if row.runner else None,
        "exitCode": row.exit_code,
        "startedAt": _iso(row.started_at),
        "finishedAt": _iso(row.finished_at),
        "durationSeconds": row.duration_seconds,
        "logLineCount": row.log_line_count,
        "logTruncated": bool(row.log_truncated),
        "error": row.error,
        # What a deploy stage did: target, image, the image it replaced, the
        # change bundle it waited on, and how it ended. None on other stages.
        "deploy": row.deploy_state if isinstance(row.deploy_state, dict) else None,
        # An Approval stage's wait: who may approve, the decisions so far, how
        # it ended. An App store upload stage's publish and its steps.
        "approval": _server_state(row, "approval"),
        "storeUpload": _server_state(row, "store_upload"),
    }


def _server_state(row: CiBuildStage, stage_type: str) -> Optional[Dict[str, Any]]:
    if row.stage_type != stage_type:
        return None
    value = getattr(row, "server_state", None)
    return value if isinstance(value, dict) else None


def _awaiting_approval(row: CiBuild) -> Optional[Dict[str, Any]]:
    """The approval a running build is held at, so lists can say so."""
    if row.status != "running":
        return None
    for stage in row.stages:
        if stage.stage_type != "approval" or stage.status != "running":
            continue
        state = stage.server_state if isinstance(stage.server_state, dict) else {}
        if state.get("phase") != "waiting_approval":
            continue
        approvals = len({
            d.get("userId") for d in state.get("decisions") or [] if d.get("decision") == "approve"
        })
        return {
            "stageId": stage.id,
            "stageName": stage.name,
            "approvals": approvals,
            "required": int(state.get("required") or 1),
            "deadlineAt": state.get("deadlineAt"),
        }
    return None


def build_summary(row: CiBuild) -> Dict[str, Any]:
    """The compact form used on cards and in lists.

    Carries the one-line verdict the concept cards render: which stage failed,
    or which stage is running right now — so a card never needs the full build.
    """
    failed_stage = None
    current_stage = None
    stage_progress = None
    if row.status in ("failed", "timeout"):
        failed = next(
            (s for s in row.stages if s.status in ("failed", "timeout")), None
        )
        failed_stage = failed.name if failed else None
    elif row.status == "running":
        stages = sorted(row.stages, key=lambda s: s.position)
        running = [s for s in stages if s.status == "running"]
        if running:
            # A parallel group runs several at once; the card names them all
            # rather than picking one and implying the others are not running.
            current_stage = (
                running[0].name
                if len(running) == 1
                else f"{running[0].name} + {len(running) - 1} in parallel"
            )
            stage_progress = f"{running[0].position + 1}/{len(stages)}"
    return {
        "id": row.id,
        "serviceId": row.service_id,
        "number": row.number,
        "status": row.status,
        "triggerType": row.trigger_type,
        "branch": row.branch,
        "refType": (row.pipeline_snapshot or {}).get("refType") or "branch",
        # {"id", "name"} of the schedule that queued this build, from the
        # snapshot so it survives the schedule being renamed or deleted.
        "schedule": (
            (row.pipeline_snapshot or {}).get("schedule")
            if isinstance((row.pipeline_snapshot or {}).get("schedule"), dict)
            else None
        ),
        # {"id", "name", "kind", "event"} of the webhook trigger that queued
        # this build — snapshot, for the same reason as ``schedule``.
        "webhook": (
            (row.pipeline_snapshot or {}).get("webhook")
            if isinstance((row.pipeline_snapshot or {}).get("webhook"), dict)
            else None
        ),
        # {id, slug, name, pipelineId, version} of the shared pipeline (the
        # Pipelines page) this build ran with, or None for the service's own.
        "sharedPipeline": (
            (row.pipeline_snapshot or {}).get("sharedPipeline")
            if isinstance((row.pipeline_snapshot or {}).get("sharedPipeline"), dict)
            else None
        ),
        "commitSha": row.commit_sha,
        "durationSeconds": row.duration_seconds,
        "queuedAt": _iso(row.queued_at),
        "startedAt": _iso(row.started_at),
        "finishedAt": _iso(row.finished_at),
        "requestedBy": row.requested_by.username if row.requested_by else None,
        "queueReason": row.queue_reason,
        "failedStage": failed_stage,
        "currentStage": current_stage,
        "stageProgress": stage_progress,
        # {stageId, stageName, approvals, required, deadlineAt} while the build
        # waits at an Approval stage; None otherwise.
        "awaitingApproval": _awaiting_approval(row),
        # Test counts and coverage from the reports the build kept, or None.
        # The compact form only; the failed cases are behind /builds/<id>/tests.
        "testSummary": test_reports.compact(getattr(row, "test_summary", None)),
    }


def build_to_dict(row: CiBuild, *, with_stages: bool = True) -> Dict[str, Any]:
    data = build_summary(row)
    data.update(
        {
            "serviceName": row.service.name if row.service else None,
            "serviceSlug": row.service.slug if row.service else None,
            "serviceKind": (
                "pipeline" if row.service is not None and row.service.is_pipeline_home else "service"
            ),
            "pipelineId": row.pipeline_id,
            "commitMessage": row.commit_message,
            "retryOfBuildId": row.retry_of_build_id,
            "runnerId": row.runner_id,
            "runnerName": row.runner.name if row.runner else None,
            "workspaceRef": row.workspace_ref,
            "cancelRequested": bool(row.cancel_requested),
            "error": row.error,
            "createdAt": _iso(row.created_at),
        }
    )
    if with_stages:
        from . import parallel_groups

        snapshot = (row.pipeline_snapshot or {}).get("stages") or []
        data["stages"] = []
        for stage in row.stages:
            item = build_stage_to_dict(stage)
            item["codeScan"] = _stage_code_scan(snapshot, stage.position)
            item["scan"] = _stage_scan(snapshot, stage.position)
            # The group this stage ran in, as the BUILD saw it: a group of
            # two or more consecutive members in the snapshot, else None.
            item["parallelGroup"] = parallel_groups.group_name(snapshot, stage.position)
            item["parallelFailFast"] = bool(
                item["parallelGroup"]
                and parallel_groups.fail_fast(
                    snapshot, parallel_groups.group_positions(snapshot, stage.position)
                )
            )
            data["stages"].append(item)
        # {"mode": "parallel"|"sequential", "reason"} once a build with groups
        # has started — "sequential" says the runner could not run them side by
        # side, and why. None for a build with no groups.
        decided = (row.pipeline_snapshot or {}).get("parallel")
        data["parallel"] = dict(decided) if isinstance(decided, dict) else None
        # What ran when the build ended: notifications (sent / not sent / why)
        # and cleanup rows with their own logs. Not stages — see post_actions.py.
        from . import post_actions

        data["postActions"] = post_actions.serialize(row)
    return data


def _stage_code_scan(snapshot: List[Any], position: int) -> Optional[Dict[str, Any]]:
    """The quality gate this build's stage ran under, from the build's snapshot.

    The threshold only: the build drawer uses it to offer the report, and the
    saved recipients are read from the live pipeline when the dialog opens.
    """
    if not 0 <= position < len(snapshot):
        return None
    gate = (snapshot[position] or {}).get("codeScan")
    if not isinstance(gate, dict) or gate.get("enabled") is False:
        return None
    return {
        "tool": gate.get("tool") or "semgrep",
        "maxBlocking": int(gate.get("maxBlocking") or 0),
        "countFrom": gate.get("countFrom") or "info",
    }


def _stage_scan(snapshot: List[Any], position: int) -> Optional[Dict[str, Any]]:
    """What a scan stage ran, from the build's snapshot: tool, one-line policy,
    and the artifacts it leaves - so the drawer can point at the report."""
    from . import scan_stage

    if not 0 <= position < len(snapshot):
        return None
    definition = snapshot[position] or {}
    config = definition.get("scan")
    if definition.get("stageType") != "scan" or not scan_stage.configured(config):
        return None
    return {
        "tool": config["tool"],
        "label": scan_stage.TOOL_LABELS[config["tool"]],
        "summary": scan_stage.summary(config),
        "produces": scan_stage.produces(config, position),
    }


# ---------------------------------------------------------------------------
# Artifacts, runners, secrets
# ---------------------------------------------------------------------------

def artifact_to_dict(row: CiArtifact) -> Dict[str, Any]:
    return {
        "id": row.id,
        "serviceId": row.service_id,
        "buildId": row.build_id,
        "buildStageId": row.build_stage_id,
        "artifactType": row.artifact_type,
        "name": row.name,
        "version": row.version,
        "uri": row.uri,
        "digest": row.digest,
        "checksumSha256": row.checksum_sha256,
        "sizeBytes": row.size_bytes,
        "storageBackend": row.storage_backend,
        "registryConnectionId": row.registry_connection_id,
        "commitSha": row.commit_sha,
        "branch": row.branch,
        "metadata": dict(row.artifact_metadata or {}),
        "downloadable": bool(row.storage_backend == "local" and row.storage_ref),
        # A container image can be handed straight to the existing deploy flow.
        "deployable": bool(row.artifact_type == "container-image" and row.uri),
        "createdAt": _iso(row.created_at),
    }


def runner_to_dict(row: CiRunner) -> Dict[str, Any]:
    return {
        "id": row.id,
        "name": row.name,
        "description": row.description,
        "runnerType": row.runner_type,
        "status": row.status,
        "enabled": bool(row.enabled),
        "hostname": row.hostname,
        "os": row.os,
        "osVersion": row.os_version,
        "arch": row.arch,
        "labels": list(row.labels or []),
        "capabilities": list(row.capabilities or []),
        "maxConcurrent": row.max_concurrent,
        "currentLoad": row.current_load,
        "version": row.version,
        "isBuiltin": bool(row.is_builtin),
        # Where an agent puts its builds. Empty means the agent's own default.
        "workspaceRoot": (row.runner_metadata or {}).get("workspaceRoot") or "",
        "lastHeartbeatAt": _iso(row.last_heartbeat_at),
        "lastAssignedAt": _iso(row.last_assigned_at),
        "lastError": row.last_error,
        "createdAt": _iso(row.created_at),
    }


def secret_to_dict(row: CiSecret) -> Dict[str, Any]:
    """Key and metadata only — ``value_cipher`` is never exposed by any route."""
    return {
        "id": row.id,
        "scope": row.scope,
        "serviceId": row.service_id,
        "key": row.key,
        "description": row.description,
        "lastUsedAt": _iso(row.last_used_at),
        "createdBy": row.created_by.username if row.created_by else None,
        "createdAt": _iso(row.created_at),
        "updatedAt": _iso(row.updated_at),
    }


def credential_profile_to_dict(row) -> Dict[str, Any]:
    """Source credential profile without its secret."""
    return {
        "id": row.id,
        "name": row.name,
        "provider": getattr(row, "provider", "bitbucket") or "bitbucket",
        "credentialType": row.credential_type,
        "principal": row.principal,
        "readOnly": bool(row.read_only),
        "enabled": bool(row.enabled),
        "createdAt": _iso(row.created_at),
    }


def serialize_all(rows: List[Any], serializer) -> List[Dict[str, Any]]:
    return [serializer(row) for row in rows]
