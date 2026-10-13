"""VMs asked for on a ticket: Hermes asks, KubeSight plans, a VM approver approves.

The whole road runs for real — the ticket agent's tools, a VMs-only build made
from a VM request profile, the OpenTofu job engine (simulated OpenTofu, fake
vCenter inventory, an SSH wait that answers), the approval state machine, the
scheduler's advance, and the follow-up Hermes gets — with Hermes faked the way
test_ticket_agent fakes it and ticket write-back captured at the seam.
"""

from __future__ import annotations

import pytest

from api.db import db
from api.models import (
    ClusterBuild,
    TicketInterpretation,
    User,
    VmRequestProfile,
    VSphereIpReservation,
)
from api.services.cluster_build.provisioning import state_store
from api.services.ssh import set_transport_factory
from api.services.ticket_agent import engine, settings as agent_settings, vm_requests

from tests.test_cluster_builds import (  # noqa: F401  (fixtures are used by name)
    auth_headers,
    build_default_fake,
    fake_ssh,
    no_network_cni_manifest,
    ssh_profile,
)
from tests.test_cluster_provisioning import (  # noqa: F401
    engine as tofu_engine,
    placement_payload,
    second_admin_token,
    vcenter,
)
from tests.test_ticket_agent import _fake_hermes, _ticket, agent, writes  # noqa: F401


@pytest.fixture(autouse=True)
def _cluster_needs_no_approval(no_cluster_approvals):
    yield


def _placement(connection_id, **overrides):
    spec = placement_payload(connection_id, **overrides)
    for key in ("counts", "sizes"):
        spec.pop(key, None)
    return spec


@pytest.fixture()
def vm_agent(agent, vcenter, tofu_engine, ssh_profile):
    """The agent with VM requests on, and one profile: 'DR test VMs'."""
    agent_settings.update({"vmRequestsEnabled": True})
    profile = vm_requests.create_profile({
        "name": "DR test VMs",
        "description": "Test VMs in the DR site, RHEL 8",
        "placement": _placement(vcenter["connection"].id),
        "connectionProfileId": ssh_profile["id"],
        "defaultSize": {"cpu": 2, "memoryGb": 4, "diskGb": 40},
        "maxSize": {"cpu": 8, "memoryGb": 16, "diskGb": 200},
        "maxVms": 3,
    }, created_by="admin")
    return profile


def _hermes_user():
    return User.query.filter_by(username="admin").first()


def _ask(ticket, **overrides):
    arguments = {
        "profile": "DR test VMs",
        "name": "tm-501",
        "count": 2,
        "size": {"cpu": 4, "memoryGb": 8},
        "confidence": "High",
        "understanding": "Two RHEL VMs in DR, 4 vCPU / 8 GB each.",
        "comment": "Understood: two VMs in DR, 4 vCPU and 8 GB. Waiting for DevOps approval.",
        "commentOnApprove": "Approved — the VMs are being created now.",
    }
    arguments.update(overrides)
    return vm_requests.request(ticket.id, arguments, user=_hermes_user())


def _task(ticket):
    return (
        TicketInterpretation.query.filter_by(ticket_record_id=ticket.id, route=vm_requests.ROUTE)
        .order_by(TicketInterpretation.id.desc()).first()
    )


def _vm(task):
    db.session.refresh(task)
    return (task.decision or {}).get("vmRequest") or {}


# ---------------------------------------------------------------------------
# Profiles
# ---------------------------------------------------------------------------

