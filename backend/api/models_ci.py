"""Persistence model for native KubeSight CI.

The CI domain is deliberately self-contained: a service is registered here with
its own repository configuration, pipeline, builds and artifacts, and nothing on
the build path reads Application Intelligence, Hermes, or any AI functionality.
The optional links out of :class:`CiService` (blueprint, intelligence
application, inventory catalog entry, registry connection) are all nullable and
purely informational — CI runs correctly with every one of them unset.

Vocabulary:
    CiService   WHAT we build   — an application and where its source lives
    CiPipeline  HOW we build it — ordered stages
    CiRunner    WHERE we build  — capability-matched execution target
    CiArtifact  WHAT came out   — image, jar, apk, report, ...
"""

from __future__ import annotations

from datetime import datetime, timezone

from .db import db


def _now():
    return datetime.now(timezone.utc)


# Application types a service can declare. Drives the generated default,
# customization kit and card icon; never gates execution.
APPLICATION_TYPES = (
    "container",
    "java_maven",
    "java_gradle",
    # Legacy. Services registered before Java was split by build tool carry
    # this; it resolves to the Maven kit. Kept valid so those rows keep working
    # and are not silently downgraded to the generic template.
    "java",
    "node",
    "python",
    "android",
    "ios",
    "flutter",
    "generic",
)

SERVICE_STATUSES = ("active", "paused", "archived")
CRITICALITIES = ("low", "medium", "high", "critical")

# Stage kinds. Only ``checkout`` and ``command`` execute in Phase 1; the rest are
# recognised and validated now so a pipeline authored today stays valid when
# their executors land.
STAGE_TYPES = (
    "checkout",
    "command",
    "container_image",
    "publish_artifact",
    "scan",
)

# --- Image scanning ---------------------------------------------------------
# A container_image stage builds, scans and pushes in ONE stage, in that order.
# The scan is not a stage of its own on purpose: a separate stage could be
# reordered, disabled or deleted and the gate would silently disappear, while
# the image it was meant to guard still reached the registry. Here the push is
# physically downstream of the verdict in the same shell script — there is no
# arrangement of the pipeline that pushes an unscanned image.
IMAGE_SCANNERS = ("trivy",)

# Ordered worst-first. A threshold means "this severity and anything above it".
IMAGE_SCAN_SEVERITIES = ("critical", "high", "medium", "low")

# What a finding at or above the threshold does. ``block`` fails the stage
# before the push; ``warn`` records the report and pushes anyway. There is no
# third option: a gate that neither blocks nor reports is not a gate.
IMAGE_SCAN_ON_FAIL = ("block", "warn")

# What a pipeline is FOR. A service's pipelines used to be interchangeable, and
# "the default one" was simply the first — which stops being safe the moment
# something other than Run Build owns a pipeline. A merge check pipeline runs
# ESLint and a dependency scan and produces no artifact; running it because it
# happened to be the only row would be a silent, confusing wrong answer.
#
# Existing rows read as 'build', which is what they are.
PIPELINE_PURPOSES = ("build", "merge_check")

RUNNER_TYPES = ("kubernetes", "agent_linux", "agent_macos", "ssh_linux", "mock")
RUNNER_STATUSES = ("online", "offline", "draining", "disabled")

# Terminal build states are the last four.
BUILD_STATUSES = ("queued", "running", "success", "failed", "cancelled", "timeout")
TERMINAL_BUILD_STATUSES = ("success", "failed", "cancelled", "timeout")
STAGE_STATUSES = (
    "pending",
    "running",
    "success",
    "failed",
    "skipped",
    "cancelled",
    "timeout",
)

TRIGGER_TYPES = ("manual", "retry", "api", "webhook", "automation")

ARTIFACT_TYPES = (
    "container-image",
    "jar",
    "war",
    "zip",
    "binary",
    "apk",
    "aab",
    "ipa",
    "test-report",
    "coverage-report",
    "sbom",
    # The vulnerability report a container_image stage's scan produced. Uploaded
    # whether the scan passed or blocked the push — a blocked build is exactly
    # when somebody wants to read why.
    "scan-report",
)

ARTIFACT_BACKENDS = ("local", "registry", "s3", "nexus_raw")

# --- Assisted configuration -------------------------------------------------
# Where a service's application profile came from. NULL means "nobody has said"
# — every service registered before this existed, and the reason every one of
# these columns is nullable.
PROFILE_SOURCES = ("hermes", "manual", "derived")

