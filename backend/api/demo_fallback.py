"""When the App Services / Clients pages may show their built-in demo data.

A fresh install with no live cluster shows a demo catalogue instead of an
empty page. That fallback is a presentation choice for humans, so it is gated
by the ``DEMO_DATA_FALLBACK`` app config flag (default on). The pytest config
turns it off: a test that creates, deletes or lists rows must see exactly the
database, never five demo rows that happen to share its ids. A test that wants
the demo sets ``app.config["DEMO_DATA_FALLBACK"] = True`` explicitly.
"""

from __future__ import annotations

from flask import current_app

from .k8s_provider import should_use_real_k8s


def demo_fallback_enabled() -> bool:
    if should_use_real_k8s():
        return False
    try:
        return bool(current_app.config.get("DEMO_DATA_FALLBACK", True))
    except RuntimeError:  # no app context
        return True
