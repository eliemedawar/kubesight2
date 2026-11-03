"""Google Cloud adapter: the account, the JWT-bearer sign-in, inventory over a
fake HTTP transport, and the main.tf.json it writes for VMs and GKE."""

from __future__ import annotations

import json
import urllib.parse

import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from api.services.cluster_build.provisioning.clouds import base, gcp
from api.services.cluster_build.provisioning.clouds.base import Account, CloudError

PROJECT = "kubesight-demo"
REGION = "europe-west1"
TOKEN_URI = "https://oauth2.googleapis.com/token"
NETWORK = f"https://www.googleapis.com/compute/v1/projects/{PROJECT}/global/networks/core"
SUBNET = f"https://www.googleapis.com/compute/v1/projects/{PROJECT}/regions/{REGION}/subnetworks/apps"
IMAGE = "https://www.googleapis.com/compute/v1/projects/ubuntu-os-cloud/global/images/ubuntu-2404-noble-amd64-v20261020"


@pytest.fixture(scope="module")
def private_key():
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
    ).decode()
    return key, pem


@pytest.fixture
def key_json(private_key):
    return json.dumps({
        "type": "service_account", "project_id": PROJECT, "private_key_id": "kid-1",
        "private_key": private_key[1], "client_email": f"ks@{PROJECT}.iam.gserviceaccount.com",
        "client_id": "1", "token_uri": TOKEN_URI,
    })


@pytest.fixture
def account(key_json):
    settings, secrets = gcp.normalize_account({}, {"serviceAccountKey": key_json})
    return Account(provider="gcp", name="gcp", region=REGION, settings=settings, secrets=secrets)


class FakeGoogle:
    def __init__(self):
        self.calls = []
        self.routes = {}
        self.token_status = 200

    def __call__(self, method, url, headers, body, timeout_s, ca_pem):
        self.calls.append((method, url, headers, body))
        if url == TOKEN_URI:
            if self.token_status != 200:
                return self.token_status, {}, json.dumps(
                    {"error": "invalid_grant", "error_description": "Invalid JWT Signature."}
                ).encode()
            return 200, {}, json.dumps({"access_token": "ya29.token", "expires_in": 3599}).encode()
        assert headers.get("Authorization") == "Bearer ya29.token"
        path = url.split("?", 1)[0]
        if path in self.routes:
            status, data = self.routes[path]
            return status, {}, json.dumps(data).encode()
        return 404, {}, json.dumps({"error": {"code": 404, "message": f"{path} not found"}}).encode()


@pytest.fixture
def fake():
    google = FakeGoogle()
    base.set_transport(google)
    gcp._clear_token_cache()
    yield google
    base.set_transport(None)
    gcp._clear_token_cache()


# ---------------------------------------------------------------------------
# Account
# ---------------------------------------------------------------------------

class TestAccount:
    def test_project_defaults_to_the_keys(self, key_json):
        settings, secrets = gcp.normalize_account({}, {"serviceAccountKey": key_json})
        assert settings == {"projectId": PROJECT, "clientEmail": f"ks@{PROJECT}.iam.gserviceaccount.com"}
        assert json.loads(secrets["serviceAccountKey"])["private_key_id"] == "kid-1"

    def test_blank_key_keeps_the_stored_one(self, key_json):
        settings, secrets = gcp.normalize_account(
            {"projectId": "other-project-1"}, {"serviceAccountKey": ""},
            {"projectId": PROJECT}, {"serviceAccountKey": key_json},
        )
        assert settings["projectId"] == "other-project-1"
        assert secrets["serviceAccountKey"] == key_json

    @pytest.mark.parametrize("bad, message", [
        ("not json", "not valid JSON"),
        (json.dumps({"type": "authorized_user"}), "service account JSON key"),
        (json.dumps({"type": "service_account", "client_email": "a@b"}), "no private_key"),
    ])
    def test_rejects_keys_that_are_not_service_account_keys(self, bad, message):
        with pytest.raises(ValueError, match=message):
            gcp.normalize_account({}, {"serviceAccountKey": bad})

    def test_rejects_a_token_uri_that_is_not_google(self, key_json):
        data = json.loads(key_json)
        data["token_uri"] = "https://evil.example.com/token"
        with pytest.raises(ValueError, match="not a Google address"):
            gcp.normalize_account({}, {"serviceAccountKey": json.dumps(data)})

    def test_new_account_needs_a_key(self):
        with pytest.raises(ValueError, match="Service account key is required"):
            gcp.normalize_account({"projectId": PROJECT}, {})

    def test_identity_label_has_no_secret(self, account):
        label = gcp.identity_label(account)
        assert "ks@kubesight-demo" in label and "PRIVATE" not in label


