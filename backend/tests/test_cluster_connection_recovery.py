"""A built cluster can recover its connection without copying admin.conf by hand."""

from pathlib import Path

import pytest
import yaml

from api.cluster_store import read_kubeconfig_file
from api.db import db
from api.models import AuditLog, Cluster, ClusterBuild, ClusterBuildNode
from api.services.cluster_build import recovery
from api.services.ssh import SshCommandError
from tests.conftest import auth_headers
from tests.test_cluster_builds import ADMIN_CONF, fake_ssh, ssh_profile  # noqa: F401


@pytest.fixture()
def built_cluster(app, tmp_path, monkeypatch, ssh_profile):
    monkeypatch.setenv("KUBESIGHT_KUBECONFIG_DIR", str(tmp_path / "persisted"))
    cluster = Cluster(
        name="Built cluster", host="10.0.0.100", port=6443, protocol="https",
        is_active=True, connection_method="kubeconfig",
        kubeconfig_path=str(tmp_path / "old-pod" / "cluster.yaml"),
    )
    db.session.add(cluster)
    db.session.flush()
    build = ClusterBuild(
        name=cluster.name, status="completed", result_cluster_id=f"custom-{cluster.id}",
        connection_profile_id=ssh_profile["id"],
    )
    db.session.add(build)
    db.session.flush()
    db.session.add(ClusterBuildNode(
        build_id=build.id, role="control_plane", address="10.0.0.11",
        position=0, is_primary_cp=True,
    ))
    db.session.commit()
    return cluster.id, build.id


def _test_connection(client, token, cluster_id):
    return client.post(
        f"/api/clusters/custom/custom-{cluster_id}/test", headers=auth_headers(token)
    )


def test_missing_file_is_recovered_encrypted_without_rebuilding(
    app, client, admin_token, built_cluster, fake_ssh, monkeypatch
):
    from api.routes import clusters as routes

    cluster_id, build_id = built_cluster
    fake_ssh.add(lambda host, cmd: cmd == "cat /etc/kubernetes/admin.conf", ADMIN_CONF)

    def probe(cluster):
        document = yaml.safe_load(read_kubeconfig_file(cluster.id))
        assert document["clusters"][0]["cluster"]["server"] == "https://10.0.0.100:6443"
        assert cluster.context_name == "kubernetes-admin@kubernetes"
        return {"success": True, "reachable": True, "nodes": [{"name": "cp-1"}]}

    monkeypatch.setattr(routes, "test_cluster_connection", probe)
    response = _test_connection(client, admin_token, cluster_id)
    assert response.status_code == 200
    data = response.get_json()["data"]
    assert data["success"] is True
    assert data["kubeconfigRestored"] is True
    assert "client-key-data" not in response.get_data(as_text=True)
    assert fake_ssh.calls == [("10.0.0.11", "cat /etc/kubernetes/admin.conf")]
    cluster = db.session.get(Cluster, cluster_id)
    assert cluster.last_connection_status == "connected"
    assert cluster.kubeconfig_path.endswith(".enc")
    assert "apiVersion" not in Path(cluster.kubeconfig_path).read_text()
    assert Cluster.query.count() == 1
    build = db.session.get(ClusterBuild, build_id)
    assert build.status == "completed"
    assert build.result_cluster_id == f"custom-{cluster_id}"
    audit = AuditLog.query.filter_by(action="cluster_kubeconfig_restored").one()
    assert audit.details["buildId"] == build_id
    assert "client-key-data" not in str(audit.details)

    # Testing an already-restored file must not fetch credentials again.
    assert _test_connection(client, admin_token, cluster_id).status_code == 200
    assert len(fake_ssh.calls) == 1


