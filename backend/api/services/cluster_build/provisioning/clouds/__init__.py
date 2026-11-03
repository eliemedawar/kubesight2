"""Public clouds KubeSight creates machines and clusters in: Oracle Cloud (oci),
AWS (aws) and Google Cloud (gcp). See ``base`` for the contract each adapter
module implements."""

from __future__ import annotations

from types import ModuleType
from typing import Dict, List

from .base import Account, CloudError  # noqa: F401

PROVIDERS = ("oci", "aws", "gcp")


def _modules() -> Dict[str, ModuleType]:
    from . import aws, gcp, oci

    return {"aws": aws, "gcp": gcp, "oci": oci}


def adapter(provider: str) -> ModuleType:
    try:
        return _modules()[provider]
    except KeyError as exc:
        raise ValueError(f"Unknown cloud provider: {provider}.") from exc


def is_cloud(machine_source: str) -> bool:
    return (machine_source or "") in PROVIDERS


def catalog() -> List[Dict]:
    """What the Sources form and the wizard need to know about each cloud."""
    out = []
    for provider in PROVIDERS:
        module = adapter(provider)
        out.append({
            "id": provider,
            "label": module.LABEL,
            "managedLabel": module.MANAGED_LABEL,
            "fields": module.FIELDS,
            "tofuProvider": dict(module.TOFU_PROVIDER),
        })
    return out


def vm_resource_types() -> Dict[str, str]:
    """resource type -> provider, for every cloud's VM resource."""
    return {adapter(p).VM_RESOURCE_TYPE: p for p in PROVIDERS}
