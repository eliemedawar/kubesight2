"""Manage the DevOps Request layout's fields + dropdowns from KubeSight.

A 1:1 editor over the Zoho Desk layout: read its sections/fields, manage any
picklist's option list, edit a field's label/required, and (scope permitting)
create a new custom field. Everything routes through ``zoho_client`` and is
pinned to the single allowed layout by ``_assert_layout_allowed`` +
``field_on_layout`` — the master rule holds even if the token has full scope.

Note: ``cf_application`` and ``cf_environment`` are auto-published by the sync
(deployments / namespaces). Editing their options here is allowed but the next
sync overwrites them — the UI flags them as auto-managed.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from ..models import ZohoIntegration
from . import zoho_client
from . import zoho_layout_service as layout_svc
from . import zoho_option_sources as sources
from .zoho_client import ZohoError
from .zoho_sync_service import (
    NONE_VALUE,
    _sanitize_value,
    _source_entries as svc_source_entries,
    _to_client_config,
    get_or_create_config,
)


def _config_and_cfg():
    row = get_or_create_config()
    return row, _to_client_config(row)


def _allowed_values(field: Dict[str, Any]) -> Optional[List[str]]:
    av = field.get("allowedValues")
    if av is None:
        return None
    out: List[str] = []
    for entry in av:
        val = entry.get("value") if isinstance(entry, dict) else entry
        if val is not None:
            out.append(str(val))
    return out


def _field_dict(
    field: Dict[str, Any],
    auto_managed_ids: set,
    bindings: Optional[Dict[str, Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    fid = str(field.get("id") or "")
    return {
        "id": fid,
        "apiName": field.get("apiName"),
        "label": field.get("displayLabel") or field.get("label") or field.get("apiName"),
        "type": field.get("type"),
        "required": bool(field.get("isMandatory")),
        "custom": bool(field.get("isCustomField")),
        "isPicklist": (field.get("type") == "Picklist"),
        "allowedValues": _allowed_values(field),
        "defaultValue": field.get("defaultValue"),
        # cf_application / cf_environment are pushed by the sync — flag them.
        "autoManaged": fid in auto_managed_ids,
        # Carried inline so a card can show its source chip without a fetch per field.
        "binding": (bindings or {}).get(fid),
    }


def _auto_managed_ids(row) -> set:
    """Field ids the sync currently owns (only when their per-field toggle is on)."""
    ids = set()
    if row.sync_application and row.app_field_id:
        ids.add(str(row.app_field_id))
    if row.sync_environment and row.environment_field_id:
        ids.add(str(row.environment_field_id))
    if row.sync_variables and row.variable_field_id:
        ids.add(str(row.variable_field_id))
    return ids


def get_layout_structure(fresh: bool = False) -> Dict[str, Any]:
    """Read the pinned layout and return its sections + fields for the editor UI.

    ``fresh=True`` (manual refresh) bypasses the layout read cache.
    """
    row, cfg = _config_and_cfg()
    auto = _auto_managed_ids(row)
    bindings = sources.bindings_by_field(row)
    layout = zoho_client.get_layout(cfg, fresh=fresh)
    sections = []
    for index, sec in enumerate(layout.get("sections") or []):
        sections.append(
            {
                # The id is what a layout write keys on; the editor needs it to
                # target a section, so it is no longer discarded here.
                "id": sec.get("id"),
                "index": index,
                "name": sec.get("name"),
                "fields": [_field_dict(f, auto, bindings) for f in (sec.get("fields") or [])],
            }
        )
    return {
        "layoutId": str(layout.get("id") or cfg.layout_id),
        "layoutName": layout.get("name") or "DevOps Request",
        "sections": sections,
        # Served from the backend so the UI's type list cannot drift from the
        # set the API actually accepts.
        "creatableTypes": sorted(CREATABLE_TYPES),
        "layoutWritesEnabled": layout_svc.writes_enabled(),
    }


def create_section(name: str, actor: Optional[str] = None) -> Dict[str, Any]:
    """Add an empty section to the layout (whole-layout write — see layout_svc)."""
    result = layout_svc.apply_layout_write(
        [layout_svc.add_section(name)], reason="add_section", actor=actor
    )
    return {"name": str(name).strip(), "diff": result["diff"], "layoutId": result["layoutId"]}


def plan_section(name: str) -> Dict[str, Any]:
    """Dry-run for :func:`create_section` — the exact body, diff, and warnings."""
    return layout_svc.plan_layout_write([layout_svc.add_section(name)])


def _normalize_option_list(values: Any) -> List[str]:
    """Sanitize + de-dup a picklist option list with ``-None-`` guaranteed first."""
    raw = values if isinstance(values, (list, tuple)) else str(values or "").split(",")
    out: List[str] = [NONE_VALUE]
    seen = {NONE_VALUE}
    for v in raw:
        s = _sanitize_value(v)
        if s and s != NONE_VALUE and s not in seen:
            seen.add(s)
            out.append(s)
    return out


def set_field_options(field_id: str, payload: Dict[str, Any]) -> Dict[str, Any]:
    """Replace a picklist field's option list (add/rename/remove/reorder).

    Guarded: the field must live on the pinned layout and be a Picklist.
    """
    row, cfg = _config_and_cfg()
    field = zoho_client.field_on_layout(cfg, str(field_id))
    if field is None:
        raise LookupError("That field is not on the DevOps Request layout.")
    if field.get("type") != "Picklist":
        raise ValueError("Only picklist (dropdown) fields have an option list.")

    values = _normalize_option_list(payload.get("values"))
    default_value = _sanitize_value(payload.get("defaultValue") or NONE_VALUE) or NONE_VALUE
    if default_value not in values:
        default_value = NONE_VALUE
    is_mandatory = bool(payload.get("isMandatory", field.get("isMandatory")))

    zoho_client.set_allowed_values(
        cfg, values, field_id=str(field_id), default_value=default_value, is_mandatory=is_mandatory
    )
    auto = _auto_managed_ids(row)
    updated = zoho_client.field_on_layout(cfg, str(field_id)) or field
    return _field_dict(updated, auto)


# Field types we let the UI create (maps to Zoho Desk fieldType values).
CREATABLE_TYPES = {"Text", "Picklist", "Number", "Decimal", "Date", "DateTime", "Boolean", "Textarea", "Email", "Phone", "URL"}


def update_field(field_id: str, payload: Dict[str, Any]) -> Dict[str, Any]:
    """Edit an existing field's label / required (needs Desk.settings.UPDATE)."""
    row, cfg = _config_and_cfg()
    field = zoho_client.field_on_layout(cfg, str(field_id))
    if field is None:
        raise LookupError("That field is not on the DevOps Request layout.")

    label = str(payload.get("label") or "").strip()
    if not label and "required" not in payload:
        raise ValueError("Nothing to update (send a label and/or required flag).")

    required_change: Optional[bool] = None
    if "required" in payload:
        required = bool(payload.get("required"))
        if required != bool(field.get("isMandatory")):
            required_change = required

    body: Dict[str, Any] = {}
    current_label = field.get("displayLabel") or field.get("label") or ""
    if label and label != current_label:
        body["displayLabel"] = label
    if required_change is not None and field.get("type") != "Picklist":
        body["isMandatory"] = required_change
    if body:
        zoho_client.update_org_field(cfg, str(field_id), body)

    if required_change is not None and field.get("type") == "Picklist":
        # Desk 422s a bare isMandatory flip on a picklist via organizationFields —
        # it must go through the layout-field endpoint with the full value list.
        values = _allowed_values(field) or [NONE_VALUE]
        default = str(field.get("defaultValue") or NONE_VALUE)
        if default not in values:
            default = NONE_VALUE
        zoho_client.set_allowed_values(
            cfg,
            values,
            field_id=str(field_id),
            default_value=default,
            is_mandatory=required_change,
        )

    auto = _auto_managed_ids(row)
    updated = zoho_client.field_on_layout(cfg, str(field_id)) or field
    return _field_dict(updated, auto)


