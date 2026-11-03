"""One-click inbound webhook setup — KubeSight registers its own webhooks.

The counterpart of the CI "Set up in Bitbucket" button, for the ticketing
providers. Each provider gets two webhooks:

* **tickets** → ``/api/ticketing/<provider>/inbound`` — a new DevOps Request
  (Zoho ``Ticket_Add``, Jira ``jira:issue_created``; optionally the update
  event too). Deploy automation and Hermes take it from there.
* **comments** → ``/api/ticketing/<provider>/inbound/comment`` — a comment or
  an incoming email reply (Zoho ``Ticket_Comment_Add`` + inbound
  ``Ticket_Thread_Add``, Jira ``comment_created``) that wakes a parked Hermes
  task.

Neither Desk nor Jira's admin webhooks can send a custom header, so the shared
secret travels as ``?secret=`` — the fallback the inbound routes have always
accepted. It is never shown back: every URL this module returns is masked.

Same rules as the Bitbucket setup, for the same reasons:

* The secret is minted only when none is set, never rotated by setup — a
  hand-made rule (a Deluge function) using the current one keeps working.
* An existing webhook is adopted, not duplicated: matched by the id setup
  stored last time, then by URL, then by the inbound path at any address
  (so a webhook moves when a gateway URL is set, instead of a second one
  delivering every ticket twice).
* A loopback / unset public address is refused before anything is sent,
  unless a gateway base URL is set (the gateway's address is the reachable one
  by definition).
* Setup switches nothing on and never deletes a webhook. Webhooks that call
  KubeSight but are not the two it manages are listed, so the operator can
  remove them by hand.

KubeSight cannot see Desk workflow rules or Deluge functions through the API,
so one posting to the same URLs keeps doing so; duplicate deliveries are
harmless (a ticket is handled once per id, a comment once per comment id).
"""

from __future__ import annotations

import json
import secrets
import uuid
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import quote, urlsplit

from ...audit import log_audit
from ...db import db
from ...secret_encryption import decrypt_secret, encrypt_secret

KINDS = ("tickets", "comments")
KIND_LABELS = {"tickets": "New tickets", "comments": "Comments and replies"}
MAX_BASE_URL_CHARS = 512

# Paths an older, hand-made webhook may still be calling — adopted as ours.
_LEGACY_PATHS = {
    "zoho": {"tickets": ("/api/zoho/inbound",), "comments": ("/api/zoho/inbound/comment",)},
}

_ZOHO_TICKET_EVENTS = ("Ticket_Add", "Ticket_Update")
_JIRA_ISSUE_PREFIX = "jira:issue_"


class SetupError(Exception):
    """Setup could not start — nothing was sent to the ticketing system."""


# ---------------------------------------------------------------------------
# Addresses
# ---------------------------------------------------------------------------

def clean_base_url(value: Any) -> Optional[str]:
    """Validate a gateway base URL (blank = KubeSight's own). Raises ValueError."""
    raw = str(value or "").strip()
    if not raw:
        return None
    if len(raw) > MAX_BASE_URL_CHARS:
        raise ValueError(f"The gateway base URL is at most {MAX_BASE_URL_CHARS} characters.")
    parsed = urlsplit(raw)
    if (
        parsed.scheme not in ("http", "https")
        or not parsed.hostname
        or parsed.query
        or parsed.fragment
        or any(ch.isspace() for ch in raw)
    ):
        raise ValueError(
            "The gateway base URL must be a full http:// or https:// address without a query,"
            " e.g. https://tyk.example.com/kubesight."
        )
    return raw.rstrip("/")


def inbound_paths(provider_key: str) -> Dict[str, str]:
    return {
        "tickets": f"/api/ticketing/{provider_key}/inbound",
        "comments": f"/api/ticketing/{provider_key}/inbound/comment",
    }


def _base_url(row) -> str:
    if row.webhook_base_url:
        return row.webhook_base_url.rstrip("/")
    from ..ci.merge_checks import delivery

    return delivery.public_base_url()


def _reach_problem(row) -> str:
    if row.webhook_base_url:
        return ""
    from ..ci.webhook_triggers import _base_problem

    problem = _base_problem(_base_url(row))
    return f"{problem} Or set a gateway base URL if a gateway forwards to KubeSight." if problem else ""


def _strip(url: str) -> str:
    return str(url or "").split("?", 1)[0].rstrip("/")


