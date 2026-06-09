import pytest

from api.dashboard_intelligence import (
    build_utilization_metric,
    evaluate_version_status,
    format_cpu_display,
    format_memory_gib,
    utilization_from_overview_resources,
    utilization_status,
)


def test_utilization_status_thresholds():
    assert utilization_status(38) == "healthy"
    assert utilization_status(70) == "warning"
    assert utilization_status(82) == "warning"
    assert utilization_status(94) == "critical"
    assert utilization_status(None) == "unknown"


def test_cpu_percentage_calculation():
    cpu, _ = utilization_from_overview_resources(
        {
            "cpu": {"usedCores": 0.76, "capacityCores": 2.0},
            "memory": {"usedGiB": 0, "capacityGiB": 0},
        }
    )
    assert cpu["available"] is True
    assert cpu["percent"] == 38.0
    assert cpu["status"] == "healthy"
    assert cpu["usedDisplay"] == "760m"
    assert cpu["allocatableDisplay"] == "2"


def test_memory_percentage_calculation():
    _, memory = utilization_from_overview_resources(
        {
            "cpu": {"usedGiB": 0, "capacityGiB": 0},
            "memory": {"usedGiB": 4.8, "capacityGiB": 8.0},
        }
    )
    assert memory["available"] is True
    assert memory["percent"] == 60.0
    assert memory["status"] == "healthy"
    assert memory["usedDisplay"] == "4.8 Gi"
    assert memory["allocatableDisplay"] == "8 Gi"


def test_missing_capacity_returns_unavailable_not_zero():
    cpu, memory = utilization_from_overview_resources({"cpu": {}, "memory": {}})
    assert cpu["available"] is False
    assert memory["available"] is False
    assert "percent" not in cpu
    assert cpu["reason"]
    assert cpu["helpText"]


def test_build_utilization_metric_unavailable():
    metric = build_utilization_metric(
        available=False,
        used=0,
        allocatable=0,
        percent=None,
        used_display="",
        allocatable_display="",
        unit="cores",
        reason="Metrics Server is not installed or accessible.",
    )
    assert metric["available"] is False
    assert metric["title"] == "Metrics unavailable"


def test_version_up_to_date():
    result = evaluate_version_status("v1.36.1", "v1.36.1")
    assert result["status"] == "up_to_date"
    assert result["label"] == "Up To Date"


def test_version_one_minor_behind():
    result = evaluate_version_status("v1.35.0", "v1.36.1")
    assert result["status"] == "one_minor_version_behind"
    assert result["minorVersionsBehind"] == 1


def test_version_two_minor_behind():
    result = evaluate_version_status("v1.34.3", "v1.36.1")
    assert result["status"] == "two_minor_versions_behind"
    assert result["minorVersionsBehind"] == 2


def test_version_unknown_when_latest_missing():
    result = evaluate_version_status("v1.34.3", "unknown")
    assert result["status"] == "unknown"


def test_version_new_patch_available():
    result = evaluate_version_status("v1.36.0", "v1.36.1")
    assert result["status"] == "new_version_available"


def test_format_cpu_display():
    assert format_cpu_display(0.76) == "760m"
    assert format_cpu_display(2) == "2"


def test_format_memory_gib():
    assert format_memory_gib(4.8) == "4.8 Gi"
    assert format_memory_gib(8) == "8 Gi"
