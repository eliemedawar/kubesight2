"""Counts and problems per kind for the Resources page's side rail.

The rules for "this row needs attention" live here and in the frontend's
``lib/resourceTypes.js`` (``rowTone``). Keep the two in step: the rail badge
counts what the "Needs attention" filter then shows.
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Optional

# Statuses that mean a row is fine. Anything else on a status-bearing kind
# (CrashLoopBackOff, Pending, warning, Failed, ...) needs attention.
HEALTHY_STATUSES = frozenset(
    {
        "running",
        "completed",
        "complete",
        "succeeded",
        "active",
        "available",
        "healthy",
        "ready",
        "bound",
        "updated",
        "true",
    }
)

_CRITICAL_STATUS = re.compile(r"err|crash|backoff|oom|evicted|fail|exitcode|signal|lost")

# Kinds whose rows carry a kubectl-style status, and the field it is in.
_STATUS_FIELDS = {
    "pods": ("status",),
    "jobs": ("status",),
    "deployments": ("updateStatus", "status"),
    "statefulsets": ("updateStatus", "status"),
    "daemonsets": ("updateStatus", "status"),
}

# Kinds whose rows carry a computed ``health`` ("ok" | "warn" | "crit").
_HEALTH_KINDS = frozenset({"hpas", "pvcs", "pvs"})

# The order problems are listed in when they tie on severity.
_ATTENTION_KIND_ORDER = (
    "pods",
    "deployments",
    "statefulsets",
    "daemonsets",
    "jobs",
    "hpas",
    "pvcs",
    "pvs",
)

ATTENTION_LIMIT = 6


def status_tone(status: Any) -> str:
    """"" for a fine status, else "crit" or "warn"."""
    if status is None:
        return ""
    text = str(status).strip().lower()
    if not text or text in ("-", "—") or text in HEALTHY_STATUSES:
        return ""
    return "crit" if _CRITICAL_STATUS.search(text) else "warn"


def row_tone(list_key: str, row: Dict[str, Any], namespace: str) -> str:
    """How much a row needs attention: "", "warn" or "crit"."""
    if list_key in _STATUS_FIELDS:
        for field in _STATUS_FIELDS[list_key]:
            value = row.get(field)
            if value not in (None, "", "-"):
                return status_tone(value)
        return ""
    if list_key in _HEALTH_KINDS:
        # Volumes are cluster-wide; only those claimed from this namespace are
        # this namespace's problem.
        if list_key == "pvs" and row.get("claimNamespace") != namespace:
            return ""
        health = row.get("health")
        return health if health in ("warn", "crit") else ""
    if list_key == "events":
        return "warn" if str(row.get("type") or "").lower() == "warning" else ""
    return ""


def _status_text(list_key: str, row: Dict[str, Any]) -> str:
    if list_key in _STATUS_FIELDS:
        for field in _STATUS_FIELDS[list_key]:
            value = row.get(field)
            if value not in (None, "", "-"):
                return str(value)
    return str(row.get("status") or "")


def summarize_namespace_resources(
    namespace: str, lists: Dict[str, Any], limit: Optional[int] = ATTENTION_LIMIT
) -> Dict[str, Any]:
    """{"kinds": {key: {count, issues, tone[, total]}}, "attention": [...], "attentionTotal": n}"""
    kinds: Dict[str, Dict[str, Any]] = {}
    attention: List[Dict[str, Any]] = []
    for key, rows in lists.items():
        if key == "namespace" or not isinstance(rows, list):
            continue
        issues = 0
        worst = ""
        for row in rows:
            tone = row_tone(key, row, namespace)
            if not tone:
                continue
            issues += 1
            if tone == "crit" or not worst:
                worst = tone
            if key in _ATTENTION_KIND_ORDER:
                attention.append(
                    {
                        "listKey": key,
                        "name": row.get("name") or "",
                        "status": _status_text(key, row),
                        "tone": tone,
                    }
                )
        entry: Dict[str, Any] = {"count": len(rows), "issues": issues, "tone": worst}
        if key == "pvs":
            # The rail counts this namespace's volumes; the whole list is one click away.
            entry["total"] = len(rows)
            entry["count"] = sum(1 for row in rows if row.get("claimNamespace") == namespace)
        kinds[key] = entry

    attention.sort(
        key=lambda item: (
            0 if item["tone"] == "crit" else 1,
            _ATTENTION_KIND_ORDER.index(item["listKey"]),
            item["name"],
        )
    )
    total = len(attention)
    if limit is not None:
        attention = attention[:limit]
    return {
        "namespace": namespace,
        "kinds": kinds,
        "attention": attention,
        "attentionTotal": total,
    }
