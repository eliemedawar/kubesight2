"""Application Intelligence domain service and safe DTOs."""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import secrets
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from flask import current_app
from sqlalchemy import func

from ..audit import log_audit
from ..db import db
from ..models import (
    ApplicationAnalysis,
    ApplicationArtifact,
    ApplicationCommunication,
    ApplicationDependency,
    ApplicationFinding,
    ApplicationFindingStatusEvent,
    ApplicationPullRequest,
    BitbucketCredentialProfile,
    IntelligenceApplication,
    User,
)
from ..secret_encryption import decrypt_secret, encrypt_secret
from . import application_analysis_jobs
from . import application_analysis_local_docker
from . import application_pull_request_local_docker
from . import application_intelligence_bitbucket as bitbucket_metadata
from .application_intelligence_hermes import analyze as analyze_with_hermes
from .application_intelligence_schema import validate_hermes_output
from .application_intelligence_security import (
    redact_structure,
    safe_error,
    validate_relative_path,
    validate_repository_url,
)

APPLICATION_STATUSES = {
    "Queued",
    "Cloning",
    "Discovering",
    "Scanning",
    "Analyzing",
    "Building",
    "Generating Report",
    "Completed",
    "Completed With Warnings",
    "Failed",
    "Cancelled",
}
TERMINAL_STATUSES = {"Completed", "Completed With Warnings", "Failed", "Cancelled"}
PHASE_ONE_MODES = {"Quick", "Deep"}
ALL_MODES = PHASE_ONE_MODES | {"Build Verified"}
CREDENTIAL_TYPES = {
    "api_token",
    "oauth",
    "repository_access_token",
    "project_access_token",
    "workspace_access_token",
}
FINDING_STATUSES = {"Open", "Accepted", "Resolved", "False Positive", "Risk Accepted"}
EXECUTOR = "hermes-agent"


def test_hermes_connection(user: User | None) -> dict:
    """Exercise the configured gateway, model provider, and response contract."""
    started = time.monotonic()
    _result, model, prompt_version = analyze_with_hermes(
        {
            "connection_test": True,
            "repository": {
                "provider": "none",
                "name": "local-configuration-check",
            },
            "deterministic_evidence": {
                "files": [],
                "scanners": [],
            },
            "constraints": [
                "This is a connection test. No repository content is included.",
                "Return the required schema with empty collections and note the "
                "absence of repository evidence under limitations.",
            ],
        }
    )
    duration_ms = round((time.monotonic() - started) * 1000)
    log_audit(
        "application.hermes.connection_tested",
        actor=user,
        target_type="hermes",
        target_id="application-intelligence",
        details={
            "model": model,
            "prompt_version": prompt_version,
            "latency_ms": duration_ms,
            "executed_by": EXECUTOR,
        },
    )
    return {
        "connected": True,
        "model": model,
        "promptVersion": prompt_version,
        "latencyMs": duration_ms,
    }


def _now():
    return datetime.now(timezone.utc)


def _iso(value):
    return value.isoformat() if value else None


def _slug(value: str) -> str:
    result = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")
    return result[:180]


def _page(query, page: int, per_page: int):
    page = max(1, int(page or 1))
    per_page = min(100, max(1, int(per_page or 25)))
    total = query.order_by(None).count()
    return query.offset((page - 1) * per_page).limit(per_page).all(), total, page, per_page


def credential_to_dict(row: BitbucketCredentialProfile) -> dict:
    return {
        "id": row.id,
        "name": row.name,
        "credentialType": row.credential_type,
        "principal": row.principal,
        "readOnly": bool(row.read_only),
        "enabled": bool(row.enabled),
        "secretConfigured": bool(row.secret_cipher),
        "createdAt": _iso(row.created_at),
        "updatedAt": _iso(row.updated_at),
    }


def list_credentials() -> dict:
    rows = BitbucketCredentialProfile.query.order_by(
        BitbucketCredentialProfile.name.asc()
    ).all()
    return {"items": [credential_to_dict(row) for row in rows], "count": len(rows)}


def create_credential(payload: dict, user: User) -> dict:
    name = str(payload.get("name") or "").strip()
    credential_type = str(payload.get("credentialType") or "").strip().lower()
    secret_value = str(payload.get("token") or payload.get("secret") or "")
    if not name:
        raise ValueError("Credential profile name is required.")
    if credential_type not in CREDENTIAL_TYPES:
        raise ValueError("Unsupported Bitbucket credential type.")
    if not secret_value:
        raise ValueError("A Bitbucket token is required.")
    read_only = payload.get("readOnly", True) is not False
    if BitbucketCredentialProfile.query.filter(
        func.lower(BitbucketCredentialProfile.name) == name.lower()
    ).first():
        raise ValueError("A credential profile with this name already exists.")
    principal = str(payload.get("principal") or "").strip()
    if credential_type == "api_token" and (
        "@" not in principal or len(principal) > 255
    ):
        raise ValueError(
            "Atlassian API tokens require the Atlassian account email."
        )
    row = BitbucketCredentialProfile(
        name=name,
        credential_type=credential_type,
        principal=principal or None,
        secret_cipher=encrypt_secret(secret_value),
        read_only=read_only,
        enabled=True,
        created_by_user_id=user.id,
    )
    db.session.add(row)
    db.session.commit()
    log_audit(
        "application.credential_profile.created",
        actor=user,
        target_type="bitbucket_credential_profile",
        target_id=str(row.id),
        details={"credential_type": credential_type, "read_only": read_only},
    )
    return credential_to_dict(row)


def update_credential(
    credential_id: int, payload: dict, user: User
) -> dict:
    row = db.session.get(BitbucketCredentialProfile, credential_id)
    if row is None:
        raise LookupError("Credential profile not found.")
    name = str(payload.get("name", row.name) or "").strip()
    credential_type = str(
        payload.get("credentialType", row.credential_type) or ""
    ).strip().lower()
    principal = str(payload.get("principal", row.principal or "") or "").strip()
    if not name:
        raise ValueError("Credential profile name is required.")
    if credential_type not in CREDENTIAL_TYPES:
        raise ValueError("Unsupported Bitbucket credential type.")
    if credential_type == "api_token" and (
        "@" not in principal or len(principal) > 255
    ):
        raise ValueError(
            "Atlassian API tokens require the Atlassian account email."
        )
    duplicate = BitbucketCredentialProfile.query.filter(
        func.lower(BitbucketCredentialProfile.name) == name.lower(),
        BitbucketCredentialProfile.id != row.id,
    ).first()
    if duplicate:
        raise ValueError("A credential profile with this name already exists.")
    secret_value = str(payload.get("token") or payload.get("secret") or "")
    requested_read_only = (
        payload.get("readOnly") is not False
        if "readOnly" in payload
        else row.read_only
    )
    if not requested_read_only and IntelligenceApplication.query.filter_by(
        credential_profile_id=row.id
    ).count():
        raise ValueError(
            "This profile is used by an application and cannot be changed to "
            "pull-request access."
        )
    row.name = name
    row.credential_type = credential_type
    row.principal = principal or None
    row.read_only = requested_read_only
    if secret_value:
        row.secret_cipher = encrypt_secret(secret_value)
    db.session.commit()
    log_audit(
        "application.credential_profile.updated",
        actor=user,
        target_type="bitbucket_credential_profile",
        target_id=str(row.id),
        details={"credential_type": credential_type, "read_only": row.read_only},
    )
    return credential_to_dict(row)


def delete_credential(credential_id: int, user: User) -> dict:
    row = db.session.get(BitbucketCredentialProfile, credential_id)
    if row is None:
        raise LookupError("Credential profile not found.")
    application_count = IntelligenceApplication.query.filter_by(
        credential_profile_id=row.id
    ).count()
    pull_request_count = ApplicationPullRequest.query.filter_by(
        credential_profile_id=row.id
    ).count()
    if application_count or pull_request_count:
        suffix = "" if application_count == 1 else "s"
        pr_detail = (
            f" and {pull_request_count} pull-request record"
            f"{'' if pull_request_count == 1 else 's'}"
            if pull_request_count
            else ""
        )
        raise ValueError(
            f"This credential profile is used by {application_count} application"
            f"{suffix}{pr_detail}. Update or delete those records first."
        )
    deleted = {
        "id": row.id,
        "name": row.name,
        "credential_type": row.credential_type,
    }
    db.session.delete(row)
    db.session.commit()
    log_audit(
        "application.credential_profile.deleted",
        actor=user,
        target_type="bitbucket_credential_profile",
        target_id=str(deleted["id"]),
        details={
            "name": deleted["name"],
            "credential_type": deleted["credential_type"],
        },
    )
    return {"deleted": True, "id": deleted["id"]}


