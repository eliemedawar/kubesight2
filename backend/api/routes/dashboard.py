from flask import Blueprint, request

from ..auth_utils import get_current_user
from ..decorators import require_cluster_access, require_permission
from ..response import error_response, success_response
from ..services.dashboard_service import get_dashboard_summary

dashboard_bp = Blueprint("dashboard", __name__, url_prefix="/api/dashboard")


@dashboard_bp.route("/summary", methods=["GET"])
@require_permission("overview:view")
@require_cluster_access
def dashboard_summary():
    cluster_id = str(request.args.get("clusterId", "")).strip()
    user = get_current_user()
    data, error, status = get_dashboard_summary(cluster_id, user=user)
    if error:
        return error_response(error, status)
    return success_response(data)
