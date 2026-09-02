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

from sqlalchemy import text
from sqlalchemy.exc import OperationalError

from ...db import db
from ...models_ci import CiBuild

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

    Returns ids only. The caller re-reads each build and decides whether a
    compatible runner is free; a build it cannot start is left queued (its row
    was never mutated) and reconsidered on the next tick.

    On PostgreSQL the rows are locked for the remainder of the transaction, so
    the caller must commit or roll back promptly.
    """
    limit = max(1, min(int(limit), 50))
    if _is_postgres():
        try:
            return _claim_postgres(limit, service_ids)
        except OperationalError:
            # Lock timeout or a transient failure: yield this pass rather than
            # risk an unsynchronised fallback claim.
            logger.warning("CI queue claim contended; skipping this pass")
            db.session.rollback()
            return []
    return _claim_generic(limit, service_ids)


def _claim_postgres(limit: int, service_ids: Optional[List[int]]) -> List[int]:
    clause = ""
    params = {"limit": limit}
    if service_ids:
        clause = " AND service_id = ANY(:service_ids)"
        params["service_ids"] = list(service_ids)
    statement = text(
        "SELECT id FROM ci_builds "
        "WHERE status = 'queued'" + clause + " "
        "ORDER BY queued_at ASC, id ASC "
        "LIMIT :limit "
        "FOR UPDATE SKIP LOCKED"
    )
    return [int(row[0]) for row in db.session.execute(statement, params)]


def _claim_generic(limit: int, service_ids: Optional[List[int]]) -> List[int]:
    query = CiBuild.query.filter(CiBuild.status == "queued")
    if service_ids:
        query = query.filter(CiBuild.service_id.in_(service_ids))
    rows = (
        query.order_by(CiBuild.queued_at.asc(), CiBuild.id.asc()).limit(limit).all()
    )
    return [row.id for row in rows]


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
