"""MetalLB native-mode add-on.

MetalLB without an ``IPAddressPool`` is inert: the controller runs, and every
``type: LoadBalancer`` Service sits in ``<pending>`` forever. The pool is
therefore part of selecting the add-on, not a documented afterthought — the
executor applies it and then proves a Service actually gets an address out of
it.
"""

from __future__ import annotations

import ipaddress
import json
from typing import Any, Dict, List, Tuple

from .base import AddonConfigError, AddonDescriptor

POOL_NAME = "kubesight-default"
_MAX_POOL_ENTRIES = 16


def parse_pool_entry(entry: str):
    """Return the (first, last) address pair a pool entry covers.

    Accepts MetalLB's two forms: a CIDR (``10.0.0.240/28``) or an inclusive
    range (``10.0.0.240-10.0.0.250``).
    """
    text = str(entry).strip()
    if not text:
        raise AddonConfigError("An address pool entry cannot be blank.")
    if "-" in text:
        start_text, _, end_text = text.partition("-")
        try:
            start = ipaddress.ip_address(start_text.strip())
            end = ipaddress.ip_address(end_text.strip())
        except ValueError as exc:
            raise AddonConfigError(
                f"'{text}' is not a valid address range — use start-end, "
                "for example 10.0.0.240-10.0.0.250."
            ) from exc
        if start.version != end.version:
            raise AddonConfigError(f"'{text}' mixes IPv4 and IPv6 addresses.")
        if int(end) < int(start):
            raise AddonConfigError(f"'{text}' ends before it starts.")
        return start, end
    try:
        network = ipaddress.ip_network(text, strict=False)
    except ValueError as exc:
        raise AddonConfigError(
            f"'{text}' is not a valid CIDR or address range."
        ) from exc
    return network[0], network[-1]


def pool_contains(entries, address) -> bool:
    """True when ``address`` falls inside any of the pool ``entries``."""
    try:
        candidate = ipaddress.ip_address(str(address).strip())
    except ValueError:
        return False
    for entry in entries:
        first, last = parse_pool_entry(entry)
        if (
            first.version == candidate.version
            and int(first) <= int(candidate) <= int(last)
        ):
            return True
    return False


def _canonicalize(entry: str) -> str:
    text = str(entry).strip()
    if "-" in text:
        start, end = parse_pool_entry(text)
        return f"{start}-{end}"
    return str(ipaddress.ip_network(text, strict=False))


class _MetalLb(AddonDescriptor):
    def normalize_config(self, config: Dict[str, Any]) -> Dict[str, Any]:
        if not isinstance(config, dict):
            raise AddonConfigError("MetalLB configuration must be an object.")
        unknown = set(config) - {"addressPools"}
        if unknown:
            raise AddonConfigError(
                f"Unknown MetalLB setting(s): {', '.join(sorted(unknown))}."
            )
        raw = config.get("addressPools")
        if isinstance(raw, str):
            raw = raw.replace("\n", ",").split(",")
        if raw is None:
            raw = []
        if not isinstance(raw, list):
            raise AddonConfigError(
                "MetalLB addressPools must be a list of CIDRs or ranges."
            )
        entries = [str(item).strip() for item in raw if str(item).strip()]
        if not entries:
            raise AddonConfigError(
                "MetalLB needs at least one address pool — a range such as "
                "10.0.0.240-10.0.0.250, or a CIDR such as 10.0.0.240/28, "
                "reserved for LoadBalancer services on the node network. "
                "Without one, every LoadBalancer service stays pending."
            )
        if len(entries) > _MAX_POOL_ENTRIES:
            raise AddonConfigError(
                f"At most {_MAX_POOL_ENTRIES} MetalLB address pool entries "
                "are supported."
            )

        canonical: List[str] = []
        spans: List[Tuple[int, int, int, str]] = []
        for entry in entries:
            first, last = parse_pool_entry(entry)
            text = _canonicalize(entry)
            if text in canonical:
                raise AddonConfigError(
                    f"Address pool entry '{text}' is listed more than once."
                )
            for version, other_first, other_last, other_text in spans:
                if version != first.version:
                    continue
                if int(first) <= other_last and other_first <= int(last):
                    raise AddonConfigError(
                        f"Address pool entries '{other_text}' and '{text}' overlap."
                    )
            spans.append((first.version, int(first), int(last), text))
            canonical.append(text)
        return {"addressPools": canonical}

    def pool_manifest(self, config: Dict[str, Any]) -> str:
        """IPAddressPool + L2Advertisement for the configured ranges."""
        entries = list((config or {}).get("addressPools") or [])
        if not entries:
            raise AddonConfigError("MetalLB was selected without an address pool.")
        addresses = "\n".join(f"    - {json.dumps(entry)}" for entry in entries)
        return f"""apiVersion: metallb.io/v1beta1
kind: IPAddressPool
metadata:
  name: {POOL_NAME}
  namespace: metallb-system
spec:
  autoAssign: true
  addresses:
{addresses}
---
apiVersion: metallb.io/v1beta1
kind: L2Advertisement
metadata:
  name: {POOL_NAME}
  namespace: metallb-system
spec:
  ipAddressPools:
    - {POOL_NAME}
"""


METALLB = _MetalLb(
    id="metallb",
    display_name="MetalLB",
    description=(
        "Bare-metal LoadBalancer support in lightweight native mode (L2). "
        "Requires TCP/UDP 7946 between cluster nodes and a range of free "
        "addresses on the node network reserved for LoadBalancer services."
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
    config_fields=(
        {
            "key": "addressPools",
            "type": "ipRangeList",
            "label": "LoadBalancer address pool",
            "required": True,
            "placeholder": "10.0.0.240-10.0.0.250",
            "help": (
                "Free addresses on the node network, as ranges or CIDRs, one "
                "per line. They must not overlap the API VIP, the node "
                "addresses, or your DHCP scope."
            ),
        },
    ),
)
