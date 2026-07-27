"""Curated Cluster Builder add-on catalog."""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from .base import (  # noqa: F401
    AddonConfigError,
    AddonDescriptor,
    AddonRenderError,
)
from .metallb import METALLB
from .metrics_server import METRICS_SERVER
from .nginx_ingress import NGINX_INGRESS

_AVAILABLE = (METRICS_SERVER, NGINX_INGRESS, METALLB)
_BY_ID: Dict[str, AddonDescriptor] = {addon.id: addon for addon in _AVAILABLE}


def available() -> List[AddonDescriptor]:
    return list(_AVAILABLE)


def get(addon_id: str) -> Optional[AddonDescriptor]:
    return _BY_ID.get(addon_id)


def catalog() -> List[Dict[str, Any]]:
    return [
        {
            "id": addon.id,
            "displayName": addon.display_name,
            "description": addon.description,
            "supportTier": addon.support_tier,
            "versions": list(addon.versions),
            "defaultVersion": addon.versions[0],
            "default": False,
            "configFields": [dict(field) for field in addon.config_fields],
        }
        for addon in available()
    ]


def normalize_selection(value: Any) -> List[Dict[str, Any]]:
    """Validate and canonicalize a build payload's ``addons`` list."""
    if value is None:
        return []
    if not isinstance(value, list):
        raise ValueError("addons must be a list.")

    normalized: List[Dict[str, Any]] = []
    seen = set()
    for item in value:
        config: Any = {}
        if isinstance(item, str):
            addon_id = item.strip()
            requested_version = ""
        elif isinstance(item, dict):
            addon_id = str(item.get("id") or "").strip()
            requested_version = str(item.get("version") or "").strip().lstrip("v")
            config = item.get("config") or {}
            if not isinstance(config, dict):
                raise ValueError(
                    f"Add-on '{addon_id}' config must be an object."
                )
        else:
            raise ValueError("Each add-on must be an ID or an {id, version} object.")

        descriptor = get(addon_id)
        if descriptor is None:
            choices = ", ".join(addon.id for addon in available())
            raise ValueError(f"Unknown add-on '{addon_id}'. Choose from: {choices}.")
        if addon_id in seen:
            raise ValueError(f"Add-on '{addon_id}' was selected more than once.")
        version = requested_version or descriptor.versions[0]
        if version not in descriptor.versions:
            raise ValueError(
                f"Version {version} is not supported for {descriptor.display_name}; "
                f"choose from: {', '.join(descriptor.versions)}."
            )
        seen.add(addon_id)
        entry: Dict[str, Any] = {"id": addon_id, "version": version}
        normalized_config = descriptor.normalize_config(config)
        if normalized_config:
            entry["config"] = normalized_config
        normalized.append(entry)
    return normalized