# How much KubeSight knows about what this repository IS.
ANALYSIS_STATES = ("not_analyzed", "analyzing", "analyzed", "partial", "failed", "cancelled")
# ... and how far the pipeline proposal for it got. Tracked separately because
# the two genuinely diverge: a repository can be understood perfectly and still
# produce a pipeline that fails validation.
PIPELINE_PROPOSAL_STATES = (
    "not_generated",
    "generating",
    "valid",
    "invalid",
    "accepted",
    "user_modified",
)
# What a proposal asks the user for. `registry` is not a secret: KubeSight keeps
# registry credentials on a RegistryConnection, so the answer to "what registry"
# is a link to one, not two strings a stage would never read.
REQUIRED_INPUT_KINDS = ("parameter", "secret", "registry")


class CiService(db.Model):
    """One buildable application in the Service Catalog.

    Source configuration is nullable: a service is registered first and its
    repository connected afterwards, so the catalog can show "source not
    configured" rather than refusing to create the record. :meth:`source_ready`
    is what gates Run Build.
    """

    __tablename__ = "ci_services"
    __table_args__ = (
        db.Index("ix_ci_service_status_name", "status", "name"),
    )

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(160), nullable=False)
    slug = db.Column(db.String(180), unique=True, nullable=False, index=True)
    description = db.Column(db.Text, nullable=True)
    owner_team = db.Column(db.String(255), nullable=True)
    criticality = db.Column(db.String(32), nullable=True)
    application_type = db.Column(db.String(32), nullable=False, default="generic")
    status = db.Column(db.String(16), nullable=False, default="active", index=True)

    # --- Source (Bitbucket in Phase 1; the provider column keeps GitLab/GitHub
    # a data change rather than a schema change) -----------------------------
    repository_provider = db.Column(db.String(32), nullable=False, default="bitbucket")
    repository_url = db.Column(db.String(1024), nullable=True)
    repository_workspace = db.Column(db.String(255), nullable=True)
    repository_name = db.Column(db.String(255), nullable=True)
    default_branch = db.Column(db.String(255), nullable=False, default="main")
    # Monorepo support: every stage's working directory is resolved relative to
    # this, so one repository can back several services.
    working_directory = db.Column(db.String(512), nullable=True)
    # Credentials never live on the service row — only a reference to the shared
    # profile store.
    credential_profile_id = db.Column(
        db.Integer, db.ForeignKey("bitbucket_credential_profiles.id"), nullable=True
    )

    # An inline Dockerfile, for repositories that do not carry one. When set it
    # is what container_image stages build; when empty the Dockerfile in the
    # checkout is used, as before. Kept on the service rather than a stage
    # because it describes the application, not one step of one pipeline.
    dockerfile = db.Column(db.Text, nullable=True)

    # --- What this application IS, in detail --------------------------------
    # ``application_type`` above stays the discriminator everything already
    # reads (templates, fallback pipelines, icons, readiness). This is the
    # structured truth it is DERIVED from: language and version, framework and
    # version, build system, packaging, and the evidence for each. Null on every
    # service registered before assisted configuration existed, which is exactly
    # what "not analyzed" looks like — nothing changes for those.
    #
    # One JSON document rather than a column per field: it is written whole,
    # read whole, owned by one row, and ``CiPipeline.parameters`` already
    # established the pattern. A column per detected attribute would be a
    # migration every time a language gains one.
    application_profile = db.Column(db.JSON, nullable=True)
    profile_source = db.Column(db.String(16), nullable=True)
    analysis_state = db.Column(db.String(16), nullable=True)

    # --- Optional links. All nullable; none is read on the build path. -------
    registry_connection_id = db.Column(
        db.Integer, db.ForeignKey("registry_connections.id"), nullable=True
    )
    blueprint_id = db.Column(
        db.Integer, db.ForeignKey("service_blueprints.id"), nullable=True
    )
    intelligence_application_id = db.Column(
        db.Integer, db.ForeignKey("intelligence_applications.id"), nullable=True
    )
    catalog_entry_id = db.Column(
        db.Integer, db.ForeignKey("app_catalog_entries.id"), nullable=True
    )

    max_concurrent_builds = db.Column(db.Integer, nullable=False, default=1)
    # Monotonic per-service build number. Incremented under the same transaction
    # that inserts the build, with UNIQUE(service_id, number) as the backstop.
    next_build_number = db.Column(db.Integer, nullable=False, default=1)

    created_by_user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=True)
    created_at = db.Column(db.DateTime(timezone=True), nullable=False, default=_now)
    updated_at = db.Column(
        db.DateTime(timezone=True), nullable=False, default=_now, onupdate=_now
    )

    credential_profile = db.relationship("BitbucketCredentialProfile")
    created_by = db.relationship("User", foreign_keys=[created_by_user_id])
    pipelines = db.relationship(
        "CiPipeline",
        back_populates="service",
        cascade="all, delete-orphan",
        order_by="CiPipeline.id",
    )
    builds = db.relationship(
        "CiBuild",
        back_populates="service",
        cascade="all, delete-orphan",
        lazy="dynamic",
    )
    artifacts = db.relationship(
        "CiArtifact",
        back_populates="service",
        cascade="all, delete-orphan",
        lazy="dynamic",
    )
    secrets = db.relationship(
        "CiSecret",
        back_populates="service",
        cascade="all, delete-orphan",
        lazy="dynamic",
    )
    analyses = db.relationship(
        "CiRepositoryAnalysis",
        back_populates="service",
        cascade="all, delete-orphan",
        lazy="dynamic",
    )

    def source_ready(self) -> bool:
        """Whether this service has enough source configuration to build."""
        return bool(self.repository_url and self.credential_profile_id)

    def build_pipelines(self):
        """The pipelines Run Build may choose from — never a merge check one."""
        return [
            pipeline
            for pipeline in self.pipelines
            if (pipeline.purpose or "build") == "build"
        ]

    def default_pipeline(self):
        buildable = self.build_pipelines()
        for pipeline in buildable:
            if pipeline.is_default:
                return pipeline
        # The fallback is deliberately over `buildable` rather than over every
        # pipeline: a service whose only saved pipeline is its merge check one
        # still builds from the generated default, which is what it did before
        # merge checks existed.
        return buildable[0] if buildable else None


