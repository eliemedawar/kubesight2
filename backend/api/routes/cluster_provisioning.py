"""OpenTofu provisioning for the Cluster Builder.

  /api/cluster-templates                     the shapes the wizard starts from
  /api/cluster-builds/<id>/provision/...     plan, apply, grow, destroy, approve,
                                             install Kubernetes on a VMs-only build
  /api/vsphere-connections/<id>/placement    where VMs can go, what to clone
  /api/vsphere-connections/<id>/networks     address ranges per vCenter network
  /api/cluster-provisioning                  engine status, states and locks
  /api/internal/tofu-state/<build_id>        OpenTofu's HTTP state backend

The last one is not for people. OpenTofu calls it from inside this pod with
the running job's one-time credentials (basic auth ``job-<id>:<token>``), and
it refuses anything that does not come from the loopback interface.
"""

from __future__ import annotations

import base64
import json
import os

from flask import Blueprint, Response, request

from ..audit import log_audit
from ..auth_utils import get_current_user
from ..db import db
from ..decorators import require_permission
from ..response import error_response, success_response
from ..services import vsphere_service
from ..services.cluster_build import service as build_service
from ..services.cluster_build.provisioning import ip_pool, jobs, state_store, templates
from ..services.cluster_build.provisioning import service as provisioning
from ..services.vsphere_client import VSphereError
from .cluster_builds import _day_two_approval_refusal

cluster_provisioning_bp = Blueprint("cluster_provisioning", __name__)


def _actor():
    user = get_current_user()
    return user, getattr(user, "username", "") or ""


def _build_or_404(build_id: int):
    try:
        return build_service.get_build(build_id), None
    except LookupError:
        return None, error_response("Cluster build not found.", 404)


def _job_or_404(build_id: int, job_id: int):
    try:
        return jobs.get_job(job_id, build_id), None
    except LookupError:
        return None, error_response("Provisioning job not found.", 404)


def _build_response(build, status_code: int = 200):
    db.session.refresh(build)
    return success_response(build_service.serialize_build(build, include_detail=True), status_code)


# ---------------------------------------------------------------------------
# Templates
# ---------------------------------------------------------------------------

@cluster_provisioning_bp.route("/api/cluster-templates", methods=["GET"])
@require_permission("cluster_builds:view")
def list_cluster_templates():
    return success_response(templates.catalog())


@cluster_provisioning_bp.route("/api/cluster-templates", methods=["POST"])
@require_permission("cluster_templates:manage")
def create_cluster_template():
    user, actor = _actor()
    payload = request.get_json(silent=True) or {}
    try:
        data = templates.create_template(payload, created_by=actor)
    except LookupError as exc:
        return error_response(str(exc), 404)
    except ValueError as exc:
        return error_response(str(exc), 400)
    log_audit("cluster_template_created", actor=user, target_type="cluster_template",
              target_id=data["id"], details={"name": data["name"],
                                             "fromBuildId": payload.get("fromBuildId")})
    return success_response(data, 201)


@cluster_provisioning_bp.route("/api/cluster-templates/<int:template_id>", methods=["PUT"])
@require_permission("cluster_templates:manage")
def update_cluster_template(template_id: int):
    user, _ = _actor()
    try:
        data = templates.update_template(template_id, request.get_json(silent=True) or {})
    except LookupError as exc:
        return error_response(str(exc), 404)
    except ValueError as exc:
        return error_response(str(exc), 400)
    log_audit("cluster_template_updated", actor=user, target_type="cluster_template",
              target_id=data["id"], details={"name": data["name"]})
    return success_response(data)


@cluster_provisioning_bp.route("/api/cluster-templates/<int:template_id>", methods=["DELETE"])
@require_permission("cluster_templates:manage")
def delete_cluster_template(template_id: int):
    user, _ = _actor()
    try:
        templates.delete_template(template_id)
    except LookupError as exc:
        return error_response(str(exc), 404)
    log_audit("cluster_template_deleted", actor=user, target_type="cluster_template",
              target_id=f"custom:{template_id}")
    return success_response({"deleted": True})


