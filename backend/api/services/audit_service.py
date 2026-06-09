"""Audit log queries."""

from __future__ import annotations

from typing import Any, Dict, Optional

from ..models import AuditLog
from ..serializers import audit_log_to_dict


def list_audit_logs(
    *,
    limit: int = 100,
    action: Optional[str] = None,
    actor_username: Optional[str] = None,
) -> Dict[str, Any]:
    query = AuditLog.query.order_by(AuditLog.created_at.desc())
    if action:
        query = query.filter_by(action=action)
    if actor_username:
        query = query.join(AuditLog.actor).filter_by(username=actor_username)
    entries = query.limit(limit).all()
    return {"items": [audit_log_to_dict(e) for e in entries], "count": len(entries)}
