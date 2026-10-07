"""The promotion timetable: releases leave each environment on a schedule.

Runs on the mock ladder (see test_promotion_rules): SIT -> UAT has three
applications ready (payments, ledger, checkout).
"""

from datetime import datetime, timedelta, timezone

import pytest

from api.db import db
from api.models import ChangeBundle, DeploymentRequestSetting, User
from api.models_promotion import PromotionDeparture, PromotionEnvironment, PromotionEvent, PromotionRelease
from api.services import promotion_mock
from api.services import promotion_service as svc
from api.services import promotion_timetable as tt
from tests.conftest import auth_headers

STAGING = "staging-eu-west"
PROD = "prod-us-east"
DAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")


@pytest.fixture(autouse=True)
def _fresh_mock_ladder():
    promotion_mock.reset()
    svc.invalidate_scan()
    yield
    promotion_mock.reset()
    svc.invalidate_scan()


def _admin() -> User:
    return User.query.filter_by(username="admin").first()


def _approvals(count: int) -> None:
    row = DeploymentRequestSetting.query.first() or DeploymentRequestSetting(recipients=[], group_ids=[])
    row.required_approvals = count
    row.cluster_required_approvals = {}
    row.recipients = ["approver@example.com"]
    db.session.add(row)
    db.session.commit()


def _ladder():
    svc.create_default_ladder(_admin())
    envs = {env.key: env for env in svc.ladder()}
    for key, cluster, ns in (
        ("dev", STAGING, "payments-dev"),
        ("sit", STAGING, "payments-sit"),
        ("uat", STAGING, "payments-uat"),
        ("preprod", PROD, "payments-preprod"),
    ):
        svc.add_binding(_admin(), envs[key].id, {"clusterId": cluster, "namespace": ns})
        svc.update_environment(_admin(), envs[key].id, {"mode": "enforce"})
    return envs


def _slot(days_ahead: int = 2) -> datetime:
    """A 14:00 UTC departure a couple of days from the real clock."""
    day = (datetime.now(timezone.utc) + timedelta(days=days_ahead)).date()
    return datetime(day.year, day.month, day.day, 14, 0, tzinfo=timezone.utc)


def _schedule(env, slot: datetime, cutoff: int = 15):
    return tt.set_schedule(
        _admin(),
        env.id,
        {"enabled": True, "days": {DAYS[slot.weekday()]: ["09:30", "14:00"]}, "cutoffMinutes": cutoff, "timezone": "UTC"},
    )


def test_schedules_are_validated():
    with pytest.raises(svc.PromotionError):
        tt.clean_schedule({"days": {"mon": ["25:00"]}})
    with pytest.raises(svc.PromotionError):
        tt.clean_schedule({"days": {"mon": ["09:00"]}, "timezone": "Mars/Olympus"})
    clean = tt.clean_schedule({"days": {"mon": ["14:00", "09:30", "14:00"]}, "cutoffMinutes": 20})
    assert clean["days"]["mon"] == ["09:30", "14:00"]
    assert clean["cutoffMinutes"] == 20


def test_departures_follow_the_schedule_in_its_timezone(app):
    envs = _ladder()
    tt.set_schedule(_admin(), envs["uat"].id, {"enabled": True, "days": {"tue": ["14:00"]}, "timezone": "Asia/Beirut"})
    start = datetime(2026, 10, 5, tzinfo=timezone.utc)  # a Monday
    deps = tt.departures_between(db.session.get(PromotionEnvironment, envs["uat"].id), start, start + timedelta(days=7))
    assert len(deps) == 1
    # 14:00 in Beirut (UTC+3 in October) is 11:00 UTC.
    assert deps[0]["departsAt"] == datetime(2026, 10, 6, 11, 0, tzinfo=timezone.utc)
    assert deps[0]["code"] == "UAT-1006-1"
    assert deps[0]["version"] == "2026.10.06.1"
    assert deps[0]["cutoffAt"] == deps[0]["departsAt"] - timedelta(minutes=15)


def test_the_entry_environment_has_no_schedule(app):
    envs = _ladder()
    with pytest.raises(svc.PromotionError):
        tt.set_schedule(_admin(), envs["dev"].id, {"enabled": True, "days": {"mon": ["09:00"]}})


def test_the_board_shows_boarding_scheduled_and_on_demand(client, admin_token):
    envs = _ladder()
    slot = _slot()
    _schedule(envs["uat"], slot)
    data = client.get("/api/promotions/timetable", headers=auth_headers(admin_token)).get_json()["data"]
    uat = [d for d in data["departures"] if d["toEnvironmentId"] == envs["uat"].id]
    assert uat[0]["status"] == "boarding"
    assert all(d["status"] == "scheduled" for d in uat[1:])
    # SIT and Pre-prod have no schedule: one open, on-demand release each.
    ondemand = [d for d in data["departures"] if d["kind"] == "ondemand"]
    assert {d["toEnvironmentId"] for d in ondemand} == {envs["sit"].id, envs["preprod"].id}


