"""KubeSight creates the VMs with OpenTofu, then builds the cluster on them.

Runs the real job engine against the simulated OpenTofu engine (same
main.tf.json in, same state store out), a fake vCenter inventory, and the fake
SSH transport the Cluster Builder tests already use — so a test walks the
whole road: template → plan → apply → SSH → preflight → kubeadm → done, and
then grow and a two-person destroy.
"""

from __future__ import annotations

import base64
import json
from datetime import datetime, timedelta, timezone

import pytest

from api.db import db
from api.models import (
    Cluster,
    ClusterBuild,
    ClusterInfraState,
    ClusterProvisionJob,
    User,
    VSphereConnection,
    VSphereIpReservation,
)
from api.secret_encryption import encrypt_secret
from api.services.cluster_build.provisioning import (
    inventory,
    ip_pool,
    jobs,
    state_store,
    templates,
    tofu_config,
    tofu_runner,
)
from api.services.ssh import set_transport_factory

from tests.test_cluster_builds import (  # noqa: F401  (fixtures are used by name)
    auth_headers,
    build_default_fake,
    fake_ssh,
    no_network_cni_manifest,
    ssh_profile,
)


@pytest.fixture(autouse=True)
def _cluster_needs_no_approval(no_cluster_approvals):
    yield


# ---------------------------------------------------------------------------
# A vCenter, a network range, and the seams
# ---------------------------------------------------------------------------

@pytest.fixture()
def engine(tmp_path, monkeypatch):
    monkeypatch.setenv("KUBESIGHT_TOFU_WORKDIR", str(tmp_path / "tofu"))
    simulated = tofu_runner.SimulatedEngine()
    tofu_runner.set_engine(simulated)
    yield simulated
    tofu_runner.set_engine(None)


@pytest.fixture()
def vcenter(app):
    """A vCenter connection with a provisioning account, a range, and a fake inventory."""
    inventory.set_placement_fetcher(lambda cfg: inventory.demo_placement(cfg))
    inventory.set_privilege_checker(lambda cfg, ids: [
        {"privilege": p, "purpose": purpose, "entity": ids.get(scope) or ids.get("datacenter"),
         "granted": True}
        for p, purpose, scope in inventory.REQUIRED_PRIVILEGES
    ])
    ip_pool.set_address_prober(lambda address: False)
    jobs.set_ssh_waiter(lambda targets, timeout_s, on_ready: [on_ready(n, True, "") for n, _ in targets])
    row = VSphereConnection(
        name="vcsa-01", base_url="https://vcsa-01.example.test", username="ro@vsphere.local",
        password_cipher=encrypt_secret("ro-pass"),
        provisioning_username="prov@vsphere.local",
        provisioning_password_cipher=encrypt_secret("prov-pass-Secret1"),
    )
    db.session.add(row)
    db.session.commit()
    network = ip_pool.create_range(row.id, {
        "networkName": "VM-Net-K8S-30", "cidr": "10.20.30.0/24",
        "rangeStart": "10.20.30.50", "rangeEnd": "10.20.30.90",
        "gateway": "10.20.30.1", "dnsServers": "10.20.1.10, 10.20.1.11",
        "dnsDomain": "areeba.local",
    })
    yield {"connection": row, "range": network}
    inventory.set_placement_fetcher(None)
    inventory.set_privilege_checker(None)
    ip_pool.set_address_prober(None)
    jobs.set_ssh_waiter(None)


def placement_payload(connection_id, **overrides):
    dc = inventory.demo_placement()["datacenters"][0]
    template = dc["templates"][0]
    spec = {
        "vsphereConnectionId": connection_id,
        "datacenterId": dc["id"], "datacenterName": dc["name"],
        "clusterId": "domain-c8", "clusterName": "Cluster-Prod-A",
        "resourcePoolId": "resgroup-20", "resourcePoolName": "k8s-builds",
        "folderParentId": "group-v22", "folderParent": "KubeSight",
        "datastoreId": "datastore-12", "datastoreName": "ds-ssd-02",
        "networkId": "dvportgroup-41", "networkName": "VM-Net-K8S-30",
        "template": template,
        "counts": {"loadbalancer": 1, "controlPlane": 1, "worker": 2},
        "sizes": templates.BUILTIN_TEMPLATES[1]["spec"]["sizes"],
    }
    spec.update(overrides)
    return spec


def make_vmware_build(client, token, ssh_profile, vcenter, *, name="uat-02", **spec_overrides):
    payload = {
        "name": name,
        "k8sVersion": "1.32.4",
        "machineSource": "vmware",
        "templateId": "small",
        "cniPlugin": "calico",
        "podCidr": "10.244.0.0/16",
        "serviceCidr": "10.96.0.0/12",
        "connectionProfileId": ssh_profile["id"],
        "provisioning": placement_payload(vcenter["connection"].id, **spec_overrides),
    }
    response = client.post("/api/cluster-builds", json=payload, headers=auth_headers(token))
    assert response.status_code == 201, response.get_json()
    return response.get_json()["data"]


def plan(client, token, build_id):
    response = client.post(f"/api/cluster-builds/{build_id}/provision/plan", headers=auth_headers(token))
    assert response.status_code == 202, response.get_json()
    return response.get_json()["data"]


def apply(client, token, build_id, job_id, expect=200):
    response = client.post(
        f"/api/cluster-builds/{build_id}/provision/jobs/{job_id}/apply", headers=auth_headers(token)
    )
    assert response.status_code == expect, response.get_json()
    return response.get_json()["data"]


SMALL_HOSTS = {
    "10.20.30.51": ("uat-02-lb-1", "loadbalancer"),
    "10.20.30.52": ("uat-02-cp-1", "control_plane"),
    "10.20.30.53": ("uat-02-wk-1", "worker"),
    "10.20.30.54": ("uat-02-wk-2", "worker"),
}


def second_admin_token(client, app):
    admin = User.query.filter_by(username="admin").first()
    other = User(username="admin2", email="admin2@example.com", is_active=True, role_id=admin.role_id)
    other.password_hash = admin.password_hash
    for flag in ("first_login_completed",):
        if hasattr(admin, flag):
            setattr(other, flag, getattr(admin, flag))
    db.session.add(other)
    db.session.commit()
    response = client.post("/api/auth/login", json={"username": "admin2", "password": "admin123"})
    assert response.status_code == 200, response.get_json()
    return response.get_json()["data"]["token"]


# ---------------------------------------------------------------------------
# Templates
# ---------------------------------------------------------------------------

