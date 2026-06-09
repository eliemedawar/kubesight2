"""CRUD and serialization for cluster alert policies."""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

from ..access_engine import can_access_cluster, is_admin
from ..alert_policy_catalog import (
    CONDITION_LOGIC,
    DEFAULT_EVALUATION_INTERVAL_SECONDS,
    SEVERITY_LEVELS,
    catalog_payload,
    evaluation_interval_display,
    normalize_alert_type,
    normalize_evaluation_interval_seconds,
    normalize_log_config,
    validate_condition,
    validate_evaluation_interval,
    validate_log_config,
    normalize_scope,
    validate_scope,
)
from ..datetime_utils import serialize_utc_datetime
from ..audit import log_audit
from ..db import db
from ..models import AlertHistory, AlertPolicy, AlertRoutingReceiver, AlertRoutingReceiverGroup, LogAlertSeen, User


def _reset_policy_evaluation_state(policy: AlertPolicy) -> None:
    policy.last_evaluated_at = None
    policy.last_evaluation_result = None
    policy.last_measured_value = None
    policy.last_threshold = None
    policy.last_evaluation_error = None


def _trigger_policy_evaluation(policy: AlertPolicy, user: Optional[User] = None) -> None:
    if not policy.enabled:
        return
    from ..alert_notifier import dispatch_pending_policy_notifications
    from .alert_policy_evaluator import evaluate_policies_for_cluster

    try:
        evaluate_policies_for_cluster(policy.cluster_id, user=user, persist=True)
        dispatch_pending_policy_notifications(policy.id)
        db.session.refresh(policy)
    except Exception:
        logger.exception(
            "Immediate alert policy evaluation failed: policy_id=%s cluster=%s",
            policy.id,
            policy.cluster_id,
        )


def policy_show_on_dashboard(policy: AlertPolicy) -> bool:
    channels = policy.notification_channels or []
    return any(
        str(item.get("channel") if isinstance(item, dict) else item).strip().lower() == "dashboard"
        for item in channels
    )


def _dashboard_channels_from_payload(payload: Dict[str, Any], *, default: bool = True) -> List[Dict[str, str]]:
    if "showOnDashboard" in payload:
        return [{"channel": "dashboard"}] if bool(payload.get("showOnDashboard")) else []
    channels = payload.get("notificationChannels")
    if channels is None:
        return [{"channel": "dashboard"}] if default else []
    if not isinstance(channels, list):
        return [{"channel": "dashboard"}] if default else []
    return (
        [{"channel": "dashboard"}]
        if any(
            str(item.get("channel") if isinstance(item, dict) else item).strip().lower() == "dashboard"
            for item in channels
        )
        else []
    )


def _receiver_destination(receiver: AlertRoutingReceiver) -> str:
    if receiver.receiver_type == "email":
        return receiver.email_address or ""
    return receiver.url or ""


def list_receiver_groups_for_policy_catalog() -> List[Dict[str, Any]]:
    rows = (
        AlertRoutingReceiverGroup.query.filter_by(enabled=True)
        .order_by(AlertRoutingReceiverGroup.name.asc())
        .all()
    )
    return [
        {
            "id": row.id,
            "name": row.name,
            "description": row.description or "",
            "memberCount": len(list(row.members or [])),
            "receiverNames": [member.name for member in list(row.members or [])],
        }
        for row in rows
    ]


def list_receivers_for_policy_catalog() -> List[Dict[str, Any]]:
    rows = (
        AlertRoutingReceiver.query.filter_by(enabled=True)
        .order_by(AlertRoutingReceiver.name.asc())
        .all()
    )
    return [
        {
            "id": row.id,
            "name": row.name,
            "type": row.receiver_type,
            "destination": _receiver_destination(row),
        }
        for row in rows
    ]


def _normalize_receiver_ids(payload: Dict[str, Any], *, existing: Optional[AlertPolicy] = None) -> List[int]:
    if "receiverIds" in payload:
        raw = payload.get("receiverIds") or []
    elif existing is not None:
        return [receiver.id for receiver in existing.notification_receivers]
    else:
        raw = []
    if not isinstance(raw, list):
        return []
    result: List[int] = []
    seen: set[int] = set()
    for item in raw:
        receiver_id = int(item)
        if receiver_id in seen:
            continue
        seen.add(receiver_id)
        result.append(receiver_id)
    return result


