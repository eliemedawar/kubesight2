from __future__ import annotations

from datetime import datetime, timezone

from .db import db

role_permissions = db.Table(
    "role_permissions",
    db.Column("role_id", db.Integer, db.ForeignKey("roles.id"), primary_key=True),
    db.Column("permission_id", db.Integer, db.ForeignKey("permissions.id"), primary_key=True),
)


class Role(db.Model):
    __tablename__ = "roles"

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(64), unique=True, nullable=False, index=True)
    description = db.Column(db.String(255), nullable=False, default="")
    is_system_role = db.Column(db.Boolean, nullable=False, default=False)
    permissions = db.relationship("Permission", secondary=role_permissions, lazy="joined")
    users = db.relationship("User", back_populates="role", lazy="dynamic")


class Permission(db.Model):
    __tablename__ = "permissions"

    id = db.Column(db.Integer, primary_key=True)
    key = db.Column(db.String(120), unique=True, nullable=False, index=True)
    description = db.Column(db.String(255), nullable=False, default="")


class User(db.Model):
    __tablename__ = "users"

    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(120), unique=True, nullable=False, index=True)
    email = db.Column(db.String(255), nullable=False, default="")
    password_hash = db.Column(db.String(255), nullable=False, default="")
    full_name = db.Column(db.String(255), nullable=False, default="")
    role_id = db.Column(db.Integer, db.ForeignKey("roles.id"), nullable=True)
    is_active = db.Column(db.Boolean, nullable=False, default=True)
    created_at = db.Column(
        db.DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )
    updated_at = db.Column(
        db.DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )
    last_login_at = db.Column(db.DateTime(timezone=True), nullable=True)
    last_login_ip = db.Column(db.String(64), nullable=True)

    # --- Onboarding / first-login authentication flow -------------------
    # A newly created user receives a random temporary password (hashed in
    # ``password_hash``) that must be changed on first login. ``mfa_enabled`` and
    # ``totp_secret`` back TOTP MFA enrolment; ``first_login_completed`` gates
    # dashboard access until password change + MFA setup are both done. Existing
    # users are migrated with ``first_login_completed = True`` so they are never
    # forced through onboarding retroactively.
    must_change_password = db.Column(db.Boolean, nullable=False, default=False)
    temporary_password_expires_at = db.Column(db.DateTime(timezone=True), nullable=True)
    # Single-use guard for the temporary password: flipped to True once the user
    # replaces it, so a temporary password can never authenticate twice.
    temporary_password_used = db.Column(db.Boolean, nullable=False, default=False)
    mfa_enabled = db.Column(db.Boolean, nullable=False, default=False)
    totp_secret = db.Column(db.String(64), nullable=True)
    first_login_completed = db.Column(db.Boolean, nullable=False, default=True)

    # --- Failed-attempt / lockout tracking ------------------------------
    # Password and MFA failures are counted separately. Five consecutive
    # failures of either kind trigger a 15-minute temporary lock; three temporary
    # locks inside 24h escalate to ``requires_admin_unlock`` (only an admin can
    # clear it). ``is_active`` remains the canonical enabled/disabled flag.
    failed_login_attempts = db.Column(db.Integer, nullable=False, default=0)
    mfa_failed_attempts = db.Column(db.Integer, nullable=False, default=0)
    last_failed_login_at = db.Column(db.DateTime(timezone=True), nullable=True)
    locked_until = db.Column(db.DateTime(timezone=True), nullable=True)
    lock_reason = db.Column(db.String(64), nullable=True)
    lock_count_24h = db.Column(db.Integer, nullable=False, default=0)
    requires_admin_unlock = db.Column(db.Boolean, nullable=False, default=False)
    created_by_admin_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=True)

    role = db.relationship("Role", back_populates="users")
    cluster_access_entries = db.relationship(
        "UserClusterAccess",
        back_populates="user",
        cascade="all, delete-orphan",
        lazy="joined",
    )
    namespace_access_entries = db.relationship(
        "UserNamespaceAccess",
        back_populates="user",
        cascade="all, delete-orphan",
        lazy="joined",
    )
    access_rules = db.relationship(
        "AccessRule",
        back_populates="user",
        cascade="all, delete-orphan",
        lazy="joined",
    )


class AccessRule(db.Model):
    __tablename__ = "access_rules"
    __table_args__ = (
        db.Index("ix_access_rule_user_cluster", "user_id", "cluster_id"),
        db.Index("ix_access_rule_user_cluster_perm", "user_id", "cluster_id", "permission_key"),
    )

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False, index=True)
    cluster_id = db.Column(db.String(120), nullable=False, index=True)
    namespace = db.Column(db.String(253), nullable=True)
    resource_type = db.Column(db.String(32), nullable=False, default="cluster")
    resource_name = db.Column(db.String(253), nullable=True)
    container_name = db.Column(db.String(253), nullable=True)
    port = db.Column(db.Integer, nullable=True)
    permission_key = db.Column(db.String(120), nullable=False, index=True)
    effect = db.Column(db.String(16), nullable=False, default="allow")
    created_at = db.Column(
        db.DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )
    updated_at = db.Column(
        db.DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )

    user = db.relationship("User", back_populates="access_rules")


class UserClusterAccess(db.Model):
    __tablename__ = "user_cluster_access"
    __table_args__ = (db.UniqueConstraint("user_id", "cluster_id", name="uq_user_cluster"),)

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False, index=True)
    cluster_id = db.Column(db.String(120), nullable=False, index=True)
    can_view = db.Column(db.Boolean, nullable=False, default=True)

    user = db.relationship("User", back_populates="cluster_access_entries")


class UserNamespaceAccess(db.Model):
    __tablename__ = "user_namespace_access"
    __table_args__ = (
        db.UniqueConstraint("user_id", "cluster_id", "namespace", name="uq_user_cluster_namespace"),
    )

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False, index=True)
    cluster_id = db.Column(db.String(120), nullable=False, index=True)
    namespace = db.Column(db.String(253), nullable=False)
    can_view = db.Column(db.Boolean, nullable=False, default=True)

    user = db.relationship("User", back_populates="namespace_access_entries")


class AuditLog(db.Model):
    __tablename__ = "audit_logs"
    __table_args__ = (
        db.Index("ix_audit_log_actor_created", "actor_user_id", "created_at"),
        db.Index("ix_audit_log_action_created", "action", "created_at"),
    )

    id = db.Column(db.Integer, primary_key=True)
    actor_user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=True, index=True)
    action = db.Column(db.String(120), nullable=False, index=True)
    target_type = db.Column(db.String(64), nullable=True)
    target_id = db.Column(db.String(255), nullable=True)
    details = db.Column(db.JSON, nullable=True)
    created_at = db.Column(
        db.DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
        index=True,
    )

    actor = db.relationship("User", foreign_keys=[actor_user_id])


class AppSettings(db.Model):
    __tablename__ = "app_settings"

    id = db.Column(db.Integer, primary_key=True)
    theme = db.Column(db.String(50), nullable=False, default="system")
    refresh_interval_seconds = db.Column(db.Integer, nullable=False, default=30)
    default_cluster = db.Column(db.String(120), nullable=False, default="prod-us-east")
    notifications = db.Column(
        db.JSON,
        nullable=False,
        default=lambda: {"alerts": True, "upgrades": True},
    )


class Cluster(db.Model):
    __tablename__ = "clusters"

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(120), nullable=False)
    host = db.Column(db.String(253), nullable=False)
    port = db.Column(db.Integer, nullable=False)
    protocol = db.Column(db.String(10), nullable=False, default="https")
    connection_method = db.Column(db.String(32), nullable=False, default="kubeconfig")
    authentication_type = db.Column(db.String(32), nullable=True)
    skip_tls_verify = db.Column(db.Boolean, nullable=False, default=False)
    connection_timeout_seconds = db.Column(db.Integer, nullable=True)
    kubeconfig_path = db.Column(db.String(512), nullable=True)
    context_name = db.Column(db.String(255), nullable=True)
    is_active = db.Column(db.Boolean, nullable=False, default=True)
    last_connection_status = db.Column(db.String(32), nullable=True)
    last_connection_error = db.Column(db.Text, nullable=True)
    last_tested_at = db.Column(db.DateTime(timezone=True), nullable=True)
    created_at = db.Column(
        db.DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )
    updated_at = db.Column(
        db.DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )


class AppCatalogEntry(db.Model):
    __tablename__ = "app_catalog_entries"
    __table_args__ = (
        db.Index("ix_app_catalog_cluster_ns_workload", "cluster_id", "namespace", "workload_name"),
    )

    id = db.Column(db.Integer, primary_key=True)
    cluster_id = db.Column(db.String(120), nullable=False, index=True)
    namespace = db.Column(db.String(253), nullable=False)
    workload_type = db.Column(db.String(64), nullable=True)
    workload_name = db.Column(db.String(253), nullable=True)
    display_name = db.Column(db.String(253), nullable=False)
    owner_team = db.Column(db.String(255), nullable=True)
    environment = db.Column(db.String(64), nullable=True)
    criticality = db.Column(db.String(64), nullable=True)
    description = db.Column(db.Text, nullable=True)
    documentation_url = db.Column(db.String(512), nullable=True)
    contact_email = db.Column(db.String(255), nullable=True)
    tags = db.Column(db.JSON, nullable=True, default=list)
    source = db.Column(db.String(64), nullable=False, default="Registered")
    release_name = db.Column(db.String(253), nullable=True, index=True)
    chart_name = db.Column(db.String(253), nullable=True)
    chart_version = db.Column(db.String(64), nullable=True)
    app_version = db.Column(db.String(64), nullable=True)
    helm_revision = db.Column(db.Integer, nullable=True)
    created_by_user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=True, index=True)
    created_at = db.Column(
        db.DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )
    updated_at = db.Column(
        db.DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )
    is_active = db.Column(db.Boolean, nullable=False, default=True, index=True)

    created_by = db.relationship("User", foreign_keys=[created_by_user_id])
    deployment_versions = db.relationship(
        "ApplicationDeploymentVersion",
        back_populates="catalog_entry",
        cascade="all, delete-orphan",
        lazy="dynamic",
    )


class ApplicationDeploymentVersion(db.Model):
    __tablename__ = "application_deployment_versions"
    __table_args__ = (
        db.Index("ix_app_deploy_version_cluster_ns_app", "cluster_id", "namespace", "app_name"),
    )

    id = db.Column(db.Integer, primary_key=True)
    catalog_entry_id = db.Column(db.Integer, db.ForeignKey("app_catalog_entries.id"), nullable=True, index=True)
    cluster_id = db.Column(db.String(120), nullable=False, index=True)
    namespace = db.Column(db.String(253), nullable=False)
    app_name = db.Column(db.String(253), nullable=False)
    version_label = db.Column(db.String(32), nullable=False)
    version_major = db.Column(db.Integer, nullable=False, default=1)
    version_minor = db.Column(db.Integer, nullable=False, default=0)
    workload_type = db.Column(db.String(64), nullable=True)
    change_summary = db.Column(db.Text, nullable=True)
    yaml_snapshot = db.Column(db.Text, nullable=False)
    wizard_config = db.Column(db.JSON, nullable=True)
    created_by_user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=True, index=True)
    created_at = db.Column(
        db.DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
        index=True,
    )

    catalog_entry = db.relationship("AppCatalogEntry", back_populates="deployment_versions")
    created_by = db.relationship("User", foreign_keys=[created_by_user_id])


class AlertNotificationSent(db.Model):
    __tablename__ = "alert_notifications_sent"
    __table_args__ = (db.UniqueConstraint("alert_id", "channel", name="uq_alert_notification_channel"),)

    id = db.Column(db.Integer, primary_key=True)
    alert_id = db.Column(db.String(255), nullable=False, index=True)
    channel = db.Column(db.String(32), nullable=False, default="email")
    sent_at = db.Column(db.DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc))


class AlertRoutingSmtp(db.Model):
    __tablename__ = "alert_routing_smtp"

    id = db.Column(db.Integer, primary_key=True)
    host = db.Column(db.String(255), nullable=False, default="")
    port = db.Column(db.Integer, nullable=False, default=587)
    username = db.Column(db.String(255), nullable=False, default="")
    password_encrypted = db.Column(db.Text, nullable=True)
    from_email = db.Column(db.String(255), nullable=False, default="")
    from_name = db.Column(db.String(255), nullable=False, default="KubeSight")
    use_tls = db.Column(db.Boolean, nullable=False, default=True)
    use_ssl = db.Column(db.Boolean, nullable=False, default=False)
    last_test_at = db.Column(db.DateTime(timezone=True), nullable=True)
    last_test_status = db.Column(db.String(16), nullable=True)
    last_test_message = db.Column(db.Text, nullable=True)
    updated_at = db.Column(
        db.DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )


alert_policy_receivers = db.Table(
    "alert_policy_receivers",
    db.Column(
        "policy_id",
        db.Integer,
        db.ForeignKey("alert_policies.id", ondelete="CASCADE"),
        primary_key=True,
    ),
    db.Column(
        "receiver_id",
        db.Integer,
        db.ForeignKey("alert_routing_receivers.id", ondelete="CASCADE"),
        primary_key=True,
    ),
)

alert_receiver_group_members = db.Table(
    "alert_receiver_group_members",
    db.Column(
        "group_id",
        db.Integer,
        db.ForeignKey("alert_routing_receiver_groups.id", ondelete="CASCADE"),
        primary_key=True,
    ),
    db.Column(
        "receiver_id",
        db.Integer,
        db.ForeignKey("alert_routing_receivers.id", ondelete="CASCADE"),
        primary_key=True,
    ),
)

alert_policy_receiver_groups = db.Table(
    "alert_policy_receiver_groups",
    db.Column(
        "policy_id",
        db.Integer,
        db.ForeignKey("alert_policies.id", ondelete="CASCADE"),
        primary_key=True,
    ),
    db.Column(
        "group_id",
        db.Integer,
        db.ForeignKey("alert_routing_receiver_groups.id", ondelete="CASCADE"),
        primary_key=True,
    ),
)


