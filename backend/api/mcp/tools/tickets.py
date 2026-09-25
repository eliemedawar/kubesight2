"""Ticket agent tools — how Hermes handles a DevOps ticket end to end.

KubeSight hands each new ticket to Hermes; Hermes reads it and acts through
these tools, and writes every comment the requester sees. They sit in the
``platform`` domain beside the other ticketing tools.

These are not thin wrappers. Each write re-checks what Hermes asked for:

* ``kubesight_ticket_execute`` only runs an action whose application and
  environment match a published deploy target exactly, and refuses — with
  "request approval instead" — when Hermes' confidence is under the
  installation's bar, when it contradicts the ticket's own dropdowns, or when
  Hermes itself raised a concern. The deploy is an ordinary deploy-automation
  run, so the cluster's approval rules, rollback and the pod-health watch
  apply unchanged.
* ``kubesight_ticket_request_approval`` parks the action behind a human
  (Telegram buttons + the KubeSight UI). Approving runs exactly what was
  proposed; nothing else can be approved through it.
* ``kubesight_ticket_set_status`` refuses to close a ticket whose run is still
  going — Hermes is woken with a follow-up when it finishes.

Approving is deliberately not a tool: the agent that proposed an action must
not be able to approve it.
"""

from __future__ import annotations

from typing import Any, Dict

from ..protocol import ToolError
from .registry import tool as _register

_RECORD = {"type": "integer", "description": "The ticket's ticketRecordId (from the task message or kubesight_tickets_list)."}
_ACTION_PROPS = {
    "ticketRecordId": _RECORD,
    "action": {"type": "string", "enum": ["deploy_image", "set_env_var", "restart"]},
    "environment": {"type": "string", "description": "Copied exactly from a catalog entry."},
    "application": {"type": "string", "description": "Copied exactly from the same catalog entry."},
    "tag": {"type": "string", "description": "deploy_image only: the tag exactly as the ticket states it."},
    "variable": {"type": "string", "description": "set_env_var only: the variable name."},
    "value": {"type": "string", "description": "set_env_var only: the new value, verbatim."},
    "confidence": {"type": "string", "enum": ["High", "Medium", "Low"]},
    "understanding": {"type": "string", "description": "One or two sentences, for the DevOps team, on what the ticket asks."},
    "concerns": {"type": "array", "items": {"type": "string"}, "description": "Anything a human should know first. Non-empty means it needs approval."},
}


def tool(name, **kwargs):
    kwargs.setdefault("domain", "platform")
    return _register(name, **kwargs)


def _call(fn, *args, **kwargs) -> Dict[str, Any]:
    from ...services.ticket_agent.engine import AgentError

    try:
        return fn(*args, **kwargs)
    except AgentError as exc:
        raise ToolError(str(exc))


@tool(
    "kubesight_ticket_get",
    permission="ticketing:view",
    description=(
        "One DevOps ticket as the ticket agent sees it: subject, description, the dropdown "
        "fields the requester picked, recent comments, the catalog of deploy targets "
        "(environment + application pairs) an action must be copied from, the confidence "
        "bar, and the ticket's runs and earlier agent tasks."
    ),
    schema={"type": "object", "properties": {"ticketRecordId": _RECORD}, "required": ["ticketRecordId"]},
)
def _ticket_get(arguments: Dict[str, Any]) -> Dict[str, Any]:
    from ...services.ticket_agent import engine

    return _call(engine.brief, arguments.get("ticketRecordId"))


