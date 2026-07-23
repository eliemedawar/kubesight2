"""Cilium — experimental until kernel/kube-proxy prerequisites are validated.

Hidden unless KUBESIGHT_ENABLE_CILIUM=true. No upstream single-file manifest is
published for modern Cilium (helm is the supported path), so this descriptor
requires a bundled, pre-rendered manifest — there is no internet fallback.
"""

from __future__ import annotations

from .base import CniDescriptor

CILIUM = CniDescriptor(
    id="cilium",
    display_name="Cilium",
    support_tier="experimental",
    versions=("1.16.3",),
    default_pod_cidr="10.244.0.0/16",
    manifest_files=("cilium.yaml",),
    manifest_urls=(),  # bundled-only: render helm template to data/cni/cilium/<ver>/
    readiness_daemonset=("kube-system", "cilium"),
)
