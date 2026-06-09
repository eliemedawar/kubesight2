from __future__ import annotations

from dataclasses import dataclass
from typing import Optional


@dataclass(frozen=True)
class ClusterAccess:
    cluster_id: str
    context_name: Optional[str]
    kubeconfig_path: Optional[str] = None
    display_name: Optional[str] = None
    is_custom: bool = False


def custom_cluster_public_id(db_id: int) -> str:
    return f"custom-{db_id}"


def parse_custom_cluster_db_id(cluster_id: str) -> Optional[int]:
    if not cluster_id:
        return None
    value = cluster_id.strip()
    if value.startswith("custom-"):
        suffix = value[len("custom-") :]
    else:
        suffix = value
    try:
        parsed = int(suffix)
    except ValueError:
        return None
    return parsed if parsed > 0 else None


def is_custom_cluster_id(cluster_id: str) -> bool:
    return parse_custom_cluster_db_id(cluster_id) is not None
