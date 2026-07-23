"""Calico — the production default CNI."""

from __future__ import annotations

import re

from ..profiles import ResolvedProfile
from .base import CniDescriptor


class _Calico(CniDescriptor):
    def apply_pod_cidr(self, manifest: str, pod_cidr: str) -> str:
        """Uncomment and set CALICO_IPV4POOL_CIDR to the build's pod CIDR.
        Upstream ships it commented with a 192.168.0.0/16 default."""
        if not pod_cidr:
            return manifest
        # Commented form. Match horizontal whitespace explicitly: ``\s`` also
        # consumes newlines and previously produced a blank line plus an
        # over-indented ``value``, making the rendered manifest invalid YAML.
        #
        #   # - name: CALICO_IPV4POOL_CIDR
        #   #   value: "192.168.0.0/16"
        manifest = re.sub(
            r'(?m)^([ \t]*)# - name: CALICO_IPV4POOL_CIDR[ \t]*\r?\n'
            r'[ \t]*#   value: "[^"]*"',
            rf'\g<1>- name: CALICO_IPV4POOL_CIDR'
            rf'\n\g<1>  value: "{pod_cidr}"',
            manifest,
        )
        # Already-uncommented form (bundled/mirrored copies).
        manifest = re.sub(
            r'(?m)^([ \t]*)- name: CALICO_IPV4POOL_CIDR[ \t]*\r?\n'
            r'[ \t]+value: "[^"]*"',
            rf'\g<1>- name: CALICO_IPV4POOL_CIDR'
            rf'\n\g<1>  value: "{pod_cidr}"',
            manifest,
        )
        return manifest


CALICO = _Calico(
    id="calico",
    display_name="Calico",
    support_tier="production",
    versions=("3.28.2", "3.27.4"),
    default_pod_cidr="10.244.0.0/16",
    manifest_files=("calico.yaml",),
    manifest_urls=(
        "https://raw.githubusercontent.com/projectcalico/calico/v{version}/manifests/calico.yaml",
    ),
    readiness_daemonset=("kube-system", "calico-node"),
)
