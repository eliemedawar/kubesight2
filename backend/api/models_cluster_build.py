"""Cluster Builder data model.

Tables backing the "build a cluster from VMs" feature: vCenter connections,
SSH credentials/routing, repository build profiles, and the build itself
(build -> nodes -> steps). Everything secret is Fernet-encrypted via
``secret_encryption`` — columns hold ciphertext, never plaintext.

Re-exported from ``models.py`` so callers keep the single canonical import
surface (``from .models import ClusterBuild``).
"""

from __future__ import annotations

from datetime import datetime, timezone

from .db import db


def _utcnow():
    return datetime.now(timezone.utc)


class VSphereConnection(db.Model):
    """A read-only vCenter link used to browse VM inventory in the builder.

    v1 never mutates vSphere — the account only needs the Read-Only role.
    Inventory (name, power, IP, CPU/mem, guest OS, Tools state, ESXi host,
    datastore) feeds the VM picker and the placement/anti-affinity preflight.
    """

    __tablename__ = "vsphere_connections"

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(120), nullable=False, default="")
    base_url = db.Column(db.String(255), nullable=False, default="")
    username = db.Column(db.String(255), nullable=False, default="")
    password_cipher = db.Column(db.Text, nullable=True)
    skip_tls_verify = db.Column(db.Boolean, nullable=False, default=False)
    ca_pem = db.Column(db.Text, nullable=True)
    # Optional inventory scoping (vCenter folder / datacenter names).
    datacenter_filter = db.Column(db.String(255), nullable=True)
    folder_filter = db.Column(db.String(255), nullable=True)
    is_active = db.Column(db.Boolean, nullable=False, default=True)
    last_connection_status = db.Column(db.String(32), nullable=True)
    last_connection_error = db.Column(db.Text, nullable=True)
    last_tested_at = db.Column(db.DateTime(timezone=True), nullable=True)
    created_at = db.Column(db.DateTime(timezone=True), nullable=False, default=_utcnow)
    updated_at = db.Column(
        db.DateTime(timezone=True), nullable=False, default=_utcnow, onupdate=_utcnow
    )


class SshCredential(db.Model):
    """A reusable SSH identity (who we log in as and how we escalate)."""

    __tablename__ = "ssh_credentials"

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(120), nullable=False, default="")
    username = db.Column(db.String(120), nullable=False, default="")
    # key | password
    auth_method = db.Column(db.String(16), nullable=False, default="key")
    # Private key PEM or password, Fernet-encrypted.
    secret_cipher = db.Column(db.Text, nullable=True)
    key_passphrase_cipher = db.Column(db.Text, nullable=True)
    port = db.Column(db.Integer, nullable=False, default=22)
    # root | nopasswd | password
    sudo_mode = db.Column(db.String(16), nullable=False, default="nopasswd")
    sudo_password_cipher = db.Column(db.Text, nullable=True)
    created_by = db.Column(db.String(120), nullable=True)
    created_at = db.Column(db.DateTime(timezone=True), nullable=False, default=_utcnow)
    updated_at = db.Column(
        db.DateTime(timezone=True), nullable=False, default=_utcnow, onupdate=_utcnow
    )


class SshConnectionProfile(db.Model):
    """How to *reach* hosts: credential + direct-or-bastion route + host-key policy."""

    __tablename__ = "ssh_connection_profiles"

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(120), nullable=False, default="")
    credential_id = db.Column(
        db.Integer, db.ForeignKey("ssh_credentials.id"), nullable=False
    )
    # direct | bastion
    route_mode = db.Column(db.String(16), nullable=False, default="direct")
    bastion_host = db.Column(db.String(253), nullable=True)
    bastion_port = db.Column(db.Integer, nullable=True)
    bastion_credential_id = db.Column(
        db.Integer, db.ForeignKey("ssh_credentials.id"), nullable=True
    )
    # strict | tofu | pinned
    host_key_policy = db.Column(db.String(16), nullable=False, default="tofu")
    connect_timeout_s = db.Column(db.Integer, nullable=False, default=15)
    command_timeout_s = db.Column(db.Integer, nullable=False, default=600)
    retry_count = db.Column(db.Integer, nullable=False, default=2)
    retry_backoff_s = db.Column(db.Integer, nullable=False, default=5)
    created_at = db.Column(db.DateTime(timezone=True), nullable=False, default=_utcnow)
    updated_at = db.Column(
        db.DateTime(timezone=True), nullable=False, default=_utcnow, onupdate=_utcnow
    )

    credential = db.relationship("SshCredential", foreign_keys=[credential_id])
    bastion_credential = db.relationship(
        "SshCredential", foreign_keys=[bastion_credential_id]
    )


