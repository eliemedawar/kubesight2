"""KubeSight creates machines and clusters on Oracle Cloud, AWS and Google Cloud.

The same road as the VMware tests (test_cluster_provisioning.py) — the real job
engine, the simulated OpenTofu engine, the fake SSH transport — with each
cloud's adapter answering instead of vCenter, in simulation mode (no cloud API
is called). Per cloud: an account, VMs only, a kubeadm cluster behind the
cloud's load balancer, a managed cluster (OKE / EKS / GKE), growth, destroy.
"""

from __future__ import annotations

import base64
import hashlib
import json

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ed25519, rsa

from api.db import db
from api.models import Cluster, ClusterBuild, ClusterProvisionJob, CloudAccount, VSphereIpReservation
from api.services import ssh_profile_service
from api.services.cluster_build import executor
from api.services.cluster_build.provisioning import cloud_build, clouds, jobs, state_store, tofu_runner
from api.services.cluster_build.provisioning.clouds import base as cloud_base
from api.services.ssh import set_transport_factory

from tests.test_cluster_builds import (  # noqa: F401  (fixtures are used by name)
    auth_headers,
    build_default_fake,
    fake_ssh,
    no_network_cni_manifest,
)

PROVIDERS = ("oci", "aws", "gcp")


@pytest.fixture(autouse=True)
def _cluster_needs_no_approval(no_cluster_approvals):
    yield


@pytest.fixture()
def simulated_cloud(tmp_path, monkeypatch):
    """Simulated OpenTofu + simulated cloud inventories, SSH that always answers."""
    monkeypatch.setenv("KUBESIGHT_PROVISIONING_SIMULATE", "1")
    monkeypatch.setenv("KUBESIGHT_TOFU_WORKDIR", str(tmp_path / "tofu"))
    tofu_runner.set_engine(tofu_runner.SimulatedEngine())
    jobs.set_ssh_waiter(lambda targets, timeout_s, on_ready: [on_ready(n, True, "") for n, _ in targets])
    yield
    tofu_runner.set_engine(None)
    jobs.set_ssh_waiter(None)
    cloud_build.set_token_minter(None)


# ---------------------------------------------------------------------------
# Keys and accounts
# ---------------------------------------------------------------------------

def _rsa_pem() -> str:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption(),
    ).decode("ascii")


def _oci_fingerprint(pem: str) -> str:
    key = serialization.load_pem_private_key(pem.encode("ascii"), password=None)
    der = key.public_key().public_bytes(
        serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    digest = hashlib.md5(der).hexdigest()  # noqa: S324 — OCI's own fingerprint format
    return ":".join(digest[i:i + 2] for i in range(0, len(digest), 2))


SECRET_MARKERS = {"aws": "wJalrXUtnFEMI-secret-K7MDENG", "gcp": None, "oci": None}


def account_payload(provider: str) -> dict:
    if provider == "aws":
        return {
            "name": "aws-prod", "provider": "aws", "region": "eu-west-1",
            "settings": {"accessKeyId": "AKIAIOSFODNN7EXAMPLE"},
            "secrets": {"secretAccessKey": SECRET_MARKERS["aws"]},
        }
    if provider == "gcp":
        pem = _rsa_pem()
        key = {
            "type": "service_account", "project_id": "areeba-k8s",
            "private_key_id": "abc123", "private_key": pem,
            "client_email": "kubesight@areeba-k8s.iam.gserviceaccount.com",
            "client_id": "1", "token_uri": "https://oauth2.googleapis.com/token",
        }
        return {
            "name": "gcp-prod", "provider": "gcp", "region": "europe-west1",
            "settings": {"projectId": "areeba-k8s"},
            "secrets": {"serviceAccountKey": json.dumps(key)},
        }
    pem = _rsa_pem()
    return {
        "name": "oci-prod", "provider": "oci", "region": "me-dubai-1",
        "settings": {
            "tenancyOcid": "ocid1.tenancy.oc1..aaaaaaaatenancy",
            "userOcid": "ocid1.user.oc1..aaaaaaaauser",
            "fingerprint": _oci_fingerprint(pem),
            "compartmentOcid": "ocid1.compartment.oc1..aaaaaaaacomp",
        },
        "secrets": {"privateKey": pem},
    }


def create_account(client, token, provider):
    response = client.post("/api/cloud-accounts", json=account_payload(provider), headers=auth_headers(token))
    assert response.status_code == 201, response.get_json()
    return response.get_json()["data"]["account"]


@pytest.fixture()
def ssh_key_profile(app):
    """An SSH route whose credential is a real key (the cloud gets its public half)."""
    key = ed25519.Ed25519PrivateKey.generate()
    pem = key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.OpenSSH, serialization.NoEncryption(),
    ).decode("ascii")
    cred = ssh_profile_service.create_credential({
        "name": "cloud-key", "username": "kubesight", "authMethod": "key",
        "secret": pem, "sudoMode": "nopasswd",
    })
    profile = ssh_profile_service.create_profile({
        "name": "cloud-route", "credentialId": cred["id"], "routeMode": "direct", "hostKeyPolicy": "tofu",
    })
    public = key.public_key().public_bytes(
        serialization.Encoding.OpenSSH, serialization.PublicFormat.OpenSSH,
    ).decode("ascii")
    return {**profile, "publicKey": public}