class TestTemplates:
    def test_built_in_catalog(self, client, admin_token):
        data = client.get("/api/cluster-templates", headers=auth_headers(admin_token)).get_json()["data"]
        ids = [t["id"] for t in data["builtin"]]
        assert ids == ["lab", "small", "standard-ha"]
        lab, small, ha = data["builtin"]
        assert lab["counts"] == {"loadbalancer": 0, "controlPlane": 1, "worker": 1}
        assert lab["endpointMode"] == "manual_endpoint"
        assert small["endpointMode"] == "managed_haproxy" and small["topologyType"] == "single_cp"
        assert ha["topologyType"] == "stacked_ha"

    @pytest.mark.parametrize("counts,message", [
        ({"loadbalancer": 2, "controlPlane": 2, "worker": 1}, "1, 3 or 5"),
        ({"loadbalancer": 1, "controlPlane": 3, "worker": 1}, "exactly 2 load balancers"),
        ({"loadbalancer": 2, "controlPlane": 1, "worker": 1}, "0 or 1 load balancer"),
        ({"loadbalancer": 0, "controlPlane": 1, "worker": 0}, "at least 1 worker"),
    ])
    def test_shape_rules(self, counts, message):
        with pytest.raises(ValueError, match=message):
            templates.normalize_counts(counts)

    def test_sizes_have_minimums(self):
        counts = {"loadbalancer": 0, "controlPlane": 1, "worker": 1}
        with pytest.raises(ValueError, match="Control planes need at least 2 vCPU"):
            templates.normalize_sizes({"controlPlane": {"cpu": 1, "memoryGb": 8, "diskGb": 80}}, counts)
        # A role with no machines is not held to its minimum.
        sizes = templates.normalize_sizes({"loadbalancer": {"cpu": 1, "memoryGb": 1, "diskGb": 1}}, counts)
        assert sizes["loadbalancer"]["memoryGb"] == 1

    def test_admin_saves_and_renames_a_template(self, client, admin_token):
        spec = {"counts": {"loadbalancer": 1, "controlPlane": 1, "worker": 3},
                "sizes": {"worker": {"cpu": 8, "memoryGb": 32, "diskGb": 200}},
                "addons": [{"id": "metallb", "config": {"addressPools": ["10.0.0.200-10.0.0.210"]}}]}
        response = client.post("/api/cluster-templates", json={"name": "Payments UAT", "spec": spec},
                               headers=auth_headers(admin_token))
        assert response.status_code == 201, response.get_json()
        saved = response.get_json()["data"]
        assert saved["id"].startswith("custom:")
        assert saved["counts"]["worker"] == 3 and saved["sizes"]["worker"]["memoryGb"] == 32
        assert saved["addons"][0]["id"] == "metallb"
        renamed = client.put(f"/api/cluster-templates/{saved['dbId']}", json={"name": "Payments UAT v2"},
                             headers=auth_headers(admin_token)).get_json()["data"]
        assert renamed["name"] == "Payments UAT v2"
        clash = client.post("/api/cluster-templates", json={"name": "small", "spec": spec},
                            headers=auth_headers(admin_token))
        assert clash.status_code == 400 and "built-in" in clash.get_json()["error"]

    def test_saving_needs_its_permission(self, client, viewer_token):
        response = client.post("/api/cluster-templates", json={"name": "x"},
                               headers=auth_headers(viewer_token))
        assert response.status_code == 403


# ---------------------------------------------------------------------------
# Address ranges
# ---------------------------------------------------------------------------

class TestAddressRanges:
    def test_gateway_inside_the_range_is_refused(self, app, vcenter):
        with pytest.raises(ValueError, match="gateway"):
            ip_pool.create_range(vcenter["connection"].id, {
                "networkName": "Other", "cidr": "10.21.0.0/24", "rangeStart": "10.21.0.1",
                "rangeEnd": "10.21.0.50", "gateway": "10.21.0.10", "dnsServers": "10.21.0.2",
            })

    def test_overlapping_ranges_are_refused(self, app, vcenter):
        with pytest.raises(ValueError, match="overlaps"):
            ip_pool.create_range(vcenter["connection"].id, {
                "networkName": "Other", "cidr": "10.20.30.0/24", "rangeStart": "10.20.30.80",
                "rangeEnd": "10.20.30.99", "gateway": "10.20.30.1", "dnsServers": "10.20.1.10",
            })

    def test_two_builds_never_share_an_address(self, client, admin_token, ssh_profile, vcenter, engine):
        first = make_vmware_build(client, admin_token, ssh_profile, vcenter, name="one")
        second = make_vmware_build(client, admin_token, ssh_profile, vcenter, name="two")
        plan(client, admin_token, first["id"])
        plan(client, admin_token, second["id"])
        addresses = [r.address for r in VSphereIpReservation.query.all()]
        assert len(addresses) == len(set(addresses)) == 10  # (VIP + 4 machines) × 2

    def test_an_address_that_answers_is_skipped(self, client, admin_token, ssh_profile, vcenter, engine):
        ip_pool.set_address_prober(lambda address: address == "10.20.30.52")
        build = make_vmware_build(client, admin_token, ssh_profile, vcenter)
        data = plan(client, admin_token, build["id"])
        addresses = {n["hostname"]: n["address"] for n in data["nodes"]}
        assert "10.20.30.52" not in addresses.values()
        checks = data["provisioning"]["job"]["summary"]["checks"]
        assert any(c["label"] == "Addresses" and "10.20.30.52" in c["detail"] for c in checks)


# ---------------------------------------------------------------------------
# The generated configuration
# ---------------------------------------------------------------------------

class TestConfiguration:
    def test_render(self):
        spec = placement_payload(1)
        spec.update({"template": {**spec["template"], "nicType": "vmxnet3"}})
        nodes = [
            {"name": "c-lb-1", "role": "loadbalancer", "ip": "10.0.0.51", "cpu": 2, "memoryGb": 2, "diskGb": 40},
            {"name": "c-cp-1", "role": "controlPlane", "ip": "10.0.0.52", "cpu": 4, "memoryGb": 8, "diskGb": 20},
        ]
        config = tofu_config.render(
            cluster_name="c", build_id=7, spec=spec, nodes=nodes,
            network_range={"cidr": "10.0.0.0/24", "gateway": "10.0.0.1",
                           "dnsServers": ["10.0.0.2"], "dnsDomain": "example.local"},
        )
        vm = config["resource"]["vsphere_virtual_machine"]["node"]
        assert set(vm["for_each"]) == {"c-lb-1", "c-cp-1"}
        # A clone cannot shrink the template's 40 GB disk.
        assert vm["for_each"]["c-cp-1"]["disk_gb"] == 40
        assert vm["clone"][0]["template_uuid"] == spec["template"]["uuid"]
        assert "clone" in vm["lifecycle"][0]["ignore_changes"]
        assert config["resource"]["vsphere_folder"]["cluster"]["path"] == "KubeSight/c"
        # One control plane: no keep-apart rule to make.
        assert "vsphere_compute_cluster_vm_anti_affinity_rule" not in config["resource"]
        text = json.dumps(config)
        assert "password" not in text.lower()

    def test_ha_gets_keep_apart_rules(self):
        spec = placement_payload(1)
        nodes = [{"name": f"c-cp-{i}", "role": "controlPlane", "ip": f"10.0.0.{i}", "cpu": 4,
                  "memoryGb": 8, "diskGb": 80} for i in (1, 2, 3)]
        config = tofu_config.render(
            cluster_name="c", build_id=1, spec=spec, nodes=nodes,
            network_range={"cidr": "10.0.0.0/24", "gateway": "10.0.0.254", "dnsServers": []},
        )
        rule = config["resource"]["vsphere_compute_cluster_vm_anti_affinity_rule"]["control_planes"]
        assert len(rule["virtual_machine_ids"]) == 3
        assert rule["compute_cluster_id"] == "domain-c8"


# ---------------------------------------------------------------------------
# The whole road
# ---------------------------------------------------------------------------

