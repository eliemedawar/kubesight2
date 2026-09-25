"""The ticket agent's engine: hand tickets to Hermes, and carry out what it does.

Two halves, meeting on the task row (:class:`TicketInterpretation`):

**KubeSight → Hermes.** A new ticket becomes a ``handle`` task; later events
(the deploy finished, an approval was rejected or expired) become ``followup``
tasks. The scheduler tick hands pending tasks to a small worker pool, and each
worker makes one call to the operators' Hermes and waits while it works.

**Hermes → KubeSight.** While that call is open, Hermes acts through the
``kubesight_ticket_*`` MCP tools, which land in the functions below
(:func:`execute`, :func:`request_approval`, :func:`set_status`,
:func:`add_comment`). They are the guard rail: a deploy must match a published
target exactly, and a request under the confidence bar — or one that
contradicts the ticket's own dropdowns — is refused with "ask for approval
instead". The deploy itself is an ordinary deploy-automation run, so cluster
approval gates, rollback and the pod-health watch all still apply.

When Hermes returns, the worker only checks whether a tool settled the task. If
Hermes returned without acting, the task is an error an operator can retry;
for a follow-up, KubeSight posts its own factual comment instead so a ticket is
never left without its outcome.
"""

from __future__ import annotations

import hashlib
import logging
import os
import re
import secrets
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from ...audit import log_audit
from ...db import db
from ...models import (
    DeployAutomationRun,
    TicketAgentPostedComment,
    TicketInterpretation,
    ZohoInboundTicket,
)
from . import catalog, hermes, schema, settings as agent_settings, telegram, validator

logger = logging.getLogger(__name__)

PENDING, RUNNING = "pending", "running"
OPEN_STATUSES = (PENDING, RUNNING, "awaiting_approval")
# A run started by Hermes carries this in ``triggered_by`` (possibly with the
# approver appended) — how deploy automation knows to wake Hermes on the outcome.
TRIGGER = "hermes"
SESSION_KEY = "kubesight-ticket-agent"

_executor: Optional[ThreadPoolExecutor] = None
_executor_lock = threading.Lock()


class AgentError(Exception):
    """A refusal worth showing the caller (Hermes via MCP, or an operator)."""

    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _aware(dt: Optional[datetime]) -> Optional[datetime]:
    if dt is None:
        return None
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt


def _int_env(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, "").strip() or default)
    except ValueError:
        return default


def _max_attempts() -> int:
    return max(1, _int_env("TICKET_AGENT_MAX_ATTEMPTS", 3))


def _stale_seconds() -> int:
    # Longer than the Hermes timeout: a worker blocks on one call the whole time.
    return max(120, _int_env("TICKET_AGENT_STALE_SECONDS", 1200))


def _testing() -> bool:
    try:
        from flask import current_app

        return bool(current_app.config.get("TESTING"))
    except Exception:  # noqa: BLE001
        return False


# ---------------------------------------------------------------------------
# Queueing
# ---------------------------------------------------------------------------

def _new_task(ticket: ZohoInboundTicket, kind: str, *, event=None, requested_by=None,
              status: str = PENDING) -> TicketInterpretation:
    task = TicketInterpretation(
        ticket_record_id=ticket.id,
        provider=ticket.provider or "zoho",
        ticket_number=ticket.ticket_number or ticket.ticket_id,
        kind=kind,
        event=event,
        status=status,
        requested_by=requested_by,
        started_at=_now() if status == RUNNING else None,
        heartbeat_at=_now() if status == RUNNING else None,
    )
    db.session.add(task)
    db.session.commit()
    return task


def queue_ticket(record_id: int) -> Optional[TicketInterpretation]:
    """Intake hook: hand a freshly-received ticket to Hermes, once.

    Idempotent per ticket — Desk re-delivers webhooks on edits and retries, and
    a re-delivery must not make Hermes handle the same ticket twice. Asking
    again is an explicit operator action (:func:`reinterpret`).
    """
    ticket = db.session.get(ZohoInboundTicket, int(record_id))
    if ticket is None:
        return None
    if TicketInterpretation.query.filter_by(ticket_record_id=ticket.id, kind="handle").first():
        return None
    task = _new_task(ticket, "handle")
    log_audit(
        "ticket_agent_queued",
        actor=None,
        target_type="ticket_interpretation",
        target_id=str(task.id),
        details={"ticket": task.ticket_number, "provider": task.provider},
    )
    kick()
    return task


def on_ticket_received(record_id: int) -> bool:
    """Webhook hook: True when the agent took the ticket (so auto-run must not).

    Never raises — the webhook's answer to the ticketing system must not depend
    on the agent's health. Queueing is a DB write plus a hand-off to the worker
    pool; Hermes is never called on the webhook's thread (except under TESTING).
    """
    try:
        if not agent_settings.is_active():
            return False
        queue_ticket(record_id)
        return True
    except Exception:  # noqa: BLE001
        db.session.rollback()
        logger.exception("Ticket agent intake failed for record %s", record_id)
        return False


def reinterpret(record_id: int, user=None) -> TicketInterpretation:
    """Operator: hand the ticket to Hermes again (after the requester fixed it)."""
    ticket = db.session.get(ZohoInboundTicket, int(record_id))
    if ticket is None:
        raise AgentError("Inbound ticket not found.", 404)
    busy = TicketInterpretation.query.filter(
        TicketInterpretation.ticket_record_id == ticket.id,
        TicketInterpretation.status.in_((PENDING, RUNNING)),
    ).first()
    if busy:
        raise AgentError(f"Hermes is already working on this ticket (task #{busy.id}).", 409)
    from ..deploy_automation_service import OPEN_STATUSES as RUN_OPEN

    run = DeployAutomationRun.query.filter(
        DeployAutomationRun.ticket_record_id == ticket.id,
        DeployAutomationRun.status.in_(RUN_OPEN),
    ).first()
    if run:
        raise AgentError(f"Run #{run.id} is still going for this ticket — cancel it first.", 409)
    for waiting in TicketInterpretation.query.filter_by(
        ticket_record_id=ticket.id, status="awaiting_approval"
    ).all():
        _close_approval(waiting, "superseded", "Superseded — Hermes was asked to read the ticket again.")
    who = getattr(user, "username", None)
    task = _new_task(ticket, "handle", requested_by=who)
    log_audit(
        "ticket_agent_requeued",
        actor=user,
        target_type="ticket_interpretation",
        target_id=str(task.id),
        details={"ticket": task.ticket_number},
    )
    kick()
    return task


