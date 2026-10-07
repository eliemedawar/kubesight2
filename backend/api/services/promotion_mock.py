"""Workloads of the bound namespaces when the cluster is a mock one.

Mock mode has no API server, so the promotion board would be empty and the
promote flow untestable. This keeps a small, believable ladder in memory —
three applications spread over dev/sit/uat/preprod namespaces at different
versions — plus whatever the mock namespace data already holds. A promotion
on a mock cluster changes the image here, so the board moves the way it would
on a real one. Process-local and reset on restart, like the rest of mock mode.
"""

from __future__ import annotations

import copy
import os
import random
import threading
from typing import Any, Dict, List, Optional, Tuple

_LOCK = threading.Lock()

# (cluster, namespace, name) -> image. Deployments only, two replicas each.
_SEED: Dict[Tuple[str, str, str], str] = {
    ("staging-eu-west", "payments-dev", "payments-api"): "ghcr.io/mock/payments:v2.9.0",
    ("staging-eu-west", "payments-dev", "ledger-worker"): "ghcr.io/mock/ledger:v1.21.0",
    ("staging-eu-west", "payments-dev", "checkout-api"): "ghcr.io/mock/checkout:v3.3.0",
    ("staging-eu-west", "payments-dev", "redis"): "redis:7.2",
    ("staging-eu-west", "payments-sit", "payments-api"): "ghcr.io/mock/payments:v2.8.4",
    ("staging-eu-west", "payments-sit", "ledger-worker"): "ghcr.io/mock/ledger:v1.21.0",
    ("staging-eu-west", "payments-sit", "checkout-api"): "ghcr.io/mock/checkout:v3.3.0",
    ("staging-eu-west", "payments-sit", "redis"): "redis:7.2",
    ("staging-eu-west", "payments-uat", "payments-api"): "ghcr.io/mock/payments:v2.8.1",
    ("staging-eu-west", "payments-uat", "ledger-worker"): "ghcr.io/mock/ledger:v1.20.2",
    ("staging-eu-west", "payments-uat", "checkout-api"): "ghcr.io/mock/checkout:v3.2.0",
    ("prod-us-east", "payments-preprod", "payments-api"): "ghcr.io/mock/payments:v2.8.1",
    ("prod-us-east", "payments-preprod", "ledger-worker"): "ghcr.io/mock/ledger:v1.20.2",
    ("prod-us-east", "payments-preprod", "checkout-api"): "ghcr.io/mock/checkout:v3.2.0",
}

# PROMOTION_MOCK_APPS=150 adds a realistic estate on top of the seed: that
# many applications over ten systems, each system in <system>-dev/-sit/-uat
# (staging) and <system>-preprod (prod), versions spread the way a real ladder
# drifts — most behind by a step or two, some rolling out, a few skipped.
_SYSTEMS = (
    "cards", "acquiring", "ledger", "onboarding", "notifications",
    "risk", "reporting", "loyalty", "identity", "gateway",
)
_COMPONENTS = (
    "api", "worker", "scheduler", "web", "adapter", "sync", "consumer", "batch",
    "auth", "events", "export", "rules", "pricing", "search", "audit", "webhooks",
)
_PART_OF: Dict[Tuple[str, str, str], str] = {}
_UNHEALTHY: set = set()


