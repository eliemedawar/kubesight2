"""The Kubernetes release calendar: when each minor version leaves upstream support.

Upstream patches the three newest minor versions; each one gets about 14
months of patches. The table below holds the published dates; a minor newer
than the table gets dates projected from the usual four-month cadence and is
marked ``estimated`` so the UI can say so.

Nothing here touches the network: the newest released minor comes from the
release lookup's cache when it has an answer (see ``upgrade_provider``) and
from this table otherwise, so the Upgrade Center fleet view never waits on
dl.k8s.io.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

# minor -> (release date, end of life, dates are an estimate)
_CALENDAR: Dict[int, Tuple[date, date, bool]] = {
    27: (date(2023, 4, 11), date(2024, 6, 28), False),
    28: (date(2023, 8, 15), date(2024, 10, 28), False),
    29: (date(2023, 12, 13), date(2025, 2, 28), False),
    30: (date(2024, 4, 17), date(2025, 6, 28), False),
    31: (date(2024, 8, 13), date(2025, 10, 28), False),
    32: (date(2024, 12, 11), date(2026, 2, 28), False),
    33: (date(2025, 4, 23), date(2026, 6, 28), False),
    34: (date(2025, 8, 27), date(2026, 10, 27), False),
    35: (date(2025, 12, 17), date(2027, 2, 28), True),
    36: (date(2026, 4, 22), date(2027, 6, 28), True),
    37: (date(2026, 8, 26), date(2027, 10, 28), True),
}

# A cluster whose minor leaves support within this many days is "ending".
ENDING_SOON_DAYS = 60
SUPPORTED_MINOR_COUNT = 3


def _today() -> date:
    return datetime.now(timezone.utc).date()


def minor_dates(minor: int) -> Tuple[date, date, bool]:
    """(released, end of life, estimated) for ``1.<minor>``."""
    if minor in _CALENDAR:
        return _CALENDAR[minor]
    newest = max(_CALENDAR)
    oldest = min(_CALENDAR)
    if minor > newest:
        released = _CALENDAR[newest][0] + timedelta(days=122 * (minor - newest))
    else:
        released = _CALENDAR[oldest][0] - timedelta(days=122 * (oldest - minor))
    return released, released + timedelta(days=426), True


def newest_released_minor(latest_version: Optional[str] = None) -> int:
    """The newest released minor: from a known latest version, else the table."""
    from .upgrade_provider import parse_k8s_version

    if latest_version and latest_version not in ("", "unknown"):
        parsed = parse_k8s_version(latest_version)
        if parsed[1]:
            return parsed[1]
    today = _today()
    released = [m for m, (rel, _eol, _est) in _CALENDAR.items() if rel <= today]
    return max(released) if released else max(_CALENDAR)


def support_status(version: str, *, today: Optional[date] = None) -> Dict[str, Any]:
    """Where ``version`` stands against upstream support today."""
    from .upgrade_provider import parse_k8s_version

    parsed = parse_k8s_version(version)
    if parsed == (0, 0, 0):
        return {"state": "unknown", "minor": None, "endOfLife": None, "daysLeft": None, "estimated": False}
    today = today or _today()
    _released, eol, estimated = minor_dates(parsed[1])
    days_left = (eol - today).days
    if days_left < 0:
        state = "ended"
    elif days_left <= ENDING_SOON_DAYS:
        state = "ending"
    else:
        state = "supported"
    return {
        "state": state,
        "minor": f"{parsed[0]}.{parsed[1]}",
        "endOfLife": eol.isoformat(),
        "daysLeft": days_left,
        "estimated": estimated,
    }


def calendar(latest_version: Optional[str] = None, *, around: Optional[List[int]] = None) -> List[Dict[str, Any]]:
    """The release line the fleet view draws: every minor a cluster sits on,
    up to the newest released one, oldest first."""
    newest = newest_released_minor(latest_version)
    # The supported window and the minor that just left it, plus any older
    # minor a cluster still runs — not every ended minor in between.
    minors = set(range(newest - SUPPORTED_MINOR_COUNT, newest + 1))
    minors.update(m for m in (around or []) if m and newest - 12 <= m <= newest)
    today = _today()
    rows = []
    for minor in sorted(minors):
        released, eol, estimated = minor_dates(minor)
        days_left = (eol - today).days
        rows.append({
            "minor": f"1.{minor}",
            "released": released.isoformat(),
            "endOfLife": eol.isoformat(),
            "daysLeft": days_left,
            "estimated": estimated,
            "state": "ended" if days_left < 0 else "ending" if days_left <= ENDING_SOON_DAYS else "supported",
            "latest": minor == newest,
        })
    return rows


def upgrade_path(current_version: str, latest_version: Optional[str], patch_for_minor) -> List[Dict[str, Any]]:
    """The hops from ``current_version`` to the newest minor, one minor at a time.

    ``patch_for_minor(major, minor)`` returns the newest known patch for that
    minor (``v1.35.7``) or None while it is not known yet; a hop without one
    reads ``v1.35.x`` and the target picker asks again later."""
    from .upgrade_provider import parse_k8s_version

    major, minor, _patch = parse_k8s_version(current_version)
    if not minor:
        return []
    newest = newest_released_minor(latest_version)
    hops = []
    for step, target_minor in enumerate(range(minor + 1, newest + 1), start=1):
        released, eol, estimated = minor_dates(target_minor)
        target = patch_for_minor(major, target_minor)
        hops.append({
            "hop": step,
            "minor": f"{major}.{target_minor}",
            "version": target,
            "label": target or f"v{major}.{target_minor}.x",
            "endOfLife": eol.isoformat(),
            "estimated": estimated,
            "latest": target_minor == newest,
        })
    return hops
