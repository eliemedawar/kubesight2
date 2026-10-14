"""vCenters re-test themselves once a day: connection and provisioning account."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from api.db import db
from api.models import VSphereConnection
from api.secret_encryption import encrypt_secret
from api.services import vsphere_service

NOW = datetime(2026, 10, 14, 12, 0, tzinfo=timezone.utc)


@pytest.fixture()
def calls(app, monkeypatch):
    seen = []

    def connection(connection_id):
        seen.append(("connection", connection_id))
        row = db.session.get(VSphereConnection, connection_id)
        row.last_tested_at = NOW
        db.session.commit()
        return {"status": "ok"}

    def provisioning(connection_id):
        seen.append(("provisioning", connection_id))
        row = db.session.get(VSphereConnection, connection_id)
        row.provisioning_last_test_at = NOW
        db.session.commit()
        return {"status": "ok"}

    monkeypatch.setattr(vsphere_service, "test_connection", connection)
    monkeypatch.setattr(vsphere_service, "test_provisioning", provisioning)
    monkeypatch.delenv("VSPHERE_AUTO_TEST_HOURS", raising=False)
    return seen


def _vcenter(name, tested_ago=None, provisioning=False, active=True):
    row = VSphereConnection(
        name=name, base_url=f"https://{name}.example.test", username="ro@vsphere.local",
        password_cipher=encrypt_secret("ro-pass"), is_active=active,
        last_tested_at=NOW - tested_ago if tested_ago is not None else None,
    )
    if provisioning:
        row.provisioning_username = "prov@vsphere.local"
        row.provisioning_password_cipher = encrypt_secret("prov-pass")
        row.provisioning_last_test_at = NOW - (tested_ago or timedelta(days=2))
    db.session.add(row)
    db.session.commit()
    return row


def test_a_day_old_test_runs_again_with_the_provisioning_check(calls):
    row = _vcenter("dr", tested_ago=timedelta(hours=25), provisioning=True)
    assert vsphere_service.run_due_tests(NOW) == 2
    assert calls == [("connection", row.id), ("provisioning", row.id)]


def test_a_fresh_test_is_left_alone(calls):
    _vcenter("prod", tested_ago=timedelta(hours=3))
    assert vsphere_service.run_due_tests(NOW) == 0
    assert calls == []


def test_one_vcenter_per_tick(calls):
    first = _vcenter("a")
    second = _vcenter("b")
    vsphere_service.run_due_tests(NOW)
    assert calls == [("connection", first.id)]
    vsphere_service.run_due_tests(NOW)
    assert calls[-1] == ("connection", second.id)


def test_inactive_vcenters_are_skipped(calls):
    _vcenter("off", active=False)
    assert vsphere_service.run_due_tests(NOW) == 0


def test_zero_hours_turns_it_off(calls, monkeypatch):
    _vcenter("prod")
    monkeypatch.setenv("VSPHERE_AUTO_TEST_HOURS", "0")
    assert vsphere_service.run_due_tests(NOW) == 0


def test_an_unexpected_error_is_recorded_not_retried_every_tick(app, monkeypatch):
    def boom(connection_id):
        raise RuntimeError("socket closed")

    monkeypatch.setattr(vsphere_service, "test_connection", boom)
    row = _vcenter("flaky")
    assert vsphere_service.run_due_tests() == 1
    db.session.expire_all()
    row = db.session.get(VSphereConnection, row.id)
    assert row.last_connection_status == "failed"
    assert "socket closed" in row.last_connection_error
    assert vsphere_service.run_due_tests() == 0
