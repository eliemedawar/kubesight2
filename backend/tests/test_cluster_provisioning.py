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
            {"privilege": "Folder.Create", "purpose": "", "entity": "x", "granted": False},
        ])
        build = make_vmware_build(client, admin_token, ssh_profile, vcenter)
        job = plan(client, admin_token, build["id"])["provisioning"]["job"]
        assert job["status"] == "plan_failed"
        assert "Folder.Create" in job["error"]

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
    """Create the VMs and stop: the VM side tested on its own, Kubernetes later or never."""

    @pytest.fixture()
    def ready(self, client, admin_token, ssh_profile, vcenter, engine):
        build = make_vmware_build(client, admin_token, ssh_profile, vcenter)
        response = client.put(
            f"/api/cluster-builds/{build['id']}", json={"vmsOnly": True},
            headers=auth_headers(admin_token),
        )
        assert response.status_code == 200 and response.get_json()["data"]["vmsOnly"] is True
        job = plan(client, admin_token, build["id"])["provisioning"]["job"]
        assert job["status"] == "planned", job.get("error")
        # No SSH transport is installed: anything past "the VMs answer" would fail.
        done = apply(client, admin_token, build["id"], job["id"])
        return done

    def test_stops_once_the_vms_answer(self, client, admin_token, ready):
        assert ready["status"] == "vms_ready", (ready.get("error"), ready["provisioning"]["job"])
        assert ready["provisionStatus"] == "ready"
        assert ready["provisioning"]["job"]["status"] == "succeeded"
        assert not ready["resultClusterId"] and not ready["steps"]
        assert len(state_store.vm_instances(ready["id"])) == 4
        assert {v["state"] for v in ready["provisioning"]["job"]["progress"]["vms"].values()} == {"ready"}
        assert ready["canDestroy"] is True

        for path in ("preflight", "start"):
            refused = client.post(f"/api/cluster-builds/{ready['id']}/{path}", headers=auth_headers(admin_token))
            assert refused.status_code == 400 and "VMs only" in refused.get_json()["error"], path
        deleted = client.delete(f"/api/cluster-builds/{ready['id']}", headers=auth_headers(admin_token))
        assert deleted.status_code == 400

    def test_install_kubernetes_later(self, client, admin_token, ready, fake_ssh):
        set_transport_factory(lambda: build_default_fake(SMALL_HOSTS))
        response = client.post(
            f"/api/cluster-builds/{ready['id']}/provision/install-kubernetes",
            headers=auth_headers(admin_token),
        )
        assert response.status_code == 200, response.get_json()
        built = response.get_json()["data"]
        assert built["vmsOnly"] is False
        assert built["status"] == "completed", (built.get("error"), built["provisioning"]["job"])
        assert built["resultClusterId"]
        assert all(n["status"] == "joined" for n in built["nodes"] if n["role"] != "loadbalancer")
        assert len(state_store.vm_instances(ready["id"])) == 4  # the same VMs, none created

        again = client.post(
            f"/api/cluster-builds/{ready['id']}/provision/install-kubernetes",
            headers=auth_headers(admin_token),
        )
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