class TestSignIn:
    def test_assertion_is_what_google_auth_sends(self, account, private_key):
        assertion = gcp.jwt_assertion(gcp._key(account), now=1_000_000)
        header = jwt.get_unverified_header(assertion)
        claims = jwt.decode(assertion, private_key[0].public_key(), algorithms=["RS256"], audience=TOKEN_URI,
                            options={"verify_exp": False, "verify_iat": False})
        assert header == {"alg": "RS256", "kid": "kid-1", "typ": "JWT"}
        assert claims == {
            "iss": f"ks@{PROJECT}.iam.gserviceaccount.com", "aud": TOKEN_URI,
            "scope": "https://www.googleapis.com/auth/cloud-platform",
            "iat": 1_000_000, "exp": 1_003_600,
        }

    def test_token_is_fetched_once_then_cached(self, account, fake):
        assert gcp.access_token(account) == "ya29.token"
        assert gcp.access_token(account) == "ya29.token"
        token_calls = [c for c in fake.calls if c[1] == TOKEN_URI]
        assert len(token_calls) == 1
        form = urllib.parse.parse_qs(token_calls[0][3].decode())
        assert form["grant_type"] == ["urn:ietf:params:oauth:grant-type:jwt-bearer"]

    def test_refusal_reads_googles_message(self, account, fake):
        fake.token_status = 400
        with pytest.raises(CloudError, match="Invalid JWT Signature"):
            gcp.access_token(account)

    def test_test_reads_the_project(self, account, fake):
        fake.routes[f"{gcp.COMPUTE}/projects/{PROJECT}"] = (200, {"name": PROJECT})
        assert gcp.test(account) == f"ks@{PROJECT}.iam.gserviceaccount.com · project {PROJECT}"

    def test_test_says_when_the_project_is_missing(self, account, fake):
        with pytest.raises(CloudError, match="does not exist"):
            gcp.test(account)

    def test_token_never_goes_to_a_link_from_a_spec(self, account, fake):
        spec = {"region": REGION, "networkId": "https://evil.example.com/steal", "subnetId": SUBNET}
        fake.routes[f"{gcp.COMPUTE}/projects/{PROJECT}/regions/{REGION}"] = (200, {"name": REGION})
        with pytest.raises(CloudError, match="not a Google Compute resource"):
            gcp.plan_checks(account, spec)
        assert not any("evil" in call[1] for call in fake.calls)


# ---------------------------------------------------------------------------
# Inventory
# ---------------------------------------------------------------------------

