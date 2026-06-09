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


class AlertRoutingReceiver(db.Model):
    __tablename__ = "alert_routing_receivers"

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(120), nullable=False)
    receiver_type = db.Column(db.String(32), nullable=False, index=True)
    email_address = db.Column(db.String(255), nullable=True)
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

    policies = db.relationship(
        "AlertPolicy",
        secondary=alert_policy_receivers,
        lazy="dynamic",
        back_populates="notification_receivers",
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
    receiver_id = db.Column(db.Integer, nullable=True, index=True)
    receiver_name = db.Column(db.String(120), nullable=False, default="")
    receiver_type = db.Column(db.String(32), nullable=False, default="")
    status = db.Column(db.String(16), nullable=False, index=True)
    error_message = db.Column(db.Text, nullable=True)
    delivered_at = db.Column(
        db.DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
        index=True,
    )


class AlertPolicy(db.Model):
    __tablename__ = "alert_policies"

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(120), nullable=False)
    cluster_id = db.Column(db.String(120), nullable=False, index=True)
    description = db.Column(db.Text, nullable=True)
    enabled = db.Column(db.Boolean, nullable=False, default=True, index=True)
    severity = db.Column(db.String(16), nullable=False, default="warning")
    condition_logic = db.Column(db.String(8), nullable=False, default="any")
    conditions = db.Column(db.JSON, nullable=False, default=list)
    scope = db.Column(db.JSON, nullable=False, default=dict)
    notification_channels = db.Column(db.JSON, nullable=False, default=list)
    evaluation_interval_seconds = db.Column(db.Integer, nullable=False, default=300)
    last_evaluated_at = db.Column(db.DateTime(timezone=True), nullable=True)
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
    severity = db.Column(db.String(16), nullable=False, default="warning")
    status = db.Column(db.String(16), nullable=False, default="active", index=True)
    title = db.Column(db.String(255), nullable=False, default="")
    description = db.Column(db.Text, nullable=True)
    triggered_conditions = db.Column(db.JSON, nullable=False, default=list)
    metric_snapshot = db.Column(db.JSON, nullable=True)
    fired_at = db.Column(
        db.DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
        index=True,
    )
    resolved_at = db.Column(db.DateTime(timezone=True), nullable=True)
    last_notified_at = db.Column(db.DateTime(timezone=True), nullable=True)

    policy = db.relationship("AlertPolicy", back_populates="history_entries")
