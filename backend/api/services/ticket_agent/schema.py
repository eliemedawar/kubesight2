"""The vocabulary the ticket tools speak: actions, confidences, statuses.

Hermes acts through the ``kubesight_ticket_*`` MCP tools rather than returning a
decision, so this is the shape of those tools' arguments — closed enums and
bounded strings — and the normaliser they share. A malformed argument is a
:class:`ContractError`, which the tool returns to Hermes as an error it can fix.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

ACTIONS = ("deploy_image", "set_env_var", "restart")
CONFIDENCES = ("High", "Medium", "Low")
# Action → the run change_type deploy automation understands.
ACTION_CHANGE_TYPE = {"deploy_image": "image", "set_env_var": "env_var", "restart": "restart"}
# What Hermes may set a ticket to, and the write-back outcome each maps to
# (the provider's configured status label / transition for that outcome).
TICKET_STATUSES = {
    "in_progress": "started",
    "done": "deployed",
    "failed": "failed",
    "impediment": "impediment",
}

MAX_UNDERSTANDING = 600
MAX_COMMENT = 3000
MAX_LIST_ITEMS = 5
MAX_LIST_ITEM = 300
MAX_FIELD = 253


class ContractError(ValueError):
    """A tool argument is malformed. The message is written for Hermes."""


def opt_str(value: Any, path: str, limit: int = MAX_FIELD) -> Optional[str]:
    if value is None:
        return None
    if not isinstance(value, (str, int, float)) or isinstance(value, bool):
        raise ContractError(f"{path} must be a string.")
    text = str(value).strip()
    if len(text) > limit:
        raise ContractError(f"{path} is longer than {limit} characters.")
    return text or None


def str_list(value: Any, path: str) -> List[str]:
    if value is None:
        return []
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, list):
        raise ContractError(f"{path} must be a list of strings.")
    out: List[str] = []
    for index, item in enumerate(value[:MAX_LIST_ITEMS]):
        text = opt_str(item, f"{path}[{index}]", MAX_LIST_ITEM)
        if text:
            out.append(text)
    return out


def comment(value: Any, path: str = "comment", required: bool = True) -> Optional[str]:
    text = opt_str(value, path, MAX_COMMENT)
    if required and not text:
        raise ContractError(f"{path} is required: the message to post on the ticket for the requester.")
    return text


def action_request(arguments: Dict[str, Any]) -> Dict[str, Any]:
    """Normalise an execute / request-approval call into one decision dict."""
    action = arguments.get("action")
    if action not in ACTIONS:
        raise ContractError(f"action must be one of {', '.join(ACTIONS)}.")
    confidence = arguments.get("confidence")
    if isinstance(confidence, str):
        confidence = confidence.strip().capitalize()
    if confidence not in CONFIDENCES:
        raise ContractError(f"confidence must be one of {', '.join(CONFIDENCES)}.")
    understanding = opt_str(arguments.get("understanding"), "understanding", MAX_UNDERSTANDING)
    if not understanding:
        raise ContractError("understanding is required: one or two sentences on what the ticket asks for.")
    return {
        "decision": "execute",
        "action": action,
        "target": {
            "environment": opt_str(arguments.get("environment"), "environment"),
            "application": opt_str(arguments.get("application"), "application"),
        },
        "change": {
            "tag": opt_str(arguments.get("tag"), "tag", 200),
            "variable": opt_str(arguments.get("variable"), "variable"),
            # A value may legitimately be long (a URL, a JSON blob).
            "value": opt_str(arguments.get("value"), "value", 2000),
        },
        "confidence": confidence,
        "understanding": understanding,
        "concerns": str_list(arguments.get("concerns"), "concerns"),
    }
