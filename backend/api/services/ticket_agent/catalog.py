"""What Hermes may choose from, and what it is told about the ticket.

The catalog is the same set of targets the ticket form's dropdowns publish: the
provider's source cluster narrowed to its chosen namespaces and deployments,
plus its custom (Jenkins-only) environments. A decision naming anything outside
it is refused — Hermes can only pick a target the operator already exposed.
"""

from __future__ import annotations

import html
import re
from typing import Any, Dict, List, Optional

from ...models import ZohoDeploymentSnapshot, ZohoInboundTicket

# Enough for any real estate; bounded so a huge cluster can't blow the prompt.
MAX_TARGETS = 400
MAX_DESCRIPTION = 6000
MAX_COMMENTS = 10
MAX_COMMENT_CHARS = 1500

_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"[ \t\r\f\v]+")


def _custom_cluster() -> str:
    from ..zoho_sync_service import CUSTOM_SOURCE_CLUSTER

    return CUSTOM_SOURCE_CLUSTER


def targets(provider: str) -> List[ZohoDeploymentSnapshot]:
    """What this provider may deploy to: its source cluster's chosen namespaces
    (and deployments) plus its custom environments.

    Read LIVE from the cluster — the same builder the dropdown sync uses, which
    also mints the snapshot ids — so it holds even when tickets are free text
    and the dropdown sync never runs, and a deployment deleted since the last
    sync is not offered. Falls back to the stored snapshots if the cluster read
    fails.
    """
    try:
        from ..zoho_sync_service import _source_entries

        entries = _source_entries(None, provider=provider)
        ids = [e["id"] for e in entries][:MAX_TARGETS]
        if ids:
            by_id = {
                row.id: row
                for row in ZohoDeploymentSnapshot.query.filter(ZohoDeploymentSnapshot.id.in_(ids)).all()
            }
            return [by_id[i] for i in dict.fromkeys(ids) if i in by_id]
    except Exception:  # noqa: BLE001 — no source / cluster unreachable: use what is stored
        from ...db import db

        db.session.rollback()
    return _stored_targets(provider)


def _stored_targets(provider: str) -> List[ZohoDeploymentSnapshot]:
    """The snapshots last seen for this provider's source (no cluster read)."""
    from .. import ticketing_targets as tt

    source = (tt.source_cluster_id(provider) or "").strip()
    custom = _custom_cluster()
    namespaces = {n.casefold() for n in tt.namespace_list(provider)}
    selection = {str(k).casefold(): v for k, v in (tt.deployment_selection(provider) or {}).items()}
    custom_names = {n.casefold() for n in tt.custom_environment_names(provider)}

    clusters = [c for c in (source, custom) if c]
    if not clusters:
        return []
    rows = (
        ZohoDeploymentSnapshot.query.filter(ZohoDeploymentSnapshot.cluster_id.in_(clusters))
        .order_by(ZohoDeploymentSnapshot.namespace, ZohoDeploymentSnapshot.deployment_name)
        .all()
    )
    out: List[ZohoDeploymentSnapshot] = []
    seen = set()
    for row in rows:
        ns = (row.namespace or "").casefold()
        if row.cluster_id == custom:
            if custom_names and ns not in custom_names:
                continue
        else:
            # No namespace chosen yet = the whole source cluster (nothing to narrow by).
            if namespaces and ns not in namespaces:
                continue
            pick = selection.get(ns)
            if isinstance(pick, dict) and not pick.get("all", True):
                names = {str(n).casefold() for n in pick.get("names") or []}
                if (row.deployment_name or "").casefold() not in names:
                    continue
        key = (ns, (row.deployment_name or "").casefold())
        if key in seen:
            continue
        seen.add(key)
        out.append(row)
        if len(out) >= MAX_TARGETS:
            break
    return out


def catalog_entries(rows: List[ZohoDeploymentSnapshot]) -> List[Dict[str, Any]]:
    custom = _custom_cluster()
    entries = []
    for row in rows:
        is_custom = row.cluster_id == custom
        entries.append(
            {
                "environment": row.namespace,
                "application": row.deployment_name,
                # Custom environments are a Jenkins job, not a live Deployment:
                # there is nothing to restart and no variable to set.
                "allowedActions": ["deploy_image"] if is_custom else ["deploy_image", "set_env_var", "restart"],
            }
        )
    return entries


def find_target(
    rows: List[ZohoDeploymentSnapshot], environment: Optional[str], application: Optional[str]
) -> Optional[ZohoDeploymentSnapshot]:
    """Exact (case-insensitive) match on environment + application."""
    if not (environment and application):
        return None
    env, app = environment.strip().casefold(), application.strip().casefold()
    for row in rows:
        if (row.namespace or "").casefold() == env and (row.deployment_name or "").casefold() == app:
            return row
    return None


