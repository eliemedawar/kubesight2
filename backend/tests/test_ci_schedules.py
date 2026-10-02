"""Scheduled builds: the cron evaluator, the API, and firing on the engine pass.

The cron half is pure and tested against fixed instants, including the DST
nights in the zones this installation's users actually live in. The firing half
drives ``schedules.fire_due_schedules`` with an explicit ``now`` instead of
waiting for a clock, and proves the properties the module docstring promises:
exactly once across workers, a missed run fires once, skip-if-running, and a
broken schedule records why instead of crashing the pass.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from api.db import db
from api.models import AuditLog, User
from api.models_application_intelligence import BitbucketCredentialProfile
from api.models_ci import CiBuild, CiService
from api.models_ci_schedules import CiSchedule
from api.secret_encryption import encrypt_secret
from api.services.ci import cron
from api.services.ci import schedules as schedules_service
from tests.conftest import auth_headers

UTC = timezone.utc


def _at(*parts, tz=UTC):
    return datetime(*parts, tzinfo=tz)


def _runs(expression, after, tz, count=3):
    return cron.upcoming(cron.parse(expression), after, tz, count)


def _local(instants, tz):
    zone = cron.zone(tz)
    return [instant.astimezone(zone).strftime("%Y-%m-%d %H:%M %z") for instant in instants]


# ---------------------------------------------------------------------------
# Cron: parsing
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "expression, field, expected",
    [
        ("*/15 * * * *", "minutes", {0, 15, 30, 45}),
        ("1-5/2 * * * *", "minutes", {1, 3, 5}),
        ("5/20 * * * *", "minutes", {5, 25, 45}),
        ("0,30 9-11 * * *", "hours", {9, 10, 11}),
        ("0 0 * JAN,jul *", "months", {1, 7}),
        ("0 0 * * MON-FRI", "weekdays", {1, 2, 3, 4, 5}),
        ("0 0 * * 7", "weekdays", {0}),
        ("0 0 * * 5-7", "weekdays", {5, 6, 0}),
        ("0 0 * * sun,0", "weekdays", {0}),
    ],
)
def test_parse_reads_lists_ranges_steps_and_names(expression, field, expected):
    assert getattr(cron.parse(expression), field) == frozenset(expected)


@pytest.mark.parametrize(
    "alias, fields",
    [
        ("@hourly", "0 * * * *"),
        ("@daily", "0 0 * * *"),
        ("@midnight", "0 0 * * *"),
        ("@nightly", "0 2 * * *"),
        ("@weekly", "0 0 * * 0"),
        ("@monthly", "0 0 1 * *"),
        ("@yearly", "0 0 1 1 *"),
        ("@ANNUALLY", "0 0 1 1 *"),
    ],
)
def test_aliases_expand_to_their_five_fields(alias, fields):
    expression = cron.parse(alias)
    assert expression.fields == fields
    assert expression.source == alias.lower()


@pytest.mark.parametrize(
    "expression, message",
    [
        ("", "Enter a cron expression"),
        ("* * * *", "has five fields"),
        ("* * * * * *", "this one has 6"),
        ("60 * * * *", "minute field has 60, outside 0-59"),
        ("0 24 * * *", "hour field has 24"),
        ("0 0 0 * *", "day-of-month field has 0"),
        ("0 0 * 13 *", "month field has 13"),
        ("0 0 * * 8", "day-of-week field has 8"),
        ("10-5 * * * *", "runs backwards"),
        ("*/0 * * * *", "steps by 0"),
        ("0 0 * * FUN", "not a number or one of SUN"),
        ("0 0 * FOO *", "one of JAN"),
        ("a * * * *", "not a number"),
        ("0,,5 * * * *", "stray comma"),
        ("0 0 31 2,4 *", "would never run"),
        ("@reboot", "@reboot has no meaning"),
        ("@sometimes", "Unknown shortcut"),
    ],
)
def test_parse_refuses_with_a_reason(expression, message):
    with pytest.raises(cron.CronError, match=message):
        cron.parse(expression)


def test_feb_29_is_reachable_and_found_in_a_leap_year():
    runs = _runs("0 0 29 2 *", _at(2026, 3, 1), "UTC", 2)
    assert [run.date().isoformat() for run in runs] == ["2028-02-29", "2032-02-29"]


@pytest.mark.parametrize("name", ["", None, "UTC", "Asia/Beirut", "Europe/Berlin", "America/New_York"])
def test_zone_accepts_iana_names(name):
    assert cron.zone(name).key == (name or "UTC")


@pytest.mark.parametrize("name", ["Mars/Olympus", "../etc/passwd", "Asia/Beirut; rm", "x" * 80])
def test_zone_refuses_anything_else(name):
    with pytest.raises(cron.CronError):
        cron.zone(name)


# ---------------------------------------------------------------------------
# Cron: next run
# ---------------------------------------------------------------------------

def test_next_after_is_strictly_after():
    expression = cron.parse("0 2 * * *")
    assert cron.next_after(expression, _at(2026, 6, 1, 2, 0), "UTC") == _at(2026, 6, 2, 2, 0)
    assert cron.next_after(expression, _at(2026, 6, 1, 1, 59, 59), "UTC") == _at(2026, 6, 1, 2, 0)


def test_next_after_is_evaluated_on_the_zones_wall_clock():
    # 02:00 in Beirut in summer is 23:00 UTC the evening before.
    runs = _runs("0 2 * * *", _at(2026, 7, 1, 12), "Asia/Beirut", 2)
    assert runs == [_at(2026, 7, 1, 23), _at(2026, 7, 2, 23)]


def test_dom_and_dow_both_restricted_match_either():
    # The 1st of the month, OR any Monday — Vixie's rule.
    runs = _runs("0 9 1 * MON", _at(2026, 6, 1, 10), "UTC", 4)
    assert [run.date().isoformat() for run in runs] == [
        "2026-06-08", "2026-06-15", "2026-06-22", "2026-06-29",
    ]
    runs = _runs("0 9 1 * MON", _at(2026, 6, 29, 10), "UTC", 1)
    assert runs[0].date().isoformat() == "2026-07-01"


def test_a_star_step_day_field_still_means_and():
    # */2 in day-of-month starts with '*', so the two day fields are ANDed:
    # odd days that are also Mondays, not "odd days, or Mondays".
    expression = cron.parse("0 0 */2 * MON")
    runs = cron.upcoming(expression, _at(2026, 6, 1, 1), "UTC", 3)
    assert all(run.weekday() == 0 and run.day % 2 == 1 for run in runs)
    assert cron.describe(expression) == "On every 2nd day of every month (Mondays only) at 00:00"


def test_weekdays_skip_the_weekend():
    runs = _runs("30 8 * * 1-5", _at(2026, 10, 2, 9), "UTC", 2)  # a Friday, after 08:30
    assert [run.isoformat() for run in runs] == [
        "2026-10-05T08:30:00+00:00", "2026-10-06T08:30:00+00:00",
    ]


def test_a_years_schedule_finds_next_year_quickly():
    runs = _runs("@yearly", _at(2026, 1, 1, 0, 0, 1), "UTC", 1)
    assert runs == [_at(2027, 1, 1)]


# --- DST: Europe/Berlin (clocks 02:00 -> 03:00 on 2026-03-29, 03:00 -> 02:00 on 2026-10-25)

def test_berlin_spring_forward_moves_a_fixed_time_to_the_jump_once():
    runs = _runs("30 2 * * *", _at(2026, 3, 28, 12), "Europe/Berlin", 3)
    assert _local(runs, "Europe/Berlin") == [
        "2026-03-29 03:00 +0200",  # 02:30 does not exist; runs as the clocks land
        "2026-03-30 02:30 +0200",
        "2026-03-31 02:30 +0200",
    ]


def test_berlin_fall_back_runs_a_fixed_time_once():
    runs = _runs("30 2 * * *", _at(2026, 10, 24, 12), "Europe/Berlin", 2)
    assert _local(runs, "Europe/Berlin") == [
        "2026-10-25 02:30 +0200",  # the first 02:30 only
        "2026-10-26 02:30 +0100",
    ]


def test_berlin_fall_back_keeps_an_interval_an_interval():
    runs = _runs("*/30 * * * *", _at(2026, 10, 25, 0, 10), "Europe/Berlin", 4)
    assert [run.isoformat() for run in runs] == [
        "2026-10-25T00:30:00+00:00",  # 02:30 CEST
        "2026-10-25T01:00:00+00:00",  # 02:00 CET — the repeated hour runs again
        "2026-10-25T01:30:00+00:00",
        "2026-10-25T02:00:00+00:00",
    ]


def test_berlin_spring_forward_drops_interval_times_that_do_not_exist():
    runs = _runs("*/30 * * * *", _at(2026, 3, 29, 0, 10), "Europe/Berlin", 3)
    assert _local(runs, "Europe/Berlin") == [
        "2026-03-29 01:30 +0100",
        "2026-03-29 03:00 +0200",  # 02:00 and 02:30 were never on the clock
        "2026-03-29 03:30 +0200",
    ]
    gaps = {(b - a) for a, b in zip(runs, runs[1:])}
    assert gaps == {timedelta(minutes=30)}


# --- DST: America/New_York (02:00 -> 03:00 on 2026-03-08, 02:00 -> 01:00 on 2026-11-01)

def test_new_york_spring_forward():
    runs = _runs("30 2 * * *", _at(2026, 3, 7, 12), "America/New_York", 2)
    assert _local(runs, "America/New_York") == [
        "2026-03-08 03:00 -0400",
        "2026-03-09 02:30 -0400",
    ]


def test_new_york_fall_back_fires_once():
    runs = _runs("30 1 * * *", _at(2026, 10, 31, 12), "America/New_York", 2)
    assert [run.isoformat() for run in runs] == [
        "2026-11-01T05:30:00+00:00",  # 01:30 EDT, the first of the two
        "2026-11-02T06:30:00+00:00",  # 01:30 EST
    ]


def test_new_york_firing_inside_the_repeated_hour_does_not_fire_again():
    # The engine computes the next run from the moment it fired. Firing at the
    # first 01:30 must not find the second 01:30 that same night.
    expression = cron.parse("30 1 * * *")
    first = cron.next_after(expression, _at(2026, 11, 1, 5, 0), "America/New_York")
    assert first == _at(2026, 11, 1, 5, 30)
    assert cron.next_after(expression, first, "America/New_York") == _at(2026, 11, 2, 6, 30)


# --- DST: Asia/Beirut (00:00 -> 01:00 on the last Sunday of March, 2026-03-29)

def test_beirut_midnight_that_does_not_exist_runs_at_one():
    runs = _runs("0 0 * * *", _at(2026, 3, 28, 12), "Asia/Beirut", 2)
    assert _local(runs, "Asia/Beirut") == [
        "2026-03-29 01:00 +0300",
        "2026-03-30 00:00 +0300",
    ]


def test_beirut_nightly_follows_the_offset_change():
    winter = _runs("@nightly", _at(2026, 1, 10, 12), "Asia/Beirut", 1)[0]
    summer = _runs("@nightly", _at(2026, 7, 10, 12), "Asia/Beirut", 1)[0]
    assert (winter.hour, summer.hour) == (0, 23)  # 02:00 local, UTC+2 then UTC+3


def test_beirut_fall_back_runs_once():
    # Clocks go 00:00 -> 23:00 on Saturday night 2026-10-24 (last Sunday at 00:00).
    runs = _runs("30 23 * * *", _at(2026, 10, 24, 12), "Asia/Beirut", 2)
    assert len({run.date() for run in runs}) == 2
    assert runs[1] - runs[0] >= timedelta(hours=24)


# ---------------------------------------------------------------------------
# Cron: words and limits
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "expression, words",
    [
        ("* * * * *", "Every minute"),
        ("*/15 * * * *", "Every 15 minutes"),
        ("0 * * * *", "Every hour, on the hour"),
        ("@hourly", "Every hour, on the hour"),
        ("30 * * * *", "Every hour at 30 minutes past"),
        ("0 */2 * * *", "Every 2 hours, on the hour"),
        ("0 2 * * *", "Every day at 02:00"),
        ("@nightly", "Every day at 02:00"),
        ("30 8 * * 1-5", "Weekdays at 08:30"),
        ("0 8 * * MON-FRI", "Weekdays at 08:00"),
        ("0 3 * * 0", "Sundays at 03:00"),
        ("0 3 * * 7", "Sundays at 03:00"),
        ("0 10 * * 6,0", "Weekends at 10:00"),
        ("0 9 * * 1,3,5", "Mondays, Wednesdays and Fridays at 09:00"),
        ("0 9 * * 1-4", "Monday to Thursday at 09:00"),
        ("0 9,17 * * *", "Every day at 09:00 and 17:00"),
        ("0 8-18 * * 1-5", "Every hour from 08:00 to 18:00 on weekdays"),
        ("*/15 * * * 1-5", "Every 15 minutes on weekdays"),
        ("*/10 9-17 * * *", "Every 10 minutes between 09:00 and 17:59"),
        ("0 0 1 * *", "On the 1st of every month at 00:00"),
        ("0 0 1,15 * *", "On the 1st and 15th of every month at 00:00"),
        ("0 0 1 1 *", "On the 1st of January at 00:00"),
        ("0 2 * 12 *", "Every day in December at 02:00"),
        ("0 9 1 * MON", "On the 1st of every month, or on Mondays, at 09:00"),
    ],
)
def test_describe_in_plain_english(expression, words):
    assert cron.describe(cron.parse(expression)) == words


def test_min_interval_counts_the_wrap_around_midnight():
    assert cron.min_interval_minutes(cron.parse("*/15 * * * *")) == 15
    assert cron.min_interval_minutes(cron.parse("0 2 * * *")) == 24 * 60
    assert cron.min_interval_minutes(cron.parse("0 1,23 * * *")) == 120
    assert cron.min_interval_minutes(cron.parse("0,2 9 * * *")) == 2


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

NIGHTLY_PARAMS = [
    {"name": "NIGHTLY_SCAN", "type": "boolean", "label": "Nightly dependency scan", "default": False},
    {"name": "TARGET", "type": "choice", "choices": ["uat", "prod"], "default": "uat"},
]


@pytest.fixture()
def service_id(app, client, admin_token):
    """A runnable service whose pipeline gates a scan stage on NIGHTLY_SCAN."""
    with app.app_context():
        credential = BitbucketCredentialProfile(
            name="ci-token",
            provider="bitbucket",
            credential_type="repository_access_token",
            secret_cipher=encrypt_secret("clone-token-value"),
            read_only=True,
            enabled=True,
        )
        db.session.add(credential)
        db.session.commit()
        credential_id = credential.id

    headers = auth_headers(admin_token)
    sid = client.post(
        "/api/ci/services",
        json={"name": "Payment Service", "applicationType": "java"},
        headers=headers,
    ).get_json()["data"]["id"]
    client.put(
        f"/api/ci/services/{sid}/source",
        json={
            "repositoryUrl": "https://bitbucket.org/areeba/payment-service",
            "defaultBranch": "develop",
            "credentialProfileId": credential_id,
        },
        headers=headers,
    )
    pipeline_id = client.get(f"/api/ci/services/{sid}/pipelines", headers=headers).get_json()[
        "data"
    ]["items"][0]["id"]
    response = client.put(
        f"/api/ci/pipelines/{pipeline_id}",
        json={
            "parameters": NIGHTLY_PARAMS,
            "stages": [
                {"name": "Checkout", "stageType": "checkout", "runnerLabels": ["mock"]},
                {
                    "name": "Build",
                    "stageType": "command",
                    "commands": ["mvn -B package"],
                    "runnerLabels": ["mock"],
                },
                {
                    "name": "Dependency-Check",
                    "stageType": "command",
                    "commands": ["dependency-check.sh --scan ."],
                    "runnerLabels": ["mock"],
                    "runCondition": {"variable": "NIGHTLY_SCAN", "operator": "equals", "value": "true"},
                },
            ],
        },
        headers=headers,
    )
    assert response.status_code == 200, response.get_json()
    return sid


def _create(client, token, service_id, **overrides):
    payload = {"name": "Nightly", "cron": "0 2 * * *", "timezone": "Asia/Beirut", **overrides}
    return client.post(
        f"/api/ci/services/{service_id}/schedules", json=payload, headers=auth_headers(token)
    )


def _make_due(schedule_id, when):
    row = db.session.get(CiSchedule, schedule_id)
    row.next_run_at = when
    db.session.commit()


def _builds(service_id):
    db.session.expire_all()
    return CiBuild.query.filter_by(service_id=service_id).order_by(CiBuild.id).all()


def _row(schedule_id):
    db.session.expire_all()
    return db.session.get(CiSchedule, schedule_id)


# ---------------------------------------------------------------------------
# API: CRUD and validation
# ---------------------------------------------------------------------------

def test_create_lists_and_describes_a_schedule(client, admin_token, service_id):
    response = _create(client, admin_token, service_id, variables={"NIGHTLY_SCAN": True})
    assert response.status_code == 201, response.get_json()
    data = response.get_json()["data"]
    assert data["description"] == "Every day at 02:00"
    assert data["timezone"] == "Asia/Beirut"
    assert data["enabled"] is True and data["skipIfRunning"] is True
    assert data["variables"] == {"NIGHTLY_SCAN": "true"}
    assert data["runsAs"] == "admin"
    assert len(data["upcoming"]) == 3
    # 02:00 in Beirut is never 02:00 UTC.
    assert not data["nextRunAt"].startswith("02:00")

    listing = client.get(
        f"/api/ci/services/{service_id}/schedules", headers=auth_headers(admin_token)
    ).get_json()["data"]
    assert [item["name"] for item in listing["items"]] == ["Nightly"]
    assert listing["defaultBranch"] == "develop"
    pipeline = listing["pipelines"][0]
    assert pipeline["isDefault"] is True
    assert [param["name"] for param in pipeline["parameters"]] == ["NIGHTLY_SCAN", "TARGET"]
    assert pipeline["conditions"] == [
        {"stage": "Dependency-Check", "variable": "NIGHTLY_SCAN", "operator": "equals", "value": "true"}
    ]


@pytest.mark.parametrize(
    "overrides, message",
    [
        ({"name": ""}, "Give the schedule a name"),
        ({"cron": "0 25 * * *"}, "hour field has 25"),
        ({"cron": "* * * * *"}, "shortest interval"),
        ({"timezone": "Mars/Base"}, "Unknown timezone"),
        ({"variables": {"NOPE": "1"}}, "no parameter named 'NOPE'"),
        ({"variables": {"TARGET": "staging"}}, "must be one of: uat, prod"),
        ({"variables": "NIGHTLY_SCAN=true"}, "must be an object"),
        ({"refType": "tag", "branch": ""}, "Name the tag"),
        ({"refType": "commit"}, "branch or a tag"),
        ({"pipelineId": 999999}, "does not belong to this service"),
    ],
)
def test_create_refuses_bad_input(client, admin_token, service_id, overrides, message):
    response = _create(client, admin_token, service_id, **overrides)
    assert response.status_code == 400
    assert message in response.get_json()["error"]


def test_names_are_unique_per_service(client, admin_token, service_id):
    assert _create(client, admin_token, service_id).status_code == 201
    response = _create(client, admin_token, service_id, name="nightly")
    assert response.status_code == 400
    assert "already has a schedule" in response.get_json()["error"]


def test_update_re_arms_only_when_the_timing_changes(app, client, admin_token, service_id):
    created = _create(client, admin_token, service_id).get_json()["data"]
    headers = auth_headers(admin_token)
    before = _row(created["id"]).next_run_at

    renamed = client.put(
        f"/api/ci/services/{service_id}/schedules/{created['id']}",
        json={"branch": "release/1.0"},
        headers=headers,
    )
    assert renamed.status_code == 200
    assert _row(created["id"]).next_run_at == before

    off = client.put(
        f"/api/ci/services/{service_id}/schedules/{created['id']}",
        json={"enabled": False},
        headers=headers,
    ).get_json()["data"]
    assert off["nextRunAt"] is None and off["upcoming"] == []
    assert _row(created["id"]).next_run_at is None

    weekly = client.put(
        f"/api/ci/services/{service_id}/schedules/{created['id']}",
        json={"enabled": True, "cron": "0 3 * * SUN"},
        headers=headers,
    ).get_json()["data"]
    assert weekly["description"] == "Sundays at 03:00"
    assert weekly["nextRunAt"] is not None

    actions = [row.action for row in AuditLog.query.filter_by(target_type="ci_schedule").all()]
    assert actions.count("ci_schedule_created") == 1
    assert actions.count("ci_schedule_updated") == 3


def test_delete_removes_and_audits(client, admin_token, service_id):
    created = _create(client, admin_token, service_id).get_json()["data"]
    response = client.delete(
        f"/api/ci/services/{service_id}/schedules/{created['id']}", headers=auth_headers(admin_token)
    )
    assert response.status_code == 200
    assert _row(created["id"]) is None
    assert AuditLog.query.filter_by(action="ci_schedule_deleted").count() == 1


def test_a_schedule_of_another_service_is_not_found(client, admin_token, service_id):
    created = _create(client, admin_token, service_id).get_json()["data"]
    other = client.post(
        "/api/ci/services", json={"name": "Other"}, headers=auth_headers(admin_token)
    ).get_json()["data"]["id"]
    response = client.put(
        f"/api/ci/services/{other}/schedules/{created['id']}",
        json={"name": "x"},
        headers=auth_headers(admin_token),
    )
    assert response.status_code == 404


def test_deleting_the_service_deletes_its_schedules(client, admin_token, service_id):
    created = _create(client, admin_token, service_id).get_json()["data"]
    client.delete(f"/api/ci/services/{service_id}", headers=auth_headers(admin_token))
    assert _row(created["id"]) is None


def test_preview_returns_words_and_five_runs(client, viewer_token):
    response = client.post(
        "/api/ci/schedules/preview",
        json={"cron": "30 8 * * 1-5", "timezone": "Europe/Berlin"},
        headers=auth_headers(viewer_token),
    )
    data = response.get_json()["data"]
    assert response.status_code == 200
    assert data["valid"] is True
    assert data["description"] == "Weekdays at 08:30"
    assert len(data["nextRuns"]) == 5
    for run in data["nextRuns"]:
        local = datetime.fromisoformat(run).astimezone(cron.zone("Europe/Berlin"))
        assert (local.hour, local.minute) == (8, 30) and local.weekday() < 5

    bad = client.post(
        "/api/ci/schedules/preview",
        json={"cron": "0 0 30 2 *", "timezone": "UTC"},
        headers=auth_headers(viewer_token),
    ).get_json()["data"]
    assert bad["valid"] is False and "never run" in bad["error"]


# ---------------------------------------------------------------------------
# Permissions
# ---------------------------------------------------------------------------

def test_viewer_reads_but_cannot_change_or_run(client, admin_token, viewer_token, service_id):
    created = _create(client, admin_token, service_id).get_json()["data"]
    headers = auth_headers(viewer_token)
    assert client.get(f"/api/ci/services/{service_id}/schedules", headers=headers).status_code == 200
    assert _create(client, viewer_token, service_id, name="Mine").status_code == 403
    assert (
        client.post(
            f"/api/ci/services/{service_id}/schedules/{created['id']}/run", headers=headers
        ).status_code
        == 403
    )


def test_saving_needs_pipeline_edit_and_build_run(client, admin_token, operator_token, service_id):
    # The operator role runs builds but does not edit pipelines.
    created = _create(client, admin_token, service_id).get_json()["data"]
    headers = auth_headers(operator_token)
    assert _create(client, operator_token, service_id, name="Ops").status_code == 403
    assert (
        client.put(
            f"/api/ci/services/{service_id}/schedules/{created['id']}",
            json={"enabled": False},
            headers=headers,
        ).status_code
        == 403
    )
    assert (
        client.delete(
            f"/api/ci/services/{service_id}/schedules/{created['id']}", headers=headers
        ).status_code
        == 403
    )
    # ...and Run now is a build like any other.
    response = client.post(
        f"/api/ci/services/{service_id}/schedules/{created['id']}/run", headers=headers
    )
    assert response.status_code == 201
    assert response.get_json()["data"]["requestedBy"] == "operator"


# ---------------------------------------------------------------------------
# Firing
# ---------------------------------------------------------------------------

def test_a_due_schedule_triggers_a_build_through_the_normal_path(app, client, admin_token, service_id):
    created = _create(
        client, admin_token, service_id, variables={"NIGHTLY_SCAN": True, "TARGET": "prod"}
    ).get_json()["data"]
    now = _at(2026, 10, 1, 23, 0, 30)
    _make_due(created["id"], _at(2026, 10, 1, 23, 0))

    assert schedules_service.fire_due_schedules(now=now) == 1

    builds = _builds(service_id)
    assert len(builds) == 1
    build = builds[0]
    assert build.trigger_type == "schedule"
    assert build.branch == "develop"  # the service default, since none was named
    assert build.requested_by.username == "admin"
    snapshot = build.pipeline_snapshot
    assert snapshot["variables"]["NIGHTLY_SCAN"] == "true"
    assert snapshot["variables"]["TARGET"] == "prod"
    assert snapshot["schedule"] == {"id": created["id"], "name": "Nightly"}

    row = _row(created["id"])
    assert row.last_outcome == "triggered"
    assert row.last_build_id == build.id
    assert row.next_run_at.replace(tzinfo=UTC) > now

    drawer = client.get(f"/api/ci/builds/{build.id}", headers=auth_headers(admin_token)).get_json()["data"]
    assert drawer["triggerType"] == "schedule"
    assert drawer["schedule"]["name"] == "Nightly"
    listed = client.get(
        f"/api/ci/services/{service_id}/schedules", headers=auth_headers(admin_token)
    ).get_json()["data"]["items"][0]
    assert listed["lastBuild"]["number"] == build.number

    actions = {row.action for row in AuditLog.query.all()}
    assert {"ci_schedule_fired", "ci_build_triggered"} <= actions


def test_the_nightly_scan_stage_runs_only_on_the_schedule(app, client, admin_token, service_id):
    """The documented recipe: a run condition on a boolean input the schedule sets."""
    from api.services.ci import engine

    created = _create(client, admin_token, service_id, variables={"NIGHTLY_SCAN": True}).get_json()["data"]
    _make_due(created["id"], _at(2026, 10, 1, 23))
    schedules_service.fire_due_schedules(now=_at(2026, 10, 1, 23, 0, 5))
    manual = client.post(
        f"/api/ci/services/{service_id}/builds", json={}, headers=auth_headers(admin_token)
    ).get_json()["data"]

    scheduled, by_hand = _builds(service_id)
    assert by_hand.id == manual["id"]
    scan = next(stage for stage in scheduled.pipeline_snapshot["stages"] if stage["name"] == "Dependency-Check")
    assert engine._condition_reason(scheduled, scan) is None
    assert "runs only when NIGHTLY_SCAN is 'true'" in engine._condition_reason(by_hand, scan)


def test_two_workers_racing_on_one_due_run_fire_it_once(app, client, admin_token, service_id):
    created = _create(client, admin_token, service_id).get_json()["data"]
    due = _at(2026, 10, 1, 23)
    _make_due(created["id"], due)
    now = _at(2026, 10, 1, 23, 0, 1)

    # Both workers read the row while it said 23:00; each then tries to claim.
    stored_due = db.session.query(CiSchedule.next_run_at).filter_by(id=created["id"]).scalar()
    first = schedules_service._fire_one(created["id"], stored_due, now)
    second = schedules_service._fire_one(created["id"], stored_due, now)

    assert (first, second) == (True, False)
    assert len(_builds(service_id)) == 1
    assert AuditLog.query.filter_by(action="ci_schedule_fired").count() == 1


def test_a_pass_that_sees_nothing_due_fires_nothing(app, client, admin_token, service_id):
    _create(client, admin_token, service_id)
    assert schedules_service.fire_due_schedules(now=_at(2020, 1, 1)) == 0
    assert _builds(service_id) == []


def test_runs_missed_while_down_fire_once(app, client, admin_token, service_id):
    created = _create(client, admin_token, service_id).get_json()["data"]
    # Down over three nightly runs.
    _make_due(created["id"], _at(2026, 9, 28, 23))
    now = _at(2026, 10, 1, 12)

    assert schedules_service.fire_due_schedules(now=now) == 1
    assert schedules_service.fire_due_schedules(now=now + timedelta(seconds=1)) == 0
    assert len(_builds(service_id)) == 1
    # Re-armed for tonight, not for one of the missed nights.
    assert _row(created["id"]).next_run_at.replace(tzinfo=UTC) == _at(2026, 10, 1, 23)
    fired = AuditLog.query.filter_by(action="ci_schedule_fired").one()
    assert fired.details["late_seconds"] > 2 * 24 * 3600


def test_skip_if_running_records_a_skip_and_moves_on(app, client, admin_token, service_id):
    created = _create(client, admin_token, service_id).get_json()["data"]
    _make_due(created["id"], _at(2026, 10, 1, 23))
    schedules_service.fire_due_schedules(now=_at(2026, 10, 1, 23, 0, 1))
    first_build = _builds(service_id)[0]
    assert first_build.status == "queued"

    _make_due(created["id"], _at(2026, 10, 2, 23))
    assert schedules_service.fire_due_schedules(now=_at(2026, 10, 2, 23, 0, 1)) == 1

    assert len(_builds(service_id)) == 1
    row = _row(created["id"])
    assert row.last_outcome == "skipped"
    assert f"build #{first_build.number}" in row.last_error
    assert row.last_build_id == first_build.id
    assert row.next_run_at.replace(tzinfo=UTC) == _at(2026, 10, 3, 23)
    assert AuditLog.query.filter_by(action="ci_schedule_skipped").count() == 1


def test_without_skip_if_running_a_second_build_queues(app, client, admin_token, service_id):
    created = _create(client, admin_token, service_id, skipIfRunning=False).get_json()["data"]
    _make_due(created["id"], _at(2026, 10, 1, 23))
    schedules_service.fire_due_schedules(now=_at(2026, 10, 1, 23, 0, 1))
    _make_due(created["id"], _at(2026, 10, 2, 23))
    schedules_service.fire_due_schedules(now=_at(2026, 10, 2, 23, 0, 1))
    assert len(_builds(service_id)) == 2


@pytest.mark.parametrize("status", ["paused", "archived"])
def test_a_service_that_is_not_active_never_fires(app, client, admin_token, service_id, status):
    created = _create(client, admin_token, service_id).get_json()["data"]
    service = db.session.get(CiService, service_id)
    service.status = status
    db.session.commit()
    _make_due(created["id"], _at(2026, 10, 1, 23))

    assert schedules_service.fire_due_schedules(now=_at(2026, 10, 1, 23, 0, 1)) == 1

    assert _builds(service_id) == []
    row = _row(created["id"])
    assert row.last_outcome == "skipped"
    assert status in row.last_error
    # Moved on, so reactivating does not fire a stale run the moment it happens.
    assert row.next_run_at.replace(tzinfo=UTC) == _at(2026, 10, 2, 23)


def test_a_disabled_schedule_is_never_due(app, client, admin_token, service_id):
    created = _create(client, admin_token, service_id, enabled=False).get_json()["data"]
    assert created["nextRunAt"] is None
    row = _row(created["id"])
    row.next_run_at = _at(2026, 10, 1, 23)  # even with a stale time left behind
    db.session.commit()
    assert schedules_service.fire_due_schedules(now=_at(2026, 10, 2)) == 0
    assert _builds(service_id) == []


def test_a_deleted_pipeline_is_reported_and_the_pass_survives(app, client, admin_token, service_id):
    from api.services.ci import engine

    headers = auth_headers(admin_token)
    extra = client.post(
        f"/api/ci/services/{service_id}/pipelines",
        json={
            "name": "nightly-only",
            "isDefault": False,
            "stages": [{"name": "Build", "stageType": "command", "commands": ["make"], "runnerLabels": ["mock"]}],
        },
        headers=headers,
    )
    assert extra.status_code == 201, extra.get_json()
    extra_id = extra.get_json()["data"]["id"]
    created = _create(client, admin_token, service_id, pipelineId=extra_id).get_json()["data"]
    assert created["pipelineName"] == "nightly-only"
    assert client.delete(f"/api/ci/pipelines/{extra_id}", headers=headers).status_code == 200

    _make_due(created["id"], datetime.now(UTC) - timedelta(minutes=1))
    # Through the real engine entry point: a broken schedule must not raise.
    engine.advance_ci_builds()

    assert _builds(service_id) == []
    row = _row(created["id"])
    assert row.last_outcome == "failed"
    assert "no longer exists" in row.last_error
    listed = client.get(f"/api/ci/services/{service_id}/schedules", headers=headers).get_json()["data"]
    assert "no longer exists" in listed["items"][0]["pipelineProblem"]
    assert AuditLog.query.filter_by(action="ci_schedule_failed").count() == 1


def test_a_trigger_the_engine_refuses_is_recorded_not_raised(app, client, admin_token, service_id):
    created = _create(client, admin_token, service_id, variables={"TARGET": "prod"}).get_json()["data"]
    # The pipeline changes under the schedule: TARGET loses the 'prod' option.
    pipeline_id = client.get(
        f"/api/ci/services/{service_id}/pipelines", headers=auth_headers(admin_token)
    ).get_json()["data"]["items"][0]["id"]
    params = [dict(NIGHTLY_PARAMS[0]), {**NIGHTLY_PARAMS[1], "choices": ["uat"]}]
    client.put(f"/api/ci/pipelines/{pipeline_id}", json={"parameters": params}, headers=auth_headers(admin_token))

    _make_due(created["id"], _at(2026, 10, 1, 23))
    assert schedules_service.fire_due_schedules(now=_at(2026, 10, 1, 23, 0, 1)) == 1
    row = _row(created["id"])
    assert row.last_outcome == "failed"
    assert "must be one of: uat" in row.last_error
    assert _builds(service_id) == []


def test_a_schedule_stops_when_its_owner_loses_the_right_to_build(app, client, admin_token, service_id):
    created = _create(client, admin_token, service_id).get_json()["data"]
    viewer = User.query.filter_by(username="viewer").one()
    row = _row(created["id"])
    row.updated_by_user_id = viewer.id
    db.session.commit()

    _make_due(created["id"], _at(2026, 10, 1, 23))
    schedules_service.fire_due_schedules(now=_at(2026, 10, 1, 23, 0, 1))
    row = _row(created["id"])
    assert row.last_outcome == "failed"
    assert "viewer, who can no longer run builds" in row.last_error
    assert _builds(service_id) == []


def test_the_engine_pass_fires_a_due_schedule_even_when_idle(app, client, admin_token, service_id):
    from api.services.ci import engine

    created = _create(client, admin_token, service_id).get_json()["data"]
    _make_due(created["id"], datetime.now(UTC) - timedelta(seconds=5))
    assert CiBuild.query.count() == 0

    engine.advance_ci_builds()

    builds = _builds(service_id)
    assert len(builds) == 1 and builds[0].trigger_type == "schedule"


def test_seconds_until_next_due_drives_the_idle_ticker(app, client, admin_token, service_id):
    from api.services.ci import ticker

    assert schedules_service.seconds_until_next_due() is None
    created = _create(client, admin_token, service_id).get_json()["data"]
    _make_due(created["id"], datetime.now(UTC) + timedelta(seconds=3))
    due_in = schedules_service.seconds_until_next_due()
    assert 0 < due_in <= 3
    assert ticker._idle_wait(app) <= 3.1


def test_run_now_queues_a_build_without_moving_tonights_run(app, client, admin_token, service_id):
    created = _create(client, admin_token, service_id).get_json()["data"]
    tonight = _row(created["id"]).next_run_at
    response = client.post(
        f"/api/ci/services/{service_id}/schedules/{created['id']}/run",
        headers=auth_headers(admin_token),
    )
    assert response.status_code == 201
    build = response.get_json()["data"]
    assert build["triggerType"] == "schedule"
    assert build["schedule"]["name"] == "Nightly"
    row = _row(created["id"])
    assert row.next_run_at == tonight
    assert row.last_build_id == build["id"]
    assert AuditLog.query.filter_by(action="ci_schedule_run_now").count() == 1


def test_run_now_on_a_paused_service_says_why(client, admin_token, service_id):
    created = _create(client, admin_token, service_id).get_json()["data"]
    service = db.session.get(CiService, service_id)
    service.status = "paused"
    db.session.commit()
    response = client.post(
        f"/api/ci/services/{service_id}/schedules/{created['id']}/run",
        headers=auth_headers(admin_token),
    )
    assert response.status_code == 409
    assert "paused" in response.get_json()["error"]


# ---------------------------------------------------------------------------
# Migration
# ---------------------------------------------------------------------------

def test_an_existing_database_gains_the_table_on_migrate(app):
    from sqlalchemy import inspect

    from api.migrate_rbac import run_migrations

    CiSchedule.__table__.drop(db.engine)
    assert "ci_schedules" not in inspect(db.engine).get_table_names()
    run_migrations()
    assert "ci_schedules" in inspect(db.engine).get_table_names()
