"""OS adapter contract + shared script fragments.

An adapter produces the shell scripts that prepare one node for kubeadm. The
orchestrator never contains distro conditionals — adding an OS is a new adapter
module plus a registry entry, with zero engine changes.
"""

from __future__ import annotations

import base64
import hashlib
import shlex
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


SATELLITE_MARKER = "/etc/kubesight/satellite-registration"


def satellite_registration_script(ctx: ScriptContext) -> str:
    """Register the machine with Red Hat Satellite through its global
    registration endpoint, the way Satellite's "Register Host" page does:
    trust the capsule's CA, clean any identity the VM template carried, then
    run the script Satellite returns for the token and activation key.

    The script is downloaded first rather than piped into bash, so an expired
    token reads as one instead of as bash choking on an error page. The token
    reaches curl through a root-only header file, never the command line.
    A marker of (URL, machine UUID) skips the work on a retry; a clone of a
    registered machine has a different UUID and registers afresh.
    """
    url = ctx.profile.satellite_register_url
    url_id = hashlib.sha256(url.encode("utf-8")).hexdigest()[:16]
    header_b64 = base64.b64encode(
        f"Authorization: Bearer {ctx.profile.satellite_token}".encode("utf-8")
    ).decode("ascii")
    host = shlex.quote(ctx.profile.satellite_host)
    ca_url = shlex.quote(ctx.profile.satellite_ca_url)
    return f"""{shell_preamble(ctx)}
KS_MARK={SATELLITE_MARKER}
KS_WANT="{url_id} $(cat /sys/class/dmi/id/product_uuid 2>/dev/null)"
if [ -f "$KS_MARK" ] && [ "$(cat "$KS_MARK")" = "$KS_WANT" ] \\
   && subscription-manager identity >/dev/null 2>&1; then
  echo "Already registered with Satellite ({ctx.profile.satellite_host}) for this build: keeping it"
  exit 0
fi
if ! command -v subscription-manager >/dev/null 2>&1; then
  echo "subscription-manager is not installed; Satellite registration needs it." >&2
  exit 1
fi
echo "Trusting the CA of {ctx.profile.satellite_host}"
curl -k -sS --fail -m 60 -o /etc/pki/ca-trust/source/anchors/katello-server-ca.crt {ca_url}
update-ca-trust
# A VM cloned from a registered template carries the template's identity.
subscription-manager clean
umask 077
KS_DIR="$(mktemp -d /run/.kubesight-satellite.XXXXXX)"
trap 'rm -rf -- "$KS_DIR"' EXIT
echo "{header_b64}" | base64 -d > "$KS_DIR/header"
KS_CODE=$(curl -sS -m 120 -o "$KS_DIR/register.sh" -w '%{{http_code}}' \\
  --header @"$KS_DIR/header" {shlex.quote(url)}) || {{
  echo "Could not reach Satellite at {ctx.profile.satellite_host} to fetch the registration script." >&2
  exit 1
}}
rm -f -- "$KS_DIR/header"
case "$KS_CODE" in
  2??) ;;
  401|403)
    echo "Satellite refused the registration token (HTTP $KS_CODE). Registration tokens expire: generate a new one in Satellite (Hosts > Register Host) and replace it on this source profile." >&2
    exit 1 ;;
  *)
    echo "Satellite answered HTTP $KS_CODE instead of a registration script:" >&2
    head -c 600 "$KS_DIR/register.sh" >&2
    exit 1 ;;
esac
if ! head -1 "$KS_DIR/register.sh" | grep -q '^#!'; then
  echo "Satellite did not return a registration script:" >&2
  head -c 600 "$KS_DIR/register.sh" >&2
  exit 1
fi
echo "Registering with Satellite through {ctx.profile.satellite_host}"
bash "$KS_DIR/register.sh"
subscription-manager identity
mkdir -p "$(dirname "$KS_MARK")"
echo "$KS_WANT" > "$KS_MARK"
dnf repolist --enabled 2>/dev/null || true
"""


RELOCATED_DIRS = ("containerd", "kubelet", "etcd")


