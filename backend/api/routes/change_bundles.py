from flask import Blueprint, request

from ..auth_utils import get_current_user
from ..decorators import require_permission
from ..response import error_response, success_response
from ..services import change_bundle_service as svc

change_bundles_bp = Blueprint("change_bundles", __name__, url_prefix="/api/change-bundles")


# ---------------------------------------------------------------------------
# Draft / listing
# ---------------------------------------------------------------------------

@change_bundles_bp.route("/draft", methods=["GET", "POST"])
@require_permission("change_bundles:create")
def get_or_create_draft():
    user = get_current_user()
    bundle = svc.get_or_create_draft(user)
    return success_response(svc.serialize_bundle(bundle))


@change_bundles_bp.route("/mine", methods=["GET"])
@require_permission("change_bundles:create")
def list_my_bundles():
    user = get_current_user()
    limit = _int_arg("limit", 200)
    return success_response({"items": svc.list_my_bundles(user, limit=limit)})


@change_bundles_bp.route("", methods=["GET"])
@require_permission("change_bundles:view")
def list_bundles():
    status = request.args.get("status") or None
    limit = _int_arg("limit", 200)
    return success_response({"items": svc.list_bundles_for_approval(status=status, limit=limit)})


@change_bundles_bp.route("/pending", methods=["GET"])
@require_permission("change_bundles:manage")
def list_pending_bundles():
    limit = _int_arg("limit", 200)
    return success_response(
        {"items": svc.list_bundles_for_approval(status="pending_approval", limit=limit)}
    )


@change_bundles_bp.route("/<int:bundle_id>", methods=["GET"])
@require_permission("change_bundles:create")
def get_bundle(bundle_id: int):
    try:
        bundle = svc.get_bundle_or_error(bundle_id)
        _assert_can_read(bundle)
        return success_response(svc.serialize_bundle(bundle))
    except svc.ChangeBundleError as exc:
        return error_response(str(exc), exc.status_code)


# ---------------------------------------------------------------------------
# Items
# ---------------------------------------------------------------------------

@change_bundles_bp.route("/<int:bundle_id>/items", methods=["POST"])
@require_permission("change_bundles:create")
def add_item(bundle_id: int):
    user = get_current_user()
    payload = request.get_json(silent=True) or {}
    try:
        return success_response(svc.add_item(user, bundle_id, payload), status_code=201)
    except svc.ChangeBundleError as exc:
        return error_response(str(exc), exc.status_code)


@change_bundles_bp.route("/<int:bundle_id>/items/<int:item_id>", methods=["PUT"])
@require_permission("change_bundles:create")
def update_item(bundle_id: int, item_id: int):
    user = get_current_user()
    payload = request.get_json(silent=True) or {}
    try:
        return success_response(svc.update_item(user, bundle_id, item_id, payload))
    except svc.ChangeBundleError as exc:
        return error_response(str(exc), exc.status_code)


@change_bundles_bp.route("/<int:bundle_id>/items/<int:item_id>", methods=["DELETE"])
@require_permission("change_bundles:create")
def remove_item(bundle_id: int, item_id: int):
    user = get_current_user()
    try:
        return success_response(svc.remove_item(user, bundle_id, item_id))
    except svc.ChangeBundleError as exc:
        return error_response(str(exc), exc.status_code)


@change_bundles_bp.route("/<int:bundle_id>/items/<int:item_id>/diff", methods=["GET"])
@require_permission("change_bundles:create")
def diff_item(bundle_id: int, item_id: int):
    user = get_current_user()
    try:
        return success_response(svc.diff_item(user, bundle_id, item_id))
    except svc.ChangeBundleError as exc:
        return error_response(str(exc), exc.status_code)


@change_bundles_bp.route("/<int:bundle_id>", methods=["DELETE"])
@require_permission("change_bundles:create")
def delete_bundle(bundle_id: int):
    user = get_current_user()
    try:
        svc.delete_bundle(user, bundle_id)
        return success_response({"deleted": True})
    except svc.ChangeBundleError as exc:
        return error_response(str(exc), exc.status_code)


# ---------------------------------------------------------------------------
# Submit
# ---------------------------------------------------------------------------

