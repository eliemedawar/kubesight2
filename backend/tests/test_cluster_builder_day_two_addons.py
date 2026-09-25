"""Day two: installing add-ons on a cluster the Cluster Builder already built.

The wizard's catalog and add-ons phase, reopened — so these tests are about
what day two changes: only the new add-ons are applied, a cluster built
without Metrics Server gets kubelet serving certificates switched on, a
failed install can be withdrawn, and growth keeps the CSR approver current.
"""

from __future__ import annotations

import base64
import re

import pytest

from api.db import db
from api.models import ClusterBuild, ClusterBuildStep
from api.services.ssh import SshCommandError, set_transport_factory

from tests.test_cluster_builds import (
    SINGLE_CP_NODES,
    auth_headers,
    build_default_fake,
    make_build_payload,
    run_full_build,
)
# Fixtures live beside the add-on tests; re-exported so pytest resolves them
# here too (noqa: they are used by name, not by reference).
from tests.test_cluster_builder_addons_proxy import (  # noqa: F401
    METALLB_SELECTION,
    add_addon_responders,
    pinned_manifests_without_network,
    reset_ssh_transport,
    ssh_profile,
)

HOSTS = {
    "10.0.0.11": ("cp-1", "control_plane"),
    "10.0.0.21": ("w-1", "worker"),
    "10.0.0.22": ("w-2", "worker"),
}


def _scripts(fake, since=0):
    return [script for _, script in fake.calls[since:]]


def _uploaded(script):
    match = re.search(r"echo ([A-Za-z0-9+/=]+) \| base64 -d", script)
    return base64.b64decode(match.group(1)).decode("utf-8") if match else ""


@pytest.fixture()
def built(client, admin_token, ssh_profile, app):
    """A completed single-CP build that went up with NGINX Ingress only."""
    fake = add_addon_responders(build_default_fake(HOSTS))
    fake.add(
        lambda h, s: "kubeadm token create --print-join-command" in s,
        "kubeadm join 10.0.0.100:6443 --token abcdef.0123456789abcdef "
        "--discovery-token-ca-cert-hash "
        "sha256:1111111111111111111111111111111111111111111111111111111111111111\n",
    )
    set_transport_factory(lambda: fake)
    data = run_full_build(
        client, admin_token, ssh_profile, fake,
        make_build_payload(
            nodes=SINGLE_CP_NODES,
            addons=[{"id": "nginx-ingress", "version": "5.5.4"}],
        ),
    )
    assert data["status"] == "completed", data.get("error")
    return {"build": data, "fake": fake}


def _add(client, token, build_id, addons):
    return client.post(
        f"/api/cluster-builds/{build_id}/addons",
        json={"addons": addons}, headers=auth_headers(token),
    )