class TestProfiles:
    def test_create_list_and_catalog(self, client, admin_token, vm_agent, vcenter, ssh_profile):
        listed = client.get("/api/vm-request-profiles", headers=auth_headers(admin_token)).get_json()["data"]
        assert listed["vmRequestsEnabled"] is True
        [row] = listed["items"]
        assert row["name"] == "DR test VMs" and row["maxVms"] == 3
        assert row["placement"]["datastoreName"] == "ds-ssd-02"
        assert "counts" not in row["placement"] and "machines" not in row["placement"]
        assert "vcsa-01" in row["where"] and row["connectionProfileName"] == "default"
        [entry] = vm_requests.catalog()
        assert entry["profile"] == "DR test VMs" and entry["maxVms"] == 3
        assert "at most 8 vCPU" in entry["sizes"] and entry["kubernetes"] is False

    def test_catalog_is_empty_while_vm_requests_are_off(self, vm_agent):
        agent_settings.update({"vmRequestsEnabled": False})
        assert vm_requests.catalog() == []

    def test_disabled_profile_is_not_offered(self, client, admin_token, vm_agent):
        response = client.put(f"/api/vm-request-profiles/{vm_agent['id']}", json={"enabled": False},
                              headers=auth_headers(admin_token))
        assert response.status_code == 200, response.get_json()
        assert vm_requests.catalog() == []

    @pytest.mark.parametrize("change,message", [
        ({"defaultSize": {"cpu": 16, "memoryGb": 4, "diskGb": 40}}, "default size cannot be larger"),
        ({"maxVms": 40}, "maxVms must be between"),
        ({"allowKubernetes": True, "defaultSize": {"cpu": 1, "memoryGb": 2, "diskGb": 20},
          "maxSize": {"cpu": 1, "memoryGb": 2, "diskGb": 20}}, "control plane"),
        ({"connectionProfileId": 999}, "not found"),
        ({"placement": {"vsphereConnectionId": 999}}, "vCenter connection not found"),
        ({"name": "dr test vms"}, "already exists"),
    ])
    def test_bad_profiles_are_refused(self, client, admin_token, vm_agent, vcenter, ssh_profile, change, message):
        payload = {
            "name": "Another", "placement": _placement(vcenter["connection"].id),
            "connectionProfileId": ssh_profile["id"], **change,
        }
        response = client.post("/api/vm-request-profiles", json=payload, headers=auth_headers(admin_token))
        assert response.status_code == 400 and message in response.get_json()["error"], response.get_json()

    def test_only_vsphere_managers_edit_profiles(self, client, viewer_token, vm_agent):
        response = client.post("/api/vm-request-profiles", json={"name": "x"}, headers=auth_headers(viewer_token))
        assert response.status_code == 403

    def test_template_sized_profile_has_fixed_sizes(self, vm_agent, vcenter, ssh_profile):
        data = vm_requests.create_profile({
            "name": "Template size",
            "placement": _placement(vcenter["connection"].id, sizeMode="template"),
            "connectionProfileId": ssh_profile["id"],
        })
        assert data["defaultSize"] == data["maxSize"]
        entry = next(e for e in vm_requests.catalog() if e["profile"] == "Template size")
        assert entry["sizes"].startswith("fixed")


# ---------------------------------------------------------------------------
# The request: refused when it does not fit
# ---------------------------------------------------------------------------

