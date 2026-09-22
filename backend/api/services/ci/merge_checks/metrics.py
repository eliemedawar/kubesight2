"""How a tool's findings get from a container back to the quality gate.

The problem: the gate needs a NUMBER from each tool, and the tools disagree on
everything — ESLint writes JSON, Dependency-Check writes a different JSON,
SonarQube keeps its findings on a server and tells the pipeline nothing at all.
Worse, they run in three different images with three different sets of
available utilities, on runners KubeSight may not be able to fetch files from.

The answer is a one-line contract. Each check stage does its own counting, in
its own image, with whatever it has, and prints one line:

    ##kubesight-metric tool=eslint status=ok problems=3 errors=3 warnings=12

That line goes into the stage's log like any other output, and the log is
already collected, masked, persisted and restart-safe for every runner type.
Reading a number back is then a regex over rows KubeSight already has, with no
artifact download, no second transport, and no runner-specific path — including
the mock runner, which is why this is testable without a cluster.

`problems=` is the only key the gate reads. The rest is detail for the UI, and
a tool is free to report keys nobody reads yet.

What is deliberately NOT here: any attempt to parse a tool's native output.
Guessing at ESLint's stylish formatter from a log would break the first time
somebody changed a formatter, and it would break silently, reporting zero.
A stage that does not print the line reports `status: missing`, which the gate
treats as a tool that did not run — not as a tool that found nothing.
"""

from __future__ import annotations

import re
from typing import Any, Dict, List

from ....models_ci import CiBuild, CiBuildStage, CiLogChunk
from ....models_merge_checks import MERGE_CHECK_TOOLS

# The sentinel. `##` because every shell comments with it and no tool emits it
# by accident at the start of a line; the name because a grep for "kubesight"
# in a build log should find it.
SENTINEL = "##kubesight-metric"
_LINE_RE = re.compile(r"^\s*" + re.escape(SENTINEL) + r"\s+(.*)$")
_PAIR_RE = re.compile(r"([A-Za-z_][A-Za-z0-9_]*)=(\"[^\"]*\"|\S+)")

# Which stage belongs to which tool. Written into the stage's environment when
# the pipeline is generated, so renaming a stage in the editor does not detach
# it from its tool.
STAGE_TOOL_ENV = "KUBESIGHT_CHECK_TOOL"

_INT_KEYS = (
    "problems",
    "errors",
    "warnings",
    "bugs",
    "vulnerabilities",
    "codeSmells",
    "critical",
    "high",
    "medium",
    "low",
    "info",
    "files",
    "rules",
)


def _coerce(key: str, raw: str) -> Any:
    value = raw.strip().strip('"')
    if key in _INT_KEYS:
        try:
            return int(value)
        except ValueError:
            return 0
    return value


def parse_line(line: str) -> Dict[str, Any]:
    """One sentinel line to a dict, or {} for anything else."""
    match = _LINE_RE.match(line or "")
    if not match:
        return {}
    payload: Dict[str, Any] = {}
    for key, raw in _PAIR_RE.findall(match.group(1)):
        payload[key] = _coerce(key, raw)
    return payload


def parse_lines(lines: List[str]) -> Dict[str, Dict[str, Any]]:
    """Every sentinel in a body of output, keyed by tool.

    Last one wins: a stage that retries a tool and prints a second line meant
    the second one, and a stage that prints none contributes nothing.
    """
    found: Dict[str, Dict[str, Any]] = {}
    for line in lines:
        payload = parse_line(line)
        tool = str(payload.get("tool") or "").strip()
        if not tool:
            continue
        payload.pop("tool", None)
        payload.setdefault("status", "ok")
        found[tool] = payload
    return found


def collect(build: CiBuild, tools: List[str]) -> Dict[str, Dict[str, Any]]:
    """What every expected tool reported for one build.

    Every tool in ``tools`` appears in the result. A tool whose stage never
    printed a sentinel is reported explicitly:

        {"status": "missing", "problems": 0, "message": "..."}

    — which the gate reads as a check that did not run, not as a clean bill of
    health. The difference is the whole point of this module.
    """
    stage_ids = [stage.id for stage in build.stages]
    reported: Dict[str, Dict[str, Any]] = {}
    if stage_ids:
        rows = (
            CiLogChunk.query.filter(CiLogChunk.build_stage_id.in_(stage_ids))
            .filter(CiLogChunk.content.like(f"%{SENTINEL}%"))
            .order_by(CiLogChunk.build_stage_id.asc(), CiLogChunk.seq.asc())
            .all()
        )
        reported = parse_lines([row.content for row in rows])

    # Stage outcomes, so a tool that died before printing anything is described
    # by what actually happened to it rather than by a generic "missing".
    status_by_name = {
        (stage.name or "").strip().lower(): stage.status for stage in build.stages
    }

    result: Dict[str, Dict[str, Any]] = {}
    for tool in tools:
        if tool in reported:
            entry = dict(reported[tool])
            entry.setdefault("problems", 0)
            result[tool] = entry
            continue
        stage_status = _stage_status_for(tool, status_by_name)
        result[tool] = {
            "status": "skipped" if stage_status == "skipped" else "missing",
            "problems": 0,
            "message": _missing_message(stage_status),
        }
    return result


def _stage_status_for(tool: str, status_by_name: Dict[str, str]) -> str:
    from .stages import stage_name_for

    return status_by_name.get(stage_name_for(tool).strip().lower(), "")


def _missing_message(stage_status: str) -> str:
    if stage_status == "skipped":
        return "This check was skipped."
    if stage_status in ("failed", "timeout"):
        return "The check stage ended before it could report a result."
    if not stage_status:
        return "This check is not in the merge check pipeline."
    return "The check stage produced no result line."


def stage_metric_lines(stage: CiBuildStage) -> List[str]:
    """One stage's sentinel lines, for the drawer that shows a check's detail."""
    rows = (
        CiLogChunk.query.filter_by(build_stage_id=stage.id)
        .filter(CiLogChunk.content.like(f"%{SENTINEL}%"))
        .order_by(CiLogChunk.seq.asc())
        .all()
    )
    return [row.content for row in rows]


def known_tools() -> tuple:
    return MERGE_CHECK_TOOLS
