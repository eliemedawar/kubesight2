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
    versions=("3.32.1", "3.28.2", "3.27.4"),
    default_pod_cidr="10.244.0.0/16",
    manifest_files=("calico.yaml",),
    manifest_urls=(
        "https://raw.githubusercontent.com/projectcalico/calico/v{version}/manifests/calico.yaml",
    ),
    manifest_sha256={
        "3.32.1": (
            "a1df919d9721cf667accdc3e72848911b0cb25cfab7d2478ad0c996302c95744",
        ),
        "3.28.2": (
            "be59408bf990e96276f631d2f9285c2a0f9802194c0ad1cecdb6d9c52623a1c8",
        ),
        "3.27.4": (
            "53250439641223c04f25035d9855f980b640baa73cde99bcfac5457b242fc51f",
        ),
    },
    readiness_daemonset=("kube-system", "calico-node"),
    k8s_minors_by_version={
        # Calico documents 3.32 as tested against Kubernetes 1.34-1.36; it does
        # not cover the older minors, which is why 3.28.2 stays in the catalog.
        "3.32.1": ("1.34", "1.35", "1.36"),
        # 3.28/3.27 are what KubeSight has shipped and validated across the
        # 1.29-1.32 range. Upstream's own tested matrix for these releases is
        # narrower (3.28 → 1.27-1.29); narrowing this to match would invalidate
        # existing 1.30-1.32 builds, so the KubeSight-validated range is what is
        # declared here.
        "3.28.2": ("1.29", "1.30", "1.31", "1.32"),
        "3.27.4": ("1.29", "1.30", "1.31", "1.32"),
    },
)