class TestRefusals:
    @pytest.mark.parametrize("overrides,message", [
        ({"profile": "Prod"}, "no VM request profile called 'Prod'"),
        ({"count": 5}, "at most 3 VMs"),
        ({"size": {"cpu": 32}}, "at most 8 vCPU"),
        ({"kubernetes": {"shape": "lab"}}, "plain VMs only"),
        ({"name": "Not A Name"}, "name:"),
        ({"count": 0}, "at least 1"),
    ])
    def test_out_of_bounds(self, vm_agent, writes, overrides, message):
        ticket = _ticket(subject="Need VMs")
        with pytest.raises(engine.AgentError, match=message):
            _ask(ticket, **overrides)
        assert ClusterBuild.query.count() == 0 and _task(ticket) is None
        assert writes == []

    def test_off_means_impediment(self, vm_agent, writes):
        agent_settings.update({"vmRequestsEnabled": False})
        with pytest.raises(engine.AgentError, match="switched off"):
            _ask(_ticket(subject="Need VMs"))

    def test_a_name_in_use_is_refused(self, vm_agent, writes):
        _ask(_ticket(subject="first"))
        with pytest.raises(engine.AgentError, match="already exists"):
            _ask(_ticket(subject="second"))

    @pytest.mark.parametrize("name", [None, "", "dr-7001", "DR-7001"])
    def test_the_ticket_must_name_the_vms(self, vm_agent, writes, name):
        """No name, or the ticket's own number: Hermes asks the requester instead."""
        ticket = _ticket(subject="Need VMs")  # ticket number DR-7001
        with pytest.raises(engine.AgentError, match="ask the requester for a name"):
            _ask(ticket, name=name)
        assert ClusterBuild.query.count() == 0 and writes == []

    def test_a_name_the_ticket_gives_is_used(self, vm_agent, writes):
        result = _ask(_ticket(subject="Two VMs called batch-dr"), name="batch-dr")
        assert result["vmNames"] == ["batch-dr-vm-1", "batch-dr-vm-2"]

    def test_one_request_at_a_time_per_ticket(self, vm_agent, writes):
        ticket = _ticket(subject="Need VMs")
        _ask(ticket)
        with pytest.raises(engine.AgentError, match="already a VM request"):
            _ask(ticket, name="tm-502")


# ---------------------------------------------------------------------------
# The whole road
# ---------------------------------------------------------------------------