class AlertRoutingReceiverGroup(db.Model):
    __tablename__ = "alert_routing_receiver_groups"

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(120), nullable=False, unique=True)
    description = db.Column(db.Text, nullable=True)
    enabled = db.Column(db.Boolean, nullable=False, default=True, index=True)
    created_at = db.Column(
        db.DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )
    updated_at = db.Column(
        db.DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )

    members = db.relationship(
        "AlertRoutingReceiver",
        secondary=alert_receiver_group_members,
        lazy="joined",
        back_populates="receiver_groups",
    )
    policies = db.relationship(
        "AlertPolicy",
        secondary=alert_policy_receiver_groups,
        lazy="dynamic",
        back_populates="notification_receiver_groups",
    )


class AlertRoutingReceiver(db.Model):
    __tablename__ = "alert_routing_receivers"

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(120), nullable=False)
    # email (legacy static) | user (linked to a User) | role (all active users of
    # a Role) | webhook | slack
    receiver_type = db.Column(db.String(32), nullable=False, index=True)
    email_address = db.Column(db.String(255), nullable=True)
    # For user/role receivers the recipient email(s) are resolved dynamically
    # from the linked user(s); disabled users are excluded at send time.
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=True, index=True)
    role_id = db.Column(db.Integer, db.ForeignKey("roles.id"), nullable=True, index=True)
    url = db.Column(db.String(1024), nullable=True)
    http_method = db.Column(db.String(16), nullable=False, default="POST")
    headers = db.Column(db.JSON, nullable=True, default=dict)
    secret_encrypted = db.Column(db.Text, nullable=True)
    enabled = db.Column(db.Boolean, nullable=False, default=True, index=True)
    severity_filter = db.Column(db.JSON, nullable=False, default=list)
    namespace_filter = db.Column(db.String(253), nullable=True)
    cluster_filter = db.Column(db.String(120), nullable=True)
    last_test_at = db.Column(db.DateTime(timezone=True), nullable=True)
    last_test_status = db.Column(db.String(16), nullable=True)
    last_test_message = db.Column(db.Text, nullable=True)
    created_at = db.Column(
        db.DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )
    updated_at = db.Column(
        db.DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )

    user = db.relationship("User", foreign_keys=[user_id])
    role = db.relationship("Role", foreign_keys=[role_id])
    policies = db.relationship(
        "AlertPolicy",
        secondary=alert_policy_receivers,
        lazy="dynamic",
        back_populates="notification_receivers",
    )
    receiver_groups = db.relationship(
        "AlertRoutingReceiverGroup",
        secondary=alert_receiver_group_members,
        lazy="dynamic",
        back_populates="members",
    )


class AlertRoutingDeliverySent(db.Model):
    __tablename__ = "alert_routing_delivery_sent"
    __table_args__ = (
        db.UniqueConstraint(
            "alert_id",
            "receiver_id",
            "alert_status",
            name="uq_alert_routing_delivery",
        ),
    )

    id = db.Column(db.Integer, primary_key=True)
    alert_id = db.Column(db.String(255), nullable=False, index=True)
    receiver_id = db.Column(db.Integer, nullable=False, index=True)
    alert_status = db.Column(db.String(16), nullable=False, default="firing")
    sent_at = db.Column(db.DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc))


class AlertDeliveryLog(db.Model):
    __tablename__ = "alert_delivery_logs"

    id = db.Column(db.Integer, primary_key=True)
    alert_id = db.Column(db.String(255), nullable=False, index=True)
    alert_name = db.Column(db.String(255), nullable=False, default="")
    policy_id = db.Column(db.Integer, nullable=True, index=True)
    policy_name = db.Column(db.String(120), nullable=False, default="")
    group_name = db.Column(db.String(120), nullable=False, default="")
    receiver_id = db.Column(db.Integer, nullable=True, index=True)
    receiver_name = db.Column(db.String(120), nullable=False, default="")
    receiver_type = db.Column(db.String(32), nullable=False, default="")
    status = db.Column(db.String(16), nullable=False, index=True)
    error_message = db.Column(db.Text, nullable=True)
    matched_pattern = db.Column(db.String(512), nullable=True)
    pod_name = db.Column(db.String(253), nullable=True)
    log_snippet = db.Column(db.Text, nullable=True)
    delivered_at = db.Column(
        db.DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
        index=True,
    )


class LogAlertSeen(db.Model):
    __tablename__ = "log_alert_seen"
    __table_args__ = (
        db.UniqueConstraint(
            "policy_id",
            "pod_name",
            "container_name",
            "log_hash",
            name="uq_log_alert_seen",
        ),
    )

    id = db.Column(db.Integer, primary_key=True)
    policy_id = db.Column(db.Integer, db.ForeignKey("alert_policies.id", ondelete="CASCADE"), nullable=False, index=True)
    pod_name = db.Column(db.String(253), nullable=False)
    container_name = db.Column(db.String(253), nullable=False, default="")
    log_timestamp = db.Column(db.String(64), nullable=False, default="")
    log_hash = db.Column(db.String(64), nullable=False, index=True)
    seen_at = db.Column(
        db.DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )


class AlertPolicy(db.Model):
    __tablename__ = "alert_policies"

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(120), nullable=False)
    cluster_id = db.Column(db.String(120), nullable=False, index=True)
    description = db.Column(db.Text, nullable=True)
    enabled = db.Column(db.Boolean, nullable=False, default=True, index=True)
    alert_type = db.Column(db.String(16), nullable=False, default="metric", index=True)
    severity = db.Column(db.String(16), nullable=False, default="warning")
    condition_logic = db.Column(db.String(8), nullable=False, default="any")
    conditions = db.Column(db.JSON, nullable=False, default=list)
    log_config = db.Column(db.JSON, nullable=True)
    service_config = db.Column(db.JSON, nullable=True)
    scope = db.Column(db.JSON, nullable=False, default=dict)
    notification_channels = db.Column(db.JSON, nullable=False, default=list)
    evaluation_interval_seconds = db.Column(db.Integer, nullable=False, default=300)
    last_evaluated_at = db.Column(db.DateTime(timezone=True), nullable=True)
    last_evaluation_result = db.Column(db.String(16), nullable=True)
    last_measured_value = db.Column(db.String(255), nullable=True)
    last_threshold = db.Column(db.String(64), nullable=True)
    last_evaluation_error = db.Column(db.Text, nullable=True)
    created_by_user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=True, index=True)
    created_at = db.Column(
        db.DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )
    updated_at = db.Column(
        db.DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )

    created_by = db.relationship("User", foreign_keys=[created_by_user_id])
    notification_receivers = db.relationship(
        "AlertRoutingReceiver",
        secondary=alert_policy_receivers,
        lazy="joined",
        back_populates="policies",
    )
    notification_receiver_groups = db.relationship(
        "AlertRoutingReceiverGroup",
        secondary=alert_policy_receiver_groups,
        lazy="joined",
        back_populates="policies",
    )
    history_entries = db.relationship(
        "AlertHistory",
        back_populates="policy",
        cascade="all, delete-orphan",
        lazy="dynamic",
    )


class AlertHistory(db.Model):
    __tablename__ = "alert_history"
    __table_args__ = (db.UniqueConstraint("alert_key", name="uq_alert_history_key"),)

    id = db.Column(db.Integer, primary_key=True)
    alert_key = db.Column(db.String(512), nullable=False, index=True)
    policy_id = db.Column(db.Integer, db.ForeignKey("alert_policies.id"), nullable=True, index=True)
    policy_name = db.Column(db.String(120), nullable=False, default="")
    cluster_id = db.Column(db.String(120), nullable=False, index=True)
    namespace = db.Column(db.String(253), nullable=True, index=True)
    resource_type = db.Column(db.String(32), nullable=True)
    resource_name = db.Column(db.String(253), nullable=True)
    alert_type = db.Column(db.String(16), nullable=False, default="metric", index=True)
    severity = db.Column(db.String(16), nullable=False, default="warning")
    status = db.Column(db.String(16), nullable=False, default="active", index=True)
    title = db.Column(db.String(255), nullable=False, default="")
    description = db.Column(db.Text, nullable=True)
    triggered_conditions = db.Column(db.JSON, nullable=False, default=list)
    metric_snapshot = db.Column(db.JSON, nullable=True)
    log_snapshot = db.Column(db.JSON, nullable=True)
    fired_at = db.Column(
        db.DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
        index=True,
    )
    resolved_at = db.Column(db.DateTime(timezone=True), nullable=True)
    last_notified_at = db.Column(db.DateTime(timezone=True), nullable=True)

    policy = db.relationship("AlertPolicy", back_populates="history_entries")


# ---------------------------------------------------------------------------
# Application Services & Clients
# ---------------------------------------------------------------------------

class ApplicationService(db.Model):
    __tablename__ = "application_services"

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(120), unique=True, nullable=False, index=True)
    description = db.Column(db.Text, nullable=True)
    created_at = db.Column(
        db.DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )
    updated_at = db.Column(
        db.DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )

    deployments = db.relationship(
        "ApplicationServiceDeployment",
        back_populates="service",
        cascade="all, delete-orphan",
        lazy="joined",
    )
    topology_nodes = db.relationship(
        "ApplicationServiceTopologyNode",
        back_populates="service",
        cascade="all, delete-orphan",
        lazy="joined",
    )
    topology_edges = db.relationship(
        "ApplicationServiceTopologyEdge",
        back_populates="service",
        cascade="all, delete-orphan",
        lazy="joined",
    )
    client_links = db.relationship(
        "ClientApplicationService",
        back_populates="service",
        cascade="all, delete-orphan",
        lazy="dynamic",
    )


class ApplicationServiceDeployment(db.Model):
    __tablename__ = "application_service_deployments"
    __table_args__ = (
        db.UniqueConstraint(
            "service_id", "cluster_id", "namespace", "deployment_name", "resource_kind",
            name="uq_app_service_deployment_v2",
        ),
        db.Index("ix_asd_service_id", "service_id"),
        db.Index("ix_asd_cluster_ns", "cluster_id", "namespace"),
    )

    id = db.Column(db.Integer, primary_key=True)
    service_id = db.Column(db.Integer, db.ForeignKey("application_services.id"), nullable=False)
    cluster_id = db.Column(db.String(120), nullable=False)
    namespace = db.Column(db.String(253), nullable=False)
    deployment_name = db.Column(db.String(253), nullable=False)
    resource_kind = db.Column(db.String(20), nullable=False, default="deployment")
    # Optional disaster-recovery counterpart for this component. Operator-linked
    # manually — the DR resource may live on a different cluster/namespace and
    # have a completely different name, so nothing here is autodetected.
    dr_cluster_id = db.Column(db.String(120), nullable=True)
    dr_namespace = db.Column(db.String(253), nullable=True)
    dr_resource_name = db.Column(db.String(253), nullable=True)
    dr_resource_kind = db.Column(db.String(20), nullable=True)
    created_at = db.Column(
        db.DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )

    service = db.relationship("ApplicationService", back_populates="deployments")


class ApplicationServiceTopologyNode(db.Model):
    __tablename__ = "application_service_topology_nodes"
    __table_args__ = (
        db.Index("ix_astn_service_id", "service_id"),
    )

    id = db.Column(db.Integer, primary_key=True)
    service_id = db.Column(db.Integer, db.ForeignKey("application_services.id"), nullable=False)
    name = db.Column(db.String(120), nullable=False)
    type = db.Column(db.String(80), nullable=True)
    description = db.Column(db.Text, nullable=True)
    linked_cluster_id = db.Column(db.String(120), nullable=True)
    linked_namespace = db.Column(db.String(253), nullable=True)
    linked_deployment = db.Column(db.String(253), nullable=True)
    # Optional reference to a predefined, reusable TopologyComponent (e.g. "WAF").
    component_id = db.Column(db.Integer, db.ForeignKey("topology_components.id"), nullable=True)
    position_x = db.Column(db.Float, nullable=True)
    position_y = db.Column(db.Float, nullable=True)
    created_at = db.Column(
        db.DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )
    updated_at = db.Column(
        db.DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )

    service = db.relationship("ApplicationService", back_populates="topology_nodes")
    component = db.relationship("TopologyComponent", foreign_keys=[component_id])


class ApplicationServiceTopologyEdge(db.Model):
    __tablename__ = "application_service_topology_edges"
    __table_args__ = (
        db.UniqueConstraint("service_id", "source_node_id", "target_node_id", name="uq_topology_edge"),
        db.Index("ix_aste_service_id", "service_id"),
        db.Index("ix_aste_source", "source_node_id"),
        db.Index("ix_aste_target", "target_node_id"),
    )

    id = db.Column(db.Integer, primary_key=True)
    service_id = db.Column(db.Integer, db.ForeignKey("application_services.id"), nullable=False)
    source_node_id = db.Column(db.Integer, db.ForeignKey("application_service_topology_nodes.id"), nullable=False)
    target_node_id = db.Column(db.Integer, db.ForeignKey("application_service_topology_nodes.id"), nullable=False)
    # Connection metadata: wire protocol (HTTP, TCP, gRPC, …), whether the
    # traffic is internal to the cluster or crosses an external boundary, and a
    # free-text description (e.g. IPs, ports, notes).
    protocol = db.Column(db.String(20), nullable=True)
    scope = db.Column(db.String(20), nullable=True)
    description = db.Column(db.Text, nullable=True)
    created_at = db.Column(
        db.DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )

    service = db.relationship("ApplicationService", back_populates="topology_edges")
    source_node = db.relationship("ApplicationServiceTopologyNode", foreign_keys=[source_node_id])
    target_node = db.relationship("ApplicationServiceTopologyNode", foreign_keys=[target_node_id])