def _normalize_receiver_group_ids(payload: Dict[str, Any], *, existing: Optional[AlertPolicy] = None) -> List[int]:
    if "receiverGroupIds" in payload:
        raw = payload.get("receiverGroupIds") or []
    elif existing is not None:
        return [group.id for group in existing.notification_receiver_groups]
    else:
        raw = []
    if not isinstance(raw, list):
        return []
    result: List[int] = []
    seen: set[int] = set()
    for item in raw:
        group_id = int(item)
        if group_id in seen:
            continue
        seen.add(group_id)
        result.append(group_id)
    return result


def _sync_policy_receiver_groups(policy: AlertPolicy, group_ids: List[int]) -> None:
    if not group_ids:
        policy.notification_receiver_groups = []
        return
    groups = (
        AlertRoutingReceiverGroup.query.filter(
            AlertRoutingReceiverGroup.id.in_(group_ids),
            AlertRoutingReceiverGroup.enabled.is_(True),
        )
        .order_by(AlertRoutingReceiverGroup.name.asc())
        .all()
    )
    policy.notification_receiver_groups = groups


def _sync_policy_receivers(policy: AlertPolicy, receiver_ids: List[int]) -> None:
    if not receiver_ids:
        policy.notification_receivers = []
        return
    receivers = (
        AlertRoutingReceiver.query.filter(
            AlertRoutingReceiver.id.in_(receiver_ids),
            AlertRoutingReceiver.enabled.is_(True),
        )
        .order_by(AlertRoutingReceiver.name.asc())
        .all()
    )
    policy.notification_receivers = receivers


def _policy_dict(policy: AlertPolicy) -> Dict[str, Any]:
    receivers = list(policy.notification_receivers or [])
    groups = list(policy.notification_receiver_groups or [])
    return {
        "id": policy.id,
        "name": policy.name,
        "clusterId": policy.cluster_id,
        "description": policy.description,
        "enabled": policy.enabled,
        "alertType": normalize_alert_type(getattr(policy, "alert_type", "metric")),
        "severity": policy.severity,
        "conditionLogic": policy.condition_logic,
        "conditions": policy.conditions or [],
        "logConfig": normalize_log_config(policy.log_config),
        "scope": normalize_scope(policy.scope),
        "showOnDashboard": policy_show_on_dashboard(policy),
        "receiverIds": [receiver.id for receiver in receivers],
        "receiverNames": [receiver.name for receiver in receivers],
        "receiverGroupIds": [group.id for group in groups],
        "receiverGroupNames": [group.name for group in groups],
        "evaluationIntervalSeconds": int(policy.evaluation_interval_seconds or DEFAULT_EVALUATION_INTERVAL_SECONDS),
        "evaluationIntervalLabel": evaluation_interval_display(
            int(policy.evaluation_interval_seconds or DEFAULT_EVALUATION_INTERVAL_SECONDS)
        ),
        "lastEvaluatedAt": serialize_utc_datetime(policy.last_evaluated_at),
        "lastResult": policy.last_evaluation_result,
        "lastMeasuredValue": policy.last_measured_value,
        "lastThreshold": policy.last_threshold,
        "lastEvaluationError": policy.last_evaluation_error,
        "createdByUserId": policy.created_by_user_id,
        "createdAt": policy.created_at.isoformat() if policy.created_at else None,
        "updatedAt": policy.updated_at.isoformat() if policy.updated_at else None,
    }


def _validate_payload(
    payload: Dict[str, Any],
    *,
    partial: bool = False,
    existing: Optional[AlertPolicy] = None,
) -> Optional[str]:
    if "alertType" in payload:
        alert_type = normalize_alert_type(payload.get("alertType"))
    elif existing is not None:
        alert_type = normalize_alert_type(getattr(existing, "alert_type", "metric"))
    else:
        alert_type = "metric" if partial else normalize_alert_type(payload.get("alertType", "metric"))

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
    if alert_type == "metric":
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
    elif alert_type == "log":
        if "logConfig" in payload or not partial:
            error = validate_log_config(payload.get("logConfig") or {})
            if error:
                return error
    if "scope" in payload or not partial:
        error = validate_scope(payload.get("scope") or {})
        if error:
            return error
    if "receiverIds" in payload:
        receiver_ids = _normalize_receiver_ids(payload)
        if receiver_ids:
            found = {
                row.id
                for row in AlertRoutingReceiver.query.filter(
                    AlertRoutingReceiver.id.in_(receiver_ids),
                    AlertRoutingReceiver.enabled.is_(True),
                ).all()
            }
            missing = [rid for rid in receiver_ids if rid not in found]
            if missing:
                return "One or more selected receivers are invalid or disabled."
    if "receiverGroupIds" in payload:
        group_ids = _normalize_receiver_group_ids(payload)
        if group_ids:
            found = {
                row.id
                for row in AlertRoutingReceiverGroup.query.filter(
                    AlertRoutingReceiverGroup.id.in_(group_ids),
                    AlertRoutingReceiverGroup.enabled.is_(True),
                ).all()
            }
            missing = [gid for gid in group_ids if gid not in found]
            if missing:
                return "One or more selected receiver groups are invalid or disabled."
    if "evaluationIntervalSeconds" in payload:
        try:
            interval = int(payload.get("evaluationIntervalSeconds"))
        except (TypeError, ValueError):
            return "Invalid evaluation interval"
        error = validate_evaluation_interval(interval)
        if error:
            return error
    return None


