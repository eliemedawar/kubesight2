"""Cluster Builder add-on catalog and image/forward-proxy coverage."""

from __future__ import annotations

import base64
import hashlib
import re
from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest
import yaml
from cryptography import x509
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.hazmat.primitives.serialization import Encoding
from cryptography.x509.oid import NameOID
from sqlalchemy import inspect, text

from api.db import db
from api.migrate_rbac import (
    _migrate_cluster_build_columns,
    _sanitize_legacy_build_profile_proxies,
)
from api.models import BuildProfile, ClusterBuild, ClusterBuildNode
from api.services import ssh_profile_service
from api.services.cluster_build import addons as addon_registry
from api.services.cluster_build import executor, kubeadm
from api.services.cluster_build import preflight as preflight_mod
from api.services.cluster_build.addons.base import AddonDescriptor
from api.services.cluster_build.addons.metallb import METALLB
from api.services.cluster_build.cni.base import CniDescriptor
from api.services.cluster_build.cni.base import CniRenderError
from api.services.cluster_build.cni.calico import CALICO
from api.services.cluster_build.cni.cilium import CILIUM
from api.services.cluster_build.cni.flannel import FLANNEL
from api.services.cluster_build.os_adapters.base import (
    ScriptContext,
    containerd_config_script,
)
from api.services.cluster_build.profiles import default_profile, resolve
from api.services.cluster_build.scrub import scrub
from api.services.ssh import SshConnectionError, set_transport_factory

from tests.test_cluster_builds import (
    SINGLE_CP_NODES,
    SINGLE_CP_MANAGED_LB_NODES,
    FakeSshTransport,
    auth_headers,
    build_default_fake,
    make_build_payload,
    run_full_build,
)

_REAL_ADDON_LOAD_MANIFESTS = AddonDescriptor.load_manifests

# MetalLB is inert without a pool, so every selection of it carries one. The
# range sits clear of the node addresses (10.0.0.11/.21) and VIPs used above.
METALLB_POOL = "10.0.0.240-10.0.0.250"
METALLB_SELECTION = {
    "id": "metallb",
    "version": "0.16.1",
    "config": {"addressPools": [METALLB_POOL]},
}


def add_addon_responders(fake, *, lb_address="10.0.0.240"):
    """Fake output for the functional checks the add-ons phase runs.

    Order matters — the transport takes the first matching responder, so the
    narrow jsonpath queries have to be registered before the broad ones.
    """
    fake.add(
        lambda h, s: "top nodes --no-headers" in s,
        "cp-1 100m 5% 512Mi 10%\nw-1 100m 5% 512Mi 10%\n",
    )
    fake.add(lambda h, s: "jsonpath='{.spec.ports[?(@.name==\"http\")]" in s, "30080")
    fake.add(lambda h, s: "get service nginx-ingress" in s, "NodePort")
    fake.add(lambda h, s: "curl -s -o /dev/null" in s, "HTTP 404 from http://10.0.0.21:30080/\n")
    fake.add(
        lambda h, s: f"get service {executor._LB_PROBE_SERVICE}" in s,
        lb_address,
    )
    fake.add(
        lambda h, s: f"create service loadbalancer {executor._LB_PROBE_SERVICE}" in s,
        f"service/{executor._LB_PROBE_SERVICE} created",
    )
    return fake


def _test_ca_pem() -> str:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    subject = x509.Name(
        [x509.NameAttribute(NameOID.COMMON_NAME, "KubeSight test CA")]
    )
    now = datetime.now(timezone.utc)
    certificate = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=1))
        .not_valid_after(now + timedelta(days=1))
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), True)
        .sign(key, hashes.SHA256())
    )
    return certificate.public_bytes(Encoding.PEM).decode("ascii")


@pytest.fixture(autouse=True)
def pinned_manifests_without_network(monkeypatch):
    """Use tiny pinned-manifest stand-ins; these tests cover orchestration."""
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

    def _addon_manifest(self, version, profile):
        if self.id == "metrics-server":
            image = f"registry.k8s.io/metrics-server/metrics-server:v{version}"
            name = "metrics-server"
            return [
                "apiVersion: apps/v1\n"
                "kind: Deployment\n"
                "metadata:\n"
                f"  name: {name}\n"
                "spec:\n"
                "  template:\n"
                "    spec:\n"
                "      containers:\n"
                f"        - name: {name}\n"
                f"          image: {image}\n"
            ]
        if self.id == "nginx-ingress":
            image = f"docker.io/nginx/nginx-ingress:{version}"
            name = "nginx-ingress"
            return [
                "apiVersion: apps/v1\n"
                "kind: Deployment\n"
                "metadata:\n"
                f"  name: {name}\n"
                "spec:\n"
                "  template:\n"
                "    spec:\n"
                "      containers:\n"
                f"        - name: {name}\n"
                f"          image: {image}\n"
            ]
        if self.id == "metallb":
            return [
                "apiVersion: apps/v1\n"
                "kind: Deployment\n"
                "metadata:\n"
                "  name: controller\n"
                "spec:\n"
                "  template:\n"
                "    spec:\n"
                "      containers:\n"
                "        - name: controller\n"
                f"          image: quay.io/metallb/controller:v{version}\n"
                "---\n"
                "apiVersion: apps/v1\n"
                "kind: DaemonSet\n"
                "metadata:\n"
                "  name: speaker\n"
                "spec:\n"
                "  template:\n"
                "    spec:\n"
                "      containers:\n"
                "        - name: speaker\n"
                f"          image: quay.io/metallb/speaker:v{version}\n"
            ]
        raise AssertionError(f"No test manifest for add-on {self.id}")

    monkeypatch.setattr(AddonDescriptor, "load_manifests", _addon_manifest)


@pytest.fixture(autouse=True)
def reset_ssh_transport():
    yield
    set_transport_factory(None)


