"""Alert delivery: retry backoff, the delivery pool, SMTP session reuse,
marker pruning and opt-in log retention."""

import smtplib
import threading
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest

from api.db import db
from api.models import (
    AlertDeliveryLog,
    AlertHistory,
    AlertPolicy,
    AlertRoutingDeliverySent,
    AlertRoutingReceiver,
)
from api.services import alert_routing_service as ars

T0 = datetime(2026, 10, 1, 12, 0, tzinfo=timezone.utc)


# ── pure backoff schedule ───────────────────────────────────────────────────


def test_next_delivery_at_schedule():
    nxt = ars.next_delivery_at
    assert nxt({}, 300) is None  # never attempted: due now
    assert nxt({"firing": T0}, 300) == T0 + timedelta(seconds=300)
    # First failure: retry after the 60 s base.
    assert nxt({"retry-first": T0, "retry-last": T0}, 300) == T0 + timedelta(seconds=60)
    # Streak of 60 s -> wait 60 s; 120 s -> 120 s; then doubling.
    t1 = T0 + timedelta(seconds=60)
    assert nxt({"retry-first": T0, "retry-last": t1}, 3600) == t1 + timedelta(seconds=60)
    t2 = T0 + timedelta(seconds=120)
    assert nxt({"retry-first": T0, "retry-last": t2}, 3600) == t2 + timedelta(seconds=120)
    t3 = T0 + timedelta(seconds=240)
    assert nxt({"retry-first": T0, "retry-last": t3}, 3600) == t3 + timedelta(seconds=240)
    # Capped at the repeat interval.
    t9 = T0 + timedelta(seconds=5000)
    assert nxt({"retry-first": T0, "retry-last": t9}, 300) == t9 + timedelta(seconds=300)
    # A success newer than the failures puts the pair back on normal cadence.
    ok = T0 + timedelta(seconds=500)
    assert nxt({"retry-first": T0, "retry-last": t3, "firing": ok}, 300) == ok + timedelta(
        seconds=300
    )


# ── dispatch: failures back off, a success resumes the cadence ──────────────


def _webhook_policy(interval=300):
    receiver = AlertRoutingReceiver(
        name="Hook", receiver_type="webhook", url="https://hooks.example.com/x", enabled=True
    )
    policy = AlertPolicy(
        name="Backoff",
        cluster_id="prod-us-east",
        severity="critical",
        enabled=True,
        conditions=[{"metricKey": "pod_restart_count", "operator": ">", "threshold": 1}],
        scope={"type": "cluster"},
        evaluation_interval_seconds=interval,
        notification_channels=[{"channel": "dashboard"}],
    )
    policy.notification_receivers = [receiver]
    db.session.add_all([receiver, policy])
    db.session.commit()
    alert = {
        "id": "history-backoff-1",
        "policyId": policy.id,
        "severity": "critical",
        "status": "firing",
        "clusterId": "prod-us-east",
        "title": "Backoff",
    }
    return policy, receiver, alert


def _age_markers(alert_id, seconds):
    for row in AlertRoutingDeliverySent.query.filter_by(alert_id=alert_id).all():
        row.sent_at = ars._aware(row.sent_at) - timedelta(seconds=seconds)
    db.session.commit()


def test_failed_receiver_backs_off_then_success_resumes_normal_cadence(app):
    policy, receiver, alert = _webhook_policy(interval=300)
    calls = []

    def dead(_receiver, _payload):
        calls.append(1)
        raise ars.EmailDeliveryError("Webhook request failed: timed out")

    with patch.object(ars, "_post_webhook", side_effect=dead):
        first = ars.dispatch_policy_alert_notifications(alert)
        assert first["sent"] == 0 and len(first["errors"]) == 1
        # The next ticks (15 s apart) do not hammer the dead receiver.
        assert ars.dispatch_policy_alert_notifications(alert)["skipped"] == 1
        assert len(calls) == 1
        markers = {
            row.alert_status
            for row in AlertRoutingDeliverySent.query.filter_by(alert_id=alert["id"])
        }
        assert markers == {"retry-first", "retry-last"}

        # 61 s later the retry is due again; it fails once more.
        _age_markers(alert["id"], 61)
        assert len(ars.dispatch_policy_alert_notifications(alert)["errors"]) == 1
        assert len(calls) == 2
        assert ars.dispatch_policy_alert_notifications(alert)["skipped"] == 1

    # Receiver recovers: next due retry succeeds, retry markers are cleared.
    _age_markers(alert["id"], 200)
    with patch.object(ars, "_post_webhook") as alive:
        assert ars.dispatch_policy_alert_notifications(alert)["sent"] == 1
        assert alive.call_count == 1
        markers = {
            row.alert_status
            for row in AlertRoutingDeliverySent.query.filter_by(alert_id=alert["id"])
        }
        assert markers == {"firing"}
        # Back on the normal cadence: nothing again inside the repeat interval...
        _age_markers(alert["id"], 120)
        assert ars.dispatch_policy_alert_notifications(alert)["skipped"] == 1
        # ...and a repeat once the interval has passed.
        _age_markers(alert["id"], 200)
        assert ars.dispatch_policy_alert_notifications(alert)["sent"] == 1

    statuses = [
        row.status
        for row in AlertDeliveryLog.query.filter_by(alert_id=alert["id"]).order_by(
            AlertDeliveryLog.id
        )
    ]
    assert statuses == ["failed", "failed", "success", "success"]


