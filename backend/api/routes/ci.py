"""Native CI API.

Route layer only: validate the request shape, resolve the row, delegate to
``services/ci``, and serialize. Every endpoint is gated by an existing
``@require_permission`` key — CI adds no second authentication path.
"""

from __future__ import annotations

import io

from flask import Blueprint, request, send_file

from ..audit import log_audit
from ..auth_utils import get_current_user
from ..db import db
from ..decorators import require_permission
from ..models_application_intelligence import BitbucketCredentialProfile
from ..models_ci import CiRunner
from ..response import error_response, success_response
from ..secret_encryption import encrypt_secret
from ..services.ci import artifacts as artifacts_service
from ..services.ci import catalog as catalog_service
from ..services.ci import engine as engine_service
from ..services.ci import logs as logs_service
from ..services.ci import pipelines as pipelines_service
from ..services.ci import queue as queue_service
from ..services.ci import scheduler as scheduler_service
from ..services.ci import secrets as secrets_service
from ..services.ci import templates as templates_service
from ..services.ci import workspace as workspace_service
from ..services.ci.serializers import (
    artifact_to_dict,
    build_summary,
    build_to_dict,
    credential_profile_to_dict,
    pipeline_to_dict,
    runner_to_dict,
)
from ..services.ci.source import SourceError

ci_bp = Blueprint("ci", __name__, url_prefix="/api/ci")

# Errors these services raise deliberately, with user-facing messages.
_USER_ERRORS = (
    catalog_service.CatalogError,
    pipelines_service.PipelineError,
    secrets_service.SecretError,
    engine_service.BuildError,
    SourceError,
)


def _actor():
    return get_current_user()


def _int_arg(name: str, default: int) -> int:
    try:
        return int(request.args.get(name, default))
    except (TypeError, ValueError):
        return default


def _payload() -> dict:
    return request.get_json(silent=True) or {}


@ci_bp.errorhandler(LookupError)
def _not_found(exc: LookupError):
    return error_response(str(exc) or "Not found.", 404)


# ---------------------------------------------------------------------------
# Services
# ---------------------------------------------------------------------------

@ci_bp.route("/services", methods=["GET"])
@require_permission("ci_services:view")
def list_services():
    items = catalog_service.list_services(
        search=request.args.get("search", ""),
        status=request.args.get("status", ""),
        application_type=request.args.get("applicationType", ""),
    )
    return success_response(
        {
            "items": items,
            "count": len(items),
            # Health-strip counts over the UNFILTERED catalog would lie when a
            # filter is active; the tiles read the same list the grid shows.
            "summary": catalog_service.catalog_summary(items),
        }
    )


@ci_bp.route("/services", methods=["POST"])
@require_permission("ci_services:create")
def create_service():
    try:
        data = catalog_service.create_service(_payload(), actor=_actor())
    except _USER_ERRORS as exc:
        return error_response(str(exc), 400)
    return success_response(data, status_code=201)


@ci_bp.route("/services/<int:service_id>", methods=["GET"])
@require_permission("ci_services:view")
def get_service(service_id: int):
    row = catalog_service.get_service(service_id)
    return success_response(catalog_service.service_detail(row))


@ci_bp.route("/services/<int:service_id>/summary", methods=["GET"])
@require_permission("ci_services:view")
def get_service_summary(service_id: int):
    row = catalog_service.get_service(service_id)
    return success_response(catalog_service.service_summary(row))


@ci_bp.route("/services/<int:service_id>", methods=["PUT"])
@require_permission("ci_services:edit")
def update_service(service_id: int):
    row = catalog_service.get_service(service_id)
    try:
        data = catalog_service.update_service(row, _payload(), actor=_actor())
    except _USER_ERRORS as exc:
        return error_response(str(exc), 400)
    return success_response(data)


@ci_bp.route("/services/<int:service_id>", methods=["DELETE"])
@require_permission("ci_services:delete")
def delete_service(service_id: int):
    row = catalog_service.get_service(service_id)
    catalog_service.delete_service(row, actor=_actor())
    return success_response({"deleted": True, "id": service_id})


# ---------------------------------------------------------------------------
# Source
# ---------------------------------------------------------------------------

