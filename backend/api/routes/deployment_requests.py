from flask import Blueprint, request

from ..auth_utils import get_current_user
from ..decorators import require_cluster_access, require_permission
from ..response import error_response, success_response
from ..services import deployment_request_service as svc

deployment_requests_bp = Blueprint(
    "deployment_requests", __name__, url_prefix="/api/deployment-requests"
)


@deployment_requests_bp.route("", methods=["POST"])
@require_permission("deployment_requests:request")
@require_cluster_access
def create_deployment_request():
    payload = request.get_json(silent=True) or {}
    cluster_id = payload.get("cluster_id") or payload.get("clusterId") or ""
    cluster_name = payload.get("cluster_name") or payload.get("clusterName") or ""
    message = payload.get("message") or ""
    user = get_current_user()
    try:
        data = svc.create_request(user, cluster_id, cluster_name, message)
        return success_response(data, status_code=201)
    except svc.DeploymentRequestError as exc:
        return error_response(str(exc), exc.status_code)


@deployment_requests_bp.route("/recipients", methods=["GET"])
@require_permission("deployment_requests:manage")
def get_deployment_request_recipients():
    return success_response(svc.get_recipient_config())


@deployment_requests_bp.route("/recipients", methods=["PUT"])
@require_permission("deployment_requests:manage")
def update_deployment_request_recipients():
    payload = request.get_json(silent=True) or {}
    try:
        return success_response(
            svc.update_recipient_config(
                emails=payload.get("recipients"),
                group_ids=payload.get("groupIds"),
                required_approvals=payload.get("requiredApprovals"),
            )
        )
    except svc.DeploymentRequestError as exc:
        return error_response(str(exc), exc.status_code)


@deployment_requests_bp.route("", methods=["GET"])
@require_permission("deployment_requests:view")
def list_deployment_requests():
    limit = request.args.get("limit", 200)
    try:
        limit_int = int(limit)
    except (TypeError, ValueError):
        limit_int = 200
    return success_response({"items": svc.list_requests(limit=limit_int)})


# ---------------------------------------------------------------------------
# In-app approve / decline (authenticated admin/management action)
# ---------------------------------------------------------------------------

@deployment_requests_bp.route("/<int:request_id>/approve", methods=["POST"])
@require_permission("deployment_requests:manage")
def approve_deployment_request(request_id: int):
    user = get_current_user()
    try:
        return success_response(svc.decide_request(request_id, "approve", actor=user))
    except svc.DeploymentRequestError as exc:
        return error_response(str(exc), exc.status_code)


@deployment_requests_bp.route("/<int:request_id>/decline", methods=["POST"])
@require_permission("deployment_requests:manage")
def decline_deployment_request(request_id: int):
    user = get_current_user()
    try:
        return success_response(svc.decide_request(request_id, "decline", actor=user))
    except svc.DeploymentRequestError as exc:
        return error_response(str(exc), exc.status_code)


# ---------------------------------------------------------------------------
# Email link actions (signed token; no session required)
# ---------------------------------------------------------------------------

def _action_result_page(title: str, body: str, status_code: int = 200):
    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>{title} - KubeSight</title>
  <style>
    body {{ font-family: -apple-system, Segoe UI, Roboto, Helvetica, Arial, sans-serif;
            background: #0f172a; color: #e2e8f0; display: flex; align-items: center;
            justify-content: center; min-height: 100vh; margin: 0; }}
    .card {{ background: #1e293b; border: 1px solid #334155; border-radius: 12px;
             padding: 2.5rem 3rem; max-width: 480px; text-align: center;
             box-shadow: 0 10px 30px rgba(0,0,0,.35); }}
    h1 {{ font-size: 1.4rem; margin: 0 0 .75rem; }}
    p {{ color: #94a3b8; line-height: 1.5; margin: 0; }}
    .brand {{ color: #38bdf8; font-weight: 600; letter-spacing: .04em;
              text-transform: uppercase; font-size: .75rem; margin-bottom: 1rem; }}
  </style>
</head>
<body>
  <div class="card">
    <div class="brand">KubeSight</div>
    <h1>{title}</h1>
    <p>{body}</p>
  </div>
</body>
</html>"""
    return html, status_code, {"Content-Type": "text/html; charset=utf-8"}


def _handle_email_action(request_id: int, action: str):
    token = request.args.get("token", "")
    try:
        voter_email = svc.verify_action_token(token, request_id, action)
        result = svc.record_vote(request_id, action, voter_email=voter_email)
    except svc.DeploymentRequestError as exc:
        return _action_result_page("Action failed", str(exc), exc.status_code)

    cluster = result.get("clusterName") or result.get("clusterId")
    voted = "approve" if action == "approve" else "decline"
    status = result.get("status")
    required = result.get("requiredApprovals", 1)
    approvals = result.get("approvals", 0)

    if status == "approved":
        title, detail = "Request approved", "has been <strong>approved</strong>."
    elif status == "declined":
        title, detail = "Request declined", "has been <strong>declined</strong>."
    else:
        title = "Vote recorded"
        detail = (
            f"your <strong>{voted}</strong> was recorded. "
            f"{approvals} of {required} required approval(s) so far — "
            "awaiting other approvers."
        )

    return _action_result_page(
        title,
        f"Thank you — for deployment request #{request_id} (cluster "
        f"<strong>{cluster}</strong>), {detail} You can close this window.",
    )


@deployment_requests_bp.route("/<int:request_id>/approve", methods=["GET"])
def approve_deployment_request_link(request_id: int):
    return _handle_email_action(request_id, "approve")


@deployment_requests_bp.route("/<int:request_id>/decline", methods=["GET"])
def decline_deployment_request_link(request_id: int):
    return _handle_email_action(request_id, "decline")
