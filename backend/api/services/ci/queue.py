"""The build queue.

There is no queue table: the queue *is* ``ci_builds`` with ``status='queued'``,
ordered by ``queued_at``. A separate table would duplicate state that already
has to be correct on the build row, and would let the two disagree.

Every access goes through this module so the backing store stays replaceable.
Swapping in Redis or RabbitMQ means reimplementing :func:`claim_next`,
:func:`requeue`, :func:`depth` and :func:`enqueue` — nothing above this file
knows how the queue is stored.

Claiming is the part that has to be right. On PostgreSQL the claim uses
``SELECT ... FOR UPDATE SKIP LOCKED``, so two schedulers racing on the same row
cannot both win: the loser skips it and takes the next one. KubeSight runs a
single gunicorn worker today, but a tick that overlaps its own previous run —
or a future multi-worker deployment — would otherwise double-dispatch a build.
SQLite has no ``SKIP LOCKED``; it also has no concurrency to protect against
here, so it falls back to a plain ordered read.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import List, Optional

from sqlalchemy import case, func, select, text
from sqlalchemy.exc import OperationalError

from ...db import db
from ...models_ci import CiBuild, CiService

logger = logging.getLogger(__name__)


def _is_postgres() -> bool:
    return db.engine.dialect.name == "postgresql"


def enqueue(build: CiBuild, *, commit: bool = True) -> None:
    """Place a build on the queue. The caller owns creating the row."""
    build.status = "queued"
    build.queued_at = datetime.now(timezone.utc)
    build.queue_reason = None
    db.session.add(build)
    if commit:
        db.session.commit()


def claim_next(limit: int = 5, service_ids: Optional[List[int]] = None) -> List[int]:
    """Reserve up to ``limit`` queued build ids for this scheduler pass.

    Returns ids only, oldest first. The caller re-reads each build and decides
    whether a compatible runner is free; a build it cannot start is left
    queued (its row was never mutated) and reconsidered on the next tick.

    Builds whose service is already at its concurrency limit are not claimed
    at all - counting the ones queued ahead of them in the same service - so a
    busy service's backlog can no longer fill every claim and starve the
    others behind it (it used to: the oldest five were claimed, found
    blocked, put back unchanged, and claimed again next pass).

    On PostgreSQL the rows are locked for the remainder of the transaction, so
    the caller must commit or roll back promptly.
    """
    limit = max(1, min(int(limit), 50))
    candidates = _startable(limit, service_ids)
    if not candidates:
        return []
    if _is_postgres():
        try:
            return _lock_postgres(candidates)
        except OperationalError:
            # Lock timeout or a transient failure: yield this pass rather than
            # risk an unsynchronised fallback claim.
            logger.warning("CI queue claim contended; skipping this pass")
            db.session.rollback()
            return []
    return candidates


def _ranked_queue(service_ids: Optional[List[int]] = None):
    """Queued builds with their place in their service's queue, the service's
    running count and its limit - one statement, portable (window function and
    correlated subqueries, no FOR UPDATE: PostgreSQL refuses that beside a
    window, so locking is a second, plain statement)."""
    queued = CiBuild.__table__
    running = CiBuild.__table__.alias("running_builds")
    services = CiService.__table__
    running_count = (
        select(func.count())
        .select_from(running)
        .where(running.c.service_id == queued.c.service_id, running.c.status == "running")
        .scalar_subquery()
    )
    cap = func.coalesce(
        select(services.c.max_concurrent_builds)
        .where(services.c.id == queued.c.service_id)
        .scalar_subquery(),
        1,
    )
    place = func.row_number().over(
        partition_by=queued.c.service_id, order_by=(queued.c.queued_at, queued.c.id)
    )
    statement = select(
        queued.c.id,
        queued.c.service_id,
        queued.c.queued_at,
        queued.c.queue_reason,
        place.label("place"),
        running_count.label("running"),
        cap.label("cap"),
    ).where(queued.c.status == "queued")
    if service_ids:
        statement = statement.where(queued.c.service_id.in_(list(service_ids)))
    return statement.subquery()


def _within_limit(ranked):
    limit = case((ranked.c.cap < 1, 1), else_=ranked.c.cap)
    return ranked.c.place + ranked.c.running <= limit


def _startable(limit: int, service_ids: Optional[List[int]]) -> List[int]:
    ranked = _ranked_queue(service_ids)
    statement = (
        select(ranked.c.id)
        .where(_within_limit(ranked))
        .order_by(ranked.c.queued_at.asc(), ranked.c.id.asc())
        .limit(limit)
    )
    return [int(row[0]) for row in db.session.execute(statement)]


def _lock_postgres(candidates: List[int]) -> List[int]:
    statement = text(
        "SELECT id FROM ci_builds "
        "WHERE id = ANY(:ids) AND status = 'queued' "
        "ORDER BY queued_at ASC, id ASC "
        "FOR UPDATE SKIP LOCKED"
    )
    return [int(row[0]) for row in db.session.execute(statement, {"ids": list(candidates)})]


def explain_concurrency_waits(*, commit: bool = True) -> int:
    """Give a ``queue_reason`` to builds held back by their service's limit.

    They are never claimed (see :func:`claim_next`), so nothing else would say
    why they wait. Touches only builds with no reason yet, so a queue that is
    not moving costs one read a pass and no writes.
    """
    ranked = _ranked_queue()
    rows = db.session.execute(
        select(ranked.c.id, ranked.c.service_id, ranked.c.cap)
        .where(ranked.c.queue_reason.is_(None), ~_within_limit(ranked))
    ).all()
    if not rows:
        return 0
    names = {
        service_id: name
        for service_id, name in db.session.query(CiService.id, CiService.name).filter(
            CiService.id.in_({row.service_id for row in rows})
        )
    }
    for row in rows:
        reason = (
            f"Waiting: {names.get(row.service_id, 'this service')} allows "
            f"{max(1, int(row.cap or 1))} concurrent build(s)."
        )
        CiBuild.query.filter(CiBuild.id == row.id, CiBuild.queue_reason.is_(None)).update(
            {CiBuild.queue_reason: reason[:255]}, synchronize_session=False
        )
    if commit:
        db.session.commit()
    return len(rows)


def requeue(build: CiBuild, reason: str = "", *, commit: bool = True) -> None:
    """Return a build to the queue, recording why it is still waiting.

    ``queue_reason`` is shown verbatim in the UI, so a build that sits queued
    always explains itself instead of looking stuck.
    """
    build.status = "queued"
    build.queue_reason = (reason or "")[:255] or None
    build.runner_id = None
    db.session.add(build)
    if commit:
        db.session.commit()


def depth(service_id: Optional[int] = None) -> int:
    query = CiBuild.query.filter(CiBuild.status == "queued")
    if service_id is not None:
        query = query.filter(CiBuild.service_id == service_id)
    return int(query.count())


def running_count(service_id: Optional[int] = None) -> int:
    query = CiBuild.query.filter(CiBuild.status == "running")
    if service_id is not None:
        query = query.filter(CiBuild.service_id == service_id)
    return int(query.count())
