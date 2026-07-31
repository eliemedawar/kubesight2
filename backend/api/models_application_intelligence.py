"""Persistence model for Application Intelligence.

The source-analysis domain is intentionally separate from the existing runtime
application catalog: an analyzed repository may not be deployed, and analysis
must never imply permission to mutate a workload.
"""

from __future__ import annotations

from datetime import datetime, timezone

from .db import db


def _now():
    return datetime.now(timezone.utc)


class BitbucketCredentialProfile(db.Model):
    __tablename__ = "bitbucket_credential_profiles"

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(120), unique=True, nullable=False, index=True)
    credential_type = db.Column(db.String(32), nullable=False)
    principal = db.Column(db.String(255), nullable=True)
    secret_cipher = db.Column(db.Text, nullable=False)
    read_only = db.Column(db.Boolean, nullable=False, default=True)
    enabled = db.Column(db.Boolean, nullable=False, default=True)
    created_by_user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=True)
    created_at = db.Column(db.DateTime(timezone=True), nullable=False, default=_now)
    updated_at = db.Column(
        db.DateTime(timezone=True), nullable=False, default=_now, onupdate=_now
    )

    created_by = db.relationship("User", foreign_keys=[created_by_user_id])


class IntelligenceApplication(db.Model):
    __tablename__ = "intelligence_applications"

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(160), nullable=False)
    slug = db.Column(db.String(180), unique=True, nullable=False, index=True)
    description = db.Column(db.Text, nullable=True)
    repository_provider = db.Column(db.String(32), nullable=False, default="bitbucket")
    repository_url = db.Column(db.String(1024), nullable=False)
    repository_workspace = db.Column(db.String(255), nullable=False)
    repository_name = db.Column(db.String(255), nullable=False)
    default_branch = db.Column(db.String(255), nullable=False, default="main")
    credential_profile_id = db.Column(
        db.Integer, db.ForeignKey("bitbucket_credential_profiles.id"), nullable=False
    )
    repository_subdirectory = db.Column(db.String(512), nullable=True)
    dockerfile_path = db.Column(db.String(512), nullable=True)
    mapped_cluster_id = db.Column(db.String(120), nullable=True)
    mapped_namespace = db.Column(db.String(253), nullable=True)
    mapped_workload_kind = db.Column(db.String(64), nullable=True)
    mapped_workload_name = db.Column(db.String(253), nullable=True)
    created_by_user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    created_at = db.Column(db.DateTime(timezone=True), nullable=False, default=_now)
    updated_at = db.Column(
        db.DateTime(timezone=True), nullable=False, default=_now, onupdate=_now
    )

    credential_profile = db.relationship("BitbucketCredentialProfile")
    created_by = db.relationship("User", foreign_keys=[created_by_user_id])
    analyses = db.relationship(
        "ApplicationAnalysis",
        back_populates="application",
        cascade="all, delete-orphan",
        lazy="dynamic",
    )


class ApplicationAnalysis(db.Model):
    __tablename__ = "application_analyses"
    __table_args__ = (
        db.Index("ix_app_analysis_application_created", "application_id", "created_at"),
        db.Index("ix_app_analysis_status_created", "status", "created_at"),
    )

    id = db.Column(db.Integer, primary_key=True)
    application_id = db.Column(
        db.Integer, db.ForeignKey("intelligence_applications.id"), nullable=False
    )
    status = db.Column(db.String(32), nullable=False, default="Queued", index=True)
    progress_percent = db.Column(db.Integer, nullable=False, default=0)
    current_stage = db.Column(db.String(64), nullable=True)
    analysis_mode = db.Column(db.String(32), nullable=False)
    requested_by_user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    executed_by_account = db.Column(db.String(120), nullable=False, default="hermes-agent")
    branch = db.Column(db.String(255), nullable=True)
    requested_revision = db.Column(db.String(255), nullable=True)
    commit_sha = db.Column(db.String(64), nullable=True)
    worker_job_name = db.Column(db.String(253), nullable=True, unique=True)
    worker_callback_token_hash = db.Column(db.String(64), nullable=True)
    started_at = db.Column(db.DateTime(timezone=True), nullable=True)
    completed_at = db.Column(db.DateTime(timezone=True), nullable=True)
    failed_at = db.Column(db.DateTime(timezone=True), nullable=True)
    failure_stage = db.Column(db.String(64), nullable=True)
    safe_error_message = db.Column(db.Text, nullable=True)
    # Risk is never a model-invented number. Severity counts over persisted
    # findings are the only quantitative posture KubeSight publishes, so no
    # 0-100 score column exists here.
    topology_confidence = db.Column(db.String(32), nullable=True)
    scanner_versions = db.Column(db.JSON, nullable=False, default=dict)
    scanner_runs = db.Column(db.JSON, nullable=False, default=list)
    # How much of the repository the model was actually shown. Its findings can
    # only ever describe the slice it received.
    source_coverage = db.Column(db.JSON, nullable=True)
    hermes_model = db.Column(db.String(120), nullable=True)
    hermes_prompt_version = db.Column(db.String(64), nullable=True)
    result_summary = db.Column(db.JSON, nullable=True)
    workspace_cleanup_status = db.Column(db.String(32), nullable=False, default="Pending")
    warnings = db.Column(db.JSON, nullable=False, default=list)
    created_at = db.Column(db.DateTime(timezone=True), nullable=False, default=_now)

    application = db.relationship("IntelligenceApplication", back_populates="analyses")
    requested_by = db.relationship("User", foreign_keys=[requested_by_user_id])
    findings = db.relationship(
        "ApplicationFinding", cascade="all, delete-orphan", lazy="dynamic"
    )
    dependencies = db.relationship(
        "ApplicationDependency", cascade="all, delete-orphan", lazy="dynamic"
    )
    communications = db.relationship(
        "ApplicationCommunication", cascade="all, delete-orphan", lazy="dynamic"
    )
    artifacts = db.relationship(
        "ApplicationArtifact", cascade="all, delete-orphan", lazy="dynamic"
    )
    pull_requests = db.relationship(
        "ApplicationPullRequest",
        back_populates="analysis",
        cascade="all, delete-orphan",
        lazy="dynamic",
    )
    runtime_snapshots = db.relationship(
        "ApplicationRuntimeSnapshot",
        back_populates="analysis",
        cascade="all, delete-orphan",
        lazy="dynamic",
    )


