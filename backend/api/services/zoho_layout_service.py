"""Safe whole-layout writes for the pinned Zoho Desk layout.

Desk exposes section editing only through ``PATCH /layouts/{id}``, which is a
**whole-layout replace**: the body must carry the complete ``sections`` array,
and any field missing from it is unassociated from the layout. On Areeba's live
DevOps Request layout that would silently drop fields from the ticket form.

So nothing calls :func:`zoho_client.update_layout` directly. Every write goes:

    read fresh -> apply a declarative mutation -> rebuild the full body
    -> assert only the intended thing changed -> PATCH

:func:`plan_layout_write` runs everything except the PATCH and returns the exact
body plus a diff, so an operator can inspect a write before committing it — and
so the first live run can be verified without touching the layout at all.

Field echo policy: Desk's GET returns more keys than its PATCH accepts, and
unexpected properties draw a 422. We echo a known-safe subset rather than
round-tripping blindly, and report every key we dropped in the plan output so a
genuinely required one surfaces during bring-up instead of in production.
"""

from __future__ import annotations

import copy
import os
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Sequence, Set, Tuple

from ..db import db
from ..models import ZohoLayoutSnapshot
from . import zoho_client
from .zoho_sync_service import _sync_lock, _to_client_config, get_or_create_config

# A bad whole-layout PATCH can wreck the section structure and every field's
# mandatory/allowed-value state for a whole department — categorically worse
# than the picklist writes ZOHO_ALLOWED_LAYOUT_IDS already guards. Writes stay
# off until an operator has run the dry-run against the real layout and opted in.
_WRITE_ENABLED_VAR = "ZOHO_LAYOUT_WRITE_ENABLED"
# Pre-write snapshots kept per layout (same idea as INBOUND_TICKET_RETENTION).
_SNAPSHOT_RETENTION = 10

# Echoed for every field: `id` identifies it, `isMandatory` is required by the
# layout schema.
_FIELD_ECHO_KEYS = ("isMandatory",)
# Picklists must echo their option list or Desk rebuilds the field empty.
_PICKLIST_ECHO_KEYS = ("allowedValues", "defaultValue", "sortBy", "isNested")
# Status / ticket-status fields lose their reopen behaviour without this.
_CONDITIONAL_ECHO_KEYS = ("restoreOnReplyValues",)

# Layout-level keys the PATCH body carries. The first four plus `sections` and
# `skipDeptAccessValidation` are required by the OAS; the last two are optional
# but echoed so a write never blanks them.
_REQUIRED_LAYOUT_KEYS = ("departmentId", "isDefaultLayout", "layoutName", "module")
_OPTIONAL_LAYOUT_KEYS = ("layoutDisplayName", "layoutDesc")

# Field keys a GET returns that we deliberately never send back. Listing them
# keeps ``droppedKeys`` in the plan output signal rather than noise.
_IGNORED_FIELD_KEYS = frozenset(
    {
        "id",
        "apiName",
        "displayLabel",
        "label",
        "type",
        "isCustomField",
        "isSystemMandatory",
        "isVisible",
        "isEditable",
        "isReadOnly",
        "isEncrypted",
        "isPiiData",
        "maxLength",
        "toolTip",
        "i18nLabel",
        "i18NLabel",
        "createdTime",
        "modifiedTime",
    }
)

_SECTION_IGNORED_KEYS = frozenset({"id", "name", "i18NLabel", "i18nLabel", "fields", "isSubSection"})


class LayoutWriteError(ValueError):
    """A rebuilt layout body failed its safety guards — nothing was sent."""


# ---------------------------------------------------------------------------
# Reading
# ---------------------------------------------------------------------------

def _read_layout(fresh: bool = True) -> Tuple[Any, Dict[str, Any]]:
    """``(cfg, layout)`` read straight from Zoho — writes never trust the cache."""
    row = get_or_create_config()
    cfg = _to_client_config(row)
    return cfg, zoho_client.get_layout(cfg, fresh=fresh)


