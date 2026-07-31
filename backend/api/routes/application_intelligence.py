"""Application Intelligence HTTP API."""

from __future__ import annotations

from io import BytesIO

from flask import Blueprint, request, send_file

from ..audit import log_audit
from ..auth_utils import get_current_user
from ..db import db
from ..decorators import require_permission
from ..response import error_response, success_response
from ..services import application_intelligence_service as service
from ..services.application_intelligence_bitbucket import BitbucketMetadataError
from ..services.application_intelligence_hermes import HermesError
from ..services import application_runtime_intelligence as runtime_service

application_intelligence_bp = Blueprint("application_intelligence", __name__)


def _pagination():
    try:
        page = int(request.args.get("page", 1))
        per_page = int(request.args.get("perPage", 25))
    except (TypeError, ValueError):
        raise ValueError("Pagination parameters must be integers.")
    return page, per_page


def _not_found(exc):
    return error_response(str(exc), 404)


@application_intelligence_bp.route(
    "/api/application-intelligence/hermes/test", methods=["POST"]
)
@require_permission("applications:analyze")
def test_hermes_connection():
    try:
        return success_response(service.test_hermes_connection(get_current_user()))
    except HermesError as exc:
        return error_response(str(exc), 503)


@application_intelligence_bp.route(
    "/api/application-intelligence/bitbucket/revisions", methods=["POST"]
)
@require_permission("applications:manage")
def list_bitbucket_revisions():
    try:
        data = service.list_repository_revisions(request.get_json(silent=True) or {})
    except ValueError as exc:
        return error_response(str(exc), 400)
    except BitbucketMetadataError as exc:
        return error_response(str(exc), 502)
    return success_response(data)


@application_intelligence_bp.route(
    "/api/application-intelligence/bitbucket/dockerfiles", methods=["POST"]
)
@require_permission("applications:manage")
def list_bitbucket_dockerfiles():
    try:
        data = service.list_repository_dockerfiles(
            request.get_json(silent=True) or {}
        )
    except ValueError as exc:
        return error_response(str(exc), 400)
    except BitbucketMetadataError as exc:
        return error_response(str(exc), 502)
    return success_response(data)


@application_intelligence_bp.route("/api/bitbucket-credential-profiles", methods=["GET"])
@require_permission("applications:manage")
def list_bitbucket_credentials():
    return success_response(service.list_credentials())


@application_intelligence_bp.route("/api/bitbucket-credential-profiles", methods=["POST"])
@require_permission("applications:manage")
def create_bitbucket_credential():
    try:
        data = service.create_credential(request.get_json(silent=True) or {}, get_current_user())
    except ValueError as exc:
        return error_response(str(exc), 400)
    return success_response(data, status_code=201)


@application_intelligence_bp.route(
    "/api/bitbucket-credential-profiles/<int:credential_id>", methods=["PATCH"]
)
@require_permission("applications:manage")
def update_bitbucket_credential(credential_id: int):
    try:
        data = service.update_credential(
            credential_id,
            request.get_json(silent=True) or {},
            get_current_user(),
        )
    except LookupError as exc:
        return _not_found(exc)
    except ValueError as exc:
        return error_response(str(exc), 400)
    return success_response(data)


@application_intelligence_bp.route(
    "/api/bitbucket-credential-profiles/<int:credential_id>", methods=["DELETE"]
)
@require_permission("applications:manage")
def delete_bitbucket_credential(credential_id: int):
    try:
        data = service.delete_credential(credential_id, get_current_user())
    except LookupError as exc:
        return _not_found(exc)
    except ValueError as exc:
        return error_response(str(exc), 409)
    return success_response(data)


@application_intelligence_bp.route("/api/applications", methods=["GET"])
@require_permission("applications:view")
def list_applications():
    try:
        return success_response(service.list_applications(*_pagination()))
    except ValueError as exc:
        return error_response(str(exc), 400)


@application_intelligence_bp.route("/api/applications", methods=["POST"])
@require_permission("applications:manage")
def create_application():
    try:
        data = service.create_application(
            request.get_json(silent=True) or {}, get_current_user()
        )
    except ValueError as exc:
        return error_response(str(exc), 400)
    return success_response(data, status_code=201)


