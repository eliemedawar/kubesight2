from __future__ import annotations

from typing import Any, Dict, Optional

from flask import request

from .db import db
from .models import AuditLog, User


def log_audit(
    action: str,
    *,
    actor: Optional[User] = None,
    actor_user_id: Optional[int] = None,
    target_type: Optional[str] = None,
    target_id: Optional[str] = None,
    details: Optional[Dict[str, Any]] = None,
    commit: bool = True,
) -> AuditLog:
    entry = AuditLog(
        actor_user_id=actor_user_id or (actor.id if actor else None),
        action=action,
        target_type=target_type,
        target_id=str(target_id) if target_id is not None else None,
        details={
            **(details or {}),
            "ip": request.remote_addr if request else None,
        },
    )
    db.session.add(entry)
    if commit:
        db.session.commit()
    return entry
