"""Low-level Jira REST client — the Jira half of the ticketing integration.

The counterpart of :mod:`zoho_client`. Same shape (stdlib ``urllib`` only, one
retry on an expired credential, a short TTL cache in front of the expensive
structural read, an ops-level allowlist guarding every write) against Jira's
very different model:

* **Form structure is a SCREEN.** Jira does not have layouts; a screen has tabs
  and a tab has fields (``/rest/api/3/screens/{id}/tabs`` and
  ``.../tabs/{tabId}/fields``). Unlike Zoho's whole-layout PATCH, each of those
  is an individual add/remove/move call, so there is no "one bad body wipes the
  form" failure mode — but a *removal* is still destructive, hence the guard.

* **Options live on a field CONTEXT, not on the field.** A single-select custom
  field's option list belongs to a context
  (``/field/{fieldId}/context/{contextId}/option``), and options are addressed by
  their own ids. Replacing a list is therefore a diff — create the new, rename
  the changed, disable the removed — not one array PATCH.

* **Cascade is one field, not a mapping.** Jira has no cross-field dependency
  resource. A dependent dropdown is a *cascading select* whose parent options
  carry child options (``optionId`` on the create body), which is what
  :func:`set_cascading_options` writes.

* **Auth is a static credential.** Cloud uses HTTP Basic with
  ``email:api-token``; Server/DC uses a Bearer personal access token. There is no
  refresh dance, so ``force`` on the auth header is a no-op — kept in the shape
  of the Zoho calls so the two providers read the same way.

Rather than deleting options that fall out of the published list, they are
**disabled**: Jira rejects deleting an option that is set on an existing issue,
and a closed ticket that referenced a since-removed deployment must keep
rendering. Disabled options stop being selectable, which is the behaviour the
sync wants.
"""

from __future__ import annotations

import base64
import json
import logging
import os
import urllib.error
import urllib.request
from dataclasses import dataclass, field as dc_field
from typing import Any, Dict, List, Optional, Set, Tuple
from urllib.parse import urlencode

from ..ttl_cache import TTLCache

logger = logging.getLogger(__name__)

_TIMEOUT_SECONDS = 20

# The screen read walks every tab (1 + N calls over the internet) and the tab
# re-reads it on every open, so cache it exactly like the Zoho layout read:
# short TTL, single-flight, stale-while-revalidate. Every structural write
# invalidates it. The compute closure is pure urllib — safe on the background
# refresh thread, which has no Flask app context.
_SCREEN_CACHE = TTLCache("jira-screen")
_SCREEN_TTL_SECONDS = int(os.getenv("JIRA_SCREEN_CACHE_TTL_SECONDS", "60"))
_SCREEN_STALE_SERVE_SECONDS = int(os.getenv("JIRA_SCREEN_CACHE_STALE_SECONDS", "600"))

# Jira paginates option reads at 100; 50 pages is 5000 options, far past any
# realistic deployment list and a hard stop against a paging bug looping.
_PAGE_SIZE = 100
_MAX_PAGES = 50

# The custom-field types this integration knows how to publish into.
SELECT_TYPE = "com.atlassian.jira.plugin.system.customfieldtypes:select"
CASCADING_TYPE = "com.atlassian.jira.plugin.system.customfieldtypes:cascadingselect"
TEXT_TYPE = "com.atlassian.jira.plugin.system.customfieldtypes:textfield"
# Searchers Jira requires alongside each type on create.
_SEARCHER_FOR_TYPE = {
    SELECT_TYPE: "com.atlassian.jira.plugin.system.customfieldtypes:multiselectsearcher",
    CASCADING_TYPE: "com.atlassian.jira.plugin.system.customfieldtypes:cascadingselectsearcher",
    TEXT_TYPE: "com.atlassian.jira.plugin.system.customfieldtypes:textsearcher",
}


def invalidate_screen_cache() -> None:
    """Drop cached screen reads — call after any write that changes the screen."""
    _SCREEN_CACHE.invalidate("screen:")


class JiraError(Exception):
    """A Jira REST call failed. ``status`` mirrors the HTTP code when known."""

    def __init__(self, message: str, status: Optional[int] = None):
        super().__init__(message)
        self.status = status