class TestAddAddons:
    def test_installs_only_the_new_add_on(self, client, admin_token, built):
        build, fake = built["build"], built["fake"]
        assert build["addons"][0]["installedAt"]
        mark = len(fake.calls)

        response = _add(client, admin_token, build["id"], [dict(METALLB_SELECTION)])
        assert response.status_code == 200, response.get_json()
        data = response.get_json()["data"]
        assert data["status"] == "completed", data.get("error")

        by_id = {item["id"]: item for item in data["addons"]}
        assert set(by_id) == {"nginx-ingress", "metallb"}
        assert by_id["metallb"]["installedAt"]
        assert by_id["metallb"]["config"]["addressPools"] == ["10.0.0.240-10.0.0.250"]

        scripts = _scripts(fake, mark)
        assert any("kubesight-addon-metallb" in s for s in scripts)
        assert any("kubesight-addon-metallb-pool.yaml" in s for s in scripts)
        # What was already there is not re-applied — that would revert any
        # change made on the live cluster since.
        assert not any("kubesight-addon-nginx-ingress" in s for s in scripts)
        # And nothing else in the phase machine ran again.
        assert not any("kubeadm init" in s for s in scripts)
        assert not any("kubeadm join" in s for s in scripts)
        assert not any("preflight probe" in s for s in scripts)

    def test_build_duration_is_not_rewritten(self, client, admin_token, built):
        before = built["build"]["buildSeconds"]
        data = _add(
            client, admin_token, built["build"]["id"], [dict(METALLB_SELECTION)]
        ).get_json()["data"]
        assert data["buildSeconds"] == before
        assert data["growthStartedAt"]

    def test_metrics_server_turns_on_kubelet_serving_certificates(
        self, client, admin_token, built
    ):
        build, fake = built["build"], built["fake"]
        # This cluster was initialised without Metrics Server, so every kubelet
        # reports the change.
        fake.responders.insert(
            0, (lambda h, s: "serverTLSBootstrap" in s and "grep -Eq" in s,
                "KS_CHANGED=1\n"),
        )
        mark = len(fake.calls)
        data = _add(
            client, admin_token, build["id"],
            [{"id": "metrics-server", "version": "0.7.2"}],
        ).get_json()["data"]
        assert data["status"] == "completed", data.get("error")

        calls = fake.calls[mark:]
        enabled_on = {host for host, s in calls if "serverTLSBootstrap: true" in s}
        assert enabled_on == {"10.0.0.11", "10.0.0.21"}
        scripts = [s for _, s in calls]
        upload = next(s for s in scripts if "upload-config kubelet" in s)
        assert "serverTLSBootstrap: true" in _uploaded(upload)
        # The approver goes in before kubelets start asking for certificates.
        approver_at = next(
            i for i, s in enumerate(scripts) if "deployment/kubelet-csr-approver" in s
        )
        enable_at = next(
            i for i, s in enumerate(scripts) if "serverTLSBootstrap: true" in s
        )
        assert approver_at < enable_at
        assert any("top nodes --no-headers" in s for s in scripts)

    def test_kubelet_config_is_not_reuploaded_when_nothing_changed(
        self, client, admin_token, built
    ):
        build, fake = built["build"], built["fake"]
        mark = len(fake.calls)
        _add(
            client, admin_token, build["id"],
            [{"id": "metrics-server", "version": "0.7.2"}],
        )
        assert not any("upload-config kubelet" in s for s in _scripts(fake, mark))

    def test_refuses_an_add_on_already_installed(self, client, admin_token, built):
        response = _add(
            client, admin_token, built["build"]["id"],
            [{"id": "nginx-ingress", "version": "5.5.4"}],
        )
        assert response.status_code == 400
        assert "Already installed" in response.get_json()["error"]

    def test_refuses_a_pool_over_a_node_address(self, client, admin_token, built):
        response = _add(
            client, admin_token, built["build"]["id"],
            [{"id": "metallb", "version": "0.16.1",
              "config": {"addressPools": ["10.0.0.20-10.0.0.30"]}}],
        )
        assert response.status_code == 400
        assert "overlaps" in response.get_json()["error"]

    def test_refuses_an_empty_request(self, client, admin_token, built):
        response = _add(client, admin_token, built["build"]["id"], [])
        assert response.status_code == 400

    def test_refused_on_a_build_that_is_not_finished(
        self, client, admin_token, ssh_profile
    ):
        payload = make_build_payload(nodes=SINGLE_CP_NODES)
        payload["connectionProfileId"] = ssh_profile["id"]
        draft = client.post(
            "/api/cluster-builds", json=payload, headers=auth_headers(admin_token)
        ).get_json()["data"]
        response = _add(client, admin_token, draft["id"], [dict(METALLB_SELECTION)])
        assert response.status_code == 400
        assert "completed" in response.get_json()["error"]

    def test_needs_the_execute_permission(self, client, viewer_token, built):
        response = _add(
            client, viewer_token, built["build"]["id"], [dict(METALLB_SELECTION)]
        )
        assert response.status_code == 403

    def test_is_audited(self, client, admin_token, built, app):
        from api.models import AuditLog

        _add(client, admin_token, built["build"]["id"], [dict(METALLB_SELECTION)])
        entry = (
            AuditLog.query.filter_by(action="cluster_build_addons_added")
            .order_by(AuditLog.id.desc()).first()
        )
        assert entry is not None
        assert entry.target_id == str(built["build"]["id"])

    def test_a_build_finished_before_the_stamp_existed_is_not_reapplied(
        self, client, admin_token, built, app
    ):
        build_row = db.session.get(ClusterBuild, built["build"]["id"])
        build_row.addons_json = [
            {k: v for k, v in item.items() if k != "installedAt"}
            for item in build_row.addons_json
        ]
        db.session.commit()
        fake = built["fake"]
        mark = len(fake.calls)
        data = _add(
            client, admin_token, build_row.id, [dict(METALLB_SELECTION)]
        ).get_json()["data"]
        assert data["status"] == "completed", data.get("error")
        assert not any(
            "kubesight-addon-nginx-ingress" in s for s in _scripts(fake, mark)
        )
        assert all(item.get("installedAt") for item in data["addons"])


