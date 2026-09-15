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
    CiSecret,
    CiService,
)
from . import artifacts as artifacts_service
from . import source as source_port
from . import default_pipelines, templates as templates_service
from .serializers import service_to_dict


class CatalogError(ValueError):
    """A service payload was rejected. Message is user-facing."""


# An inline Dockerfile travels to the build pod inside the per-build Secret,
# which Kubernetes caps at 1MiB for all keys together. This leaves ample room.
MAX_DOCKERFILE_CHARS = 64_000


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
        "expectedSecrets": expected_secrets(row),
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


def expected_secrets(row: CiService) -> List[Dict[str, Any]]:
    """The secret keys this application type needs, and whether each is set.

    Advisory, and deliberately NOT one of :func:`readiness`'s checks: a build
    can be entirely correct without them (the stage that would use one may not
    be in this pipeline), and a failing readiness check disables Run Build with
    a reason the backend would not actually enforce.

    A service secret shadows a global of the same key, which is how a service
    overrides a shared default — so the scope reported is the one that wins.
    """
    expected = templates_service.expected_secrets_for(row.application_type)
    if not expected:
        return []
    rows = CiSecret.query.filter(
        or_(CiSecret.service_id == row.id, CiSecret.scope == "global")
    ).all()
    scope_by_key: Dict[str, str] = {}
    for secret in rows:
        if secret.scope == "service" or secret.key not in scope_by_key:
            scope_by_key[secret.key] = secret.scope
    return [
        {
            **item,
            "set": item["key"] in scope_by_key,
            "scope": scope_by_key.get(item["key"], ""),
        }
        for item in expected
    ]