def _section_of_field(cfg, field_id: str) -> Optional[str]:
    """Which section the field currently sits in, per a fresh layout read."""
    layout = zoho_client.get_layout(cfg, fresh=True)
    for sec in layout.get("sections") or []:
        for field in sec.get("fields") or []:
            if str(field.get("id")) == str(field_id):
                return str(sec.get("name") or "")
    return None


def create_field(payload: Dict[str, Any], actor: Optional[str] = None) -> Dict[str, Any]:
    """Create a new custom field on the layout (needs Desk.settings.CREATE).

    ``sectionName`` is optional. Desk's ``/organizationFields`` endpoint has no
    section parameter, so placement is a second step: create the field, then move
    it with a whole-layout write. If that second step fails the field still
    exists — the result says where it actually landed rather than pretending
    nothing happened.

    Raises ValueError on bad input; ZohoError (403) if the token lacks CREATE.
    """
    row, cfg = _config_and_cfg()
    label = str(payload.get("label") or "").strip()
    field_type = str(payload.get("type") or "Text").strip()
    section_name = str(payload.get("sectionName") or "").strip()
    if not label:
        raise ValueError("A field label is required.")
    if field_type not in CREATABLE_TYPES:
        raise ValueError(f"Unsupported field type '{field_type}'.")

    body: Dict[str, Any] = {
        # Zoho Desk requires the module; the DevOps Request layout is a Tickets layout.
        "module": "tickets",
        "displayLabel": label,
        "type": field_type,
        "isMandatory": bool(payload.get("required", False)),
        # Place the new field on the DevOps Request layout only.
        "layoutId": cfg.layout_id,
    }
    if field_type == "Picklist":
        body["allowedValues"] = _normalize_option_list(payload.get("values") or [])
        body["defaultValue"] = NONE_VALUE

    created = zoho_client.create_org_field(cfg, body)
    field_id = str(created.get("id") or "")
    result = {
        "id": field_id,
        "apiName": created.get("apiName"),
        "label": created.get("displayLabel") or label,
        "type": created.get("type") or field_type,
        "sectionName": None,
        "warnings": [],
    }
    if not section_name:
        result["sectionName"] = _section_of_field(cfg, field_id) if field_id else None
        return result

    landed = _section_of_field(cfg, field_id) if field_id else None
    result["sectionName"] = landed
    if landed == section_name:
        return result
    if not field_id:
        result["warnings"].append(
            "Zoho did not return an id for the new field, so it could not be moved "
            f"into '{section_name}'. Move it in Zoho Desk, or refresh and try again."
        )
        return result
    if not layout_svc.writes_enabled():
        result["warnings"].append(
            f"The field was created in '{landed}'. Moving it into '{section_name}' "
            "needs layout writes, which are disabled — review the dry-run and set "
            "ZOHO_LAYOUT_WRITE_ENABLED=true."
        )
        return result

    try:
        layout_svc.apply_layout_write(
            [layout_svc.place_field(field_id, section_name)],
            reason="place_field",
            actor=actor,
        )
        result["sectionName"] = section_name
    except (layout_svc.LayoutWriteError, ZohoError, PermissionError) as exc:
        result["warnings"].append(
            f"The field was created but stayed in '{landed}' — moving it into "
            f"'{section_name}' failed: {exc}"
        )
    return result