@ci_bp.route("/services/<int:service_id>/source", methods=["PUT"])
@require_permission("ci_services:edit")
def update_source(service_id: int):
    row = catalog_service.get_service(service_id)
    try:
        data = catalog_service.update_source(row, _payload(), actor=_actor())
    except _USER_ERRORS as exc:
        return error_response(str(exc), 400)
    return success_response(data)


@ci_bp.route("/services/<int:service_id>/source/test", methods=["POST"])
@require_permission("ci_services:edit")
def test_source(service_id: int):
    row = catalog_service.get_service(service_id)
    try:
        return success_response(catalog_service.test_source(row))
    except _USER_ERRORS as exc:
        # A failed probe is a valid answer, not a server fault: report it as a
        # result the UI can render inline.
        return success_response({"ok": False, "message": str(exc)})


@ci_bp.route("/services/<int:service_id>/source/branches", methods=["GET"])
@require_permission("ci_services:view")
def list_branches(service_id: int):
    row = catalog_service.get_service(service_id)
    try:
        return success_response(catalog_service.list_branches(row))
    except _USER_ERRORS as exc:
        return error_response(str(exc), 400)


@ci_bp.route("/source/credentials", methods=["GET"])
@require_permission("ci_services:view")
def list_source_credentials():
    items = catalog_service.list_credential_profiles()
    return success_response({"items": items, "count": len(items)})


@ci_bp.route("/source/credentials", methods=["POST"])
@require_permission("ci_secrets:manage")
def create_source_credential():
    payload = _payload()
    name = " ".join(str(payload.get("name") or "").split())[:120]
    secret = str(payload.get("secret") or "")
    credential_type = str(payload.get("credentialType") or "").strip()
    principal = " ".join(str(payload.get("principal") or "").split())[:255]

    if not name:
        return error_response("A credential name is required.")
    if not secret:
        return error_response("A credential secret is required.")
    if credential_type not in {"oauth", "api_token", "repository_access_token"}:
        return error_response(
            "Credential type must be 'oauth', 'api_token', or 'repository_access_token'."
        )
    if credential_type == "api_token" and not principal:
        return error_response("An Atlassian account email is required for an API token.")
    if BitbucketCredentialProfile.query.filter_by(name=name).first():
        return error_response("A credential profile with that name already exists.")

    row = BitbucketCredentialProfile(
        name=name,
        provider=str(payload.get("provider") or "bitbucket").strip().lower(),
        credential_type=credential_type,
        principal=principal or None,
        secret_cipher=encrypt_secret(secret),
        # CI clones and reads only. A write-capable credential is not needed for
        # any Phase 1 stage, so it is not accepted.
        read_only=True,
        enabled=payload.get("enabled") is not False,
        created_by_user_id=getattr(_actor(), "id", None),
    )
    db.session.add(row)
    db.session.commit()
    log_audit(
        "ci_source_credential_created",
        actor=_actor(),
        target_type="bitbucket_credential_profile",
        target_id=str(row.id),
        details={"name": row.name, "credentialType": row.credential_type},
    )
    return success_response(credential_profile_to_dict(row), status_code=201)


@ci_bp.route("/source/credentials/<int:credential_id>", methods=["PUT"])
@require_permission("ci_secrets:manage")
def update_source_credential(credential_id: int):
    row = db.session.get(BitbucketCredentialProfile, credential_id)
    if row is None:
        return error_response("Credential profile not found.", 404)
    payload = _payload()
    if payload.get("name"):
        row.name = " ".join(str(payload["name"]).split())[:120]
    if "principal" in payload:
        row.principal = " ".join(str(payload.get("principal") or "").split())[:255] or None
    if payload.get("secret"):
        row.secret_cipher = encrypt_secret(str(payload["secret"]))
    if "enabled" in payload:
        row.enabled = bool(payload["enabled"])
    db.session.add(row)
    db.session.commit()
    log_audit(
        "ci_source_credential_updated",
        actor=_actor(),
        target_type="bitbucket_credential_profile",
        target_id=str(row.id),
        details={"name": row.name, "secretRotated": bool(payload.get("secret"))},
    )
    return success_response(credential_profile_to_dict(row))