def queue_followup(ticket_record_id: Optional[int], event: Dict[str, Any]) -> bool:
    """Wake Hermes about something that happened after it acted.

    ``event["fallback"]`` is what KubeSight writes itself when Hermes can't
    (agent off, Hermes down, Hermes returned without moving the ticket).
    Returns True when the event was taken care of (queued or fallen back).
    """
    ticket = db.session.get(ZohoInboundTicket, int(ticket_record_id)) if ticket_record_id else None
    if ticket is None:
        return False
    if not agent_settings.is_active():
        _apply_fallback(ticket, event)
        return True
    _new_task(ticket, "followup", event=event)
    kick()
    return True


def _apply_fallback(ticket: ZohoInboundTicket, event: Optional[Dict[str, Any]]) -> None:
    fallback = (event or {}).get("fallback") or {}
    outcome = fallback.get("outcome")
    if not outcome:
        return
    from .. import ticketing

    if fallback.get("comment"):
        _remember(ticket, fallback["comment"])
        db.session.commit()
    ticketing.report_outcome(
        ticket.provider or "zoho",
        ticket.ticket_id,
        outcome,
        comment=fallback.get("comment"),
        resolution=fallback.get("resolution"),
        public=agent_settings.comments_public(),
    )


# ---------------------------------------------------------------------------
# Workers
# ---------------------------------------------------------------------------

def _pool() -> ThreadPoolExecutor:
    global _executor
    with _executor_lock:
        if _executor is None:
            _executor = ThreadPoolExecutor(
                max_workers=max(1, _int_env("TICKET_AGENT_WORKERS", 2)),
                thread_name_prefix="ticket-agent",
            )
        return _executor


def _run_in_app(app, task_id: int) -> None:
    with app.app_context():
        try:
            process(task_id)
        except Exception:  # noqa: BLE001 — a worker must never die silently
            logger.exception("Ticket agent task %s crashed", task_id)
            db.session.rollback()
            task = db.session.get(TicketInterpretation, task_id)
            if task is not None and task.status == RUNNING:
                _fail(task, "The ticket agent crashed while handling this task.")


def kick() -> int:
    """Claim due pending tasks and hand them to workers. Returns how many."""
    now = _now()
    due = [
        t for t in TicketInterpretation.query.filter_by(status=PENDING)
        .order_by(TicketInterpretation.id.asc()).limit(10).all()
        if not t.retry_at or _aware(t.retry_at) <= now
    ]
    claimed: List[int] = []
    for task in due:
        # Conditional claim: two ticks (or a tick and a webhook) never both win.
        won = TicketInterpretation.query.filter_by(id=task.id, status=PENDING).update(
            {"status": RUNNING, "heartbeat_at": now, "started_at": now,
             "attempts": (task.attempts or 0) + 1},
            synchronize_session=False,
        )
        if won:
            claimed.append(task.id)
    db.session.commit()
    if not claimed:
        return 0
    if _testing():
        for task_id in claimed:
            process(task_id)
        return len(claimed)
    from flask import current_app

    app = current_app._get_current_object()
    for task_id in claimed:
        _pool().submit(_run_in_app, app, task_id)
    return len(claimed)


def _task_message(task: TicketInterpretation, ticket: ZohoInboundTicket) -> Dict[str, Any]:
    row = agent_settings.get_or_create()
    base = {
        "ticketRecordId": ticket.id,
        "provider": ticket.provider or "zoho",
        "confidenceBar": row.min_confidence or "High",
    }
    if task.kind == "followup":
        event = dict(task.event or {})
        event.pop("fallback", None)
        previous = (
            TicketInterpretation.query.filter_by(ticket_record_id=ticket.id, kind="handle")
            .order_by(TicketInterpretation.id.desc()).first()
        )
        return {
            **base,
            "task": "followup",
            "instruction": (
                "Something happened on a ticket you handled. Write the requester a comment about it "
                "and move the ticket with kubesight_ticket_set_status."
            ),
            "event": event,
            "ticket": {"number": ticket.ticket_number or ticket.ticket_id, "subject": ticket.subject},
            "yourEarlierUnderstanding": previous.understanding if previous else None,
        }
    targets = catalog.targets(ticket.provider or "zoho")
    event = task.event or {}
    if event.get("type") == "requester_replied":
        return {
            **base,
            "task": "continue_ticket",
            "instruction": (
                "You parked this ticket earlier and the requester has replied. Read the "
                "conversation and the new comments, then handle it again with the "
                "kubesight_ticket_* tools: execute it, request approval, or set it to "
                "impediment / on_hold with a comment asking what is still missing."
            ),
            "ticket": catalog.ticket_context(ticket),
            "conversation": _conversation(ticket, before=task.id),
            "newComments": event.get("comments") or [],
            "catalog": catalog.catalog_entries(targets),
        }
    return {
        **base,
        "task": "handle_new_ticket",
        "instruction": (
            "A new DevOps ticket arrived. Handle it with the kubesight_ticket_* tools: execute it, "
            "request approval, or set it to impediment with a comment."
        ),
        "ticket": catalog.ticket_context(ticket),
        "catalog": catalog.catalog_entries(targets),
    }