def _repository_metadata_context(payload: dict) -> tuple[str, str, str, str]:
    _normalized_url, repository_ref = validate_repository_url(
        payload.get("repositoryUrl", "")
    )
    try:
        credential_id = int(payload.get("credentialProfileId"))
    except (TypeError, ValueError):
        raise ValueError("Select an enabled, read-only Bitbucket credential profile.")
    credential = db.session.get(BitbucketCredentialProfile, credential_id)
    if (
        credential is None
        or not credential.enabled
        or not credential.read_only
        or not credential.secret_cipher
    ):
        raise ValueError("Select an enabled, read-only Bitbucket credential profile.")
    return (
        repository_ref,
        decrypt_secret(credential.secret_cipher),
        credential.credential_type,
        credential.principal or "",
    )


def list_repository_revisions(payload: dict) -> dict:
    repository_ref, token, credential_type, principal = (
        _repository_metadata_context(payload)
    )
    return bitbucket_metadata.list_revisions(
        repository_ref, token, credential_type, principal
    )


def list_repository_dockerfiles(payload: dict) -> dict:
    repository_ref, token, credential_type, principal = (
        _repository_metadata_context(payload)
    )
    revision = str(payload.get("revision") or "").strip()
    return bitbucket_metadata.list_dockerfiles(
        repository_ref, token, revision, credential_type, principal
    )


SEVERITY_ORDER = ("Critical", "High", "Medium", "Low", "Informational")
# Statuses in which a human has consciously taken a finding off the board.
CLOSED_FINDING_STATUSES = frozenset({"Resolved", "False Positive", "Risk Accepted"})


def _finding_posture(analysis_id: int) -> dict:
    """Severity counts over persisted findings.

    These counts are the only quantitative risk signal KubeSight publishes.
    They are reproducible from the findings table, unlike a model-supplied
    score, and their polarity is unambiguous.
    """
    grouped = (
        db.session.query(
            ApplicationFinding.severity,
            ApplicationFinding.status,
            func.count(ApplicationFinding.id),
        )
        .filter(ApplicationFinding.analysis_id == analysis_id)
        .group_by(ApplicationFinding.severity, ApplicationFinding.status)
        .all()
    )
    total = {severity: 0 for severity in SEVERITY_ORDER}
    open_counts = {severity: 0 for severity in SEVERITY_ORDER}
    for severity, status, count in grouped:
        if severity not in total:
            continue
        total[severity] += count
        if status not in CLOSED_FINDING_STATUSES:
            open_counts[severity] += count
    risk_level = "None"
    for severity in ("Critical", "High", "Medium", "Low"):
        if open_counts[severity]:
            risk_level = severity
            break
    return {
        "bySeverity": total,
        "openBySeverity": open_counts,
        "total": sum(total.values()),
        "openTotal": sum(open_counts.values()),
        "riskLevel": risk_level,
    }


def _evidence_coverage(row: ApplicationAnalysis) -> dict:
    """Which deterministic scanners actually produced evidence.

    A result is only as complete as the tools behind it. Without this, an empty
    dependency or CVE list reads as a clean repository instead of an absent
    scanner.
    """
    available: list[str] = []
    unavailable: list[str] = []
    for run in row.scanner_runs or []:
        if not isinstance(run, dict) or not run.get("name"):
            continue
        # A scanner that ran reports a process exit code; anything else is
        # "unavailable" or "failed" and contributed no evidence.
        bucket = available if isinstance(run.get("exitStatus"), int) else unavailable
        bucket.append(str(run["name"]))
    if not available:
        label = "Hermes only"
    elif unavailable:
        label = "Partial"
    else:
        label = "Full"
    return {"label": label, "available": available, "unavailable": unavailable}


def _source_coverage(payload: Any) -> dict | None:
    """Validate the worker's account of how much source the model was shown."""
    if not isinstance(payload, dict):
        return None
    coverage = {}
    for key in (
        "selectedFiles",
        "eligibleFiles",
        "repositoryFiles",
        "truncatedFiles",
        "fileLimit",
        "bytesSent",
    ):
        value = payload.get(key)
        coverage[key] = value if isinstance(value, int) and 0 <= value < 10_000_000 else None
    if coverage["selectedFiles"] is None or not coverage["eligibleFiles"]:
        return None
    coverage["reviewedPercent"] = round(
        100 * coverage["selectedFiles"] / coverage["eligibleFiles"]
    )
    return coverage


def _build_verification_status(row: ApplicationAnalysis) -> str | None:
    """Status of KubeSight's own build stage, not the model's account of it."""
    profile = (row.result_summary or {}).get("application_profile")
    report = profile.get("build_verification") if isinstance(profile, dict) else None
    return report.get("status") if isinstance(report, dict) else None


def application_to_dict(row: IntelligenceApplication, *, include_history=False) -> dict:
    latest = row.analyses.order_by(ApplicationAnalysis.created_at.desc()).first()
    critical = high = 0
    if latest:
        grouped = (
            db.session.query(ApplicationFinding.severity, func.count(ApplicationFinding.id))
            .filter(ApplicationFinding.analysis_id == latest.id)
            .group_by(ApplicationFinding.severity)
            .all()
        )
        counts = {severity: count for severity, count in grouped}
        critical, high = counts.get("Critical", 0), counts.get("High", 0)
    data = {
        "id": row.id,
        "name": row.name,
        "slug": row.slug,
        "description": row.description,
        "repositoryProvider": row.repository_provider,
        "repositoryUrl": row.repository_url,
        "repositoryWorkspace": row.repository_workspace,
        "repositoryName": row.repository_name,
        "defaultBranch": row.default_branch,
        "credentialProfileId": row.credential_profile_id,
        "credentialProfileName": (
            row.credential_profile.name if row.credential_profile else None
        ),
        "repositorySubdirectory": row.repository_subdirectory,
        "dockerfilePath": row.dockerfile_path,
        "mappedClusterId": row.mapped_cluster_id,
        "mappedNamespace": row.mapped_namespace,
        "mappedWorkloadKind": row.mapped_workload_kind,
        "mappedWorkloadName": row.mapped_workload_name,
        "createdBy": row.created_by.username if row.created_by else None,
        "createdAt": _iso(row.created_at),
        "updatedAt": _iso(row.updated_at),
        "latestAnalysis": analysis_to_dict(latest, compact=True) if latest else None,
        "criticalFindingCount": critical,
        "highFindingCount": high,
    }
    if include_history:
        data["analyses"] = [
            analysis_to_dict(item, compact=True)
            for item in row.analyses.order_by(ApplicationAnalysis.created_at.desc())
            .limit(50)
            .all()
        ]
    return data


def analysis_to_dict(row: ApplicationAnalysis | None, *, compact=False) -> dict | None:
    if row is None:
        return None
    data = {
        "id": row.id,
        "applicationId": row.application_id,
        "status": row.status,
        "progressPercent": row.progress_percent,
        "currentStage": row.current_stage,
        "analysisMode": row.analysis_mode,
        "requestedByUserId": row.requested_by_user_id,
        "requestedBy": row.requested_by.username if row.requested_by else None,
        "executedBy": row.executed_by_account,
        "branch": row.branch,
        "requestedRevision": row.requested_revision,
        "commitSha": row.commit_sha,
        "startedAt": _iso(row.started_at),
        "completedAt": _iso(row.completed_at),
        "failedAt": _iso(row.failed_at),
        "failureStage": row.failure_stage,
        "safeErrorMessage": row.safe_error_message,
        "posture": _finding_posture(row.id),
        "evidenceCoverage": _evidence_coverage(row),
        "sourceCoverage": row.source_coverage,
        "buildVerificationStatus": _build_verification_status(row),
        "topologyConfidence": row.topology_confidence,
        "scannerVersions": row.scanner_versions or {},
        "scannerRuns": row.scanner_runs or [],
        "hermesModel": row.hermes_model,
        "hermesPromptVersion": row.hermes_prompt_version,
        "workspaceCleanupStatus": row.workspace_cleanup_status,
        "warnings": row.warnings or [],
        "createdAt": _iso(row.created_at),
    }
    if not compact:
        data["result"] = redact_structure(row.result_summary or {})
        data["dependencies"] = [
            {
                "id": item.id,
                "type": item.dependency_type,
                "name": item.name,
                "version": item.version,
                "ecosystem": item.ecosystem,
                "direct": item.direct,
                "vulnerable": item.vulnerable,
                "license": item.license,
                "sourceFile": item.source_file,
            }
            for item in row.dependencies.order_by(ApplicationDependency.name.asc()).all()
        ]
        data["application"] = (
            {
                "id": row.application.id,
                "name": row.application.name,
                "repositoryUrl": row.application.repository_url,
            }
            if row.application
            else None
        )
    return data