@dataclass
class JiraConfig:
    """Everything a Jira call needs, with the API token already decrypted."""

    base_url: str
    # "cloud" -> /rest/api/3 + Basic email:token; "server" -> /rest/api/2 + Bearer PAT.
    deployment_type: str = "cloud"
    email: str = ""
    api_token: str = ""
    project_key: str = ""
    screen_id: str = ""
    app_field_id: str = ""
    environment_field_id: str = ""

    @property
    def api_root(self) -> str:
        version = "2" if str(self.deployment_type).lower() == "server" else "3"
        return f"{self.base_url.rstrip('/')}/rest/api/{version}"

    @property
    def is_cloud(self) -> bool:
        return str(self.deployment_type).lower() != "server"


# ---------------------------------------------------------------------------
# HTTP helper
# ---------------------------------------------------------------------------

def _request(
    method: str,
    url: str,
    *,
    headers: Optional[Dict[str, str]] = None,
    body: Optional[bytes] = None,
) -> Tuple[int, Any]:
    """Do one HTTP call. Returns (status, parsed-json). Raises JiraError on transport failure.

    The parsed body is deliberately typed ``Any``: Jira returns a bare JSON list
    from several endpoints (``/screens/{id}/tabs``, ``/field``), not always an
    object.
    """
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
        raise JiraError(f"Could not reach Jira ({exc.reason}).") from exc
    except TimeoutError as exc:
        raise JiraError("Jira request timed out.") from exc


def _auth_headers(cfg: JiraConfig) -> Dict[str, str]:
    """Basic (Cloud, email + API token) or Bearer (Server/DC personal access token)."""
    if not cfg.api_token:
        raise JiraError("Jira is not fully configured (no API token / personal access token).")
    if cfg.is_cloud:
        if not cfg.email:
            raise JiraError("Jira Cloud needs the account email the API token belongs to.")
        raw = f"{cfg.email}:{cfg.api_token}".encode("utf-8")
        credential = base64.b64encode(raw).decode("ascii")
        authorization = f"Basic {credential}"
    else:
        authorization = f"Bearer {cfg.api_token}"
    return {"Authorization": authorization, "Accept": "application/json"}


def _error_detail(prefix: str, status: int, payload: Any) -> str:
    """Flatten Jira's several error shapes into one operator-readable sentence.

    Jira reports failures as ``errorMessages`` (a list), ``errors`` (a field ->
    message object), or ``message``, and which one appears depends on the
    endpoint — so all three are collected rather than guessed between.
    """
    pieces: List[str] = []
    if isinstance(payload, dict):
        for message in payload.get("errorMessages") or []:
            if message:
                pieces.append(str(message))
        errors = payload.get("errors")
        if isinstance(errors, dict):
            for name, message in errors.items():
                pieces.append(f"{name}: {message}" if name else str(message))
        for key in ("message", "raw"):
            value = payload.get(key)
            if value and str(value) not in pieces:
                pieces.append(str(value))
    detail = "; ".join(p for p in pieces if p)
    return f"{prefix} (HTTP {status}){f': {detail}' if detail else ''}."


def _call(
    cfg: JiraConfig,
    method: str,
    url: str,
    body: Optional[Dict[str, Any]] = None,
    *,
    what: str = "Jira request",
    ok: Tuple[int, ...] = (200, 201, 204),
) -> Any:
    """One authenticated call, raising :class:`JiraError` on anything unexpected."""
    headers = _auth_headers(cfg)
    data = None
    if body is not None:
        headers["Content-Type"] = "application/json"
        data = json.dumps(body).encode("utf-8")
    status, payload = _request(method, url, headers=headers, body=data)
    if status not in ok:
        raise JiraError(_error_detail(f"{what} failed", status, payload), status)
    return payload


# ---------------------------------------------------------------------------
# Write guard — the Jira counterpart of ZOHO_ALLOWED_LAYOUT_IDS
# ---------------------------------------------------------------------------

