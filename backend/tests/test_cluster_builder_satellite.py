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

    def test_satellite_mode_switches_to_the_minor_repository_first(self):
        ctx = ScriptContext(profile=_satellite_profile(), k8s_version="1.37.1")
        packages = self._rhel().script_install_kube_packages(ctx)
        assert "[vV-]1[._]37([^0-9]|$)" in packages
        assert packages.index("subscription-manager repos $KS_KUBE_ARGS") < packages.index("dnf install")

    def test_other_modes_keep_their_repositories(self):
        ctx = ScriptContext(profile=_satellite_profile("internet"), k8s_version=VERSION)
        assert "pkgs.k8s.io" in self._rhel().script_install_kube_packages(ctx)
        assert "download.docker.com" in self._rhel().script_install_containerd(ctx)

    def test_only_rhel_registers(self):
        assert self._rhel().supports_satellite
        assert not os_adapters.by_id("debian").supports_satellite

    def test_a_bare_token_is_scrubbed_from_logs(self):
        assert TOKEN not in scrub(f"token was {TOKEN}")


REPOS_LISTING = """+----------------------------------------------------------+
    Available Repositories in /etc/yum.repos.d/redhat.repo
+----------------------------------------------------------+
Repo ID:   Areeba_Kubernetes_k8s-packages-v1_33
Repo Name: k8s-packages-v1.33
Repo URL:  https://capsule/pulp/content/k8s-packages-v1_33
Enabled:   0

Repo ID:   Areeba_Kubernetes_helm-stable-rhel9
Repo Name: helm-stable-rhel9
Repo URL:  https://capsule/pulp/content/helm-stable-rhel9
Enabled:   1

Repo ID:   rhel-9-for-x86_64-baseos-rpms
Repo Name: Red Hat Enterprise Linux 9 for x86_64 - BaseOS (RPMs)
Repo URL:  https://capsule/pulp/content/baseos
Enabled:   1

Repo ID:   Areeba_Kubernetes_k8s-packages-v1_37
Repo Name: k8s-packages-v1.37
Repo URL:  https://capsule/pulp/content/k8s-packages-v1_37
Enabled:   0

Repo ID:   Areeba_Kubernetes_k8s-packages-v1_34
Repo Name: k8s-packages-v1.34
Repo URL:  https://capsule/pulp/content/k8s-packages-v1_34
Enabled:   1

Repo ID:   Areeba_Kubernetes_docker-ce-stable-rhel9
Repo Name: docker-ce-stable-rhel9
Repo URL:  https://capsule/pulp/content/docker-ce-stable-rhel9
Enabled:   1
"""


def _run_repo_switch(tmp_path, version, listing=REPOS_LISTING, registered=True):
    """Run the repository switch against a fake subscription-manager; return
    (exit code, output, the arguments ``repos`` was changed with)."""
    import shutil
    import subprocess

    from api.upgrade_executor import satellite_kube_repo_script

    if shutil.which("sh") is None:
        pytest.skip("needs a POSIX shell")
    (tmp_path / "listing.txt").write_text(listing)
    fake = tmp_path / "subscription-manager"
    fake.write_text(
        "#!/bin/sh\n"
        f'case "$1" in\n'
        f'  identity) exit {0 if registered else 1} ;;\n'
        f'  repos) if [ "$2" = "--list" ]; then cat "{(tmp_path / "listing.txt").as_posix()}"; '
        f'else echo "$@" >> "{(tmp_path / "changed.txt").as_posix()}"; fi ;;\n'
        "esac\n"
    )
    fake.chmod(0o755)
    import os

    env = {**os.environ, "PATH": f"{tmp_path}{os.pathsep}{os.environ.get('PATH', '')}"}
    script = "set -e\n" + satellite_kube_repo_script(version)
    result = subprocess.run(["sh", "-c", script], capture_output=True, text=True, env=env)
    changed = tmp_path / "changed.txt"
    return result.returncode, result.stdout + result.stderr, changed.read_text().split() if changed.exists() else []


class TestSatelliteMinorRepository:
    """The activation key enables one Kubernetes minor; the build or upgrade
    switches the host to the minor it installs."""

    def test_the_requested_minor_is_enabled_and_the_others_disabled(self, tmp_path):
        code, output, changed = _run_repo_switch(tmp_path, "1.37.1")
        assert code == 0, output
        assert "--enable=Areeba_Kubernetes_k8s-packages-v1_37" in changed
        assert {"--disable=Areeba_Kubernetes_k8s-packages-v1_33",
                "--disable=Areeba_Kubernetes_k8s-packages-v1_34"} <= set(changed)
        # Kubernetes-branded repositories without a version are left alone.
        assert not [arg for arg in changed if "helm" in arg or "docker" in arg or "rhel-9" in arg]

    def test_a_minor_satellite_lacks_changes_nothing(self, tmp_path):
        code, output, changed = _run_repo_switch(tmp_path, "1.36.2")
        assert code == 0, output
        assert changed == []
        assert "no Kubernetes 1.36 repository" in output

    def test_an_unregistered_host_is_left_alone(self, tmp_path):
        code, output, changed = _run_repo_switch(tmp_path, "1.37.1", registered=False)
        assert code == 0 and changed == []

    def test_upgrades_switch_before_installing(self):
        from api import upgrade_executor as ex

        for script in (ex._shell_script_control_plane_upgrade("v1.35.2"),
                       ex._shell_script_upgrade_worker_node("v1.35.2")):
            assert "[vV-]1[._]35([^0-9]|$)" in script
            assert script.index("subscription-manager repos $KS_KUBE_ARGS") < script.index("dnf install")


