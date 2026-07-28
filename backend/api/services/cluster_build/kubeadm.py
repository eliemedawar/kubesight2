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

from . import k8s_versions
from .profiles import DEFAULT_K8S_IMAGE_REGISTRY, ResolvedProfile

CRI_SOCKET = "unix:///run/containerd/containerd.sock"


def pause_image_tag(k8s_version: str) -> str:
    """The pause (sandbox) tag kubeadm pins for this version's minor.

    Read from the per-minor support table, which records kubeadm's
    ``PauseVersion`` constant per release branch. There is deliberately **no**
    generic fallback: guessing here would point containerd's ``sandbox_image``
    and the verify smoke pod at a tag kubeadm never asks for, which a mirror or
    offline registry carrying only kubeadm's image list cannot serve — and the
    failure would surface as an unexplained sandbox pull error mid-build rather
    than as a rejected version.
    """
    record = k8s_versions.record_for(k8s_version)
    if record is None:
        raise ValueError(
            f"No pause image tag is recorded for Kubernetes '{k8s_version}'. "
            "Add a support record for its minor before building with it."
        )
    return record.pause_image_tag


def config_api_version(k8s_version: str) -> str:
    """The kubeadm configuration API this version's minor must be given.

    ``kubeadm.k8s.io/v1beta4`` exists from kubeadm 1.31 and is the only version
    current kubeadm documents as supported; v1beta3 is deprecated upstream and
    is emitted only for the pre-1.31 minors that cannot read v1beta4. Builds
    pinned to those older minors keep working unchanged.
    """
    record = k8s_versions.record_for(k8s_version)
    if record is None:
        raise ValueError(
            f"No kubeadm configuration API is recorded for Kubernetes "
            f"'{k8s_version}'."
        )
    return record.kubeadm_config_api


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

    The kubeadm API version comes from the minor's support record. Every field
    emitted below carries the same name and nesting in v1beta3 and v1beta4 —
    ``nodeRegistration.{name,criSocket}``, ``kubernetesVersion``,
    ``controlPlaneEndpoint``, ``imageRepository``,
    ``networking.{podSubnet,serviceSubnet}`` and ``apiServer.certSANs`` — so the
    document is schema-valid under both. The v1beta4 changes that would matter
    (structured ``extraArgs`` lists, the removal of
    ``apiServer.timeoutForControlPlane`` in favour of ``timeouts``) touch fields
    this renderer does not set; ``preflight`` re-parses the rendered document
    per build rather than trusting that by inspection.

    KubeletConfiguration keeps its own group: ``kubelet.config.k8s.io/v1beta1``
    is unrelated to the kubeadm API version and is current across this range.
    """
    version = k8s_version.lstrip("v")
    api_version = config_api_version(version)
    image_repo_block = ""
    if profile.k8s_image_registry and profile.k8s_image_registry != DEFAULT_K8S_IMAGE_REGISTRY:
        image_repo_block = f"imageRepository: {profile.k8s_image_registry}\n"
    endpoint_host = control_plane_endpoint.rsplit(":", 1)[0]
    server_tls_line = "serverTLSBootstrap: true\n" if server_tls_bootstrap else ""
    return f"""apiVersion: {api_version}
kind: InitConfiguration
nodeRegistration:
  name: {json.dumps(node_name)}
  criSocket: {CRI_SOCKET}
---
apiVersion: {api_version}
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
