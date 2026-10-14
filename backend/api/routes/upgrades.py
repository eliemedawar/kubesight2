"""Upgrade Center API.

Read routes need ``upgrades:precheck``; anything that changes a cluster or
decides on a request needs ``upgrades:start`` (the service adds the rules on
top: no self-approval, waivers are admin-only).
"""

from flask import Blueprint, g, request

from ..access_engine import can_access_cluster
from ..auth_utils import auth_required_enabled, get_current_user
from ..decorators import require_cluster_access, require_permission
from ..response import error_response, success_response
from ..services import upgrade_center as center
from ..services.upgrade_service import get_upgrade_info, get_upgrade_job, run_precheck, run_start

upgrades_bp = Blueprint("upgrades", __name__, url_prefix="/api/upgrades")


def _user():
    return getattr(g, "current_user", None) or (get_current_user() if auth_required_enabled() else None)


def _actor_user_id():
    user = getattr(g, "current_user", None)
    return user.id if user else None


def _reply(result, ok_status: int = 200):
    data, error, status = result
    if error:
        # ``data`` keeps the detail behind an error (the readiness result
        # behind a 409, the phrase to type behind a 400) for the UI.
        return error_response(error, status, data)
    return success_response(data, status_code=status or ok_status)


def _body():
    return request.get_json(silent=True) or {}


# ---------------------------------------------------------------------------
# Fleet, workspace, readiness, runbook, history
# ---------------------------------------------------------------------------

@upgrades_bp.route("/fleet", methods=["GET"])
@require_permission("upgrades:precheck")
def upgrade_fleet():
    return _reply(center.fleet(_user()))


@upgrades_bp.route("/clusters/<cluster_id>", methods=["GET"])
@require_permission("upgrades:precheck")
@require_cluster_access
def upgrade_workspace(cluster_id: str):
    return _reply(center.workspace(cluster_id, user=_user()))


@upgrades_bp.route("/clusters/<cluster_id>/readiness", methods=["POST"])
@require_permission("upgrades:precheck")
@require_cluster_access
def upgrade_readiness(cluster_id: str):
    target = str(_body().get("targetVersion") or "").strip()
    return _reply(center.run_readiness(cluster_id, target, user=_user()))


@upgrades_bp.route("/clusters/<cluster_id>/runbook", methods=["GET"])
@require_permission("upgrades:precheck")
@require_cluster_access
def upgrade_runbook(cluster_id: str):
    target = str(request.args.get("targetVersion") or "").strip() or None
    return _reply(center.runbook(cluster_id, target, user=_user()))


@upgrades_bp.route("/clusters/<cluster_id>/guided", methods=["POST"])
@require_permission("upgrades:start")
@require_cluster_access
def upgrade_start_guided(cluster_id: str):
    target = str(_body().get("targetVersion") or "").strip()
    return _reply(center.start_guided(_user(), cluster_id, target), 201)


@upgrades_bp.route("/jobs", methods=["GET"])
@require_permission("upgrades:precheck")
@require_cluster_access
def upgrade_history():
    cluster_id = str(request.args.get("clusterId") or "").strip() or None
    try:
        limit = int(request.args.get("limit") or 50)
    except ValueError:
        limit = 50
    return _reply(center.history(_user(), cluster_id, limit))


# ---------------------------------------------------------------------------
# Requests: plan → approval → schedule → run
# ---------------------------------------------------------------------------

@upgrades_bp.route("/requests", methods=["POST"])
@require_permission("upgrades:start")
@require_cluster_access
def upgrade_request():
    body = _body()
    cluster_id = str(body.get("clusterId") or "").strip()
    if not cluster_id:
        return error_response("clusterId is required.", 400)
    return _reply(center.create_request(
        _user(),
        cluster_id,
        str(body.get("targetVersion") or "").strip(),
        options=body.get("options") if isinstance(body.get("options"), dict) else None,
        schedule=body.get("schedule") if isinstance(body.get("schedule"), dict) else None,
        waivers=body.get("waivers") if isinstance(body.get("waivers"), list) else None,
        confirmation=body.get("confirmation"),
        note=body.get("note"),
    ), 201)