class Client(db.Model):
    __tablename__ = "clients"

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(120), unique=True, nullable=False, index=True)
    contact_person = db.Column(db.String(255), nullable=True)
    email = db.Column(db.String(255), nullable=True)
    phone = db.Column(db.String(64), nullable=True)
    notes = db.Column(db.Text, nullable=True)
    created_at = db.Column(
        db.DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )
    updated_at = db.Column(
        db.DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )

    service_links = db.relationship(
        "ClientApplicationService",
        back_populates="client",
        cascade="all, delete-orphan",
        lazy="joined",
    )
    service_connections = db.relationship(
        "ClientServiceConnection",
        back_populates="client",
        cascade="all, delete-orphan",
        lazy="dynamic",
    )
    service_egress_connections = db.relationship(
        "ClientServiceEgressConnection",
        back_populates="client",
        cascade="all, delete-orphan",
        lazy="dynamic",
    )


class ClientApplicationService(db.Model):
    __tablename__ = "client_application_services"
    __table_args__ = (
        db.UniqueConstraint("client_id", "service_id", name="uq_client_service"),
        db.Index("ix_cas_client_id", "client_id"),
        db.Index("ix_cas_service_id", "service_id"),
    )

    id = db.Column(db.Integer, primary_key=True)
    client_id = db.Column(db.Integer, db.ForeignKey("clients.id"), nullable=False)
    service_id = db.Column(db.Integer, db.ForeignKey("application_services.id"), nullable=False)
    created_at = db.Column(
        db.DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )

    client = db.relationship("Client", back_populates="service_links")
    service = db.relationship("ApplicationService", back_populates="client_links")


class ClientServiceConnection(db.Model):
    """Client-specific connectivity overlay for a client↔service link.

    The reusable service topology (:class:`ApplicationServiceTopologyNode` /
    ``...Edge``) is never duplicated per client. Instead, each client-service
    pair carries a single connectivity overlay describing *how this particular
    client reaches the service* — source/destination IPs, transport, and the
    cluster/namespace/environment it lands in. The composed client topology
    endpoint prepends a client node and a transport node onto the shared service
    topology using these fields; the service topology itself is left untouched.
    """

    __tablename__ = "client_service_connections"
    __table_args__ = (
        db.UniqueConstraint("client_id", "service_id", name="uq_client_service_connection"),
        db.Index("ix_csc_client_id", "client_id"),
        db.Index("ix_csc_service_id", "service_id"),
    )

    id = db.Column(db.Integer, primary_key=True)
    client_id = db.Column(db.Integer, db.ForeignKey("clients.id"), nullable=False)
    service_id = db.Column(db.Integer, db.ForeignKey("application_services.id"), nullable=False)
    source_ip = db.Column(db.String(64), nullable=True)
    destination_ip = db.Column(db.String(64), nullable=True)
    # Optional NAT ("netted") addresses when the connection traverses NAT: the
    # translated source/destination the other side actually sees.
    netted_source_ip = db.Column(db.String(64), nullable=True)
    netted_destination_ip = db.Column(db.String(64), nullable=True)
    # VPN | Leased Line | MPLS | Internet | Private Link | Direct Connect |
    # Internal Network | Other  (validated in the service layer).
    transport_type = db.Column(db.String(32), nullable=True)
    # Free-text carrier / circuit name; required when transport_type is "Other".
    transport_name = db.Column(db.String(255), nullable=True)
    transport_notes = db.Column(db.Text, nullable=True)
    cluster_id = db.Column(db.String(120), nullable=True)
    namespace = db.Column(db.String(253), nullable=True)
    environment = db.Column(db.String(64), nullable=True)
    # JSON-encoded list of the service topology components this connection
    # attaches to, e.g. ``[{"ref": "3", "name": "persona-ms"}]``. The composed
    # client topology draws client → transport → each of these components. Empty
    # or null falls back to the service entrypoint (keeps pre-existing rows
    # rendering). Validated against live topology node ids in the service layer.
    component_refs = db.Column(db.Text, nullable=True)
    # Direction of the client↔service connectivity link drawn in the composed
    # topology: inbound (client → service), outbound (service → client), or both
    # (bidirectional). Validated in the service layer. Defaults to "inbound".
    direction = db.Column(db.String(16), nullable=False, default="inbound")
    # active | inactive | degraded | planned  (free-text-ish operational status).
    status = db.Column(db.String(32), nullable=False, default="active")
    is_active = db.Column(db.Boolean, nullable=False, default=True)
    created_at = db.Column(
        db.DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )
    updated_at = db.Column(
        db.DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )

    client = db.relationship("Client", back_populates="service_connections")
    service = db.relationship("ApplicationService")


class ClientServiceEgressConnection(db.Model):
    """DEPRECATED — superseded by :attr:`ClientServiceConnection.component_refs`.

    No longer referenced by the app (routes/service layer/UI were removed when
    inbound + egress were unified into a single per-client↔service connection
    with a direction and a multi-component selection). The table is retained so
    historical rows are not dropped; a later cleanup migration may remove it.

    Per-deployment *egress* connectivity: how a service component reaches a client.

    The counterpart to :class:`ClientServiceConnection` (which describes the
    inbound client → service path). This describes the reverse direction — a
    specific deployment/topology node inside the service reaching back out to the
    client (e.g. ``persona-ms → connectivity → AUDI``). It is keyed per topology
    node so each deployment in a service can carry its own independent egress
    config. Like the inbound overlay, the reusable service topology is never
    duplicated: the composed egress topology reverses the service topology edges
    and appends a transport node and client node onto the service entrypoint.
    """

    __tablename__ = "client_service_egress_connections"
    __table_args__ = (
        db.UniqueConstraint(
            "client_id", "service_id", "node_ref", name="uq_client_service_egress_connection"
        ),
        db.Index("ix_csec_client_id", "client_id"),
        db.Index("ix_csec_service_id", "service_id"),
    )

    id = db.Column(db.Integer, primary_key=True)
    client_id = db.Column(db.Integer, db.ForeignKey("clients.id"), nullable=False)
    service_id = db.Column(db.Integer, db.ForeignKey("application_services.id"), nullable=False)
    # Identifies the source topology node (deployment) within the service. Stored
    # as a string so it works for both integer node ids and synthetic ids.
    node_ref = db.Column(db.String(120), nullable=False)
    # Snapshot of the node's display name, kept for resilience if the node id
    # changes but the deployment is re-identified by name.
    node_name = db.Column(db.String(253), nullable=True)
    source_ip = db.Column(db.String(64), nullable=True)
    destination_ip = db.Column(db.String(64), nullable=True)
    # VPN | Leased Line | MPLS | Internet | Private Link | Direct Connect |
    # Internal Network | Other  (validated in the service layer).
    transport_type = db.Column(db.String(32), nullable=True)
    # Free-text carrier / circuit name; required when transport_type is "Other".
    transport_name = db.Column(db.String(255), nullable=True)
    transport_notes = db.Column(db.Text, nullable=True)
    # Direction of the *direct* deployment↔client link drawn on top of the
    # reversed service chain: outbound (deployment → client), inbound
    # (client → deployment), or both (bidirectional). Validated in the service
    # layer. Defaults to "outbound".
    direction = db.Column(db.String(16), nullable=False, default="outbound")
    # active | inactive | degraded | planned  (free-text-ish operational status).
    status = db.Column(db.String(32), nullable=False, default="active")
    is_active = db.Column(db.Boolean, nullable=False, default=True)
    created_at = db.Column(
        db.DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )
    updated_at = db.Column(
        db.DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )

    client = db.relationship("Client", back_populates="service_egress_connections")
    service = db.relationship("ApplicationService")


class UserTemplate(db.Model):
    """Admin-authored application templates for the Application Builder marketplace.

    Stored alongside the built-in templates in wizard_templates.py. The full
    builder spec (containers, resources, networking, etc.) lives in `spec`; the
    top-level columns mirror the summary fields the marketplace lists by.
    """

    __tablename__ = "user_templates"

    id = db.Column(db.Integer, primary_key=True)
    slug = db.Column(db.String(120), unique=True, nullable=False, index=True)
    name = db.Column(db.String(120), nullable=False)
    description = db.Column(db.String(500), nullable=True)
    category = db.Column(db.String(80), nullable=False, default="Custom")
    workload_type = db.Column(db.String(40), nullable=False, default="Deployment")
    spec = db.Column(db.JSON, nullable=False, default=dict)
    created_by = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=True, index=True)
    created_at = db.Column(
        db.DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )
    updated_at = db.Column(
        db.DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )

    creator = db.relationship("User", foreign_keys=[created_by])


class HelmChartTemplate(db.Model):
    """Reusable Helm charts imported from Kubernetes YAML or Git.

    ``definition`` contains the chart's relative files (base64 encoded), the
    scrubbed default values, generated input descriptors, and import warnings.
    Git credentials are deliberately never part of this model.
    """

    __tablename__ = "helm_chart_templates"

    id = db.Column(db.Integer, primary_key=True)
    slug = db.Column(db.String(120), unique=True, nullable=False, index=True)
    name = db.Column(db.String(120), nullable=False)
    description = db.Column(db.String(500), nullable=True)
    version = db.Column(db.String(64), nullable=False, default="0.1.0")
    app_version = db.Column(db.String(64), nullable=True)
    source_type = db.Column(db.String(32), nullable=False)
    source_ref = db.Column(db.String(1000), nullable=True)
    definition = db.Column(db.JSON, nullable=False, default=dict)
    created_by = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=True, index=True)
    created_at = db.Column(
        db.DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )
    updated_at = db.Column(
        db.DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )

    creator = db.relationship("User", foreign_keys=[created_by])
    versions = db.relationship(
        "HelmChartTemplateVersion",
        back_populates="template",
        cascade="all, delete-orphan",
        order_by="HelmChartTemplateVersion.created_at",
    )


class HelmChartTemplateVersion(db.Model):
    """One uploaded revision of a reusable Helm chart.

    Every version carries its own complete ``definition`` (files, values,
    generated inputs, warnings, inspection) so an older revision stays
    deployable unchanged. The parent row mirrors whichever version is current,
    which keeps every single-version code path behaving exactly as before.
    """

    __tablename__ = "helm_chart_template_versions"
    __table_args__ = (
        db.UniqueConstraint("template_id", "version", name="uq_helm_chart_template_version"),
    )

    id = db.Column(db.Integer, primary_key=True)
    template_id = db.Column(
        db.Integer,
        db.ForeignKey("helm_chart_templates.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    version = db.Column(db.String(64), nullable=False)
    app_version = db.Column(db.String(64), nullable=True)
    description = db.Column(db.String(500), nullable=True)
    source_type = db.Column(db.String(32), nullable=False)
    source_ref = db.Column(db.String(1000), nullable=True)
    definition = db.Column(db.JSON, nullable=False, default=dict)
    created_by = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=True, index=True)
    created_at = db.Column(
        db.DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )

    template = db.relationship("HelmChartTemplate", back_populates="versions")
    creator = db.relationship("User", foreign_keys=[created_by])


class DeploymentRequest(db.Model):
    """A user request to deploy or change something in a cluster.

    Created from the Clusters tab; routed to the management team by email with
    signed approve/decline links. Approval/decline is recorded back here.
    """

    __tablename__ = "deployment_requests"
    __table_args__ = (
        db.Index("ix_deployment_request_status_created", "status", "created_at"),
    )

    id = db.Column(db.Integer, primary_key=True)
    requester_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=True, index=True)
    cluster_id = db.Column(db.String(120), nullable=False, index=True)
    cluster_name = db.Column(db.String(255), nullable=False, default="")
    message = db.Column(db.Text, nullable=False)
    status = db.Column(db.String(16), nullable=False, default="pending", index=True)
    # Quorum snapshot taken at creation time so later config changes don't alter
    # in-flight requests.
    required_approvals = db.Column(db.Integer, nullable=False, default=1)
    total_recipients = db.Column(db.Integer, nullable=False, default=1)
    # Optional preferred maintenance window the requester wants the work done in.
    # Stored as timezone-aware UTC; the IANA zone the requester entered them in is
    # kept so approvers see the window in the original (e.g. Beirut) local time.
    requested_window_start = db.Column(db.DateTime(timezone=True), nullable=True)
    requested_window_end = db.Column(db.DateTime(timezone=True), nullable=True)
    requested_window_timezone = db.Column(db.String(64), nullable=True)
    decided_by_user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=True)
    decided_at = db.Column(db.DateTime(timezone=True), nullable=True)
    created_at = db.Column(
        db.DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
        index=True,
    )

    requester = db.relationship("User", foreign_keys=[requester_id])
    decided_by = db.relationship("User", foreign_keys=[decided_by_user_id])
    votes = db.relationship(
        "DeploymentRequestVote",
        back_populates="request",
        cascade="all, delete-orphan",
        lazy="joined",
    )


