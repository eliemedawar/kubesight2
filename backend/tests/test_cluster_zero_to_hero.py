"""Zero to hero: one cluster's whole life, end to end, on both machine sources.

    build Small (1 balancer, 1 control plane, 1 worker) with NGINX Ingress
      → add MetalLB to the running cluster
      → add a second balancer, two control planes and a worker in one run
      → add Metrics Server to the now highly available cluster

Once with machines that already exist, once with VMs OpenTofu creates (the
simulated engine). The SSH transport is the Cluster Builder suite's fake, so
every command each phase sends is recorded and asserted on — which balancer
was reloaded rather than restarted, which control planes joined, in what order.
"""

from __future__ import annotations

import base64
import re

import pytest

from api.db import db
from api.models import ClusterBuild, ClusterBuildStep
from api.services.cluster_build.provisioning import state_store
from api.services.ssh import set_transport_factory

from tests.test_cluster_builds import (
    auth_headers,
    build_default_fake,
    make_build_payload,
    probe_output,
    run_full_build,
)
from tests.test_cluster_builder_addons_proxy import (  # noqa: F401  (fixtures by name)
    METALLB_SELECTION,
    add_addon_responders,
    pinned_manifests_without_network,
    reset_ssh_transport,
    ssh_profile,
)
from tests.test_cluster_provisioning import (  # noqa: F401  (fixtures by name)
    apply,
    engine,
    make_vmware_build,
    placement_payload,
    plan,
    vcenter,
)

CERT_KEY = "ab" * 32
JOIN = (
    "kubeadm join 10.0.0.100:6443 --token abcdef.0123456789abcdef "
    "--discovery-token-ca-cert-hash "
    "sha256:1111111111111111111111111111111111111111111111111111111111111111\n"
)


@pytest.fixture(autouse=True)
def _cluster_needs_no_approval(no_cluster_approvals):
    """Day-two changes are held to the cluster's approval rule elsewhere
    (test_cluster_approval_gate); this file is about the changes themselves."""
    yield


def lifecycle_fake(hosts, *, new_balancers=()):
    """A cluster that answers every phase, now and after it grows.

    A balancer joining a live cluster sees the VIP already taken (by the
    running balancer) — that is the healthy answer on day two.
    """
    fake = add_addon_responders(build_default_fake(hosts))
    for address in new_balancers:
        hostname = hosts[address][0]
        output = probe_output(hostname, address.replace(".", ""), "loadbalancer")
        fake.responders.insert(0, (
            lambda h, s, a=address: h == a and "preflight probe" in s,
            output.replace("KS_VIP_STATE=free", "KS_VIP_STATE=in_use"),
        ))
    fake.add(lambda h, s: "kubeadm token create --print-join-command" in s, JOIN)
    # Ahead of the generic "kubeadm init" responder, which would match too.
    fake.responders.insert(0, (
        lambda h, s: "kubeadm init phase upload-certs" in s,
        f"[upload-certs] Using certificate key:\n{CERT_KEY}\n",
    ))
    fake.responders.insert(0, (
        lambda h, s: "serverTLSBootstrap" in s and "grep -Eq" in s, "KS_CHANGED=1\n",
    ))
    # Metrics Server's proof waits until every Kubernetes node reports usage.
    fake.responders.insert(0, (
        lambda h, s: "top nodes --no-headers" in s,
        "\n".join(f"{name} 100m 5% 512Mi 10%" for name, role in hosts.values()
                  if role != "loadbalancer") + "\n",
    ))
    return fake


def decoded(script, path):
    """The file a script writes to ``path`` with ``echo <b64> | base64 -d > path``."""
    match = re.search(rf"echo ([A-Za-z0-9+/=]+) \| base64 -d > {re.escape(path)}", script)
    return base64.b64decode(match.group(1)).decode() if match else ""


