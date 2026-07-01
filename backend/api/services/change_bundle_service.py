"""Change Bundle workflow — a "shopping cart" of Kubernetes changes.

A requester stages multiple change actions into a draft bundle, then submits it
with a requested deployment window. The bundle reuses the deployment-request
approval audience (quorum + signed email approve/decline links). On approval the
background scheduler (see :mod:`change_bundle_executor`) auto-executes each item
when the window opens.

This module deliberately reuses the existing building blocks rather than
re-implementing them:
  * templates       -> template_resolver.resolve_template + wizard_manifest_generator
  * image changes   -> manifest_generator.generate_manifests
  * YAML apply/diff  -> deployment_service (validate/sanitize/apply)
  * approval/quorum  -> deployment_request_service helpers
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import jwt

from ..access_engine import can_access_namespace, is_admin
from ..audit import log_audit
from ..auth_utils import _jwt_secret
from ..db import db
from ..email_delivery import EmailDeliveryError, send_email, smtp_is_configured
from ..models import ChangeBundle, ChangeBundleItem, ChangeBundleVote, User
from .deployment_request_service import (
    _clean_timezone,
    _html_escape,
    _parse_iso_datetime,
    _public_base_url,
    _resolve_recipients_with_source,
    cluster_required_approvals,
)
from .deployment_service import (
    check_registry_images,
    parse_yaml_documents,
    sanitize_for_apply,
    validate_yaml,
)

# Audit action names (kept stable so audit log filters can target them).
ACTION_ITEM_ADDED = "BUNDLE_ITEM_ADDED"
ACTION_SUBMITTED = "BUNDLE_SUBMITTED"
ACTION_APPROVED = "BUNDLE_APPROVED"
ACTION_REJECTED = "BUNDLE_REJECTED"

_TOKEN_TYPE = "change_bundle_action"
_TOKEN_TTL_HOURS = 72
VALID_ACTIONS = {"approve", "decline"}

# action_type -> execution mode + the permission required to stage it.
ACTION_TYPES: Dict[str, Dict[str, str]] = {
    "create_from_template": {"mode": "apply", "permission": "apps:deploy"},
    "edit_deployment": {"mode": "apply", "permission": "apps:deploy"},
    "change_image": {"mode": "apply", "permission": "apps:deploy"},
    "update_env": {"mode": "apply", "permission": "apps:deploy"},
    "update_resources": {"mode": "apply", "permission": "apps:deploy"},
    "update_hpa": {"mode": "apply", "permission": "apps:deploy"},
    "scale_replicas": {"mode": "scale", "permission": "apps:deploy"},
    "delete_deployment": {"mode": "delete", "permission": "apps:delete"},
}

# Bundles in these statuses are immutable to the requester.
LOCKED_STATUSES = {
    "pending_approval",
    "approved",
    "scheduled",
    "deploying",
    "completed",
    "failed",
    "partially_failed",
    "expired",
}


class ChangeBundleError(RuntimeError):
    """Validation / state error with an associated HTTP status code."""

    def __init__(self, message: str, status_code: int = 400):
        super().__init__(message)
        self.status_code = status_code


# ---------------------------------------------------------------------------
# Signed action tokens (HMAC via JWT HS256) — mirrors deployment requests
# ---------------------------------------------------------------------------

def generate_action_token(bundle_id: int, action: str, voter_email: str = "") -> str:
    if action not in VALID_ACTIONS:
        raise ValueError(f"Unsupported action: {action}")
    now = datetime.now(timezone.utc)
    payload = {
        "typ": _TOKEN_TYPE,
        "cbid": int(bundle_id),
        "act": action,
        "eml": (voter_email or "").strip().lower(),
        "iat": now,
        "exp": now + timedelta(hours=_TOKEN_TTL_HOURS),
    }
    token = jwt.encode(payload, _jwt_secret(), algorithm="HS256")
    return token if isinstance(token, str) else token.decode("utf-8")


def verify_action_token(token: str, bundle_id: int, action: str) -> str:
    if not token:
        raise ChangeBundleError("Missing approval token.", 401)
    try:
        payload = jwt.decode(token, _jwt_secret(), algorithms=["HS256"])
    except jwt.ExpiredSignatureError as exc:
        raise ChangeBundleError("This approval link has expired.", 401) from exc
    except jwt.PyJWTError as exc:
        raise ChangeBundleError("Invalid approval token.", 401) from exc
    if payload.get("typ") != _TOKEN_TYPE:
        raise ChangeBundleError("Invalid approval token.", 401)
    if str(payload.get("cbid")) != str(bundle_id):
        raise ChangeBundleError("Token does not match this bundle.", 401)
    if payload.get("act") != action:
        raise ChangeBundleError("Token does not match this action.", 401)
    return str(payload.get("eml") or "")


# ---------------------------------------------------------------------------
# Serialization
# ---------------------------------------------------------------------------

def _iso(dt: Optional[datetime]) -> Optional[str]:
    if not dt:
        return None
    # SQLite returns naive UTC; emit an explicit UTC offset so clients parse it right.
    return (dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt).isoformat()


def _as_utc(dt: datetime) -> datetime:
    """Coerce a (possibly naive) datetime to timezone-aware UTC.

    SQLite drops tz info, so datetimes read back are naive UTC wall-clocks. Without
    this, ``astimezone()`` would treat them as *local* time and skip the conversion
    (showing UTC clock with a non-UTC label).
    """
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt


def _format_window(
    start: Optional[datetime], end: Optional[datetime], tz_name: Optional[str]
) -> Optional[str]:
    if not start or not end:
        return None
    start, end = _as_utc(start), _as_utc(end)
    tz = None
    if tz_name:
        try:
            tz = ZoneInfo(tz_name)
        except (ZoneInfoNotFoundError, ValueError):
            tz = None
    if tz is not None:
        start_local, end_local, label = start.astimezone(tz), end.astimezone(tz), tz_name
    else:
        start_local, end_local, label = start, end, "UTC"
    if start_local.date() == end_local.date():
        span = f"{start_local.strftime('%Y-%m-%d %H:%M')}–{end_local.strftime('%H:%M')}"
    else:
        span = f"{start_local.strftime('%Y-%m-%d %H:%M')} – {end_local.strftime('%Y-%m-%d %H:%M')}"
    return f"{span} ({label})"


def serialize_item(item: ChangeBundleItem) -> Dict[str, Any]:
    return {
        "id": item.id,
        "position": item.position,
        "actionType": item.action_type,
        "clusterId": item.cluster_id,
        "clusterName": _resolve_cluster_name(item.cluster_id, item.cluster_name),
        "namespace": item.namespace,
        "resourceKind": item.resource_kind,
        "resourceName": item.resource_name,
        "oldPayload": item.old_payload_json,
        "newPayload": item.new_payload_json,
        "yamlPreview": item.yaml_preview,
        "validationStatus": item.validation_status,
        "validationMessage": item.validation_message,
        "status": item.status,
        "executionResult": item.execution_result,
        "createdAt": _iso(item.created_at),
    }


def _vote_tally(row: ChangeBundle) -> Tuple[int, int]:
    approvals = sum(1 for v in row.votes if v.decision == "approve")
    declines = sum(1 for v in row.votes if v.decision == "decline")
    return approvals, declines


def serialize_bundle(row: ChangeBundle, *, include_items: bool = True) -> Dict[str, Any]:
    requester = row.requester
    approver = row.approved_by
    approvals, declines = _vote_tally(row)
    clusters = sorted({item.cluster_id for item in row.items})
    cluster_names = sorted(
        {_resolve_cluster_name(item.cluster_id, item.cluster_name) for item in row.items}
    )
    payload: Dict[str, Any] = {
        "id": row.id,
        "status": row.status,
        "note": row.note,
        "requesterId": row.requester_user_id,
        "requesterName": (requester.full_name or requester.username) if requester else "Unknown",
        "requestedStartTime": _iso(row.requested_start_time),
        "requestedEndTime": _iso(row.requested_end_time),
        "requestedWindowTimezone": row.requested_window_timezone,
        "requestedWindowLabel": _format_window(
            row.requested_start_time, row.requested_end_time, row.requested_window_timezone
        ),
        "requiredApprovals": row.required_approvals if row.required_approvals is not None else 1,
        "totalRecipients": row.total_recipients or 0,
        "approvals": approvals,
        "declines": declines,
        "stopOnFailure": bool(row.stop_on_failure),
        "approvedById": row.approved_by_user_id,
        "approvedByName": (approver.full_name or approver.username) if approver else None,
        "approvedAt": _iso(row.approved_at),
        "rejectionReason": row.rejection_reason,
        "executionStartedAt": _iso(row.execution_started_at),
        "executionFinishedAt": _iso(row.execution_finished_at),
        "clusters": clusters,
        "clusterNames": cluster_names,
        "itemCount": len(row.items),
        "createdAt": _iso(row.created_at),
        "updatedAt": _iso(row.updated_at),
    }
    if include_items:
        payload["items"] = [serialize_item(item) for item in sorted(row.items, key=lambda i: i.position)]
    return payload


# ---------------------------------------------------------------------------
# Lookup helpers
# ---------------------------------------------------------------------------

def _resolve_cluster_name(cluster_id: str, provided: str = "") -> str:
    """Best display name for a cluster id (real name > caller value > id)."""
    try:
        from ..cluster_store import get_active_cluster_by_public_id

        cluster = get_active_cluster_by_public_id(cluster_id)
        if cluster and cluster.name:
            return cluster.name
    except Exception:  # noqa: BLE001 — name resolution must never block staging
        pass
    provided = (provided or "").strip()
    if provided and provided != cluster_id:
        return provided
    return cluster_id


def get_bundle_or_error(bundle_id: int) -> ChangeBundle:
    row = ChangeBundle.query.get(bundle_id)
    if not row:
        raise ChangeBundleError("Change bundle not found.", 404)
    return row


def _assert_owner(bundle: ChangeBundle, user: Optional[User]) -> None:
    if user is None:
        return
    if is_admin(user):
        return
    if bundle.requester_user_id != user.id:
        raise ChangeBundleError("You can only modify your own change bundles.", 403)


def _assert_editable(bundle: ChangeBundle) -> None:
    if bundle.status != "draft":
        raise ChangeBundleError(
            f"This bundle is {bundle.status.replace('_', ' ')} and can no longer be edited.", 409
        )


def get_or_create_draft(user: Optional[User]) -> ChangeBundle:
    """Return the user's open draft bundle, creating one if none exists."""
    if user is not None:
        existing = (
            ChangeBundle.query.filter(
                ChangeBundle.requester_user_id == user.id,
                ChangeBundle.status == "draft",
            )
            .order_by(ChangeBundle.created_at.desc())
            .first()
        )
        if existing:
            _revalidate_draft_items(existing)
            return existing
    bundle = ChangeBundle(requester_user_id=user.id if user else None, status="draft")
    db.session.add(bundle)
    db.session.commit()
    return bundle


