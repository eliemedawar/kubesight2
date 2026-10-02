"""Node disk, pressure and triage data on the dashboard summary."""

from unittest.mock import patch

from api import dashboard_k8s_snapshot
from api.cluster_access import ClusterAccess
from api.k8s_provider import build_node_health
from api.services.dashboard_service import _disk_usage, _top_alerts
from tests.conftest import auth_headers

GIB = 1024 ** 3


def _node(name, ready=True, pressures=(), cordoned=False, disk="100Gi"):
    conditions = [{"type": "Ready", "status": "True" if ready else "False"}]
    conditions += [{"type": p, "status": "True"} for p in pressures]
    # A pressure condition reported as False must not count.
    conditions.append({"type": "PIDPressure", "status": "False"})
    return {
        "metadata": {"name": name, "labels": {"node-role.kubernetes.io/worker": ""}},
        "spec": {"unschedulable": cordoned},
        "status": {
            "conditions": conditions,
            "capacity": {"cpu": "4", "memory": "8192Mi", "ephemeral-storage": disk, "pods": "110"},
        },
    }


def test_measured_disk_sets_usage_and_warns_at_85_percent():
    rows = build_node_health(
        [_node("n1")],
        {"n1": {"cpu": 1.0, "mem_mib": 2048}},
        fs_by_name={"n1": {"usedBytes": 88 * GIB, "capacityBytes": 100 * GIB}},
    )
    row = rows[0]
    assert row["diskUsedBytes"] == 88 * GIB
    assert row["diskTotalBytes"] == 100 * GIB
    assert row["diskPercent"] == 88.0
    assert row["issues"] == ["Disk 88%"]
    assert row["status"] == "warning"


def test_unmeasured_disk_keeps_capacity_and_never_reports_zero_usage():
    rows = build_node_health([_node("n1", disk="104857600Ki")], {})
    row = rows[0]
    assert row["diskUsedBytes"] is None
    assert row["diskPercent"] is None
    assert row["diskTotalBytes"] == 100 * GIB
    assert row["status"] == "healthy"


def test_pressure_is_critical_and_cordon_and_pod_count_are_reported():
    pods = [
        {"spec": {"nodeName": "n1"}, "status": {"phase": "Running"}},
        {"spec": {"nodeName": "n1"}, "status": {"phase": "Pending"}},
        {"spec": {"nodeName": "n1"}, "status": {"phase": "Succeeded"}},
    ]
    rows = build_node_health(
        [_node("n1", pressures=("DiskPressure",), cordoned=True)], {}, pod_items=pods
    )
    row = rows[0]
    assert row["pressures"] == ["DiskPressure"]
    assert row["status"] == "critical"
    assert row["cordoned"] is True
    assert row["podsRunning"] == 2


def test_not_ready_node_sorts_first():
    rows = build_node_health([_node("a-ok"), _node("z-down", ready=False)], {})
    assert [r["name"] for r in rows] == ["z-down", "a-ok"]
    assert rows[0]["issues"] == ["Not ready"]


def test_cluster_disk_total_covers_only_measured_nodes():
    rows = [
        {"diskUsedBytes": 30 * GIB, "diskTotalBytes": 100 * GIB},
        {"diskUsedBytes": None, "diskTotalBytes": 100 * GIB},
    ]
    usage = _disk_usage(rows, None)
    assert usage["available"] is True
    assert usage["percent"] == 30.0
    assert (usage["measuredNodes"], usage["totalNodes"]) == (1, 2)


def test_cluster_disk_unavailable_carries_the_reason():
    usage = _disk_usage([{"diskUsedBytes": None, "diskTotalBytes": GIB}], "needs nodes/proxy")
    assert usage["available"] is False
    assert usage["reason"] == "needs nodes/proxy"
    assert "percent" not in usage


def test_top_alerts_put_critical_first_then_newest_and_drop_info():
    alerts = [
        {"id": "w-old", "severity": "warning", "firedAt": "2026-09-01T00:00:00"},
        {"id": "i", "severity": "info", "firedAt": "2026-09-03T00:00:00"},
        {"id": "c", "severity": "critical", "firedAt": "2026-09-01T00:00:00"},
        {"id": "w-new", "severity": "warning", "firedAt": "2026-09-02T00:00:00"},
    ]
    assert [a["id"] for a in _top_alerts(alerts)] == ["c", "w-new", "w-old"]