def mask(url: str) -> str:
    """The URL with its secret hidden — the only form any response carries."""
    base, _, query = str(url or "").partition("?")
    if not query:
        return base
    parts = [
        "secret=••••" if part.startswith("secret=") else part for part in query.split("&")
    ]
    return f"{base}?{'&'.join(parts)}"


def _call_url(base: str, path: str, secret: str) -> str:
    return f"{base}{path}?secret={quote(secret, safe='')}"


def _registrations(row) -> Dict[str, Dict[str, str]]:
    try:
        value = json.loads(row.webhook_registrations or "{}")
    except ValueError:
        return {}
    return value if isinstance(value, dict) else {}


# ---------------------------------------------------------------------------
# Provider adapters — the only provider-specific part
# ---------------------------------------------------------------------------

class _Zoho:
    key = "zoho"
    name = "Zoho Desk"

    def __init__(self, row):
        from .. import zoho_client, zoho_sync_service

        self.row = row
        self.client = zoho_client
        self.cfg = zoho_sync_service._to_client_config(row)
        self.errors: Tuple[type, ...] = (zoho_client.ZohoError,)

    def list(self) -> List[Dict[str, Any]]:
        return [
            {
                "id": str(hook.get("id") or ""),
                "name": hook.get("name") or "",
                "url": hook.get("url") or "",
                "events": sorted((hook.get("subscriptions") or {}).keys()),
                "enabled": hook.get("isEnabled") is not False,
            }
            for hook in self.client.list_webhooks(self.cfg)
        ]

    def _filter(self) -> Optional[Dict[str, Any]]:
        dept = (self.row.department_id or "").strip()
        return {"departmentIds": [dept]} if dept else None

    def subscriptions(self, kind: str) -> Dict[str, Any]:
        only = self._filter()
        if kind == "tickets":
            subs: Dict[str, Any] = {"Ticket_Add": only}
            if self.row.webhook_send_updates:
                subs["Ticket_Update"] = dict(only) if only else None
            return subs
        return {
            "Ticket_Comment_Add": only,
            # Outgoing threads are the agents' own replies, not the requester.
            "Ticket_Thread_Add": {**(only or {}), "direction": "in"},
        }

    def wanted_events(self, kind: str) -> List[str]:
        return sorted(self.subscriptions(kind).keys())

    def body(self, kind: str, url: str) -> Dict[str, Any]:
        if not self.row.webhook_source_id:
            self.row.webhook_source_id = str(uuid.uuid4())
        return {
            "name": f"KubeSight — {KIND_LABELS[kind].lower()}",
            "description": "Created by KubeSight's one-click setup. Delivers to KubeSight.",
            "url": url,
            "subscriptions": self.subscriptions(kind),
            "ignoreSourceId": self.row.webhook_source_id,
            "isEnabled": True,
        }

    def create(self, body: Dict[str, Any]) -> str:
        return str(self.client.create_webhook(self.cfg, body).get("id") or "")

    def update(self, hook_id: str, body: Dict[str, Any]) -> None:
        self.client.update_webhook(self.cfg, hook_id, body)


class _Jira:
    key = "jira"
    name = "Jira"

    def __init__(self, row):
        from .. import jira_client, jira_sync_service

        self.row = row
        self.client = jira_client
        self.cfg = jira_sync_service._to_client_config(row)
        self.errors = (jira_client.JiraError,)

    def list(self) -> List[Dict[str, Any]]:
        return [
            {
                "id": self.client.webhook_id(hook),
                "name": hook.get("name") or "",
                "url": hook.get("url") or "",
                "events": sorted(hook.get("events") or []),
                "enabled": hook.get("enabled") is not False,
            }
            for hook in self.client.list_webhooks(self.cfg)
        ]

    def wanted_events(self, kind: str) -> List[str]:
        if kind == "tickets":
            events = ["jira:issue_created"]
            if self.row.webhook_send_updates:
                events.append("jira:issue_updated")
            return sorted(events)
        return ["comment_created"]

    def body(self, kind: str, url: str) -> Dict[str, Any]:
        project = (self.row.project_key or "").strip()
        return {
            "name": f"KubeSight — {KIND_LABELS[kind].lower()}",
            "url": url,
            "events": self.wanted_events(kind),
            "filters": {"issue-related-events-section": f'project = "{project}"'} if project else {},
            "excludeBody": False,
        }

    def create(self, body: Dict[str, Any]) -> str:
        return self.client.webhook_id(self.client.create_webhook(self.cfg, body))

    def update(self, hook_id: str, body: Dict[str, Any]) -> None:
        self.client.update_webhook(self.cfg, hook_id, body)