class CiPipeline(db.Model):
    """An ordered stage list belonging to one service.

    ``version`` is bumped on every save and copied into each build's
    ``pipeline_snapshot``, so editing a pipeline never rewrites the history of
    builds that already ran.
    """

    __tablename__ = "ci_pipelines"
    __table_args__ = (
        db.UniqueConstraint("service_id", "name", name="uq_ci_pipeline_service_name"),
        db.Index("ix_ci_pipeline_service", "service_id"),
    )

    id = db.Column(db.Integer, primary_key=True)
    service_id = db.Column(
        db.Integer,
        db.ForeignKey("ci_services.id", ondelete="CASCADE"),
        nullable=False,
    )
    name = db.Column(db.String(120), nullable=False, default="default")
    description = db.Column(db.Text, nullable=True)
    is_default = db.Column(db.Boolean, nullable=False, default=True)
    enabled = db.Column(db.Boolean, nullable=False, default=True)
    # See PIPELINE_PURPOSES. Only a 'build' pipeline can be a service's default,
    # be listed on the Pipeline tab, or be what Run Build runs.
    purpose = db.Column(db.String(16), nullable=False, default="build")
    version = db.Column(db.Integer, nullable=False, default=1)
    # What a person is asked before a build starts. A list of
    # {name, type, label, description, default, required, choices, source} —
    # see services/ci/pipelines._parameters. Accepted values travel as the
    # build's `variables`, which every stage already receives as environment.
    parameters = db.Column(db.JSON, nullable=False, default=list)
    created_by_user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=True)
    created_at = db.Column(db.DateTime(timezone=True), nullable=False, default=_now)
    updated_at = db.Column(
        db.DateTime(timezone=True), nullable=False, default=_now, onupdate=_now
    )

    service = db.relationship("CiService", back_populates="pipelines")
    stages = db.relationship(
        "CiPipelineStage",
        back_populates="pipeline",
        cascade="all, delete-orphan",
        order_by="CiPipelineStage.position",
    )