class TestCreateCluster:
    def test_vmware_build_needs_a_dns_style_name(self, client, admin_token, ssh_profile, vcenter):
        payload = {
            "name": "UAT 02", "k8sVersion": "1.32.4", "machineSource": "vmware",
            "connectionProfileId": ssh_profile["id"],
            "provisioning": placement_payload(vcenter["connection"].id),
        }
        response = client.post("/api/cluster-builds", json=payload, headers=auth_headers(admin_token))
        assert response.status_code == 400
        assert "lowercase" in response.get_json()["error"]

    def test_network_without_a_range_is_refused(self, client, admin_token, ssh_profile, vcenter):
        payload = {
            "name": "uat-02", "k8sVersion": "1.32.4", "machineSource": "vmware",
            "connectionProfileId": ssh_profile["id"],
            "provisioning": placement_payload(vcenter["connection"].id, networkName="VM-Net-DMZ-12"),
        }
        response = client.post("/api/cluster-builds", json=payload, headers=auth_headers(admin_token))
        assert response.status_code == 400
        assert "no address range" in response.get_json()["error"]

    def test_plan_apply_build(self, client, admin_token, ssh_profile, vcenter, engine, fake_ssh):
        fake = build_default_fake(SMALL_HOSTS)
        set_transport_factory(lambda: fake)
        build = make_vmware_build(client, admin_token, ssh_profile, vcenter)
        assert build["machineSource"] == "vmware"
        assert build["topologyType"] == "single_cp" and build["endpointMode"] == "managed_haproxy"

        planned = plan(client, admin_token, build["id"])
        job = planned["provisioning"]["job"]
        assert job["status"] == "planned", job.get("error")
        summary = job["summary"]
        # One folder and four VMs; one control plane, so no keep-apart rule.
        assert (summary["add"], summary["change"], summary["destroy"]) == (5, 0, 0)
        assert summary["blocked"] is None
        assert {r["title"] for r in summary["resources"] if r["kind"] == "vm"} == {
            "uat-02-lb-1", "uat-02-cp-1", "uat-02-wk-1", "uat-02-wk-2",
        }
        assert planned["vipAddress"] == "10.20.30.50"
        assert planned["controlPlaneEndpoint"] == "10.20.30.50:6443"
        assert {n["address"] for n in planned["nodes"]} == set(SMALL_HOSTS)
        # Nothing secret ever reaches the stored plan.
        stored = ClusterProvisionJob.query.get(job["id"])
        assert "prov-pass-Secret1" not in (stored.plan_text or "") + (stored.log_tail or "")
        assert "prov-pass-Secret1" not in json.dumps(stored.config_json)

        # Preflight is refused before the VMs exist.
        early = client.post(f"/api/cluster-builds/{build['id']}/preflight", headers=auth_headers(admin_token))
        assert early.status_code == 400 and "not created" in early.get_json()["error"]

        done = apply(client, admin_token, build["id"], job["id"])
        assert done["status"] == "completed", (done.get("error"), done["provisioning"]["job"])
        assert done["provisionStatus"] == "ready"
        assert done["resultClusterId"]
        vms = state_store.vm_instances(build["id"])
        assert set(vms) == {"uat-02-lb-1", "uat-02-cp-1", "uat-02-wk-1", "uat-02-wk-2"}
        node = next(n for n in done["nodes"] if n["hostname"] == "uat-02-cp-1")
        assert node["vsphereVmMoid"].startswith("vm-") and node["status"] == "joined"
        assert {r.status for r in VSphereIpReservation.query.filter_by(build_id=build["id"])} == {"in_use"}
        assert done["canDestroy"] is True
        # A cluster with VMs is never simply deleted.
        response = client.delete(f"/api/cluster-builds/{build['id']}", headers=auth_headers(admin_token))
        assert response.status_code == 400

    def test_discarding_a_plan_returns_the_addresses(self, client, admin_token, ssh_profile, vcenter, engine):
        build = make_vmware_build(client, admin_token, ssh_profile, vcenter)
        job = plan(client, admin_token, build["id"])["provisioning"]["job"]
        assert VSphereIpReservation.query.count() == 5
        response = client.post(
            f"/api/cluster-builds/{build['id']}/provision/jobs/{job['id']}/discard",
            headers=auth_headers(admin_token),
        )
        assert response.status_code == 200, response.get_json()
        assert VSphereIpReservation.query.count() == 0
        assert response.get_json()["data"]["nodes"] == []

    def test_name_already_in_vcenter_fails_the_plan(self, client, admin_token, ssh_profile, vcenter, engine):
        build = make_vmware_build(client, admin_token, ssh_profile, vcenter, name="payments-uat-01")
        job = plan(client, admin_token, build["id"])["provisioning"]["job"]
        assert job["status"] == "plan_failed"
        assert "already has VMs called payments-uat-01-cp-1" in job["error"]

    def test_missing_privileges_fail_the_plan(self, client, admin_token, ssh_profile, vcenter, engine):
        inventory.set_privilege_checker(lambda cfg, ids: [
            {"privilege": "VirtualMachine.Inventory.CreateFromExisting", "purpose": "", "entity": "x",
             "granted": False},
        ])
        build = make_vmware_build(client, admin_token, ssh_profile, vcenter)
        job = plan(client, admin_token, build["id"])["provisioning"]["job"]
        assert job["status"] == "plan_failed"
        assert "CreateFromExisting" in job["error"]

    def test_incompatible_template_fails_the_plan(self, client, admin_token, ssh_profile, vcenter, engine):
        windows = inventory.demo_placement()["datacenters"][0]["templates"][4]
        build = make_vmware_build(client, admin_token, ssh_profile, vcenter, template=windows)
        job = plan(client, admin_token, build["id"])["provisioning"]["job"]
        assert job["status"] == "plan_failed"
        assert "cannot be used" in job["error"]


class TestPartialFailure:
    def test_failed_vm_then_finish_with_a_new_plan(
        self, client, admin_token, ssh_profile, vcenter, engine, fake_ssh
    ):
        fake = build_default_fake(SMALL_HOSTS)
        set_transport_factory(lambda: fake)
        engine.fail_on = "uat-02-wk-2"
        build = make_vmware_build(client, admin_token, ssh_profile, vcenter)
        job = plan(client, admin_token, build["id"])["provisioning"]["job"]
        failed = apply(client, admin_token, build["id"], job["id"])
        assert failed["status"] == "provision_failed"
        assert failed["provisionStatus"] == "apply_failed"
        progress = failed["provisioning"]["job"]["progress"]["vms"]
        assert progress["uat-02-wk-2"]["state"] == "failed"
        assert progress["uat-02-cp-1"]["state"] == "created"
        assert len(state_store.vm_instances(build["id"])) == 3
        assert not ClusterInfraState.query.filter_by(build_id=build["id"]).first().lock_id

        # Space was freed; plan again: only the missing VM is created.
        engine.fail_on = None
        again = plan(client, admin_token, build["id"])["provisioning"]["job"]
        assert (again["summary"]["add"], again["summary"]["destroy"]) == (1, 0)
        done = apply(client, admin_token, build["id"], again["id"])
        assert done["status"] == "completed", done.get("error")
        assert len(state_store.vm_instances(build["id"])) == 4

    def test_datastore_of_created_vms_cannot_change_but_new_ones_can(
        self, client, admin_token, ssh_profile, vcenter, engine
    ):
        engine.fail_on = "uat-02-wk-2"
        build = make_vmware_build(client, admin_token, ssh_profile, vcenter)
        job = plan(client, admin_token, build["id"])["provisioning"]["job"]
        apply(client, admin_token, build["id"], job["id"])
        moved = client.put(
            f"/api/cluster-builds/{build['id']}",
            json={"provisioning": placement_payload(vcenter["connection"].id,
                                                    datastoreId="datastore-13",
                                                    datastoreName="ds-nvme-01")},
            headers=auth_headers(admin_token),
        )
        assert moved.status_code == 200, moved.get_json()
        engine.fail_on = None
        again = plan(client, admin_token, build["id"])["provisioning"]["job"]
        stored = ClusterProvisionJob.query.get(again["id"])
        for_each = stored.config_json["tofu"]["resource"]["vsphere_virtual_machine"]["node"]["for_each"]
        assert for_each["uat-02-wk-2"]["datastore_id"] == "datastore-13"
        assert for_each["uat-02-cp-1"]["datastore_id"] == "datastore-12"

        recount = client.put(
            f"/api/cluster-builds/{build['id']}",
            json={"provisioning": placement_payload(
                vcenter["connection"].id,
                counts={"loadbalancer": 2, "controlPlane": 3, "worker": 2})},
            headers=auth_headers(admin_token),
        )
        assert recount.status_code == 400
        assert "can no longer change" in recount.get_json()["error"]


