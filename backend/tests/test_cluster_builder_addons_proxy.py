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
from api.services.cluster_build import executor, kubeadm
from api.services.cluster_build import preflight as preflight_mod
from api.services.cluster_build.addons.base import AddonDescriptor
from api.services.cluster_build.addons.metallb import METALLB
from api.services.cluster_build.cni.base import CniDescriptor
from api.services.cluster_build.cni.base import CniRenderError
from api.services.cluster_build.cni.calico import CALICO
from api.services.cluster_build.os_adapters.base import (
    ScriptContext,
    containerd_config_script,
)
from api.services.cluster_build.profiles import default_profile, resolve
from api.services.cluster_build.scrub import scrub
from api.services.ssh import set_transport_factory

from tests.test_cluster_builds import (
    SINGLE_CP_NODES,
    SINGLE_CP_MANAGED_LB_NODES,
    auth_headers,
    build_default_fake,
    make_build_payload,
    run_full_build,
)

_REAL_ADDON_LOAD_MANIFESTS = AddonDescriptor.load_manifests


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
        assert "IPAddressPool" in catalog["metallb"]["description"]

        payload = make_build_payload(
            addons=[
                {"id": "metrics-server", "version": "0.7.2"},
                {"id": "nginx-ingress", "version": "5.5.4"},
                {"id": "metallb", "version": "0.16.1"},
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
        assert script.count("https://proxy.local:5000/v2/") == 1
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
        fake = build_default_fake(hosts)
        fake.add(
            lambda h, s: "top nodes --no-headers" in s,
            "cp-1 100m 5% 512Mi 10%\nw-1 100m 5% 512Mi 10%\n",
        )
        fake.add(
            lambda h, s: "get service nginx-ingress" in s,
            "NodePort",
        )
        set_transport_factory(lambda: fake)

        payload = make_build_payload(
            nodes=SINGLE_CP_NODES,
            buildProfileId=profile["id"],
            addons=[
                {"id": "metrics-server", "version": "0.7.2"},
                {"id": "nginx-ingress", "version": "5.5.4"},
                {"id": "metallb", "version": "0.16.1"},
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
        fake.add(lambda h, s: "get service nginx-ingress" in s, "NodePort")
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