# ---------------------------------------------------------------------------
# Option-source bindings — "this dropdown's options come from Kubernetes".
#
# The Application / Environment / Variable fields have always worked this way;
# a binding row opens the same mechanism to any picklist on the layout. Their
# three field ids are refused here: they are configured on the Source tab and a
# row would be a second writer on the same list.
# ---------------------------------------------------------------------------

def _picklist_on_layout(cfg, field_id: str) -> Dict[str, Any]:
    field = zoho_client.field_on_layout(cfg, str(field_id))
    if field is None:
        raise LookupError("That field is not on the DevOps Request layout.")
    if field.get("type") != "Picklist":
        raise ValueError("Only picklist (dropdown) fields can be bound to a source.")
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
        "bindings": [sources.serialize(b) for b in sources.all_bindings(row)],
    }


def get_field_binding(field_id: str) -> Optional[Dict[str, Any]]:
    """One field's binding, or None. Locked (legacy) fields report theirs too."""
    row = get_or_create_config()
    for binding in sources.all_bindings(row):
        if binding.field_id == str(field_id):
            return sources.serialize(binding)
    return None


def set_field_binding(field_id: str, payload: Dict[str, Any]) -> Dict[str, Any]:
    """Create or replace a picklist's option-source binding.

    Validated against the live layout so a binding can never point at a field
    that is not there, is not a picklist, or would form a cascade cycle.
    """
    row, cfg = _config_and_cfg()
    field_id = str(field_id)
    _reject_legacy_field(row, field_id)
    field = _picklist_on_layout(cfg, field_id)

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
        _picklist_on_layout(cfg, parent_field_id)
        parent = next(
            (b for b in sources.all_bindings(row) if b.field_id == parent_field_id), None
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
    label = (
        str(payload.get("label") or "").strip()
        or field.get("displayLabel")
        or field.get("apiName")
        or f"Field {field_id}"
    )

    # Dry-run the resulting cascade graph before writing it.
    prospective = [
        b for b in sources.all_bindings(row) if b.field_id != field_id
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
        api_name=str(field.get("apiName") or ""),
        params=params or {},
        parent_field_id=parent_field_id or None,
        enabled=enabled,
    )
    return sources.serialize(sources._from_row(stored))


def delete_field_binding(field_id: str) -> bool:
    """Drop a binding. The field keeps whatever options it currently holds."""
    return sources.delete_binding(str(field_id))


def preview_field_binding(field_id: str, payload: Dict[str, Any]) -> Dict[str, Any]:
    """Resolve a source without saving or publishing — what the next sync would send.

    POST, not GET: the request carries the unsaved binding being edited.
    """
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
        entries = svc_source_entries(row, fresh=bool(payload.get("fresh")))
    except ValueError as exc:
        # No source configured yet is a normal state, not an error page.
        return {"values": [], "count": 0, "byParent": {}, "error": str(exc)}

    ctx = sources.SourceContext(row, entries, fresh=bool(payload.get("fresh")))
    options = sources.resolve(binding, ctx)
    by_parent = options.by_parent
    if binding.parent_field_id:
        parent = next(
            (b for b in sources.all_bindings(row) if b.field_id == binding.parent_field_id), None
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


def move_field_to_section(
    field_id: str, section_name: str, actor: Optional[str] = None
) -> Dict[str, Any]:
    """Move an existing field into another section (whole-layout write)."""
    row, cfg = _config_and_cfg()
    if zoho_client.field_on_layout(cfg, str(field_id)) is None:
        raise LookupError("That field is not on the DevOps Request layout.")
    result = layout_svc.apply_layout_write(
        [layout_svc.place_field(str(field_id), section_name)],
        reason="place_field",
        actor=actor,
    )
    return {"id": str(field_id), "sectionName": section_name, "diff": result["diff"]}