# ---------------------------------------------------------------------------
# A build's VMs
# ---------------------------------------------------------------------------

def _audit_job(action: str, user, build, job, **details):
    log_audit(action, actor=user, target_type="cluster_build", target_id=str(build.id),
              details={"name": build.name, "jobId": job.id, "operation": job.operation, **details})


@cluster_provisioning_bp.route("/api/cluster-builds/<int:build_id>/provision/plan", methods=["POST"])
@require_permission("cluster_builds:execute")
def plan_new_vms(build_id: int):
    build, err = _build_or_404(build_id)
    if err:
        return err
    user, actor = _actor()
    try:
        job = provisioning.request_create_plan(build, actor=actor, user=user)
    except ValueError as exc:
        return error_response(str(exc), 400)
    _audit_job("cluster_build_vm_plan_requested", user, build, job)
    return _build_response(build, 202)


@cluster_provisioning_bp.route("/api/cluster-builds/<int:build_id>/provision/grow-plan", methods=["POST"])
@require_permission("cluster_builds:execute")
def plan_more_workers(build_id: int):
    build, err = _build_or_404(build_id)
    if err:
        return err
    user, actor = _actor()
    payload = request.get_json(silent=True) or {}
    try:
        job = provisioning.request_grow_plan(build, payload, actor=actor, user=user)
    except ValueError as exc:
        return error_response(str(exc), 400)
    _audit_job("cluster_build_vm_grow_planned", user, build, job, count=payload.get("count"))
    return _build_response(build, 202)


@cluster_provisioning_bp.route("/api/cluster-builds/<int:build_id>/provision/destroy", methods=["POST"])
@require_permission("cluster_builds:execute")
def request_destroy(build_id: int):
    build, err = _build_or_404(build_id)
    if err:
        return err
    user, actor = _actor()
    payload = request.get_json(silent=True) or {}
    try:
        job = provisioning.request_destroy(build, payload, actor=actor, user=user)
    except ValueError as exc:
        return error_response(str(exc), 400)
    _audit_job("cluster_build_destroy_requested", user, build, job, reason=job.reason)
    return _build_response(build, 202)


@cluster_provisioning_bp.route(
    "/api/cluster-builds/<int:build_id>/provision/jobs/<int:job_id>", methods=["GET"]
)
@require_permission("cluster_builds:view")
def get_provision_job(build_id: int, job_id: int):
    job, err = _job_or_404(build_id, job_id)
    if err:
        return err
    return success_response(jobs.serialize_job(job, include_plan=True))


@cluster_provisioning_bp.route(
    "/api/cluster-builds/<int:build_id>/provision/jobs/<int:job_id>/apply", methods=["POST"]
)
@require_permission("cluster_builds:execute")
def apply_provision_job(build_id: int, job_id: int):
    build, err = _build_or_404(build_id)
    if err:
        return err
    job, err = _job_or_404(build_id, job_id)
    if err:
        return err
    if job.operation == "grow":
        refused = _day_two_approval_refusal(build_id, "grow")
        if refused is not None:
            return refused
    user, actor = _actor()
    try:
        provisioning.apply_plan(build, job, actor=actor, user=user)
    except ValueError as exc:
        return error_response(str(exc), 400)
    _audit_job("cluster_build_vm_plan_applied", user, build, job,
               add=(job.plan_summary_json or {}).get("add"))
    return _build_response(build)