def _sections_of(layout: Dict[str, Any]) -> List[Dict[str, Any]]:
    sections = layout.get("sections")
    return [s for s in sections if isinstance(s, dict)] if isinstance(sections, list) else []


def _fields_of(section: Dict[str, Any]) -> List[Dict[str, Any]]:
    fields = section.get("fields")
    return [f for f in fields if isinstance(f, dict)] if isinstance(fields, list) else []


def _field_ids(sections: Sequence[Dict[str, Any]]) -> List[str]:
    """Every field id across the sections, in order, WITH duplicates kept.

    Duplicates matter: a mutation that copies rather than moves a field would
    otherwise slip past a set-based comparison.
    """
    out: List[str] = []
    for section in sections:
        for field in _fields_of(section):
            if field.get("id") is not None:
                out.append(str(field["id"]))
    return out


def _section_names(sections: Sequence[Dict[str, Any]]) -> List[str]:
    return [str(s.get("name") or "") for s in sections]


def _section_ordinal(section: Dict[str, Any], position: int) -> Any:
    """A section's id for the write.

    Existing ids are echoed **verbatim** — renumbering a section is how you make
    Desk treat it as new and orphan its fields, and Zoho's own example uses small
    ordinals while a live layout may return snowflake ids. Only a section that
    arrived without an id gets its 1-based read position (see
    :func:`_assert_consistent_section_ids` — we refuse to mix the two).
    """
    raw = section.get("id")
    return position + 1 if raw in (None, "") else raw


def _assert_consistent_section_ids(sections: Sequence[Dict[str, Any]]) -> List[str]:
    """Refuse to write when only *some* sections carry an id.

    Synthesizing ``1, 2`` alongside echoed 18-digit ids is the likeliest way to
    collide two sections onto one id. Returns the names whose id we synthesized.
    """
    with_id = [s for s in sections if s.get("id") not in (None, "")]
    without = [s for s in sections if s.get("id") in (None, "")]
    if with_id and without:
        raise LayoutWriteError(
            "Zoho returned sections with inconsistent ids "
            f"({len(with_id)} with, {len(without)} without) — refusing to write the "
            "layout because synthesized ids could collide with real ones."
        )
    return [str(s.get("name") or "") for s in without]


def _find_section(sections: Sequence[Dict[str, Any]], name: str) -> Optional[Dict[str, Any]]:
    target = (name or "").strip().casefold()
    for section in sections:
        if str(section.get("name") or "").strip().casefold() == target:
            return section
    return None


def _locate_field(
    sections: Sequence[Dict[str, Any]], field_id: str
) -> Tuple[Optional[Dict[str, Any]], Optional[Dict[str, Any]]]:
    """``(section, field)`` for ``field_id``, or ``(None, None)``."""
    target = str(field_id)
    for section in sections:
        for field in _fields_of(section):
            if str(field.get("id")) == target:
                return section, field
    return None, None


# ---------------------------------------------------------------------------
# Body construction
# ---------------------------------------------------------------------------

def _field_write(field: Dict[str, Any], dropped: Set[str]) -> Dict[str, Any]:
    """One field entry for the PATCH body, recording keys we chose not to send."""
    out: Dict[str, Any] = {"id": str(field.get("id"))}
    for key in _FIELD_ECHO_KEYS:
        out[key] = bool(field.get(key))

    keys = set(_FIELD_ECHO_KEYS)
    if field.get("type") == "Picklist" or field.get("allowedValues") is not None:
        for key in _PICKLIST_ECHO_KEYS:
            if field.get(key) is not None:
                out[key] = field[key]
            keys.add(key)
    for key in _CONDITIONAL_ECHO_KEYS:
        if field.get(key) is not None:
            out[key] = field[key]
        keys.add(key)

    for key in field:
        if key not in keys and key not in _IGNORED_FIELD_KEYS:
            dropped.add(key)
    return out