@ci_bp.route("/source/credentials/<int:credential_id>", methods=["DELETE"])
@require_permission("ci_secrets:manage")
def delete_source_credential(credential_id: int):
    row = db.session.get(BitbucketCredentialProfile, credential_id)
    if row is None:
        return error_response("Credential profile not found.", 404)
    # This store is shared with Application Intelligence; deleting a profile in
    # use there would silently break its analyses.
    from ..models_application_intelligence import IntelligenceApplication
    from ..models_ci import CiService

    in_use_ci = CiService.query.filter_by(credential_profile_id=row.id).count()
    in_use_ai = IntelligenceApplication.query.filter_by(
        credential_profile_id=row.id
    ).count()
    if in_use_ci or in_use_ai:
        return error_response(
            f"This credential is used by {in_use_ci} CI service(s) and "
            f"{in_use_ai} analysed application(s). Reassign them first.",
            409,
        )
    name = row.name
    db.session.delete(row)
    db.session.commit()
    log_audit(
        "ci_source_credential_deleted",
        actor=_actor(),
        target_type="bitbucket_credential_profile",
        target_id=str(credential_id),
        details={"name": name},
    )
    return success_response({"deleted": True, "id": credential_id})


# ---------------------------------------------------------------------------
# Pipelines
# ---------------------------------------------------------------------------

@ci_bp.route("/pipeline-templates", methods=["GET"])
@require_permission("ci_pipelines:view")
def list_pipeline_templates():
    items = templates_service.list_templates()
    return success_response({"items": items, "count": len(items)})


@ci_bp.route("/services/<int:service_id>/pipelines", methods=["GET"])
@require_permission("ci_pipelines:view")
def list_pipelines(service_id: int):
    catalog_service.get_service(service_id)
    items = pipelines_service.list_pipelines(service_id)
    return success_response({"items": items, "count": len(items)})


@ci_bp.route("/services/<int:service_id>/pipelines", methods=["POST"])
@require_permission("ci_pipelines:edit")
def create_pipeline(service_id: int):
    service = catalog_service.get_service(service_id)
    try:
        data = pipelines_service.create_pipeline(service, _payload(), actor=_actor())
    except _USER_ERRORS as exc:
        return error_response(str(exc), 400)
    return success_response(data, status_code=201)


@ci_bp.route("/services/<int:service_id>/pipelines/from-template", methods=["POST"])
@require_permission("ci_pipelines:edit")
def create_pipeline_from_template(service_id: int):
    service = catalog_service.get_service(service_id)
    payload = _payload()
    try:
        data = pipelines_service.create_from_template(
            service, payload.get("applicationType"), actor=_actor()
        )
    except _USER_ERRORS as exc:
        return error_response(str(exc), 400)
    return success_response(data, status_code=201)


@ci_bp.route("/pipelines/<int:pipeline_id>", methods=["GET"])
@require_permission("ci_pipelines:view")
def get_pipeline(pipeline_id: int):
    row = pipelines_service.get_pipeline(pipeline_id)
    return success_response(pipeline_to_dict(row))


@ci_bp.route("/pipelines/<int:pipeline_id>", methods=["PUT"])
@require_permission("ci_pipelines:edit")
def update_pipeline(pipeline_id: int):
    row = pipelines_service.get_pipeline(pipeline_id)
    try:
        data = pipelines_service.update_pipeline(row, _payload(), actor=_actor())
    except _USER_ERRORS as exc:
        return error_response(str(exc), 400)
    return success_response(data)


@ci_bp.route("/pipelines/<int:pipeline_id>", methods=["DELETE"])
@require_permission("ci_pipelines:edit")
def delete_pipeline(pipeline_id: int):
    row = pipelines_service.get_pipeline(pipeline_id)
    pipelines_service.delete_pipeline(row, actor=_actor())
    return success_response({"deleted": True, "id": pipeline_id})


# ---------------------------------------------------------------------------
# Builds
# ---------------------------------------------------------------------------

def _merge_automation_info(items: list) -> list:
    """Attach ticket provenance and deploy outcome to serialized builds.

    Ticket-driven builds carry the DeployAutomationRun that asked for them;
    a run that reached 'deployed' closes the commit → build → artifact →
    deploy chain, which the Builds table renders as the Deployed column.
    One batched query, never per-row.
    """
    ids = [item["id"] for item in items if item.get("id")]
    if not ids:
        return items
    from ..models import DeployAutomationRun

    runs = DeployAutomationRun.query.filter(
        DeployAutomationRun.ci_build_id.in_(ids)
    ).all()
    by_build = {}
    for run in runs:
        by_build[run.ci_build_id] = {
            "runId": run.id,
            "ticketNumber": run.ticket_number,
            "runStatus": run.status,
            "deployed": run.status == "deployed",
            "clusterId": run.cluster_id,
            "namespace": run.namespace,
        }
    for item in items:
        item["automation"] = by_build.get(item["id"])
    return items


