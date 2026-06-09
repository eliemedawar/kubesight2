"""UTC datetime helpers for API serialization."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional


def serialize_utc_datetime(value: Optional[datetime]) -> Optional[str]:
    """Serialize a datetime as UTC ISO-8601 with Z suffix for browser parsing."""
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    else:
        value = value.astimezone(timezone.utc)
    text = value.isoformat()
    if text.endswith("+00:00"):
        return text[:-6] + "Z"
    return text
