"""Persistence for merge checks — the gate between a pull request and a merge.

The shape of the feature, in one paragraph: Bitbucket posts a pull request to
KubeSight's inbound webhook; KubeSight runs the service's *merge check*
pipeline (lint, static analysis, dependency scan) on the PR's source commit;
when that build ends, the findings each tool reported are counted against a
quality gate, and the verdict is written back to Bitbucket as a commit build
status. Bitbucket's own branch restriction ("require successful builds before
merging") is what physically stops the merge — KubeSight states the verdict, the
host enforces it, which is the only arrangement that cannot be bypassed by
merging through a different client.

Three tables, and the split between them is deliberate:

    CiMergeCheckPolicy  the INSTALLATION's gate. One row, id 1.
    CiMergeCheckConfig  one SERVICE's webhook, events, and gate override.
    CiMergeCheck        one PULL REQUEST's evaluation, and its delivery.

A service inherits the installation policy unless it says otherwise, so raising
the org-wide bar is one edit rather than one per repository, and a service that
genuinely needs a different number still says so in its own row instead of
quietly diverging.

:class:`CiMergeCheck` is a record of a *verdict*, not of a build. The build is
an ordinary :class:`CiBuild` and is linked by id — everything about how it ran,
what it logged and why a stage failed already lives there and is not duplicated
here. What is here is what Bitbucket was told, and when, because that is the
part nobody can reconstruct afterwards.
"""

from __future__ import annotations

from datetime import datetime, timezone

from .db import db


def _now():
    return datetime.now(timezone.utc)


# The checks a merge gate can run, in the order they belong in a pipeline:
# cheapest and most local first, so a build that is going to fail on a lint
# error does not first spend four minutes downloading an NVD database.
MERGE_CHECK_TOOLS = ("eslint", "semgrep", "sonar", "dependency_check")

TOOL_LABELS = {
    "eslint": "ESLint",
    "semgrep": "Semgrep",
    "sonar": "SonarQube",
    "dependency_check": "Dependency-Check",
}

# Semgrep and SonarQube answer the same question — "what is wrong with this
# code?" — and differ in what they need to answer it. SonarQube is a SERVER
# somebody has to run, keep up and hold the history in; Semgrep is a binary in
# the build container that reads the checkout and exits. For a merge gate, which
# only ever wants a number for one commit, the server buys nothing: the history,
# the dashboards and the trend lines are what a server is for, and none of them
# are what blocks a merge.
#
# So an installation with no SonarQube can pick Semgrep and get the same gate.
# Both are offered rather than one replacing the other, because a site that
# already runs SonarQube should not be made to stop using it.

# What a merge check is doing, and what it concluded.
#
#   queued/running  the build is not finished; nothing has been told to anyone
#   passed/failed   the gate returned a verdict
#   error           the checks could not produce a verdict (a tool crashed, the
#                   build was cancelled, the service is not runnable). Kept
#                   distinct from `failed` on purpose: "we found 9 problems" and
#                   "we could not look" are different answers, and only the
#                   first of them is the developer's to fix.
#   skipped         the webhook was accepted but no checks were due — the event
#                   or the target branch is not one this service watches.
MERGE_CHECK_STATES = ("queued", "running", "passed", "failed", "error", "skipped")
TERMINAL_MERGE_CHECK_STATES = ("passed", "failed", "error", "skipped")

# Delivery of the verdict to the source host, tracked separately from the
# verdict itself. A gate that decided correctly and failed to say so is a
# different failure from a gate that decided wrongly, and it is the one that
# has to be retried.
DELIVERY_STATES = ("pending", "delivered", "failed", "not_applicable")

# How many times the engine will try to hand a verdict to Bitbucket before it
# gives up and leaves the row for a human. Bitbucket being briefly unreachable
# is ordinary; it being unreachable ten times is an outage somebody should see.
MAX_DELIVERY_ATTEMPTS = 8

# Severities, worst first. A threshold means "this and anything above it".
SEVERITIES = ("critical", "high", "medium", "low", "info")

