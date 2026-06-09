"""Application settings — see notification_routing and routes/settings.py."""

from __future__ import annotations

from ..models import AppSettings
from ..notification_routing import merge_notifications, serialize_settings_row