def _section_write(
    section: Dict[str, Any], position: int, dropped: Set[str]
) -> Dict[str, Any]:
    name = str(section.get("name") or "")
    for key in section:
        if key not in _SECTION_IGNORED_KEYS:
            dropped.add(f"section.{key}")
    return {
        "id": _section_ordinal(section, position),
        "name": name,
        "i18NLabel": str(section.get("i18NLabel") or section.get("i18nLabel") or name),
        "fields": [_field_write(f, dropped) for f in _fields_of(section)],
    }


def _department_id(layout: Dict[str, Any], row) -> Any:
    dept = layout.get("departmentId")
    if dept in (None, ""):
        nested = layout.get("department")
        if isinstance(nested, dict):
            dept = nested.get("id")
    if dept in (None, ""):
        dept = getattr(row, "department_id", None)
    return dept


def _build_body(
    layout: Dict[str, Any], sections: Sequence[Dict[str, Any]], row
) -> Tuple[Dict[str, Any], List[str]]:
    """The complete PATCH body plus the sorted list of keys we did not echo.

    Identity keys come from the READ, never from a guess: defaulting
    ``isDefaultLayout`` could demote (or promote) the department's default
    layout, and a wrong ``departmentId`` could move the layout between
    departments. If Zoho did not return them, abort rather than invent them.
    """
    dropped: Set[str] = set()
    body: Dict[str, Any] = {
        # NOT layout state — a request-behaviour flag Desk will not return.
        # Always false: we want the department access validation to run.
        "skipDeptAccessValidation": False,
        "sections": [_section_write(s, i, dropped) for i, s in enumerate(sections)],
    }
    for key in _OPTIONAL_LAYOUT_KEYS:
        if layout.get(key) is not None:
            body[key] = layout[key]

    body["departmentId"] = _department_id(layout, row)
    body["isDefaultLayout"] = layout.get("isDefaultLayout")
    body["layoutName"] = layout.get("layoutName") or layout.get("name")
    # `tickets` is the only module this integration is wired for, so inferring
    # it is safe in a way the other three are not.
    body["module"] = layout.get("module") or "tickets"

    missing = [k for k in _REQUIRED_LAYOUT_KEYS if body.get(k) in (None, "")]
    if missing:
        raise LayoutWriteError(
            "Zoho's layout read is missing " + ", ".join(missing) + " — refusing to "
            "write the layout, because guessing these could move it between "
            "departments or change which layout is the default."
        )
    return body, sorted(dropped)


# ---------------------------------------------------------------------------
# Mutations
# ---------------------------------------------------------------------------

def add_section(name: str) -> Dict[str, Any]:
    """Mutation: append an empty section."""
    return {"kind": "add_section", "name": name}