def finding_to_dict(row: ApplicationFinding) -> dict:
    return {
        "id": row.id,
        "analysisId": row.analysis_id,
        "category": row.category,
        "severity": row.severity,
        "confidence": row.confidence,
        "title": row.title,
        "description": row.description,
        "impact": row.impact,
        "recommendation": row.recommendation,
        "evidence": row.evidence,
        "evidenceType": row.evidence_type,
        "filePath": row.file_path,
        "startLine": row.start_line,
        "endLine": row.end_line,
        "scannerSource": row.scanner_source,
        "cwe": row.cwe,
        "cve": row.cve,
        "status": row.status,
        "fingerprint": row.fingerprint,
        "hasSuggestedPatch": bool(row.suggested_patch),
        "createdAt": _iso(row.created_at),
        "updatedAt": _iso(row.updated_at),
        "statusHistory": [
            {
                "id": event.id,
                "previousStatus": event.previous_status,
                "status": event.status,
                "reason": event.reason,
                "changedBy": event.changed_by.username if event.changed_by else None,
                "createdAt": _iso(event.created_at),
            }
            for event in row.status_events.order_by(
                ApplicationFindingStatusEvent.created_at.asc()
            ).all()
        ],
    }


def get_finding(finding_id: int) -> ApplicationFinding:
    row = db.session.get(ApplicationFinding, finding_id)
    if row is None:
        raise LookupError("Finding not found.")
    return row


def update_finding_status(
    finding_id: int, payload: dict, user: User
) -> dict:
    row = get_finding(finding_id)
    status = str(payload.get("status") or "").strip()
    reason = str(payload.get("reason") or "").strip()
    if status not in FINDING_STATUSES:
        raise ValueError("Finding status is invalid.")
    if status in {"Risk Accepted", "False Positive"} and not reason:
        raise ValueError(f"A reason is required for {status}.")
    if len(reason) > 4000:
        raise ValueError("Finding status reason is too long.")
    previous = row.status
    if previous == status:
        return finding_to_dict(row)
    row.status = status
    db.session.add(
        ApplicationFindingStatusEvent(
            finding_id=row.id,
            previous_status=previous,
            status=status,
            reason=reason or None,
            changed_by_user_id=user.id,
        )
    )
    action = (
        "application.finding.risk_accepted"
        if status == "Risk Accepted"
        else "application.finding.status_changed"
    )
    log_audit(
        action,
        actor=user,
        target_type="application_finding",
        target_id=str(row.id),
        details={
            "analysis_id": row.analysis_id,
            "previous_status": previous,
            "status": status,
            "reason": reason or None,
            "executed_by": user.username,
        },
        commit=False,
    )
    db.session.commit()
    return finding_to_dict(row)


def list_applications(page=1, per_page=25) -> dict:
    query = IntelligenceApplication.query.order_by(IntelligenceApplication.updated_at.desc())
    rows, total, page, per_page = _page(query, page, per_page)
    return {
        "items": [application_to_dict(row) for row in rows],
        "pagination": {"page": page, "perPage": per_page, "total": total},
    }


def get_application(application_id: int) -> IntelligenceApplication:
    row = db.session.get(IntelligenceApplication, application_id)
    if row is None:
        raise LookupError("Application not found.")
    return row


def _apply_application_payload(
    row: IntelligenceApplication, payload: dict, *, creating: bool
) -> None:
    name = str(payload.get("name", row.name or "")).strip()
    if not name:
        raise ValueError("Microservice name is required.")
    repository_url, repository_ref = validate_repository_url(
        payload.get("repositoryUrl", row.repository_url or "")
    )
    workspace, repository = repository_ref.split("/", 1)
    credential_id = payload.get("credentialProfileId", row.credential_profile_id)
    credential = db.session.get(BitbucketCredentialProfile, credential_id)
    if not credential or not credential.enabled or not credential.read_only:
        raise ValueError("Select an enabled, read-only Bitbucket credential profile.")
    slug = _slug(str(payload.get("slug") or name))
    duplicate = IntelligenceApplication.query.filter_by(slug=slug).first()
    if duplicate and duplicate.id != row.id:
        raise ValueError("An application with this name already exists.")

    row.name = name
    row.slug = slug
    row.description = str(payload.get("description", row.description or "")).strip() or None
    row.repository_provider = "bitbucket"
    row.repository_url = repository_url
    row.repository_workspace = workspace
    row.repository_name = repository
    row.default_branch = (
        str(payload.get("defaultBranch", row.default_branch or "main")).strip() or "main"
    )
    row.credential_profile_id = credential.id
    row.repository_subdirectory = validate_relative_path(
        payload.get("repositorySubdirectory", row.repository_subdirectory),
        "Repository subdirectory",
    )
    row.dockerfile_path = validate_relative_path(
        payload.get("dockerfilePath", row.dockerfile_path), "Dockerfile path"
    )
    row.mapped_cluster_id = (
        str(payload.get("mappedClusterId", row.mapped_cluster_id or "")).strip() or None
    )
    row.mapped_namespace = (
        str(payload.get("mappedNamespace", row.mapped_namespace or "")).strip() or None
    )
    row.mapped_workload_kind = (
        str(payload.get("mappedWorkloadKind", row.mapped_workload_kind or "")).strip()
        or None
    )
    row.mapped_workload_name = (
        str(payload.get("mappedWorkloadName", row.mapped_workload_name or "")).strip()
        or None
    )


def create_application(payload: dict, user: User) -> dict:
    row = IntelligenceApplication(
        name="",
        slug="",
        repository_url="",
        repository_workspace="",
        repository_name="",
        credential_profile_id=0,
        created_by_user_id=user.id,
    )
    _apply_application_payload(row, payload, creating=True)
    db.session.add(row)
    db.session.commit()
    log_audit(
        "application.created",
        actor=user,
        target_type="intelligence_application",
        target_id=str(row.id),
        details={"repository": f"{row.repository_workspace}/{row.repository_name}"},
    )
    return application_to_dict(row)


def update_application(application_id: int, payload: dict, user: User) -> dict:
    row = get_application(application_id)
    _apply_application_payload(row, payload, creating=False)
    db.session.commit()
    log_audit(
        "application.updated",
        actor=user,
        target_type="intelligence_application",
        target_id=str(row.id),
        details={"repository": f"{row.repository_workspace}/{row.repository_name}"},
    )
    return application_to_dict(row)


def delete_application(application_id: int, user: User) -> None:
    row = get_application(application_id)
    if row.analyses.filter(~ApplicationAnalysis.status.in_(TERMINAL_STATUSES)).count():
        raise ValueError("Cancel active analyses before deleting this application.")
    for analysis in row.analyses.all():
        for artifact in analysis.artifacts.all():
            try:
                root = _artifact_root().resolve()
                path = (root / artifact.storage_reference).resolve()
                if root in path.parents and path.is_file():
                    path.unlink()
            except OSError:
                pass
    db.session.delete(row)
    db.session.commit()
    log_audit(
        "application.deleted",
        actor=user,
        target_type="intelligence_application",
        target_id=str(application_id),
    )


def _audit_execution(action: str, analysis: ApplicationAnalysis, details=None) -> None:
    hermes = User.query.filter_by(username=EXECUTOR).first()
    log_audit(
        action,
        actor=hermes,
        target_type="application_analysis",
        target_id=str(analysis.id),
        details={
            "requested_by_user_id": analysis.requested_by_user_id,
            "executed_by": EXECUTOR,
            "application_id": analysis.application_id,
            "repository": (
                f"{analysis.application.repository_workspace}/"
                f"{analysis.application.repository_name}"
            ),
            "commit_sha": analysis.commit_sha,
            **(details or {}),
        },
    )


