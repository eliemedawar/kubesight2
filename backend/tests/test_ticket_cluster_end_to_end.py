"""One ticket, end to end: VMs → Kubernetes from a Satellite capsule → add-ons → Helm app.

Everything the production run exercises at once: a Hermes VM request on a
profile with a Satellite source profile, container data on /data, a default
and a requested add-on and an app profile; a VM approver's approval; OpenTofu
creating the VMs; preflight with warnings only (which must not hold the
install); node prep registering with Satellite and settling on the version the
capsule carries; a worker that registers after kubeadm stopped waiting; and the
one follow-up the ticket gets, saying what was installed.
"""

from __future__ import annotations

import time

import pytest

from api.db import db
from api.models import ClusterBuild
from api.services.cluster_build import executor
from api.services.ssh import SshCommandError, set_transport_factory
from api.services.ticket_agent import engine, vm_requests

from tests.test_cluster_builds import (  # noqa: F401  (fixtures are used by name)
    auth_headers,
    build_default_fake,
    fake_ssh,
    no_network_cni_manifest,
    ssh_profile,
)
from tests.test_cluster_builder_addons_proxy import (  # noqa: F401
    add_addon_responders,
    pinned_manifests_without_network,
    reset_ssh_transport,
)
from tests.test_cluster_builder_apps import PASSWORD, chart, helm  # noqa: F401
from tests.test_cluster_provisioning import (  # noqa: F401
    engine as tofu_engine,
    second_admin_token,
    vcenter,
)
from tests.test_ticket_agent import _fake_hermes, _ticket, agent, writes  # noqa: F401
from tests.test_ticket_agent_apps import shop  # noqa: F401
from tests.test_ticket_agent_vms import _ask, _task, _vm, vm_agent  # noqa: F401

CAPSULE_URL = (
    "https://capsule.example.com:9090/register?activation_keys=LEB-DOPS-9-DR_AK"
    "&download_utility=curl&hostgroup_id=2&location_id=3&organization_id=1&update_packages=false"
)
TOKEN = "eyJhbGciOiJIUzI1NiJ9.eyJ1c2VyX2lkIjo0Mn0.c2lnbmF0dXJlLXNpZ25hdHVyZQ"
CP, WORKER = ("10.20.30.50", "tm-901-vm-1"), ("10.20.30.51", "tm-901-vm-2")


@pytest.fixture(autouse=True)
def _cluster_needs_no_approval(no_cluster_approvals):
    yield


def _rhel_probe(hostname: str, suffix: str) -> str:
    return "\n".join([
        "KS_OS_ID=rhel", 'KS_OS_LIKE="fedora"', "KS_OS_VERSION=9.7",
        "KS_OS_PRETTY=Red Hat Enterprise Linux 9.7 (Plow)", "KS_ARCH=x86_64",
        f"KS_HOSTNAME={hostname}", f"KS_PRODUCT_UUID=42000000-0000-0000-0000-{suffix}",
        "KS_CPUS=4", "KS_MEM_MIB=8192", "KS_SWAP_KB=0", "KS_DISK_FREE_GB=80",
        f"KS_EPOCH={int(time.time())}", "KS_MOD_overlay=ok", "KS_MOD_br_netfilter=ok",
        "KS_ESCALATION=ok", "KS_SUBMAN=present", "KS_SAT_CA=200", "KS_SAT_REG=401",
    ])


def _capsule_listing(version: str) -> str:
    return "Available Packages\n" + "".join(
        f"{name}.x86_64   {version}-150500.1.1   k8s-packages-v{version.rsplit('.', 1)[0]}\n"
        for name in ("kubeadm", "kubelet", "kubectl")
    )


def _fake_vms():
    fake = build_default_fake({CP[0]: (CP[1], "control_plane"), WORKER[0]: (WORKER[1], "worker")})
    for (address, name), suffix in ((CP, "000000000050"), (WORKER, "000000000051")):
        fake.responders.insert(0, (
            lambda host, script, a=address: host == a and "preflight probe" in script,
            _rhel_probe(name, suffix),
        ))
    fake.responders.insert(0, (lambda h, s: "showduplicates" in s, _capsule_listing("1.33.5")))
    # The worker's first join: kubeadm gives up while the kubelet is still registering.
    state = {"late": True}

    def _late_join(host, script):
        if "kubeadm join" in script and host == WORKER[0] and state["late"]:
            state["late"] = False
            return True
        return False

    fake.responders.insert(0, (_late_join, SshCommandError("boom", exit_code=1, output=(
        "error execution phase kubelet-wait-bootstrap: error writing CRISocket for this node: "
        f'nodes "{WORKER[1]}" not found\n'
    ))))
    fake.responders.insert(0, (lambda h, s: f"get node {WORKER[1]} -o name" in s, f"node/{WORKER[1]}\n"))
    # The add-ons' functional checks: Metrics Server reports both nodes,
    # NGINX Ingress answers on its NodePort.
    add_addon_responders(fake)
    # The app's Deployment, which must roll out before the app counts as installed.
    fake.responders.insert(0, (
        lambda h, s: "get deployment,statefulset,daemonset -l" in s and "instance=web" in s,
        "deployment.apps/web\n",
    ))
    fake.responders.insert(0, (
        lambda h, s: "top nodes --no-headers" in s,
        f"{CP[1]} 100m 5% 512Mi 10%\n{WORKER[1]} 100m 5% 512Mi 10%\n",
    ))
    return fake