# Pull request events a source host can announce. `created` and `updated` are
# the two that matter: `updated` is what fires when somebody pushes a fix to the
# branch, and without it the gate would judge a PR by its first commit forever.
MERGE_CHECK_EVENTS = (
    "pullrequest:created",
    "pullrequest:updated",
    "pullrequest:approved",
    "pullrequest:fulfilled",
)
DEFAULT_EVENTS = ("pullrequest:created", "pullrequest:updated")

# Where the gate numbers come from for one service.
GATE_MODES = ("inherit", "override")


class _GateColumns:
    """The gate's numbers, defined once and mixed into both tables.

    The installation policy and a service's override hold exactly the same
    fields — the only difference is that a NULL in the override means "use the
    policy's", while a NULL in the policy means "no cap". Declaring them twice
    by hand is how the two silently drift apart, so they are declared here.

    Every cap is nullable and every one of them is a *maximum that is allowed*:
    a gate of 5 passes on 5 problems and fails on 6.
    """

    # The cap across every tool's findings added together. The number somebody
    # means when they say "the quality gate is 5".
    max_total_problems = db.Column(db.Integer, nullable=True)
    # Per-tool caps, applied in addition to the total. NULL means this tool is
    # only bound by the total.
    max_eslint_problems = db.Column(db.Integer, nullable=True)
    max_semgrep_problems = db.Column(db.Integer, nullable=True)
    max_sonar_problems = db.Column(db.Integer, nullable=True)
    max_dependency_problems = db.Column(db.Integer, nullable=True)

    # Whether an ESLint warning counts as a problem. Off by default: a warning
    # is advice, and a gate that blocks a merge on advice is a gate people
    # learn to route around.
    eslint_count_warnings = db.Column(db.Boolean, nullable=True)
    # The floor each scanner counts from — findings BELOW this severity are
    # reported in the metrics and not counted against the gate.
    semgrep_min_severity = db.Column(db.String(16), nullable=True)
    sonar_min_severity = db.Column(db.String(16), nullable=True)
    dependency_min_severity = db.Column(db.String(16), nullable=True)

    # What happens when a check could not run at all. True blocks the merge —
    # the safe reading, and the default — and False lets it through with the
    # tool's failure recorded on the check.
    block_on_tool_error = db.Column(db.Boolean, nullable=True)


class CiMergeCheckPolicy(db.Model, _GateColumns):
    """The installation-wide quality gate. Exactly one row, id 1.

    Seeded with every cap NULL except the total, which starts unset as well:
    an installation that has not configured a gate does not have one, and a
    number invented here would start blocking merges nobody agreed to block.
    """

    __tablename__ = "ci_merge_check_policies"

    id = db.Column(db.Integer, primary_key=True)

    # Whether a service that has never been configured starts with merge checks
    # on. Off: a webhook that nobody has pointed at KubeSight does nothing
    # either way, and this keeps it that way after somebody points one.
    enabled_by_default = db.Column(db.Boolean, nullable=False, default=False)

    updated_by_user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=True)
    created_at = db.Column(db.DateTime(timezone=True), nullable=False, default=_now)
    updated_at = db.Column(
        db.DateTime(timezone=True), nullable=False, default=_now, onupdate=_now
    )

    updated_by = db.relationship("User", foreign_keys=[updated_by_user_id])


