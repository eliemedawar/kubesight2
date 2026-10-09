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
    """A vCenter link: browsed read-only, and optionally provisioned into.

    Browsing uses the Read-Only account (``username``/``password_cipher``).
    Inventory (name, power, IP, CPU/mem, guest OS, Tools state, ESXi host,
    datastore) feeds the VM picker and the placement/anti-affinity preflight.
    Creating and deleting VMs — OpenTofu provisioning — uses the separate
    ``provisioning_*`` account, so the browsing account never needs more than
    the Read-Only role.
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
    # A second, separate account that may create and delete VMs. Browsing keeps
    # using the read-only one above; only OpenTofu provisioning jobs use this.
    provisioning_username = db.Column(db.String(255), nullable=True)
    provisioning_password_cipher = db.Column(db.Text, nullable=True)
    provisioning_last_test_at = db.Column(db.DateTime(timezone=True), nullable=True)
    provisioning_last_test_status = db.Column(db.String(16), nullable=True)
    provisioning_last_test_message = db.Column(db.Text, nullable=True)
    # [{"privilege": "Folder.Create", "entity": "DC-Beirut", "granted": true}]
    provisioning_privileges_json = db.Column(db.JSON, nullable=True)
    created_at = db.Column(db.DateTime(timezone=True), nullable=False, default=_utcnow)
    updated_at = db.Column(
        db.DateTime(timezone=True), nullable=False, default=_utcnow, onupdate=_utcnow
    )


class VSphereNetworkRange(db.Model):
    """The addresses KubeSight may hand to VMs it creates on one vCenter network.

    New VMs only ever get a static address from a range set here; nothing is
    taken from DHCP. Reservations against a range are rows in
    ``vsphere_ip_reservations``, so two builds can never be handed one address.
    """

    __tablename__ = "vsphere_network_ranges"
    __table_args__ = (
        db.UniqueConstraint(
            "connection_id", "network_name", name="uq_vsphere_network_range"
        ),
    )

    id = db.Column(db.Integer, primary_key=True)
    connection_id = db.Column(
        db.Integer, db.ForeignKey("vsphere_connections.id"), nullable=False
    )
    # The port group / network name exactly as vCenter shows it.
    network_name = db.Column(db.String(255), nullable=False, default="")
    cidr = db.Column(db.String(64), nullable=False, default="")
    range_start = db.Column(db.String(64), nullable=False, default="")
    range_end = db.Column(db.String(64), nullable=False, default="")
    gateway = db.Column(db.String(64), nullable=False, default="")
    # Comma-separated resolver addresses.
    dns_servers = db.Column(db.String(512), nullable=False, default="")
    # The DNS domain written into each VM's guest customization.
    dns_domain = db.Column(db.String(255), nullable=True)
    created_at = db.Column(db.DateTime(timezone=True), nullable=False, default=_utcnow)
    updated_at = db.Column(
        db.DateTime(timezone=True), nullable=False, default=_utcnow, onupdate=_utcnow
    )


class VSphereIpReservation(db.Model):
    """One address taken out of a range for one build.

    ``reserved`` from the moment a plan names it until the VM exists, then
    ``in_use`` until the VM is destroyed. Discarding a plan or destroying the
    cluster deletes the row, which is what returns the address to the range.
    """

    __tablename__ = "vsphere_ip_reservations"
    __table_args__ = (
        db.UniqueConstraint("range_id", "address", name="uq_vsphere_ip_reservation"),
    )

    id = db.Column(db.Integer, primary_key=True)
    range_id = db.Column(
        db.Integer, db.ForeignKey("vsphere_network_ranges.id"), nullable=False
    )
    address = db.Column(db.String(64), nullable=False)
    build_id = db.Column(
        db.Integer,
        db.ForeignKey("cluster_builds.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    # vip | node
    purpose = db.Column(db.String(16), nullable=False, default="node")
    node_name = db.Column(db.String(253), nullable=True)
    # reserved | in_use
    status = db.Column(db.String(16), nullable=False, default="reserved")
    created_at = db.Column(db.DateTime(timezone=True), nullable=False, default=_utcnow)


class ClusterTemplate(db.Model):
    """A cluster shape an admin saved. The built-in templates live in code.

    ``spec_json`` holds what a template decides — role counts, per-role VM
    sizes, networking and add-ons — and never where in vCenter the machines go
    or which addresses they get, so one template works on any vCenter.
    """

    __tablename__ = "cluster_templates"

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(120), nullable=False, unique=True)
    description = db.Column(db.Text, nullable=True)
    spec_json = db.Column(db.JSON, nullable=False, default=dict)
    created_by = db.Column(db.String(120), nullable=True)
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
    # Last-test bookkeeping (RegistryConnection.last_test_* convention). A route
    # that passed once and has not been exercised since is the usual cause of a
    # build dying in node preparation, so the age is worth surfacing.
    last_test_at = db.Column(db.DateTime(timezone=True), nullable=True)
    last_test_status = db.Column(db.String(16), nullable=True)
    last_test_message = db.Column(db.Text, nullable=True)
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

    When KubeSight creates the VMs (``machine_source == "vmware"``), OpenTofu
    runs first:
        draft -> provisioning -> preflighting -> ...   (as above)
        provisioning -> provision_failed
    A VMs-only build (``vms_only``) stops once every VM answers SSH:
        draft -> provisioning -> vms_ready
        vms_ready -> draft -> preflighting -> ...   ("Install Kubernetes")
    and a cluster whose VMs KubeSight created can be taken down again:
        completed | failed | provision_failed | vms_ready -> destroying -> destroyed
    ``provision_status`` says what OpenTofu is doing inside those states.
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
    # Filesystem path preflight measures free space on. Null means /var, where
    # kubeadm, containerd and pulled images land by default; a node that backs
    # those directories with another mount must be measured there instead.
    disk_check_path = db.Column(db.String(255), nullable=True)

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
    # Optional workloads copied out of an existing cluster once this one is up:
    # {"sourceClusterId": "custom-3", "registryConnectionId": 2,
    #  "items": [{"namespace": "core", "kind": "Deployment", "name": "api"},
    #            {"namespace": "payments", "kind": "Namespace", "name": ""}],
    #  "imageAck": {...}, "result": {...}}
    # Only the *selection* is stored — manifests are re-read from the source at
    # apply time, so a build never applies a stale copy of a live object.
    workloads_json = db.Column(db.JSON, nullable=True)

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
    # The user whose current cluster/namespace access authorizes background
    # workload reads. Persisted because the phase may run after a process
    # restart, when the original HTTP request context no longer exists.
    execution_user_id = db.Column(
        db.Integer, db.ForeignKey("users.id"), nullable=True
    )
    created_by = db.Column(db.String(120), nullable=True)
    created_at = db.Column(
        db.DateTime(timezone=True), nullable=False, default=_utcnow, index=True
    )
    updated_at = db.Column(
        db.DateTime(timezone=True), nullable=False, default=_utcnow, onupdate=_utcnow
    )
    started_at = db.Column(db.DateTime(timezone=True), nullable=True)
    finished_at = db.Column(db.DateTime(timezone=True), nullable=True)
    # A completed build can be grown by adding workers. That run reuses the same
    # phase machine, so it needs its own clock — and the original build's
    # duration is banked at first completion, because a growth run rewrites
    # finished_at and would otherwise turn "built in 18 min" into "built in 5 d".
    growth_started_at = db.Column(db.DateTime(timezone=True), nullable=True)
    build_seconds = db.Column(db.Integer, nullable=True)
    # etcd snapshots taken before control planes joined a running cluster:
    # [{"path", "node", "address", "bytes", "sha256", "takenAt", "reason"}].
    # The files live on that control plane; only their record is kept here.
    etcd_backups_json = db.Column(db.JSON, nullable=True)

    # existing | vmware. "vmware" means KubeSight creates the machines itself
    # with OpenTofu before the phase machine below ever runs.
    machine_source = db.Column(db.String(16), nullable=False, default="existing")
    # VMware only: create the VMs and stop once they answer SSH, without
    # installing Kubernetes — for testing the VM side on its own. Cleared when
    # someone asks for Kubernetes on those VMs later.
    vms_only = db.Column(db.Boolean, nullable=False, default=False)
    # The template the wizard started from: a built-in id ("lab", "small",
    # "standard-ha"), "custom:<id>" for a saved one, or "custom".
    template_id = db.Column(db.String(64), nullable=True)
    # Where and how the VMs are created (vCenter placement, VM template, role
    # counts and sizes, network range). Never secrets.
    provisioning_json = db.Column(db.JSON, nullable=True)
    # What OpenTofu is doing for this build, alongside ``status``:
    # planning | planned | plan_failed | applying | connecting | apply_failed |
    # connect_failed | ready | grow_* | destroy_planning | destroy_pending |
    # destroy_plan_failed | destroying | destroy_failed | destroyed
    provision_status = db.Column(db.String(24), nullable=True)

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


class ClusterInfraState(db.Model):
    """OpenTofu's state for one build's VMs, and the lock that guards it.

    Served to the ``tofu`` process through KubeSight's own HTTP state backend,
    so every write OpenTofu makes during an apply lands here as it happens —
    a KubeSight restart mid-apply loses nothing that OpenTofu had recorded.
    The state is Fernet-encrypted at rest like every other secret-bearing
    column: it names VMs, addresses and vCenter object ids.
    """

    __tablename__ = "cluster_infra_states"

    id = db.Column(db.Integer, primary_key=True)
    build_id = db.Column(
        db.Integer,
        db.ForeignKey("cluster_builds.id", ondelete="CASCADE"),
        nullable=False,
        unique=True,
    )
    state_cipher = db.Column(db.Text, nullable=True)
    # From the state document itself; serial moves on every OpenTofu write.
    serial = db.Column(db.Integer, nullable=True)
    lineage = db.Column(db.String(64), nullable=True)
    resource_count = db.Column(db.Integer, nullable=False, default=0)
    # KubeSight's own write counter, shown as "state version N".
    version = db.Column(db.Integer, nullable=False, default=0)
    # The OpenTofu lock: its id, the lock info OpenTofu sent, and the job that
    # took it — recovery releases a lock only on behalf of the job that held it.
    lock_id = db.Column(db.String(64), nullable=True)
    lock_info_json = db.Column(db.JSON, nullable=True)
    lock_job_id = db.Column(db.Integer, nullable=True)
    locked_at = db.Column(db.DateTime(timezone=True), nullable=True)
    created_at = db.Column(db.DateTime(timezone=True), nullable=False, default=_utcnow)
    updated_at = db.Column(
        db.DateTime(timezone=True), nullable=False, default=_utcnow, onupdate=_utcnow
    )


class ClusterProvisionJob(db.Model):
    """One OpenTofu operation on a build's VMs: plan, then (maybe) apply.

    Status lifecycle:
        planning -> planned | plan_failed
        planned -> applying (create/grow, by anyone allowed to execute)
        planned -> awaiting_approval -> applying (destroy, by a second person)
        applying -> connecting -> succeeded           (create/grow)
        applying -> succeeded                         (destroy)
        applying | connecting -> apply_failed | connect_failed
        planned | awaiting_approval -> discarded | rejected
        planning | applying | connecting -> interrupted (backend restart;
            a recovery job takes over, see ``provisioning.jobs``)
    """

    __tablename__ = "cluster_provision_jobs"
    __table_args__ = (
        db.Index("ix_cluster_provision_job_build", "build_id", "created_at"),
    )

    id = db.Column(db.Integer, primary_key=True)
    build_id = db.Column(
        db.Integer,
        db.ForeignKey("cluster_builds.id", ondelete="CASCADE"),
        nullable=False,
    )
    # create | grow | destroy
    operation = db.Column(db.String(16), nullable=False, default="create")
    status = db.Column(db.String(24), nullable=False, default="planning", index=True)
    # The rendered OpenTofu configuration (main.tf.json) this job planned with,
    # plus what the job needs to finish (new node specs for growth). No secrets:
    # vCenter credentials reach OpenTofu through its environment only.
    config_json = db.Column(db.JSON, nullable=True)
    # The saved plan file (binary), base64 then Fernet-encrypted.
    plan_cipher = db.Column(db.Text, nullable=True)
    # {"add": n, "change": n, "destroy": n, "resources": [...], "checks": [...]}
    plan_summary_json = db.Column(db.JSON, nullable=True)
    # OpenTofu's own human-readable plan, scrubbed.
    plan_text = db.Column(db.Text, nullable=True)
    log_tail = db.Column(db.Text, nullable=True)
    # {"phase": "...", "vms": {name: {"state": ..., "detail": ...}}}
    progress_json = db.Column(db.JSON, nullable=True)
    error = db.Column(db.Text, nullable=True)
    # The build status to return to when a grow or destroy does not go through.
    prior_build_status = db.Column(db.String(24), nullable=True)
    requested_by = db.Column(db.String(120), nullable=True)
    requested_by_user_id = db.Column(db.Integer, nullable=True)
    reason = db.Column(db.Text, nullable=True)
    applied_by = db.Column(db.String(120), nullable=True)
    applied_by_user_id = db.Column(db.Integer, nullable=True)
    approved_by = db.Column(db.String(120), nullable=True)
    approved_by_user_id = db.Column(db.Integer, nullable=True)
    approved_at = db.Column(db.DateTime(timezone=True), nullable=True)
    decision_note = db.Column(db.Text, nullable=True)
    # sha256 of the per-job secret OpenTofu presents to the state backend.
    auth_token_hash = db.Column(db.String(64), nullable=True)
    # Recovery jobs apply on their own when the new plan only finishes what
    # the interrupted, already-approved job set out to do.
    auto_apply = db.Column(db.Boolean, nullable=False, default=False)
    recovered_from_job_id = db.Column(db.Integer, nullable=True)
    created_at = db.Column(db.DateTime(timezone=True), nullable=False, default=_utcnow)
    started_at = db.Column(db.DateTime(timezone=True), nullable=True)
    finished_at = db.Column(db.DateTime(timezone=True), nullable=True)
    # Heartbeat while a worker drives the job.
    updated_at = db.Column(
        db.DateTime(timezone=True), nullable=False, default=_utcnow, onupdate=_utcnow
    )