@application_intelligence_bp.route("/api/applications/<int:application_id>", methods=["GET"])
@require_permission("applications:view")
def get_application(application_id: int):
    try:
        data = service.application_to_dict(
            service.get_application(application_id), include_history=True
        )
    except LookupError as exc:
        return _not_found(exc)
    return success_response(data)


@application_intelligence_bp.route("/api/applications/<int:application_id>", methods=["PATCH"])
@require_permission("applications:manage")
def update_application(application_id: int):
    try:
        data = service.update_application(
            application_id, request.get_json(silent=True) or {}, get_current_user()
        )
    except LookupError as exc:
        return _not_found(exc)
    except ValueError as exc:
        return error_response(str(exc), 400)
    return success_response(data)


@application_intelligence_bp.route("/api/applications/<int:application_id>", methods=["DELETE"])
@require_permission("applications:manage")
def delete_application(application_id: int):
    try:
        service.delete_application(application_id, get_current_user())
    except LookupError as exc:
        return _not_found(exc)
    except ValueError as exc:
        return error_response(str(exc), 409)
    return success_response({"deleted": True})


@application_intelligence_bp.route(
    "/api/applications/<int:application_id>/analyses", methods=["POST"]
)
@require_permission("applications:analyze")
def request_analysis(application_id: int):
    try:
        data = service.request_analysis(
            application_id, request.get_json(silent=True) or {}, get_current_user()
        )
    except LookupError as exc:
        return _not_found(exc)
    except ValueError as exc:
        return error_response(str(exc), 400)
    return success_response(data, status_code=202)


@application_intelligence_bp.route(
    "/api/applications/<int:application_id>/analyses", methods=["GET"]
)
@require_permission("applications:view")
def list_analyses(application_id: int):
    try:
        data = service.list_analyses(application_id, *_pagination())
    except LookupError as exc:
        return _not_found(exc)
    except ValueError as exc:
        return error_response(str(exc), 400)
    return success_response(data)


@application_intelligence_bp.route(
    "/api/application-analyses/<int:analysis_id>", methods=["GET"]
)
@require_permission("applications:view")
def get_analysis(analysis_id: int):
    try:
        return success_response(service.analysis_to_dict(service.get_analysis(analysis_id)))
    except LookupError as exc:
        return _not_found(exc)


@application_intelligence_bp.route(
    "/api/application-analyses/<int:analysis_id>/compare", methods=["GET"]
)
@require_permission("applications:view")
def compare_analyses(analysis_id: int):
    try:
        baseline_id = int(request.args.get("baselineAnalysisId", ""))
        return success_response(service.compare_analyses(analysis_id, baseline_id))
    except LookupError as exc:
        return _not_found(exc)
    except (TypeError, ValueError) as exc:
        return error_response(str(exc) or "A baseline analysis is required.", 400)


@application_intelligence_bp.route(
    "/api/application-analyses/<int:analysis_id>/cancel", methods=["POST"]
)
@require_permission("applications:analyze")
def cancel_analysis(analysis_id: int):
    try:
        return success_response(service.cancel_analysis(analysis_id, get_current_user()))
    except LookupError as exc:
        return _not_found(exc)
    except ValueError as exc:
        return error_response(str(exc), 409)


@application_intelligence_bp.route(
    "/api/application-analyses/<int:analysis_id>/findings", methods=["GET"]
)
@require_permission("applications:view")
def list_findings(analysis_id: int):
    try:
        page, per_page = _pagination()
        data = service.list_findings(
            analysis_id,
            {
                key: request.args.get(key)
                for key in ("severity", "category", "confidence", "status", "scanner", "file")
            },
            page,
            per_page,
        )
    except LookupError as exc:
        return _not_found(exc)
    except ValueError as exc:
        return error_response(str(exc), 400)
    return success_response(data)


@application_intelligence_bp.route(
    "/api/application-findings/<int:finding_id>", methods=["PATCH"]
)
@require_permission("applications:manage")
def update_finding_status(finding_id: int):
    try:
        return success_response(
            service.update_finding_status(
                finding_id,
                request.get_json(silent=True) or {},
                get_current_user(),
            )
        )
    except LookupError as exc:
        return _not_found(exc)
    except ValueError as exc:
        return error_response(str(exc), 400)


