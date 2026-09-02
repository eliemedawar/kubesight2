"""camelCase serialization for the CI API.

One rule, enforced here rather than trusted at each call site: a secret *value*
never appears in a serialized payload. ``ci_secrets`` rows serialize to their
key and metadata; credential profiles serialize to their name and type.
"""

from __future__ import annotations

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


def _iso(value: Optional[datetime]) -> Optional[str]:
    return value.isoformat() if value else None


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
    pipeline = row.default_pipeline()
    data: Dict[str, Any] = {
        "id": row.id,
        "name": row.name,
        "slug": row.slug,
        "description": row.description,
        "ownerTeam": row.owner_team,
        "criticality": row.criticality,
        "applicationType": row.application_type,
        "status": row.status,
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
        "sourceConfigured": row.source_ready(),
        "pipelineConfigured": bool(pipeline and pipeline.stages),
        "pipelineId": pipeline.id if pipeline else None,
        "pipelineStageCount": len(pipeline.stages) if pipeline else 0,
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
    return data


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
        "timeoutSeconds": row.timeout_seconds,
        "continueOnFailure": bool(row.continue_on_failure),
        "parallelGroup": row.parallel_group,
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
        "version": row.version,
        "stageCount": len(row.stages),
        "createdAt": _iso(row.created_at),
        "updatedAt": _iso(row.updated_at),
    }
    if with_stages:
        data["stages"] = [pipeline_stage_to_dict(stage) for stage in row.stages]
    return data


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
    }


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
        running = next((s for s in stages if s.status == "running"), None)
        if running is not None:
            current_stage = running.name
            stage_progress = f"{running.position + 1}/{len(stages)}"
    return {
        "id": row.id,
        "serviceId": row.service_id,
        "number": row.number,
        "status": row.status,
        "triggerType": row.trigger_type,
        "branch": row.branch,
        "refType": (row.pipeline_snapshot or {}).get("refType") or "branch",
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
    }


def build_to_dict(row: CiBuild, *, with_stages: bool = True) -> Dict[str, Any]:
    data = build_summary(row)
    data.update(
        {
            "serviceName": row.service.name if row.service else None,
            "serviceSlug": row.service.slug if row.service else None,
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
        data["stages"] = [build_stage_to_dict(stage) for stage in row.stages]
    return data


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
