"""Low-level Zoho Desk API client for the DevOps Request field-sync integration.

Just enough of the Desk REST API to (a) mint an access token from the stored
refresh token, (b) read a layout-scoped picklist field's ``allowedValues``, and
(c) replace that whole list (create/rename/delete = PATCH the full array). See
``DEVOPS-REQUEST-FIELD-SYNC-CONFIG.md`` §3–4 for the protocol this implements.

Only the standard library is used (``urllib``), matching the rest of the backend
(see ``registry_client`` / ``alert_routing_service``) — no new dependency. All
inputs are plain values; the caller (``zoho_sync_service``) is responsible for
decrypting the client secret / refresh token before calling in.
"""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Set, Tuple
from urllib.parse import urlencode

from ..ttl_cache import TTLCache

_TIMEOUT_SECONDS = 20

# Layout reads back the whole DevOps Request layout from Zoho over the internet
# (~0.5-3s) and the tab reads it on every open, so cache it briefly with
# single-flight + stale-while-revalidate. Every field mutation invalidates it.
# The compute closure is pure urllib (no Flask/app context), so the background
# stale refresh is safe.
_LAYOUT_CACHE = TTLCache("zoho-layout")
_LAYOUT_TTL_SECONDS = int(os.getenv("ZOHO_LAYOUT_CACHE_TTL_SECONDS", "60"))
_LAYOUT_STALE_SERVE_SECONDS = int(os.getenv("ZOHO_LAYOUT_CACHE_STALE_SECONDS", "600"))


def invalidate_layout_cache() -> None:
    """Drop cached layout reads — call after any write that changes the layout."""
    _LAYOUT_CACHE.invalidate("layout:")


class ZohoError(Exception):
    """A Zoho Desk API call failed. ``status`` mirrors the HTTP code when known."""

    def __init__(self, message: str, status: Optional[int] = None):
        super().__init__(message)
        self.status = status


@dataclass
class ZohoConfig:
    """Everything a Desk call needs, with secrets already decrypted."""

    api_base: str
    accounts_base: str
    token_endpoint: str
    org_id: str
    layout_id: str
    app_field_id: str
    client_id: str
    client_secret: str
    refresh_token: str
    environment_field_id: str = ""


# ---------------------------------------------------------------------------
# HTTP helper
# ---------------------------------------------------------------------------

def _request(
    method: str,
    url: str,
    *,
    headers: Optional[Dict[str, str]] = None,
    body: Optional[bytes] = None,
) -> Tuple[int, Dict[str, Any]]:
    """Do one HTTP call. Returns (status, parsed-json). Raises ZohoError on transport failure."""
    req = urllib.request.Request(url, data=body, headers=headers or {}, method=method)
    try:
        with urllib.request.urlopen(req, timeout=_TIMEOUT_SECONDS) as resp:
            raw = resp.read().decode("utf-8", "replace")
            return resp.status, (json.loads(raw) if raw.strip() else {})
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", "replace") if exc.fp else ""
        try:
            parsed = json.loads(raw) if raw.strip() else {}
        except ValueError:
            parsed = {"raw": raw}
        return exc.code, parsed
    except urllib.error.URLError as exc:
        raise ZohoError(f"Could not reach Zoho ({exc.reason}).") from exc
    except TimeoutError as exc:
        raise ZohoError("Zoho request timed out.") from exc


# ---------------------------------------------------------------------------
# OAuth — refresh-token flow, with a small in-process access-token cache
# ---------------------------------------------------------------------------

# key -> (access_token, expires_at_epoch). The cache is best-effort; a miss just
# means one extra token call. Never persisted (tokens are short-lived).
_TOKEN_CACHE: Dict[str, Tuple[str, float]] = {}
_TOKEN_SKEW_SECONDS = 120


def _token_cache_key(cfg: ZohoConfig) -> str:
    # Refresh token uniquely identifies the grant; client_id disambiguates.
    return f"{cfg.client_id}:{hash(cfg.refresh_token)}"