def list_my_bundles(user: Optional[User], *, limit: int = 200) -> List[Dict[str, Any]]:
    if not user:
        return []
    rows = (
        ChangeBundle.query.filter(ChangeBundle.requester_user_id == user.id)
        .order_by(ChangeBundle.created_at.desc())
        .limit(max(1, min(int(limit), 500)))
        .all()
    )
    return [serialize_bundle(row, include_items=False) for row in rows]


def list_bundles_for_approval(*, status: Optional[str] = None, limit: int = 200) -> List[Dict[str, Any]]:
    query = ChangeBundle.query
    if status:
        query = query.filter(ChangeBundle.status == status)
    else:
        query = query.filter(ChangeBundle.status != "draft")
    rows = query.order_by(ChangeBundle.created_at.desc()).limit(max(1, min(int(limit), 500))).all()
    return [serialize_bundle(row, include_items=False) for row in rows]


# ---------------------------------------------------------------------------
# Item building — the reuse hub
# ---------------------------------------------------------------------------

def _resource_from_yaml(yaml_content: str) -> Tuple[str, str, str]:
    """Best-effort (kind, name, namespace) of the primary workload in a manifest."""
    docs, _ = parse_yaml_documents(yaml_content)
    workload_kinds = {"Deployment", "StatefulSet", "DaemonSet", "Job", "CronJob"}
    primary = None
    for doc in docs:
        if isinstance(doc, dict) and doc.get("kind") in workload_kinds:
            primary = doc
            break
    if primary is None and docs:
        primary = next((d for d in docs if isinstance(d, dict)), None)
    if not isinstance(primary, dict):
        return "Deployment", "", ""
    meta = primary.get("metadata") or {}
    return primary.get("kind") or "Deployment", meta.get("name") or "", meta.get("namespace") or ""


