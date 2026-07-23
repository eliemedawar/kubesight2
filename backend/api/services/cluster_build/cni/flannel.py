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
    readiness_daemonset=("kube-flannel", "kube-flannel-ds"),
)
