"""Red Hat Satellite registration on a source profile.

A profile carries the URL Satellite's "Register Host" page generates and the
bearer token it is called with. Every RHEL node registers before anything is
installed, and in Satellite mode containerd and the Kubernetes packages come
from the repositories the activation key enables.
"""

from __future__ import annotations

import base64
from dataclasses import replace

import pytest

from api.db import db
from api.models import BuildProfile, ClusterBuild, ClusterBuildNode
from api.services.cluster_build import executor, os_adapters
from api.services.cluster_build import preflight as preflight_mod
from api.services.cluster_build.os_adapters import OsFacts
from api.services.cluster_build.os_adapters.base import ScriptContext
from api.services.cluster_build.profiles import (
    _normalize_satellite_register_url,
    default_profile,
    resolve,
)
from api.services.cluster_build.scrub import scrub
from api.services.ssh import set_transport_factory
from tests.test_cluster_builds import (  # noqa: F401 — ssh_profile is a fixture
    SINGLE_CP_MANAGED_LB_NODES,
    FakeSshTransport,
    auth_headers,
    make_build_payload,
    ssh_profile,
)

URL = (
    "https://capsule.example.com:9090/register?activation_keys=LEB-DOPS-9-DR_AK"
    "&download_utility=curl&hostgroup_id=2&location_id=3&organization_id=1"
    "&update_packages=false"
)
TOKEN = "eyJhbGciOiJIUzI1NiJ9.eyJ1c2VyX2lkIjo0Mn0.c2lnbmF0dXJlLXNpZ25hdHVyZQ"
PASTED = (
    "set -o pipefail && curl --silent --show-error   "
    f"'{URL}' --header 'Authorization: Bearer {TOKEN}' | bash"
)
VERSION = "1.32.4"


def _satellite_profile(mode="satellite"):
    return replace(
        default_profile(),
        repo_mode=mode,
        satellite_register_url=URL,
        satellite_token=TOKEN,
    )


def _create(client, token, **extra):
    payload = {
        "name": "satellite sources",
        "repoMode": "satellite",
        "satelliteRegisterUrl": URL,
        "satelliteToken": TOKEN,
        **extra,
    }
    return client.post(
        "/api/build-profiles", json=payload, headers=auth_headers(token)
    )


class TestRegistrationUrl:
    def test_the_url_is_kept_as_given(self):
        assert _normalize_satellite_register_url(URL) == URL

    def test_the_whole_generated_command_keeps_only_its_url(self):
        kept = _normalize_satellite_register_url(PASTED)
        assert kept == URL
        assert TOKEN not in kept

    @pytest.mark.parametrize("bad", [
        URL.replace("https://", "http://"),
        URL.replace("/register", "/hosts"),
        URL.replace("activation_keys=LEB-DOPS-9-DR_AK&", ""),
        URL + "';reboot;'",
        URL + "$(id)",
        "https://user:pw@capsule.example.com:9090/register?activation_keys=a",
        "not a url",
    ])
    def test_unsafe_or_wrong_urls_are_refused(self, bad):
        with pytest.raises(ValueError):
            _normalize_satellite_register_url(bad)


class TestProfile:
    def test_token_is_stored_encrypted_and_never_returned(self, client, admin_token, app):
        response = _create(client, admin_token)
        assert response.status_code == 201, response.get_json()
        assert TOKEN not in response.get_data(as_text=True)
        data = response.get_json()["data"]
        assert data["repoMode"] == "satellite"
        assert data["satelliteHost"] == "capsule.example.com"
        assert data["satelliteTokenConfigured"] is True

        row = db.session.get(BuildProfile, data["id"])
        assert TOKEN not in (row.satellite_token_cipher or "")
        profile = resolve(row)
        assert profile.satellite_token == TOKEN
        assert profile.registers_with_satellite
        assert profile.satellite_ca_url == (
            "https://capsule.example.com/pub/katello-server-ca.crt"
        )

    def test_a_bearer_prefix_on_the_token_is_dropped(self, client, admin_token, app):
        data = _create(
            client, admin_token, satelliteToken=f"Bearer {TOKEN}"
        ).get_json()["data"]
        assert resolve(db.session.get(BuildProfile, data["id"])).satellite_token == TOKEN

    def test_satellite_mode_needs_a_registration(self, client, admin_token):
        response = _create(client, admin_token, satelliteRegisterUrl="", satelliteToken="")
        assert response.status_code == 400

    def test_url_and_token_come_together(self, client, admin_token):
        response = _create(client, admin_token, repoMode="internet", satelliteToken="")
        assert response.status_code == 400

    def test_registration_alone_works_with_other_package_sources(self, client, admin_token):
        response = _create(client, admin_token, repoMode="internet")
        assert response.status_code == 201, response.get_json()

    def test_only_the_token_can_change_once_a_build_uses_the_profile(
        self, client, admin_token, app
    ):
        data = _create(client, admin_token).get_json()["data"]
        db.session.add(ClusterBuild(
            name="uses-it", k8s_version=VERSION, topology_type="single_cp",
            endpoint_mode="manual_endpoint", control_plane_endpoint="10.0.0.100:6443",
            cni_plugin="calico", pod_cidr="10.244.0.0/16", service_cidr="10.96.0.0/12",
            build_profile_id=data["id"],
        ))
        db.session.commit()

        refused = client.put(
            f"/api/build-profiles/{data['id']}",
            json={"name": "renamed"},
            headers=auth_headers(admin_token),
        )
        assert refused.status_code == 400

        replaced = client.put(
            f"/api/build-profiles/{data['id']}",
            json={"satelliteToken": "eyJnew.token.value"},
            headers=auth_headers(admin_token),
        )
        assert replaced.status_code == 200, replaced.get_json()
        db.session.expire_all()
        assert resolve(db.session.get(BuildProfile, data["id"])).satellite_token == (
            "eyJnew.token.value"
        )


