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
    describe_api_name_usage as svc_api_name_usage,
    get_or_create_config,
    repoint_api_name as svc_repoint_api_name,
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
        "removable": bool(
            field.get("isRemovable", field.get("isCustomField"))
        ),
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


def get_field(field_id: str) -> Dict[str, Any]:
    """Return one field in the same detailed shape as the layout response."""
    wanted = str(field_id)
    for section in get_layout_structure().get("sections") or []:
        for field in section.get("fields") or []:
            if str(field.get("id")) == wanted:
                return field
    raise LookupError("That field is not available in the configured Zoho Desk layout.")


def create_section(
    name: str, first_field_id: str, actor: Optional[str] = None
) -> Dict[str, Any]:
    """Reject section creation: Desk can only update sections already on a layout."""
    raise layout_svc.LayoutWriteError(
        "Zoho Desk's API cannot add a section to an existing layout. "
        "Create the section in Zoho Desk, then refresh KubeSight."
    )


def plan_section(name: str, first_field_id: str) -> Dict[str, Any]:
    """Reject the unsupported operation before presenting an invalid dry-run."""
    raise layout_svc.LayoutWriteError(
        "Zoho Desk's API cannot add a section to an existing layout. "
        "Create the section in Zoho Desk, then refresh KubeSight."
    )


def rename_section(
    section_id: str, name: str, actor: Optional[str] = None
) -> Dict[str, Any]:
    """Rename an existing section through the guarded whole-layout writer."""
    section_id = str(section_id or "").strip()
    name = str(name or "").strip()
    result = layout_svc.apply_layout_write(
        [layout_svc.rename_section(section_id, name)],
        reason="rename_section",
        actor=actor,
    )
    return {
        "id": section_id,
        "name": name,
        "diff": result["diff"],
        "layoutId": result["layoutId"],
    }


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

# Zoho Desk requires maxLength when creating a Text field and expects the
# request value to be a numeric string (its response normalizes it to a number).
_MAX_LENGTH_TYPES = {"Text"}
_DEFAULT_MAX_LENGTH = 50


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


