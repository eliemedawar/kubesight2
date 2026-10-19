"""Rocky Linux / RHEL 9 adapter."""

from __future__ import annotations

import base64
from typing import Optional

from ....upgrade_executor import satellite_kube_repo_script
from ..profiles import DEFAULT_CRI_REPO_RPM
from .base import (
    OsAdapter,
    already_installed_check,
    OsFacts,
    ScriptContext,
    containerd_config_script,
    shell_preamble,
)


class RhelAdapter(OsAdapter):
    id = "rhel"
    display_name = "Rocky Linux / RHEL"
    validated_versions = ("9",)
    supports_satellite = True

    def matches(self, facts: OsFacts) -> bool:
        blob = f"{facts.os_id} {facts.os_id_like}".lower()
        return any(
            marker in blob for marker in ("rhel", "rocky", "centos", "almalinux", "fedora")
        )

    def script_configure_ca(self, ctx: ScriptContext) -> Optional[str]:
        pem = ctx.profile.extra_ca_certs_pem.strip()
        if not pem:
            return f"""{shell_preamble(ctx)}
rm -f -- /etc/pki/ca-trust/source/anchors/kubesight-extra.crt
update-ca-trust
"""
        pem_b64 = base64.b64encode(pem.encode("ascii")).decode("ascii")
        return f"""{shell_preamble(ctx)}
rm -f -- /etc/pki/ca-trust/source/anchors/kubesight-extra.crt
echo "{pem_b64}" | base64 -d > /etc/pki/ca-trust/source/anchors/kubesight-extra.crt
chmod 644 /etc/pki/ca-trust/source/anchors/kubesight-extra.crt
update-ca-trust
"""

    def script_install_containerd(self, ctx: ScriptContext) -> str:
        # containerd is not in the Rocky/RHEL base repos; the docker-ce repo
        # (or the profile's mirror of it) carries containerd.io.
        if ctx.profile.repo_mode == "satellite":
            install = """  # Satellite mode: the activation key's repositories carry containerd.io.
  if ! dnf install -y containerd.io; then
    echo "containerd.io is not in the repositories this activation key enables." >&2
    exit 1
  fi"""
        else:
            cri_repo = ctx.profile.cri_pkg_repo_url or DEFAULT_CRI_REPO_RPM
            install = f"""  dnf install -y dnf-plugins-core
  dnf config-manager --add-repo "{cri_repo}"
  dnf install -y containerd.io"""
        return f"""{shell_preamble(ctx)}
if command -v containerd >/dev/null 2>&1; then
  echo "containerd is already installed ($(containerd --version 2>/dev/null | head -1)); configuring it"
else
{install}
fi
{containerd_config_script(ctx)}
"""

    def script_install_kube_packages(self, ctx: ScriptContext) -> str:
        if ctx.profile.repo_mode == "satellite":
            return self._kube_packages_from_satellite(ctx)
        repo = ctx.profile.k8s_pkg_repo("rhel", ctx.k8s_minor)
        key_url = (
            ctx.profile.k8s_pkg_gpg_key_url
            or (repo.rstrip("/") + "/repodata/repomd.xml.key")
        )
        version = ctx.k8s_version.lstrip("v")
        return f"""{shell_preamble(ctx)}
K8S_VER="{version}"
{already_installed_check()}
cat > /etc/yum.repos.d/kubesight-kubernetes.repo <<EOF
[kubesight-kubernetes]
name=Kubernetes (KubeSight)
baseurl={repo}
enabled=1
gpgcheck=1
gpgkey={key_url}
exclude=kubelet kubeadm kubectl cri-tools kubernetes-cni
EOF
# kubeadm preflight requires SELinux permissive (upstream documented step).
setenforce 0 2>/dev/null || true
sed -i 's/^SELINUX=enforcing$/SELINUX=permissive/' /etc/selinux/config 2>/dev/null || true
# Packages that came with the machine may be version-locked; dnf install of an
# exact version up- or downgrades to it once the lock is gone.
dnf versionlock delete kubelet kubeadm kubectl >/dev/null 2>&1 || true
dnf install -y "kubelet-${{K8S_VER}}" "kubeadm-${{K8S_VER}}" "kubectl-${{K8S_VER}}" \\
  --disableexcludes=kubesight-kubernetes
# crictl is installed explicitly rather than inherited: it is a kubeadm package
# dependency on some minors and not on others, and the image pre-pull phase
# drives it directly for every CNI and add-on image. Its version floats — only
# the Kubernetes packages are pinned, and cri-tools is in this repo's exclude
# list, so the install must opt out of it by name.
dnf install -y cri-tools --disableexcludes=kubesight-kubernetes
command -v crictl
systemctl enable kubelet
kubeadm version -o short
"""

    def _kube_packages_from_satellite(self, ctx: ScriptContext) -> str:
        """The repositories come with the activation key. A repo file KubeSight
        wrote earlier (or the VM template carried) would send dnf back out to
        the internet, so it goes."""
        version = ctx.k8s_version.lstrip("v")
        return f"""{shell_preamble(ctx)}
K8S_VER="{version}"
{already_installed_check()}
rm -f /etc/yum.repos.d/kubesight-kubernetes.repo
{satellite_kube_repo_script(version)}
setenforce 0 2>/dev/null || true
sed -i 's/^SELINUX=enforcing$/SELINUX=permissive/' /etc/selinux/config 2>/dev/null || true
dnf versionlock delete kubelet kubeadm kubectl >/dev/null 2>&1 || true
if ! dnf install -y "kubelet-${{K8S_VER}}" "kubeadm-${{K8S_VER}}" "kubectl-${{K8S_VER}}"; then
  echo "kubelet/kubeadm/kubectl ${{K8S_VER}} are not in the Satellite repositories this host can use." >&2
  exit 1
fi
if ! dnf install -y cri-tools; then
  echo "cri-tools (crictl) is not in the repositories this activation key enables." >&2
  exit 1
fi
command -v crictl
systemctl enable kubelet
kubeadm version -o short
"""

    def script_install_nfs_client(self, ctx: ScriptContext) -> str:
        # The kubelet shells out to /sbin/mount.nfs; without nfs-utils an
        # NFS-backed volume fails at pod start with an opaque "wrong fs type".
        return f"""{shell_preamble(ctx)}
dnf install -y nfs-utils
command -v mount.nfs
"""

    def script_install_haproxy_keepalived(self, ctx: ScriptContext) -> str:
        return f"""{shell_preamble(ctx)}
dnf install -y haproxy keepalived psmisc
# keepalived VIP is not a locally-configured address; haproxy must bind it anyway.
cat > /etc/sysctl.d/98-kubesight-lb.conf <<'EOF'
net.ipv4.ip_nonlocal_bind = 1
EOF
sysctl --system > /dev/null
"""