def add_addons(client, token, build_id, addons):
    response = client.post(
        f"/api/cluster-builds/{build_id}/addons", json={"addons": addons},
        headers=auth_headers(token),
    )
    assert response.status_code == 200, response.get_json()
    data = response.get_json()["data"]
    assert data["status"] == "completed", data.get("error")
    return data


def grow_existing(client, token, build_id, nodes):
    headers = auth_headers(token)
    response = client.post(f"/api/cluster-builds/{build_id}/nodes", json={"nodes": nodes}, headers=headers)
    assert response.status_code == 201, response.get_json()
    response = client.post(f"/api/cluster-builds/{build_id}/grow-preflight", headers=headers)
    assert response.status_code == 200, response.get_json()
    verdict = response.get_json()["data"]
    assert verdict["status"] in ("pass", "warn"), verdict
    response = client.post(
        f"/api/cluster-builds/{build_id}/grow", json={"ackWarnings": ["ack"]}, headers=headers,
    )
    assert response.status_code == 200, response.get_json()
    data = response.get_json()["data"]
    assert data["status"] == "completed", data.get("error")
    return data


def assert_ha_tier(calls, *, balancers, control_planes, master, new_balancer):
    """What growing the balancer and control-plane tiers must have done."""
    scripts = [(host, s) for host, s in calls]
    applies = [(host, s) for host, s in scripts if "/etc/haproxy/haproxy.cfg" in s]
    assert {host for host, _ in applies} == set(balancers)
    for host, script in applies:
        # Live cluster: reloaded in place, never restarted.
        assert "systemctl reload haproxy" in script
        assert "systemctl reload keepalived" in script
        assert "systemctl restart" not in script
        haproxy = decoded(script, "/etc/haproxy/haproxy.cfg")
        for address in control_planes:
            assert f"{address}:6443 check" in haproxy
        keepalived = decoded(script, "/etc/keepalived/keepalived.conf")
        peers = set(balancers) - {host}
        for peer in peers:
            assert peer in keepalived
        assert ("state MASTER" in keepalived) == (host == master)
    assert new_balancer in {host for host, _ in applies}

    joins = [(host, s) for host, s in scripts if "--control-plane --certificate-key" in s]
    joined = [host for host, _ in joins]
    # etcd was snapshotted on the first control plane, once, before any joined.
    snapshots = [i for i, (host, s) in enumerate(scripts) if "etcdctl" in s and "snapshot save" in s]
    assert len(snapshots) == 1
    assert scripts[snapshots[0]][0] == control_planes[0]
    first_join = next(i for i, (_, s) in enumerate(scripts) if "--control-plane --certificate-key" in s)
    assert snapshots[0] < first_join
    assert joined == sorted(joined) and set(joined) == set(control_planes[1:])
    assert all(CERT_KEY in s for _, s in joins)
    # The secrets were re-minted: the ones from the original build are gone.
    assert any("kubeadm init phase upload-certs" in s for _, s in scripts)
    # Nothing was re-initialised or reset on the running cluster.
    assert not any("kubeadm init --config" in s for _, s in scripts)
    assert not any("kubeadm reset" in s for _, s in scripts)


# ---------------------------------------------------------------------------
# Machines that already exist
# ---------------------------------------------------------------------------

EXISTING_HOSTS = {
    "10.0.0.5": ("lb-1", "loadbalancer"),
    "10.0.0.6": ("lb-2", "loadbalancer"),
    "10.0.0.11": ("cp-1", "control_plane"),
    "10.0.0.12": ("cp-2", "control_plane"),
    "10.0.0.13": ("cp-3", "control_plane"),
    "10.0.0.21": ("w-1", "worker"),
    "10.0.0.22": ("w-2", "worker"),
}
SMALL_NODES = [
    {"role": "loadbalancer", "hostname": "lb-1", "address": "10.0.0.5"},
    {"role": "control_plane", "hostname": "cp-1", "address": "10.0.0.11"},
    {"role": "worker", "hostname": "w-1", "address": "10.0.0.21"},
]


