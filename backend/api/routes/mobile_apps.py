"""Mobile Applications: registered APK/AAB/IPA apps, Jenkins-built binaries,
and store publishing.

Publishing is deliberately admin-only (not a grantable permission): pushing a
binary to Google Play / App Store Connect is an outward-facing, hard-to-undo
action, so it stays behind ``require_admin`` plus an explicit confirmation in
the UI.
"""

import os

from flask import Blueprint, request, send_file

from ..audit import log_audit
from ..auth_utils import get_current_user
from ..decorators import require_admin, require_permission
from ..response import error_response, success_response
from ..services import mobile_app_service as svc
from ..services.mobile_app_service import MobileAppError

mobile_apps_bp = Blueprint("mobile_apps", __name__, url_prefix="/api/mobile-apps")


@mobile_apps_bp.route("", methods=["GET"])
@require_permission("mobile_apps:view")
def list_apps():
    return success_response({"items": svc.list_apps()})


@mobile_apps_bp.route("/environments", methods=["GET"])
@require_permission("mobile_apps:manage")
def list_environments():
    """Custom environment names the register/edit form offers as the
    zohoEnvironment dropdown. Each ticketing provider keeps its own deploy
    source, and a mobile app belongs to none of them, so this is the union across
    providers (de-duped casefolded). Gated by mobile_apps:manage (not
    ticketing:view) because it exists solely for that manage-only form."""
    from ..services import ticketing_targets

    return success_response(
        {"items": ticketing_targets.custom_environment_names(provider=None)}
    )


@mobile_apps_bp.route("", methods=["POST"])
@require_permission("mobile_apps:manage")
def create_app():
    payload = request.get_json(silent=True) or {}
    try:
        data = svc.create_app(payload)
    except MobileAppError as exc:
        return error_response(str(exc), exc.status)
    log_audit(
        "mobile_app_created",
        actor=get_current_user(),
        target_type="mobile_app",
        target_id=str(data.get("id")),
        details={"name": data.get("name"), "zohoEnvironment": data.get("zohoEnvironment")},
    )
    return success_response(data, status_code=201)


@mobile_apps_bp.route("/<int:app_id>", methods=["PUT"])
@require_permission("mobile_apps:manage")
def update_app(app_id: int):
    payload = request.get_json(silent=True) or {}
    try:
        data = svc.update_app(app_id, payload)
    except MobileAppError as exc:
        return error_response(str(exc), exc.status)
    log_audit(
        "mobile_app_updated",
        actor=get_current_user(),
        target_type="mobile_app",
        target_id=str(app_id),
        details={"name": data.get("name")},
    )
    return success_response(data)


@mobile_apps_bp.route("/<int:app_id>", methods=["DELETE"])
@require_permission("mobile_apps:manage")
def delete_app(app_id: int):
    try:
        svc.delete_app(app_id)
    except MobileAppError as exc:
        return error_response(str(exc), exc.status)
    log_audit(
        "mobile_app_deleted",
        actor=get_current_user(),
        target_type="mobile_app",
        target_id=str(app_id),
    )
    return success_response({"deleted": True})


@mobile_apps_bp.route("/<int:app_id>/test-jenkins", methods=["POST"])
@require_permission("mobile_apps:manage")
def test_jenkins(app_id: int):
    try:
        return success_response(svc.test_jenkins_source(app_id))
    except MobileAppError as exc:
        return error_response(str(exc), exc.status)


@mobile_apps_bp.route("/<int:app_id>/test-play", methods=["POST"])
@require_permission("mobile_apps:manage")
def test_play(app_id: int):
    try:
        return success_response(svc.test_play_credentials(app_id))
    except MobileAppError as exc:
        return error_response(str(exc), exc.status)


@mobile_apps_bp.route("/<int:app_id>/test-appstore", methods=["POST"])
@require_permission("mobile_apps:manage")
def test_appstore(app_id: int):
    try:
        return success_response(svc.test_app_store_credentials(app_id))
    except MobileAppError as exc:
        return error_response(str(exc), exc.status)


