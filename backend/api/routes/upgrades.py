from flask import Blueprint, g, request

from ..access_engine import can_access_cluster
from ..auth_utils import auth_required_enabled, get_current_user
from ..decorators import require_cluster_access, require_permission
from ..response import error_response, success_response
from ..services.upgrade_service import get_upgrade_info, get_upgrade_job, run_precheck, run_start



upgrades_bp = Blueprint("upgrades", __name__, url_prefix="/api/upgrades")





def _actor_user_id():

    user = getattr(g, "current_user", None)

    return user.id if user else None





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

    payload = request.get_json(silent=True) or {}

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

    payload = request.get_json(silent=True) or {}

    cluster_id = str(payload.get("clusterId", "")).strip()

    target_version = str(payload.get("targetVersion", "v1.31.0")).strip()

    confirmation = payload.get("confirmation")

    if confirmation is not None:

        confirmation = str(confirmation).strip()



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
    data, error, status = get_upgrade_job(job_id)
    if error:
        return error_response(error, status)

    # Validate that the requesting user has access to the cluster this job belongs to.
    # Without this check any user with upgrades:precheck could poll any job ID.
    if auth_required_enabled():
        user = get_current_user()
        cluster_id = data.get("clusterId")
        if user and cluster_id and not can_access_cluster(user, cluster_id):
            return error_response("Forbidden", 403)

    return success_response(data)

