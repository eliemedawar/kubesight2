"""Applications asked for on a ticket: app profiles, one approval, Helm installs.

An administrator saves app profiles (an Inventory chart, its version and values,
the namespace and release); Hermes names them — with new VMs and Kubernetes, or
onto a cluster the Cluster Builder built — and never writes values. A person
approves first; a cluster that needs approval for changes asks its own
approvers after that. Hermes gets one follow-up with what was installed.
"""

from __future__ import annotations

import pytest

from api.db import db
from api.models import AppProfile, ChangeBundle, ClusterBuild, DeploymentRequestSetting
from api.services.cluster_build import app_profiles
from api.services.cluster_build import apps as apps_mod
from api.services.ssh import set_transport_factory
from api.services.ticket_agent import engine, settings as agent_settings, vm_requests

from tests.test_cluster_builds import (  # noqa: F401  (fixtures are used by name)
    SINGLE_CP_NODES,
    auth_headers,
    build_default_fake,
    create_build,
    fake_ssh,
    make_build_payload,
    no_network_cni_manifest,
    ssh_profile,
)
from tests.test_cluster_builder_addons_proxy import (  # noqa: F401
    pinned_manifests_without_network,
    reset_ssh_transport,
)
from tests.test_cluster_builder_apps import PASSWORD, _start, chart, helm  # noqa: F401
from tests.test_cluster_provisioning import (  # noqa: F401
    engine as tofu_engine,
    second_admin_token,
    vcenter,
)
from tests.test_ticket_agent import _fake_hermes, _ticket, agent, writes  # noqa: F401
from tests.test_ticket_agent_vms import _ask, _hermes_user, _task, _vm, vm_agent  # noqa: F401


@pytest.fixture(autouse=True)
def _cluster_needs_no_approval(no_cluster_approvals):
    yield


@pytest.fixture()
def shop(app, chart):
    """One app profile: the shop front end with UAT values and its password."""
    return app_profiles.create({
        "name": "Shop front",
        "description": "Shop front end, UAT settings",
        "chartSlug": chart["slug"],
        "valuesFile": chart["uat"],
        "namespace": "shop",
        "releaseName": "web",
        "valuesYaml": f"database:\n  password: {PASSWORD}\n",
    }, created_by="admin")


class TestProfiles:
    def test_values_are_encrypted_masked_and_never_offered_to_hermes(self, client, admin_token, shop, vm_agent):
        row = db.session.get(AppProfile, shop["id"])
        assert PASSWORD not in row.values_cipher
        assert row.chart_version == "1.2.0"  # pinned when saved
        listed = client.get("/api/app-profiles", headers=auth_headers(admin_token)).get_json()["data"]["items"]
        assert apps_mod.MASK in listed[0]["valuesYaml"] and PASSWORD not in str(listed)
        [entry] = vm_requests.app_catalog()
        assert entry["profile"] == "Shop front" and "release web in namespace shop" == entry["installsAs"]
        assert "valuesYaml" not in entry and PASSWORD not in str(entry)

    def test_not_offered_while_vm_requests_are_off(self, shop, agent):
        agent_settings.update({"vmRequestsEnabled": False})
        assert vm_requests.app_catalog() == []

    def test_crud_over_the_api(self, client, admin_token, chart):
        response = client.post("/api/app-profiles", json={
            "name": "Reports", "chartSlug": chart["slug"], "namespace": "reports",
            "valuesYaml": "database:\n  password: x\n",
        }, headers=auth_headers(admin_token))
        assert response.status_code == 201, response.get_json()
        profile = response.get_json()["data"]
        response = client.put(f"/api/app-profiles/{profile['id']}", json={
            "enabled": False, "valuesYaml": profile["valuesYaml"] + "replicaCount: 3\n",
        }, headers=auth_headers(admin_token))
        assert response.status_code == 200, response.get_json()
        row = db.session.get(AppProfile, profile["id"])
        assert not row.enabled
        text = apps_mod.values_yaml_of(row)
        assert "password: x" in text and "replicaCount: 3" in text
        response = client.post("/api/app-profiles", json={
            "name": "reports", "chartSlug": chart["slug"], "namespace": "reports",
        }, headers=auth_headers(admin_token))
        assert response.status_code == 400 and "already exists" in response.get_json()["error"]
        assert client.delete(f"/api/app-profiles/{profile['id']}",
                             headers=auth_headers(admin_token)).status_code == 200

    def test_viewers_cannot_edit(self, client, viewer_token, chart):
        response = client.post("/api/app-profiles", json={"name": "x"}, headers=auth_headers(viewer_token))
        assert response.status_code == 403


