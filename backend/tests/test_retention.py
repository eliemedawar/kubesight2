"""Opt-in retention: nothing is deleted unless its *_RETENTION_DAYS is set."""

from __future__ import annotations

import sys
import types
from datetime import datetime, timedelta, timezone

import pytest

from api.db import db
from api.models import AuditLog
from api.services import audit_service, retention


@pytest.fixture(autouse=True)
def _no_retention_env(monkeypatch):
    for env_var, _, _ in retention.RETENTION_JOBS:
        monkeypatch.delenv(env_var, raising=False)
    monkeypatch.setattr(retention, "_last_run", 0.0)


def _entries(app):
    now = datetime.now(timezone.utc)
    with app.app_context():
        db.session.add_all(
            [
                AuditLog(action="old_probe", created_at=now - timedelta(days=40)),
                AuditLog(action="old_probe", created_at=now - timedelta(days=31)),
                AuditLog(action="new_probe", created_at=now - timedelta(days=2)),
            ]
        )
        db.session.commit()


def test_nothing_is_deleted_by_default(app):
    _entries(app)
    with app.app_context():
        assert retention.run_due_retention(force=True) == {}
        assert audit_service.purge_old_audit_logs() == 0
        assert AuditLog.query.filter_by(action="old_probe").count() == 2


def test_audit_retention_deletes_only_older_entries(app, monkeypatch):
    _entries(app)
    monkeypatch.setenv("AUDIT_LOG_RETENTION_DAYS", "30")
    with app.app_context():
        result = retention.run_due_retention()
        assert result == {"AUDIT_LOG_RETENTION_DAYS": 2}
        assert AuditLog.query.filter_by(action="old_probe").count() == 0
        assert AuditLog.query.filter_by(action="new_probe").count() == 1
        # At most once an hour.
        assert retention.run_due_retention() == {}


def test_purge_works_in_batches(app, monkeypatch):
    monkeypatch.setattr(audit_service, "_PURGE_BATCH", 2)
    now = datetime.now(timezone.utc)
    with app.app_context():
        db.session.add_all(
            [AuditLog(action="batch_probe", created_at=now - timedelta(days=90)) for _ in range(5)]
        )
        db.session.commit()
        assert audit_service.purge_old_audit_logs(days=30) == 5
        assert AuditLog.query.filter_by(action="batch_probe").count() == 0


def test_other_features_jobs_run_only_when_present_and_switched_on(app, monkeypatch):
    calls = []
    fake = types.ModuleType("api.services.zz_fake_retention")
    fake.prune = lambda: calls.append("prune") or 3
    monkeypatch.setitem(sys.modules, "api.services.zz_fake_retention", fake)
    monkeypatch.setattr(
        retention,
        "RETENTION_JOBS",
        (
            ("ZZ_FAKE_RETENTION_DAYS", "api.services.zz_fake_retention", "prune"),
            ("ZZ_MISSING_RETENTION_DAYS", "api.services.zz_not_there", "prune"),
            ("ZZ_BROKEN_RETENTION_DAYS", "api.services.zz_fake_retention", "absent"),
        ),
    )
    with app.app_context():
        assert retention.run_due_retention(force=True) == {}
        monkeypatch.setenv("ZZ_FAKE_RETENTION_DAYS", "7")
        monkeypatch.setenv("ZZ_MISSING_RETENTION_DAYS", "7")
        monkeypatch.setenv("ZZ_BROKEN_RETENTION_DAYS", "7")
        assert retention.run_due_retention(force=True) == {"ZZ_FAKE_RETENTION_DAYS": 3}
        assert calls == ["prune"]


def test_a_failing_job_does_not_stop_the_others(app, monkeypatch):
    fake = types.ModuleType("api.services.zz_fake_retention2")

    def boom():
        raise RuntimeError("disk full")

    fake.boom = boom
    fake.ok = lambda: 1
    monkeypatch.setitem(sys.modules, "api.services.zz_fake_retention2", fake)
    monkeypatch.setattr(
        retention,
        "RETENTION_JOBS",
        (
            ("ZZ_A_DAYS", "api.services.zz_fake_retention2", "boom"),
            ("ZZ_B_DAYS", "api.services.zz_fake_retention2", "ok"),
        ),
    )
    monkeypatch.setenv("ZZ_A_DAYS", "1")
    monkeypatch.setenv("ZZ_B_DAYS", "1")
    with app.app_context():
        assert retention.run_due_retention(force=True) == {"ZZ_B_DAYS": 1}