def _allowed_screen_ids() -> Optional[Set[str]]:
    """Ops-level allowlist of screen ids the integration may touch (env var).

    A Jira API token carries the whole user's permissions and cannot be scoped to
    one screen, so this is the hard application-side guard: set
    ``JIRA_ALLOWED_SCREEN_IDS`` (comma-separated) and every structural read/write
    is refused for any screen not in it — even if the DB config is edited to point
    somewhere else. Unset ⇒ no restriction (config decides).
    """
    raw = os.getenv("JIRA_ALLOWED_SCREEN_IDS", "").strip()
    if not raw:
        return None
    return {part.strip() for part in raw.split(",") if part.strip()}


def _assert_screen_allowed(cfg: JiraConfig) -> None:
    allowed = _allowed_screen_ids()
    if allowed is not None and str(cfg.screen_id) not in allowed:
        raise JiraError(
            f"Refusing to operate on Jira screen {cfg.screen_id}: it is not in "
            "JIRA_ALLOWED_SCREEN_IDS.",
            403,
        )


# ---------------------------------------------------------------------------
# Connection probe
# ---------------------------------------------------------------------------

def get_myself(cfg: JiraConfig) -> Dict[str, Any]:
    """``/myself`` — the cheapest call that proves the credential works."""
    return _call(cfg, "GET", f"{cfg.api_root}/myself", what="Reading the Jira account")


def get_project(cfg: JiraConfig) -> Dict[str, Any]:
    """The configured project, so the test can confirm the key actually resolves."""
    if not cfg.project_key:
        raise JiraError("No Jira project key is configured.", 400)
    return _call(
        cfg,
        "GET",
        f"{cfg.api_root}/project/{cfg.project_key}",
        what=f"Reading Jira project {cfg.project_key}",
    )


# ---------------------------------------------------------------------------
# Fields — the org-level catalogue
# ---------------------------------------------------------------------------

def list_fields(cfg: JiraConfig) -> List[Dict[str, Any]]:
    """Every field visible to the credential (system + custom), as Jira returns them."""
    payload = _call(cfg, "GET", f"{cfg.api_root}/field", what="Listing Jira fields")
    return [f for f in payload if isinstance(f, dict)] if isinstance(payload, list) else []


def create_field(
    cfg: JiraConfig, name: str, field_type: str, description: str = ""
) -> Dict[str, Any]:
    """Create a custom field. Jira derives the id (``customfield_NNNNN``) itself."""
    _assert_screen_allowed(cfg)
    searcher = _SEARCHER_FOR_TYPE.get(field_type)
    if not searcher:
        raise JiraError(f"Unsupported Jira custom field type: {field_type}", 400)
    body = {
        "name": name,
        "description": description or "",
        "type": field_type,
        "searcherKey": searcher,
    }
    created = _call(
        cfg, "POST", f"{cfg.api_root}/field", body, what="Creating the Jira custom field"
    )
    invalidate_screen_cache()
    return created if isinstance(created, dict) else {}


def update_field(cfg: JiraConfig, field_id: str, name: str, description: str = "") -> None:
    """Rename a custom field / change its description.

    Jira, like Zoho, cannot change a field's TYPE in place — that is what the
    conversion flow exists for.
    """
    _assert_screen_allowed(cfg)
    body = {"name": name}
    if description:
        body["description"] = description
    _call(
        cfg,
        "PUT",
        f"{cfg.api_root}/field/{field_id}",
        body,
        what="Updating the Jira custom field",
    )
    invalidate_screen_cache()


def delete_field(cfg: JiraConfig, field_id: str) -> Dict[str, Any]:
    """Trash a custom field. Jira soft-deletes: it lands in the trash, recoverable."""
    _assert_screen_allowed(cfg)
    payload = _call(
        cfg,
        "DELETE",
        f"{cfg.api_root}/field/{field_id}",
        what="Deleting the Jira custom field",
    )
    invalidate_screen_cache()
    return payload if isinstance(payload, dict) else {}


# ---------------------------------------------------------------------------
# Field contexts + options
# ---------------------------------------------------------------------------

