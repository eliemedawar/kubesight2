"""CNI plugin registry.

Each plugin is a ``CniDescriptor`` with a closed set of tested versions —
users pick from the list, never paste manifest URLs. Manifests are preferred
from the bundled data dir (``backend/api/data/cni/<id>/<version>/``).
Non-offline modes may fetch the integrity-pinned upstream URL through the
configured outbound proxy; offline mode requires the bundled copy.

Tiers: calico=production (default), flannel=lab, cilium=experimental (hidden
unless KUBESIGHT_ENABLE_CILIUM=true — its kernel/kube-proxy prerequisites are
not yet validated).
"""

from __future__ import annotations

import os
from typing import Dict, List, Optional

from .base import CniDescriptor, CniRenderError  # noqa: F401
from .calico import CALICO
from .cilium import CILIUM
from .flannel import FLANNEL

_ALL: Dict[str, CniDescriptor] = {d.id: d for d in (CALICO, FLANNEL, CILIUM)}


def _cilium_enabled() -> bool:
    return os.getenv("KUBESIGHT_ENABLE_CILIUM", "").strip().lower() in {"1", "true", "yes"}


def available() -> List[CniDescriptor]:
    plugins = [CALICO, FLANNEL]
    if _cilium_enabled():
        plugins.append(CILIUM)
    return plugins


def get(plugin_id: str) -> Optional[CniDescriptor]:
    descriptor = _ALL.get(plugin_id)
    if descriptor is CILIUM and not _cilium_enabled():
        return None
    return descriptor


def catalog() -> List[dict]:
    """Plugin catalog for the wizard.

    ``k8sMinorsByVersion`` lets the wizard narrow the plugin and version lists
    to whatever the user picked on the Kubernetes-version row, so an
    incompatible pairing is never offered in the first place. The options
    payload is fetched once, before that choice exists, which is why the whole
    matrix ships rather than a pre-filtered list.
    """
    return [
        {
            "id": d.id,
            "displayName": d.display_name,
            "supportTier": d.support_tier,
            "versions": list(d.versions),
            "defaultVersion": d.versions[0],
            "defaultPodCidr": d.default_pod_cidr,
            "k8sMinorsByVersion": {
                version: list(d.supported_k8s_minors(version))
                for version in d.versions
            },
        }
        for d in available()
    ]