class CiPipelineStage(db.Model):
    """One stage definition: where it runs, what it runs, how it fails.

    ``position`` is indexed but deliberately not unique — reordering under a
    unique constraint would need a temporary-offset dance on every save.
    """

    __tablename__ = "ci_pipeline_stages"
    __table_args__ = (
        db.Index("ix_ci_pipeline_stage_pipeline_pos", "pipeline_id", "position"),
    )

    id = db.Column(db.Integer, primary_key=True)
    pipeline_id = db.Column(
        db.Integer,
        db.ForeignKey("ci_pipelines.id", ondelete="CASCADE"),
        nullable=False,
    )
    position = db.Column(db.Integer, nullable=False, default=0)
    name = db.Column(db.String(120), nullable=False)
    stage_type = db.Column(db.String(32), nullable=False, default="command")

    # WHERE it runs. Null runner_type means "any runner whose capabilities cover
    # runner_labels" — the scheduler decides.
    runner_type = db.Column(db.String(24), nullable=True)
    runner_labels = db.Column(db.JSON, nullable=False, default=list)

    # WHAT it runs.
    image = db.Column(db.String(512), nullable=True)
    working_directory = db.Column(db.String(512), nullable=True)
    commands = db.Column(db.JSON, nullable=False, default=list)
    env = db.Column(db.JSON, nullable=False, default=dict)
    # [{"name": "NEXUS_PASSWORD", "envVar": "NEXUS_PASSWORD"}] — references only.
    # A secret value is never stored in a stage definition.
    secret_refs = db.Column(db.JSON, nullable=False, default=list)
    # [{"path": "target/*.jar", "type": "jar", "name": "app"}]
    artifacts = db.Column(db.JSON, nullable=False, default=list)
    resources = db.Column(db.JSON, nullable=True)
    # [{"ip": "10.10.10.20", "hostnames": ["nexus.areeba.com", "nexus"]}] — extra
    # /etc/hosts entries for the build. Kubernetes applies hostAliases to the
    # POD, and a build is one pod, so every stage's entries are merged and every
    # stage sees all of them. Empty list on stages saved before this existed.
    host_aliases = db.Column(db.JSON, nullable=False, default=list)

    # WHETHER it runs. Null means always. Otherwise
    # ``{"variable": "DEPLOY_UAT", "operator": "equals", "value": "true"}``
    # evaluated against the build's variables — the Jenkins ``when`` clause,
    # reduced to the one form pipelines actually use. A stage whose condition is
    # false is closed as ``skipped`` with the reason in its log; it is never
    # dispatched, so a whole-build runner does not even create its container.
    run_condition = db.Column(db.JSON, nullable=True)

    # WHETHER THE IMAGE IT BUILDS MAY BE PUSHED. container_image stages only;
    # NULL (every stage saved before this existed, and every non-image stage)
    # means no scan runs and the stage behaves exactly as it did before.
    # ``{"enabled": true, "scanner": "trivy", "threshold": "critical",
    #    "onFail": "block", "ignoreUnfixed": false}``
    image_scan = db.Column(db.JSON, nullable=True)

    # HOW it behaves.
    timeout_seconds = db.Column(db.Integer, nullable=False, default=1800)
    continue_on_failure = db.Column(db.Boolean, nullable=False, default=False)
    # Reserved for parallel execution. Written and serialized, never read by the
    # sequential executor, so enabling parallelism later needs no migration.
    parallel_group = db.Column(db.String(64), nullable=True)
    enabled = db.Column(db.Boolean, nullable=False, default=True)
    created_at = db.Column(db.DateTime(timezone=True), nullable=False, default=_now)

    pipeline = db.relationship("CiPipeline", back_populates="stages")


