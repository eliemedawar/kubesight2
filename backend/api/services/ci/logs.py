"""Build log ingest, masking, and offset reads.

Masking happens **here**, on the way in — not in the runner, not in the UI. A
runner is the least trustworthy place to enforce redaction (an external agent is
someone else's machine), and masking on read leaves the plaintext sitting in the
database. Every path that persists log output goes through :func:`append`.

Two layers of redaction are applied:

1. Exact-value masking of every secret in scope for the build, longest-first so
   a value that contains another value cannot leave a fragment behind.
2. The shared pattern-based redactor already used by Application Intelligence,
   which catches credentials in URLs, ``KEY=value`` assignments, bearer headers
   and PEM blocks — including secrets KubeSight never issued.
"""

from __future__ import annotations

import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Dict, Iterable, Iterator, List, Optional, Tuple

from sqlalchemy import func

from ...db import db
from ...models_ci import CiBuildStage, CiLogChunk
from ..application_intelligence_security import redact_text

MASK = "***"

# A value shorter than this is masked only when it looks deliberately secret;
# blanket-masking "1" or "ok" would shred every log line.
_MIN_MASKABLE_LENGTH = 4

# Hard ceiling per stage. Runaway output is truncated rather than allowed to
# fill the database; the stage row records that it happened.
_MAX_LINES_PER_STAGE = int(os.getenv("CI_MAX_LOG_LINES_PER_STAGE", "20000"))
_MAX_LINE_CHARS = int(os.getenv("CI_MAX_LOG_LINE_CHARS", "8000"))
# Rows per INSERT statement when storing a burst of output.
_INSERT_BATCH = 500

logger = logging.getLogger(__name__)


def build_masker(secret_values: Iterable[str]):
    """Return ``mask(text) -> text`` for one build's secret set.

    Sorted longest-first: masking a short value first could otherwise leave the
    tail of a longer secret that contains it visible in the output.
    """
    values = sorted(
        {
            str(value)
            for value in (secret_values or [])
            if value and len(str(value)) >= _MIN_MASKABLE_LENGTH
        },
        key=len,
        reverse=True,
    )

    def mask(text: str) -> str:
        cleaned = str(text or "")
        for value in values:
            if value in cleaned:
                cleaned = cleaned.replace(value, MASK)
        return redact_text(cleaned, max_chars=_MAX_LINE_CHARS)

    return mask


def append(
    stage: CiBuildStage,
    lines: Iterable[Tuple[int, str, str]],
    *,
    mask=None,
    commit: bool = True,
    known_max: Optional[int] = None,
) -> int:
    """Persist masked log lines. ``lines`` is ``(seq, content, stream)``.

    Idempotent: a line whose ``seq`` is already stored is skipped - by the
    quick filter on the highest stored ``seq`` and, for two writers racing on
    the same lines, by the insert itself (``ON CONFLICT DO NOTHING`` on
    PostgreSQL and SQLite). A runner that replays its output on reconnect, or
    two passes draining one stage, can therefore never duplicate a line nor
    fail on the unique ``(stage, seq)`` constraint. Returns the highest ``seq``
    this call tried to store (or the stored maximum if none).

    ``known_max`` is the caller's fresh ``highest_seq`` for the stage, which
    saves asking again.
    """
    mask = mask or (lambda text: redact_text(str(text or ""), max_chars=_MAX_LINE_CHARS))
    existing_max = highest_seq(stage.id) if known_max is None else int(known_max)
    stored = existing_max
    room = _MAX_LINES_PER_STAGE - int(stage.log_line_count or 0)
    now = datetime.now(timezone.utc)
    rows: List[Dict[str, object]] = []
    truncated = False

    for seq, content, stream in lines:
        seq = int(seq)
        if seq <= existing_max:
            continue
        if len(rows) >= room:
            truncated = True
            break
        rows.append(
            {
                "build_stage_id": stage.id,
                "seq": seq,
                "content": mask(content)[:_MAX_LINE_CHARS],
                "stream": (stream or "stdout")[:8],
                "created_at": now,
            }
        )
        stored = max(stored, seq)

    added = _insert_new(rows) if rows else 0
    if truncated and not stage.log_truncated:
        stage.log_truncated = True
        db.session.add(stage)
    if added:
        stage.log_line_count = (stage.log_line_count or 0) + added
        db.session.add(stage)
    if commit and (added or truncated):
        db.session.commit()
    return stored