def build_item_preview(action_type: str, payload: Dict[str, Any]) -> Dict[str, Any]:
    """Generate yaml_preview + execution descriptor for one staged change.

    Returns a dict with keys: yamlPreview, execution, resourceKind, resourceName,
    namespace (resolved). Raises ChangeBundleError on a generation failure.
    """
    payload = payload or {}
    namespace = (payload.get("namespace") or "").strip()
    resource_kind = (payload.get("resourceKind") or payload.get("resource_kind") or "Deployment").strip()
    resource_name = (payload.get("resourceName") or payload.get("resource_name") or "").strip()

    if action_type == "create_from_template":
        # The deploy wizard already resolves the template + answers into a manifest;
        # accept that YAML directly so we don't re-resolve. Fall back to resolving
        # from templateId + answers when only those are supplied.
        pre_yaml = payload.get("yaml") or ""
        if pre_yaml.strip():
            yaml_content = sanitize_for_apply(pre_yaml)
            kind, name, ns = _resource_from_yaml(yaml_content)
            return {
                "yamlPreview": yaml_content,
                "execution": {"mode": "apply"},
                "resourceKind": kind or resource_kind,
                "resourceName": name or resource_name,
                "namespace": ns or namespace,
            }

        from .template_resolver import resolve_template
        from .user_template_service import get_user_template_detail
        from .wizard_manifest_generator import generate_wizard_manifests
        from .wizard_templates import get_template

        template_id = str(payload.get("templateId") or payload.get("template_id") or "").strip()
        answers = payload.get("answers") or {}
        template = get_template(template_id) or get_user_template_detail(template_id)
        if not template:
            raise ChangeBundleError("Template not found.", 404)
        resolved, err = resolve_template(template, answers)
        if err:
            raise ChangeBundleError(err, 400)
        yaml_content, summary, gen_err = generate_wizard_manifests(resolved)
        if gen_err:
            raise ChangeBundleError(gen_err, 400)
        kind, name, ns = _resource_from_yaml(yaml_content)
        return {
            "yamlPreview": yaml_content,
            "execution": {"mode": "apply"},
            "resourceKind": kind,
            "resourceName": name or (summary or {}).get("appName") or resource_name,
            "namespace": ns or namespace,
        }

    if action_type == "change_image":
        from .manifest_generator import generate_manifests

        yaml_content, summary, gen_err = generate_manifests(payload)
        if gen_err:
            raise ChangeBundleError(gen_err, 400)
        kind, name, ns = _resource_from_yaml(yaml_content)
        return {
            "yamlPreview": yaml_content,
            "execution": {"mode": "apply"},
            "resourceKind": kind,
            "resourceName": name or resource_name,
            "namespace": ns or namespace,
        }

    if action_type in ("edit_deployment", "update_env", "update_resources", "update_hpa"):
        yaml_content = payload.get("yaml") or ""
        if not yaml_content.strip():
            raise ChangeBundleError("YAML content is required for this change.", 400)
        yaml_content = sanitize_for_apply(yaml_content)
        kind, name, ns = _resource_from_yaml(yaml_content)
        return {
            "yamlPreview": yaml_content,
            "execution": {"mode": "apply"},
            "resourceKind": kind or resource_kind,
            "resourceName": name or resource_name,
            "namespace": ns or namespace,
        }

    if action_type == "scale_replicas":
        if not resource_name:
            raise ChangeBundleError("A target deployment name is required to scale.", 400)
        try:
            replicas = int(payload.get("replicas"))
        except (TypeError, ValueError):
            raise ChangeBundleError("replicas must be a whole number.", 400)
        if replicas < 0 or replicas > 100:
            raise ChangeBundleError("replicas must be between 0 and 100.", 400)
        preview = f"# Scale {resource_kind.lower()}/{resource_name} to {replicas} replica(s)\n"
        return {
            "yamlPreview": preview,
            "execution": {"mode": "scale", "replicas": replicas},
            "resourceKind": resource_kind or "Deployment",
            "resourceName": resource_name,
            "namespace": namespace,
        }

    if action_type == "delete_deployment":
        if not resource_name:
            raise ChangeBundleError("A target resource name is required to delete.", 400)
        preview = f"# Delete {resource_kind.lower()}/{resource_name} in namespace {namespace}\n"
        return {
            "yamlPreview": preview,
            "execution": {"mode": "delete"},
            "resourceKind": resource_kind or "Deployment",
            "resourceName": resource_name,
            "namespace": namespace,
        }

    raise ChangeBundleError(f"Unsupported action type: {action_type}", 400)


