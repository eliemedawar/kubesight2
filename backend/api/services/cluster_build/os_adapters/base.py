"""OS adapter contract + shared script fragments.

An adapter produces the shell scripts that prepare one node for kubeadm. The
orchestrator never contains distro conditionals — adding an OS is a new adapter
module plus a registry entry, with zero engine changes.
"""

from __future__ import annotations

import base64
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
    kubeadm's systemd cgroup driver), sandbox image from the resolved registry,
    persistent forward-proxy environment, and optional registry authentication.

    Sensitive generated content is base64-fed into root-owned files, and the
    executor deliberately hides this stage's command text from persisted logs.
    """
    from ..kubeadm import pause_image_tag  # local import: avoids module cycle

    pause_image = (
        f"{ctx.profile.k8s_image_registry}/pause:{pause_image_tag(ctx.k8s_version)}"
    )
    proxy_lines = ["[Service]"]
    for name, value in (
        ("HTTP_PROXY", ctx.profile.http_proxy),
        ("HTTPS_PROXY", ctx.profile.https_proxy),
        ("NO_PROXY", ctx.profile.no_proxy),
    ):
        if value:
            escaped = (
                value.replace("\\", "\\\\")
                .replace('"', '\\"')
                .replace("%", "%%")
                .replace("\r", "")
                .replace("\n", "")
            )
            proxy_lines.append(f'Environment="{name}={escaped}"')
            proxy_lines.append(f'Environment="{name.lower()}={escaped}"')
    proxy_dropin = "\n".join(proxy_lines) + "\n" if len(proxy_lines) > 1 else ""
    proxy_b64 = base64.b64encode(proxy_dropin.encode("utf-8")).decode("ascii")

    def _toml(value: str) -> str:
        return (
            value.replace("\\", "\\\\")
            .replace('"', '\\"')
            .replace("\r", "\\r")
            .replace("\n", "\\n")
        )

    def _auth_b64(plugin_id: str) -> str:
        blocks = []
        if (
            ctx.profile.registry_username
            and ctx.profile.registry_password
            and ctx.profile.registry_auth_host
        ):
            blocks.append(
                f'[plugins."{plugin_id}".registry.configs.'
                f'"{_toml(ctx.profile.registry_auth_host)}".auth]\n'
                f'  username = "{_toml(ctx.profile.registry_username)}"\n'
                f'  password = "{_toml(ctx.profile.registry_password)}"\n'
            )
        content = "\n".join(blocks)
        return base64.b64encode(content.encode("utf-8")).decode("ascii")

    # containerd 1.x uses config v2/plugin grpc.v1.cri. containerd 2.x uses
    # config v3/plugin cri.v1.images. The node chooses the matching encrypted
    # fragment after generating its default config.
    auth_v2_b64 = _auth_b64("io.containerd.grpc.v1.cri")
    auth_v3_b64 = _auth_b64("io.containerd.cri.v1.images")

    return f"""
umask 077
mkdir -p /etc/containerd
containerd config default > /etc/containerd/config.toml
if grep -Eq '^[[:space:]]*version[[:space:]]*=[[:space:]]*3' /etc/containerd/config.toml; then
  # containerd 2.x / config v3.
  sed -i 's#sandbox = .*#sandbox = "{pause_image}"#' /etc/containerd/config.toml
  sed -i 's/SystemdCgroup = false/SystemdCgroup = true/' /etc/containerd/config.toml
  if ! grep -q 'SystemdCgroup = true' /etc/containerd/config.toml; then
    awk '{{ print; if ($0 ~ /io\\.containerd\\.cri\\.v1\\.runtime.*runtimes\\.runc\\.options\\]/) print "          SystemdCgroup = true" }}' \
      /etc/containerd/config.toml > /etc/containerd/config.toml.kubesight
    mv /etc/containerd/config.toml.kubesight /etc/containerd/config.toml
  fi
  AUTH_B64="{auth_v3_b64}"
else
  # containerd 1.x / config v2.
  sed -i 's/SystemdCgroup = false/SystemdCgroup = true/' /etc/containerd/config.toml
  sed -i 's#sandbox_image = ".*"#sandbox_image = "{pause_image}"#' /etc/containerd/config.toml
  AUTH_B64="{auth_v2_b64}"
fi
if [ -n "$AUTH_B64" ]; then
  echo "$AUTH_B64" | base64 -d >> /etc/containerd/config.toml
fi
chmod 600 /etc/containerd/config.toml
mkdir -p /etc/systemd/system/containerd.service.d
if [ -n "{proxy_b64}" ]; then
  umask 077
  echo "{proxy_b64}" | base64 -d > \
    /etc/systemd/system/containerd.service.d/kubesight-proxy.conf
  chmod 600 /etc/systemd/system/containerd.service.d/kubesight-proxy.conf
else
  rm -f /etc/systemd/system/containerd.service.d/kubesight-proxy.conf
fi
systemctl daemon-reload
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

    def script_install_nfs_client(self, ctx: ScriptContext) -> str:
        """Userspace NFS mount helper, needed on every node that will run a pod
        with an NFS-backed volume. Only generated when a build actually has
        one — see the Cluster Builder's workload copy."""
        raise NotImplementedError

    # Shared fragments (distro-independent).
    def script_kernel_prep(self, ctx: ScriptContext) -> str:
        return shell_preamble(ctx) + kernel_prep_script(ctx)

    def script_reset_node(self, ctx: ScriptContext) -> str:
        return reset_node_script(ctx)
