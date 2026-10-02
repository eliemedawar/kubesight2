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
    "on_hold": "on_hold",
}
# Statuses that park a ticket on the requester: a comment on it wakes Hermes.
PARKED_STATUSES = ("impediment", "on_hold")

MAX_UNDERSTANDING = 600
MAX_COMMENT = 3000
MAX_LIST_ITEMS = 5
MAX_LIST_ITEM = 300
MAX_FIELD = 253
# A troubleshooting answer: how much evidence it may carry, and where the ticket
# may go once it is answered (waiting on the requester, or closed).
MAX_FINDINGS = 8
MAX_DIAGNOSIS = 600
MAX_RECOMMENDATION = 1500
ANSWER_STATUSES = ("on_hold", "done")

# How many applications one ticket may change at once. Each becomes its own
# deploy-automation run, so this bounds the runs a single ticket can start.
MAX_CHANGES = 20


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


def _change(item: Dict[str, Any], path: str, default_action: Any = None) -> Dict[str, Any]:
    """One target + change: what a single run will do."""
    action = item.get("action") or default_action
    if action not in ACTIONS:
        raise ContractError(f"{path}action must be one of {', '.join(ACTIONS)}.")
    return {
        "action": action,
        "target": {
            "environment": opt_str(item.get("environment"), f"{path}environment"),
            "application": opt_str(item.get("application"), f"{path}application"),
        },
        "change": {
            "tag": opt_str(item.get("tag"), f"{path}tag", 200),
            "variable": opt_str(item.get("variable"), f"{path}variable"),
            # A value may legitimately be long (a URL, a JSON blob).
            "value": opt_str(item.get("value"), f"{path}value", 2000),
        },
    }


def action_request(arguments: Dict[str, Any]) -> Dict[str, Any]:
    """Normalise an execute / request-approval call into one decision dict.

    A ticket may change several applications: ``changes`` is then a list of
    ``{action, environment, application, tag | variable + value}``, one per
    application (an item without ``action`` takes the top-level one). Without
    ``changes`` the top-level fields are the one change, as before. Either way
    the result carries ``changes`` — the list every caller iterates — and the
    first change is mirrored at the top level for the single-change readers.
    """
    raw = arguments.get("changes")
    if raw not in (None, [], ""):
        if not isinstance(raw, list):
            raise ContractError("changes must be a list of {action, environment, application, tag | variable + value}.")
        if len(raw) > MAX_CHANGES:
            raise ContractError(
                f"changes lists {len(raw)} applications; one ticket can change at most {MAX_CHANGES}. "
                "Ask the requester to split the ticket."
            )
        changes = []
        for index, item in enumerate(raw):
            if not isinstance(item, dict):
                raise ContractError(f"changes[{index}] must be an object.")
            changes.append(_change(item, f"changes[{index}].", arguments.get("action")))
    else:
        changes = [_change(arguments, "")]
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
        **changes[0],
        "changes": changes,
        "confidence": confidence,
        "understanding": understanding,
        "concerns": str_list(arguments.get("concerns"), "concerns"),
    }


def changes_of(request: Dict[str, Any]) -> List[Dict[str, Any]]:
    """The request's changes — also for one stored before ``changes`` existed."""
    changes = request.get("changes")
    if isinstance(changes, list) and changes:
        return changes
    return [{"action": request.get("action"), "target": request.get("target") or {},
             "change": request.get("change") or {}}]


def single(request: Dict[str, Any], change: Dict[str, Any]) -> Dict[str, Any]:
    """The decision for ONE change of a request — what one run is checked against.

    Carries the request's confidence and concerns, so a run checked on its own
    (deploy automation re-validates every agent run) meets the same bar.
    """
    out = {k: v for k, v in request.items() if k not in ("changes", "commentOnApprove")}
    out.update({"action": change["action"], "target": change["target"], "change": change["change"]})
    return out


def answer(arguments: Dict[str, Any]) -> Dict[str, Any]:
    """Normalise a kubesight_ticket_answer call: diagnosis, evidence, recommendation.

    Evidence is required, not optional: a diagnosis with nothing behind it is a
    guess, and the requester cannot tell the two apart from the comment alone.
    The DevOps team reads the findings in KubeSight next to the comment.
    """
    diagnosis = opt_str(arguments.get("diagnosis"), "diagnosis", MAX_DIAGNOSIS)
    if not diagnosis:
        raise ContractError("diagnosis is required: one or two sentences on what is wrong and why.")
    recommendation = opt_str(arguments.get("recommendation"), "recommendation", MAX_RECOMMENDATION)
    if not recommendation:
        raise ContractError("recommendation is required: what should be done about it, and by whom.")

    raw = arguments.get("findings")
    if not isinstance(raw, list) or not raw:
        raise ContractError(
            "findings is required: at least one {finding, evidence} — what you saw and where "
            "(the tool, the pod, the log line, the event)."
        )
    findings: List[Dict[str, str]] = []
    for index, item in enumerate(raw[:MAX_FINDINGS]):
        if isinstance(item, str):
            item = {"finding": item}
        if not isinstance(item, dict):
            raise ContractError(f"findings[{index}] must be an object {{finding, evidence}}.")
        finding = opt_str(item.get("finding"), f"findings[{index}].finding", MAX_LIST_ITEM)
        if not finding:
            raise ContractError(f"findings[{index}].finding is required.")
        evidence = opt_str(item.get("evidence"), f"findings[{index}].evidence", 1000)
        findings.append({"finding": finding, "evidence": evidence or ""})

    confidence = arguments.get("confidence")
    if isinstance(confidence, str):
        confidence = confidence.strip().capitalize()
    if confidence not in CONFIDENCES:
        raise ContractError(f"confidence must be one of {', '.join(CONFIDENCES)}.")

    status = str(arguments.get("status") or "on_hold").strip().lower().replace(" ", "_")
    if status not in ANSWER_STATUSES:
        raise ContractError(
            "status must be on_hold (the requester should confirm or act — their reply comes back "
            "to you) or done (fully answered, nothing left to do)."
        )
    return {
        "diagnosis": diagnosis,
        "findings": findings,
        "recommendation": recommendation,
        "confidence": confidence,
        "status": status,
        "checked": str_list(arguments.get("checked"), "checked"),
    }
