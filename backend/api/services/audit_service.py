"""Audit log queries."""

from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional

from ..db import db
from ..models import AuditLog, user_display_loader
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
    # Actors print by name only: one batched user query, no access collections.
    entries = query.options(user_display_loader(AuditLog.actor)).limit(limit).all()
    return {"items": [audit_log_to_dict(e) for e in entries], "count": len(entries)}


# ---------------------------------------------------------------------------
# Retention (opt-in; nothing is deleted unless AUDIT_LOG_RETENTION_DAYS is set)
# ---------------------------------------------------------------------------

_PURGE_BATCH = 5000
_PURGE_MAX_BATCHES = 20


def audit_log_retention_days() -> Optional[int]:
    """``AUDIT_LOG_RETENTION_DAYS``; unset, invalid or <= 0 keeps everything."""
    raw = os.getenv("AUDIT_LOG_RETENTION_DAYS", "").strip()
    if not raw:
        return None
    try:
        days = int(raw)
    except ValueError:
        return None
    return days if days > 0 else None


def purge_old_audit_logs(days: Optional[int] = None) -> int:
    """Delete audit entries older than ``days`` (default: the env setting).

    In batches of 5,000, each its own short transaction, and at most 100,000
    rows per call so a first run over years of history cannot hold the
    scheduler for long; the rest goes on the next run. Returns rows deleted.
    """
    days = days if days is not None else audit_log_retention_days()
    if not days:
        return 0
    cutoff = datetime.now(timezone.utc) - timedelta(days=int(days))
    removed = 0
    for _ in range(_PURGE_MAX_BATCHES):
        ids = [
            row_id
            for (row_id,) in db.session.query(AuditLog.id)
            .filter(AuditLog.created_at < cutoff)
            .order_by(AuditLog.id.asc())
            .limit(_PURGE_BATCH)
        ]
        if not ids:
            break
        AuditLog.query.filter(AuditLog.id.in_(ids)).delete(synchronize_session=False)
        db.session.commit()
        removed += len(ids)
        if len(ids) < _PURGE_BATCH:
            break
    return removed