@pytest.fixture()
def ssh_profile(app):
    credential = ssh_profile_service.create_credential(
        {
            "name": "root-key",
            "username": "kubesight",
            "authMethod": "key",
            "secret": "-----BEGIN KEY-----\nfake\n-----END KEY-----",
            "sudoMode": "nopasswd",
        }
    )
    return ssh_profile_service.create_profile(
        {
            "name": "default",
            "credentialId": credential["id"],
            "routeMode": "direct",
            "hostKeyPolicy": "tofu",
        }
    )


class TestAddonApi:
    def test_catalog_and_round_trip(self, client, admin_token, ssh_profile):
        options = client.get(
            "/api/cluster-builds/options", headers=auth_headers(admin_token)
        ).get_json()["data"]
        catalog = {item["id"]: item for item in options["addons"]}
        assert set(catalog) == {"metrics-server", "nginx-ingress", "metallb"}
        assert catalog["metrics-server"]["defaultVersion"] == "0.7.2"
        assert catalog["nginx-ingress"]["defaultVersion"] == "5.5.4"
        assert catalog["metallb"]["defaultVersion"] == "0.16.1"
        assert catalog["metallb"]["supportTier"] == "best-effort"
        assert catalog["metallb"]["configFields"] == [
            {
                "key": "addressPools",
                "type": "ipRangeList",
                "label": "LoadBalancer address pool",
                "required": True,
                "placeholder": "10.0.0.240-10.0.0.250",
                "help": METALLB.config_fields[0]["help"],
            }
        ]
        assert catalog["metrics-server"]["configFields"] == []

        payload = make_build_payload(
            addons=[
                {"id": "metrics-server", "version": "0.7.2"},
                {"id": "nginx-ingress", "version": "5.5.4"},
                dict(METALLB_SELECTION),
            ]
        )
        payload["connectionProfileId"] = ssh_profile["id"]
        response = client.post(
            "/api/cluster-builds",
            json=payload,
            headers=auth_headers(admin_token),
        )
        assert response.status_code == 201
        data = response.get_json()["data"]
        assert data["addons"] == payload["addons"]

        listed = client.get(
            "/api/cluster-builds", headers=auth_headers(admin_token)
        ).get_json()["data"]["items"]
        assert listed[0]["addons"] == payload["addons"]

        response = client.put(
            f"/api/cluster-builds/{data['id']}",
            json={**payload, "addons": []},
            headers=auth_headers(admin_token),
        )
        assert response.status_code == 200
        assert response.get_json()["data"]["addons"] == []

    def test_catalog_reports_manifest_provenance(self, client, admin_token):
        """The wizard shows where each manifest came from, so an offline build
        can be told apart from one that needs an internet fallback."""
        options = client.get(
            "/api/cluster-builds/options", headers=auth_headers(admin_token)
        ).get_json()["data"]
        catalog = {item["id"]: item for item in options["addons"]}

        for addon in addon_registry.available():
            entry = catalog[addon.id]
            assert entry["manifestDigests"] == [
                {"file": filename, "sha256": digest}
                for filename, digest in zip(
                    addon.manifest_files, addon.manifest_sha256
                )
            ]
            # Every digest is a full SHA-256 — the UI shortens it for display.
            assert all(
                re.fullmatch(r"[0-9a-f]{64}", item["sha256"])
                for item in entry["manifestDigests"]
            )
            # Bundled versions are the ones actually vendored on this host.
            assert entry["bundledVersions"] == [
                version
                for version in addon.versions
                if all(
                    addon.bundled_path(version, filename).is_file()
                    for filename in addon.manifest_files
                )
            ]
            assert set(entry["bundledVersions"]) <= set(entry["versions"])

    def test_metallb_descriptor_is_integrity_pinned(self):
        assert METALLB.versions == ("0.16.1",)
        assert METALLB.manifest_urls == (
            "https://raw.githubusercontent.com/metallb/metallb/"
            "v{version}/config/manifests/metallb-native.yaml",
        )
        assert METALLB.manifest_sha256 == (
            "bf25feebb7582ca7df845efd52ffbc2960d6cbf4cfc972f47fded9f788b67f0b",
        )
        assert any(
            "deployment/controller" in command
            for command in METALLB.readiness_commands
        )
        assert any(
            "daemonset/speaker" in command
            for command in METALLB.readiness_commands
        )

    @pytest.mark.parametrize(
        "config,error",
        [
            (None, "at least one address pool"),
            ({"addressPools": []}, "at least one address pool"),
            ({"addressPools": ["10.0.0.250-10.0.0.240"]}, "ends before it starts"),
            ({"addressPools": ["not-an-address"]}, "not a valid"),
            (
                {"addressPools": ["10.0.0.240-10.0.0.250", "10.0.0.245/32"]},
                "overlap",
            ),
            ({"addressPools": ["10.0.0.240/28"], "speaker": True}, "Unknown MetalLB"),
            ({"addressPools": ["10.0.0.0/24"]}, "overlaps"),  # swallows the nodes
        ],
    )
    def test_metallb_pool_is_validated(
        self, client, admin_token, ssh_profile, config, error
    ):
        selection = {"id": "metallb", "version": "0.16.1"}
        if config is not None:
            selection["config"] = config
        payload = make_build_payload(
            nodes=SINGLE_CP_NODES, addons=[selection]
        )
        payload["connectionProfileId"] = ssh_profile["id"]
        response = client.post(
            "/api/cluster-builds", json=payload, headers=auth_headers(admin_token)
        )
        assert response.status_code == 400
        assert error.lower() in response.get_json()["error"].lower()

    def test_metallb_pool_may_not_swallow_the_api_vip(
        self, client, admin_token, ssh_profile
    ):
        payload = make_build_payload(
            topology="single_cp",
            endpoint_mode="managed_haproxy",
            nodes=SINGLE_CP_MANAGED_LB_NODES,
            vipAddress="10.0.1.9",
            vipInterface="ens192",
            addons=[{
                "id": "metallb",
                "version": "0.16.1",
                "config": {"addressPools": ["10.0.1.0-10.0.1.20"]},
            }],
        )
        payload.pop("controlPlaneEndpoint", None)
        payload["connectionProfileId"] = ssh_profile["id"]
        response = client.post(
            "/api/cluster-builds", json=payload, headers=auth_headers(admin_token)
        )
        assert response.status_code == 400
        assert "api vip" in response.get_json()["error"].lower()

    def test_metallb_pool_entries_are_canonicalized(
        self, client, admin_token, ssh_profile
    ):
        payload = make_build_payload(
            nodes=SINGLE_CP_NODES,
            addons=[{
                "id": "metallb",
                "version": "0.16.1",
                # Whitespace, a host-bit-dirty CIDR, a bare address, and a
                # newline-separated string are all shapes the wizard can
                # produce. MetalLB itself accepts only CIDRs and ranges.
                "config": {
                    "addressPools":
                        " 10.0.0.241/28 \n10.0.0.200 - 10.0.0.210\n10.0.0.99 ",
                },
            }],
        )
        payload["connectionProfileId"] = ssh_profile["id"]
        response = client.post(
            "/api/cluster-builds", json=payload, headers=auth_headers(admin_token)
        )
        assert response.status_code == 201, response.get_json()
        assert response.get_json()["data"]["addons"] == [{
            "id": "metallb",
            "version": "0.16.1",
            "config": {"addressPools": [
                "10.0.0.240/28", "10.0.0.200-10.0.0.210", "10.0.0.99/32",
            ]},
        }]

    def test_addons_without_configuration_reject_one(
        self, client, admin_token, ssh_profile
    ):
        payload = make_build_payload(
            nodes=SINGLE_CP_NODES,
            addons=[{
                "id": "metrics-server",
                "version": "0.7.2",
                "config": {"addressPools": ["10.0.0.240/28"]},
            }],
        )
        payload["connectionProfileId"] = ssh_profile["id"]
        response = client.post(
            "/api/cluster-builds", json=payload, headers=auth_headers(admin_token)
        )
        assert response.status_code == 400
        assert "does not take any configuration" in response.get_json()["error"]

    @pytest.mark.parametrize(
        "addons,error",
        [
            ("metrics-server", "list"),
            ([{"id": "unknown"}], "Unknown"),
            (
                [{"id": "metrics-server"}, {"id": "metrics-server"}],
                "more than once",
            ),
            ([{"id": "metrics-server", "version": "99.0"}], "not supported"),
        ],
    )
    def test_invalid_selection_rejected(
        self, client, admin_token, ssh_profile, addons, error
    ):
        payload = make_build_payload(addons=addons)
        payload["connectionProfileId"] = ssh_profile["id"]
        response = client.post(
            "/api/cluster-builds",
            json=payload,
            headers=auth_headers(admin_token),
        )
        assert response.status_code == 400
        assert error.lower() in response.get_json()["error"].lower()

    def test_alias_conflict_and_worker_requirement(
        self, client, admin_token, ssh_profile
    ):
        payload = make_build_payload(
            addons=["metrics-server"],
            plugins=["metrics-server"],
        )
        payload["connectionProfileId"] = ssh_profile["id"]
        response = client.post(
            "/api/cluster-builds", json=payload, headers=auth_headers(admin_token)
        )
        assert response.status_code == 400
        assert "either" in response.get_json()["error"].lower()

        payload = make_build_payload(
            addons=["metrics-server"],
            nodes=[
                {
                    "role": "control_plane",
                    "hostname": "cp-1",
                    "address": "10.0.0.11",
                }
            ],
        )
        payload["connectionProfileId"] = ssh_profile["id"]
        response = client.post(
            "/api/cluster-builds", json=payload, headers=auth_headers(admin_token)
        )
        assert response.status_code == 400
        assert "worker" in response.get_json()["error"].lower()

    def test_profile_reference_node_names_and_interface_are_validated(
        self, client, admin_token, ssh_profile
    ):
        payload = make_build_payload(
            nodes=SINGLE_CP_NODES,
            buildProfileId=999999,
        )
        payload["connectionProfileId"] = ssh_profile["id"]
        response = client.post(
            "/api/cluster-builds", json=payload, headers=auth_headers(admin_token)
        )
        assert response.status_code == 400
        assert "build profile not found" in response.get_json()["error"].lower()

        duplicate_nodes = [
            dict(SINGLE_CP_NODES[0]),
            {**SINGLE_CP_NODES[1], "hostname": "cp-1"},
        ]
        payload = make_build_payload(nodes=duplicate_nodes)
        payload["connectionProfileId"] = ssh_profile["id"]
        response = client.post(
            "/api/cluster-builds", json=payload, headers=auth_headers(admin_token)
        )
        assert response.status_code == 400
        assert "must be unique" in response.get_json()["error"].lower()

        injected_nodes = [
            {**SINGLE_CP_NODES[0], "hostname": "cp-1\nkubeletExtraArgs:"},
            dict(SINGLE_CP_NODES[1]),
        ]
        payload = make_build_payload(nodes=injected_nodes)
        payload["connectionProfileId"] = ssh_profile["id"]
        response = client.post(
            "/api/cluster-builds", json=payload, headers=auth_headers(admin_token)
        )
        assert response.status_code == 400
        assert "dns subdomain" in response.get_json()["error"].lower()

        payload = make_build_payload(
            endpoint_mode="managed_haproxy",
            nodes=SINGLE_CP_MANAGED_LB_NODES,
            vipAddress="10.0.0.100",
            vipInterface="eth0\nnotify_master /tmp/pwn",
        )
        payload["connectionProfileId"] = ssh_profile["id"]
        response = client.post(
            "/api/cluster-builds", json=payload, headers=auth_headers(admin_token)
        )
        assert response.status_code == 400
        assert "vipinterface" in response.get_json()["error"].lower()

    def test_metallb_reserves_memberlist_port_on_cluster_nodes(self):
        build = ClusterBuild(
            k8s_version="1.32.4",
            addons_json=[{"id": "metallb", "version": "0.16.1"}],
        )
        control_plane = ClusterBuildNode(role="control_plane")
        worker = ClusterBuildNode(role="worker")
        load_balancer = ClusterBuildNode(role="loadbalancer")

        assert 7946 in preflight_mod._required_ports(build, control_plane)
        assert 7946 in preflight_mod._required_ports(build, worker)
        assert 7946 not in preflight_mod._required_ports(build, load_balancer)

        build.addons_json = []
        assert 7946 not in preflight_mod._required_ports(build, worker)

    def test_cni_integrity_is_checked_before_node_preparation(
        self, client, admin_token, ssh_profile, monkeypatch
    ):
        fake = build_default_fake(
            {
                "10.0.0.11": ("cp-1", "control_plane"),
                "10.0.0.21": ("w-1", "worker"),
            }
        )
        set_transport_factory(lambda: fake)
        payload = make_build_payload(nodes=SINGLE_CP_NODES)
        payload["connectionProfileId"] = ssh_profile["id"]
        created = client.post(
            "/api/cluster-builds", json=payload, headers=auth_headers(admin_token)
        ).get_json()["data"]

        def _bad_manifest(self, version, profile):
            raise CniRenderError("failed its pinned SHA-256 integrity check")

        monkeypatch.setattr(type(CALICO), "load_manifests", _bad_manifest)
        response = client.post(
            f"/api/cluster-builds/{created['id']}/preflight",
            headers=auth_headers(admin_token),
        )
        assert response.status_code == 400
        assert "integrity" in response.get_json()["error"].lower()
        assert fake.calls == []

    def test_manifest_integrity_and_mirror_fallback(self, monkeypatch):
        content = b"apiVersion: v1\nkind: ConfigMap\n"
        digest = hashlib.sha256(content).hexdigest()
        descriptor = AddonDescriptor(
            id="integrity-test",
            display_name="Integrity Test",
            description="test",
            support_tier="test",
            versions=("1.0.0",),
            manifest_files=("manifest.yaml",),
            manifest_urls=("https://example.invalid/{version}.yaml",),
            manifest_sha256=(digest,),
            readiness_commands=(),
        )
        monkeypatch.setattr(
            AddonDescriptor,
            "_download",
            lambda self, **kwargs: content,
        )
        mirror = replace(default_profile(), repo_mode="mirror")
        assert _REAL_ADDON_LOAD_MANIFESTS(
            descriptor, "1.0.0", mirror
        ) == [content.decode()]

        bad = replace(descriptor, manifest_sha256=("0" * 64,))
        with pytest.raises(Exception, match="integrity"):
            _REAL_ADDON_LOAD_MANIFESTS(bad, "1.0.0", mirror)

        offline = replace(default_profile(), repo_mode="offline")
        with pytest.raises(Exception, match="offline"):
            _REAL_ADDON_LOAD_MANIFESTS(descriptor, "1.0.0", offline)

    def test_cni_manifest_integrity_is_pinned(self, monkeypatch):
        content = b"apiVersion: apps/v1\nkind: DaemonSet\n"
        digest = hashlib.sha256(content).hexdigest()
        descriptor = CniDescriptor(
            id="integrity-cni",
            display_name="Integrity CNI",
            support_tier="test",
            versions=("1.0.0",),
            default_pod_cidr="10.244.0.0/16",
            manifest_files=("manifest.yaml",),
            manifest_urls=("https://example.invalid/{version}.yaml",),
            manifest_sha256={"1.0.0": (digest,)},
        )
        calls = []

        def _download(self, **kwargs):
            calls.append(kwargs)
            return content

        monkeypatch.setattr(CniDescriptor, "_download", _download)
        mirror = replace(default_profile(), repo_mode="mirror")
        assert descriptor.load_manifests("1.0.0", mirror) == [content.decode()]
        assert len(calls) == 1

        bad = replace(
            descriptor,
            manifest_sha256={"1.0.0": ("0" * 64,)},
        )
        with pytest.raises(Exception, match="integrity"):
            bad.load_manifests("1.0.0", mirror)