@application_intelligence_bp.route(
    "/api/application-findings/<int:finding_id>/patch", methods=["GET"]
)
@require_permission("applications:view")
def download_finding_patch(finding_id: int):
    try:
        finding = service.get_finding(finding_id)
    except LookupError as exc:
        return _not_found(exc)
    if not finding.suggested_patch:
        return error_response("This finding has no suggested patch.", 404)
    user = get_current_user()
    log_audit(
        "application.finding.patch.downloaded",
        actor=user,
        target_type="application_finding",
        target_id=str(finding.id),
        details={
            "analysis_id": finding.analysis_id,
            "requested_by_user_id": user.id if user else None,
            "executed_by": service.EXECUTOR,
        },
    )
    return send_file(
        BytesIO(finding.suggested_patch.encode("utf-8")),
        mimetype="text/x-diff",
        as_attachment=True,
        download_name=f"finding-{finding.id}.patch",
    )


@application_intelligence_bp.route(
    "/api/application-analyses/<int:analysis_id>/topology", methods=["GET"]
)
@require_permission("applications:view")
def get_topology(analysis_id: int):
    try:
        return success_response(service.topology(analysis_id))
    except LookupError as exc:
        return _not_found(exc)


@application_intelligence_bp.route(
    "/api/application-analyses/<int:analysis_id>/apis", methods=["GET"]
)
@require_permission("applications:view")
def get_apis(analysis_id: int):
    try:
        return success_response({"items": service.result_section(analysis_id, "api_inventory")})
    except LookupError as exc:
        return _not_found(exc)


@application_intelligence_bp.route(
    "/api/application-analyses/<int:analysis_id>/configuration", methods=["GET"]
)
@require_permission("applications:view")
def get_configuration(analysis_id: int):
    try:
        return success_response(
            {
                "items": service.result_section(analysis_id, "configuration_inventory"),
                "secretRequirements": service.result_section(
                    analysis_id, "secret_requirements"
                ),
            }
        )
    except LookupError as exc:
        return _not_found(exc)


@application_intelligence_bp.route(
    "/api/application-analyses/<int:analysis_id>/runtime", methods=["GET"]
)
@require_permission("applications:view")
def get_runtime_snapshot(analysis_id: int):
    try:
        return success_response(
            runtime_service.latest_snapshot(analysis_id, get_current_user())
        )
    except LookupError as exc:
        return _not_found(exc)
    except PermissionError as exc:
        return error_response(str(exc), 403)


@application_intelligence_bp.route(
    "/api/application-analyses/<int:analysis_id>/runtime/collect", methods=["POST"]
)
@require_permission("applications:analyze")
def collect_runtime_snapshot(analysis_id: int):
    try:
        return success_response(
            runtime_service.collect_snapshot(analysis_id, get_current_user()),
            status_code=201,
        )
    except LookupError as exc:
        return _not_found(exc)
    except ValueError as exc:
        return error_response(str(exc), 400)
    except PermissionError as exc:
        return error_response(str(exc), 403)
    except RuntimeError as exc:
        return error_response(str(exc), 503)


@application_intelligence_bp.route(
    "/api/application-analyses/<int:analysis_id>/runtime/network-policy",
    methods=["GET"],
)
@require_permission("applications:view")
def download_runtime_network_policy(analysis_id: int):
    try:
        snapshot = runtime_service.latest_snapshot(
            analysis_id, get_current_user()
        )
    except LookupError as exc:
        return _not_found(exc)
    except PermissionError as exc:
        return error_response(str(exc), 403)
    policy = snapshot.get("networkPolicy") or {}
    content = policy.get("yaml")
    if not content:
        return error_response("A NetworkPolicy recommendation is not available.", 404)
    log_audit(
        "application.network_policy_recommendation.downloaded",
        actor=get_current_user(),
        target_type="application_analysis",
        target_id=str(analysis_id),
        details={"review_only": True, "auto_applied": False},
    )
    return send_file(
        BytesIO(content.encode("utf-8")),
        mimetype="application/yaml",
        as_attachment=True,
        download_name=f"network-policy-recommendation-{analysis_id}.yaml",
    )


@application_intelligence_bp.route(
    "/api/application-analyses/<int:analysis_id>/artifacts", methods=["GET"]
)
@require_permission("applications:view")
def list_artifacts(analysis_id: int):
    try:
        return success_response(service.list_artifacts(analysis_id))
    except LookupError as exc:
        return _not_found(exc)