@ci_bp.route("/builds", methods=["GET"])
@require_permission("ci_builds:view")
def list_all_builds():
    service_id = request.args.get("serviceId")
    rows, total = engine_service.list_builds(
        service_id=int(service_id) if service_id and service_id.isdigit() else None,
        status=request.args.get("status"),
        limit=_int_arg("limit", 50),
        offset=_int_arg("offset", 0),
    )
    return success_response(
        {
            "items": _merge_automation_info([build_summary(row) for row in rows]),
            "total": total,
            "queueDepth": queue_service.depth(),
        }
    )


@ci_bp.route("/services/<int:service_id>/builds", methods=["GET"])
@require_permission("ci_builds:view")
def list_service_builds(service_id: int):
    catalog_service.get_service(service_id)
    rows, total = engine_service.list_builds(
        service_id=service_id,
        status=request.args.get("status"),
        limit=_int_arg("limit", 50),
        offset=_int_arg("offset", 0),
    )
    return success_response(
        {
            "items": _merge_automation_info([build_summary(row) for row in rows]),
            "total": total,
            "queueDepth": queue_service.depth(service_id),
        }
    )


@ci_bp.route("/services/<int:service_id>/builds", methods=["POST"])
@require_permission("ci_builds:run")
def run_build(service_id: int):
    service = catalog_service.get_service(service_id)
    payload = _payload()
    try:
        variables = payload.get("variables")
        data = engine_service.trigger_build(
            service,
            branch=payload.get("branch"),
            commit_sha=payload.get("commitSha"),
            pipeline_id=payload.get("pipelineId"),
            trigger_type="manual",
            actor=_actor(),
            variables=variables if isinstance(variables, dict) else None,
            # "branch" or "tag" — what the ref above names. Unknown values
            # fall back to branch inside the engine.
            ref_type=payload.get("refType"),
        )
    except _USER_ERRORS as exc:
        return error_response(str(exc), 400)
    return success_response(data, status_code=201)


@ci_bp.route("/builds/<int:build_id>", methods=["GET"])
@require_permission("ci_builds:view")
def get_build(build_id: int):
    row = engine_service.get_build(build_id)
    return success_response(_merge_automation_info([build_to_dict(row)])[0])


@ci_bp.route("/builds/<int:build_id>/cancel", methods=["POST"])
@require_permission("ci_builds:cancel")
def cancel_build(build_id: int):
    row = engine_service.get_build(build_id)
    try:
        return success_response(engine_service.cancel_build(row, actor=_actor()))
    except _USER_ERRORS as exc:
        return error_response(str(exc), 409)


@ci_bp.route("/builds/<int:build_id>/retry", methods=["POST"])
@require_permission("ci_builds:retry")
def retry_build(build_id: int):
    row = engine_service.get_build(build_id)
    try:
        return success_response(
            engine_service.retry_build(row, actor=_actor()), status_code=201
        )
    except _USER_ERRORS as exc:
        return error_response(str(exc), 409)


@ci_bp.route("/builds/<int:build_id>/rerun-from/<int:position>", methods=["POST"])
@require_permission("ci_builds:retry")
def rerun_build_from(build_id: int, position: int):
    """Queue a build that starts at ``position``, reusing this build's outputs.

    For retrying one stage after changing its settings without paying for the
    stages before it again. The pipeline is re-read, so the edits apply.
    """
    row = engine_service.get_build(build_id)
    try:
        return success_response(
            engine_service.rerun_from(row, position, actor=_actor()), status_code=201
        )
    except _USER_ERRORS as exc:
        return error_response(str(exc), 409)


@ci_bp.route("/builds/<int:build_id>/workspace", methods=["GET"])
@require_permission("ci_builds:view")
def get_build_workspace(build_id: int):
    """One directory of a running build's shared workspace.

    Live only: the workspace is an emptyDir that dies with the build pod, so a
    finished build reports why there is nothing to show rather than an empty
    listing. Names, sizes and types only — never file content, which would
    expose credentials a stage wrote for its own use.
    """
    build = engine_service.get_build(build_id)
    try:
        return success_response(
            workspace_service.list_directory(build, request.args.get("path") or "")
        )
    except workspace_service.WorkspaceError as exc:
        return error_response(str(exc), 409)