@cluster_provisioning_bp.route(
    "/api/cluster-builds/<int:build_id>/provision/jobs/<int:job_id>/approve", methods=["POST"]
)
@require_permission("cluster_builds:execute")
def approve_destroy(build_id: int, job_id: int):
    build, err = _build_or_404(build_id)
    if err:
        return err
    job, err = _job_or_404(build_id, job_id)
    if err:
        return err
    refused = _day_two_approval_refusal(build_id, "destroy")
    if refused is not None:
        return refused
    user, actor = _actor()
    note = (request.get_json(silent=True) or {}).get("note") or ""
    try:
        provisioning.approve_destroy(build, job, actor=actor, user=user, note=note)
    except PermissionError as exc:
        return error_response(str(exc), 403)
    except ValueError as exc:
        return error_response(str(exc), 400)
    _audit_job("cluster_build_destroy_approved", user, build, job,
               requestedBy=job.requested_by, destroy=(job.plan_summary_json or {}).get("destroy"))
    return _build_response(build)


@cluster_provisioning_bp.route(
    "/api/cluster-builds/<int:build_id>/provision/jobs/<int:job_id>/reject", methods=["POST"]
)
@require_permission("cluster_builds:execute")
def reject_destroy(build_id: int, job_id: int):
    build, err = _build_or_404(build_id)
    if err:
        return err
    job, err = _job_or_404(build_id, job_id)
    if err:
        return err
    user, actor = _actor()
    note = (request.get_json(silent=True) or {}).get("note") or ""
    try:
        provisioning.reject_destroy(build, job, actor=actor, user=user, note=note)
    except PermissionError as exc:
        return error_response(str(exc), 403)
    except ValueError as exc:
        return error_response(str(exc), 400)
    _audit_job("cluster_build_destroy_rejected", user, build, job, note=note[:200])
    return _build_response(build)


@cluster_provisioning_bp.route(
    "/api/cluster-builds/<int:build_id>/provision/jobs/<int:job_id>/discard", methods=["POST"]
)
@require_permission("cluster_builds:create")
def discard_provision_job(build_id: int, job_id: int):
    build, err = _build_or_404(build_id)
    if err:
        return err
    job, err = _job_or_404(build_id, job_id)
    if err:
        return err
    user, actor = _actor()
    try:
        provisioning.discard(build, job, actor=actor, user=user)
    except PermissionError as exc:
        return error_response(str(exc), 403)
    except ValueError as exc:
        return error_response(str(exc), 400)
    _audit_job("cluster_build_vm_plan_discarded", user, build, job)
    return _build_response(build)


@cluster_provisioning_bp.route(
    "/api/cluster-builds/<int:build_id>/provision/jobs/<int:job_id>/retry-connect", methods=["POST"]
)
@require_permission("cluster_builds:execute")
def retry_connect(build_id: int, job_id: int):
    build, err = _build_or_404(build_id)
    if err:
        return err
    job, err = _job_or_404(build_id, job_id)
    if err:
        return err
    user, _ = _actor()
    try:
        provisioning.retry_connect(build, job)
    except ValueError as exc:
        return error_response(str(exc), 400)
    _audit_job("cluster_build_vm_connect_retried", user, build, job)
    return _build_response(build)


@cluster_provisioning_bp.route(
    "/api/cluster-builds/<int:build_id>/provision/install-kubernetes", methods=["POST"]
)
@require_permission("cluster_builds:execute")
def install_kubernetes(build_id: int):
    """A VMs-only build whose VMs are ready: put Kubernetes on them."""
    build, err = _build_or_404(build_id)
    if err:
        return err
    user, actor = _actor()
    try:
        note = provisioning.install_kubernetes(
            build, request.get_json(silent=True) or {}, actor=actor, user=user
        )
    except ValueError as exc:
        return error_response(str(exc), 400)
    except PermissionError as exc:
        return error_response(str(exc), 403)
    log_audit("cluster_build_kubernetes_requested", actor=user, target_type="cluster_build",
              target_id=str(build.id), details={"name": build.name, "started": note is None})
    return _build_response(build)


# ---------------------------------------------------------------------------
# vCenter placement, networks, the provisioning account
# ---------------------------------------------------------------------------

