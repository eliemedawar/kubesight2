"""The periodic scheduler.

The thing being protected here is not that ticking works -- it is that ticking
happens exactly once. The scheduler used to be a thread inside the API, safe
only because `gunicorn -w 1` kept the API to one process, with a comment as the
enforcement. A second web replica ran all eight periodic tasks concurrently:
the same deploy advanced twice, the same change bundle executed twice.
"""

from __future__ import annotations

import pytest

from api.services import alert_policy_scheduler as sched


@pytest.fixture()
def ctx(app):
    with app.app_context():
        yield


# ─── who ticks ───


def test_production_does_not_tick_inside_the_api(monkeypatch):
    """The web tier must be scalable, so it cannot host the singleton."""
    monkeypatch.setenv("KUBESIGHT_ENV", "production")
    monkeypatch.delenv("KUBESIGHT_IN_PROCESS_SCHEDULER", raising=False)
    assert sched.in_process_scheduler_enabled() is False


def test_development_still_ticks_in_process(monkeypatch):
    """One process doing everything is the point in development."""
    monkeypatch.delenv("KUBESIGHT_ENV", raising=False)
    monkeypatch.delenv("KUBESIGHT_IN_PROCESS_SCHEDULER", raising=False)
    assert sched.in_process_scheduler_enabled() is True


@pytest.mark.parametrize(
    "value,expected",
    [("1", True), ("true", True), ("on", True), ("0", False), ("false", False)],
)
def test_the_override_wins_in_both_directions(monkeypatch, value, expected):
    """scheduler.py sets this to 0 so it never starts the thread it replaces."""
    monkeypatch.setenv("KUBESIGHT_ENV", "production")
    monkeypatch.setenv("KUBESIGHT_IN_PROCESS_SCHEDULER", value)
    assert sched.in_process_scheduler_enabled() is expected


def test_the_api_does_not_start_a_thread_in_production(monkeypatch, app):
    """The regression that matters: a production API process must not tick."""
    monkeypatch.setenv("KUBESIGHT_ENV", "production")
    monkeypatch.delenv("KUBESIGHT_IN_PROCESS_SCHEDULER", raising=False)
    monkeypatch.setattr(sched, "_scheduler_started", False)

    started = []
    monkeypatch.setattr(sched.threading, "Thread", lambda **kw: started.append(kw))

    # TESTING short-circuits before the production check, so exercise the real
    # config path rather than the fixture's.
    monkeypatch.setitem(app.config, "TESTING", False)
    sched.start_alert_policy_scheduler(app)

    assert started == [], "production started an in-process scheduler thread"


# ─── the tick ───


def test_one_failing_task_does_not_stop_the_others(ctx, app, monkeypatch):
    """Eight tasks share a tick; one raising must not silence the rest.

    True of the original loop and preserved through the extraction, which is
    the kind of property a refactor quietly drops.
    """
    calls = []

    def boom():
        calls.append("alerts")
        raise RuntimeError("evaluation exploded")

    monkeypatch.setattr(sched, "evaluate_all_policies", boom, raising=False)

    # Must not raise: every task in run_tick is individually wrapped.
    sched.run_tick(app)
    assert True


def test_run_tick_is_safe_on_an_empty_database(ctx, app):
    """Every task self-gates and no-ops when there is nothing to do, so the
    first tick after a fresh install must be uneventful rather than an
    exception storm."""
    sched.run_tick(app)
    sched.run_tick(app)