def get_access_token(cfg: ZohoConfig, *, force: bool = False) -> str:
    """Return a valid access token, refreshing (and caching ~55 min) as needed."""
    if not (cfg.client_id and cfg.client_secret and cfg.refresh_token):
        raise ZohoError("Zoho OAuth is not fully configured (client id / secret / refresh token).")

    key = _token_cache_key(cfg)
    if not force:
        cached = _TOKEN_CACHE.get(key)
        if cached and cached[1] - _TOKEN_SKEW_SECONDS > time.time():
            return cached[0]

    body = urlencode(
        {
            "grant_type": "refresh_token",
            "client_id": cfg.client_id,
            "client_secret": cfg.client_secret,
            "refresh_token": cfg.refresh_token,
        }
    ).encode("ascii")
    status, payload = _request(
        "POST",
        cfg.token_endpoint,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        body=body,
    )
    token = payload.get("access_token")
    if status != 200 or not token:
        detail = payload.get("error") or payload.get("raw") or f"HTTP {status}"
        raise ZohoError(f"Zoho token refresh failed: {detail}", status)

    expires_in = int(payload.get("expires_in", 3600) or 3600)
    _TOKEN_CACHE[key] = (token, time.time() + expires_in)
    return token


def _auth_headers(cfg: ZohoConfig, access_token: str) -> Dict[str, str]:
    return {
        "Authorization": f"Zoho-oauthtoken {access_token}",
        "orgId": cfg.org_id,
    }


# ---------------------------------------------------------------------------
# Desk layout / picklist field operations
# ---------------------------------------------------------------------------

def _allowed_layout_ids() -> Optional[Set[str]]:
    """Ops-level allowlist of layout ids the integration may touch (env var).

    Zoho OAuth scopes cannot be pinned to a single layout, so this is the hard
    application-side guard: set ``ZOHO_ALLOWED_LAYOUT_IDS`` (comma-separated) and
    every read/write is refused for any layout not in the list — even if the DB
    config is edited to point elsewhere. Unset ⇒ no restriction (config decides).
    """
    raw = os.getenv("ZOHO_ALLOWED_LAYOUT_IDS", "").strip()
    if not raw:
        return None
    return {part.strip() for part in raw.split(",") if part.strip()}


def _assert_layout_allowed(cfg: ZohoConfig) -> None:
    allowed = _allowed_layout_ids()
    if allowed is not None and str(cfg.layout_id) not in allowed:
        raise ZohoError(
            f"Refusing to operate on Zoho layout {cfg.layout_id}: it is not in "
            "ZOHO_ALLOWED_LAYOUT_IDS.",
            403,
        )


def _get_layout_once(cfg: ZohoConfig, access_token: str) -> Tuple[int, Dict[str, Any]]:
    # Read the WHOLE layout — Desk rejects GET on the per-field sub-resource
    # (that endpoint is PATCH-only, returns 405). The field is found in
    # sections[].fields[] (field-sync spec §7 Step 2, the proven path).
    url = f"{cfg.api_base.rstrip('/')}/layouts/{cfg.layout_id}"
    return _request("GET", url, headers=_auth_headers(cfg, access_token))


def _find_field(layout: Dict[str, Any], field_id: str) -> Optional[Dict[str, Any]]:
    """Locate the managed field (by id) inside a layout payload's sections/fields."""
    target = str(field_id)

    def _match(field: Any) -> bool:
        return isinstance(field, dict) and str(field.get("id")) == target

    # A GET on a single layout may return the layout object directly or wrapped
    # as {"layouts": [ ... ]}; tolerate both, plus fields at the top level.
    layouts: List[Dict[str, Any]] = []
    if isinstance(layout, dict):
        if isinstance(layout.get("layouts"), list):
            layouts = [l for l in layout["layouts"] if isinstance(l, dict)]
        else:
            layouts = [layout]
    for lay in layouts:
        for section in lay.get("sections") or []:
            if not isinstance(section, dict):
                continue
            for field in section.get("fields") or []:
                if _match(field):
                    return field
        for field in lay.get("fields") or []:
            if _match(field):
                return field
    return None


def get_field(cfg: ZohoConfig) -> Dict[str, Any]:
    """Read the managed picklist field object (with its ``allowedValues``).

    Reads the whole layout and extracts the field. Refreshes the token once on 401.
    """
    _assert_layout_allowed(cfg)
    token = get_access_token(cfg)
    status, payload = _get_layout_once(cfg, token)
    if status == 401:
        token = get_access_token(cfg, force=True)
        status, payload = _get_layout_once(cfg, token)
    if status != 200:
        raise ZohoError(_error_detail("Reading the Zoho layout failed", status, payload), status)
    field = _find_field(payload, cfg.app_field_id)
    if field is None:
        raise ZohoError(
            f"Field id {cfg.app_field_id} was not found in layout {cfg.layout_id}. "
            "Check the Application field ID and that the field is on this layout.",
            404,
        )
    return field