def _build():
    return ClusterBuild(
        name="sat", k8s_version=VERSION, topology_type="single_cp",
        endpoint_mode="manual_endpoint", control_plane_endpoint="10.0.0.100:6443",
        cni_plugin="calico", pod_cidr="10.244.0.0/16", service_cidr="10.96.0.0/12",
    )


def _checks(app, profile, drop=(), **facts):
    base = {"KS_OS_ID": "rhel", "KS_OS_LIKE": "fedora", "KS_OS_VERSION": "9.4",
            "KS_ARCH": "x86_64", "KS_SUBMAN": "present", "KS_SAT_CA": "200",
            "KS_SAT_REG": "404", "repo_ok": [], "repo_fail": []}
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
            executor._phase_base_prep(build, resolve(db.session.get(BuildProfile, profile_id)))
        finally:
            set_transport_factory(None)
        from api.models import ClusterBuildStep

        step = ClusterBuildStep.query.filter_by(
            build_id=build.id, phase="base_prep", node_id=build.nodes[0].id
        ).one()
        # What ran is in the node's own log while it runs, not an empty panel.
        assert "Kubernetes versions on the Satellite capsule" in step.log_tail
        label = next(
            line for line in step.log_tail.splitlines()
            if line.startswith(">>> Kubernetes packages (kubeadm, kubelet, kubectl)")
        )
        note = label.split(" — ", 1)[1] if " — " in label else None
        # The packages install the version the capsule settled on.
        packages = [script for _, script in fake.calls if 'K8S_VER="' in script and "dnf install" in script]
        assert packages and all(f'K8S_VER="{build.k8s_version}"' in script for script in packages)
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

    def test_the_minor_asked_for_wins_over_a_newer_one(
        self, client, admin_token, app, ssh_profile  # noqa: F811
    ):
        from api.services.cluster_build import k8s_versions

        minors = k8s_versions.enabled_minors()
        newest, older = minors[0], minors[-1]
        build, note = self._build_on(
            client, admin_token, ssh_profile, f"{older}.1",
            self._listing(f"{newest}.3", f"{older}.2", f"{older}.5"),
        )
        assert build.k8s_version == f"{older}.5"
        assert note and f"newest {older} patch" in note

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


class TestMachinesPrepareTogether:
    def test_the_second_machine_starts_while_the_first_checks_the_capsule(
        self, client, admin_token, app, ssh_profile  # noqa: F811
    ):
        """Both machines start at once; the worker waits for the capsule's
        answer only before containerd and the Kubernetes packages."""
        import threading
        import time as _time

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
        worker_started = threading.Event()
        fake = FakeSshTransport()
        real_run = fake.run

        def run(target, script, timeout_s=None, on_output=None, escalate=True):
            if target.host == "10.0.0.21" and "swapoff" in script:
                worker_started.set()
            if "showduplicates" in script:
                # Before: the worker had not begun while cp-1 checked the capsule.
                assert worker_started.wait(10), "the worker waited for cp-1 to finish"
                _time.sleep(0.5)  # the worker reaches the version-dependent steps
            return real_run(target, script, timeout_s=timeout_s, on_output=on_output, escalate=escalate)

        fake.run = run
        set_transport_factory(lambda: fake)
        try:
            executor._phase_base_prep(build, resolve(db.session.get(BuildProfile, profile_id)))
        finally:
            set_transport_factory(None)
        worker = next(n for n in build.nodes if n.role == "worker")
        step = ClusterBuildStep.query.filter_by(build_id=build.id, phase="base_prep", node_id=worker.id).one()
        assert step.status == "completed"
        assert "Waiting for cp-1" in step.log_tail
        # containerd and the packages came only after cp-1's answer.
        listing = next(i for i, (_, sc) in enumerate(fake.calls) if "showduplicates" in sc)
        worker_containerd = next(
            i for i, (host, sc) in enumerate(fake.calls) if host == "10.0.0.21" and "containerd" in sc
            and "config default" in sc
        )
        assert worker_containerd > listing


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
