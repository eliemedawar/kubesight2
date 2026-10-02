"""Scheduled builds: a cron expression that triggers an ordinary build.

Bolted onto the CI domain the way merge checks are: the table references
``ci_services``, but nothing on the build path reads it. A scheduled build is a
normal :class:`CiBuild` with ``trigger_type='schedule'`` and the schedule's name
in its snapshot — runners, caches, logs, masking and restart safety apply with
no second execution path.
"""

from __future__ import annotations

from datetime import datetime, timezone

from .db import db


def _now():
    return datetime.now(timezone.utc)


# What happened the last time a schedule came due.
#   triggered — a build was queued (``last_build_id`` names it)
#   skipped   — deliberately not run: the previous one was still going, or the
#               service is not active
#   failed    — it should have run and could not; ``last_error`` says why
SCHEDULE_OUTCOMES = ("triggered", "skipped", "failed")


class CiSchedule(db.Model):
    """When a service builds without anybody pressing Run build.

    ``next_run_at`` is both the answer to "when does it run next" and the
    claim ticket that keeps it from running twice: the engine fires a schedule
    only by moving that column forward with a compare-and-set UPDATE, so of
    any number of workers that see the same due row exactly one wins. NULL
    means "not armed" — a disabled schedule has no next run.
    """

    __tablename__ = "ci_schedules"
    __table_args__ = (
        db.UniqueConstraint("service_id", "name", name="uq_ci_schedule_service_name"),
        # The engine's only query on this table: enabled rows that are due.
        db.Index("ix_ci_schedule_due", "enabled", "next_run_at"),
    )

    id = db.Column(db.Integer, primary_key=True)
    service_id = db.Column(
        db.Integer,
        db.ForeignKey("ci_services.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    # NULL is "the service's default build pipeline, whichever that is when the
    # schedule fires". Deliberately NOT a foreign key: a pipeline deleted under
    # a schedule must leave the schedule pointing at the id it was given, so it
    # can say "the pipeline this ran no longer exists" — SET NULL would quietly
    # turn it into a schedule for some other pipeline.
    pipeline_id = db.Column(db.Integer, nullable=True)
    name = db.Column(db.String(120), nullable=False)
    cron = db.Column(db.String(120), nullable=False)
    # IANA name. The cron is evaluated on this zone's wall clock, so "02:00"
    # stays 02:00 for the people who wrote it through every DST change.
    timezone = db.Column(db.String(64), nullable=False, default="UTC")
    # What to check out. NULL branch is the service's default branch at fire
    # time; ``ref_type`` says whether ``branch`` names a branch or a tag, the
    # same distinction Run build makes.
    branch = db.Column(db.String(255), nullable=True)
    ref_type = db.Column(db.String(8), nullable=False, default="branch")
    # Build-input values, checked against the pipeline's declared parameters
    # on save and again by the engine when the build is triggered.
    variables = db.Column(db.JSON, nullable=False, default=dict)
    enabled = db.Column(db.Boolean, nullable=False, default=True)
    skip_if_running = db.Column(db.Boolean, nullable=False, default=True)

    next_run_at = db.Column(db.DateTime(timezone=True), nullable=True)
    last_run_at = db.Column(db.DateTime(timezone=True), nullable=True)
    last_build_id = db.Column(
        db.Integer, db.ForeignKey("ci_builds.id", ondelete="SET NULL"), nullable=True
    )
    last_outcome = db.Column(db.String(16), nullable=True)
    last_error = db.Column(db.Text, nullable=True)

    created_by_user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=True)
    # Whoever last saved it — and so whose rights the build runs with, the
    # same rule a Deploy stage's target follows. Re-checked at every fire.
    updated_by_user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=True)
    created_at = db.Column(db.DateTime(timezone=True), nullable=False, default=_now)
    updated_at = db.Column(
        db.DateTime(timezone=True), nullable=False, default=_now, onupdate=_now
    )

    # Declared from THIS side with a backref, so models_ci keeps knowing
    # nothing about schedules — and a deleted service takes its schedules with
    # it through the ORM, not only through the database's ON DELETE, which
    # SQLite does not enforce unless asked.
    service = db.relationship(
        "CiService",
        backref=db.backref("schedules", cascade="all, delete-orphan", lazy="dynamic"),
    )
    last_build = db.relationship("CiBuild", foreign_keys=[last_build_id])
    created_by = db.relationship("User", foreign_keys=[created_by_user_id])
    updated_by = db.relationship("User", foreign_keys=[updated_by_user_id])
