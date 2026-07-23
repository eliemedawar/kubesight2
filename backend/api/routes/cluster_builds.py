"""Cluster Builder API: build CRUD, preflight, start/retry/cancel, logs.

Permissions (all admin-only by default, see rbac_data):
  cluster_builds:view    — read builds, steps, logs, wizard options
  cluster_builds:create  — create/edit/delete builds
  cluster_builds:execute — preflight, start, retry, cancel (separable from
                           create so a reviewer can gate the run)
"""

from flask import Blueprint, request

from ..audit import log_audit
from ..auth_utils import get_current_user
from ..decorators import require_permission
from ..response import error_response, success_response
from ..services.cluster_build import service as svc

cluster_builds_bp = Blueprint("cluster_builds", __name__, url_prefix="/api/cluster-builds")


def _actor_name():
    user = get_current_user()
    return getattr(user, "username", "") or ""


@cluster_builds_bp.route("/options", methods=["GET"])
@require_permission("cluster_builds:view")
def wizard_options():
    return success_response(svc.wizard_options())


@cluster_builds_bp.route("", methods=["GET"])
@require_permission("cluster_builds:view")
def list_builds():
    return success_response({"items": svc.list_builds()})


@cluster_builds_bp.route("", methods=["POST"])
@require_permission("cluster_builds:create")
def create_build():
    payload = request.get_json(silent=True) or {}
    try:
        data = svc.create_build(payload, created_by=_actor_name())
    except LookupError as exc:
        return error_response(str(exc), 400)
    except ValueError as exc:
        return error_response(str(exc), 400)
    log_audit(
        "cluster_build_created",
        actor=get_current_user(),
        target_type="cluster_build",
        target_id=str(data.get("id")),
        details={
            "name": data.get("name"),
            "topology": data.get("topologyType"),
            "endpointMode": data.get("endpointMode"),
            "k8sVersion": data.get("k8sVersion"),
        },
    )
    return success_response(data, status_code=201)


@cluster_builds_bp.route("/<int:build_id>", methods=["GET"])
@require_permission("cluster_builds:view")
def get_build(build_id: int):
    try:
        build = svc.get_build(build_id)
    except LookupError:
        return error_response("Cluster build not found.", 404)
    return success_response(svc.serialize_build(build, include_detail=True))


@cluster_builds_bp.route("/<int:build_id>", methods=["PUT"])
@require_permission("cluster_builds:create")
def update_build(build_id: int):
    payload = request.get_json(silent=True) or {}
    try:
        data = svc.update_build(build_id, payload)
    except LookupError:
        return error_response("Cluster build not found.", 404)
    except ValueError as exc:
        return error_response(str(exc), 400)
    log_audit(
        "cluster_build_updated",
        actor=get_current_user(),
        target_type="cluster_build",
        target_id=str(build_id),
        details={"name": data.get("name")},
    )
    return success_response(data)


@cluster_builds_bp.route("/<int:build_id>", methods=["DELETE"])
@require_permission("cluster_builds:create")
def delete_build(build_id: int):
    try:
        svc.delete_build(build_id)
    except LookupError:
        return error_response("Cluster build not found.", 404)
    except ValueError as exc:
        return error_response(str(exc), 400)
    log_audit(
        "cluster_build_deleted",
        actor=get_current_user(),
        target_type="cluster_build",
        target_id=str(build_id),
    )
    return success_response({"deleted": True})


@cluster_builds_bp.route("/<int:build_id>/preflight", methods=["POST"])
@require_permission("cluster_builds:execute")
def preflight_build(build_id: int):
    try:
        result = svc.run_preflight(build_id)
    except LookupError:
        return error_response("Cluster build not found.", 404)
    except ValueError as exc:
        return error_response(str(exc), 400)
    log_audit(
        "cluster_build_preflight",
        actor=get_current_user(),
        target_type="cluster_build",
        target_id=str(build_id),
        details={"status": result.get("status")},
    )
    return success_response(result)


@cluster_builds_bp.route("/<int:build_id>/start", methods=["POST"])
@require_permission("cluster_builds:execute")
def start_build(build_id: int):
    payload = request.get_json(silent=True) or {}
    try:
        data = svc.start_build(
            build_id,
            ack_warnings=payload.get("ackWarnings"),
            actor=_actor_name(),
        )
    except LookupError:
        return error_response("Cluster build not found.", 404)
    except ValueError as exc:
        return error_response(str(exc), 400)
    log_audit(
        "cluster_build_started",
        actor=get_current_user(),
        target_type="cluster_build",
        target_id=str(build_id),
        details={"name": data.get("name"), "warningsAcked": bool(payload.get("ackWarnings"))},
    )
    return success_response(data)


@cluster_builds_bp.route("/<int:build_id>/retry", methods=["POST"])
@require_permission("cluster_builds:execute")
def retry_build(build_id: int):
    try:
        data = svc.retry_build(build_id)
    except LookupError:
        return error_response("Cluster build not found.", 404)
    except ValueError as exc:
        return error_response(str(exc), 400)
    log_audit(
        "cluster_build_retried",
        actor=get_current_user(),
        target_type="cluster_build",
        target_id=str(build_id),
        details={"resetNodes": data.get("resetNodes")},
    )
    return success_response(data)


@cluster_builds_bp.route("/<int:build_id>/cancel", methods=["POST"])
@require_permission("cluster_builds:execute")
def cancel_build(build_id: int):
    try:
        data = svc.cancel_build(build_id)
    except LookupError:
        return error_response("Cluster build not found.", 404)
    except ValueError as exc:
        return error_response(str(exc), 400)
    log_audit(
        "cluster_build_cancelled",
        actor=get_current_user(),
        target_type="cluster_build",
        target_id=str(build_id),
    )
    return success_response(data)


@cluster_builds_bp.route("/<int:build_id>/logs", methods=["GET"])
@require_permission("cluster_builds:view")
def build_logs(build_id: int):
    node_id = request.args.get("node", type=int)
    try:
        items = svc.build_logs(build_id, node_id=node_id)
    except LookupError:
        return error_response("Cluster build not found.", 404)
    return success_response({"items": items})