def _paged(cfg: JiraConfig, url: str, what: str) -> List[Dict[str, Any]]:
    """Walk Jira's ``startAt``/``isLast`` paging and return every value."""
    out: List[Dict[str, Any]] = []
    start = 0
    for _ in range(_MAX_PAGES):
        joiner = "&" if "?" in url else "?"
        page = _call(
            cfg,
            "GET",
            f"{url}{joiner}{urlencode({'startAt': start, 'maxResults': _PAGE_SIZE})}",
            what=what,
        )
        values = page.get("values") if isinstance(page, dict) else None
        rows = [v for v in (values or []) if isinstance(v, dict)]
        out.extend(rows)
        if not isinstance(page, dict) or page.get("isLast") or not rows:
            break
        start += len(rows)
    return out


def default_context_id(cfg: JiraConfig, field_id: str) -> str:
    """The context whose options this integration publishes into.

    A custom field can have several contexts (per project / issue type). The
    integration owns exactly one: the context covering the configured project,
    falling back to the field's global context. Anything else would publish into
    a list the DevOps Request form does not show.
    """
    contexts = _paged(
        cfg,
        f"{cfg.api_root}/field/{field_id}/context",
        f"Reading contexts for Jira field {field_id}",
    )
    if not contexts:
        raise JiraError(
            f"Jira field {field_id} has no field context — it cannot hold options.", 404
        )
    for context in contexts:
        if context.get("isGlobalContext"):
            return str(context.get("id"))
    return str(contexts[0].get("id"))


def get_options(cfg: JiraConfig, field_id: str, context_id: Optional[str] = None) -> List[Dict[str, Any]]:
    """Every option on the field's owned context, newest paging walked through.

    Each row is Jira's own shape: ``{"id", "value", "disabled", "optionId"?}``
    where ``optionId`` (present on a cascading select's children) names the
    parent option.
    """
    ctx = context_id or default_context_id(cfg, field_id)
    return _paged(
        cfg,
        f"{cfg.api_root}/field/{field_id}/context/{ctx}/option",
        f"Reading options for Jira field {field_id}",
    )


def _create_options(
    cfg: JiraConfig, field_id: str, context_id: str, options: List[Dict[str, Any]]
) -> List[Dict[str, Any]]:
    """POST a batch of new options. Returns the created rows (with their new ids)."""
    if not options:
        return []
    created: List[Dict[str, Any]] = []
    # Jira caps a create batch at 1000; chunk well under that so one oversized
    # namespace can never trip the limit.
    for start in range(0, len(options), _PAGE_SIZE):
        chunk = options[start : start + _PAGE_SIZE]
        payload = _call(
            cfg,
            "POST",
            f"{cfg.api_root}/field/{field_id}/context/{context_id}/option",
            {"options": chunk},
            what=f"Creating options on Jira field {field_id}",
        )
        if isinstance(payload, dict):
            created.extend([o for o in (payload.get("options") or []) if isinstance(o, dict)])
    return created


def _update_options(
    cfg: JiraConfig, field_id: str, context_id: str, options: List[Dict[str, Any]]
) -> None:
    """PUT existing options (used to re-enable ones coming back into the list)."""
    if not options:
        return
    for start in range(0, len(options), _PAGE_SIZE):
        chunk = options[start : start + _PAGE_SIZE]
        _call(
            cfg,
            "PUT",
            f"{cfg.api_root}/field/{field_id}/context/{context_id}/option",
            {"options": chunk},
            what=f"Updating options on Jira field {field_id}",
        )


def _reorder_options(
    cfg: JiraConfig, field_id: str, context_id: str, option_ids: List[str]
) -> None:
    """Put the option list into the published order.

    Zoho's ``sortBy: userDefined`` + a whole-array PATCH gives ordering for free;
    Jira needs an explicit move. Best-effort: a failure here is cosmetic (the
    dropdown is merely ordered differently), so it must not fail a sync that
    already published the right values.
    """
    if not option_ids:
        return
    try:
        for start in range(0, len(option_ids), _PAGE_SIZE):
            chunk = option_ids[start : start + _PAGE_SIZE]
            body: Dict[str, Any] = {"customFieldOptionIds": chunk}
            if start == 0:
                body["position"] = "First"
            else:
                body["after"] = option_ids[start - 1]
            _call(
                cfg,
                "PUT",
                f"{cfg.api_root}/field/{field_id}/context/{context_id}/option/move",
                body,
                what=f"Reordering options on Jira field {field_id}",
            )
    except JiraError as exc:
        logger.warning("Could not reorder Jira options on %s: %s", field_id, exc)