class TestVmsOnly:
    """VMs first — just how many — and Kubernetes later, in a shape that fits them."""

    VM_HOSTS = {
        "10.20.30.50": ("uat-02-vm-1", "loadbalancer"),
        "10.20.30.51": ("uat-02-vm-2", "control_plane"),
        "10.20.30.52": ("uat-02-vm-3", "worker"),
        "10.20.30.53": ("uat-02-vm-4", "worker"),
    }

    @staticmethod
    def make(client, token, ssh_profile, vcenter, *, count=4, **spec_overrides):
        spec = placement_payload(vcenter["connection"].id, counts=None, vmCount=count,
                                 sizes={"vm": {"cpu": 2, "memoryGb": 4, "diskGb": 40}})
        spec.update(spec_overrides)
        response = client.post("/api/cluster-builds", json={
            "name": "uat-02", "k8sVersion": "1.32.4", "machineSource": "vmware", "vmsOnly": True,
            "templateId": "custom", "connectionProfileId": ssh_profile["id"], "provisioning": spec,
        }, headers=auth_headers(token))
        assert response.status_code == 201, response.get_json()
        return response.get_json()["data"]

    @pytest.fixture()
    def ready(self, client, admin_token, ssh_profile, vcenter, engine):
        build = self.make(client, admin_token, ssh_profile, vcenter)
        assert build["vmsOnly"] is True
        planned = plan(client, admin_token, build["id"])
        job = planned["provisioning"]["job"]
        assert job["status"] == "planned", job.get("error")
        assert {r["title"] for r in job["summary"]["resources"] if r["kind"] == "vm"} == {
            "uat-02-vm-1", "uat-02-vm-2", "uat-02-vm-3", "uat-02-vm-4",
        }
        assert planned["vipAddress"] is None  # no roles yet, so no API address
        # No SSH transport is installed: anything past "the VMs answer" would fail.
        return apply(client, admin_token, build["id"], job["id"])

    def test_one_vm(self, client, admin_token, ssh_profile, vcenter, engine):
        build = self.make(client, admin_token, ssh_profile, vcenter, count=1)
        job = plan(client, admin_token, build["id"])["provisioning"]["job"]
        assert [r["title"] for r in job["summary"]["resources"] if r["kind"] == "vm"] == ["uat-02-vm-1"]
        done = apply(client, admin_token, build["id"], job["id"])
        assert done["status"] == "vms_ready" and done["nodeCounts"]["vm"] == 1

    def test_stops_once_the_vms_answer(self, client, admin_token, ready):
        assert ready["status"] == "vms_ready", (ready.get("error"), ready["provisioning"]["job"])
        assert ready["provisionStatus"] == "ready"
        assert not ready["resultClusterId"] and not ready["steps"]
        assert {n["role"] for n in ready["nodes"]} == {"vm"}
        assert len(state_store.vm_instances(ready["id"])) == 4
        assert ready["canDestroy"] is True
        for path in ("preflight", "start"):
            refused = client.post(f"/api/cluster-builds/{ready['id']}/{path}", headers=auth_headers(admin_token))
            assert refused.status_code == 400 and "VMs only" in refused.get_json()["error"], path
        deleted = client.delete(f"/api/cluster-builds/{ready['id']}", headers=auth_headers(admin_token))
        assert deleted.status_code == 400

    def test_install_kubernetes_in_a_shape_that_fits(self, client, admin_token, ready, fake_ssh):
        url = f"/api/cluster-builds/{ready['id']}/provision/install-kubernetes"
        wrong = client.post(url, json={"counts": {"loadbalancer": 0, "controlPlane": 1, "worker": 1}},
                            headers=auth_headers(admin_token))
        assert wrong.status_code == 400 and "4 VMs" in wrong.get_json()["error"]

        set_transport_factory(lambda: build_default_fake({**self.VM_HOSTS}))
        response = client.post(url, json={"counts": {"loadbalancer": 1, "controlPlane": 1, "worker": 2}},
                               headers=auth_headers(admin_token))
        assert response.status_code == 200, response.get_json()
        built = response.get_json()["data"]
        assert built["vmsOnly"] is False and built["templateId"] == "small"
        roles = {n["hostname"]: n["role"] for n in built["nodes"]}
        assert roles == {"uat-02-vm-1": "loadbalancer", "uat-02-vm-2": "control_plane",
                         "uat-02-vm-3": "worker", "uat-02-vm-4": "worker"}
        assert built["vipAddress"] == "10.20.30.54"
        assert built["controlPlaneEndpoint"] == "10.20.30.54:6443"
        assert built["status"] == "completed", (built.get("error"), built["provisioning"]["job"])
        assert built["resultClusterId"]
        assert len(state_store.vm_instances(ready["id"])) == 4  # the same VMs, none created

        again = client.post(url, json={"counts": {"loadbalancer": 1, "controlPlane": 1, "worker": 2}},
                            headers=auth_headers(admin_token))
        assert again.status_code == 400

    def test_destroy_takes_a_second_person_too(self, client, app, admin_token, ready):
        job = client.post(
            f"/api/cluster-builds/{ready['id']}/provision/destroy",
            json={"confirmName": "uat-02"}, headers=auth_headers(admin_token),
        ).get_json()["data"]["provisioning"]["job"]
        assert job["status"] == "awaiting_approval"
        approved = client.post(
            f"/api/cluster-builds/{ready['id']}/provision/jobs/{job['id']}/approve",
            headers=auth_headers(second_admin_token(client, app)),
        )
        assert approved.status_code == 200, approved.get_json()
        assert approved.get_json()["data"]["status"] == "destroyed"
        assert state_store.vm_instances(ready["id"]) == {}

    def test_only_for_vms_kubesight_creates(self, client, admin_token, ssh_profile):
        response = client.post("/api/cluster-builds", json={
            "name": "lab-1", "k8sVersion": "1.32.4", "machineSource": "existing", "vmsOnly": True,
            "endpointMode": "manual_endpoint", "controlPlaneEndpoint": "10.0.0.10:6443",
            "connectionProfileId": ssh_profile["id"],
        }, headers=auth_headers(admin_token))
        assert response.status_code == 201, response.get_json()
        assert response.get_json()["data"]["vmsOnly"] is False