def process(task_id: int) -> None:
    """One worker's job: hand the task to Hermes, then see what it recorded."""
    task = db.session.get(TicketInterpretation, int(task_id))
    if task is None or task.status != RUNNING:
        return
    ticket = db.session.get(ZohoInboundTicket, task.ticket_record_id) if task.ticket_record_id else None
    if ticket is None:
        _fail(task, "The ticket is no longer in the inbound log.")
        return
    try:
        message = _task_message(task, ticket)
    except Exception as exc:  # noqa: BLE001
        _fail(task, f"Could not prepare the task for Hermes: {exc}")
        return
    if task.kind == "handle" and not message.get("catalog"):
        _fail(task, "No deploy targets are published for this provider — pick a source cluster and "
                    "namespaces in its Field sync first.")
        return

    try:
        text, model = hermes.run_task(message, session_key=SESSION_KEY)
    except hermes.HermesTransientError as exc:
        task = _reload(task_id)
        if task is None or task.status != RUNNING:
            return  # a tool settled it before the connection dropped
        if (task.attempts or 0) < _max_attempts():
            task.status = PENDING
            task.retry_at = _now() + timedelta(minutes=2 * (task.attempts or 1))
            task.error = f"{exc} Retrying."
            db.session.commit()
            return
        _fail(task, str(exc))
        return
    except hermes.HermesError as exc:
        task = _reload(task_id)
        if task is not None and task.status == RUNNING:
            _fail(task, str(exc))
        return

    task = _reload(task_id)
    if task is None:
        return
    summary = hermes.parse_summary(text)
    task.final_message = summary.get("summary") or None
    task.model = model
    if task.status == RUNNING:
        _fail(task, "Hermes finished without acting on the ticket"
                    + (f": {summary['summary']}" if summary.get("summary") else "."), commit=False)
    db.session.commit()


def _reload(task_id: int) -> Optional[TicketInterpretation]:
    # The tools commit from the MCP request threads while Hermes works.
    db.session.expire_all()
    return db.session.get(TicketInterpretation, int(task_id))


def _fail(task: TicketInterpretation, message: str, commit: bool = True) -> None:
    task.status = "error"
    task.error = message
    task.finished_at = _now()
    if task.kind == "followup":
        ticket = db.session.get(ZohoInboundTicket, task.ticket_record_id) if task.ticket_record_id else None
        if ticket is not None:
            _apply_fallback(ticket, task.event)
            task.error = f"{message} KubeSight posted its own update instead."
            outcome = ((task.event or {}).get("fallback") or {}).get("outcome")
            if outcome in schema.PARKED_STATUSES:
                # The ticket IS parked now, so a requester reply must resume it.
                task.route = outcome
    log_audit(
        "ticket_agent_failed",
        actor=None,
        target_type="ticket_interpretation",
        target_id=str(task.id),
        details={"ticket": task.ticket_number, "kind": task.kind, "error": message},
        commit=False,
    )
    if commit:
        db.session.commit()


# ---------------------------------------------------------------------------
# The tool side — what Hermes calls through MCP
# ---------------------------------------------------------------------------

def _ticket(record_id: Any) -> ZohoInboundTicket:
    try:
        ticket = db.session.get(ZohoInboundTicket, int(record_id))
    except (TypeError, ValueError):
        raise AgentError("ticketRecordId must be a number.", 400)
    if ticket is None:
        raise AgentError("No inbound ticket with that ticketRecordId.", 404)
    return ticket


def _current_task(ticket: ZohoInboundTicket, user=None) -> TicketInterpretation:
    """The task this tool call belongs to: the one Hermes is working on.

    Hermes may also be driven from chat ("handle ticket 12"), with no task
    KubeSight started — then one is opened on the spot, so what it does is
    recorded the same way.
    """
    task = (
        TicketInterpretation.query.filter(
            TicketInterpretation.ticket_record_id == ticket.id,
            TicketInterpretation.status.in_((RUNNING, PENDING)),
        )
        .order_by(TicketInterpretation.id.desc())
        .first()
    )
    if task is not None:
        return task
    return _new_task(
        ticket, "handle", status=RUNNING, requested_by=getattr(user, "username", None) or "hermes"
    )


def _post(ticket: ZohoInboundTicket, text: str) -> None:
    from .. import ticketing

    _remember(ticket, text)
    db.session.commit()
    ticketing.post_comment(
        ticket.provider or "zoho", ticket.ticket_id, text, public=agent_settings.comments_public()
    )


def _open_run(ticket: ZohoInboundTicket) -> Optional[DeployAutomationRun]:
    from ..deploy_automation_service import OPEN_STATUSES as RUN_OPEN

    return DeployAutomationRun.query.filter(
        DeployAutomationRun.ticket_record_id == ticket.id,
        DeployAutomationRun.status.in_(RUN_OPEN),
    ).first()


def brief(record_id: Any) -> Dict[str, Any]:
    """kubesight_ticket_get: the ticket, the catalog, and where it stands."""
    ticket = _ticket(record_id)
    targets = catalog.targets(ticket.provider or "zoho")
    runs = (
        DeployAutomationRun.query.filter_by(ticket_record_id=ticket.id)
        .order_by(DeployAutomationRun.id.desc()).limit(5).all()
    )
    tasks = (
        TicketInterpretation.query.filter_by(ticket_record_id=ticket.id)
        .order_by(TicketInterpretation.id.desc()).limit(5).all()
    )
    return {
        "ticketRecordId": ticket.id,
        "provider": ticket.provider or "zoho",
        "ticket": catalog.ticket_context(ticket),
        "catalog": catalog.catalog_entries(targets),
        "confidenceBar": agent_settings.get_or_create().min_confidence or "High",
        "runs": [
            {"id": r.id, "status": r.status, "changeType": r.change_type, "deployment": r.deployment_name,
             "namespace": r.namespace, "error": r.error}
            for r in runs
        ],
        "agentTasks": [serialize(t, brief=True) for t in tasks],
    }


def _record(task: TicketInterpretation, req: Dict[str, Any], plan: validator.Plan) -> None:
    task.decision = req
    task.confidence = req.get("confidence")
    task.understanding = req.get("understanding")
    task.change_type = plan.change_type
    snap = plan.snapshot
    task.snapshot_id = snap.id if snap else None
    task.cluster_id = snap.cluster_id if snap else None
    task.namespace = snap.namespace if snap else None
    task.deployment_name = snap.deployment_name if snap else None
    task.tag = plan.tag
    task.variable_name = plan.variable
    task.variable_value = plan.value


def _check(ticket: ZohoInboundTicket, req: Dict[str, Any]) -> validator.Plan:
    row = agent_settings.get_or_create()
    targets = catalog.targets(ticket.provider or "zoho")
    plan = validator.check(req, targets, ticket, row.min_confidence or "High")
    if plan.errors:
        raise AgentError(
            "Refused: " + " ".join(plan.errors)
            + " Fix the arguments, or set the ticket to impediment and ask the requester.",
            422,
        )
    return plan