_ADAPTERS = {"zoho": _Zoho, "jira": _Jira}


def supported(provider_key: str) -> bool:
    return provider_key in _ADAPTERS


def _adapter(provider):
    cls = _ADAPTERS.get(provider.key)
    if cls is None:
        raise SetupError(f"KubeSight cannot register webhooks in {provider.name}.")
    return cls(provider.sync.get_or_create_config())


# ---------------------------------------------------------------------------
# Matching
# ---------------------------------------------------------------------------

def _match(
    provider_key: str, hooks: List[Dict[str, Any]], regs: Dict[str, Dict[str, str]], urls: Dict[str, str]
) -> Tuple[Dict[str, Optional[Dict[str, Any]]], List[Dict[str, Any]]]:
    """Which existing webhook is each kind's, and which others call KubeSight."""
    paths = inbound_paths(provider_key)
    legacy = _LEGACY_PATHS.get(provider_key, {})
    taken: set = set()
    found: Dict[str, Optional[Dict[str, Any]]] = {}

    def pick(test) -> Optional[Dict[str, Any]]:
        for hook in hooks:
            if hook["id"] not in taken and test(hook):
                taken.add(hook["id"])
                return hook
        return None

    for kind in KINDS:
        known = (regs.get(kind) or {}).get("id")
        suffixes = (paths[kind],) + tuple(legacy.get(kind, ()))
        found[kind] = (
            (pick(lambda h: h["id"] == known) if known else None)
            or pick(lambda h: _strip(h["url"]) == _strip(urls[kind]))
            or pick(lambda h: any(_strip(h["url"]).endswith(sfx) for sfx in suffixes))
        )

    ours = tuple(paths.values()) + tuple(p for group in legacy.values() for p in group)
    others = [
        {"id": hook["id"], "name": hook["name"], "url": mask(hook["url"]), "enabled": hook["enabled"]}
        for hook in hooks
        if hook["id"] not in taken and any(path in hook["url"] for path in ours)
    ]
    return found, others


def _state(kind: str, hook: Optional[Dict[str, Any]], url: str, wanted: List[str]) -> Dict[str, Any]:
    if hook is None:
        return {
            "kind": kind, "label": KIND_LABELS[kind], "exists": False, "inSync": False,
            "url": mask(url), "wantedEvents": wanted,
        }
    events = hook["events"]
    return {
        "kind": kind,
        "label": KIND_LABELS[kind],
        "exists": True,
        "id": hook["id"],
        "name": hook["name"],
        "url": mask(url),
        "registeredUrl": mask(hook["url"]),
        "urlMatches": hook["url"] == url,
        "enabled": hook["enabled"],
        "events": events,
        "wantedEvents": wanted,
        "missingEvents": [e for e in wanted if e not in events],
        "extraEvents": [e for e in events if e not in wanted],
        "inSync": hook["enabled"] and hook["url"] == url and sorted(events) == sorted(wanted),
    }


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def status(provider) -> Dict[str, Any]:
    """Whether the ticketing system already calls KubeSight. A live read, so
    "could not look" is an answer, not an error."""
    out: Dict[str, Any] = {"provider": provider.key, "supported": supported(provider.key)}
    if not out["supported"]:
        return {**out, "known": False, "reason": f"KubeSight cannot register webhooks in {provider.name}."}
    adapter = _adapter(provider)
    row = adapter.row
    out["gateway"] = bool(row.webhook_base_url)
    problem = _reach_problem(row)
    if problem:
        return {**out, "known": False, "reason": problem}
    secret = decrypt_secret(row.inbound_secret_encrypted or "")
    base = _base_url(row)
    urls = {kind: _call_url(base, path, secret) for kind, path in inbound_paths(provider.key).items()}
    try:
        hooks = adapter.list()
    except adapter.errors as exc:
        return {**out, "known": False, "reason": str(exc)}
    found, others = _match(provider.key, hooks, _registrations(row), urls)
    webhooks = [_state(kind, found[kind], urls[kind], adapter.wanted_events(kind)) for kind in KINDS]
    return {
        **out,
        "known": True,
        "secretSet": bool(secret),
        "webhooks": webhooks,
        "others": others,
        "inSync": bool(secret) and all(w["inSync"] for w in webhooks),
    }


