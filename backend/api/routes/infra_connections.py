"""Reusable infrastructure records for the Cluster Builder: vSphere
connections, SSH credentials, SSH connection profiles, and build profiles.

These live under Settings — they are configured once and referenced by many
builds, so they get their own blueprint separate from the builds themselves.
"""

from flask import Blueprint, request

from ..audit import log_audit
from ..auth_utils import get_current_user
from ..decorators import require_permission
from ..response import error_response, success_response
from ..services import ssh_profile_service, vsphere_service
from ..services.cluster_build import profiles as build_profiles
from ..services.vsphere_client import VSphereError

infra_bp = Blueprint("infra_connections", __name__, url_prefix="/api")


def _actor_name():
    user = get_current_user()
    return getattr(user, "username", "") or ""


# ---------------------------------------------------------------------------
# vSphere connections
# ---------------------------------------------------------------------------

@infra_bp.route("/vsphere-connections", methods=["GET"])
@require_permission("vsphere:manage")
def list_vsphere_connections():
    return success_response({"items": vsphere_service.list_connections()})


@infra_bp.route("/vsphere-connections", methods=["POST"])
@require_permission("vsphere:manage")
def create_vsphere_connection():
    payload = request.get_json(silent=True) or {}
    try:
        data = vsphere_service.create_connection(payload)
    except ValueError as exc:
        return error_response(str(exc), 400)
    log_audit(
        "vsphere_connection_created",
        actor=get_current_user(),
        target_type="vsphere_connection",
        target_id=str(data.get("id")),
        details={"name": data.get("name"), "baseUrl": data.get("baseUrl")},
    )
    return success_response(data, status_code=201)


@infra_bp.route("/vsphere-connections/<int:connection_id>", methods=["PUT"])
@require_permission("vsphere:manage")
def update_vsphere_connection(connection_id: int):
    payload = request.get_json(silent=True) or {}
    try:
        data = vsphere_service.update_connection(connection_id, payload)
    except LookupError:
        return error_response("vSphere connection not found.", 404)
    except ValueError as exc:
        return error_response(str(exc), 400)
    log_audit(
        "vsphere_connection_updated",
        actor=get_current_user(),
        target_type="vsphere_connection",
        target_id=str(connection_id),
        details={"name": data.get("name")},
    )
    return success_response(data)


@infra_bp.route("/vsphere-connections/<int:connection_id>", methods=["DELETE"])
@require_permission("vsphere:manage")
def delete_vsphere_connection(connection_id: int):
    try:
        vsphere_service.delete_connection(connection_id)
    except LookupError:
        return error_response("vSphere connection not found.", 404)
    except ValueError as exc:
        return error_response(str(exc), 400)
    log_audit(
        "vsphere_connection_deleted",
        actor=get_current_user(),
        target_type="vsphere_connection",
        target_id=str(connection_id),
    )
    return success_response({"deleted": True})


@infra_bp.route("/vsphere-connections/<int:connection_id>/test", methods=["POST"])
@require_permission("vsphere:manage")
def test_vsphere_connection(connection_id: int):
    try:
        return success_response(vsphere_service.test_connection(connection_id))
    except LookupError:
        return error_response("vSphere connection not found.", 404)


@infra_bp.route("/vsphere-connections/<int:connection_id>/vms", methods=["GET"])
@require_permission("cluster_builds:create")
def vsphere_inventory(connection_id: int):
    force = str(request.args.get("refresh", "")).lower() in {"1", "true"}
    try:
        items = vsphere_service.get_inventory(connection_id, force_refresh=force)
    except LookupError:
        return error_response("vSphere connection not found.", 404)
    except ValueError as exc:
        return error_response(str(exc), 400)
    except VSphereError as exc:
        return error_response(f"vCenter inventory failed: {exc}", 502)
    return success_response({"items": items})


# ---------------------------------------------------------------------------
# SSH credentials
# ---------------------------------------------------------------------------

@infra_bp.route("/ssh-credentials", methods=["GET"])
@require_permission("ssh_credentials:manage")
def list_ssh_credentials():
    return success_response({"items": ssh_profile_service.list_credentials()})


@infra_bp.route("/ssh-credentials", methods=["POST"])
@require_permission("ssh_credentials:manage")
def create_ssh_credential():
    payload = request.get_json(silent=True) or {}
    try:
        data = ssh_profile_service.create_credential(payload, created_by=_actor_name())
    except ValueError as exc:
        return error_response(str(exc), 400)
    log_audit(
        "ssh_credential_created",
        actor=get_current_user(),
        target_type="ssh_credential",
        target_id=str(data.get("id")),
        details={"name": data.get("name"), "username": data.get("username")},
    )
    return success_response(data, status_code=201)


@infra_bp.route("/ssh-credentials/<int:credential_id>", methods=["PUT"])
@require_permission("ssh_credentials:manage")
def update_ssh_credential(credential_id: int):
    payload = request.get_json(silent=True) or {}
    try:
        data = ssh_profile_service.update_credential(credential_id, payload)
    except LookupError:
        return error_response("SSH credential not found.", 404)
    except ValueError as exc:
        return error_response(str(exc), 400)
    log_audit(
        "ssh_credential_updated",
        actor=get_current_user(),
        target_type="ssh_credential",
        target_id=str(credential_id),
        details={"name": data.get("name")},
    )
    return success_response(data)


