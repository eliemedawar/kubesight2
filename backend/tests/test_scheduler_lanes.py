"""The scheduler runs alerts on their own lane so other jobs cannot delay them."""

from __future__ import annotations

import threading
import time

from api.services import alert_policy_scheduler as sched


def test_alert_lane_runs_health_then_evaluation_then_notifications(app, monkeypatch):
    from api import alert_notifier
    from api.services import alert_policy_evaluator, topology_component_service

    order = []
    monkeypatch.setattr(
        topology_component_service, "refresh_stale_component_healths", lambda: order.append("health")
    )
    monkeypatch.setattr(
        alert_policy_evaluator,
        "evaluate_all_enabled_policies",
        lambda persist=True: order.append("evaluate"),
    )
    monkeypatch.setattr(
        alert_notifier, "dispatch_active_alert_notifications", lambda: order.append("notify")
    )
    sched.run_alert_lane(app)
    assert order == ["health", "evaluate", "notify"]


def test_one_failing_job_does_not_stop_its_lane(app, monkeypatch):
    from api import alert_notifier
    from api.services import alert_policy_evaluator, topology_component_service

    order = []

    def boom():
        raise RuntimeError("cluster unreachable")

    monkeypatch.setattr(topology_component_service, "refresh_stale_component_healths", boom)
    monkeypatch.setattr(
        alert_policy_evaluator,
        "evaluate_all_enabled_policies",
        lambda persist=True: order.append("evaluate"),
    )
    monkeypatch.setattr(
        alert_notifier, "dispatch_active_alert_notifications", lambda: order.append("notify")
    )
    sched.run_alert_lane(app)
    assert order == ["evaluate", "notify"]


def test_tick_runs_both_lanes_in_order(app, monkeypatch):
    order = []
    monkeypatch.setattr(sched, "run_alert_lane", lambda a: order.append("alerts"))
    monkeypatch.setattr(sched, "run_background_lane", lambda a: order.append("background"))
    sched.run_scheduler_tick(app)
    assert order == ["alerts", "background"]


class _Elector:
    def __init__(self):
        self.leading = True

    def is_leader(self):
        return self.leading


def test_a_slow_background_lane_does_not_hold_alerts_back(app, monkeypatch):
    monkeypatch.setattr(sched, "_scheduler_tick_seconds", lambda: 0.01)
    elector = _Elector()
    release = threading.Event()
    stop = threading.Event()
    alert_passes = []
    background_passes = []

    def slow_background(_app):
        background_passes.append(1)
        release.wait(5)

    threads = [
        threading.Thread(
            target=sched._scheduler_loop,
            args=(app, lambda a: alert_passes.append(1), elector, stop),
            daemon=True,
        ),
        threading.Thread(
            target=sched._scheduler_loop, args=(app, slow_background, elector, stop), daemon=True
        ),
    ]
    for thread in threads:
        thread.start()
    deadline = time.time() + 10
    while not background_passes and time.time() < deadline:
        time.sleep(0.01)
    try:
        assert background_passes, "the background lane never ran"
        stuck_at = len(alert_passes)
        while len(alert_passes) < stuck_at + 5 and time.time() < deadline:
            time.sleep(0.01)
        # Several alert passes went by while one background pass was stuck.
        assert len(alert_passes) >= stuck_at + 5
        assert len(background_passes) == 1

        # Losing leadership stops both lanes.
        elector.leading = False
        release.set()
        time.sleep(0.1)
        seen = (len(alert_passes), len(background_passes))
        time.sleep(0.2)
        assert (len(alert_passes), len(background_passes)) == seen
    finally:
        elector.leading = False
        stop.set()
        release.set()
        for thread in threads:
            thread.join(2)
        assert not any(thread.is_alive() for thread in threads)