def request_analysis(application_id: int, payload: dict, user: User) -> dict:
    application = get_application(application_id)
    mode = str(payload.get("analysisMode") or "Quick").strip()
    if mode not in ALL_MODES:
        raise ValueError("Analysis mode must be Quick, Deep, or Build Verified.")
    revision = (
        str(payload.get("revision") or application.default_branch or "main").strip() or "main"
    )
    callback_token = secrets.token_urlsafe(32)
    row = ApplicationAnalysis(
        application_id=application.id,
        status="Queued",
        progress_percent=0,
        current_stage="Queued",
        analysis_mode=mode,
        requested_by_user_id=user.id,
        executed_by_account=EXECUTOR,
        branch=revision,
        requested_revision=revision,
        worker_callback_token_hash=hashlib.sha256(callback_token.encode()).hexdigest(),
    )
    db.session.add(row)
    db.session.commit()
    log_audit(
        "application.analysis.started",
        actor=user,
        target_type="application_analysis",
        target_id=str(row.id),
        details={
            "requested_by_user_id": user.id,
            "executed_by": EXECUTOR,
            "application_id": application.id,
            "repository": f"{application.repository_workspace}/{application.repository_name}",
            "commit_sha": None,
            "analysis_mode": mode,
        },
    )

    credential = application.credential_profile
    repository_token = decrypt_secret(credential.secret_cipher if credential else "")
    try:
        execution_mode = os.getenv(
            "APPLICATION_ANALYSIS_EXECUTION_MODE", "kubernetes"
        ).strip().lower()
        if current_app.config.get("TESTING") or execution_mode == "disabled":
            row.worker_job_name = f"test-app-analysis-{row.id}"
        elif execution_mode == "local_docker":
            row.worker_job_name = application_analysis_local_docker.launch(
                analysis_id=row.id,
                repository_url=application.repository_url,
                revision=revision,
                subdirectory=application.repository_subdirectory or "",
                repository_token=repository_token,
                repository_credential_type=(
                    credential.credential_type if credential else "oauth"
                ),
                repository_principal=(
                    (credential.principal or "") if credential else ""
                ),
                callback_token=callback_token,
                analysis_mode=mode,
            )
        else:
            resources = application_analysis_jobs.build_job_resources(
                analysis_id=row.id,
                repository_url=application.repository_url,
                revision=revision,
                subdirectory=application.repository_subdirectory or "",
                repository_token=repository_token,
                callback_token=callback_token,
                analysis_mode=mode,
                repository_credential_type=(
                    credential.credential_type if credential else "oauth"
                ),
                repository_principal=(
                    (credential.principal or "") if credential else ""
                ),
            )
            row.worker_job_name = application_analysis_jobs.launch(resources)
        db.session.commit()
    except Exception as exc:
        row.status = "Failed"
        row.current_stage = "Scheduling"
        row.failed_at = _now()
        row.failure_stage = "Scheduling"
        row.safe_error_message = safe_error(
            exc, "The isolated analysis job could not be scheduled."
        )
        row.workspace_cleanup_status = "Not Created"
        db.session.commit()
        _audit_execution(
            "application.analysis.failed",
            row,
            {"failure_stage": "Scheduling", "safe_error": row.safe_error_message},
        )
    finally:
        repository_token = ""
        callback_token = ""
    return analysis_to_dict(row)


def list_analyses(application_id: int, page=1, per_page=25) -> dict:
    get_application(application_id)
    query = ApplicationAnalysis.query.filter_by(application_id=application_id).order_by(
        ApplicationAnalysis.created_at.desc()
    )
    rows, total, page, per_page = _page(query, page, per_page)
    return {
        "items": [analysis_to_dict(row, compact=True) for row in rows],
        "pagination": {"page": page, "perPage": per_page, "total": total},
    }


def compare_analyses(analysis_id: int, baseline_analysis_id: int) -> dict:
    current = get_analysis(analysis_id)
    baseline = get_analysis(baseline_analysis_id)
    if current.application_id != baseline.application_id:
        raise ValueError("Analyses can only be compared within the same application.")
    if current.id == baseline.id:
        raise ValueError("Select two different analyses to compare.")

    def finding_key(item: ApplicationFinding):
        return (
            (item.scanner_source or "").lower(),
            item.category.lower(),
            item.title.lower(),
            (item.file_path or "").lower(),
            item.start_line or 0,
        )

    current_findings = {
        finding_key(item): finding_to_dict(item)
        for item in current.findings.order_by(ApplicationFinding.id.asc()).all()
    }
    baseline_findings = {
        finding_key(item): finding_to_dict(item)
        for item in baseline.findings.order_by(ApplicationFinding.id.asc()).all()
    }

    def dependency_key(item: ApplicationDependency):
        return ((item.ecosystem or "").lower(), item.name.lower())

    current_dependencies = {
        dependency_key(item): item
        for item in current.dependencies.order_by(ApplicationDependency.id.asc()).all()
    }
    baseline_dependencies = {
        dependency_key(item): item
        for item in baseline.dependencies.order_by(ApplicationDependency.id.asc()).all()
    }
    changed_dependencies = []
    for key in sorted(set(current_dependencies) & set(baseline_dependencies)):
        before = baseline_dependencies[key]
        after = current_dependencies[key]
        if before.version != after.version or before.license != after.license:
            changed_dependencies.append(
                {
                    "name": after.name,
                    "ecosystem": after.ecosystem,
                    "beforeVersion": before.version,
                    "afterVersion": after.version,
                    "beforeLicense": before.license,
                    "afterLicense": after.license,
                }
            )

    def dependency_dto(item: ApplicationDependency) -> dict:
        return {
            "name": item.name,
            "version": item.version,
            "ecosystem": item.ecosystem,
            "license": item.license,
            "sourceFile": item.source_file,
        }

    # Severity movement, not score movement: both sides are counted from the
    # findings actually persisted for each run.
    baseline_posture = _finding_posture(baseline.id)
    current_posture = _finding_posture(current.id)
    severity_deltas = {
        severity: {
            "baseline": baseline_posture["openBySeverity"][severity],
            "current": current_posture["openBySeverity"][severity],
            "delta": (
                current_posture["openBySeverity"][severity]
                - baseline_posture["openBySeverity"][severity]
            ),
        }
        for severity in SEVERITY_ORDER
    }

    new_keys = set(current_findings) - set(baseline_findings)
    resolved_keys = set(baseline_findings) - set(current_findings)
    unchanged_keys = set(current_findings) & set(baseline_findings)
    added_dependency_keys = set(current_dependencies) - set(baseline_dependencies)
    removed_dependency_keys = set(baseline_dependencies) - set(current_dependencies)
    return {
        "baseline": analysis_to_dict(baseline, compact=True),
        "current": analysis_to_dict(current, compact=True),
        "severityDeltas": severity_deltas,
        "riskLevel": {
            "baseline": baseline_posture["riskLevel"],
            "current": current_posture["riskLevel"],
        },
        "findings": {
            "new": [current_findings[key] for key in sorted(new_keys)],
            "resolved": [baseline_findings[key] for key in sorted(resolved_keys)],
            "unchangedCount": len(unchanged_keys),
        },
        "dependencies": {
            "added": [
                dependency_dto(current_dependencies[key])
                for key in sorted(added_dependency_keys)
            ],
            "removed": [
                dependency_dto(baseline_dependencies[key])
                for key in sorted(removed_dependency_keys)
            ],
            "changed": changed_dependencies,
        },
    }


def pull_request_to_dict(row: ApplicationPullRequest) -> dict:
    return {
        "id": row.id,
        "analysisId": row.analysis_id,
        "credentialProfileId": row.credential_profile_id,
        "requestedBy": row.requested_by.username if row.requested_by else None,
        "selectedFindingIds": row.selected_finding_ids or [],
        "branchName": row.branch_name,
        "destinationBranch": row.destination_branch,
        "title": row.title,
        "description": row.description,
        "status": row.status,
        "providerPullRequestId": row.provider_pull_request_id,
        "providerUrl": row.provider_url,
        "validationSummary": redact_structure(row.validation_summary or {}),
        "safeErrorMessage": row.safe_error_message,
        "completedAt": _iso(row.completed_at),
        "createdAt": _iso(row.created_at),
    }