class ApplicationFinding(db.Model):
    __tablename__ = "application_findings"
    __table_args__ = (
        db.UniqueConstraint("analysis_id", "fingerprint", name="uq_analysis_finding_fingerprint"),
        db.Index("ix_app_finding_analysis_severity", "analysis_id", "severity"),
    )

    id = db.Column(db.Integer, primary_key=True)
    analysis_id = db.Column(db.Integer, db.ForeignKey("application_analyses.id"), nullable=False)
    category = db.Column(db.String(80), nullable=False)
    severity = db.Column(db.String(32), nullable=False)
    confidence = db.Column(db.String(32), nullable=False)
    title = db.Column(db.String(500), nullable=False)
    description = db.Column(db.Text, nullable=False)
    impact = db.Column(db.Text, nullable=True)
    recommendation = db.Column(db.Text, nullable=True)
    # The literal source observation the finding rests on. Without it a reader
    # cannot tell an evidenced claim from a plausible-sounding one.
    evidence = db.Column(db.Text, nullable=True)
    evidence_type = db.Column(db.String(64), nullable=True)
    file_path = db.Column(db.String(1024), nullable=True)
    start_line = db.Column(db.Integer, nullable=True)
    end_line = db.Column(db.Integer, nullable=True)
    scanner_source = db.Column(db.String(120), nullable=True)
    cwe = db.Column(db.String(64), nullable=True)
    cve = db.Column(db.String(64), nullable=True)
    status = db.Column(db.String(32), nullable=False, default="Open")
    fingerprint = db.Column(db.String(64), nullable=False)
    suggested_patch = db.Column(db.Text, nullable=True)
    created_at = db.Column(db.DateTime(timezone=True), nullable=False, default=_now)
    updated_at = db.Column(
        db.DateTime(timezone=True), nullable=False, default=_now, onupdate=_now
    )
    status_events = db.relationship(
        "ApplicationFindingStatusEvent",
        cascade="all, delete-orphan",
        lazy="dynamic",
    )


class ApplicationFindingStatusEvent(db.Model):
    __tablename__ = "application_finding_status_events"
    __table_args__ = (
        db.Index(
            "ix_app_finding_status_event_finding_created",
            "finding_id",
            "created_at",
        ),
    )

    id = db.Column(db.Integer, primary_key=True)
    finding_id = db.Column(
        db.Integer, db.ForeignKey("application_findings.id"), nullable=False
    )
    previous_status = db.Column(db.String(32), nullable=False)
    status = db.Column(db.String(32), nullable=False)
    reason = db.Column(db.Text, nullable=True)
    changed_by_user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    created_at = db.Column(db.DateTime(timezone=True), nullable=False, default=_now)

    changed_by = db.relationship("User", foreign_keys=[changed_by_user_id])


class ApplicationDependency(db.Model):
    __tablename__ = "application_dependencies"

    id = db.Column(db.Integer, primary_key=True)
    analysis_id = db.Column(db.Integer, db.ForeignKey("application_analyses.id"), nullable=False)
    dependency_type = db.Column(db.String(64), nullable=True)
    name = db.Column(db.String(500), nullable=False)
    version = db.Column(db.String(255), nullable=True)
    ecosystem = db.Column(db.String(64), nullable=True)
    direct = db.Column(db.Boolean, nullable=False, default=True)
    vulnerable = db.Column(db.Boolean, nullable=False, default=False)
    license = db.Column(db.String(255), nullable=True)
    source_file = db.Column(db.String(1024), nullable=True)
    dependency_metadata = db.Column("metadata", db.JSON, nullable=False, default=dict)


