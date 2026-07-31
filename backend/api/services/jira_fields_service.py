"""Jira screen + field editing — the Jira counterpart of :mod:`zoho_fields_service`.

Serves the same field-editor UI: read the form's structure, create/edit/remove
fields, move them between sections, hand-edit an option list, and bind a dropdown
to a live Kubernetes source. Jira's structural model is a *screen* (tabs holding
fields) rather than a layout, and its option lists live on a field *context*, so
the vocabulary is translated here and the UI stays one component.

What Jira makes easier, and what it makes harder:

* **Easier.** There is no whole-layout replace, so there is no way for one bad
  body to strip the form. Each add/remove/rename is its own call, which is why
  this module has no ``plan``/``diff``/snapshot machinery — the Zoho equivalent
  exists purely to make a destructive PATCH survivable.

* **Harder.** Removing a field from a tab and *deleting* the field are different
  operations with very different blast radii, and options cannot be deleted at
  all once an issue uses them. Both are handled explicitly: a "remove" takes the
  field off the screen, and a hand-edited option list disables what it drops
  rather than destroying issue history.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from . import jira_client
from . import zoho_option_sources as sources
from .jira_client import CASCADING_TYPE, SELECT_TYPE, TEXT_TYPE, JiraError
from .jira_sync_service import PROVIDER, _to_client_config, get_or_create_config
from .zoho_sync_service import _sanitize_value, _source_entries as svc_source_entries

# Custom-field types the editor offers. Deliberately short: these are the types
# this integration can actually drive end-to-end (publish options into, or read a
# value out of on an inbound issue). Jira has dozens more that the DevOps Request
# flow has no meaning for.
CREATABLE_TYPES = {
    "Select": SELECT_TYPE,
    "Cascading select": CASCADING_TYPE,
    "Text": TEXT_TYPE,
}
# Which Jira type strings count as a single-value dropdown for option editing.
_OPTION_TYPES = {SELECT_TYPE, CASCADING_TYPE}


def _config_and_cfg():
    row = get_or_create_config()
    return row, _to_client_config(row)


def _auto_managed_ids(row) -> set:
    """Field ids the sync currently owns (only when their per-field toggle is on)."""
    ids = set()
    if row.sync_application and row.app_field_id:
        ids.add(str(row.app_field_id))
    if row.sync_environment and row.environment_field_id:
        ids.add(str(row.environment_field_id))
    if row.sync_variables and row.variable_field_id:
        ids.add(str(row.variable_field_id))
    if row.cascade_enabled and row.cascade_field_id:
        ids.add(str(row.cascade_field_id))
    return ids


def _option_values(cfg, field: Dict[str, Any]) -> Optional[List[str]]:
    """The field's current option list, or None when it is not an option field.

    Best-effort: a field whose context cannot be read (a permissions gap, or a
    field with no context yet) reports ``None`` — "unknown", which the UI renders
    as no option list rather than as an empty one.
    """
    if field.get("type") not in _OPTION_TYPES:
        return None
    try:
        options = jira_client.get_options(cfg, str(field.get("id")))
    except JiraError:
        return None
    # Parents only for a cascading select — the children belong under them and
    # would read as a flat jumble in a plain list.
    return [
        str(o.get("value"))
        for o in options
        if not o.get("optionId") and not o.get("disabled")
    ]


def _field_dict(
    cfg,
    field: Dict[str, Any],
    auto_managed_ids: set,
    bindings: Optional[Dict[str, Dict[str, Any]]] = None,
    *,
    with_options: bool = True,
) -> Dict[str, Any]:
    """One field in the shape the shared editor UI reads (see ``zoho_fields_service``)."""
    fid = str(field.get("id") or "")
    is_option_field = field.get("type") in _OPTION_TYPES
    return {
        "id": fid,
        # A Jira custom field's id IS its webhook key, so they are the same thing.
        "apiName": fid,
        "label": field.get("name") or fid,
        "type": field.get("type") or "",
        # Jira's per-tab field rows do not carry "required" — that lives on the
        # issue-type field configuration, a different object this editor does not
        # own. Reported as False rather than guessed at.
        "required": False,
        "custom": bool(field.get("custom")),
        # System fields (summary, status, ...) can be taken off a screen but must
        # never be offered for deletion.
        "removable": bool(field.get("custom")),
        "isPicklist": is_option_field,
        "allowedValues": _option_values(cfg, field) if with_options else None,
        "defaultValue": None,
        "autoManaged": fid in auto_managed_ids,
        "binding": (bindings or {}).get(fid),
    }


def get_layout_structure(fresh: bool = False) -> Dict[str, Any]:
    """Read the pinned screen and return its tabs + fields for the editor UI.

    ``fresh=True`` (manual refresh) bypasses the screen read cache.

    Option lists are NOT read here. Every option field needs its own context +
    option call, so a ten-dropdown screen would be twenty extra round trips on
    every tab open; the editor fetches a field's options when it opens that
    field. (Zoho gets them free — its one layout read carries them inline.)
    """
    row, cfg = _config_and_cfg()
    auto = _auto_managed_ids(row)
    bindings = sources.bindings_by_field(row, PROVIDER)
    screen = jira_client.get_screen(cfg, fresh=fresh)
    sections = []
    for index, sec in enumerate(screen.get("sections") or []):
        sections.append(
            {
                "id": sec.get("id"),
                "index": index,
                "name": sec.get("name"),
                "fields": [
                    _field_dict(cfg, f, auto, bindings, with_options=False)
                    for f in (sec.get("fields") or [])
                ],
            }
        )
    return {
        "layoutId": str(screen.get("id") or cfg.screen_id),
        "layoutName": f"Screen {screen.get('id') or cfg.screen_id}",
        "sections": sections,
        "creatableTypes": sorted(CREATABLE_TYPES),
        # Jira has no whole-layout replace to gate, so structural writes are
        # always available (subject to the token's own permissions).
        "layoutWritesEnabled": True,
    }


def get_field(field_id: str) -> Dict[str, Any]:
    """Return one field with its option list populated for the edit modal.

    The screen mirror deliberately omits Jira option lists because every dropdown
    costs two additional Jira requests (context + options).  Fetching one field
    on demand keeps the main layout fast without making "Manage options" mistake
    an unloaded list for an empty one.
    """
    row, cfg = _config_and_cfg()
    field = _find_field(cfg, field_id)
    bindings = sources.bindings_by_field(row, PROVIDER)
    return _field_dict(cfg, field, _auto_managed_ids(row), bindings, with_options=True)


def _find_field(cfg, field_id: str) -> Dict[str, Any]:
    field = jira_client.field_on_screen(cfg, str(field_id))
    if field is None:
        raise LookupError("That field is not on the configured Jira screen.")
    return field


def _tab_of_field(cfg, field_id: str) -> Optional[str]:
    """The id of the tab holding ``field_id`` — needed to remove or move it."""
    for section in jira_client.get_screen(cfg).get("sections") or []:
        for field in section.get("fields") or []:
            if str(field.get("id")) == str(field_id):
                return str(section.get("id"))
    return None


# ---------------------------------------------------------------------------
# Sections (Jira tabs)
# ---------------------------------------------------------------------------

def create_section(
    name: str, first_field_id: str, actor: Optional[str] = None
) -> Dict[str, Any]:
    """Add a tab to the screen and (optionally) move a field onto it.

    Unlike Zoho — whose API simply cannot add a section to an existing layout —
    Jira creates tabs directly, so this is a real operation here.
    """
    _row, cfg = _config_and_cfg()
    name = str(name or "").strip()
    if not name:
        raise ValueError("A section name is required.")
    created = jira_client.add_tab(cfg, name)
    tab_id = str(created.get("id") or "")
    if first_field_id and tab_id:
        jira_client.add_field_to_tab(cfg, tab_id, str(first_field_id))
    return {"id": tab_id, "name": name, "layoutId": cfg.screen_id, "diff": None}


def plan_section(name: str, first_field_id: str) -> Dict[str, Any]:
    """Dry run for adding a tab.

    Jira applies each structural change on its own — there is no whole-object
    rewrite to preview — so the "plan" is just a statement of intent, returned in
    the shape the shared confirm dialog expects.
    """
    return {
        "supported": True,
        "diff": {
            "summary": f"Add the tab “{name}” to the screen"
            + (f" and move one field onto it." if first_field_id else "."),
            "sectionsAdded": [name],
            "fieldsMoved": [str(first_field_id)] if first_field_id else [],
        },
        "body": None,
    }


def rename_section(
    section_id: str, name: str, actor: Optional[str] = None
) -> Dict[str, Any]:
    """Rename a tab."""
    _row, cfg = _config_and_cfg()
    section_id = str(section_id or "").strip()
    name = str(name or "").strip()
    if not (section_id and name):
        raise ValueError("A section id and a new name are both required.")
    jira_client.rename_tab(cfg, section_id, name)
    return {"id": section_id, "name": name, "diff": None, "layoutId": cfg.screen_id}


def move_field_to_section(
    field_id: str, section_name: str, actor: Optional[str] = None
) -> Dict[str, Any]:
    """Move a field to the named tab (remove from its current one, add to target)."""
    _row, cfg = _config_and_cfg()
    _find_field(cfg, field_id)
    target_name = str(section_name or "").strip()
    screen = jira_client.get_screen(cfg)
    target = next(
        (
            s
            for s in (screen.get("sections") or [])
            if str(s.get("name") or "").strip().casefold() == target_name.casefold()
        ),
        None,
    )
    if target is None:
        raise ValueError(f"No tab named “{target_name}” on this screen.")
    current_tab = _tab_of_field(cfg, field_id)
    target_id = str(target.get("id"))
    if current_tab and str(current_tab) == target_id:
        return {"id": str(field_id), "sectionName": target_name, "diff": None}

    removed_from = str(current_tab) if current_tab else None
    if removed_from:
        jira_client.remove_field_from_tab(cfg, removed_from, str(field_id))
    try:
        jira_client.add_field_to_tab(cfg, target_id, str(field_id))
    except Exception:
        # Jira requires the field to leave its current tab before another tab
        # accepts it. If the second call fails, best-effort rollback avoids
        # silently leaving the field off the configured screen.
        if removed_from:
            try:
                jira_client.add_field_to_tab(cfg, removed_from, str(field_id))
            except Exception:
                pass
        raise
    return {"id": str(field_id), "sectionName": target_name, "diff": None}


# ---------------------------------------------------------------------------
# Fields
# ---------------------------------------------------------------------------

def create_field(payload: Dict[str, Any], actor: Optional[str] = None) -> Dict[str, Any]:
    """Create a custom field and place it on a tab of the pinned screen.

    Jira creates the field org-wide first, then it has to be *added to a screen*
    to appear on the form at all — a field that exists but is on no screen is
    invisible and is the single most common "I created it and nothing happened"
    confusion, so placement is part of creating it here rather than a second step.
    """
    _row, cfg = _config_and_cfg()
    label = str(payload.get("label") or "").strip()
    if not label:
        raise ValueError("A field label is required.")
    type_key = str(payload.get("type") or "Text").strip()
    field_type = CREATABLE_TYPES.get(type_key)
    if field_type is None:
        known = ", ".join(sorted(CREATABLE_TYPES))
        raise ValueError(f"Unsupported field type '{type_key}'. Supported: {known}.")

    created = jira_client.create_field(
        cfg, label, field_type, description=str(payload.get("description") or "")
    )
    field_id = str(created.get("id") or "")
    if not field_id:
        raise JiraError("Jira did not return an id for the new field.", 502)

    section_name = str(payload.get("sectionName") or "").strip()
    screen = jira_client.get_screen(cfg, fresh=True)
    sections = screen.get("sections") or []
    target = None
    if section_name:
        target = next(
            (
                s
                for s in sections
                if str(s.get("name") or "").strip().casefold() == section_name.casefold()
            ),
            None,
        )
    target = target or (sections[0] if sections else None)
    if target is None:
        raise JiraError(
            f"Field {field_id} was created but the screen has no tab to place it on.", 409
        )
    jira_client.add_field_to_tab(cfg, str(target.get("id")), field_id)

    # Seed the option list when one was supplied with the create.
    values = payload.get("values")
    if values and field_type in _OPTION_TYPES:
        jira_client.set_options(cfg, field_id, _normalize_option_list(values))

    updated = jira_client.field_on_screen(cfg, field_id) or {
        "id": field_id,
        "name": label,
        "custom": True,
        "type": field_type,
    }
    return _field_dict(cfg, updated, _auto_managed_ids(_row))


def update_field(field_id: str, payload: Dict[str, Any]) -> Dict[str, Any]:
    """Rename a custom field.

    Jira's field resource exposes name + description only; "required" belongs to
    the issue-type field configuration, which this editor deliberately does not
    reach into — changing it there affects every screen the field appears on.
    """
    row, cfg = _config_and_cfg()
    field = _find_field(cfg, field_id)
    if not field.get("custom"):
        raise ValueError("Only custom fields can be edited — this is a Jira system field.")
    label = str(payload.get("label") or "").strip()
    if not label:
        raise ValueError("A field label is required.")
    jira_client.update_field(
        cfg, str(field_id), label, description=str(payload.get("description") or "")
    )
    updated = jira_client.field_on_screen(cfg, str(field_id)) or {**field, "name": label}
    return _field_dict(cfg, updated, _auto_managed_ids(row))


def delete_field(field_id: str, payload: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Take a field off the screen, and optionally trash the field itself.

    Two very different operations, so they are one explicit switch rather than
    one guessed-at default: removing from the screen is reversible and local to
    this form; deleting sends the field (and its value on every issue in the
    site) to the Jira trash.
    """
    row, cfg = _config_and_cfg()
    field = _find_field(cfg, field_id)
    hard_delete = bool((payload or {}).get("deleteField"))
    if hard_delete and not field.get("custom"):
        raise ValueError("Jira system fields cannot be deleted.")

    for legacy_name, legacy_id in sources.legacy_field_ids(row).items():
        if legacy_id and legacy_id == str(field_id):
            raise ValueError(
                f"The {legacy_name.capitalize()} field is published by the sync — clear it "
                "in Settings before removing it from the screen."
            )

    tab_id = _tab_of_field(cfg, field_id)
    if tab_id:
        jira_client.remove_field_from_tab(cfg, tab_id, str(field_id))
    if hard_delete:
        jira_client.delete_field(cfg, str(field_id))
    # A binding pointing at a field that is gone would silently fail every sync.
    sources.delete_binding(str(field_id), PROVIDER)
    return {
        "id": str(field_id),
        "removedFromScreen": bool(tab_id),
        "deleted": hard_delete,
    }


