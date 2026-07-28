"""Flannel — lightweight lab option."""

from __future__ import annotations

import re

from .base import CniDescriptor


class _Flannel(CniDescriptor):
    def apply_pod_cidr(self, manifest: str, pod_cidr: str) -> str:
        """Rewrite the Network CIDR inside the kube-flannel ConfigMap's
        net-conf.json (upstream default 10.244.0.0/16)."""
        if not pod_cidr:
            return manifest
        return re.sub(
            r'("Network"\s*:\s*)"[^"]*"',
            rf'\g<1>"{pod_cidr}"',
            manifest,
        )


FLANNEL = _Flannel(
    id="flannel",
    display_name="Flannel",
    support_tier="lab",
    versions=("0.25.6",),
    default_pod_cidr="10.244.0.0/16",
    manifest_files=("kube-flannel.yml",),
    manifest_urls=(
        "https://github.com/flannel-io/flannel/releases/download/v{version}/kube-flannel.yml",
    ),
    manifest_sha256={
        "0.25.6": (
            "e4e34aeb64934aa2122dff71b369d2c29b1ccea8d7aa23d5349d6361f1ee5a5c",
        ),
    },
    readiness_daemonset=("kube-flannel", "kube-flannel-ds"),
    # No Flannel release is vendored for Kubernetes 1.33+, so the wizard offers
    # Flannel only on the older minors rather than pairing it with a Kubernetes
    # version nobody validated it against.
    k8s_minors_by_version={"0.25.6": ("1.29", "1.30", "1.31", "1.32")},
)
