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


# Application types a service can declare. Drives the starter pipeline template
# and the card icon; never gates execution.
APPLICATION_TYPES = (
    "container",
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
)

ARTIFACT_BACKENDS = ("local", "registry", "s3", "nexus_raw")


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

    def source_ready(self) -> bool:
        """Whether this service has enough source configuration to build."""
        return bool(self.repository_url and self.credential_profile_id)

    def default_pipeline(self):
        for pipeline in self.pipelines:
            if pipeline.is_default:
                return pipeline
        return self.pipelines[0] if self.pipelines else None


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
    version = db.Column(db.Integer, nullable=False, default=1)
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