class SshHostKey(db.Model):
    """Recorded/approved SSH host key fingerprints (TOFU or pre-approved)."""

    __tablename__ = "ssh_host_keys"
    __table_args__ = (
        db.UniqueConstraint("host", "port", "key_type", name="uq_ssh_host_key"),
    )

    id = db.Column(db.Integer, primary_key=True)
    host = db.Column(db.String(253), nullable=False)
    port = db.Column(db.Integer, nullable=False, default=22)
    key_type = db.Column(db.String(32), nullable=False, default="")
    fingerprint_sha256 = db.Column(db.String(64), nullable=False, default="")
    # preapproved | tofu
    source = db.Column(db.String(16), nullable=False, default="tofu")
    approved_by_user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=True)
    approved_at = db.Column(db.DateTime(timezone=True), nullable=True)
    created_at = db.Column(db.DateTime(timezone=True), nullable=False, default=_utcnow)


class BuildProfile(db.Model):
    """Where packages and images come from: internet | mirror | offline."""

    __tablename__ = "build_profiles"

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(120), nullable=False, default="")
    # internet | mirror | offline
    repo_mode = db.Column(db.String(16), nullable=False, default="internet")
    k8s_pkg_repo_url = db.Column(db.String(512), nullable=True)
    k8s_pkg_gpg_key_url = db.Column(db.String(512), nullable=True)
    cri_pkg_repo_url = db.Column(db.String(512), nullable=True)
    k8s_image_registry = db.Column(db.String(255), nullable=True)
    cni_image_registry = db.Column(db.String(255), nullable=True)
    addon_image_registry = db.Column(db.String(255), nullable=True)
    registry_username = db.Column(db.String(255), nullable=True)
    registry_password_cipher = db.Column(db.Text, nullable=True)
    http_proxy = db.Column(db.String(512), nullable=True)
    https_proxy = db.Column(db.String(512), nullable=True)
    no_proxy = db.Column(db.String(1024), nullable=True)
    extra_ca_certs_pem = db.Column(db.Text, nullable=True)
    offline_bundle_path = db.Column(db.String(512), nullable=True)
    offline_bundle_checksum = db.Column(db.String(128), nullable=True)
    created_at = db.Column(db.DateTime(timezone=True), nullable=False, default=_utcnow)
    updated_at = db.Column(
        db.DateTime(timezone=True), nullable=False, default=_utcnow, onupdate=_utcnow
    )


class ClusterBuild(db.Model):
    """One cluster-provisioning run: settings, endpoint/HA config, and outcome.

    Status lifecycle:
        draft -> preflighting -> preflight_passed | preflight_failed
        preflight_passed -> building -> completed | failed
        any non-terminal -> cancelled
    """

    __tablename__ = "cluster_builds"
    __table_args__ = (
        db.Index("ix_cluster_build_status_created", "status", "created_at"),
    )

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(120), nullable=False, default="")
    status = db.Column(db.String(24), nullable=False, default="draft", index=True)
    k8s_version = db.Column(db.String(32), nullable=False, default="")
    cri = db.Column(db.String(24), nullable=False, default="containerd")

    # single_cp | stacked_ha
    topology_type = db.Column(db.String(16), nullable=False, default="single_cp")
    # The stable kubeadm controlPlaneEndpoint (host:port or ip:port). Mandatory
    # even for single-CP builds so the HA migration path stays open.
    control_plane_endpoint = db.Column(db.String(255), nullable=False, default="")
    # managed_haproxy | external_lb | manual_endpoint
    endpoint_mode = db.Column(db.String(24), nullable=False, default="managed_haproxy")
    vip_address = db.Column(db.String(64), nullable=True)
    vip_interface = db.Column(db.String(64), nullable=True)
    vrrp_router_id = db.Column(db.Integer, nullable=True)
    vrrp_auth_pass_cipher = db.Column(db.Text, nullable=True)
    lb_config_json = db.Column(db.JSON, nullable=True)
    # manual | ipam — ipam fields reserved for v2 so it lands without migration.
    vip_source = db.Column(db.String(16), nullable=False, default="manual")
    ipam_reservation_id = db.Column(db.String(120), nullable=True)

    # kubeadm --upload-certs certificate key: short-lived; nulled after joins.
    cert_key_cipher = db.Column(db.Text, nullable=True)
    cert_key_expires_at = db.Column(db.DateTime(timezone=True), nullable=True)
    # Worker join command (token + CA hash), encrypted; nulled after completion.
    join_command_cipher = db.Column(db.Text, nullable=True)

    cni_plugin = db.Column(db.String(24), nullable=False, default="calico")
    cni_version = db.Column(db.String(32), nullable=False, default="")
    pod_cidr = db.Column(db.String(64), nullable=False, default="")
    service_cidr = db.Column(db.String(64), nullable=False, default="")
    cni_params_json = db.Column(db.JSON, nullable=True)
    # Optional, version-pinned cluster add-ons selected in the builder:
    # [{"id": "metrics-server", "version": "0.7.2"}, ...].
    addons_json = db.Column(db.JSON, nullable=True)

    vsphere_connection_id = db.Column(
        db.Integer, db.ForeignKey("vsphere_connections.id"), nullable=True
    )
    build_profile_id = db.Column(
        db.Integer, db.ForeignKey("build_profiles.id"), nullable=True
    )
    connection_profile_id = db.Column(
        db.Integer, db.ForeignKey("ssh_connection_profiles.id"), nullable=True
    )

    # Public cluster id ("custom-<n>") once onboarding registers the cluster.
    result_cluster_id = db.Column(db.String(120), nullable=True)
    error = db.Column(db.Text, nullable=True)
    # Recorded acknowledgement when a user overrides preflight warnings.
    warnings_ack_json = db.Column(db.JSON, nullable=True)
    created_by = db.Column(db.String(120), nullable=True)
    created_at = db.Column(
        db.DateTime(timezone=True), nullable=False, default=_utcnow, index=True
    )
    updated_at = db.Column(
        db.DateTime(timezone=True), nullable=False, default=_utcnow, onupdate=_utcnow
    )
    started_at = db.Column(db.DateTime(timezone=True), nullable=True)
    finished_at = db.Column(db.DateTime(timezone=True), nullable=True)

    nodes = db.relationship(
        "ClusterBuildNode",
        backref="build",
        cascade="all, delete-orphan",
        order_by="ClusterBuildNode.position",
    )
    steps = db.relationship(
        "ClusterBuildStep",
        backref="build",
        cascade="all, delete-orphan",
        order_by="ClusterBuildStep.id",
    )


