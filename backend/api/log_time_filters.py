from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import List, Optional, Tuple

LOG_LINE_TIMESTAMP = re.compile(
    r"^(\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:?\d{2}(?::?\d{2})?))\s"
)


def normalize_log_timestamp_token(token: str) -> str:
    return token.strip().replace(" ", "T", 1)

ALLOWED_SINCE_SECONDS = {900, 3600, 21600, 86400}
MAX_LOG_RANGE_SECONDS = 7 * 24 * 3600


@dataclass(frozen=True)
class LogTimeFilters:
    since_seconds: Optional[int] = None
    since_time: Optional[datetime] = None
    until_time: Optional[datetime] = None


def parse_rfc3339(value: str) -> datetime:
    text = normalize_log_timestamp_token(value)
    if not text:
        raise ValueError("timestamp is required")
    if text.endswith("Z"):
        text = f"{text[:-1]}+00:00"
    parsed = datetime.fromisoformat(text)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def seconds_to_kubectl_duration(seconds: int) -> str:
    if seconds % 3600 == 0 and seconds >= 3600:
        return f"{seconds // 3600}h"
    if seconds % 60 == 0 and seconds >= 60:
        return f"{seconds // 60}m"
    return f"{seconds}s"


def filter_log_lines_until(lines: List[str], until_time: datetime) -> List[str]:
    until_utc = until_time.astimezone(timezone.utc)
    filtered: List[str] = []
    for line in lines:
        match = LOG_LINE_TIMESTAMP.match(line)
        if not match:
            filtered.append(line)
            continue
        try:
            timestamp = parse_rfc3339(normalize_log_timestamp_token(match.group(1)))
        except ValueError:
            filtered.append(line)
            continue
        if timestamp <= until_utc:
            filtered.append(line)
    return filtered


def filter_log_lines_after(lines: List[str], since_time: datetime) -> List[str]:
    since_utc = since_time.astimezone(timezone.utc)
    filtered: List[str] = []
    for line in lines:
        match = LOG_LINE_TIMESTAMP.match(line)
        if not match:
            continue
        try:
            timestamp = parse_rfc3339(normalize_log_timestamp_token(match.group(1)))
        except ValueError:
            continue
        if timestamp > since_utc:
            filtered.append(line)
    return filtered


def format_rfc3339_z(value: datetime) -> str:
    utc = value.astimezone(timezone.utc)
    text = utc.strftime("%Y-%m-%dT%H:%M:%S")
    if utc.microsecond:
        text += f".{utc.microsecond:06d}".rstrip("0").rstrip(".")
    return f"{text}Z"


def advance_log_cursor(value: datetime, *, milliseconds: int = 1) -> datetime:
    """Move a tail cursor forward so kubectl --since-time does not repeat the last line."""
    from datetime import timedelta

    return value.astimezone(timezone.utc) + timedelta(milliseconds=milliseconds)


def parse_log_time_filters(
    since_seconds_raw: str,
    since_time_raw: str,
    until_time_raw: str,
) -> Tuple[Optional[LogTimeFilters], Optional[str]]:
    since_seconds_value = since_seconds_raw.strip()
    since_time_value = since_time_raw.strip()
    until_time_value = until_time_raw.strip()

    has_since_seconds = bool(since_seconds_value)
    has_since_time = bool(since_time_value)
    has_until_time = bool(until_time_value)

    if not has_since_seconds and not has_since_time and not has_until_time:
        return LogTimeFilters(), None

    if has_since_seconds and (has_since_time or has_until_time):
        return None, "Cannot combine sinceSeconds with sinceTime or untilTime."

    if has_since_seconds:
        try:
            since_seconds = int(since_seconds_value)
        except ValueError:
            return None, "sinceSeconds must be a positive integer."
        if since_seconds <= 0:
            return None, "sinceSeconds must be a positive integer."
        if since_seconds not in ALLOWED_SINCE_SECONDS:
            allowed = ", ".join(str(value) for value in sorted(ALLOWED_SINCE_SECONDS))
            return None, f"sinceSeconds must be one of: {allowed}."
        return LogTimeFilters(since_seconds=since_seconds), None

    if has_until_time and not has_since_time:
        return None, "sinceTime is required when untilTime is provided."

    try:
        since_time = parse_rfc3339(since_time_value)
    except ValueError:
        return None, "sinceTime must be a valid RFC3339 timestamp."

    until_time: Optional[datetime] = None
    if has_until_time:
        try:
            until_time = parse_rfc3339(until_time_value)
        except ValueError:
            return None, "untilTime must be a valid RFC3339 timestamp."

        if since_time >= until_time:
            return None, "sinceTime must be before untilTime."

        range_seconds = (until_time - since_time).total_seconds()
        if range_seconds > MAX_LOG_RANGE_SECONDS:
            return None, "Custom log time range cannot exceed 7 days."

    return LogTimeFilters(since_time=since_time, until_time=until_time), None
