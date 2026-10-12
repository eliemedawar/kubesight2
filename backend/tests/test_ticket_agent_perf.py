"""The Hermes ticket agent's scheduler work, kept small.

Locked here: kick claims only as many tasks as there are free workers (a task
queued behind a busy pool would be reaped and handed out twice), a worker
heartbeats when it starts, no transaction is held while Hermes works, expiry
is found in SQL, and the VM-request tick only reads requests that can move.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from api.db import db
from api.models import TicketInterpretation
from api.services.ticket_agent import engine, hermes, vm_requests
from tests.test_ticket_agent import _ticket, agent, writes  # noqa: F401


def _task(ticket, status="pending", **fields):
    task = TicketInterpretation(ticket_record_id=ticket.id, provider="zoho", ticket_number="DR-1",
                                kind="handle", status=status, **fields)
    db.session.add(task)
    db.session.commit()
    return task


def test_kick_claims_only_free_workers(app, agent, monkeypatch):
    ticket = _ticket()
    ids = [_task(ticket).id for _ in range(5)]
    submitted = []

    class Pool:
        def submit(self, fn, app_obj, task_id):
            submitted.append(task_id)

    monkeypatch.setattr(engine, "_testing", lambda: False)
    monkeypatch.setattr(engine, "_pool", lambda: Pool())
    monkeypatch.setattr(engine, "_workers", lambda: 2)
    engine._in_flight.clear()
    try:
        assert engine.kick() == 2 and submitted == ids[:2]
        statuses = [db.session.get(TicketInterpretation, i).status for i in ids]
        assert statuses == ["running", "running", "pending", "pending", "pending"]
        assert engine.kick() == 0  # both workers busy
        # A claimed task its worker still runs is never reaped as stale.
        db.session.execute(db.text("UPDATE ticket_interpretations SET heartbeat_at = :t, started_at = :t "
                                   "WHERE id = :i"),
                           {"t": (datetime.now(timezone.utc) - timedelta(hours=2)).replace(tzinfo=None),
                            "i": ids[0]})
        db.session.commit()
        assert engine.reap_stale() == 0
        engine._in_flight.discard(ids[1])
        assert engine.kick() == 1 and submitted[-1] == ids[2]
    finally:
        engine._in_flight.clear()


def test_a_worker_heartbeats_when_it_starts(app, agent, monkeypatch):
    ticket = _ticket()
    claimed_at = datetime.now(timezone.utc) - timedelta(minutes=30)
    task = _task(ticket, status="running", heartbeat_at=claimed_at, started_at=claimed_at)
    engine._in_flight.add(task.id)
    seen = {}

    def process(task_id):
        row = db.session.get(TicketInterpretation, task_id)
        beat = row.heartbeat_at.replace(tzinfo=timezone.utc) if row.heartbeat_at.tzinfo is None else row.heartbeat_at
        seen["age"] = (datetime.now(timezone.utc) - beat).total_seconds()

    monkeypatch.setattr(engine, "process", process)
    engine._run_in_app(app, task.id)
    assert seen["age"] < 60
    assert task.id not in engine._in_flight


def test_no_transaction_is_open_while_hermes_works(app, agent, writes, monkeypatch):
    ticket = _ticket()
    task = _task(ticket, status="running", attempts=1)
    seen = {}

    def run_task(message, session_key=None):
        seen["in_transaction"] = db.session().in_transaction()
        return '{"outcome": "nothing", "summary": "did nothing"}', "hermes-test"

    monkeypatch.setattr(hermes, "run_task", run_task)
    engine.process(task.id)
    assert seen == {"in_transaction": False}


def test_expiry_is_found_in_sql(app, agent, writes):
    ticket = _ticket()
    now = datetime.now(timezone.utc)
    old = _task(ticket, status="awaiting_approval", expires_at=now - timedelta(minutes=1))
    fresh = _task(ticket, status="awaiting_approval", expires_at=now + timedelta(hours=1))
    never = _task(ticket, status="awaiting_approval")
    assert engine.expire_approvals() == 1
    db.session.expire_all()
    assert db.session.get(TicketInterpretation, old.id).status == "impediment"
    assert db.session.get(TicketInterpretation, fresh.id).status == "awaiting_approval"
    assert db.session.get(TicketInterpretation, never.id).status == "awaiting_approval"


def test_the_vm_tick_reads_only_requests_that_can_move(app, agent, monkeypatch):
    ticket = _ticket()
    route = vm_requests.ROUTE
    moving = _task(ticket, status=vm_requests.PLANNING, route=route, decision={"vmRequest": {"phase": "planning"}})
    executing = _task(ticket, status="executed", route=route, decision={"vmRequest": {"phase": "creating"}})
    cleanup = _task(ticket, status="impediment", route=route,
                    decision={"vmRequest": {"phase": "expired", "cleanup": True}})
    for status in ("impediment", "awaiting_approval", "executed"):
        _task(ticket, status=status, route="deploy", decision={})
    _task(ticket, status="impediment", route=route, decision={"vmRequest": {"phase": "rejected"}})
    seen = []
    monkeypatch.setattr(vm_requests, "_advance_one", lambda task: seen.append(task.id) or False)
    vm_requests.advance()
    assert sorted(seen) == sorted([moving.id, executing.id, cleanup.id])