def _start_run(ticket: ZohoInboundTicket, plan: validator.Plan, user, triggered_by: str):
    from ..deploy_automation_service import AutomationError, start_run

    override = {
        "snapshotId": plan.snapshot.id,
        "changeType": plan.change_type,
        "tag": plan.tag,
        "variable": plan.variable,
        "value": plan.value,
    }
    try:
        return start_run(ticket.id, user=user, auto=True, override=override, triggered_by=triggered_by)
    except AutomationError as exc:
        raise AgentError(str(exc), exc.status)


def execute(record_id: Any, arguments: Dict[str, Any], user=None) -> Dict[str, Any]:
    """kubesight_ticket_execute: check, start the run, post Hermes' comment."""
    ticket = _ticket(record_id)
    try:
        req = schema.action_request(arguments)
        text = schema.comment(arguments.get("comment"))
    except schema.ContractError as exc:
        raise AgentError(str(exc), 400)
    plan = _check(ticket, req)
    if plan.route == "approval":
        raise AgentError(
            "This needs approval before it runs: " + " ".join(plan.reasons)
            + " Call kubesight_ticket_request_approval with the same action, plus `comment` "
            "(posted now) and `commentOnApprove` (posted when it is approved).",
            409,
        )
    task = _current_task(ticket, user)
    run = _start_run(ticket, plan, user, TRIGGER)
    _record(task, req, plan)
    task.route, task.status = "execute", "executed"
    task.run_id = run.get("id")
    task.comment = text
    task.finished_at = _now()
    task.error = None
    db.session.commit()
    _post(ticket, text)
    log_audit(
        "ticket_agent_executed",
        actor=user,
        target_type="ticket_interpretation",
        target_id=str(task.id),
        details={"ticket": task.ticket_number, "runId": task.run_id, "action": req["action"],
                 "target": f"{task.namespace}/{task.deployment_name}", "confidence": task.confidence},
    )
    return {
        "started": True,
        "runId": task.run_id,
        "runStatus": run.get("status"),
        "ticketStatus": "in_progress",
        "note": "The ticket is In Progress and your comment is posted. KubeSight will send you a "
                "follow-up task when the run finishes — do not set the ticket to done yourself now.",
    }


def request_approval(record_id: Any, arguments: Dict[str, Any], user=None) -> Dict[str, Any]:
    """kubesight_ticket_request_approval: park the action behind a human."""
    ticket = _ticket(record_id)
    try:
        req = schema.action_request(arguments)
        text = schema.comment(arguments.get("comment"))
        on_approve = schema.comment(arguments.get("commentOnApprove"), "commentOnApprove")
        reasons = schema.str_list(arguments.get("reasons"), "reasons")
    except schema.ContractError as exc:
        raise AgentError(str(exc), 400)
    plan = _check(ticket, req)
    if _open_run(ticket):
        raise AgentError("A run is already going for this ticket.", 409)
    waiting = TicketInterpretation.query.filter_by(
        ticket_record_id=ticket.id, status="awaiting_approval"
    ).first()
    if waiting:
        raise AgentError(f"Task #{waiting.id} is already waiting for approval on this ticket.", 409)

    row = agent_settings.get_or_create()
    task = _current_task(ticket, user)
    _record(task, req, plan)
    task.decision = {**req, "commentOnApprove": on_approve}
    task.reasons = (reasons + [r for r in plan.reasons if r not in reasons])[:10] or [
        "Hermes asked for a human to confirm."
    ]
    task.route, task.status = "approval", "awaiting_approval"
    task.comment = text
    task.approval_nonce = secrets.token_hex(4)
    task.expires_at = _now() + timedelta(hours=int(row.approval_timeout_hours or 24))
    task.error = None
    db.session.commit()
    _post(ticket, text)

    where = "in KubeSight (Ticketing → Tickets & runs)"
    if agent_settings.telegram_ready(row):
        try:
            message_id = telegram.send_message(
                agent_settings.bot_token(row), row.telegram_chat_id, _approval_text(task, ticket),
                buttons=[[
                    {"text": "✅ Approve", "callback_data": f"ka:{task.id}:a:{task.approval_nonce}"},
                    {"text": "❌ Reject", "callback_data": f"ka:{task.id}:r:{task.approval_nonce}"},
                ]],
            )
            task.telegram_chat_id, task.telegram_message_id = row.telegram_chat_id, message_id
            where = "on Telegram and in KubeSight"
        except telegram.TelegramError as exc:
            task.error = f"Telegram: {exc} — approve it in KubeSight instead."
        db.session.commit()
    log_audit(
        "ticket_agent_approval_requested",
        actor=user,
        target_type="ticket_interpretation",
        target_id=str(task.id),
        details={"ticket": task.ticket_number, "action": req["action"], "reasons": task.reasons},
    )
    return {
        "approvalRequested": True,
        "taskId": task.id,
        "where": where,
        "expiresAt": task.expires_at.isoformat(),
        "note": "Your comment is posted. If it is approved, KubeSight starts the run and posts "
                "commentOnApprove; if it is rejected or expires you get a follow-up task.",
    }


