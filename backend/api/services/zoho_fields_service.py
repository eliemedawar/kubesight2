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
from .zoho_client import ZohoError
from .zoho_sync_service import (
    NONE_VALUE,
    _sanitize_value,
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


def _field_dict(field: Dict[str, Any], auto_managed_ids: set) -> Dict[str, Any]:
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
    }


def _auto_managed_ids(row) -> set:
    """Field ids the sync currently owns (only when their per-field toggle is on)."""
    ids = set()
    if row.sync_application and row.app_field_id:
        ids.add(str(row.app_field_id))
    if row.sync_environment and row.environment_field_id:
        ids.add(str(row.environment_field_id))
    return ids


def get_layout_structure() -> Dict[str, Any]:
    """Read the pinned layout and return its sections + fields for the editor UI."""
    row, cfg = _config_and_cfg()
    auto = _auto_managed_ids(row)
    layout = zoho_client.get_layout(cfg)
    sections = []
    for sec in layout.get("sections") or []:
        sections.append(
            {
                "name": sec.get("name"),
                "fields": [_field_dict(f, auto) for f in (sec.get("fields") or [])],
            }
        )
    return {
        "layoutId": str(layout.get("id") or cfg.layout_id),
        "layoutName": layout.get("name") or "DevOps Request",
        "sections": sections,
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


def update_field(field_id: str, payload: Dict[str, Any]) -> Dict[str, Any]:
    """Edit an existing field's label / required (needs Desk.settings.UPDATE)."""
    row, cfg = _config_and_cfg()
    field = zoho_client.field_on_layout(cfg, str(field_id))
    if field is None:
        raise LookupError("That field is not on the DevOps Request layout.")

    body: Dict[str, Any] = {}
    if payload.get("label") is not None and str(payload.get("label")).strip():
        body["displayLabel"] = str(payload.get("label")).strip()
    if "required" in payload:
        body["isMandatory"] = bool(payload.get("required"))
    if not body:
        raise ValueError("Nothing to update (send a label and/or required flag).")

    zoho_client.update_org_field(cfg, str(field_id), body)
    auto = _auto_managed_ids(row)
    updated = zoho_client.field_on_layout(cfg, str(field_id)) or field
    return _field_dict(updated, auto)


def create_field(payload: Dict[str, Any]) -> Dict[str, Any]:
    """Create a new custom field on the layout (needs Desk.settings.CREATE).

    Raises ValueError on bad input; ZohoError (403) if the token lacks CREATE.
    """
    row, cfg = _config_and_cfg()
    label = str(payload.get("label") or "").strip()
    field_type = str(payload.get("type") or "Text").strip()
    if not label:
        raise ValueError("A field label is required.")
    if field_type not in CREATABLE_TYPES:
        raise ValueError(f"Unsupported field type '{field_type}'.")

    body: Dict[str, Any] = {
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
    return {
        "id": str(created.get("id") or ""),
        "apiName": created.get("apiName"),
        "label": created.get("displayLabel") or label,
        "type": created.get("type") or field_type,
    }