# ── the delivery pool ───────────────────────────────────────────────────────


@pytest.fixture()
def async_delivery(monkeypatch):
    monkeypatch.setattr(ars, "_FORCE_ASYNC", True)
    yield
    assert ars.wait_for_pending_deliveries(10)


def test_sweep_queues_deliveries_and_the_worker_records_them(app, async_delivery):
    policy, receiver, alert = _webhook_policy()
    release = threading.Event()
    calls = []

    def slow(_receiver, _payload):
        calls.append(1)
        release.wait(5)

    with patch.object(ars, "_post_webhook", side_effect=slow):
        summary = ars.dispatch_policy_alert_notifications(alert)
        assert summary["queued"] == 1 and summary["sent"] == 0
        # Still in flight: the next tick does not queue it twice.
        assert ars.dispatch_policy_alert_notifications(alert)["skipped"] == 1
        release.set()
        assert ars.wait_for_pending_deliveries(10)
    assert len(calls) == 1
    db.session.expire_all()
    log = AlertDeliveryLog.query.filter_by(alert_id=alert["id"]).one()
    assert log.status == "success" and log.receiver_name == "Hook"
    assert AlertRoutingDeliverySent.query.filter_by(alert_id=alert["id"]).one().alert_status == "firing"


def test_sweep_resolves_destinations_and_markers_once(app):
    from api import alert_notifier

    policy, receiver, _alert = _webhook_policy()
    for index in range(3):
        db.session.add(
            AlertHistory(
                alert_key=f"policy-{policy.id}:k{index}",
                policy_id=policy.id,
                policy_name=policy.name,
                cluster_id="prod-us-east",
                resource_type="cluster",
                severity="critical",
                status="active",
                title="Backoff",
                fired_at=datetime.now(timezone.utc),
            )
        )
    db.session.commit()
    real_dest, real_markers = ars.destinations_for_policies, ars.load_delivery_markers
    with patch.object(alert_notifier, "_get_notification_settings", return_value={"alerts": True}), patch.object(
        alert_notifier, "destinations_for_policies", side_effect=real_dest
    ) as dest, patch.object(
        alert_notifier, "load_delivery_markers", side_effect=real_markers
    ) as markers, patch.object(ars, "_post_webhook") as post:
        summary = alert_notifier.dispatch_active_alert_notifications()
    assert summary["sent"] == 3 and post.call_count == 3
    assert dest.call_count == 1 and markers.call_count == 1


# ── SMTP: one session per receiver ──────────────────────────────────────────


class _FakeSMTP:
    instances = []
    fail_connect = False

    def __init__(self, host, port, timeout=None):
        if _FakeSMTP.fail_connect:
            raise OSError("connection refused")
        self.logins = 0
        self.sent = []
        _FakeSMTP.instances.append(self)

    def ehlo(self):
        pass

    def starttls(self, context=None):
        pass

    def login(self, user, password):
        self.logins += 1

    def send_message(self, message):
        self.sent.append(message["To"])

    def quit(self):
        pass

    def close(self):
        pass


_SETTINGS = {
    "host": "smtp.test",
    "port": 587,
    "user": "u",
    "password": "p",
    "from_addr": "kubesight@test",
    "use_tls": True,
    "use_ssl": False,
}