class CiMergeCheckConfig(db.Model, _GateColumns):
    """One service's merge gate: the webhook, what it watches, what it runs.

    The inbound secret is encrypted rather than hashed, unlike a runner token:
    it has to be readable to be shown again to whoever is pasting it into
    Bitbucket's webhook form, and it authenticates a caller rather than a
    person. Same treatment as the ticketing inbound secret, for the same reason.
    """

    __tablename__ = "ci_merge_check_configs"
    __table_args__ = (
        db.UniqueConstraint("service_id", name="uq_ci_merge_check_config_service"),
    )

    id = db.Column(db.Integer, primary_key=True)
    service_id = db.Column(
        db.Integer,
        db.ForeignKey("ci_services.id", ondelete="CASCADE"),
        nullable=False,
    )
    enabled = db.Column(db.Boolean, nullable=False, default=False)

    # The pipeline the checks run as. An ordinary CiPipeline on the same
    # service, not a default one, so the whole build engine — runners, caches,
    # logs, masking, restart safety — applies with no second execution path.
    # SET NULL rather than CASCADE: deleting the pipeline must disable the gate,
    # not delete the record of every verdict it ever gave.
    pipeline_id = db.Column(
        db.Integer, db.ForeignKey("ci_pipelines.id", ondelete="SET NULL"), nullable=True
    )
    # Which tools the generated pipeline includes, as a subset of
    # MERGE_CHECK_TOOLS. A repository with no JavaScript has no use for ESLint,
    # and a stage that always reports zero is worse than no stage: it reads as
    # evidence.
    tools = db.Column(db.JSON, nullable=False, default=list)

    # Per-tool command overrides: {"eslint": ["line", "line", ...]}.
    #
    # The generated script is a starting point, not a contract — a repository
    # with an unusual layout needs a different command and should not have to
    # give up the gate to get one. Kept HERE rather than only on the pipeline
    # stage because the stages are regenerated whenever the tools or the
    # severity floors change, and an edit that a settings change silently threw
    # away would be worse than not being editable at all.
    #
    # A tool absent from this dict uses the generated script. Clearing an entry
    # is how "reset to the default" works, and why it is a dict rather than a
    # full stage list.
    custom_commands = db.Column(db.JSON, nullable=False, default=dict)

    inbound_secret_encrypted = db.Column(db.Text, nullable=True)
    events = db.Column(db.JSON, nullable=False, default=list)
    # Destination branches this gate applies to, as fnmatch patterns
    # ("main", "release/*"). Empty means every branch — the useful default,
    # because a gate that silently ignores a branch is the failure mode here.
    target_branches = db.Column(db.JSON, nullable=False, default=list)

    gate_mode = db.Column(db.String(16), nullable=False, default="inherit")

    # The context name the build status is filed under in Bitbucket. Fixed per
    # service so a re-run REPLACES the previous verdict on that commit instead
    # of adding a second, contradictory one beside it.
    status_key = db.Column(db.String(40), nullable=False, default="KUBESIGHT-MERGE")
    # Whether the verdict is also posted as a pull request comment. The build
    # status is what gates the merge; the comment is what explains it to whoever
    # has to fix it, which is why it defaults on.
    post_comment = db.Column(db.Boolean, nullable=False, default=True)

    last_event_at = db.Column(db.DateTime(timezone=True), nullable=True)
    last_check_id = db.Column(db.Integer, nullable=True)
    # Why the last webhook delivery did nothing, when it did nothing. Shown
    # verbatim so "I sent it and nothing happened" has an answer on the page.
    last_error = db.Column(db.Text, nullable=True)

    created_by_user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=True)
    created_at = db.Column(db.DateTime(timezone=True), nullable=False, default=_now)
    updated_at = db.Column(
        db.DateTime(timezone=True), nullable=False, default=_now, onupdate=_now
    )

    # Declared from THIS side with a backref, so `models_ci` keeps knowing
    # nothing about merge checks while deleting a service still takes its
    # configuration with it through the ORM — not only through the database's
    # own ON DELETE, which SQLite does not enforce unless asked.
    service = db.relationship(
        "CiService",
        backref=db.backref(
            "merge_check_configs", cascade="all, delete-orphan", lazy="dynamic"
        ),
    )
    # No cascade: deleting the pipeline must disable the gate, never delete its
    # configuration and the history that hangs off it.
    pipeline = db.relationship("CiPipeline", backref=db.backref("merge_check_configs"))
    created_by = db.relationship("User", foreign_keys=[created_by_user_id])