def test_unmanaged_cluster_keeps_missing_file_error(
    app, client, admin_token, built_cluster, fake_ssh
):
    cluster_id, build_id = built_cluster
    db.session.get(ClusterBuild, build_id).result_cluster_id = None
    db.session.commit()
    data = _test_connection(client, admin_token, cluster_id).get_json()["data"]
    assert data["success"] is False
    assert "Kubeconfig file is missing" in data["error"]
    assert fake_ssh.calls == []


@pytest.mark.parametrize("denied", ["clusters:update", "cluster_builds:execute", "scope"])
def test_recovery_requires_update_execution_and_cluster_access(
    app, client, admin_token, built_cluster, fake_ssh, monkeypatch, denied
):
    cluster_id, _ = built_cluster
    if denied == "scope":
        monkeypatch.setattr(recovery, "can_access_cluster", lambda *args: False)
    else:
        original = recovery.user_has_permission
        monkeypatch.setattr(
            recovery, "user_has_permission",
            lambda user, permission: permission != denied and original(user, permission),
        )
    assert _test_connection(client, admin_token, cluster_id).status_code == 403
    assert fake_ssh.calls == []


def test_recovery_failure_does_not_leak_ssh_output_or_change_registration(
    app, client, admin_token, built_cluster, fake_ssh
):
    cluster_id, _ = built_cluster
    old_path = db.session.get(Cluster, cluster_id).kubeconfig_path
    fake_ssh.add(
        lambda *args: True, SshCommandError("secret-output", exit_code=1, output=ADMIN_CONF)
    )
    response = _test_connection(client, admin_token, cluster_id)
    assert response.status_code == 200
    assert response.get_json()["data"]["success"] is False
    assert "could not recover" in response.get_json()["data"]["error"]
    assert "secret-output" not in response.get_data(as_text=True)
    assert "client-key-data" not in response.get_data(as_text=True)
    assert db.session.get(Cluster, cluster_id).kubeconfig_path == old_path
    assert AuditLog.query.filter_by(action="cluster_kubeconfig_restored").count() == 0


def test_removed_cluster_is_not_restored(app, client, admin_token, built_cluster, fake_ssh):
    cluster_id, _ = built_cluster
    db.session.get(Cluster, cluster_id).is_active = False
    db.session.commit()
    assert _test_connection(client, admin_token, cluster_id).status_code == 400
    assert fake_ssh.calls == []


def test_recovery_tries_another_control_plane_and_reports_api_failure(
    app, client, admin_token, built_cluster, fake_ssh, monkeypatch
):
    from api.routes import clusters as routes

    cluster_id, build_id = built_cluster
    db.session.add(ClusterBuildNode(
        build_id=build_id, role="control_plane", address="10.0.0.12", position=1,
    ))
    db.session.commit()
    fake_ssh.add(
        lambda host, cmd: host == "10.0.0.11",
        SshCommandError("unreachable", exit_code=1, output=""),
    )
    fake_ssh.add(lambda host, cmd: host == "10.0.0.12", ADMIN_CONF)
    monkeypatch.setattr(routes, "test_cluster_connection", lambda cluster: {
        "success": False, "reachable": False, "error": "API connection timed out",
    })
    data = _test_connection(client, admin_token, cluster_id).get_json()["data"]
    assert data["kubeconfigRestored"] is True
    assert data["success"] is False
    assert data["error"] == "API connection timed out"
    assert [host for host, cmd in fake_ssh.calls] == ["10.0.0.11", "10.0.0.12"]
    assert Path(db.session.get(Cluster, cluster_id).kubeconfig_path).exists()


def test_invalid_remote_file_is_not_saved(app, client, admin_token, built_cluster, fake_ssh):
    cluster_id, _ = built_cluster
    old_path = db.session.get(Cluster, cluster_id).kubeconfig_path
    fake_ssh.add(lambda *args: True, "not a kubeconfig")
    data = _test_connection(client, admin_token, cluster_id).get_json()["data"]
    assert data["success"] is False
    assert db.session.get(Cluster, cluster_id).kubeconfig_path == old_path
    assert not Path(old_path).exists()
