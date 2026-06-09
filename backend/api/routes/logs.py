from flask import Blueprint, request

from ..decorators import require_cluster_access, require_namespace_access, require_permission
from ..response import error_response, success_response
from ..services.logs_service import fetch_pod_logs, parse_logs_query

logs_bp = Blueprint("logs", __name__, url_prefix="/api")


@logs_bp.route("/logs", methods=["GET"])
@require_permission("logs:view")
@require_cluster_access
@require_namespace_access
def get_logs():
    """Legacy query-param logs endpoint (kept for backward compatibility)."""
    cluster = request.args.get("cluster", "").strip()
    namespace = request.args.get("namespace", "").strip()
    pod = request.args.get("pod", "").strip()
    container = request.args.get("container", "").strip() or None

    if not cluster or not namespace or not pod:
        return error_response("cluster, namespace, and pod query parameters are required.", 400)

    params, param_error = parse_logs_query(request)
    if param_error:
        return param_error

    data, error = fetch_pod_logs(
        cluster_id=cluster,
        namespace=namespace,
        pod_name=pod,
        container_name=container,
        params=params,
    )
    if error:
        return error
    return success_response(data)