class TestRoad:
    def test_plan_then_approval_then_vms_then_one_followup(
        self, client, app, admin_token, vm_agent, writes, monkeypatch, vcenter
    ):
        ticket = _ticket(subject="Two VMs in DR please")
        result = _ask(ticket)
        assert result["vmNames"] == ["tm-501-vm-1", "tm-501-vm-2"]
        task = _task(ticket)
        vm = _vm(task)
        # The plan was made at once (TESTING), so the approval is already out.
        assert task.status == "awaiting_approval" and vm["phase"] == "awaiting_approval"
        assert [m["name"] for m in vm["machines"]] == ["tm-501-vm-1", "tm-501-vm-2"]
        assert all(m["address"].startswith("10.20.30.") for m in vm["machines"])
        assert vm["machines"][0]["cpu"] == 4 and vm["machines"][0]["memoryGb"] == 8
        assert vm["machines"][0]["diskGb"] == 40  # the profile's default
        assert writes[-1]["comment"].startswith("Understood: two VMs")
        build = db.session.get(ClusterBuild, vm["buildId"])
        assert build.vms_only and build.machine_source == "vmware" and build.created_by.startswith("hermes")
        assert not state_store.vm_instances(build.id)  # nothing exists yet

        # Hermes' own account (the one that asked) cannot approve it.
        response = client.post(f"/api/ticket-agent/tasks/{task.id}/approve", headers=auth_headers(admin_token))
        assert response.status_code == 403 and "someone else" in response.get_json()["error"]

        other = second_admin_token(client, app)
        response = client.post(f"/api/ticket-agent/tasks/{task.id}/approve", headers=auth_headers(other))
        assert response.status_code == 200, response.get_json()
        assert any(w.get("comment") == "Approved — the VMs are being created now." for w in writes)
        db.session.refresh(task)
        assert task.status == "executed" and task.decided_by == "admin2"
        db.session.expire_all()
        build = db.session.get(ClusterBuild, vm["buildId"])
        assert build.status == "vms_ready"
        assert len(state_store.vm_instances(build.id)) == 2

        followups = []
        _fake_hermes(monkeypatch, lambda message: followups.append(message) or engine.set_status(
            ticket.id, "done", "Your VMs are ready: tm-501-vm-1, tm-501-vm-2.") and None)
        engine.tick()
        vm = _vm(task)
        assert vm["phase"] == "ready" and vm["finished"] is True
        [message] = followups
        assert message["task"] == "followup"
        event = message["event"]
        assert event["type"] == "vm_request_finished" and event["result"] == "ready"
        assert [v["name"] for v in event["vms"]] == ["tm-501-vm-1", "tm-501-vm-2"]
        assert "fallback" not in event
        assert writes[-1]["kind"] == "status" and writes[-1]["outcome"] == "deployed"

        # Nothing more happens to a finished request.
        assert vm_requests.advance() == 0

    def test_done_is_refused_while_the_vms_are_on_their_way(self, vm_agent, writes):
        ticket = _ticket(subject="VMs")
        _ask(ticket)
        with pytest.raises(engine.AgentError, match="VM request"):
            engine.set_status(ticket.id, "done", "Done!")
        # Saying it is being worked on does not withdraw the request.
        engine.set_status(ticket.id, "in_progress", "Working on it.")
        assert _task(ticket).status == "awaiting_approval"

    def test_reject_throws_the_draft_away_and_frees_the_addresses(
        self, client, admin_token, vm_agent, writes, monkeypatch
    ):
        ticket = _ticket(subject="VMs")
        _ask(ticket)
        task = _task(ticket)
        build_id = _vm(task)["buildId"]
        assert VSphereIpReservation.query.filter_by(build_id=build_id).count() == 2

        seen = []
        _fake_hermes(monkeypatch, lambda message: seen.append(message) and None)
        response = client.post(f"/api/ticket-agent/tasks/{task.id}/reject", json={"note": "use the UAT site"},
                               headers=auth_headers(admin_token))
        assert response.status_code == 200, response.get_json()
        vm = _vm(task)
        assert vm["phase"] == "rejected" and vm.get("draftDeleted") is True
        assert db.session.get(ClusterBuild, build_id) is None
        assert VSphereIpReservation.query.filter_by(build_id=build_id).count() == 0
        assert seen and seen[0]["event"]["type"] == "approval_rejected"
        assert "create 2 VMs" in seen[0]["event"]["action"]
        # The name is free again.
        _ask(_ticket(subject="again"))

    def test_expiry_releases_too(self, vm_agent, writes, monkeypatch):
        from datetime import timedelta

        ticket = _ticket(subject="VMs")
        _ask(ticket)
        task = _task(ticket)
        task.expires_at = engine._now() - timedelta(minutes=1)
        db.session.commit()
        _fake_hermes(monkeypatch, lambda message: None)
        assert engine.expire_approvals() == 1
        vm = _vm(task)
        assert vm["phase"] == "expired" and db.session.get(ClusterBuild, vm["buildId"]) is None

    def test_asking_hermes_again_withdraws_the_request(self, vm_agent, writes, monkeypatch):
        ticket = _ticket(subject="VMs")
        _ask(ticket)
        task = _task(ticket)
        _fake_hermes(monkeypatch, lambda message: None)
        engine.reinterpret(ticket.id)
        db.session.refresh(task)
        assert task.status == "superseded" and _vm(task)["phase"] == "withdrawn"
        assert db.session.get(ClusterBuild, _vm(task)["buildId"]) is None

    def test_a_plan_that_fails_goes_back_to_hermes(self, vm_agent, writes, monkeypatch):
        from api.services.cluster_build.provisioning import inventory

        # The provisioning account may not clone: the plan refuses.
        inventory.set_privilege_checker(lambda cfg, ids: [
            {"privilege": "VirtualMachine.Inventory.CreateFromExisting", "purpose": "", "entity": "x",
             "granted": False},
        ])
        ticket = _ticket(subject="VMs")
        seen = []
        _fake_hermes(monkeypatch, lambda message: seen.append(message) and None)
        _ask(ticket, count=1)
        task = _task(ticket)
        vm = _vm(task)
        assert vm["phase"] == "plan_failed" and task.status == "impediment"
        assert "CreateFromExisting" in vm["note"]
        assert db.session.get(ClusterBuild, vm["buildId"]) is None
        engine.kick()
        assert seen and seen[-1]["event"]["type"] == "vm_request_failed"

    def test_telegram_uses_the_vm_approver_list(self, client, app, vm_agent, writes, monkeypatch):
        sent, answered = [], []
        monkeypatch.setattr("api.services.ticket_agent.telegram.send_message",
                            lambda token, chat, text, buttons=None: sent.append((text, buttons)) or 91)
        monkeypatch.setattr("api.services.ticket_agent.telegram.edit_message", lambda *a, **k: None)
        monkeypatch.setattr("api.services.ticket_agent.telegram.answer_callback",
                            lambda token, cid, text="": answered.append(text))
        agent_settings.update({"telegramEnabled": True, "telegramBotToken": "123:abc",
                               "telegramChatId": "-100200", "telegramApprovers": "@deployer",
                               "vmApprovers": "@infra_lead"})
        ticket = _ticket(subject="VMs")
        _ask(ticket)
        task = _task(ticket)
        text, buttons = sent[-1]
        assert "tm-501-vm-1  10.20.30." in text and "4 vCPU · 8 GB · 40 GB" in text
        assert buttons[0][0]["callback_data"] == f"ka:{task.id}:a:{task.approval_nonce}"

        def press(username):
            monkeypatch.setattr(
                "api.services.ticket_agent.telegram.get_updates",
                lambda token, offset: [{"update_id": 30, "callback_query": {
                    "id": "cb", "data": f"ka:{task.id}:a:{task.approval_nonce}",
                    "from": {"id": 5, "username": username}, "message": {"chat": {"id": -100200}}}}],
            )
            return engine.poll_telegram()

        # A deploy approver is not a VM approver.
        assert press("deployer") == 0 and "VM approver list" in answered[-1]
        db.session.refresh(task)
        assert task.status == "awaiting_approval"
        assert press("infra_lead") == 1
        db.session.refresh(task)
        assert task.status == "executed" and task.decided_by == "@infra_lead (Telegram)"
        assert "VMs are being created" in answered[-1]

    def test_no_vm_approvers_means_kubesight_only(self, vm_agent, writes, monkeypatch):
        sent = []
        monkeypatch.setattr("api.services.ticket_agent.telegram.send_message",
                            lambda token, chat, text, buttons=None: sent.append((text, buttons)) or 92)
        monkeypatch.setattr("api.services.ticket_agent.telegram.answer_callback", lambda *a, **k: None)
        agent_settings.update({"telegramEnabled": True, "telegramBotToken": "123:abc",
                               "telegramChatId": "-100200", "telegramApprovers": ""})
        ticket = _ticket(subject="VMs")
        _ask(ticket)
        task = _task(ticket)
        text, buttons = sent[-1]
        assert buttons is None and "approve it in KubeSight" in text
        # Even with an empty deploy list ("anyone"), a press cannot approve VMs.
        monkeypatch.setattr(
            "api.services.ticket_agent.telegram.get_updates",
            lambda token, offset: [{"update_id": 40, "callback_query": {
                "id": "cb", "data": f"ka:{task.id}:a:{task.approval_nonce}",
                "from": {"id": 5, "username": "anyone"}, "message": {"chat": {"id": -100200}}}}],
        )
        assert engine.poll_telegram() == 0
        db.session.refresh(task)
        assert task.status == "awaiting_approval"

    def test_approving_vms_needs_cluster_build_permission(self, client, vm_agent, writes, monkeypatch):
        from api.access_engine import user_has_permission

        ticket = _ticket(subject="VMs")
        _ask(ticket)
        task = _task(ticket)
        monkeypatch.setattr(
            "api.routes.ticket_agent.user_has_permission",
            lambda user, key: False if key == "cluster_builds:execute" else user_has_permission(user, key),
        )
        login = client.post("/api/auth/login", json={"username": "admin", "password": "admin123"})
        token = login.get_json()["data"]["token"]
        response = client.post(f"/api/ticket-agent/tasks/{task.id}/approve", headers=auth_headers(token))
        assert response.status_code == 403 and "cluster_builds:execute" in response.get_json()["error"]


