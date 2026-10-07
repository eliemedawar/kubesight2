"""Promotion rules: an ordered ladder of environments every image climbs.

Dev → SIT → UAT → Pre-prod. An environment is a set of namespaces (or whole
clusters) — the binding is the only setup there is; nothing else in KubeSight
stores "which environment is this" as free text any more for promotion's sake.

The unit that is promoted is the IMAGE, because images are built once: the
exact ``repository:tag`` that ran in SIT is what may go to UAT. So the rule
needs no notion of "the same application" — the ledger below remembers which
images were seen running healthy in which environment, and a deploy into
environment N is allowed when its images are in the ledger for N−1 (or were
in N already, which is what a rollback or a redeploy is).

Nothing on the deploy paths owns these tables: ``services/promotion_service``
reads them from the one check every path calls.
"""

from __future__ import annotations

from datetime import datetime, timezone

from .db import db


def _now():
    return datetime.now(timezone.utc)


# How strictly an environment holds the line.
#   off     — no rule; anything may be deployed into it
#   warn    — the deploy goes ahead, but it is flagged and recorded
#   enforce — a deploy whose images have not passed the previous environment
#             is refused (an approved exception is the only way past)
PROMOTION_MODES = ("off", "warn", "enforce")

# The whole-cluster binding: every namespace of the cluster that has no
# namespace binding of its own.
WHOLE_CLUSTER = "*"


class PromotionEnvironment(db.Model):
    """One rung of the ladder. ``position`` orders them (lowest = entry)."""

    __tablename__ = "promotion_environments"

    id = db.Column(db.Integer, primary_key=True)
    key = db.Column(db.String(40), nullable=False, unique=True)
    name = db.Column(db.String(80), nullable=False)
    position = db.Column(db.Integer, nullable=False, default=0)
    mode = db.Column(db.String(12), nullable=False, default="warn")
    # How long an image must have run healthy in THIS environment before it may
    # go to the next one. 0 = as soon as it is healthy.
    min_soak_minutes = db.Column(db.Integer, nullable=False, default=0)
    description = db.Column(db.Text, nullable=True)
    # The release schedule of the hop INTO this environment (from the one
    # below): {"enabled": bool, "days": {"mon": ["09:30", …], …},
    # "cutoffMinutes": 15, "timezone": "Asia/Beirut"}. NULL = on demand only.
    schedule = db.Column(db.JSON, nullable=True)
    # Scheduled releases run as the person who saved the schedule (they need
    # apps:deploy) — the way a CI Deploy stage runs as whoever authorised it.
    schedule_owner_id = db.Column(db.Integer, nullable=True)
    schedule_owner = db.Column(db.String(120), nullable=True)
    created_at = db.Column(db.DateTime(timezone=True), nullable=False, default=_now)
    updated_at = db.Column(db.DateTime(timezone=True), nullable=False, default=_now, onupdate=_now)

    bindings = db.relationship(
        "PromotionBinding",
        backref="environment",
        cascade="all, delete-orphan",
        order_by="PromotionBinding.cluster_id, PromotionBinding.namespace",
    )