def readiness(row: CiService) -> Dict[str, Any]:
    """What still has to be true before this service can build."""
    pipeline = row.default_pipeline()
    saved_stages = list(pipeline.stages) if pipeline else []
    generated_available = not saved_stages and default_pipelines.is_available(
        row.application_type
    )
    custom_has_command = any(
        stage.enabled and stage.stage_type == "command" and list(stage.commands or [])
        for stage in saved_stages
    )
    pipeline_ok = bool(saved_stages or generated_available)
    if row.application_type == "generic":
        pipeline_ok = custom_has_command
    checks = [
        {
            "key": "source",
            "label": "Source connected",
            "ok": row.source_ready(),
            "hint": "Connect a repository and credential on the Source tab.",
        },
        {
            "key": "pipeline",
            "label": (
                "Using KubeSight default pipeline"
                if generated_available
                else "Pipeline configured"
            ),
            "ok": pipeline_ok,
            "hint": (
                "Custom services need at least one command in Customize Pipeline."
                if row.application_type == "generic"
                else "Add at least one stage on the Pipeline tab."
            ),
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
    stages = list(pipeline.stages) if pipeline else []
    if row.application_type == "generic" and not any(
        stage.enabled and stage.stage_type == "command" and list(stage.commands or [])
        for stage in stages
    ):
        return (
            "Custom services need at least one command stage. "
            "Choose Customize Pipeline and provide the command."
        )
    if not stages and not default_pipelines.is_available(row.application_type):
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
        # Editable after registration since assisted configuration landed: an
        # analysis that establishes this is Gradle rather than Maven has to be
        # able to say so, and a user correcting it has to be able to as well.
        # Changing it only changes what the STARTER kit and the unsaved fallback
        # would produce — a pipeline already saved is never rewritten by it.
        app_type = _clean(payload.get("applicationType"), 32).lower() or "generic"
        if app_type not in APPLICATION_TYPES:
            raise CatalogError(
                f"Application type must be one of: {', '.join(APPLICATION_TYPES)}."
            )
        row.application_type = app_type

    if "dockerfile" in payload:
        # A document, not a field: line endings and indentation are content, so
        # only trailing whitespace on the whole thing is removed. Empty means
        # "use the Dockerfile in the repository", which is the original
        # behaviour and must stay reachable by clearing the box.
        text = str(payload.get("dockerfile") or "").replace("\r\n", "\n").rstrip()
        if len(text) > MAX_DOCKERFILE_CHARS:
            raise CatalogError(
                f"The Dockerfile may not exceed {MAX_DOCKERFILE_CHARS:,} characters."
            )
        row.dockerfile = text or None

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
    # The application type's starter Dockerfile, so a new service is one click
    # from a runnable image build instead of an empty editor. Only types whose
    # pipeline actually builds an image define one, and `container` defines
    # none on purpose — that type exists to build the repository's own
    # Dockerfile, and an inline recipe would silently take its place. A caller
    # that passed its own `dockerfile` keeps it; `createDefaultDockerfile:
    # false` opts out entirely, mirroring `createDefaultPipeline`.
    if row.dockerfile is None and payload.get("createDefaultDockerfile") is not False:
        row.dockerfile = templates_service.dockerfile_for(row.application_type) or None
    # Source is optional at creation: registering the service and connecting the
    # repository are two separate steps in the UI.
    if payload.get("repositoryUrl"):
        apply_source(row, payload)
    db.session.add(row)
    db.session.commit()

    # Keep a stable editable pipeline identity, but leave its stages empty.
    # Until a user explicitly customizes it, resolution supplies an unsaved,
    # repository-aware KubeSight default at build time.
    if payload.get("createDefaultPipeline") is not False:
        from . import pipelines as pipelines_service

        pipelines_service.create_pipeline(
            row,
            {
                "name": "default",
                "description": "Uses the KubeSight default until customized.",
                "isDefault": True,
                "enabled": True,
                "parameters": [],
                "stages": [],
            },
            actor=actor,
        )

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


def update_application_profile(
    row: CiService, resolved_profile: Dict[str, Any], *, actor=None
) -> Dict[str, Any]:
    """Record what this application is, and re-derive the type from it.

    The profile is validated by its own module before it arrives here — this
    only stores it and keeps ``application_type`` consistent with it, because
    every existing reader (templates, fallback pipelines, icons, readiness)
    still asks that field and must not start disagreeing with the detail.
    """
    if not isinstance(resolved_profile, dict) or not resolved_profile:
        raise CatalogError("An application profile is required.")

    row.application_profile = resolved_profile
    row.profile_source = resolved_profile.get("source") or "manual"
    derived = _clean(resolved_profile.get("derivedApplicationType"), 32).lower()
    if derived and derived in APPLICATION_TYPES:
        row.application_type = derived
    if not row.analysis_state:
        row.analysis_state = "analyzed"
    row.updated_at = datetime.now(timezone.utc)
    db.session.add(row)
    db.session.commit()
    log_audit(
        "ci_application_profile_updated",
        actor=actor,
        target_type="ci_service",
        target_id=str(row.id),
        details={
            "service": row.slug,
            "source": row.profile_source,
            "applicationType": row.application_type,
            "overridden": sorted((resolved_profile.get("overrides") or {}).keys()),
        },
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


def read_source_file(row: CiService, path: str, revision: str = "") -> Dict[str, Any]:
    """Read one file out of the service's repository at a revision.

    The service's own working directory is applied first, so a monorepo service
    asking for ``Jenkinsfile`` gets the one next to its own code rather than the
    one at the root of the repository.
    """
    handler, ref = _repository_ref(row)
    clean = str(path or "").strip().replace("\\", "/").lstrip("/")
    if not clean:
        raise CatalogError("Name the file to read.")
    if row.working_directory and not clean.startswith(f"{row.working_directory}/"):
        clean = f"{row.working_directory.strip('/')}/{clean}"
    chosen = (revision or "").strip() or row.default_branch or "main"
    content = handler.read_file(ref, row.credential_profile, chosen, clean)
    return {"path": clean, "revision": chosen, "content": content}


REVISION_KINDS = ("branch", "tag", "commit")


def preview_revisions(payload: Dict[str, Any]) -> Dict[str, Any]:
    """Revisions for a repository that has no service row yet.

    The registration wizard needs this: it asks for a starting revision before
    the service exists, so it cannot use the per-service listing. Same provider,
    same credential store, same validation — only the lookup differs.

    ``kinds`` is how the caller says what it will actually show. A branch picker
    that also pulled five pages of tags would spend several seconds of somebody's
    time on a list that control never opens.
    """
    provider = _clean(payload.get("repositoryProvider"), 32).lower() or "bitbucket"
    if provider not in source_port.supported_providers():
        raise CatalogError(f"Source provider '{provider}' is not supported yet.")
    handler = source_port.get_provider(provider)

    url = _clean(payload.get("repositoryUrl"), 1024)
    if not url:
        raise CatalogError("A repository URL is required.")
    try:
        ref = handler.parse_repository_url(url)
    except ValueError as exc:
        raise CatalogError(str(exc)) from exc

    credential = _credential_or_error(payload.get("credentialProfileId"))
    if credential is None:
        raise CatalogError("Select a source credential profile.")

    requested = payload.get("kinds")
    if isinstance(requested, str):
        requested = [requested]
    kinds = tuple(
        kind
        for kind in (requested or ["branch"])
        if str(kind).strip().lower() in REVISION_KINDS
    )
    if not kinds:
        raise CatalogError(
            f"Revision kinds must be any of: {', '.join(REVISION_KINDS)}."
        )

    revisions = handler.list_revisions(ref, credential, kinds=kinds)
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
        "kinds": list(kinds),
        "repository": ref.full_name,
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