@application_intelligence_bp.route(
    "/api/application-artifacts/<int:artifact_id>/download", methods=["GET"]
)
@require_permission("applications:view")
def download_artifact(artifact_id: int):
    try:
        artifact, path = service.artifact_path(artifact_id)
    except LookupError as exc:
        return _not_found(exc)
    except ValueError as exc:
        return error_response(str(exc), 409)
    user = get_current_user()
    log_audit(
        "application.artifact.downloaded",
        actor=user,
        target_type="application_artifact",
        target_id=str(artifact.id),
        details={
            "analysis_id": artifact.analysis_id,
            "requested_by_user_id": user.id if user else None,
            "executed_by": service.EXECUTOR,
        },
    )
    return send_file(path, as_attachment=True, download_name=artifact.filename)


@application_intelligence_bp.route(
    "/api/application-analyses/<int:analysis_id>/pull-requests",
    methods=["GET"],
)
@require_permission("applications:view")
def list_pull_requests(analysis_id: int):
    try:
        return success_response(service.list_pull_requests(analysis_id))
    except LookupError as exc:
        return _not_found(exc)


@application_intelligence_bp.route(
    "/api/application-analyses/<int:analysis_id>/pull-requests",
    methods=["POST"],
)
@require_permission("applications:manage")
def request_pull_request(analysis_id: int):
    try:
        return success_response(
            service.request_pull_request(
                analysis_id,
                request.get_json(silent=True) or {},
                get_current_user(),
            ),
            status_code=202,
        )
    except LookupError as exc:
        return _not_found(exc)
    except ValueError as exc:
        return error_response(str(exc), 400)


def _worker_row_and_auth(analysis_id: int):
    try:
        row = service.get_analysis(analysis_id)
    except LookupError:
        return None, error_response("Analysis not found.", 404)
    header = request.headers.get("Authorization", "")
    token = header[7:].strip() if header.startswith("Bearer ") else ""
    if not service.verify_worker_token(row, token):
        return None, error_response("Unauthorized", 401)
    return row, None


@application_intelligence_bp.route(
    "/api/application-analysis-worker/<int:analysis_id>/event", methods=["POST"]
)
def worker_event(analysis_id: int):
    row, error = _worker_row_and_auth(analysis_id)
    if error:
        return error
    try:
        return success_response(
            service.record_worker_event(row, request.get_json(silent=True) or {})
        )
    except ValueError as exc:
        return error_response(str(exc), 400)


@application_intelligence_bp.route(
    "/api/application-analysis-worker/<int:analysis_id>/result", methods=["POST"]
)
def worker_result(analysis_id: int):
    row, error = _worker_row_and_auth(analysis_id)
    if error:
        return error
    try:
        return success_response(
            service.record_worker_result(row, request.get_json(silent=True) or {})
        )
    except ValueError:
        # Schema details are deliberately not reflected to the worker/client.
        service.record_worker_event(
            row,
            {
                "status": "Failed",
                "failureStage": "Analyzing",
                "safeErrorMessage": "Hermes returned a response that failed schema validation.",
            },
        )
        return error_response("Hermes response validation failed.", 422)


@application_intelligence_bp.route(
    "/api/application-analysis-worker/<int:analysis_id>/cleanup", methods=["POST"]
)
def worker_cleanup(analysis_id: int):
    row, error = _worker_row_and_auth(analysis_id)
    if error:
        return error
    return success_response(
        service.record_cleanup(row, request.get_json(silent=True) or {})
    )


@application_intelligence_bp.route(
    "/api/application-pull-request-worker/<int:pull_request_id>/result",
    methods=["POST"],
)
def pull_request_worker_result(pull_request_id: int):
    from ..models import ApplicationPullRequest

    row = db.session.get(ApplicationPullRequest, pull_request_id)
    if row is None:
        return error_response("Pull request not found.", 404)
    header = request.headers.get("Authorization", "")
    token = header[7:].strip() if header.startswith("Bearer ") else ""
    if not service.verify_pull_request_worker_token(row, token):
        return error_response("Unauthorized", 401)
    try:
        return success_response(
            service.record_pull_request_result(
                row, request.get_json(silent=True) or {}
            )
        )
    except ValueError as exc:
        return error_response(str(exc), 400)