@cluster_provisioning_bp.route("/api/vsphere-connections/<int:connection_id>/placement", methods=["GET"])
@require_permission("cluster_builds:create")
def vsphere_placement(connection_id: int):
    refresh = request.args.get("refresh") in ("1", "true", "yes")
    try:
        data = vsphere_service.placement(connection_id, force_refresh=refresh)
    except LookupError as exc:
        return error_response(str(exc), 404)
    except ValueError as exc:
        return error_response(str(exc), 400)
    except VSphereError as exc:
        return error_response(f"vCenter could not be read: {exc}", 502)
    return success_response({
        **data,
        "networks": ip_pool.list_ranges(connection_id),
    })


@cluster_provisioning_bp.route(
    "/api/vsphere-connections/<int:connection_id>/test-provisioning", methods=["POST"]
)
@require_permission("vsphere:manage")
def test_provisioning_account(connection_id: int):
    user, _ = _actor()
    try:
        result = vsphere_service.test_provisioning(connection_id)
    except LookupError as exc:
        return error_response(str(exc), 404)
    except ValueError as exc:
        return error_response(str(exc), 400)
    log_audit("vsphere_provisioning_tested", actor=user, target_type="vsphere_connection",
              target_id=str(connection_id), details={"status": result.get("status")})
    return success_response(result)


@cluster_provisioning_bp.route("/api/vsphere-connections/<int:connection_id>/networks", methods=["GET"])
@require_permission("cluster_builds:view")
def list_network_ranges(connection_id: int):
    return success_response({"items": ip_pool.list_ranges(connection_id)})


@cluster_provisioning_bp.route("/api/vsphere-connections/<int:connection_id>/networks", methods=["POST"])
@require_permission("vsphere:manage")
def create_network_range(connection_id: int):
    user, _ = _actor()
    try:
        vsphere_service.get_connection(connection_id)
        data = ip_pool.create_range(connection_id, request.get_json(silent=True) or {})
    except LookupError as exc:
        return error_response(str(exc), 404)
    except ValueError as exc:
        return error_response(str(exc), 400)
    log_audit("vsphere_network_range_created", actor=user, target_type="vsphere_connection",
              target_id=str(connection_id),
              details={"network": data["networkName"], "range": f"{data['rangeStart']}-{data['rangeEnd']}"})
    return success_response(data, 201)


@cluster_provisioning_bp.route(
    "/api/vsphere-connections/<int:connection_id>/networks/<int:range_id>", methods=["PUT"]
)
@require_permission("vsphere:manage")
def update_network_range(connection_id: int, range_id: int):
    user, _ = _actor()
    try:
        data = ip_pool.update_range(connection_id, range_id, request.get_json(silent=True) or {})
    except LookupError as exc:
        return error_response(str(exc), 404)
    except ValueError as exc:
        return error_response(str(exc), 400)
    log_audit("vsphere_network_range_updated", actor=user, target_type="vsphere_connection",
              target_id=str(connection_id),
              details={"network": data["networkName"], "range": f"{data['rangeStart']}-{data['rangeEnd']}"})
    return success_response(data)


@cluster_provisioning_bp.route(
    "/api/vsphere-connections/<int:connection_id>/networks/<int:range_id>", methods=["DELETE"]
)
@require_permission("vsphere:manage")
def delete_network_range(connection_id: int, range_id: int):
    user, _ = _actor()
    try:
        ip_pool.delete_range(connection_id, range_id)
    except LookupError as exc:
        return error_response(str(exc), 404)
    except ValueError as exc:
        return error_response(str(exc), 400)
    log_audit("vsphere_network_range_deleted", actor=user, target_type="vsphere_connection",
              target_id=str(connection_id), details={"rangeId": range_id})
    return success_response({"deleted": True})


@cluster_provisioning_bp.route("/api/vsphere-networks/<int:range_id>/preview", methods=["GET"])
@require_permission("cluster_builds:create")
def preview_addresses(range_id: int):
    try:
        data = ip_pool.preview(range_id, request.args.get("count", type=int) or 0)
    except LookupError as exc:
        return error_response(str(exc), 404)
    return success_response(data)


