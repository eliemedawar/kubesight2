"""Filter repetitive probe traffic from pod log streams."""



from __future__ import annotations



import re

from typing import Iterable, List



_HEALTH_PROBE_LINE = re.compile(

    r'"(?:GET|HEAD)\s+/(?:health|healthz|readyz|livez)(?:\?[^\s"]*)?\s+HTTP/',

    re.IGNORECASE,

)



_LOGS_API_SELF_LINE = re.compile(
    r'"(?:GET|HEAD)\s+/api/(?:logs(?:\?[^\s"]*)?|clusters/[^/]+/namespaces/[^/]+/pods/[^/]+/containers/[^/]+/logs(?:\?[^\s"]*)?)\s+HTTP/',
    re.IGNORECASE,
)

# kubesight.api concise access logs (no quoted HTTP request line).
_LOGS_API_SELF_LINE_PLAIN = re.compile(
    r"(?:GET|HEAD)\s+/api/(?:logs(?:\?|\s)|clusters/.+/containers/[^/\s]+/logs(?:\?|\s))",
    re.IGNORECASE,
)



def is_health_probe_log_line(line: str) -> bool:

    return bool(_HEALTH_PROBE_LINE.search(str(line or "")))





def is_logs_api_self_line(line: str) -> bool:
    text = str(line or "")
    return bool(_LOGS_API_SELF_LINE.search(text) or _LOGS_API_SELF_LINE_PLAIN.search(text))





def filter_logs_api_self_lines(lines: Iterable[str]) -> List[str]:

    return [line for line in lines if line and not is_logs_api_self_line(line)]





def filter_health_probe_log_lines(lines: Iterable[str]) -> List[str]:

    materialized = [line for line in lines if line]

    filtered = [line for line in materialized if not is_health_probe_log_line(line)]

    # Keep the raw tail when every line is probe noise so the viewer is not blank.

    return filtered if filtered else materialized





def filter_live_log_noise(lines: Iterable[str]) -> List[str]:
    """Drop probe traffic and self-referential log polling from live streams."""
    materialized = [line for line in lines if line]
    without_self = [line for line in materialized if not is_logs_api_self_line(line)]
    without_noise = [
        line for line in without_self if not is_health_probe_log_line(line)
    ]
    if without_noise:
        return without_noise
    # Keep probe-only tails visible, but never replay log-polling feedback loops.
    if without_self:
        return without_self
    if any(is_logs_api_self_line(line) for line in materialized):
        return []
    return materialized if materialized else []