class ClusterBuildNode(db.Model):
    """One machine in a build: role, address, vSphere placement, node status."""

    __tablename__ = "cluster_build_nodes"
    __table_args__ = (
        db.Index("ix_cluster_build_node_build", "build_id"),
    )

    id = db.Column(db.Integer, primary_key=True)
    build_id = db.Column(
        db.Integer,
        db.ForeignKey("cluster_builds.id", ondelete="CASCADE"),
        nullable=False,
    )
    hostname = db.Column(db.String(253), nullable=False, default="")
    address = db.Column(db.String(64), nullable=False, default="")
    # vmware_tools | manual — Tools is recommended, not required.
    address_source = db.Column(db.String(16), nullable=False, default="manual")
    # control_plane | worker | loadbalancer
    role = db.Column(db.String(16), nullable=False, default="worker")
    is_primary_cp = db.Column(db.Boolean, nullable=False, default=False)
    is_lb_master = db.Column(db.Boolean, nullable=False, default=False)

    vsphere_vm_moid = db.Column(db.String(64), nullable=True)
    vsphere_vm_name = db.Column(db.String(255), nullable=True)
    vsphere_host = db.Column(db.String(255), nullable=True)
    vsphere_datastore = db.Column(db.String(255), nullable=True)
    vsphere_tools_status = db.Column(db.String(32), nullable=True)
    vsphere_power_state = db.Column(db.String(32), nullable=True)
    vsphere_cpu = db.Column(db.Integer, nullable=True)
    vsphere_memory_mb = db.Column(db.Integer, nullable=True)

    # Per-node route override (mixed direct/bastion environments).
    connection_profile_id = db.Column(
        db.Integer, db.ForeignKey("ssh_connection_profiles.id"), nullable=True
    )

    os_family = db.Column(db.String(24), nullable=True)   # debian | rhel
    os_version = db.Column(db.String(32), nullable=True)
    arch = db.Column(db.String(16), nullable=True)
    # pending | preflight_passed | preflight_failed | preparing | ready | joined |
    # failed | removed
    status = db.Column(db.String(24), nullable=False, default="pending")
    preflight_json = db.Column(db.JSON, nullable=True)
    error = db.Column(db.Text, nullable=True)
    position = db.Column(db.Integer, nullable=False, default=0)


class ClusterBuildStep(db.Model):
    """One phase execution record (optionally scoped to a node). Restart-safe:
    completed steps are skipped on resume, so a backend restart resumes the
    build rather than restarting it."""

    __tablename__ = "cluster_build_steps"
    __table_args__ = (
        db.Index("ix_cluster_build_step_build", "build_id", "phase"),
    )

    id = db.Column(db.Integer, primary_key=True)
    build_id = db.Column(
        db.Integer,
        db.ForeignKey("cluster_builds.id", ondelete="CASCADE"),
        nullable=False,
    )
    node_id = db.Column(
        db.Integer,
        db.ForeignKey("cluster_build_nodes.id", ondelete="CASCADE"),
        nullable=True,
    )
    # vsphere_preflight | node_preflight | base_prep | loadbalancer | pull_images |
    # init | cni | join_cp | join_workers | verify | onboard | addons
    phase = db.Column(db.String(24), nullable=False, default="")
    # pending | running | completed | failed | skipped
    status = db.Column(db.String(16), nullable=False, default="pending")
    attempt = db.Column(db.Integer, nullable=False, default=1)
    started_at = db.Column(db.DateTime(timezone=True), nullable=True)
    finished_at = db.Column(db.DateTime(timezone=True), nullable=True)
    # Scrubbed before persisting — never raw kubeadm output (join tokens /
    # certificate keys appear in init output).
    log_tail = db.Column(db.Text, nullable=True)
    error = db.Column(db.Text, nullable=True)
