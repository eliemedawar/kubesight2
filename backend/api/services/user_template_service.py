"""Admin-authored application templates that extend the built-in marketplace.

Built-in templates live in ``wizard_templates.py``. These are stored in the
database and are visible to (and managed by) admins only. The public shape of a
template returned here matches the built-in templates so the frontend builder can
consume either kind interchangeably.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

from ..audit import log_audit
from ..db import db
from ..models import UserTemplate
from .wizard_templates import get_template as get_builtin_template

# Spec sub-objects copied verbatim into a template's body.
_SPEC_KEYS = (
    "containers",
    "resources",
    "networking",
    "scaling",
    "storage",
    "healthChecks",
    "environment",
    # The deployment schema: which fields are overridable, the env-var requirements,
    # and the backing-service dependencies a deployer fills in later.
    "schema",
)

_WORKLOAD_TYPES = (
    "Deployment",
    "StatefulSet",
    "DaemonSet",
    "Job",
    "CronJob",
)


def _slugify(value: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", (value or "").strip().lower()).strip("-")
    return slug or "template"


def _unique_slug(base: str) -> str:
    """Return a slug that collides with neither built-in nor existing custom ids."""
    candidate = base
    suffix = 2
    while get_builtin_template(candidate) is not None or UserTemplate.query.filter_by(slug=candidate).first():
        candidate = f"{base}-{suffix}"
        suffix += 1
    return candidate


def _summary(tpl: UserTemplate) -> Dict[str, Any]:
    return {
        "id": tpl.slug,
        "name": tpl.name,
        "description": tpl.description or "",
        "category": tpl.category or "Custom",
        "workloadType": tpl.workload_type or "Deployment",
        "custom": True,
        "createdBy": tpl.created_by,
    }


def _detail(tpl: UserTemplate) -> Dict[str, Any]:
    spec = tpl.spec or {}
    return {
        "id": tpl.slug,
        "name": tpl.name,
        "description": tpl.description or "",
        "category": tpl.category or "Custom",
        "workloadType": tpl.workload_type or "Deployment",
        "custom": True,
        **{key: spec[key] for key in _SPEC_KEYS if key in spec},
    }


def list_user_template_summaries() -> List[Dict[str, Any]]:
    rows = UserTemplate.query.order_by(UserTemplate.category.asc(), UserTemplate.name.asc()).all()
    return [_summary(row) for row in rows]


def get_user_template_detail(slug: str) -> Optional[Dict[str, Any]]:
    tpl = UserTemplate.query.filter_by(slug=slug).first()
    return _detail(tpl) if tpl else None


def _validate_payload(payload: Dict[str, Any]) -> Tuple[Optional[str], Dict[str, Any]]:
    name = (payload.get("name") or "").strip()
    if not name:
        return "Template name is required.", {}
    if len(name) > 120:
        return "Template name must be 120 characters or less.", {}

    category = (payload.get("category") or "Custom").strip() or "Custom"
    if len(category) > 80:
        return "Category name must be 80 characters or less.", {}

    workload_type = (payload.get("workloadType") or "Deployment").strip()
    if workload_type not in _WORKLOAD_TYPES:
        return f"Unsupported workload type '{workload_type}'.", {}

    containers = payload.get("containers") or []
    if not isinstance(containers, list) or not containers:
        return "At least one container is required.", {}
    first = containers[0] if isinstance(containers[0], dict) else {}
    if not str(first.get("image") or "").strip():
        return "The first container needs an image.", {}

    schema = payload.get("schema")
    if schema is not None:
        if not isinstance(schema, dict):
            return "Template schema must be an object.", {}
        if schema.get("env") is not None and not isinstance(schema["env"], list):
            return "Template schema 'env' must be a list.", {}
        if schema.get("dependencies") is not None and not isinstance(schema["dependencies"], list):
            return "Template schema 'dependencies' must be a list.", {}

    spec = {key: payload[key] for key in _SPEC_KEYS if payload.get(key) is not None}
    cleaned = {
        "name": name,
        "description": (payload.get("description") or "").strip()[:500] or None,
        "category": category,
        "workload_type": workload_type,
        "spec": spec,
    }
    return None, cleaned


def create_user_template(
    payload: Dict[str, Any],
    actor_user_id: Optional[int] = None,
) -> Tuple[Optional[Dict[str, Any]], Optional[str], int]:
    error, cleaned = _validate_payload(payload)
    if error:
        return None, error, 400

    slug = _unique_slug(_slugify(cleaned["name"]))
    tpl = UserTemplate(
        slug=slug,
        name=cleaned["name"],
        description=cleaned["description"],
        category=cleaned["category"],
        workload_type=cleaned["workload_type"],
        spec=cleaned["spec"],
        created_by=actor_user_id,
    )
    db.session.add(tpl)
    db.session.commit()
    log_audit(
        "user_template_created",
        actor_user_id=actor_user_id,
        target_type="user_template",
        target_id=slug,
        details={"name": cleaned["name"], "category": cleaned["category"]},
    )
    return _summary(tpl), None, 201


def update_user_template(
    slug: str,
    payload: Dict[str, Any],
    actor_user_id: Optional[int] = None,
) -> Tuple[Optional[Dict[str, Any]], Optional[str], int]:
    """Replace an existing custom template's fields. The slug (id) is preserved so
    existing references stay valid even when the name changes."""
    tpl = UserTemplate.query.filter_by(slug=slug).first()
    if not tpl:
        return None, "Template not found", 404

    error, cleaned = _validate_payload(payload)
    if error:
        return None, error, 400

    tpl.name = cleaned["name"]
    tpl.description = cleaned["description"]
    tpl.category = cleaned["category"]
    tpl.workload_type = cleaned["workload_type"]
    tpl.spec = cleaned["spec"]
    db.session.commit()
    log_audit(
        "user_template_updated",
        actor_user_id=actor_user_id,
        target_type="user_template",
        target_id=slug,
        details={"name": cleaned["name"], "category": cleaned["category"]},
    )
    return _summary(tpl), None, 200


def delete_user_template_category(
    category: str,
    actor_user_id: Optional[int] = None,
) -> Tuple[Optional[Dict[str, Any]], Optional[str], int]:
    """Delete every custom template filed under ``category``."""
    name = (category or "").strip()
    if not name:
        return None, "Category is required", 400
    rows = UserTemplate.query.filter_by(category=name).all()
    if not rows:
        return None, "No templates found in this category", 404
    count = len(rows)
    for tpl in rows:
        db.session.delete(tpl)
    db.session.commit()
    log_audit(
        "user_template_category_deleted",
        actor_user_id=actor_user_id,
        target_type="user_template_category",
        target_id=name,
        details={"category": name, "deleted": count},
    )
    return {"category": name, "deleted": count}, None, 200


def delete_user_template(
    slug: str,
    actor_user_id: Optional[int] = None,
) -> Tuple[Optional[Dict[str, Any]], Optional[str], int]:
    tpl = UserTemplate.query.filter_by(slug=slug).first()
    if not tpl:
        return None, "Template not found", 404
    name = tpl.name
    db.session.delete(tpl)
    db.session.commit()
    log_audit(
        "user_template_deleted",
        actor_user_id=actor_user_id,
        target_type="user_template",
        target_id=slug,
        details={"name": name},
    )
    return {"id": slug, "deleted": True}, None, 200