def inventory_of(client, token, account):
    regions = client.get(f"/api/cloud-accounts/{account['id']}/regions", headers=auth_headers(token))
    assert regions.status_code == 200, regions.get_json()
    region = regions.get_json()["data"]["regions"][0]["id"]
    response = client.get(
        f"/api/cloud-accounts/{account['id']}/inventory?region={region}", headers=auth_headers(token),
    )
    assert response.status_code == 200, response.get_json()
    return response.get_json()["data"]


def placement(client, token, account, *, kind="vms"):
    inv = inventory_of(client, token, account)
    network = next(n for n in inv["networks"] if n["subnets"])
    subnet = network["subnets"][0]
    spec = {
        "cloudAccountId": account["id"], "region": inv["region"], "kind": kind,
        "networkId": network["id"], "networkName": network["name"], "networkCidr": network.get("cidr") or "",
        "subnetId": subnet["id"], "subnetName": subnet["name"], "subnetCidr": subnet.get("cidr") or "",
        "zone": subnet.get("zone") or (inv["zones"][0]["id"] if inv["zones"] else None),
        "allowedCidrs": ["10.50.0.0/16"],
    }
    if inv.get("images"):
        spec["image"] = inv["images"][0]
    if inv.get("compartments"):
        spec["compartmentId"] = inv["compartments"][0]["id"]
    if kind == "managed":
        spec["managed"] = {"kubernetesVersion": inv["kubernetesVersions"][0]}
        zones_seen, subnet_ids = set(), []
        for net in inv["networks"]:
            if net["id"] != network["id"]:
                continue
            for item in net["subnets"]:
                if item.get("zone") not in zones_seen:
                    zones_seen.add(item.get("zone"))
                    subnet_ids.append(item["id"])
        spec["subnetIds"] = subnet_ids
        if inv.get("images"):
            spec["nodeImageId"] = inv["images"][0]["id"]
    return spec


def make_cloud_build(client, token, account, profile, *, name, counts=None, vms_only=False,
                     vm_count=None, kind="vms", template="small"):
    spec = placement(client, token, account, kind=kind)
    sizes = {
        "loadbalancer": {"cpu": 2, "memoryGb": 4, "diskGb": 40},
        "controlPlane": {"cpu": 4, "memoryGb": 8, "diskGb": 80},
        "worker": {"cpu": 4, "memoryGb": 16, "diskGb": 100},
        "vm": {"cpu": 2, "memoryGb": 4, "diskGb": 40},
    }
    if vms_only:
        spec.update(vmCount=vm_count or 2, sizes={"vm": sizes["vm"]})
    else:
        spec.update(counts=counts or {"loadbalancer": 1, "controlPlane": 1, "worker": 2}, sizes=sizes)
    payload = {
        "name": name, "k8sVersion": "1.32.4", "machineSource": account["provider"],
        "templateId": template, "vmsOnly": vms_only, "cniPlugin": "calico",
        "podCidr": "10.244.0.0/16", "serviceCidr": "10.96.0.0/12",
        "provisioning": spec,
    }
    if kind != "managed":
        payload["connectionProfileId"] = profile["id"]
    response = client.post("/api/cluster-builds", json=payload, headers=auth_headers(token))
    assert response.status_code == 201, response.get_json()
    return response.get_json()["data"]