class CiMergeCheck(db.Model):
    """One pull request, judged once.

    Re-delivered rather than re-decided: ``delivery_state`` is what the engine
    retries, and the verdict itself is written once and never revised. A new
    push to the branch is a new row, because it is a new commit and Bitbucket
    files build statuses per commit.
    """

    __tablename__ = "ci_merge_checks"
    __table_args__ = (
        db.Index("ix_ci_merge_check_service_created", "service_id", "created_at"),
        db.Index("ix_ci_merge_check_state", "state"),
        db.Index("ix_ci_merge_check_delivery", "delivery_state"),
        db.Index("ix_ci_merge_check_build", "build_id"),
    )

    id = db.Column(db.Integer, primary_key=True)
    service_id = db.Column(
        db.Integer,
        db.ForeignKey("ci_services.id", ondelete="CASCADE"),
        nullable=False,
    )
    config_id = db.Column(
        db.Integer,
        db.ForeignKey("ci_merge_check_configs.id", ondelete="SET NULL"),
        nullable=True,
    )
    build_id = db.Column(
        db.Integer, db.ForeignKey("ci_builds.id", ondelete="SET NULL"), nullable=True
    )

    provider = db.Column(db.String(32), nullable=False, default="bitbucket")
    event = db.Column(db.String(48), nullable=False, default="pullrequest:created")
    # Bitbucket's own PR id, as a string: it is an identifier, not a quantity,
    # and another provider's will not be an integer.
    pull_request_id = db.Column(db.String(64), nullable=True)
    pull_request_url = db.Column(db.String(1024), nullable=True)
    title = db.Column(db.Text, nullable=True)
    author = db.Column(db.String(255), nullable=True)
    source_branch = db.Column(db.String(255), nullable=True)
    destination_branch = db.Column(db.String(255), nullable=True)
    commit_sha = db.Column(db.String(64), nullable=True)

    state = db.Column(db.String(16), nullable=False, default="queued", index=True)
    # The one-word answer Bitbucket is given: allowed | blocked | unknown.
    verdict = db.Column(db.String(16), nullable=True)
    # What each tool reported: {"eslint": {"status": "ok", "problems": 3, ...}}.
    # Whole document, written once, read whole — the same reasoning as
    # CiPipeline.parameters.
    metrics = db.Column(db.JSON, nullable=False, default=dict)
    # The gate as it was RESOLVED for this check, not a pointer to the policy.
    # A gate that is later relaxed must not rewrite the record of a merge that
    # was blocked under the old one.
    gate = db.Column(db.JSON, nullable=False, default=dict)
    # One sentence per reason the gate blocked, in the order they are shown.
    reasons = db.Column(db.JSON, nullable=False, default=list)
    total_problems = db.Column(db.Integer, nullable=True)

    delivery_state = db.Column(db.String(20), nullable=False, default="pending")
    delivery_attempts = db.Column(db.Integer, nullable=False, default=0)
    delivery_error = db.Column(db.Text, nullable=True)
    delivered_at = db.Column(db.DateTime(timezone=True), nullable=True)
    # Set when a delivery fails, so the retry backs off instead of hammering a
    # host that is already struggling.
    next_delivery_at = db.Column(db.DateTime(timezone=True), nullable=True)

    error = db.Column(db.Text, nullable=True)
    created_at = db.Column(
        db.DateTime(timezone=True), nullable=False, default=_now, index=True
    )
    evaluated_at = db.Column(db.DateTime(timezone=True), nullable=True)

    service = db.relationship(
        "CiService",
        backref=db.backref("merge_checks", cascade="all, delete-orphan", lazy="dynamic"),
    )
    # A verdict outlives both the configuration that produced it and the build
    # that ran it: what a merge was allowed or blocked on is a record, and
    # reconfiguring the gate must not erase it.
    config = db.relationship(
        "CiMergeCheckConfig", backref=db.backref("checks", lazy="dynamic")
    )
    build = db.relationship("CiBuild", backref=db.backref("merge_checks", lazy="dynamic"))