class TestFailedInstall:
    @pytest.fixture()
    def failed(self, client, admin_token, built):
        fake = built["fake"]
        fake.responders.insert(0, (
            lambda h, s: "kubesight-addon-metallb-0.yaml" in s,
            SshCommandError("apply failed", 1, "webhook unavailable"),
        ))
        data = _add(
            client, admin_token, built["build"]["id"], [dict(METALLB_SELECTION)]
        ).get_json()["data"]
        assert data["status"] == "failed"
        return data

    def test_the_failed_add_on_is_not_stamped(self, failed):
        by_id = {item["id"]: item for item in failed["addons"]}
        assert not by_id["metallb"].get("installedAt")
        assert by_id["nginx-ingress"]["installedAt"]

    def test_withdrawing_it_returns_the_cluster_to_completed(
        self, client, admin_token, failed
    ):
        response = client.delete(
            f"/api/cluster-builds/{failed['id']}/addons/metallb",
            headers=auth_headers(admin_token),
        )
        assert response.status_code == 200, response.get_json()
        data = response.get_json()["data"]
        assert data["status"] == "completed"
        assert [item["id"] for item in data["addons"]] == ["nginx-ingress"]
        step = next(s for s in data["steps"] if s["phase"] == "addons")
        assert step["status"] == "completed"

    def test_an_installed_add_on_cannot_be_withdrawn(
        self, client, admin_token, failed
    ):
        response = client.delete(
            f"/api/cluster-builds/{failed['id']}/addons/nginx-ingress",
            headers=auth_headers(admin_token),
        )
        assert response.status_code == 400
        assert "installed" in response.get_json()["error"]

    def test_retry_installs_only_what_is_left(
        self, client, admin_token, built, failed
    ):
        fake = built["fake"]
        fake.responders.pop(0)
        mark = len(fake.calls)
        response = client.post(
            f"/api/cluster-builds/{failed['id']}/retry",
            headers=auth_headers(admin_token),
        )
        assert response.status_code == 200, response.get_json()
        data = response.get_json()["data"]
        assert data["status"] == "completed", data.get("error")
        scripts = _scripts(fake, mark)
        assert any("kubesight-addon-metallb-0.yaml" in s for s in scripts)
        assert not any("kubesight-addon-nginx-ingress" in s for s in scripts)


class TestGrowthKeepsTheApproverCurrent:
    def test_new_worker_is_added_to_the_csr_policy(
        self, client, admin_token, built
    ):
        build, fake = built["build"], built["fake"]
        _add(
            client, admin_token, build["id"],
            [{"id": "metrics-server", "version": "0.7.2"}],
        )
        response = client.post(
            f"/api/cluster-builds/{build['id']}/nodes",
            json={"nodes": [{"role": "worker", "hostname": "w-2",
                             "address": "10.0.0.22"}]},
            headers=auth_headers(admin_token),
        )
        assert response.status_code == 201, response.get_json()
        preflight = client.post(
            f"/api/cluster-builds/{build['id']}/grow-preflight",
            headers=auth_headers(admin_token),
        )
        assert preflight.status_code == 200, preflight.get_json()
        mark = len(fake.calls)
        data = client.post(
            f"/api/cluster-builds/{build['id']}/grow",
            json={"ackWarnings": ["ack"]}, headers=auth_headers(admin_token),
        ).get_json()["data"]
        assert data["status"] == "completed", data.get("error")

        scripts = _scripts(fake, mark)
        approver = next(
            _uploaded(s) for s in scripts if "kubesight-addon-csr-approver.yaml" in s
        )
        assert "w-2" in approver and "10.0.0.22" in approver
        # Growth installs nothing new.
        assert not any("kubesight-addon-metrics-server-0.yaml" in s for s in scripts)
        assert not any("kubesight-addon-nginx-ingress" in s for s in scripts)

    def test_growth_without_metrics_server_leaves_add_ons_alone(
        self, client, admin_token, built
    ):
        build = built["build"]
        client.post(
            f"/api/cluster-builds/{build['id']}/nodes",
            json={"nodes": [{"role": "worker", "hostname": "w-2",
                             "address": "10.0.0.22"}]},
            headers=auth_headers(admin_token),
        )
        client.post(
            f"/api/cluster-builds/{build['id']}/grow-preflight",
            headers=auth_headers(admin_token),
        )
        client.post(
            f"/api/cluster-builds/{build['id']}/grow",
            json={"ackWarnings": ["ack"]}, headers=auth_headers(admin_token),
        )
        step = ClusterBuildStep.query.filter_by(
            build_id=build["id"], phase="addons"
        ).first()
        assert step.status == "completed"
        assert "csr-approver" not in (step.log_tail or "")