class PromotionBinding(db.Model):
    """A namespace (or a whole cluster) that belongs to one environment."""

    __tablename__ = "promotion_bindings"
    __table_args__ = (
        # A namespace is in one environment at most — otherwise "where is this
        # deploy going" has two answers.
        db.UniqueConstraint("cluster_id", "namespace", name="uq_promotion_binding_target"),
    )

    id = db.Column(db.Integer, primary_key=True)
    environment_id = db.Column(
        db.Integer,
        db.ForeignKey("promotion_environments.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    cluster_id = db.Column(db.String(120), nullable=False)
    # A namespace name, a glob pattern ("*-sit", "uat-*"), or WHOLE_CLUSTER.
    # Precedence when several match: an exact name, then the most specific
    # pattern (most literal characters), then the whole cluster.
    namespace = db.Column(db.String(253), nullable=False)
    created_by = db.Column(db.String(120), nullable=True)
    created_at = db.Column(db.DateTime(timezone=True), nullable=False, default=_now)


class PromotionRecord(db.Model):
    """An image that ran healthy in an environment — the promotion ledger.

    One row per (environment, image). ``first_healthy_at`` is when it was first
    seen fully rolled out there (the soak clock); ``last_seen_at`` moves every
    time it is seen again. Rows are written by the deploy paths when a rollout
    they watched succeeds, and by the observer that scans the bound namespaces,
    so images deployed outside KubeSight (Jenkins, kubectl) are recorded too.
    """

    __tablename__ = "promotion_records"
    __table_args__ = (
        db.UniqueConstraint("environment_id", "image", name="uq_promotion_record_env_image"),
        db.Index("ix_promotion_record_repository", "repository"),
    )

    id = db.Column(db.Integer, primary_key=True)
    environment_id = db.Column(
        db.Integer,
        db.ForeignKey("promotion_environments.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    # Normalised ``repository:tag`` (no digest) — what the rule compares.
    image = db.Column(db.String(512), nullable=False)
    repository = db.Column(db.String(512), nullable=False)
    tag = db.Column(db.String(128), nullable=True)
    digest = db.Column(db.String(128), nullable=True)
    # Where it was last seen.
    cluster_id = db.Column(db.String(120), nullable=True)
    namespace = db.Column(db.String(253), nullable=True)
    workload_kind = db.Column(db.String(32), nullable=True)
    workload_name = db.Column(db.String(253), nullable=True)
    # observed | ci_deploy | automation | bundle | promotion
    source = db.Column(db.String(24), nullable=False, default="observed")
    deployed_by = db.Column(db.String(120), nullable=True)
    first_healthy_at = db.Column(db.DateTime(timezone=True), nullable=False, default=_now)
    last_seen_at = db.Column(db.DateTime(timezone=True), nullable=False, default=_now)


# What one activity row says happened.
#   promoted            — an image was sent to the next environment from the board
#   blocked             — a deploy was refused by an enforcing environment
#   warned              — a deploy went ahead in a warn-mode environment without
#                         having passed the previous one
#   exception_requested — someone asked approvers to let a blocked deploy through
#   exception_applied   — an approved exception was deployed
PROMOTION_EVENT_KINDS = (
    "promoted",
    "blocked",
    "warned",
    "exception_requested",
    "exception_applied",
)


class PromotionEvent(db.Model):
    """The promotion activity feed: who moved what where, and what was stopped."""

    __tablename__ = "promotion_events"
    __table_args__ = (db.Index("ix_promotion_event_at", "created_at"),)

    id = db.Column(db.Integer, primary_key=True)
    kind = db.Column(db.String(24), nullable=False)
    environment_id = db.Column(db.Integer, nullable=True, index=True)
    environment_name = db.Column(db.String(80), nullable=True)
    from_environment_name = db.Column(db.String(80), nullable=True)
    images = db.Column(db.JSON, nullable=False, default=list)
    cluster_id = db.Column(db.String(120), nullable=True)
    namespace = db.Column(db.String(253), nullable=True)
    workload_name = db.Column(db.String(253), nullable=True)
    # ui | promote | ci | ticket | helm | bundle | mcp
    path = db.Column(db.String(16), nullable=True)
    actor = db.Column(db.String(120), nullable=True)
    message = db.Column(db.Text, nullable=True)
    bundle_id = db.Column(db.Integer, nullable=True)
    # The release this event belongs to, when it came from one.
    release_id = db.Column(db.Integer, nullable=True, index=True)
    created_at = db.Column(db.DateTime(timezone=True), nullable=False, default=_now)


class PromotionRelease(db.Model):
    """A set of applications promoted into one environment together.

    What a person reviews and submits from the Promote view: "UAT drop · 6 Oct",
    eight applications, one change bundle where the clusters need approval.
    ``items`` keeps, per application, the version change and what happened to
    each workload; a workload waiting in a bundle reads its fate from the
    bundle when the release is shown, so the record never goes stale.
    """

    __tablename__ = "promotion_releases"
    __table_args__ = (db.Index("ix_promotion_release_created", "created_at"),)

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(160), nullable=False)
    # A change/ticket reference, free text (CHG-2291, a Jira key…).
    reference = db.Column(db.String(120), nullable=True)
    note = db.Column(db.Text, nullable=True)
    # promotion — every application passed the environment below
    # exception  — at least one skips it, sent to approvers with a reason
    kind = db.Column(db.String(16), nullable=False, default="promotion")
    exception_reason = db.Column(db.Text, nullable=True)
    environment_id = db.Column(db.Integer, nullable=True, index=True)
    environment_name = db.Column(db.String(80), nullable=True)
    from_environment_name = db.Column(db.String(80), nullable=True)
    actor = db.Column(db.String(120), nullable=True)
    # [{repository, name, image, tag, fromTags, exception, targets: [{clusterId,
    #   namespace, kind, name, status, message, bundleId}]}]
    items = db.Column(db.JSON, nullable=False, default=list)
    bundle_ids = db.Column(db.JSON, nullable=False, default=list)
    # "UAT-1006-2" — the departure it belongs to (or a manual one), and the
    # dated release version people quote ("2026.10.06.2").
    code = db.Column(db.String(40), nullable=True, index=True)
    version = db.Column(db.String(40), nullable=True)
    # When it deploys. NULL = immediately (a manual promotion).
    departs_at = db.Column(db.DateTime(timezone=True), nullable=True)
    created_at = db.Column(db.DateTime(timezone=True), nullable=False, default=_now)


# What a person did to one scheduled departure before it ran.
#   open    — nothing special (the row exists for its exclusions)
#   held    — paused: nothing closes or deploys until released
#   skipped — this departure does not run; its applications wait for the next
#   closed  — the cut-off passed (or someone promoted early): ``release_id`` is
#             the release it became
DEPARTURE_STATES = ("open", "held", "skipped", "closed")


class PromotionDeparture(db.Model):
    """One scheduled departure of a hop, once anybody has touched it.

    Departures themselves are computed from the schedule; a row exists only
    when one is held, skipped, has applications moved off it, or has run.
    """

    __tablename__ = "promotion_departures"
    __table_args__ = (
        db.UniqueConstraint("environment_id", "departs_at", name="uq_promotion_departure_slot"),
    )

    id = db.Column(db.Integer, primary_key=True)
    environment_id = db.Column(
        db.Integer,
        db.ForeignKey("promotion_environments.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    departs_at = db.Column(db.DateTime(timezone=True), nullable=False)
    state = db.Column(db.String(12), nullable=False, default="open")
    # Image repositories moved off this departure to the next one.
    excluded = db.Column(db.JSON, nullable=False, default=list)
    release_id = db.Column(db.Integer, nullable=True)
    note = db.Column(db.Text, nullable=True)
    updated_by = db.Column(db.String(120), nullable=True)
    updated_at = db.Column(db.DateTime(timezone=True), nullable=False, default=_now, onupdate=_now)


class PromotionPolicy(db.Model):
    """The one row of ladder-wide settings."""

    __tablename__ = "promotion_policy"

    id = db.Column(db.Integer, primary_key=True)
    # Image patterns the ladder ignores — third-party images (redis, nginx)
    # that are pulled, not built, so they never "pass" anything. fnmatch globs
    # against the normalised repository, e.g. "redis", "docker.io/*", "*/bitnami/*".
    exempt_images = db.Column(db.JSON, nullable=False, default=list)
    # Refuse mutable tags (":latest", no tag) past the entry environment: the
    # same tag can name different builds, so "it passed SIT" means nothing.
    require_versioned_tags = db.Column(db.Boolean, nullable=False, default=True)
    updated_by = db.Column(db.String(120), nullable=True)
    updated_at = db.Column(db.DateTime(timezone=True), nullable=False, default=_now, onupdate=_now)