def get_layout(cfg: ZohoConfig, fresh: bool = False) -> Dict[str, Any]:
    """Return the whole layout object (sections[] -> fields[]). Guarded + 401 retry.

    Served from a short TTL cache (see ``_LAYOUT_CACHE``); ``fresh=True`` bypasses
    and repopulates it.
    """
    _assert_layout_allowed(cfg)
    key = f"layout:{cfg.api_base}:{cfg.layout_id}"
    if fresh:
        _LAYOUT_CACHE.invalidate(key)
    return _LAYOUT_CACHE.get_or_compute(
        key,
        _LAYOUT_TTL_SECONDS,
        lambda: _get_layout_uncached(cfg),
        stale_ttl=_LAYOUT_STALE_SERVE_SECONDS,
    )


def _get_layout_uncached(cfg: ZohoConfig) -> Dict[str, Any]:
    token = get_access_token(cfg)
    status, payload = _get_layout_once(cfg, token)
    if status == 401:
        token = get_access_token(cfg, force=True)
        status, payload = _get_layout_once(cfg, token)
    if status != 200:
        raise ZohoError(_error_detail("Reading the Zoho layout failed", status, payload), status)
    layouts = payload.get("layouts") if isinstance(payload.get("layouts"), list) else None
    if layouts:
        return next((l for l in layouts if str(l.get("id")) == str(cfg.layout_id)), layouts[0])
    return payload


def field_on_layout(cfg: ZohoConfig, field_id: str) -> Optional[Dict[str, Any]]:
    """The field dict if ``field_id`` is on the pinned layout, else None.

    The master single-layout guard for field edits: we only ever mutate a field
    that actually belongs to the allowed layout.
    """
    return _find_field(get_layout(cfg), field_id)


def _org_fields_url(cfg: ZohoConfig, field_id: Optional[str] = None) -> str:
    base = cfg.api_base.rstrip("/")
    suffix = f"/{field_id}" if field_id else ""
    return f"{base}/organizationFields{suffix}?orgId={cfg.org_id}"


def _mutate_org_field(cfg: ZohoConfig, method: str, body: Dict[str, Any],
                      field_id: Optional[str] = None) -> Dict[str, Any]:
    _assert_layout_allowed(cfg)
    url = _org_fields_url(cfg, field_id)
    data = json.dumps(body).encode("utf-8")

    def _do(tok: str) -> Tuple[int, Dict[str, Any]]:
        headers = _auth_headers(cfg, tok)
        headers["Content-Type"] = "application/json"
        return _request(method, url, headers=headers, body=data)

    token = get_access_token(cfg)
    status, payload = _do(token)
    if status == 401:
        token = get_access_token(cfg, force=True)
        status, payload = _do(token)
    if status not in (200, 201):
        raise ZohoError(_error_detail(f"{method} organizationFields failed", status, payload), status)
    invalidate_layout_cache()
    return payload


def create_org_field(cfg: ZohoConfig, body: Dict[str, Any]) -> Dict[str, Any]:
    """POST a new custom field (needs Desk.settings.CREATE)."""
    return _mutate_org_field(cfg, "POST", body)


def update_org_field(cfg: ZohoConfig, field_id: str, body: Dict[str, Any]) -> Dict[str, Any]:
    """PATCH an existing field's properties (needs Desk.settings.UPDATE)."""
    return _mutate_org_field(cfg, "PATCH", body, field_id=field_id)


def get_allowed_values(cfg: ZohoConfig) -> List[str]:
    """The current picklist values (including the leading ``-None-`` placeholder)."""
    field = get_field(cfg)
    values = field.get("allowedValues") or []
    out: List[str] = []
    for entry in values:
        # allowedValues may be plain strings or {"value": "..."} objects depending
        # on the Desk API version — tolerate both.
        if isinstance(entry, dict):
            val = entry.get("value")
        else:
            val = entry
        if val is not None:
            out.append(str(val))
    return out