def place_field(
    field_id: str,
    section_name: str,
    after_field_id: Optional[str] = None,
    field_data: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Mutation: move ``field_id`` into ``section_name`` (optionally after a sibling)."""
    return {
        "kind": "place_field",
        "fieldId": str(field_id),
        "sectionName": section_name,
        "afterFieldId": str(after_field_id) if after_field_id else None,
        "fieldData": copy.deepcopy(field_data) if isinstance(field_data, dict) else None,
    }


def _apply_add_section(
    sections: List[Dict[str, Any]], mutation: Dict[str, Any]
) -> Dict[str, Any]:
    name = str(mutation.get("name") or "").strip()
    if not name:
        raise LayoutWriteError("A section name is required.")
    if len(name) > 50:
        raise LayoutWriteError("Section names are limited to 50 characters.")
    if _find_section(sections, name) is not None:
        raise LayoutWriteError(f"A section named '{name}' already exists on this layout.")

    ordinals: List[int] = []
    for i, section in enumerate(sections):
        try:
            ordinals.append(int(str(_section_ordinal(section, i))))
        except (TypeError, ValueError):
            raise LayoutWriteError(
                f"Section '{section.get('name')}' has a non-numeric id "
                f"({section.get('id')!r}) — cannot derive an id for a new section. "
                "Inspect the dry-run output before retrying."
            )
    sections.append(
        {"id": (max(ordinals) + 1) if ordinals else 1, "name": name, "i18NLabel": name, "fields": []}
    )
    return {"addedSections": [name], "removedSections": [], "addedFields": [], "removedFields": []}


def _apply_place_field(
    sections: List[Dict[str, Any]], mutation: Dict[str, Any]
) -> Dict[str, Any]:
    field_id = str(mutation.get("fieldId") or "").strip()
    section_name = str(mutation.get("sectionName") or "").strip()
    if not field_id:
        raise LayoutWriteError("A field id is required.")
    target = _find_section(sections, section_name)
    if target is None:
        raise LayoutWriteError(f"No section named '{section_name}' on this layout.")

    source, field = _locate_field(sections, field_id)
    added: List[str] = []
    if field is None:
        # Desk normally auto-places a field created with `layoutId`, but if it
        # did not, placing it is an ADD rather than a move. Picklists must carry
        # their values in this first layout write or Desk rejects the whole body.
        supplied = mutation.get("fieldData")
        field = copy.deepcopy(supplied) if isinstance(supplied, dict) else {}
        field["id"] = field_id
        field.setdefault("isMandatory", False)
        added.append(field_id)
    else:
        source["fields"] = [f for f in _fields_of(source) if str(f.get("id")) != field_id]

    dest = _fields_of(target)
    after = mutation.get("afterFieldId")
    index = len(dest)
    if after:
        for i, existing in enumerate(dest):
            if str(existing.get("id")) == str(after):
                index = i + 1
                break
    dest.insert(index, field)
    target["fields"] = dest
    return {
        "addedSections": [],
        "removedSections": [],
        "addedFields": added,
        "removedFields": [],
    }


_MUTATIONS = {"add_section": _apply_add_section, "place_field": _apply_place_field}


# ---------------------------------------------------------------------------
# Guards
# ---------------------------------------------------------------------------

def _guard(
    before: Sequence[Dict[str, Any]],
    after: Sequence[Dict[str, Any]],
    expected: Dict[str, Any],
) -> None:
    """Refuse to send a body that changes anything the mutation did not declare."""
    before_ids, after_ids = _field_ids(before), _field_ids(after)

    duplicates = sorted({fid for fid in after_ids if after_ids.count(fid) > 1})
    if duplicates:
        raise LayoutWriteError(
            "Refusing to write the layout: field(s) "
            f"{', '.join(duplicates)} would appear in more than one section."
        )

    expected_added = {str(f) for f in expected.get("addedFields") or []}
    expected_removed = {str(f) for f in expected.get("removedFields") or []}
    lost = (set(before_ids) - set(after_ids)) - expected_removed
    if lost:
        raise LayoutWriteError(
            "Refusing to write the layout: it would remove field(s) "
            f"{', '.join(sorted(lost))} from the form. This is a bug in the layout "
            "writer — no change was sent to Zoho."
        )
    gained = (set(after_ids) - set(before_ids)) - expected_added
    if gained:
        raise LayoutWriteError(
            "Refusing to write the layout: unexpected field(s) "
            f"{', '.join(sorted(gained))} appeared."
        )

    before_names, after_names = _section_names(before), _section_names(after)
    want = list(before_names)
    for name in expected.get("removedSections") or []:
        if name in want:
            want.remove(name)
    want.extend(expected.get("addedSections") or [])
    if sorted(after_names) != sorted(want):
        raise LayoutWriteError(
            "Refusing to write the layout: the section list changed unexpectedly "
            f"({', '.join(before_names)} -> {', '.join(after_names)})."
        )

    _guard_field_attrs(before, after)


def _field_attr_projection(sections: Sequence[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    """``{field_id: the exact attrs we will echo}`` — the thing a bad write corrupts."""
    dropped: Set[str] = set()
    out: Dict[str, Dict[str, Any]] = {}
    for section in sections:
        for field in _fields_of(section):
            if field.get("id") is not None:
                out[str(field["id"])] = _field_write(field, dropped)
    return out


def _guard_field_attrs(
    before: Sequence[Dict[str, Any]], after: Sequence[Dict[str, Any]]
) -> None:
    """No field's mandatory flag or option list may change as a side effect.

    A whole-layout replace rewrites every field's state, so a writer bug that
    normalized ``allowedValues`` (Desk returns both bare strings and
    ``{"value": ...}`` objects) or reset ``isMandatory`` would silently corrupt
    every ticket form in the department. Only section membership may change here
    — attribute edits go through the dedicated field endpoints.
    """
    before_attrs, after_attrs = _field_attr_projection(before), _field_attr_projection(after)
    changed = [
        fid
        for fid, attrs in after_attrs.items()
        if fid in before_attrs and before_attrs[fid] != attrs
    ]
    if changed:
        raise LayoutWriteError(
            "Refusing to write the layout: it would change the required flag or "
            f"option list of field(s) {', '.join(sorted(changed))} as a side effect. "
            "This is a bug in the layout writer — no change was sent to Zoho."
        )


def _diff(
    before: Sequence[Dict[str, Any]], after: Sequence[Dict[str, Any]]
) -> Dict[str, Any]:
    """A UI-friendly summary of what the write does."""
    before_by_name = {str(s.get("name") or ""): s for s in before}
    after_by_name = {str(s.get("name") or ""): s for s in after}
    rows = []
    for name in list(before_by_name) + [n for n in after_by_name if n not in before_by_name]:
        old, new = before_by_name.get(name), after_by_name.get(name)
        if new is None:
            change = "removed"
        elif old is None:
            change = "added"
        elif len(_fields_of(old)) != len(_fields_of(new)) or _field_ids([old]) != _field_ids([new]):
            change = "changed"
        else:
            change = "unchanged"
        rows.append(
            {
                "name": name,
                "change": change,
                "fieldCount": len(_fields_of(new)) if new is not None else 0,
                "previousFieldCount": len(_fields_of(old)) if old is not None else 0,
            }
        )
    return {
        "sections": rows,
        "sectionsAdded": sum(1 for r in rows if r["change"] == "added"),
        "sectionsChanged": sum(1 for r in rows if r["change"] == "changed"),
        "sectionsUnchanged": sum(1 for r in rows if r["change"] == "unchanged"),
        "fieldsCarried": len(_field_ids(after)),
        "fieldsDropped": sorted(set(_field_ids(before)) - set(_field_ids(after))),
    }


# ---------------------------------------------------------------------------
# Public entry points
# ---------------------------------------------------------------------------

def writes_enabled() -> bool:
    return os.getenv(_WRITE_ENABLED_VAR, "").strip().lower() in ("1", "true", "yes", "on")


def _snapshot(layout_id: str, layout: Dict[str, Any], reason: str, actor: Optional[str]) -> None:
    """Persist the pre-write layout so a bad PATCH is recoverable, not terminal."""
    db.session.add(
        ZohoLayoutSnapshot(
            layout_id=str(layout_id),
            reason=reason[:80],
            actor=(actor or "")[:120],
            payload=layout,
            taken_at=datetime.now(timezone.utc),
        )
    )
    db.session.flush()
    stale = (
        ZohoLayoutSnapshot.query.filter_by(layout_id=str(layout_id))
        .order_by(ZohoLayoutSnapshot.taken_at.desc())
        .offset(_SNAPSHOT_RETENTION)
        .all()
    )
    for row in stale:
        db.session.delete(row)
    db.session.commit()


def snapshot_layout(reason: str, actor: Optional[str] = None) -> Dict[str, Any]:
    """Snapshot the live layout before a structural change made OUTSIDE the
    whole-layout writer.

    Only ``unassociate`` needs this: it removes a field through its own Desk
    endpoint, so it never passes the writer's guards. Taking the snapshot first
    is what keeps "we retired the wrong field" recoverable.
    """
    cfg, layout = _read_layout(fresh=True)
    if not _sections_of(layout):
        raise LayoutWriteError(
            "Zoho returned a layout with no sections — refusing to change it."
        )
    _snapshot(str(layout.get("id") or cfg.layout_id), layout, reason, actor)
    return layout


def _plan(
    mutations: Sequence[Dict[str, Any]], row, cfg, layout: Dict[str, Any]
) -> Dict[str, Any]:
    before = _sections_of(layout)
    if not before:
        raise LayoutWriteError(
            "Zoho returned a layout with no sections — refusing to write it back."
        )
    synthesized = _assert_consistent_section_ids(before)

    after = copy.deepcopy(before)
    expected: Dict[str, List[str]] = {
        "addedSections": [],
        "removedSections": [],
        "addedFields": [],
        "removedFields": [],
    }
    for mutation in mutations:
        handler = _MUTATIONS.get(str(mutation.get("kind") or ""))
        if handler is None:
            raise LayoutWriteError(f"Unknown layout mutation '{mutation.get('kind')}'.")
        for key, values in handler(after, mutation).items():
            expected[key].extend(values)

    _guard(before, after, expected)
    body, dropped = _build_body(layout, after, row)

    warnings: List[str] = []
    if dropped:
        warnings.append(
            "Returned by Zoho but not echoed back: "
            + ", ".join(dropped)
            + ". Confirm none of these matter before writing to a production layout."
        )
    if synthesized:
        warnings.append(
            "Zoho returned no id for section(s) "
            + ", ".join(synthesized)
            + " — their read position was used instead."
        )
    if not writes_enabled():
        warnings.append(
            f"Layout writes are disabled. Set {_WRITE_ENABLED_VAR}=true once this "
            "dry-run looks correct."
        )
    return {
        "layoutId": str(layout.get("id") or cfg.layout_id),
        "layoutName": layout.get("name") or body.get("layoutName") or "",
        "body": body,
        "diff": _diff(before, after),
        "droppedKeys": dropped,
        "synthesizedSectionIds": synthesized,
        "writesEnabled": writes_enabled(),
        "warnings": warnings,
    }


def plan_layout_write(mutations: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
    """Build + guard the body for ``mutations`` WITHOUT sending it.

    The dry-run behind the UI's confirm step, and the way to validate this writer
    against a production layout before enabling writes at all.
    """
    row = get_or_create_config()
    cfg = _to_client_config(row)
    return _plan(mutations, row, cfg, zoho_client.get_layout(cfg, fresh=True))


def apply_layout_write(
    mutations: Sequence[Dict[str, Any]], *, reason: str = "layout_write", actor: Optional[str] = None
) -> Dict[str, Any]:
    """Plan, snapshot, PATCH, then verify. Returns the plan plus ``applied``.

    Held under the sync lock: ``sync_now`` rewrites picklist option lists, and
    this body echoes them — an interleaved write would silently revert whatever
    the sync just published.
    """
    if not writes_enabled():
        raise PermissionError(
            "Layout writes are disabled. Review the dry-run "
            f"(POST /api/zoho/layout/plan), then set {_WRITE_ENABLED_VAR}=true to enable them."
        )
    with _sync_lock:
        row = get_or_create_config()
        cfg = _to_client_config(row)
        layout = zoho_client.get_layout(cfg, fresh=True)
        plan = _plan(mutations, row, cfg, layout)

        _snapshot(plan["layoutId"], layout, reason, actor)
        zoho_client.update_layout(cfg, plan["body"])

        # Confirm Zoho actually stored what we sent — a whole-layout replace that
        # half-applied is worse than one that failed outright.
        verify = zoho_client.get_layout(cfg, fresh=True)
        sent_ids = set(_field_ids(_sections_of({"sections": plan["body"]["sections"]})))
        live_ids = set(_field_ids(_sections_of(verify)))
        lost = sent_ids - live_ids
        if lost:
            raise LayoutWriteError(
                "The layout was written but Zoho no longer reports field(s) "
                f"{', '.join(sorted(lost))}. Restore from the snapshot taken before "
                "this write."
            )
        return {**plan, "applied": True, "verifiedFields": len(live_ids)}