class TestScripts:
    def _rhel(self):
        return os_adapters.by_id("rhel")

    def test_registration_follows_the_satellite_steps(self):
        script = self._rhel().script_register_satellite(
            ScriptContext(profile=_satellite_profile(), k8s_version=VERSION)
        )
        assert "https://capsule.example.com/pub/katello-server-ca.crt" in script
        assert "update-ca-trust" in script
        assert "subscription-manager clean" in script
        # Shell-quoted: the & in the query must not background anything.
        assert f"'{URL}'" in script
        assert script.index("subscription-manager clean") < script.index('bash "$KS_DIR/register.sh"')

    def test_the_token_never_appears_in_clear(self):
        script = self._rhel().script_register_satellite(
            ScriptContext(profile=_satellite_profile(), k8s_version=VERSION)
        )
        assert TOKEN not in script
        header = base64.b64encode(f"Authorization: Bearer {TOKEN}".encode()).decode()
        assert header in script
        assert '--header @"$KS_DIR/header"' in script

    def test_no_script_without_a_registration(self):
        ctx = ScriptContext(profile=default_profile(), k8s_version=VERSION)
        assert self._rhel().script_register_satellite(ctx) is None

    def test_satellite_mode_installs_from_the_activation_key_repos(self):
        ctx = ScriptContext(profile=_satellite_profile(), k8s_version=VERSION)
        packages = self._rhel().script_install_kube_packages(ctx)
        containerd = self._rhel().script_install_containerd(ctx)
        assert "pkgs.k8s.io" not in packages
        assert "rm -f /etc/yum.repos.d/kubesight-kubernetes.repo" in packages
        assert f'"kubeadm-${{K8S_VER}}"' in packages
        assert "--disableexcludes" not in packages
        assert "download.docker.com" not in containerd
        assert "dnf install -y containerd.io" in containerd

    def test_other_modes_keep_their_repositories(self):
        ctx = ScriptContext(profile=_satellite_profile("internet"), k8s_version=VERSION)
        assert "pkgs.k8s.io" in self._rhel().script_install_kube_packages(ctx)
        assert "download.docker.com" in self._rhel().script_install_containerd(ctx)

    def test_only_rhel_registers(self):
        assert self._rhel().supports_satellite
        assert not os_adapters.by_id("debian").supports_satellite

    def test_a_bare_token_is_scrubbed_from_logs(self):
        assert TOKEN not in scrub(f"token was {TOKEN}")


def _build():
    return ClusterBuild(
        name="sat", k8s_version=VERSION, topology_type="single_cp",
        endpoint_mode="manual_endpoint", control_plane_endpoint="10.0.0.100:6443",
        cni_plugin="calico", pod_cidr="10.244.0.0/16", service_cidr="10.96.0.0/12",
    )


def _checks(app, profile, drop=(), **facts):
    base = {"KS_OS_ID": "rhel", "KS_OS_LIKE": "fedora", "KS_OS_VERSION": "9.4",
            "KS_ARCH": "x86_64", "KS_SUBMAN": "present", "KS_SAT_CA": "200",
            "KS_SAT_REG": "404", "repo_ok": [], "repo_fail": [], "image_ok": [],
            "image_fail": []}
    base.update(facts)
    for key in drop:
        base.pop(key, None)
    node = ClusterBuildNode(role="control_plane", address_source="manual")
    with app.app_context():
        checks = preflight_mod._node_checks(_build(), node, base, profile)
    return {check["id"]: check for check in checks}


