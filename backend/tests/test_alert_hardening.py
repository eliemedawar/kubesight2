"""Production hardening for alerts & monitoring.

Covers: a read-only alert list API (delivery moved to the scheduler), real
disk / PVC usage from the kubelet Summary API, log-alert auto-resolve, and
receiver severity/namespace/cluster filters.
"""

import json
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest

from api import k8s_volume_stats
from api.cluster_access import ClusterAccess
from api.db import db
from api.k8s_provider import K8sCommandError
from api.models import AlertHistory, AlertPolicy, AlertRoutingDeliverySent, AlertRoutingReceiver
from tests.conftest import auth_headers

ACCESS = ClusterAccess(cluster_id="real-1", context_name="ctx-real", kubeconfig_path=None)


@pytest.fixture(autouse=True)
def _fresh_volume_cache():
    k8s_volume_stats.clear_cache()
    yield
    k8s_volume_stats.clear_cache()


def _node(name, ready=True):
    return {
        "metadata": {"name": name},
        "status": {"conditions": [{"type": "Ready", "status": "True" if ready else "False"}]},
    }


def _summary(node_used, node_cap, volumes=()):
    return {
        "node": {"fs": {"usedBytes": node_used, "capacityBytes": node_cap}},
        "pods": [
            {
                "podRef": {"name": pod, "namespace": ns},
                "volume": [
                    {
                        "name": "data",
                        "usedBytes": used,
                        "capacityBytes": cap,
                        "pvcRef": {"name": pvc, "namespace": ns},
                    },
                    # Non-PVC volumes (emptyDir, configMap...) are ignored.
                    {"name": "tmp", "usedBytes": 1, "capacityBytes": 2},
                ],
            }
            for (pod, ns, pvc, used, cap) in volumes
        ],
    }


def _fake_kubectl(nodes, summaries, pvcs=None, fail_nodes=()):
    """Route kubectl args to canned outputs; summaries = {node: dict}."""

    def run(access, args, timeout=None):
        if args[:2] == ["get", "--raw"]:
            node = args[2].split("/")[4]
            if node in fail_nodes:
                raise K8sCommandError('nodes "x" is forbidden: cannot get resource "nodes/proxy"')
            return json.dumps(summaries[node])
        if args[:2] == ["get", "nodes"]:
            return json.dumps({"items": nodes})
        if args[:2] == ["get", "pvc"]:
            return json.dumps({"items": pvcs or []})
        raise AssertionError(f"unexpected kubectl call {args}")

    return run


# ── kubelet Summary API parsing ───────────────────────────────────────────────


def test_parse_summary_extracts_node_fs_and_pvcs():
    parsed = k8s_volume_stats.parse_summary(
        "n1", _summary(50, 200, [("db-0", "data", "pgdata", 30, 100)])
    )
    assert parsed["nodeFs"]["percent"] == 25.0
    usage = parsed["pvcs"][("data", "pgdata")]
    assert usage["percent"] == 30.0
    assert usage["usedBytes"] == 30 and usage["capacityBytes"] == 100


def test_fetch_cluster_volume_stats_skips_not_ready_nodes_and_keeps_partial_data():
    nodes = [_node("n1"), _node("n2"), _node("down", ready=False)]
    summaries = {"n1": _summary(10, 100, [("a", "ns", "claim", 90, 100)])}
    calls = []
    fake = _fake_kubectl(nodes, summaries, fail_nodes=("n2",))

    def spy(access, args, timeout=None):
        calls.append(args)
        return fake(access, args, timeout)

    with patch("api.k8s_volume_stats._run_for_access", side_effect=spy):
        stats = k8s_volume_stats.fetch_cluster_volume_stats(ACCESS)
        # Second call inside the TTL is served from the per-cluster cache.
        k8s_volume_stats.fetch_cluster_volume_stats(ACCESS)

    assert stats["available"] is True
    assert stats["nodes"] == {"n1": {"usedBytes": 10, "capacityBytes": 100, "percent": 10.0}}
    assert "n2" in stats["errors"] and "n2" not in stats["nodes"]
    assert stats["pvcs"][("ns", "claim")]["percent"] == 90.0
    raw_calls = [c for c in calls if c[:2] == ["get", "--raw"]]
    assert len(raw_calls) == 2  # n1 + n2 once each; NotReady node never probed
    assert not any("/down/" in c[2] for c in raw_calls)


def test_fetch_cluster_volume_stats_unavailable_when_no_node_answers():
    with patch(
        "api.k8s_volume_stats._run_for_access",
        side_effect=_fake_kubectl([_node("n1")], {}, fail_nodes=("n1",)),
    ):
        stats = k8s_volume_stats.fetch_cluster_volume_stats(ACCESS)
    assert stats["available"] is False
    assert "nodes/proxy" in stats["reason"]