def test_hold_skip_and_moving_an_app_off_a_departure(app):
    envs = _ladder()
    slot = _slot()
    _schedule(envs["uat"], slot)
    tt.set_departure_state(_admin(), envs["uat"].id, slot.isoformat(), "held")
    assert tt.close_departure(envs["uat"], tt.slot_identity(envs["uat"], slot)) is None
    tt.set_departure_state(_admin(), envs["uat"].id, slot.isoformat(), "open")
    out = tt.set_excluded(_admin(), envs["uat"].id, slot.isoformat(), "ghcr.io/mock/payments", True)
    assert out["excluded"] == ["ghcr.io/mock/payments"]
    with pytest.raises(svc.PromotionError):
        tt.set_departure_state(_admin(), envs["uat"].id, (slot + timedelta(minutes=7)).isoformat(), "held")


def test_the_cut_off_closes_a_release_that_deploys_at_departure(app, monkeypatch):
    from api.services import change_bundle_executor
    from api.services.change_bundle_executor import process_due_bundles
    from api.services.change_bundle_service import decide_bundle

    envs = _ladder()
    _approvals(1)
    slot = _slot()
    _schedule(envs["uat"], slot)
    tt.set_excluded(_admin(), envs["uat"].id, slot.isoformat(), "ghcr.io/mock/payments", True)
    svc.overview()  # what runs where is recorded

    result = tt.run_due(now=slot - timedelta(minutes=10))
    assert result["closed"] == [f"UAT-{slot:%m%d}-2"]
    release = PromotionRelease.query.filter_by(code=f"UAT-{slot:%m%d}-2").one()
    assert release.version == f"{slot:%Y.%m.%d}.2"
    assert sorted(i["name"] for i in release.items) == ["checkout", "ledger"]
    bundle = db.session.get(ChangeBundle, release.bundle_ids[0])
    assert bundle.status == "pending_approval"
    assert bundle.requested_start_time.replace(tzinfo=timezone.utc) == slot
    # Idempotent: the next tick does nothing.
    assert tt.run_due(now=slot - timedelta(minutes=9))["closed"] == []

    approver = User.query.filter_by(username="operator").first()
    approver.email = "approver@example.com"
    db.session.commit()
    decide_bundle(bundle.id, "approve", actor=approver)
    # Approved, but nothing deploys before the departure.
    process_due_bundles(now=slot - timedelta(minutes=5))
    db.session.refresh(bundle)
    assert bundle.status == "approved"
    monkeypatch.setattr(change_bundle_executor, "_now", lambda: slot + timedelta(minutes=1))
    process_due_bundles(now=slot + timedelta(minutes=1))
    db.session.refresh(bundle)
    assert bundle.status == "completed"
    image = promotion_mock.get(STAGING, "payments-uat", "ledger-worker")["spec"]["template"]["spec"]["containers"][0]["image"]
    assert image == "ghcr.io/mock/ledger:v1.21.0"
    # payments was moved off this departure: still on its old version.
    image = promotion_mock.get(STAGING, "payments-uat", "payments-api")["spec"]["template"]["spec"]["containers"][0]["image"]
    assert image == "ghcr.io/mock/payments:v2.8.1"


def test_promoting_early_closes_the_departure(client, admin_token, no_cluster_approvals):
    envs = _ladder()
    slot = _slot()
    _schedule(envs["uat"], slot)
    headers = auth_headers(admin_token)
    items = tt.eligible_items(envs["uat"], [])
    body = {"environmentId": envs["uat"].id, "items": items, "departsAt": slot.isoformat()}
    release = client.post("/api/promotions/releases", headers=headers, json=body).get_json()["data"]
    assert release["code"] == f"UAT-{slot:%m%d}-2"
    assert release["status"] == "applied"
    row = PromotionDeparture.query.filter_by(environment_id=envs["uat"].id).one()
    assert row.state == "closed" and row.release_id == release["id"]
    again = client.post("/api/promotions/releases", headers=headers, json=body)
    assert again.status_code == 409
    board = client.get("/api/promotions/timetable", headers=headers).get_json()["data"]
    mine = next(d for d in board["departures"] if d["kind"] == "release")
    assert mine["code"] == release["code"] and mine["status"] == "promoted"


def test_a_manual_release_gets_a_manual_code(app, no_cluster_approvals):
    envs = _ladder()
    release = svc.create_release(_admin(), environment_id=envs["uat"].id, items=tt.eligible_items(envs["uat"], []))
    assert release["code"].startswith("UAT-") and release["code"].endswith("-M1")


def test_a_schedule_whose_owner_cannot_deploy_does_not_run(app):
    envs = _ladder()
    slot = _slot()
    _schedule(envs["uat"], slot)
    env = envs["uat"]
    env.schedule_owner_id = User.query.filter_by(username="viewer").first().id
    db.session.commit()
    assert tt.run_due(now=slot - timedelta(minutes=10))["closed"] == []
    assert PromotionEvent.query.filter_by(kind="blocked", path="schedule").count() == 1