class TestPreflight:
    def test_a_reachable_capsule_passes(self, app):
        checks = _checks(app, _satellite_profile())
        assert checks["satellite"]["status"] == "pass"
        assert checks["k8s_packages"]["status"] == "warn"

    def test_an_unreachable_ca_fails(self, app):
        checks = _checks(app, _satellite_profile(), KS_SAT_CA="000")
        assert checks["satellite"]["status"] == "fail"
        assert "katello-server-ca.crt" in checks["satellite"]["detail"]

    def test_an_unreachable_registration_port_fails(self, app):
        checks = _checks(app, _satellite_profile(), KS_SAT_REG="000")
        assert checks["satellite"]["status"] == "fail"
        assert "9090" in checks["satellite"]["hint"]

    def test_missing_subscription_manager_fails(self, app):
        checks = _checks(app, _satellite_profile(), drop=("KS_SUBMAN",))
        assert checks["satellite"]["status"] == "fail"
        assert "subscription-manager" in checks["satellite"]["detail"]

    def test_ubuntu_cannot_use_a_satellite_profile(self, app):
        checks = _checks(app, _satellite_profile(), KS_OS_ID="ubuntu",
                         KS_OS_LIKE="debian", KS_OS_VERSION="24.04")
        assert checks["satellite"]["status"] == "fail"

    def test_no_check_without_satellite(self, app):
        assert "satellite" not in _checks(app, default_profile())

    def test_probe_asks_the_capsule_not_pkgs_k8s_io(self, app):
        node = ClusterBuildNode(role="control_plane", address_source="manual")
        with app.app_context():
            script = preflight_mod._probe_script(_build(), node, _satellite_profile())
        assert "KS_SAT_CA=" in script and "KS_SAT_REG=" in script
        assert "https://capsule.example.com:9090/register" in script
        assert "pkgs.k8s.io" not in script
        assert "KS_SUBMAN=present" in script


class TestBasePrep:
    def test_every_node_registers_before_installing(
        self, client, admin_token, app, ssh_profile  # noqa: F811
    ):
        profile_id = _create(client, admin_token).get_json()["data"]["id"]
        payload = make_build_payload(
            endpoint_mode="managed_haproxy",
            nodes=SINGLE_CP_MANAGED_LB_NODES,
            vipAddress="10.0.1.9",
            vipInterface="ens192",
        )
        payload["connectionProfileId"] = ssh_profile["id"]
        payload["buildProfileId"] = profile_id
        created = client.post(
            "/api/cluster-builds", json=payload, headers=auth_headers(admin_token)
        )
        assert created.status_code == 201, created.get_json()
        build = db.session.get(ClusterBuild, created.get_json()["data"]["id"])
        for node in build.nodes:
            node.os_family = "rhel"
            node.os_version = "9.4"
        db.session.commit()

        fake = FakeSshTransport()
        set_transport_factory(lambda: fake)
        try:
            executor._phase_base_prep(build, resolve(db.session.get(BuildProfile, profile_id)))
        finally:
            set_transport_factory(None)

        for address in ("10.0.0.5", "10.0.0.11", "10.0.0.21"):
            scripts = [script for host, script in fake.calls if host == address]
            register = next(i for i, s in enumerate(scripts) if "subscription-manager clean" in s)
            installs = [i for i, s in enumerate(scripts) if "dnf install" in s]
            assert installs and register < min(installs)

    def test_a_debian_node_fails_with_a_reason(
        self, client, admin_token, app, ssh_profile  # noqa: F811
    ):
        profile_id = _create(client, admin_token).get_json()["data"]["id"]
        payload = make_build_payload(nodes=[
            {"role": "control_plane", "hostname": "cp-1", "address": "10.0.0.11"},
        ])
        payload["connectionProfileId"] = ssh_profile["id"]
        payload["buildProfileId"] = profile_id
        build_id = client.post(
            "/api/cluster-builds", json=payload, headers=auth_headers(admin_token)
        ).get_json()["data"]["id"]
        build = db.session.get(ClusterBuild, build_id)
        build.nodes[0].os_family = "debian"
        db.session.commit()
        with pytest.raises(executor._PhaseFailed, match="Satellite"):
            executor._phase_base_prep(build, resolve(db.session.get(BuildProfile, profile_id)))


