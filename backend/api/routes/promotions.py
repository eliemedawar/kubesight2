"""Promotion rules API: the ladder board, promoting, exceptions and setup.

Reading the board needs ``promotions:view``. Changing the ladder (environments,
bindings, modes, the exempt list) needs ``promotions:manage``. Promoting and
asking for an exception are deploys, so they need ``apps:deploy`` — and each
target still goes through apply_yaml's namespace access, registry check, the
ladder itself and the cluster's approval rule.
"""

from __future__ import annotations

import json

from flask import Blueprint, g, request

from ..auth_utils import get_current_user
from ..decorators import require_permission
from ..response import error_response, success_response
from ..services import promotion_service as svc

promotions_bp = Blueprint("promotions", __name__, url_prefix="/api/promotions")


def _payload() -> dict:
    data = request.get_json(silent=True)
    return data if isinstance(data, dict) else {}


@promotions_bp.errorhandler(svc.PromotionError)
def _promotion_error(exc: svc.PromotionError):
    return error_response(str(exc), exc.status_code)


@promotions_bp.after_app_request
def _attach_verdict(response):
    """A deploy the ladder refused (409, from any route) carries the verdict.

    The deploy paths return plain ``(data, error, status)`` triples; rather than
    widen every route, the check leaves its verdict on ``g`` and this adds it to
    the error body, so any deploy form can offer "ask for an exception"."""
    verdict = getattr(g, "promotion_verdict", None)
    if not verdict or response.status_code != 409 or not response.is_json:
        return response
    try:
        body = response.get_json(silent=True) or {}
        if body.get("success") is False:
            data = body.get("data") if isinstance(body.get("data"), dict) else {}
            data["promotion"] = verdict
            body["data"] = data
            response.set_data(json.dumps(body))
    except Exception:  # noqa: BLE001 — never break the response itself
        pass
    return response


# ---------------------------------------------------------------------------
# The board
# ---------------------------------------------------------------------------

@promotions_bp.route("/overview", methods=["GET"])
@promotions_bp.route("/board", methods=["GET"])
@require_permission("promotions:view")
def get_overview():
    if request.args.get("refresh"):
        svc.invalidate_scan()
    return success_response(svc.overview())


@promotions_bp.route("/activity", methods=["GET"])
@require_permission("promotions:view")
def get_activity():
    limit = request.args.get("limit", type=int) or 100
    env_id = request.args.get("environmentId", type=int)
    kinds = [k for k in (request.args.get("kinds") or "").split(",") if k]
    items = svc.list_events(limit=limit, environment_id=env_id, kinds=kinds or None)
    return success_response({"items": items, "count": len(items)})


@promotions_bp.route("/releases", methods=["GET"])
@require_permission("promotions:view")
def get_releases():
    limit = request.args.get("limit", type=int) or 100
    env_id = request.args.get("environmentId", type=int)
    items = svc.list_releases(limit=limit, environment_id=env_id)
    return success_response({"items": items, "count": len(items)})


@promotions_bp.route("/releases/<int:release_id>", methods=["GET"])
@require_permission("promotions:view")
def get_release(release_id: int):
    from ..services.promotion_releases import get_release as _get

    return success_response(_get(release_id))


@promotions_bp.route("/releases", methods=["POST"])
@require_permission("apps:deploy")
def create_release():
    body = _payload()
    items = body.get("items")
    if not isinstance(items, list):
        return error_response("items must be a list", 400)
    try:
        env_id = int(body.get("environmentId"))
    except (TypeError, ValueError):
        return error_response("environmentId is required", 400)
    slot = None
    if body.get("departsAt"):
        from datetime import datetime

        try:
            slot = datetime.fromisoformat(str(body["departsAt"]).replace("Z", "+00:00"))
        except ValueError:
            return error_response("departsAt must be an ISO date-time", 400)
    data = svc.create_release(
        get_current_user(),
        environment_id=env_id,
        items=items,
        name=str(body.get("name") or ""),
        reference=str(body.get("reference") or ""),
        note=str(body.get("note") or ""),
        exception_reason=str(body.get("exceptionReason") or ""),
        slot=slot,
    )
    return success_response(data, status_code=201)


# ---------------------------------------------------------------------------
# The timetable
# ---------------------------------------------------------------------------

@promotions_bp.route("/timetable", methods=["GET"])
@require_permission("promotions:view")
def get_timetable():
    from ..services import promotion_timetable as tt

    return success_response(tt.timetable())


@promotions_bp.route("/environments/<int:env_id>/schedule", methods=["PUT"])
@require_permission("promotions:manage")
def put_schedule(env_id: int):
    from ..services import promotion_timetable as tt

    return success_response(tt.set_schedule(get_current_user(), env_id, _payload()))


@promotions_bp.route("/departures/state", methods=["POST"])
@require_permission("apps:deploy")
def departure_state():
    from ..services import promotion_timetable as tt

    body = _payload()
    return success_response(
        tt.set_departure_state(get_current_user(), body.get("environmentId"), body.get("departsAt"), str(body.get("state") or ""))
    )


@promotions_bp.route("/departures/exclude", methods=["POST"])
@require_permission("apps:deploy")
def departure_exclude():
    from ..services import promotion_timetable as tt

    body = _payload()
    return success_response(
        tt.set_excluded(
            get_current_user(),
            body.get("environmentId"),
            body.get("departsAt"),
            str(body.get("repository") or ""),
            bool(body.get("excluded", True)),
        )
    )