class TestExistingMachinesZeroToHero:
    def test_the_whole_life(self, client, admin_token, ssh_profile, app):
        fake = lifecycle_fake(EXISTING_HOSTS, new_balancers=("10.0.0.6",))
        set_transport_factory(lambda: fake)

        # 1. Build Small with NGINX Ingress.
        data = run_full_build(client, admin_token, ssh_profile, fake, make_build_payload(
            name="hero", topology="single_cp", endpoint_mode="managed_haproxy",
            vipAddress="10.0.0.100", nodes=SMALL_NODES, templateId="small",
            addons=[{"id": "nginx-ingress", "version": "5.5.4"}],
        ))
        assert data["status"] == "completed", data.get("error")
        build_id = data["id"]
        # A first build has nothing to protect: no snapshot, no backup step.
        assert not any("snapshot save" in s for _, s in fake.calls)
        assert data["etcdBackups"] == []
        assert data["growthLimits"]["controlPlane"]["allowed"] is True
        assert data["growthLimits"]["loadbalancer"]["max"] == 1

        # 2. Add MetalLB to the running cluster.
        mark = len(fake.calls)
        data = add_addons(client, admin_token, build_id, [METALLB_SELECTION])
        assert {a["id"]: bool(a.get("installedAt")) for a in data["addons"]} == {
            "nginx-ingress": True, "metallb": True,
        }
        assert not any("haproxy.cfg" in s for _, s in fake.calls[mark:])

        # 3. A second balancer, two control planes and a worker, in one run.
        mark = len(fake.calls)
        data = grow_existing(client, admin_token, build_id, [
            {"role": "loadbalancer", "hostname": "lb-2", "address": "10.0.0.6"},
            {"role": "control_plane", "hostname": "cp-2", "address": "10.0.0.12"},
            {"role": "control_plane", "hostname": "cp-3", "address": "10.0.0.13"},
            {"role": "worker", "hostname": "w-2", "address": "10.0.0.22"},
        ])
        assert data["topologyType"] == "stacked_ha"
        assert data["nodeCounts"] == {"controlPlane": 3, "worker": 2, "loadbalancer": 2}
        statuses = {n["hostname"]: n["status"] for n in data["nodes"]}
        for name in ("cp-1", "cp-2", "cp-3", "w-1", "w-2"):
            assert statuses[name] == "joined", statuses
        assert_ha_tier(
            fake.calls[mark:],
            balancers=["10.0.0.5", "10.0.0.6"],
            control_planes=["10.0.0.11", "10.0.0.12", "10.0.0.13"],
            master="10.0.0.5", new_balancer="10.0.0.6",
        )
        grown = fake.calls[mark:]
        worker_joins = [h for h, s in grown if "kubeadm join" in s and "--control-plane" not in s]
        assert worker_joins == ["10.0.0.22"]
        # Only the new machines were prepared; the running ones were not touched.
        prepared = {h for h, s in grown if "base_prep" in s or "containerd" in s.lower()}
        assert prepared <= {"10.0.0.6", "10.0.0.12", "10.0.0.13", "10.0.0.22"}
        # Add-ons already installed were not applied again.
        assert not any("kubesight-addon-metallb" in s for _, s in grown)
        assert data["growthLimits"]["loadbalancer"]["allowed"] is False
        assert data["growthLimits"]["controlPlane"]["allowed"] is True  # 3 → 5 later
        [backup] = data["etcdBackups"]
        assert backup["path"] == "/var/backups/kubesight/etcd/etcd-20261006T101500Z.db"
        assert backup["node"] == "cp-1" and backup["address"] == "10.0.0.11"
        assert backup["bytes"] == 4210688 and len(backup["sha256"]) == 64
        assert backup["reason"] == "before cp-2, cp-3 joined"

        # 4. Metrics Server on the highly available cluster.
        mark = len(fake.calls)
        data = add_addons(client, admin_token, build_id, [{"id": "metrics-server", "version": "0.7.2"}])
        enabled = {h for h, s in fake.calls[mark:] if "serverTLSBootstrap: true" in s}
        assert enabled == {"10.0.0.11", "10.0.0.12", "10.0.0.13", "10.0.0.21", "10.0.0.22"}
        assert len({a["id"] for a in data["addons"]}) == 3

    def test_workers_alone_take_no_snapshot(self, client, admin_token, ssh_profile, app):
        fake = lifecycle_fake(EXISTING_HOSTS)
        set_transport_factory(lambda: fake)
        data = run_full_build(client, admin_token, ssh_profile, fake, make_build_payload(
            name="hero-w", topology="single_cp", endpoint_mode="managed_haproxy",
            vipAddress="10.0.0.100", nodes=SMALL_NODES, templateId="small",
        ))
        mark = len(fake.calls)
        data = grow_existing(client, admin_token, data["id"], [
            {"role": "worker", "hostname": "w-2", "address": "10.0.0.22"},
        ])
        assert not any("snapshot save" in s for _, s in fake.calls[mark:])
        assert data["etcdBackups"] == []
        assert not [s for s in data["steps"] if s["phase"] == "etcd_backup"]

    def test_a_failed_snapshot_adds_no_control_plane(self, client, admin_token, ssh_profile, app):
        from api.services.ssh import SshCommandError

        fake = lifecycle_fake(EXISTING_HOSTS, new_balancers=("10.0.0.6",))
        set_transport_factory(lambda: fake)
        data = run_full_build(client, admin_token, ssh_profile, fake, make_build_payload(
            name="hero-f", topology="single_cp", endpoint_mode="managed_haproxy",
            vipAddress="10.0.0.100", nodes=SMALL_NODES, templateId="small",
        ))
        build_id = data["id"]
        failure = (
            lambda h, s: "snapshot save" in s,
            SshCommandError("etcdctl snapshot save", 1, "Error: context deadline exceeded"),
        )
        fake.responders.insert(0, failure)
        headers = auth_headers(admin_token)
        response = client.post(f"/api/cluster-builds/{build_id}/nodes", json={"nodes": [
            {"role": "control_plane", "hostname": "cp-2", "address": "10.0.0.12"},
            {"role": "control_plane", "hostname": "cp-3", "address": "10.0.0.13"},
        ]}, headers=headers)
        assert response.status_code == 201, response.get_json()
        assert client.post(f"/api/cluster-builds/{build_id}/grow-preflight", headers=headers).status_code == 200
        mark = len(fake.calls)
        data = client.post(
            f"/api/cluster-builds/{build_id}/grow", json={"ackWarnings": ["ack"]}, headers=headers,
        ).get_json()["data"]
        assert data["status"] == "failed"
        assert "no control plane was added" in data["error"]
        assert not any("--control-plane" in s for _, s in fake.calls[mark:])
        [step] = [s for s in data["steps"] if s["phase"] == "etcd_backup"]
        assert step["status"] == "failed"
        assert "context deadline exceeded" in db.session.get(ClusterBuildStep, step["id"]).log_tail
        assert data["etcdBackups"] == []
        statuses = {n["hostname"]: n["status"] for n in data["nodes"]}
        assert statuses["cp-1"] == "joined" and statuses["cp-2"] != "joined"

        # Fixed on the node; the retry takes the snapshot, then the joins run.
        fake.responders.remove(failure)
        response = client.post(f"/api/cluster-builds/{build_id}/retry", headers=headers)
        assert response.status_code == 200, response.get_json()
        data = client.get(f"/api/cluster-builds/{build_id}", headers=headers).get_json()["data"]
        assert data["status"] == "completed", data.get("error")
        assert len(data["etcdBackups"]) == 1
        assert data["nodeCounts"]["controlPlane"] == 3

    def test_balancer_and_control_plane_rules(self, client, admin_token, ssh_profile, app):
        fake = lifecycle_fake(EXISTING_HOSTS, new_balancers=("10.0.0.6",))
        set_transport_factory(lambda: fake)
        data = run_full_build(client, admin_token, ssh_profile, fake, make_build_payload(
            name="rules", topology="single_cp", endpoint_mode="managed_haproxy",
            vipAddress="10.0.0.100", nodes=SMALL_NODES,
        ))
        build_id = data["id"]
        headers = auth_headers(admin_token)
        too_many = client.post(f"/api/cluster-builds/{build_id}/nodes", json={"nodes": [
            {"role": "loadbalancer", "hostname": "lb-2", "address": "10.0.0.6"},
            {"role": "loadbalancer", "hostname": "lb-3", "address": "10.0.0.7"},
        ]}, headers=headers)
        assert too_many.status_code == 400
        assert "Two load balancers" in too_many.get_json()["error"]

        six = client.post(f"/api/cluster-builds/{build_id}/nodes", json={"nodes": [
            {"role": "control_plane", "hostname": f"cp-{i}", "address": f"10.0.0.{10 + i}"}
            for i in range(2, 7)
        ]}, headers=headers)
        assert six.status_code == 400 and "Five control planes" in six.get_json()["error"]

        # A balancer alone (1 → 2) is a valid shape on its own.
        data = grow_existing(client, admin_token, build_id, [
            {"role": "loadbalancer", "hostname": "lb-2", "address": "10.0.0.6"},
        ])
        lbs = [n for n in data["nodes"] if n["role"] == "loadbalancer"]
        assert len(lbs) == 2
        master = next(n for n in lbs if n["isLbMaster"])
        assert master["address"] == "10.0.0.5"  # the running master keeps the VIP

    def test_a_lab_cannot_grow_its_control_plane(self, client, admin_token, ssh_profile, app):
        hosts = {"10.0.0.11": ("cp-1", "control_plane"), "10.0.0.21": ("w-1", "worker")}
        fake = lifecycle_fake(hosts)
        set_transport_factory(lambda: fake)
        data = run_full_build(client, admin_token, ssh_profile, fake, make_build_payload(
            name="lab", nodes=[
                {"role": "control_plane", "hostname": "cp-1", "address": "10.0.0.11"},
                {"role": "worker", "hostname": "w-1", "address": "10.0.0.21"},
            ], controlPlaneEndpoint="10.0.0.11:6443",
        ))
        assert data["growthLimits"]["controlPlane"]["allowed"] is False
        response = client.post(f"/api/cluster-builds/{data['id']}/nodes", json={"nodes": [
            {"role": "control_plane", "hostname": "cp-2", "address": "10.0.0.12"},
            {"role": "control_plane", "hostname": "cp-3", "address": "10.0.0.13"},
        ]}, headers=auth_headers(admin_token))
        assert response.status_code == 400
        assert "control plane's own address" in response.get_json()["error"]

    def test_an_external_balancer_grows_with_a_warning(self, client, admin_token, ssh_profile, app):
        fake = lifecycle_fake(EXISTING_HOSTS)
        set_transport_factory(lambda: fake)
        data = run_full_build(client, admin_token, ssh_profile, fake, make_build_payload(
            name="ext", topology="single_cp", endpoint_mode="external_lb",
            controlPlaneEndpoint="api.example.com:6443", nodes=SMALL_NODES[1:],
        ))
        headers = auth_headers(admin_token)
        client.post(f"/api/cluster-builds/{data['id']}/nodes", json={"nodes": [
            {"role": "control_plane", "hostname": "cp-2", "address": "10.0.0.12"},
            {"role": "control_plane", "hostname": "cp-3", "address": "10.0.0.13"},
        ]}, headers=headers)
        verdict = client.post(f"/api/cluster-builds/{data['id']}/grow-preflight", headers=headers)
        warnings = verdict.get_json()["data"]["topologyWarnings"]
        assert any("api.example.com:6443" in w and "yourself" in w for w in warnings)