class TestCloneMirrorsTemplate:
    """The provider reconfigures every clone to match its config; KubeSight writes
    that config from the template, so nothing on the clone is added, removed or
    changed except the hostname and address."""

    def test_devices_are_read_like_the_provider_reads_them(self):
        from pyVmomi import vim

        disk_backing = vim.vm.device.VirtualDisk.FlatVer2BackingInfo(
            thinProvisioned=True, eagerlyScrub=False, diskMode="persistent",
            sharing="sharingNone", writeThrough=False,
        )
        devices = [
            vim.vm.device.ParaVirtualSCSIController(key=1000, busNumber=0, scsiCtlrUnitNumber=7),
            vim.vm.device.VirtualIDEController(key=200, busNumber=0),
            vim.vm.device.VirtualIDEController(key=201, busNumber=1),
            vim.vm.device.VirtualAHCIController(key=15000, busNumber=0),
            vim.vm.device.VirtualDisk(
                key=2000, controllerKey=1000, unitNumber=0,
                capacityInBytes=40 * 1024 ** 3, capacityInKB=40 * 1024 ** 2, backing=disk_backing,
                storageIOAllocation=vim.StorageResourceManager.IOAllocationInfo(
                    limit=-1, reservation=0, shares=vim.SharesInfo(level="normal", shares=1000)),
            ),
            vim.vm.device.VirtualCdrom(
                key=16000, controllerKey=15000, unitNumber=0,
                backing=vim.vm.device.VirtualCdrom.RemoteAtapiBackingInfo(deviceName=""),
            ),
            vim.vm.device.VirtualVmxnet3(
                key=4000,
                backing=vim.vm.device.VirtualEthernetCard.DistributedVirtualPortBackingInfo(
                    port=vim.dvs.PortConnection(portgroupKey="dvportgroup-41", switchUuid="dvs")),
            ),
            vim.vm.device.VirtualTPM(key=11000),
        ]
        hardware = inventory._template_hardware(devices)
        odd = vim.vm.device.VirtualDisk(key=2001, controllerKey=1000, unitNumber=1,
                                        capacityInBytes=40 * 1024 ** 3 + 1024, backing=disk_backing)
        assert inventory._template_hardware([devices[0], odd])["disks"][0]["sizeGb"] == 41  # up, like the provider
        assert hardware["controllers"] == {"scsi": 1, "sata": 1, "ide": 2, "nvme": 0}
        assert hardware["scsiType"] == "pvscsi"
        assert hardware["disks"] == [{
            "unit": 0, "controllerType": "scsi", "sizeGb": 40, "thin": True, "eagerlyScrub": False,
            "diskMode": "persistent", "sharing": "sharingNone", "writeThrough": False,
            "ioLimit": -1, "ioReservation": 0, "ioShareLevel": "normal", "ioShareCount": 1000,
        }]
        assert hardware["cdroms"] == [{"clientDevice": True}]
        assert hardware["nics"] == [{"type": "vmxnet3", "networkId": "dvportgroup-41"}]
        assert hardware["vtpm"] is True

    def test_settings_are_read_under_the_providers_names(self):
        from pyVmomi import vim

        config = vim.vm.ConfigInfo(
            annotation="Golden image 2026-09", cpuHotAddEnabled=True, memoryHotAddEnabled=False,
            swapPlacement="inherit",
            hardware=vim.vm.VirtualHardware(numCPU=4, numCoresPerSocket=2, memoryMB=8192),
            tools=vim.vm.ToolsConfigInfo(
                syncTimeWithHostAllowed=True, syncTimeWithHost=False, toolsUpgradePolicy="manual",
                afterPowerOn=True, afterResume=True, beforeGuestStandby=True,
                beforeGuestShutdown=True, beforeGuestReboot=True),
            flags=vim.vm.FlagInfo(diskUuidEnabled=True, virtualExecUsage="hvAuto",
                                  virtualMmuUsage="automatic", enableLogging=True),
            bootOptions=vim.vm.BootOptions(bootDelay=0, efiSecureBootEnabled=True,
                                           bootRetryEnabled=False, bootRetryDelay=10000),
            cpuAllocation=vim.ResourceAllocationInfo(
                limit=-1, reservation=0, shares=vim.SharesInfo(level="normal", shares=4000)),
            latencySensitivity=vim.LatencySensitivity(level="normal"),
        )
        settings = inventory._template_settings(config)
        assert settings["annotation"] == "Golden image 2026-09"
        assert settings["enable_disk_uuid"] is True and settings["efi_secure_boot_enabled"] is True
        assert settings["sync_time_with_host"] is True and settings["sync_time_with_host_periodically"] is False
        assert settings["num_cores_per_socket"] == 2 and settings["cpu_hot_add_enabled"] is True
        assert settings["cpu_share_level"] == "normal" and settings["cpu_limit"] == -1

    @staticmethod
    def with_template(**fields):
        def placement(cfg):
            data = inventory.demo_placement(cfg)
            data["datacenters"][0]["templates"][0].update(fields)
            return data
        inventory.set_placement_fetcher(placement)
        inventory.invalidate()

    def test_the_config_repeats_the_template(self, client, admin_token, ssh_profile, vcenter, engine):
        self.with_template(
            settings={"annotation": "Golden image", "enable_disk_uuid": True,
                      "num_cores_per_socket": 2, "cpu_share_level": "normal", "cpu_share_count": 4000},
            cdroms=[{"clientDevice": True}], vtpm=True,
            controllers={"scsi": 1, "sata": 1, "ide": 2, "nvme": 0},
            nics=[{"type": "vmxnet3", "networkId": "dvportgroup-41"},
                  {"type": "vmxnet3", "networkId": "dvportgroup-42"}],
        )
        # An account that may only clone: none of the Config privileges.
        inventory.set_privilege_checker(lambda cfg, ids: [
            {"privilege": p, "purpose": purpose, "entity": ids.get(scope) or ids.get("datacenter"),
             "granted": p in inventory.REQUIRED_TO_CLONE}
            for p, purpose, scope in inventory.REQUIRED_PRIVILEGES
        ])
        build = make_vmware_build(client, admin_token, ssh_profile, vcenter, name="uat-07",
                                  sizeMode="template", folderMode="existing")
        job = plan(client, admin_token, build["id"])["provisioning"]["job"]
        assert job["status"] == "planned", job.get("error")
        vm = ClusterProvisionJob.query.get(job["id"]).config_json["tofu"]["resource"][
            "vsphere_virtual_machine"]["node"]
        assert vm["annotation"] == "Golden image"  # the template's note, not KubeSight's
        assert vm["enable_disk_uuid"] is True and vm["num_cores_per_socket"] == 2
        assert "cpu_share_count" not in vm  # derived by vCenter for a named level
        assert vm["cdrom"] == [{"client_device": True}] and vm["vtpm"] == [{"version": "2.0"}]
        assert vm["sata_controller_count"] == 1 and vm["ide_controller_count"] == 2
        assert [n["network_id"] for n in vm["network_interface"]] == ["dvportgroup-41", "dvportgroup-42"]
        customize = vm["clone"][0]["customize"][0]
        assert customize["network_interface"][1] == {}  # DHCP on the template's second card
        assert {"cdrom", "vtpm", "enable_disk_uuid"} <= set(vm["lifecycle"][0]["ignore_changes"])
        said = {c["label"]: c for c in job["summary"]["checks"]}
        assert said["Clone"]["status"] == "ok" and "left exactly as" in said["Clone"]["detail"]
        assert "Network card" not in said

    def test_the_network_card_edit_needs_edit_device(self, client, admin_token, ssh_profile, vcenter, engine):
        # The provider re-applies the clone's card once (vCenter gave it a MAC
        # the config cannot know): an edit, so Modify device settings is needed.
        self.with_template(nics=[{"type": "vmxnet3", "networkId": "dvportgroup-42"}])
        inventory.set_privilege_checker(lambda cfg, ids: [
            {"privilege": p, "purpose": purpose, "entity": ids.get(scope) or ids.get("datacenter"),
             "granted": p in inventory.REQUIRED_TO_CLONE and p != inventory.EDIT_DEVICE}
            for p, purpose, scope in inventory.REQUIRED_PRIVILEGES
        ])
        build = make_vmware_build(client, admin_token, ssh_profile, vcenter, name="uat-08",
                                  sizeMode="template", folderMode="existing")
        job = plan(client, admin_token, build["id"])["provisioning"]["job"]
        assert job["status"] == "plan_failed"
        assert "VirtualMachine.Config.EditDevice" in job["error"]

    def test_moving_the_card_to_another_network_is_said(self, client, admin_token, ssh_profile, vcenter, engine):
        self.with_template(nics=[{"type": "vmxnet3", "networkId": "dvportgroup-42"}])
        build = make_vmware_build(client, admin_token, ssh_profile, vcenter, name="uat-09",
                                  sizeMode="template", folderMode="existing")
        job = plan(client, admin_token, build["id"])["provisioning"]["job"]
        assert job["status"] == "planned", job.get("error")
        card = next(c for c in job["summary"]["checks"] if c["label"] == "Network card")
        assert card["status"] == "warn" and "VM-Net-DMZ-12" in card["detail"]



class TestExportConfig:
    def test_the_jobs_main_tf_json_can_be_downloaded(self, client, admin_token, ssh_profile, vcenter, engine):
        build = make_vmware_build(client, admin_token, ssh_profile, vcenter)
        job = plan(client, admin_token, build["id"])["provisioning"]["job"]
        response = client.get(f"/api/cluster-builds/{build['id']}/provision/jobs/{job['id']}/config",
                              headers=auth_headers(admin_token))
        assert response.status_code == 200, response.get_json()
        data = response.get_json()["data"]
        assert data["filename"] == f"uat-02-job-{job['id']}-main.tf.json"
        config = json.loads(data["content"])
        assert "vsphere_virtual_machine" in config["resource"]
        assert "prov-pass" not in data["content"] and "ro-pass" not in data["content"]
        missing = client.get(f"/api/cluster-builds/{build['id']}/provision/jobs/999999/config",
                             headers=auth_headers(admin_token))
        assert missing.status_code == 404