class TestInventory:
    def _routes(self, fake):
        compute = f"{gcp.COMPUTE}/projects/{PROJECT}"
        fake.routes[f"{compute}/regions"] = (200, {"items": [
            {"name": "europe-west1", "status": "UP"}, {"name": "old-region1", "status": "DOWN"},
        ]})
        fake.routes[f"{compute}/zones"] = (200, {"items": [
            {"name": "europe-west1-b", "region": f"{compute}/regions/europe-west1", "status": "UP"},
            {"name": "europe-west1-c", "region": f"{compute}/regions/europe-west1", "status": "DOWN"},
            {"name": "us-east1-b", "region": f"{compute}/regions/us-east1", "status": "UP"},
        ]})
        fake.routes[f"{compute}/global/networks"] = (200, {"items": [{"name": "core", "selfLink": NETWORK}]})
        fake.routes[f"{compute}/regions/{REGION}/subnetworks"] = (200, {"items": [
            {"name": "apps", "selfLink": SUBNET, "network": NETWORK, "ipCidrRange": "10.20.0.0/20"},
        ]})
        fake.routes[f"{gcp.COMPUTE}/projects/ubuntu-os-cloud/global/images/family/ubuntu-2404-lts-amd64"] = (
            200, {"name": "ubuntu-2404-noble-amd64-v20261020", "selfLink": IMAGE,
                  "creationTimestamp": "2026-10-20T00:00:00Z"})
        fake.routes[f"{gcp.CONTAINER}/projects/{PROJECT}/locations/{REGION}/serverConfig"] = (
            200, {"validMasterVersions": ["1.33.5-gke.100", "1.34.1-gke.200", "1.33.4-gke.9"]})

    def test_regions_skip_regions_that_are_down(self, account, fake):
        self._routes(fake)
        assert gcp.regions(account) == [{"id": "europe-west1", "name": "europe-west1"}]

    def test_inventory(self, account, fake):
        self._routes(fake)
        inv = gcp.inventory(account, REGION, {})
        assert set(inv) == set(base.empty_inventory(REGION))
        assert inv["zones"] == [{"id": "europe-west1-b", "name": "europe-west1-b"}]
        assert inv["networks"] == [{"id": NETWORK, "name": "core", "cidr": "", "subnets": [
            {"id": SUBNET, "name": "apps", "cidr": "10.20.0.0/20", "zone": None},
        ]}]
        # Families that are not there are skipped, not fatal.
        assert [i["id"] for i in inv["images"]] == [IMAGE]
        assert inv["images"][0]["osFamily"] == "debian"
        assert inv["kubernetesVersions"] == ["1.34", "1.33"]
        assert {"id": "e2-standard-4", "cpu": 4, "memoryGb": 16, "flexible": False, "note": ""} in inv["machineTypes"]

    def test_paging_follows_next_page_token(self, account, fake):
        calls = []

        def paged(method, url, headers, body, timeout_s, ca_pem):
            if url == TOKEN_URI:
                return 200, {}, json.dumps({"access_token": "ya29.token", "expires_in": 3599}).encode()
            calls.append(url)
            if "pageToken=two" in url:
                return 200, {}, json.dumps({"items": [{"name": "b-region1"}]}).encode()
            return 200, {}, json.dumps({"items": [{"name": "a-region1"}], "nextPageToken": "two"}).encode()

        base.set_transport(paged)
        assert [r["id"] for r in gcp.regions(account)] == ["a-region1", "b-region1"]
        assert len(calls) == 2

    def test_simulated(self):
        assert len(gcp.simulated_regions()) == 2
        inv = gcp.simulated_inventory("me-central1", {})
        assert [z["id"] for z in inv["zones"]] == ["me-central1-a", "me-central1-b", "me-central1-c"]
        assert len(inv["networks"][0]["subnets"]) == 2
        assert inv["images"] and inv["machineTypes"] and inv["kubernetesVersions"]

    def test_plan_checks(self, account, fake):
        compute = f"{gcp.COMPUTE}/projects/{PROJECT}"
        fake.routes[f"{compute}/regions/{REGION}"] = (200, {"name": REGION})
        fake.routes[f"{compute}/zones/europe-west1-b"] = (200, {"name": "europe-west1-b", "status": "UP"})
        fake.routes[NETWORK] = (200, {"name": "core"})
        fake.routes[SUBNET] = (200, {"name": "apps", "ipCidrRange": "10.20.0.0/20"})
        fake.routes[IMAGE] = (200, {"name": "ubuntu-2404"})
        spec = {"region": REGION, "zone": "europe-west1-b", "networkId": NETWORK, "subnetId": SUBNET,
                "image": {"id": IMAGE}, "kind": "vms"}
        checks = gcp.plan_checks(account, spec)
        assert [c["label"] for c in checks] == ["Region", "Network", "Subnetwork", "Image"]
        assert all(c["status"] == "ok" for c in checks)
        del fake.routes[IMAGE]
        with pytest.raises(CloudError, match="no longer exists"):
            gcp.plan_checks(account, spec)


# ---------------------------------------------------------------------------
# Spec and sizes
# ---------------------------------------------------------------------------