class ApplicationCommunication(db.Model):
    __tablename__ = "application_communications"

    id = db.Column(db.Integer, primary_key=True)
    analysis_id = db.Column(db.Integer, db.ForeignKey("application_analyses.id"), nullable=False)
    source_component = db.Column(db.String(255), nullable=False)
    destination_component = db.Column(db.String(500), nullable=False)
    destination_type = db.Column(db.String(64), nullable=False)
    protocol = db.Column(db.String(32), nullable=True)
    port = db.Column(db.Integer, nullable=True)
    direction = db.Column(db.String(16), nullable=True)
    endpoint = db.Column(db.String(1024), nullable=True)
    configuration_key = db.Column(db.String(255), nullable=True)
    required = db.Column(db.Boolean, nullable=False, default=True)
    evidence = db.Column(db.Text, nullable=True)
    file_path = db.Column(db.String(1024), nullable=True)
    line_number = db.Column(db.Integer, nullable=True)
    confidence = db.Column(db.String(32), nullable=True)
    evidence_state = db.Column(db.String(64), nullable=False, default="Source Inferred")


class ApplicationArtifact(db.Model):
    __tablename__ = "application_artifacts"

    id = db.Column(db.Integer, primary_key=True)
    analysis_id = db.Column(db.Integer, db.ForeignKey("application_analyses.id"), nullable=False)
    artifact_type = db.Column(db.String(80), nullable=False)
    filename = db.Column(db.String(255), nullable=False)
    storage_reference = db.Column(db.String(1024), nullable=False)
    checksum = db.Column(db.String(64), nullable=False)
    created_at = db.Column(db.DateTime(timezone=True), nullable=False, default=_now)


class ApplicationRuntimeSnapshot(db.Model):
    """Redacted, read-only evidence collected for one mapped workload.

    Kubernetes credentials and raw Secret/ConfigMap values never enter this
    table. Derived comparisons remain tied to the user-scoped collection event
    that produced them.
    """

    __tablename__ = "application_runtime_snapshots"
    __table_args__ = (
        db.Index(
            "ix_application_runtime_snapshot_analysis_created",
            "analysis_id",
            "created_at",
        ),
    )

    id = db.Column(db.Integer, primary_key=True)
    analysis_id = db.Column(
        db.Integer, db.ForeignKey("application_analyses.id"), nullable=False
    )
    cluster_id = db.Column(db.String(120), nullable=False)
    namespace = db.Column(db.String(253), nullable=False)
    workload_kind = db.Column(db.String(64), nullable=False)
    workload_name = db.Column(db.String(253), nullable=False)
    collected_by_user_id = db.Column(
        db.Integer, db.ForeignKey("users.id"), nullable=False
    )
    status = db.Column(db.String(32), nullable=False, default="Completed")
    safe_error_message = db.Column(db.Text, nullable=True)
    evidence = db.Column(db.JSON, nullable=False, default=dict)
    comparison = db.Column(db.JSON, nullable=False, default=list)
    topology = db.Column(db.JSON, nullable=False, default=dict)
    network_policy = db.Column(db.JSON, nullable=False, default=dict)
    readiness_gates = db.Column(db.JSON, nullable=False, default=list)
    created_at = db.Column(db.DateTime(timezone=True), nullable=False, default=_now)

    analysis = db.relationship("ApplicationAnalysis", back_populates="runtime_snapshots")
    collected_by = db.relationship("User", foreign_keys=[collected_by_user_id])


class ApplicationPullRequest(db.Model):
    __tablename__ = "application_pull_requests"
    __table_args__ = (
        db.Index(
            "ix_application_pull_request_analysis_created",
            "analysis_id",
            "created_at",
        ),
    )

    id = db.Column(db.Integer, primary_key=True)
    analysis_id = db.Column(
        db.Integer, db.ForeignKey("application_analyses.id"), nullable=False
    )
    credential_profile_id = db.Column(
        db.Integer, db.ForeignKey("bitbucket_credential_profiles.id"), nullable=False
    )
    requested_by_user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    selected_finding_ids = db.Column(db.JSON, nullable=False, default=list)
    branch_name = db.Column(db.String(255), nullable=False)
    destination_branch = db.Column(db.String(255), nullable=False)
    title = db.Column(db.String(500), nullable=False)
    description = db.Column(db.Text, nullable=False)
    status = db.Column(db.String(32), nullable=False, default="Queued", index=True)
    worker_job_name = db.Column(db.String(253), nullable=True, unique=True)
    worker_callback_token_hash = db.Column(db.String(64), nullable=True)
    provider_pull_request_id = db.Column(db.String(120), nullable=True)
    provider_url = db.Column(db.String(1024), nullable=True)
    validation_summary = db.Column(db.JSON, nullable=True)
    safe_error_message = db.Column(db.Text, nullable=True)
    completed_at = db.Column(db.DateTime(timezone=True), nullable=True)
    created_at = db.Column(db.DateTime(timezone=True), nullable=False, default=_now)

    analysis = db.relationship("ApplicationAnalysis", back_populates="pull_requests")
    credential_profile = db.relationship("BitbucketCredentialProfile")
    requested_by = db.relationship("User", foreign_keys=[requested_by_user_id])