def _revalidate_draft_items(bundle: ChangeBundle) -> None:
    """Refresh stored validation flags on a draft's items (rules may have changed)."""
    changed = False
    for item in bundle.items:
        prev = (item.validation_status, item.validation_message)
        _validate_item_now(item)
        if (item.validation_status, item.validation_message) != prev:
            changed = True
    if changed:
        db.session.commit()


def _validate_item_now(item: ChangeBundleItem) -> None:
    """Static (submission-time) validation. Live cluster state is re-checked at execution."""
    mode = (item.new_payload_json or {}).get("execution", {}).get("mode")
    if mode == "apply":
        result, err, _ = validate_yaml(
            item.yaml_preview or "", item.namespace, user=None, preview_mode=True
        )
        if not err:
            # Reject staging an image that's missing from its linked registry
            # (block enforcement) — early feedback; execution re-checks too.
            _checks, blocking, image_err = check_registry_images(item.yaml_preview or "")
            if blocking:
                err = image_err
        if err:
            item.validation_status = "invalid"
            item.validation_message = err
        else:
            item.validation_status = "valid"
            item.validation_message = None
    else:
        # scale / delete: identifiers already checked when building the preview.
        item.validation_status = "valid"
        item.validation_message = None


# ---------------------------------------------------------------------------
# Item CRUD
# ---------------------------------------------------------------------------