# ---------------------------------------------------------------------------
# VMs KubeSight creates (simulated OpenTofu)
# ---------------------------------------------------------------------------

VM_HOSTS = {
    "10.20.30.51": ("hero-vm-lb-1", "loadbalancer"),
    "10.20.30.52": ("hero-vm-cp-1", "control_plane"),
    "10.20.30.53": ("hero-vm-wk-1", "worker"),
    "10.20.30.54": ("hero-vm-lb-2", "loadbalancer"),
    "10.20.30.55": ("hero-vm-cp-2", "control_plane"),
    "10.20.30.56": ("hero-vm-cp-3", "control_plane"),
    "10.20.30.57": ("hero-vm-wk-2", "worker"),
}


class TestVmwareZeroToHero:
    def test_the_whole_life(self, client, admin_token, ssh_profile, vcenter, engine, app):
        fake = lifecycle_fake(VM_HOSTS, new_balancers=("10.20.30.54",))
        set_transport_factory(lambda: fake)
        headers = auth_headers(admin_token)

        # 1. Small, created by OpenTofu, with NGINX Ingress.
        response = client.post("/api/cluster-builds", json={
            "name": "hero-vm", "k8sVersion": "1.32.4", "machineSource": "vmware",
            "templateId": "small", "cniPlugin": "calico",
            "podCidr": "10.244.0.0/16", "serviceCidr": "10.96.0.0/12",
            "connectionProfileId": ssh_profile["id"],
            "addons": [{"id": "nginx-ingress", "version": "5.5.4"}],
            "provisioning": placement_payload(
                vcenter["connection"].id,
                counts={"loadbalancer": 1, "controlPlane": 1, "worker": 1},
            ),
        }, headers=headers)
        assert response.status_code == 201, response.get_json()
        build_id = response.get_json()["data"]["id"]
        job = plan(client, admin_token, build_id)["provisioning"]["job"]
        assert job["status"] == "planned", job.get("error")
        data = apply(client, admin_token, build_id, job["id"])
        assert data["status"] == "completed", (data.get("error"), data["provisioning"]["job"])
        assert data["vipAddress"] == "10.20.30.50"
        assert len(state_store.vm_instances(build_id)) == 3

        # 2. MetalLB on the running cluster.
        add_addons(client, admin_token, build_id, [METALLB_SELECTION])

        # 3. OpenTofu creates a balancer, two control planes and a worker.
        mark = len(fake.calls)
        response = client.post(
            f"/api/cluster-builds/{build_id}/provision/grow-plan",
            json={"workers": 1, "controlPlanes": 2, "loadBalancers": 1}, headers=headers,
        )
        assert response.status_code == 202, response.get_json()
        job = response.get_json()["data"]["provisioning"]["job"]
        assert job["status"] == "planned", job.get("error")
        summary = job["summary"]
        assert summary["blocked"] is None
        created = {r["title"] for r in summary["resources"] if r["kind"] == "vm"}
        assert created == {"hero-vm-lb-2", "hero-vm-cp-2", "hero-vm-cp-3", "hero-vm-wk-2"}
        rules = [r for r in summary["resources"] if r["kind"] == "rule"]
        assert {r["action"] for r in rules} <= {"create", "update"} and len(rules) == 2
        assert not [r for r in summary["resources"] if r["kind"] == "vm" and r["action"] != "create"]

        data = apply(client, admin_token, build_id, job["id"])
        assert data["status"] == "completed", (data.get("error"), data["provisioning"]["job"])
        handoff = (data["provisioning"]["job"]["progress"] or {}).get("handoffNote")
        assert handoff is None, (handoff, [
            (n["hostname"], c["label"], c.get("detail"))
            for n in data["nodes"] for c in (n.get("preflight") or {}).get("checks", [])
            if c["status"] != "pass"
        ])
        assert data["provisioning"]["spec"]["counts"] == {"loadbalancer": 2, "controlPlane": 3, "worker": 2}
        assert data["nodeCounts"] == {"controlPlane": 3, "worker": 2, "loadbalancer": 2}
        assert data["topologyType"] == "stacked_ha"
        assert len(state_store.vm_instances(build_id)) == 7
        assert_ha_tier(
            fake.calls[mark:],
            balancers=["10.20.30.51", "10.20.30.54"],
            control_planes=["10.20.30.52", "10.20.30.55", "10.20.30.56"],
            master="10.20.30.51", new_balancer="10.20.30.54",
        )

        assert len(data["etcdBackups"]) == 1 and data["etcdBackups"][0]["node"] == "hero-vm-cp-1"

        # 4. Metrics Server on the highly available cluster.
        data = add_addons(client, admin_token, build_id, [{"id": "metrics-server", "version": "0.7.2"}])
        assert len(data["addons"]) == 3

    def test_lab_vms_cannot_grow_their_control_plane(
        self, client, admin_token, ssh_profile, vcenter, engine, app
    ):
        fake = lifecycle_fake({
            "10.20.30.50": ("lab-vm-cp-1", "control_plane"),
            "10.20.30.51": ("lab-vm-wk-1", "worker"),
        })
        set_transport_factory(lambda: fake)
        build = make_vmware_build(
            client, admin_token, ssh_profile, vcenter, name="lab-vm",
            counts={"loadbalancer": 0, "controlPlane": 1, "worker": 1},
        )
        job = plan(client, admin_token, build["id"])["provisioning"]["job"]
        data = apply(client, admin_token, build["id"], job["id"])
        assert data["status"] == "completed", data.get("error")
        headers = auth_headers(admin_token)
        for body, needle in (
            ({"controlPlanes": 2}, "control plane's own address"),
            ({"loadBalancers": 1}, "HAProxy and keepalived"),
        ):
            response = client.post(
                f"/api/cluster-builds/{build['id']}/provision/grow-plan", json=body, headers=headers,
            )
            assert response.status_code == 400, response.get_json()
            assert needle in response.get_json()["error"]
        # Workers still grow, OpenTofu or not.
        response = client.post(
            f"/api/cluster-builds/{build['id']}/provision/grow-plan", json={"workers": 1}, headers=headers,
        )
        assert response.status_code == 202, response.get_json()