@infra_bp.route("/ssh-credentials/<int:credential_id>", methods=["DELETE"])
@require_permission("ssh_credentials:manage")
def delete_ssh_credential(credential_id: int):
    try:
        ssh_profile_service.delete_credential(credential_id)
    except LookupError:
        return error_response("SSH credential not found.", 404)
    except ValueError as exc:
        return error_response(str(exc), 400)
    log_audit(
        "ssh_credential_deleted",
        actor=get_current_user(),
        target_type="ssh_credential",
        target_id=str(credential_id),
    )
    return success_response({"deleted": True})


# ---------------------------------------------------------------------------
# SSH connection profiles
# ---------------------------------------------------------------------------

@infra_bp.route("/ssh-connection-profiles", methods=["GET"])
@require_permission("ssh_credentials:manage")
def list_ssh_profiles():
    return success_response({"items": ssh_profile_service.list_profiles()})


@infra_bp.route("/ssh-connection-profiles", methods=["POST"])
@require_permission("ssh_credentials:manage")
def create_ssh_profile():
    payload = request.get_json(silent=True) or {}
    try:
        data = ssh_profile_service.create_profile(payload)
    except LookupError as exc:
        return error_response(str(exc), 400)
    except ValueError as exc:
        return error_response(str(exc), 400)
    log_audit(
        "ssh_profile_created",
        actor=get_current_user(),
        target_type="ssh_connection_profile",
        target_id=str(data.get("id")),
        details={"name": data.get("name"), "routeMode": data.get("routeMode")},
    )
    return success_response(data, status_code=201)


@infra_bp.route("/ssh-connection-profiles/<int:profile_id>", methods=["PUT"])
@require_permission("ssh_credentials:manage")
def update_ssh_profile(profile_id: int):
    payload = request.get_json(silent=True) or {}
    try:
        data = ssh_profile_service.update_profile(profile_id, payload)
    except LookupError:
        return error_response("SSH connection profile not found.", 404)
    except ValueError as exc:
        return error_response(str(exc), 400)
    log_audit(
        "ssh_profile_updated",
        actor=get_current_user(),
        target_type="ssh_connection_profile",
        target_id=str(profile_id),
        details={"name": data.get("name")},
    )
    return success_response(data)


@infra_bp.route("/ssh-connection-profiles/<int:profile_id>", methods=["DELETE"])
@require_permission("ssh_credentials:manage")
def delete_ssh_profile(profile_id: int):
    try:
        ssh_profile_service.delete_profile(profile_id)
    except LookupError:
        return error_response("SSH connection profile not found.", 404)
    except ValueError as exc:
        return error_response(str(exc), 400)
    log_audit(
        "ssh_profile_deleted",
        actor=get_current_user(),
        target_type="ssh_connection_profile",
        target_id=str(profile_id),
    )
    return success_response({"deleted": True})


@infra_bp.route("/ssh-connection-profiles/<int:profile_id>/test", methods=["POST"])
@require_permission("ssh_credentials:manage")
def test_ssh_profile(profile_id: int):
    payload = request.get_json(silent=True) or {}
    try:
        result = ssh_profile_service.test_profile(profile_id, payload.get("host", ""))
    except LookupError:
        return error_response("SSH connection profile not found.", 404)
    except ValueError as exc:
        return error_response(str(exc), 400)
    log_audit(
        "ssh_profile_tested",
        actor=get_current_user(),
        target_type="ssh_connection_profile",
        target_id=str(profile_id),
        details={"host": payload.get("host"), "status": result.get("status")},
    )
    return success_response(result)


# ---------------------------------------------------------------------------
# Build profiles (repository modes)
# ---------------------------------------------------------------------------

@infra_bp.route("/build-profiles", methods=["GET"])
@require_permission("cluster_builds:view")
def list_build_profiles():
    return success_response({"items": build_profiles.list_profiles()})


@infra_bp.route("/build-profiles", methods=["POST"])
@require_permission("cluster_builds:create")
def create_build_profile():
    payload = request.get_json(silent=True) or {}
    try:
        data = build_profiles.create_profile(payload)
    except ValueError as exc:
        return error_response(str(exc), 400)
    log_audit(
        "build_profile_created",
        actor=get_current_user(),
        target_type="build_profile",
        target_id=str(data.get("id")),
        details={"name": data.get("name"), "repoMode": data.get("repoMode")},
    )
    return success_response(data, status_code=201)


@infra_bp.route("/build-profiles/<int:profile_id>", methods=["PUT"])
@require_permission("cluster_builds:create")
def update_build_profile(profile_id: int):
    payload = request.get_json(silent=True) or {}
    try:
        data = build_profiles.update_profile(profile_id, payload)
    except LookupError:
        return error_response("Build profile not found.", 404)
    except ValueError as exc:
        return error_response(str(exc), 400)
    log_audit(
        "build_profile_updated",
        actor=get_current_user(),
        target_type="build_profile",
        target_id=str(profile_id),
        details={"name": data.get("name")},
    )
    return success_response(data)


@infra_bp.route("/build-profiles/<int:profile_id>", methods=["DELETE"])
@require_permission("cluster_builds:create")
def delete_build_profile(profile_id: int):
    try:
        build_profiles.delete_profile(profile_id)
    except LookupError:
        return error_response("Build profile not found.", 404)
    except ValueError as exc:
        return error_response(str(exc), 400)
    log_audit(
        "build_profile_deleted",
        actor=get_current_user(),
        target_type="build_profile",
        target_id=str(profile_id),
    )
    return success_response({"deleted": True})
