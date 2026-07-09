import csv
import io
import json

from flask import Blueprint, Response, request
from sqlalchemy import func

from ..decorators import require_permission
from ..models import AuditLog
from ..response import success_response
from ..serializers import audit_log_to_dict

audit_bp = Blueprint("audit", __name__, url_prefix="/api/audit-logs")

# Label shown (and filtered/exported) for system-initiated actions — audit rows
# with no human actor (scheduler, webhook, deploy automation). Mirrors the
# frontend so the actor filter + export agree.
AUTOMATION_ACTOR = "KubeSight automation"

# How an audit entry references a cluster (mirrors AuditLogsPage.clusterOf).
_CLUSTER_PREFIXED_TARGETS = {"namespace", "pod", "deployment", "service", "resource"}
# Cap on rows scanned for an export so a huge history can't exhaust memory.
_EXPORT_SCAN_LIMIT = 10000


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


def _display_actor(entry: dict) -> str:
    return (
        entry.get("actorUsername")
        or (str(entry["actorUserId"]) if entry.get("actorUserId") else "")
        or AUTOMATION_ACTOR
    )


def _cluster_of(entry: dict):
    details = entry.get("details") or {}
    if details.get("clusterId"):
        return str(details["clusterId"])
    if details.get("cluster"):
        return str(details["cluster"])
    target_id = entry.get("targetId") or ""
    if entry.get("targetType") == "cluster" and target_id:
        return target_id
    if entry.get("targetType") in _CLUSTER_PREFIXED_TARGETS and "/" in target_id:
        return target_id.split("/")[0]
    return None


@audit_bp.route("/export", methods=["GET"])
@require_permission("audit:view")
def export_audit_logs():
    """Export audit entries as CSV, honoring the same actor/action/cluster
    filters as the page. Action is filtered in SQL (substring); actor and
    cluster are matched in Python so the logic matches the UI exactly."""
    actor = (request.args.get("actor") or "").strip()
    action = (request.args.get("action") or "").strip().lower()
    cluster = (request.args.get("cluster") or "").strip()

    query = AuditLog.query.order_by(AuditLog.created_at.desc())
    if action:
        query = query.filter(func.lower(AuditLog.action).like(f"%{action}%"))
    rows = [audit_log_to_dict(e) for e in query.limit(_EXPORT_SCAN_LIMIT).all()]

    filtered = []
    for entry in rows:
        if actor and _display_actor(entry) != actor:
            continue
        if cluster and _cluster_of(entry) != cluster:
            continue
        filtered.append(entry)

    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(["Date/Time (UTC)", "Actor", "Action", "Target Type", "Target ID", "Details"])
    for entry in filtered:
        writer.writerow(
            [
                entry.get("createdAt") or "",
                _display_actor(entry),
                entry.get("action") or "",
                entry.get("targetType") or "",
                entry.get("targetId") or "",
                json.dumps(entry.get("details") or {}, ensure_ascii=False),
            ]
        )

    # Prepend a UTF-8 BOM so Excel opens non-ASCII cleanly.
    return Response(
        "﻿" + buffer.getvalue(),
        mimetype="text/csv",
        headers={"Content-Disposition": "attachment; filename=audit-logs.csv"},
    )