@ci_bp.route("/builds/<int:build_id>/stages/<int:stage_id>/logs", methods=["GET"])
@require_permission("ci_builds:view")
def get_stage_logs(build_id: int, stage_id: int):
    build = engine_service.get_build(build_id)
    stage = engine_service.get_build_stage(build, stage_id)
    payload = logs_service.read(
        stage.id, after_seq=_int_arg("after", 0), limit=_int_arg("limit", 1000)
    )
    payload["stageStatus"] = stage.status
    # The viewer stops polling when the stage is terminal AND it has drained
    # everything — a stage can finish with chunks still unread.
    payload["complete"] = stage.status not in ("pending", "running") and not payload["hasMore"]
    return success_response(payload)


@ci_bp.route("/builds/<int:build_id>/stages/<int:stage_id>/logs/download", methods=["GET"])
@require_permission("ci_builds:view")
def download_stage_logs(build_id: int, stage_id: int):
    build = engine_service.get_build(build_id)
    stage = engine_service.get_build_stage(build, stage_id)
    text = logs_service.download_text(stage.id)
    return send_file(
        io.BytesIO(text.encode("utf-8")),
        mimetype="text/plain",
        as_attachment=True,
        download_name=f"build-{build.number}-{stage.position + 1}-{stage.name}.log",
    )


# ---------------------------------------------------------------------------
# Artifacts
# ---------------------------------------------------------------------------

@ci_bp.route("/services/<int:service_id>/artifacts", methods=["GET"])
@require_permission("ci_artifacts:view")
def list_service_artifacts(service_id: int):
    catalog_service.get_service(service_id)
    rows = artifacts_service.list_for_service(service_id, limit=_int_arg("limit", 100))
    return success_response(
        {"items": [artifact_to_dict(row) for row in rows], "count": len(rows)}
    )


@ci_bp.route("/builds/<int:build_id>/artifacts", methods=["GET"])
@require_permission("ci_artifacts:view")
def list_build_artifacts(build_id: int):
    engine_service.get_build(build_id)
    rows = artifacts_service.list_for_build(build_id)
    return success_response(
        {"items": [artifact_to_dict(row) for row in rows], "count": len(rows)}
    )


@ci_bp.route("/artifacts/<int:artifact_id>", methods=["GET"])
@require_permission("ci_artifacts:view")
def get_artifact(artifact_id: int):
    from ..models_ci import CiArtifact

    row = db.session.get(CiArtifact, artifact_id)
    if row is None:
        return error_response("Artifact not found.", 404)
    return success_response(artifact_to_dict(row))


@ci_bp.route("/artifacts/<int:artifact_id>/download", methods=["GET"])
@require_permission("ci_artifacts:view")
def download_artifact(artifact_id: int):
    from ..models_ci import CiArtifact

    row = db.session.get(CiArtifact, artifact_id)
    if row is None:
        return error_response("Artifact not found.", 404)
    if row.storage_backend != "local" or not row.storage_ref:
        return error_response(
            "This artifact is not stored locally. Pull it from its registry instead.",
            400,
        )
    try:
        stream = artifacts_service.get_store("local").open(row)
    except (OSError, ValueError):
        return error_response("The artifact file is no longer available.", 404)
    log_audit(
        "ci_artifact_downloaded",
        actor=_actor(),
        target_type="ci_artifact",
        target_id=str(row.id),
        details={"name": row.name, "serviceId": row.service_id, "buildId": row.build_id},
    )
    return send_file(
        stream,
        mimetype="application/octet-stream",
        as_attachment=True,
        download_name=artifacts_service.safe_filename(row.name),
    )


# ---------------------------------------------------------------------------
# Secrets
# ---------------------------------------------------------------------------

@ci_bp.route("/secrets", methods=["GET"])
@require_permission("ci_secrets:view")
def list_global_secrets():
    items = secrets_service.list_secrets(None)
    return success_response({"items": items, "count": len(items)})


