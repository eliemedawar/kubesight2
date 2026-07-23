"""OS adapter contract + shared script fragments.

An adapter produces the shell scripts that prepare one node for kubeadm. The
orchestrator never contains distro conditionals — adding an OS is a new adapter
module plus a registry entry, with zero engine changes.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Optional

from ..profiles import ResolvedProfile


@dataclass(frozen=True)
class OsFacts:
    """Parsed /etc/os-release + uname, gathered by the preflight probe."""

    os_id: str = ""          # "ubuntu" | "rocky" | "rhel" | ...
    os_id_like: str = ""     # "debian" | "rhel fedora" | ...
    version_id: str = ""     # "24.04" | "9.4"
    pretty_name: str = ""
    arch: str = ""           # "x86_64" | "aarch64"


@dataclass(frozen=True)
class ScriptContext:
    """Everything an adapter script needs: version pins + resolved repos."""

    profile: ResolvedProfile
    k8s_version: str          # "1.32.4" (no leading v)
    facts: OsFacts = field(default_factory=OsFacts)

    @property
    def k8s_minor(self) -> str:
        parts = self.k8s_version.lstrip("v").split(".")
        return ".".join(parts[:2]) if len(parts) >= 2 else self.k8s_version


def shell_preamble(ctx: ScriptContext) -> str:
    """set -e + proxy exports; the first lines of every generated script."""
    lines = ["set -e"]
    proxy = ctx.profile.proxy_env()
    if proxy:
        lines.append(proxy)
    return "\n".join(lines)


def containerd_config_script(ctx: ScriptContext) -> str:
    """Shared containerd post-install config: SystemdCgroup=true (mandatory for
    kubeadm's systemd cgroup driver) + sandbox image from the resolved registry."""
    from ..kubeadm import pause_image_tag  # local import: avoids module cycle

    pause_image = (
        f"{ctx.profile.k8s_image_registry}/pause:{pause_image_tag(ctx.k8s_version)}"
    )
    return f"""
mkdir -p /etc/containerd
containerd config default > /etc/containerd/config.toml
sed -i 's/SystemdCgroup = false/SystemdCgroup = true/' /etc/containerd/config.toml
sed -i 's#sandbox_image = ".*"#sandbox_image = "{pause_image}"#' /etc/containerd/config.toml
systemctl enable containerd
systemctl restart containerd
"""


def kernel_prep_script(_ctx: ScriptContext) -> str:
    """Swap off + kernel modules + sysctl — identical across supported distros."""
    return """
swapoff -a
sed -i '/\\sswap\\s/s/^\\([^#]\\)/#\\1/' /etc/fstab
cat > /etc/modules-load.d/kubesight-k8s.conf <<'EOF'
overlay
br_netfilter
EOF
modprobe overlay
modprobe br_netfilter
cat > /etc/sysctl.d/99-kubesight-k8s.conf <<'EOF'
net.bridge.bridge-nf-call-iptables  = 1
net.bridge.bridge-nf-call-ip6tables = 1
net.ipv4.ip_forward                 = 1
EOF
sysctl --system > /dev/null
"""


def reset_node_script(_ctx: ScriptContext) -> str:
    """kubeadm reset + CNI/iptables cleanup — the per-node retry path."""
    return """
set +e
kubeadm reset -f 2>/dev/null
rm -rf /etc/cni/net.d /var/lib/cni /etc/kubernetes/pki /etc/kubernetes/manifests
rm -rf /var/lib/etcd
iptables -F 2>/dev/null; iptables -t nat -F 2>/dev/null; iptables -t mangle -F 2>/dev/null
ipvsadm --clear 2>/dev/null
systemctl restart containerd 2>/dev/null
exit 0
"""


class OsAdapter:
    """Interface. Subclasses override everything below."""

    id = ""
    display_name = ""
    validated_versions: tuple = ()

    def matches(self, facts: OsFacts) -> bool:
        raise NotImplementedError

    def version_validated(self, facts: OsFacts) -> bool:
        return any(facts.version_id.startswith(v) for v in self.validated_versions)

    def script_configure_ca(self, ctx: ScriptContext) -> Optional[str]:
        raise NotImplementedError

    def script_install_containerd(self, ctx: ScriptContext) -> str:
        raise NotImplementedError

    def script_install_kube_packages(self, ctx: ScriptContext) -> str:
        raise NotImplementedError

    def script_install_haproxy_keepalived(self, ctx: ScriptContext) -> str:
        raise NotImplementedError

    # Shared fragments (distro-independent).
    def script_kernel_prep(self, ctx: ScriptContext) -> str:
        return shell_preamble(ctx) + kernel_prep_script(ctx)

    def script_reset_node(self, ctx: ScriptContext) -> str:
        return reset_node_script(ctx)
