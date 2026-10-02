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

The writes answer under ``ticketing:agent`` rather than ``ticketing:manage``:
the hermes-agent service account holds the former and not the latter, so a
Hermes token can act on tickets without also being able to rewrite the
integration's credentials or webhook secret. Admins hold both.
"""

from __future__ import annotations

from typing import Any, Dict

from ..protocol import ToolError
from .registry import tool as _register

_RECORD = {"type": "integer", "description": "The ticket's ticketRecordId (from the task message or kubesight_tickets_list)."}
_CHANGE_PROPS = {
    "action": {"type": "string", "enum": ["deploy_image", "set_env_var", "restart"]},
    "environment": {"type": "string", "description": "Copied exactly from a catalog entry."},
    "application": {"type": "string", "description": "Copied exactly from the same catalog entry."},
    "tag": {"type": "string", "description": "deploy_image only: the tag exactly as the ticket states it."},
    "variable": {"type": "string", "description": "set_env_var only: the variable name."},
    "value": {"type": "string", "description": "set_env_var only: the new value, verbatim."},
}
_ACTION_PROPS = {
    "ticketRecordId": _RECORD,
    **_CHANGE_PROPS,
    "changes": {
        "type": "array",
        "maxItems": 20,
        "description": (
            "When the ticket names SEVERAL applications: one entry per application, each with its "
            "own action, environment, application and tag (or variable + value), all copied from "
            "the catalog and the ticket. KubeSight starts one run per entry and sends you one "
            "follow-up when all of them have finished. Leave out for a single application and "
            "use the top-level fields instead."
        ),
        "items": {"type": "object", "properties": _CHANGE_PROPS, "required": ["environment", "application"]},
    },
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
    cluster_scoped=True,
    permission="ticketing:agent",
    description=(
        "Carry out what a ticket asks — deploy an image tag, set one environment variable, or "
        "restart — on one catalog target, or on several (`changes`, one entry per application), "
        "and post your comment for the requester. A deploy whose tag is already in one of the "
        "cluster's registries only swaps the tag; otherwise KubeSight builds it first. The ticket "
        "moves to In Progress; KubeSight sends you a follow-up task when the run(s) finish. "
        "Refused (with the reason) when a target is not in the catalog, or when the request "
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
        "required": ["ticketRecordId", "confidence", "understanding", "comment"],
    },
)
def _ticket_execute(arguments: Dict[str, Any], *, user=None) -> Dict[str, Any]:
    from ...services.ticket_agent import engine

    result = _call(engine.execute, arguments.get("ticketRecordId"), arguments, user=user)
    runs = result.get("runs") or []
    started = (
        f"started run #{result.get('runId')}" if len(runs) <= 1
        else "started runs " + ", ".join(f"#{r.get('runId')}" for r in runs)
    )
    return {"changed": f"{started} for ticket record {arguments.get('ticketRecordId')} "
                       "and moved it to In Progress", **result}


@tool(
    "kubesight_ticket_request_approval",
    permission="ticketing:agent",
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
        "required": ["ticketRecordId", "confidence", "understanding", "comment", "commentOnApprove"],
    },
)
def _ticket_request_approval(arguments: Dict[str, Any], *, user=None) -> Dict[str, Any]:
    from ...services.ticket_agent import engine

    result = _call(engine.request_approval, arguments.get("ticketRecordId"), arguments, user=user)
    return {"changed": f"asked for approval {result.get('where')} for ticket record "
                       f"{arguments.get('ticketRecordId')}", **result}


@tool(
    "kubesight_ticket_set_status",
    permission="ticketing:agent",
    description=(
        "Move a ticket and post your comment with it. impediment = not understandable, not "
        "possible, or missing information (say what, and ask). on_hold = clear, but waiting on "
        "something from the requester. A comment on an impediment / on-hold ticket hands it back "
        "to you. done = the change is live (only after KubeSight's follow-up says the run "
        "succeeded). failed = the run failed. in_progress = you are working on it. Status names "
        "map to the ticketing system's own labels."
    ),
    write=True,
    schema={
        "type": "object",
        "properties": {
            "ticketRecordId": _RECORD,
            "status": {"type": "string", "enum": ["in_progress", "done", "failed", "impediment", "on_hold"]},
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
    "kubesight_ticket_answer",
    permission="ticketing:agent",
    description=(
        "Answer a troubleshooting ticket — one that asks WHY something is wrong, failing, slow or "
        "not working, rather than for a change. Investigate first with the read-only tools "
        "(inventory, pod issues, pod logs, events, resources, alerts, rollout history, builds), "
        "then call this once with: `comment` (posted for the requester: what is wrong, why, and "
        "what to do), `diagnosis` (one or two sentences for the DevOps team), `findings` (what you "
        "saw and the evidence for each — tool, pod, log line, event), `recommendation`, "
        "`confidence`, and `status` (on_hold = the requester should confirm or act, and their "
        "reply comes back to you; done = fully answered). When the fix is something KubeSight "
        "can do (deploy a tag, set a variable, restart), put it in `proposedFix` — it is NOT run: "
        "it goes to a DevOps engineer for approval, like kubesight_ticket_request_approval."
    ),
    write=True,
    schema={
        "type": "object",
        "properties": {
            "ticketRecordId": _RECORD,
            "comment": {"type": "string", "description": "Posted on the ticket for the requester."},
            "diagnosis": {"type": "string", "description": "What is wrong and why, in one or two sentences."},
            "findings": {
                "type": "array",
                "maxItems": 8,
                "description": "The evidence: one entry per thing you found.",
                "items": {
                    "type": "object",
                    "properties": {
                        "finding": {"type": "string", "description": "What you found."},
                        "evidence": {"type": "string", "description": "Where: the tool, pod, log line or event that shows it."},
                    },
                    "required": ["finding"],
                },
            },
            "recommendation": {"type": "string", "description": "What should be done, and by whom."},
            "confidence": {
                "type": "string",
                "enum": ["High", "Medium", "Low"],
                "description": "High = the evidence shows the cause. Medium = the most likely cause. Low = a lead, not a cause.",
            },
            "status": {"type": "string", "enum": ["on_hold", "done"]},
            "checked": {
                "type": "array",
                "items": {"type": "string"},
                "description": "What you looked at and found healthy — so nobody checks it twice.",
            },
            "proposedFix": {
                "type": "object",
                "description": (
                    "Optional. A fix KubeSight can carry out, in the same shape as "
                    "kubesight_ticket_request_approval (action/environment/application/tag or "
                    "variable+value, or `changes`; plus commentOnApprove). Sent for approval, never run directly."
                ),
                "properties": {
                    **_CHANGE_PROPS,
                    "changes": _ACTION_PROPS["changes"],
                    "confidence": {"type": "string", "enum": ["High", "Medium", "Low"]},
                    "understanding": {"type": "string"},
                    "commentOnApprove": {"type": "string", "description": "Posted if the fix is approved and starts."},
                    "reasons": {"type": "array", "items": {"type": "string"}},
                },
            },
        },
        "required": ["ticketRecordId", "comment", "diagnosis", "findings", "recommendation", "confidence"],
    },
)
def _ticket_answer(arguments: Dict[str, Any], *, user=None) -> Dict[str, Any]:
    from ...services.ticket_agent import engine

    result = _call(engine.answer, arguments.get("ticketRecordId"), arguments, user=user)
    then = (
        "sent the proposed fix for approval"
        if result.get("fixProposed")
        else f"moved it to {result.get('ticketStatus')}"
    )
    return {"changed": f"answered ticket record {arguments.get('ticketRecordId')} and {then}", **result}


@tool(
    "kubesight_ticket_comment",
    permission="ticketing:agent",
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