@dataclass
class OptionSync:
    """What one option-list publish actually changed, for the sync's log line."""

    created: int = 0
    reenabled: int = 0
    disabled: int = 0
    unchanged: int = 0
    # value (casefolded) -> Jira option id, for the caller's cascade wiring.
    ids_by_value: Dict[str, str] = dc_field(default_factory=dict)

    @property
    def changed(self) -> int:
        return self.created + self.reenabled + self.disabled


def set_options(
    cfg: JiraConfig,
    field_id: str,
    values: List[str],
    *,
    context_id: Optional[str] = None,
) -> OptionSync:
    """Make the field's option list exactly ``values`` — a diff, not a replace.

    Jira addresses options by id and refuses to delete one that is set on an
    existing issue, so this reconciles instead: create what is new, re-enable
    what came back, **disable** (never delete) what fell out. A closed ticket
    referencing a since-removed deployment therefore keeps rendering, while the
    option stops being selectable — the same end state Zoho's array replace
    produces for new tickets.

    Values are matched case-insensitively, which is also how the option lists are
    canonicalized upstream, so a rename that only changes case is a no-op rather
    than a duplicate.
    """
    _assert_screen_allowed(cfg)
    ctx = context_id or default_context_id(cfg, field_id)
    existing = get_options(cfg, field_id, ctx)
    by_key = {str(o.get("value", "")).casefold(): o for o in existing}
    wanted = [v for v in values if str(v).strip()]
    wanted_keys = {str(v).casefold() for v in wanted}

    result = OptionSync()

    to_create = [{"value": v} for v in wanted if str(v).casefold() not in by_key]
    # Options that exist but were disabled by an earlier sync and are wanted again.
    to_reenable = [
        {"id": str(by_key[str(v).casefold()].get("id")), "value": v, "disabled": False}
        for v in wanted
        if str(v).casefold() in by_key and by_key[str(v).casefold()].get("disabled")
    ]
    to_disable = [
        {"id": str(o.get("id")), "value": o.get("value"), "disabled": True}
        for key, o in by_key.items()
        if key not in wanted_keys and not o.get("disabled")
    ]

    created = _create_options(cfg, field_id, ctx, to_create)
    result.created = len(created)
    _update_options(cfg, field_id, ctx, to_reenable)
    result.reenabled = len(to_reenable)
    _update_options(cfg, field_id, ctx, to_disable)
    result.disabled = len(to_disable)
    result.unchanged = len(wanted) - result.created - result.reenabled

    for option in created:
        result.ids_by_value[str(option.get("value", "")).casefold()] = str(option.get("id"))
    for key, option in by_key.items():
        result.ids_by_value.setdefault(key, str(option.get("id")))

    ordered_ids = [
        result.ids_by_value[str(v).casefold()]
        for v in wanted
        if str(v).casefold() in result.ids_by_value
    ]
    _reorder_options(cfg, field_id, ctx, ordered_ids)
    invalidate_screen_cache()
    return result