def list_policies(user: Optional[User], cluster_id: Optional[str] = None) -> List[Dict[str, Any]]:
    if cluster_id:
        from .alert_policy_evaluator import evaluate_policies_for_cluster

        evaluate_policies_for_cluster(cluster_id, user=user, persist=True)

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
    alert_type = normalize_alert_type(payload.get("alertType", "metric"))
    policy = AlertPolicy(
        name=str(payload.get("name")).strip(),
        cluster_id=cluster_id,
        description=(payload.get("description") or None),
        enabled=bool(payload.get("enabled", True)),
        alert_type=alert_type,
        severity=str(payload.get("severity") or "warning").lower(),
        condition_logic=str(payload.get("conditionLogic") or "any").lower(),
        conditions=(payload.get("conditions") or []) if alert_type == "metric" else [],
        log_config=normalize_log_config(payload.get("logConfig")) if alert_type == "log" else None,
        scope=normalize_scope(payload.get("scope")),
        notification_channels=_dashboard_channels_from_payload(payload),
        evaluation_interval_seconds=normalize_evaluation_interval_seconds(
            payload.get("evaluationIntervalSeconds", DEFAULT_EVALUATION_INTERVAL_SECONDS)
        ),
        created_by_user_id=user.id if user else None,
        created_at=now,
        updated_at=now,
    )
    db.session.add(policy)
    db.session.flush()
    _sync_policy_receivers(policy, _normalize_receiver_ids(payload))
    _sync_policy_receiver_groups(policy, _normalize_receiver_group_ids(payload))
    _reset_policy_evaluation_state(policy)
    db.session.commit()
    _trigger_policy_evaluation(policy, user=user)
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

    error = _validate_payload(payload, partial=True, existing=policy)
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
    if "alertType" in payload:
        policy.alert_type = normalize_alert_type(payload.get("alertType"))
    if "severity" in payload:
        policy.severity = str(payload.get("severity")).lower()
    if policy.alert_type == "metric":
        if "conditionLogic" in payload:
            policy.condition_logic = str(payload.get("conditionLogic")).lower()
        if "conditions" in payload:
            policy.conditions = payload.get("conditions") or []
        if "alertType" in payload:
            policy.log_config = None
    else:
        if "logConfig" in payload:
            policy.log_config = normalize_log_config(payload.get("logConfig"))
        elif "alertType" in payload and not policy.log_config:
            policy.log_config = normalize_log_config({})
        if "alertType" in payload:
            policy.conditions = []
    if "scope" in payload:
        policy.scope = normalize_scope(payload.get("scope"))
    if "showOnDashboard" in payload or "notificationChannels" in payload:
        policy.notification_channels = _dashboard_channels_from_payload(
            payload,
            default=policy_show_on_dashboard(policy),
        )
    if "receiverIds" in payload:
        _sync_policy_receivers(policy, _normalize_receiver_ids(payload))
    if "receiverGroupIds" in payload:
        _sync_policy_receiver_groups(policy, _normalize_receiver_group_ids(payload))
    if "evaluationIntervalSeconds" in payload:
        interval = normalize_evaluation_interval_seconds(payload.get("evaluationIntervalSeconds"))
        policy.evaluation_interval_seconds = interval

    _reset_policy_evaluation_state(policy)
    policy.updated_at = datetime.now(timezone.utc)
    db.session.commit()
    _trigger_policy_evaluation(policy, user=user)
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
    LogAlertSeen.query.filter_by(policy_id=policy.id).delete()
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
    return catalog_payload(
        receivers=list_receivers_for_policy_catalog(),
        receiver_groups=list_receiver_groups_for_policy_catalog(),
    )