class DeploymentRequestVote(db.Model):
    """An individual approver's approve/decline vote on a request (quorum)."""

    __tablename__ = "deployment_request_votes"
    __table_args__ = (
        db.UniqueConstraint("request_id", "voter_email", name="uq_deployment_request_voter"),
    )

    id = db.Column(db.Integer, primary_key=True)
    request_id = db.Column(
        db.Integer,
        db.ForeignKey("deployment_requests.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    voter_email = db.Column(db.String(255), nullable=False)
    decision = db.Column(db.String(16), nullable=False)  # approve | decline
    created_at = db.Column(
        db.DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )

    request = db.relationship("DeploymentRequest", back_populates="votes")


class DeploymentRequestSetting(db.Model):
    """Singleton config for the deployment-request workflow.

    Holds the admin-configured list of management recipients that deployment
    requests are emailed to. Empty means "fall back to env / alert-routing".
    """

    __tablename__ = "deployment_request_settings"

    id = db.Column(db.Integer, primary_key=True)
    recipients = db.Column(db.JSON, nullable=False, default=list)
    # IDs of AlertRoutingReceiverGroup whose email members are approvers.
    group_ids = db.Column(db.JSON, nullable=False, default=list)
    # How many approvals are required before a request is approved.
    required_approvals = db.Column(db.Integer, nullable=False, default=1)
    # Per-cluster overrides: map of clusterId -> required approvals (0 = none).
    # Unset clusters fall back to ``required_approvals``.
    cluster_required_approvals = db.Column(db.JSON, nullable=False, default=dict)
    # Post-execution pod-health watch on bundle-applied deployments: how long
    # pods get to become ready, and whether a timeout triggers `rollout undo`
    # (mirrors the deploy-automation safety net).
    rollout_timeout_minutes = db.Column(db.Integer, nullable=False, default=15)
    rollback_on_failure = db.Column(db.Boolean, nullable=False, default=True)
    updated_at = db.Column(
        db.DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )


class ChangeBundle(db.Model):
    """A "shopping cart" of staged Kubernetes changes submitted for one approval.

    A requester stages multiple change actions (create from template, edit YAML,
    change image, scale, env/resource/HPA updates, delete) into a draft bundle,
    then submits it with a requested deployment window. The bundle reuses the
    deployment-request approval audience (quorum + signed email links). On
    approval the background scheduler auto-executes each item when the window
    opens, recording per-item results.

    Status lifecycle:
        draft -> pending_approval -> approved | rejected
        approved -> scheduled -> deploying -> completed | failed | partially_failed
        approved/scheduled -> expired (window ended before execution)
    """

    __tablename__ = "change_bundles"
    __table_args__ = (
        db.Index("ix_change_bundle_status_created", "status", "created_at"),
        db.Index("ix_change_bundle_requester_status", "requester_user_id", "status"),
    )

    id = db.Column(db.Integer, primary_key=True)
    requester_user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=True, index=True)
    status = db.Column(db.String(24), nullable=False, default="draft", index=True)
    note = db.Column(db.Text, nullable=True)
    # Preferred deployment window. Stored timezone-aware UTC; the IANA zone the
    # requester entered it in is kept so approvers see the original local time.
    requested_start_time = db.Column(db.DateTime(timezone=True), nullable=True)
    requested_end_time = db.Column(db.DateTime(timezone=True), nullable=True)
    requested_window_timezone = db.Column(db.String(64), nullable=True)
    # Quorum snapshot taken at submission so later config changes don't alter
    # an in-flight bundle. required = max per-cluster requirement across items.
    required_approvals = db.Column(db.Integer, nullable=False, default=1)
    total_recipients = db.Column(db.Integer, nullable=False, default=1)
    # If true, stop executing remaining items after the first failure (default).
    stop_on_failure = db.Column(db.Boolean, nullable=False, default=True)
    approved_by_user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=True)
    approved_at = db.Column(db.DateTime(timezone=True), nullable=True)
    rejection_reason = db.Column(db.Text, nullable=True)
    execution_started_at = db.Column(db.DateTime(timezone=True), nullable=True)
    execution_finished_at = db.Column(db.DateTime(timezone=True), nullable=True)
    created_at = db.Column(
        db.DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
        index=True,
    )
    updated_at = db.Column(
        db.DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )

    requester = db.relationship("User", foreign_keys=[requester_user_id])
    approved_by = db.relationship("User", foreign_keys=[approved_by_user_id])
    items = db.relationship(
        "ChangeBundleItem",
        back_populates="bundle",
        cascade="all, delete-orphan",
        order_by="ChangeBundleItem.position",
        lazy="joined",
    )
    votes = db.relationship(
        "ChangeBundleVote",
        back_populates="bundle",
        cascade="all, delete-orphan",
        lazy="joined",
    )


class ChangeBundleItem(db.Model):
    """One staged change action within a :class:`ChangeBundle`."""

    __tablename__ = "change_bundle_items"
    __table_args__ = (
        db.Index("ix_change_bundle_item_bundle_pos", "bundle_id", "position"),
    )

    id = db.Column(db.Integer, primary_key=True)
    bundle_id = db.Column(
        db.Integer,
        db.ForeignKey("change_bundles.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    position = db.Column(db.Integer, nullable=False, default=0)
    # create_from_template | edit_deployment | change_image | scale_replicas |
    # update_env | update_resources | update_hpa | delete_deployment
    action_type = db.Column(db.String(32), nullable=False)
    cluster_id = db.Column(db.String(120), nullable=False, index=True)
    cluster_name = db.Column(db.String(255), nullable=False, default="")
    namespace = db.Column(db.String(253), nullable=False, default="")
    resource_kind = db.Column(db.String(40), nullable=False, default="Deployment")
    resource_name = db.Column(db.String(253), nullable=False, default="")
    old_payload_json = db.Column(db.JSON, nullable=True)
    new_payload_json = db.Column(db.JSON, nullable=True)
    yaml_preview = db.Column(db.Text, nullable=True)
    validation_status = db.Column(db.String(16), nullable=False, default="pending")
    validation_message = db.Column(db.Text, nullable=True)
    # pending | applying | succeeded | failed | skipped
    status = db.Column(db.String(16), nullable=False, default="pending")
    execution_result = db.Column(db.JSON, nullable=True)
    created_at = db.Column(
        db.DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )
    updated_at = db.Column(
        db.DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )

    bundle = db.relationship("ChangeBundle", back_populates="items")


class ChangeBundleVote(db.Model):
    """An individual approver's approve/decline vote on a bundle (quorum)."""

    __tablename__ = "change_bundle_votes"
    __table_args__ = (
        db.UniqueConstraint("bundle_id", "voter_email", name="uq_change_bundle_voter"),
    )

    id = db.Column(db.Integer, primary_key=True)
    bundle_id = db.Column(
        db.Integer,
        db.ForeignKey("change_bundles.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    voter_email = db.Column(db.String(255), nullable=False)
    decision = db.Column(db.String(16), nullable=False)  # approve | decline
    created_at = db.Column(
        db.DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )

    bundle = db.relationship("ChangeBundle", back_populates="votes")


class BundleRolloutWatch(db.Model):
    """Post-execution pod-health watch for one deployment a Change Bundle applied.

    Created by the bundle executor for every successfully-applied Deployment
    item (unless a deploy-automation run already watches that bundle) and
    advanced on the scheduler tick: the deployment must report ready within the
    configured timeout or the watch fails — optionally ``kubectl rollout undo``
    — and the requester + admins are emailed. Status: ``watching`` (active) →
    ``healthy`` | ``failed``.
    """

    __tablename__ = "bundle_rollout_watches"
    __table_args__ = (db.Index("ix_bundle_watch_status", "status"),)

    id = db.Column(db.Integer, primary_key=True)
    bundle_id = db.Column(db.Integer, nullable=False, index=True)
    item_id = db.Column(db.Integer, nullable=True)
    cluster_id = db.Column(db.String(120), nullable=False)
    namespace = db.Column(db.String(253), nullable=False)
    deployment_name = db.Column(db.String(253), nullable=False)

    status = db.Column(db.String(16), nullable=False, default="watching")
    detail = db.Column(db.Text, nullable=True)
    rolled_back = db.Column(db.Boolean, nullable=False, default=False)

    started_at = db.Column(
        db.DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )
    finished_at = db.Column(db.DateTime(timezone=True), nullable=True)


class ApiToken(db.Model):
    __tablename__ = "api_tokens"

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False, index=True)
    name = db.Column(db.String(120), nullable=False)
    token_prefix = db.Column(db.String(16), nullable=False)
    token_hash = db.Column(db.String(64), nullable=False, unique=True, index=True)
    is_active = db.Column(db.Boolean, nullable=False, default=True, index=True)
    expires_at = db.Column(db.DateTime(timezone=True), nullable=True)
    last_used_at = db.Column(db.DateTime(timezone=True), nullable=True)
    created_at = db.Column(
        db.DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )

    user = db.relationship("User", foreign_keys=[user_id])


# ---------------------------------------------------------------------------
# Service Catalog — reusable business service blueprints
# ---------------------------------------------------------------------------
#
# A ServiceBlueprint describes the *logical* architecture of a reusable business
# service (e.g. "QR Code Service") using general components (Frontend, Backend,
# Database, ...) and logical connections — independent of any real Kubernetes
# object name. When deployed (Deploy From Blueprint), an AppService instance is
# created and each logical component is mapped to a real/created/external/skipped
# resource via AppServiceComponentMapping. Runtime topology is resolved from the
# blueprint + mappings, never from hardcoded object names.

class ServiceBlueprint(db.Model):
    __tablename__ = "service_blueprints"

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(120), unique=True, nullable=False, index=True)
    description = db.Column(db.Text, nullable=True)
    category = db.Column(db.String(80), nullable=True)
    owner_team = db.Column(db.String(255), nullable=True)
    criticality = db.Column(db.String(32), nullable=True)
    # draft | ready | deprecated
    status = db.Column(db.String(16), nullable=False, default="draft", index=True)
    version = db.Column(db.String(32), nullable=False, default="1.0.0")
    created_by_user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=True, index=True)
    created_at = db.Column(
        db.DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )
    updated_at = db.Column(
        db.DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )

    created_by = db.relationship("User", foreign_keys=[created_by_user_id])
    components = db.relationship(
        "ServiceBlueprintComponent",
        back_populates="blueprint",
        cascade="all, delete-orphan",
        lazy="joined",
        order_by="ServiceBlueprintComponent.position",
    )
    connections = db.relationship(
        "ServiceBlueprintConnection",
        back_populates="blueprint",
        cascade="all, delete-orphan",
        lazy="joined",
    )
    requirements = db.relationship(
        "ServiceBlueprintRequirement",
        back_populates="blueprint",
        cascade="all, delete-orphan",
        lazy="joined",
    )
    app_services = db.relationship(
        "AppService",
        back_populates="blueprint",
        lazy="dynamic",
    )


class ServiceBlueprintComponent(db.Model):
    __tablename__ = "service_blueprint_components"
    __table_args__ = (
        db.Index("ix_sbc_blueprint_id", "blueprint_id"),
    )

    id = db.Column(db.Integer, primary_key=True)
    blueprint_id = db.Column(
        db.Integer,
        db.ForeignKey("service_blueprints.id", ondelete="CASCADE"),
        nullable=False,
    )
    name = db.Column(db.String(120), nullable=False)
    role = db.Column(db.String(120), nullable=True)
    # deployment | statefulset | daemonset | cronjob | service | ingress |
    # database | redis | kafka | worker | external_service | ...
    component_type = db.Column(db.String(48), nullable=False, default="deployment")
    required = db.Column(db.Boolean, nullable=False, default=True)
    # Whether this component may be satisfied by an external dependency.
    supports_external = db.Column(db.Boolean, nullable=False, default=False)
    # Slug or id of a KubeSight deployment template used as the create-new default.
    default_template_id = db.Column(db.String(120), nullable=True)
    description = db.Column(db.Text, nullable=True)
    # Smart defaults applied during Deploy From Blueprint (all optional JSON).
    config_schema = db.Column(db.JSON, nullable=True)
    default_values = db.Column(db.JSON, nullable=True)
    validation_rules = db.Column(db.JSON, nullable=True)
    default_port = db.Column(db.Integer, nullable=True)
    default_resources = db.Column(db.JSON, nullable=True)
    default_health = db.Column(db.JSON, nullable=True)
    default_hpa = db.Column(db.JSON, nullable=True)
    # Topology builder canvas position.
    position_x = db.Column(db.Float, nullable=True)
    position_y = db.Column(db.Float, nullable=True)
    position = db.Column(db.Integer, nullable=False, default=0)
    created_at = db.Column(
        db.DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )

    blueprint = db.relationship("ServiceBlueprint", back_populates="components")


class ServiceBlueprintConnection(db.Model):
    __tablename__ = "service_blueprint_connections"
    __table_args__ = (
        db.Index("ix_sbcn_blueprint_id", "blueprint_id"),
    )

    id = db.Column(db.Integer, primary_key=True)
    blueprint_id = db.Column(
        db.Integer,
        db.ForeignKey("service_blueprints.id", ondelete="CASCADE"),
        nullable=False,
    )
    source_component_id = db.Column(
        db.Integer,
        db.ForeignKey("service_blueprint_components.id", ondelete="CASCADE"),
        nullable=False,
    )
    target_component_id = db.Column(
        db.Integer,
        db.ForeignKey("service_blueprint_components.id", ondelete="CASCADE"),
        nullable=False,
    )
    connection_type = db.Column(db.String(32), nullable=True)
    protocol = db.Column(db.String(20), nullable=True)
    port = db.Column(db.Integer, nullable=True)
    description = db.Column(db.Text, nullable=True)
    created_at = db.Column(
        db.DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )

    blueprint = db.relationship("ServiceBlueprint", back_populates="connections")
    source_component = db.relationship(
        "ServiceBlueprintComponent", foreign_keys=[source_component_id]
    )
    target_component = db.relationship(
        "ServiceBlueprintComponent", foreign_keys=[target_component_id]
    )


class ServiceBlueprintRequirement(db.Model):
    __tablename__ = "service_blueprint_requirements"
    __table_args__ = (
        db.Index("ix_sbr_blueprint_id", "blueprint_id"),
    )

    id = db.Column(db.Integer, primary_key=True)
    blueprint_id = db.Column(
        db.Integer,
        db.ForeignKey("service_blueprints.id", ondelete="CASCADE"),
        nullable=False,
    )
    # Optional: requirement scoped to a single component.
    component_id = db.Column(
        db.Integer,
        db.ForeignKey("service_blueprint_components.id", ondelete="CASCADE"),
        nullable=True,
    )
    key = db.Column(db.String(120), nullable=False)
    # env_var | secret | configmap | pvc | ingress_host | tls_secret |
    # image_pull_secret | hpa | resource_limit | database_credential |
    # external_endpoint | ...
    requirement_type = db.Column(db.String(48), nullable=False, default="env_var")
    required = db.Column(db.Boolean, nullable=False, default=True)
    default_value = db.Column(db.Text, nullable=True)
    allowed_values = db.Column(db.JSON, nullable=True)
    # manual | dropdown | existing_secret | existing_configmap | generated |
    # blueprint_default | detected_from_cluster
    value_source = db.Column(db.String(32), nullable=False, default="manual")
    secret = db.Column(db.Boolean, nullable=False, default=False)
    auto_generate = db.Column(db.Boolean, nullable=False, default=False)
    description = db.Column(db.Text, nullable=True)
    created_at = db.Column(
        db.DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )

    blueprint = db.relationship("ServiceBlueprint", back_populates="requirements")


class AppService(db.Model):
    """A real deployed service instance created from a ServiceBlueprint.

    Distinct from :class:`ApplicationService` (the manually-curated App Services
    tab): an AppService is the blueprint-aware instance that ties a blueprint to
    a client/environment/cluster/namespace and records how each logical component
    maps to real Kubernetes resources.
    """

    __tablename__ = "app_services"
    __table_args__ = (
        db.Index("ix_app_service_client", "client_id"),
        db.Index("ix_app_service_blueprint", "blueprint_id"),
        db.Index("ix_app_service_cluster_ns", "cluster_id", "namespace"),
    )

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(180), unique=True, nullable=False, index=True)
    # Stable slug used in labels (kubesight.io/app-service-id).
    slug = db.Column(db.String(180), nullable=True, index=True)
    description = db.Column(db.Text, nullable=True)
    client_id = db.Column(db.Integer, db.ForeignKey("clients.id"), nullable=True)
    blueprint_id = db.Column(
        db.Integer, db.ForeignKey("service_blueprints.id"), nullable=True
    )
    # Bridge to the operational App Services tab (ApplicationService) created on
    # deploy so the instance surfaces there with health/topology/workloads.
    application_service_id = db.Column(
        db.Integer, db.ForeignKey("application_services.id"), nullable=True
    )
    environment = db.Column(db.String(32), nullable=True)
    cluster_id = db.Column(db.String(120), nullable=True, index=True)
    namespace = db.Column(db.String(253), nullable=True)
    # planned | deploying | active | degraded | failed
    status = db.Column(db.String(24), nullable=False, default="planned", index=True)
    created_by_user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=True, index=True)
    created_at = db.Column(
        db.DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )
    updated_at = db.Column(
        db.DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )

    blueprint = db.relationship("ServiceBlueprint", back_populates="app_services")
    client = db.relationship("Client", foreign_keys=[client_id])
    created_by = db.relationship("User", foreign_keys=[created_by_user_id])
    component_mappings = db.relationship(
        "AppServiceComponentMapping",
        back_populates="app_service",
        cascade="all, delete-orphan",
        lazy="joined",
    )