def add_item(user: Optional[User], bundle_id: int, payload: Dict[str, Any]) -> Dict[str, Any]:
    bundle = get_bundle_or_error(bundle_id)
    _assert_owner(bundle, user)
    _assert_editable(bundle)

    action_type = str(payload.get("actionType") or payload.get("action_type") or "").strip()
    if action_type not in ACTION_TYPES:
        raise ChangeBundleError(f"Unsupported action type: {action_type}", 400)

    cluster_id = str(payload.get("clusterId") or payload.get("cluster_id") or "").strip()
    cluster_name = str(payload.get("clusterName") or payload.get("cluster_name") or "").strip()
    namespace = str(payload.get("namespace") or "").strip()
    if not cluster_id:
        raise ChangeBundleError("A target cluster is required.", 400)
    # Prefer the cluster's real display name; the public id (e.g. "custom-6") is a
    # poor label. Resolve it from the cluster store, falling back to whatever the
    # caller sent and finally the id itself.
    cluster_name = _resolve_cluster_name(cluster_id, cluster_name)

    # Access is enforced per item on the backend before staging. Staging only
    # requires access to the target namespace — not deploy rights — because the
    # bundle goes through approval before anything is applied (the approval is the
    # authorization to execute). This lets a user propose changes for review even
    # if they cannot apply them directly.
    if user is not None and namespace and not can_access_namespace(user, cluster_id, namespace):
        log_audit(
            "forbidden_access_attempt",
            actor=user,
            target_type="change_bundle_item",
            target_id=f"{cluster_id}/{namespace}",
            details={"action": action_type},
        )
        raise ChangeBundleError(
            f"You do not have access to {namespace} in this cluster.", 403
        )

    built = build_item_preview(action_type, payload)
    resolved_namespace = built.get("namespace") or namespace
    if user is not None and resolved_namespace and not can_access_namespace(
        user, cluster_id, resolved_namespace
    ):
        raise ChangeBundleError(
            f"You do not have access to {resolved_namespace} in this cluster.", 403
        )

    next_position = max((i.position for i in bundle.items), default=-1) + 1
    item = ChangeBundleItem(
        bundle_id=bundle.id,
        position=next_position,
        action_type=action_type,
        cluster_id=cluster_id,
        cluster_name=cluster_name or cluster_id,
        namespace=resolved_namespace,
        resource_kind=built.get("resourceKind") or "Deployment",
        resource_name=built.get("resourceName") or "",
        old_payload_json=payload.get("oldPayload") or payload.get("old_payload"),
        new_payload_json={
            "input": {k: v for k, v in payload.items() if k not in ("oldPayload", "old_payload")},
            "execution": built.get("execution") or {"mode": "apply"},
        },
        yaml_preview=built.get("yamlPreview"),
    )
    _validate_item_now(item)
    db.session.add(item)
    bundle.updated_at = datetime.now(timezone.utc)
    db.session.commit()

    log_audit(
        ACTION_ITEM_ADDED,
        actor=user,
        target_type="change_bundle",
        target_id=str(bundle.id),
        details={
            "bundleId": bundle.id,
            "itemId": item.id,
            "actionType": action_type,
            "clusterId": cluster_id,
            "namespace": resolved_namespace,
            "resourceName": item.resource_name,
        },
    )
    return serialize_bundle(bundle)


def remove_item(user: Optional[User], bundle_id: int, item_id: int) -> Dict[str, Any]:
    bundle = get_bundle_or_error(bundle_id)
    _assert_owner(bundle, user)
    _assert_editable(bundle)
    item = ChangeBundleItem.query.filter_by(id=item_id, bundle_id=bundle.id).first()
    if not item:
        raise ChangeBundleError("Change item not found.", 404)
    db.session.delete(item)
    db.session.commit()
    return serialize_bundle(bundle)


def update_item(user: Optional[User], bundle_id: int, item_id: int, payload: Dict[str, Any]) -> Dict[str, Any]:
    """Replace a staged item in place by re-running the preview build."""
    bundle = get_bundle_or_error(bundle_id)
    _assert_owner(bundle, user)
    _assert_editable(bundle)
    item = ChangeBundleItem.query.filter_by(id=item_id, bundle_id=bundle.id).first()
    if not item:
        raise ChangeBundleError("Change item not found.", 404)

    action_type = str(
        payload.get("actionType") or payload.get("action_type") or item.action_type
    ).strip()
    if action_type not in ACTION_TYPES:
        raise ChangeBundleError(f"Unsupported action type: {action_type}", 400)
    namespace = str(payload.get("namespace") or item.namespace or "").strip()
    cluster_id = str(payload.get("clusterId") or payload.get("cluster_id") or item.cluster_id).strip()

    if user is not None and namespace and not can_access_namespace(user, cluster_id, namespace):
        raise ChangeBundleError(f"You do not have access to {namespace} in this cluster.", 403)

    built = build_item_preview(action_type, {**payload, "namespace": namespace})
    item.action_type = action_type
    item.cluster_id = cluster_id
    item.namespace = built.get("namespace") or namespace
    item.resource_kind = built.get("resourceKind") or item.resource_kind
    item.resource_name = built.get("resourceName") or item.resource_name
    item.yaml_preview = built.get("yamlPreview")
    item.new_payload_json = {
        "input": {k: v for k, v in payload.items() if k not in ("oldPayload", "old_payload")},
        "execution": built.get("execution") or {"mode": "apply"},
    }
    _validate_item_now(item)
    db.session.commit()
    return serialize_bundle(bundle)