def list_pull_requests(analysis_id: int) -> dict:
    row = get_analysis(analysis_id)
    items = [
        pull_request_to_dict(item)
        for item in row.pull_requests.order_by(
            ApplicationPullRequest.created_at.desc()
        ).all()
    ]
    return {"items": items, "count": len(items)}


def request_pull_request(
    analysis_id: int, payload: dict, user: User
) -> dict:
    analysis = get_analysis(analysis_id)
    if analysis.status not in {"Completed", "Completed With Warnings"}:
        raise ValueError("A completed analysis is required before creating a pull request.")
    if not analysis.commit_sha:
        raise ValueError("The analyzed commit is unavailable.")
    try:
        credential_id = int(payload.get("credentialProfileId"))
    except (TypeError, ValueError):
        raise ValueError("Select a separate write-enabled Bitbucket credential profile.")
    credential = db.session.get(BitbucketCredentialProfile, credential_id)
    if (
        credential is None
        or not credential.enabled
        or credential.read_only
        or not credential.secret_cipher
    ):
        raise ValueError("Select a separate write-enabled Bitbucket credential profile.")
    finding_ids = payload.get("findingIds")
    if (
        not isinstance(finding_ids, list)
        or not finding_ids
        or len(finding_ids) > 50
    ):
        raise ValueError("Select between 1 and 50 findings with suggested patches.")
    try:
        finding_ids = list(dict.fromkeys(int(value) for value in finding_ids))
    except (TypeError, ValueError):
        raise ValueError("Selected finding identifiers are invalid.")
    findings = ApplicationFinding.query.filter(
        ApplicationFinding.analysis_id == analysis.id,
        ApplicationFinding.id.in_(finding_ids),
    ).all()
    if len(findings) != len(finding_ids) or any(
        not item.suggested_patch for item in findings
    ):
        raise ValueError("Every selected finding must belong to this analysis and have a patch.")
    total_patch_bytes = sum(
        len((item.suggested_patch or "").encode("utf-8")) for item in findings
    )
    if total_patch_bytes > 750_000:
        raise ValueError("The selected patch bundle exceeds the configured limit.")
    destination = analysis.application.default_branch or "main"
    branch = str(payload.get("branchName") or "").strip() or (
        f"kubesight/analysis-{analysis.id}-{int(time.time())}"
    )
    if (
        not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._/-]{0,199}", branch)
        or branch.endswith(("/", "."))
        or ".." in branch
        or branch == destination
    ):
        raise ValueError("Choose a valid non-default pull-request branch name.")
    title = str(payload.get("title") or "").strip() or (
        f"KubeSight recommendations for {analysis.application.name}"
    )
    if len(title) > 500:
        raise ValueError("Pull-request title is too long.")
    finding_lines = "\n".join(
        f"- Finding #{item.id}: {item.title}" for item in findings
    )
    changed_files = sorted(
        {item.file_path for item in findings if item.file_path}
    )
    description = (
        f"{str(payload.get('description') or '').strip()}\n\n"
        "## KubeSight Application Intelligence\n\n"
        f"- Analysis ID: {analysis.id}\n"
        f"- Original commit: `{analysis.commit_sha}`\n"
        f"- Selected findings: {len(findings)}\n"
        f"- Proposed files: {', '.join(changed_files) or 'Determined by patch'}\n"
        "- Validation: isolated Build Verified checks must pass before publication\n"
        "- Assistance: suggestions were generated with Hermes; an authorized human "
        "selected and requested these changes.\n\n"
        "### Findings\n"
        f"{finding_lines}\n"
    ).strip()
    callback_token = secrets.token_urlsafe(32)
    row = ApplicationPullRequest(
        analysis_id=analysis.id,
        credential_profile_id=credential.id,
        requested_by_user_id=user.id,
        selected_finding_ids=finding_ids,
        branch_name=branch,
        destination_branch=destination,
        title=title,
        description=description,
        status="Queued",
        worker_callback_token_hash=hashlib.sha256(callback_token.encode()).hexdigest(),
    )
    db.session.add(row)
    db.session.commit()
    log_audit(
        "application.pull_request.requested",
        actor=user,
        target_type="application_pull_request",
        target_id=str(row.id),
        details={
            "analysis_id": analysis.id,
            "original_commit": analysis.commit_sha,
            "finding_ids": finding_ids,
            "branch": branch,
            "destination_branch": destination,
            "credential_profile_id": credential.id,
            "executed_by": EXECUTOR,
        },
    )
    bundle = {
        "repositoryUrl": analysis.application.repository_url,
        "repositoryRef": (
            f"{analysis.application.repository_workspace}/"
            f"{analysis.application.repository_name}"
        ),
        "commitSha": analysis.commit_sha,
        "branchName": branch,
        "destinationBranch": destination,
        "title": title,
        "description": description,
        "commitMessage": f"KubeSight: apply selected findings from analysis {analysis.id}",
        "patches": [
            {"findingId": item.id, "content": item.suggested_patch}
            for item in sorted(findings, key=lambda value: value.id)
        ],
    }
    write_token = decrypt_secret(credential.secret_cipher)
    try:
        execution_mode = os.getenv(
            "APPLICATION_ANALYSIS_EXECUTION_MODE", "kubernetes"
        ).strip().lower()
        if current_app.config.get("TESTING") or execution_mode == "disabled":
            row.worker_job_name = f"test-app-pr-{row.id}"
        elif execution_mode == "local_docker":
            row.worker_job_name = application_pull_request_local_docker.launch(
                pull_request_id=row.id,
                bundle=bundle,
                write_token=write_token,
                credential_type=credential.credential_type,
                principal=credential.principal or "",
                callback_token=callback_token,
                subdirectory=analysis.application.repository_subdirectory or "",
            )
        else:
            from . import application_pull_request_jobs

            resources = application_pull_request_jobs.build_job_resources(
                pull_request_id=row.id,
                bundle=bundle,
                write_token=write_token,
                credential_type=credential.credential_type,
                principal=credential.principal or "",
                callback_token=callback_token,
                subdirectory=analysis.application.repository_subdirectory or "",
            )
            row.worker_job_name = application_pull_request_jobs.launch(resources)
        db.session.commit()
    except Exception as exc:
        row.status = "Failed"
        row.safe_error_message = safe_error(
            exc, "The isolated pull-request job could not be scheduled."
        )
        row.completed_at = _now()
        db.session.commit()
    finally:
        write_token = ""
        callback_token = ""
    return pull_request_to_dict(row)


def verify_pull_request_worker_token(
    row: ApplicationPullRequest, token: str
) -> bool:
    expected = row.worker_callback_token_hash or ""
    actual = hashlib.sha256(str(token or "").encode()).hexdigest()
    return bool(expected and hmac.compare_digest(expected, actual))


def record_pull_request_result(
    row: ApplicationPullRequest, payload: dict
) -> dict:
    if row.status == "Created":
        return pull_request_to_dict(row)
    status = str(payload.get("status") or "")
    if status not in {"Created", "Failed", "Validation Failed"}:
        raise ValueError("Pull-request worker status is invalid.")
    row.status = status
    row.provider_pull_request_id = (
        str(payload.get("providerPullRequestId") or "")[:120] or None
    )
    provider_url = str(payload.get("providerUrl") or "").strip()
    if provider_url and not provider_url.startswith("https://bitbucket.org/"):
        provider_url = ""
    row.provider_url = provider_url[:1024] or None
    row.validation_summary = redact_structure(
        payload.get("validationSummary") or {}
    )
    row.safe_error_message = (
        safe_error(RuntimeError(payload.get("safeErrorMessage") or ""))
        if status != "Created"
        else None
    )
    row.completed_at = _now()
    db.session.commit()
    action = (
        "application.pull_request.created"
        if status == "Created"
        else "application.pull_request.failed"
    )
    hermes = User.query.filter_by(username=EXECUTOR).first()
    log_audit(
        action,
        actor=hermes,
        target_type="application_pull_request",
        target_id=str(row.id),
        details={
            "analysis_id": row.analysis_id,
            "original_commit": row.analysis.commit_sha,
            "branch": row.branch_name,
            "destination_branch": row.destination_branch,
            "provider_pull_request_id": row.provider_pull_request_id,
            "changed_files": redact_structure(payload.get("changedFiles") or []),
            "validation_status": (row.validation_summary or {}).get("status"),
            "requested_by_user_id": row.requested_by_user_id,
            "executed_by": EXECUTOR,
        },
    )
    return pull_request_to_dict(row)