@promotions_bp.route("/history", methods=["GET"])
@require_permission("promotions:view")
def get_history():
    repository = (request.args.get("repository") or "").strip()
    if not repository:
        return error_response("repository is required", 400)
    return success_response({"repository": repository, "items": svc.image_history(repository)})


@promotions_bp.route("/check", methods=["POST"])
@require_permission("promotions:view")
def check():
    """What the ladder would say — for a deploy form's preview."""
    body = _payload()
    cluster_id = str(body.get("clusterId") or "").strip()
    namespace = str(body.get("namespace") or "").strip()
    images = body.get("images")
    if not isinstance(images, list):
        images = svc.images_in_yaml(body.get("yaml") or "")
    if not cluster_id or not namespace:
        return error_response("clusterId and namespace are required", 400)
    return success_response(svc.evaluate(cluster_id, namespace, images))


@promotions_bp.route("/promote", methods=["POST"])
@require_permission("apps:deploy")
def promote():
    body = _payload()
    targets = body.get("targets")
    if not isinstance(targets, list):
        return error_response("targets must be a list", 400)
    try:
        env_id = int(body.get("environmentId"))
    except (TypeError, ValueError):
        return error_response("environmentId is required", 400)
    data = svc.promote(
        get_current_user(),
        image=str(body.get("image") or ""),
        environment_id=env_id,
        targets=targets,
        note=str(body.get("note") or "").strip()[:500],
    )
    return success_response(data)


@promotions_bp.route("/exceptions", methods=["POST"])
@require_permission("apps:deploy")
def request_exception():
    body = _payload()
    changes = body.get("changes")
    if not isinstance(changes, list):
        return error_response("changes must be a list", 400)
    data = svc.request_exception(get_current_user(), changes=changes, reason=str(body.get("reason") or ""))
    return success_response(data, status_code=202)


# ---------------------------------------------------------------------------
# Setup
# ---------------------------------------------------------------------------

@promotions_bp.route("/setup", methods=["GET"])
@require_permission("promotions:view")
def get_setup():
    return success_response(svc.setup_payload())


@promotions_bp.route("/setup/defaults", methods=["POST"])
@require_permission("promotions:manage")
def create_defaults():
    return success_response(svc.create_default_ladder(get_current_user()), status_code=201)


@promotions_bp.route("/environments", methods=["POST"])
@require_permission("promotions:manage")
def create_environment():
    return success_response(svc.create_environment(get_current_user(), _payload()), status_code=201)


@promotions_bp.route("/environments/<int:env_id>", methods=["PUT"])
@require_permission("promotions:manage")
def update_environment(env_id: int):
    return success_response(svc.update_environment(get_current_user(), env_id, _payload()))


@promotions_bp.route("/environments/<int:env_id>", methods=["DELETE"])
@require_permission("promotions:manage")
def delete_environment(env_id: int):
    svc.delete_environment(get_current_user(), env_id)
    return success_response({"deleted": True, "id": env_id})


@promotions_bp.route("/environments/order", methods=["PUT"])
@require_permission("promotions:manage")
def reorder():
    ids = _payload().get("ids")
    if not isinstance(ids, list):
        return error_response("ids must be a list", 400)
    return success_response(svc.reorder(get_current_user(), ids))


@promotions_bp.route("/environments/<int:env_id>/bindings", methods=["POST"])
@require_permission("promotions:manage")
def add_binding(env_id: int):
    return success_response(svc.add_binding(get_current_user(), env_id, _payload()), status_code=201)


@promotions_bp.route("/bindings/<int:binding_id>", methods=["DELETE"])
@require_permission("promotions:manage")
def remove_binding(binding_id: int):
    return success_response(svc.remove_binding(get_current_user(), binding_id))


@promotions_bp.route("/policy", methods=["PUT"])
@require_permission("promotions:manage")
def update_policy():
    return success_response(svc.update_policy(get_current_user(), _payload()))


@promotions_bp.route("/namespace-map", methods=["GET"])
@require_permission("promotions:view")
def get_namespace_map():
    cluster_id = (request.args.get("clusterId") or "").strip()
    if not cluster_id:
        return error_response("clusterId is required", 400)
    return success_response(svc.namespace_map(cluster_id, get_current_user()))


@promotions_bp.route("/bindings/preview", methods=["POST"])
@require_permission("promotions:manage")
def preview_binding():
    body = _payload()
    cluster_id = str(body.get("clusterId") or "").strip()
    if not cluster_id:
        return error_response("clusterId is required", 400)
    env_id = body.get("environmentId")
    return success_response(
        svc.preview_binding(
            cluster_id,
            "*" if body.get("wholeCluster") else str(body.get("pattern") or ""),
            int(env_id) if env_id else None,
            get_current_user(),
        )
    )


@promotions_bp.route("/namespaces", methods=["GET"])
@require_permission("promotions:manage")
def namespaces():
    cluster_id = (request.args.get("clusterId") or "").strip()
    if not cluster_id:
        return error_response("clusterId is required", 400)
    return success_response({"items": svc.namespaces_for(cluster_id, get_current_user())})


@promotions_bp.route("/observe", methods=["POST"])
@require_permission("promotions:manage")
def observe_now():
    svc.invalidate_scan()
    return success_response(svc.observe())