class TestPlacement:
    def test_zone_must_be_in_the_region(self):
        assert gcp.normalize_placement({"region": REGION, "zone": "europe-west1-b"}, "vms") == {
            "zone": "europe-west1-b", "diskType": "pd-balanced",
        }
        with pytest.raises(ValueError, match="not in region"):
            gcp.normalize_placement({"region": REGION, "zone": "us-east1-b"}, "vms")
        with pytest.raises(ValueError, match="Choose the zone"):
            gcp.normalize_placement({"region": REGION}, "vms")

    def test_managed_defaults_and_master_range(self):
        out = gcp.normalize_placement({"region": REGION, "regional": True}, "managed")
        assert out == {"zone": None, "diskType": "pd-balanced", "masterCidr": "172.16.0.32/28", "regional": True}
        with pytest.raises(ValueError, match="private IPv4 /28"):
            gcp.normalize_placement({"region": REGION, "zone": "europe-west1-b", "masterCidr": "10.0.0.0/24"}, "managed")

    def test_disk_type(self):
        with pytest.raises(ValueError, match="Disk type"):
            gcp.normalize_placement({"region": REGION, "zone": "europe-west1-b", "diskType": "ssd"}, "vms")


class TestMachineTypes:
    def test_smallest_fitting_prefers_e2(self):
        assert gcp.resolve_machine_type({"cpu": 4, "memoryGb": 8, "role": "worker"}, {}, []) == {
            "type": "e2-standard-4", "cpu": 4, "memoryGb": 16.0,
        }
        assert gcp.resolve_machine_type({"cpu": 8, "memoryGb": 64, "role": "worker"}, {}, [])["type"] == "n2-highmem-8"

    def test_override_wins_and_is_checked(self):
        spec = {"machineTypes": {"worker": "n2-standard-16", "controlPlane": "e2-custom-4-16384"}}
        assert gcp.resolve_machine_type({"cpu": 8, "memoryGb": 16, "role": "worker"}, spec)["type"] == "n2-standard-16"
        assert gcp.resolve_machine_type({"cpu": 4, "memoryGb": 16, "role": "controlPlane"}, spec)["memoryGb"] == 16
        with pytest.raises(ValueError, match="less than"):
            gcp.resolve_machine_type({"cpu": 32, "memoryGb": 16, "role": "worker"}, spec)
        with pytest.raises(ValueError, match="not a Google Cloud machine type"):
            gcp.resolve_machine_type({"cpu": 2, "memoryGb": 4, "role": "worker"}, {"machineTypes": {"worker": "BIG ONE"}})

    def test_unknown_override_is_kept_with_a_note(self):
        out = gcp.resolve_machine_type({"cpu": 4, "memoryGb": 16, "role": "worker"},
                                       {"machineTypes": {"worker": "a3-megagpu-8g"}})
        assert out["type"] == "a3-megagpu-8g" and out["note"]

    def test_too_big(self):
        with pytest.raises(ValueError, match="No Google Cloud machine type"):
            gcp.resolve_machine_type({"cpu": 500, "memoryGb": 4, "role": "worker"}, {}, [])


# ---------------------------------------------------------------------------
# main.tf.json
# ---------------------------------------------------------------------------

SSH = {"username": "kube", "publicKey": "ssh-ed25519 AAAAC3Nza kube@ks", "password": None}


def _spec(**extra):
    return {"region": REGION, "zone": "europe-west1-b", "diskType": "pd-ssd", "networkId": NETWORK,
            "subnetId": SUBNET, "subnetCidr": "10.20.0.0/20", "image": {"id": IMAGE}, **extra}


def _nodes(cps=3, workers=2):
    nodes = []
    for role, short, count in (("controlPlane", "cp", cps), ("worker", "wk", workers)):
        for index in range(1, count + 1):
            nodes.append({"name": f"prod-{short}-{index}", "role": role, "cpu": 4, "memoryGb": 8, "diskGb": 80,
                          "machineType": {"type": "e2-standard-4", "cpu": 4, "memoryGb": 16}})
    return nodes


def _vm_ctx(load_balancer=True, ssh=SSH, nodes=None, cidrs=("10.0.0.0/8",)):
    return base.VmContext("prod", 9, REGION, _spec(), nodes if nodes is not None else _nodes(),
                          load_balancer, dict(ssh), list(cidrs), base.tags("prod", 9))