def diff_item(user: Optional[User], bundle_id: int, item_id: int) -> Dict[str, Any]:
    """Live ``kubectl diff`` of a staged item against current cluster state.

    Meaningful for apply-mode items (template/edit/image/env/resources/hpa).
    Scale/delete items have no manifest to diff, so a summary is returned.
    """
    from ..k8s_provider import should_use_real_k8s
    from .deployment_service import diff_yaml

    bundle = get_bundle_or_error(bundle_id)
    item = ChangeBundleItem.query.filter_by(id=item_id, bundle_id=bundle.id).first()
    if not item:
        raise ChangeBundleError("Change item not found.", 404)

    mode = (item.new_payload_json or {}).get("execution", {}).get("mode", "apply")
    if mode != "apply":
        return {"mode": mode, "diff": (item.yaml_preview or "").strip() or "No diff available."}

    if not should_use_real_k8s(item.cluster_id):
        return {"mode": "apply", "diff": "[mock] No live cluster to diff against."}

    # user=None bypasses the apps:diff gate — this is a read-only preview for a
    # change the user is already authorized to stage in this namespace.
    data, err, code = diff_yaml(None, item.cluster_id, item.namespace, item.yaml_preview or "")
    if err:
        raise ChangeBundleError(err, code)
    diff_text = (data or {}).get("diff", "") or "No differences from the current cluster state."
    return {"mode": "apply", "diff": diff_text}


def delete_bundle(user: Optional[User], bundle_id: int) -> None:
    bundle = get_bundle_or_error(bundle_id)
    _assert_owner(bundle, user)
    if bundle.status not in ("draft", "rejected", "expired", "failed", "completed", "partially_failed"):
        raise ChangeBundleError("Only draft or finished bundles can be deleted.", 409)
    db.session.delete(bundle)
    db.session.commit()


# ---------------------------------------------------------------------------
# Submit / approve / reject
# ---------------------------------------------------------------------------

def _bundle_required_approvals(bundle: ChangeBundle) -> int:
    """Strictest per-cluster requirement across all the bundle's clusters."""
    clusters = {item.cluster_id for item in bundle.items}
    if not clusters:
        return 0
    return max(cluster_required_approvals(c) for c in clusters)


def submit_bundle(
    user: Optional[User],
    bundle_id: int,
    *,
    note: str = "",
    window_start: Any = None,
    window_end: Any = None,
    window_timezone: Any = None,
    stop_on_failure: bool = True,
) -> Dict[str, Any]:
    bundle = get_bundle_or_error(bundle_id)
    _assert_owner(bundle, user)
    _assert_editable(bundle)

    if not bundle.items:
        raise ChangeBundleError("Add at least one change before submitting.", 400)
    invalid = [i for i in bundle.items if i.validation_status == "invalid"]
    if invalid:
        raise ChangeBundleError(
            "One or more changes failed validation. Fix or remove them before submitting.", 400
        )

    start_dt = _parse_iso_datetime(window_start)
    end_dt = _parse_iso_datetime(window_end)
    if not start_dt or not end_dt:
        raise ChangeBundleError("A deployment window (start and end) is required.", 400)
    now = datetime.now(timezone.utc)
    if start_dt <= now:
        raise ChangeBundleError("The window start must be in the future.", 400)
    if end_dt <= start_dt:
        raise ChangeBundleError("The window end must be after the start.", 400)

    bundle.note = (note or "").strip() or bundle.note
    bundle.requested_start_time = start_dt
    bundle.requested_end_time = end_dt
    bundle.requested_window_timezone = _clean_timezone(window_timezone)
    bundle.stop_on_failure = bool(stop_on_failure)

    # Snapshot the approval audience + quorum at submission time.
    recipients, _ = _resolve_recipients_with_source()
    total = len(recipients)
    configured_required = _bundle_required_approvals(bundle)
    if configured_required <= 0:
        required = 0
    else:
        required = min(configured_required, total) if total else 1
    bundle.total_recipients = total
    bundle.required_approvals = required

    if required == 0:
        bundle.status = "approved"
        bundle.approved_at = now
        db.session.commit()
        _audit_bundle(ACTION_SUBMITTED, user, bundle, extra={"autoApproved": True})
        _audit_bundle(ACTION_APPROVED, None, bundle, extra={"auto": True})
        return serialize_bundle(bundle)

    bundle.status = "pending_approval"
    db.session.commit()
    _audit_bundle(ACTION_SUBMITTED, user, bundle)

    requester_name = (user.full_name or user.username) if user else "Unknown"
    email_result = _notify_approvers(bundle, requester_name, recipients)
    payload = serialize_bundle(bundle)
    payload["emailResult"] = email_result
    return payload


def _audit_bundle(action: str, actor: Optional[User], bundle: ChangeBundle, *, extra: Optional[Dict] = None) -> None:
    details = {
        "bundleId": bundle.id,
        "status": bundle.status,
        "itemCount": len(bundle.items),
        "clusters": sorted({i.cluster_id for i in bundle.items}),
    }
    if extra:
        details.update(extra)
    log_audit(action, actor=actor, target_type="change_bundle", target_id=str(bundle.id), details=details)


