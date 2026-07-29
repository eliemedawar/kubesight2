"""Rocky Linux / RHEL 9 adapter."""

from __future__ import annotations

import base64
from typing import Optional

from ..profiles import DEFAULT_CRI_REPO_RPM
from .base import (
    OsAdapter,
    OsFacts,
    ScriptContext,
    containerd_config_script,
    shell_preamble,
)


class RhelAdapter(OsAdapter):
    id = "rhel"
    display_name = "Rocky Linux / RHEL"
    validated_versions = ("9",)

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
        cri_repo = ctx.profile.cri_pkg_repo_url or DEFAULT_CRI_REPO_RPM
        return f"""{shell_preamble(ctx)}
dnf install -y dnf-plugins-core
dnf config-manager --add-repo "{cri_repo}"
dnf install -y containerd.io
{containerd_config_script(ctx)}
"""

    def script_install_kube_packages(self, ctx: ScriptContext) -> str:
        repo = ctx.profile.k8s_pkg_repo("rhel", ctx.k8s_minor)
        key_url = (
            ctx.profile.k8s_pkg_gpg_key_url
            or (repo.rstrip("/") + "/repodata/repomd.xml.key")
        )
        version = ctx.k8s_version.lstrip("v")
        return f"""{shell_preamble(ctx)}
K8S_VER="{version}"
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
dnf install -y "kubelet-${{K8S_VER}}" "kubeadm-${{K8S_VER}}" "kubectl-${{K8S_VER}}" \\
  --disableexcludes=kubesight-kubernetes
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