def set_allowed_values(
    cfg: ZohoConfig,
    allowed_values: List[str],
    *,
    field_id: Optional[str] = None,
    default_value: str = "-None-",
    sort_by: str = "userDefined",
    is_mandatory: bool = False,
) -> Dict[str, Any]:
    """Replace the whole picklist value list (create/rename/delete in one PATCH).

    Targets ``field_id`` (defaults to the Application field) on the pinned layout.
    All four body fields are required by Desk; ``isMandatory`` is also sent as a
    query param (per the field-sync spec §4). Refreshes the token once on 401.
    """
    _assert_layout_allowed(cfg)
    target_field = field_id or cfg.app_field_id
    base = cfg.api_base.rstrip("/")
    query = urlencode({"orgId": cfg.org_id, "isMandatory": str(is_mandatory).lower()})
    url = f"{base}/layouts/{cfg.layout_id}/fields/{target_field}?{query}"
    body = json.dumps(
        {
            "allowedValues": allowed_values,
            "defaultValue": default_value,
            "sortBy": sort_by,
            "isMandatory": is_mandatory,
        }
    ).encode("utf-8")

    def _do(tok: str) -> Tuple[int, Dict[str, Any]]:
        headers = _auth_headers(cfg, tok)
        headers["Content-Type"] = "application/json"
        return _request("PATCH", url, headers=headers, body=body)

    token = get_access_token(cfg)
    status, payload = _do(token)
    if status == 401:
        token = get_access_token(cfg, force=True)
        status, payload = _do(token)
    if status not in (200, 201):
        raise ZohoError(_error_detail("Updating the Zoho field failed", status, payload), status)
    invalidate_layout_cache()
    return payload


# ---------------------------------------------------------------------------
# Field dependency mapping (Application <- Environment cascade)
#
# Verified against Areeba's live Zoho Desk (2026-07-07). The resource is
# TOP-LEVEL ``/dependencyMappings`` (NOT ``/layouts/{id}/...`` — that 404s), with
# ``layoutId`` in the body/query and ``orgId`` in the header. The create body is::
#
#     { "layoutId": ..., "parentId": <parent field id>, "childId": <child field id>,
#       "mappings": { "<parentValue>": ["<childValue>", ...], ... } }
#
# ``mappings`` MUST be a JSON OBJECT — an array/string yields a misleading
# HTTP 415 "unsupported media type". Parent/child values must already exist on the
# layout's picklists (publish them first). These helpers do transport + layout
# guard + one 401 retry, like the picklist ops.
# ---------------------------------------------------------------------------

def _dependency_base(cfg: ZohoConfig) -> str:
    return f"{cfg.api_base.rstrip('/')}/dependencyMappings"


def _layout_dependency_request(
    cfg: ZohoConfig, method: str, url: str, body: Optional[Dict[str, Any]] = None
) -> Dict[str, Any]:
    """Do one dependency-mapping call with the layout guard + a single 401 retry."""
    _assert_layout_allowed(cfg)
    data = json.dumps(body).encode("utf-8") if body is not None else None

    def _do(tok: str) -> Tuple[int, Dict[str, Any]]:
        headers = _auth_headers(cfg, tok)
        if data is not None:
            headers["Content-Type"] = "application/json"
        return _request(method, url, headers=headers, body=data)

    token = get_access_token(cfg)
    status, payload = _do(token)
    if status == 401:
        token = get_access_token(cfg, force=True)
        status, payload = _do(token)
    if status not in (200, 201, 204):
        raise ZohoError(_error_detail(f"{method} dependency mapping failed", status, payload), status)
    return payload if isinstance(payload, dict) else {}


def list_dependency_mappings(cfg: ZohoConfig) -> Dict[str, Any]:
    """List the dependency mappings on this layout (204/empty -> {})."""
    return _layout_dependency_request(cfg, "GET", f"{_dependency_base(cfg)}?layoutId={cfg.layout_id}")


def create_dependency_mapping(cfg: ZohoConfig, body: Dict[str, Any]) -> Dict[str, Any]:
    """Create a parent->child dependency mapping (needs Desk.settings.CREATE)."""
    return _layout_dependency_request(cfg, "POST", _dependency_base(cfg), body)


def update_dependency_mapping(cfg: ZohoConfig, mapping_id: str, body: Dict[str, Any]) -> Dict[str, Any]:
    """Replace an existing dependency mapping's value pairs (needs Desk.settings.UPDATE)."""
    return _layout_dependency_request(cfg, "PUT", f"{_dependency_base(cfg)}/{mapping_id}", body)