def plan(client, token, build_id, expect=202):
    response = client.post(f"/api/cluster-builds/{build_id}/provision/plan", headers=auth_headers(token))
    assert response.status_code == expect, response.get_json()
    return response.get_json()["data"]


def apply(client, token, build_id, job_id):
    response = client.post(
        f"/api/cluster-builds/{build_id}/provision/jobs/{job_id}/apply", headers=auth_headers(token)
    )
    assert response.status_code == 200, response.get_json()
    return response.get_json()["data"]


def pin_addresses(monkeypatch, provider, addresses):
    """The simulator's addresses, made predictable: VM name → private IP."""
    module = clouds.adapter(provider)
    original = module.vm_facts

    def facts(attrs):
        out = dict(original(attrs))
        name = attrs.get("name")
        if name in addresses:
            out["address"] = addresses[name]
        return out

    monkeypatch.setattr(module, "vm_facts", facts)


# ---------------------------------------------------------------------------
# Accounts
# ---------------------------------------------------------------------------

class TestAccounts:
    @pytest.mark.parametrize("provider", PROVIDERS)
    def test_secrets_never_come_back(self, client, admin_token, simulated_cloud, provider):
        payload = account_payload(provider)
        account = create_account(client, admin_token, provider)
        assert account["provider"] == provider and account["providerLabel"]
        assert account["secretsSet"] and all(account["secretsSet"].values())
        body = json.dumps(client.get("/api/cloud-accounts", headers=auth_headers(admin_token)).get_json())
        for secret in payload["secrets"].values():
            assert secret not in body
            # A key's body (not its PEM armour: a form placeholder may show that).
            for line in secret.replace("\\n", "\n").splitlines():
                if len(line) > 40 and "PRIVATE KEY" not in line:
                    assert line not in body
        row = db.session.get(CloudAccount, account["id"])
        assert all(value not in (row.secrets_cipher or "") for value in payload["secrets"].values())

    @pytest.mark.parametrize("provider", PROVIDERS)
    def test_blank_secret_keeps_the_stored_one(self, client, admin_token, simulated_cloud, provider):
        account = create_account(client, admin_token, provider)
        before = db.session.get(CloudAccount, account["id"]).secrets_cipher
        # The form sends the settings it showed (an OCI fingerprint belongs to
        # the stored key) and leaves every secret blank.
        payload = {**account_payload(provider), "name": f"{provider}-renamed",
                   "settings": account["settings"],
                   "secrets": {key: "" for key in account_payload(provider)["secrets"]}}
        response = client.put(f"/api/cloud-accounts/{account['id']}", json=payload,
                              headers=auth_headers(admin_token))
        assert response.status_code == 200, response.get_json()
        row = db.session.get(CloudAccount, account["id"])
        assert row.name == f"{provider}-renamed"
        from api.services.cluster_build.provisioning.clouds import accounts

        assert accounts._secrets(row) == json.loads(
            __import__("api.secret_encryption", fromlist=["decrypt_secret"]).decrypt_secret(before)
        )

    def test_catalog_lists_the_three_clouds(self, client, admin_token, simulated_cloud):
        data = client.get("/api/cloud-accounts", headers=auth_headers(admin_token)).get_json()["data"]
        assert [p["id"] for p in data["providers"]] == list(PROVIDERS)
        assert data["simulated"] is True
        for provider in data["providers"]:
            assert provider["fields"] and provider["managedLabel"]
            assert provider["tofuProvider"]["version"]

    @pytest.mark.parametrize("provider", PROVIDERS)
    def test_test_and_inventory(self, client, admin_token, simulated_cloud, provider):
        account = create_account(client, admin_token, provider)
        response = client.post(f"/api/cloud-accounts/{account['id']}/test", headers=auth_headers(admin_token))
        data = response.get_json()["data"]
        assert data["ok"] is True and data["account"]["lastTestStatus"] == "ok"
        inv = inventory_of(client, admin_token, account)
        assert set(cloud_base.empty_inventory("x")) <= set(inv)
        assert inv["networks"] and inv["images"] and inv["machineTypes"] and inv["kubernetesVersions"]

    @pytest.mark.parametrize("provider", PROVIDERS)
    def test_resolve_sizes(self, client, admin_token, simulated_cloud, provider):
        account = create_account(client, admin_token, provider)
        response = client.post(
            f"/api/cloud-accounts/{account['id']}/resolve-sizes",
            json={"sizes": {"controlPlane": {"cpu": 4, "memoryGb": 8}, "worker": {"cpu": 8, "memoryGb": 32}}},
            headers=auth_headers(admin_token),
        )
        assert response.status_code == 200, response.get_json()
        sizes = response.get_json()["data"]["sizes"]
        for role, wanted in (("controlPlane", (4, 8)), ("worker", (8, 32))):
            assert sizes[role]["type"], sizes[role]
            assert sizes[role]["cpu"] >= wanted[0] and sizes[role]["memoryGb"] >= wanted[1]

    def test_writes_need_signing_in(self, client, simulated_cloud, app):
        response = client.post("/api/cloud-accounts", json=account_payload("aws"))
        assert response.status_code in (401, 403)

    def test_account_in_use_cannot_be_deleted(self, client, admin_token, simulated_cloud, ssh_key_profile):
        account = create_account(client, admin_token, "aws")
        make_cloud_build(client, admin_token, account, ssh_key_profile, name="inuse-01")
        response = client.delete(f"/api/cloud-accounts/{account['id']}", headers=auth_headers(admin_token))
        assert response.status_code == 400 and "still use" in response.get_json()["error"]