class TestBundledManifests:
    """The shipped data directory is what makes offline mode real."""

    def test_every_pinned_manifest_is_bundled_and_matches_its_digest(self):
        offline = replace(default_profile(), repo_mode="offline")
        missing = []
        for descriptor in addon_registry.available():
            for version in descriptor.versions:
                for filename in descriptor.manifest_files:
                    path = descriptor.bundled_path(version, filename)
                    if not path.is_file():
                        missing.append(str(path))
        for descriptor in (CALICO, FLANNEL):
            for version in descriptor.versions:
                for filename in descriptor.manifest_files:
                    path = descriptor.bundled_path(version, filename)
                    if not path.is_file():
                        missing.append(str(path))
        assert not missing, (
            "Missing bundled manifests — run "
            "`python tools/fetch_cluster_build_bundles.py`:\n"
            + "\n".join(missing)
        )

        # Offline mode never touches the network, and load_manifests re-checks
        # the pinned digest against the bundled bytes on every read.
        def _no_network(self, **kwargs):
            raise AssertionError("offline mode must not download anything")

        with pytest.MonkeyPatch.context() as patch:
            patch.setattr(AddonDescriptor, "_download", _no_network)
            patch.setattr(CniDescriptor, "_download", _no_network)
            for descriptor in addon_registry.available():
                for version in descriptor.versions:
                    rendered = _REAL_ADDON_LOAD_MANIFESTS(
                        descriptor, version, offline
                    )
                    assert len(rendered) == len(descriptor.manifest_files)
            for descriptor in (CALICO, FLANNEL):
                for version in descriptor.versions:
                    assert CniDescriptor.load_manifests(
                        descriptor, version, offline
                    )

    def test_bundled_calico_carries_the_build_pod_cidr(self):
        offline = replace(default_profile(), repo_mode="offline")
        # Unbound so the stand-in manifests the autouse fixture installs on the
        # Calico class do not hide the bundled file.
        manifest = CALICO.apply_pod_cidr(
            CniDescriptor.load_manifests(CALICO, CALICO.versions[0], offline)[0],
            "172.31.0.0/16",
        )
        assert 'value: "172.31.0.0/16"' in manifest
        assert "# - name: CALICO_IPV4POOL_CIDR" not in manifest
        # The rewrite must leave the document parseable, not just textually
        # patched — a stray blank line here used to produce invalid YAML.
        documents = [
            document for document in yaml.safe_load_all(manifest)
            if document is not None
        ]
        assert any(document.get("kind") == "DaemonSet" for document in documents)

    def test_bundled_flannel_carries_the_build_pod_cidr(self):
        offline = replace(default_profile(), repo_mode="offline")
        manifest = FLANNEL.apply_pod_cidr(
            CniDescriptor.load_manifests(FLANNEL, FLANNEL.versions[0], offline)[0],
            "172.31.0.0/16",
        )
        assert '"Network": "172.31.0.0/16"' in manifest
        assert "10.244.0.0/16" not in manifest

    def test_cilium_rewrites_cluster_pool_cidr_and_rejects_unknown_manifests(self):
        rendered = CILIUM.apply_pod_cidr(
            "apiVersion: v1\n"
            "kind: ConfigMap\n"
            "data:\n"
            '  cluster-pool-ipv4-cidr: "10.244.0.0/16"\n'
            "  cluster-pool-ipv4-mask-size: \"24\"\n",
            "172.31.0.0/16",
        )
        assert 'cluster-pool-ipv4-cidr: "172.31.0.0/16"' in rendered
        assert 'cluster-pool-ipv4-mask-size: "24"' in rendered

        with pytest.raises(CniRenderError, match="cluster-pool-ipv4-cidr"):
            CILIUM.apply_pod_cidr("apiVersion: v1\nkind: ConfigMap\n", "10.244.0.0/16")

    def test_cilium_is_bundled_only_and_says_how_to_render_it(self):
        offline = replace(default_profile(), repo_mode="offline")
        assert CILIUM.manifest_urls == ()
        if CILIUM.bundled_path(CILIUM.versions[0], "cilium.yaml").is_file():
            pytest.skip("a Cilium bundle is present in this checkout")
        with pytest.raises(CniRenderError, match="bundled-only"):
            CniDescriptor.load_manifests(CILIUM, CILIUM.versions[0], offline)


