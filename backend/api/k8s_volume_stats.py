"""Real node-filesystem and PVC usage from the kubelet Summary API.

The API server proxies each node's kubelet ``/stats/summary`` endpoint:

    kubectl get --raw /api/v1/nodes/<node>/proxy/stats/summary

That payload carries ``node.fs`` (the node's root filesystem) and, per pod,
``volume[]`` entries whose ``pvcRef`` names the claim they back. Both expose
``usedBytes`` / ``capacityBytes``, which is all the alert evaluator needs for
``disk_usage_percent`` and ``pvc_usage_percent``.

Design rules:

* One fetch per cluster per evaluator tick — results are cached for a short TTL
  (shorter than the scheduler tick) keyed on the kubeconfig/context, so every
  disk/PVC policy on a cluster shares one sweep.
* Nodes are fetched in parallel, each with its own bounded kubectl timeout, so a
  single slow kubelet cannot stall the tick.
* Unknown is never zero. A node or claim whose stats could not be read is simply
  absent from the result; callers must treat absence as "no data" and not fire.
  ``available`` is False when no node answered at all (missing ``nodes/proxy``
  permission, kubelet unreachable, ...), with ``reason`` explaining why.

kubectl invocation goes through ``k8s_provider._run_for_access`` so kubeconfig
and context handling stay in one place.
"""

from __future__ import annotations

import json
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Dict, List, Optional, Tuple

from .k8s_provider import K8sCommandError, _run_for_access

DEFAULT_CACHE_SECONDS = 10
DEFAULT_NODE_TIMEOUT_SECONDS = 8
DEFAULT_MAX_WORKERS = 8

_cache: Dict[str, Tuple[float, Dict[str, Any]]] = {}
_cache_lock = threading.Lock()


def _env_int(name: str, default: int, minimum: int) -> int:
    try:
        return max(minimum, int(os.getenv(name, str(default)).strip()))
    except (TypeError, ValueError):
        return default


def _cache_seconds() -> int:
    return _env_int("KUBELET_STATS_CACHE_SECONDS", DEFAULT_CACHE_SECONDS, 0)


def _node_timeout_seconds() -> int:
    return _env_int("KUBELET_STATS_TIMEOUT_SECONDS", DEFAULT_NODE_TIMEOUT_SECONDS, 1)


def _max_workers() -> int:
    return _env_int("KUBELET_STATS_MAX_WORKERS", DEFAULT_MAX_WORKERS, 1)


def _cache_key(access) -> str:
    return f"{getattr(access, 'context_name', '')}:{getattr(access, 'kubeconfig_path', '')}"


def clear_cache() -> None:
    with _cache_lock:
        _cache.clear()


def _usage(entry: Any) -> Optional[Dict[str, Any]]:
    """Return {usedBytes, capacityBytes, percent} or None when not measurable."""
    if not isinstance(entry, dict):
        return None
    used = entry.get("usedBytes")
    capacity = entry.get("capacityBytes")
    if used is None or not capacity:
        return None
    try:
        used_f = float(used)
        capacity_f = float(capacity)
    except (TypeError, ValueError):
        return None
    if capacity_f <= 0:
        return None
    return {
        "usedBytes": int(used_f),
        "capacityBytes": int(capacity_f),
        "percent": round(min(100.0, max(0.0, used_f / capacity_f * 100.0)), 2),
    }


def parse_summary(node_name: str, summary: Dict[str, Any]) -> Dict[str, Any]:
    """Extract node fs usage and per-PVC usage from one kubelet summary."""
    node_fs = _usage((summary.get("node") or {}).get("fs"))
    pvcs: Dict[Tuple[str, str], Dict[str, Any]] = {}
    for pod in summary.get("pods") or []:
        pod_ref = pod.get("podRef") or {}
        for volume in pod.get("volume") or []:
            pvc_ref = volume.get("pvcRef")
            if not isinstance(pvc_ref, dict) or not pvc_ref.get("name"):
                continue
            usage = _usage(volume)
            if not usage:
                continue
            key = (str(pvc_ref.get("namespace") or pod_ref.get("namespace") or ""), str(pvc_ref["name"]))
            usage = {**usage, "node": node_name, "pod": pod_ref.get("name")}
            # RWX claims mounted on several nodes report once per mount; keep
            # the fullest reading.
            current = pvcs.get(key)
            if current is None or usage["percent"] > current["percent"]:
                pvcs[key] = usage
    return {"nodeFs": node_fs, "pvcs": pvcs}


