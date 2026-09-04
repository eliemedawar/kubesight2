"""The stage matrix: builds as rows, stages as columns.

Answers the question a flat build list cannot — *where does this pipeline keep
breaking* — by putting the recent history in one grid.

Alignment is the whole problem. A build snapshots its pipeline, so the history
holds pipelines that no longer exist: stages get added, removed, renamed and
reordered while old builds keep their own shape forever. Columns are therefore
keyed by stage NAME and ordered by the newest build's positions; a build that
never had a column gets a ``null`` cell, never an empty pass. A stage that did
not exist must not read like one that did nothing.

Averages come from successful runs only. A stage that fails in 30ms would
otherwise drag its own average down and make every real run look slow.
"""

from __future__ import annotations

import re
from typing import Any, Dict, Iterable, List, Optional, Tuple

from sqlalchemy import and_, func, or_

from ...db import db
from ...models_ci import CiBuild, CiBuildStage, CiLogChunk
from . import engine as engine_service
from .serializers import build_summary

# How many builds the grid reads back. Twelve rows fit a screen; past thirty the
# grid stops being readable and the table is the better tool.
DEFAULT_BUILDS = 12
MAX_BUILDS = 30

# Below this, an "average" is one or two runs wearing a statistic's clothes.
# The UI hides the header average instead of inventing one.
MIN_SAMPLES_FOR_AVERAGE = 3

# A cell this much slower than its column average is worth a marker. Served to
# the client so the grid and these tests agree on one threshold.
SLOW_FACTOR = 1.6

# Enough log to name the error, not enough to need scrolling in a hover card.
TAIL_LINES = 3

FAILURE_STATUSES = ("failed", "timeout")
SKIP_STATUSES = ("skipped",)

# A stage in one of these states stops the build walking, so anything skipped
# after it was never reached. Cancelled belongs here and not in FAILURE_STATUSES:
# it pre-empts the stages behind it without being the pipeline's fault.
PREEMPTING_STATUSES = FAILURE_STATUSES + ("cancelled",)

# The engine writes one status — 'skipped' — for three different situations, and
# the grid must not paint them alike. Which one applies is read off the build's
# own shape rather than the wording of a message:
#   not_reached  an earlier stage failed, so this one never came up. Nothing is
#                wrong with it, so it stays quiet.
#   reused       a rerun-from-a-later-stage restored this stage's output instead
#                of repeating the work. Neutral, and worth naming.
#   unavailable  the runner could not honestly run it (no executor for the stage
#                type, no registry, BuildKit not configured). This is the one
#                that matters: the build can be green and still have produced
#                nothing, and the engine's explanation is in the stage's log.
SKIP_NOT_REACHED = "not_reached"
SKIP_REUSED = "reused"
SKIP_UNAVAILABLE = "unavailable"


def _key(name: str) -> str:
    """A URL-safe column key derived from the stage name.

    Pipelines already reject two stages with the same name case-insensitively,
    so the slug is a stable identity for the column across builds.
    """
    slug = re.sub(r"[^a-z0-9]+", "-", (name or "").strip().lower()).strip("-")
    return slug or "stage"


def _ordered_stages(build: CiBuild) -> List[CiBuildStage]:
    return sorted(build.stages, key=lambda stage: stage.position)


def _column_order(builds: Iterable[CiBuild]) -> Tuple[List[str], Dict[str, CiBuildStage]]:
    """Merge every build's stage order into one column order, newest first.

    Walking newest to oldest means the current pipeline sets the order. A stage
    only older builds had is placed by its neighbours *in that build*: before
    the next stage the grid already knows, else after the one that preceded it.
    So a stage removed from the middle stays in the middle, one that used to run
    first still comes first, and a pipeline renamed wholesale leaves its old
    columns behind the current ones rather than ahead of them.
    """
    order: List[str] = []
    exemplar: Dict[str, CiBuildStage] = {}
    for build in builds:
        stages = _ordered_stages(build)
        keys = [_key(stage.name) for stage in stages]
        for index, (stage, key) in enumerate(zip(stages, keys)):
            if key in exemplar:
                continue
            exemplar[key] = stage
            following = next((k for k in keys[index + 1 :] if k in order), None)
            previous = keys[index - 1] if index else None
            if following is not None:
                position = order.index(following)
            elif previous in order:
                position = order.index(previous) + 1
            else:
                position = len(order)
            order.insert(position, key)
    return order, exemplar