# ---------------------------------------------------------------------------
# Overview and the lock escape hatch
# ---------------------------------------------------------------------------

@cluster_provisioning_bp.route("/api/cluster-provisioning", methods=["GET"])
@require_permission("cluster_builds:view")
def provisioning_overview():
    return success_response(provisioning.overview())


@cluster_provisioning_bp.route(
    "/api/cluster-provisioning/locks/<int:build_id>/release", methods=["POST"]
)
@require_permission("vsphere:manage")
def release_lock(build_id: int):
    user, _ = _actor()
    try:
        released = provisioning.release_stale_lock(build_id)
    except ValueError as exc:
        return error_response(str(exc), 400)
    log_audit("cluster_build_state_lock_released", actor=user, target_type="cluster_build",
              target_id=str(build_id), details=released)
    return success_response({"released": released})


# ---------------------------------------------------------------------------
# OpenTofu's HTTP state backend
# ---------------------------------------------------------------------------

_LOOPBACK = {"127.0.0.1", "::1", "::ffff:127.0.0.1"}


def _state_caller(build_id: int):
    """The job behind this request, or an error response."""
    remote_ok = os.getenv("KUBESIGHT_TOFU_STATE_ALLOW_REMOTE", "").lower() in ("1", "true", "yes")
    if not remote_ok and request.remote_addr not in _LOOPBACK:
        return None, Response("state backend is only reachable from inside KubeSight\n", 403)
    header = request.headers.get("Authorization", "")
    username = password = ""
    if header.lower().startswith("basic "):
        try:
            decoded = base64.b64decode(header.split(" ", 1)[1]).decode("utf-8")
            username, _, password = decoded.partition(":")
        except (ValueError, UnicodeDecodeError):
            pass
    job = jobs.authenticate(username, password, build_id)
    if job is None:
        return None, Response("unauthorized\n", 401, {"WWW-Authenticate": 'Basic realm="tofu-state"'})
    return job, None


def _lock_body():
    try:
        return json.loads(request.get_data(as_text=True) or "{}")
    except ValueError:
        return {}


@cluster_provisioning_bp.route(
    "/api/internal/tofu-state/<int:build_id>", methods=["GET", "POST", "DELETE"]
)
def tofu_state(build_id: int):
    job, err = _state_caller(build_id)
    if err:
        return err
    if request.method == "GET":
        text = state_store.read_state(build_id)
        if not text:
            return Response(status=204)
        return Response(text, 200, mimetype="application/json")
    lock_id = request.args.get("ID") or None
    try:
        if request.method == "POST":
            state_store.write_state(build_id, request.get_data(as_text=True), lock_id=lock_id)
        else:
            state_store.delete_state(build_id, lock_id=lock_id)
    except state_store.StateLocked as exc:
        return Response(json.dumps(exc.lock_info), 423, mimetype="application/json")
    except ValueError as exc:
        return Response(f"{exc}\n", 400)
    return Response(status=200)


@cluster_provisioning_bp.route("/api/internal/tofu-state/<int:build_id>/lock", methods=["POST"])
def tofu_state_lock(build_id: int):
    job, err = _state_caller(build_id)
    if err:
        return err
    try:
        ok, held = state_store.lock(build_id, _lock_body(), job_id=job.id)
    except ValueError as exc:
        return Response(f"{exc}\n", 400)
    if not ok:
        return Response(json.dumps(held or {}), 423, mimetype="application/json")
    return Response(status=200)


@cluster_provisioning_bp.route("/api/internal/tofu-state/<int:build_id>/unlock", methods=["POST"])
def tofu_state_unlock(build_id: int):
    job, err = _state_caller(build_id)
    if err:
        return err
    ok, held = state_store.unlock(build_id, _lock_body())
    if not ok:
        return Response(json.dumps(held or {}), 409, mimetype="application/json")
    return Response(status=200)