def set_status(record_id: Any, status: str, comment_text: Any, user=None) -> Dict[str, Any]:
    """kubesight_ticket_set_status: move the ticket, with Hermes' comment."""
    from .. import ticketing

    ticket = _ticket(record_id)
    status = str(status or "").strip().lower().replace(" ", "_").replace("-", "_")
    outcome = schema.TICKET_STATUSES.get(status)
    if outcome is None:
        raise AgentError(f"status must be one of {', '.join(schema.TICKET_STATUSES)}.", 400)
    try:
        text = schema.comment(comment_text)
    except schema.ContractError as exc:
        raise AgentError(str(exc), 400)
    run = _open_run(ticket)
    if status == "done" and run is not None:
        raise AgentError(
            f"Run #{run.id} is still {run.status.replace('_', ' ')} — you will get a follow-up when it "
            "finishes. Do not close the ticket before that.",
            409,
        )

    task = _current_task(ticket, user)
    for waiting in TicketInterpretation.query.filter(
        TicketInterpretation.ticket_record_id == ticket.id,
        TicketInterpretation.status == "awaiting_approval",
        TicketInterpretation.id != task.id,
    ).all():
        _close_approval(waiting, "superseded", f"Superseded — Hermes set the ticket to {status}.")

    _remember(ticket, text)
    db.session.commit()
    ticketing.report_outcome(
        ticket.provider or "zoho", ticket.ticket_id, outcome,
        comment=text, public=agent_settings.comments_public(),
    )
    task.comment = text
    if status in schema.PARKED_STATUSES:
        # Parked on the requester: their next comment wakes Hermes again.
        task.route, task.status = status, status
        task.finished_at = _now()
    elif status in ("done", "failed"):
        task.route = task.route or "status"
        task.status = "done"
        task.finished_at = _now()
    task.error = None
    db.session.commit()
    log_audit(
        "ticket_agent_status_set",
        actor=user,
        target_type="ticket_interpretation",
        target_id=str(task.id),
        details={"ticket": task.ticket_number, "status": status},
    )
    return {"ticketStatus": status, "commentPosted": True}


def add_comment(record_id: Any, comment_text: Any, user=None) -> Dict[str, Any]:
    """kubesight_ticket_comment: a comment with no status change."""
    ticket = _ticket(record_id)
    try:
        text = schema.comment(comment_text)
    except schema.ContractError as exc:
        raise AgentError(str(exc), 400)
    _post(ticket, text)
    task = (
        TicketInterpretation.query.filter_by(ticket_record_id=ticket.id)
        .order_by(TicketInterpretation.id.desc()).first()
    )
    if task is not None:
        task.comment = text
        db.session.commit()
    return {"commentPosted": True}


# ---------------------------------------------------------------------------
# Approvals — Telegram buttons, the UI, and expiry
# ---------------------------------------------------------------------------

def _describe_change(task: TicketInterpretation) -> str:
    target = f"{task.deployment_name} in {task.namespace}"
    if task.change_type == "env_var":
        return f"set {task.variable_name}={task.variable_value} on {target}"
    if task.change_type == "restart":
        return f"restart {target}"
    return f"deploy {task.deployment_name} {task.tag} to {task.namespace}"