@tool(
    "kubesight_ticket_execute",
    permission="ticketing:manage",
    description=(
        "Carry out what a ticket asks — deploy an image tag, set one environment variable, or "
        "restart — on one catalog target, and post your comment for the requester. The ticket "
        "moves to In Progress; KubeSight sends you a follow-up task when the run finishes. "
        "Refused (with the reason) when the target is not in the catalog, or when the request "
        "needs a human first — then call kubesight_ticket_request_approval instead."
    ),
    approval="Runs through the ordinary deploy path, so an approval-gated cluster still gates it.",
    write=True,
    schema={
        "type": "object",
        "properties": {
            **_ACTION_PROPS,
            "comment": {"type": "string", "description": "Posted on the ticket: what you understood and that it is starting."},
        },
        "required": ["ticketRecordId", "action", "environment", "application", "confidence", "understanding", "comment"],
    },
)
def _ticket_execute(arguments: Dict[str, Any], *, user=None) -> Dict[str, Any]:
    from ...services.ticket_agent import engine

    result = _call(engine.execute, arguments.get("ticketRecordId"), arguments, user=user)
    return {"changed": f"started run #{result.get('runId')} for ticket record "
                       f"{arguments.get('ticketRecordId')} and moved it to In Progress", **result}


@tool(
    "kubesight_ticket_request_approval",
    permission="ticketing:manage",
    description=(
        "Ask a DevOps engineer to approve an action before it runs — for a ticket you understand "
        "but are not fully confident about. Posts `comment` on the ticket now and an "
        "Approve/Reject request on Telegram. If approved, KubeSight runs exactly this action and "
        "posts `commentOnApprove`; if rejected or expired, you get a follow-up task."
    ),
    write=True,
    schema={
        "type": "object",
        "properties": {
            **_ACTION_PROPS,
            "reasons": {"type": "array", "items": {"type": "string"}, "description": "Why a human should look first."},
            "comment": {"type": "string", "description": "Posted now: it is waiting for DevOps review."},
            "commentOnApprove": {"type": "string", "description": "Posted if it is approved and starts."},
        },
        "required": ["ticketRecordId", "action", "environment", "application", "confidence",
                     "understanding", "comment", "commentOnApprove"],
    },
)
def _ticket_request_approval(arguments: Dict[str, Any], *, user=None) -> Dict[str, Any]:
    from ...services.ticket_agent import engine

    result = _call(engine.request_approval, arguments.get("ticketRecordId"), arguments, user=user)
    return {"changed": f"asked for approval {result.get('where')} for ticket record "
                       f"{arguments.get('ticketRecordId')}", **result}


@tool(
    "kubesight_ticket_set_status",
    permission="ticketing:manage",
    description=(
        "Move a ticket and post your comment with it. impediment = not understandable, not "
        "possible, or missing information (say what, and ask). done = the change is live (only "
        "after KubeSight's follow-up says the run succeeded). failed = the run failed. "
        "in_progress = you are working on it. Status names map to the ticketing system's own labels."
    ),
    write=True,
    schema={
        "type": "object",
        "properties": {
            "ticketRecordId": _RECORD,
            "status": {"type": "string", "enum": ["in_progress", "done", "failed", "impediment"]},
            "comment": {"type": "string", "description": "Posted on the ticket for the requester."},
        },
        "required": ["ticketRecordId", "status", "comment"],
    },
)
def _ticket_set_status(arguments: Dict[str, Any], *, user=None) -> Dict[str, Any]:
    from ...services.ticket_agent import engine

    result = _call(
        engine.set_status, arguments.get("ticketRecordId"), arguments.get("status"),
        arguments.get("comment"), user=user,
    )
    return {"changed": f"moved ticket record {arguments.get('ticketRecordId')} to "
                       f"{result.get('ticketStatus')} with your comment", **result}


@tool(
    "kubesight_ticket_comment",
    permission="ticketing:manage",
    description="Post a comment on a ticket without changing its status.",
    write=True,
    schema={
        "type": "object",
        "properties": {"ticketRecordId": _RECORD, "comment": {"type": "string"}},
        "required": ["ticketRecordId", "comment"],
    },
)
def _ticket_comment(arguments: Dict[str, Any], *, user=None) -> Dict[str, Any]:
    from ...services.ticket_agent import engine

    result = _call(engine.add_comment, arguments.get("ticketRecordId"), arguments.get("comment"), user=user)
    return {"changed": f"commented on ticket record {arguments.get('ticketRecordId')}", **result}