class TestFolderAndSizeModes:
    """An admin-made folder the account works in, and an account that may not resize VMs."""

    def config_of(self, client, token, build_id):
        job = plan(client, token, build_id)["provisioning"]["job"]
        assert job["status"] == "planned", job.get("error")
        return ClusterProvisionJob.query.get(job["id"]).config_json["tofu"], job

    def test_vms_go_straight_into_an_existing_folder(self, client, admin_token, ssh_profile, vcenter, engine):
        build = make_vmware_build(client, admin_token, ssh_profile, vcenter, folderMode="existing")
        tofu, job = self.config_of(client, admin_token, build["id"])
        assert "vsphere_folder" not in tofu["resource"]
        assert tofu["resource"]["vsphere_virtual_machine"]["node"]["folder"] == "KubeSight"
        assert not [r for r in job["summary"]["resources"] if r["kind"] == "folder"]

    def test_existing_folder_must_be_chosen(self, client, admin_token, ssh_profile, vcenter):
        payload = {
            "name": "uat-02", "k8sVersion": "1.32.4", "machineSource": "vmware",
            "connectionProfileId": ssh_profile["id"],
            "provisioning": placement_payload(vcenter["connection"].id, folderMode="existing",
                                              folderParentId=None, folderParent=""),
        }
        response = client.post("/api/cluster-builds", json=payload, headers=auth_headers(admin_token))
        assert response.status_code == 400 and "folder" in response.get_json()["error"]

    def test_template_size_is_kept(self, client, admin_token, ssh_profile, vcenter, engine):
        build = make_vmware_build(client, admin_token, ssh_profile, vcenter, sizeMode="template")
        tofu, _ = self.config_of(client, admin_token, build["id"])
        for_each = tofu["resource"]["vsphere_virtual_machine"]["node"]["for_each"]
        # The demo template: 2 vCPU, 4096 MB, a 40 GB disk — on every role.
        assert {(v["cpu"], v["memory_mb"], v["disk_gb"]) for v in for_each.values()} == {(2, 4096, 40)}

    @staticmethod
    def account_without(not_granted):
        inventory.set_privilege_checker(lambda cfg, ids: [
            {"privilege": p, "purpose": purpose, "entity": ids.get(scope) or ids.get("datacenter"),
             "granted": p not in not_granted}
            for p, purpose, scope in inventory.REQUIRED_PRIVILEGES
        ])

    def test_the_plan_fits_itself_to_the_account(self, client, admin_token, ssh_profile, vcenter, engine):
        # A role a vSphere admin granted on one folder: no folders, no resizing,
        # no DRS rules, no deleting — and a few settings privileges missing too.
        self.account_without({
            "Folder.Create", "Folder.Delete", "VirtualMachine.Config.DiskExtend",
            "Host.Inventory.EditCluster", "VirtualMachine.Interact.PowerOff",
            "VirtualMachine.Config.AddNewDisk", "VirtualMachine.Config.AdvancedConfig",
        })
        build = make_vmware_build(client, admin_token, ssh_profile, vcenter, name="uat-03")
        planned = plan(client, admin_token, build["id"])
        job = planned["provisioning"]["job"]
        assert job["status"] == "planned", job.get("error")
        spec = planned["provisioning"]["spec"]
        assert spec["folderMode"] == "existing" and spec["sizeMode"] == "template"
        tofu = ClusterProvisionJob.query.get(job["id"]).config_json["tofu"]
        assert "vsphere_folder" not in tofu["resource"]
        for_each = tofu["resource"]["vsphere_virtual_machine"]["node"]["for_each"]
        assert {(v["cpu"], v["memory_mb"], v["disk_gb"]) for v in for_each.values()} == {(2, 4096, 40)}
        said = {c["label"]: c for c in job["summary"]["checks"]}
        assert said["Machine sizes"]["status"] == "warn" and "keeps" in said["Machine sizes"]["detail"]
        assert said["VM folder"]["status"] == "warn"
        assert said["Account privileges"]["status"] == "warn"
        assert "VirtualMachine.Config.AdvancedConfig" in said["Account privileges"]["detail"]
        assert "PowerOff" not in said["Account privileges"]["detail"]  # only a destroy needs it

    def test_ha_without_drs_rights_skips_the_rules(self, client, admin_token, ssh_profile, vcenter, engine):
        self.account_without({"Host.Inventory.EditCluster"})
        build = make_vmware_build(client, admin_token, ssh_profile, vcenter, name="uat-05",
                                  counts={"loadbalancer": 2, "controlPlane": 3, "worker": 2})
        job = plan(client, admin_token, build["id"])["provisioning"]["job"]
        assert job["status"] == "planned", job.get("error")
        tofu = ClusterProvisionJob.query.get(job["id"]).config_json["tofu"]
        assert "vsphere_compute_cluster_vm_anti_affinity_rule" not in tofu["resource"]
        assert any(c["label"] == "Keep-apart rules" and c["status"] == "warn" for c in job["summary"]["checks"])

    def test_a_guess_from_roles_warns_instead_of_blocking(self, client, admin_token, ssh_profile, vcenter, engine):
        # vCenter refused to say; KubeSight read the account's roles and they do
        # not show these. A group could still grant them, so vCenter decides.
        inventory.set_privilege_checker(lambda cfg, ids: [
            {"privilege": p, "purpose": purpose, "entity": ids.get(scope) or ids.get("datacenter"),
             "granted": p not in {"VirtualMachine.Provisioning.DeployTemplate", "Resource.AssignVMToPool"},
             "source": "roles"}
            for p, purpose, scope in inventory.REQUIRED_PRIVILEGES
        ])
        build = make_vmware_build(client, admin_token, ssh_profile, vcenter, name="uat-06")
        job = plan(client, admin_token, build["id"])["provisioning"]["job"]
        assert job["status"] == "planned", job.get("error")
        note = next(c for c in job["summary"]["checks"] if c["label"] == "Privileges not confirmed")
        assert note["status"] == "warn"
        assert "DeployTemplate" in note["detail"] and "Resource.AssignVMToPool" in note["detail"]

    def test_only_what_the_clone_cannot_do_without_stops_it(
        self, client, admin_token, ssh_profile, vcenter, engine
    ):
        self.account_without({
            "VirtualMachine.Provisioning.DeployTemplate", "VirtualMachine.Provisioning.Customize",
            "Network.Assign", "VirtualMachine.Config.AdvancedConfig",
        })
        build = make_vmware_build(client, admin_token, ssh_profile, vcenter, name="uat-04")
        job = plan(client, admin_token, build["id"])["provisioning"]["job"]
        assert job["status"] == "plan_failed"
        error = job["error"]
        assert "cannot clone VMs here" in error
        # Grouped by the object, nothing cut off, and only the blocking ones.
        assert "on VM template" in error and "DeployTemplate, VirtualMachine.Provisioning.Customize" in error
        assert "on network VM-Net-K8S-30: Network.Assign" in error
        assert "AdvancedConfig" not in error

    def test_sources_check_says_what_blocks(self, client, admin_token, vcenter):
        self.account_without({"Folder.Create", "VirtualMachine.Config.DiskExtend"})
        result = client.post(f"/api/vsphere-connections/{vcenter['connection'].id}/test-provisioning",
                             headers=auth_headers(admin_token)).get_json()["data"]
        assert result["status"] == "ok" and "Can create VMs" in result["message"]
        needs = {p["privilege"]: p["need"] for p in result["privileges"]}
        assert needs["Folder.Create"] == "adapts" and needs["Network.Assign"] == "required"
        assert needs["VirtualMachine.Interact.PowerOff"] == "destroy"
        # The clone repeats the template, so CPU/memory are only needed to resize;
        # delete only cleans up a failed clone or a destroyed build.
        assert needs["VirtualMachine.Config.CPUCount"] == "adapts"
        assert needs["VirtualMachine.Inventory.Delete"] == "cleanup"
        assert needs["VirtualMachine.Config.Settings"] == "other"

    def test_a_half_made_vm_left_in_vcenter_is_named(self):
        log = (
            "Error: warning:\nThere was an error performing post-clone changes to virtual machine "
            '"/AreebaDR/vm/Kubesight-VMs/test-vm-1": error reconfiguring virtual machine: '
            "ServerFaultCode: Permission to perform this operation was denied..\n"
            "Additionally, there was an error removing the cloned virtual machine: error destroying "
            "virtual machine: ServerFaultCode: Permission to perform this operation was denied.."
        )
        assert jobs._left_behind(log) == ["/AreebaDR/vm/Kubesight-VMs/test-vm-1"]
        assert jobs._left_behind("Error: something else") == []