# ---------------------------------------------------------------------------
# The spec
# ---------------------------------------------------------------------------

class TestSpec:
    def test_allowed_networks_are_required(self, client, admin_token, simulated_cloud, ssh_key_profile):
        account = create_account(client, admin_token, "aws")
        spec = placement(client, admin_token, account)
        spec.update(allowedCidrs=[], counts={"loadbalancer": 0, "controlPlane": 1, "worker": 1})
        response = client.post("/api/cluster-builds", json={
            "name": "noallow", "k8sVersion": "1.32.4", "machineSource": "aws",
            "connectionProfileId": ssh_key_profile["id"], "provisioning": spec,
        }, headers=auth_headers(admin_token))
        assert response.status_code == 400 and "allowed" in response.get_json()["error"].lower()

    def test_the_whole_internet_is_refused(self):
        with pytest.raises(ValueError, match="everyone"):
            cloud_base.normalize_cidrs(["0.0.0.0/0"])

    def test_account_must_match_the_cloud(self, client, admin_token, simulated_cloud, ssh_key_profile):
        account = create_account(client, admin_token, "aws")
        spec = placement(client, admin_token, account)
        spec["counts"] = {"loadbalancer": 0, "controlPlane": 1, "worker": 1}
        response = client.post("/api/cluster-builds", json={
            "name": "mismatch", "k8sVersion": "1.32.4", "machineSource": "gcp",
            "connectionProfileId": ssh_key_profile["id"], "provisioning": spec,
        }, headers=auth_headers(admin_token))
        assert response.status_code == 400 and "not Google" in response.get_json()["error"]

    def test_name_must_start_with_a_letter(self, client, admin_token, simulated_cloud, ssh_key_profile):
        account = create_account(client, admin_token, "oci")
        spec = placement(client, admin_token, account)
        spec["counts"] = {"loadbalancer": 0, "controlPlane": 1, "worker": 1}
        response = client.post("/api/cluster-builds", json={
            "name": "1st-cluster", "k8sVersion": "1.32.4", "machineSource": "oci",
            "connectionProfileId": ssh_key_profile["id"], "provisioning": spec,
        }, headers=auth_headers(admin_token))
        assert response.status_code == 400 and "letter" in response.get_json()["error"]

    @pytest.mark.parametrize("provider", PROVIDERS)
    def test_shape_on_a_cloud(self, client, admin_token, simulated_cloud, ssh_key_profile, provider):
        account = create_account(client, admin_token, provider)
        ha = make_cloud_build(client, admin_token, account, ssh_key_profile, name="ha-01",
                              counts={"loadbalancer": 2, "controlPlane": 3, "worker": 3})
        assert ha["topologyType"] == "stacked_ha" and ha["endpointMode"] == "external_lb"
        assert ha["cloud"]["loadBalancer"] is True and ha["cloud"]["provider"] == provider
        lab = make_cloud_build(client, admin_token, account, ssh_key_profile, name="lab-01",
                               counts={"loadbalancer": 0, "controlPlane": 1, "worker": 1})
        assert lab["endpointMode"] == "manual_endpoint" and lab["cloud"]["loadBalancer"] is False


