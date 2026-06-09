"""Fetch and scan Kubernetes pod logs for log-based alerting."""

from __future__ import annotations

import hashlib
import re
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple, Union

from .cluster_access import ClusterAccess
from .k8s_provider import K8sCommandError, _run_for_access

PodLogMatch = Dict[str, Any]


def _since_arg(seconds: int) -> str:
    seconds = max(1, int(seconds))
    if seconds >= 3600 and seconds % 3600 == 0:
        return f"{seconds // 3600}h"
    if seconds >= 60 and seconds % 60 == 0:
        return f"{seconds // 60}m"
    return f"{seconds}s"


def pod_container_names(pod: dict) -> List[str]:
    spec = pod.get("spec", {}) or {}
    names: List[str] = []
    for container in spec.get("containers") or []:
        name = container.get("name")
        if name:
            names.append(str(name))
    return names


def fetch_pod_logs(
    access: Union[ClusterAccess, str],
    namespace: str,
    pod_name: str,
    container_name: str,
    since_seconds: int,
) -> str:
    """Return pod log text with timestamps from kubectl logs --since."""
    args = [
        "logs",
        pod_name,
        "-n",
        namespace,
        "-c",
        container_name,
        f"--since={_since_arg(since_seconds)}",
        "--timestamps=true",
    ]
    try:
        return _run_for_access(access, args)
    except K8sCommandError:
        return ""


def _split_log_lines(log_text: str) -> List[str]:
    if not log_text:
        return []
    return log_text.splitlines()


def _extract_log_timestamp(line: str) -> Tuple[Optional[str], str]:
    """Parse kubectl --timestamps=true prefix; return (iso_timestamp, message)."""
    if not line:
        return None, ""
    match = re.match(r"^(\S+)\s+(.*)$", line)
    if not match:
        return None, line
    ts_raw, message = match.group(1), match.group(2)
    try:
        normalized = ts_raw.replace("Z", "+00:00")
        parsed = datetime.fromisoformat(normalized)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc).isoformat().replace("+00:00", "Z"), message
    except ValueError:
        return ts_raw, message


def _line_matches(
    text: str,
    *,
    match_type: str,
    pattern: str,
    case_sensitive: bool,
) -> bool:
    if not pattern:
        return False
    if match_type == "contains" and "," in pattern:
        parts = [part.strip() for part in pattern.split(",") if part.strip()]
        return any(
            _line_matches(
                text,
                match_type="contains",
                pattern=part,
                case_sensitive=case_sensitive,
            )
            for part in parts
        )
    haystack = text
    needle = pattern
    if not case_sensitive:
        haystack = haystack.lower()
        needle = needle.lower()
    if match_type == "regex":
        flags = 0 if case_sensitive else re.IGNORECASE
        try:
            return re.search(pattern, text, flags) is not None
        except re.error:
            return False
    return needle in haystack


def _log_entry_hash(
    *,
    pod_name: str,
    container_name: str,
    log_timestamp: str,
    matching_line: str,
) -> str:
    payload = f"{pod_name}:{container_name}:{log_timestamp}:{matching_line}"
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def find_log_matches(
    log_text: str,
    *,
    match_type: str,
    pattern: str,
    case_sensitive: bool,
    context_before: int,
    context_after: int,
    max_lines: int,
) -> List[Dict[str, Any]]:
    """Return match entries with context lines from raw pod log text."""
    lines = _split_log_lines(log_text)
    if not lines:
        return []

    matches: List[Dict[str, Any]] = []
    for index, line in enumerate(lines):
        timestamp, message = _extract_log_timestamp(line)
        if not _line_matches(message, match_type=match_type, pattern=pattern, case_sensitive=case_sensitive):
            continue

        start = max(0, index - max(0, context_before))
        end = min(len(lines), index + max(0, context_after) + 1)
        context_lines = lines[start:end]
        if max_lines > 0:
            context_lines = context_lines[:max_lines]

        log_timestamp = timestamp or datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        matches.append(
            {
                "matchingLine": line,
                "matchingMessage": message,
                "logTimestamp": log_timestamp,
                "logLines": context_lines,
                "logSnippet": "\n".join(context_lines),
                "logHash": _log_entry_hash(
                    pod_name="",
                    container_name="",
                    log_timestamp=log_timestamp,
                    matching_line=line,
                ),
            }
        )
    return matches


def scan_pod_logs_for_matches(
    access: Union[ClusterAccess, str],
    *,
    namespace: str,
    pod: dict,
    since_seconds: int,
    match_type: str,
    pattern: str,
    case_sensitive: bool,
    context_before: int,
    context_after: int,
    max_lines: int,
) -> List[PodLogMatch]:
    meta = pod.get("metadata", {}) or {}
    pod_name = meta.get("name")
    if not pod_name:
        return []

    results: List[PodLogMatch] = []
    for container_name in pod_container_names(pod):
        log_text = fetch_pod_logs(access, namespace, pod_name, container_name, since_seconds)
        for match in find_log_matches(
            log_text,
            match_type=match_type,
            pattern=pattern,
            case_sensitive=case_sensitive,
            context_before=context_before,
            context_after=context_after,
            max_lines=max_lines,
        ):
            log_timestamp = match["logTimestamp"]
            matching_line = match["matchingLine"]
            results.append(
                {
                    "podName": pod_name,
                    "containerName": container_name,
                    "namespace": namespace,
                    "logTimestamp": log_timestamp,
                    "matchedPattern": pattern,
                    "matchingLine": matching_line,
                    "logLines": match["logLines"],
                    "logSnippet": match["logSnippet"],
                    "logHash": _log_entry_hash(
                        pod_name=pod_name,
                        container_name=container_name,
                        log_timestamp=log_timestamp,
                        matching_line=matching_line,
                    ),
                }
            )
    return results