def _scaled_seed() -> Dict[Tuple[str, str, str], str]:
    try:
        count = int(os.getenv("PROMOTION_MOCK_APPS", "0") or 0)
    except ValueError:
        count = 0
    if count <= 0:
        return {}
    rng = random.Random(42)
    out: Dict[Tuple[str, str, str], str] = {}
    for index in range(count):
        system = _SYSTEMS[index % len(_SYSTEMS)]
        component = _COMPONENTS[(index // len(_SYSTEMS)) % len(_COMPONENTS)]
        suffix = index // (len(_SYSTEMS) * len(_COMPONENTS))
        name = f"{system}-{component}" + (f"-{suffix + 1}" if suffix else "")
        repo = f"nexus.areeba.local/{system}/{name}"
        major, minor = rng.randint(1, 4), rng.randint(0, 30)
        patch = rng.randint(2, 9)
        versions = [f"{major}.{minor}.{patch - d}" for d in range(4)]  # newest first
        roll = rng.random()
        # How far behind each environment is (index into versions).
        if roll < 0.35:
            lag = (0, 0, 1, 1)
        elif roll < 0.6:
            lag = (0, 1, 2, 2)
        elif roll < 0.8:
            lag = (0, 0, 0, 1)
        elif roll < 0.92:
            lag = (0, 0, 0, 0)
        else:
            lag = (1, 1, 0, 2)  # UAT runs a version SIT never had: a skip
        places = (
            ("staging-eu-west", f"{system}-dev"),
            ("staging-eu-west", f"{system}-sit"),
            ("staging-eu-west", f"{system}-uat"),
            ("prod-us-east", f"{system}-preprod"),
        )
        for (cluster, namespace), back in zip(places, lag):
            if namespace.endswith("-preprod") and rng.random() < 0.08:
                continue  # not deployed in pre-prod yet
            key = (cluster, namespace, name)
            out[key] = f"{repo}:{versions[back]}"
            _PART_OF[key] = system
            if namespace.endswith("-dev") and rng.random() < 0.06:
                _UNHEALTHY.add(key)
    return out


_SEED.update(_scaled_seed())
_STATE: Dict[Tuple[str, str, str], str] = dict(_SEED)


def _workload(
    cluster_id: str, namespace: str, name: str, image: str, desired: int = 2, *, ready: Optional[int] = None,
    part_of: str = "",
) -> Dict[str, Any]:
    app = name
    labels = {"app.kubernetes.io/name": app}
    if part_of:
        labels["app.kubernetes.io/part-of"] = part_of
    ready = desired if ready is None else ready
    return {
        "apiVersion": "apps/v1",
        "kind": "Deployment",
        "metadata": {
            "name": name,
            "namespace": namespace,
            "labels": labels,
            "generation": 1,
        },
        "spec": {
            "replicas": desired,
            "selector": {"matchLabels": {"app.kubernetes.io/name": app}},
            "template": {
                "metadata": {"labels": {"app.kubernetes.io/name": app}},
                "spec": {"containers": [{"name": name, "image": image}]},
            },
        },
        "status": {
            "observedGeneration": 1,
            "replicas": desired,
            "updatedReplicas": desired,
            "readyReplicas": ready,
            "availableReplicas": ready,
        },
    }


def _from_namespace_resources(cluster_id: str) -> List[Dict[str, Any]]:
    try:
        from ..mock_data import NAMESPACE_RESOURCES
    except Exception:  # noqa: BLE001
        return []
    out = []
    for namespace, resources in (NAMESPACE_RESOURCES.get(cluster_id) or {}).items():
        for dep in resources.get("deployments") or []:
            if dep.get("name") and dep.get("image"):
                replicas = dep.get("replicas") or {}
                out.append(
                    _workload(
                        cluster_id, namespace, dep["name"], dep["image"], int(replicas.get("desired") or 1)
                    )
                )
    return out


def workloads(cluster_id: str) -> List[Dict[str, Any]]:
    """Every mock workload of a cluster, as ``kubectl get -o json`` items."""
    with _LOCK:
        state = dict(_STATE)
    items = [
        _workload(
            cluster, namespace, name, image,
            ready=1 if (cluster, namespace, name) in _UNHEALTHY else None,
            part_of=_PART_OF.get((cluster, namespace, name), ""),
        )
        for (cluster, namespace, name), image in sorted(state.items())
        if cluster == cluster_id
    ]
    return items + _from_namespace_resources(cluster_id)


def namespaces(cluster_id: str) -> List[str]:
    with _LOCK:
        names = {ns for (cluster, ns, _name) in _STATE if cluster == cluster_id}
    try:
        from ..mock_data import NAMESPACE_RESOURCES

        names |= set((NAMESPACE_RESOURCES.get(cluster_id) or {}).keys())
    except Exception:  # noqa: BLE001
        pass
    return sorted(names)


def get(cluster_id: str, namespace: str, name: str) -> Optional[Dict[str, Any]]:
    for item in workloads(cluster_id):
        meta = item.get("metadata") or {}
        if meta.get("namespace") == namespace and meta.get("name") == name:
            return copy.deepcopy(item)
    return None


def set_image(cluster_id: str, namespace: str, name: str, image: str) -> None:
    with _LOCK:
        _STATE[(cluster_id, namespace, name)] = image
        _UNHEALTHY.discard((cluster_id, namespace, name))


def reset() -> None:
    with _LOCK:
        _STATE.clear()
        _STATE.update(_SEED)
