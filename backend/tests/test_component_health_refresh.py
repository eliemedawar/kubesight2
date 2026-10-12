"""Scheduled component health refresh: only due rows are read, quiet rows are cheap."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import event

from api.db import db
from api.models import TopologyComponent
from api.services import topology_component_service as tcs


def _statements(fn):
    seen = []

    def hook(conn, cursor, statement, parameters, context, executemany):
        seen.append(statement)

    event.listen(db.engine, "before_cursor_execute", hook)
    try:
        result = fn()
    finally:
        event.remove(db.engine, "before_cursor_execute", hook)
    return result, seen


def _webhook(name, *, checked_ago=None, status=None, message=None):
    now = datetime.now(timezone.utc)
    return TopologyComponent(
        name=name,
        check_type="webhook",
        heartbeat_interval_seconds=300,
        last_heartbeat_at=now,
        last_status=status,
        last_message=message,
        last_checked_at=None if checked_ago is None else now - timedelta(seconds=checked_ago),
    )


def test_fresh_components_are_not_read(app):
    with app.app_context():
        db.session.add_all([_webhook(f"fresh-{i}", checked_ago=1) for i in range(5)])
        db.session.commit()
        db.session.expunge_all()
        count, seen = _statements(tcs.refresh_stale_component_healths)
        assert count == 0
        assert len(seen) == 1  # the filtered SELECT, returning nothing


def test_unchanged_results_move_only_the_timestamp_in_one_update(app):
    with app.app_context():
        # First pass establishes the status/message each component reports.
        rows = [_webhook(f"quiet-{i}") for i in range(4)]
        db.session.add_all(rows)
        db.session.commit()
        assert tcs.refresh_stale_component_healths() == 4
        before = {c.id: c.last_checked_at for c in TopologyComponent.query.all()}

        count, seen = _statements(lambda: tcs.refresh_stale_component_healths(older_than_seconds=0))
        assert count == 4
        updates = [s for s in seen if s.lstrip().upper().startswith("UPDATE")]
        assert len(updates) == 1, updates
        after = {c.id: c.last_checked_at for c in TopologyComponent.query.all()}
        assert all(after[i] >= before[i] for i in before)


def test_changed_results_are_written(app):
    with app.app_context():
        component = _webhook("flips", checked_ago=600, status="unhealthy", message="old")
        db.session.add(component)
        db.session.commit()
        assert tcs.refresh_stale_component_healths() == 1
        refreshed = db.session.get(TopologyComponent, component.id)
        assert refreshed.last_status == "healthy"
        assert refreshed.last_message != "old"


def test_jitter_only_ever_lengthens_the_period_a_little():
    for component_id in range(1, 200):
        limit = tcs._jittered_threshold(component_id, 60, 0.2)
        assert 60 <= limit < 72
    assert len({tcs._jittered_threshold(i, 60, 0.2) for i in range(1, 50)}) > 10
    assert tcs._jittered_threshold(7, 60, 0.0) == 60


def test_the_probe_pool_is_reused(app):
    assert tcs._probe_executor() is tcs._probe_executor()