def _approval_text(task: TicketInterpretation, ticket: ZohoInboundTicket) -> str:
    lines = [
        f"🎫 {task.ticket_number or 'Ticket'} — Hermes needs approval",
        (ticket.subject or "").strip(),
        "",
        f"Understood: {task.understanding or '-'}",
        f"Will: {_describe_change(task)}",
        f"Confidence: {task.confidence or '-'}",
    ]
    if task.reasons:
        lines += ["", "Why it needs you:"] + [f"• {r}" for r in task.reasons]
    hours = max(1, int(((_aware(task.expires_at) or _now()) - _now()).total_seconds() // 3600))
    lines += ["", f"Expires in {hours}h."]
    return "\n".join(line for line in lines if line is not None)


def _edit_telegram(task: TicketInterpretation, suffix: str) -> None:
    if not (task.telegram_chat_id and task.telegram_message_id):
        return
    row = agent_settings.get_or_create()
    ticket = db.session.get(ZohoInboundTicket, task.ticket_record_id) if task.ticket_record_id else None
    try:
        body = _approval_text(task, ticket) if ticket else (task.ticket_number or "Ticket")
        body = body.rsplit("\n\nExpires in", 1)[0]
        telegram.edit_message(agent_settings.bot_token(row), task.telegram_chat_id,
                              task.telegram_message_id, f"{body}\n\n{suffix}")
    except telegram.TelegramError:
        logger.warning("Could not update the Telegram approval message for task %s", task.id)


def _close_approval(task: TicketInterpretation, status: str, note: str) -> None:
    task.status = status
    task.decision_note = note
    task.finished_at = _now()
    db.session.commit()
    _edit_telegram(task, note)


def _claim_approval(task_id: int, nonce: Optional[str]) -> TicketInterpretation:
    task = db.session.get(TicketInterpretation, int(task_id))
    if task is None:
        raise AgentError("Approval request not found.", 404)
    if nonce is not None and nonce != task.approval_nonce:
        raise AgentError("This approval button is stale.", 409)
    won = TicketInterpretation.query.filter_by(id=task.id, status="awaiting_approval").update(
        {"status": "deciding"}, synchronize_session=False
    )
    db.session.commit()
    if not won:
        db.session.refresh(task)
        raise AgentError(f"Already decided ({task.status.replace('_', ' ')}).", 409)
    db.session.refresh(task)
    return task


def approve(task_id: int, by: str, *, nonce: Optional[str] = None, user=None) -> Dict[str, Any]:
    task = _claim_approval(task_id, nonce)
    ticket = db.session.get(ZohoInboundTicket, task.ticket_record_id) if task.ticket_record_id else None
    task.decided_by, task.decided_at = by, _now()
    try:
        if ticket is None:
            raise AgentError("The ticket is no longer in the inbound log.", 404)
        req = dict(task.decision or {})
        on_approve = req.pop("commentOnApprove", None)
        plan = _check(ticket, req)  # targets may have changed while it waited
        run = _start_run(ticket, plan, user, f"{TRIGGER} · approved by {by}"[:120])
    except AgentError as exc:
        task.status, task.error, task.finished_at = "error", f"Approved, but could not start: {exc}", _now()
        db.session.commit()
        _edit_telegram(task, f"⚠️ Approved by {by}, but it could not start: {exc}")
        raise
    task.status, task.run_id, task.finished_at, task.error = "executed", run.get("id"), _now(), None
    db.session.commit()
    if on_approve:
        _post(ticket, on_approve)
    log_audit(
        "ticket_agent_approved",
        actor=user,
        target_type="ticket_interpretation",
        target_id=str(task.id),
        details={"ticket": task.ticket_number, "by": by, "runId": task.run_id},
    )
    _edit_telegram(task, f"✅ Approved by {by} — run #{task.run_id} started.")
    return serialize(task)


def reject(task_id: int, by: str, *, note: str = "", nonce: Optional[str] = None, user=None) -> Dict[str, Any]:
    task = _claim_approval(task_id, nonce)
    note = (note or "").strip()[:500]
    task.status, task.decided_by, task.decided_at = "impediment", by, _now()
    task.decision_note, task.finished_at = note or None, _now()
    db.session.commit()
    log_audit(
        "ticket_agent_rejected",
        actor=user,
        target_type="ticket_interpretation",
        target_id=str(task.id),
        details={"ticket": task.ticket_number, "by": by, "note": note},
    )
    _edit_telegram(task, f"❌ Rejected by {by}" + (f": {note}" if note else "."))
    queue_followup(task.ticket_record_id, {
        "type": "approval_rejected",
        "action": _describe_change(task),
        "rejectedBy": by,
        "note": note or None,
        "fallback": {
            "outcome": "impediment",
            "comment": "The DevOps team reviewed this request and did not run it"
                       + (f": {note}" if note else ".")
                       + " Please update the ticket with the details needed.",
        },
    })
    return serialize(task)


def expire_approvals() -> int:
    now = _now()
    expired = 0
    for task in TicketInterpretation.query.filter_by(status="awaiting_approval").all():
        if not task.expires_at or _aware(task.expires_at) > now:
            continue
        won = TicketInterpretation.query.filter_by(id=task.id, status="awaiting_approval").update(
            {"status": "impediment", "finished_at": now,
             "decision_note": "No one approved it in time."},
            synchronize_session=False,
        )
        db.session.commit()
        if not won:
            continue
        db.session.refresh(task)
        expired += 1
        _edit_telegram(task, "⌛ Expired — nobody approved it in time.")
        queue_followup(task.ticket_record_id, {
            "type": "approval_expired",
            "action": _describe_change(task),
            "fallback": {
                "outcome": "impediment",
                "comment": "This request was waiting for DevOps approval and nobody approved it in time. "
                           "Please confirm the details, or ask the DevOps team.",
            },
        })
    return expired


def _allowed(sender: Dict[str, Any], allow: List[str]) -> bool:
    if not allow:
        return True
    ids = {str(sender.get("id") or "").casefold(), str(sender.get("username") or "").casefold()}
    return bool(ids & set(allow))


def poll_telegram() -> int:
    """Collect Approve/Reject presses. Returns how many were handled."""
    row = agent_settings.get_or_create()
    if not agent_settings.telegram_ready(row):
        return 0
    if not TicketInterpretation.query.filter_by(status="awaiting_approval").first():
        return 0
    token = agent_settings.bot_token(row)
    try:
        updates = telegram.get_updates(token, row.telegram_update_offset)
    except telegram.TelegramError as exc:
        logger.warning("Telegram getUpdates failed: %s", exc)
        return 0
    allow = agent_settings.approvers(row)
    handled = 0
    for update in updates:
        row.telegram_update_offset = int(update.get("update_id", 0)) + 1
        db.session.commit()
        query = update.get("callback_query") or {}
        data = str(query.get("data") or "")
        if not data.startswith("ka:"):
            continue
        sender = query.get("from") or {}
        chat = str(((query.get("message") or {}).get("chat") or {}).get("id") or "")
        reply = ""
        try:
            _, task_id, verb, nonce = data.split(":", 3)
            if row.telegram_chat_id and not row.telegram_chat_id.startswith("@") and chat != row.telegram_chat_id:
                raise AgentError("Wrong chat.", 403)
            if not _allowed(sender, allow):
                raise AgentError("You are not on the approver list.", 403)
            who = f"@{sender['username']}" if sender.get("username") else (sender.get("first_name") or "Telegram user")
            who = f"{who} (Telegram)"
            if verb == "a":
                approve(int(task_id), who, nonce=nonce)
                reply = "Approved — the run is starting."
            elif verb == "r":
                reject(int(task_id), who, nonce=nonce)
                reply = "Rejected."
            handled += 1
        except (AgentError, ValueError) as exc:
            reply = str(exc) or "Could not handle that."
        try:
            if query.get("id"):
                telegram.answer_callback(token, query["id"], reply)
        except telegram.TelegramError:
            pass
    return handled


# ---------------------------------------------------------------------------
# Deploy automation → Hermes
# ---------------------------------------------------------------------------

def is_agent_run(run: DeployAutomationRun) -> bool:
    return str(run.triggered_by or "").startswith(TRIGGER)


def on_run_finished(run: DeployAutomationRun, outcome: str, comment: Optional[str],
                    resolution: Optional[str]) -> bool:
    """A Hermes-started run ended: Hermes writes the outcome onto the ticket.

    Returns True when this took over the write-back (the caller then skips its
    own). KubeSight's factual comment rides along as the fallback.
    """
    if not is_agent_run(run) or outcome not in ("deployed", "failed", "cancelled"):
        return False
    return queue_followup(run.ticket_record_id, {
        "type": "run_finished",
        "runId": run.id,
        "result": outcome,
        "deployment": run.deployment_name,
        "namespace": run.namespace,
        "changeType": run.change_type,
        "tag": run.image_tag or None,
        "variable": run.variable_name,
        "error": run.error,
        "fallback": {
            # A cancelled run leaves the ticket where an operator can see it.
            "outcome": "impediment" if outcome == "cancelled" else outcome,
            "comment": comment,
            "resolution": resolution,
        },
    })


# ---------------------------------------------------------------------------
# Requester replies — a comment on a parked ticket wakes Hermes again
# ---------------------------------------------------------------------------

_WS = re.compile(r"\s+")
# How many comment fingerprints to keep per ticket.
_POSTED_KEEP = 50


def _max_rounds() -> int:
    """How many times one ticket may bounce between Hermes and the requester."""
    return max(1, _int_env("TICKET_AGENT_MAX_ROUNDS", 10))


def _digest(text: str) -> str:
    normalised = _WS.sub(" ", catalog.html_to_text(text or "")).strip().casefold()
    return hashlib.sha256(normalised.encode("utf-8")).hexdigest()


def _remember(ticket: ZohoInboundTicket, text: Optional[str]) -> None:
    """Fingerprint a comment KubeSight is about to post (caller commits)."""
    if not text:
        return
    db.session.add(TicketAgentPostedComment(ticket_record_id=ticket.id, digest=_digest(text)))
    old = (
        TicketAgentPostedComment.query.filter_by(ticket_record_id=ticket.id)
        .order_by(TicketAgentPostedComment.id.desc()).offset(_POSTED_KEEP).all()
    )
    for row in old:
        db.session.delete(row)


def _is_ours(ticket: ZohoInboundTicket, text: str) -> bool:
    return bool(
        TicketAgentPostedComment.query.filter_by(ticket_record_id=ticket.id, digest=_digest(text)).first()
    )


def _parked_task(ticket: ZohoInboundTicket) -> Optional[TicketInterpretation]:
    """The task that parked this ticket on the requester, if it is parked now."""
    for task in (
        TicketInterpretation.query.filter_by(ticket_record_id=ticket.id)
        .order_by(TicketInterpretation.id.desc()).all()
    ):
        if task.status == "superseded":
            continue
        if task.status in (PENDING, RUNNING, "awaiting_approval", "deciding"):
            return None
        return task if task.route in schema.PARKED_STATUSES else None
    return None


def _conversation(ticket: ZohoInboundTicket, before: Optional[int] = None) -> List[Dict[str, Any]]:
    """What has been said so far: Hermes' comments and the requester's replies, in order."""
    query = TicketInterpretation.query.filter_by(ticket_record_id=ticket.id)
    if before is not None:
        query = query.filter(TicketInterpretation.id < before)
    out: List[Dict[str, Any]] = []
    for task in query.order_by(TicketInterpretation.id.asc()).all():
        event = task.event or {}
        if event.get("type") == "requester_replied":
            for c in event.get("comments") or []:
                out.append({"from": c.get("author") or "requester", "text": c.get("text")})
        if task.comment:
            out.append({"from": "you (Hermes)", "text": task.comment})
    return out[-12:]


def on_ticket_comment(
    provider: str,
    ticket_id: Any,
    text: Any,
    *,
    author: Optional[str] = None,
    comment_id: Optional[str] = None,
    ticket_status: Optional[str] = None,
) -> Dict[str, Any]:
    """A comment was added on a ticket. Wake Hermes if it was waiting on one.

    "Waiting" is read from the ticket's CURRENT status when the sender includes
    it (``ticket_status``) — a person may have moved the ticket to On Hold by
    hand, or back to In Progress to take it over, and the ticketing system is
    the truth about that. Without a status, it falls back to what Hermes itself
    last did (the task that parked it).

    Returns ``{"handled": bool, "reason": str}`` — the webhook answers 200
    either way (a ticketing system retrying an ignored comment helps nobody),
    and the reason says why a comment did or did not wake Hermes.
    """
    if not agent_settings.is_active():
        return {"handled": False, "reason": "The ticket agent is off."}
    ticket = (
        ZohoInboundTicket.query.filter_by(ticket_id=str(ticket_id)).first() if ticket_id else None
    )
    if ticket is None:
        return {"handled": False, "reason": "KubeSight has no ticket with that id."}
    body = catalog.html_to_text(text or "").strip()
    if not body:
        return {"handled": False, "reason": "Empty comment."}
    if _is_ours(ticket, body):
        return {"handled": False, "reason": "KubeSight's own comment."}
    entry = {"id": str(comment_id) if comment_id else None, "author": (author or "")[:120] or None,
             "text": body[:catalog.MAX_COMMENT_CHARS]}

    # Several replies in a row become one task, not one each.
    queued = (
        TicketInterpretation.query.filter_by(ticket_record_id=ticket.id, status=PENDING, kind="handle")
        .order_by(TicketInterpretation.id.desc()).first()
    )
    if queued is not None and (queued.event or {}).get("type") == "requester_replied":
        event = dict(queued.event)
        comments = list(event.get("comments") or [])
        if not any(entry["id"] and c.get("id") == entry["id"] for c in comments):
            comments.append(entry)
        event["comments"] = comments[-10:]
        queued.event = event
        db.session.commit()
        return {"handled": True, "reason": f"Added to task #{queued.id}, already queued."}

    parked = _parked_task(ticket)
    parked_as = parked.route if parked else None
    status = (ticket_status or "").strip()
    if status:
        by_status = _status_parks(ticket.provider or "zoho", status)
        if by_status is None:
            return {
                "handled": False,
                "reason": f"The ticket's status '{status}' is not your Impediment or On Hold status.",
            }
        if _busy(ticket):
            return {"handled": False, "reason": "Hermes is already working on this ticket."}
        parked_as = by_status
    if parked_as is None:
        return {"handled": False, "reason": "The ticket is not waiting on the requester."}
    if _open_run(ticket):
        return {"handled": False, "reason": "A run is going for this ticket."}
    rounds = TicketInterpretation.query.filter_by(ticket_record_id=ticket.id, kind="handle").count()
    if rounds >= _max_rounds():
        log_audit(
            "ticket_agent_round_limit",
            actor=None,
            target_type="ticket_interpretation",
            target_id=str(parked.id if parked else ""),
            details={"ticket": ticket.ticket_number, "rounds": rounds},
        )
        return {"handled": False, "reason": f"This ticket reached {rounds} rounds with Hermes — a person should take it."}

    task = _new_task(
        ticket, "handle", requested_by="requester reply",
        event={"type": "requester_replied", "parkedAs": parked_as, "comments": [entry]},
    )
    log_audit(
        "ticket_agent_resumed",
        actor=None,
        target_type="ticket_interpretation",
        target_id=str(task.id),
        details={"ticket": task.ticket_number, "after": parked_as, "author": entry["author"]},
    )
    kick()
    return {"handled": True, "reason": f"Hermes picked the ticket up again (task #{task.id})."}


def _status_parks(provider: str, status: str) -> Optional[str]:
    """``impediment`` / ``on_hold`` when ``status`` is that provider's label for it."""
    wanted = status.strip().casefold()
    try:
        if provider == "jira":
            from ...models import JiraIntegration

            row = JiraIntegration.query.get(1)
            labels = {
                "impediment": getattr(row, "transition_impediment", None) or "Impediment",
                "on_hold": getattr(row, "transition_on_hold", None) or "On Hold",
            }
        else:
            from ...models import ZohoIntegration

            row = ZohoIntegration.query.get(1)
            labels = {
                "impediment": getattr(row, "ticket_status_impediment", None) or "Impediment",
                "on_hold": getattr(row, "ticket_status_on_hold", None) or "On Hold",
            }
    except Exception:  # noqa: BLE001
        labels = {"impediment": "Impediment", "on_hold": "On Hold"}
    for key, label in labels.items():
        if label.strip().casefold() == wanted:
            return key
    return None


def _busy(ticket: ZohoInboundTicket) -> bool:
    return bool(
        TicketInterpretation.query.filter(
            TicketInterpretation.ticket_record_id == ticket.id,
            TicketInterpretation.status.in_((PENDING, RUNNING, "awaiting_approval", "deciding")),
        ).first()
    )


def parse_comment_webhook(payload: Any) -> List[Dict[str, Any]]:
    """Comments out of whatever delivered them.

    Two shapes: KubeSight's own ``{ticketId, comment, author, commentId}``
    (what the Deluge function sends), and a Zoho Desk webhook — a JSON array of
    ``{eventType: "Ticket_Comment_Add" | "Ticket_Thread_Add", payload: {...}}``.
    Outgoing threads (an agent's reply) are skipped; incoming ones are the
    requester answering by email.
    """
    out: List[Dict[str, Any]] = []
    events = payload if isinstance(payload, list) else [payload]
    for event in events:
        if not isinstance(event, dict):
            continue
        body = event.get("payload") if isinstance(event.get("payload"), dict) else event
        kind = str(event.get("eventType") or "")
        if kind == "Ticket_Thread_Add" and str(body.get("direction") or "in").lower() == "out":
            continue
        who = body.get("commenter") or body.get("author") or {}
        if isinstance(who, dict):
            author = who.get("name") or who.get("email") or who.get("emailId")
        else:
            author = str(who)
        out.append({
            "ticketId": body.get("ticketId") or body.get("ticket_id"),
            "text": body.get("comment") or body.get("content") or body.get("summary") or body.get("plainText"),
            "author": author or body.get("authorName"),
            "commentId": body.get("commentId") or body.get("id"),
            "status": body.get("status") or body.get("ticketStatus"),
        })
    return [c for c in out if c.get("ticketId")]


# ---------------------------------------------------------------------------
# Scheduler hook
# ---------------------------------------------------------------------------

def reap_stale() -> int:
    """Running tasks whose worker vanished (restart) go back in the queue."""
    cutoff = _now() - timedelta(seconds=_stale_seconds())
    reaped = 0
    for task in TicketInterpretation.query.filter_by(status=RUNNING).all():
        beat = _aware(task.heartbeat_at) or _aware(task.started_at)
        if beat and beat > cutoff:
            continue
        reaped += 1
        if (task.attempts or 0) < _max_attempts():
            task.status, task.retry_at = PENDING, None
            task.error = "The worker handling this task stopped; retrying."
            db.session.commit()
        else:
            _fail(task, "Hermes did not finish this task.")
    return reaped


def tick() -> None:
    """Every scheduler tick: reap, expire, collect button presses, dispatch."""
    for step in (reap_stale, expire_approvals, poll_telegram, kick):
        try:
            step()
        except Exception:  # noqa: BLE001 — one step must not starve the others
            db.session.rollback()
            logger.exception("Ticket agent tick step %s failed", step.__name__)


# ---------------------------------------------------------------------------
# Reads
# ---------------------------------------------------------------------------

def _iso(dt):
    dt = _aware(dt)
    return dt.isoformat() if dt else None


def serialize(task: TicketInterpretation, brief: bool = False) -> Dict[str, Any]:
    decision = dict(task.decision or {})
    out = {
        "id": task.id,
        "ticketRecordId": task.ticket_record_id,
        "ticketNumber": task.ticket_number,
        "provider": task.provider,
        "kind": task.kind,
        "status": task.status,
        "route": task.route,
        "confidence": task.confidence,
        "understanding": task.understanding,
        "comment": task.comment,
        "changeType": task.change_type,
        "action": decision.get("action"),
        "clusterId": task.cluster_id,
        "namespace": task.namespace,
        "deploymentName": task.deployment_name,
        "tag": task.tag,
        "variableName": task.variable_name,
        "variableValue": task.variable_value,
        "reasons": task.reasons or [],
        "concerns": decision.get("concerns") or [],
        "runId": task.run_id,
        "error": task.error,
        "finalMessage": task.final_message,
        "createdAt": _iso(task.created_at),
        "finishedAt": _iso(task.finished_at),
    }
    if brief:
        return out
    event = dict(task.event or {})
    event.pop("fallback", None)
    out.update({
        "event": event or None,
        "commentOnApprove": decision.get("commentOnApprove"),
        "attempts": task.attempts,
        "model": task.model,
        "requestedBy": task.requested_by,
        "decidedBy": task.decided_by,
        "decidedAt": _iso(task.decided_at),
        "decisionNote": task.decision_note,
        "expiresAt": _iso(task.expires_at),
        "telegramSent": bool(task.telegram_message_id),
        "startedAt": _iso(task.started_at),
    })
    return out


def tasks_by_ticket(record_ids: List[int]) -> Dict[int, List[Dict[str, Any]]]:
    """Every task per ticket, newest first — one query for the ticket list."""
    if not record_ids:
        return {}
    out: Dict[int, List[Dict[str, Any]]] = {}
    rows = (
        TicketInterpretation.query.filter(TicketInterpretation.ticket_record_id.in_(record_ids))
        .order_by(TicketInterpretation.id.desc()).all()
    )
    for task in rows:
        out.setdefault(task.ticket_record_id, []).append(serialize(task))
    return out


def delete_for_ticket(record_id: int) -> None:
    """The inbound log deletes a ticket: its tasks go with it (SQLite has no FK cascade)."""
    TicketInterpretation.query.filter_by(ticket_record_id=record_id).delete(synchronize_session=False)
    TicketAgentPostedComment.query.filter_by(ticket_record_id=record_id).delete(synchronize_session=False)


def ticket_has_open_task(record_id: int) -> bool:
    return bool(
        TicketInterpretation.query.filter(
            TicketInterpretation.ticket_record_id == record_id,
            TicketInterpretation.status.in_(OPEN_STATUSES + ("deciding",)),
        ).first()
    )
