"""The CI Service Catalog: what we build.

A service is registered first and connected to its source second, so a catalog
entry can exist in a visibly incomplete state rather than forcing a user to have
every answer before they can start. ``sourceConfigured`` and
``pipelineConfigured`` are what the cards and the Run Build gate read.

Nothing here imports Hermes, Application Intelligence analyses, or any AI code
path. The optional ``intelligenceApplicationId`` / ``catalogEntryId`` /
``blueprintId`` links are stored and echoed back for navigation and never read
when a build runs.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from sqlalchemy import or_

from ...audit import log_audit
from ...db import db
from ...models import RegistryConnection, ServiceBlueprint
from ...models_application_intelligence import BitbucketCredentialProfile
from ...models_ci import (
    APPLICATION_TYPES,
    CRITICALITIES,
    SERVICE_STATUSES,
    CiArtifact,
    CiBuild,
    CiService,
)
from . import artifacts as artifacts_service
from . import source as source_port
from .serializers import service_to_dict


class CatalogError(ValueError):
    """A service payload was rejected. Message is user-facing."""


def _clean(value: Any, limit: int) -> str:
    return " ".join(str(value or "").split())[:limit]


def _slug(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", str(value or "").lower()).strip("-")[:180]


def _unique_slug(desired: str, exclude_id: Optional[int] = None) -> str:
    """A slug nobody else holds. Slugs are used in Kubernetes object names, so
    they must be stable and DNS-safe rather than merely unique."""
    base = _slug(desired) or "service"
    candidate = base
    suffix = 2
    while True:
        clash = CiService.query.filter_by(slug=candidate)
        if exclude_id:
            clash = clash.filter(CiService.id != exclude_id)
        if clash.first() is None:
            return candidate
        candidate = f"{base[:172]}-{suffix}"
        suffix += 1


def _credential_or_error(credential_id: Optional[int]) -> Optional[BitbucketCredentialProfile]:
    if credential_id in (None, "", 0):
        return None
    row = db.session.get(BitbucketCredentialProfile, int(credential_id))
    if row is None or not row.enabled:
        raise CatalogError("Select an enabled source credential profile.")
    return row


def _optional_fk(model, value: Any, label: str) -> Optional[int]:
    if value in (None, "", 0):
        return None
    row = db.session.get(model, int(value))
    if row is None:
        raise CatalogError(f"{label} not found.")
    return row.id


# ---------------------------------------------------------------------------
# Reads
# ---------------------------------------------------------------------------

def _latest_build(service_id: int) -> Optional[CiBuild]:
    return (
        CiBuild.query.filter_by(service_id=service_id)
        .order_by(CiBuild.number.desc())
        .first()
    )


def _recent_builds(service_id: int, limit: int = 10) -> List[CiBuild]:
    """Newest-first recent builds — the first one is the card's verdict, the
    statuses of all of them are its sparkline."""
    return (
        CiBuild.query.filter_by(service_id=service_id)
        .order_by(CiBuild.number.desc())
        .limit(limit)
        .all()
    )


def list_services(
    *, search: str = "", status: str = "", application_type: str = ""
) -> List[Dict[str, Any]]:
    query = CiService.query
    if status and status != "all":
        query = query.filter(CiService.status == status)
    if application_type and application_type != "all":
        query = query.filter(CiService.application_type == application_type)
    term = _clean(search, 120)
    if term:
        like = f"%{term.lower()}%"
        query = query.filter(
            or_(
                db.func.lower(CiService.name).like(like),
                db.func.lower(CiService.slug).like(like),
                db.func.lower(db.func.coalesce(CiService.owner_team, "")).like(like),
                db.func.lower(db.func.coalesce(CiService.repository_name, "")).like(like),
            )
        )
    rows = query.order_by(CiService.name.asc()).all()
    items = []
    for row in rows:
        recent = _recent_builds(row.id)
        items.append(
            service_to_dict(
                row,
                latest_build=recent[0] if recent else None,
                latest_artifact=artifacts_service.latest_for_service(row.id),
                recent_statuses=[b.status for b in recent],
            )
        )
    return items


def catalog_summary(items: List[Dict[str, Any]]) -> Dict[str, int]:
    """The health-strip counts, derived from an already-serialized item list.

    'Failing' means the LATEST build failed — an old red behind a green is
    history, not a problem. 'Needs setup' is any service that cannot build yet.
    """
    latest = [item.get("latestBuild") for item in items]
    return {
        "total": len(items),
        "building": sum(1 for b in latest if b and b.get("status") == "running"),
        "queued": sum(1 for b in latest if b and b.get("status") == "queued"),
        "failing": sum(1 for b in latest if b and b.get("status") in ("failed", "timeout")),
        "needsSetup": sum(
            1
            for item in items
            if not (item.get("sourceConfigured") and item.get("pipelineConfigured"))
        ),
    }


def get_service(service_id: int) -> CiService:
    row = db.session.get(CiService, int(service_id))
    if row is None:
        raise LookupError("Service not found.")
    return row


def service_detail(row: CiService) -> Dict[str, Any]:
    return service_to_dict(
        row,
        latest_build=_latest_build(row.id),
        latest_artifact=artifacts_service.latest_for_service(row.id),
        include_counts=True,
    )


def service_summary(row: CiService) -> Dict[str, Any]:
    """Overview-tab payload: identity, readiness, and recent activity."""
    from .serializers import artifact_to_dict, build_summary

    recent_builds = (
        CiBuild.query.filter_by(service_id=row.id)
        .order_by(CiBuild.number.desc())
        .limit(5)
        .all()
    )
    recent_artifacts = (
        CiArtifact.query.filter_by(service_id=row.id)
        .order_by(CiArtifact.created_at.desc(), CiArtifact.id.desc())
        .limit(5)
        .all()
    )
    succeeded = CiBuild.query.filter_by(service_id=row.id, status="success").count()
    failed = CiBuild.query.filter_by(service_id=row.id, status="failed").count()
    total = CiBuild.query.filter_by(service_id=row.id).count()
    # The Overview pipeline strip shows the LAST BUILD's truth, not the
    # definition: which stages passed, which one failed, which were skipped.
    latest_stages = []
    if recent_builds:
        latest_stages = [
            {"id": stage.id, "name": stage.name, "status": stage.status}
            for stage in sorted(recent_builds[0].stages, key=lambda s: s.position)
        ]
    return {
        "service": service_detail(row),
        "readiness": readiness(row),
        "latestBuildId": recent_builds[0].id if recent_builds else None,
        "latestBuildStages": latest_stages,
        "recentBuilds": [build_summary(build) for build in recent_builds],
        "recentArtifacts": [artifact_to_dict(item) for item in recent_artifacts],
        "stats": {
            "totalBuilds": total,
            "succeeded": succeeded,
            "failed": failed,
            # Percentage over builds that reached a pass/fail verdict — queued,
            # cancelled and timed-out builds are not evidence either way.
            "successRate": (
                round(succeeded * 100 / (succeeded + failed))
                if (succeeded + failed)
                else None
            ),
        },
    }


def readiness(row: CiService) -> Dict[str, Any]:
    """What still has to be true before this service can build."""
    checks = [
        {
            "key": "source",
            "label": "Source connected",
            "ok": row.source_ready(),
            "hint": "Connect a repository and credential on the Source tab.",
        },
        {
            "key": "pipeline",
            "label": "Pipeline configured",
            "ok": bool(row.default_pipeline() and row.default_pipeline().stages),
            "hint": "Add at least one stage on the Pipeline tab.",
        },
        {
            "key": "active",
            "label": "Service active",
            "ok": row.status == "active",
            "hint": "Set the service back to active in Settings.",
        },
    ]
    return {"ready": all(check["ok"] for check in checks), "checks": checks}


def can_run_build(row: CiService) -> Optional[str]:
    """None when a build may start, otherwise the reason it may not."""
    if row.status != "active":
        return f"This service is {row.status}. Set it to active before running builds."
    if not row.source_ready():
        return "Connect a repository and credential before running a build."
    pipeline = row.default_pipeline()
    if pipeline is None or not pipeline.stages:
        return "Configure a pipeline with at least one stage before running a build."
    return None


# ---------------------------------------------------------------------------
# Writes
# ---------------------------------------------------------------------------

def _apply_identity(row: CiService, payload: Dict[str, Any], *, creating: bool) -> None:
    name = _clean(payload.get("name", row.name), 160)
    if not name:
        raise CatalogError("A service name is required.")
    row.name = name
    if creating or payload.get("slug"):
        row.slug = _unique_slug(payload.get("slug") or name, exclude_id=row.id)

    if "description" in payload:
        row.description = _clean(payload.get("description"), 2000) or None
    if "ownerTeam" in payload:
        row.owner_team = _clean(payload.get("ownerTeam"), 255) or None

    if "criticality" in payload:
        criticality = _clean(payload.get("criticality"), 32).lower() or None
        if criticality and criticality not in CRITICALITIES:
            raise CatalogError(f"Criticality must be one of: {', '.join(CRITICALITIES)}.")
        row.criticality = criticality

    if creating or "applicationType" in payload:
        app_type = _clean(payload.get("applicationType"), 32).lower() or "generic"
        if app_type not in APPLICATION_TYPES:
            raise CatalogError(
                f"Application type must be one of: {', '.join(APPLICATION_TYPES)}."
            )
        row.application_type = app_type

    if "status" in payload:
        status = _clean(payload.get("status"), 16).lower() or "active"
        if status not in SERVICE_STATUSES:
            raise CatalogError(f"Status must be one of: {', '.join(SERVICE_STATUSES)}.")
        row.status = status

    if "maxConcurrentBuilds" in payload:
        try:
            limit = int(payload.get("maxConcurrentBuilds") or 1)
        except (TypeError, ValueError):
            raise CatalogError("Maximum concurrent builds must be a whole number.")
        row.max_concurrent_builds = max(1, min(limit, 20))

    if "registryConnectionId" in payload:
        row.registry_connection_id = _optional_fk(
            RegistryConnection, payload.get("registryConnectionId"), "Registry connection"
        )
    if "blueprintId" in payload:
        row.blueprint_id = _optional_fk(
            ServiceBlueprint, payload.get("blueprintId"), "Service blueprint"
        )
    if "intelligenceApplicationId" in payload:
        from ...models_application_intelligence import IntelligenceApplication

        row.intelligence_application_id = _optional_fk(
            IntelligenceApplication,
            payload.get("intelligenceApplicationId"),
            "Application Intelligence application",
        )
    if "catalogEntryId" in payload:
        from ...models import AppCatalogEntry

        row.catalog_entry_id = _optional_fk(
            AppCatalogEntry, payload.get("catalogEntryId"), "Inventory catalog entry"
        )


def apply_source(row: CiService, payload: Dict[str, Any]) -> None:
    """Set (or clear) the repository configuration."""
    provider = _clean(payload.get("repositoryProvider", row.repository_provider), 32).lower()
    provider = provider or "bitbucket"
    if provider not in source_port.supported_providers():
        raise CatalogError(
            f"Source provider '{provider}' is not supported yet. "
            f"Available: {', '.join(source_port.supported_providers())}."
        )
    handler = source_port.get_provider(provider)

    url = _clean(payload.get("repositoryUrl", row.repository_url), 1024)
    if not url:
        raise CatalogError("A repository URL is required.")
    try:
        ref = handler.parse_repository_url(url)
    except ValueError as exc:
        raise CatalogError(str(exc)) from exc

    credential = _credential_or_error(
        payload.get("credentialProfileId", row.credential_profile_id)
    )
    if credential is None:
        raise CatalogError("Select a source credential profile.")

    try:
        working_directory = handler.checkout_spec(
            ref, credential, "HEAD", payload.get("workingDirectory", row.working_directory)
        ).working_directory
    except ValueError as exc:
        raise CatalogError(str(exc)) from exc

    row.repository_provider = provider
    row.repository_url = ref.url
    row.repository_workspace = ref.workspace
    row.repository_name = ref.name
    row.default_branch = _clean(payload.get("defaultBranch", row.default_branch), 255) or "main"
    row.working_directory = working_directory
    row.credential_profile_id = credential.id


def create_service(payload: Dict[str, Any], *, actor=None) -> Dict[str, Any]:
    row = CiService(name="", slug="", created_by_user_id=getattr(actor, "id", None))
    _apply_identity(row, payload, creating=True)
    # Source is optional at creation: registering the service and connecting the
    # repository are two separate steps in the UI.
    if payload.get("repositoryUrl"):
        apply_source(row, payload)
    db.session.add(row)
    db.session.commit()

    # A new service gets its application type's starter pipeline so it is one
    # click from runnable instead of landing on an empty editor.
    if payload.get("createDefaultPipeline") is not False:
        from . import pipelines as pipelines_service

        pipelines_service.create_from_template(row, row.application_type, actor=actor)

    log_audit(
        "ci_service_created",
        actor=actor,
        target_type="ci_service",
        target_id=str(row.id),
        details={
            "name": row.name,
            "slug": row.slug,
            "applicationType": row.application_type,
            "sourceConfigured": row.source_ready(),
        },
    )
    db.session.refresh(row)
    return service_detail(row)


def update_service(row: CiService, payload: Dict[str, Any], *, actor=None) -> Dict[str, Any]:
    _apply_identity(row, payload, creating=False)
    row.updated_at = datetime.now(timezone.utc)
    db.session.add(row)
    db.session.commit()
    log_audit(
        "ci_service_updated",
        actor=actor,
        target_type="ci_service",
        target_id=str(row.id),
        details={"name": row.name, "slug": row.slug, "status": row.status},
    )
    return service_detail(row)


def update_source(row: CiService, payload: Dict[str, Any], *, actor=None) -> Dict[str, Any]:
    apply_source(row, payload)
    row.updated_at = datetime.now(timezone.utc)
    db.session.add(row)
    db.session.commit()
    log_audit(
        "ci_source_configured",
        actor=actor,
        target_type="ci_service",
        target_id=str(row.id),
        details={
            "service": row.slug,
            "provider": row.repository_provider,
            "repository": f"{row.repository_workspace}/{row.repository_name}",
            "defaultBranch": row.default_branch,
            "credentialProfileId": row.credential_profile_id,
        },
    )
    return service_detail(row)


def delete_service(row: CiService, *, actor=None) -> None:
    """Delete a service and everything it owns.

    Builds, stages, logs, artifact records and secrets cascade. Artifact *files*
    are left in the store — removing bytes a deployment may still reference is
    not something a catalog delete should do silently.
    """
    identity = {"name": row.name, "slug": row.slug, "id": row.id}
    db.session.delete(row)
    db.session.commit()
    log_audit(
        "ci_service_deleted",
        actor=actor,
        target_type="ci_service",
        target_id=str(identity["id"]),
        details=identity,
    )


# ---------------------------------------------------------------------------
# Source operations
# ---------------------------------------------------------------------------

def _repository_ref(row: CiService):
    if not row.source_ready():
        raise CatalogError("Connect a repository before using this action.")
    handler = source_port.get_provider(row.repository_provider)
    return handler, handler.parse_repository_url(row.repository_url)


def test_source(row: CiService) -> Dict[str, Any]:
    handler, ref = _repository_ref(row)
    return handler.verify_access(ref, row.credential_profile)


def list_branches(row: CiService) -> Dict[str, Any]:
    handler, ref = _repository_ref(row)
    revisions = handler.list_revisions(ref, row.credential_profile)
    return {
        "items": [
            {
                "value": item.value,
                "label": item.label,
                "type": item.kind,
                "commit": item.commit,
            }
            for item in revisions
        ],
        "count": len(revisions),
        "defaultBranch": row.default_branch,
    }


def list_credential_profiles() -> List[Dict[str, Any]]:
    """Enabled source credentials, without their secrets."""
    from .serializers import credential_profile_to_dict

    rows = (
        BitbucketCredentialProfile.query.filter_by(enabled=True)
        .order_by(BitbucketCredentialProfile.name.asc())
        .all()
    )
    return [credential_profile_to_dict(row) for row in rows]