# ── disk / PVC observations ───────────────────────────────────────────────────


def _observe(metric, fake, target=None):
    from api.services.alert_policy_evaluator import _collect_real_observations

    gaps = {}
    with patch("api.services.alert_policy_evaluator._run_for_access", side_effect=fake), patch(
        "api.k8s_volume_stats._run_for_access", side_effect=fake
    ):
        obs = _collect_real_observations(
            ACCESS,
            "real-1",
            target or {"namespace": None, "resourceType": "cluster", "resourceName": None},
            [metric],
            gaps,
        )
    return obs, gaps


def test_disk_usage_uses_real_node_fs_percent():
    fake = _fake_kubectl([_node("n1"), _node("n2")], {"n1": _summary(92, 100), "n2": _summary(10, 100)})
    obs, gaps = _observe("disk_usage_percent", fake)
    by_node = {o["resourceName"]: o["value"] for o in obs if o["metricKey"] == "disk_usage_percent"}
    assert by_node == {"n1": 92.0, "n2": 10.0}
    assert gaps == {}


def test_disk_usage_no_data_when_summary_api_forbidden():
    fake = _fake_kubectl([_node("n1")], {}, fail_nodes=("n1",))
    obs, gaps = _observe("disk_usage_percent", fake)
    assert [o for o in obs if o["metricKey"] == "disk_usage_percent"] == []
    assert "disk_usage_percent" in gaps


def test_pvc_usage_real_values_and_unknown_is_not_zero():
    pvcs = [
        {"metadata": {"name": "pgdata", "namespace": "data"}, "status": {"phase": "Bound", "capacity": {"storage": "10Gi"}}},
        {"metadata": {"name": "unmounted", "namespace": "data"}, "status": {"phase": "Bound", "capacity": {"storage": "5Gi"}}},
        {"metadata": {"name": "waiting", "namespace": "data"}, "status": {"phase": "Pending"}},
    ]
    fake = _fake_kubectl(
        [_node("n1")], {"n1": _summary(1, 10, [("db-0", "data", "pgdata", 87, 100)])}, pvcs=pvcs
    )
    obs, gaps = _observe("pvc_usage_percent", fake)
    values = {o["resourceName"]: o["value"] for o in obs}
    # Only the mounted claim is measured; the unmounted and Pending claims have
    # no observation at all (previously 50.0 and 85.0 were invented).
    assert values == {"pgdata": 87.0}
    assert gaps == {}


def _real_mode_policy(metric, threshold=80):
    policy = AlertPolicy(
        name=f"{metric} policy",
        cluster_id="real-1",
        severity="warning",
        enabled=True,
        condition_logic="any",
        conditions=[{"metricKey": metric, "operator": ">", "threshold": threshold}],
        scope={"type": "cluster"},
    )
    db.session.add(policy)
    db.session.commit()
    return policy


def _evaluate_real(fake):
    from api.services.alert_policy_evaluator import evaluate_policies_for_cluster

    with patch("api.services.alert_policy_evaluator.should_use_real_k8s", return_value=True), patch(
        "api.services.alert_policy_evaluator.resolve_cluster_access", return_value=ACCESS
    ), patch("api.services.alert_policy_evaluator._run_for_access", side_effect=fake), patch(
        "api.k8s_volume_stats._run_for_access", side_effect=fake
    ), patch("api.alert_notifier.dispatch_policy_alert_notifications", return_value={}):
        return evaluate_policies_for_cluster("real-1", persist=True)


def test_disk_policy_fires_on_real_usage(app):
    policy = _real_mode_policy("disk_usage_percent")
    fake = _fake_kubectl([_node("n1")], {"n1": _summary(95, 100)})
    active = _evaluate_real(fake)
    assert len(active) == 1
    assert active[0].resource_name == "n1"
    db.session.refresh(policy)
    assert policy.last_evaluation_result == "met"


def test_disk_policy_records_no_data_and_does_not_fire_or_resolve(app):
    policy = _real_mode_policy("disk_usage_percent")
    fake_ok = _fake_kubectl([_node("n1")], {"n1": _summary(95, 100)})
    assert len(_evaluate_real(fake_ok)) == 1

    policy.last_evaluated_at = None
    db.session.commit()
    fake_forbidden = _fake_kubectl([_node("n1")], {}, fail_nodes=("n1",))
    k8s_volume_stats.clear_cache()
    _evaluate_real(fake_forbidden)

    db.session.refresh(policy)
    assert policy.last_evaluation_result == "no_data"
    assert "Disk Usage" in (policy.last_evaluation_error or "")
    # Unknown is not "recovered": the active alert stays active.
    row = AlertHistory.query.filter_by(policy_id=policy.id).one()
    assert row.status == "active"


