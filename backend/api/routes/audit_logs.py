import csv
import io
import json
import os
import threading
import time
import weakref
from datetime import datetime, timezone

from flask import Blueprint, Response, request
from sqlalchemy import false, func

from ..db import db
from ..decorators import require_permission
from ..models import AuditLog, User, user_display_loader
from ..response import error_response, success_response
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
# Each entry prints its actor's username: one batched user query per page
# instead of one access-collection-laden load per distinct actor.
_ACTOR_LOADER = user_display_loader(AuditLog.actor)


# COUNT(*) over the whole audit table is a full scan on PostgreSQL, and it ran
# on every page. It is now needed only when a page comes back full (a short page
# says exactly where the end is), and then kept briefly per database and per
# filter (a filtered view counts only what matches).
_TOTAL_TTL_SECONDS = float(os.getenv("AUDIT_LOG_TOTAL_CACHE_SECONDS", "30"))
# Filter combinations remembered per database; the oldest is dropped first.
_TOTAL_CACHE_KEYS = 64
_total_cache: "weakref.WeakKeyDictionary" = weakref.WeakKeyDictionary()
_total_lock = threading.Lock()


def _cached_total(query, key: tuple = ()) -> int:
    engine = db.engine
    now = time.monotonic()
    with _total_lock:
        hit = _total_cache.get(engine, {}).get(key)
    if hit is not None and hit[0] > now:
        return hit[1]
    total = query.order_by(None).count()
    with _total_lock:
        per_engine = _total_cache.setdefault(engine, {})
        per_engine.pop(key, None)
        per_engine[key] = (now + _TOTAL_TTL_SECONDS, total)
        while len(per_engine) > _TOTAL_CACHE_KEYS:
            per_engine.pop(next(iter(per_engine)))
    return total


class _BadFilter(ValueError):
    pass


def _parse_instant(name: str):
    """``from``/``to`` as ISO-8601. A value without a zone is UTC (that is what
    the API itself sends); one with a zone is converted to UTC. A bare date is
    that day's midnight UTC."""
    raw = (request.args.get(name) or "").strip()
    if not raw:
        return None
    try:
        value = datetime.fromisoformat(raw.replace("Z", "+00:00").replace("z", "+00:00"))
    except ValueError:
        raise _BadFilter(f"'{name}' must be an ISO-8601 date or time, e.g. 2026-10-12T08:00:00Z.")
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _filtered_query():
    """The AuditLog query for the request's filters, and a hashable key for them.

    ``from`` is inclusive and ``to`` exclusive (a day picked in the UI is sent
    as [that midnight, the next one)); both ride the ``created_at`` index.
    ``actor`` is a display name, as the page shows it: a username (resolved to
    its id first, so the (actor_user_id, created_at) index serves it), or
    "KubeSight automation" for entries with no human actor. ``action`` is a
    case-insensitive substring, as on the export.
    """
    start = _parse_instant("from")
    end = _parse_instant("to")
    if start and end and end <= start:
        raise _BadFilter("'to' must be after 'from'.")
    actor = (request.args.get("actor") or "").strip()
    action = (request.args.get("action") or "").strip().lower()

    query = AuditLog.query
    if start is not None:
        query = query.filter(AuditLog.created_at >= start)
    if end is not None:
        query = query.filter(AuditLog.created_at < end)
    if actor == AUTOMATION_ACTOR:
        query = query.filter(AuditLog.actor_user_id.is_(None))
    elif actor:
        user_id = db.session.query(User.id).filter(User.username == actor).scalar()
        if user_id is None and actor.isdigit():
            # An entry whose user is gone shows the id in place of a name.
            user_id = int(actor)
        query = query.filter(AuditLog.actor_user_id == user_id) if user_id is not None else query.filter(false())
    if action:
        query = query.filter(func.lower(AuditLog.action).like(f"%{action}%"))
    key = (
        start.isoformat() if start else None,
        end.isoformat() if end else None,
        actor or None,
        action or None,
    )
    return query, key


@audit_bp.route("", methods=["GET"])
@require_permission("audit:view")
def list_audit_logs():
    limit = min(int(request.args.get("limit", 100)), 500)
    offset = max(int(request.args.get("offset", 0)), 0)
    try:
        filtered, key = _filtered_query()
    except _BadFilter as exc:
        return error_response(str(exc), 400)
    query = filtered.order_by(AuditLog.created_at.desc())
    # One row past the page answers "is there more?" without counting.
    rows = query.options(_ACTOR_LOADER).offset(offset).limit(limit + 1).all()
    has_more = len(rows) > limit
    entries = rows[:limit]
    if has_more:
        # Approximate for up to the cache TTL; never below what was just seen.
        total = max(_cached_total(filtered, key), offset + len(rows))
    elif entries or offset == 0:
        # A short page ends the list: everything is accounted for.
        total = offset + len(entries)
    else:
        # Paged past the end: the offset says nothing about the size.
        total = _cached_total(filtered, key)
    return success_response(
        {
            "items": [audit_log_to_dict(e) for e in entries],
            "count": len(entries),
            "total": total,
            "hasMore": has_more,
            "offset": offset,
            "limit": limit,
        }
    )


@audit_bp.route("/actors", methods=["GET"])
@require_permission("audit:view")
def list_audit_actors():
    """What the actor filter offers: every username (the users table is small,
    so no scan of the audit table) and the automation label."""
    names = [name for (name,) in db.session.query(User.username).order_by(User.username).all() if name]
    return success_response({"items": [AUTOMATION_ACTOR, *names]})


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
    """Export audit entries as CSV, honoring the same filters as the page:
    from/to, actor and action in SQL (as on the list), and cluster in Python so
    its logic matches the UI exactly."""
    actor = (request.args.get("actor") or "").strip()
    cluster = (request.args.get("cluster") or "").strip()
    try:
        filtered, _ = _filtered_query()
    except _BadFilter as exc:
        return error_response(str(exc), 400)

    query = filtered.order_by(AuditLog.created_at.desc())
    rows = [
        audit_log_to_dict(e)
        for e in query.options(_ACTOR_LOADER).limit(_EXPORT_SCAN_LIMIT).all()
    ]

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