def delete_dependency_mapping(cfg: ZohoConfig, mapping_id: str) -> Dict[str, Any]:
    """Remove a dependency mapping (needs Desk.settings.DELETE)."""
    return _layout_dependency_request(cfg, "DELETE", f"{_dependency_base(cfg)}/{mapping_id}")


# ---------------------------------------------------------------------------
# Ticket write-back — update status/owner/resolution + post a comment. Needs the
# token minted with Desk.tickets.ALL (or tickets.UPDATE + tickets.CREATE for
# comments). NOT layout-guarded: these act on a specific ticket by id, which the
# inbound webhook gave us. All do one 401-refresh retry like the other calls.
# ---------------------------------------------------------------------------

# email(lowercased) -> agentId, cached per process. Agents rarely change and a
# miss just re-fetches; never persisted.
_AGENT_ID_CACHE: Dict[str, str] = {}


def _ticket_request(
    cfg: ZohoConfig, method: str, url: str, body: Optional[Dict[str, Any]] = None
) -> Tuple[int, Dict[str, Any]]:
    data = json.dumps(body).encode("utf-8") if body is not None else None

    def _do(tok: str) -> Tuple[int, Dict[str, Any]]:
        headers = _auth_headers(cfg, tok)
        if data is not None:
            headers["Content-Type"] = "application/json"
        return _request(method, url, headers=headers, body=data)

    token = get_access_token(cfg)
    status, payload = _do(token)
    if status == 401:
        token = get_access_token(cfg, force=True)
        status, payload = _do(token)
    return status, (payload if isinstance(payload, dict) else {})


def resolve_agent_id(cfg: ZohoConfig, email: str) -> Optional[str]:
    """Agent id for an email (e.g. the zagent service account), cached.

    Pages through ``/agents`` matching ``emailId`` case-insensitively. Returns
    None if not found; the caller then just skips the owner reassignment.
    """
    email = (email or "").strip().lower()
    if not email:
        return None
    key = f"{cfg.org_id}:{email}"
    if key in _AGENT_ID_CACHE:
        return _AGENT_ID_CACHE[key]

    base = cfg.api_base.rstrip("/")
    from_index = 1
    for _ in range(20):  # up to 20 pages of 100 = 2000 agents
        status, payload = _ticket_request(
            cfg, "GET", f"{base}/agents?from={from_index}&limit=100"
        )
        if status != 200:
            break
        rows = payload.get("data") or []
        for agent in rows:
            if str(agent.get("emailId") or "").strip().lower() == email and agent.get("id"):
                _AGENT_ID_CACHE[key] = str(agent["id"])
                return _AGENT_ID_CACHE[key]
        if len(rows) < 100:
            break
        from_index += 100
    return None


def update_ticket(cfg: ZohoConfig, ticket_id: str, fields: Dict[str, Any]) -> Dict[str, Any]:
    """PATCH a ticket's fields (e.g. ``status``, ``assigneeId``, ``resolution``)."""
    if not fields:
        return {}
    url = f"{cfg.api_base.rstrip('/')}/tickets/{ticket_id}"
    status, payload = _ticket_request(cfg, "PATCH", url, fields)
    if status not in (200, 201):
        raise ZohoError(_error_detail("Updating the Zoho ticket failed", status, payload), status)
    return payload


def add_ticket_comment(
    cfg: ZohoConfig, ticket_id: str, content: str, *, is_public: bool = False
) -> Dict[str, Any]:
    """Post a comment on a ticket (private by default)."""
    url = f"{cfg.api_base.rstrip('/')}/tickets/{ticket_id}/comments"
    body = {"content": content or "", "isPublic": bool(is_public), "contentType": "plainText"}
    status, payload = _ticket_request(cfg, "POST", url, body)
    if status not in (200, 201):
        raise ZohoError(_error_detail("Posting the Zoho comment failed", status, payload), status)
    return payload


def _error_detail(prefix: str, status: int, payload: Dict[str, Any]) -> str:
    detail = ""
    if isinstance(payload, dict):
        detail = payload.get("message") or payload.get("errorCode") or payload.get("raw") or ""
    return f"{prefix} (HTTP {status}){f': {detail}' if detail else ''}."