# ---------------------------------------------------------------------------
# Option lists
# ---------------------------------------------------------------------------

def _normalize_option_list(values: Any) -> List[str]:
    """Sanitize + de-dup a hand-typed option list.

    Uses the same sanitizer as Zoho even though Jira accepts more characters: an
    operator who moves a field list between the two providers should get the same
    spelling, and the sync's own values are already reduced this way.
    """
    raw = values if isinstance(values, (list, tuple)) else str(values or "").split(",")
    out: List[str] = []
    seen = set()
    for v in raw:
        s = _sanitize_value(v)
        if s and s.casefold() not in seen:
            seen.add(s.casefold())
            out.append(s)
    return out


def set_field_options(field_id: str, payload: Dict[str, Any]) -> Dict[str, Any]:
    """Replace a dropdown's option list by hand (add / rename / retire / reorder).

    Guarded: the field must live on the pinned screen and be an option field.
    Options that fall out are disabled rather than deleted — Jira refuses to
    delete an option an issue still uses, and a closed issue must keep rendering.
    """
    row, cfg = _config_and_cfg()
    field = _find_field(cfg, field_id)
    if field.get("type") not in _OPTION_TYPES:
        raise ValueError("Only dropdown (select) fields have an option list.")

    values = _normalize_option_list(payload.get("values"))
    jira_client.set_options(cfg, str(field_id), values)
    updated = jira_client.field_on_screen(cfg, str(field_id)) or field
    return _field_dict(cfg, updated, _auto_managed_ids(row))


