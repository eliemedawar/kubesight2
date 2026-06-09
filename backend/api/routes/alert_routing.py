from flask import Blueprint, request

from ..decorators import require_admin
from ..email_delivery import EmailDeliveryError
from ..response import error_response, success_response
from ..services import alert_routing_service as svc

alert_routing_bp = Blueprint("alert_routing", __name__, url_prefix="/api/alert-routing")


@alert_routing_bp.route("/smtp", methods=["GET"])
@require_admin
def get_smtp():
    return success_response(svc.serialize_smtp())


@alert_routing_bp.route("/smtp", methods=["POST"])
@require_admin
def save_smtp():
    payload = request.get_json(silent=True) or {}
    try:
        return success_response(svc.update_smtp(payload))
    except ValueError as exc:
        return error_response(str(exc), 400)


@alert_routing_bp.route("/smtp/test", methods=["POST"])
@require_admin
def test_smtp():
    payload = request.get_json(silent=True) or {}
    recipient = payload.get("recipient")
    try:
        return success_response(svc.send_smtp_test(recipient))
    except EmailDeliveryError as exc:
        return error_response(str(exc), 400)


@alert_routing_bp.route("/receivers", methods=["GET"])
@require_admin
def list_receivers():
    return success_response({"items": svc.list_receivers()})


@alert_routing_bp.route("/receivers", methods=["POST"])
@require_admin
def create_receiver():
    payload = request.get_json(silent=True) or {}
    try:
        return success_response(svc.create_receiver(payload), status_code=201)
    except ValueError as exc:
        return error_response(str(exc), 400)


@alert_routing_bp.route("/receivers/<int:receiver_id>", methods=["PUT"])
@require_admin
def update_receiver(receiver_id: int):
    payload = request.get_json(silent=True) or {}
    try:
        return success_response(svc.update_receiver(receiver_id, payload))
    except LookupError:
        return error_response("Receiver not found.", 404)
    except ValueError as exc:
        return error_response(str(exc), 400)


@alert_routing_bp.route("/receivers/<int:receiver_id>", methods=["DELETE"])
@require_admin
def delete_receiver(receiver_id: int):
    try:
        svc.delete_receiver(receiver_id)
        return success_response({"deleted": True})
    except LookupError:
        return error_response("Receiver not found.", 404)


@alert_routing_bp.route("/receivers/<int:receiver_id>/test", methods=["POST"])
@require_admin
def test_receiver(receiver_id: int):
    try:
        return success_response(svc.send_receiver_test(receiver_id))
    except LookupError:
        return error_response("Receiver not found.", 404)
    except EmailDeliveryError as exc:
        return error_response(str(exc), 400)


@alert_routing_bp.route("/delivery-logs", methods=["GET"])
@require_admin
def delivery_logs():
    limit = request.args.get("limit", 100)
    try:
        limit_int = int(limit)
    except (TypeError, ValueError):
        limit_int = 100
    return success_response({"items": svc.list_delivery_logs(limit=limit_int)})