class TestRenderVms:
    def test_vms_are_private_keyed_by_name_and_forward_ips(self):
        config = gcp.render_vms(_vm_ctx())
        assert config["terraform"]["required_providers"]["google"] == {
            "source": "hashicorp/google", "version": "= 8.9.0"}
        assert config["terraform"]["backend"] == {"http": {}}
        vm = config["resource"]["google_compute_instance"]["node"]
        assert sorted(vm["for_each"]) == ["prod-cp-1", "prod-cp-2", "prod-cp-3", "prod-wk-1", "prod-wk-2"]
        assert vm["for_each"]["prod-cp-1"] == {
            "role": "controlPlane", "role_label": "cp", "cpu": 4, "memory_mb": 8192, "disk_gb": 80,
            "machine_type": "e2-standard-4"}
        assert vm["network_interface"] == [{"subnetwork": SUBNET}]  # no access_config: no public IP
        assert vm["can_ip_forward"] is True and vm["deletion_protection"] is False
        assert vm["metadata"]["ssh-keys"] == "kube:ssh-ed25519 AAAAC3Nza kube@ks"
        assert vm["metadata"]["enable-oslogin"] == "FALSE"
        assert vm["metadata"]["user-data"].startswith("#cloud-config")
        assert vm["boot_disk"][0]["initialize_params"][0] == {
            "image": IMAGE, "size": "${each.value.disk_gb}", "type": "pd-ssd"}
        assert vm["labels"]["kubesight-cluster"] == "prod" and vm["labels"]["managed-by"] == "kubesight"
        assert "metadata" in vm["lifecycle"][0]["ignore_changes"]

    def test_firewall(self):
        rules = gcp.render_vms(_vm_ctx())["resource"]["google_compute_firewall"]
        assert rules["cluster_internal"]["source_tags"] == ["kubesight-prod"]
        assert rules["cluster_internal"]["allow"] == [{"protocol": "all"}]
        assert rules["admin"]["source_ranges"] == ["10.0.0.0/8"]
        assert rules["admin"]["allow"] == [{"protocol": "tcp", "ports": ["22", "6443"]}]
        assert rules["health"]["source_ranges"] == gcp.HEALTH_CHECK_RANGES
        assert all(rule["name"].startswith("ks-prod-") for rule in rules.values())

    def test_admin_rule_falls_back_to_the_subnet(self):
        rules = gcp.render_vms(_vm_ctx(cidrs=()))["resource"]["google_compute_firewall"]
        assert rules["admin"]["source_ranges"] == ["10.20.0.0/20"]

    def test_load_balancer_fronts_the_control_planes(self):
        resources = gcp.render_vms(_vm_ctx())["resource"]
        group = resources["google_compute_instance_group"]["control_planes"]
        assert group["instances"] == [
            f'${{google_compute_instance.node["prod-cp-{n}"].self_link}}' for n in (1, 2, 3)]
        rule = resources["google_compute_forwarding_rule"]["api"]
        assert rule["load_balancing_scheme"] == "INTERNAL" and rule["ports"] == ["6443"]
        assert resources["google_compute_region_backend_service"]["api"]["protocol"] == "TCP"
        assert resources["google_compute_region_health_check"]["api"]["tcp_health_check"] == [{"port": 6443}]
        assert gcp.HAIRPIN_LOCAL_ROUTE is True

    def test_no_load_balancer(self):
        resources = gcp.render_vms(_vm_ctx(load_balancer=False))["resource"]
        assert "google_compute_forwarding_rule" not in resources
        assert "health" not in resources["google_compute_firewall"]

    def test_password_only_credentials_are_refused(self):
        with pytest.raises(ValueError, match="only take SSH keys"):
            gcp.render_vms(_vm_ctx(ssh={"username": "kube", "publicKey": None, "password": "secret"}))

    def test_root_is_refused(self):
        with pytest.raises(ValueError, match="root"):
            gcp.render_vms(_vm_ctx(ssh={"username": "root", "publicKey": "ssh-ed25519 AAAA", "password": None}))

    def test_vm_names_must_start_with_a_letter(self):
        nodes = _nodes(1, 0)
        nodes[0]["name"] = "1prod-cp-1"
        with pytest.raises(ValueError, match="start with a letter"):
            gcp.render_vms(_vm_ctx(nodes=nodes))