def _evaluate_quorum(bundle: ChangeBundle) -> Optional[str]:
    approvals, declines = _vote_tally(bundle)
    required = bundle.required_approvals or 1
    pool = max(bundle.total_recipients or 0, required)
    if approvals >= required:
        return "approved"
    if declines > (pool - required):
        return "rejected"
    return None


def _finalize(bundle: ChangeBundle, status: str, *, actor: Optional[User] = None, reason: Optional[str] = None) -> None:
    bundle.status = status
    if status == "approved":
        bundle.approved_by_user_id = actor.id if actor else None
        bundle.approved_at = datetime.now(timezone.utc)
    elif status == "rejected":
        bundle.approved_by_user_id = actor.id if actor else None
        bundle.rejection_reason = reason or bundle.rejection_reason
    db.session.commit()
    _audit_bundle(
        ACTION_APPROVED if status == "approved" else ACTION_REJECTED,
        actor,
        bundle,
        extra={"reason": reason} if reason else None,
    )


def record_vote(
    bundle_id: int,
    action: str,
    *,
    voter_email: str,
    actor: Optional[User] = None,
    reason: Optional[str] = None,
) -> Dict[str, Any]:
    """Record one approver's vote and re-evaluate quorum (shared by email + in-app)."""
    if action not in VALID_ACTIONS:
        raise ChangeBundleError("Unsupported action.", 400)
    bundle = get_bundle_or_error(bundle_id)
    if bundle.status != "pending_approval":
        raise ChangeBundleError(f"This bundle is already {bundle.status.replace('_', ' ')}.", 409)

    # A bundle can no longer be approved once its window has ended.
    end = bundle.requested_end_time
    if end is not None:
        if end.tzinfo is None:
            end = end.replace(tzinfo=timezone.utc)
        if end <= datetime.now(timezone.utc):
            _finalize(bundle, "expired", actor=actor, reason="window_passed")
            raise ChangeBundleError("The deployment window has already passed.", 409)

    email = (voter_email or "").strip().lower()
    if reason and action == "decline":
        bundle.rejection_reason = reason
    if not email:
        _finalize(bundle, "approved" if action == "approve" else "rejected", actor=actor, reason=reason)
        return serialize_bundle(bundle)

    decision = "approve" if action == "approve" else "decline"
    existing = ChangeBundleVote.query.filter_by(bundle_id=bundle.id, voter_email=email).first()
    if existing:
        existing.decision = decision
    else:
        db.session.add(ChangeBundleVote(bundle_id=bundle.id, voter_email=email, decision=decision))
    db.session.commit()
    db.session.refresh(bundle)

    outcome = _evaluate_quorum(bundle)
    if outcome:
        _finalize(bundle, outcome, actor=actor, reason=reason)
    return serialize_bundle(bundle)


def decide_bundle(
    bundle_id: int, action: str, *, actor: Optional[User] = None, reason: Optional[str] = None
) -> Dict[str, Any]:
    """In-app approve/reject by a management user (counts toward quorum)."""
    if action not in VALID_ACTIONS:
        raise ChangeBundleError("Unsupported action.", 400)
    actor_email = (getattr(actor, "email", "") or "").strip().lower() if actor else ""
    return record_vote(bundle_id, action, voter_email=actor_email, actor=actor, reason=reason)


# ---------------------------------------------------------------------------
# Email notification (per-recipient signed links, mirrors deployment requests)
# ---------------------------------------------------------------------------

def _action_urls(bundle: ChangeBundle, base_url: str, voter_email: str = "") -> Tuple[str, str]:
    approve = generate_action_token(bundle.id, "approve", voter_email)
    decline = generate_action_token(bundle.id, "decline", voter_email)
    return (
        f"{base_url}/api/change-bundles/{bundle.id}/approve?token={approve}",
        f"{base_url}/api/change-bundles/{bundle.id}/decline?token={decline}",
    )


