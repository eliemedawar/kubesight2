from flask import Blueprint, request

from ..access_engine import invalidate_access_rules_cache
from ..access_rules import access_rule_to_dict, apply_access_rules, get_user_access_rules, parse_access_rule_payload
from ..audit import log_audit
from ..auth_utils import get_current_user
from ..db import db
from ..decorators import require_permission
from ..models import AccessRule, User
from ..response import error_response, success_response

access_rules_bp = Blueprint("access_rules", __name__, url_prefix="/api/users")


@access_rules_bp.route("/<int:user_id>/access-rules", methods=["GET"])
@require_permission("users:view")
def list_user_access_rules(user_id: int):
    user = User.query.get(user_id)
    if not user:
        return error_response("User not found", 404)
    items = get_user_access_rules(user)
    return success_response({"items": items, "count": len(items)})


@access_rules_bp.route("/<int:user_id>/access-rules", methods=["PUT"])
@require_permission("users:update")
def replace_user_access_rules(user_id: int):
    user = User.query.get(user_id)
    if not user:
        return error_response("User not found", 404)

    payload = request.get_json(silent=True) or {}
    rules = payload.get("accessRules") or payload.get("rules") or []
    if not isinstance(rules, list):
        return error_response("accessRules must be a list", 400)

    try:
        parsed = [parse_access_rule_payload(r) for r in rules if isinstance(r, dict)]
    except ValueError as exc:
        return error_response(str(exc), 400)

    apply_access_rules(user, rules)
    db.session.commit()
    invalidate_access_rules_cache(user.id)
    log_audit(
        "access_rules_changed",
        actor=get_current_user(),
        target_type="user",
        target_id=str(user.id),
        details={"count": len(parsed)},
    )
    return success_response({"items": get_user_access_rules(user), "count": len(parsed)})


@access_rules_bp.route("/<int:user_id>/access-rules", methods=["POST"])
@require_permission("users:update")
def create_user_access_rule(user_id: int):
    user = User.query.get(user_id)
    if not user:
        return error_response("User not found", 404)
    payload = request.get_json(silent=True) or {}
    try:
        data = parse_access_rule_payload(payload)
    except ValueError as exc:
        return error_response(str(exc), 400)
    rule = AccessRule(user_id=user.id, **data)
    db.session.add(rule)
    db.session.commit()
    invalidate_access_rules_cache(user.id)
    log_audit(
        "access_rule_created",
        actor=get_current_user(),
        target_type="access_rule",
        target_id=str(rule.id),
        details={"userId": user.id},
    )
    return success_response(access_rule_to_dict(rule), 201)


@access_rules_bp.route("/<int:user_id>/access-rules/<int:rule_id>", methods=["PUT"])
@require_permission("users:update")
def update_user_access_rule(user_id: int, rule_id: int):
    rule = AccessRule.query.filter_by(user_id=user_id, id=rule_id).first()
    if not rule:
        return error_response("Access rule not found", 404)
    payload = request.get_json(silent=True) or {}
    try:
        data = parse_access_rule_payload({**access_rule_to_dict(rule), **payload})
    except ValueError as exc:
        return error_response(str(exc), 400)
    for key, value in data.items():
        setattr(rule, key, value)
    db.session.commit()
    invalidate_access_rules_cache(user_id)
    log_audit(
        "access_rule_updated",
        actor=get_current_user(),
        target_type="access_rule",
        target_id=str(rule.id),
    )
    return success_response(access_rule_to_dict(rule))


@access_rules_bp.route("/<int:user_id>/access-rules/<int:rule_id>", methods=["DELETE"])
@require_permission("users:update")
def delete_user_access_rule(user_id: int, rule_id: int):
    rule = AccessRule.query.filter_by(user_id=user_id, id=rule_id).first()
    if not rule:
        return error_response("Access rule not found", 404)
    db.session.delete(rule)
    db.session.commit()
    invalidate_access_rules_cache(user_id)
    log_audit(
        "access_rule_deleted",
        actor=get_current_user(),
        target_type="access_rule",
        target_id=str(rule_id),
        details={"userId": user_id},
    )
    return success_response({"id": rule_id, "deleted": True})