class CiBuild(db.Model):
    """One execution of one pipeline.

    Restart-safe by construction: every transition is committed before any work
    is dispatched, so a backend restart resumes the build from its persisted
    state rather than restarting it.

    Status lifecycle:
        queued -> running -> success | failed | timeout
        queued | running -> cancelled
    """

    __tablename__ = "ci_builds"
    __table_args__ = (
        db.UniqueConstraint("service_id", "number", name="uq_ci_build_service_number"),
        db.Index("ix_ci_build_status_queued", "status", "queued_at"),
        db.Index("ix_ci_build_service_id_desc", "service_id", "id"),
    )

    id = db.Column(db.Integer, primary_key=True)
    service_id = db.Column(
        db.Integer,
        db.ForeignKey("ci_services.id", ondelete="CASCADE"),
        nullable=False,
    )
    pipeline_id = db.Column(
        db.Integer,
        db.ForeignKey("ci_pipelines.id", ondelete="SET NULL"),
        nullable=True,
    )
    number = db.Column(db.Integer, nullable=False)
    status = db.Column(db.String(16), nullable=False, default="queued", index=True)
    trigger_type = db.Column(db.String(16), nullable=False, default="manual")

    branch = db.Column(db.String(255), nullable=True)
    commit_sha = db.Column(db.String(64), nullable=True)
    commit_message = db.Column(db.Text, nullable=True)

    requested_by_user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=True)
    retry_of_build_id = db.Column(
        db.Integer, db.ForeignKey("ci_builds.id", ondelete="SET NULL"), nullable=True
    )

    # The pipeline exactly as it was when the build was triggered. Builds render
    # and retry from this, never from the live pipeline.
    pipeline_snapshot = db.Column(db.JSON, nullable=False, default=dict)

    runner_id = db.Column(
        db.Integer, db.ForeignKey("ci_runners.id", ondelete="SET NULL"), nullable=True
    )
    # Runner-scoped workspace identity (Kubernetes Job name, agent workspace id).
    workspace_ref = db.Column(db.String(255), nullable=True)
    # Why a queued build has not started yet — surfaced verbatim in the UI.
    queue_reason = db.Column(db.String(255), nullable=True)

    cancel_requested = db.Column(db.Boolean, nullable=False, default=False)
    cancel_requested_by_user_id = db.Column(
        db.Integer, db.ForeignKey("users.id"), nullable=True
    )
    error = db.Column(db.Text, nullable=True)
    # sha256 of the token an in-cluster job presents on its callbacks (Phase 3).
    worker_callback_token_hash = db.Column(db.String(64), nullable=True)

    queued_at = db.Column(db.DateTime(timezone=True), nullable=False, default=_now)
    started_at = db.Column(db.DateTime(timezone=True), nullable=True)
    finished_at = db.Column(db.DateTime(timezone=True), nullable=True)
    duration_seconds = db.Column(db.Integer, nullable=True)
    created_at = db.Column(db.DateTime(timezone=True), nullable=False, default=_now)

    service = db.relationship("CiService", back_populates="builds")
    pipeline = db.relationship("CiPipeline", foreign_keys=[pipeline_id])
    requested_by = db.relationship("User", foreign_keys=[requested_by_user_id])
    runner = db.relationship("CiRunner", foreign_keys=[runner_id])
    stages = db.relationship(
        "CiBuildStage",
        back_populates="build",
        cascade="all, delete-orphan",
        order_by="CiBuildStage.position",
    )
    artifacts = db.relationship(
        "CiArtifact",
        back_populates="build",
        cascade="all, delete-orphan",
        lazy="dynamic",
    )


class CiBuildStage(db.Model):
    """One stage execution inside a build.

    The definition FK is nullable and SET NULL: a stage removed from the
    pipeline must not erase the record of the build that ran it. Everything the
    UI needs is denormalized onto this row.
    """

    __tablename__ = "ci_build_stages"
    __table_args__ = (
        db.Index("ix_ci_build_stage_build_pos", "build_id", "position"),
    )

    id = db.Column(db.Integer, primary_key=True)
    build_id = db.Column(
        db.Integer,
        db.ForeignKey("ci_builds.id", ondelete="CASCADE"),
        nullable=False,
    )
    pipeline_stage_id = db.Column(
        db.Integer,
        db.ForeignKey("ci_pipeline_stages.id", ondelete="SET NULL"),
        nullable=True,
    )
    position = db.Column(db.Integer, nullable=False, default=0)
    name = db.Column(db.String(120), nullable=False)
    stage_type = db.Column(db.String(32), nullable=False, default="command")
    status = db.Column(db.String(16), nullable=False, default="pending")
    attempt = db.Column(db.Integer, nullable=False, default=1)
    runner_id = db.Column(
        db.Integer, db.ForeignKey("ci_runners.id", ondelete="SET NULL"), nullable=True
    )
    # Runner-scoped handle for this stage (container name, agent job id).
    external_ref = db.Column(db.String(255), nullable=True)
    exit_code = db.Column(db.Integer, nullable=True)
    started_at = db.Column(db.DateTime(timezone=True), nullable=True)
    finished_at = db.Column(db.DateTime(timezone=True), nullable=True)
    duration_seconds = db.Column(db.Integer, nullable=True)
    log_line_count = db.Column(db.Integer, nullable=False, default=0)
    log_truncated = db.Column(db.Boolean, nullable=False, default=False)
    error = db.Column(db.Text, nullable=True)
    created_at = db.Column(db.DateTime(timezone=True), nullable=False, default=_now)

    build = db.relationship("CiBuild", back_populates="stages")
    runner = db.relationship("CiRunner", foreign_keys=[runner_id])