class TestGrowAndDestroy:
    @pytest.fixture()
    def built(self, client, admin_token, ssh_profile, vcenter, engine, fake_ssh):
        hosts = dict(SMALL_HOSTS)
        hosts["10.20.30.55"] = ("uat-02-wk-3", "worker")
        fake = build_default_fake(hosts)
        fake.add(
            lambda h, s: "kubeadm token create --print-join-command" in s,
            "kubeadm join 10.20.30.50:6443 --token abcdef.0123456789abcdef "
            "--discovery-token-ca-cert-hash "
            "sha256:1111111111111111111111111111111111111111111111111111111111111111\n",
        )
        set_transport_factory(lambda: fake)
        build = make_vmware_build(client, admin_token, ssh_profile, vcenter)
        job = plan(client, admin_token, build["id"])["provisioning"]["job"]
        done = apply(client, admin_token, build["id"], job["id"])
        assert done["status"] == "completed", done.get("error")
        return done

    def test_add_a_worker(self, client, admin_token, built):
        response = client.post(
            f"/api/cluster-builds/{built['id']}/provision/grow-plan",
            json={"count": 1}, headers=auth_headers(admin_token),
        )
        assert response.status_code == 202, response.get_json()
        job = response.get_json()["data"]["provisioning"]["job"]
        assert job["operation"] == "grow" and job["status"] == "planned", job.get("error")
        assert (job["summary"]["add"], job["summary"]["change"], job["summary"]["destroy"]) == (1, 0, 0)
        assert job["summary"]["resources"][0]["title"] == "uat-02-wk-3"
        grown = apply(client, admin_token, built["id"], job["id"])
        assert grown["status"] == "completed", (grown.get("error"), grown["provisioning"]["job"])
        workers = [n for n in grown["nodes"] if n["role"] == "worker"]
        assert len(workers) == 3 and all(n["status"] == "joined" for n in workers)
        assert len(state_store.vm_instances(built["id"])) == 5

    def test_destroy_takes_a_second_person(self, client, app, admin_token, built):
        refused = client.post(
            f"/api/cluster-builds/{built['id']}/provision/destroy",
            json={"confirmName": "wrong"}, headers=auth_headers(admin_token),
        )
        assert refused.status_code == 400 and "Type the cluster name" in refused.get_json()["error"]

        response = client.post(
            f"/api/cluster-builds/{built['id']}/provision/destroy",
            json={"confirmName": "uat-02", "reason": "UAT moved"}, headers=auth_headers(admin_token),
        )
        assert response.status_code == 202, response.get_json()
        data = response.get_json()["data"]
        job = data["provisioning"]["job"]
        assert job["status"] == "awaiting_approval"
        assert data["provisionStatus"] == "destroy_pending"
        assert job["summary"]["destroy"] == 5 and job["summary"]["add"] == 0

        mine = client.post(
            f"/api/cluster-builds/{built['id']}/provision/jobs/{job['id']}/approve",
            headers=auth_headers(admin_token),
        )
        assert mine.status_code == 403
        assert "someone else" in mine.get_json()["error"]

        other = second_admin_token(client, app)
        approved = client.post(
            f"/api/cluster-builds/{built['id']}/provision/jobs/{job['id']}/approve",
            json={"note": "checked"}, headers=auth_headers(other),
        )
        assert approved.status_code == 200, approved.get_json()
        gone = approved.get_json()["data"]
        assert gone["status"] == "destroyed" and gone["provisionStatus"] == "destroyed"
        assert state_store.vm_instances(built["id"]) == {}
        assert VSphereIpReservation.query.filter_by(build_id=built["id"]).count() == 0
        cluster_id = int(built["resultClusterId"].split("-")[-1])
        assert Cluster.query.get(cluster_id).is_active is False

    def test_rejected_destroy_leaves_everything(self, client, app, admin_token, built):
        job = client.post(
            f"/api/cluster-builds/{built['id']}/provision/destroy",
            json={"confirmName": "uat-02"}, headers=auth_headers(admin_token),
        ).get_json()["data"]["provisioning"]["job"]
        other = second_admin_token(client, app)
        rejected = client.post(
            f"/api/cluster-builds/{built['id']}/provision/jobs/{job['id']}/reject",
            json={"note": "still in use"}, headers=auth_headers(other),
        )
        assert rejected.status_code == 200
        data = rejected.get_json()["data"]
        assert data["status"] == "completed" and data["provisionStatus"] == "ready"
        assert len(state_store.vm_instances(built["id"])) == 4


# ---------------------------------------------------------------------------
# Restart recovery
# ---------------------------------------------------------------------------

class TestRecovery:
    def test_interrupted_apply_is_finished_by_a_recovery_plan(
        self, client, admin_token, ssh_profile, vcenter, engine, fake_ssh, app
    ):
        fake = build_default_fake(SMALL_HOSTS)
        set_transport_factory(lambda: fake)
        build = make_vmware_build(client, admin_token, ssh_profile, vcenter)
        job_data = plan(client, admin_token, build["id"])["provisioning"]["job"]

        # Two VMs made it into the state, then the process died holding the lock.
        engine.fail_on = "uat-02-wk-1"
        apply(client, admin_token, build["id"], job_data["id"])
        engine.fail_on = None
        job = ClusterProvisionJob.query.get(job_data["id"])
        job.status = "applying"
        job.error = None
        job.updated_at = datetime.now(timezone.utc) - timedelta(minutes=10)
        build_row = ClusterBuild.query.get(build["id"])
        build_row.status = "provisioning"
        state_store.lock(build["id"], {"ID": "dead-lock"}, job_id=job.id)
        db.session.commit()

        jobs.advance_provision_jobs()

        db.session.expire_all()
        old = ClusterProvisionJob.query.get(job_data["id"])
        assert old.status == "interrupted"
        recovery = ClusterProvisionJob.query.filter_by(recovered_from_job_id=old.id).one()
        assert recovery.status == "succeeded", recovery.error
        assert ClusterInfraState.query.filter_by(build_id=build["id"]).first().lock_id is None
        assert len(state_store.vm_instances(build["id"])) == 4
        assert ClusterBuild.query.get(build["id"]).status == "completed"

    def test_stuck_plan_is_planned_again(self, client, admin_token, ssh_profile, vcenter, engine):
        build = make_vmware_build(client, admin_token, ssh_profile, vcenter)
        job_data = plan(client, admin_token, build["id"])["provisioning"]["job"]
        job = ClusterProvisionJob.query.get(job_data["id"])
        job.status = "planning"
        job.updated_at = datetime.now(timezone.utc) - timedelta(minutes=10)
        db.session.commit()
        jobs.advance_provision_jobs()
        db.session.expire_all()
        assert ClusterProvisionJob.query.get(job_data["id"]).status == "planned"