def delete_field(field_id: str, payload: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Permanently delete an eligible custom field from the Zoho organization.

    ``payload`` is accepted and ignored so both providers' field editors call one
    signature. Jira uses it to distinguish "take off the screen" from "delete the
    field"; Desk has no such split — removing a field from a layout is a separate
    endpoint (``unassociate``, used by the conversion flow), and this route has
    always meant the destructive one.
    """
    row, cfg = _config_and_cfg()
    field_id = str(field_id)
    field = zoho_client.field_on_layout(cfg, field_id)
    if field is None:
        raise LookupError("That field is not on the DevOps Request layout.")
    if not bool(field.get("isCustomField")):
        raise ValueError("Zoho system fields cannot be deleted.")
    if field.get("isRemovable") is False:
        raise ValueError("Zoho marks this field as non-removable.")

    protected_ids = {
        str(value)
        for value in (row.app_field_id, row.environment_field_id, row.variable_field_id)
        if value
    }
    if field_id in protected_ids:
        raise ValueError(
            "This field is configured for KubeSight synchronization. "
            "Reconfigure the integration before deleting it."
        )

    bindings = sources.all_bindings(row)
    if any(
        binding.field_id == field_id or str(binding.parent_field_id or "") == field_id
        for binding in bindings
    ):
        raise ValueError(
            "This field participates in a live-source binding. Remove or reconfigure "
            "the binding before deleting it."
        )

    api_name = str(field.get("apiName") or "")
    impact = svc_api_name_usage(api_name)
    if impact.get("configKeys") or impact.get("jenkinsParams"):
        raise ValueError(
            "This field is referenced by KubeSight configuration or Jenkins parameters. "
            "Remove those references before deleting it."
        )

    zoho_client.delete_org_field(cfg, field_id)
    return {
        "id": field_id,
        "apiName": api_name,
        "label": field.get("displayLabel") or field.get("label") or api_name,
        "deleted": True,
    }


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
    nothing happened. ``afterFieldId`` puts the new field directly after a
    sibling (the conversion uses it to sit the dropdown next to the text field
    it replaces).

    Raises ValueError on bad input; ZohoError (403) if the token lacks CREATE.
    """
    row, cfg = _config_and_cfg()
    label = str(payload.get("label") or "").strip()
    field_type = str(payload.get("type") or "Text").strip()
    section_name = str(payload.get("sectionName") or "").strip()
    after_field_id = str(payload.get("afterFieldId") or "").strip()
    if not label:
        raise ValueError("A field label is required.")
    if field_type not in CREATABLE_TYPES:
        raise ValueError(f"Unsupported field type '{field_type}'.")

    required = bool(payload.get("required", False))
    is_picklist = field_type == "Picklist"
    # What Desk's create endpoint accepts is narrow, and it 422s on anything it
    # does not want ("An extra parameter 'X' is found") — `module` goes in the
    # query string, and a picklist's option list is not accepted here at all
    # (both verified live 2026-07-27). `zoho_client` drops any further property
    # Desk names. A picklist's required flag rides with its values instead, for
    # the same reason update_field has to send it that way.
    body: Dict[str, Any] = {
        "displayLabel": label,
        "type": field_type,
        # Place the new field on the DevOps Request layout only.
        "layoutId": cfg.layout_id,
    }
    if not is_picklist:
        body["isMandatory"] = required
    if field_type in _MAX_LENGTH_TYPES:
        # Desk rejects a text field with no length ("Invalid maximum length for
        # the field", HTTP 400 — verified live 2026-07-27). It is a real Desk
        # property, not a KubeSight one, so the caller may override it.
        try:
            body["maxLength"] = str(
                int(payload.get("maxLength") or _DEFAULT_MAX_LENGTH)
            )
        except (TypeError, ValueError):
            raise ValueError("maxLength must be a number.")
    values = _normalize_option_list(payload.get("values") or []) if is_picklist else []

    try:
        created = zoho_client.create_org_field(cfg, body)
    except ZohoError as exc:
        # The type enum differs between Desk versions; name the culprit rather
        # than handing back a raw 422.
        if field_type.lower() in str(exc).lower() and "type" in str(exc).lower():
            raise ValueError(
                f"Zoho rejected the field type '{field_type}': {exc} "
                "Pick another type — this org may not offer that one."
            ) from exc
        raise
    field_id = str(created.get("id") or "")
    result = {
        "id": field_id,
        "apiName": created.get("apiName"),
        "label": created.get("displayLabel") or label,
        "type": created.get("type") or field_type,
        "sectionName": None,
        "warnings": [],
    }

    # 1. Placement. Desk associates the field with the layout itself (that is
    #    what `layoutId` is for), so this only runs when the operator asked for a
    #    specific section or ordering.
    landed = _section_of_field(cfg, field_id) if field_id else None
    result["sectionName"] = landed
    needs_move = bool(section_name) and (landed != section_name or bool(after_field_id))
    if needs_move and not field_id:
        result["warnings"].append(
            "Zoho did not return an id for the new field, so it could not be moved "
            f"into '{section_name}'. Move it in Zoho Desk, or refresh and try again."
        )
    elif needs_move and not layout_svc.writes_enabled():
        result["warnings"].append(
            f"The field was created in '{landed}'. Moving it into '{section_name}' "
            "needs layout writes, which are disabled — review the dry-run and set "
            "ZOHO_LAYOUT_WRITE_ENABLED=true."
        )
    elif needs_move:
        try:
            field_data = None
            if is_picklist:
                field_data = {
                    "id": field_id,
                    "type": "Picklist",
                    "allowedValues": values,
                    "defaultValue": NONE_VALUE,
                    "sortBy": "userDefined",
                    "isNested": False,
                    "isMandatory": required,
                }
            layout_svc.apply_layout_write(
                [layout_svc.place_field(
                    field_id,
                    section_name,
                    after_field_id=after_field_id or None,
                    field_data=field_data,
                )],
                reason="place_field",
                actor=actor,
            )
            result["sectionName"] = section_name
        except (layout_svc.LayoutWriteError, ZohoError, PermissionError) as exc:
            result["warnings"].append(
                f"The field was created but stayed in '{landed}' — moving it into "
                f"'{section_name}' failed: {exc}"
            )

    # 2. Options LAST: set_allowed_values targets /layouts/{id}/fields/{id}, so
    #    the field has to be on the layout before its values can be published.
    if is_picklist and field_id:
        if not result["sectionName"]:
            result["warnings"].append(
                "The field was created but is not on the DevOps Request layout, so its "
                "options could not be published. Add it to the layout in Zoho Desk, then "
                "use “Manage options”."
            )
        else:
            try:
                zoho_client.set_allowed_values(
                    cfg,
                    values,
                    field_id=field_id,
                    default_value=NONE_VALUE,
                    is_mandatory=required,
                )
                result["allowedValues"] = values
            except ZohoError as exc:
                # The field exists either way — say so rather than implying it doesn't.
                result["warnings"].append(
                    f"The field was created, but its options could not be set: {exc} "
                    "Use “Manage options” on the field to add them."
                )
    return result


# ---------------------------------------------------------------------------
# Text -> Picklist conversion.
#
# Zoho cannot change a field's type in place, so "convert" means: create a new
# Picklist field, sit it next to the old one, optionally retire the old one —
# and tell the operator loudly that the new field has a DIFFERENT cf_ api name,
# because every KubeSight config key and Desk webhook payload keyed on the old
# name goes quietly stale otherwise.
# ---------------------------------------------------------------------------

CONVERTIBLE_TYPES = {"Text", "Textarea", "Email", "Phone", "URL", "Number"}


def _convertible(field: Dict[str, Any]) -> None:
    field_type = str(field.get("type") or "")
    if field_type == "Picklist":
        raise ValueError("That field is already a dropdown.")
    if field_type not in CONVERTIBLE_TYPES:
        raise ValueError(
            f"A {field_type or 'field'} cannot be converted to a dropdown "
            f"(convertible types: {', '.join(sorted(CONVERTIBLE_TYPES))})."
        )


def _label_taken(layout: Dict[str, Any], label: str) -> bool:
    """Whether any field on the layout already uses this display label.

    The field being converted counts: it keeps its own label until it is
    retired, and Zoho rejects a duplicate — so the replacement always needs a
    different name.
    """
    target = label.casefold()
    for section in layout.get("sections") or []:
        for field in section.get("fields") or []:
            current = field.get("displayLabel") or field.get("label") or ""
            if str(current).casefold() == target:
                return True
    return False


def _suggest_label(layout: Dict[str, Any], base: str) -> str:
    """A free label near ``base``. The old field still holds ``base`` itself."""
    if not _label_taken(layout, base):
        return base
    for candidate in (f"{base} (list)", f"{base} (dropdown)"):
        if not _label_taken(layout, candidate):
            return candidate
    for n in range(2, 20):
        candidate = f"{base} {n}"
        if not _label_taken(layout, candidate):
            return candidate
    return base


def plan_field_conversion(field_id: str) -> Dict[str, Any]:
    """Read-only: what converting this field would involve, before anything runs.

    The impact list is the point — it names every KubeSight setting still keyed
    on the old api name, so the operator sees the cost before the new field
    exists rather than after tickets start arriving without the key.
    """
    row, cfg = _config_and_cfg()
    field = zoho_client.field_on_layout(cfg, str(field_id))
    if field is None:
        raise LookupError("That field is not on the DevOps Request layout.")
    _convertible(field)

    layout = zoho_client.get_layout(cfg)
    label = field.get("displayLabel") or field.get("label") or field.get("apiName") or ""
    api_name = str(field.get("apiName") or "")
    return {
        "field": {
            "id": str(field_id),
            "apiName": api_name,
            "label": label,
            "type": field.get("type"),
            "required": bool(field.get("isMandatory")),
        },
        "sectionName": _section_of_field(cfg, str(field_id)),
        "suggestedLabel": _suggest_label(layout, str(label)),
        "impact": svc_api_name_usage(api_name),
        "layoutWritesEnabled": layout_svc.writes_enabled(),
    }


def _conversion_warnings(
    old: Dict[str, Any], new_api_name: str, impact: Dict[str, Any], repointed: List[str]
) -> List[str]:
    """The api-name change, spelled out per affected setting."""
    old_api = str(old.get("apiName") or "")
    warnings = [
        f"The new field's api name is “{new_api_name}”, not “{old_api}” — Zoho cannot "
        "change a field's type in place, so this is a different field. Update the Desk "
        "workflow/webhook that posts DevOps Request tickets to send the new key."
    ]
    remaining = [k for k in impact.get("configKeys") or [] if k["key"] not in repointed]
    if repointed:
        warnings.append(
            "KubeSight settings repointed at the new field: " + ", ".join(repointed) + "."
        )
    if remaining:
        warnings.append(
            "Still pointing at the old field: "
            + ", ".join(f"{k['label']} ({k['key']})" for k in remaining)
            + " — update them on the Settings tab."
        )
    for entry in impact.get("jenkinsParams") or []:
        warnings.append(
            f"Jenkins parameter “{entry['param']}” in {entry['where']} passes "
            f"{entry['value']} — it will resolve empty until you point it at "
            f"{{{new_api_name}}} (check the Jenkins job expects the same value)."
        )
    return warnings


def convert_field_to_picklist(
    field_id: str, payload: Dict[str, Any], actor: Optional[str] = None
) -> Dict[str, Any]:
    """Replace a free-text field with a dropdown, reporting every consequence.

    Order matters: create + place first (the reversible part), then the optional
    config repoint, then the optional retirement of the old field — so a failure
    late in the sequence leaves the new field in place rather than a layout with
    neither field usable. Every step's outcome is in the result.
    """
    row, cfg = _config_and_cfg()
    field_id = str(field_id)
    old = zoho_client.field_on_layout(cfg, field_id)
    if old is None:
        raise LookupError("That field is not on the DevOps Request layout.")
    _convertible(old)

    layout = zoho_client.get_layout(cfg)
    old_label = str(old.get("displayLabel") or old.get("label") or old.get("apiName") or "")
    old_api_name = str(old.get("apiName") or "")
    label = str(payload.get("label") or "").strip() or _suggest_label(layout, old_label)
    if _label_taken(layout, label):
        raise ValueError(
            f"A field named “{label}” already exists on this layout. Zoho requires "
            "unique field labels, so give the dropdown a different name."
        )
    section_name = str(payload.get("sectionName") or "").strip() or (
        _section_of_field(cfg, field_id) or ""
    )

    try:
        created = create_field(
            {
                "label": label,
                "type": "Picklist",
                "values": payload.get("values") or [],
                "required": bool(payload.get("required", old.get("isMandatory"))),
                "sectionName": section_name,
                # Sit the dropdown exactly where the text field is, so the ticket
                # form does not visibly reshuffle for agents mid-migration.
                "afterFieldId": field_id,
            },
            actor=actor,
        )
    except ZohoError as exc:
        # Field labels are unique across the ORG, not just this layout, so the
        # pre-check above cannot see a collision with a field on another layout.
        # Say what to do about it instead of surfacing a bare 502.
        if any(word in str(exc).lower() for word in ("duplicate", "already exists", "unique")):
            raise ValueError(
                f"Zoho already has a field named “{label}” (labels are unique across the "
                "whole Desk org, not just this layout). Pick a different name."
            ) from exc
        raise
    new_api_name = str(created.get("apiName") or "")
    result: Dict[str, Any] = {
        "newField": created,
        "oldField": {"id": field_id, "apiName": old_api_name, "label": old_label},
        "retired": False,
        "repointed": [],
        "binding": None,
        "warnings": list(created.get("warnings") or []),
    }

    # Optional: bind the new dropdown to a live source straight away.
    source_kind = str(payload.get("sourceKind") or "").strip()
    if source_kind and created.get("id"):
        try:
            result["binding"] = set_field_binding(
                str(created["id"]),
                {
                    "sourceKind": source_kind,
                    "parentFieldId": payload.get("parentFieldId"),
                    "label": label,
                },
            )
        except (ValueError, LookupError, ZohoError) as exc:
            result["warnings"].append(
                f"The dropdown was created but its option source could not be set: {exc}"
            )

    if bool(payload.get("repointConfig")) and new_api_name:
        result["repointed"] = svc_repoint_api_name(old_api_name, new_api_name)

    impact = svc_api_name_usage(old_api_name)
    result["impact"] = impact

    # Optional: take the old field off the layout. Last, and snapshot first —
    # unassociate goes through its own Desk endpoint, so it never passes the
    # whole-layout writer's guards.
    if bool(payload.get("retireOld")):
        if not layout_svc.writes_enabled():
            result["warnings"].append(
                f"“{old_label}” was left on the layout: removing it needs layout writes, "
                "which are disabled — set ZOHO_LAYOUT_WRITE_ENABLED=true."
            )
        else:
            try:
                layout_svc.snapshot_layout("field_conversion", actor)
                zoho_client.unassociate_field(cfg, field_id)
                result["retired"] = True
            except (ZohoError, layout_svc.LayoutWriteError) as exc:
                result["warnings"].append(
                    f"The dropdown was created, but “{old_label}” could not be removed "
                    f"from the layout: {exc}"
                )
    if not result["retired"]:
        result["warnings"].append(
            f"“{old_label}” is still on the form. Leave it until the Desk workflow sends "
            "the new key, then retire it — its historical ticket data is unaffected."
        )

    result["warnings"] = _conversion_warnings(
        old, new_api_name, impact, result["repointed"]
    ) + result["warnings"]
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
