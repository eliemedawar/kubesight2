"""Upgrade Center job records.

One row per automated upgrade started from the Upgrade Center. The API shape
(``/api/upgrades/jobs/<id>``) is the ``payload`` column verbatim; the other
columns exist so jobs can be found and reconciled without parsing JSON.
"""

from __future__ import annotations

from datetime import datetime, timezone

from .db import db


def _now():
    return datetime.now(timezone.utc)


class UpgradeJob(db.Model):
    __tablename__ = "upgrade_jobs"

    id = db.Column(db.Integer, primary_key=True)
    job_id = db.Column(db.String(64), nullable=False, unique=True, index=True)
    cluster_id = db.Column(db.String(120), nullable=False, index=True)
    target_version = db.Column(db.String(64), nullable=True)
    provider = db.Column(db.String(64), nullable=True)
    # queued | running | completed | failed
    status = db.Column(db.String(32), nullable=False, default="queued", index=True)
    # hostname:pid of the process executing the job — informational, for the
    # operator reading an "interrupted" job.
    owner = db.Column(db.String(255), nullable=True)
    # Bumped while the worker thread is alive; a queued/running row whose
    # heartbeat is stale belongs to a process that no longer exists.
    heartbeat_at = db.Column(db.DateTime(timezone=True), nullable=True)
    payload = db.Column(db.JSON, nullable=False, default=dict)
    created_at = db.Column(db.DateTime(timezone=True), nullable=False, default=_now)
    updated_at = db.Column(
        db.DateTime(timezone=True), nullable=False, default=_now, onupdate=_now
    )


class UpgradePrecheck(db.Model):
    """The latest readiness checks per cluster and target.

    The fleet view shows "1 blocker · 4 warnings, checked 4 min ago" for every
    cluster without re-running anything, and an upgrade request records which
    result (and which waived blockers) it was approved against."""

    __tablename__ = "upgrade_prechecks"

    id = db.Column(db.Integer, primary_key=True)
    cluster_id = db.Column(db.String(120), nullable=False, index=True)
    target_version = db.Column(db.String(64), nullable=True)
    current_version = db.Column(db.String(64), nullable=True)
    can_upgrade = db.Column(db.Boolean, nullable=False, default=False)
    # {"passed": n, "warning": n, "failed": n}
    summary = db.Column(db.JSON, nullable=False, default=dict)
    # The checks list plus baseline / nodes, as the precheck returned them.
    payload = db.Column(db.JSON, nullable=False, default=dict)
    created_by = db.Column(db.String(255), nullable=True)
    created_at = db.Column(db.DateTime(timezone=True), nullable=False, default=_now, index=True)