def test_cluster_cpu_policy_reads_utilization_tuple(app):
    """cluster_utilization_metrics returns (cpu, mem); cluster CPU policies used
    to call .get() on the tuple and silently never fire."""
    from api.services.alert_policy_evaluator import _collect_cluster_cpu_memory

    with patch(
        "api.k8s_metrics.cluster_utilization_metrics",
        return_value=({"available": True, "percent": 91.5}, {"available": False}),
    ):
        assert _collect_cluster_cpu_memory(ACCESS, None) == (91.5, None)


# ── GET /api/alerts is read-only ──────────────────────────────────────────────


def _policy_with_receiver(**receiver_kwargs):
    receiver = AlertRoutingReceiver(
        name=receiver_kwargs.pop("name", "Ops"),
        receiver_type="email",
        email_address="ops@example.com",
        enabled=True,
        **receiver_kwargs,
    )
    policy = AlertPolicy(
        name="Crashy",
        cluster_id="prod-us-east",
        severity="critical",
        enabled=True,
        conditions=[{"metricKey": "pod_restart_count", "operator": ">", "threshold": 1}],
        scope={"type": "cluster"},
        evaluation_interval_seconds=60,
        notification_channels=[{"channel": "dashboard"}],
    )
    policy.notification_receivers = [receiver]
    db.session.add_all([receiver, policy])
    db.session.commit()
    row = AlertHistory(
        alert_key=f"policy-{policy.id}:prod-us-east:*:cluster:*",
        policy_id=policy.id,
        policy_name=policy.name,
        cluster_id="prod-us-east",
        namespace="payments",
        resource_type="cluster",
        severity="critical",
        status="active",
        title="Crashy triggered",
        fired_at=datetime.now(timezone.utc),
        # Already evaluated just now so the GET would not be due anyway.
    )
    policy.last_evaluated_at = datetime.now(timezone.utc)
    db.session.add(row)
    db.session.commit()
    return policy, receiver, row


@patch("api.services.alert_routing_service.send_alert_email")
@patch("api.services.alert_routing_service.smtp_is_configured", return_value=True)
def test_get_alerts_sends_nothing_and_deletes_nothing(_smtp, send_email, client, admin_token):
    policy, receiver, row = _policy_with_receiver()
    db.session.add(
        AlertRoutingDeliverySent(alert_id="history-stale", receiver_id=receiver.id, alert_status="firing")
    )
    db.session.commit()

    with patch("api.services.alert_policy_evaluator.evaluate_policies_for_cluster") as evaluate:
        for url in ("/api/alerts", "/api/alerts?cluster=prod-us-east", "/api/alert-policies?cluster=prod-us-east"):
            response = client.get(url, headers=auth_headers(admin_token))
            assert response.status_code == 200, url
        evaluate.assert_not_called()

    send_email.assert_not_called()
    assert AlertRoutingDeliverySent.query.filter_by(alert_id="history-stale").count() == 1
    payload = client.get("/api/alerts?cluster=prod-us-east", headers=auth_headers(admin_token)).get_json()
    status = payload["data"]["metadata"]["emailDelivery"]
    assert status["deliveredBy"] == "scheduler"
    assert "sent" not in status
    assert any(item["id"] == f"history-{row.id}" for item in payload["data"]["items"])


@patch("api.services.alert_routing_service.send_alert_email")
@patch("api.services.alert_routing_service.smtp_is_configured", return_value=True)
def test_scheduler_sweep_delivers_repeats_and_prunes_markers(_smtp, send_email, app):
    from api.alert_notifier import dispatch_active_alert_notifications

    policy, receiver, row = _policy_with_receiver()
    db.session.add(
        AlertRoutingDeliverySent(alert_id="history-stale", receiver_id=receiver.id, alert_status="firing")
    )
    db.session.commit()

    first = dispatch_active_alert_notifications()
    assert first["sent"] == 1
    assert send_email.call_count == 1
    # Within the repeat interval nothing is re-sent.
    second = dispatch_active_alert_notifications()
    assert second["sent"] == 0 and second["skipped"] == 1
    assert AlertRoutingDeliverySent.query.filter_by(alert_id="history-stale").count() == 0
    assert AlertRoutingDeliverySent.query.filter_by(alert_id=f"history-{row.id}").count() == 1

    # Disabled policies are not delivered.
    policy.enabled = False
    db.session.commit()
    from api.models import AlertDeliveryLog

    AlertDeliveryLog.query.delete()
    db.session.commit()
    assert dispatch_active_alert_notifications()["sent"] == 0


