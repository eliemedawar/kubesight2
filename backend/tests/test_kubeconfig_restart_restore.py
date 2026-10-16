"""A backend restart wipes the kubeconfig files; the database copy brings them back."""

import shutil
from pathlib import Path

import pytest

from api.cluster_store import (
    delete_kubeconfig_file,
    read_kubeconfig_file,
    restore_kubeconfig_files,
    write_kubeconfig_file,
)
from api.db import db
from api.k8s_provider import resolve_cluster_access
from api.models import Cluster

KUBECONFIG = """apiVersion: v1
kind: Config
clusters:
- name: c
  cluster: {server: "https://10.4.112.2:6443"}
contexts:
- name: ctx
  context: {cluster: c, user: u}
current-context: ctx
users:
- name: u
  user: {token: secret-token}
"""


@pytest.fixture()
def stored_cluster(app, tmp_path, monkeypatch):
    storage = tmp_path / "kubeconfigs"
    monkeypatch.setenv("KUBESIGHT_KUBECONFIG_DIR", str(storage))
    cluster = Cluster(name="test", host="10.4.112.2", port=6443, protocol="https", is_active=True)
    db.session.add(cluster)
    db.session.flush()
    cluster.kubeconfig_path = write_kubeconfig_file(cluster.id, KUBECONFIG)
    db.session.commit()
    return cluster.id, storage


def _restart(storage: Path):
    shutil.rmtree(storage)


def test_save_keeps_an_encrypted_copy_in_the_database(stored_cluster):
    cluster_id, _ = stored_cluster
    cluster = db.session.get(Cluster, cluster_id)
    assert cluster.kubeconfig_cipher
    assert "secret-token" not in cluster.kubeconfig_cipher
    assert Path(cluster.kubeconfig_path).read_text(encoding="utf-8") == cluster.kubeconfig_cipher


def test_boot_restores_files_a_restart_wiped(stored_cluster):
    cluster_id, storage = stored_cluster
    _restart(storage)
    assert restore_kubeconfig_files() == (1, 0)
    assert "secret-token" in read_kubeconfig_file(cluster_id)


def test_reading_the_cluster_restores_the_file_without_a_boot(stored_cluster):
    cluster_id, storage = stored_cluster
    _restart(storage)
    access = resolve_cluster_access(f"custom-{cluster_id}")
    assert access is not None
    assert Path(access.kubeconfig_path).is_file()


def test_boot_saves_files_that_have_no_database_copy(stored_cluster):
    cluster_id, storage = stored_cluster
    cluster = db.session.get(Cluster, cluster_id)
    cluster.kubeconfig_cipher = None  # registered before the copy existed
    db.session.commit()
    assert restore_kubeconfig_files() == (0, 1)
    _restart(storage)
    assert restore_kubeconfig_files() == (1, 0)
    assert "secret-token" in read_kubeconfig_file(cluster_id)


def test_removed_cluster_is_never_restored(stored_cluster):
    cluster_id, storage = stored_cluster
    cluster = db.session.get(Cluster, cluster_id)
    delete_kubeconfig_file(cluster_id)
    cluster.kubeconfig_path = None
    cluster.is_active = False
    db.session.commit()
    assert cluster.kubeconfig_cipher is None
    assert restore_kubeconfig_files() == (0, 0)
    assert not any(storage.iterdir())
