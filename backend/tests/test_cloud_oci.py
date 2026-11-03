"""Oracle Cloud adapter: account checks, request signing, inventory, sizes,
main.tf.json and reading the result back — against a fake OCI REST API.

The signer was compared byte for byte with the oci SDK's ``Signer`` (GET, POST,
encrypted key) and the token with oci-cli's ``generate-token``, and the
rendered configurations planned with oracle/oci 9.11.0; these tests pin the
behaviour that comparison established.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import urllib.parse

import pytest
import yaml
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa

from api.services.cluster_build.provisioning.clouds import base, oci
from api.services.cluster_build.provisioning.clouds.base import Account, CloudError, ManagedContext, VmContext

TENANCY = "ocid1.tenancy.oc1..aaaaaaaatenancy"
USER = "ocid1.user.oc1..aaaaaaaauser"
COMPARTMENT = "ocid1.compartment.oc1..aaaaaaaacompartment"
VCN = "ocid1.vcn.oc1.me-dubai-1.aaaaaaaavcn"
SUBNET = "ocid1.subnet.oc1.me-dubai-1.aaaaaaaasubnet"
IMAGE = "ocid1.image.oc1.me-dubai-1.aaaaaaaaol9"
AD1 = "kIdk:ME-DUBAI-1-AD-1"
DATE = "Mon, 03 Nov 2026 10:00:00 GMT"


@pytest.fixture(scope="module")
def key():
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


@pytest.fixture(scope="module")
def pem(key):
    return key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
    ).decode()


@pytest.fixture
def account(key, pem):
    settings, secrets = oci.normalize_account(
        {"tenancyOcid": TENANCY, "userOcid": USER, "fingerprint": oci.key_fingerprint(key)},
        {"privateKey": pem},
    )
    return Account(provider="oci", name="oci", region="me-dubai-1", settings=settings, secrets=secrets)


class FakeOci:
    """Answers by (method, path prefix); records every request."""

    def __init__(self):
        self.routes = []
        self.calls = []

    def on(self, method, path, status=200, body=None, headers=None, raw=None):
        self.routes.append((method, path, status, body, headers or {}, raw))

    def __call__(self, method, url, headers, body, timeout_s, ca_pem):
        self.calls.append({"method": method, "url": url, "headers": headers, "body": body})
        parts = urllib.parse.urlsplit(url)
        query = dict(urllib.parse.parse_qsl(parts.query))
        for index, (m, path, status, payload, extra, raw) in enumerate(self.routes):
            if m != method or not parts.path.endswith(path):
                continue
            if callable(payload):
                status, payload, extra = payload(query)
            data = raw if raw is not None else json.dumps(payload).encode()
            return status, extra, data
        return 404, {}, json.dumps({"code": "NotAuthorizedOrNotFound", "message": f"no route {parts.path}"}).encode()


@pytest.fixture
def fake():
    transport = FakeOci()
    base.set_transport(transport)
    yield transport
    base.set_transport(None)


def _verify(key, headers, method, url, signed):
    parts = urllib.parse.urlsplit(url)
    target = parts.path + (f"?{parts.query}" if parts.query else "")
    lines = []
    for name in signed:
        lines.append(f"(request-target): {method.lower()} {target}" if name == "(request-target)" else f"{name}: {headers[name]}")
    signature = headers["authorization"].split('signature="', 1)[1].split('"', 1)[0]
    key.public_key().verify(base64.b64decode(signature), "\n".join(lines).encode(), padding.PKCS1v15(), hashes.SHA256())


# ---------------------------------------------------------------------------
# The account
# ---------------------------------------------------------------------------

class TestAccount:
    def test_valid_account_keeps_secrets_apart(self, account, key):
        assert account.settings == {
            "tenancyOcid": TENANCY, "userOcid": USER,
            "fingerprint": oci.key_fingerprint(key), "compartmentOcid": TENANCY,
        }
        assert set(account.secrets) == {"privateKey"}
        assert "privateKey" not in json.dumps(account.settings)

    def test_fingerprint_must_belong_to_the_key(self, pem):
        with pytest.raises(ValueError, match="does not belong to this private key"):
            oci.normalize_account(
                {"tenancyOcid": TENANCY, "userOcid": USER, "fingerprint": ":".join(["ab"] * 16)},
                {"privateKey": pem},
            )

    @pytest.mark.parametrize("field,value", [
        ("tenancyOcid", "ocid1.user.oc1..aaaa"),
        ("userOcid", "not-an-ocid"),
        ("fingerprint", "12:34"),
        ("compartmentOcid", "ocid1.vcn.oc1..aaaa"),
    ])
    def test_bad_settings_are_refused(self, key, pem, field, value):
        settings = {"tenancyOcid": TENANCY, "userOcid": USER, "fingerprint": oci.key_fingerprint(key)}
        settings[field] = value
        with pytest.raises(ValueError):
            oci.normalize_account(settings, {"privateKey": pem})

    def test_editing_without_the_key_keeps_the_stored_one(self, account):
        settings, secrets = oci.normalize_account(
            {"compartmentOcid": COMPARTMENT}, {"privateKey": ""}, account.settings, account.secrets,
        )
        assert secrets["privateKey"] == account.secrets["privateKey"]
        assert settings["compartmentOcid"] == COMPARTMENT

    def test_new_account_needs_the_key(self, key):
        with pytest.raises(ValueError, match="private key is required"):
            oci.normalize_account(
                {"tenancyOcid": TENANCY, "userOcid": USER, "fingerprint": oci.key_fingerprint(key)}, {},
            )

    def test_encrypted_key_needs_its_passphrase(self, key):
        encrypted = key.private_bytes(
            serialization.Encoding.PEM, serialization.PrivateFormat.TraditionalOpenSSL,
            serialization.BestAvailableEncryption(b"pa55"),
        ).decode()
        settings = {"tenancyOcid": TENANCY, "userOcid": USER, "fingerprint": oci.key_fingerprint(key)}
        with pytest.raises(ValueError, match="give its passphrase"):
            oci.normalize_account(settings, {"privateKey": encrypted})
        with pytest.raises(ValueError, match="wrong passphrase"):
            oci.normalize_account(settings, {"privateKey": encrypted, "privateKeyPassphrase": "nope"})
        _, secrets = oci.normalize_account(settings, {"privateKey": encrypted, "privateKeyPassphrase": "pa55"})
        assert secrets["privateKeyPassphrase"] == "pa55"

    def test_fingerprint_is_md5_of_public_der(self, key):
        der = key.public_key().public_bytes(serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo)
        digest = hashlib.md5(der).hexdigest()  # noqa: S324
        assert oci.key_fingerprint(key).replace(":", "") == digest

    def test_identity_label_has_no_secret(self, account):
        label = oci.identity_label(account)
        assert label.startswith("user ocid1.user") and "…" in label

    def test_test_reads_the_user(self, account, fake):
        fake.on("GET", f"/users/{USER}", body={"name": "kubesight-api"})
        assert oci.test(account).startswith("kubesight-api (user ")
        assert fake.calls[0]["url"].startswith("https://identity.me-dubai-1.oci.oraclecloud.com/20160918/users/")


# ---------------------------------------------------------------------------
# Signing
# ---------------------------------------------------------------------------

class TestSigning:
    def test_get_signs_date_target_host(self, account, key):
        url = "https://iaas.me-dubai-1.oraclecloud.com/20160918/vcns?compartmentId=x&limit=100"
        headers = oci.sign(account, "GET", url, date=DATE)
        assert headers["authorization"].startswith(
            f'Signature algorithm="rsa-sha256",headers="date (request-target) host",keyId="{TENANCY}/{USER}/'
        )
        assert headers["authorization"].endswith('",version="1"')
        assert "x-content-sha256" not in headers
        _verify(key, headers, "GET", url, ["date", "(request-target)", "host"])

    def test_post_also_signs_the_body(self, account, key):
        url = "https://containerengine.me-dubai-1.oci.oraclecloud.com/20180222/clusters/c/kubeconfig/content"
        body = b'{"tokenVersion": "2.0.0"}'
        headers = oci.sign(account, "POST", url, body=body, date=DATE)
        assert 'headers="date (request-target) host content-length content-type x-content-sha256"' in headers["authorization"]
        assert headers["content-length"] == str(len(body))
        assert headers["x-content-sha256"] == base64.b64encode(hashlib.sha256(body).digest()).decode()
        _verify(key, headers, "POST", url,
                ["date", "(request-target)", "host", "content-length", "content-type", "x-content-sha256"])

    def test_cluster_token_is_a_presigned_url(self, account, key):
        token = oci.cluster_token(account, "me-dubai-1", "ocid1.cluster.oc1.x", date=DATE)
        url = base64.urlsafe_b64decode(token).decode()
        assert url.startswith("https://containerengine.me-dubai-1.oraclecloud.com/cluster_request/ocid1.cluster.oc1.x?authorization=")
        query = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(url).query))
        assert query["date"] == DATE
        bare = url.split("?", 1)[0]
        _verify(key, {**query, "host": "containerengine.me-dubai-1.oraclecloud.com"}, "GET", bare,
                ["date", "(request-target)", "host"])

    def test_cloud_errors_name_the_hint_never_the_key(self, account, fake):
        fake.on("GET", f"/users/{USER}", status=401,
                body={"code": "NotAuthenticated", "message": "The required information to complete authentication was not provided."})
        with pytest.raises(CloudError) as caught:
            oci.test(account)
        text = str(caught.value)
        assert "401 NotAuthenticated" in text and "fingerprint" in text
        assert "PRIVATE KEY" not in text


# ---------------------------------------------------------------------------
# Inventory
# ---------------------------------------------------------------------------

def _inventory_routes(fake, *, oke_ok=True):
    fake.on("GET", "/compartments", body=[
        {"id": COMPARTMENT, "name": "platform", "compartmentId": TENANCY, "lifecycleState": "ACTIVE"},
        {"id": "ocid1.compartment.oc1..aaaadev", "name": "dev", "compartmentId": COMPARTMENT},
    ])
    fake.on("GET", "/availabilityDomains", body=[{"name": AD1}, {"name": "kIdk:ME-DUBAI-1-AD-2"}])

    def vcns(query):
        if query.get("page") == "2":
            return 200, [{"id": "ocid1.vcn.oc1..b", "displayName": "b-vcn", "cidrBlock": "10.9.0.0/16"}], {}
        return 200, [{"id": VCN, "displayName": "a-vcn", "cidrBlock": "10.0.0.0/16"}], {"opc-next-page": "2"}

    fake.on("GET", "/vcns", body=vcns)
    fake.on("GET", "/subnets", body=[
        {"id": SUBNET, "displayName": "nodes", "cidrBlock": "10.0.1.0/24", "vcnId": VCN, "availabilityDomain": None},
        {"id": "ocid1.subnet.oc1..ad", "displayName": "ad1", "cidrBlock": "10.0.2.0/24", "vcnId": VCN,
         "availabilityDomain": AD1},
        {"id": "ocid1.subnet.oc1..gone", "displayName": "gone", "vcnId": VCN, "lifecycleState": "TERMINATED"},
    ])

    def images(query):
        os_name, version = query["operatingSystem"], query["operatingSystemVersion"]
        return 200, [
            {"id": f"gpu-{version}", "displayName": f"{os_name}-{version}-GPU-2026"},
            {"id": f"img-{version}", "displayName": f"{os_name}-{version}-2026", "timeCreated": "2026-10-01"},
        ], {}

    fake.on("GET", "/images", body=images)
    if oke_ok:
        fake.on("GET", "/clusterOptions/all", body={"kubernetesVersions": ["v1.32.1", "v1.34.1", "v1.33.1"]})
        fake.on("GET", "/nodePoolOptions/all", body={"sources": [
            {"sourceName": "Oracle-Linux-8.10-2026.09.30-0-OKE-1.33.1-900", "imageId": "oke-133"},
            {"sourceName": "Oracle-Linux-8.10-aarch64-2026.09.30-0-OKE-1.34.1-900", "imageId": "arm"},
            {"sourceName": "Oracle-Linux-8.10-2026.09.30-0", "imageId": "platform"},
        ]})
    else:
        fake.on("GET", "/clusterOptions/all", status=404, body={"code": "NotAuthorizedOrNotFound", "message": "no"})


class TestInventory:
    def test_inventory_shape_and_contents(self, account, fake):
        _inventory_routes(fake)
        inv = oci.inventory(account, "me-dubai-1", {"compartmentId": COMPARTMENT})
        assert set(base.empty_inventory("x")) <= set(inv)
        assert inv["compartments"][0] == {"id": TENANCY, "name": "(root)", "path": "(root)"}
        assert {"id": "ocid1.compartment.oc1..aaaadev", "name": "dev", "path": "platform/dev"} in inv["compartments"]
        assert [z["id"] for z in inv["zones"]] == [AD1, "kIdk:ME-DUBAI-1-AD-2"]
        assert [n["name"] for n in inv["networks"]] == ["a-vcn", "b-vcn"]  # second page read
        subnets = inv["networks"][0]["subnets"]
        assert [(s["name"], s["zone"]) for s in subnets] == [("ad1", AD1), ("nodes", None)]
        assert [i["id"] for i in inv["images"]] == ["img-9", "img-8", "img-24.04", "img-22.04"]
        assert [i["osFamily"] for i in inv["images"]] == ["rhel", "rhel", "debian", "debian"]
        assert inv["kubernetesVersions"] == ["v1.34.1", "v1.33.1", "v1.32.1"]
        assert inv["nodeImages"] == [{"id": "oke-133", "name": "Oracle-Linux-8.10-2026.09.30-0-OKE-1.33.1-900",
                                      "kubernetesVersion": "v1.33.1"}]
        assert all(m["flexible"] for m in inv["machineTypes"])
        vcn_call = next(c for c in fake.calls if "/vcns?" in c["url"])
        assert f"compartmentId={urllib.parse.quote(COMPARTMENT)}" in vcn_call["url"]
        assert all("authorization" in c["headers"] for c in fake.calls)

    def test_without_oke_rights_vms_still_work(self, account, fake):
        _inventory_routes(fake, oke_ok=False)
        inv = oci.inventory(account, "me-dubai-1", {})
        assert inv["networks"] and inv["kubernetesVersions"] == []
        assert "OKE options could not be read" in inv["warnings"][0]

    def test_regions_are_the_subscribed_ones(self, account, fake):
        fake.on("GET", "/regionSubscriptions", body=[
            {"regionName": "me-dubai-1", "status": "READY", "isHomeRegion": True},
            {"regionName": "eu-frankfurt-1", "status": "READY"},
            {"regionName": "us-ashburn-1", "status": "IN_PROGRESS"},
        ])
        assert oci.regions(account) == [
            {"id": "eu-frankfurt-1", "name": "eu-frankfurt-1"},
            {"id": "me-dubai-1", "name": "me-dubai-1 (home)"},
        ]

    def test_simulated_inventory_has_every_key(self):
        inv = oci.simulated_inventory("me-dubai-1")
        assert set(base.empty_inventory("x")) <= set(inv)
        assert inv["networks"][0]["subnets"] and inv["images"] and inv["kubernetesVersions"]
        assert [r["id"] for r in oci.simulated_regions()] == ["eu-frankfurt-1", "me-dubai-1"]


# ---------------------------------------------------------------------------
# Placement and sizes
# ---------------------------------------------------------------------------

class TestPlacement:
    def test_vms_need_compartment_and_ad(self):
        with pytest.raises(ValueError, match="compartment"):
            oci.normalize_placement({"zone": AD1}, "vms")
        with pytest.raises(ValueError, match="availability domain"):
            oci.normalize_placement({"compartmentId": COMPARTMENT}, "vms")
        out = oci.normalize_placement({"compartmentId": COMPARTMENT, "subnetZone": AD1}, "vms")
        assert out == {"compartmentId": COMPARTMENT, "zone": AD1, "shape": oci.DEFAULT_SHAPE}

    def test_shape_must_be_flexible(self):
        with pytest.raises(ValueError, match="not a flexible shape"):
            oci.normalize_placement({"compartmentId": COMPARTMENT, "zone": AD1, "shape": "VM.Standard2.4"}, "vms")

    def test_managed_node_image_is_optional(self):
        out = oci.normalize_placement({"compartmentId": COMPARTMENT, "zone": AD1}, "managed")
        assert "nodeImageId" not in out
        out = oci.normalize_placement({"compartmentId": COMPARTMENT, "zone": AD1, "nodeImageId": IMAGE}, "managed")
        assert out["nodeImageId"] == IMAGE
        with pytest.raises(ValueError, match="image OCID"):
            oci.normalize_placement({"compartmentId": COMPARTMENT, "zone": AD1, "nodeImageId": "nope"}, "managed")

    def test_default_node_image_is_the_newest_x86_one_for_the_version(self, account, fake):
        fake.on("GET", "/nodePoolOptions/all", body={"sources": [
            {"sourceName": "Oracle-Linux-8.10-2026.09.30-0-OKE-1.33.1-900", "imageId": "ol8-new"},
            {"sourceName": "Oracle-Linux-9.6-2026.08.01-0-OKE-1.33.1-880", "imageId": "ol9"},
            {"sourceName": "Oracle-Linux-9.6-2026.09.15-0-OKE-1.33.1-901", "imageId": "ol9-new"},
            {"sourceName": "Oracle-Linux-9.6-aarch64-2026.10.01-0-OKE-1.33.1-950", "imageId": "arm"},
            {"sourceName": "Oracle-Linux-9.6-Gen2-GPU-2026.10.01-0-OKE-1.33.1-950", "imageId": "gpu"},
            {"sourceName": "Oracle-Linux-9.6-2026.10.01-0-OKE-1.34.1-960", "imageId": "other-version"},
            {"sourceName": "Oracle-Linux-9.6-2026.10.01-0", "imageId": "platform"},
        ]})
        spec = {"region": "me-dubai-1", "compartmentId": COMPARTMENT, "managed": {"kubernetesVersion": "1.33.1"}}
        assert oci.default_node_image(account, spec) == "ol9-new"
        spec["managed"]["kubernetesVersion"] = "v1.30.0"
        with pytest.raises(CloudError, match="no Oracle Linux node image for Kubernetes v1.30.0"):
            oci.default_node_image(account, spec)

    def test_simulated_default_node_image_is_in_the_simulated_inventory(self):
        spec = {"region": "me-dubai-1", "managed": {"kubernetesVersion": "1.33.1"}}
        ids = [i["id"] for i in oci.simulated_inventory("me-dubai-1")["nodeImages"]]
        assert oci.simulated_default_node_image(spec) in ids

    def test_previous_values_are_kept(self):
        previous = {"compartmentId": COMPARTMENT, "zone": AD1, "shape": "VM.Standard3.Flex"}
        assert oci.normalize_placement({}, "vms", previous)["shape"] == "VM.Standard3.Flex"

    @pytest.mark.parametrize("cpu,memory,ocpus,mem_out", [
        (2, 4, 1, 4), (4, 8, 2, 8), (3, 1, 2, 2), (1, 1, 1, 1), (2, 200, 4, 200),
    ])
    def test_sizes_become_ocpus(self, cpu, memory, ocpus, mem_out):
        out = oci.resolve_machine_type({"cpu": cpu, "memoryGb": memory, "role": "worker"}, {}, [])
        assert (out["type"], out["ocpus"], out["cpu"], out["memoryGb"]) == (oci.DEFAULT_SHAPE, ocpus, ocpus * 2, mem_out)
        assert out["note"] == f"{ocpus} OCPU"

    def test_role_override_and_limits(self):
        spec = {"shape": "VM.Standard.E5.Flex", "machineTypes": {"controlPlane": "VM.Standard3.Flex"}}
        assert oci.resolve_machine_type({"cpu": 4, "memoryGb": 8, "role": "controlPlane"}, spec)["type"] == "VM.Standard3.Flex"
        assert oci.resolve_machine_type({"cpu": 4, "memoryGb": 8, "role": "worker"}, spec)["type"] == "VM.Standard.E5.Flex"
        with pytest.raises(ValueError, match="goes up to 32 OCPU"):
            oci.resolve_machine_type({"cpu": 80, "memoryGb": 64, "role": "controlPlane"}, spec)


# ---------------------------------------------------------------------------
# main.tf.json
# ---------------------------------------------------------------------------

SSH = {"username": "kubesight", "publicKey": "ssh-ed25519 AAAAC3Nza kubesight", "password": "p${w}d"}


def _spec(**extra):
    spec = {
        "region": "me-dubai-1", "kind": "vms", "networkId": VCN, "networkCidr": "10.0.0.0/16",
        "subnetId": SUBNET, "subnetCidr": "10.0.1.0/24", "image": {"id": IMAGE, "osFamily": "rhel"},
        "compartmentId": COMPARTMENT, "zone": AD1, "shape": oci.DEFAULT_SHAPE,
    }
    spec.update(extra)
    return spec


def _nodes(spec, cps=3, workers=2):
    nodes = []
    for role, count, short in (("controlPlane", cps, "cp"), ("worker", workers, "wk")):
        for index in range(1, count + 1):
            nodes.append({"name": f"demo-{short}-{index}", "role": role, "cpu": 4, "memoryGb": 8, "diskGb": 40,
                          "machineType": oci.resolve_machine_type({"cpu": 4, "memoryGb": 8, "role": role}, spec)})
    return nodes


class TestRender:
    def test_vms_with_a_load_balancer(self):
        spec = _spec()
        config = oci.render_vms(VmContext("demo", 7, "me-dubai-1", spec, _nodes(spec), True, SSH,
                                          ["10.20.0.0/16"], base.tags("demo", 7)))
        assert config["terraform"]["required_providers"]["oci"] == {"source": "oracle/oci", "version": "= 9.11.0"}
        assert config["terraform"]["backend"] == {"http": {}}
        assert config["provider"] == {"oci": {"region": "me-dubai-1"}}
        resources = config["resource"]
        vm = resources["oci_core_instance"]["node"]
        assert sorted(vm["for_each"]) == ["demo-cp-1", "demo-cp-2", "demo-cp-3", "demo-wk-1", "demo-wk-2"]
        value = vm["for_each"]["demo-cp-1"]
        assert value == {"role": "controlPlane", "cpu": 4, "memory_mb": 8192, "disk_gb": 50,
                         "shape": oci.DEFAULT_SHAPE, "ocpus": 2, "memory_gb": 8, "subnet_cidr": "10.0.1.0/24"}
        vnic = vm["create_vnic_details"][0]
        assert vnic["assign_public_ip"] == "false" and vnic["skip_source_dest_check"] is True
        assert "hostname_label" not in vnic
        assert vm["metadata"] == {"user_data": "${var.node_user_data[each.key]}",
                                  "ssh_authorized_keys": SSH["publicKey"]}
        assert vm["freeform_tags"]["kubesight-build"] == "7"
        # cloud-init is per VM, base64, plain (not $$-escaped), and sensitive.
        variable = config["variable"]["node_user_data"]
        assert variable["sensitive"] is True
        text = base64.b64decode(variable["default"]["demo-wk-2"]).decode()
        assert text.startswith("#cloud-config\nhostname: demo-wk-2\n")
        assert '"password": "p${w}d"' in text and "NOPASSWD" in text
        # The API load balancer sends to the control planes only.
        assert resources["oci_network_load_balancer_network_load_balancer"]["api"]["is_private"] is True
        assert sorted(resources["oci_network_load_balancer_backend"]["api"]["for_each"]) == [
            "demo-cp-1", "demo-cp-2", "demo-cp-3"]
        assert resources["oci_network_load_balancer_backend_set"]["api"]["is_preserve_source"] is False
        assert resources["oci_network_load_balancer_listener"]["api"]["port"] == 6443
        rules = resources["oci_core_network_security_group_security_rule"]
        assert sorted(rules["admin"]["for_each"]) == ["10.20.0.0/16 tcp/22", "10.20.0.0/16 tcp/6443"]
        assert rules["api_health"]["source"] == "10.0.1.0/24"
        assert rules["intra"]["source_type"] == "NETWORK_SECURITY_GROUP"
        assert "0.0.0.0/0" not in json.dumps({k: v for k, v in rules.items() if k != "egress"})

    def test_vms_without_a_load_balancer(self):
        spec = _spec()
        config = oci.render_vms(VmContext("demo", 7, "me-dubai-1", spec, _nodes(spec, 1, 1), False, SSH, []))
        resources = config["resource"]
        assert not any(k.startswith("oci_network_load_balancer") for k in resources)
        assert "admin" not in resources["oci_core_network_security_group_security_rule"]
        assert "api_health" not in resources["oci_core_network_security_group_security_rule"]

    def test_managed_cluster(self):
        spec = _spec(kind="managed", nodeImageId="ocid1.image.oc1..oke")
        machine = oci.resolve_machine_type({"cpu": 4, "memoryGb": 16, "role": "worker"}, spec)
        config = oci.render_managed(ManagedContext("demo", 8, "me-dubai-1", spec, "1.33.1",
                                                   {"count": 3, "diskGb": 100, "machineType": machine},
                                                   {"username": "opc", "publicKey": None}, ["10.20.0.0/16"]))
        cluster = config["resource"]["oci_containerengine_cluster"]["cluster"]
        assert cluster["kubernetes_version"] == "v1.33.1"
        assert cluster["endpoint_config"][0]["is_public_ip_enabled"] is False
        assert cluster["cluster_pod_network_options"] == [{"cni_type": "FLANNEL_OVERLAY"}]
        pool = config["resource"]["oci_containerengine_node_pool"]["workers"]
        assert pool["node_config_details"][0]["size"] == 3
        assert pool["node_config_details"][0]["placement_configs"] == [{"availability_domain": AD1, "subnet_id": SUBNET}]
        assert pool["node_source_details"] == [{"source_type": "IMAGE", "image_id": "ocid1.image.oc1..oke",
                                                "boot_volume_size_in_gbs": "100"}]
        assert pool["node_shape_config"] == [{"ocpus": 2, "memory_in_gbs": 16}]
        assert "ssh_public_key" not in pool

    def test_pod_subnet_means_vcn_native_pods(self):
        spec = _spec(kind="managed", nodeImageId="ocid1.image.oc1..oke", podSubnetId="ocid1.subnet.oc1..pods")
        machine = oci.resolve_machine_type({"cpu": 2, "memoryGb": 8, "role": "worker"}, spec)
        config = oci.render_managed(ManagedContext("demo", 8, "me-dubai-1", spec, "v1.34.1",
                                                   {"count": 2, "diskGb": 60, "machineType": machine}, SSH, []))
        pool = config["resource"]["oci_containerengine_node_pool"]["workers"]
        assert pool["node_config_details"][0]["node_pool_pod_network_option_details"][0]["pod_subnet_ids"] == [
            "ocid1.subnet.oc1..pods"]
        assert config["resource"]["oci_containerengine_cluster"]["cluster"]["cluster_pod_network_options"] == [
            {"cni_type": "OCI_VCN_IP_NATIVE"}]


# ---------------------------------------------------------------------------
# OpenTofu's environment, and reading the result
# ---------------------------------------------------------------------------

class TestEnvironmentAndState:
    def test_environment_uses_oci_variables_and_a_private_key_file(self, account, tmp_path):
        account.secrets["privateKeyPassphrase"] = "pa55"
        env, hide = oci.environment(account, str(tmp_path))
        assert env["OCI_TENANCY_OCID"] == TENANCY and env["OCI_USER_OCID"] == USER
        assert env["OCI_PRIVATE_KEY_PASSWORD"] == "pa55" and env["OCI_REGION"] == "me-dubai-1"
        with open(env["OCI_PRIVATE_KEY_PATH"], encoding="utf-8") as handle:
            assert handle.read().strip() == account.secrets["privateKey"].strip()
        if os.name == "posix":
            assert os.stat(env["OCI_PRIVATE_KEY_PATH"]).st_mode & 0o777 == 0o600
        assert account.secrets["privateKey"].strip() in hide and "pa55" in hide
        assert not any(account.secrets["privateKey"].strip() in value for value in env.values())

    def test_vm_facts_and_endpoint(self):
        assert oci.vm_facts({"id": "i1", "private_ip": "10.0.1.5", "availability_domain": AD1, "shape": "S"}) == {
            "id": "i1", "address": "10.0.1.5", "zone": AD1, "machineType": "S"}
        resources = [{"type": oci.NLB_TYPE, "name": "api", "attributes": {"ip_addresses": [
            {"ip_address": "129.1.1.1", "is_public": True}, {"ip_address": "10.0.1.200", "is_public": False}]}}]
        assert oci.endpoint_from_state(resources) == "10.0.1.200"
        assert oci.endpoint_from_state([]) is None

    def test_managed_access_reads_ca_and_mints_a_token(self, account, fake):
        kubeconfig = yaml.safe_dump({"clusters": [{"name": "c", "cluster": {
            "server": "https://10.0.1.3:6443", "certificate-authority-data": "Q0EtREFUQQ=="}}]})
        fake.on("POST", "/kubeconfig/content", raw=kubeconfig.encode())
        resources = [{"type": oci.CLUSTER_TYPE, "name": "cluster", "attributes": {
            "id": "ocid1.cluster.oc1.me-dubai-1.x", "endpoints": [{"private_endpoint": "10.0.1.3:6443"}]}}]
        access = oci.managed_access(account, resources, {"region": "me-dubai-1"})
        assert access["server"] == "https://10.0.1.3:6443" and access["caData"] == "Q0EtREFUQQ=="
        assert base64.urlsafe_b64decode(access["token"]).decode().startswith(
            "https://containerengine.me-dubai-1.oraclecloud.com/cluster_request/ocid1.cluster.oc1.me-dubai-1.x?")
        call = fake.calls[0]
        assert json.loads(call["body"]) == {"tokenVersion": "2.0.0", "endpoint": "PRIVATE_ENDPOINT"}
        assert "x-content-sha256" in call["headers"]

    def test_managed_access_needs_the_cluster_in_state(self, account):
        with pytest.raises(CloudError, match="no OKE cluster"):
            oci.managed_access(account, [], {"region": "me-dubai-1"})

    def test_describe_and_simulate(self):
        kind, title, detail = oci.describe_resource(oci.VM_RESOURCE_TYPE, "node", "demo-cp-1", {
            "shape": oci.DEFAULT_SHAPE, "shape_config": [{"ocpus": 2, "memory_in_gbs": 8}],
            "source_details": [{"boot_volume_size_in_gbs": "50"}]})
        assert (kind, title) == ("vm", "demo-cp-1") and detail.startswith("2 OCPU · 8 GB · 50 GB disk")
        assert oci.describe_resource(oci.NLB_TYPE, "api", None, {})[0] == "lb"
        assert oci.describe_resource("oci_network_load_balancer_listener", "api", None, {})[0] == "lb"
        assert oci.describe_resource(oci.CLUSTER_TYPE, "cluster", None, {"name": "demo"})[0] == "cluster"
        assert oci.describe_resource(oci.NODE_POOL_TYPE, "workers", None, {"name": "w"})[0] == "nodepool"
        assert oci.describe_resource("oci_something_else", "x", None, {}) is None
        attrs = oci.simulate_attributes(oci.VM_RESOURCE_TYPE, "node", "demo-cp-1",
                                        {"shape": oci.DEFAULT_SHAPE, "subnet_cidr": "10.0.2.0/24"}, 3)
        assert oci.vm_facts(attrs)["address"] == "10.0.2.13"
        nlb = oci.simulate_attributes(oci.NLB_TYPE, "api", None, {}, 0)
        assert oci.endpoint_from_state([{"type": oci.NLB_TYPE, "name": "api", "attributes": nlb}])
        cluster = oci.simulate_attributes(oci.CLUSTER_TYPE, "cluster", None, {"name": "demo"}, 0)
        assert cluster["endpoints"][0]["private_endpoint"].endswith(":6443")


# ---------------------------------------------------------------------------
# Plan checks
# ---------------------------------------------------------------------------

class TestPlanChecks:
    def _routes(self, fake, *, subnet_ad=None, image_status=200):
        fake.on("GET", f"/users/{USER}", body={"name": "kubesight-api"})
        fake.on("GET", f"/vcns/{VCN}", body={"id": VCN, "lifecycleState": "AVAILABLE"})
        fake.on("GET", f"/subnets/{SUBNET}", body={"id": SUBNET, "vcnId": VCN, "cidrBlock": "10.0.1.0/24",
                                                   "availabilityDomain": subnet_ad, "prohibitPublicIpOnVnic": True})
        fake.on("GET", f"/images/{IMAGE}", status=image_status,
                body={"displayName": "Oracle-Linux-9", "lifecycleState": "AVAILABLE"} if image_status == 200
                else {"code": "NotAuthorizedOrNotFound", "message": "gone"})

    def test_all_present(self, account, fake):
        self._routes(fake)
        checks = oci.plan_checks(account, _spec(networkName="a-vcn", subnetName="nodes"))
        assert [c["status"] for c in checks] == ["ok", "ok", "ok"]
        assert checks[-1]["detail"] == "Oracle-Linux-9"

    def test_subnet_in_another_ad(self, account, fake):
        self._routes(fake, subnet_ad="kIdk:ME-DUBAI-1-AD-2")
        with pytest.raises(CloudError, match="lives in kIdk:ME-DUBAI-1-AD-2"):
            oci.plan_checks(account, _spec())

    def test_image_gone(self, account, fake):
        self._routes(fake, image_status=404)
        with pytest.raises(CloudError, match="404 NotAuthorizedOrNotFound"):
            oci.plan_checks(account, _spec())

    def test_managed_version_must_be_offered(self, account, fake):
        self._routes(fake)
        fake.on("GET", "/clusterOptions/all", body={"kubernetesVersions": ["v1.33.1"]})
        fake.on("GET", "/images/ocid1.image.oc1..oke", body={"displayName": "OKE-1.33.1", "lifecycleState": "AVAILABLE"})
        spec = _spec(kind="managed", nodeImageId="ocid1.image.oc1..oke", managed={"kubernetesVersion": "1.33.1"})
        assert [c["label"] for c in oci.plan_checks(account, spec)][-2:] == ["Kubernetes version", "Node image"]
        spec.pop("nodeImageId")
        node_image = next(c for c in oci.plan_checks(account, spec) if c["label"] == "Node image")
        assert node_image["status"] == "info"  # picked before rendering
        spec["managed"]["kubernetesVersion"] = "1.31.0"
        with pytest.raises(CloudError, match="does not offer Kubernetes v1.31.0"):
            oci.plan_checks(account, spec)