def set_cascading_options(
    cfg: JiraConfig,
    field_id: str,
    tree: List[Tuple[str, List[str]]],
    *,
    context_id: Optional[str] = None,
) -> OptionSync:
    """Publish a parent → children tree into a cascading-select field.

    This is Jira's whole answer to Zoho's ``/dependencyMappings``: there is no way
    to make picking field A filter field B, so a dependent dropdown is ONE field
    whose options nest. ``tree`` is ordered ``[(parent, [child, ...]), ...]``.

    Parents are reconciled first (their ids are needed as the children's
    ``optionId``), then each parent's children. Same create / re-enable / disable
    discipline as :func:`set_options`, including for a child list that shrank.
    """
    _assert_screen_allowed(cfg)
    ctx = context_id or default_context_id(cfg, field_id)
    existing = get_options(cfg, field_id, ctx)
    parents = {
        str(o.get("value", "")).casefold(): o for o in existing if not o.get("optionId")
    }
    children_by_parent: Dict[str, Dict[str, Dict[str, Any]]] = {}
    for option in existing:
        parent_id = option.get("optionId")
        if parent_id:
            bucket = children_by_parent.setdefault(str(parent_id), {})
            bucket[str(option.get("value", "")).casefold()] = option

    result = OptionSync()
    wanted_parent_keys = {str(p).casefold() for p, _ in tree}

    # --- Parents ---
    new_parents = [{"value": p} for p, _ in tree if str(p).casefold() not in parents]
    for option in _create_options(cfg, field_id, ctx, new_parents):
        parents[str(option.get("value", "")).casefold()] = option
        result.created += 1
    reenable_parents = [
        {"id": str(parents[str(p).casefold()].get("id")), "value": p, "disabled": False}
        for p, _ in tree
        if parents.get(str(p).casefold(), {}).get("disabled")
    ]
    _update_options(cfg, field_id, ctx, reenable_parents)
    result.reenabled += len(reenable_parents)
    stale_parents = [
        {"id": str(o.get("id")), "value": o.get("value"), "disabled": True}
        for key, o in parents.items()
        if key not in wanted_parent_keys and not o.get("disabled")
    ]
    _update_options(cfg, field_id, ctx, stale_parents)
    result.disabled += len(stale_parents)

    # --- Children, per parent ---
    for parent, children in tree:
        parent_option = parents.get(str(parent).casefold())
        if not parent_option:
            continue
        parent_id = str(parent_option.get("id"))
        result.ids_by_value[str(parent).casefold()] = parent_id
        current = children_by_parent.get(parent_id, {})
        wanted_keys = {str(c).casefold() for c in children}

        new_children = [
            {"value": c, "optionId": parent_id}
            for c in children
            if str(c).casefold() not in current
        ]
        for option in _create_options(cfg, field_id, ctx, new_children):
            result.created += 1
            current[str(option.get("value", "")).casefold()] = option
        reenable = [
            {"id": str(current[str(c).casefold()].get("id")), "value": c, "disabled": False}
            for c in children
            if current.get(str(c).casefold(), {}).get("disabled")
        ]
        _update_options(cfg, field_id, ctx, reenable)
        result.reenabled += len(reenable)
        stale = [
            {"id": str(o.get("id")), "value": o.get("value"), "disabled": True}
            for key, o in current.items()
            if key not in wanted_keys and not o.get("disabled")
        ]
        _update_options(cfg, field_id, ctx, stale)
        result.disabled += len(stale)

    invalidate_screen_cache()
    return result


# ---------------------------------------------------------------------------
# Screens + tabs — Jira's "layout"
# ---------------------------------------------------------------------------

def _screen_tabs(cfg: JiraConfig) -> List[Dict[str, Any]]:
    payload = _call(
        cfg,
        "GET",
        f"{cfg.api_root}/screens/{cfg.screen_id}/tabs",
        what=f"Reading tabs on Jira screen {cfg.screen_id}",
    )
    return [t for t in payload if isinstance(t, dict)] if isinstance(payload, list) else []


def _tab_fields(cfg: JiraConfig, tab_id: str) -> List[Dict[str, Any]]:
    payload = _call(
        cfg,
        "GET",
        f"{cfg.api_root}/screens/{cfg.screen_id}/tabs/{tab_id}/fields",
        what=f"Reading fields on Jira screen tab {tab_id}",
    )
    return [f for f in payload if isinstance(f, dict)] if isinstance(payload, list) else []


def _get_screen_uncached(cfg: JiraConfig) -> Dict[str, Any]:
    """One screen read: tabs, each tab's fields, joined to the field catalogue.

    Jira's per-tab field rows carry only ``{id, name}``, so the org-level ``/field``
    list is joined in to recover each field's type and whether it is custom —
    which is what tells the UI a dropdown from a text box.
    """
    tabs = _screen_tabs(cfg)
    catalogue = {str(f.get("id")): f for f in list_fields(cfg)}
    sections: List[Dict[str, Any]] = []
    for tab in tabs:
        tab_id = str(tab.get("id"))
        fields: List[Dict[str, Any]] = []
        for field in _tab_fields(cfg, tab_id):
            field_id = str(field.get("id"))
            meta = catalogue.get(field_id) or {}
            schema = meta.get("schema") if isinstance(meta.get("schema"), dict) else {}
            fields.append(
                {
                    "id": field_id,
                    "name": field.get("name") or meta.get("name") or field_id,
                    "custom": bool(meta.get("custom")),
                    "type": (schema or {}).get("custom") or (schema or {}).get("type") or "",
                }
            )
        sections.append({"id": tab_id, "name": tab.get("name") or "", "fields": fields})
    return {"id": str(cfg.screen_id), "sections": sections}