class TestImageAndForwardProxy:
    def _create_profile(self, client, token):
        payload = {
            "name": "proxy sources",
            "repoMode": "internet",
            "k8sImageRegistry": "proxy.local:5000/kubernetes",
            "cniImageRegistry": "proxy.local:5000/networking",
            "addonImageRegistry": "proxy.local:5000/addons",
            "registryUsername": "robot",
            "registryPassword": "registry-secret",
            "httpProxy": "http://egress.local:3128",
            "httpsProxy": "http://egress.local:3128",
            "noProxy": "localhost,127.0.0.1,.cluster.local",
        }
        response = client.post(
            "/api/build-profiles",
            json=payload,
            headers=auth_headers(token),
        )
        assert response.status_code == 201, response.get_json()
        assert "registry-secret" not in response.get_data(as_text=True)
        return response.get_json()["data"]

    def test_profile_resolves_registry_and_persistent_proxy(
        self, client, admin_token, app
    ):
        created = self._create_profile(client, admin_token)
        assert created["imageProxyEnabled"] is True
        assert created["registryPasswordConfigured"] is True

        row = db.session.get(BuildProfile, created["id"])
        profile = resolve(row)
        assert profile.registry_hosts() == ["proxy.local:5000"]
        assert profile.registry_auth_host == "proxy.local:5000"
        env = profile.proxy_env()
        assert "http_proxy" in env and "HTTP_PROXY" in env
        assert "https_proxy" in env and "HTTPS_PROXY" in env
        assert "no_proxy" in env and "NO_PROXY" in env

        script = containerd_config_script(
            ScriptContext(profile=profile, k8s_version="1.32.4")
        )
        assert "kubesight-proxy.conf" in script
        assert "systemctl daemon-reload" in script
        assert "registry-secret" not in script
        encoded = re.findall(
            r'(?:echo |AUTH_B64=)"([A-Za-z0-9+/=]+)"',
            script,
        )
        decoded = "\n".join(
            base64.b64decode(value).decode("utf-8") for value in encoded
        )
        assert "registry-secret" in decoded
        assert "proxy.local:5000" in decoded
        assert "addons.local:5443" not in decoded
        assert "io.containerd.grpc.v1.cri" in decoded
        assert "io.containerd.cri.v1.images" in decoded
        assert "config v3" in script
        assert 'AUTH_B64="' in script
        assert "umask 077" in script

        cleaned = scrub("export HTTP_PROXY=http://egress-user:egress-pass@egress.local:3128")
        assert "egress-pass" not in cleaned
        assert "[REDACTED]" in cleaned

    def test_preflight_probes_registry_authorities_not_prefix_paths(
        self, client, admin_token, app
    ):
        created = self._create_profile(client, admin_token)
        profile = resolve(db.session.get(BuildProfile, created["id"]))
        build = ClusterBuild(k8s_version="1.32.4")
        node = ClusterBuildNode(role="worker")
        script = preflight_mod._probe_script(build, node, profile)

        assert "https://proxy.local:5000/v2/" in script
        assert "proxy.local:5000/kubernetes/v2" not in script
        # The reachability probe hits the registry ROOT once, deduplicated
        # across the k8s/CNI/add-on prefixes that share this authority. Match
        # only the bare root so the image-manifest probe below — which
        # legitimately continues with a repository path — is not counted here.
        assert len(re.findall(
            r"https://proxy\.local:5000/v2/(?![A-Za-z0-9])", script
        )) == 1
        # The pause tag kubeadm pins for this minor is probed under the
        # prefix's repository path, not at the authority root.
        assert (
            "https://proxy.local:5000/v2/kubernetes/pause/manifests/3.10"
            in script
        )
        assert "HTTP_PROXY" in script
        assert "mktemp /run/.kubesight-preflight-ca.XXXXXX" in script
        assert "mktemp /var/tmp/.kubesight-fsync.XXXXXX" in script
        assert "/var/tmp/.ks_fsync" not in script

    @pytest.mark.parametrize(
        "payload,expected",
        [
            (
                {
                    "name": "bad",
                    "repoMode": "internet",
                    "k8sImageRegistry": "https://proxy.local/repo",
                },
                "without http",
            ),
            (
                {
                    "name": "bad",
                    "repoMode": "internet",
                    "k8sImageRegistry": "proxy.local/repo\nmalicious",
                },
                "whitespace",
            ),
            (
                {
                    "name": "bad",
                    "repoMode": "internet",
                    "registryUsername": "robot",
                },
                "together",
            ),
            (
                {
                    "name": "bad",
                    "repoMode": "internet",
                    "httpProxy": "http://user:secret@proxy.local:3128",
                },
                "must not contain credentials",
            ),
            (
                {
                    "name": "bad",
                    "repoMode": "internet",
                    "noProxy": "localhost\nEnvironment=HTTP_PROXY=evil",
                },
                "control characters",
            ),
            (
                {
                    "name": "bad",
                    "repoMode": "mirror",
                    "k8sPkgRepoUrl": "https://repo.local/k8s?token=secret",
                },
                "query or fragment",
            ),
            (
                {
                    "name": "bad",
                    "repoMode": "internet",
                    "k8sImageRegistry": "one.local/k8s",
                    "cniImageRegistry": "two.local/cni",
                    "registryUsername": "robot",
                    "registryPassword": "secret",
                },
                "one registry authority",
            ),
        ],
    )
    def test_invalid_profile_is_rejected(
        self, client, admin_token, payload, expected
    ):
        response = client.post(
            "/api/build-profiles",
            json=payload,
            headers=auth_headers(admin_token),
        )
        assert response.status_code == 400
        assert expected in response.get_json()["error"].lower()

    def test_ca_bundle_is_parsed_and_cannot_escape_shell(
        self, client, admin_token
    ):
        pem = _test_ca_pem()
        response = client.post(
            "/api/build-profiles",
            json={
                "name": "trusted ca",
                "repoMode": "internet",
                "extraCaCertsPem": pem,
            },
            headers=auth_headers(admin_token),
        )
        assert response.status_code == 201, response.get_json()

        response = client.post(
            "/api/build-profiles",
            json={
                "name": "malicious ca",
                "repoMode": "internet",
                "extraCaCertsPem": (
                    pem + "\nKS_CA_EOF\nid > /tmp/pwned\n#"
                ),
            },
            headers=auth_headers(admin_token),
        )
        assert response.status_code == 400
        assert "only pem-encoded certificates" in (
            response.get_json()["error"].lower()
        )

    def test_referenced_profile_is_immutable(
        self, client, admin_token, ssh_profile
    ):
        profile = self._create_profile(client, admin_token)
        payload = make_build_payload(buildProfileId=profile["id"])
        payload["connectionProfileId"] = ssh_profile["id"]
        created = client.post(
            "/api/cluster-builds",
            json=payload,
            headers=auth_headers(admin_token),
        )
        assert created.status_code == 201, created.get_json()

        response = client.put(
            f"/api/build-profiles/{profile['id']}",
            json={"name": "changed after review"},
            headers=auth_headers(admin_token),
        )
        assert response.status_code == 400
        assert "immutable" in response.get_json()["error"].lower()

    def test_registry_credentials_require_operator_secret(
        self, client, admin_token, monkeypatch
    ):
        monkeypatch.delenv("ALERT_ROUTING_SECRET_KEY", raising=False)
        monkeypatch.delenv("JWT_SECRET_KEY", raising=False)
        response = client.post(
            "/api/build-profiles",
            json={
                "name": "unsafe secret storage",
                "repoMode": "internet",
                "k8sImageRegistry": "proxy.local/k8s",
                "registryUsername": "robot",
                "registryPassword": "secret",
            },
            headers=auth_headers(admin_token),
        )
        assert response.status_code == 400
        assert "secret_key" in response.get_json()["error"].lower()

    def test_legacy_source_values_are_not_disclosed_and_proxy_is_sanitized(
        self, client, admin_token
    ):
        row = BuildProfile(
            name="legacy secrets",
            repo_mode="internet",
            k8s_pkg_repo_url="https://repo.local/k8s?token=repo-secret",
            k8s_image_registry="robot:image-secret@proxy.local/k8s",
            http_proxy="http://user:proxy-secret@egress.local:3128/path?token=x",
        )
        db.session.add(row)
        db.session.commit()

        response = client.get(
            "/api/build-profiles",
            headers=auth_headers(admin_token),
        )
        body = response.get_data(as_text=True)
        assert "repo-secret" not in body
        assert "image-secret" not in body
        assert "proxy-secret" not in body

        _sanitize_legacy_build_profile_proxies()
        db.session.expire_all()
        migrated = db.session.get(BuildProfile, row.id)
        assert migrated.http_proxy == "http://egress.local:3128"


