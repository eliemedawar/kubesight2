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

import os
from typing import Dict, Iterable, List, Optional, Tuple

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
) -> int:
    """Persist masked log lines. ``lines`` is ``(seq, content, stream)``.

    Chunks already stored at the same ``seq`` are skipped, so a runner that
    replays its output on reconnect cannot duplicate it. Returns the highest
    ``seq`` now stored for the stage.
    """
    mask = mask or (lambda text: redact_text(str(text or ""), max_chars=_MAX_LINE_CHARS))
    existing_max = highest_seq(stage.id)
    stored = existing_max
    added = 0

    for seq, content, stream in lines:
        seq = int(seq)
        if seq <= existing_max:
            continue
        if stage.log_line_count + added >= _MAX_LINES_PER_STAGE:
            if not stage.log_truncated:
                stage.log_truncated = True
                db.session.add(stage)
            break
        db.session.add(
            CiLogChunk(
                build_stage_id=stage.id,
                seq=seq,
                content=mask(content)[:_MAX_LINE_CHARS],
                stream=(stream or "stdout")[:8],
            )
        )
        added += 1
        stored = max(stored, seq)

    if added:
        stage.log_line_count = (stage.log_line_count or 0) + added
        db.session.add(stage)
    if commit and (added or stage.log_truncated):
        db.session.commit()
    return stored


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


def download_text(stage_id: int, mask_reapply: Optional[object] = None) -> str:
    """Whole-stage log as plain text, for the download button."""
    rows = (
        CiLogChunk.query.filter_by(build_stage_id=stage_id)
        .order_by(CiLogChunk.seq.asc())
        .all()
    )
    return "\n".join(row.content for row in rows)