class TestContainerDataPath:
    """A build whose disk path is not /var keeps its container data there."""

    def _scripts(self, client, admin_token, ssh_profile, path):
        payload = make_build_payload(
            endpoint_mode="managed_haproxy",
            nodes=SINGLE_CP_MANAGED_LB_NODES,
            vipAddress="10.0.1.9",
            vipInterface="ens192",
            diskCheckPath=path,
        )
        payload["connectionProfileId"] = ssh_profile["id"]
        created = client.post(
            "/api/cluster-builds", json=payload, headers=auth_headers(admin_token)
        )
        assert created.status_code == 201, created.get_json()
        build = db.session.get(ClusterBuild, created.get_json()["data"]["id"])
        for node in build.nodes:
            node.os_family = "rhel"
        db.session.commit()
        fake = FakeSshTransport()
        set_transport_factory(lambda: fake)
        try:
            executor._phase_base_prep(build, default_profile())
        finally:
            set_transport_factory(None)
        return fake.calls

    def test_data_is_bind_mounted_onto_the_path(
        self, client, admin_token, app, ssh_profile  # noqa: F811
    ):
        calls = self._scripts(client, admin_token, ssh_profile, "/data")
        for address in ("10.0.0.11", "10.0.0.21"):
            scripts = [s for host, s in calls if host == address]
            move = next(i for i, s in enumerate(scripts) if "kubesight-data" in s)
            containerd = next(i for i, s in enumerate(scripts) if "containerd config default" in s)
            assert move < containerd
            assert "KS_BASE=/data/kubesight-data" in scripts[move]
            assert "mount --bind" in scripts[move] and "/etc/fstab" in scripts[move]
        # Load balancers keep no container data.
        assert not any("kubesight-data" in s for host, s in calls if host == "10.0.0.5")

    def test_var_moves_nothing(self, client, admin_token, app, ssh_profile):  # noqa: F811
        calls = self._scripts(client, admin_token, ssh_profile, "/var")
        assert not any("kubesight-data" in s for _, s in calls)


class TestCapsuleVersion:
    """Satellite mode installs the Kubernetes the capsule actually carries."""

    @staticmethod
    def _listing(*versions):
        lines = ["Available Packages"]
        for version in versions:
            for name in ("kubeadm", "kubelet", "kubectl"):
                lines.append(f"{name}.x86_64   {version}-150500.1.1   k8s-packages")
        return "\n".join(lines) + "\n"

    def test_only_versions_with_all_three_packages_count(self):
        output = self._listing("1.33.5", "1.33.2") + "kubeadm.x86_64  1.33.9-1.1  k8s-packages\n"
        assert executor._capsule_versions(output) == ["1.33.5", "1.33.2"]

    def _build_on(self, client, admin_token, ssh_profile, version, listing, addons=None):  # noqa: F811
        profile_id = _create(client, admin_token).get_json()["data"]["id"]
        payload = make_build_payload(nodes=[
            {"role": "control_plane", "hostname": "cp-1", "address": "10.0.0.11"},
        ])
        payload["k8sVersion"] = version
        payload["connectionProfileId"] = ssh_profile["id"]
        payload["buildProfileId"] = profile_id
        created = client.post("/api/cluster-builds", json=payload, headers=auth_headers(admin_token))
        assert created.status_code == 201, created.get_json()
        build = db.session.get(ClusterBuild, created.get_json()["data"]["id"])
        build.nodes[0].os_family = "rhel"
        if addons is not None:
            build.addons_json = addons
        db.session.commit()
        fake = FakeSshTransport()
        fake.add(lambda host, script: "showduplicates" in script, listing)
        set_transport_factory(lambda: fake)
        try:
            note, logs = executor._settle_satellite_version(
                build, resolve(db.session.get(BuildProfile, profile_id))
            )
        finally:
            set_transport_factory(None)
        # What ran is in the node's own log while it runs, not an empty panel.
        assert "Kubernetes versions on the Satellite capsule" in logs[build.nodes[0].id]
        return build, note

    def test_the_newest_capsule_version_replaces_one_it_lacks(
        self, client, admin_token, app, ssh_profile  # noqa: F811
    ):
        from api.services.cluster_build import k8s_versions

        minors = k8s_versions.enabled_minors()
        newest, older = minors[0], minors[-1]
        build, note = self._build_on(
            client, admin_token, ssh_profile, f"{newest}.1",
            self._listing(f"{older}.2", f"{older}.5"),
        )
        assert build.k8s_version == f"{older}.5"
        assert note and f"{older}.5" in note and f"{newest}.1" in note
        descriptor = executor.cni_registry.get(build.cni_plugin)
        assert descriptor.supports_k8s_minor(build.cni_version, older)

    def test_a_version_the_capsule_has_is_kept(
        self, client, admin_token, app, ssh_profile  # noqa: F811
    ):
        from api.services.cluster_build import k8s_versions

        minor = k8s_versions.enabled_minors()[-1]
        build, note = self._build_on(
            client, admin_token, ssh_profile, f"{minor}.2",
            self._listing(f"{minor}.2", f"{minor}.5"),
        )
        assert build.k8s_version == f"{minor}.2" and note is None

    def test_an_empty_capsule_changes_nothing(
        self, client, admin_token, app, ssh_profile  # noqa: F811
    ):
        from api.services.cluster_build import k8s_versions

        minor = k8s_versions.enabled_minors()[0]
        build, note = self._build_on(client, admin_token, ssh_profile, f"{minor}.1", "")
        assert build.k8s_version == f"{minor}.1" and note is None


