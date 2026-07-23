"""Final phase: fetch admin.conf, register the cluster, make it visible.

The admin.conf content holds the cluster CA key material and a
cluster-admin credential — it is passed straight from the SSH channel into
``cluster_store`` and never enters a log or step record.
"""

from __future__ import annotations

from typing import Tuple

from ...cluster_store import (
    build_cluster_kubeconfig,
    custom_cluster_public_id,
    validate_name,
    write_kubeconfig_file,
)
from ...db import db
from ...models import Cluster, ClusterBuild


def _endpoint_parts(endpoint: str) -> Tuple[str, int]:
    host, _, port = endpoint.rpartition(":")
    if not host:
        return endpoint, 6443
    try:
        return host, int(port)
    except ValueError:
        return endpoint, 6443


def register_cluster(build: ClusterBuild, admin_conf: str) -> str:
    """Create the Cluster row + kubeconfig file; returns the public id."""
    host, port = _endpoint_parts(build.control_plane_endpoint)
    cluster = Cluster(
        name=validate_name(build.name),
        host=host,
        port=port,
        protocol="https",
        connection_method="kubeconfig",
        authentication_type="kubeconfig",
        # The API serves a kubeadm-issued cert; the kubeconfig carries its CA,
        # so verification stays ON.
        skip_tls_verify=False,
        is_active=True,
    )
    db.session.add(cluster)
    db.session.flush()  # allocate the id for the kubeconfig filename

    rendered, context_name = build_cluster_kubeconfig(
        kubeconfig_content=admin_conf,
        host=host,
        port=port,
        protocol="https",
        context_name=None,
    )
    path = write_kubeconfig_file(cluster.id, rendered)
    cluster.kubeconfig_path = path
    cluster.context_name = context_name
    db.session.commit()

    public_id = custom_cluster_public_id(cluster.id)
    build.result_cluster_id = public_id
    db.session.commit()
    return public_id