@mobile_apps_bp.route("/<int:app_id>/fetch", methods=["POST"])
@require_permission("mobile_apps:manage")
def fetch_latest(app_id: int):
    try:
        builds = svc.fetch_latest(app_id, user=get_current_user())
    except MobileAppError as exc:
        return error_response(str(exc), exc.status)
    return success_response({"builds": builds}, status_code=202)


@mobile_apps_bp.route("/<int:app_id>/builds/upload", methods=["POST"])
@require_permission("mobile_apps:manage")
def upload_build(app_id: int):
    """Register a binary uploaded directly (multipart: file, platform, version)
    as a ready-to-publish build — bypasses Jenkins entirely."""
    file = request.files.get("file")
    platform = str(request.form.get("platform") or "")
    version = str(request.form.get("version") or "")
    try:
        data = svc.create_upload_build(
            app_id, platform, file, version, user=get_current_user()
        )
    except MobileAppError as exc:
        return error_response(str(exc), exc.status)
    return success_response(data, status_code=201)


@mobile_apps_bp.route("/<int:app_id>/builds", methods=["GET"])
@require_permission("mobile_apps:view")
def list_builds(app_id: int):
    try:
        svc.get_app(app_id)
    except MobileAppError as exc:
        return error_response(str(exc), exc.status)
    return success_response({"items": svc.list_builds(app_id)})


@mobile_apps_bp.route("/<int:app_id>/publishes", methods=["GET"])
@require_permission("mobile_apps:view")
def list_publishes(app_id: int):
    return success_response({"items": svc.list_publishes(app_id=app_id)})


@mobile_apps_bp.route("/builds/<int:build_id>/download", methods=["GET"])
@require_permission("mobile_apps:view")
def download_build(build_id: int):
    from ..models import MobileAppBuild

    build = MobileAppBuild.query.get(build_id)
    if build is None:
        return error_response("Build not found.", 404)
    path = svc.binary_path(build)
    if build.status != "available" or not (path and os.path.isfile(path)):
        return error_response("This build's binary is not available.", 409)
    log_audit(
        "mobile_app_build_downloaded_by_user",
        actor=get_current_user(),
        target_type="mobile_app_build",
        target_id=str(build_id),
        details={"file": build.file_name},
    )
    return send_file(
        path,
        as_attachment=True,
        download_name=build.file_name or os.path.basename(path),
        conditional=True,
    )


@mobile_apps_bp.route("/builds/<int:build_id>", methods=["DELETE"])
@require_permission("mobile_apps:manage")
def delete_build(build_id: int):
    try:
        svc.delete_build(build_id)
    except MobileAppError as exc:
        return error_response(str(exc), exc.status)
    log_audit(
        "mobile_app_build_deleted",
        actor=get_current_user(),
        target_type="mobile_app_build",
        target_id=str(build_id),
    )
    return success_response({"deleted": True})


@mobile_apps_bp.route("/builds/<int:build_id>/resign", methods=["POST"])
@require_permission("mobile_apps:manage")
def resign_build(build_id: int):
    """Queue a signing job for a build whose signature was stripped.

    Not admin-gated like publish: this produces a candidate binary inside
    KubeSight, it does not ship anything outward.
    """
    try:
        data = svc.start_resign(build_id, user=get_current_user())
    except MobileAppError as exc:
        return error_response(str(exc), exc.status)
    return success_response(data, status_code=202)


@mobile_apps_bp.route("/<int:app_id>/resigns", methods=["GET"])
@require_permission("mobile_apps:view")
def list_resigns(app_id: int):
    return success_response({"items": svc.list_resigns(app_id=app_id)})


@mobile_apps_bp.route("/builds/<int:build_id>/publish", methods=["POST"])
@require_admin
def publish_build(build_id: int):
    payload = request.get_json(silent=True) or {}
    try:
        data = svc.start_publish(
            build_id,
            str(payload.get("store") or ""),
            str(payload.get("target") or ""),
            user=get_current_user(),
        )
    except MobileAppError as exc:
        return error_response(str(exc), exc.status)
    return success_response(data, status_code=202)