class TestRenderManaged:
    def _ctx(self, **spec):
        pool = {"count": 3, "cpu": 4, "memoryGb": 16, "diskGb": 100,
                "machineType": {"type": "e2-standard-4", "cpu": 4, "memoryGb": 16}}
        return base.ManagedContext("prod", 9, REGION, _spec(masterCidr="172.16.0.32/28", **spec), "1.33",
                                   pool, SSH, ["10.0.0.0/8"], base.tags("prod", 9))

    def test_private_cluster_and_pool(self):
        resources = gcp.render_managed(self._ctx())["resource"]
        cluster = resources["google_container_cluster"]["cluster"]
        assert cluster["location"] == "europe-west1-b"
        assert cluster["deletion_protection"] is False
        assert cluster["private_cluster_config"] == [{
            "enable_private_nodes": True, "enable_private_endpoint": True,
            "master_ipv4_cidr_block": "172.16.0.32/28"}]
        assert cluster["master_authorized_networks_config"][0]["cidr_blocks"] == [
            {"cidr_block": "10.0.0.0/8", "display_name": "kubesight-1"}]
        pool = resources["google_container_node_pool"]["workers"]
        assert pool["node_count"] == 3
        assert pool["node_config"][0]["machine_type"] == "e2-standard-4"

    def test_regional(self):
        cluster = gcp.render_managed(self._ctx(regional=True))["resource"]["google_container_cluster"]["cluster"]
        assert cluster["location"] == REGION

    def test_name_limit(self):
        ctx = self._ctx()
        ctx.cluster_name = "a" * 41
        with pytest.raises(ValueError, match="GKE cluster"):
            gcp.render_managed(ctx)


# ---------------------------------------------------------------------------
# Environment and reading the result
# ---------------------------------------------------------------------------

class TestResult:
    def test_environment(self, account, tmp_path, key_json):
        env, hide = gcp.environment(account, str(tmp_path))
        assert env["GOOGLE_CREDENTIALS"] == key_json
        assert env["GOOGLE_PROJECT"] == PROJECT and env["GOOGLE_REGION"] == REGION
        assert "SSL_CERT_FILE" not in env
        assert key_json in hide and any("PRIVATE KEY" in item for item in hide)

    def test_environment_with_a_ca(self, account, tmp_path):
        account.ca_pem = "-----BEGIN CERTIFICATE-----\nMIIB\n-----END CERTIFICATE-----"
        env, _ = gcp.environment(account, str(tmp_path))
        assert open(env["SSL_CERT_FILE"], encoding="utf-8").read().startswith("-----BEGIN CERTIFICATE-----")

    def test_vm_facts(self):
        attrs = gcp.simulate_attributes("google_compute_instance", "node", "prod-cp-1",
                                        {"machine_type": "e2-standard-4", "zone": "europe-west1-b"}, 3)
        facts = gcp.vm_facts(attrs)
        assert facts["address"] == "10.10.1.13" and facts["zone"] == "europe-west1-b"
        assert facts["machineType"] == "e2-standard-4" and facts["id"]

    def test_endpoint_from_state(self):
        resources = [{"type": "google_compute_forwarding_rule", "name": "api",
                      "attributes": {"ip_address": "10.20.0.50"}}]
        assert gcp.endpoint_from_state(resources) == "10.20.0.50"
        assert gcp.endpoint_from_state([]) is None

    def test_managed_access(self, account, fake):
        attrs = gcp.simulate_attributes("google_container_cluster", "cluster", None, {"name": "prod"}, 0)
        access = gcp.managed_access(account, [{"type": "google_container_cluster", "name": "cluster",
                                               "attributes": attrs}], {})
        assert access == {"server": "https://172.16.0.34", "caData": attrs["master_auth"][0]["cluster_ca_certificate"],
                          "token": "ya29.token"}
        with pytest.raises(CloudError, match="no GKE cluster"):
            gcp.managed_access(account, [], {})

    def test_describe(self):
        attrs = {"machine_type": "e2-standard-4", "network_interface": [{"network_ip": "10.0.0.5"}],
                 "boot_disk": [{"initialize_params": [{"size": 80}]}]}
        assert gcp.describe_resource("google_compute_instance", "node", "prod-cp-1", attrs) == (
            "vm", "prod-cp-1", "e2-standard-4 · 80 GB disk · 10.0.0.5")
        assert gcp.describe_resource("google_compute_forwarding_rule", "api", None, {})[0] == "loadbalancer"
        assert gcp.describe_resource("google_container_node_pool", "workers", None,
                                     {"name": "workers", "node_count": 3,
                                      "node_config": [{"machine_type": "e2-standard-4"}]}) == (
            "nodepool", "Node pool workers", "3 × e2-standard-4")
        assert gcp.describe_resource("null_resource", "x", None, {}) is None