def get_analysis(analysis_id: int) -> ApplicationAnalysis:
    row = db.session.get(ApplicationAnalysis, analysis_id)
    if row is None:
        raise LookupError("Analysis not found.")
    return row


def cancel_analysis(analysis_id: int, user: User) -> dict:
    row = get_analysis(analysis_id)
    if row.status in TERMINAL_STATUSES:
        raise ValueError("This analysis is already complete.")
    if row.worker_job_name and not current_app.config.get("TESTING"):
        if row.worker_job_name.startswith("ks-app-analysis-local-"):
            application_analysis_local_docker.cancel(row.worker_job_name)
        else:
            application_analysis_jobs.cancel(row.worker_job_name)
    row.status = "Cancelled"
    row.current_stage = "Cancelled"
    row.completed_at = _now()
    row.safe_error_message = "Analysis cancelled by an authorized user."
    db.session.commit()
    log_audit(
        "application.analysis.cancelled",
        actor=user,
        target_type="application_analysis",
        target_id=str(row.id),
        details={"requested_by_user_id": row.requested_by_user_id, "executed_by": EXECUTOR},
    )
    return analysis_to_dict(row)


def verify_worker_token(row: ApplicationAnalysis, token: str) -> bool:
    expected = row.worker_callback_token_hash or ""
    actual = hashlib.sha256(str(token or "").encode()).hexdigest()
    return bool(expected and hmac.compare_digest(expected, actual))


def record_worker_event(row: ApplicationAnalysis, payload: dict) -> dict:
    status = str(payload.get("status") or "").strip()
    if status not in APPLICATION_STATUSES:
        raise ValueError("Invalid analysis status.")
    if row.status in {"Cancelled", "Completed", "Completed With Warnings"}:
        return analysis_to_dict(row)
    row.status = status
    row.current_stage = status
    row.progress_percent = min(100, max(0, int(payload.get("progressPercent") or 0)))
    if status not in {"Queued", "Failed"} and not row.started_at:
        row.started_at = _now()
    if payload.get("commitSha"):
        sha = str(payload["commitSha"]).strip().lower()
        if re.fullmatch(r"[0-9a-f]{40,64}", sha):
            row.commit_sha = sha
    if status == "Failed":
        row.failed_at = _now()
        row.failure_stage = str(payload.get("failureStage") or row.current_stage)[:64]
        row.safe_error_message = safe_error(
            RuntimeError(payload.get("safeErrorMessage") or ""), "Analysis failed safely."
        )
    db.session.commit()

    scanner = str(payload.get("scanner") or "").strip()
    scanner_event = str(payload.get("scannerEvent") or "").strip()
    action = "application.analysis.status"
    if status == "Cloning":
        action = "application.repository.cloned.started"
    elif status == "Discovering" and row.commit_sha:
        action = "application.repository.cloned"
    elif scanner and scanner_event:
        action = f"application.scanner.{scanner_event}"
    elif status == "Analyzing":
        action = "application.hermes.started"
    elif status == "Failed":
        action = "application.analysis.failed"
    _audit_execution(
        action,
        row,
        {"status": status, "scanner": scanner or None, "exit_status": payload.get("exitStatus")},
    )
    return analysis_to_dict(row)


def _fingerprint(_analysis_id: int, finding: dict) -> str:
    provided = str(finding.get("fingerprint") or "").strip()
    if re.fullmatch(r"[0-9a-fA-F]{64}", provided):
        return provided.lower()
    material = "|".join(
        [
            str(finding.get("scanner_source") or finding.get("scannerSource") or "Hermes"),
            str(finding.get("category") or ""),
            str(finding.get("title") or ""),
            str(finding.get("file") or finding.get("file_path") or ""),
            str(finding.get("line") or finding.get("start_line") or ""),
        ]
    )
    return hashlib.sha256(material.encode()).hexdigest()


def _artifact_root() -> Path:
    configured = os.getenv("APPLICATION_ANALYSIS_ARTIFACT_ROOT", "").strip()
    return (
        Path(configured)
        if configured
        else Path(current_app.instance_path) / "application-analysis-artifacts"
    )


def _store_artifact(
    row: ApplicationAnalysis, artifact_type: str, filename: str, content: str
) -> None:
    safe_filename = re.sub(r"[^A-Za-z0-9._-]+", "-", filename).strip("-")
    relative = Path(str(row.id)) / safe_filename
    root = _artifact_root().resolve()
    path = (root / relative).resolve()
    if root not in path.parents:
        raise ValueError("Invalid artifact storage path.")
    path.parent.mkdir(parents=True, exist_ok=True)
    encoded = content.encode("utf-8")
    path.write_bytes(encoded)
    db.session.add(
        ApplicationArtifact(
            analysis_id=row.id,
            artifact_type=artifact_type,
            filename=safe_filename,
            storage_reference=relative.as_posix(),
            checksum=hashlib.sha256(encoded).hexdigest(),
        )
    )


def _build_verification(payload: Any) -> dict | None:
    if payload is None:
        return None
    if (
        not isinstance(payload, dict)
        or payload.get("schemaVersion") != 1
        or payload.get("status")
        not in {"Passed", "Completed With Warnings", "Failed", "Unavailable"}
        or not isinstance(payload.get("commands"), list)
        or len(payload["commands"]) > 6
    ):
        raise ValueError("Build verification report is invalid.")
    commands = []
    for item in payload["commands"]:
        if (
            not isinstance(item, dict)
            or item.get("status")
            not in {"Passed", "Failed", "Timed Out", "Unavailable"}
            or not isinstance(item.get("command"), list)
        ):
            raise ValueError("Build verification command result is invalid.")
        commands.append(
            {
                "label": str(item.get("label") or "")[:120],
                "command": [str(value)[:200] for value in item["command"][:12]],
                "status": item["status"],
                "exitCode": (
                    item.get("exitCode")
                    if isinstance(item.get("exitCode"), int)
                    else None
                ),
                "output": str(item.get("output") or "")[:12000],
            }
        )
    return {
        "schemaVersion": 1,
        "status": payload["status"],
        "startedAt": str(payload.get("startedAt") or "")[:64] or None,
        "completedAt": str(payload.get("completedAt") or "")[:64] or None,
        "networkPolicy": (
            payload.get("networkPolicy")
            if payload.get("networkPolicy")
            in {
                "Deny all network access",
                "Controlled proxy unavailable to build process",
            }
            else "Unknown"
        ),
        "credentialExposure": "None",
        "commands": commands,
    }


def _dependency_components(row: ApplicationAnalysis) -> list[dict]:
    components = []
    seen = set()
    for item in row.dependencies.order_by(ApplicationDependency.name.asc()).all():
        key = (
            (item.ecosystem or "generic").lower(),
            item.name.lower(),
            item.version or "",
        )
        if key in seen:
            continue
        seen.add(key)
        components.append(
            {
                "type": "library",
                "name": item.name,
                "version": item.version,
                "ecosystem": item.ecosystem,
                "license": item.license,
                "sourceFile": item.source_file,
                "bomRef": "pkg:"
                + "/".join(
                    value
                    for value in (
                        (item.ecosystem or "generic").lower(),
                        item.name,
                    )
                    if value
                )
                + (f"@{item.version}" if item.version else ""),
            }
        )
    return components