# ---------------------------------------------------------------------------
# Option-source bindings — "this dropdown's options come from Kubernetes"
# ---------------------------------------------------------------------------

def _option_field_on_screen(cfg, field_id: str) -> Dict[str, Any]:
    field = jira_client.field_on_screen(cfg, str(field_id))
    if field is None:
        raise LookupError("That field is not on the configured Jira screen.")
    if field.get("type") not in _OPTION_TYPES:
        raise ValueError("Only dropdown (select) fields can be bound to a source.")
    return field


def _reject_legacy_field(row, field_id: str) -> None:
    for name, legacy_id in sources.legacy_field_ids(row).items():
        if legacy_id and legacy_id == str(field_id):
            raise ValueError(
                f"The {name.capitalize()} field is published by the sync itself — "
                "change its source on the Source tab instead of binding it here."
            )


def list_option_sources() -> Dict[str, Any]:
    """The source catalogue + every current binding, for the picker UI."""
    row = get_or_create_config()
    return {
        "sources": sources.describe_sources(),
        "bindings": [sources.serialize(b) for b in sources.all_bindings(row, PROVIDER)],
    }


def get_field_binding(field_id: str) -> Optional[Dict[str, Any]]:
    """One field's binding, or None. Locked (legacy) fields report theirs too."""
    row = get_or_create_config()
    for binding in sources.all_bindings(row, PROVIDER):
        if binding.field_id == str(field_id):
            return sources.serialize(binding)
    return None