class CiLogChunk(db.Model):
    """An append-only slice of one stage's output.

    Content is masked before it reaches this table — see
    ``services/ci/logs.py``. Nothing downstream may assume otherwise.
    """

    __tablename__ = "ci_log_chunks"
    __table_args__ = (
        db.UniqueConstraint("build_stage_id", "seq", name="uq_ci_log_chunk_stage_seq"),
        db.Index("ix_ci_log_chunk_stage_seq", "build_stage_id", "seq"),
    )

    id = db.Column(db.Integer, primary_key=True)
    build_stage_id = db.Column(
        db.Integer,
        db.ForeignKey("ci_build_stages.id", ondelete="CASCADE"),
        nullable=False,
    )
    seq = db.Column(db.Integer, nullable=False)
    stream = db.Column(db.String(8), nullable=False, default="stdout")
    content = db.Column(db.Text, nullable=False, default="")
    created_at = db.Column(db.DateTime(timezone=True), nullable=False, default=_now)


class CiRunner(db.Model):
    """An execution target the scheduler can assign work to.

    Capabilities are a JSON list matched in Python rather than a join table:
    the fleet is tens of rows, and JSON columns are the established pattern in
    this codebase. Registration and heartbeat land with external runners.
    """

    __tablename__ = "ci_runners"

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(120), unique=True, nullable=False, index=True)
    description = db.Column(db.Text, nullable=True)
    runner_type = db.Column(db.String(24), nullable=False, default="kubernetes")
    status = db.Column(db.String(16), nullable=False, default="offline", index=True)
    enabled = db.Column(db.Boolean, nullable=False, default=True)

    hostname = db.Column(db.String(253), nullable=True)
    os = db.Column(db.String(32), nullable=True)
    os_version = db.Column(db.String(64), nullable=True)
    arch = db.Column(db.String(16), nullable=True)

    # Free-form routing labels and the capability set a stage's labels must be
    # a subset of.
    labels = db.Column(db.JSON, nullable=False, default=list)
    capabilities = db.Column(db.JSON, nullable=False, default=list)

    max_concurrent = db.Column(db.Integer, nullable=False, default=2)
    current_load = db.Column(db.Integer, nullable=False, default=0)
    version = db.Column(db.String(64), nullable=True)

    # External runner identity (external runners only).
    token_prefix = db.Column(db.String(16), nullable=True)
    token_hash = db.Column(db.String(64), nullable=True, unique=True)

    last_heartbeat_at = db.Column(db.DateTime(timezone=True), nullable=True)
    last_assigned_at = db.Column(db.DateTime(timezone=True), nullable=True)
    last_error = db.Column(db.Text, nullable=True)
    # "metadata" is reserved on the declarative base — map the column explicitly.
    runner_metadata = db.Column("metadata", db.JSON, nullable=False, default=dict)

    created_at = db.Column(db.DateTime(timezone=True), nullable=False, default=_now)
    updated_at = db.Column(
        db.DateTime(timezone=True), nullable=False, default=_now, onupdate=_now
    )

    # Built-in runners are managed by KubeSight itself and cannot be deleted.
    is_builtin = db.Column(db.Boolean, nullable=False, default=False)