def test_email_batch_uses_one_connection_and_one_login(app, monkeypatch):
    from api import email_delivery

    _FakeSMTP.instances, _FakeSMTP.fail_connect = [], False
    monkeypatch.setattr(email_delivery.smtplib, "SMTP", _FakeSMTP)
    monkeypatch.setattr(email_delivery, "smtp_is_configured", lambda: True)
    monkeypatch.setattr(email_delivery, "_smtp_settings", lambda: dict(_SETTINGS))

    errors = email_delivery.send_alert_emails(
        ["a@x.io", "b@x.io", "c@x.io"], {"title": "t", "severity": "warning", "status": "firing"}
    )
    assert errors == {}
    assert len(_FakeSMTP.instances) == 1
    assert _FakeSMTP.instances[0].logins == 1
    assert _FakeSMTP.instances[0].sent == ["a@x.io", "b@x.io", "c@x.io"]

    # The single-message API still works on its own connection.
    email_delivery.send_email("d@x.io", "s", "b")
    assert len(_FakeSMTP.instances) == 2


def test_email_batch_unreachable_server_fails_fast(app, monkeypatch):
    from api import email_delivery

    attempts = []

    class Refusing(_FakeSMTP):
        def __init__(self, *args, **kwargs):
            attempts.append(1)
            raise OSError("connection refused")

    monkeypatch.setattr(email_delivery.smtplib, "SMTP", Refusing)
    monkeypatch.setattr(email_delivery, "smtp_is_configured", lambda: True)
    monkeypatch.setattr(email_delivery, "_smtp_settings", lambda: dict(_SETTINGS))
    errors = email_delivery.send_alert_emails(["a@x.io", "b@x.io"], {"title": "t"})
    assert set(errors) == {"a@x.io", "b@x.io"}
    assert "Could not reach SMTP server" in errors["a@x.io"]
    assert len(attempts) == 1  # the second address did not wait on another connect


def test_smtp_session_reconnects_once_after_disconnect(app, monkeypatch):
    from email.message import EmailMessage

    from api import email_delivery

    class Flaky(_FakeSMTP):
        def send_message(self, message):
            if len(_FakeSMTP.instances) == 1 and not self.sent:
                self.sent.append("dropped")
                raise smtplib.SMTPServerDisconnected("gone")
            super().send_message(message)

    _FakeSMTP.instances, _FakeSMTP.fail_connect = [], False
    monkeypatch.setattr(email_delivery.smtplib, "SMTP", Flaky)
    msg = EmailMessage()
    msg["To"] = "a@x.io"
    with email_delivery.SmtpSession(dict(_SETTINGS)) as session:
        session.send_message(msg)
    assert len(_FakeSMTP.instances) == 2
    assert _FakeSMTP.instances[1].sent == ["a@x.io"]


# ── pruning and retention ───────────────────────────────────────────────────


def test_prune_markers_only_deletes_stale(app):
    db.session.add_all(
        [
            AlertRoutingDeliverySent(alert_id="history-1", receiver_id=1, alert_status="firing"),
            AlertRoutingDeliverySent(alert_id="history-2", receiver_id=1, alert_status="retry-last"),
        ]
    )
    db.session.commit()
    with patch.object(db.session, "commit", wraps=db.session.commit) as commit:
        assert ars.prune_delivery_markers(["history-1", "history-2"]) == 0
        commit.assert_not_called()
    assert ars.prune_delivery_markers(["history-1"]) == 1
    assert {row.alert_id for row in AlertRoutingDeliverySent.query} == {"history-1"}
    assert ars.prune_delivery_markers([]) == 1


def test_delivery_log_retention_is_opt_in(app, monkeypatch):
    old = datetime.now(timezone.utc) - timedelta(days=40)
    db.session.add_all(
        [
            AlertDeliveryLog(alert_id="a", status="success", delivered_at=old),
            AlertDeliveryLog(alert_id="b", status="failed"),
        ]
    )
    db.session.commit()
    monkeypatch.delenv("ALERT_DELIVERY_LOG_RETENTION_DAYS", raising=False)
    assert ars.prune_delivery_logs() == 0
    monkeypatch.setenv("ALERT_DELIVERY_LOG_RETENTION_DAYS", "nonsense")
    assert ars.prune_delivery_logs() == 0
    monkeypatch.setenv("ALERT_DELIVERY_LOG_RETENTION_DAYS", "30")
    assert ars.prune_delivery_logs() == 1
    assert [row.alert_id for row in AlertDeliveryLog.query] == ["b"]