class TestKubernetes:
    HOSTS = {
        "10.20.30.50": ("tm-601-vm-1", "control_plane"),
        "10.20.30.51": ("tm-601-vm-2", "worker"),
    }

    def test_a_lab_cluster_on_new_vms(self, client, app, vm_agent, writes, monkeypatch, fake_ssh):
        vm_requests.update_profile(vm_agent["id"], {"allowKubernetes": True})
        ticket = _ticket(subject="A lab cluster please")
        result = _ask(ticket, name="tm-601", count=None, kubernetes={"shape": "lab"})
        assert result["vmNames"] == ["tm-601-vm-1", "tm-601-vm-2"]
        task = _task(ticket)
        vm = _vm(task)
        assert vm["kubernetes"]["counts"] == {"loadbalancer": 0, "controlPlane": 1, "worker": 1}
        assert vm["kubernetes"]["version"]

        set_transport_factory(lambda: build_default_fake({**self.HOSTS}))
        other = second_admin_token(client, app)
        response = client.post(f"/api/ticket-agent/tasks/{task.id}/approve", headers=auth_headers(other))
        assert response.status_code == 200, response.get_json()

        followups = []
        _fake_hermes(monkeypatch, lambda message: followups.append(message) and None)
        engine.tick()  # vms_ready → Kubernetes installed (synchronously under TESTING)
        engine.tick()  # completed → one follow-up
        vm = _vm(task)
        build = db.session.get(ClusterBuild, vm["buildId"])
        assert build.status == "completed", (build.error, vm)
        assert vm["phase"] == "cluster_ready" and vm["clusterId"] == build.result_cluster_id
        event = followups[-1]["event"]
        assert event["result"] == "cluster_ready" and event["clusterId"]
        assert {v["role"] for v in event["vms"]} == {"controlPlane", "worker"}

    def _approved_lab(self, client, app, vm_agent, name):
        vm_requests.update_profile(vm_agent["id"], {"allowKubernetes": True})
        ticket = _ticket(subject="A lab cluster please")
        _ask(ticket, name=name, count=None, kubernetes={"shape": "lab"})
        task = _task(ticket)
        other = second_admin_token(client, app)
        response = client.post(f"/api/ticket-agent/tasks/{task.id}/approve", headers=auth_headers(other))
        assert response.status_code == 200, response.get_json()
        return ticket, task

    def test_a_crashed_install_is_reported_not_silent(
        self, client, app, vm_agent, writes, monkeypatch, fake_ssh,
    ):
        from api.services.cluster_build.provisioning import service as provisioning_service
        from api.services.cluster_build.provisioning import jobs

        ticket, task = self._approved_lab(client, app, vm_agent, "tm-611")
        monkeypatch.setattr(provisioning_service, "install_kubernetes",
                            lambda *a, **k: (_ for _ in ()).throw(RuntimeError("kaboom")))
        followups = []
        _fake_hermes(monkeypatch, lambda message: followups.append(message) and None)
        engine.tick()
        vm = _vm(task)
        assert vm["phase"] == "needs_attention" and "kaboom" in vm["installNote"]
        build = db.session.get(ClusterBuild, vm["buildId"])
        job = next(j for j in jobs.jobs_for(build.id) if j.operation == "create")
        assert "kaboom" in job.progress_json["handoffNote"]
        engine.tick()
        assert followups[-1]["event"]["result"] == "needs_attention"

    def test_an_install_that_never_moved_is_reported(
        self, client, app, vm_agent, writes, monkeypatch, fake_ssh,
    ):
        # The install thread dies with the backend: started, never moved.
        monkeypatch.setattr(vm_requests, "_start_install", lambda task_id: None)
        ticket, task = self._approved_lab(client, app, vm_agent, "tm-612")
        _fake_hermes(monkeypatch, lambda message: None)
        engine.tick()
        vm = _vm(task)
        assert vm["phase"] == "installing" and vm["installStarted"]
        engine.tick()  # not stalled yet: still waiting
        assert _vm(task)["phase"] == "installing"
        vm["installStartedAt"] = "2026-01-01T00:00:00+00:00"
        task.decision = {**task.decision, "vmRequest": vm}
        db.session.commit()
        engine.tick()
        vm = _vm(task)
        assert vm["phase"] == "needs_attention" and "Install Kubernetes" in vm["installNote"]

    @pytest.mark.parametrize("ticket_names_a_version", [False, True])
    def test_the_vms_own_kubernetes_is_used_unless_the_ticket_names_one(
        self, client, app, vm_agent, writes, monkeypatch, fake_ssh, ticket_names_a_version,
    ):
        from api.services.cluster_build import k8s_versions

        offered = k8s_versions.supported_versions()
        newest, carried = offered[0], offered[-1]
        vm_requests.update_profile(vm_agent["id"], {"allowKubernetes": True})
        ticket = _ticket(subject="A lab cluster")
        kubernetes = {"shape": "lab", **({"version": newest} if ticket_names_a_version else {})}
        _ask(ticket, name="tm-621", count=None, kubernetes=kubernetes)
        task = _task(ticket)
        assert _vm(task)["kubernetes"]["versionAsked"] is ticket_names_a_version

        fake = build_default_fake({
            "10.20.30.50": ("tm-621-vm-1", "control_plane"),
            "10.20.30.51": ("tm-621-vm-2", "worker"),
        })
        # The template carries an older Kubernetes on every VM.
        fake.responders.insert(0, (
            lambda h, s: "KS_HAS_$tool" in s and "preflight probe" not in s,
            "".join(f"KS_HAS_{t}=v{carried}\n" for t in ("kubeadm", "kubelet", "kubectl")),
        ))
        set_transport_factory(lambda: fake)
        other = second_admin_token(client, app)
        assert client.post(f"/api/ticket-agent/tasks/{task.id}/approve",
                           headers=auth_headers(other)).status_code == 200
        _fake_hermes(monkeypatch, lambda message: None)
        engine.tick()
        build = db.session.get(ClusterBuild, _vm(task)["buildId"])
        assert build.k8s_version == (newest if ticket_names_a_version else carried)

    def test_kubernetes_needs_big_enough_vms(self, vm_agent, writes):
        vm_requests.update_profile(vm_agent["id"], {"allowKubernetes": True})
        with pytest.raises(engine.AgentError, match="Kubernetes needs every VM"):
            _ask(_ticket(subject="cluster"), count=None, size={"cpu": 1, "memoryGb": 2, "diskGb": 20},
                 kubernetes={"shape": "lab"})

    def test_shape_and_count_must_agree(self, vm_agent, writes):
        vm_requests.update_profile(vm_agent["id"], {"allowKubernetes": True})
        with pytest.raises(engine.AgentError, match="uses 2 VMs"):
            _ask(_ticket(subject="cluster"), count=3, kubernetes={"shape": "lab"})


def test_hermes_reaches_it_through_mcp(client, admin_token, vm_agent, writes):
    ticket = _ticket(subject="VMs")
    response = client.post("/api/mcp", json={
        "jsonrpc": "2.0", "id": 1, "method": "tools/call",
        "params": {"name": "kubesight_ticket_request_vms", "arguments": {
            "ticketRecordId": ticket.id, "profile": "DR test VMs", "name": "tm-701", "count": 1,
            "confidence": "High", "understanding": "One VM.", "comment": "One VM, waiting for approval.",
            "commentOnApprove": "Approved.",
        }},
    }, headers=auth_headers(admin_token))
    result = response.get_json()["result"]
    assert not result.get("isError"), result
    assert "nothing created yet" in result["structuredContent"]["changed"]
    brief = engine.brief(ticket.id)
    assert brief["vmProfiles"][0]["profile"] == "DR test VMs"
    assert brief["agentTasks"][0]["vmRequest"]["phase"] == "awaiting_approval"