def _insert_new(rows: List[Dict[str, object]]) -> int:
    """Insert ``rows``, skipping any ``(stage, seq)`` already stored.

    Returns how many were actually inserted. PostgreSQL and SQLite share the
    ``ON CONFLICT DO NOTHING`` spelling; anything else gets a pre-read of the
    seqs already there, which is the best a portable insert can do.
    """
    table = CiLogChunk.__table__
    dialect = db.session.get_bind(mapper=CiLogChunk).dialect.name
    insert = None
    if dialect == "postgresql":
        from sqlalchemy.dialects.postgresql import insert
    elif dialect == "sqlite":
        from sqlalchemy.dialects.sqlite import insert
    added = 0
    for start in range(0, len(rows), _INSERT_BATCH):
        batch = rows[start:start + _INSERT_BATCH]
        if insert is not None:
            statement = (
                insert(table)
                .values(batch)
                .on_conflict_do_nothing(index_elements=["build_stage_id", "seq"])
            )
            result = db.session.execute(statement)
            count = result.rowcount
            added += len(batch) if count is None or count < 0 else int(count)
            continue
        stage_id = batch[0]["build_stage_id"]
        present = {
            int(seq)
            for (seq,) in db.session.query(CiLogChunk.seq).filter(
                CiLogChunk.build_stage_id == stage_id,
                CiLogChunk.seq.in_([row["seq"] for row in batch]),
            )
        }
        fresh = [row for row in batch if int(row["seq"]) not in present]
        if fresh:
            db.session.execute(table.insert(), fresh)
            added += len(fresh)
    return added


def append_system(stage: CiBuildStage, message: str, *, commit: bool = True) -> None:
    """Record one KubeSight-authored line (not runner output)."""
    append(stage, [(highest_seq(stage.id) + 1, message, "system")], commit=commit)


def highest_seq(stage_id: int) -> int:
    value = (
        db.session.query(func.max(CiLogChunk.seq))
        .filter(CiLogChunk.build_stage_id == stage_id)
        .scalar()
    )
    return int(value or 0)


def read(
    stage_id: int, after_seq: int = 0, limit: int = 1000
) -> Dict[str, object]:
    """Offset read for the log viewer.

    Returns ``{lines, nextSeq, hasMore}``. The viewer polls with the previous
    ``nextSeq``, which makes reads cheap and resumable without holding a
    server thread open per viewer.
    """
    limit = max(1, min(int(limit), 5000))
    rows: List[CiLogChunk] = (
        CiLogChunk.query.filter(
            CiLogChunk.build_stage_id == stage_id, CiLogChunk.seq > int(after_seq)
        )
        .order_by(CiLogChunk.seq.asc())
        .limit(limit + 1)
        .all()
    )
    has_more = len(rows) > limit
    rows = rows[:limit]
    return {
        "lines": [
            {"seq": row.seq, "stream": row.stream, "content": row.content} for row in rows
        ],
        "nextSeq": rows[-1].seq if rows else int(after_seq),
        "hasMore": has_more,
    }


def clear(stage_id: int, *, commit: bool = False) -> None:
    """Drop a stage's output. Used when a stage is re-attempted."""
    CiLogChunk.query.filter_by(build_stage_id=stage_id).delete(synchronize_session=False)
    if commit:
        db.session.commit()


def read_tail(stage_id: int, limit: int = 200) -> Dict[str, object]:
    """The LAST ``limit`` lines of a stage, oldest first.

    For "how did this end" questions (a failure's last words): reading from
    the start and slicing would show the wrong part of any stage longer than
    the page. Returns ``{lines, truncated, firstSeq}`` - ``truncated`` when
    earlier lines exist.
    """
    limit = max(1, min(int(limit), 5000))
    rows: List[CiLogChunk] = (
        CiLogChunk.query.filter(CiLogChunk.build_stage_id == stage_id)
        .order_by(CiLogChunk.seq.desc())
        .limit(limit + 1)
        .all()
    )
    truncated = len(rows) > limit
    rows = list(reversed(rows[:limit]))
    return {
        "lines": [
            {"seq": row.seq, "stream": row.stream, "content": row.content} for row in rows
        ],
        "truncated": truncated,
        "firstSeq": rows[0].seq if rows else None,
    }