# ---------------------------------------------------------------------------
# Lifecycles
# ---------------------------------------------------------------------------

class TestVmsOnly:
    @pytest.mark.parametrize("provider", PROVIDERS)
    def test_plan_apply_ready(self, client, admin_token, simulated_cloud, ssh_key_profile, provider):
        account = create_account(client, admin_token, provider)
        build = make_cloud_build(client, admin_token, account, ssh_key_profile, name="tools-01",
                                 vms_only=True, vm_count=2)
        planned = plan(client, admin_token, build["id"])
        job = planned["provisioning"]["job"]
        assert job["status"] == "planned", job.get("error")
        vms = {r["title"] for r in job["summary"]["resources"] if r["kind"] == "vm"}
        assert vms == {"tools-01-vm-1", "tools-01-vm-2"}
        # No vCenter-style address ranges: the cloud picks the addresses.
        assert VSphereIpReservation.query.count() == 0
        assert all(n["address"] == "" for n in planned["nodes"])
        stored = db.session.get(ClusterProvisionJob, job["id"])
        config = json.dumps(stored.config_json)
        assert ssh_key_profile["publicKey"].split()[1] in config  # the key goes onto the VMs
        for secret in account_payload(provider)["secrets"].values():
            assert secret not in config and secret not in (stored.plan_text or "")
        assert config.count('"backend": {"http": {}}') == 1

        done = apply(client, admin_token, build["id"], job["id"])
        assert done["status"] == "vms_ready", done["provisioning"]["job"]
        assert all(n["address"] for n in done["nodes"])
        assert len({n["address"] for n in done["nodes"]}) == 2
        assert set(state_store.vm_instances(build["id"])) == {"tools-01-vm-1", "tools-01-vm-2"}
        assert done["canDestroy"] is True

    def test_install_kubernetes_without_a_balancer_only(
        self, client, admin_token, simulated_cloud, ssh_key_profile
    ):
        account = create_account(client, admin_token, "aws")
        build = make_cloud_build(client, admin_token, account, ssh_key_profile, name="tools-02",
                                 vms_only=True, vm_count=4)
        job = plan(client, admin_token, build["id"])["provisioning"]["job"]
        apply(client, admin_token, build["id"], job["id"])
        response = client.post(
            f"/api/cluster-builds/{build['id']}/provision/install-kubernetes",
            json={"counts": {"loadbalancer": 1, "controlPlane": 1, "worker": 2}},
            headers=auth_headers(admin_token),
        )
        assert response.status_code == 400 and "load balancer" in response.get_json()["error"]