def relocate_data_script(data_path: str) -> str:
    """Put /var/lib/{containerd,kubelet,etcd} on ``data_path`` with bind mounts.

    Every component keeps its standard path, so nothing else changes; the
    mounts go in /etc/fstab and survive a reboot. Anything already in a
    directory (a VM template's containerd) is copied across first. SELinux
    labels are copied from the original directory so containerd keeps working
    on an enforcing machine.
    """
    base = data_path.rstrip("/") + "/kubesight-data"
    dirs = " ".join(RELOCATED_DIRS)
    return f"""set -e
KS_BASE={shlex.quote(base)}
if [ ! -d {shlex.quote(data_path)} ]; then
  echo "{data_path} does not exist on this machine." >&2
  exit 1
fi
systemctl stop kubelet 2>/dev/null || true
systemctl stop containerd 2>/dev/null || true
for name in {dirs}; do
  src="$KS_BASE/$name"
  dst="/var/lib/$name"
  mkdir -p "$src" "$dst"
  if mountpoint -q "$dst"; then
    echo "$dst is already a mount ($(findmnt -n -o SOURCE "$dst")): keeping it"
    continue
  fi
  if [ -n "$(ls -A "$dst" 2>/dev/null)" ] && [ -z "$(ls -A "$src" 2>/dev/null)" ]; then
    echo "Copying what $dst already holds to $src"
    cp -a "$dst/." "$src/"
  fi
  if command -v selinuxenabled >/dev/null 2>&1 && selinuxenabled; then
    chcon -R --reference="$dst" "$src" 2>/dev/null || true
  fi
  grep -qE "^[^#]*[[:space:]]$dst[[:space:]]+none[[:space:]]+bind" /etc/fstab \\
    || echo "$src $dst none bind 0 0" >> /etc/fstab
  mount --bind "$src" "$dst"
  echo "$dst -> $src"
done
systemctl daemon-reload
if systemctl list-unit-files containerd.service >/dev/null 2>&1; then
  systemctl start containerd 2>/dev/null || true
fi
df -h {shlex.quote(data_path)}
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


# What a machine that already ran Kubernetes carries, and what kubeadm's own
# preflight refuses to overwrite. Preflight looks for exactly these; the
# template cleanup below removes exactly these.
K8S_STATE_FILES = (
    "/etc/kubernetes/admin.conf",
    "/etc/kubernetes/kubelet.conf",
    "/etc/kubernetes/pki/ca.crt",
    "/var/lib/kubelet/config.yaml",
)
K8S_STATE_DIRS = ("/etc/kubernetes/manifests", "/var/lib/etcd")


def k8s_state_probe() -> str:
    """POSIX fragment that sets ``KS_STATE`` to the leftover paths found."""
    files = " ".join(K8S_STATE_FILES)
    dirs = " ".join(K8S_STATE_DIRS)
    return f"""KS_STATE=""
for f in {files}; do
  [ -e "$f" ] && KS_STATE="$KS_STATE $f"
done
for d in {dirs}; do
  [ -d "$d" ] && [ -n "$(ls -A "$d" 2>/dev/null)" ] && KS_STATE="$KS_STATE $d"
done
KS_STATE="${{KS_STATE# }}"
"""


def already_installed_check() -> str:
    """Shell that ends the package step early when the machine already has
    kubeadm, kubelet and kubectl at ``$K8S_VER`` and crictl: nothing is
    downloaded, which is what lets a VM template's own Kubernetes build a
    cluster even where the package repository is out of reach."""
    return """
ks_ver() { "$@" 2>/dev/null | grep -oE 'v[0-9]+[.][0-9]+[.][0-9]+' | head -1; }
if [ "$(ks_ver kubeadm version -o short)" = "v$K8S_VER" ] \
   && [ "$(ks_ver kubelet --version)" = "v$K8S_VER" ] \
   && [ "$(ks_ver kubectl version --client)" = "v$K8S_VER" ] \
   && command -v crictl >/dev/null 2>&1; then
  echo "kubeadm, kubelet and kubectl v$K8S_VER are already installed (with crictl): keeping them"
  apt-mark hold kubelet kubeadm kubectl >/dev/null 2>&1 || true
  systemctl enable kubelet
  kubeadm version -o short
  exit 0
fi
"""


def clear_template_state_script(_ctx: ScriptContext) -> str:
    """Remove a previous cluster's state from a VM cloned for this build.

    Only ever generated for machines OpenTofu created for this build: a VM
    template made from a machine that once ran Kubernetes hands every clone
    that cluster's certificates, static pods and etcd data, and kubeadm will
    not start over them. A no-op when the machine is clean.
    """
    return f"""
set +e
{k8s_state_probe()}
if [ -z "$KS_STATE" ]; then
  echo "No earlier Kubernetes state on this machine."
  exit 0
fi
echo "Clearing Kubernetes state carried over from the VM template: $KS_STATE"
if command -v kubeadm >/dev/null 2>&1; then
  kubeadm reset -f
fi
systemctl stop kubelet 2>/dev/null
# Static pods keep running inside containerd after the kubelet stops, and keep
# holding :6443 and :2379 — stop them too.
if command -v crictl >/dev/null 2>&1; then
  crictl --runtime-endpoint unix:///run/containerd/containerd.sock rmp -af 2>/dev/null
fi
rm -rf /etc/kubernetes/manifests /etc/kubernetes/pki /etc/kubernetes/admin.conf \\
  /etc/kubernetes/kubelet.conf /etc/kubernetes/controller-manager.conf \\
  /etc/kubernetes/scheduler.conf /etc/kubernetes/super-admin.conf
rm -rf /var/lib/kubelet/config.yaml /var/lib/kubelet/kubeadm-flags.env /var/lib/kubelet/pki
rm -rf /var/lib/etcd /etc/cni/net.d /var/lib/cni
iptables -F 2>/dev/null; iptables -t nat -F 2>/dev/null; iptables -t mangle -F 2>/dev/null
ipvsadm --clear 2>/dev/null
exit 0
"""


class OsAdapter:
    """Interface. Subclasses override everything below."""

    id = ""
    display_name = ""
    validated_versions: tuple = ()
    # Red Hat Satellite registers RHEL-family machines only.
    supports_satellite = False

    def matches(self, facts: OsFacts) -> bool:
        raise NotImplementedError

    def script_register_satellite(self, ctx: ScriptContext) -> Optional[str]:
        """None when the profile does not register with Satellite."""
        if not ctx.profile.registers_with_satellite:
            return None
        return satellite_registration_script(ctx)

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

    def script_clear_template_state(self, ctx: ScriptContext) -> str:
        return clear_template_state_script(ctx)