def _build_email(bundle: ChangeBundle, requester_name: str, voter_email: str = "") -> Tuple[str, str, Optional[str]]:
    subject = f"KubeSight Change Bundle #{bundle.id} — approval requested"
    base_url = _public_base_url()
    window_label = _format_window(
        bundle.requested_start_time, bundle.requested_end_time, bundle.requested_window_timezone
    )
    item_lines = [
        f"  - [{i.action_type}] {i.cluster_name or i.cluster_id}/{i.namespace} "
        f"{i.resource_kind}/{i.resource_name}".rstrip("/")
        for i in sorted(bundle.items, key=lambda x: x.position)
    ]
    quorum_note = (
        f"This bundle needs {bundle.required_approvals} of {bundle.total_recipients} approval(s)."
        if (bundle.required_approvals or 1) > 1
        else ""
    )

    lines = [
        f"Requester: {requester_name}",
        f"Changes: {len(bundle.items)}",
    ]
    if window_label:
        lines.append(f"Deployment window: {window_label}")
    if bundle.note:
        lines += ["", "Note:", bundle.note]
    lines += ["", "Staged changes:", *item_lines, "", "Status: Pending approval"]
    if quorum_note:
        lines.append(quorum_note)
    if base_url:
        approve_url, decline_url = _action_urls(bundle, base_url, voter_email)
        lines += ["", "Take action (links expire in 72 hours):", f"Approve: {approve_url}", f"Reject: {decline_url}"]
    text_body = "\n".join(lines)

    items_html = "".join(
        f'<li style="margin:4px 0;color:#e2e8f0;font-size:13px;">'
        f'<span style="color:#38bdf8;">{_html_escape(i.action_type)}</span> — '
        f"{_html_escape(i.cluster_name or i.cluster_id)}/{_html_escape(i.namespace)} "
        f"{_html_escape(i.resource_kind)}/{_html_escape(i.resource_name)}</li>"
        for i in sorted(bundle.items, key=lambda x: x.position)
    )
    window_html = (
        f'<tr><td style="color:#94a3b8;padding:2px 16px 2px 0;">Window</td>'
        f'<td style="padding:2px 0;">{_html_escape(window_label)}</td></tr>'
        if window_label
        else ""
    )
    note_html = (
        f'<div style="margin-top:14px;padding:12px 14px;background:#0f172a;border:1px solid #334155;'
        f'border-radius:8px;color:#e2e8f0;font-size:13px;">{_html_escape(bundle.note)}</div>'
        if bundle.note
        else ""
    )
    buttons_html = ""
    if base_url:
        approve_url, decline_url = _action_urls(bundle, base_url, voter_email)
        buttons_html = f"""
        <table role="presentation" cellpadding="0" cellspacing="0" style="margin:22px 0;">
          <tr>
            <td style="padding-right:12px;"><a href="{_html_escape(approve_url)}"
              style="display:inline-block;background:#16a34a;color:#fff;text-decoration:none;font-weight:600;padding:11px 26px;border-radius:8px;font-size:14px;">✓ Approve</a></td>
            <td><a href="{_html_escape(decline_url)}"
              style="display:inline-block;background:#dc2626;color:#fff;text-decoration:none;font-weight:600;padding:11px 26px;border-radius:8px;font-size:14px;">✕ Reject</a></td>
          </tr>
        </table>
        <p style="color:#94a3b8;font-size:12px;margin:0;">These action links expire in 72 hours.</p>
        """
    html_body = f"""<!DOCTYPE html>
<html><body style="margin:0;padding:24px;background:#0f172a;font-family:-apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif;">
  <table role="presentation" cellpadding="0" cellspacing="0" width="100%" style="max-width:600px;margin:0 auto;background:#1e293b;border:1px solid #334155;border-radius:12px;">
    <tr><td style="padding:28px 32px;">
      <div style="color:#38bdf8;font-weight:600;letter-spacing:.05em;text-transform:uppercase;font-size:12px;">KubeSight</div>
      <h1 style="color:#e2e8f0;font-size:18px;margin:6px 0 18px;">Change Bundle #{bundle.id}</h1>
      <table role="presentation" cellpadding="0" cellspacing="0" style="color:#e2e8f0;font-size:14px;">
        <tr><td style="color:#94a3b8;padding:2px 16px 2px 0;">Requester</td><td style="padding:2px 0;">{_html_escape(requester_name)}</td></tr>
        <tr><td style="color:#94a3b8;padding:2px 16px 2px 0;">Changes</td><td style="padding:2px 0;">{len(bundle.items)}</td></tr>
        {window_html}
        <tr><td style="color:#94a3b8;padding:2px 16px 2px 0;">Status</td><td style="padding:2px 0;color:#fbbf24;">Pending approval</td></tr>
      </table>
      {note_html}
      <div style="margin-top:16px;color:#94a3b8;font-size:13px;">Staged changes</div>
      <ul style="margin:6px 0 0;padding-left:18px;">{items_html}</ul>
      {buttons_html}
    </td></tr>
  </table>
</body></html>"""
    return subject, text_body, html_body


def _notify_approvers(bundle: ChangeBundle, requester_name: str, recipients: List[str]) -> Dict[str, Any]:
    if not smtp_is_configured():
        return {"sent": 0, "skipped": True, "reason": "SMTP not configured."}
    if not recipients:
        return {"sent": 0, "skipped": True, "reason": "No approver email recipients configured."}
    sent, errors = 0, []
    for address in recipients:
        subject, body, html_body = _build_email(bundle, requester_name, voter_email=address)
        try:
            send_email(address, subject, body, html_body=html_body)
            sent += 1
        except EmailDeliveryError as exc:
            errors.append(f"{address}: {exc}")
    return {"sent": sent, "recipients": recipients, "errors": errors}