class TestPlanAcknowledgedWarnings:
    """The handoff acknowledges only what the approved plan itself chose."""

    def _result(self, *checks):
        return {"status": "warn", "nodes": [{"nodeId": 1, "checks": list(checks)}]}

    def test_shared_datastore_is_the_plans_own_choice(self):
        from api.services.cluster_build.provisioning import jobs
        from api.models import ClusterProvisionJob

        ack = jobs._plan_acknowledges(self._result(
            {"id": "vs_cp_datastore", "label": "Control-plane datastore diversity", "status": "warn"},
            {"id": "swap", "label": "Swap off", "status": "pass"},
        ), ClusterProvisionJob(id=7))
        assert ack == ["Placement chosen in OpenTofu plan #7: Control-plane datastore diversity"]

    def test_any_other_warning_waits_for_a_person(self):
        from api.services.cluster_build.provisioning import jobs
        from api.models import ClusterProvisionJob

        assert jobs._plan_acknowledges(self._result(
            {"id": "vs_cp_datastore", "label": "Control-plane datastore diversity", "status": "warn"},
            {"id": "fsync", "label": "etcd disk fsync latency", "status": "warn"},
        ), ClusterProvisionJob(id=7)) is None
        assert jobs._plan_acknowledges({"status": "fail", "nodes": []}, ClusterProvisionJob(id=7)) is None