def test_ticket_to_vms_to_kubernetes_to_addons_to_helm_app(
    client, app, admin_token, vm_agent, shop, writes, monkeypatch, fake_ssh, helm,
):
    monkeypatch.setattr(executor.time, "sleep", lambda s: None)
    source = client.post("/api/build-profiles", json={
        "name": "Capsule", "repoMode": "satellite",
        "satelliteRegisterUrl": CAPSULE_URL, "satelliteToken": TOKEN,
    }, headers=auth_headers(admin_token))
    assert source.status_code == 201, source.get_json()
    vm_requests.update_profile(vm_agent["id"], {
        "allowKubernetes": True,
        "buildProfileId": source.get_json()["data"]["id"],
        "diskCheckPath": "/data",
        "addons": {"allowed": ["metrics-server", "nginx-ingress"], "defaults": ["metrics-server"]},
    })

    # Hermes asks: a lab cluster, ingress on request, the shop app.
    ticket = _ticket(subject="A lab cluster with ingress and the shop")
    _ask(ticket, name="tm-901", count=None,
         kubernetes={"shape": "lab", "addons": ["nginx-ingress"]}, apps=["Shop front"])
    task = _task(ticket)
    vm = _vm(task)
    asked_version = vm["kubernetes"]["version"]
    assert [a["id"] for a in vm["kubernetes"]["addons"]] == ["metrics-server", "nginx-ingress"]
    assert "Metrics Server" in vm_requests.approval_text(task, ticket)

    # A VM approver approves; OpenTofu creates the VMs.
    fake = _fake_vms()
    set_transport_factory(lambda: fake)
    other = second_admin_token(client, app)
    response = client.post(f"/api/ticket-agent/tasks/{task.id}/approve", headers=auth_headers(other))
    assert response.status_code == 200, response.get_json()

    followups = []
    _fake_hermes(monkeypatch, lambda message: followups.append(message) and None)
    engine.tick()  # vms_ready → preflight (warnings only) → the build, synchronously under TESTING
    engine.tick()  # completed → one follow-up
    vm = _vm(task)
    build = db.session.get(ClusterBuild, vm["buildId"])
    assert build.status == "completed", (build.error, vm)

    # Warnings did not hold it: the build started past them, on record.
    assert any("past preflight warnings" in note for note in (build.warnings_ack_json or {}).get("notes", []))
    # The capsule's version, not the one asked for, with a CNI and add-ons for it.
    assert build.k8s_version == "1.33.5" and asked_version != "1.33.5"
    assert build.cni_version == "3.31.7"
    assert {a["id"]: a["version"] for a in build.addons_json} == {
        "metrics-server": "0.7.2", "nginx-ingress": "5.5.4",
    }

    for address in (CP[0], WORKER[0]):
        scripts = [s for host, s in fake.calls if host == address]
        first = lambda needle: next(i for i, s in enumerate(scripts) if needle in s)  # noqa: E731
        # The profile's CAs before anything talks to the capsule.
        assert first("kubesight-extra.crt") < first("subscription-manager clean")
        # Registered before any package; container data bind-mounted on /data
        # before containerd; node names in /etc/hosts; packages from the capsule.
        assert first("subscription-manager clean") < first("dnf install -y")
        assert first("KS_BASE=/data/kubesight-data") < first("containerd config default")
        assert any("# kubesight-node" in s and f"{CP[0]} {CP[1]}" in s for s in scripts)
        assert not any("pkgs.k8s.io" in s for s in scripts)
        assert any('"kubeadm-${K8S_VER}"' in s and 'K8S_VER="1.33.5"' in s for s in scripts)
    # The late worker counted as joined.
    assert any(f"annotate node {WORKER[1]}" in s for _, s in fake.calls)

    # The app went in with its values.
    payload, context = helm["install"][-1]
    assert payload["releaseName"] == "web" and PASSWORD in payload["valuesYaml"]
    # …and was running before it counted as installed.
    assert any("-n shop rollout status deployment.apps/web" in s for _, s in fake.calls)

    # One follow-up, saying what is there.
    event = followups[-1]["event"]
    assert event["result"] == "cluster_ready"
    assert event["kubernetesVersion"] == "1.33.5"
    assert {a["id"] for a in event["addons"]} == {"metrics-server", "nginx-ingress"}
    assert event["apps"] == [{"release": "web", "namespace": "shop", "status": "installed", "error": None}]