def _log_tails(stage_ids: List[int]) -> Dict[int, List[Dict[str, Any]]]:
    """The last few lines of the given stages, in two queries for the lot.

    Per-group limits need a window function, which SQLite gives us only on
    recent builds, so the highest seq per stage is read first and the tail then
    selected by range on the ``(build_stage_id, seq)`` index.
    """
    if not stage_ids:
        return {}
    highest = dict(
        db.session.query(CiLogChunk.build_stage_id, func.max(CiLogChunk.seq))
        .filter(CiLogChunk.build_stage_id.in_(stage_ids))
        .group_by(CiLogChunk.build_stage_id)
        .all()
    )
    if not highest:
        return {}
    clauses = [
        and_(
            CiLogChunk.build_stage_id == stage_id,
            CiLogChunk.seq > max(0, int(top) - TAIL_LINES),
        )
        for stage_id, top in highest.items()
    ]
    rows = (
        CiLogChunk.query.filter(or_(*clauses))
        .order_by(CiLogChunk.build_stage_id.asc(), CiLogChunk.seq.asc())
        .all()
    )
    tails: Dict[int, List[Dict[str, Any]]] = {}
    for row in rows:
        tails.setdefault(row.build_stage_id, []).append(
            # Content was masked on the way in (logs.append), so a tail cannot
            # leak a secret the log viewer would have hidden.
            {"seq": row.seq, "stream": row.stream, "content": row.content}
        )
    return tails


def _snapshot_definition(build: CiBuild, stage: CiBuildStage) -> Dict[str, Any]:
    """The stage's definition as this build was told to run it, by position.

    The pipeline stage may have been edited or deleted since; what this build
    was told to do is what the grid must report.
    """
    stages = (build.pipeline_snapshot or {}).get("stages") or []
    if 0 <= stage.position < len(stages):
        return stages[stage.position] or {}
    return {}


def _skip_kind(
    build: CiBuild, stage: CiBuildStage, preempted_positions: List[int]
) -> Optional[str]:
    """Which of the three skips this is — see the SKIP_* notes above.

    Read most-specific first, and never off the wording of a message.
    """
    if stage.status not in SKIP_STATUSES:
        return None
    if any(position < stage.position for position in preempted_positions):
        return SKIP_NOT_REACHED
    restore = (build.pipeline_snapshot or {}).get("restore") or {}
    start_from = restore.get("startFromPosition")
    definition = _snapshot_definition(build, stage)
    stage_type = definition.get("stageType") or stage.stage_type
    if (
        start_from is not None
        and stage_type != "checkout"
        and stage.position < int(start_from)
    ):
        return SKIP_REUSED
    # The engine stamps started_at before declining a stage it cannot run, so a
    # skip with no start time was never dispatched at all — a build cancelled
    # while queued, say. Claiming its runner "could not run it" would be a lie.
    if stage.started_at is None:
        return SKIP_NOT_REACHED
    return SKIP_UNAVAILABLE


def _cell(
    build: CiBuild,
    stage: CiBuildStage,
    tails: Dict[int, List[Dict[str, Any]]],
    skip_kind: Optional[str],
) -> Dict[str, Any]:
    restore = (build.pipeline_snapshot or {}).get("restore") or {}
    return {
        "stageId": stage.id,
        "position": stage.position,
        "name": stage.name,
        "stageType": stage.stage_type,
        "status": stage.status,
        "durationSeconds": stage.duration_seconds,
        # A running stage has no server-side duration yet; the grid counts up
        # from here so a working build never looks frozen.
        "startedAt": stage.started_at.isoformat() if stage.started_at else None,
        "exitCode": stage.exit_code,
        "attempt": stage.attempt,
        "runnerName": stage.runner.name if stage.runner else None,
        # The engine writes the reason it failed a stage; a deliberate skip puts
        # its explanation in the log instead, which is why logTail carries one.
        "error": stage.error,
        "skipKind": skip_kind,
        "reusedFromBuildNumber": (
            restore.get("fromBuildNumber") if skip_kind == SKIP_REUSED else None
        ),
        "continueOnFailure": bool(_snapshot_definition(build, stage).get("continueOnFailure")),
        "logTail": tails.get(stage.id, []),
    }


