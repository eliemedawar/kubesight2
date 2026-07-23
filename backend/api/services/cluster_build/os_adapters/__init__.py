"""OS adapter registry + detection.

``detect(facts)`` returns the matching adapter or None. An unsupported distro
is a hard preflight failure naming the supported matrix — never a generic
best-effort command path.
"""

from __future__ import annotations

from typing import List, Optional

from .base import OsAdapter, OsFacts, ScriptContext  # noqa: F401
from .debian import DebianAdapter
from .rhel import RhelAdapter

ADAPTERS: List[OsAdapter] = [DebianAdapter(), RhelAdapter()]


def detect(facts: OsFacts) -> Optional[OsAdapter]:
    for adapter in ADAPTERS:
        if adapter.matches(facts):
            return adapter
    return None


def by_id(adapter_id: str) -> Optional[OsAdapter]:
    for adapter in ADAPTERS:
        if adapter.id == adapter_id:
            return adapter
    return None


def supported_matrix() -> List[dict]:
    return [
        {
            "id": adapter.id,
            "displayName": adapter.display_name,
            "validatedVersions": list(adapter.validated_versions),
        }
        for adapter in ADAPTERS
    ]
