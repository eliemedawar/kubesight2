"""kubeadm config rendering + init-output parsing.

``kubeadm init`` runs from a rendered config FILE (never a pile of flags) so
what executed is reviewable in the step log. Join commands are reconstructed
from parsed token/hash/cert-key rather than scraped verbatim — kubeadm's
multi-line, backslash-continued echo is too fragile to copy.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Optional

from .profiles import DEFAULT_K8S_IMAGE_REGISTRY, ResolvedProfile

CRI_SOCKET = "unix:///run/containerd/containerd.sock"

# Pause (sandbox) image tag kubeadm pins per minor — the containerd
# sandbox_image and the verify smoke pod must match it, or mirror/offline
# registries that only carry the kubeadm image list break.
_PAUSE_TAGS = {"1.29": "3.9", "1.30": "3.9", "1.31": "3.10", "1.32": "3.10"}


def pause_image_tag(k8s_version: str) -> str:
    parts = k8s_version.lstrip("v").split(".")
    minor = ".".join(parts[:2]) if len(parts) >= 2 else k8s_version
    return _PAUSE_TAGS.get(minor, "3.10")


def render_init_config(
    *,
    k8s_version: str,
    control_plane_endpoint: str,
    pod_cidr: str,
    service_cidr: str,
    profile: ResolvedProfile,
    node_name: str,
    server_tls_bootstrap: bool = False,
) -> str:
    """InitConfiguration + ClusterConfiguration + KubeletConfiguration YAML.

    v1beta3 is accepted across the supported 1.29–1.33 range (deprecated from
    1.31 but functional; revisit at 1.34 when it is removed).
    """
    version = k8s_version.lstrip("v")
    image_repo_block = ""
    if profile.k8s_image_registry and profile.k8s_image_registry != DEFAULT_K8S_IMAGE_REGISTRY:
        image_repo_block = f"imageRepository: {profile.k8s_image_registry}\n"
    endpoint_host = control_plane_endpoint.rsplit(":", 1)[0]
    server_tls_line = "serverTLSBootstrap: true\n" if server_tls_bootstrap else ""
    return f"""apiVersion: kubeadm.k8s.io/v1beta3
kind: InitConfiguration
nodeRegistration:
  name: {json.dumps(node_name)}
  criSocket: {CRI_SOCKET}
---
apiVersion: kubeadm.k8s.io/v1beta3
kind: ClusterConfiguration
kubernetesVersion: v{version}
controlPlaneEndpoint: "{control_plane_endpoint}"
{image_repo_block}networking:
  podSubnet: {pod_cidr}
  serviceSubnet: {service_cidr}
apiServer:
  certSANs:
    - "{endpoint_host}"
---
apiVersion: kubelet.config.k8s.io/v1beta1
kind: KubeletConfiguration
cgroupDriver: systemd
{server_tls_line}"""


@dataclass
class InitArtifacts:
    """Secrets parsed from kubeadm init output. Handle like live ammunition:
    encrypt immediately, scrub from logs, null after the joins complete."""

    token: str = ""
    ca_cert_hash: str = ""    # "sha256:<hex>" — public
    certificate_key: str = ""

    def worker_join_command(self, endpoint: str) -> str:
        return (
            f"kubeadm join {endpoint} --token {self.token} "
            f"--discovery-token-ca-cert-hash {self.ca_cert_hash}"
        )

    def control_plane_join_command(self, endpoint: str) -> str:
        return (
            self.worker_join_command(endpoint)
            + f" --control-plane --certificate-key {self.certificate_key}"
        )


_TOKEN_RE = re.compile(r"--token[=\s]+([a-z0-9]{6}\.[a-z0-9]{16})")
_HASH_RE = re.compile(r"--discovery-token-ca-cert-hash[=\s]+(sha256:[0-9a-f]{64})")
_CERT_KEY_ARG_RE = re.compile(r"--certificate-key[=\s]+([0-9a-f]{64})")
_CERT_KEY_LINE_RE = re.compile(
    r"Using certificate key:\s*\n?\s*([0-9a-f]{64})", re.IGNORECASE
)


def parse_certificate_key(output: str) -> str:
    """Certificate key from ``kubeadm init phase upload-certs --upload-certs``
    output (used to re-mint an expired key before a control-plane join)."""
    match = _CERT_KEY_LINE_RE.search(output) or _CERT_KEY_ARG_RE.search(output)
    return match.group(1) if match else ""


def parse_init_output(output: str) -> InitArtifacts:
    artifacts = InitArtifacts()
    token_match = _TOKEN_RE.search(output)
    if token_match:
        artifacts.token = token_match.group(1)
    hash_match = _HASH_RE.search(output)
    if hash_match:
        artifacts.ca_cert_hash = hash_match.group(1)
    key_match = _CERT_KEY_ARG_RE.search(output) or _CERT_KEY_LINE_RE.search(output)
    if key_match:
        artifacts.certificate_key = key_match.group(1)
    return artifacts


def validate_init_artifacts(
    artifacts: InitArtifacts, *, need_certificate_key: bool
) -> Optional[str]:
    """Returns an error string when parsing came up short, else None."""
    if not artifacts.token or not artifacts.ca_cert_hash:
        return (
            "kubeadm init succeeded but the join token/CA hash could not be "
            "parsed from its output."
        )
    if need_certificate_key and not artifacts.certificate_key:
        return (
            "kubeadm init succeeded but no certificate key was found in its "
            "output (--upload-certs expected for HA builds)."
        )
    return None
