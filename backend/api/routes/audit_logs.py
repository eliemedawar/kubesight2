from flask import Blueprint, request

from ..decorators import require_permission
from ..models import AuditLog
from ..response import success_response
from ..serializers import audit_log_to_dict

audit_bp = Blueprint("audit", __name__, url_prefix="/api/audit-logs")


@audit_bp.route("", methods=["GET"])
@require_permission("audit:view")
def list_audit_logs():
    limit = min(int(request.args.get("limit", 100)), 500)
    offset = max(int(request.args.get("offset", 0)), 0)
    query = AuditLog.query.order_by(AuditLog.created_at.desc())
    total = query.count()
    entries = query.offset(offset).limit(limit).all()
    return success_response(
        {
            "items": [audit_log_to_dict(e) for e in entries],
            "count": len(entries),
            "total": total,
            "offset": offset,
            "limit": limit,
        }
    )