class AppServiceComponentMapping(db.Model):
    __tablename__ = "app_service_component_mappings"
    __table_args__ = (
        db.Index("ix_ascm_app_service", "app_service_id"),
        db.Index("ix_ascm_component", "blueprint_component_id"),
    )

    id = db.Column(db.Integer, primary_key=True)
    app_service_id = db.Column(
        db.Integer,
        db.ForeignKey("app_services.id", ondelete="CASCADE"),
        nullable=False,
    )
    blueprint_component_id = db.Column(
        db.Integer,
        db.ForeignKey("service_blueprint_components.id"),
        nullable=True,
    )
    # Denormalized component identity so runtime topology renders even if the
    # blueprint component is later renamed/removed.
    component_name = db.Column(db.String(120), nullable=True)
    component_role = db.Column(db.String(120), nullable=True)
    # create_new | existing_resource | external_dependency | skip
    mapping_type = db.Column(db.String(24), nullable=False, default="create_new")
    kubernetes_kind = db.Column(db.String(40), nullable=True)
    kubernetes_name = db.Column(db.String(253), nullable=True)
    namespace = db.Column(db.String(253), nullable=True)
    cluster_id = db.Column(db.String(120), nullable=True)
    external_endpoint = db.Column(db.String(512), nullable=True)
    # planned | created | linked | skipped | failed
    status = db.Column(db.String(24), nullable=False, default="planned")
    # Auto-generated resource name (create_new) before it is materialized.
    generated_name = db.Column(db.String(253), nullable=True)
    # kubesight.io/* labels recorded for this mapping.
    labels = db.Column(db.JSON, nullable=True)
    # Resolved per-component values (ports, image tag, template, overrides, ...).
    config = db.Column(db.JSON, nullable=True)
    created_at = db.Column(
        db.DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )

    app_service = db.relationship("AppService", back_populates="component_mappings")
    blueprint_component = db.relationship(
        "ServiceBlueprintComponent", foreign_keys=[blueprint_component_id]
    )


# ---------------------------------------------------------------------------
# Topology Components
#
# A reusable, predefined building block (e.g. "WAF", "API Gateway") that can be
# dropped into an application service's topology. Each component carries an
# optional health check (outbound HTTP/TCP probe, or an inbound webhook
# heartbeat) so its current status can be shown in a table and on the topology.
# ---------------------------------------------------------------------------

class TopologyComponent(db.Model):
    __tablename__ = "topology_components"

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(120), unique=True, nullable=False, index=True)
    # Free-text category shown as the node "type" (e.g. Security, Gateway, Cache).
    category = db.Column(db.String(80), nullable=True)
    description = db.Column(db.Text, nullable=True)

    # Health check configuration.
    # none | http | tcp | webhook
    check_type = db.Column(db.String(16), nullable=False, default="none")
    health_check_url = db.Column(db.String(512), nullable=True)   # http
    tcp_host = db.Column(db.String(253), nullable=True)            # tcp
    tcp_port = db.Column(db.Integer, nullable=True)               # tcp
    webhook_token = db.Column(db.String(64), nullable=True)        # webhook (inbound)
    # For webhook checks: a heartbeat older than this many seconds is unhealthy.
    heartbeat_interval_seconds = db.Column(db.Integer, nullable=True, default=300)
    last_heartbeat_at = db.Column(db.DateTime(timezone=True), nullable=True)

    # Last computed health: healthy | degraded | unhealthy | unknown
    last_status = db.Column(db.String(16), nullable=True)
    last_message = db.Column(db.Text, nullable=True)
    last_checked_at = db.Column(db.DateTime(timezone=True), nullable=True)

    created_by = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=True, index=True)
    created_at = db.Column(
        db.DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )
    updated_at = db.Column(
        db.DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )

    created_by_user = db.relationship("User", foreign_keys=[created_by])


# ---------------------------------------------------------------------------
# Smart Deployment Form — offline Excel round-trip for the Deploy Wizard
# ---------------------------------------------------------------------------
#
# A deployment form is generated *from* a UserTemplate (the source of truth): the
# template's defaults + schema become a fillable .xlsx. The filled file is uploaded
# back, parsed into the wizard's ``answers`` shape, re-validated against the current
# template + live cluster, and used to prefill the Deploy Wizard. The Excel never
# becomes the deploy payload — ``resolve_template`` still merges the template with the
# parsed answers. These two tables track generations and imports for auditing, form
# forgery/expiry checks, and re-validation.

class DeploymentFormGeneration(db.Model):
    """A generated deployment form (one downloaded .xlsx) issued from a template."""

    __tablename__ = "deployment_form_generations"
    __table_args__ = (
        db.Index("ix_deployment_form_gen_template", "template_slug", "created_at"),
    )

    id = db.Column(db.Integer, primary_key=True)
    # Public form id embedded in the workbook's hidden metadata sheet.
    form_uid = db.Column(db.String(48), unique=True, nullable=False, index=True)
    template_slug = db.Column(db.String(120), nullable=False, index=True)
    # Content-hash of the template detail at generation time (Kubesight has no
    # version table); import compares this to detect template drift.
    template_version = db.Column(db.String(64), nullable=False, default="")
    schema_version = db.Column(db.Integer, nullable=False, default=1)
    generated_by = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=True, index=True)
    cluster_id = db.Column(db.String(120), nullable=True)
    namespace = db.Column(db.String(253), nullable=True)
    # The form field schema baked into the workbook + the metadata block.
    schema_json = db.Column(db.JSON, nullable=False, default=dict)
    metadata_json = db.Column(db.JSON, nullable=False, default=dict)
    # active | expired | consumed
    status = db.Column(db.String(16), nullable=False, default="active", index=True)
    created_at = db.Column(
        db.DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )
    expires_at = db.Column(db.DateTime(timezone=True), nullable=True)

    generated_by_user = db.relationship("User", foreign_keys=[generated_by])


class DeploymentFormImport(db.Model):
    """An uploaded deployment form after parsing + validation.

    ``parsed_answers_json`` holds the wizard ``answers`` reconstructed from the
    workbook; ``validation_result_json`` holds the structured ✅/⚠️/❌ result the UI
    renders. Nothing here is deployed — it only prefills the wizard / bundle.
    """

    __tablename__ = "deployment_form_imports"
    __table_args__ = (
        db.Index("ix_deployment_form_import_status", "status", "created_at"),
    )

    id = db.Column(db.Integer, primary_key=True)
    generation_id = db.Column(
        db.Integer,
        db.ForeignKey("deployment_form_generations.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    form_uid = db.Column(db.String(48), nullable=True, index=True)
    template_slug = db.Column(db.String(120), nullable=False, index=True)
    template_version = db.Column(db.String(64), nullable=False, default="")
    uploaded_by = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=True, index=True)
    cluster_id = db.Column(db.String(120), nullable=True)
    namespace = db.Column(db.String(253), nullable=True)
    parsed_answers_json = db.Column(db.JSON, nullable=False, default=dict)
    validation_result_json = db.Column(db.JSON, nullable=False, default=dict)
    # parsed | valid | invalid | applied | bundled | submitted
    status = db.Column(db.String(16), nullable=False, default="parsed", index=True)
    created_at = db.Column(
        db.DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )
    updated_at = db.Column(
        db.DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )

    generation = db.relationship("DeploymentFormGeneration", foreign_keys=[generation_id])
    uploaded_by_user = db.relationship("User", foreign_keys=[uploaded_by])


class RegistryConnection(db.Model):
    """A linked container image registry (e.g. Sonatype Nexus).

    Before a deploy, KubeSight can query the registry's Docker Registry HTTP API
    V2 to confirm each container image actually exists — a cheap ``HEAD`` on the
    manifest, no layer pull. ``enforcement`` decides what happens when an image is
    missing: ``block`` fails the dry-run/apply, ``warn`` surfaces a warning, ``off``
    skips the check. Only images whose registry host matches ``base_url`` are
    checked against this connection; everything else is left to Kubernetes.
    """

    __tablename__ = "registry_connections"

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(120), nullable=False, default="")
    # nexus | generic (any Docker Registry V2 endpoint)
    registry_type = db.Column(db.String(32), nullable=False, default="nexus")
    # How the backend REACHES the registry V2 API — a host[:port] or full URL,
    # e.g. "nexus.example.com:8083" or "https://10.0.0.5:8083". May be an IP.
    base_url = db.Column(db.String(255), nullable=False, default="")
    # Host(s) as they appear in image REFERENCES (comma-separated), e.g.
    # "registry.example.com". An image is matched to this connection when its
    # registry host is base_url's host OR any of these. Lets you connect by IP
    # but still match images pulled by hostname.
    image_hosts = db.Column(db.Text, nullable=True)
    # none | basic (bearer is auto-negotiated from a WWW-Authenticate challenge)
    auth_mode = db.Column(db.String(16), nullable=False, default="basic")
    username = db.Column(db.String(255), nullable=False, default="")
    password_encrypted = db.Column(db.Text, nullable=True)
    verify_tls = db.Column(db.Boolean, nullable=False, default=True)
    ca_cert = db.Column(db.Text, nullable=True)
    # off | warn | block
    enforcement = db.Column(db.String(8), nullable=False, default="block")
    enabled = db.Column(db.Boolean, nullable=False, default=True)
    last_test_at = db.Column(db.DateTime(timezone=True), nullable=True)
    last_test_status = db.Column(db.String(16), nullable=True)
    last_test_message = db.Column(db.Text, nullable=True)
    created_at = db.Column(
        db.DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )
    updated_at = db.Column(
        db.DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )


class ZohoIntegration(db.Model):
    """Configuration for the Zoho Desk "DevOps Request" field-sync integration.

    A single-row config (id is always 1). KubeSight is the source of truth for
    deployed services: a background job publishes the AppService list into a Zoho
    Desk picklist as composite labels ("Client / Application / Environment · #id"),
    each carrying its AppService primary key so an inbound ticket resolves to an
    exact service instead of fuzzy-matching a free-text name. See
    DEVOPS-REQUEST-FIELD-SYNC-CONFIG.md for the Zoho-side field/layout IDs.

    OAuth is a server-to-server "Self Client" acting as the zagent service
    account; the client secret + refresh token are Fernet-encrypted at rest (same
    treatment as registry/SMTP secrets). The inbound webhook is authenticated by a
    shared secret, also encrypted at rest.
    """

    __tablename__ = "zoho_integration"

    id = db.Column(db.Integer, primary_key=True)
    enabled = db.Column(db.Boolean, nullable=False, default=False)

    # --- Zoho connection (non-secret; see the field-sync spec for real values) ---
    api_base = db.Column(db.String(255), nullable=False, default="https://desk.zoho.com/api/v1")
    accounts_base = db.Column(db.String(255), nullable=False, default="https://accounts.zoho.com")
    org_id = db.Column(db.String(64), nullable=False, default="")
    department_id = db.Column(db.String(64), nullable=True)
    layout_id = db.Column(db.String(64), nullable=False, default="")
    # The picklist field this integration publishes DEPLOYMENT names into (cf_application).
    app_field_id = db.Column(db.String(64), nullable=False, default="")
    app_field_api_name = db.Column(db.String(120), nullable=False, default="cf_application")
    # Optional second picklist we publish the NAMESPACE list into (cf_environment).
    # When blank, the environment/namespace dropdown is not managed.
    environment_field_id = db.Column(db.String(64), nullable=True)
    environment_field_api_name = db.Column(db.String(120), nullable=False, default="cf_environment")
    # Which inbound-ticket field carries the version/tag (free text, e.g. cf_tag).
    tag_field_api_name = db.Column(db.String(120), nullable=False, default="cf_tag")
    # Optional third picklist we publish deployment ENV-VAR NAMES into (cf_variable).
    # A ticket carrying a variable + value (instead of a tag) becomes a
    # variable-change automation run. When blank, the field is not managed.
    variable_field_id = db.Column(db.String(64), nullable=True)
    variable_field_api_name = db.Column(db.String(120), nullable=False, default="cf_variable")
    # Which inbound-ticket field carries the variable's new value (free text).
    value_field_api_name = db.Column(db.String(120), nullable=False, default="cf_value")

    # --- OAuth (server-to-server Self Client) ---
    client_id = db.Column(db.String(255), nullable=False, default="")
    client_secret_encrypted = db.Column(db.Text, nullable=True)
    refresh_token_encrypted = db.Column(db.Text, nullable=True)
    token_endpoint = db.Column(
        db.String(255), nullable=False, default="https://accounts.zoho.com/oauth/v2/token"
    )

    # --- Inbound webhook ---
    inbound_secret_encrypted = db.Column(db.Text, nullable=True)

    # --- Sync behaviour ---
    # Comma-separated AppService statuses to publish (default: active,degraded).
    status_filter = db.Column(db.String(120), nullable=False, default="active,degraded")
    sync_interval_minutes = db.Column(db.Integer, nullable=False, default=30)
    # Per-field sync switches: whether the sync publishes deployments into the
    # Application field / namespaces into the Environment field. When off, that
    # field is left for manual editing and never overwritten by the sync.
    sync_application = db.Column(db.Boolean, nullable=False, default=True)
    sync_environment = db.Column(db.Boolean, nullable=False, default=True)
    # Publishing env-var names is opt-in: it reads every published deployment's
    # full spec and can produce a large picklist, so the operator turns it on
    # deliberately (and must set variable_field_id).
    sync_variables = db.Column(db.Boolean, nullable=False, default=False)

    # --- Dropdown source: a live cluster + a chosen set of namespaces ---
    # The Environment picklist is fed by these selected namespaces; the Application
    # picklist is fed by the LIVE deployments running in those namespaces (read via
    # kubectl through k8s_provider). This replaces the older status-filtered
    # AppService source. ``selected_namespaces`` is a JSON-encoded list of names.
    source_cluster_id = db.Column(db.String(120), nullable=True)
    selected_namespaces = db.Column(db.Text, nullable=True)
    # Per-namespace deployment selection (JSON): {namespace: {"all": bool, "names": [...]}}.
    # A namespace absent from the map (or {"all": true}) publishes ALL its live
    # deployments (dynamic — future ones auto-included); {"all": false, "names": [...]}
    # publishes only that subset. Lets the operator curate exactly what Zoho shows.
    selected_deployments = db.Column(db.Text, nullable=True)
    # Custom (non-cluster) Environment entries (JSON-encoded list). Each entry:
    # {"name": "POS-UAT", "applications": ["pos"], "jenkinsJobPath": "pos-deploy",
    #  "jenkinsParams": {"msName": "{app}", "repotag": "{tag}", "envi": "uat", ...}}.
    # The name joins the Environment picklist, the applications join the
    # Application picklist (cascade-filtered), and an inbound ticket for one of
    # these routes straight to the configured Jenkins job — there is no live
    # cluster deployment behind it. Param values may reference {app}, {tag},
    # {environment} or any inbound-ticket field like {cf_country}.
    custom_environments = db.Column(db.Text, nullable=True)
    # Jenkins job overrides for CLUSTER targets (JSON-encoded list of rules):
    # [{"namespace": "common-dev", "deployments": ["persona-ms"],  # [] = whole namespace
    #   "jenkinsJobPath": "folder/persona-deploy", "jenkinsParams": {"msName": "{app}"}}].
    # When a run needs a BUILD (the ticket's image tag is not in the registry),
    # the matching rule's job + parameter map replace the global router call —
    # a rule naming the deployment beats a whole-namespace rule. Everything else
    # (registry gate, kubectl deploy, rollout watch) is unchanged. Blank
    # jenkinsJobPath = the router job; empty jenkinsParams = the standard
    # APP/TAG/NAMESPACE contract honoring the send_param_* toggles.
    job_overrides = db.Column(db.Text, nullable=True)

    # --- Application <- Environment cascade (Zoho field dependency mapping) ---
    # When on, after publishing both picklists the sync configures a Zoho Desk
    # dependency mapping so that picking an Environment (namespace) on a ticket
    # filters the Application options to that namespace's deployments.
    cascade_enabled = db.Column(db.Boolean, nullable=False, default=True)
    dependency_mapping_id = db.Column(db.String(64), nullable=True)
    # Second cascade: Application -> Variable (env-var names of the picked app).
    variable_mapping_id = db.Column(db.String(64), nullable=True)
    last_dependency_status = db.Column(db.String(16), nullable=True)  # ok | error | skipped
    last_dependency_message = db.Column(db.Text, nullable=True)

    # --- Ticket write-back (deploy automation → Zoho ticket) ---
    # When on, a finished automation run updates its originating Desk ticket:
    # sets the status, posts a comment + resolution describing the outcome, and
    # reassigns the ticket to the service account (zagent). Needs the token
    # minted with Desk.tickets.ALL. Status labels are operator-editable because
    # Zoho matches them exactly and "Failed"/"Canceled" are custom statuses.
    ticket_writeback_enabled = db.Column(db.Boolean, nullable=False, default=False)
    ticket_status_started = db.Column(db.String(120), nullable=False, default="Open")
    ticket_status_deployed = db.Column(db.String(120), nullable=False, default="Closed")
    ticket_status_failed = db.Column(db.String(120), nullable=False, default="Failed")
    ticket_status_cancelled = db.Column(db.String(120), nullable=False, default="Canceled")
    # Email of the agent tickets are reassigned to (resolved to an agent id at
    # call time and cached). Defaults to the zagent service account.
    ticket_owner_email = db.Column(db.String(255), nullable=False, default="zagent@areeba.com")

    # --- Last-run bookkeeping (mirrors RegistryConnection.last_test_*) ---
    last_sync_at = db.Column(db.DateTime(timezone=True), nullable=True)
    last_sync_status = db.Column(db.String(16), nullable=True)  # ok | error
    last_sync_message = db.Column(db.Text, nullable=True)
    last_synced_count = db.Column(db.Integer, nullable=True)
    last_test_at = db.Column(db.DateTime(timezone=True), nullable=True)
    last_test_status = db.Column(db.String(16), nullable=True)
    last_test_message = db.Column(db.Text, nullable=True)

    created_at = db.Column(
        db.DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )
    updated_at = db.Column(
        db.DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )


class ZohoInboundTicket(db.Model):
    """Audit log of DevOps Request tickets a ticketing provider pushed to the webhook.

    Each row records the raw picklist value, the parsed AppService id + tag, and
    whether it resolved to a live AppService. This is the intake precursor to the
    (later) deploy-automation state machine — for now it captures and resolves,
    nothing more. ``ticket_id`` is unique so duplicate webhook deliveries for the
    same ticket coalesce instead of stacking.

    Despite the name (kept so the live table is not renamed under a running
    deployment) the row is provider-neutral: ``provider`` says which integration
    delivered it, and deploy automation reads the same columns either way.
    """

    __tablename__ = "zoho_inbound_tickets"
    __table_args__ = (
        db.Index("ix_zoho_inbound_app_service", "app_service_id"),
    )

    id = db.Column(db.Integer, primary_key=True)
    # "zoho" | "jira" — which provider's webhook delivered this ticket. Existing
    # rows predate Jira and are all Zoho, hence the default.
    provider = db.Column(db.String(16), nullable=False, default="zoho", index=True)
    # Unique across providers: a Zoho ticket id and a Jira issue id never collide
    # (Jira's are project-prefixed keys / numeric ids from a different space), and
    # a single unique index keeps duplicate deliveries coalescing as before.
    ticket_id = db.Column(db.String(64), nullable=True, unique=True, index=True)
    ticket_number = db.Column(db.String(64), nullable=True)
    subject = db.Column(db.Text, nullable=True)
    # The raw picklist string as received (e.g. "Areeba / Payment Gateway / Prod · #42").
    raw_app_value = db.Column(db.Text, nullable=True)
    # Parsed out of the picklist value / payload.
    app_service_id = db.Column(db.Integer, nullable=True)
    app_service_name = db.Column(db.String(180), nullable=True)
    tag = db.Column(db.String(200), nullable=True)
    # A variable-change request: the env-var name picked on the ticket plus its
    # desired new value. A ticket carries EITHER a tag OR a variable+value.
    variable_name = db.Column(db.Text, nullable=True)
    variable_value = db.Column(db.Text, nullable=True)
    resolved = db.Column(db.Boolean, nullable=False, default=False)
    error = db.Column(db.Text, nullable=True)
    payload = db.Column(db.JSON, nullable=True)
    received_at = db.Column(
        db.DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
        index=True,
    )


class ZohoLayoutSnapshot(db.Model):
    """The raw Zoho layout as it looked immediately before a whole-layout write.

    ``PATCH /layouts/{id}`` replaces the entire layout, so a writer bug could
    strip fields or sections from the live ticket form with no undo. One snapshot
    is taken before every write (newest :data:`
    api.services.zoho_layout_service._SNAPSHOT_RETENTION` kept per layout), which
    turns "we destroyed the production layout" into "restore from the snapshot".
    """

    __tablename__ = "zoho_layout_snapshots"

    id = db.Column(db.Integer, primary_key=True)
    # "zoho" | "jira" — Jira takes the same before-write snapshot of a screen's
    # tabs, since reordering tabs there is also a whole-object rewrite.
    provider = db.Column(db.String(16), nullable=False, default="zoho", index=True)
    layout_id = db.Column(db.String(64), nullable=False, index=True)
    # Why the write happened: "add_section", "place_field", "field_conversion"…
    reason = db.Column(db.String(80), nullable=True)
    actor = db.Column(db.String(120), nullable=True)
    payload = db.Column(db.JSON, nullable=True)
    taken_at = db.Column(
        db.DateTime(timezone=True),
        nullable=False,
        index=True,
        default=lambda: datetime.now(timezone.utc),
    )


class ZohoFieldBinding(db.Model):
    """Binds one Zoho picklist to a live KubeSight option source.

    The Application / Environment / Variable fields have always been published
    from live cluster reads, but through three hardcoded blocks in the sync. A
    binding row generalizes that to any picklist on the layout: pick a source
    kind (see ``zoho_option_sources``), optionally name a parent field, and the
    sync publishes its values every run — exactly like the original three.

    The original three are deliberately NOT stored here: they are synthesized in
    memory from :class:`ZohoIntegration`'s own columns/toggles each sync, so the
    production integration keeps the identical code path and there is no data
    migration to get wrong. A row targeting one of those field ids is refused.
    """

    __tablename__ = "zoho_field_bindings"

    id = db.Column(db.Integer, primary_key=True)
    # "zoho" | "jira" — which provider's field this binds. The option SOURCES are
    # provider-neutral (they read Kubernetes); only the publish call differs.
    provider = db.Column(db.String(16), nullable=False, default="zoho", index=True)
    # One binding per field — two writers on the same picklist would race. The
    # index stays globally unique rather than (provider, field_id): Zoho field ids
    # are bare digits and Jira's are "customfield_NNNNN", so the two id spaces
    # cannot collide, and keeping the original index avoids rebuilding it under a
    # live SQLite deployment.
    field_id = db.Column(db.String(64), nullable=False, unique=True, index=True)
    # Cached from the layout for display only; the field id is the identity.
    api_name = db.Column(db.String(120), nullable=True)
    label = db.Column(db.String(255), nullable=True)
    # A key from zoho_option_sources.SOURCE_KINDS ("namespaces", "deployments"…).
    source_kind = db.Column(db.String(40), nullable=False)
    # JSON-encoded provider parameters ({} for every kind shipped so far).
    params = db.Column(db.Text, nullable=True)
    # Optional cascade parent: picking a value there filters this field's options.
    parent_field_id = db.Column(db.String(64), nullable=True)
    # Zoho's id for the parent->this dependency mapping, when one was created.
    mapping_id = db.Column(db.String(64), nullable=True)
    enabled = db.Column(db.Boolean, nullable=False, default=True)

    last_status = db.Column(db.String(16), nullable=True)  # ok | error | skipped
    last_message = db.Column(db.Text, nullable=True)
    last_count = db.Column(db.Integer, nullable=True)
    last_synced_at = db.Column(db.DateTime(timezone=True), nullable=True)

    created_at = db.Column(
        db.DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )
    updated_at = db.Column(
        db.DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )


class ZohoDeploymentSnapshot(db.Model):
    """Stable id for a live deployment the Zoho Application picklist publishes.

    The Application dropdown is driven by *live* cluster deployments (read via
    kubectl), which have no database primary key. To resolve an inbound ticket back
    to an exact deployment, each ``(cluster_id, namespace, deployment_name)`` tuple
    is assigned a stable synthetic id here (get-or-create on every sync/preview) and
    that id is embedded as the trailing ``- <id>`` of the picklist value — the same
    role ``AppServiceComponentMapping.id`` played for the old curated source. Rows
    persist so an old ticket referencing a since-deleted deployment still resolves
    to its name/namespace (flagged possibly-stale by ``last_seen_at``).
    """

    __tablename__ = "zoho_deployment_snapshots"
    __table_args__ = (
        db.UniqueConstraint(
            "cluster_id", "namespace", "deployment_name", name="uq_zoho_snapshot_identity"
        ),
    )

    id = db.Column(db.Integer, primary_key=True)
    cluster_id = db.Column(db.String(120), nullable=False)
    namespace = db.Column(db.String(253), nullable=False)
    deployment_name = db.Column(db.String(253), nullable=False)
    first_seen_at = db.Column(
        db.DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )
    last_seen_at = db.Column(
        db.DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )


