"""Cilium — experimental until kernel/kube-proxy prerequisites are validated.

Hidden unless KUBESIGHT_ENABLE_CILIUM=true. No upstream single-file manifest is
published for modern Cilium (helm is the supported path), so this descriptor
requires a bundled, pre-rendered manifest — there is no internet fallback.
Render one with::

    python tools/fetch_cluster_build_bundles.py --cilium

which runs ``helm template`` with ``CILIUM_HELM_VALUES`` below and writes
``backend/api/data/cni/cilium/<version>/cilium.yaml``. The values deliberately
leave kube-proxy in place — kubeadm installs it, and kube-proxy replacement is
part of what is still unvalidated here.
"""

from __future__ import annotations

import re

from .base import CniDescriptor, CniRenderError

CILIUM_CHART_REPO = "https://helm.cilium.io/"

# The pod CIDR baked in here is rewritten per build by ``apply_pod_cidr``; it is
# only the value the bundled artifact happens to be rendered with.
CILIUM_HELM_VALUES = {
    "ipam.mode": "cluster-pool",
    "ipam.operator.clusterPoolIPv4PodCIDRList": "{10.244.0.0/16}",
    "kubeProxyReplacement": "false",
    "operator.replicas": "1",
    "hubble.enabled": "false",
}

_CLUSTER_POOL_CIDR_RE = re.compile(
    r'(?m)^([ \t]*cluster-pool-ipv4-cidr:[ \t]*)(?:"[^"\r\n]*"|\'[^\'\r\n]*\'|[^\s#]*)'
    r'[ \t]*$'
)


class _Cilium(CniDescriptor):
    def apply_pod_cidr(self, manifest: str, pod_cidr: str) -> str:
        """Point cluster-pool IPAM at this build's pod CIDR.

        The bundled manifest carries whatever CIDR was passed to helm when it
        was rendered, so applying it unchanged would hand the cluster a pod
        network that disagrees with ``kubeadm --pod-network-cidr``. A manifest
        this rewrite cannot find the key in is rejected rather than installed.
        """
        if not pod_cidr:
            return manifest
        rewritten, count = _CLUSTER_POOL_CIDR_RE.subn(
            lambda match: f'{match.group(1)}"{pod_cidr}"',
            manifest,
        )
        if count == 0:
            raise CniRenderError(
                "Cilium: the bundled manifest has no 'cluster-pool-ipv4-cidr' "
                f"key, so the pod CIDR {pod_cidr} cannot be applied. Re-render "
                "it with tools/fetch_cluster_build_bundles.py --cilium."
            )
        return rewritten


CILIUM = _Cilium(
    id="cilium",
    display_name="Cilium",
    support_tier="experimental",
    versions=("1.16.3",),
    default_pod_cidr="10.244.0.0/16",
    manifest_files=("cilium.yaml",),
    manifest_urls=(),  # bundled-only: render helm template to data/cni/cilium/<ver>/
    readiness_daemonset=("kube-system", "cilium"),
    supported_k8s_minors=("1.29", "1.30", "1.31", "1.32"),
    bundle_hint=(
        "Cilium ships no single-file manifest — render one with "
        "`python tools/fetch_cluster_build_bundles.py --cilium` (needs helm) "
        "on the KubeSight host."
    ),
)