class TestPreflightFailuresForTickets:
    def test_failed_checks_are_listed_with_their_fix(self):
        from api.services.cluster_build.provisioning import jobs

        lines = jobs._preflight_failures({"status": "fail", "nodes": [{
            "hostname": "test-vm-1",
            "checks": [
                {"id": "disk", "label": "Free disk on /var", "status": "fail",
                 "detail": "5 GiB free", "hint": "Grow the disk."},
                {"id": "cpu", "label": "CPU count", "status": "pass"},
                {"id": "fsync", "label": "fsync", "status": "warn"},
            ],
        }]})
        assert lines == ["- test-vm-1: Free disk on /var: 5 GiB free Fix: Grow the disk."]


class TestCapsuleVersionMovesAddOns(TestCapsuleVersion):
    def test_add_on_versions_follow_the_minor(self, client, admin_token, app, ssh_profile):  # noqa: F811
        from api.services.cluster_build import addons as addon_registry
        from api.services.cluster_build import k8s_versions

        minors = k8s_versions.enabled_minors()
        newest, older = minors[0], minors[-1]
        metrics = addon_registry.get("metrics-server")
        on_newest = metrics.default_version_for_k8s_minor(newest)
        on_older = metrics.default_version_for_k8s_minor(older)
        assert on_newest != on_older  # 0.9.0 vs 0.7.2
        build, _ = self._build_on(
            client, admin_token, ssh_profile, f"{newest}.1", self._listing(f"{older}.5"),
            addons=[{"id": "metrics-server", "version": on_newest, "config": {}}],
        )
        assert build.k8s_version == f"{older}.5"
        assert build.addons_json[0]["version"] == on_older


class TestWaitingMachinesSaySo:
    def test_the_other_machines_log_that_they_wait(self, client, admin_token, app, ssh_profile):  # noqa: F811
        from api.models import ClusterBuildStep

        profile_id = _create(client, admin_token).get_json()["data"]["id"]
        payload = make_build_payload(nodes=[
            {"role": "control_plane", "hostname": "cp-1", "address": "10.0.0.11"},
            {"role": "worker", "hostname": "w-1", "address": "10.0.0.21"},
        ])
        payload["connectionProfileId"] = ssh_profile["id"]
        payload["buildProfileId"] = profile_id
        created = client.post("/api/cluster-builds", json=payload, headers=auth_headers(admin_token))
        build = db.session.get(ClusterBuild, created.get_json()["data"]["id"])
        for node in build.nodes:
            node.os_family = "rhel"
        db.session.commit()
        set_transport_factory(lambda: FakeSshTransport())
        try:
            executor._settle_satellite_version(build, resolve(db.session.get(BuildProfile, profile_id)))
        finally:
            set_transport_factory(None)
        worker = next(n for n in build.nodes if n.role == "worker")
        step = ClusterBuildStep.query.filter_by(build_id=build.id, phase="base_prep", node_id=worker.id).one()
        assert step.log_tail.startswith("Waiting for cp-1")


class TestEmailWrappedPaste:
    """An email wraps the long command; the copy carries its line breaks."""

    def test_a_url_broken_across_lines_keeps_its_activation_key(self):
        wrapped = URL.replace("LEB-DOPS-9-", "LEB-DOPS-9-\n", 1)
        assert _normalize_satellite_register_url(wrapped) == URL
        assert _normalize_satellite_register_url(PASTED.replace("LEB-DOPS-9-", "LEB-DOPS-9-\r\n", 1)) == URL

    def test_a_token_broken_across_lines_is_joined(self, client, admin_token, app):
        data = _create(client, admin_token,
                       satelliteToken="Bearer\n" + TOKEN[:20] + "\n" + TOKEN[20:]).get_json()["data"]
        assert resolve(db.session.get(BuildProfile, data["id"])).satellite_token == TOKEN