@change_bundles_bp.route("/<int:bundle_id>/submit", methods=["POST"])
@require_permission("change_bundles:create")
def submit_bundle(bundle_id: int):
    user = get_current_user()
    payload = request.get_json(silent=True) or {}
    try:
        data = svc.submit_bundle(
            user,
            bundle_id,
            note=payload.get("note") or "",
            window_start=payload.get("windowStart") or payload.get("window_start"),
            window_end=payload.get("windowEnd") or payload.get("window_end"),
            window_timezone=payload.get("windowTimezone") or payload.get("window_timezone"),
            stop_on_failure=payload.get("stopOnFailure", payload.get("stop_on_failure", True)),
        )
        return success_response(data)
    except svc.ChangeBundleError as exc:
        return error_response(str(exc), exc.status_code)


# ---------------------------------------------------------------------------
# In-app approve / reject (management action)
# ---------------------------------------------------------------------------

@change_bundles_bp.route("/<int:bundle_id>/approve", methods=["POST"])
@require_permission("change_bundles:manage")
def approve_bundle(bundle_id: int):
    user = get_current_user()
    try:
        return success_response(svc.decide_bundle(bundle_id, "approve", actor=user))
    except svc.ChangeBundleError as exc:
        return error_response(str(exc), exc.status_code)


@change_bundles_bp.route("/<int:bundle_id>/reject", methods=["POST"])
@require_permission("change_bundles:manage")
def reject_bundle(bundle_id: int):
    user = get_current_user()
    payload = request.get_json(silent=True) or {}
    try:
        return success_response(
            svc.decide_bundle(bundle_id, "decline", actor=user, reason=payload.get("reason"))
        )
    except svc.ChangeBundleError as exc:
        return error_response(str(exc), exc.status_code)


# ---------------------------------------------------------------------------
# Email link actions (signed token; no session required)
# ---------------------------------------------------------------------------

def _action_result_page(title: str, body: str, status_code: int = 200):
    html = f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8" />
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
</style></head>
<body><div class="card"><div class="brand">KubeSight</div>
<h1>{title}</h1><p>{body}</p></div></body></html>"""
    return html, status_code, {"Content-Type": "text/html; charset=utf-8"}


def _handle_email_action(bundle_id: int, action: str):
    token = request.args.get("token", "")
    try:
        voter_email = svc.verify_action_token(token, bundle_id, action)
        result = svc.record_vote(bundle_id, action, voter_email=voter_email)
    except svc.ChangeBundleError as exc:
        return _action_result_page("Action failed", str(exc), exc.status_code)

    status = result.get("status")
    required = result.get("requiredApprovals", 1)
    approvals = result.get("approvals", 0)
    voted = "approve" if action == "approve" else "reject"
    if status == "approved":
        title, detail = "Bundle approved", "has been <strong>approved</strong> and will deploy in its window."
    elif status == "rejected":
        title, detail = "Bundle rejected", "has been <strong>rejected</strong>."
    else:
        title = "Vote recorded"
        detail = (
            f"your <strong>{voted}</strong> was recorded. "
            f"{approvals} of {required} required approval(s) so far — awaiting other approvers."
        )
    return _action_result_page(
        title,
        f"Thank you — for change bundle #{bundle_id}, {detail} You can close this window.",
    )


@change_bundles_bp.route("/<int:bundle_id>/approve", methods=["GET"])
def approve_bundle_link(bundle_id: int):
    return _handle_email_action(bundle_id, "approve")


@change_bundles_bp.route("/<int:bundle_id>/decline", methods=["GET"])
def decline_bundle_link(bundle_id: int):
    return _handle_email_action(bundle_id, "decline")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _int_arg(name: str, default: int) -> int:
    try:
        return int(request.args.get(name, default))
    except (TypeError, ValueError):
        return default


def _assert_can_read(bundle) -> None:
    """A requester may read their own bundle; managers/viewers may read any."""
    from ..access_engine import user_has_permission

    user = get_current_user()
    if user is None:
        return
    if bundle.requester_user_id == user.id:
        return
    if user_has_permission(user, "change_bundles:view") or user_has_permission(
        user, "change_bundles:manage"
    ):
        return
    raise svc.ChangeBundleError("You do not have access to this bundle.", 403)
