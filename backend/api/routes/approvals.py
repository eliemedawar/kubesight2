"""The Approvals page: one inbox, one history, one place for the rules.

  GET  /api/approvals/inbox?scope=mine|all   what waits on a person (mine = I may decide it now)
  GET  /api/approvals/count                   the sidebar badge: how many wait on me
  GET  /api/approvals/history?kind=           what was decided
  GET  /api/approvals/rules                   every approval rule, sectioned by what the viewer may see
  GET/POST/PUT/DELETE /api/approvals/groups   approver groups as people, roles and emails

Reading needs only a signed-in user: each section and each pending item is
filtered by the permission that guards it. Deciding is NOT here — the inbox
sends each vote to that kind's own route, which keeps its own rules (no
self-approval, quorum, the CI stage's approver list, the VM approver check).
Rule writes go through the existing settings routes; only the approver groups
have their own endpoints here.
"""

from flask import Blueprint, request

from ..audit import log_audit
from ..auth_utils import get_current_user
from ..decorators import require_auth, require_permission
from ..response import error_response, success_response
from ..services import approvals_service as svc

approvals_bp = Blueprint("approvals", __name__, url_prefix="/api/approvals")


@approvals_bp.route("/inbox", methods=["GET"])
@require_auth
def inbox():
    scope = "all" if request.args.get("scope") == "all" else "mine"
    return success_response(svc.inbox(get_current_user(), scope))


@approvals_bp.route("/count", methods=["GET"])
@require_auth
def count():
    return success_response({"waitingOnMe": svc.waiting_count(get_current_user())})


@approvals_bp.route("/history", methods=["GET"])
@require_auth
def history():
    kind = request.args.get("kind") or None
    if kind and kind not in svc.KINDS:
        return error_response(f"kind must be one of {', '.join(svc.KINDS)}.", 400)
    try:
        limit = int(request.args.get("limit") or 200)
    except ValueError:
        limit = 200
    return success_response(svc.history(get_current_user(), kind, limit))


@approvals_bp.route("/rules", methods=["GET"])
@require_auth
def rules():
    return success_response(svc.rules(get_current_user()))


@approvals_bp.route("/groups", methods=["GET"])
@require_permission("deployment_requests:view")
def list_groups():
    return success_response(svc.list_groups())


@approvals_bp.route("/groups", methods=["POST"])
@require_permission("deployment_requests:manage")
def create_group():
    try:
        data = svc.save_group(request.get_json(silent=True) or {})
    except ValueError as exc:
        return error_response(str(exc), 400)
    log_audit("approver_group_created", actor=get_current_user(), target_type="receiver_group",
              target_id=str(data["id"]), details={"name": data["name"], "approvesDeploys": data["approvesDeploys"]})
    return success_response(data, status_code=201)


@approvals_bp.route("/groups/<int:group_id>", methods=["PUT"])
@require_permission("deployment_requests:manage")
def update_group(group_id: int):
    try:
        data = svc.save_group(request.get_json(silent=True) or {}, group_id)
    except LookupError as exc:
        return error_response(str(exc), 404)
    except ValueError as exc:
        return error_response(str(exc), 400)
    log_audit("approver_group_updated", actor=get_current_user(), target_type="receiver_group",
              target_id=str(group_id),
              details={"name": data["name"], "users": len(data["users"]), "roles": len(data["roles"]),
                       "emails": len(data["emails"]), "approvesDeploys": data["approvesDeploys"]})
    return success_response(data)


@approvals_bp.route("/groups/<int:group_id>", methods=["DELETE"])
@require_permission("deployment_requests:manage")
def delete_group(group_id: int):
    try:
        summary = svc.delete_group(group_id)
    except LookupError as exc:
        return error_response(str(exc), 404)
    log_audit("approver_group_deleted", actor=get_current_user(), target_type="receiver_group",
              target_id=str(group_id), details=summary)
    return success_response({"deleted": True})