class CiAgentTask(db.Model):
    """One stage handed to an external agent, awaiting or under execution.

    Agents are PULL runners: KubeSight cannot reach into somebody's Mac, so it
    records that a stage is available to a specific runner and waits for that
    agent to claim it. This row is the claim ticket and nothing more.

    It deliberately stores NO payload. Commands, environment and — above all —
    decrypted secrets are rebuilt when the agent claims the task and travel
    once, over the API. Persisting them here would put every build's secrets in
    the database, which is exactly what the rest of CI is careful not to do.
    """

    __tablename__ = "ci_agent_tasks"
    __table_args__ = (
        db.Index("ix_ci_agent_task_runner_state", "runner_id", "state"),
    )

    # queued  -> the agent has not picked it up yet
    # claimed -> an agent is running it and is expected to report back
    # done    -> the agent reported an outcome; exit_code says which
    STATES = ("queued", "claimed", "done")

    id = db.Column(db.Integer, primary_key=True)
    build_id = db.Column(
        db.Integer, db.ForeignKey("ci_builds.id", ondelete="CASCADE"), nullable=False
    )
    build_stage_id = db.Column(
        db.Integer, db.ForeignKey("ci_build_stages.id", ondelete="CASCADE"), nullable=False
    )
    runner_id = db.Column(
        db.Integer, db.ForeignKey("ci_runners.id", ondelete="SET NULL"), nullable=True
    )
    state = db.Column(db.String(16), nullable=False, default="queued", index=True)
    # Proves a result callback belongs to the agent that claimed this task, and
    # not to a stale process that woke up after the task was reassigned.
    claim_token_hash = db.Column(db.String(64), nullable=True)
    exit_code = db.Column(db.Integer, nullable=True)
    error = db.Column(db.Text, nullable=True)
    log_seq = db.Column(db.Integer, nullable=False, default=0)
    created_at = db.Column(db.DateTime(timezone=True), nullable=False, default=_now)
    claimed_at = db.Column(db.DateTime(timezone=True), nullable=True)
    # The agent's own liveness for THIS task: a claimed task whose agent goes
    # quiet is reaped, rather than pinning a build forever.
    last_heartbeat_at = db.Column(db.DateTime(timezone=True), nullable=True)
    finished_at = db.Column(db.DateTime(timezone=True), nullable=True)

    build = db.relationship("CiBuild")
    stage = db.relationship("CiBuildStage")
    runner = db.relationship("CiRunner")


class CiArtifact(db.Model):
    """Something a build produced, addressable independently of its runner.

    Container images live in a registry (``storage_backend='registry'``, with
    ``uri`` + ``digest``); files live wherever the configured
    :class:`ArtifactStore` put them (``storage_ref``).
    """

    __tablename__ = "ci_artifacts"
    __table_args__ = (
        db.Index("ix_ci_artifact_service_created", "service_id", "created_at"),
        db.Index("ix_ci_artifact_build", "build_id"),
    )

    id = db.Column(db.Integer, primary_key=True)
    service_id = db.Column(
        db.Integer,
        db.ForeignKey("ci_services.id", ondelete="CASCADE"),
        nullable=False,
    )
    build_id = db.Column(
        db.Integer, db.ForeignKey("ci_builds.id", ondelete="CASCADE"), nullable=True
    )
    build_stage_id = db.Column(
        db.Integer,
        db.ForeignKey("ci_build_stages.id", ondelete="SET NULL"),
        nullable=True,
    )
    artifact_type = db.Column(db.String(32), nullable=False, default="binary")
    name = db.Column(db.String(255), nullable=False)
    version = db.Column(db.String(120), nullable=True)
    # Registry reference (nexus.host/repo:tag) or a download URL.
    uri = db.Column(db.Text, nullable=True)
    # sha256:... for container images, from the builder's own metadata.
    digest = db.Column(db.String(128), nullable=True)
    checksum_sha256 = db.Column(db.String(64), nullable=True)
    size_bytes = db.Column(db.BigInteger, nullable=True)

    storage_backend = db.Column(db.String(24), nullable=False, default="local")
    storage_ref = db.Column(db.String(1024), nullable=True)
    registry_connection_id = db.Column(
        db.Integer, db.ForeignKey("registry_connections.id"), nullable=True
    )

    commit_sha = db.Column(db.String(64), nullable=True)
    branch = db.Column(db.String(255), nullable=True)
    artifact_metadata = db.Column("metadata", db.JSON, nullable=False, default=dict)
    created_at = db.Column(
        db.DateTime(timezone=True), nullable=False, default=_now, index=True
    )

    service = db.relationship("CiService", back_populates="artifacts")
    build = db.relationship("CiBuild", back_populates="artifacts")


