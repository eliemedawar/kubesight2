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