def _store_sboms(row: ApplicationAnalysis) -> None:
    components = _dependency_components(row)
    timestamp = _now().isoformat()
    repository = (
        f"{row.application.repository_workspace}/{row.application.repository_name}"
    )
    cyclonedx = {
        "bomFormat": "CycloneDX",
        "specVersion": "1.5",
        "serialNumber": f"urn:uuid:{uuid.uuid5(uuid.NAMESPACE_URL, f'kubesight:{row.id}')}",
        "version": 1,
        "metadata": {
            "timestamp": timestamp,
            "component": {
                "type": "application",
                "name": row.application.name,
                "version": row.commit_sha or row.requested_revision or "unknown",
                "properties": [
                    {"name": "kubesight:analysisId", "value": str(row.id)},
                    {"name": "kubesight:repository", "value": repository},
                ],
            },
        },
        "components": [
            {
                "type": item["type"],
                "name": item["name"],
                **({"version": item["version"]} if item["version"] else {}),
                "bom-ref": item["bomRef"],
                **(
                    {"licenses": [{"license": {"name": item["license"]}}]}
                    if item["license"]
                    else {}
                ),
                **(
                    {
                        "properties": [
                            {"name": "kubesight:sourceFile", "value": item["sourceFile"]}
                        ]
                    }
                    if item["sourceFile"]
                    else {}
                ),
            }
            for item in components
        ],
    }
    namespace = (
        "https://kubesight.local/spdx/"
        + str(uuid.uuid5(uuid.NAMESPACE_URL, f"kubesight-spdx:{row.id}"))
    )
    spdx_packages = []
    for item in components:
        package_id = hashlib.sha256(item["bomRef"].encode()).hexdigest()[:16]
        spdx_packages.append(
            {
                "name": item["name"],
                "SPDXID": f"SPDXRef-Package-{package_id}",
                "downloadLocation": "NOASSERTION",
                "filesAnalyzed": False,
                "versionInfo": item["version"] or "NOASSERTION",
                "licenseConcluded": item["license"] or "NOASSERTION",
                "licenseDeclared": item["license"] or "NOASSERTION",
                "supplier": "NOASSERTION",
            }
        )
    spdx = {
        "spdxVersion": "SPDX-2.3",
        "dataLicense": "CC0-1.0",
        "SPDXID": "SPDXRef-DOCUMENT",
        "name": f"{row.application.name}-analysis-{row.id}",
        "documentNamespace": namespace,
        "creationInfo": {
            "created": timestamp,
            "creators": ["Tool: KubeSight Application Intelligence"],
        },
        "packages": spdx_packages,
    }
    _store_artifact(
        row,
        "CycloneDX SBOM",
        f"application-analysis-{row.id}.cdx.json",
        json.dumps(cyclonedx, indent=2, ensure_ascii=False),
    )
    _store_artifact(
        row,
        "SPDX SBOM",
        f"application-analysis-{row.id}.spdx.json",
        json.dumps(spdx, indent=2, ensure_ascii=False),
    )


def record_worker_result(row: ApplicationAnalysis, payload: dict) -> dict:
    if row.status in {"Cancelled", "Completed", "Completed With Warnings"}:
        return analysis_to_dict(row)
    result = validate_hermes_output(redact_structure(payload.get("result")))
    build_verification = _build_verification(
        redact_structure(payload.get("buildVerification"))
    )
    if build_verification:
        result.setdefault("application_profile", {})[
            "build_verification"
        ] = build_verification
    # Hermes sees the build report as evidence and has been observed restating
    # it incorrectly (claiming a verified build while the stage failed). Only
    # KubeSight's own report may speak to build outcome.
    operational = result.get("operational_readiness")
    if isinstance(operational, dict):
        operational.pop("build_verified", None)

    deterministic_findings = redact_structure(payload.get("scannerFindings") or [])
    scanner_severity = {
        "Critical": "Critical",
        "High": "High",
        "Error": "High",
        "Medium": "Medium",
        "Warning": "Medium",
        "Low": "Low",
        "Info": "Informational",
        "Unknown": "Informational",
    }
    combined_findings = list(result.get("findings", []))
    if build_verification and build_verification["status"] in {
        "Failed",
        "Unavailable",
        "Completed With Warnings",
    }:
        severity = (
            "High" if build_verification["status"] == "Failed" else "Medium"
        )
        combined_findings.append(
            {
                "category": "Build Verification",
                "severity": severity,
                "confidence": "Confirmed",
                "title": f"Build verification {build_verification['status'].lower()}",
                "description": (
                    "The credential-free isolated build/test stage did not pass all "
                    "detected verification commands."
                ),
                "recommendation": (
                    "Review the Build Verification artifact, make the build "
                    "reproducible without hidden dependencies, and rerun this mode."
                ),
                "scanner_source": "KubeSight Build Verifier",
            }
        )
    for item in deterministic_findings:
        if not isinstance(item, dict) or item.get("type") == "dependency":
            continue
        combined_findings.append(
            {
                "category": "Deterministic Scanner",
                "severity": scanner_severity.get(
                    str(item.get("severity") or "").title(), "Informational"
                ),
                "confidence": "Confirmed",
                "title": item.get("title") or item.get("ruleId") or "Scanner finding",
                "description": item.get("title") or "A deterministic scanner reported this item.",
                "file": item.get("file"),
                "start_line": item.get("startLine"),
                "end_line": item.get("endLine"),
                "scanner_source": item.get("scanner"),
                "cve": (
                    item.get("ruleId")
                    if str(item.get("ruleId") or "").upper().startswith("CVE-")
                    else None
                ),
            }
        )
    for item in combined_findings:
        fingerprint = _fingerprint(row.id, item)
        if ApplicationFinding.query.filter_by(
            analysis_id=row.id, fingerprint=fingerprint
        ).first():
            continue
        db.session.add(
            ApplicationFinding(
                analysis_id=row.id,
                category=str(item.get("category") or "Source"),
                severity=str(item.get("severity") or "Informational"),
                confidence=str(item.get("confidence") or "Low"),
                title=str(item.get("title") or "")[:500],
                description=str(item.get("description") or item.get("explanation") or ""),
                impact=str(item.get("impact") or "") or None,
                recommendation=str(item.get("recommendation") or "") or None,
                evidence=str(item.get("evidence") or "")[:4000] or None,
                evidence_type=str(item.get("evidence_type") or "Hermes")[:64],
                file_path=str(item.get("file") or item.get("file_path") or "")[:1024] or None,
                start_line=item.get("line") or item.get("start_line"),
                end_line=item.get("end_line"),
                scanner_source=str(
                    item.get("scanner_source") or item.get("scannerSource") or "Hermes"
                )[:120],
                cwe=str(item.get("cwe") or "")[:64] or None,
                cve=str(item.get("cve") or "")[:64] or None,
                fingerprint=fingerprint,
                suggested_patch=str(item.get("suggested_patch") or "") or None,
            )
        )
    for item in redact_structure(payload.get("dependencies") or []):
        if not isinstance(item, dict) or not item.get("name"):
            continue
        db.session.add(
            ApplicationDependency(
                analysis_id=row.id,
                dependency_type=str(item.get("type") or "package")[:64],
                name=str(item.get("name"))[:500],
                version=str(item.get("version") or "")[:255] or None,
                ecosystem=str(item.get("ecosystem") or "")[:64] or None,
                direct=bool(item.get("direct", True)),
                vulnerable=bool(item.get("vulnerable", False)),
                license=", ".join(str(value) for value in item.get("licenses") or [])[:255]
                or None,
                source_file=str(item.get("source") or "")[:1024] or None,
                dependency_metadata={},
            )
        )
    for item in result.get("communications", []):
        if not isinstance(item, dict):
            continue
        db.session.add(
            ApplicationCommunication(
                analysis_id=row.id,
                source_component=str(item.get("source") or row.application.name)[:255],
                destination_component=str(item.get("destination") or "Unknown")[:500],
                destination_type=str(item.get("destination_type") or "Unknown")[:64],
                protocol=str(item.get("protocol") or "")[:32] or None,
                port=item.get("port") if isinstance(item.get("port"), int) else None,
                direction=str(item.get("direction") or "Outbound")[:16],
                endpoint=str(item.get("endpoint") or "")[:1024] or None,
                configuration_key=str(item.get("configuration_key") or "")[:255] or None,
                required=bool(item.get("required", True)),
                evidence=str(item.get("evidence") or "") or None,
                file_path=str(item.get("file") or "")[:1024] or None,
                line_number=item.get("line") if isinstance(item.get("line"), int) else None,
                confidence=str(item.get("confidence") or "Low")[:32],
                evidence_state=str(item.get("evidence_state") or "Source Inferred")[:64],
            )
        )

    scanner_runs = redact_structure(payload.get("scannerRuns") or [])
    row.scanner_runs = scanner_runs
    row.scanner_versions = {
        str(item.get("name")): item.get("version")
        for item in scanner_runs
        if isinstance(item, dict) and item.get("name")
    }
    row.result_summary = result
    row.hermes_model = str(payload.get("hermesModel") or "")[:120] or None
    row.hermes_prompt_version = (
        str(payload.get("hermesPromptVersion") or "")[:64] or None
    )
    row.warnings = redact_structure(payload.get("warnings") or [])
    row.source_coverage = _source_coverage(payload.get("evidenceCoverage"))
    row.topology_confidence = str(
        (result.get("architecture_summary") or {}).get("topology_confidence") or ""
    )[:32] or None
    row.status = "Completed With Warnings" if row.warnings else "Completed"
    row.progress_percent = 100
    row.current_stage = row.status
    row.completed_at = _now()
    row.workspace_cleanup_status = str(
        payload.get("workspaceCleanupStatus") or row.workspace_cleanup_status
    )[:32]
    # Persist only redacted, validated output. Reports are review/download
    # artifacts and never modify the checked-out repository.
    _store_artifact(
        row,
        "JSON report",
        f"application-analysis-{row.id}.json",
        json.dumps(result, indent=2, ensure_ascii=False),
    )
    db.session.flush()
    _store_sboms(row)
    if build_verification:
        _store_artifact(
            row,
            "Build Verification",
            f"build-verification-{row.id}.json",
            json.dumps(build_verification, indent=2, ensure_ascii=False),
        )
    docker_analysis = result.get("docker_analysis") or {}
    proposed = docker_analysis.get("hardened_dockerfile") or docker_analysis.get(
        "proposed_dockerfile"
    )
    if isinstance(proposed, str) and proposed.strip():
        _store_artifact(
            row, "Hardened Dockerfile", f"Dockerfile.proposed-{row.id}", proposed
        )
    docker_diff = docker_analysis.get("diff") or docker_analysis.get("dockerfile_diff")
    if isinstance(docker_diff, str) and docker_diff.strip():
        _store_artifact(
            row, "Dockerfile diff", f"dockerfile-{row.id}.patch", docker_diff
        )
    db.session.commit()
    _audit_execution("application.hermes.completed", row, {"status": row.status})
    _audit_execution("application.analysis.completed", row, {"status": row.status})
    return analysis_to_dict(row)


