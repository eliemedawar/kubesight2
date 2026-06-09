"""Migrate legacy routing rules and settings into policy-receiver assignments."""

from __future__ import annotations

from typing import Any, Dict, List, Set

from sqlalchemy import inspect

from .db import db


def _policy_matches_rule(policy, rule) -> bool:
    if rule.severity and str(policy.severity or "").lower() != str(rule.severity).lower():
        return False
    if rule.cluster_id and policy.cluster_id != rule.cluster_id:
        return False
    if rule.namespace:
        scope = policy.scope if isinstance(policy.scope, dict) else {}
        scope_type = str(scope.get("type") or "cluster").lower()
        if scope_type == "cluster":
            return False
        if str(scope.get("namespace") or "").strip() != str(rule.namespace).strip():
            return False
    return True


def _rule_receiver_ids(rule) -> List[int]:
    merged: List[int] = []
    seen: Set[int] = set()
    for raw_id in (rule.email_receiver_ids or []) + (rule.webhook_receiver_ids or []):
        receiver_id = int(raw_id)
        if receiver_id in seen:
            continue
        seen.add(receiver_id)
        merged.append(receiver_id)
    return merged


def _find_or_create_receiver(
    *,
    name: str,
    receiver_type: str,
    email_address: str | None = None,
    url: str | None = None,
) -> Any:
    from .models import AlertRoutingReceiver

    if receiver_type == "email" and email_address:
        existing = AlertRoutingReceiver.query.filter_by(
            receiver_type="email",
            email_address=email_address,
        ).first()
        if existing:
            return existing
    elif url:
        existing = AlertRoutingReceiver.query.filter_by(
            receiver_type=receiver_type,
            url=url,
        ).first()
        if existing:
            return existing

    row = AlertRoutingReceiver(
        name=name,
        receiver_type=receiver_type,
        email_address=email_address,
        url=url,
        enabled=True,
    )
    db.session.add(row)
    db.session.flush()
    return row


def _attach_receivers_to_policy(policy, receiver_ids: List[int]) -> None:
    from .models import AlertRoutingReceiver

    if not receiver_ids:
        return
    existing_ids = {receiver.id for receiver in policy.notification_receivers}
    receivers = AlertRoutingReceiver.query.filter(
        AlertRoutingReceiver.id.in_(receiver_ids),
        AlertRoutingReceiver.enabled.is_(True),
    ).all()
    for receiver in receivers:
        if receiver.id not in existing_ids:
            policy.notification_receivers.append(receiver)


class _LegacyRoutingRule:
    def __init__(self, row: Dict[str, Any]) -> None:
        self.severity = row.get("severity")
        self.cluster_id = row.get("cluster_id")
        self.namespace = row.get("namespace")
        self.email_receiver_ids = row.get("email_receiver_ids") or []
        self.webhook_receiver_ids = row.get("webhook_receiver_ids") or []


def _notifications_dict(settings) -> Dict[str, Any]:
    if not settings:
        return {}
    notifications = settings.notifications
    return dict(notifications) if isinstance(notifications, dict) else {}


def migrate_routing_rules_to_policy_receivers() -> None:
    """Convert enabled routing rules into policy-receiver links where policies match (once)."""
    from sqlalchemy import text

    from .models import AlertPolicy, AppSettings

    settings = AppSettings.query.first()
    notifications = _notifications_dict(settings)
    if notifications.get("routingRulesMigrated"):
        return

    tables = inspect(db.engine).get_table_names()
    if "alert_routing_rules" in tables and "alert_policy_receivers" in tables:
        rows = db.session.execute(
            text(
                "SELECT severity, cluster_id, namespace, email_receiver_ids, webhook_receiver_ids "
                "FROM alert_routing_rules WHERE enabled = 1"
            )
        ).mappings().all()

        policies = AlertPolicy.query.all()
        for raw in rows:
            rule = _LegacyRoutingRule(dict(raw))
            receiver_ids = _rule_receiver_ids(rule)
            if not receiver_ids:
                continue
            for policy in policies:
                if not _policy_matches_rule(policy, rule):
                    continue
                _attach_receivers_to_policy(policy, receiver_ids)

    notifications["routingRulesMigrated"] = True
    if settings:
        settings.notifications = notifications
    db.session.commit()


def migrate_settings_routing_to_policy_receivers() -> None:
    """Create receivers from legacy AppSettings notification routing and assign to bare policies (once)."""
    from .models import AlertPolicy, AppSettings
    from .notification_routing import normalize_alert_routing

    settings = AppSettings.query.first()
    if not settings:
        return

    notifications = _notifications_dict(settings)
    if notifications.get("routingReceiversMigrated"):
        return

    routing = normalize_alert_routing(notifications.get("routing"))
    created_ids: List[int] = []

    email_cfg = routing.get("email") or {}
    if email_cfg.get("enabled"):
        address = str(email_cfg.get("address") or "").strip()
        if "@" in address:
            receiver = _find_or_create_receiver(
                name="Settings Email",
                receiver_type="email",
                email_address=address,
            )
            created_ids.append(receiver.id)

    slack_cfg = routing.get("slack") or {}
    if slack_cfg.get("enabled"):
        url = str(slack_cfg.get("webhookUrl") or "").strip()
        if url:
            receiver = _find_or_create_receiver(
                name="Settings Slack",
                receiver_type="slack",
                url=url,
            )
            created_ids.append(receiver.id)

    webhook_cfg = routing.get("webhook") or {}
    if webhook_cfg.get("enabled"):
        url = str(webhook_cfg.get("url") or "").strip()
        if url:
            receiver = _find_or_create_receiver(
                name="Settings Webhook",
                receiver_type="webhook",
                url=url,
            )
            created_ids.append(receiver.id)

    if created_ids:
        for policy in AlertPolicy.query.all():
            if list(policy.notification_receivers):
                continue
            _attach_receivers_to_policy(policy, created_ids)

    notifications["routingReceiversMigrated"] = True
    settings.notifications = notifications
    db.session.commit()


def migrate_delivery_log_columns() -> None:
    """Add policy and alert name columns to delivery logs."""
    from .migrate_rbac import _add_column_if_missing

    if "alert_delivery_logs" not in inspect(db.engine).get_table_names():
        return
    _add_column_if_missing("alert_delivery_logs", "alert_name", "VARCHAR(255)")
    _add_column_if_missing("alert_delivery_logs", "policy_id", "INTEGER")
    _add_column_if_missing("alert_delivery_logs", "policy_name", "VARCHAR(120)")


def run_alert_routing_migrations() -> None:
    migrate_delivery_log_columns()
    migrate_routing_rules_to_policy_receivers()
    migrate_settings_routing_to_policy_receivers()