def setup(provider, *, actor=None) -> Dict[str, Any]:
    """Create (or correct) both webhooks. Raises :class:`SetupError` when it
    cannot start; a failure on one webhook is reported, not raised, so the other
    still gets set up."""
    adapter = _adapter(provider)
    row = adapter.row
    problem = _reach_problem(row)
    if problem:
        raise SetupError(problem)
    try:
        hooks = adapter.list()
    except adapter.errors as exc:
        raise SetupError(str(exc)) from exc

    secret = decrypt_secret(row.inbound_secret_encrypted or "")
    minted = not secret
    if minted:
        secret = secrets.token_urlsafe(32)
        row.inbound_secret_encrypted = encrypt_secret(secret)
    base = _base_url(row)
    urls = {kind: _call_url(base, path, secret) for kind, path in inbound_paths(provider.key).items()}
    regs = _registrations(row)
    found, _others = _match(provider.key, hooks, regs, urls)

    results = []
    for kind in KINDS:
        hook = found[kind]
        wanted = adapter.wanted_events(kind)
        result: Dict[str, Any] = {"kind": kind, "label": KIND_LABELS[kind], "url": mask(urls[kind])}
        try:
            if hook is not None and _state(kind, hook, urls[kind], wanted)["inSync"]:
                result.update(action="unchanged", id=hook["id"])
            elif hook is not None:
                adapter.update(hook["id"], adapter.body(kind, urls[kind]))
                result.update(action="updated", id=hook["id"])
            else:
                result.update(action="created", id=adapter.create(adapter.body(kind, urls[kind])))
            regs[kind] = {"id": result["id"], "url": _strip(urls[kind])}
        except adapter.errors as exc:
            result.update(action="failed", error=str(exc))
        results.append(result)

    row.webhook_registrations = json.dumps(regs)
    db.session.add(row)
    db.session.commit()
    log_audit(
        f"ticketing_{provider.key}_webhooks_configured",
        actor=actor,
        target_type=f"{provider.key}_integration",
        target_id="1",
        details={
            "provider": provider.key,
            "secretGenerated": minted,
            "gateway": bool(row.webhook_base_url),
            "webhooks": [{k: r.get(k) for k in ("kind", "action", "id", "url")} for r in results],
        },
    )
    return {
        "ok": all(r["action"] != "failed" for r in results),
        "secretGenerated": minted,
        "webhooks": results,
        "status": status(provider),
    }


def resync_if_registered(provider, *, actor=None) -> Optional[Dict[str, Any]]:
    """After the secret changed: move registered webhooks to the new one, or
    every delivery is refused. None when setup never ran."""
    if not supported(provider.key):
        return None
    if not _registrations(provider.sync.get_or_create_config()):
        return None
    try:
        return setup(provider, actor=actor)
    except SetupError as exc:
        return {"ok": False, "error": str(exc)}


# ---------------------------------------------------------------------------
# Inbound — the providers' native webhook bodies
# ---------------------------------------------------------------------------

def unwrap_ticket_delivery(provider_key: str, payload: Any, row=None) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """The tickets inside one delivery to the tickets URL, and what was skipped.

    A Desk webhook POSTs a JSON array of ``{eventType, payload}``; only ticket
    events on the configured layout count (Desk cannot filter by layout, so
    every department ticket arrives). A Jira webhook is one object whose
    ``webhookEvent`` must be an issue event. Anything else — a Deluge function's
    flat body, an Automation rule's — is one ticket, as it always was.
    """
    if isinstance(payload, list):
        tickets: List[Dict[str, Any]] = []
        skipped: List[Dict[str, Any]] = []
        layout = str(getattr(row, "layout_id", "") or "").strip()
        for event in payload:
            if not isinstance(event, dict):
                continue
            kind = str(event.get("eventType") or "")
            ticket = event.get("payload") if isinstance(event.get("payload"), dict) else None
            ticket_id = str((ticket or {}).get("id") or "")
            if ticket is None or kind not in _ZOHO_TICKET_EVENTS:
                skipped.append({"eventType": kind, "ticketId": ticket_id, "reason": "Not a ticket event."})
            elif layout and ticket.get("layoutId") and str(ticket["layoutId"]) != layout:
                skipped.append({"eventType": kind, "ticketId": ticket_id, "reason": "Another layout."})
            else:
                tickets.append(ticket)
        return tickets, skipped
    if not isinstance(payload, dict):
        return [], [{"reason": "Expected a JSON object or a webhook event list."}]
    event = str(payload.get("webhookEvent") or "")
    if provider_key == "jira" and event and not event.startswith(_JIRA_ISSUE_PREFIX):
        return [], [{"webhookEvent": event, "reason": "Not an issue event."}]
    return [payload], []