class TestWithNewVms:
    HOSTS = {
        "10.20.30.50": ("tm-801-vm-1", "control_plane"),
        "10.20.30.51": ("tm-801-vm-2", "worker"),
    }

    def test_apps_need_kubernetes(self, vm_agent, shop, writes):
        with pytest.raises(engine.AgentError, match="apps need a Kubernetes cluster"):
            _ask(_ticket(subject="VMs and the shop"), apps=["Shop front"])

    def test_unknown_app_is_refused(self, vm_agent, shop, writes):
        vm_requests.update_profile(vm_agent["id"], {"allowKubernetes": True})
        with pytest.raises(engine.AgentError, match="no app profile called 'CRM'"):
            _ask(_ticket(subject="cluster"), name="tm-802", count=None,
                 kubernetes={"shape": "lab"}, apps=["CRM"])

    def test_one_approval_covers_vms_kubernetes_and_apps(
        self, client, app, vm_agent, shop, writes, monkeypatch, fake_ssh, helm,
    ):
        vm_requests.update_profile(vm_agent["id"], {"allowKubernetes": True})
        ticket = _ticket(subject="A lab cluster with the shop on it")
        _ask(ticket, name="tm-801", count=None, kubernetes={"shape": "lab"}, apps=["Shop front"])
        task = _task(ticket)
        vm = _vm(task)
        [described] = vm["apps"]
        assert described["release"] == "web" and described["valueKeys"] == ["database"]
        text = vm_requests.approval_text(task, ticket)
        assert "Then these apps, with Helm:" in text and "web → shop" in text
        assert PASSWORD not in text
        build = db.session.get(ClusterBuild, vm["buildId"])
        assert [a.release_name for a in build.apps] == ["web"]
        assert build.apps[0].profile_id == shop["id"]

        set_transport_factory(lambda: build_default_fake({**self.HOSTS}))
        other = second_admin_token(client, app)
        response = client.post(f"/api/ticket-agent/tasks/{task.id}/approve", headers=auth_headers(other))
        assert response.status_code == 200, response.get_json()

        followups = []
        _fake_hermes(monkeypatch, lambda message: followups.append(message) and None)
        engine.tick()  # vms_ready → Kubernetes and then the apps (synchronously under TESTING)
        engine.tick()  # completed → one follow-up
        vm = _vm(task)
        build = db.session.get(ClusterBuild, vm["buildId"])
        assert build.status == "completed", (build.error, vm)
        payload, context = helm["install"][-1]
        assert payload["releaseName"] == "web" and PASSWORD in payload["valuesYaml"]
        assert context == f"cluster_build:{build.id}"
        event = followups[-1]["event"]
        assert event["result"] == "cluster_ready"
        assert event["apps"] == [{"release": "web", "namespace": "shop", "status": "installed", "error": None}]

    def test_reject_throws_the_apps_away_with_the_draft(self, client, app, vm_agent, shop, writes):
        vm_requests.update_profile(vm_agent["id"], {"allowKubernetes": True})
        ticket = _ticket(subject="cluster + shop")
        _ask(ticket, name="tm-803", count=None, kubernetes={"shape": "lab"}, apps=["Shop front"])
        task = _task(ticket)
        other = second_admin_token(client, app)
        response = client.post(f"/api/ticket-agent/tasks/{task.id}/reject", json={"note": "no"},
                               headers=auth_headers(other))
        assert response.status_code == 200, response.get_json()
        assert ClusterBuild.query.filter_by(name="tm-803").first() is None


@pytest.fixture()
def built(client, admin_token, ssh_profile, helm, agent):
    """A finished cluster the Cluster Builder built, with VM/app requests on."""
    hosts = {"10.0.0.11": ("cp-1", "control_plane"), "10.0.0.21": ("w-1", "worker")}
    set_transport_factory(lambda: build_default_fake(hosts))
    build = create_build(client, admin_token, ssh_profile,
                         make_build_payload(name="shop-cluster", nodes=SINGLE_CP_NODES))
    data = _start(client, admin_token, build["id"])
    assert data["status"] == "completed", data.get("error")
    agent_settings.update({"vmRequestsEnabled": True})
    return data


def _ask_apps(ticket, **overrides):
    arguments = {
        "cluster": "shop-cluster",
        "apps": ["Shop front"],
        "confidence": "High",
        "understanding": "Install the shop front end on shop-cluster.",
        "comment": "Understood: the shop front end on shop-cluster. Waiting for approval.",
        "commentOnApprove": "Approved — installing it now.",
    }
    arguments.update(overrides)
    return vm_requests.request_apps(ticket.id, arguments, user=_hermes_user())