class JenkinsConnection(db.Model):
    """Connection to the Jenkins ROUTER pipeline for ticket-driven deploy automation.

    Single-row config (id=1), same pattern as :class:`ZohoIntegration`. KubeSight
    only ever triggers ONE Jenkins job — a router pipeline (maintained outside
    KubeSight) that owns the app→job mapping, waits on the routed child job and
    propagates its result. Contract: ``buildWithParameters`` with APP_NAME /
    NAMESPACE / IMAGE_TAG / TICKET (see DEPLOY-AUTOMATION-PLAN.md §1). Auth is a
    Jenkins user + API token over HTTP Basic (no CSRF crumb needed with a token);
    the token is Fernet-encrypted at rest like every other integration secret.
    """

    __tablename__ = "jenkins_connection"

    id = db.Column(db.Integer, primary_key=True)
    enabled = db.Column(db.Boolean, nullable=False, default=False)

    base_url = db.Column(db.String(255), nullable=False, default="")
    username = db.Column(db.String(120), nullable=False, default="")
    api_token_encrypted = db.Column(db.Text, nullable=True)
    # Optional job-level remote-trigger token ("Trigger builds remotely") sent
    # as the `token` form field on buildWithParameters.
    build_token_encrypted = db.Column(db.Text, nullable=True)
    # Router job path, folder-style: "folder/router" -> /job/folder/job/router.
    router_job_path = db.Column(db.String(255), nullable=False, default="")
    verify_tls = db.Column(db.Boolean, nullable=False, default=True)

    # Which parameters buildWithParameters carries. Routers that aren't
    # parameterized with all of APP/NAMESPACE/TAG can have the extras turned
    # off (Jenkins drops-or-rejects undeclared parameters depending on setup).
    send_param_app = db.Column(db.Boolean, nullable=False, default=True)
    send_param_namespace = db.Column(db.Boolean, nullable=False, default=True)
    send_param_tag = db.Column(db.Boolean, nullable=False, default=True)

    # Automation behaviour. ``auto_run_tickets`` is the DEFAULT for clusters not
    # listed in ``auto_run_clusters`` — a per-cluster override map
    # ``{cluster_id: "auto" | "manual"}`` (public id strings, like the
    # per-cluster approval map on DeploymentRequestSetting).
    auto_run_tickets = db.Column(db.Boolean, nullable=False, default=False)
    auto_run_clusters = db.Column(db.JSON, nullable=True)
    # How a ticket's raw tag becomes the registry/deploy tag: "{tag}" is the
    # ticket value, e.g. "v{tag}-prod" turns "1.72.1" into "v1.72.1-prod". The
    # Jenkins router always receives the RAW ticket tag (it owns its own naming).
    image_tag_template = db.Column(db.String(120), nullable=False, default="{tag}")
    build_timeout_minutes = db.Column(db.Integer, nullable=False, default=45)
    # Jenkins queues a build behind whatever is already on the executor, so this
    # has to cover a full in-progress build, not just scheduler latency.
    queue_timeout_minutes = db.Column(db.Integer, nullable=False, default=30)
    # How long the auto-created Change Bundle's deployment window stays open.
    bundle_window_hours = db.Column(db.Integer, nullable=False, default=24)
    # How long to wait for the rolled-out pods to become ready before failing.
    rollout_timeout_minutes = db.Column(db.Integer, nullable=False, default=15)
    # On rollout failure: kubectl rollout undo back to the previous revision.
    rollback_on_failure = db.Column(db.Boolean, nullable=False, default=True)
    # Pin the automation's image checks to one RegistryConnection. Several linked
    # registries can claim the same image host (e.g. one DNS name fronting two
    # Nexus instances); when set and that connection matches the image's host, it
    # wins over the default first-created-wins scan. Null = auto-match by host.
    registry_connection_id = db.Column(db.Integer, nullable=True)

    last_test_at = db.Column(db.DateTime(timezone=True), nullable=True)
    last_test_status = db.Column(db.String(16), nullable=True)
    last_test_message = db.Column(db.Text, nullable=True)

    updated_at = db.Column(
        db.DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )


class DeployAutomationRun(db.Model):
    """One automation run for one inbound Zoho ticket: registry gate → optional
    Jenkins router build → registry verify → Change Bundle or direct image apply.

    The run is a DB-persisted state machine advanced by the scheduler tick, so it
    survives restarts mid-build. ``status`` values: active = queued /
    checking_image / building / verifying_image / awaiting_approval; terminal =
    deployed / failed / cancelled. ``steps`` is the display log the UI renders as
    pipeline chips: a JSON list of ``{key, status, detail, at}`` where key ∈
    image_check | build | verify | approval | deploy and status ∈ wait | run |
    done | fail | skip.
    """

    __tablename__ = "deploy_automation_runs"
    __table_args__ = (
        db.Index("ix_automation_run_status", "status"),
        db.Index("ix_automation_run_ticket", "ticket_record_id"),
    )

    id = db.Column(db.Integer, primary_key=True)
    ticket_record_id = db.Column(
        db.Integer, db.ForeignKey("zoho_inbound_tickets.id", ondelete="SET NULL"), nullable=True
    )
    ticket_number = db.Column(db.String(64), nullable=True)

    snapshot_id = db.Column(db.Integer, nullable=True)
    cluster_id = db.Column(db.String(120), nullable=False)
    namespace = db.Column(db.String(253), nullable=False)
    deployment_name = db.Column(db.String(253), nullable=False)
    container_name = db.Column(db.String(253), nullable=True)
    # Registry host + repository WITHOUT a tag (e.g. "nexus.areeba.com/areeba/aims-ui").
    image_repo = db.Column(db.Text, nullable=True)
    # The RESOLVED tag used for registry checks + deploy (tag template applied).
    image_tag = db.Column(db.String(200), nullable=False)
    # The raw tag as it arrived on the ticket — sent to the Jenkins router as-is.
    ticket_tag = db.Column(db.String(200), nullable=True)

    # What kind of change this run applies: "image" (tag deploy, the original
    # flow) or "env_var" (set one container env var to a new value — skips the
    # registry/Jenkins stages entirely). image_tag is "" for env_var runs.
    change_type = db.Column(db.String(16), nullable=False, default="image")
    variable_name = db.Column(db.Text, nullable=True)
    variable_value = db.Column(db.Text, nullable=True)

    status = db.Column(db.String(24), nullable=False, default="queued")
    error = db.Column(db.Text, nullable=True)
    steps = db.Column(db.JSON, nullable=True)
    # Transient retry counter for flaky registry reads (unreachable → retry a few ticks).
    retry_count = db.Column(db.Integer, nullable=False, default=0)

    jenkins_queue_url = db.Column(db.Text, nullable=True)
    jenkins_build_url = db.Column(db.Text, nullable=True)
    jenkins_build_number = db.Column(db.Integer, nullable=True)
    build_triggered_at = db.Column(db.DateTime(timezone=True), nullable=True)
    # When the post-deploy pod-health wait began (anchors the rollout timeout).
    rollout_started_at = db.Column(db.DateTime(timezone=True), nullable=True)

    # Change Bundle the approval path created (plain id, not a FK — bundles have
    # their own lifecycle and may be pruned independently).
    bundle_id = db.Column(db.Integer, nullable=True)

    auto = db.Column(db.Boolean, nullable=False, default=False)
    triggered_by = db.Column(db.String(120), nullable=True)

    created_at = db.Column(
        db.DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
        index=True,
    )
    updated_at = db.Column(
        db.DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )
    finished_at = db.Column(db.DateTime(timezone=True), nullable=True)


class MobileApplication(db.Model):
    """A registered mobile app whose binaries (APK/AAB/IPA) come out of Jenkins.

    The Zoho→Jenkins automation already handles the trigger: the operator adds a
    custom Environment (e.g. "POS Mobile") whose Jenkins job produces the app
    binary, and a ticket for that environment runs the job via the existing
    custom-environment flow. ``zoho_environment`` links this registration to that
    environment (matched casefolded, like all Zoho picklist values): when such a
    run's build succeeds, the artifact is pulled from Jenkins into KubeSight's
    binary store and listed under Mobile Applications.

    Store credentials are per-app and Fernet-encrypted at rest like every other
    integration secret. Google Play wants the service account's JSON key file;
    App Store Connect wants an API key (issuer id + key id + .p8 private key).
    """

    __tablename__ = "mobile_applications"

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(120), nullable=False, default="")
    description = db.Column(db.Text, nullable=True)
    enabled = db.Column(db.Boolean, nullable=False, default=True)

    # Custom Zoho Environment value whose successful Jenkins build feeds this app.
    zoho_environment = db.Column(db.String(180), nullable=True)

    # Jenkins job that builds the binaries, folder-style like router_job_path
    # ("folder/pos-apk" -> /job/folder/job/pos-apk). Used for manual fetches;
    # ticket-driven runs already carry their own build URL.
    jenkins_job_path = db.Column(db.String(255), nullable=False, default="")
    # Per-platform signing-job setup (JSON). Shielding strips the signature, so
    # the shielded binary must be signed again before any store will take it.
    # Signing runs on Jenkins, where the keys already are — the Android upload
    # keystore on a Linux agent, the macOS keychain on the Mac agent:
    #   {"android": {"executor": "jenkins", "jobPath": "mobile/android-resign",
    #                "resultPattern": "*.aab", "baseUrl": "https://kubesight.example.com"},
    #    "ios":     {"executor": "jenkins", "jobPath": "mobile/ios-resign",
    #                "resultPattern": "signed/*.ipa",
    #                "extraParams": {"PROFILE": "…_AppStore.mobileprovision"}}}
    # Only job names, globs, parameter names and URLs live here — never key
    # material, which stays on the agent that holds it.
    resign_config = db.Column(db.JSON, nullable=True)

    # Per-platform artifact resolution (JSON):
    #   {"android": {"source": "archive", "pattern": "*.apk"},
    #    "ios":     {"source": "workspace", "path": "execution/node/71/ws/app.ipa"}}
    # ``archive`` matches ``pattern`` against the build's archived artifacts
    # (recommended — survives the next build); ``workspace`` fetches ``path``
    # relative to the build URL (volatile: the next build overwrites it, which is
    # why KubeSight downloads the file into its own store immediately).
    artifact_config = db.Column(db.JSON, nullable=True)

    # --- Android / Google Play ---
    android_package_name = db.Column(db.String(255), nullable=False, default="")
    play_service_account_json_encrypted = db.Column(db.Text, nullable=True)

    # --- iOS / App Store Connect ---
    ios_bundle_id = db.Column(db.String(255), nullable=False, default="")
    asc_issuer_id = db.Column(db.String(64), nullable=False, default="")
    asc_key_id = db.Column(db.String(64), nullable=False, default="")
    asc_private_key_encrypted = db.Column(db.Text, nullable=True)
    # The numeric App Store Connect app id (resolved from the bundle id on the
    # first successful credential test; needed for TestFlight/review calls).
    asc_app_id = db.Column(db.String(64), nullable=False, default="")

    # Last connectivity/credential test outcome, per side (mirrors the
    # RegistryConnection.last_test_* convention).
    last_test_at = db.Column(db.DateTime(timezone=True), nullable=True)
    last_test_status = db.Column(db.String(16), nullable=True)
    last_test_message = db.Column(db.Text, nullable=True)

    created_at = db.Column(
        db.DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )
    updated_at = db.Column(
        db.DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )


class MobileAppBuild(db.Model):
    """One binary ingested from a Jenkins build for a registered mobile app.

    A DB-persisted download job advanced by the scheduler tick (same pattern as
    DeployAutomationRun): created ``pending`` when a matching Jenkins build
    succeeds (or a manual fetch is requested), the tick downloads the artifact
    into the binary store and flips it ``available``. ``storage_path`` is
    relative to the mobile artifact dir so the store can be relocated wholesale.
    """

    __tablename__ = "mobile_app_builds"
    __table_args__ = (
        db.Index("ix_mobile_build_app", "app_id"),
        db.Index("ix_mobile_build_status", "status"),
    )

    id = db.Column(db.Integer, primary_key=True)
    app_id = db.Column(
        db.Integer, db.ForeignKey("mobile_applications.id", ondelete="CASCADE"), nullable=False
    )
    platform = db.Column(db.String(16), nullable=False, default="android")  # android | ios
    artifact_type = db.Column(db.String(8), nullable=False, default="apk")  # apk | aab | ipa
    # Human version label — the Zoho ticket's tag for automation builds.
    version = db.Column(db.String(200), nullable=True)

    file_name = db.Column(db.String(255), nullable=False, default="")
    file_size = db.Column(db.BigInteger, nullable=True)
    sha256 = db.Column(db.String(64), nullable=True)
    storage_path = db.Column(db.Text, nullable=True)

    # Does the stored binary still carry a code signature? Shielding (SafeCore)
    # strips it, so a shielded upload arrives "unsigned" and must be re-signed
    # before any store will take it — publish refuses those outright. "unknown"
    # means the probe could not read the archive and never blocks a publish.
    # signed | unsigned | unknown  (see services/binary_signature.py)
    signature_state = db.Column(db.String(16), nullable=False, default="unknown")

    jenkins_build_number = db.Column(db.Integer, nullable=True)
    jenkins_build_url = db.Column(db.Text, nullable=True)

    ticket_record_id = db.Column(
        db.Integer, db.ForeignKey("zoho_inbound_tickets.id", ondelete="SET NULL"), nullable=True
    )
    ticket_number = db.Column(db.String(64), nullable=True)
    # Originating DeployAutomationRun (plain id, not a FK — runs may be pruned).
    run_id = db.Column(db.Integer, nullable=True)
    # ticket (Zoho automation) | manual ("Fetch latest build" button)
    # | upload (operator-supplied binary) | resign (output of a signing job)
    source = db.Column(db.String(16), nullable=False, default="ticket")

    # For source="resign": the build this one was produced from. Keeps the
    # shielded original and its signed output together in the releases timeline
    # instead of leaving the signed binary as an orphan.
    parent_build_id = db.Column(
        db.Integer, db.ForeignKey("mobile_app_builds.id", ondelete="SET NULL"), nullable=True
    )

    # pending | downloading | available | failed
    status = db.Column(db.String(16), nullable=False, default="pending")
    error = db.Column(db.Text, nullable=True)
    retry_count = db.Column(db.Integer, nullable=False, default=0)

    created_at = db.Column(
        db.DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
        index=True,
    )
    downloaded_at = db.Column(db.DateTime(timezone=True), nullable=True)
    updated_at = db.Column(
        db.DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )


class MobileAppResign(db.Model):
    """One re-signing run: take a signature-stripped build, hand it to a Jenkins
    job that holds the key, and register the signed result as a new build.

    Shielding (SafeCore) strips the code signature, so the shielded binary is
    unpublishable until it is signed again. The signing itself cannot run inside
    KubeSight — Android needs the upload keystore, iOS needs macOS and a
    keychain — so this is an orchestration record: it triggers the Jenkins build,
    follows it, then pulls the artifact it archived.

    Same tick-advanced state machine as MobileAppPublish, and ``steps`` is the
    same pipeline-chip JSON the drawer already renders.
    """

    __tablename__ = "mobile_app_resigns"
    __table_args__ = (
        db.Index("ix_mobile_resign_app", "app_id"),
        db.Index("ix_mobile_resign_build", "build_id"),
        db.Index("ix_mobile_resign_status", "status"),
    )

    id = db.Column(db.Integer, primary_key=True)
    app_id = db.Column(
        db.Integer, db.ForeignKey("mobile_applications.id", ondelete="CASCADE"), nullable=False
    )
    # The unsigned source build.
    build_id = db.Column(
        db.Integer, db.ForeignKey("mobile_app_builds.id", ondelete="CASCADE"), nullable=False
    )
    # The signed build this produced, once the result has been ingested.
    result_build_id = db.Column(
        db.Integer, db.ForeignKey("mobile_app_builds.id", ondelete="SET NULL"), nullable=True
    )

    platform = db.Column(db.String(16), nullable=False, default="android")
    # How the signing was driven. Only "jenkins" today.
    executor = db.Column(db.String(16), nullable=False, default="jenkins")

    # queued | running | collecting | completed | failed
    status = db.Column(db.String(16), nullable=False, default="queued")
    steps = db.Column(db.JSON, nullable=True)
    error = db.Column(db.Text, nullable=True)

    # Jenkins handle for diagnosis + the drawer's build link:
    # {"kind", "jobPath", "queueUrl", "buildUrl", "buildNumber"}
    job_ref = db.Column(db.JSON, nullable=True)
    triggered_by = db.Column(db.String(120), nullable=True)

    created_at = db.Column(
        db.DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
        index=True,
    )
    updated_at = db.Column(
        db.DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )
    finished_at = db.Column(db.DateTime(timezone=True), nullable=True)


class MobileAppPublish(db.Model):
    """One store-publish job for one ingested build: Google Play (track release)
    or App Store Connect (TestFlight upload, optionally submitted for review).

    Same tick-advanced state machine pattern as DeployAutomationRun. ``steps``
    is the pipeline-chip JSON the UI renders ({key, status, detail, at} with
    status ∈ wait | run | done | fail | skip). ``store_ref`` keeps the store's
    identifiers (Play edit id / ASC upload + build ids) so a restart mid-upload
    can resume or at least report precisely where it stopped.
    """

    __tablename__ = "mobile_app_publishes"
    __table_args__ = (
        db.Index("ix_mobile_publish_app", "app_id"),
        db.Index("ix_mobile_publish_build", "build_id"),
        db.Index("ix_mobile_publish_status", "status"),
    )

    id = db.Column(db.Integer, primary_key=True)
    app_id = db.Column(
        db.Integer, db.ForeignKey("mobile_applications.id", ondelete="CASCADE"), nullable=False
    )
    build_id = db.Column(
        db.Integer, db.ForeignKey("mobile_app_builds.id", ondelete="CASCADE"), nullable=False
    )

    store = db.Column(db.String(16), nullable=False)  # google_play | app_store
    # google_play: internal | alpha | beta | production
    # app_store:   testflight | review (TestFlight upload + submit for App Review)
    target = db.Column(db.String(32), nullable=False, default="internal")

    # queued | uploading | processing | published | failed
    status = db.Column(db.String(16), nullable=False, default="queued")
    steps = db.Column(db.JSON, nullable=True)
    store_ref = db.Column(db.JSON, nullable=True)
    error = db.Column(db.Text, nullable=True)
    retry_count = db.Column(db.Integer, nullable=False, default=0)

    triggered_by = db.Column(db.String(120), nullable=True)

    created_at = db.Column(
        db.DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
        index=True,
    )
    updated_at = db.Column(
        db.DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )
    finished_at = db.Column(db.DateTime(timezone=True), nullable=True)


class TicketingDeployConfig(db.Model):
    """What KubeSight can deploy — shared by every ticketing provider.

    The dropdown *source* (a cluster, its namespaces, which deployments to
    publish), the custom non-cluster environments and the Jenkins job overrides
    describe KubeSight's own deploy surface, not Zoho's or Jira's. They lived on
    :class:`ZohoIntegration` while Zoho was the only provider; a second provider
    would otherwise mean configuring the same clusters twice and two sources of
    truth for deploy routing.

    Single row (id is always 1), seeded once from the existing Zoho row by
    ``migrate_rbac._migrate_ticketing_tables``. The columns keep their original
    names and JSON encodings so the migration is a copy, not a transform.
    """

    __tablename__ = "ticketing_deploy_config"

    id = db.Column(db.Integer, primary_key=True)

    # The cluster whose live deployments feed the Application dropdown, and the
    # namespaces the operator picked out of it (JSON-encoded list of names).
    source_cluster_id = db.Column(db.String(120), nullable=True)
    selected_namespaces = db.Column(db.Text, nullable=True)
    # Per-namespace deployment selection (JSON): {namespace: {"all": bool, "names": [...]}}.
    # A namespace absent from the map (or {"all": true}) publishes ALL its live
    # deployments — future ones auto-included.
    selected_deployments = db.Column(db.Text, nullable=True)
    # Custom (non-cluster) Environment entries (JSON-encoded list). Each entry:
    # {"name": "POS-UAT", "applications": ["pos"], "jenkinsJobPath": "pos-deploy",
    #  "jenkinsParams": {"msName": "{app}", "repotag": "{tag}", ...}}. An inbound
    # ticket for one of these routes straight to Jenkins — there is no live
    # cluster deployment behind it.
    custom_environments = db.Column(db.Text, nullable=True)
    # Jenkins job overrides for CLUSTER targets (JSON-encoded list of rules):
    # [{"namespace": ..., "deployments": [...], "jenkinsJobPath": ..., "jenkinsParams": {...}}].
    # A rule naming the deployment beats a whole-namespace rule.
    job_overrides = db.Column(db.Text, nullable=True)

    created_at = db.Column(
        db.DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )
    updated_at = db.Column(
        db.DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )


class JiraIntegration(db.Model):
    """Configuration for the Jira ticketing provider — the Zoho row's counterpart.

    Same contract as :class:`ZohoIntegration` (publish live cluster options into
    the ticket form's dropdowns, take tickets back through a webhook, write the
    deploy outcome onto the issue), against Jira's very different primitives:

    ==================  ==========================  =============================
    Concept             Zoho Desk                   Jira
    ==================  ==========================  =============================
    Form structure      layout -> sections          screen -> tabs
    Dropdown values     field ``allowedValues``     custom-field *context* options
    Cascade             ``/dependencyMappings``     one cascading-select field
    Auth                OAuth self-client refresh   API token (Cloud) / PAT (DC)
    Outcome write-back  set ``status`` directly     execute a named *transition*
    ==================  ==========================  =============================

    The deploy surface itself (source cluster/namespaces/deployments, custom
    environments, Jenkins job overrides) is NOT here — it is shared, on
    :class:`TicketingDeployConfig`.
    """

    __tablename__ = "jira_integration"

    id = db.Column(db.Integer, primary_key=True)
    enabled = db.Column(db.Boolean, nullable=False, default=False)

    # --- Connection ---
    # Site root, e.g. "https://areeba.atlassian.net" (no /rest suffix).
    base_url = db.Column(db.String(255), nullable=False, default="")
    # "cloud" -> /rest/api/3 + Basic email:apiToken; "server" -> /rest/api/2 +
    # Bearer personal access token. Cloud is the common case.
    deployment_type = db.Column(db.String(16), nullable=False, default="cloud")
    # Account email the API token belongs to (Basic auth username). Unused for
    # Bearer/PAT auth on Server/DC.
    email = db.Column(db.String(255), nullable=False, default="")
    api_token_encrypted = db.Column(db.Text, nullable=True)

    # --- Scope: which project / issue type / screen this integration owns ---
    project_key = db.Column(db.String(64), nullable=False, default="")
    issue_type_id = db.Column(db.String(64), nullable=True)
    # Jira places fields on SCREENS (screen -> tabs -> fields); the screen is the
    # closest analogue of a Zoho layout and is the single object every structural
    # write is guarded to.
    screen_id = db.Column(db.String(64), nullable=False, default="")

    # --- Managed fields (Jira ids look like "customfield_10050") ---
    # In Jira a custom field's id IS its webhook key, so *_field_id and
    # *_field_api_name normally carry the same value; both are kept so the
    # provider-neutral inbound resolver reads one shape for Zoho and Jira.
    app_field_id = db.Column(db.String(64), nullable=False, default="")
    app_field_api_name = db.Column(db.String(120), nullable=False, default="")
    environment_field_id = db.Column(db.String(64), nullable=True)
    environment_field_api_name = db.Column(db.String(120), nullable=False, default="")
    # Free-text field carrying the version/tag on the issue.
    tag_field_api_name = db.Column(db.String(120), nullable=False, default="")
    variable_field_id = db.Column(db.String(64), nullable=True)
    variable_field_api_name = db.Column(db.String(120), nullable=False, default="")
    value_field_api_name = db.Column(db.String(120), nullable=False, default="")

    # --- Cascade ---
    # Jira has no cross-field dependency API: a dependent dropdown is ONE
    # cascading-select field whose parent options carry child options. When this
    # names such a field and cascade_enabled is on, the sync publishes the whole
    # environment -> application tree into it (and an inbound issue reads the
    # environment from `.value` and the application from `.child.value`). Blank =
    # the two flat single-select fields above are published independently.
    cascade_enabled = db.Column(db.Boolean, nullable=False, default=True)
    cascade_field_id = db.Column(db.String(64), nullable=True)
    cascade_field_api_name = db.Column(db.String(120), nullable=False, default="")
    last_dependency_status = db.Column(db.String(16), nullable=True)  # ok | error | skipped
    last_dependency_message = db.Column(db.Text, nullable=True)

    # --- Inbound webhook ---
    inbound_secret_encrypted = db.Column(db.Text, nullable=True)

    # --- Sync behaviour (mirrors the Zoho toggles) ---
    sync_interval_minutes = db.Column(db.Integer, nullable=False, default=30)
    sync_application = db.Column(db.Boolean, nullable=False, default=True)
    sync_environment = db.Column(db.Boolean, nullable=False, default=True)
    sync_variables = db.Column(db.Boolean, nullable=False, default=False)

    # --- Issue write-back (deploy automation -> Jira issue) ---
    # Jira cannot be told "set status = Deployed": a status change is a workflow
    # TRANSITION, looked up by name on the issue's own transition list. These are
    # transition names, matched case-insensitively.
    ticket_writeback_enabled = db.Column(db.Boolean, nullable=False, default=False)
    transition_started = db.Column(db.String(120), nullable=False, default="In Progress")
    transition_deployed = db.Column(db.String(120), nullable=False, default="Done")
    transition_failed = db.Column(db.String(120), nullable=False, default="Done")
    transition_cancelled = db.Column(db.String(120), nullable=False, default="Done")
    # Assignee for automated issues; resolved to an accountId at call time.
    ticket_owner_email = db.Column(db.String(255), nullable=False, default="")

    # --- Last-run bookkeeping (identical shape to ZohoIntegration) ---
    last_sync_at = db.Column(db.DateTime(timezone=True), nullable=True)
    last_sync_status = db.Column(db.String(16), nullable=True)  # ok | error
    last_sync_message = db.Column(db.Text, nullable=True)
    last_synced_count = db.Column(db.Integer, nullable=True)
    last_test_at = db.Column(db.DateTime(timezone=True), nullable=True)
    last_test_status = db.Column(db.String(16), nullable=True)
    last_test_message = db.Column(db.Text, nullable=True)

    created_at = db.Column(
        db.DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )
    updated_at = db.Column(
        db.DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )


# Cluster Builder tables live in their own module; re-exported here so the
# canonical import surface stays "from .models import X".
from .models_cluster_build import (  # noqa: E402,F401
    BuildProfile,
    ClusterBuild,
    ClusterBuildNode,
    ClusterBuildStep,
    SshConnectionProfile,
    SshCredential,
    SshHostKey,
    VSphereConnection,
)