def is_custom(row: ZohoDeploymentSnapshot) -> bool:
    return row.cluster_id == _custom_cluster()


# ---------------------------------------------------------------------------
# Ticket context
# ---------------------------------------------------------------------------

def html_to_text(value: Any) -> str:
    """Desk descriptions are HTML. Keep line breaks, drop tags and entities."""
    if value is None:
        return ""
    text = str(value)
    text = re.sub(r"(?i)<\s*br\s*/?>|</\s*(p|div|li|tr|h\d)\s*>", "\n", text)
    text = _TAG_RE.sub("", text)
    text = html.unescape(text)
    lines = [_WS_RE.sub(" ", line).strip() for line in text.split("\n")]
    out, blank = [], False
    for line in lines:
        if not line:
            if not blank and out:
                out.append("")
            blank = True
            continue
        out.append(line)
        blank = False
    return "\n".join(out).strip()


def _adf_to_text(node: Any) -> str:
    """Jira Cloud descriptions are Atlassian Document Format — flatten the text."""
    if isinstance(node, str):
        return node
    if isinstance(node, list):
        return "".join(_adf_to_text(n) for n in node)
    if not isinstance(node, dict):
        return ""
    if node.get("type") == "text":
        return str(node.get("text") or "")
    inner = _adf_to_text(node.get("content") or [])
    if node.get("type") in ("paragraph", "heading", "listItem", "codeBlock", "blockquote"):
        return inner + "\n"
    if node.get("type") == "hardBreak":
        return "\n"
    return inner


def _payload_description(payload: Dict[str, Any]) -> str:
    for container in (payload, payload.get("data") if isinstance(payload.get("data"), dict) else None):
        if not isinstance(container, dict):
            continue
        for key in ("description", "descriptionText", "plainDescription", "body"):
            value = container.get(key)
            if value:
                return _adf_to_text(value) if isinstance(value, (dict, list)) else html_to_text(value)
    issue = payload.get("issue")
    if isinstance(issue, dict):
        fields = issue.get("fields") or {}
        value = fields.get("description")
        if value:
            return _adf_to_text(value) if isinstance(value, (dict, list)) else html_to_text(value)
    return ""


def _other_fields(payload: Dict[str, Any], skip: set) -> Dict[str, str]:
    """Remaining custom fields (``cf``) as short strings — context like country or priority."""
    out: Dict[str, str] = {}
    for key in ("cf", "customFields", "customfields"):
        nested = payload.get(key)
        if not isinstance(nested, dict):
            continue
        for name, value in nested.items():
            if name in skip or value in (None, "", [], {}):
                continue
            if isinstance(value, (str, int, float, bool)):
                out[str(name)[:80]] = str(value)[:300]
            if len(out) >= 30:
                return out
    return out


def ticket_context(ticket: ZohoInboundTicket) -> Dict[str, Any]:
    """Everything Hermes is shown about one ticket."""
    payload = ticket.payload if isinstance(ticket.payload, dict) else {}
    provider = ticket.provider or "zoho"
    description = _payload_description(payload)
    comments: List[str] = []

    # Desk webhooks often carry only the fields they were told to send; fetch
    # the description and the latest comments from the ticket itself.
    if provider == "zoho" and ticket.ticket_id:
        try:
            from ..zoho_sync_service import fetch_ticket_details

            details = fetch_ticket_details(ticket.ticket_id) or {}
            description = description or html_to_text(details.get("description"))
            comments = [html_to_text(c) for c in details.get("comments") or [] if c]
        except Exception:  # noqa: BLE001 — the payload alone is still worth reading
            pass

    structured = {
        "application": ticket.raw_app_value,
        "tag": ticket.tag,
        "variable": ticket.variable_name,
        "value": ticket.variable_value,
    }
    for key in ("cf_environment", "environment"):
        cf = payload.get("cf") if isinstance(payload.get("cf"), dict) else {}
        env = payload.get(key) or cf.get(key)
        if env:
            structured["environment"] = str(env)
            break
    if ticket.app_service_name:
        structured["resolvedTarget"] = ticket.app_service_name

    skip = {"cf_application", "cf_environment", "cf_tag", "cf_variable", "cf_value"}
    return {
        "number": ticket.ticket_number or ticket.ticket_id,
        "subject": (ticket.subject or "")[:500],
        "description": description[:MAX_DESCRIPTION],
        "structuredFields": {k: v for k, v in structured.items() if v not in (None, "")},
        "otherFields": _other_fields(payload, skip),
        "comments": [c[:MAX_COMMENT_CHARS] for c in comments if c][:MAX_COMMENTS],
    }
