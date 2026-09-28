"""Recover a builder-created cluster's missing local connection file over SSH."""

from ...access_engine import can_access_cluster, user_has_permission
from ...audit import log_audit
from ...cluster_store import build_cluster_kubeconfig, custom_cluster_public_id, write_kubeconfig_file
from ...db import db
from ...kubeconfig_vault import kubeconfig_exists
from ...models import ClusterBuild
from .. import ssh_profile_service
from ..ssh import SshCommandError, SshConnectionError, get_transport


def restore_missing_kubeconfig(cluster, user) -> bool:
    """Restore the existing registration, never recreate a deleted cluster.

    Called explicitly by Test connection, not by background/read requests.
    Retrieving admin.conf uses the build's normal SSH/host-key policy, requires
    both builder execution and cluster update access, and never returns or logs
    the SSH output (including error output, which could contain credentials).
    """
    if not cluster.is_active or kubeconfig_exists(cluster.kubeconfig_path):
        return False
    cluster_id = custom_cluster_public_id(cluster.id)
    build = ClusterBuild.query.filter(
        ClusterBuild.result_cluster_id == cluster_id,
        ClusterBuild.status.in_(("completed", "failed", "cancelled")),
    ).order_by(ClusterBuild.id.desc()).first()
    if build is None:
        return False
    if (
        user is None
        or not can_access_cluster(user, cluster_id)
        or not user_has_permission(user, "clusters:update")
        or not user_has_permission(user, "cluster_builds:execute")
    ):
        raise PermissionError(
            "Restoring this cluster's connection requires cluster access, "
            "clusters:update and cluster_builds:execute."
        )
    nodes = sorted(
        (node for node in build.nodes if node.role == "control_plane"),
        key=lambda node: (not node.is_primary_cp, node.position),
    )
    for node in nodes:
        profile_id = node.connection_profile_id or build.connection_profile_id
        if not profile_id:
            continue
        try:
            profile = ssh_profile_service.get_profile(profile_id)
            target = ssh_profile_service.build_target(profile, node.address)
            result = get_transport().run(
                target, "cat /etc/kubernetes/admin.conf", timeout_s=60
            )
            rendered, context = build_cluster_kubeconfig(
                kubeconfig_content=result.output,
                host=cluster.host,
                port=cluster.port,
                protocol=cluster.protocol,
                context_name=None,
            )
        except (SshCommandError, SshConnectionError, LookupError, ValueError, OSError):
            # Try another control plane, but never put admin.conf in an error.
            continue
        try:
            path = write_kubeconfig_file(cluster.id, rendered)
        except OSError:
            raise ValueError(
                "Could not save the recovered kubeconfig. Check that the "
                "backend's kubeconfig storage is mounted and writable."
            ) from None
        cluster.kubeconfig_path = path
        cluster.context_name = context
        db.session.commit()
        log_audit(
            "cluster_kubeconfig_restored",
            actor=user,
            target_type="cluster",
            target_id=cluster_id,
            details={"buildId": build.id, "nodeId": node.id},
        )
        return True
    raise ValueError(
        "KubeSight could not recover the missing kubeconfig from this build's "
        "control-plane nodes. Check the saved SSH connection profile, host-key "
        "trust, and SSH access to /etc/kubernetes/admin.conf."
    )