class CiRepositoryAnalysis(db.Model):
    """One attempt to work out what a repository is and how to build it.

    A record of a *proposal*, not of a configuration. Nothing here is on the
    build path: accepting an analysis copies its result into an ordinary
    :class:`CiPipeline` and an ``application_profile``, after which this row is
    history and a build never reads it again. That is the whole point — a build
    must not depend on a model call, and structurally it cannot.

    The row exists before the work does, so an analysis that dies mid-flight is
    a visible failed row rather than a request that vanished. It carries its own
    heartbeat for the same reason a build does: the worker runs in this process,
    and a process that goes away must leave something the next one can reap.
    """

    __tablename__ = "ci_repository_analyses"
    __table_args__ = (
        db.Index("ix_ci_analysis_service_created", "service_id", "created_at"),
        db.Index("ix_ci_analysis_state", "state"),
    )

    id = db.Column(db.Integer, primary_key=True)
    service_id = db.Column(
        db.Integer,
        db.ForeignKey("ci_services.id", ondelete="CASCADE"),
        nullable=False,
    )
    state = db.Column(db.String(16), nullable=False, default="queued", index=True)
    pipeline_state = db.Column(db.String(16), nullable=False, default="not_generated")
    progress_percent = db.Column(db.Integer, nullable=False, default=0)
    # Shown verbatim in the UI — "Reading build configuration…", not a percentage
    # invented to look like progress.
    current_stage = db.Column(db.String(64), nullable=True)

    # `repository` analyses read the source; `profile` analyses generate from an
    # application profile the user typed, with no repository access at all.
    mode = db.Column(db.String(16), nullable=False, default="repository")
    revision = db.Column(db.String(255), nullable=True)
    commit_sha = db.Column(db.String(64), nullable=True)

    requested_by_user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=True)
    # Mirrors Application Intelligence: an audit label for the account KubeSight
    # acts as, never a credential and never a Bitbucket identity.
    executed_by_account = db.Column(db.String(120), nullable=False, default="hermes-agent")

    schema_version = db.Column(db.String(16), nullable=True)
    hermes_model = db.Column(db.String(120), nullable=True)
    hermes_prompt_version = db.Column(db.String(64), nullable=True)

    application_profile = db.Column(db.JSON, nullable=True)
    generated_pipeline = db.Column(db.JSON, nullable=True)
    required_inputs = db.Column(db.JSON, nullable=False, default=list)
    # {"valid": bool, "errors": [...], "warnings": [...]} from services/ci/generated.
    validation = db.Column(db.JSON, nullable=True)
    # One entry per generate/repair round: error codes, model, duration. Enough
    # to debug a bad proposal without running it again; never any file content.
    attempts = db.Column(db.JSON, nullable=False, default=list)
    warnings = db.Column(db.JSON, nullable=False, default=list)
    # How much of the repository was actually read. A profile can only describe
    # the slice the analysis saw, and a truncated tree has to say so.
    evidence_coverage = db.Column(db.JSON, nullable=True)

    failure_stage = db.Column(db.String(64), nullable=True)
    safe_error_message = db.Column(db.Text, nullable=True)
    cancel_requested = db.Column(db.Boolean, nullable=False, default=False)

    created_at = db.Column(db.DateTime(timezone=True), nullable=False, default=_now)
    started_at = db.Column(db.DateTime(timezone=True), nullable=True)
    completed_at = db.Column(db.DateTime(timezone=True), nullable=True)
    last_heartbeat_at = db.Column(db.DateTime(timezone=True), nullable=True)

    service = db.relationship("CiService", back_populates="analyses")
    requested_by = db.relationship("User", foreign_keys=[requested_by_user_id])


class CiSecret(db.Model):
    """A named value a pipeline may reference but never contains.

    Encrypted at rest with the shared Fernet helper. ``value_cipher`` is never
    serialized to any API response — reads return the key and metadata only.
    Global secrets use ``scope='global'`` with a NULL ``service_id``; because
    PostgreSQL treats NULLs as distinct, uniqueness for that scope is enforced
    in the service layer rather than by the constraint below.
    """

    __tablename__ = "ci_secrets"
    __table_args__ = (
        db.UniqueConstraint("service_id", "key", name="uq_ci_secret_service_key"),
        db.Index("ix_ci_secret_scope", "scope", "service_id"),
    )

    id = db.Column(db.Integer, primary_key=True)
    scope = db.Column(db.String(16), nullable=False, default="service")
    service_id = db.Column(
        db.Integer,
        db.ForeignKey("ci_services.id", ondelete="CASCADE"),
        nullable=True,
    )
    key = db.Column(db.String(120), nullable=False)
    value_cipher = db.Column(db.Text, nullable=False)
    description = db.Column(db.String(255), nullable=True)
    created_by_user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=True)
    last_used_at = db.Column(db.DateTime(timezone=True), nullable=True)
    created_at = db.Column(db.DateTime(timezone=True), nullable=False, default=_now)
    updated_at = db.Column(
        db.DateTime(timezone=True), nullable=False, default=_now, onupdate=_now
    )

    service = db.relationship("CiService", back_populates="secrets")
    created_by = db.relationship("User", foreign_keys=[created_by_user_id])
