from __future__ import annotations

from typing import Any, Dict, Optional, Tuple

from .upgrade_provider import parse_k8s_version

UTILIZATION_THRESHOLDS = {
    "warning": 70.0,
    "critical": 90.0,
}

VERSION_STATUS_LABELS = {
    "up_to_date": ("✅", "Up To Date"),
    "one_minor_version_behind": ("⚠", "One Minor Version Behind"),
    "two_minor_versions_behind": ("⚠", "Two Minor Versions Behind"),
    "two_or_more_minor_versions_behind": ("⚠", "Two Or More Minor Versions Behind"),
    "new_version_available": ("⚠", "New version available"),
    "unknown": ("⚪", "Unknown"),
}


def utilization_status(percent: Optional[float]) -> str:
    if percent is None:
        return "unknown"
    if percent >= UTILIZATION_THRESHOLDS["critical"]:
        return "critical"
    if percent >= UTILIZATION_THRESHOLDS["warning"]:
        return "warning"
    return "healthy"


def format_cpu_display(cores: float) -> str:
    if cores <= 0:
        return "0"
    if cores < 1:
        return f"{int(round(cores * 1000))}m"
    rounded = round(cores, 2)
    if rounded.is_integer():
        return f"{int(rounded)}"
    return f"{rounded}"


def format_memory_gib(gib: float) -> str:
    if gib <= 0:
        return "0 Gi"
    rounded = round(gib, 1)
    if rounded.is_integer():
        return f"{int(rounded)} Gi"
    return f"{rounded} Gi"


def evaluate_version_status(current: str, latest: str) -> Dict[str, Any]:
    if not latest or str(latest).lower() == "unknown":
        icon, label = VERSION_STATUS_LABELS["unknown"]
        return {
            "status": "unknown",
            "label": label,
            "icon": icon,
            "minorVersionsBehind": None,
            "message": "Latest available version could not be determined.",
        }

    if not current or str(current).lower() == "unknown":
        icon, label = VERSION_STATUS_LABELS["unknown"]
        return {
            "status": "unknown",
            "label": label,
            "icon": icon,
            "minorVersionsBehind": None,
            "message": "Current cluster version is unknown.",
        }

    cur = parse_k8s_version(current)
    lat = parse_k8s_version(latest)

    if cur >= lat:
        icon, label = VERSION_STATUS_LABELS["up_to_date"]
        return {
            "status": "up_to_date",
            "label": label,
            "icon": icon,
            "minorVersionsBehind": 0,
            "message": label,
        }

    if lat[0] > cur[0]:
        behind = max((lat[0] - cur[0]) * 10 + (lat[1] - cur[1]), 2)
        icon, label = VERSION_STATUS_LABELS["two_or_more_minor_versions_behind"]
        display = f"{icon} {behind} minor versions behind" if behind > 2 else f"{icon} Two Minor Versions Behind"
        return {
            "status": "two_or_more_minor_versions_behind",
            "label": label,
            "icon": icon,
            "minorVersionsBehind": behind,
            "message": display,
        }

    minor_diff = lat[1] - cur[1]
    if minor_diff >= 2:
        icon, label = VERSION_STATUS_LABELS["two_minor_versions_behind"]
        return {
            "status": "two_minor_versions_behind",
            "label": label,
            "icon": icon,
            "minorVersionsBehind": minor_diff,
            "message": f"{icon} {minor_diff} minor versions behind",
        }
    if minor_diff == 1:
        icon, label = VERSION_STATUS_LABELS["one_minor_version_behind"]
        return {
            "status": "one_minor_version_behind",
            "label": label,
            "icon": icon,
            "minorVersionsBehind": 1,
            "message": f"{icon} One Minor Version Behind",
        }

    icon, label = VERSION_STATUS_LABELS["new_version_available"]
    return {
        "status": "new_version_available",
        "label": label,
        "icon": icon,
        "minorVersionsBehind": 0,
        "message": f"{icon} New version available",
    }


def build_utilization_metric(
    *,
    available: bool,
    used: float,
    allocatable: float,
    percent: Optional[float],
    used_display: str,
    allocatable_display: str,
    unit: str,
    reason: str = "",
    help_text: str = "",
) -> Dict[str, Any]:
    if not available:
        return {
            "available": False,
            "title": "Metrics unavailable",
            "reason": reason or "Metrics Server is not installed or accessible.",
            "helpText": help_text or "Install Metrics Server to enable utilization monitoring.",
        }

    status = utilization_status(percent)
    return {
        "available": True,
        "percent": percent,
        "status": status,
        "used": used,
        "allocatable": allocatable,
        "usedDisplay": used_display,
        "allocatableDisplay": allocatable_display,
        "unit": unit,
    }


def utilization_from_overview_resources(resources: Dict[str, Any]) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    cpu_block = resources.get("cpu") or {}
    mem_block = resources.get("memory") or {}
    used_cpu = float(cpu_block.get("usedCores") or 0)
    alloc_cpu = float(cpu_block.get("capacityCores") or 0)
    used_mem = float(mem_block.get("usedGiB") or 0)
    alloc_mem = float(mem_block.get("capacityGiB") or 0)

    cpu_percent = round((used_cpu / alloc_cpu) * 100, 1) if alloc_cpu > 0 else None
    mem_percent = round((used_mem / alloc_mem) * 100, 1) if alloc_mem > 0 else None

    if alloc_cpu <= 0 and alloc_mem <= 0:
        unavailable = {
            "available": False,
            "title": "Metrics unavailable",
            "reason": "Resource capacity data is not available for this cluster.",
            "helpText": "Install Metrics Server to enable utilization monitoring.",
        }
        return unavailable, unavailable

    cpu = build_utilization_metric(
        available=True,
        used=used_cpu,
        allocatable=alloc_cpu,
        percent=cpu_percent,
        used_display=format_cpu_display(used_cpu),
        allocatable_display=format_cpu_display(alloc_cpu),
        unit="cores",
    )
    memory = build_utilization_metric(
        available=True,
        used=used_mem,
        allocatable=alloc_mem,
        percent=mem_percent,
        used_display=format_memory_gib(used_mem),
        allocatable_display=format_memory_gib(alloc_mem),
        unit="Gi",
    )
    return cpu, memory