class TestOnAnExistingCluster:
    def test_approval_then_install_then_one_followup(
        self, client, app, built, shop, writes, monkeypatch, helm,
    ):
        ticket = _ticket(subject="Shop front on shop-cluster")
        result = _ask_apps(ticket)
        assert result["cluster"] == built["resultClusterId"] and result["apps"] == ["web in shop"]
        task = _task(ticket)
        assert task.status == "awaiting_approval"
        text = vm_requests.approval_text(task, ticket)
        assert "asks to install apps" in text and "web → shop" in text and PASSWORD not in text
        assert not helm["install"]  # nothing before the approval

        other = second_admin_token(client, app)
        response = client.post(f"/api/ticket-agent/tasks/{task.id}/approve", headers=auth_headers(other))
        assert response.status_code == 200, response.get_json()
        assert any(w.get("comment") == "Approved — installing it now." for w in writes)
        payload, _ = helm["install"][-1]
        assert payload["clusterId"] == built["resultClusterId"] and PASSWORD in payload["valuesYaml"]

        followups = []
        _fake_hermes(monkeypatch, lambda message: followups.append(message) and None)
        engine.tick()
        vm = _vm(task)
        assert vm["phase"] == "apps_ready"
        event = followups[-1]["event"]
        assert event["result"] == "apps_ready"
        assert event["apps"][0]["status"] == "installed"

    def test_a_cluster_that_needs_approval_asks_its_own_approvers_next(
        self, client, app, built, shop, writes, monkeypatch,
    ):
        cluster_id = built["resultClusterId"]
        row = DeploymentRequestSetting.query.first()
        row.cluster_required_approvals = {cluster_id: 1}
        db.session.commit()
        from api.services import helm_service

        monkeypatch.setattr(helm_service, "ensure_helm_installed", lambda: None)
        monkeypatch.setattr(helm_service, "release_exists", lambda *a, **k: False)
        monkeypatch.setattr(helm_service, "run_helm", lambda access, args: "")

        ticket = _ticket(subject="Shop front please")
        _ask_apps(ticket)
        task = _task(ticket)
        other = second_admin_token(client, app)
        response = client.post(f"/api/ticket-agent/tasks/{task.id}/approve", headers=auth_headers(other))
        assert response.status_code == 200, response.get_json()
        vm = _vm(task)
        assert vm["phase"] == "waiting_cluster_approval" and vm["bundleIds"]

        bundle = db.session.get(ChangeBundle, vm["bundleIds"][0])
        assert PASSWORD not in str(bundle.items[0].new_payload_json)
        bundle.status, bundle.items[0].status = "completed", "succeeded"
        db.session.commit()
        followups = []
        _fake_hermes(monkeypatch, lambda message: followups.append(message) and None)
        engine.tick()  # the build's charts take the bundle's outcome, then the request finishes
        engine.tick()
        assert _vm(task)["phase"] == "apps_ready"
        assert followups[-1]["event"]["result"] == "apps_ready"

    def test_reject_leaves_the_cluster_alone(self, client, app, built, shop, writes):
        ticket = _ticket(subject="Shop front")
        _ask_apps(ticket)
        task = _task(ticket)
        other = second_admin_token(client, app)
        client.post(f"/api/ticket-agent/tasks/{task.id}/reject", headers=auth_headers(other))
        build = db.session.get(ClusterBuild, built["id"])
        assert build is not None and build.status == "completed" and build.apps == []

    @pytest.mark.parametrize("overrides,message", [
        ({"cluster": "nope"}, "no cluster built by the Cluster Builder called 'nope'"),
        ({"apps": ["CRM"]}, "no app profile called 'CRM'"),
        ({"apps": []}, "apps is required"),
    ])
    def test_refusals(self, built, shop, writes, overrides, message):
        with pytest.raises(engine.AgentError, match=message):
            _ask_apps(_ticket(subject="x"), **overrides)

    def test_hermes_reaches_it_through_mcp(self, client, admin_token, built, shop, writes):
        ticket = _ticket(subject="Shop front")
        response = client.post("/api/mcp", json={
            "jsonrpc": "2.0", "id": 1, "method": "tools/call",
            "params": {"name": "kubesight_ticket_install_apps", "arguments": {
                "ticketRecordId": ticket.id, "cluster": "shop-cluster", "apps": ["Shop front"],
                "confidence": "High", "understanding": "The shop.", "comment": "Waiting for approval.",
                "commentOnApprove": "Approved.",
            }},
        }, headers=auth_headers(admin_token))
        result = response.get_json()["result"]
        assert not result.get("isError"), result
        assert "nothing installed yet" in result["structuredContent"]["changed"]
        brief = engine.brief(ticket.id)
        assert brief["appProfiles"][0]["profile"] == "Shop front"