def get_screen(cfg: JiraConfig, fresh: bool = False) -> Dict[str, Any]:
    """The whole screen as ``{"id", "sections": [{"id", "name", "fields": [...]}]}``.

    Served from a short TTL cache; ``fresh=True`` bypasses and repopulates it.
    """
    _assert_screen_allowed(cfg)
    if not cfg.screen_id:
        raise JiraError("No Jira screen id is configured.", 400)
    key = f"screen:{cfg.base_url}:{cfg.screen_id}"
    if fresh:
        _SCREEN_CACHE.invalidate(key)
    return _SCREEN_CACHE.get_or_compute(
        key,
        _SCREEN_TTL_SECONDS,
        lambda: _get_screen_uncached(cfg),
        stale_ttl=_SCREEN_STALE_SERVE_SECONDS,
    )


def field_on_screen(cfg: JiraConfig, field_id: str) -> Optional[Dict[str, Any]]:
    """The field dict if ``field_id`` sits on the pinned screen, else None.

    The master single-screen guard for field edits: only a field that actually
    belongs to the allowed screen is ever mutated.
    """
    for section in get_screen(cfg).get("sections") or []:
        for field in section.get("fields") or []:
            if str(field.get("id")) == str(field_id):
                return field
    return None


def add_tab(cfg: JiraConfig, name: str) -> Dict[str, Any]:
    """Create a tab (Jira's section) on the pinned screen."""
    _assert_screen_allowed(cfg)
    created = _call(
        cfg,
        "POST",
        f"{cfg.api_root}/screens/{cfg.screen_id}/tabs",
        {"name": name},
        what="Creating the Jira screen tab",
    )
    invalidate_screen_cache()
    return created if isinstance(created, dict) else {}


def rename_tab(cfg: JiraConfig, tab_id: str, name: str) -> Dict[str, Any]:
    _assert_screen_allowed(cfg)
    updated = _call(
        cfg,
        "PUT",
        f"{cfg.api_root}/screens/{cfg.screen_id}/tabs/{tab_id}",
        {"name": name},
        what="Renaming the Jira screen tab",
    )
    invalidate_screen_cache()
    return updated if isinstance(updated, dict) else {}


def add_field_to_tab(cfg: JiraConfig, tab_id: str, field_id: str) -> None:
    _assert_screen_allowed(cfg)
    _call(
        cfg,
        "POST",
        f"{cfg.api_root}/screens/{cfg.screen_id}/tabs/{tab_id}/fields",
        {"fieldId": field_id},
        what="Adding the field to the Jira screen tab",
    )
    invalidate_screen_cache()


def remove_field_from_tab(cfg: JiraConfig, tab_id: str, field_id: str) -> None:
    """Take a field off a tab. The field itself and its issue data survive —
    the Jira equivalent of Zoho's ``unassociate``."""
    _assert_screen_allowed(cfg)
    _call(
        cfg,
        "DELETE",
        f"{cfg.api_root}/screens/{cfg.screen_id}/tabs/{tab_id}/fields/{field_id}",
        what="Removing the field from the Jira screen tab",
    )
    invalidate_screen_cache()


# ---------------------------------------------------------------------------
# Issue write-back — transition, assign, comment
#
# NOT screen-guarded: these act on a specific issue by key, which the inbound
# webhook gave us.
# ---------------------------------------------------------------------------

# email(lowercased) -> accountId, cached per process. Users rarely change and a
# miss just re-fetches; never persisted.
_ACCOUNT_ID_CACHE: Dict[str, str] = {}


