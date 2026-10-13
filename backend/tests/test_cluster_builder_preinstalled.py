"""Machines that already carry Kubernetes — usually from the VM template.

Tools the template installed are replaced, never a blocker. A previous
cluster's state blocks a machine somebody brought, and is cleared on a VM
KubeSight created for this build.
"""

from __future__ import annotations

import pytest

from api.models import ClusterBuild, ClusterBuildNode
from api.services.cluster_build import k8s_versions
from api.services.cluster_build import os_adapters
from api.services.cluster_build import preflight as preflight_mod
from api.services.cluster_build.os_adapters import OsFacts
from api.services.cluster_build.os_adapters.base import ScriptContext
from api.services.cluster_build.profiles import default_profile

MINOR = k8s_versions.enabled_minors()[0]
VERSION = f"{MINOR}.4"


def _build() -> ClusterBuild:
    return ClusterBuild(
        name="pre", k8s_version=VERSION, topology_type="single_cp",
        endpoint_mode="manual_endpoint", control_plane_endpoint="10.0.0.100:6443",
        cni_plugin="calico", pod_cidr="10.244.0.0/16", service_cidr="10.96.0.0/12",
    )


def _checks(app, node, **facts):
    base = {"KS_OS_ID": "ubuntu", "KS_OS_LIKE": "debian", "KS_OS_VERSION": "24.04",
            "KS_ARCH": "x86_64", "repo_ok": [], "repo_fail": [], "image_ok": [],
            "image_fail": []}
    base.update(facts)
    with app.app_context():
        checks = preflight_mod._node_checks(_build(), node, base, default_profile())
    return {check["id"]: check for check in checks}


def _cp(source="manual"):
    return ClusterBuildNode(role="control_plane", address_source=source)


class TestInstalledTools:
    def test_nothing_installed_passes(self, app):
        check = _checks(app, _cp())["k8s_tools"]
        assert check["status"] == "pass"
        assert "None installed" in check["detail"]

    def test_an_older_kubectl_from_the_template_is_replaced_not_failed(self, app):
        check = _checks(app, _cp(), KS_HAS_kubectl="v1.30.2")["k8s_tools"]
        assert check["status"] == "pass"
        assert "kubectl v1.30.2" in check["detail"]
        assert f"replaces them with v{VERSION}" in check["detail"]

    def test_the_exact_version_is_kept(self, app):
        wanted = f"v{VERSION}"
        check = _checks(app, _cp(), KS_HAS_kubeadm=wanted, KS_HAS_kubelet=wanted,
                        KS_HAS_kubectl=wanted)["k8s_tools"]
        assert check["status"] == "pass"
        assert "already installed and kept" in check["detail"]

    def test_load_balancers_are_not_asked(self, app):
        assert "k8s_tools" not in _checks(app, ClusterBuildNode(role="loadbalancer"))


class TestLeftoverState:
    STATE = "/etc/kubernetes/admin.conf /etc/kubernetes/manifests /var/lib/etcd"

    def test_clean_machine_passes(self, app):
        assert _checks(app, _cp())["k8s_state"]["status"] == "pass"

    def test_a_machine_someone_brought_is_never_wiped(self, app):
        checks = _checks(app, _cp("manual"), KS_K8S_STATE=self.STATE,
                         KS_PORT_6443="busy:kube-apiserver")
        assert checks["k8s_state"]["status"] == "fail"
        assert "kubeadm reset" in checks["k8s_state"]["hint"]
        assert checks["port_6443"]["status"] == "fail"
        assert "kube-apiserver" in checks["port_6443"]["detail"]

    def test_a_vm_created_for_this_build_is_cleared(self, app):
        checks = _checks(app, _cp("provisioned"), KS_K8S_STATE=self.STATE,
                         KS_PORT_6443="busy:kube-apiserver", KS_PORT_10250="busy:kubelet")
        assert checks["k8s_state"]["status"] == "pass"
        assert "base prep clears it" in checks["k8s_state"]["detail"]
        assert checks["port_6443"]["status"] == "pass"
        assert checks["port_10250"]["status"] == "pass"

    def test_a_foreign_process_on_a_port_still_fails_on_a_created_vm(self, app):
        checks = _checks(app, _cp("provisioned"), KS_K8S_STATE=self.STATE,
                         KS_PORT_6443="busy:nginx")
        assert checks["port_6443"]["status"] == "fail"

    def test_an_owner_the_probe_could_not_name_still_fails(self, app):
        checks = _checks(app, _cp("manual"), KS_PORT_6443="busy")
        assert checks["port_6443"]["status"] == "fail"


class TestScripts:
    def test_probe_asks_for_tools_state_and_port_owner(self, app):
        with app.app_context():
            script = preflight_mod._probe_script(_build(), _cp(), default_profile())
        assert "KS_HAS_$tool=" in script
        assert "KS_K8S_STATE=" in script
        assert "/var/lib/etcd" in script
        assert 'KS_PORT_$port=busy:$KS_OWNER' in script

    @pytest.mark.parametrize("os_id,like", [("ubuntu", "debian"), ("rocky", "rhel fedora")])
    def test_install_overrides_what_the_template_pinned(self, app, os_id, like):
        adapter = os_adapters.detect(OsFacts(os_id=os_id, os_id_like=like, version_id="24.04"))
        with app.app_context():
            script = adapter.script_install_kube_packages(
                ScriptContext(profile=default_profile(), k8s_version=VERSION)
            )
        if os_id == "ubuntu":
            assert "--allow-downgrades --allow-change-held-packages" in script
            assert ".kubesight-disabled" in script
        else:
            assert "versionlock delete" in script

    def test_template_cleanup_is_a_noop_on_a_clean_machine(self, app):
        adapter = os_adapters.detect(OsFacts(os_id="ubuntu", os_id_like="debian"))
        with app.app_context():
            script = adapter.script_clear_template_state(
                ScriptContext(profile=default_profile(), k8s_version=VERSION)
            )
        assert 'if [ -z "$KS_STATE" ]' in script
        assert script.index("exit 0") < script.index("kubeadm reset -f")