def iter_download(stage_id: int, batch: int = 2000) -> Iterator[str]:
    """Whole-stage log as text, a batch at a time, for the download button.

    Paged by ``seq`` (the unique index), so a 20,000-line stage is never held
    in memory at once and every batch is an index range read.
    """
    after = 0
    first = True
    while True:
        rows = (
            db.session.query(CiLogChunk.seq, CiLogChunk.content)
            .filter(CiLogChunk.build_stage_id == stage_id, CiLogChunk.seq > after)
            .order_by(CiLogChunk.seq.asc())
            .limit(batch)
            .all()
        )
        if not rows:
            return
        text = "\n".join(content or "" for _, content in rows)
        yield text if first else "\n" + text
        first = False
        after = rows[-1][0]
        if len(rows) < batch:
            return


def download_text(stage_id: int, mask_reapply: Optional[object] = None) -> str:
    """Whole-stage log as plain text (the streamed download, joined)."""
    return "".join(iter_download(stage_id))


# ---------------------------------------------------------------------------
# Retention (opt-in)
# ---------------------------------------------------------------------------


def retention_days() -> Optional[int]:
    """``CI_LOG_RETENTION_DAYS``, or None when retention is off (the default)."""
    raw = os.getenv("CI_LOG_RETENTION_DAYS", "").strip()
    if not raw:
        return None
    try:
        days = int(raw)
    except ValueError:
        return None
    return days if days > 0 else None


def prune_old_logs(
    days: Optional[int] = None,
    *,
    keep_builds_per_service: Optional[int] = None,
    max_builds: int = 200,
    commit: bool = True,
) -> Dict[str, int]:
    """Delete the log lines of FINISHED builds older than ``days``.

    Off unless asked: with no ``days`` and no ``CI_LOG_RETENTION_DAYS`` it does
    nothing. Never touches a queued or running build, and always spares the
    newest ``keep_builds_per_service`` builds of each service
    (``CI_LOG_RETENTION_KEEP_BUILDS``, default 20) however old they are, so a
    service that builds rarely keeps its recent history. The stage rows stay -
    the build page still shows what ran and how it ended - with
    ``log_line_count`` reset to 0. ``max_builds`` bounds one call's work.

    Nothing schedules this. To run it periodically, call it from the CI engine's
    bookkeeping step (``engine._run_pass``, the 5-second housekeeping block) at
    a slow cadence, or from an admin action.
    """
    from ...models_ci import CiBuild

    days = days if days is not None else retention_days()
    if not days:
        return {"builds": 0, "stages": 0, "lines": 0}
    if keep_builds_per_service is None:
        try:
            keep_builds_per_service = max(0, int(os.getenv("CI_LOG_RETENTION_KEEP_BUILDS", "20")))
        except ValueError:
            keep_builds_per_service = 20
    cutoff = datetime.now(timezone.utc) - timedelta(days=int(days))

    ranked = (
        db.session.query(
            CiBuild.id.label("build_id"),
            func.row_number()
            .over(partition_by=CiBuild.service_id, order_by=CiBuild.id.desc())
            .label("rank"),
        )
        .subquery()
    )
    candidates = [
        int(build_id)
        for (build_id,) in db.session.query(CiBuild.id)
        .join(ranked, ranked.c.build_id == CiBuild.id)
        .filter(
            CiBuild.status.notin_(("queued", "running")),
            CiBuild.finished_at.isnot(None),
            CiBuild.finished_at < cutoff,
            ranked.c.rank > keep_builds_per_service,
        )
        .order_by(CiBuild.id.asc())
        .limit(max(1, int(max_builds)))
    ]
    if not candidates:
        return {"builds": 0, "stages": 0, "lines": 0}
    stage_ids = [
        int(stage_id)
        for (stage_id,) in db.session.query(CiBuildStage.id).filter(
            CiBuildStage.build_id.in_(candidates), CiBuildStage.log_line_count > 0
        )
    ]
    deleted = 0
    for start in range(0, len(stage_ids), 200):
        chunk = stage_ids[start:start + 200]
        deleted += int(
            CiLogChunk.query.filter(CiLogChunk.build_stage_id.in_(chunk)).delete(
                synchronize_session=False
            )
            or 0
        )
        CiBuildStage.query.filter(CiBuildStage.id.in_(chunk)).update(
            {CiBuildStage.log_line_count: 0}, synchronize_session=False
        )
    if commit:
        db.session.commit()
    if deleted:
        logger.info("CI log retention removed %s lines from %s builds", deleted, len(candidates))
    return {"builds": len(candidates), "stages": len(stage_ids), "lines": deleted}