@ci_bp.route("/secrets", methods=["POST"])
@require_permission("ci_secrets:manage")
def create_global_secret():
    try:
        data = secrets_service.create_secret(_payload(), service_id=None, actor=_actor())
    except _USER_ERRORS as exc:
        return error_response(str(exc), 400)
    return success_response(data, status_code=201)


@ci_bp.route("/services/<int:service_id>/secrets", methods=["GET"])
@require_permission("ci_secrets:view")
def list_service_secrets(service_id: int):
    catalog_service.get_service(service_id)
    items = secrets_service.list_secrets(service_id)
    return success_response({"items": items, "count": len(items)})


@ci_bp.route("/services/<int:service_id>/secrets", methods=["POST"])
@require_permission("ci_secrets:manage")
def create_service_secret(service_id: int):
    catalog_service.get_service(service_id)
    try:
        data = secrets_service.create_secret(
            _payload(), service_id=service_id, actor=_actor()
        )
    except _USER_ERRORS as exc:
        return error_response(str(exc), 400)
    return success_response(data, status_code=201)


@ci_bp.route("/secrets/<int:secret_id>", methods=["PUT"])
@require_permission("ci_secrets:manage")
def update_secret(secret_id: int):
    row = secrets_service.get_secret(secret_id)
    try:
        return success_response(secrets_service.update_secret(row, _payload(), actor=_actor()))
    except _USER_ERRORS as exc:
        return error_response(str(exc), 400)


@ci_bp.route("/secrets/<int:secret_id>", methods=["DELETE"])
@require_permission("ci_secrets:manage")
def delete_secret(secret_id: int):
    row = secrets_service.get_secret(secret_id)
    secrets_service.delete_secret(row, actor=_actor())
    return success_response({"deleted": True, "id": secret_id})


# ---------------------------------------------------------------------------
# Runners
# ---------------------------------------------------------------------------

@ci_bp.route("/runners", methods=["GET"])
@require_permission("ci_runners:view")
def list_runners():
    rows = CiRunner.query.order_by(CiRunner.name.asc()).all()
    return success_response(
        {
            "items": [runner_to_dict(row) for row in rows],
            "count": len(rows),
            "queueDepth": queue_service.depth(),
            "eligible": len(scheduler_service.eligible_runners()),
        }
    )


@ci_bp.route("/runners/<int:runner_id>", methods=["GET"])
@require_permission("ci_runners:view")
def get_runner(runner_id: int):
    row = db.session.get(CiRunner, runner_id)
    if row is None:
        return error_response("Runner not found.", 404)
    return success_response(runner_to_dict(row))


@ci_bp.route("/runners/<int:runner_id>", methods=["PUT"])
@require_permission("ci_runners:manage")
def update_runner(runner_id: int):
    row = db.session.get(CiRunner, runner_id)
    if row is None:
        return error_response("Runner not found.", 404)
    payload = _payload()

    if "enabled" in payload:
        row.enabled = bool(payload["enabled"])
    if "description" in payload:
        row.description = " ".join(str(payload.get("description") or "").split())[:2000] or None
    if "maxConcurrent" in payload:
        try:
            row.max_concurrent = max(1, min(int(payload["maxConcurrent"]), 100))
        except (TypeError, ValueError):
            return error_response("Maximum concurrency must be a whole number.")
    for field, column in (("labels", "labels"), ("capabilities", "capabilities")):
        if field in payload:
            value = payload[field]
            items = value if isinstance(value, (list, tuple)) else str(value or "").split(",")
            cleaned, seen = [], set()
            for item in items[:50]:
                label = " ".join(str(item or "").split()).lower()[:64]
                if label and label not in seen:
                    seen.add(label)
                    cleaned.append(label)
            setattr(row, column, cleaned)
    if "status" in payload and not row.is_builtin:
        status = str(payload["status"]).strip().lower()
        if status not in ("online", "offline", "draining", "disabled"):
            return error_response("Unknown runner status.")
        row.status = status

    db.session.add(row)
    db.session.commit()
    log_audit(
        "ci_runner_updated",
        actor=_actor(),
        target_type="ci_runner",
        target_id=str(row.id),
        details={
            "name": row.name,
            "enabled": row.enabled,
            "maxConcurrent": row.max_concurrent,
            "capabilities": list(row.capabilities or []),
        },
    )
    return success_response(runner_to_dict(row))
