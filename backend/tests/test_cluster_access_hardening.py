"""Area C — cluster access hardening.

1. Kubeconfigs encrypted at rest; plaintext only for the life of a subprocess.
2. Deleting a registered cluster deletes its kubeconfig.
3. Cluster overview fills workload counts and storage (null, never a fake 0).
4. Upgrade Center jobs persist and are marked interrupted after a restart.
5. SSH host keys can be listed, scanned, pinned (audited) and removed.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import pytest

from tests.conftest import auth_headers

KUBECONFIG = """apiVersion: v1
kind: Config
clusters:
  - name: test
    cluster:
      server: https://127.0.0.1:6443
contexts:
  - name: test
    context:
      cluster: test
      user: test
current-context: test
users:
  - name: test
    user:
      token: super-secret-token-value
"""


@pytest.fixture()
def kube_dir(app, tmp_path, monkeypatch):
    directory = tmp_path / "kubeconfigs"
    monkeypatch.setenv("KUBESIGHT_KUBECONFIG_DIR", str(directory))
    return directory


# ---------------------------------------------------------------------------
# 1. Encryption at rest
# ---------------------------------------------------------------------------

class TestKubeconfigEncryption:
    def test_write_stores_ciphertext_only(self, kube_dir):
        from api.cluster_store import read_kubeconfig_file, write_kubeconfig_file

        path = write_kubeconfig_file(7, KUBECONFIG)
        assert path.endswith("cluster-7.yaml.enc")
        raw = Path(path).read_text(encoding="utf-8")
        assert "super-secret-token-value" not in raw
        assert "apiVersion" not in raw
        assert not (kube_dir / "cluster-7.yaml").exists()
        assert read_kubeconfig_file(7) == KUBECONFIG

    def test_rewrite_removes_legacy_plaintext(self, kube_dir):
        from api.cluster_store import write_kubeconfig_file

        kube_dir.mkdir(parents=True, exist_ok=True)
        (kube_dir / "cluster-3.yaml").write_text(KUBECONFIG, encoding="utf-8")
        write_kubeconfig_file(3, KUBECONFIG)
        assert not (kube_dir / "cluster-3.yaml").exists()
        assert (kube_dir / "cluster-3.yaml.enc").exists()

    def test_materialized_copy_is_shared_and_deleted(self, kube_dir):
        from api.cluster_store import write_kubeconfig_file
        from api.kubeconfig_vault import live_materializations, materialized_kubeconfig

        stored = write_kubeconfig_file(8, KUBECONFIG)
        before = live_materializations()
        with materialized_kubeconfig(stored) as first:
            assert first != stored
            assert Path(first).read_text(encoding="utf-8") == KUBECONFIG
            with materialized_kubeconfig(stored) as second:
                # Concurrent users share one plaintext copy.
                assert second == first
            assert Path(first).exists()  # still leased by the outer block
        assert not Path(first).exists()
        assert live_materializations() == before

    def test_copy_deleted_even_when_the_call_raises(self, kube_dir):
        from api.cluster_store import write_kubeconfig_file
        from api.kubeconfig_vault import materialized_kubeconfig

        stored = write_kubeconfig_file(9, KUBECONFIG)
        seen = {}
        with pytest.raises(RuntimeError):
            with materialized_kubeconfig(stored) as plain:
                seen["path"] = plain
                raise RuntimeError("kubectl blew up")
        assert not Path(seen["path"]).exists()

    def test_passthrough_for_paths_that_are_not_ours(self, tmp_path):
        from api.kubeconfig_vault import materialized_kubeconfig

        local = tmp_path / "config"
        local.write_text(KUBECONFIG, encoding="utf-8")
        with materialized_kubeconfig(str(local)) as plain:
            assert plain == str(local)
        with materialized_kubeconfig(None) as plain:
            assert plain is None
        assert local.exists()

    def test_wrong_key_is_a_clear_error(self, kube_dir, monkeypatch):
        from api.cluster_store import write_kubeconfig_file
        from api.k8s_provider import K8sCommandError, _run_kubectl

        stored = write_kubeconfig_file(10, KUBECONFIG)
        Path(stored).write_text("not-a-fernet-token", encoding="utf-8")
        with pytest.raises(K8sCommandError, match="could not be decrypted"):
            _run_kubectl(["get", "pods"], kubeconfig_path=stored)

    def test_kubectl_gets_a_temp_plaintext_file_that_is_removed(self, kube_dir):
        from api import k8s_provider
        from api.cluster_store import write_kubeconfig_file

        stored = write_kubeconfig_file(11, KUBECONFIG)
        captured = {}

        def fake_run(command, **kwargs):
            path = command[command.index("--kubeconfig") + 1]
            captured["path"] = path
            captured["env"] = kwargs["env"]["KUBECONFIG"]
            captured["content"] = Path(path).read_text(encoding="utf-8")
            return subprocess.CompletedProcess(command, 0, stdout="ok", stderr="")

        with patch.object(k8s_provider.subprocess, "run", side_effect=fake_run):
            assert k8s_provider._run_kubectl(["get", "ns"], kubeconfig_path=stored) == "ok"

        assert captured["path"] != stored
        assert captured["env"] == captured["path"]
        assert captured["content"] == KUBECONFIG
        assert not Path(captured["path"]).exists()

    def test_breaker_key_is_the_cluster_id_not_a_path(self, kube_dir):
        from api.kubeconfig_vault import kubeconfig_identity

        assert kubeconfig_identity(str(kube_dir / "cluster-5.yaml.enc")) == "custom-5"
        assert kubeconfig_identity(str(kube_dir / "cluster-5.yaml")) == "custom-5"
        assert kubeconfig_identity("/home/me/.kube/config") == "/home/me/.kube/config"
        assert kubeconfig_identity(None) == ""

    def test_log_stream_holds_the_copy_until_the_stream_ends(self, kube_dir):
        from api import k8s_provider
        from api.cluster_access import ClusterAccess
        from api.cluster_store import write_kubeconfig_file

        stored = write_kubeconfig_file(12, KUBECONFIG)
        seen = {}

        class FakeProcess:
            def __init__(self):
                self.stdout = iter(["line one\n", "line two\n"])
                self.stderr = None

            def poll(self):
                return 0

            def terminate(self):
                pass

            def wait(self, timeout=None):
                return 0

        def fake_popen(args, context=None, kubeconfig_path=None):
            seen["path"] = kubeconfig_path
            return FakeProcess()

        access = ClusterAccess(
            cluster_id="custom-12", context_name="test", kubeconfig_path=stored, is_custom=True
        )
        with patch.object(k8s_provider, "_popen_kubectl", side_effect=fake_popen):
            stream = k8s_provider.stream_pod_log_lines(
                access=access, namespace="default", pod="p", container=None,
                heartbeat_seconds=0.5,
            )
            first = next(stream)
            assert first == ("line", "line one")
            assert Path(seen["path"]).exists()  # live while streaming
            assert Path(seen["path"]).read_text(encoding="utf-8") == KUBECONFIG
            stream.close()  # client disconnect
        assert not Path(seen["path"]).exists()

    def test_helm_runs_against_the_temp_copy(self, kube_dir):
        from api.cluster_access import ClusterAccess
        from api.cluster_store import write_kubeconfig_file
        from api.services import helm_service

        stored = write_kubeconfig_file(13, KUBECONFIG)
        captured = {}

        def fake_run(command, **kwargs):
            captured["path"] = command[command.index("--kubeconfig") + 1]
            captured["exists"] = Path(captured["path"]).exists()
            return subprocess.CompletedProcess(command, 0, stdout="[]", stderr="")

        access = ClusterAccess(cluster_id="custom-13", context_name="test", kubeconfig_path=stored)
        with patch.object(helm_service, "ensure_helm_installed"), patch.object(
            helm_service.subprocess, "run", side_effect=fake_run
        ):
            assert helm_service.run_helm(access, ["list", "-o", "json"]) == "[]"
        assert captured["exists"] is True
        assert captured["path"] != stored
        assert not Path(captured["path"]).exists()


class TestPlaintextMigration:
    def test_encrypts_and_repoints_rows(self, kube_dir):
        from api.cluster_store import migrate_plaintext_kubeconfigs
        from api.db import db
        from api.models import Cluster

        kube_dir.mkdir(parents=True, exist_ok=True)
        cluster = Cluster(name="legacy", host="127.0.0.1", port=6443, protocol="https")
        db.session.add(cluster)
        db.session.flush()
        plain = kube_dir / f"cluster-{cluster.id}.yaml"
        plain.write_text(KUBECONFIG, encoding="utf-8")
        cluster.kubeconfig_path = str(plain)
        db.session.commit()

        assert migrate_plaintext_kubeconfigs() == 1
        db.session.refresh(cluster)
        assert not plain.exists()
        assert cluster.kubeconfig_path.endswith(f"cluster-{cluster.id}.yaml.enc")
        encrypted = Path(cluster.kubeconfig_path)
        assert "super-secret-token-value" not in encrypted.read_text(encoding="utf-8")

        # Idempotent: a second start changes nothing.
        assert migrate_plaintext_kubeconfigs() == 0
        db.session.refresh(cluster)
        assert cluster.kubeconfig_path == str(encrypted)

    def test_runs_from_run_migrations(self, kube_dir):
        from api.migrate_rbac import run_migrations

        kube_dir.mkdir(parents=True, exist_ok=True)
        (kube_dir / "cluster-99.yaml").write_text(KUBECONFIG, encoding="utf-8")
        run_migrations()
        assert not (kube_dir / "cluster-99.yaml").exists()
        assert (kube_dir / "cluster-99.yaml.enc").exists()


# ---------------------------------------------------------------------------
# 2. Delete removes the kubeconfig
# ---------------------------------------------------------------------------

def test_delete_custom_cluster_deletes_kubeconfig(client, admin_token, kube_dir):
    from api.db import db
    from api.models import AuditLog, Cluster

    with patch(
        "api.routes.clusters.test_cluster_connection",
        return_value={"success": False, "reachable": False, "error": "offline"},
    ):
        create = client.post(
            "/api/clusters/custom",
            headers=auth_headers(admin_token),
            json={"name": "Doomed", "connectionMethod": "kubeconfig", "kubeconfigContent": KUBECONFIG},
        )
    assert create.status_code in (200, 201), create.get_json()
    public_id = create.get_json()["data"]["cluster"]["publicId"]
    db_id = create.get_json()["data"]["cluster"]["id"]
    stored = Path(Cluster.query.get(db_id).kubeconfig_path)
    assert stored.exists() and stored.name.endswith(".yaml.enc")

    delete = client.delete(f"/api/clusters/custom/{public_id}", headers=auth_headers(admin_token))
    assert delete.status_code == 200
    db.session.expire_all()
    row = Cluster.query.get(db_id)
    assert row.is_active is False
    assert row.kubeconfig_path is None
    assert not stored.exists()
    assert AuditLog.query.filter_by(action="cluster_removed", target_id=public_id).count() == 1


# ---------------------------------------------------------------------------
# 3. Overview workloads + storage
# ---------------------------------------------------------------------------

def _overview_runner(responses):
    def run(access, args, timeout=None):
        key = " ".join(args)
        # Longest prefix first: "get pvc" must not be answered as "get pv".
        for prefix, value in sorted(responses.items(), key=lambda kv: -len(kv[0])):
            if key.startswith(prefix + " ") or key == prefix:
                if isinstance(value, Exception):
                    raise value
                return value
        raise AssertionError(f"unexpected kubectl call: {key}")

    return run


def test_overview_counts_workloads_and_sums_storage(app):
    from api import k8s_provider
    from api.cluster_access import ClusterAccess

    responses = {
        "get nodes": json.dumps({"items": []}),
        "get pods": json.dumps({"items": []}),
        "get deployments,statefulsets,daemonsets": "Deployment\nDeployment\nStatefulSet\nDaemonSet\nDeployment\n",
        "get pv": "10Gi\n512Mi\n<none>\n",
        "get pvc": "8Gi\n2Gi\n",
    }
    access = ClusterAccess(cluster_id="c-ov", context_name="c-ov")
    with patch.object(k8s_provider, "_run_for_access", side_effect=_overview_runner(responses)), patch(
        "api.k8s_metrics.cluster_resource_usage", return_value=(0, 0, 0.0, 0.0)
    ):
        overview = k8s_provider._cluster_overview_from_k8s_uncached(access)

    assert overview["workloads"] == {"deployments": 3, "statefulsets": 1, "daemonsets": 1}
    storage = overview["resources"]["storage"]
    assert storage["capacityGiB"] == 10.5
    assert storage["claimedGiB"] == 10.0
    assert storage["usedGiB"] is None  # unknown, not a fake 0


def test_overview_extras_are_null_when_kubectl_cannot_list(app):
    from api import k8s_provider
    from api.cluster_access import ClusterAccess

    forbidden = k8s_provider.K8sCommandError("forbidden")
    responses = {
        "get nodes": json.dumps({"items": []}),
        "get pods": json.dumps({"items": []}),
        "get deployments,statefulsets,daemonsets": forbidden,
        "get pv": forbidden,
        "get pvc": forbidden,
    }
    access = ClusterAccess(cluster_id="c-ov2", context_name="c-ov2")
    with patch.object(k8s_provider, "_run_for_access", side_effect=_overview_runner(responses)), patch(
        "api.k8s_metrics.cluster_resource_usage", return_value=(0, 0, 0.0, 0.0)
    ):
        overview = k8s_provider._cluster_overview_from_k8s_uncached(access)
    assert overview["workloads"] == {"deployments": None, "statefulsets": None, "daemonsets": None}
    assert overview["resources"]["storage"] == {"usedGiB": None, "capacityGiB": None, "claimedGiB": None}


def test_quantity_parser():
    from api.k8s_provider import _quantity_to_gib

    assert _quantity_to_gib("1Gi") == 1.0
    assert _quantity_to_gib("1024Mi") == 1.0
    assert _quantity_to_gib("1Ti") == 1024.0
    assert round(_quantity_to_gib("1G"), 4) == round(1e9 / 1024 ** 3, 4)
    assert _quantity_to_gib(str(1024 ** 3)) == 1.0
    assert _quantity_to_gib("<none>") is None
    assert _quantity_to_gib("") is None


def test_overview_mock_mode_still_serves(client, admin_token):
    response = client.get("/api/clusters/staging-eu-west/overview", headers=auth_headers(admin_token))
    assert response.status_code == 200
    data = response.get_json()["data"]
    assert data["resources"]["storage"]["capacityGiB"] > 0


# ---------------------------------------------------------------------------
# 4. Upgrade jobs persist
# ---------------------------------------------------------------------------

class TestUpgradeJobPersistence:
    def test_job_survives_losing_the_in_memory_copy(self, app, client, admin_token):
        from api import upgrade_jobs

        job = upgrade_jobs.create_job(
            cluster_id="kubeadm-cluster", target_version="v1.31.0", provider="kubeadm", steps=[]
        )
        upgrade_jobs.update_job(job["jobId"], status="running", activeStep=2)
        with upgrade_jobs._lock:
            upgrade_jobs._jobs.clear()  # what a restart does

        response = client.get(f"/api/upgrades/jobs/{job['jobId']}", headers=auth_headers(admin_token))
        assert response.status_code == 200, response.get_json()
        data = response.get_json()["data"]
        assert data["jobId"] == job["jobId"]
        assert data["status"] == "running"  # heartbeat still fresh
        assert data["activeStep"] == 2

    def test_startup_marks_orphaned_running_jobs_interrupted(self, app):
        from api import upgrade_jobs
        from api.db import db
        from api.models import UpgradeJob

        job = upgrade_jobs.create_job(
            cluster_id="c1", target_version="v1.31.0", provider="kubeadm"
        )
        upgrade_jobs.update_job(job["jobId"], status="running")
        fresh = upgrade_jobs.create_job(cluster_id="c2", target_version="v1.31.0", provider="kubeadm")
        upgrade_jobs.update_job(fresh["jobId"], status="running")
        with upgrade_jobs._lock:
            upgrade_jobs._jobs.clear()
        row = UpgradeJob.query.filter_by(job_id=job["jobId"]).one()
        row.heartbeat_at = datetime.now(timezone.utc) - timedelta(minutes=30)
        db.session.commit()

        assert upgrade_jobs.interrupt_orphaned_jobs() == 1
        loaded = upgrade_jobs.get_job(job["jobId"])
        assert loaded["status"] == "failed"
        assert loaded["interrupted"] is True
        assert "interrupted" in loaded["message"].lower()
        # A job another live process is still heart-beating is left alone.
        assert upgrade_jobs.get_job(fresh["jobId"])["status"] == "running"

    def test_stale_job_is_reconciled_on_read(self, app):
        from api import upgrade_jobs
        from api.db import db
        from api.models import UpgradeJob

        job = upgrade_jobs.create_job(cluster_id="c3", target_version="v1.31.0", provider="minikube")
        upgrade_jobs.update_job(job["jobId"], status="running")
        with upgrade_jobs._lock:
            upgrade_jobs._jobs.clear()
        row = UpgradeJob.query.filter_by(job_id=job["jobId"]).one()
        row.heartbeat_at = datetime.now(timezone.utc) - timedelta(minutes=10)
        db.session.commit()

        assert upgrade_jobs.get_job(job["jobId"])["status"] == "failed"
        db.session.expire_all()
        assert UpgradeJob.query.filter_by(job_id=job["jobId"]).one().status == "failed"

    def test_unknown_job_is_404(self, client, admin_token):
        response = client.get("/api/upgrades/jobs/upgrade-nope", headers=auth_headers(admin_token))
        assert response.status_code == 404


# ---------------------------------------------------------------------------
# 5. SSH host keys
# ---------------------------------------------------------------------------

KEY_BYTES = b"\x00\x00\x00\x0bssh-ed25519\x00\x00\x00 " + b"k" * 32
KEY_HEX = hashlib.sha256(KEY_BYTES).hexdigest()
KEY_OPENSSH = "SHA256:" + base64.b64encode(bytes.fromhex(KEY_HEX)).decode().rstrip("=")


class _ScanTransport:
    def __init__(self, key_bytes=KEY_BYTES):
        self.key_bytes = key_bytes
        self.calls = []

    def scan_host_key(self, host, port=22, *, timeout_s=10, bastion=None):
        self.calls.append((host, port, bastion))
        return {"keyType": "ssh-ed25519", "keyBytes": self.key_bytes}


@pytest.fixture()
def scan_transport(app):
    from api.services.ssh import set_transport_factory

    fake = _ScanTransport()
    set_transport_factory(lambda: fake)
    yield fake
    set_transport_factory(None)


class TestHostKeys:
    def test_fingerprint_formats(self):
        from api.services.ssh.hostkeys import HostKeyError, normalize_fingerprint, openssh_fingerprint

        assert openssh_fingerprint(KEY_HEX) == KEY_OPENSSH
        assert normalize_fingerprint(KEY_OPENSSH) == KEY_HEX
        assert normalize_fingerprint(KEY_HEX.upper()) == KEY_HEX
        colon = ":".join(KEY_HEX[i:i + 2] for i in range(0, 64, 2))
        assert normalize_fingerprint(colon) == KEY_HEX
        for bad in ("", "MD5:aa:bb", "SHA256:@@@", "abc"):
            with pytest.raises(HostKeyError):
                normalize_fingerprint(bad)

    def test_scan_trusts_nothing(self, client, admin_token, scan_transport):
        from api.models import AuditLog, SshHostKey

        response = client.post(
            "/api/ssh-host-keys/scan", headers=auth_headers(admin_token),
            json={"host": "10.0.0.11", "port": 22},
        )
        assert response.status_code == 200, response.get_json()
        data = response.get_json()["data"]
        assert data["status"] == "unknown"
        assert data["fingerprint"] == KEY_OPENSSH
        assert data["fingerprintSha256"] == KEY_HEX
        assert SshHostKey.query.count() == 0
        assert AuditLog.query.filter_by(action="ssh_host_key_scanned").count() == 1

    def test_pin_makes_the_pinned_policy_satisfiable(self, client, admin_token, scan_transport):
        from api.models import AuditLog
        from api.services.ssh.hostkeys import verify_host_key

        ok, _ = verify_host_key(
            host="10.0.0.11", port=22, key_type="ssh-ed25519",
            fingerprint_sha256=KEY_HEX, policy="pinned",
        )
        assert ok is False

        pin = client.post(
            "/api/ssh-host-keys", headers=auth_headers(admin_token),
            json={"host": "10.0.0.11", "port": 22, "keyType": "ssh-ed25519", "fingerprint": KEY_OPENSSH},
        )
        assert pin.status_code == 201, pin.get_json()
        row = pin.get_json()["data"]
        assert row["source"] == "preapproved"
        assert row["approvedBy"] == "admin"

        for policy in ("pinned", "strict"):
            ok, reason = verify_host_key(
                host="10.0.0.11", port=22, key_type="ssh-ed25519",
                fingerprint_sha256=KEY_HEX, policy=policy,
            )
            assert ok is True, reason
        entry = AuditLog.query.filter_by(action="ssh_host_key_approved").one()
        assert entry.target_id == "10.0.0.11:22"
        assert entry.details["fingerprint"] == KEY_OPENSSH

        rescanned = client.post(
            "/api/ssh-host-keys/scan", headers=auth_headers(admin_token), json={"host": "10.0.0.11"},
        ).get_json()["data"]
        assert rescanned["status"] == "match"
        assert rescanned["recorded"]["source"] == "preapproved"

        listing = client.get("/api/ssh-host-keys", headers=auth_headers(admin_token))
        assert [item["fingerprint"] for item in listing.get_json()["data"]["items"]] == [KEY_OPENSSH]

    def test_changed_fingerprint_needs_explicit_replace(self, client, admin_token, scan_transport):
        from api.models import AuditLog

        body = {"host": "10.0.0.12", "keyType": "ssh-ed25519", "fingerprint": KEY_HEX}
        assert client.post("/api/ssh-host-keys", headers=auth_headers(admin_token), json=body).status_code == 201

        other = hashlib.sha256(b"rebuilt").hexdigest()
        conflict = client.post(
            "/api/ssh-host-keys", headers=auth_headers(admin_token), json={**body, "fingerprint": other}
        )
        assert conflict.status_code == 409
        replaced = client.post(
            "/api/ssh-host-keys", headers=auth_headers(admin_token),
            json={**body, "fingerprint": other, "replace": True},
        )
        assert replaced.status_code == 200
        assert replaced.get_json()["data"]["fingerprintSha256"] == other
        entry = AuditLog.query.filter_by(action="ssh_host_key_replaced").one()
        assert entry.details["previousFingerprint"] == KEY_OPENSSH

    def test_tofu_record_can_be_upgraded_and_deleted(self, client, admin_token, scan_transport):
        from api.models import AuditLog, SshHostKey
        from api.services.ssh.hostkeys import verify_host_key

        ok, _ = verify_host_key(
            host="10.0.0.13", port=22, key_type="ssh-ed25519",
            fingerprint_sha256=KEY_HEX, policy="tofu",
        )
        assert ok
        assert SshHostKey.query.one().source == "tofu"

        pin = client.post(
            "/api/ssh-host-keys", headers=auth_headers(admin_token),
            json={"host": "10.0.0.13", "keyType": "ssh-ed25519", "fingerprint": KEY_HEX},
        )
        assert pin.status_code == 200  # existing row upgraded in place
        row = SshHostKey.query.one()
        assert row.source == "preapproved"

        delete = client.delete(f"/api/ssh-host-keys/{row.id}", headers=auth_headers(admin_token))
        assert delete.status_code == 200
        assert SshHostKey.query.count() == 0
        assert AuditLog.query.filter_by(action="ssh_host_key_deleted").count() == 1
        assert client.delete(
            f"/api/ssh-host-keys/{row.id}", headers=auth_headers(admin_token)
        ).status_code == 404

    def test_bad_input_is_400(self, client, admin_token, scan_transport):
        bad = client.post(
            "/api/ssh-host-keys", headers=auth_headers(admin_token),
            json={"host": "10.0.0.14", "keyType": "ssh-ed25519", "fingerprint": "nope"},
        )
        assert bad.status_code == 400
        bad_host = client.post(
            "/api/ssh-host-keys/scan", headers=auth_headers(admin_token), json={"host": "; rm -rf /"},
        )
        assert bad_host.status_code == 400

    def test_requires_ssh_credentials_manage(self, client, viewer_token, scan_transport):
        for method, url in (
            ("get", "/api/ssh-host-keys"),
            ("post", "/api/ssh-host-keys"),
            ("post", "/api/ssh-host-keys/scan"),
            ("delete", "/api/ssh-host-keys/1"),
        ):
            response = getattr(client, method)(url, headers=auth_headers(viewer_token), json={})
            assert response.status_code == 403, (method, url)