# ---------------------------------------------------------------------------
# OpenTofu's HTTP state backend
# ---------------------------------------------------------------------------

class TestStateBackend:
    @pytest.fixture()
    def running_job(self, app, client, admin_token, ssh_profile, vcenter, engine):
        build = make_vmware_build(client, admin_token, ssh_profile, vcenter)
        job = ClusterProvisionJob(build_id=build["id"], operation="create", status="applying")
        db.session.add(job)
        db.session.commit()
        token = jobs._issue_token(job)
        basic = base64.b64encode(f"job-{job.id}:{token}".encode()).decode()
        return {"build": build, "job": job, "headers": {"Authorization": f"Basic {basic}"}}

    def test_requires_the_job_credentials(self, client, running_job):
        url = f"/api/internal/tofu-state/{running_job['build']['id']}"
        assert client.get(url).status_code == 401
        wrong = base64.b64encode(f"job-{running_job['job'].id}:nope".encode()).decode()
        assert client.get(url, headers={"Authorization": f"Basic {wrong}"}).status_code == 401

    def test_only_from_inside_the_pod(self, client, running_job):
        url = f"/api/internal/tofu-state/{running_job['build']['id']}"
        response = client.get(url, headers=running_job["headers"],
                              environ_base={"REMOTE_ADDR": "10.1.2.3"})
        assert response.status_code == 403

    def test_lock_save_read_unlock(self, client, running_job):
        url = f"/api/internal/tofu-state/{running_job['build']['id']}"
        headers = running_job["headers"]
        assert client.get(url, headers=headers).status_code == 204
        assert client.post(f"{url}/lock", headers=headers, json={"ID": "L1", "Operation": "Apply"}).status_code == 200
        busy = client.post(f"{url}/lock", headers=headers, json={"ID": "L2"})
        assert busy.status_code == 423 and busy.get_json()["ID"] == "L1"
        state = json.dumps({"version": 4, "serial": 3, "lineage": "abc", "resources": []})
        assert client.post(f"{url}?ID=L2", headers=headers, data=state).status_code == 423
        assert client.post(f"{url}?ID=L1", headers=headers, data=state).status_code == 200
        assert json.loads(client.get(url, headers=headers).get_data(as_text=True))["serial"] == 3
        row = ClusterInfraState.query.filter_by(build_id=running_job["build"]["id"]).first()
        assert row.lock_job_id == running_job["job"].id
        assert "abc" not in (row.state_cipher or "")  # encrypted at rest
        assert client.post(f"{url}/unlock", headers=headers, json={"ID": "L1"}).status_code == 200
        assert ClusterInfraState.query.filter_by(build_id=running_job["build"]["id"]).first().lock_id is None

    def test_finished_jobs_lose_access(self, client, running_job):
        job = running_job["job"]
        job.status = "succeeded"
        db.session.commit()
        url = f"/api/internal/tofu-state/{running_job['build']['id']}"
        assert client.get(url, headers=running_job["headers"]).status_code == 401


class TestVCenterAccounts:
    def test_provisioning_account_must_differ_from_browsing(self, client, admin_token, vcenter):
        response = client.put(
            f"/api/vsphere-connections/{vcenter['connection'].id}",
            json={"provisioningUsername": "RO@vsphere.local"},
            headers=auth_headers(admin_token),
        )
        assert response.status_code == 400
        assert "different account" in response.get_json()["error"]

    def test_password_is_never_returned(self, client, admin_token, vcenter):
        data = client.get("/api/vsphere-connections", headers=auth_headers(admin_token)).get_json()["data"]
        row = data["items"][0] if isinstance(data, dict) else data[0]
        assert row["provisioningConfigured"] is True
        assert "prov-pass" not in json.dumps(data)

    def test_a_vcenter_fault_after_login_is_a_message_not_a_500(self, client, admin_token, vcenter, monkeypatch):
        class NoPermission(Exception):
            msg = "Permission to perform this operation was denied."

        def denied(cfg):
            raise NoPermission()

        inventory.set_placement_fetcher(denied)
        url = f"/api/vsphere-connections/{vcenter['connection'].id}"
        checked = client.post(f"{url}/test-provisioning", headers=auth_headers(admin_token))
        assert checked.status_code == 200, checked.get_json()
        result = checked.get_json()["data"]
        assert result["status"] == "failed"
        assert "NoPermission" in result["error"] and "denied" in result["error"]
        placed = client.get(f"{url}/placement?refresh=1", headers=auth_headers(admin_token))
        assert placed.status_code == 502 and "NoPermission" in placed.get_json()["error"]

        inventory.set_placement_fetcher(lambda cfg: inventory.demo_placement(cfg))
        inventory.set_privilege_checker(None)

        def boom(cfg, ids):
            raise AttributeError("'NoneType' object has no attribute 'privAvailability'")

        monkeypatch.setattr(inventory, "_check_privileges", boom)
        checked = client.post(f"{url}/test-provisioning", headers=auth_headers(admin_token))
        assert checked.status_code == 200
        assert "privilege check failed (AttributeError)" in checked.get_json()["data"]["error"]

    def test_privileges_read_from_roles_when_vcenter_refuses_to_say(self, monkeypatch):
        """An account whose role sits on a datacenter, not the vCenter root, may
        not call HasPrivilegeOnEntities (System.View at the root). Its roles on
        the entity still say what it holds."""
        from pyVmomi import vim

        from api.services.vsphere_client import VSphereConfig

        roles = [
            vim.AuthorizationManager.Role(roleId=-2, name="View", privilege=["System.View"]),
            vim.AuthorizationManager.Role(
                roleId=501, name="Kubesight-RW",
                privilege=[p for p, _, _ in inventory.REQUIRED_PRIVILEGES if p != "Folder.Create"],
            ),
        ]

        class Stub:
            def InvokeAccessor(self, mo, info):
                assert info.name == "effectiveRole"
                return [501]

        class Manager:
            roleList = roles

            def HasPrivilegeOnEntities(self, **kwargs):
                raise vim.fault.NoPermission(
                    privilegeId="System.View", object=vim.Folder("group-d1", None),
                )

        class Content:
            authorizationManager = Manager()

            class sessionManager:
                class currentSession:
                    key = "session-1"

        class Si:
            _stub = Stub()

            def RetrieveContent(self):
                return Content()

        monkeypatch.setattr(inventory, "_connect", lambda cfg: Si())
        monkeypatch.setattr(inventory, "_disconnect", lambda si: None)
        cfg = VSphereConfig(base_url="https://vc.example.test", username="u", password="p")
        result = inventory._check_privileges(cfg, {"datacenter": "datacenter-3"})
        assert len(result) == len(inventory.REQUIRED_PRIVILEGES)
        assert [r["privilege"] for r in result if not r["granted"]] == ["Folder.Create"]

        Manager.roleList = property(lambda self: (_ for _ in ()).throw(
            vim.fault.NoPermission(privilegeId="System.View", object=vim.Folder("group-d1", None))
        ))
        with pytest.raises(Exception) as refused:
            inventory._check_privileges(cfg, {"datacenter": "datacenter-3"})
        assert "System.View on group-d1" in str(refused.value)
        assert "Read-only role at the top of the vCenter" in str(refused.value)

    def test_placement_lists_templates_with_verdicts(self, client, admin_token, vcenter):
        data = client.get(
            f"/api/vsphere-connections/{vcenter['connection'].id}/placement",
            headers=auth_headers(admin_token),
        ).get_json()["data"]
        dc = data["datacenters"][0]
        verdicts = {t["name"]: t["compatibility"]["status"] for t in dc["templates"]}
        assert verdicts["ubuntu-22.04-k8s-base"] == "ok"
        assert verdicts["win-2022-std"] == "bad"
        assert data["networks"][0]["networkName"] == "VM-Net-K8S-30"
