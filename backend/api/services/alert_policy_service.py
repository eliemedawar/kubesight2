"""CRUD and serialization for cluster alert policies."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

from ..access_engine import can_access_cluster, is_admin
from ..alert_policy_catalog import (
    CONDITION_LOGIC,
    NOTIFICATION_CHANNELS,
    SEVERITY_LEVELS,
    catalog_payload,
    validate_condition,
    validate_scope,
)
from ..audit import log_audit
from ..db import db
from ..models import AlertHistory, AlertPolicy, User


def _policy_dict(policy: AlertPolicy) -> Dict[str, Any]:
    return {
        "id": policy.id,
        "name": policy.name,
        "clusterId": policy.cluster_id,
        "description": policy.description,
        "enabled": policy.enabled,
        "severity": policy.severity,
        "conditionLogic": policy.condition_logic,
        "conditions": policy.conditions or [],
        "scope": policy.scope or {},
        "notificationChannels": policy.notification_channels or [],
        "createdByUserId": policy.created_by_user_id,
        "createdAt": policy.created_at.isoformat() if policy.created_at else None,
        "updatedAt": policy.updated_at.isoformat() if policy.updated_at else None,
    }


def _validate_payload(payload: Dict[str, Any], *, partial: bool = False) -> Optional[str]:
    if not partial or "name" in payload:
        if not str(payload.get("name") or "").strip():
            return "Policy name is required"
    if not partial or "clusterId" in payload:
        if not str(payload.get("clusterId") or "").strip():
            return "Cluster is required"
    if "severity" in payload or not partial:
        severity = str(payload.get("severity") or "warning").lower()
        if severity not in SEVERITY_LEVELS:
            return "Invalid severity level"
    if "conditionLogic" in payload or not partial:
        logic = str(payload.get("conditionLogic") or "any").lower()
        if logic not in CONDITION_LOGIC:
            return "Invalid condition logic"
    if "conditions" in payload or not partial:
        conditions = payload.get("conditions") or []
        if not isinstance(conditions, list) or not conditions:
            return "At least one condition is required"
        for condition in conditions:
            error = validate_condition(condition)
            if error:
                return error
    if "scope" in payload or not partial:
        error = validate_scope(payload.get("scope") or {})
        if error:
            return error
    if "notificationChannels" in payload or not partial:
        channels = payload.get("notificationChannels") or []
        if not isinstance(channels, list) or not channels:
            return "At least one notification channel is required"
        for channel in channels:
            channel_type = channel.get("channel") if isinstance(channel, dict) else channel
            if channel_type not in NOTIFICATION_CHANNELS:
                return f"Invalid notification channel: {channel_type}"
    return None


def list_policies(user: Optional[User], cluster_id: Optional[str] = None) -> List[Dict[str, Any]]:
    query = AlertPolicy.query.order_by(AlertPolicy.name.asc())
    if cluster_id:
        query = query.filter_by(cluster_id=cluster_id)
    policies = query.all()
    if not user or is_admin(user):
        return [_policy_dict(p) for p in policies]
    return [_policy_dict(p) for p in policies if can_access_cluster(user, p.cluster_id)]


def get_policy(user: Optional[User], policy_id: int) -> Tuple[Optional[Dict[str, Any]], Optional[str], int]:
    policy = AlertPolicy.query.get(policy_id)
    if not policy:
        return None, "Policy not found", 404
    if user and not is_admin(user) and not can_access_cluster(user, policy.cluster_id):
        return None, "Forbidden", 403
    return _policy_dict(policy), None, 200


def create_policy(user: Optional[User], payload: Dict[str, Any]) -> Tuple[Optional[Dict[str, Any]], Optional[str], int]:
    error = _validate_payload(payload)
    if error:
        return None, error, 400
    cluster_id = str(payload.get("clusterId")).strip()
    if user and not is_admin(user) and not can_access_cluster(user, cluster_id):
        return None, "Forbidden", 403

    now = datetime.now(timezone.utc)
    policy = AlertPolicy(
        name=str(payload.get("name")).strip(),
        cluster_id=cluster_id,
        description=(payload.get("description") or None),
        enabled=bool(payload.get("enabled", True)),
        severity=str(payload.get("severity") or "warning").lower(),
        condition_logic=str(payload.get("conditionLogic") or "any").lower(),
        conditions=payload.get("conditions") or [],
        scope=payload.get("scope") or {"type": "cluster"},
        notification_channels=payload.get("notificationChannels") or [{"channel": "dashboard"}],
        created_by_user_id=user.id if user else None,
        created_at=now,
        updated_at=now,
    )
    db.session.add(policy)
    db.session.commit()
    log_audit("alert_policy_created", actor=user, target_type="alert_policy", target_id=str(policy.id))
    return _policy_dict(policy), None, 201


def update_policy(
    user: Optional[User],
    policy_id: int,
    payload: Dict[str, Any],
) -> Tuple[Optional[Dict[str, Any]], Optional[str], int]:
    policy = AlertPolicy.query.get(policy_id)
    if not policy:
        return None, "Policy not found", 404
    if user and not is_admin(user) and not can_access_cluster(user, policy.cluster_id):
        return None, "Forbidden", 403

    error = _validate_payload(payload, partial=True)
    if error:
        return None, error, 400

    if "name" in payload:
        policy.name = str(payload.get("name")).strip()
    if "clusterId" in payload:
        cluster_id = str(payload.get("clusterId")).strip()
        if user and not is_admin(user) and not can_access_cluster(user, cluster_id):
            return None, "Forbidden", 403
        policy.cluster_id = cluster_id
    if "description" in payload:
        policy.description = payload.get("description") or None
    if "enabled" in payload:
        policy.enabled = bool(payload.get("enabled"))
    if "severity" in payload:
        policy.severity = str(payload.get("severity")).lower()
    if "conditionLogic" in payload:
        policy.condition_logic = str(payload.get("conditionLogic")).lower()
    if "conditions" in payload:
        policy.conditions = payload.get("conditions") or []
    if "scope" in payload:
        policy.scope = payload.get("scope") or {}
    if "notificationChannels" in payload:
        policy.notification_channels = payload.get("notificationChannels") or []

    policy.updated_at = datetime.now(timezone.utc)
    db.session.commit()
    log_audit("alert_policy_updated", actor=user, target_type="alert_policy", target_id=str(policy.id))
    return _policy_dict(policy), None, 200


def set_policy_enabled(
    user: Optional[User],
    policy_id: int,
    enabled: bool,
) -> Tuple[Optional[Dict[str, Any]], Optional[str], int]:
    policy = AlertPolicy.query.get(policy_id)
    if not policy:
        return None, "Policy not found", 404
    if user and not is_admin(user) and not can_access_cluster(user, policy.cluster_id):
        return None, "Forbidden", 403
    policy.enabled = bool(enabled)
    policy.updated_at = datetime.now(timezone.utc)
    db.session.commit()
    log_audit(
        "alert_policy_toggled",
        actor=user,
        target_type="alert_policy",
        target_id=str(policy.id),
        details={"enabled": policy.enabled},
    )
    return _policy_dict(policy), None, 200


def delete_policy(user: Optional[User], policy_id: int) -> Tuple[Optional[Dict[str, Any]], Optional[str], int]:
    policy = AlertPolicy.query.get(policy_id)
    if not policy:
        return None, "Policy not found", 404
    if user and not is_admin(user) and not can_access_cluster(user, policy.cluster_id):
        return None, "Forbidden", 403
    AlertHistory.query.filter_by(policy_id=policy.id).delete()
    db.session.delete(policy)
    db.session.commit()
    log_audit("alert_policy_deleted", actor=user, target_type="alert_policy", target_id=str(policy_id))
    return {"id": policy_id}, None, 200


def policy_stats(cluster_id: Optional[str] = None) -> Dict[str, Any]:
    history_query = AlertHistory.query
    policy_query = AlertPolicy.query.filter_by(enabled=True)
    if cluster_id:
        history_query = history_query.filter_by(cluster_id=cluster_id)
        policy_query = policy_query.filter_by(cluster_id=cluster_id)

    active = history_query.filter_by(status="active").all()
    by_severity = {"critical": 0, "warning": 0, "info": 0}
    by_cluster: Dict[str, int] = {}
    by_policy: Dict[str, int] = {}

    for row in active:
        sev = row.severity if row.severity in by_severity else "warning"
        by_severity[sev] += 1
        by_cluster[row.cluster_id] = by_cluster.get(row.cluster_id, 0) + 1
        by_policy[row.policy_name] = by_policy.get(row.policy_name, 0) + 1

    top_policies = sorted(
        [{"policyName": name, "count": count} for name, count in by_policy.items()],
        key=lambda item: item["count"],
        reverse=True,
    )[:5]

    return {
        "activeTotal": len(active),
        "critical": by_severity["critical"],
        "warning": by_severity["warning"],
        "info": by_severity["info"],
        "byCluster": [{"clusterId": k, "count": v} for k, v in sorted(by_cluster.items())],
        "bySeverity": by_severity,
        "topTriggeredPolicies": top_policies,
        "enabledPolicies": policy_query.count(),
    }


def get_catalog() -> Dict[str, Any]:
    return catalog_payload()