def resolve_account_id(cfg: JiraConfig, email: str) -> Optional[str]:
    """Account id for an email, cached. Returns None when nothing matches.

    The caller then simply skips the assignee change rather than failing a
    write-back that otherwise succeeded.
    """
    email = (email or "").strip().lower()
    if not email:
        return None
    key = f"{cfg.base_url}:{email}"
    if key in _ACCOUNT_ID_CACHE:
        return _ACCOUNT_ID_CACHE[key]
    try:
        payload = _call(
            cfg,
            "GET",
            f"{cfg.api_root}/user/search?{urlencode({'query': email, 'maxResults': 50})}",
            what="Searching Jira users",
        )
    except JiraError:
        return None
    for user in payload if isinstance(payload, list) else []:
        if not isinstance(user, dict):
            continue
        if str(user.get("emailAddress") or "").strip().lower() == email:
            # Cloud uses accountId; Server/DC uses name/key.
            identifier = user.get("accountId") or user.get("name") or user.get("key")
            if identifier:
                _ACCOUNT_ID_CACHE[key] = str(identifier)
                return _ACCOUNT_ID_CACHE[key]
    return None


def list_transitions(cfg: JiraConfig, issue_key: str) -> List[Dict[str, Any]]:
    """Transitions currently available on the issue (workflow position dependent)."""
    payload = _call(
        cfg,
        "GET",
        f"{cfg.api_root}/issue/{issue_key}/transitions",
        what=f"Reading transitions for Jira issue {issue_key}",
    )
    if not isinstance(payload, dict):
        return []
    return [t for t in (payload.get("transitions") or []) if isinstance(t, dict)]


def transition_issue(cfg: JiraConfig, issue_key: str, transition_name: str) -> bool:
    """Move the issue through the named transition. False when it is unavailable.

    Jira has no "set status" — a status change is a workflow transition, and
    which ones exist depends on where the issue currently sits. An unavailable
    transition is a normal outcome (someone already closed the issue by hand),
    not an error, so it returns False for the caller to record instead of raising.
    """
    wanted = str(transition_name or "").strip().casefold()
    if not wanted:
        return False
    for transition in list_transitions(cfg, issue_key):
        name = str(transition.get("name") or "").strip().casefold()
        target = str((transition.get("to") or {}).get("name") or "").strip().casefold()
        if wanted in (name, target):
            _call(
                cfg,
                "POST",
                f"{cfg.api_root}/issue/{issue_key}/transitions",
                {"transition": {"id": str(transition.get("id"))}},
                what=f"Transitioning Jira issue {issue_key}",
            )
            return True
    return False


def assign_issue(cfg: JiraConfig, issue_key: str, account_id: str) -> None:
    """Reassign the issue. Cloud keys assignment by accountId, Server/DC by name."""
    body = {"accountId": account_id} if cfg.is_cloud else {"name": account_id}
    _call(
        cfg,
        "PUT",
        f"{cfg.api_root}/issue/{issue_key}/assignee",
        body,
        what=f"Assigning Jira issue {issue_key}",
    )


def add_comment(cfg: JiraConfig, issue_key: str, text: str) -> Dict[str, Any]:
    """Post a comment.

    Jira Cloud's v3 API takes Atlassian Document Format, not a plain string;
    Server/DC's v2 takes the string. Both are produced here so callers can pass
    plain text either way.
    """
    if cfg.is_cloud:
        body = {
            "body": {
                "type": "doc",
                "version": 1,
                "content": [
                    {"type": "paragraph", "content": [{"type": "text", "text": text or ""}]}
                ],
            }
        }
    else:
        body = {"body": text or ""}
    payload = _call(
        cfg,
        "POST",
        f"{cfg.api_root}/issue/{issue_key}/comment",
        body,
        what=f"Commenting on Jira issue {issue_key}",
    )
    return payload if isinstance(payload, dict) else {}


def get_issue(cfg: JiraConfig, issue_key: str) -> Dict[str, Any]:
    """Read one issue (used to fill in fields a lean webhook payload omitted)."""
    payload = _call(
        cfg,
        "GET",
        f"{cfg.api_root}/issue/{issue_key}",
        what=f"Reading Jira issue {issue_key}",
    )
    return payload if isinstance(payload, dict) else {}
