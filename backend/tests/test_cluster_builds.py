"""Cluster Builder tests: fake SSH/vSphere transports, phase machine, preflight,
scrubbing, host keys, RBAC. No VM, vCenter, or paramiko required."""

import re
from datetime import datetime, timedelta, timezone

import pytest

from api.db import db
from api.models import (
    Cluster,
    ClusterBuild,
    ClusterBuildNode,
    ClusterBuildStep,
)
from api.services import alert_policy_scheduler as scheduler_mod
from api.services import ssh_profile_service, vsphere_service
from api.services.ssh import SshCommandError, set_transport_factory
from api.services.ssh import hostkeys
from api.services.ssh.transport import SshTarget, _escalated_command
from api.services.cluster_build import preflight as preflight_mod
from api.services.cluster_build.scrub import scrub
from api.services.cluster_build import kubeadm as kubeadm_mod
from api.services.cluster_build import executor as executor_mod
from api.services.cluster_build.cni.calico import CALICO


def auth_headers(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------

ADMIN_CONF = """apiVersion: v1
kind: Config
clusters:
- cluster:
    server: https://127.0.0.1:6443
    certificate-authority-data: Zm9v
  name: kubernetes
contexts:
- context:
    cluster: kubernetes
    user: kubernetes-admin
  name: kubernetes-admin@kubernetes
current-context: kubernetes-admin@kubernetes
users:
- name: kubernetes-admin
  user:
    client-certificate-data: Zm9v
    client-key-data: Zm9v
"""

INIT_OUTPUT = """[init] Using Kubernetes version: v1.32.4
[upload-certs] Using certificate key:
9aa3f19c4f2e8b7d6c5a4e3f2d1c0b9a8f7e6d5c4b3a291817161514131211aa
Your Kubernetes control-plane has initialized successfully!

You can now join any number of control-plane nodes running the following command:

  kubeadm join 10.0.0.100:6443 --token abcdef.0123456789abcdef \\
        --discovery-token-ca-cert-hash sha256:1111111111111111111111111111111111111111111111111111111111111111 \\
        --control-plane --certificate-key 9aa3f19c4f2e8b7d6c5a4e3f2d1c0b9a8f7e6d5c4b3a291817161514131211aa

Then you can join any number of worker nodes by running the following:

kubeadm join 10.0.0.100:6443 --token abcdef.0123456789abcdef \\
        --discovery-token-ca-cert-hash sha256:1111111111111111111111111111111111111111111111111111111111111111
"""


def probe_output(hostname: str, uuid_suffix: str, role: str = "worker") -> str:
    lines = [
        "KS_OS_ID=ubuntu",
        "KS_OS_LIKE=debian",
        "KS_OS_VERSION=24.04",
        "KS_OS_PRETTY=Ubuntu 24.04.1 LTS",
        "KS_ARCH=x86_64",
        f"KS_HOSTNAME={hostname}",
        f"KS_PRODUCT_UUID=42000000-0000-0000-0000-{uuid_suffix}",
        "KS_CPUS=4",
        "KS_MEM_MIB=8192",
        "KS_SWAP_KB=0",
        "KS_DISK_FREE_GB=50",
        "KS_EPOCH={}".format(__import__("time").time().__int__()),
        "KS_MOD_overlay=ok",
        "KS_MOD_br_netfilter=ok",
        "KS_ESCALATION=ok",
    ]
    if role == "loadbalancer":
        lines.append("KS_VIP_STATE=free")
    return "\n".join(lines)


class FakeResult:
    def __init__(self, output: str):
        self.exit_code = 0
        self.output = output


class FakeSshTransport:
    """Pattern-matched canned SSH. Records every (host, script) call."""

    def __init__(self):
        self.calls = []
        self.responders = []  # (predicate(host, script) -> bool, output or exc)

    def add(self, predicate, output):
        self.responders.append((predicate, output))

    def run(self, target, script, timeout_s=None, on_output=None, escalate=True):
        self.calls.append((target.host, script))
        for predicate, output in self.responders:
            if predicate(target.host, script):
                if isinstance(output, Exception):
                    raise output
                return FakeResult(output)
        return FakeResult("")

    def put_file(self, target, local_path, remote_path):
        self.calls.append((target.host, f"PUT {remote_path}"))


def build_default_fake(hosts_roles):
    """A transport that makes a full build succeed for the given
    {address: (hostname, role)} map."""
    fake = FakeSshTransport()

    def _probe(host, script):
        return "preflight probe" in script

    fake.add(_probe, "")  # placeholder; replaced below with per-host output

    fake.responders = []
    for address, (hostname, role) in hosts_roles.items():
        fake.add(
            lambda host, script, a=address: host == a and "preflight probe" in script,
            probe_output(hostname, address.replace(".", ""), hosts_roles[address][1]),
        )
    fake.add(lambda h, s: "kubeadm init" in s, INIT_OUTPUT)
    fake.add(lambda h, s: "ip -o -4 addr show | awk" in s, "ens192\n")
    fake.add(lambda h, s: "haproxy -c -f" in s, "Configuration file is valid\nactive\nactive\n")
    fake.add(lambda h, s: 'ip -o -4 addr show | grep -q' in s, "VIP bound\n")
    fake.add(lambda h, s: "ping -c 2" in s, "VIP reachable\n")
    fake.add(lambda h, s: "get --raw /readyz/etcd" in s, "ok")
    fake.add(
        lambda h, s: "get nodes --no-headers" in s,
        "\n".join("Ready True" for _ in range(9)),
    )
    fake.add(lambda h, s: "rollout status" in s, "successfully rolled out")
    fake.add(lambda h, s: "jsonpath='{.status.phase}'" in s, "Running")
    fake.add(lambda h, s: "run kubesight-smoke" in s, "pod/kubesight-smoke created")
    fake.add(lambda h, s: "delete pod kubesight-smoke" in s, "deleted")
    fake.add(lambda h, s: "cat /etc/kubernetes/admin.conf" in s, ADMIN_CONF)
    fake.add(lambda h, s: "kubectl --kubeconfig /etc/kubernetes/admin.conf apply" in s,
             "applied")
    fake.add(lambda h, s: "kubeadm join" in s, "This node has joined the cluster")
    fake.add(lambda h, s: "kubeadm config images pull" in s, "pulled")
    return fake


@pytest.fixture()
def fake_ssh():
    fake = FakeSshTransport()
    set_transport_factory(lambda: fake)
    yield fake
    set_transport_factory(None)


@pytest.fixture(autouse=True)
def no_network_cni_manifest(monkeypatch):
    """Cluster-build tests exercise orchestration, never public GitHub.

    Production prefers a bundled pinned manifest and otherwise fetches its
    pinned URL in internet mode. Keep the suite deterministic with the smallest
    manifest shape needed to derive and rewrite a CNI image.
    """
    monkeypatch.setattr(
        type(CALICO),
        "load_manifests",
        lambda self, version, profile: [
            "apiVersion: apps/v1\n"
            "kind: DaemonSet\n"
            "metadata:\n"
            "  name: calico-node\n"
            "spec:\n"
            "  template:\n"
            "    spec:\n"
            "      containers:\n"
            "        - name: calico-node\n"
            f"          image: docker.io/calico/node:v{version}\n"
        ],
    )


@pytest.fixture()
def ssh_profile(app):
    cred = ssh_profile_service.create_credential(
        {"name": "root-key", "username": "kubesight", "authMethod": "key",
         "secret": "-----BEGIN KEY-----\nfake\n-----END KEY-----",
         "sudoMode": "nopasswd"},
    )
    profile = ssh_profile_service.create_profile(
        {"name": "default", "credentialId": cred["id"], "routeMode": "direct",
         "hostKeyPolicy": "tofu"},
    )
    return profile


def make_build_payload(name="demo", topology="single_cp", endpoint_mode="manual_endpoint",
                       nodes=None, **extra):
    payload = {
        "name": name,
        "k8sVersion": "1.32.4",
        "topologyType": topology,
        "endpointMode": endpoint_mode,
        "cniPlugin": "calico",
        "podCidr": "10.244.0.0/16",
        "serviceCidr": "10.96.0.0/12",
        "nodes": nodes or [],
    }
    if endpoint_mode == "manual_endpoint":
        payload["controlPlaneEndpoint"] = "10.0.0.100:6443"
    payload.update(extra)
    return payload


SINGLE_CP_NODES = [
    {"role": "control_plane", "hostname": "cp-1", "address": "10.0.0.11"},
    {"role": "worker", "hostname": "w-1", "address": "10.0.0.21"},
]

HA_NODES = [
    {"role": "loadbalancer", "hostname": "lb-1", "address": "10.0.0.5"},
    {"role": "loadbalancer", "hostname": "lb-2", "address": "10.0.0.6"},
    {"role": "control_plane", "hostname": "cp-1", "address": "10.0.0.11"},
    {"role": "control_plane", "hostname": "cp-2", "address": "10.0.0.12"},
    {"role": "control_plane", "hostname": "cp-3", "address": "10.0.0.13"},
    {"role": "worker", "hostname": "w-1", "address": "10.0.0.21"},
]

SINGLE_CP_MANAGED_LB_NODES = [
    {"role": "loadbalancer", "hostname": "lb-1", "address": "10.0.0.5"},
    {"role": "control_plane", "hostname": "cp-1", "address": "10.0.0.11"},
    {"role": "worker", "hostname": "w-1", "address": "10.0.0.21"},
]


def create_build(client, token, ssh_profile, payload):
    payload = dict(payload)
    payload["connectionProfileId"] = ssh_profile["id"]
    response = client.post("/api/cluster-builds", json=payload, headers=auth_headers(token))
    assert response.status_code == 201, response.get_json()
    return response.get_json()["data"]


# ---------------------------------------------------------------------------
# Scrubbing (the security-critical unit)
# ---------------------------------------------------------------------------

class TestScrub:
    def test_bootstrap_token_redacted(self):
        assert "abcdef.0123456789abcdef" not in scrub(INIT_OUTPUT)

    def test_certificate_key_redacted(self):
        assert "9aa3f19c4f2e8b7d" not in scrub(INIT_OUTPUT)

    def test_ca_cert_hash_preserved(self):
        scrubbed = scrub(INIT_OUTPUT)
        assert "sha256:1111111111111111111111111111111111111111111111111111111111111111" in scrubbed

    def test_password_lines_redacted(self):
        assert "hunter2" not in scrub("    auth_pass hunter2is8ch")
        assert "hunter2" not in scrub("auth_pass: hunter2")

    def test_traced_script_secret_assignments_redacted(self):
        text = "REGISTRY_PASSWORD=hunter2\nAPI_TOKEN=abc123\nNORMAL=value"
        cleaned = scrub(text)
        assert "hunter2" not in cleaned
        assert "abc123" not in cleaned
        assert "NORMAL=value" in cleaned

    def test_private_key_and_bearer_redacted(self):
        text = (
            "Authorization: Bearer secret-token\n"
            "-----BEGIN PRIVATE KEY-----\nsecret\n-----END PRIVATE KEY-----"
        )
        cleaned = scrub(text)
        assert "secret-token" not in cleaned
        assert "BEGIN PRIVATE KEY" not in cleaned

    def test_parse_init_output(self):
        artifacts = kubeadm_mod.parse_init_output(INIT_OUTPUT)
        assert artifacts.token == "abcdef.0123456789abcdef"
        assert artifacts.ca_cert_hash.startswith("sha256:1111")
        assert artifacts.certificate_key.startswith("9aa3f19c")
        assert kubeadm_mod.validate_init_artifacts(artifacts, need_certificate_key=True) is None


class TestSshScriptTransport:
    def test_large_script_streams_over_stdin_instead_of_exec_packet(self):
        script = "x" * 1_000_000
        spec = _escalated_command(
            SshTarget(host="node", username="kubesight", sudo_mode="nopasswd"),
            script,
        )
        assert spec.command == "sudo -n sh -s"
        assert spec.stdin_payload == script
        assert len(spec.command) < 100

    def test_root_script_streams_over_stdin(self):
        spec = _escalated_command(
            SshTarget(host="node", username="root", sudo_mode="root"),
            "echo ok",
        )
        assert spec.command == "sh -s"
        assert spec.stdin_payload == "echo ok"

    def test_sudo_password_precedes_script_on_stdin(self):
        spec = _escalated_command(
            SshTarget(
                host="node",
                username="kubesight",
                sudo_mode="password",
                sudo_password="secret",
            ),
            "echo ok",
        )
        assert spec.command == "sudo -S -p '' sh -s"
        assert spec.stdin_payload == "secret\necho ok"


class TestCalicoRendering:
    def test_commented_pool_cidr_renders_valid_indentation(self):
        source = (
            "        env:\n"
            "            # - name: CALICO_IPV4POOL_CIDR\n"
            '            #   value: "192.168.0.0/16"\n'
            "            - name: CALICO_DISABLE_FILE_LOGGING\n"
        )
        rendered = CALICO.apply_pod_cidr(source, "10.244.0.0/16")
        assert rendered == (
            "        env:\n"
            "            - name: CALICO_IPV4POOL_CIDR\n"
            '              value: "10.244.0.0/16"\n'
            "            - name: CALICO_DISABLE_FILE_LOGGING\n"
        )
        assert "\n\n" not in rendered

    def test_existing_pool_cidr_is_rewritten_without_shape_change(self):
        source = (
            "            - name: CALICO_IPV4POOL_CIDR\n"
            '              value: "192.168.0.0/16"\n'
        )
        rendered = CALICO.apply_pod_cidr(source, "10.244.0.0/16")
        assert rendered == (
            "            - name: CALICO_IPV4POOL_CIDR\n"
            '              value: "10.244.0.0/16"\n'
        )


# ---------------------------------------------------------------------------
# Host keys
# ---------------------------------------------------------------------------

class TestHostKeys:
    def test_tofu_records_then_trusts(self, app):
        ok, reason = hostkeys.verify_host_key(
            host="10.0.0.1", port=22, key_type="ssh-ed25519",
            fingerprint_sha256="aa" * 32, policy="tofu",
        )
        assert ok and "first use" in reason
        ok, reason = hostkeys.verify_host_key(
            host="10.0.0.1", port=22, key_type="ssh-ed25519",
            fingerprint_sha256="aa" * 32, policy="tofu",
        )
        assert ok and reason == "recorded"

    def test_changed_key_always_refused(self, app):
        hostkeys.verify_host_key(
            host="10.0.0.2", port=22, key_type="ssh-ed25519",
            fingerprint_sha256="aa" * 32, policy="tofu",
        )
        ok, reason = hostkeys.verify_host_key(
            host="10.0.0.2", port=22, key_type="ssh-ed25519",
            fingerprint_sha256="bb" * 32, policy="tofu",
        )
        assert not ok and "CHANGED" in reason

    def test_strict_refuses_unknown(self, app):
        ok, _ = hostkeys.verify_host_key(
            host="10.0.0.3", port=22, key_type="ssh-ed25519",
            fingerprint_sha256="cc" * 32, policy="strict",
        )
        assert not ok

    def test_pinned_requires_preapproval(self, app):
        hostkeys.verify_host_key(
            host="10.0.0.4", port=22, key_type="ssh-ed25519",
            fingerprint_sha256="dd" * 32, policy="tofu",
        )
        ok, reason = hostkeys.verify_host_key(
            host="10.0.0.4", port=22, key_type="ssh-ed25519",
            fingerprint_sha256="dd" * 32, policy="pinned",
        )
        assert not ok and "pre-approve" in reason.lower()
        hostkeys.preapprove(
            host="10.0.0.4", port=22, key_type="ssh-ed25519",
            fingerprint_sha256="dd" * 32,
        )
        ok, _ = hostkeys.verify_host_key(
            host="10.0.0.4", port=22, key_type="ssh-ed25519",
            fingerprint_sha256="dd" * 32, policy="pinned",
        )
        assert ok


# ---------------------------------------------------------------------------
# RBAC
# ---------------------------------------------------------------------------

class TestRbac:
    def test_viewer_denied_everywhere(self, client, viewer_token):
        for method, path in (
            ("get", "/api/cluster-builds"),
            ("post", "/api/cluster-builds"),
            ("get", "/api/ssh-credentials"),
            ("post", "/api/ssh-credentials"),
            ("get", "/api/vsphere-connections"),
            ("get", "/api/build-profiles"),
        ):
            response = getattr(client, method)(path, headers=auth_headers(viewer_token), json={})
            assert response.status_code == 403, path

    def test_admin_allowed(self, client, admin_token):
        response = client.get("/api/cluster-builds", headers=auth_headers(admin_token))
        assert response.status_code == 200
        response = client.get("/api/cluster-builds/options", headers=auth_headers(admin_token))
        assert response.status_code == 200
        data = response.get_json()["data"]
        assert "1.32.4" in data["k8sVersions"]
        assert any(p["id"] == "calico" for p in data["cniPlugins"])


# ---------------------------------------------------------------------------
# Credentials / profiles
# ---------------------------------------------------------------------------

class TestSshCredentials:
    def test_secret_never_serialized(self, client, admin_token):
        response = client.post(
            "/api/ssh-credentials",
            json={"name": "k", "username": "u", "authMethod": "password",
                  "secret": "sup3rs3cret", "sudoMode": "password",
                  "sudoPassword": "als0secret"},
            headers=auth_headers(admin_token),
        )
        assert response.status_code == 201
        body = response.get_data(as_text=True)
        assert "sup3rs3cret" not in body
        assert "als0secret" not in body
        assert response.get_json()["data"]["secretConfigured"] is True

    def test_delete_blocked_while_referenced(self, client, admin_token, app):
        cred = ssh_profile_service.create_credential(
            {"name": "c", "username": "u", "authMethod": "password", "secret": "x"}
        )
        ssh_profile_service.create_profile(
            {"name": "p", "credentialId": cred["id"]}
        )
        response = client.delete(
            f"/api/ssh-credentials/{cred['id']}", headers=auth_headers(admin_token)
        )
        assert response.status_code == 400


# ---------------------------------------------------------------------------
# Build validation
# ---------------------------------------------------------------------------

class TestBuildValidation:
    @pytest.mark.parametrize(
        ("overrides", "message"),
        [
            ({"controlPlaneEndpoint": "10.0.0.100:70000"}, "port"),
            ({"podCidr": "10.96.0.0/16"}, "must not overlap"),
            ({"podCidr": "10.0.0.0/24"}, "Node address"),
        ],
    )
    def test_rejects_unsafe_network_layouts(
        self, client, admin_token, ssh_profile, overrides, message
    ):
        payload = make_build_payload(nodes=SINGLE_CP_NODES, **overrides)
        payload["connectionProfileId"] = ssh_profile["id"]
        response = client.post(
            "/api/cluster-builds", json=payload, headers=auth_headers(admin_token)
        )
        assert response.status_code == 400
        assert message.lower() in response.get_json()["error"].lower()

    def test_rejects_ipv6_managed_vip(
        self, client, admin_token, ssh_profile
    ):
        payload = make_build_payload(
            endpoint_mode="managed_haproxy",
            nodes=SINGLE_CP_MANAGED_LB_NODES,
            vipAddress="2001:db8::10",
        )
        payload["connectionProfileId"] = ssh_profile["id"]
        response = client.post(
            "/api/cluster-builds", json=payload, headers=auth_headers(admin_token)
        )
        assert response.status_code == 400
        assert "ipv4" in response.get_json()["error"].lower()

    def test_completed_build_history_cannot_be_deleted(
        self, client, admin_token, ssh_profile
    ):
        build = create_build(
            client, admin_token, ssh_profile,
            make_build_payload(nodes=SINGLE_CP_NODES),
        )
        row = db.session.get(ClusterBuild, build["id"])
        row.status = "completed"
        db.session.commit()
        response = client.delete(
            f"/api/cluster-builds/{build['id']}",
            headers=auth_headers(admin_token),
        )
        assert response.status_code == 400
        assert "retained for audit" in response.get_json()["error"]
        assert db.session.get(ClusterBuild, build["id"]) is not None

    def test_two_control_planes_rejected(self, client, admin_token, ssh_profile):
        nodes = [
            {"role": "control_plane", "hostname": "cp-1", "address": "10.0.0.11"},
            {"role": "control_plane", "hostname": "cp-2", "address": "10.0.0.12"},
        ]
        payload = make_build_payload(topology="stacked_ha", nodes=nodes)
        payload["connectionProfileId"] = ssh_profile["id"]
        response = client.post("/api/cluster-builds", json=payload,
                               headers=auth_headers(admin_token))
        assert response.status_code == 400
        assert "quorum" in response.get_json()["error"]

    def test_managed_haproxy_requires_vip(self, client, admin_token, ssh_profile):
        payload = make_build_payload(
            endpoint_mode="managed_haproxy", nodes=SINGLE_CP_MANAGED_LB_NODES
        )
        payload.pop("controlPlaneEndpoint", None)
        payload["connectionProfileId"] = ssh_profile["id"]
        response = client.post("/api/cluster-builds", json=payload,
                               headers=auth_headers(admin_token))
        assert response.status_code == 400
        assert "vip" in response.get_json()["error"].lower()

    def test_single_cp_managed_haproxy_requires_one_lb(
        self, client, admin_token, ssh_profile
    ):
        payload = make_build_payload(
            endpoint_mode="managed_haproxy",
            nodes=SINGLE_CP_NODES,
            vipAddress="10.0.0.100",
        )
        payload["connectionProfileId"] = ssh_profile["id"]
        response = client.post(
            "/api/cluster-builds", json=payload, headers=auth_headers(admin_token)
        )
        assert response.status_code == 400
        assert "exactly 1 load-balancer" in response.get_json()["error"]

    def test_ha_managed_haproxy_still_requires_two_lbs(
        self, client, admin_token, ssh_profile
    ):
        nodes = [node for node in HA_NODES if node["hostname"] != "lb-2"]
        payload = make_build_payload(
            topology="stacked_ha",
            endpoint_mode="managed_haproxy",
            nodes=nodes,
            vipAddress="10.0.0.100",
        )
        payload["connectionProfileId"] = ssh_profile["id"]
        response = client.post(
            "/api/cluster-builds", json=payload, headers=auth_headers(admin_token)
        )
        assert response.status_code == 400
        assert "exactly 2 load-balancer nodes" in response.get_json()["error"]

    def test_endpoint_mandatory_even_single_cp(self, client, admin_token, ssh_profile):
        payload = make_build_payload(nodes=SINGLE_CP_NODES)
        payload.pop("controlPlaneEndpoint")
        payload["connectionProfileId"] = ssh_profile["id"]
        response = client.post("/api/cluster-builds", json=payload,
                               headers=auth_headers(admin_token))
        assert response.status_code == 400
        assert "controlPlaneEndpoint" in response.get_json()["error"]

    def test_node_without_address_rejected(self, client, admin_token, ssh_profile):
        nodes = [{"role": "control_plane", "hostname": "cp-1"}]
        payload = make_build_payload(nodes=nodes)
        payload["connectionProfileId"] = ssh_profile["id"]
        response = client.post("/api/cluster-builds", json=payload,
                               headers=auth_headers(admin_token))
        assert response.status_code == 400
        assert "manual management IP" in response.get_json()["error"]

    def test_unknown_k8s_version_rejected(self, client, admin_token, ssh_profile):
        payload = make_build_payload(nodes=SINGLE_CP_NODES)
        payload["k8sVersion"] = "1.12.0"
        payload["connectionProfileId"] = ssh_profile["id"]
        response = client.post("/api/cluster-builds", json=payload,
                               headers=auth_headers(admin_token))
        assert response.status_code == 400


# ---------------------------------------------------------------------------
# Preflight
# ---------------------------------------------------------------------------

class TestPreflight:
    def test_preflight_pass_and_gate(self, client, admin_token, ssh_profile, fake_ssh):
        for address, (hostname, role) in {
            "10.0.0.11": ("cp-1", "control_plane"),
            "10.0.0.21": ("w-1", "worker"),
        }.items():
            fake_ssh.add(
                lambda h, s, a=address: h == a and "preflight probe" in s,
                probe_output(hostname, address.replace(".", ""), role),
            )
        build = create_build(client, admin_token, ssh_profile,
                             make_build_payload(nodes=SINGLE_CP_NODES))
        response = client.post(
            f"/api/cluster-builds/{build['id']}/preflight",
            headers=auth_headers(admin_token),
        )
        assert response.status_code == 200
        data = response.get_json()["data"]
        assert data["status"] in ("pass", "warn")
        assert len(data["nodes"]) == 2

    def test_unsupported_os_hard_fails(self, client, admin_token, ssh_profile, fake_ssh):
        fake_ssh.add(
            lambda h, s: "preflight probe" in s,
            "KS_OS_ID=sles\nKS_OS_LIKE=suse\nKS_OS_VERSION=15\n"
            "KS_OS_PRETTY=SUSE Linux Enterprise 15\nKS_ARCH=x86_64\n"
            "KS_HOSTNAME=x\nKS_CPUS=4\nKS_MEM_MIB=8192\nKS_ESCALATION=ok",
        )
        build = create_build(client, admin_token, ssh_profile,
                             make_build_payload(nodes=SINGLE_CP_NODES))
        response = client.post(
            f"/api/cluster-builds/{build['id']}/preflight",
            headers=auth_headers(admin_token),
        )
        data = response.get_json()["data"]
        assert data["status"] == "fail"
        os_checks = [c for n in data["nodes"] for c in n["checks"] if c["id"] == "os"]
        assert all(c["status"] == "fail" for c in os_checks)
        assert "Supported matrix" in os_checks[0]["hint"]

    def test_duplicate_hostnames_fail(self, client, admin_token, ssh_profile, fake_ssh):
        fake_ssh.add(
            lambda h, s: "preflight probe" in s,
            probe_output("clone", "samesame"),
        )
        build = create_build(client, admin_token, ssh_profile,
                             make_build_payload(nodes=SINGLE_CP_NODES))
        response = client.post(
            f"/api/cluster-builds/{build['id']}/preflight",
            headers=auth_headers(admin_token),
        )
        data = response.get_json()["data"]
        assert data["status"] == "fail"
        dup = [c for n in data["nodes"] for c in n["checks"] if c["id"] == "hostname_unique"]
        assert dup and all(c["status"] == "fail" for c in dup)

    def test_disk_check_follows_configured_path(
        self, client, admin_token, ssh_profile, fake_ssh
    ):
        """A build that names another mount is measured there, not on /var."""
        probe = probe_output("cp-1", "aaaa").replace(
            "KS_DISK_FREE_GB=50", "KS_DISK_FREE_GB=120"
        )
        fake_ssh.add(lambda h, s: "preflight probe" in s, probe)
        build = create_build(
            client, admin_token, ssh_profile,
            make_build_payload(nodes=SINGLE_CP_NODES, diskCheckPath="/data/containerd"),
        )
        assert build["diskCheckPath"] == "/data/containerd"
        response = client.post(
            f"/api/cluster-builds/{build['id']}/preflight",
            headers=auth_headers(admin_token),
        )
        data = response.get_json()["data"]
        disk = [c for n in data["nodes"] for c in n["checks"] if c["id"] == "disk"]
        assert disk and all(c["status"] == "pass" for c in disk)
        assert all(c["label"] == "Free disk on /data/containerd" for c in disk)
        # The probe itself must measure that path — not /var with a new label.
        scripts = [s for _, s in fake_ssh.calls if "preflight probe" in s]
        assert scripts and all("KS_DISK_PATH=/data/containerd\n" in s for s in scripts)

    def test_missing_disk_path_fails_as_missing(
        self, client, admin_token, ssh_profile, fake_ssh
    ):
        """A path that isn't on the node reads as absent, not as 0 GiB free."""
        probe = probe_output("cp-1", "bbbb").replace(
            "KS_DISK_FREE_GB=50", "KS_DISK_PATH_MISSING=1"
        )
        fake_ssh.add(lambda h, s: "preflight probe" in s, probe)
        build = create_build(
            client, admin_token, ssh_profile,
            make_build_payload(nodes=SINGLE_CP_NODES, diskCheckPath="/mnt/k8s"),
        )
        response = client.post(
            f"/api/cluster-builds/{build['id']}/preflight",
            headers=auth_headers(admin_token),
        )
        data = response.get_json()["data"]
        disk = [c for n in data["nodes"] for c in n["checks"] if c["id"] == "disk"]
        assert disk and all(c["status"] == "fail" for c in disk)
        assert "does not exist" in disk[0]["detail"]

    def test_crictl_already_installed_passes(
        self, client, admin_token, ssh_profile, fake_ssh
    ):
        fake_ssh.add(lambda h, s: "preflight probe" in s,
                     probe_output("cp-1", "cccc") + "\nKS_CRICTL=present")
        build = create_build(client, admin_token, ssh_profile,
                             make_build_payload(nodes=SINGLE_CP_NODES))
        response = client.post(
            f"/api/cluster-builds/{build['id']}/preflight",
            headers=auth_headers(admin_token),
        )
        data = response.get_json()["data"]
        cri = [c for n in data["nodes"] for c in n["checks"] if c["id"] == "cri_tools"]
        assert cri and all(c["status"] == "pass" for c in cri)

    def test_missing_crictl_and_cri_tools_fails(
        self, client, admin_token, ssh_profile, fake_ssh
    ):
        """crictl absent and unobtainable blocks the build at preflight rather
        than at the image pre-pull phase, where it reads as 'command not found'."""
        fake_ssh.add(lambda h, s: "preflight probe" in s,
                     probe_output("cp-1", "dddd") + "\nKS_PKG_CRI_TOOLS=missing")
        build = create_build(client, admin_token, ssh_profile,
                             make_build_payload(nodes=SINGLE_CP_NODES))
        response = client.post(
            f"/api/cluster-builds/{build['id']}/preflight",
            headers=auth_headers(admin_token),
        )
        data = response.get_json()["data"]
        assert data["status"] == "fail"
        cri = [c for n in data["nodes"] for c in n["checks"] if c["id"] == "cri_tools"]
        assert cri and all(c["status"] == "fail" for c in cri)
        assert "cri-tools" in cri[0]["hint"]

    def test_probe_reports_crictl_and_cri_tools(
        self, client, admin_token, ssh_profile, fake_ssh
    ):
        fake_ssh.add(lambda h, s: "preflight probe" in s, probe_output("cp-1", "eeee"))
        build = create_build(client, admin_token, ssh_profile,
                             make_build_payload(nodes=SINGLE_CP_NODES))
        client.post(f"/api/cluster-builds/{build['id']}/preflight",
                    headers=auth_headers(admin_token))
        script = next(s for _, s in fake_ssh.calls if "preflight probe" in s)
        assert "command -v crictl" in script
        assert "'^Package: cri-tools'" in script

    def test_disk_check_path_defaults_to_var(self, client, admin_token, ssh_profile):
        build = create_build(client, admin_token, ssh_profile,
                             make_build_payload(nodes=SINGLE_CP_NODES))
        assert build["diskCheckPath"] == "/var"

    @pytest.mark.parametrize("bad", ["var", "/var; rm -rf /", "/var/../../etc", "~/data"])
    def test_invalid_disk_check_path_rejected(
        self, client, admin_token, ssh_profile, bad
    ):
        payload = make_build_payload(nodes=SINGLE_CP_NODES, diskCheckPath=bad)
        payload["connectionProfileId"] = ssh_profile["id"]
        response = client.post("/api/cluster-builds", json=payload,
                               headers=auth_headers(admin_token))
        assert response.status_code == 400
        assert "diskCheckPath" in response.get_json()["error"]

    def test_start_blocked_without_preflight(self, client, admin_token, ssh_profile):
        build = create_build(client, admin_token, ssh_profile,
                             make_build_payload(nodes=SINGLE_CP_NODES))
        response = client.post(
            f"/api/cluster-builds/{build['id']}/start",
            json={}, headers=auth_headers(admin_token),
        )
        assert response.status_code == 400
        assert "Preflight" in response.get_json()["error"]


class TestVsphereChecks:
    def test_anti_affinity_colocated_cps_fail(self, app):
        build = ClusterBuild(
            name="ha", status="draft", k8s_version="1.32.4",
            topology_type="stacked_ha", control_plane_endpoint="10.0.0.100:6443",
            endpoint_mode="external_lb", cni_plugin="calico", cni_version="3.28.2",
            pod_cidr="10.244.0.0/16", service_cidr="10.96.0.0/12",
        )
        for i in range(3):
            build.nodes.append(ClusterBuildNode(
                hostname=f"cp-{i}", address=f"10.0.0.1{i}", role="control_plane",
                vsphere_vm_moid=f"vm-{i}", vsphere_vm_name=f"cp-{i}",
                vsphere_host="esxi-1.lab",  # all on ONE host
                vsphere_power_state="POWERED_ON", vsphere_tools_status="RUNNING",
                vsphere_cpu=4, vsphere_memory_mb=8192, position=i,
            ))
        db.session.add(build)
        db.session.commit()
        results = preflight_mod.vsphere_checks(build)
        affinity = [
            c for checks in results.values() for c in checks
            if c["id"] == "vs_cp_affinity"
        ]
        assert len(affinity) == 3
        assert all(c["status"] == "fail" for c in affinity)
        assert "share one ESXi host" in affinity[0]["detail"]

    def test_tools_missing_is_warn_with_manual_ip(self, app):
        build = ClusterBuild(
            name="b", status="draft", k8s_version="1.32.4",
            topology_type="single_cp", control_plane_endpoint="10.0.0.100:6443",
            endpoint_mode="manual_endpoint", cni_plugin="calico",
            cni_version="3.28.2", pod_cidr="10.244.0.0/16",
            service_cidr="10.96.0.0/12",
        )
        build.nodes.append(ClusterBuildNode(
            hostname="cp-1", address="10.0.0.11", address_source="manual",
            role="control_plane", vsphere_vm_moid="vm-1",
            vsphere_power_state="POWERED_ON", vsphere_tools_status=None,
            position=0,
        ))
        db.session.add(build)
        db.session.commit()
        results = preflight_mod.vsphere_checks(build)
        tools = [c for c in results[build.nodes[0].id] if c["id"] == "vs_tools"]
        assert tools[0]["status"] == "warn"  # manual IP present ⇒ warn, not fail


# ---------------------------------------------------------------------------
# Full builds (synchronous under TESTING)
# ---------------------------------------------------------------------------

def run_full_build(client, token, ssh_profile, fake, payload):
    build = create_build(client, token, ssh_profile, payload)
    response = client.post(f"/api/cluster-builds/{build['id']}/preflight",
                           headers=auth_headers(token))
    assert response.get_json()["data"]["status"] in ("pass", "warn"), response.get_json()
    response = client.post(
        f"/api/cluster-builds/{build['id']}/start",
        json={"ackWarnings": ["ack"]}, headers=auth_headers(token),
    )
    assert response.status_code == 200, response.get_json()
    response = client.get(f"/api/cluster-builds/{build['id']}",
                          headers=auth_headers(token))
    return response.get_json()["data"]


class TestSingleCpBuild:
    def test_end_to_end(self, client, admin_token, ssh_profile, fake_ssh, app):
        hosts = {"10.0.0.11": ("cp-1", "control_plane"), "10.0.0.21": ("w-1", "worker")}
        fake = build_default_fake(hosts)
        set_transport_factory(lambda: fake)
        data = run_full_build(client, admin_token, ssh_profile, fake,
                              make_build_payload(nodes=SINGLE_CP_NODES))
        assert data["status"] == "completed", data.get("error")
        assert data["resultClusterId"], "cluster should be registered"
        cluster = Cluster.query.filter_by(name="demo").first()
        assert cluster is not None
        assert cluster.host == "10.0.0.100"
        # Single-CP: no LB or CP-join phases.
        phases = {s["phase"] for s in data["steps"]}
        assert "loadbalancer" not in phases
        assert "join_cp" not in phases
        assert {"base_prep", "init", "cni", "join_workers", "verify", "onboard"} <= phases
        # init ran without --upload-certs on single CP
        init_calls = [s for _, s in fake.calls if "kubeadm init" in s]
        assert init_calls and "--upload-certs" not in init_calls[0]

    def test_managed_single_lb_end_to_end(
        self, client, admin_token, ssh_profile, fake_ssh, app
    ):
        hosts = {
            "10.0.0.5": ("lb-1", "loadbalancer"),
            "10.0.0.11": ("cp-1", "control_plane"),
            "10.0.0.21": ("w-1", "worker"),
        }
        fake = build_default_fake(hosts)
        set_transport_factory(lambda: fake)
        payload = make_build_payload(
            name="single-managed-lb",
            endpoint_mode="managed_haproxy",
            nodes=SINGLE_CP_MANAGED_LB_NODES,
            vipAddress="10.0.0.100",
        )
        data = run_full_build(client, admin_token, ssh_profile, fake, payload)
        assert data["status"] == "completed", data.get("error")
        assert data["controlPlaneEndpoint"] == "10.0.0.100:6443"
        phases = {step["phase"] for step in data["steps"]}
        assert "loadbalancer" in phases
        assert "join_cp" not in phases
        lb_apply_calls = [
            script for host, script in fake.calls
            if host == "10.0.0.5" and "haproxy -c -f" in script
        ]
        assert len(lb_apply_calls) == 1

    def test_secrets_cleared_and_logs_scrubbed(self, client, admin_token,
                                               ssh_profile, fake_ssh, app):
        hosts = {"10.0.0.11": ("cp-1", "control_plane"), "10.0.0.21": ("w-1", "worker")}
        fake = build_default_fake(hosts)
        set_transport_factory(lambda: fake)
        data = run_full_build(client, admin_token, ssh_profile, fake,
                              make_build_payload(nodes=SINGLE_CP_NODES))
        build_row = db.session.get(ClusterBuild, data["id"])
        assert build_row.join_command_cipher is None
        assert build_row.cert_key_cipher is None
        response = client.get(f"/api/cluster-builds/{data['id']}/logs",
                              headers=auth_headers(admin_token))
        blob = response.get_data(as_text=True)
        assert "abcdef.0123456789abcdef" not in blob, "token leaked into logs"
        assert "9aa3f19c4f2e8b7d" not in blob, "cert key leaked into logs"
        assert "BEGIN" not in blob or "admin.conf" not in blob


class TestHaBuild:
    def test_end_to_end(self, client, admin_token, ssh_profile, fake_ssh, app):
        hosts = {
            "10.0.0.5": ("lb-1", "loadbalancer"),
            "10.0.0.6": ("lb-2", "loadbalancer"),
            "10.0.0.11": ("cp-1", "control_plane"),
            "10.0.0.12": ("cp-2", "control_plane"),
            "10.0.0.13": ("cp-3", "control_plane"),
            "10.0.0.21": ("w-1", "worker"),
        }
        fake = build_default_fake(hosts)
        set_transport_factory(lambda: fake)
        payload = make_build_payload(
            name="ha-demo", topology="stacked_ha",
            endpoint_mode="managed_haproxy", nodes=HA_NODES,
            vipAddress="10.0.0.100",
        )
        data = run_full_build(client, admin_token, ssh_profile, fake, payload)
        assert data["status"] == "completed", data.get("error")
        assert data["controlPlaneEndpoint"] == "10.0.0.100:6443"
        phases = {s["phase"] for s in data["steps"]}
        assert "loadbalancer" in phases
        join_cp_steps = [s for s in data["steps"] if s["phase"] == "join_cp"]
        assert len(join_cp_steps) == 2  # cp-2 and cp-3, serial
        assert all(s["status"] == "completed" for s in join_cp_steps)
        # init DID use --upload-certs on HA
        init_calls = [s for _, s in fake.calls if "kubeadm init" in s]
        assert init_calls and "--upload-certs" in init_calls[0]
        # haproxy config reached both LBs and lists all 3 control planes
        lb_calls = [
            (h, s) for h, s in fake.calls
            if "haproxy -c -f" in s and h in ("10.0.0.5", "10.0.0.6")
        ]
        assert len(lb_calls) == 2
        # etcd quorum gate ran between serial CP joins
        etcd_checks = [s for _, s in fake.calls if "readyz/etcd" in s]
        assert len(etcd_checks) >= 2
        # Every control plane gets a kubeconfig installed, not only the primary:
        # SSHing into cp-2 and running `kubectl` must not hit localhost:8080.
        kubeconfig_hosts = {
            h for h, s in fake.calls if "$KUBECTL_HOME/.kube/config" in s
        }
        assert kubeconfig_hosts == {"10.0.0.11", "10.0.0.12", "10.0.0.13"}
        assert not kubeconfig_hosts & {"10.0.0.5", "10.0.0.6", "10.0.0.21"}

    def test_failed_worker_join_fails_build_then_retry(
        self, client, admin_token, ssh_profile, fake_ssh, app
    ):
        hosts = {"10.0.0.11": ("cp-1", "control_plane"), "10.0.0.21": ("w-1", "worker")}
        fake = build_default_fake(hosts)
        # Sabotage the worker join once.
        state = {"failed_once": False}

        def _join_fail(host, script):
            if "kubeadm join" in script and host == "10.0.0.21" and not state["failed_once"]:
                state["failed_once"] = True
                return True
            return False

        fake.responders.insert(
            0, (_join_fail, SshCommandError("boom", exit_code=1, output="kubelet exploded"))
        )
        set_transport_factory(lambda: fake)
        build = create_build(client, admin_token, ssh_profile,
                             make_build_payload(nodes=SINGLE_CP_NODES))
        client.post(f"/api/cluster-builds/{build['id']}/preflight",
                    headers=auth_headers(admin_token))
        client.post(f"/api/cluster-builds/{build['id']}/start",
                    json={"ackWarnings": ["ack"]}, headers=auth_headers(admin_token))
        response = client.get(f"/api/cluster-builds/{build['id']}",
                              headers=auth_headers(admin_token))
        data = response.get_json()["data"]
        assert data["status"] == "failed"
        assert "join_workers" in (data["error"] or "")

        # Retry: node is reset, build resumes from the failed phase and completes.
        response = client.post(f"/api/cluster-builds/{build['id']}/retry",
                               headers=auth_headers(admin_token))
        assert response.status_code == 200
        data = response.get_json()["data"]
        assert data["status"] == "completed", data.get("error")
        reset_calls = [s for h, s in fake.calls if "kubeadm reset -f" in s and h == "10.0.0.21"]
        assert reset_calls, "failed node should have been reset before rejoin"
        # init was NOT re-run (completed step skipped on resume)
        init_calls = [s for _, s in fake.calls if "kubeadm init" in s]
        assert len(init_calls) == 1


class TestRetryResilience:
    """The retry path is where 'worked the second time' bugs live (plan §11.6)."""

    def _failed_init_build(self, client, token, ssh_profile, fake):
        set_transport_factory(lambda: fake)
        build = create_build(client, token, ssh_profile,
                             make_build_payload(nodes=SINGLE_CP_NODES))
        client.post(f"/api/cluster-builds/{build['id']}/preflight",
                    headers=auth_headers(token))
        client.post(f"/api/cluster-builds/{build['id']}/start",
                    json={"ackWarnings": ["ack"]}, headers=auth_headers(token))
        return build

    def test_failed_init_marks_node_so_retry_resets_it(
        self, client, admin_token, ssh_profile, fake_ssh, app
    ):
        hosts = {"10.0.0.11": ("cp-1", "control_plane"), "10.0.0.21": ("w-1", "worker")}
        fake = build_default_fake(hosts)
        state = {"failed_once": False}

        def _init_fail(host, script):
            if "kubeadm init" in script and not state["failed_once"]:
                state["failed_once"] = True
                return True
            return False

        fake.responders.insert(
            0, (_init_fail, SshCommandError("boom", exit_code=1, output="preflight error"))
        )
        build = self._failed_init_build(client, admin_token, ssh_profile, fake)

        row = db.session.get(ClusterBuild, build["id"])
        assert row.status == "failed"
        primary = next(n for n in row.nodes if n.role == "control_plane")
        assert primary.status == "failed", (
            "a half-run init must mark the node failed, or retry skips the reset "
            "and the re-run trips over leftover certs/manifests"
        )

        response = client.post(f"/api/cluster-builds/{build['id']}/retry",
                               headers=auth_headers(admin_token))
        assert response.status_code == 200
        assert response.get_json()["data"]["status"] == "completed"
        assert [s for h, s in fake.calls if "kubeadm reset -f" in s and h == "10.0.0.11"]

    def test_expired_cert_key_is_reminted_before_cp_join(
        self, client, admin_token, ssh_profile, fake_ssh, app
    ):
        """A retry hours later finds the 2h --upload-certs key expired; kubeadm
        can mint a replacement, so the build must not dead-end."""
        hosts = {
            "10.0.0.5": ("lb-1", "loadbalancer"), "10.0.0.6": ("lb-2", "loadbalancer"),
            "10.0.0.11": ("cp-1", "control_plane"), "10.0.0.12": ("cp-2", "control_plane"),
            "10.0.0.13": ("cp-3", "control_plane"), "10.0.0.21": ("w-1", "worker"),
        }
        fake = build_default_fake(hosts)
        fresh_key = "b" * 64
        fake.responders.insert(0, (
            lambda h, s: "upload-certs" in s and "kubeadm init phase" in s,
            f"[upload-certs] Using certificate key:\n{fresh_key}\n",
        ))
        state = {"failed_once": False}

        def _cp_join_fail(host, script):
            if "--control-plane" in script and host == "10.0.0.12" and not state["failed_once"]:
                state["failed_once"] = True
                return True
            return False

        fake.responders.insert(
            0, (_cp_join_fail, SshCommandError("boom", exit_code=1, output="etcd sad"))
        )
        set_transport_factory(lambda: fake)
        payload = make_build_payload(
            name="ha-retry", topology="stacked_ha",
            endpoint_mode="managed_haproxy", nodes=HA_NODES, vipAddress="10.0.0.100",
        )
        build = create_build(client, admin_token, ssh_profile, payload)
        client.post(f"/api/cluster-builds/{build['id']}/preflight",
                    headers=auth_headers(admin_token))
        client.post(f"/api/cluster-builds/{build['id']}/start",
                    json={"ackWarnings": ["ack"]}, headers=auth_headers(admin_token))

        row = db.session.get(ClusterBuild, build["id"])
        assert row.status == "failed"
        # Age the certificate key past its TTL, as a real retry-later would.
        row.cert_key_expires_at = datetime.now(timezone.utc) - timedelta(hours=1)
        db.session.commit()

        response = client.post(f"/api/cluster-builds/{build['id']}/retry",
                               headers=auth_headers(admin_token))
        assert response.status_code == 200
        data = response.get_json()["data"]
        assert data["status"] == "completed", data.get("error")
        assert [s for _, s in fake.calls if "kubeadm init phase upload-certs" in s], (
            "an expired certificate key must be re-uploaded, not fatal"
        )
        joins = [s for _, s in fake.calls if "--control-plane" in s and "--certificate-key" in s]
        assert any(fresh_key in s for s in joins), "the rejoin must use the fresh key"

    def test_smoke_pod_deleted_before_verify_runs(
        self, client, admin_token, ssh_profile, fake_ssh, app
    ):
        """A leftover pod from a previous verify would make `kubectl run` fail
        with AlreadyExists on every subsequent attempt."""
        hosts = {"10.0.0.11": ("cp-1", "control_plane"), "10.0.0.21": ("w-1", "worker")}
        fake = build_default_fake(hosts)
        set_transport_factory(lambda: fake)
        run_full_build(client, admin_token, ssh_profile, fake,
                       make_build_payload(nodes=SINGLE_CP_NODES))
        scripts = [s for _, s in fake.calls]
        delete_at = next(i for i, s in enumerate(scripts) if "delete pod kubesight-smoke" in s)
        run_at = next(i for i, s in enumerate(scripts) if "run kubesight-smoke" in s)
        assert delete_at < run_at, "the stale smoke pod must be cleared before running one"


    def test_cancelled_build_can_be_resumed(
        self, client, admin_token, ssh_profile, fake_ssh, app
    ):
        """Cancelling stops the phase machine but keeps completed phases — a
        cancelled build must be resumable, not a delete-only dead end."""
        hosts = {"10.0.0.11": ("cp-1", "control_plane"), "10.0.0.21": ("w-1", "worker")}
        fake = build_default_fake(hosts)
        state = {"failed_once": False}

        def _join_fail(host, script):
            if "kubeadm join" in script and host == "10.0.0.21" and not state["failed_once"]:
                state["failed_once"] = True
                return True
            return False

        fake.responders.insert(
            0, (_join_fail, SshCommandError("boom", exit_code=1, output="interrupted"))
        )
        set_transport_factory(lambda: fake)
        build = create_build(client, admin_token, ssh_profile,
                             make_build_payload(nodes=SINGLE_CP_NODES))
        client.post(f"/api/cluster-builds/{build['id']}/preflight",
                    headers=auth_headers(admin_token))
        client.post(f"/api/cluster-builds/{build['id']}/start",
                    json={"ackWarnings": ["ack"]}, headers=auth_headers(admin_token))

        # Stand in for a user cancel that landed mid-build: earlier phases are
        # done, the worker join never finished.
        row = db.session.get(ClusterBuild, build["id"])
        assert row.status == "failed"
        row.status = "cancelled"
        db.session.commit()

        response = client.post(f"/api/cluster-builds/{build['id']}/retry",
                               headers=auth_headers(admin_token))
        assert response.status_code == 200, response.get_json()
        data = response.get_json()["data"]
        assert data["status"] == "completed", data.get("error")
        # Resumed, not restarted: init did not run a second time.
        assert len([s for _, s in fake.calls if "kubeadm init " in s]) == 1

    def test_resume_after_all_joins_done_does_not_mint_new_secrets(
        self, client, admin_token, ssh_profile, fake_ssh, app
    ):
        """Join secrets are destroyed on completion; a resume whose join steps
        are all complete must not try to re-mint them."""
        hosts = {"10.0.0.11": ("cp-1", "control_plane"), "10.0.0.21": ("w-1", "worker")}
        fake = build_default_fake(hosts)
        set_transport_factory(lambda: fake)
        data = run_full_build(client, admin_token, ssh_profile, fake,
                              make_build_payload(nodes=SINGLE_CP_NODES))
        assert data["status"] == "completed"

        row = db.session.get(ClusterBuild, data["id"])
        assert row.join_command_cipher is None  # destroyed on completion
        row.status = "building"
        db.session.commit()
        executor_mod.start_build_worker(row.id)

        db.session.refresh(row)
        assert row.status == "completed", row.error
        assert not [s for _, s in fake.calls if "kubeadm token create" in s]


    def test_no_op_worker_run_releases_the_in_flight_claim(
        self, client, admin_token, ssh_profile, fake_ssh, app
    ):
        """start_build_worker claims the id before the worker checks whether
        there is anything to do. If that claim is not released on the no-op
        path, every later start/retry of the build is silently ignored."""
        hosts = {"10.0.0.11": ("cp-1", "control_plane"), "10.0.0.21": ("w-1", "worker")}
        fake = build_default_fake(hosts)
        set_transport_factory(lambda: fake)
        build = create_build(client, admin_token, ssh_profile,
                             make_build_payload(nodes=SINGLE_CP_NODES))
        client.post(f"/api/cluster-builds/{build['id']}/preflight",
                    headers=auth_headers(admin_token))

        # Not in status 'building' ⇒ the worker returns immediately.
        executor_mod.start_build_worker(build["id"])
        assert build["id"] not in executor_mod._active_builds

        response = client.post(f"/api/cluster-builds/{build['id']}/start",
                               json={"ackWarnings": ["ack"]},
                               headers=auth_headers(admin_token))
        assert response.get_json()["data"]["status"] == "completed"


class TestPreflightRecovery:
    def test_crash_does_not_strand_build_in_preflighting(
        self, client, admin_token, ssh_profile, fake_ssh, app
    ):
        """'preflighting' blocks both editing and re-running — a crashed probe
        must never leave a build parked there."""
        build = create_build(client, admin_token, ssh_profile,
                             make_build_payload(nodes=SINGLE_CP_NODES))

        def _explode(*args, **kwargs):
            raise RuntimeError("vcenter melted")

        original = preflight_mod.vsphere_checks
        preflight_mod.vsphere_checks = _explode
        try:
            response = client.post(f"/api/cluster-builds/{build['id']}/preflight",
                                   headers=auth_headers(admin_token))
        finally:
            preflight_mod.vsphere_checks = original

        assert response.status_code == 400
        row = db.session.get(ClusterBuild, build["id"])
        assert row.status == "preflight_failed"

        # And the build is workable again: a clean preflight recovers it.
        fake = build_default_fake({"10.0.0.11": ("cp-1", "control_plane"),
                                   "10.0.0.21": ("w-1", "worker")})
        set_transport_factory(lambda: fake)
        response = client.post(f"/api/cluster-builds/{build['id']}/preflight",
                               headers=auth_headers(admin_token))
        assert response.status_code == 200
        assert response.get_json()["data"]["status"] in ("pass", "warn")

    def test_scheduler_recovers_interrupted_preflight(self, app, ssh_profile):
        """A backend restart mid-probe leaves 'preflighting' with no worker."""
        build = ClusterBuild(
            name="stranded", status="preflighting", k8s_version="1.32.4",
            topology_type="single_cp", endpoint_mode="manual_endpoint",
            control_plane_endpoint="10.0.0.100:6443", cni_plugin="calico",
            cni_version="3.28.2", pod_cidr="10.244.0.0/16",
            service_cidr="10.96.0.0/12",
            updated_at=datetime.now(timezone.utc) - timedelta(minutes=30),
        )
        db.session.add(build)
        db.session.commit()

        executor_mod.advance_cluster_builds()

        db.session.refresh(build)
        assert build.status == "preflight_failed"
        assert "interrupted" in (build.error or "").lower()


class TestSchedulerOwnership:
    @pytest.mark.parametrize(
        "debug,run_main,expected",
        [
            ("true", None, False),
            ("true", "false", False),
            ("true", "true", True),
            ("false", None, True),
            ("false", "true", True),
        ],
    )
    def test_debug_reloader_only_starts_scheduler_in_serving_child(
        self, monkeypatch, debug, run_main, expected
    ):
        monkeypatch.setenv("FLASK_DEBUG", debug)
        if run_main is None:
            monkeypatch.delenv("WERKZEUG_RUN_MAIN", raising=False)
        else:
            monkeypatch.setenv("WERKZEUG_RUN_MAIN", run_main)

        assert scheduler_mod._should_start_in_process() is expected


class TestPauseImage:
    def test_tag_tracks_the_kubernetes_minor(self):
        # kubeadm pins pause 3.9 through 1.30 and 3.10 from 1.31 — a mirror
        # only carries what kubeadm's image list names.
        assert kubeadm_mod.pause_image_tag("1.30.12") == "3.9"
        assert kubeadm_mod.pause_image_tag("1.32.4") == "3.10"
        assert kubeadm_mod.pause_image_tag("v1.31.8") == "3.10"


# ---------------------------------------------------------------------------
# vSphere-backed node enrichment
# ---------------------------------------------------------------------------

class TestVSphereNodePicker:
    def test_nodes_from_inventory(self, client, admin_token, ssh_profile, app):
        conn = vsphere_service.create_connection(
            {"name": "vc", "baseUrl": "https://vcenter.lab", "username": "ro",
             "password": "pw"}
        )
        vsphere_service.set_inventory_fetcher(lambda cfg: [
            {"moid": "vm-100", "name": "k8s-cp-1", "powerState": "POWERED_ON",
             "cpuCount": 4, "memoryMiB": 8192, "esxiHost": "esxi-1",
             "datastore": "ds-1", "toolsRunState": "RUNNING",
             "toolsVersionStatus": "CURRENT", "guestHostname": "k8s-cp-1",
             "guestIp": "10.0.0.11", "guestFamily": "LINUX",
             "guestOs": "Ubuntu Linux (64-bit)"},
            {"moid": "vm-101", "name": "k8s-w-1", "powerState": "POWERED_ON",
             "cpuCount": 4, "memoryMiB": 8192, "esxiHost": "esxi-2",
             "datastore": "ds-1", "toolsRunState": None,
             "toolsVersionStatus": None, "guestHostname": None,
             "guestIp": None, "guestFamily": None, "guestOs": None},
        ])
        try:
            payload = make_build_payload(nodes=[
                {"role": "control_plane", "vsphereVmMoid": "vm-100"},
                # Tools-less VM: manual IP override required and provided.
                {"role": "worker", "vsphereVmMoid": "vm-101", "address": "10.0.0.21"},
            ])
            payload["vsphereConnectionId"] = conn["id"]
            build = create_build(client, admin_token, ssh_profile, payload)
            nodes = build["nodes"]
            assert nodes[0]["address"] == "10.0.0.11"
            assert nodes[0]["addressSource"] == "vmware_tools"
            assert nodes[0]["vsphereHost"] == "esxi-1"
            assert nodes[1]["address"] == "10.0.0.21"
            assert nodes[1]["addressSource"] == "manual"
        finally:
            vsphere_service.set_inventory_fetcher(None)

    def test_toolless_vm_without_manual_ip_rejected(self, client, admin_token,
                                                    ssh_profile, app):
        conn = vsphere_service.create_connection(
            {"name": "vc2", "baseUrl": "https://vcenter.lab", "username": "ro",
             "password": "pw"}
        )
        vsphere_service.set_inventory_fetcher(lambda cfg: [
            {"moid": "vm-200", "name": "dark-vm", "powerState": "POWERED_ON",
             "cpuCount": 4, "memoryMiB": 8192, "esxiHost": "esxi-1",
             "datastore": "ds-1", "toolsRunState": None, "guestIp": None},
        ])
        try:
            payload = make_build_payload(nodes=[
                {"role": "control_plane", "vsphereVmMoid": "vm-200"},
            ])
            payload["vsphereConnectionId"] = conn["id"]
            payload["connectionProfileId"] = ssh_profile["id"]
            response = client.post("/api/cluster-builds", json=payload,
                                   headers=auth_headers(admin_token))
            assert response.status_code == 400
            assert "manual management IP" in response.get_json()["error"]
        finally:
            vsphere_service.set_inventory_fetcher(None)