def test_node_disk_is_not_probed_without_ready_nodes():
    access = ClusterAccess(cluster_id="c1", context_name="c1", display_name="c1")
    with patch.object(dashboard_k8s_snapshot, "fetch_cluster_volume_stats") as fetch:
        result = dashboard_k8s_snapshot._fetch_node_fs(access, [])
    fetch.assert_not_called()
    assert result["available"] is False


def test_snapshot_passes_disk_readings_into_node_rows():
    snapshot = dashboard_k8s_snapshot.DashboardK8sSnapshot(
        node_items=[_node("n1")],
        pod_items=[],
        version_data={},
        namespaces=[],
        node_top_cpu=0.0,
        node_top_mib=0.0,
        pod_top={},
        node_fs_by_name={"n1": {"usedBytes": 10 * GIB, "capacityBytes": 100 * GIB}},
    )
    row = dashboard_k8s_snapshot.node_health_from_snapshot(snapshot)[0]
    assert row["diskPercent"] == 10.0
    assert row["podsRunning"] == 0


def test_summary_carries_disk_problem_pods_and_top_alerts(client, admin_token):
    response = client.get(
        "/api/dashboard/summary?clusterId=prod-us-east",
        headers=auth_headers(admin_token),
    )
    assert response.status_code == 200
    data = response.get_json()["data"]
    assert data["diskUsage"]["available"] is True
    assert data["diskUsage"]["measuredNodes"] >= 1
    assert isinstance(data["problemPods"], list)
    assert data["problemPodsTotal"] >= len(data["problemPods"])
    assert data["topAlerts"] and data["topAlerts"][0]["severity"] == "critical"
    full = next(n for n in data["nodeHealth"] if "DiskPressure" in n["pressures"])
    assert full["status"] == "critical"
    assert full["diskPercent"] > 85


def test_node_without_top_metrics_reports_unknown_usage_not_zero():
    row = build_node_health([_node("n1", ready=False)], {})[0]
    assert row["cpuPercent"] is None
    assert row["memoryPercent"] is None
    assert row["cpuUsedCores"] is None
    assert row["memoryUsedMiB"] is None
    assert row["cpuTotalCores"] == 4


def test_summary_node_counts_match_the_node_rows(client, admin_token):
    data = client.get(
        "/api/dashboard/summary?clusterId=prod-us-east",
        headers=auth_headers(admin_token),
    ).get_json()["data"]
    rows = data["nodeHealth"]
    assert data["nodes"]["total"] == len(rows)
    assert data["nodes"]["ready"] == sum(1 for r in rows if r["ready"])
    assert all(e["action"] not in ("login_success", "logout") for e in data["recentActivity"])


def test_summary_reads_containerd_image_fs_alongside_root_fs():
    from api.k8s_volume_stats import parse_summary

    parsed = parse_summary("n1", {
        "node": {
            "fs": {"usedBytes": 4 * GIB, "capacityBytes": 5 * GIB},
            "runtime": {"imageFs": {"usedBytes": 60 * GIB, "capacityBytes": 200 * GIB}},
        },
    })
    assert parsed["nodeFs"]["capacityBytes"] == 5 * GIB
    assert parsed["imageFs"]["usedBytes"] == 60 * GIB
    assert parsed["imageFs"]["capacityBytes"] == 200 * GIB


def test_summary_without_runtime_fs_has_no_image_fs():
    from api.k8s_volume_stats import parse_summary

    parsed = parse_summary("n1", {"node": {"fs": {"usedBytes": 1, "capacityBytes": 10}}})
    assert parsed["imageFs"] is None


def test_dashboard_disk_prefers_containerd_and_falls_back_to_root_fs():
    stats = {
        "nodes": {
            "a": {"usedBytes": 4 * GIB, "capacityBytes": 5 * GIB},
            "b": {"usedBytes": 2 * GIB, "capacityBytes": 50 * GIB},
        },
        "imageFs": {"a": {"usedBytes": 60 * GIB, "capacityBytes": 200 * GIB}},
    }
    disks = dashboard_k8s_snapshot.node_disk_by_name(stats)
    assert disks["a"]["source"] == "containerd"
    assert disks["a"]["capacityBytes"] == 200 * GIB
    assert disks["b"]["source"] == "node"

    rows = {r["name"]: r for r in build_node_health([_node("a"), _node("b")], {}, fs_by_name=disks)}
    assert rows["a"]["diskPercent"] == 30.0
    assert rows["a"]["diskSource"] == "containerd"
    assert rows["b"]["diskSource"] == "node"


def test_unmeasured_disk_has_no_source():
    rows = build_node_health([_node("n1")], {})
    assert rows[0]["diskSource"] is None