def ready_node_names(node_items: List[Dict[str, Any]]) -> List[str]:
    """Names of Ready nodes only.

    A NotReady node's kubelet cannot answer, and proxying to it fails with a
    network timeout that the shared kubectl circuit breaker would read as the
    whole cluster API being down — so those nodes are never probed.
    """
    names: List[str] = []
    for node in node_items or []:
        name = (node.get("metadata") or {}).get("name")
        ready = any(
            c.get("type") == "Ready" and c.get("status") == "True"
            for c in (node.get("status") or {}).get("conditions") or []
        )
        if name and ready:
            names.append(name)
    return names


def _list_node_names(access) -> List[str]:
    output = _run_for_access(access, ["get", "nodes", "-o", "json"])
    return ready_node_names(json.loads(output or "{}").get("items") or [])


def _fetch_node_summary(access, node_name: str) -> Dict[str, Any]:
    output = _run_for_access(
        access,
        ["get", "--raw", f"/api/v1/nodes/{node_name}/proxy/stats/summary"],
        timeout=_node_timeout_seconds(),
    )
    return json.loads(output or "{}")


def fetch_cluster_volume_stats(
    access,
    node_names: Optional[List[str]] = None,
    *,
    use_cache: bool = True,
) -> Dict[str, Any]:
    """Collect node-fs and PVC usage across the cluster.

    Returns::

        {
          "available": bool,            # at least one node's summary was read
          "reason": str | None,         # why nothing is available
          "nodes": {node: usage},       # node.fs usage per node that answered
          "pvcs": {(ns, name): usage},  # per claim, from pod volume stats
          "errors": {node: message},    # nodes whose summary could not be read
        }
    """
    key = _cache_key(access)
    ttl = _cache_seconds()
    if use_cache and ttl > 0:
        with _cache_lock:
            entry = _cache.get(key)
        if entry and entry[0] > time.time():
            return entry[1]

    result: Dict[str, Any] = {"available": False, "reason": None, "nodes": {}, "pvcs": {}, "errors": {}}

    if node_names is None:
        try:
            node_names = _list_node_names(access)
        except (K8sCommandError, ValueError) as exc:
            result["reason"] = f"Could not list nodes: {exc}"
            return result
    node_names = [name for name in node_names if name]
    if not node_names:
        result["reason"] = "No Ready nodes to read kubelet stats from."
        return result

    def _one(name: str) -> Tuple[str, Optional[Dict[str, Any]], Optional[str]]:
        try:
            return name, parse_summary(name, _fetch_node_summary(access, name)), None
        except K8sCommandError as exc:
            return name, None, str(exc)
        except (ValueError, TypeError) as exc:
            return name, None, f"Invalid kubelet summary: {exc}"

    workers = min(_max_workers(), len(node_names))
    with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="kubelet-stats") as pool:
        outcomes = list(pool.map(_one, node_names))

    for name, parsed, error in outcomes:
        if error is not None or parsed is None:
            result["errors"][name] = error or "unknown error"
            continue
        result["available"] = True
        if parsed["nodeFs"]:
            result["nodes"][name] = parsed["nodeFs"]
        for pvc_key, usage in parsed["pvcs"].items():
            current = result["pvcs"].get(pvc_key)
            if current is None or usage["percent"] > current["percent"]:
                result["pvcs"][pvc_key] = usage

    if not result["available"]:
        sample = next(iter(result["errors"].values()), "")
        hint = ""
        lowered = sample.lower()
        if "forbidden" in lowered or "nodes/proxy" in lowered:
            hint = " (the kubeconfig needs get on nodes/proxy)"
        result["reason"] = f"Kubelet Summary API unavailable on every node{hint}: {sample}".strip()

    if ttl > 0:
        with _cache_lock:
            _cache[key] = (time.time() + ttl, result)
    return result