# ── receiver filters ──────────────────────────────────────────────────────────


def _receiver(**kwargs):
    return AlertRoutingReceiver(name="r", receiver_type="email", email_address="a@b.c", **kwargs)


@pytest.mark.parametrize(
    "kwargs,alert,expected",
    [
        ({}, {"severity": "info"}, True),
        ({"severity_filter": []}, {"severity": "info"}, True),
        ({"severity_filter": ["critical"]}, {"severity": "critical"}, True),
        ({"severity_filter": ["critical"]}, {"severity": "warning"}, False),
        ({"severity_filter": ["warning", "critical"]}, {"severity": "WARNING"}, True),
        ({"namespace_filter": "payments, prod-*"}, {"namespace": "prod-eu"}, True),
        ({"namespace_filter": "payments, prod-*"}, {"namespace": "staging"}, False),
        ({"namespace_filter": "payments"}, {"namespace": None}, False),
        ({"cluster_filter": "prod-*"}, {"clusterId": "prod-us-east"}, True),
        ({"cluster_filter": "prod-*"}, {"clusterId": "dev-1"}, False),
        ({"cluster_filter": "prod-*"}, {"clusterId": "custom-9", "clusterName": "prod-west"}, True),
        (
            {"severity_filter": ["critical"], "namespace_filter": "payments", "cluster_filter": "prod-*"},
            {"severity": "critical", "namespace": "payments", "clusterId": "prod-us-east"},
            True,
        ),
    ],
)
def test_receiver_matches_alert(app, kwargs, alert, expected):
    from api.services.alert_routing_service import receiver_matches_alert

    assert receiver_matches_alert(_receiver(**kwargs), alert) is expected


@patch("api.services.alert_routing_service.send_alert_email")
@patch("api.services.alert_routing_service.smtp_is_configured", return_value=True)
def test_dispatch_skips_receivers_whose_filters_do_not_match(_smtp, send_email, app):
    from api.services.alert_routing_service import dispatch_policy_alert_notifications

    policy, receiver, row = _policy_with_receiver(severity_filter=["warning"])
    alert = {
        "id": f"history-{row.id}",
        "policyId": policy.id,
        "severity": "critical",
        "status": "firing",
        "clusterId": "prod-us-east",
        "namespace": "payments",
    }
    summary = dispatch_policy_alert_notifications(alert)
    assert summary["sent"] == 0 and summary["filtered"] == 1
    send_email.assert_not_called()

    receiver.severity_filter = ["warning", "critical"]
    receiver.namespace_filter = "pay*"
    db.session.commit()
    assert dispatch_policy_alert_notifications(alert)["sent"] == 1


def test_receiver_filters_api_roundtrip_and_validation(client, admin_token):
    created = client.post(
        "/api/alert-routing/receivers",
        headers=auth_headers(admin_token),
        json={
            "name": "Filtered",
            "type": "email",
            "emailAddress": "f@example.com",
            "severityFilter": ["critical", "warning", "critical"],
            "namespaceFilter": " payments ,prod-* ",
            "clusterFilter": "prod-*",
        },
    )
    assert created.status_code == 201
    data = created.get_json()["data"]
    assert data["severityFilter"] == ["warning", "critical"]
    assert data["namespaceFilter"] == "payments, prod-*"
    assert data["clusterFilter"] == "prod-*"

    updated = client.put(
        f"/api/alert-routing/receivers/{data['id']}",
        headers=auth_headers(admin_token),
        json={"severityFilter": [], "namespaceFilter": ""},
    )
    assert updated.status_code == 200
    body = updated.get_json()["data"]
    assert body["severityFilter"] == [] and body["namespaceFilter"] == ""
    assert body["clusterFilter"] == "prod-*"

    bad = client.put(
        f"/api/alert-routing/receivers/{data['id']}",
        headers=auth_headers(admin_token),
        json={"severityFilter": ["urgent"]},
    )
    assert bad.status_code == 400


# ── log alerts auto-resolve ───────────────────────────────────────────────────

LOG_POLICY = {
    "name": "Backend Error Logs",
    "clusterId": "prod-us-east",
    "enabled": True,
    "alertType": "log",
    "severity": "critical",
    "logConfig": {"matchType": "contains", "pattern": "ERROR", "logWindowSeconds": 60},
    "scope": {"type": "deployment", "namespace": "default", "resourceName": "kubesight-backend"},
    "showOnDashboard": True,
}