def record_cleanup(row: ApplicationAnalysis, payload: dict) -> dict:
    row.workspace_cleanup_status = str(
        payload.get("workspaceCleanupStatus") or "Unknown"
    )[:32]
    db.session.commit()
    if not current_app.config.get("TESTING"):
        try:
            if not (row.worker_job_name or "").startswith(
                "ks-app-analysis-local-"
            ):
                application_analysis_jobs.cleanup_auxiliary(row.id)
        except Exception:
            # Workspace deletion remains authoritative; Kubernetes owner
            # references and Job TTL provide the second cleanup path.
            pass
    _audit_execution(
        "application.workspace.deleted",
        row,
        {"cleanup_status": row.workspace_cleanup_status},
    )
    return analysis_to_dict(row)


def list_findings(analysis_id: int, filters: dict, page=1, per_page=25) -> dict:
    get_analysis(analysis_id)
    query = ApplicationFinding.query.filter_by(analysis_id=analysis_id)
    columns = {
        "severity": ApplicationFinding.severity,
        "category": ApplicationFinding.category,
        "confidence": ApplicationFinding.confidence,
        "status": ApplicationFinding.status,
        "scanner": ApplicationFinding.scanner_source,
        "file": ApplicationFinding.file_path,
    }
    for key, column in columns.items():
        value = str(filters.get(key) or "").strip()
        if value:
            query = query.filter(column == value)
    query = query.order_by(ApplicationFinding.created_at.desc())
    rows, total, page, per_page = _page(query, page, per_page)
    return {
        "items": [finding_to_dict(row) for row in rows],
        "pagination": {"page": page, "perPage": per_page, "total": total},
    }


def topology(analysis_id: int) -> dict:
    row = get_analysis(analysis_id)
    edges = [
        {
            "id": item.id,
            "source": item.source_component,
            "destination": item.destination_component,
            "destinationType": item.destination_type,
            "protocol": item.protocol,
            "port": item.port,
            "direction": item.direction,
            "endpoint": item.endpoint,
            "configurationKey": item.configuration_key,
            "required": item.required,
            "evidence": item.evidence,
            "filePath": item.file_path,
            "lineNumber": item.line_number,
            "confidence": item.confidence,
            "evidenceState": item.evidence_state,
        }
        for item in row.communications.order_by(ApplicationCommunication.id.asc()).all()
    ]
    # Hermes names the analyzed service however the repository does ("profile
    # service"); the graph's root is the KubeSight application. Resolve every
    # edge whose source is not another observed peer onto that root, otherwise
    # the edges reference a node that does not exist and the graph is empty.
    application_node = row.application.name
    destinations = {edge["destination"] for edge in edges}
    for edge in edges:
        edge["sourceLabel"] = edge["source"]
        if edge["source"] not in destinations:
            edge["source"] = application_node
    nodes = [{"id": application_node, "type": "Application"}]
    seen = {application_node}
    for edge in edges:
        if edge["destination"] not in seen:
            seen.add(edge["destination"])
            nodes.append({"id": edge["destination"], "type": edge["destinationType"]})
    return {"nodes": nodes, "edges": edges}


def result_section(analysis_id: int, section: str):
    row = get_analysis(analysis_id)
    return redact_structure((row.result_summary or {}).get(section, []))


def list_artifacts(analysis_id: int) -> dict:
    row = get_analysis(analysis_id)
    items = [
        {
            "id": item.id,
            "artifactType": item.artifact_type,
            "filename": item.filename,
            "checksum": item.checksum,
            "createdAt": _iso(item.created_at),
        }
        for item in row.artifacts.order_by(ApplicationArtifact.created_at.desc()).all()
    ]
    return {"items": items, "count": len(items)}


def artifact_path(artifact_id: int) -> tuple[ApplicationArtifact, Path]:
    artifact = db.session.get(ApplicationArtifact, artifact_id)
    if artifact is None:
        raise LookupError("Artifact not found.")
    root = _artifact_root()
    path = (root / artifact.storage_reference).resolve()
    if root.resolve() not in path.parents or not path.is_file():
        raise LookupError("Artifact is not available.")
    if hashlib.sha256(path.read_bytes()).hexdigest() != artifact.checksum:
        raise ValueError("Artifact checksum validation failed.")
    return artifact, path


# ─── stale analyses ───

# Generous by design: an analysis clones a repository, builds an image and runs
# scanners, so hours is normal and killing a slow one is worse than leaving a
# dead one a little longer.
STALE_ANALYSIS_TIMEOUT_SECONDS = int(
    os.getenv("APPLICATION_ANALYSIS_TIMEOUT_SECONDS", str(6 * 60 * 60))
)


def reap_stale_analyses(*, now: datetime | None = None) -> int:
    """Fail analyses that stopped reporting. Returns how many.

    An analysis runs in a container that reports back over a callback. Nothing
    watched for the container that never calls -- it died, the host went away,
    or the process restarted between spawning it and the first callback. The row
    stayed non-terminal forever.

    That is not merely untidy. A non-terminal analysis blocks
    `delete_application`, and the operator has no way to tell a genuinely
    running analysis from one that died an hour ago, because both say "Running".
    An operator can cancel it by hand once they work that out; this removes the
    need to work it out.

    Marked Failed rather than Cancelled: cancelled means somebody decided to
    stop it, and attributing that to a person who did nothing would be a lie in
    the audit trail.
    """
    now = now or _now()
    cutoff = now - timedelta(seconds=STALE_ANALYSIS_TIMEOUT_SECONDS)

    stale = (
        ApplicationAnalysis.query.filter(
            ~ApplicationAnalysis.status.in_(TERMINAL_STATUSES)
        )
        .filter(ApplicationAnalysis.created_at < cutoff)
        .all()
    )

    for analysis in stale:
        analysis.status = "Failed"
        analysis.failure_stage = analysis.current_stage or "unknown"
        analysis.failed_at = now
        analysis.completed_at = now
        analysis.safe_error_message = (
            "The analysis stopped reporting and was closed automatically after "
            f"{STALE_ANALYSIS_TIMEOUT_SECONDS // 3600}h. Its worker is gone; "
            "re-run the analysis if you still need it."
        )
        log_audit(
            "application_analysis_timed_out",
            target_type="application_analysis",
            target_id=str(analysis.id),
            details={
                "applicationId": analysis.application_id,
                "stage": analysis.failure_stage,
                "ageSeconds": int((now - _aware(analysis.created_at)).total_seconds()),
            },
            commit=False,
        )

    if stale:
        db.session.commit()
    return len(stale)


def _aware(value: datetime) -> datetime:
    """SQLite returns naive datetimes even for timezone=True columns."""
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
