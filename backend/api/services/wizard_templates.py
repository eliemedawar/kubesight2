"""Built-in application templates for the Application Builder wizard.

The built-in catalog was retired — all templates are now author-created and stored
in the database (see ``user_template_service``). Keeping this list empty preserves
the ``list_templates``/``get_template`` contract without shipping any seed entries.
"""

from __future__ import annotations

from typing import Any, Dict, List

TEMPLATES: List[Dict[str, Any]] = []


def list_templates() -> List[Dict[str, Any]]:
    return [
        {
            "id": t["id"],
            "name": t["name"],
            "description": t["description"],
            "category": t.get("category", "General"),
            "workloadType": t.get("workloadType", "Deployment"),
        }
        for t in TEMPLATES
    ]


def get_template(template_id: str) -> Dict[str, Any] | None:
    for t in TEMPLATES:
        if t["id"] == template_id:
            return dict(t)
    return None