def set_field_binding(field_id: str, payload: Dict[str, Any]) -> Dict[str, Any]:
    """Create or replace a dropdown's option-source binding.

    Validated against the live screen so a binding can never point at a field that
    is not there, is not a dropdown, or would form a cascade cycle.
    """
    row, cfg = _config_and_cfg()
    field_id = str(field_id)
    _reject_legacy_field(row, field_id)
    field = _option_field_on_screen(cfg, field_id)

    source_kind = str(payload.get("sourceKind") or "").strip()
    kind = sources.SOURCE_KINDS.get(source_kind)
    if kind is None:
        known = ", ".join(sorted(sources.SOURCE_KINDS))
        raise ValueError(f"Unknown option source '{source_kind}'. Known sources: {known}.")

    parent_field_id = str(payload.get("parentFieldId") or "").strip()
    if parent_field_id:
        if not kind.parent_kind:
            raise ValueError(f"The '{kind.label}' source does not cascade from another field.")
        if parent_field_id == field_id:
            raise ValueError("A field cannot cascade from itself.")
        _option_field_on_screen(cfg, parent_field_id)
        parent = next(
            (b for b in sources.all_bindings(row, PROVIDER) if b.field_id == parent_field_id),
            None,
        )
        if parent is None:
            raise ValueError("The parent field has no option source of its own yet.")
        if parent.source_kind != kind.parent_kind:
            wanted = sources.SOURCE_KINDS[kind.parent_kind].label
            raise ValueError(
                f"'{kind.label}' options are grouped by {wanted.lower()}, so the parent "
                f"field must be bound to that source (it is bound to '{parent.source_kind}')."
            )

    params = payload.get("params")
    if params is not None and not isinstance(params, dict):
        raise ValueError("Binding parameters must be an object.")
    enabled = bool(payload.get("enabled", True))
    label = str(payload.get("label") or "").strip() or field.get("name") or f"Field {field_id}"

    # Dry-run the resulting cascade graph before writing it.
    prospective = [
        b for b in sources.all_bindings(row, PROVIDER) if b.field_id != field_id
    ] + [
        sources.Binding(
            field_id=field_id,
            label=label,
            source_kind=source_kind,
            params=params or {},
            parent_field_id=parent_field_id or None,
            enabled=enabled,
        )
    ]
    sources.check_cascade(prospective)

    stored = sources.upsert_binding(
        row,
        field_id,
        source_kind,
        label=label,
        api_name=field_id,
        params=params or {},
        parent_field_id=parent_field_id or None,
        enabled=enabled,
        provider=PROVIDER,
    )
    return sources.serialize(sources._from_row(stored))