@upgrades_bp.route("/requests/<job_id>/approve", methods=["POST"])
@require_permission("upgrades:start")
def upgrade_approve(job_id: str):
    return _reply(center.decide(_user(), job_id, "approve", _body().get("note")))


@upgrades_bp.route("/requests/<job_id>/reject", methods=["POST"])
@require_permission("upgrades:start")
def upgrade_reject(job_id: str):
    return _reply(center.decide(_user(), job_id, "reject", _body().get("note")))


@upgrades_bp.route("/requests/<job_id>/withdraw", methods=["POST"])
@require_permission("upgrades:start")
def upgrade_withdraw(job_id: str):
    return _reply(center.withdraw(_user(), job_id))


@upgrades_bp.route("/jobs/<job_id>/<action>", methods=["POST"])
@require_permission("upgrades:start")
def upgrade_control(job_id: str, action: str):
    if action == "cancel-guided":
        return _reply(center.cancel_guided(_user(), job_id))
    if action not in ("pause", "resume", "stop"):
        return error_response("Unknown action.", 404)
    return _reply(center.control(_user(), job_id, action))


# ---------------------------------------------------------------------------
# Earlier API (single-cluster page). /start now goes through a request, so an
# automatic upgrade still needs approval and the typed confirmation.
# ---------------------------------------------------------------------------

@upgrades_bp.route("/info", methods=["GET"])
@require_permission("upgrades:precheck")
@require_cluster_access
def upgrade_info():
    cluster_id = str(request.args.get("clusterId", "")).strip()
    target_version = str(request.args.get("targetVersion", "v1.31.0")).strip()
    data, error, status = get_upgrade_info(cluster_id, target_version, actor_user_id=_actor_user_id())
    if error:
        return error_response(error, status)
    return success_response(data)


@upgrades_bp.route("/precheck", methods=["POST"])
@require_permission("upgrades:precheck")
@require_cluster_access
def precheck_upgrade():
    payload = _body()
    cluster_id = str(payload.get("clusterId", "")).strip()
    target_version = str(payload.get("targetVersion", "v1.31.0")).strip()
    data, error, status = run_precheck(cluster_id, target_version, actor_user_id=_actor_user_id())
    if error:
        return error_response(error, status)
    return success_response(data)


@upgrades_bp.route("/start", methods=["POST"])
@require_permission("upgrades:start")
@require_cluster_access
def start_upgrade():
    payload = _body()
    cluster_id = str(payload.get("clusterId", "")).strip()
    target_version = str(payload.get("targetVersion", "v1.31.0")).strip()
    confirmation = payload.get("confirmation")
    if confirmation is not None:
        confirmation = str(confirmation).strip()
    try:
        mode = center.cluster_snapshot(cluster_id)["mode"] if cluster_id else None
    except Exception:  # noqa: BLE001 — let the older path report the error
        mode = None
    if mode == "automatic":
        return _reply(center.create_request(_user(), cluster_id, target_version, confirmation=confirmation), 201)
    data, error, status = run_start(
        cluster_id,
        target_version,
        confirmation=confirmation,
        actor_user_id=_actor_user_id(),
    )
    if error:
        return error_response(error, status)
    return success_response(data, status_code=status)


@upgrades_bp.route("/jobs/<job_id>", methods=["GET"])
@require_permission("upgrades:precheck")
def upgrade_job_status(job_id: str):
    if auth_required_enabled():
        return _reply(center.job_detail(get_current_user(), job_id))
    data, error, status = get_upgrade_job(job_id)
    if error:
        return error_response(error, status)
    user = get_current_user()
    cluster_id = data.get("clusterId")
    if user and cluster_id and not can_access_cluster(user, cluster_id):
        return error_response("Forbidden", 403)
    return success_response(data)