class TestAddonExecution:
    def _profile(self, client, token):
        response = client.post(
            "/api/build-profiles",
            json={
                "name": "pull through",
                "repoMode": "internet",
                "k8sImageRegistry": "proxy.local:5000/k8s",
                "cniImageRegistry": "proxy.local:5000/cni",
                "addonImageRegistry": "proxy.local:5000/addons",
                "registryUsername": "robot",
                "registryPassword": "registry-secret",
                "httpProxy": "http://egress.local:3128",
            },
            headers=auth_headers(token),
        )
        assert response.status_code == 201
        return response.get_json()["data"]

    def test_selected_addons_pull_apply_and_verify(
        self, client, admin_token, ssh_profile, app
    ):
        profile = self._profile(client, admin_token)
        hosts = {
            "10.0.0.11": ("cp-1", "control_plane"),
            "10.0.0.21": ("w-1", "worker"),
        }
        fake = add_addon_responders(build_default_fake(hosts))
        set_transport_factory(lambda: fake)

        payload = make_build_payload(
            nodes=SINGLE_CP_NODES,
            buildProfileId=profile["id"],
            addons=[
                {"id": "metrics-server", "version": "0.7.2"},
                {"id": "nginx-ingress", "version": "5.5.4"},
                dict(METALLB_SELECTION),
            ],
        )
        data = run_full_build(
            client, admin_token, ssh_profile, fake, payload
        )
        assert data["status"] == "completed", data.get("error")
        assert data["addons"] == payload["addons"]

        phases = {step["phase"]: step for step in data["steps"]}
        assert phases["addons"]["status"] == "completed"
        assert phases["addons"]["id"] > phases["onboard"]["id"]

        scripts = [script for _, script in fake.calls]
        assert any(
            "crictl pull proxy.local:5000/addons/metrics-server/"
            "metrics-server:v0.7.2" in script
            for script in scripts
        )
        assert any(
            "crictl pull proxy.local:5000/addons/nginx/"
            "nginx-ingress:5.5.4" in script
            for script in scripts
        )
        assert any(
            "crictl pull proxy.local:5000/addons/metallb/"
            "controller:v0.16.1" in script
            for script in scripts
        )
        assert any(
            "crictl pull proxy.local:5000/addons/metallb/"
            "speaker:v0.16.1" in script
            for script in scripts
        )
        assert any(
            "crictl pull proxy.local:5000/addons/postfinance/"
            "kubelet-csr-approver:v1.2.14" in script
            for script in scripts
        )
        assert any(
            "sha256:c0f6aa1abdc225a32f9a29992fd97f711e78e2df21434f9ce7bc60981f96a5f8"
            in script
            for script in scripts
        )
        assert any("kubesight-addon-metrics-server" in script for script in scripts)
        assert any("kubesight-addon-nginx-ingress" in script for script in scripts)
        assert any("kubesight-addon-metallb" in script for script in scripts)
        assert any("deployment/kubelet-csr-approver" in script for script in scripts)
        assert any("top nodes --no-headers" in script for script in scripts)
        assert any(
            "customresourcedefinition/ipaddresspools.metallb.io" in script
            for script in scripts
        )
        assert any(
            "rollout status deployment/controller" in script
            for script in scripts
        )
        assert any(
            "rollout status daemonset/speaker" in script
            for script in scripts
        )
        assert any(
            "for port in 6443 2379 2380 10250 10257 10259 7946" in script
            for script in scripts
        )
        assert any(
            "for port in 10250 7946" in script
            for script in scripts
        )
        assert not any("certificate approve" in script for script in scripts)

        # The plugins are proven functional, not merely applied: an ingress
        # NodePort that answers HTTP and a LoadBalancer service that receives
        # an address out of the configured pool.
        assert any("kubesight-addon-metallb-pool.yaml" in script for script in scripts)
        pool_upload = next(
            script for script in scripts
            if "kubesight-addon-metallb-pool.yaml" in script and "base64 -d" in script
        )
        encoded = re.search(r"echo ([A-Za-z0-9+/=]+) \| base64 -d", pool_upload).group(1)
        pool_manifest = base64.b64decode(encoded).decode("utf-8")
        documents = list(yaml.safe_load_all(pool_manifest))
        assert [document["kind"] for document in documents] == [
            "IPAddressPool", "L2Advertisement",
        ]
        assert documents[0]["spec"]["addresses"] == [METALLB_POOL]
        assert documents[0]["metadata"]["namespace"] == "metallb-system"
        assert documents[1]["spec"]["ipAddressPools"] == [documents[0]["metadata"]["name"]]
        assert any(
            f"create service loadbalancer {executor._LB_PROBE_SERVICE}" in script
            for script in scripts
        )
        assert any(
            f"delete service {executor._LB_PROBE_SERVICE} --ignore-not-found" in script
            for script in scripts
        )
        assert any("http://10.0.0.21:30080/" in script for script in scripts)

        logs = client.get(
            f"/api/cluster-builds/{data['id']}/logs",
            headers=auth_headers(admin_token),
        ).get_data(as_text=True)
        assert "registry-secret" not in logs
        assert "proxy-secret" not in logs

    def test_addon_failure_retries_without_rebuilding_cluster(
        self, client, admin_token, ssh_profile, app, monkeypatch
    ):
        hosts = {
            "10.0.0.11": ("cp-1", "control_plane"),
            "10.0.0.21": ("w-1", "worker"),
        }
        fake = build_default_fake(hosts)
        state = {"failed_once": False}

        def _first_service_check(host, script):
            if (
                "get service nginx-ingress" in script
                and not state["failed_once"]
            ):
                state["failed_once"] = True
                return True
            return False

        fake.responders.insert(0, (_first_service_check, "ClusterIP"))
        add_addon_responders(fake)
        set_transport_factory(lambda: fake)

        payload = make_build_payload(
            nodes=SINGLE_CP_NODES,
            addons=[{"id": "nginx-ingress", "version": "5.5.4"}],
        )
        data = run_full_build(
            client, admin_token, ssh_profile, fake, payload
        )
        assert data["status"] == "failed"
        assert data["resultClusterId"]
        assert "add-ons" in (data["error"] or "")

        def _manifest_now_unreachable(self, version, profile):
            raise CniRenderError("Calico manifest endpoint is unreachable")

        # A backend restart clears its in-memory manifest cache.  The retry
        # must not re-fetch Calico after every node completed image pulling.
        monkeypatch.setattr(
            type(CALICO),
            "required_images",
            _manifest_now_unreachable,
        )

        response = client.post(
            f"/api/cluster-builds/{data['id']}/retry",
            headers=auth_headers(admin_token),
        )
        assert response.status_code == 200
        retried = response.get_json()["data"]
        assert retried["status"] == "completed", retried.get("error")

        scripts = [script for _, script in fake.calls]
        assert len([script for script in scripts if "kubeadm init" in script]) == 1
        assert len(
            [script for script in scripts if "cat /etc/kubernetes/admin.conf" in script]
        ) == 1
        assert len(
            [script for script in scripts if "kubesight-addon-nginx-ingress" in script]
        ) >= 2

    def test_metrics_enables_secure_kubelet_serving_bootstrap(self):
        config = kubeadm.render_init_config(
            k8s_version="1.32.4",
            control_plane_endpoint="10.0.0.100:6443",
            pod_cidr="10.244.0.0/16",
            service_cidr="10.96.0.0/12",
            profile=default_profile(),
            node_name="cp-1",
            server_tls_bootstrap=True,
        )
        assert "serverTLSBootstrap: true" in config
        assert "kubelet-insecure-tls" not in config

    def test_metrics_approver_policy_binds_each_hostname_to_its_ip(self):
        build = ClusterBuild(k8s_version="1.32.4")
        build.nodes = [
            ClusterBuildNode(
                role="control_plane",
                hostname="cp-1",
                address="10.0.0.11",
            ),
            ClusterBuildNode(
                role="worker",
                hostname="worker-1",
                address="10.0.0.21",
            ),
        ]

        manifest = executor._metrics_csr_approver_manifest(
            build, default_profile()
        )
        documents = list(yaml.safe_load_all(manifest))
        deployment = next(
            document for document in documents
            if document.get("kind") == "Deployment"
        )
        pod_spec = deployment["spec"]["template"]["spec"]
        approver = pod_spec["containers"][0]
        assert pod_spec["hostAliases"] == [
            {"ip": "10.0.0.11", "hostnames": ["cp-1"]},
            {"ip": "10.0.0.21", "hostnames": ["worker-1"]},
        ]
        env = {
            item["name"]: item["value"]
            for item in approver["env"]
        }
        assert "BYPASS_DNS_RESOLUTION" not in env
        assert env["PROVIDER_IP_PREFIXES"] == "10.0.0.11/32,10.0.0.21/32"
        assert approver["livenessProbe"]["httpGet"]["path"] == "/healthz"
        assert approver["readinessProbe"]["httpGet"]["path"] == "/healthz"