def stage_matrix(
    service_id: int, *, limit: int = DEFAULT_BUILDS, status: Optional[str] = None
) -> Dict[str, Any]:
    """Columns, rows and per-stage statistics for one service's recent builds."""
    limit = max(1, min(int(limit or DEFAULT_BUILDS), MAX_BUILDS))
    builds, total = engine_service.list_builds(
        service_id=service_id, status=status, limit=limit, offset=0
    )
    # list_builds pages by id, which is the same order in practice; the grid
    # sorts by number anyway, because "newest" here means the newest build, and
    # the newest build is what sets the column order.
    builds = sorted(builds, key=lambda build: build.number, reverse=True)

    order, exemplar = _column_order(builds)

    # Classify skips before reading any log, so only the stages whose output
    # actually explains something are queried for a tail.
    skip_kinds: Dict[int, Optional[str]] = {}
    for build in builds:
        preempted = [
            stage.position
            for stage in build.stages
            if stage.status in PREEMPTING_STATUSES
        ]
        for stage in build.stages:
            skip_kinds[stage.id] = _skip_kind(build, stage, preempted)

    tails = _log_tails(
        [
            stage.id
            for build in builds
            for stage in build.stages
            if stage.status in FAILURE_STATUSES
            or skip_kinds.get(stage.id) == SKIP_UNAVAILABLE
        ]
    )

    # Cells first: the column statistics are derived from them, so there is one
    # source of truth for what each cell says.
    rows: List[Dict[str, Any]] = []
    for build in builds:
        cells: Dict[str, Dict[str, Any]] = {}
        for stage in _ordered_stages(build):
            cells[_key(stage.name)] = _cell(build, stage, tails, skip_kinds.get(stage.id))
        row = build_summary(build)
        # None rather than a missing key: the grid has to tell "this build never
        # had this stage" apart from "this stage has not started yet".
        row["cells"] = {key: cells.get(key) for key in order}
        rows.append(row)

    columns: List[Dict[str, Any]] = []
    for key in order:
        durations = [
            cell["durationSeconds"]
            for row in rows
            for cell in [row["cells"].get(key)]
            if cell and cell["status"] == "success" and cell["durationSeconds"] is not None
        ]
        present = [cell for row in rows for cell in [row["cells"].get(key)] if cell]
        statuses = [cell["status"] for cell in present]
        average = round(sum(durations) / len(durations)) if durations else None
        columns.append(
            {
                "key": key,
                "name": exemplar[key].name,
                "stageType": exemplar[key].stage_type,
                "avgSeconds": average if len(durations) >= MIN_SAMPLES_FOR_AVERAGE else None,
                "sampleSize": len(durations),
                # Counted separately: a failure is a broken build, a skip is a
                # build that quietly produced less than it looks like it did.
                # Stages that were never reached count as neither.
                "failures": sum(1 for value in statuses if value in FAILURE_STATUSES),
                "skips": sum(1 for cell in present if cell["skipKind"] == SKIP_UNAVAILABLE),
                "notReached": sum(
                    1 for cell in present if cell["skipKind"] == SKIP_NOT_REACHED
                ),
                "runs": len(statuses),
            }
        )

    # Share of a typical build, over the columns that have an average to give.
    total_average = sum(column["avgSeconds"] or 0 for column in columns)
    for column in columns:
        column["shareOfBuild"] = (
            round((column["avgSeconds"] or 0) / total_average, 4) if total_average else None
        )

    return {
        "columns": columns,
        "rows": rows,
        "total": total,
        "limit": limit,
        "slowFactor": SLOW_FACTOR,
        "minSamplesForAverage": MIN_SAMPLES_FOR_AVERAGE,
    }