class TestKubeadmOnCloud:
    @pytest.mark.parametrize("provider", PROVIDERS)
    def test_small_shape_behind_the_cloud_balancer(
        self, client, admin_token, simulated_cloud, ssh_key_profile, provider, monkeypatch
    ):
        addresses = {"uat-02-cp-1": "10.60.1.11", "uat-02-wk-1": "10.60.1.21", "uat-02-wk-2": "10.60.1.22"}
        pin_addresses(monkeypatch, provider, addresses)
        fake = build_default_fake({
            "10.60.1.11": ("uat-02-cp-1", "control_plane"),
            "10.60.1.21": ("uat-02-wk-1", "worker"),
            "10.60.1.22": ("uat-02-wk-2", "worker"),
        })
        set_transport_factory(lambda: fake)
        try:
            account = create_account(client, admin_token, provider)
            build = make_cloud_build(client, admin_token, account, ssh_key_profile, name="uat-02")
            job = plan(client, admin_token, build["id"])["provisioning"]["job"]
            assert job["status"] == "planned", job.get("error")
            kinds = {r["kind"] for r in job["summary"]["resources"]}
            assert "lb" in kinds, job["summary"]["resources"]
            # The balancer is the cloud's: three VMs, no "-lb-" machine.
            vms = {r["title"] for r in job["summary"]["resources"] if r["kind"] == "vm"}
            assert vms == set(addresses)

            done = apply(client, admin_token, build["id"], job["id"])
            assert done["status"] == "completed", (done.get("error"), done["provisioning"]["job"])
            assert done["resultClusterId"]
            assert done["endpointMode"] == "external_lb"
            host = done["controlPlaneEndpoint"].rsplit(":", 1)[0]
            assert done["controlPlaneEndpoint"].endswith(":6443") and host not in addresses.values()
            assert {n["address"] for n in done["nodes"]} == set(addresses.values())
            assert all(n["role"] != "loadbalancer" for n in done["nodes"])
            # No haproxy / keepalived went anywhere: the cloud balances the API.
            assert not any("keepalived" in script for _, script in fake.calls)
        finally:
            set_transport_factory(None)

    def test_destroy_deletes_everything(self, client, admin_token, simulated_cloud, ssh_key_profile, monkeypatch):
        account = create_account(client, admin_token, "gcp")
        build = make_cloud_build(client, admin_token, account, ssh_key_profile, name="gone-01",
                                 vms_only=True, vm_count=1)
        job = plan(client, admin_token, build["id"])["provisioning"]["job"]
        apply(client, admin_token, build["id"], job["id"])
        response = client.post(
            f"/api/cluster-builds/{build['id']}/provision/destroy",
            json={"confirmName": "gone-01", "reason": "done"}, headers=auth_headers(admin_token),
        )
        assert response.status_code == 202, response.get_json()
        data = response.get_json()["data"]
        assert data["status"] == "destroyed", data["provisioning"]["job"]
        assert not state_store.has_resources(build["id"])


class TestManaged:
    @pytest.mark.parametrize("provider", PROVIDERS)
    def test_managed_cluster_is_registered(self, client, admin_token, simulated_cloud, ssh_key_profile,
                                           provider, monkeypatch):
        module = clouds.adapter(provider)
        ca = base64.b64encode(b"-----BEGIN CERTIFICATE-----\nfake\n-----END CERTIFICATE-----\n").decode()
        seen = {}

        def access(account, resources, spec):
            seen["types"] = {item["type"] for item in resources}
            return {"server": "https://10.60.0.5:6443", "caData": ca, "token": "cloud-token"}

        monkeypatch.setattr(module, "managed_access", access)
        cloud_build.set_token_minter(lambda build, server, ca_data, token, log: f"sa-{token}")

        account = create_account(client, admin_token, provider)
        build = make_cloud_build(client, admin_token, account, ssh_key_profile, name="mgd-01", kind="managed",
                                 counts={"loadbalancer": 0, "controlPlane": 0, "worker": 3})
        assert build["cloud"]["kind"] == "managed" and build["cloud"]["managed"]["nodeCount"] == 3
        job = plan(client, admin_token, build["id"])["provisioning"]["job"]
        assert job["status"] == "planned", job.get("error")
        kinds = {r["kind"] for r in job["summary"]["resources"]}
        assert {"cluster", "nodepool"} <= kinds, job["summary"]["resources"]
        assert not [r for r in job["summary"]["resources"] if r["kind"] == "vm"]

        # Preflight and the kubeadm build are not for a managed cluster.
        refused = client.post(f"/api/cluster-builds/{build['id']}/preflight", headers=auth_headers(admin_token))
        assert refused.status_code == 400

        done = apply(client, admin_token, build["id"], job["id"])
        assert done["status"] == "completed", (done.get("error"), done["provisioning"]["job"])
        assert done["resultClusterId"] and done["nodes"] == []
        assert any(t.endswith("_cluster") for t in seen["types"])
        cluster = Cluster.query.get(int(done["resultClusterId"].split("-")[-1]))
        assert cluster is not None and cluster.host == "10.60.0.5" and cluster.port == 6443

        # Day two: the node pool grows.
        response = client.post(
            f"/api/cluster-builds/{build['id']}/provision/grow-plan", json={"workers": 2},
            headers=auth_headers(admin_token),
        )
        assert response.status_code == 202, response.get_json()
        grow = response.get_json()["data"]["provisioning"]["job"]
        assert grow["status"] == "planned", grow.get("error")
        grown = apply(client, admin_token, build["id"], grow["id"])
        assert grown["status"] == "completed"
        assert grown["cloud"]["managed"]["nodeCount"] == 5

    def test_control_planes_cannot_be_added_to_a_managed_cluster(
        self, client, admin_token, simulated_cloud, ssh_key_profile, monkeypatch
    ):
        module = clouds.adapter("aws")
        monkeypatch.setattr(module, "managed_access", lambda a, r, s: {
            "server": "https://10.60.0.5", "caData": "Zg==", "token": "t"})
        cloud_build.set_token_minter(lambda *args: "sa")
        account = create_account(client, admin_token, "aws")
        build = make_cloud_build(client, admin_token, account, ssh_key_profile, name="mgd-02", kind="managed",
                                 counts={"loadbalancer": 0, "controlPlane": 0, "worker": 2})
        job = plan(client, admin_token, build["id"])["provisioning"]["job"]
        apply(client, admin_token, build["id"], job["id"])
        response = client.post(
            f"/api/cluster-builds/{build['id']}/provision/grow-plan", json={"controlPlanes": 2},
            headers=auth_headers(admin_token),
        )
        assert response.status_code == 400 and "control plane" in response.get_json()["error"]


