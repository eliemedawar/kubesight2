"""Day-two operations on a finished cluster build.

Covers the three things a completed build can now do: hand over its kubeconfig,
accept new worker machines, and run the phase machine again to join them.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from api.db import db
from api.models import Cluster, ClusterBuild, ClusterBuildNode, ClusterBuildStep
from api.services.cluster_build import service as svc
from api.services.ssh import set_transport_factory

from tests.test_cluster_builds import (
    SINGLE_CP_NODES,
    auth_headers,
    build_default_fake,
    make_build_payload,
    run_full_build,
)
# Fixtures live beside the build tests; re-export them so pytest resolves them
# here too (noqa: they are used by name, not by reference).
from tests.test_cluster_builds import (  # noqa: F401
    fake_ssh,
    no_network_cni_manifest,
    ssh_profile,
)

GROWN_HOSTS = {
    "10.0.0.11": ("cp-1", "control_plane"),
    "10.0.0.21": ("w-1", "worker"),
    "10.0.0.22": ("w-2", "worker"),
}
NEW_WORKER = {"role": "worker", "hostname": "w-2", "address": "10.0.0.22"}


@pytest.fixture()
def finished_build(client, admin_token, ssh_profile, fake_ssh, app):
    """A completed single-CP build with a registered cluster."""
    fake = build_default_fake(GROWN_HOSTS)
    # Join secrets are destroyed at completion, so growth always re-mints them.
    # The original build never needs this responder; every growth run does.
    fake.add(
        lambda h, s: "kubeadm token create --print-join-command" in s,
        "kubeadm join 10.0.0.100:6443 --token abcdef.0123456789abcdef "
        "--discovery-token-ca-cert-hash "
        "sha256:1111111111111111111111111111111111111111111111111111111111111111\n",
    )
    set_transport_factory(lambda: fake)
    data = run_full_build(
        client, admin_token, ssh_profile, fake,
        make_build_payload(nodes=SINGLE_CP_NODES),
    )
    assert data["status"] == "completed", data.get("error")
    return {"build": data, "fake": fake}


# ---------------------------------------------------------------------------
# Kubeconfig
# ---------------------------------------------------------------------------

class TestKubeconfigDownload:
    def test_returns_the_stored_kubeconfig(self, client, admin_token, finished_build):
        build = finished_build["build"]
        response = client.get(
            f"/api/cluster-builds/{build['id']}/kubeconfig",
            headers=auth_headers(admin_token),
        )
        assert response.status_code == 200, response.get_json()
        data = response.get_json()["data"]
        assert data["filename"] == "demo-kubeconfig.yaml"
        assert "apiVersion" in data["content"]
        assert data["clusterId"] == build["resultClusterId"]

    def test_filename_cannot_escape_via_the_build_name(
        self, client, admin_token, finished_build, app
    ):
        build = ClusterBuild.query.get(finished_build["build"]["id"])
        build.name = "../../etc/pa sswd"
        db.session.commit()
        data = client.get(
            f"/api/cluster-builds/{build.id}/kubeconfig",
            headers=auth_headers(admin_token),
        ).get_json()["data"]
        assert data["filename"] == "etc-pa-sswd-kubeconfig.yaml"
        assert "/" not in data["filename"]
        assert ".." not in data["filename"]

    def test_needs_its_own_permission(self, client, viewer_token, finished_build):
        # cluster_builds:view is not enough — this is cluster-admin credentials.
        response = client.get(
            f"/api/cluster-builds/{finished_build['build']['id']}/kubeconfig",
            headers=auth_headers(viewer_token),
        )
        assert response.status_code == 403

    def test_download_is_audited(self, client, admin_token, finished_build, app):
        from api.models import AuditLog

        client.get(
            f"/api/cluster-builds/{finished_build['build']['id']}/kubeconfig",
            headers=auth_headers(admin_token),
        )
        entry = (
            AuditLog.query.filter_by(action="cluster_build_kubeconfig_downloaded")
            .order_by(AuditLog.id.desc()).first()
        )
        assert entry is not None
        assert entry.target_id == str(finished_build["build"]["id"])

    def test_refused_when_no_cluster_was_produced(
        self, client, admin_token, ssh_profile
    ):
        payload = make_build_payload(nodes=SINGLE_CP_NODES)
        payload["connectionProfileId"] = ssh_profile["id"]
        draft = client.post(
            "/api/cluster-builds", json=payload, headers=auth_headers(admin_token)
        ).get_json()["data"]
        response = client.get(
            f"/api/cluster-builds/{draft['id']}/kubeconfig",
            headers=auth_headers(admin_token),
        )
        assert response.status_code == 400
        assert "has not produced a cluster" in response.get_json()["error"]

    def test_404_when_the_cluster_row_is_gone(
        self, client, admin_token, finished_build, app
    ):
        Cluster.query.delete()
        db.session.commit()
        response = client.get(
            f"/api/cluster-builds/{finished_build['build']['id']}/kubeconfig",
            headers=auth_headers(admin_token),
        )
        assert response.status_code == 404


# ---------------------------------------------------------------------------
# Adding machines
# ---------------------------------------------------------------------------

class TestAddWorkerNodes:
    def test_attaches_a_pending_worker(self, client, admin_token, finished_build):
        build = finished_build["build"]
        response = client.post(
            f"/api/cluster-builds/{build['id']}/nodes",
            json={"nodes": [NEW_WORKER]}, headers=auth_headers(admin_token),
        )
        assert response.status_code == 201, response.get_json()
        data = response.get_json()["data"]
        assert data["pendingNodeCount"] == 1
        assert data["canGrow"] is True
        added = next(n for n in data["nodes"] if n["address"] == "10.0.0.22")
        assert added["status"] == "pending"
        assert added["role"] == "worker"
        # The machines already serving the cluster are untouched.
        assert [n["status"] for n in data["nodes"] if n["address"] != "10.0.0.22"] == \
            ["joined", "joined"]

    def test_refuses_a_control_plane_with_a_reason(
        self, client, admin_token, finished_build
    ):
        response = client.post(
            f"/api/cluster-builds/{finished_build['build']['id']}/nodes",
            json={"nodes": [{"role": "control_plane", "hostname": "cp-2",
                             "address": "10.0.0.12"}]},
            headers=auth_headers(admin_token),
        )
        assert response.status_code == 400
        error = response.get_json()["error"]
        assert "Only workers" in error
        assert "etcd quorum" in error

    def test_refuses_a_load_balancer(self, client, admin_token, finished_build):
        response = client.post(
            f"/api/cluster-builds/{finished_build['build']['id']}/nodes",
            json={"nodes": [{"role": "loadbalancer", "hostname": "lb-9",
                             "address": "10.0.0.9"}]},
            headers=auth_headers(admin_token),
        )
        assert response.status_code == 400

    def test_refuses_a_machine_already_in_the_cluster(
        self, client, admin_token, finished_build
    ):
        response = client.post(
            f"/api/cluster-builds/{finished_build['build']['id']}/nodes",
            json={"nodes": [{"role": "worker", "hostname": "w-1",
                             "address": "10.0.0.21"}]},
            headers=auth_headers(admin_token),
        )
        assert response.status_code == 400
        assert "already part of this cluster" in response.get_json()["error"]

    def test_refuses_duplicates_within_one_request(
        self, client, admin_token, finished_build
    ):
        response = client.post(
            f"/api/cluster-builds/{finished_build['build']['id']}/nodes",
            json={"nodes": [NEW_WORKER, dict(NEW_WORKER)]},
            headers=auth_headers(admin_token),
        )
        assert response.status_code == 400

    def test_refuses_an_unfinished_build(self, client, admin_token, ssh_profile):
        payload = make_build_payload(nodes=SINGLE_CP_NODES)
        payload["connectionProfileId"] = ssh_profile["id"]
        draft = client.post(
            "/api/cluster-builds", json=payload, headers=auth_headers(admin_token)
        ).get_json()["data"]
        response = client.post(
            f"/api/cluster-builds/{draft['id']}/nodes",
            json={"nodes": [NEW_WORKER]}, headers=auth_headers(admin_token),
        )
        assert response.status_code == 400
        assert "Only a completed build" in response.get_json()["error"]

    def test_refuses_an_empty_selection(self, client, admin_token, finished_build):
        response = client.post(
            f"/api/cluster-builds/{finished_build['build']['id']}/nodes",
            json={"nodes": []}, headers=auth_headers(admin_token),
        )
        assert response.status_code == 400

    def test_a_pending_machine_can_be_removed_again(
        self, client, admin_token, finished_build
    ):
        build_id = finished_build["build"]["id"]
        added = client.post(
            f"/api/cluster-builds/{build_id}/nodes",
            json={"nodes": [NEW_WORKER]}, headers=auth_headers(admin_token),
        ).get_json()["data"]
        node_id = next(n["id"] for n in added["nodes"] if n["address"] == "10.0.0.22")
        response = client.delete(
            f"/api/cluster-builds/{build_id}/nodes/{node_id}",
            headers=auth_headers(admin_token),
        )
        assert response.status_code == 200
        assert response.get_json()["data"]["pendingNodeCount"] == 0

    def test_a_joined_machine_cannot_be_removed_this_way(
        self, client, admin_token, finished_build
    ):
        build = finished_build["build"]
        joined_id = next(n["id"] for n in build["nodes"] if n["address"] == "10.0.0.21")
        response = client.delete(
            f"/api/cluster-builds/{build['id']}/nodes/{joined_id}",
            headers=auth_headers(admin_token),
        )
        assert response.status_code == 400
        assert "has not joined yet" in response.get_json()["error"]


# ---------------------------------------------------------------------------
# Preflighting and joining
# ---------------------------------------------------------------------------

def _add_worker(client, token, build_id, node=None):
    return client.post(
        f"/api/cluster-builds/{build_id}/nodes",
        json={"nodes": [node or NEW_WORKER]}, headers=auth_headers(token),
    ).get_json()["data"]


class TestGrowthPreflight:
    def test_probes_only_the_new_machine(self, client, admin_token, finished_build):
        build_id = finished_build["build"]["id"]
        fake = finished_build["fake"]
        _add_worker(client, admin_token, build_id)

        fake.calls.clear()
        response = client.post(
            f"/api/cluster-builds/{build_id}/grow-preflight",
            headers=auth_headers(admin_token),
        )
        assert response.status_code == 200, response.get_json()
        result = response.get_json()["data"]
        assert [n["address"] for n in result["nodes"]] == ["10.0.0.22"]

        # A running control plane legitimately holds :6443 — probing it would
        # report a port clash against the cluster it is already serving.
        probed = {host for host, script in fake.calls if "preflight probe" in script}
        assert probed == {"10.0.0.22"}

    def test_leaves_joined_machines_joined(self, client, admin_token, finished_build):
        build_id = finished_build["build"]["id"]
        _add_worker(client, admin_token, build_id)
        client.post(f"/api/cluster-builds/{build_id}/grow-preflight",
                    headers=auth_headers(admin_token))
        data = client.get(f"/api/cluster-builds/{build_id}",
                          headers=auth_headers(admin_token)).get_json()["data"]
        by_address = {n["address"]: n["status"] for n in data["nodes"]}
        assert by_address["10.0.0.11"] == "joined"
        assert by_address["10.0.0.21"] == "joined"
        assert by_address["10.0.0.22"] == "preflight_passed"

    def test_the_cluster_is_never_left_mid_preflight(
        self, client, admin_token, finished_build
    ):
        """A live cluster is not 'preflighting' — the status stays completed."""
        build_id = finished_build["build"]["id"]
        _add_worker(client, admin_token, build_id)
        client.post(f"/api/cluster-builds/{build_id}/grow-preflight",
                    headers=auth_headers(admin_token))
        data = client.get(f"/api/cluster-builds/{build_id}",
                          headers=auth_headers(admin_token)).get_json()["data"]
        assert data["status"] == "completed"

    def test_refused_without_a_pending_machine(
        self, client, admin_token, finished_build
    ):
        response = client.post(
            f"/api/cluster-builds/{finished_build['build']['id']}/grow-preflight",
            headers=auth_headers(admin_token),
        )
        assert response.status_code == 400
        assert "Add machines" in response.get_json()["error"]


class TestGrowBuild:
    def test_prepares_and_joins_only_the_new_machine(
        self, client, admin_token, finished_build, app
    ):
        build_id = finished_build["build"]["id"]
        fake = finished_build["fake"]
        _add_worker(client, admin_token, build_id)
        client.post(f"/api/cluster-builds/{build_id}/grow-preflight",
                    headers=auth_headers(admin_token))

        before = client.get(f"/api/cluster-builds/{build_id}",
                            headers=auth_headers(admin_token)).get_json()["data"]
        original_steps = {
            (s["phase"], s["nodeId"]) for s in before["steps"] if s["status"] == "completed"
        }

        fake.calls.clear()
        response = client.post(
            f"/api/cluster-builds/{build_id}/grow",
            json={"ackWarnings": ["ack"]}, headers=auth_headers(admin_token),
        )
        assert response.status_code == 200, response.get_json()
        data = response.get_json()["data"]
        assert data["status"] == "completed", data.get("error")
        assert data["pendingNodeCount"] == 0

        by_address = {n["address"]: n["status"] for n in data["nodes"]}
        assert by_address["10.0.0.22"] == "joined"
        assert by_address["10.0.0.11"] == "joined"

        # kubeadm join ran against the new machine and nothing else.
        joined_hosts = {host for host, script in fake.calls if "kubeadm join" in script}
        assert joined_hosts == {"10.0.0.22"}
        # No re-init, no CNI re-apply.
        assert not any("kubeadm init" in script for _, script in fake.calls)

        # Every step that had already completed is still completed.
        after_steps = {
            (s["phase"], s["nodeId"]) for s in data["steps"] if s["status"] == "completed"
        }
        assert original_steps <= after_steps

    def test_reopens_verification_so_the_cluster_is_rechecked(
        self, client, admin_token, finished_build, app
    ):
        build_id = finished_build["build"]["id"]
        _add_worker(client, admin_token, build_id)
        client.post(f"/api/cluster-builds/{build_id}/grow-preflight",
                    headers=auth_headers(admin_token))
        before = ClusterBuildStep.query.filter_by(
            build_id=build_id, phase="verify"
        ).first()
        first_finished = before.finished_at

        client.post(f"/api/cluster-builds/{build_id}/grow",
                    json={"ackWarnings": ["ack"]}, headers=auth_headers(admin_token))
        after = ClusterBuildStep.query.filter_by(
            build_id=build_id, phase="verify"
        ).first()
        assert after.status == "completed"
        assert after.finished_at != first_finished, "verify should have run again"

    def test_keeps_the_original_build_duration(
        self, client, admin_token, finished_build, app
    ):
        """A growth run rewrites finished_at; "built in 18 min" must survive."""
        build_id = finished_build["build"]["id"]
        row = ClusterBuild.query.get(build_id)
        row.started_at = datetime.now(timezone.utc) - timedelta(minutes=18)
        row.build_seconds = 18 * 60
        db.session.commit()

        _add_worker(client, admin_token, build_id)
        client.post(f"/api/cluster-builds/{build_id}/grow-preflight",
                    headers=auth_headers(admin_token))
        data = client.post(
            f"/api/cluster-builds/{build_id}/grow",
            json={"ackWarnings": ["ack"]}, headers=auth_headers(admin_token),
        ).get_json()["data"]
        assert data["buildSeconds"] == 18 * 60
        assert data["growthStartedAt"] is not None

    def test_banks_the_duration_on_the_first_completion(self, finished_build):
        build = ClusterBuild.query.get(finished_build["build"]["id"])
        assert build.build_seconds is not None
        assert build.build_seconds >= 0

    def test_refused_before_preflight(self, client, admin_token, finished_build):
        build_id = finished_build["build"]["id"]
        _add_worker(client, admin_token, build_id)
        response = client.post(
            f"/api/cluster-builds/{build_id}/grow",
            json={}, headers=auth_headers(admin_token),
        )
        assert response.status_code == 400
        assert "Run preflight" in response.get_json()["error"]

    def test_warnings_must_be_acknowledged(
        self, client, admin_token, finished_build, app
    ):
        build_id = finished_build["build"]["id"]
        _add_worker(client, admin_token, build_id)
        client.post(f"/api/cluster-builds/{build_id}/grow-preflight",
                    headers=auth_headers(admin_token))
        node = ClusterBuildNode.query.filter_by(
            build_id=build_id, address="10.0.0.22"
        ).first()
        node.preflight_json = {"status": "warn", "checks": []}
        db.session.commit()

        response = client.post(
            f"/api/cluster-builds/{build_id}/grow",
            json={}, headers=auth_headers(admin_token),
        )
        assert response.status_code == 400
        assert "acknowledge" in response.get_json()["error"]

    def test_refused_when_preflight_failed(
        self, client, admin_token, finished_build, app
    ):
        build_id = finished_build["build"]["id"]
        _add_worker(client, admin_token, build_id)
        client.post(f"/api/cluster-builds/{build_id}/grow-preflight",
                    headers=auth_headers(admin_token))
        node = ClusterBuildNode.query.filter_by(
            build_id=build_id, address="10.0.0.22"
        ).first()
        node.status = "preflight_failed"
        db.session.commit()

        response = client.post(
            f"/api/cluster-builds/{build_id}/grow",
            json={"ackWarnings": ["ack"]}, headers=auth_headers(admin_token),
        )
        assert response.status_code == 400
        assert "Preflight failed on" in response.get_json()["error"]

    def test_growth_needs_execute_permission(
        self, client, admin_token, viewer_token, finished_build
    ):
        build_id = finished_build["build"]["id"]
        _add_worker(client, admin_token, build_id)
        assert client.post(
            f"/api/cluster-builds/{build_id}/grow-preflight",
            headers=auth_headers(viewer_token),
        ).status_code == 403
        assert client.post(
            f"/api/cluster-builds/{build_id}/grow",
            json={}, headers=auth_headers(viewer_token),
        ).status_code == 403
