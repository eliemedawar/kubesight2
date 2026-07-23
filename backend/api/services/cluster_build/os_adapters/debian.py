"""Ubuntu / Debian adapter.

Reuses the hardened apt shell preamble from ``upgrade_executor`` (lock waiting,
bounded retries, permanent-error detection) — one battle-tested implementation
serving both upgrades and builds.
"""

from __future__ import annotations

from typing import Optional

from ....upgrade_executor import _APT_SHELL_PREAMBLE
from ..profiles import DEFAULT_K8S_PKG_REPO_DEB
from .base import (
    OsAdapter,
    OsFacts,
    ScriptContext,
    containerd_config_script,
    shell_preamble,
)


class DebianAdapter(OsAdapter):
    id = "debian"
    display_name = "Ubuntu / Debian"
    validated_versions = ("22.04", "24.04")

    def matches(self, facts: OsFacts) -> bool:
        blob = f"{facts.os_id} {facts.os_id_like}".lower()
        return "debian" in blob or facts.os_id.lower() in {"ubuntu", "debian"}

    def script_configure_ca(self, ctx: ScriptContext) -> Optional[str]:
        pem = ctx.profile.extra_ca_certs_pem.strip()
        if not pem:
            return None
        return f"""{shell_preamble(ctx)}
cat > /usr/local/share/ca-certificates/kubesight-extra.crt <<'KS_CA_EOF'
{pem}
KS_CA_EOF
update-ca-certificates
"""

    def script_install_containerd(self, ctx: ScriptContext) -> str:
        return f"""{shell_preamble(ctx)}
{_APT_SHELL_PREAMBLE}
_apt update -qq
_apt install -y -qq containerd
{containerd_config_script(ctx)}
"""

    def script_install_kube_packages(self, ctx: ScriptContext) -> str:
        repo = ctx.profile.k8s_pkg_repo("debian", ctx.k8s_minor)
        key_url = ctx.profile.k8s_pkg_gpg_key_url or (repo.rstrip("/") + "/Release.key")
        version = ctx.k8s_version.lstrip("v")
        return f"""{shell_preamble(ctx)}
K8S_VER="{version}"
{_APT_SHELL_PREAMBLE}
_apt update -qq
_apt install -y -qq ca-certificates curl gpg
mkdir -p /etc/apt/keyrings
curl -fsSL "{key_url}" | gpg --dearmor --yes -o /etc/apt/keyrings/kubesight-kubernetes.gpg
echo "deb [signed-by=/etc/apt/keyrings/kubesight-kubernetes.gpg] {repo} /" \\
  > /etc/apt/sources.list.d/kubesight-kubernetes.list
apt-mark unhold kubelet kubeadm kubectl 2>/dev/null || true
_apt update -qq
_apt install -y -qq kubelet="${{K8S_VER}}-*" kubeadm="${{K8S_VER}}-*" kubectl="${{K8S_VER}}-*"
apt-mark hold kubelet kubeadm kubectl
systemctl enable kubelet
kubeadm version -o short
"""

    def script_install_haproxy_keepalived(self, ctx: ScriptContext) -> str:
        return f"""{shell_preamble(ctx)}
{_APT_SHELL_PREAMBLE}
_apt update -qq
_apt install -y -qq haproxy keepalived psmisc
"""


# Documented default the resolver falls back to; referenced here so a grep for
# the constant finds both producer and consumer.
_DEFAULT_REPO = DEFAULT_K8S_PKG_REPO_DEB