def delete_field_binding(field_id: str) -> bool:
    """Drop a binding. The field keeps whatever options it currently holds."""
    return sources.delete_binding(str(field_id), PROVIDER)


def preview_field_binding(field_id: str, payload: Dict[str, Any]) -> Dict[str, Any]:
    """Resolve a source without saving or publishing — what the next sync would send."""
    row = get_or_create_config()
    source_kind = str(payload.get("sourceKind") or "").strip()
    if source_kind not in sources.SOURCE_KINDS:
        raise ValueError(f"Unknown option source '{source_kind}'.")
    params = payload.get("params") if isinstance(payload.get("params"), dict) else {}
    binding = sources.Binding(
        field_id=str(field_id),
        label=str(payload.get("label") or "") or f"Field {field_id}",
        source_kind=source_kind,
        params=params,
        parent_field_id=str(payload.get("parentFieldId") or "") or None,
    )
    try:
        entries = svc_source_entries(
            row, provider=PROVIDER, fresh=bool(payload.get("fresh"))
        )
    except ValueError as exc:
        # No source configured yet is a normal state, not an error page.
        return {"values": [], "count": 0, "byParent": {}, "error": str(exc)}

    ctx = sources.SourceContext(row, entries, PROVIDER, fresh=bool(payload.get("fresh")))
    options = sources.resolve(binding, ctx)
    by_parent = options.by_parent
    if binding.parent_field_id:
        parent = next(
            (
                b
                for b in sources.all_bindings(row, PROVIDER)
                if b.field_id == binding.parent_field_id
            ),
            None,
        )
        if parent is not None:
            parent_options = sources.resolve(parent, ctx)
            by_parent = sources.align_by_parent(
                options.by_parent, parent_options.canon, options.canon
            )
    return {
        "values": options.values,
        "count": options.count,
        "byParent": by_parent,
        "error": None,
    }