def _create_log_policy(client, admin_token, **log_overrides):
    payload = {**LOG_POLICY, "logConfig": {**LOG_POLICY["logConfig"], **log_overrides}}
    response = client.post("/api/alert-policies", headers=auth_headers(admin_token), json=payload)
    assert response.status_code in (200, 201), response.get_json()
    return AlertPolicy.query.get(response.get_json()["data"]["id"])


def _reevaluate(policy):
    from api.services.alert_policy_evaluator import evaluate_policies_for_cluster

    policy.last_evaluated_at = None
    db.session.commit()
    evaluate_policies_for_cluster("prod-us-east", persist=True)


def test_log_alert_resolves_after_quiet_period(client, admin_token):
    policy = _create_log_policy(client, admin_token)
    _reevaluate(policy)
    active = AlertHistory.query.filter_by(policy_id=policy.id, status="active").all()
    assert active

    with patch("api.services.log_alert_evaluator._mock_log_matches", return_value=[]):
        # Quiet, but not for long enough yet: stays active.
        _reevaluate(policy)
        assert AlertHistory.query.filter_by(policy_id=policy.id, status="active").count() == len(active)

        # Age the last match beyond the quiet period (default: max(window, interval)).
        for row in active:
            snap = dict(row.log_snapshot)
            snap["lastMatchAt"] = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
            row.log_snapshot = snap
        db.session.commit()
        _reevaluate(policy)

    rows = AlertHistory.query.filter_by(policy_id=policy.id).all()
    assert rows and all(r.status == "resolved" for r in rows)
    assert all(r.resolved_at is not None for r in rows)


def test_log_alert_stays_active_while_logs_unreadable(app):
    from api.services.log_alert_evaluator import evaluate_log_policy

    policy = AlertPolicy(
        name="real log",
        cluster_id="real-1",
        alert_type="log",
        severity="warning",
        enabled=True,
        log_config={"matchType": "contains", "pattern": "ERROR", "logWindowSeconds": 60},
        scope={"type": "deployment", "namespace": "default", "resourceName": "api"},
    )
    db.session.add(policy)
    db.session.commit()
    old = datetime.now(timezone.utc) - timedelta(hours=2)
    row = AlertHistory(
        alert_key="k1",
        policy_id=policy.id,
        policy_name=policy.name,
        cluster_id="real-1",
        namespace="default",
        resource_type="deployment",
        resource_name="api",
        alert_type="log",
        severity="warning",
        status="active",
        title="Error detected in logs",
        log_snapshot={"podName": "api-1", "containerName": "app", "lastMatchAt": old.isoformat()},
        fired_at=old,
    )
    db.session.add(row)
    db.session.commit()

    pod = {"metadata": {"name": "api-1", "namespace": "default"}, "spec": {"containers": [{"name": "app"}]}}

    # Pod list unavailable -> nothing resolves.
    with patch(
        "api.services.alert_policy_evaluator._list_pods_for_scope",
        side_effect=K8sCommandError("connection refused"),
    ):
        evaluate_log_policy(policy, "real-1", ACCESS, persist=True)
    assert row.status == "active"

    # Pod listed but its logs cannot be read -> unknown, stays active.
    with patch("api.services.alert_policy_evaluator._list_pods_for_scope", return_value=[pod]), patch(
        "api.k8s_logs._run_for_access", side_effect=K8sCommandError("timeout")
    ):
        evaluate_log_policy(policy, "real-1", ACCESS, persist=True)
    assert row.status == "active"

    # Logs readable and quiet for longer than the quiet period -> resolved.
    with patch("api.services.alert_policy_evaluator._list_pods_for_scope", return_value=[pod]), patch(
        "api.k8s_logs._run_for_access", return_value="2026-09-26T10:00:00Z all good\n"
    ):
        evaluate_log_policy(policy, "real-1", ACCESS, persist=True)
    assert row.status == "resolved"
    assert row.resolved_at is not None


def test_log_resolve_after_seconds_config(app):
    from api.alert_policy_catalog import normalize_log_config
    from api.services.log_alert_evaluator import log_resolve_after_seconds

    policy = AlertPolicy(name="p", cluster_id="c", evaluation_interval_seconds=300)
    assert log_resolve_after_seconds(policy, normalize_log_config({"pattern": "x", "logWindowSeconds": 60})) == 300
    assert (
        log_resolve_after_seconds(policy, normalize_log_config({"pattern": "x", "resolveAfterSeconds": 900}))
        == 900
    )
    assert normalize_log_config({"pattern": "x", "resolveAfterSeconds": 5})["resolveAfterSeconds"] == 60