def test_addons_column_migration_is_idempotent(app):
    with db.engine.begin() as connection:
        connection.execute(text("ALTER TABLE cluster_builds DROP COLUMN addons_json"))
        for column in ("last_test_at", "last_test_status", "last_test_message"):
            connection.execute(
                text(f"ALTER TABLE ssh_connection_profiles DROP COLUMN {column}")
            )
    before = {
        column["name"] for column in inspect(db.engine).get_columns("cluster_builds")
    }
    assert "addons_json" not in before

    _migrate_cluster_build_columns()
    _migrate_cluster_build_columns()
    columns = {
        column["name"] for column in inspect(db.engine).get_columns("cluster_builds")
    }
    assert "addons_json" in columns
    profile_columns = {
        column["name"]
        for column in inspect(db.engine).get_columns("ssh_connection_profiles")
    }
    assert {"last_test_at", "last_test_status", "last_test_message"} <= profile_columns


class TestSshProfileTestBookkeeping:
    """A route's last proof and its age drive the readiness bar."""

    def test_profile_starts_untested(self, ssh_profile):
        assert ssh_profile["lastTestAt"] is None
        assert ssh_profile["lastTestStatus"] is None
        assert ssh_profile["lastTestMessage"] is None

    def test_successful_test_is_recorded(self, client, admin_token, ssh_profile):
        fake = FakeSshTransport()
        fake.add(lambda h, s: "uname -sr" in s, "kubesight\nLinux 5.15.0\n")
        set_transport_factory(lambda: fake)

        response = client.post(
            f"/api/ssh-connection-profiles/{ssh_profile['id']}/test",
            json={"host": "10.0.0.21"},
            headers=auth_headers(admin_token),
        )
        assert response.status_code == 200
        assert response.get_json()["data"]["status"] == "ok"

        listed = client.get(
            "/api/ssh-connection-profiles", headers=auth_headers(admin_token)
        ).get_json()["data"]["items"]
        row = next(item for item in listed if item["id"] == ssh_profile["id"])
        assert row["lastTestStatus"] == "ok"
        assert row["lastTestAt"] is not None
        assert "10.0.0.21" in row["lastTestMessage"]
        assert "kubesight" in row["lastTestMessage"]

    def test_failed_test_is_recorded_without_leaking_secrets(
        self, client, admin_token, ssh_profile
    ):
        fake = FakeSshTransport()
        fake.add(
            lambda h, s: True,
            SshConnectionError("Authentication failed for 10.0.0.99"),
        )
        set_transport_factory(lambda: fake)

        response = client.post(
            f"/api/ssh-connection-profiles/{ssh_profile['id']}/test",
            json={"host": "10.0.0.99"},
            headers=auth_headers(admin_token),
        )
        assert response.status_code == 200
        assert response.get_json()["data"]["status"] == "failed"

        row = ssh_profile_service.serialize_profile(
            ssh_profile_service.get_profile(ssh_profile["id"])
        )
        assert row["lastTestStatus"] == "failed"
        assert row["lastTestMessage"].startswith("10.0.0.99: ")
        assert "BEGIN KEY" not in row["lastTestMessage"]