# ---------------------------------------------------------------------------
# Small pieces
# ---------------------------------------------------------------------------

class TestPieces:
    def test_cloud_init_lets_the_credential_in(self):
        text = cloud_base.cloud_init({"username": "kubesight", "publicKey": "ssh-ed25519 AAAA k", "password": None})
        assert text.startswith("#cloud-config\n") and "hostname: ${each.key}" in text
        user = json.loads(next(line for line in text.splitlines() if '"name": "kubesight"' in line)[4:])
        assert user["sudo"] == "ALL=(ALL) NOPASSWD:ALL" and user["ssh_authorized_keys"] == ["ssh-ed25519 AAAA k"]
        assert user["lock_passwd"] is True and "ssh_pwauth" not in text

    def test_cloud_init_password_is_escaped(self):
        text = cloud_base.cloud_init({"username": "ops", "publicKey": None, "password": "a${b}%{c}"})
        assert "ssh_pwauth: true" in text and "a$${b}%%{c}" in text

    def test_root_credential(self):
        text = cloud_base.cloud_init({"username": "root", "publicKey": "ssh-rsa AAAA", "password": None})
        assert "disable_root: false" in text and '"sudo"' not in text

    def test_public_key_from_private_keys(self):
        ed = ed25519.Ed25519PrivateKey.generate()
        openssh = ed.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.OpenSSH,
                                   serialization.NoEncryption()).decode()
        assert cloud_build.public_key_from_private(openssh).startswith("ssh-ed25519 ")
        pem = _rsa_pem()
        assert cloud_build.public_key_from_private(pem).startswith("ssh-rsa ")

    def test_google_join_goes_to_the_primary(self):
        build = ClusterBuild(machine_source="gcp", endpoint_mode="external_lb",
                             control_plane_endpoint="10.60.1.100:6443")
        primary = type("Node", (), {"address": "10.60.1.11"})()
        guard = executor._hairpin_guard(build, primary)
        assert "DNAT --to-destination 10.60.1.11:6443" in guard and "-d 10.60.1.100/32" in guard
        assert "trap" in guard
        build.machine_source = "aws"
        assert executor._hairpin_guard(build, primary) == ""

    def test_state_lists_cloud_vms(self):
        assert {"aws_instance", "google_compute_instance", "oci_core_instance"} <= state_store.VM_RESOURCE_TYPES

    def test_provider_mirror_covers_the_clouds(self):
        from api.services.cluster_build.provisioning import tofu_config

        text = tofu_config.cli_config("/opt/mirror")
        for provider in PROVIDERS:
            source = clouds.adapter(provider).TOFU_PROVIDER["source"]
            assert f"registry.opentofu.org/{source}" in text
