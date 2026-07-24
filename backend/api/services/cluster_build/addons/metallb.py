"""MetalLB native-mode add-on."""

from __future__ import annotations

from .base import AddonDescriptor


METALLB = AddonDescriptor(
    id="metallb",
    display_name="MetalLB",
    description=(
        "Bare-metal LoadBalancer support in lightweight native mode (L2 "
        "recommended). Requires TCP/UDP 7946 between cluster nodes; configure "
        "an IPAddressPool and advertisement after the build."
    ),
    # The builder's Kubernetes 1.29-1.32 matrix is upstream-EOL, so MetalLB's
    # compatibility policy classifies these combinations as best effort.
    support_tier="best-effort",
    versions=("0.16.1",),
    manifest_files=("metallb-native.yaml",),
    manifest_urls=(
        "https://raw.githubusercontent.com/metallb/metallb/"
        "v{version}/config/manifests/metallb-native.yaml",
    ),
    manifest_sha256=(
        "bf25feebb7582ca7df845efd52ffbc2960d6cbf4cfc972f47fded9f788b67f0b",
    ),
    readiness_commands=(
        "wait --for=condition=Established "
        "customresourcedefinition/ipaddresspools.metallb.io --timeout=120s",
        "wait --for=condition=Established "
        "customresourcedefinition/l2advertisements.metallb.io --timeout=120s",
        "-n metallb-system rollout status deployment/controller --timeout=600s",
        "-n metallb-system rollout status daemonset/speaker --timeout=900s",
    ),
)
