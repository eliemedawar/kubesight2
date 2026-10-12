"""What one CI engine pass costs: queue claims, the reaper, the tick cadence."""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timedelta, timezone

from sqlalchemy import event

from api.db import db
from api.models_ci import CiBuild, CiBuildStage, CiLogChunk, CiService
from api.services.ci import engine
from api.services.ci import logs as logs_service
from api.services.ci import queue as queue_service

_NOW = datetime.now(timezone.utc)


@contextmanager
def _statements():
    seen = []

    def before(conn, cursor, statement, parameters, context, executemany):
        seen.append(statement)

    event.listen(db.engine, "before_cursor_execute", before)
    try:
        yield seen
    finally:
        event.remove(db.engine, "before_cursor_execute", before)


def _service(slug, cap=1):
    service = CiService(name=slug.title(), slug=slug, max_concurrent_builds=cap)
    db.session.add(service)
    db.session.flush()
    return service


def _build(service, number, status, minutes_ago, stages=()):
    build = CiBuild(
        service_id=service.id,
        number=number,
        status=status,
        queued_at=_NOW - timedelta(minutes=minutes_ago),
        started_at=(_NOW - timedelta(minutes=minutes_ago)) if status == "running" else None,
    )
    db.session.add(build)
    db.session.flush()
    for position, (stage_type, stage_status) in enumerate(stages):
        db.session.add(
            CiBuildStage(
                build_id=build.id,
                name=f"s{position}",
                position=position,
                stage_type=stage_type,
                status=stage_status,
                started_at=build.started_at if stage_status == "running" else None,
            )
        )
    return build


# ---------------------------------------------------------------------------
# Queue
# ---------------------------------------------------------------------------


def test_a_service_at_its_limit_does_not_starve_the_queue(app):
    with app.app_context():
        busy = _service("busy", cap=1)
        quiet = _service("quiet", cap=1)
        _build(busy, 1, "running", 60)
        held = [_build(busy, n, "queued", 50 - n) for n in range(2, 9)]
        waiting = _build(quiet, 1, "queued", 1)  # the newest build in the queue
        db.session.commit()

        claimed = queue_service.claim_next(5)
        assert claimed == [waiting.id]
        assert not set(claimed) & {build.id for build in held}


def test_only_as_many_of_one_service_as_its_limit_allows(app):
    with app.app_context():
        service = _service("pair", cap=2)
        builds = [_build(service, n, "queued", 10 - n) for n in range(1, 5)]
        db.session.commit()
        assert queue_service.claim_next(5) == [builds[0].id, builds[1].id]

        # Held builds say why they wait, once.
        assert queue_service.explain_concurrency_waits() == 2
        reasons = [db.session.get(CiBuild, b.id).queue_reason for b in builds]
        assert reasons[:2] == [None, None]
        assert all("2 concurrent" in reason for reason in reasons[2:])
        assert queue_service.explain_concurrency_waits() == 0


# ---------------------------------------------------------------------------
# Cadence
# ---------------------------------------------------------------------------


def test_builds_waiting_only_on_an_approval_let_the_engine_idle(app):
    with app.app_context():
        service = _service("approvals")
        _build(service, 1, "running", 5, stages=[("command", "success"), ("approval", "running")])
        db.session.commit()
        assert engine._needs_fast_ticks() is False

        _build(service, 2, "queued", 1, stages=[("command", "pending")])
        db.session.commit()
        assert engine._needs_fast_ticks() is True


def test_a_running_command_stage_keeps_the_fast_cadence(app):
    with app.app_context():
        service = _service("busy-cadence")
        _build(service, 1, "running", 5, stages=[("command", "running")])
        db.session.commit()
        assert engine._needs_fast_ticks() is True


# ---------------------------------------------------------------------------
# Reaper
# ---------------------------------------------------------------------------


def test_recent_stage_activity_answers_without_reading_the_log(app):
    with app.app_context():
        service = _service("progress")
        build = _build(service, 1, "running", 1, stages=[("command", "running")])
        db.session.commit()
        stage = build.stages[0]
        logs_service.append(stage, [(1, "x", "stdout")])
        cutoff = _NOW - timedelta(minutes=60)
        with _statements() as seen:
            engine._last_progress_at(build, recent_enough=cutoff)
        assert not any("ci_log_chunks" in statement for statement in seen)


def test_an_old_stage_that_is_still_logging_counts_its_newest_line(app):
    with app.app_context():
        service = _service("chatty")
        build = _build(service, 1, "running", 300, stages=[("command", "running")])
        db.session.commit()
        stage = build.stages[0]
        logs_service.append(stage, [(1, "old", "stdout"), (2, "new", "stdout")])
        newest = CiLogChunk.query.filter_by(build_stage_id=stage.id, seq=2).one()
        newest.created_at = _NOW - timedelta(minutes=2)
        CiLogChunk.query.filter_by(build_stage_id=stage.id, seq=1).one().created_at = (
            _NOW - timedelta(minutes=290)
        )
        db.session.commit()
        last = engine._last_progress_at(build, recent_enough=_NOW - timedelta(minutes=60))
        assert last is not None and last > _NOW - timedelta(minutes=3)


def test_the_reaper_runs_on_its_own_interval(app, monkeypatch):
    calls = []
    monkeypatch.setattr(engine, "_reap_stale_builds", lambda: calls.append(1))
    monkeypatch.setattr(engine, "_last_reap", 0.0)
    with app.app_context():
        engine._reap_when_due()
        engine._reap_when_due()
    assert calls == [1]


# ---------------------------------------------------------------------------
# Merge check delivery leaves the engine's thread
# ---------------------------------------------------------------------------


def test_verdicts_are_claimed_on_the_pass_and_sent_elsewhere(app, monkeypatch):
    from api.models_merge_checks import CiMergeCheck
    from api.services.ci.merge_checks import check_payload, delivery

    sent = []
    delivery.set_delivery_runner(sent.append)
    try:
        with app.app_context():
            service = _service("verdicts")
            check = CiMergeCheck(service_id=service.id, state="finished", verdict="allowed")
            db.session.add(check)
            db.session.commit()

            assert delivery.dispatch_due() == 1
            assert sent == [check.id]
            row = db.session.get(CiMergeCheck, check.id)
            assert row.delivery_state == delivery.SENDING
            # Shown as still pending; claimed once, not again while in flight.
            assert check_payload(row)["deliveryState"] == "pending"
            assert delivery.dispatch_due() == 0

            # A claim whose sender died comes back once it expires.
            row.next_delivery_at = _NOW - timedelta(seconds=1)
            db.session.commit()
            assert delivery.dispatch_due() == 1
    finally:
        delivery.set_delivery_runner(None)
