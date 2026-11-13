"""Before an OKE cluster is destroyed: let go of what Kubernetes made in OCI.

A ``Service`` of type ``LoadBalancer`` makes Oracle's cloud controller create
an OCI load balancer. OpenTofu never knew about it, so destroying the cluster
would leave it behind in the compartment (and in the load-balancer subnet).
After the destroy is approved, and before OpenTofu runs, KubeSight deletes
those Services through its own access to the cluster and waits until the
cloud controller has removed their load balancers: the Service only goes once
its finalizer (``service.kubernetes.io/load-balancer-cleanup``) is released.

If the cluster cannot be reached, the destroy still goes ahead and the log
names what may be left in OCI.
"""

from __future__ import annotations

import json
import time
from typing import Any, Callable, Dict, List, Optional

_WAIT_S = 15 * 60
_POLL_S = 10.0
_runner: Optional[Callable[[str, List[str]], str]] = None


def set_runner(fn: Optional[Callable[[str, List[str]], str]]) -> None:
    """Test seam: ``fn(cluster_id, kubectl args) -> stdout``."""
    global _runner
    _runner = fn


def _kubectl(cluster_id: str, args: List[str]) -> str:
    if _runner is not None:
        return _runner(cluster_id, args)
    from ...deployment_service import _run_kubectl_for_cluster

    return _run_kubectl_for_cluster(cluster_id, args)


def load_balancer_services(cluster_id: str) -> List[Dict[str, str]]:
    raw = _kubectl(cluster_id, ["get", "services", "--all-namespaces", "-o", "json"])
    out = []
    for item in (json.loads(raw or "{}").get("items") or []):
        if (item.get("spec") or {}).get("type") != "LoadBalancer":
            continue
        meta = item.get("metadata") or {}
        ingress = ((item.get("status") or {}).get("loadBalancer") or {}).get("ingress") or []
        out.append({"namespace": meta.get("namespace") or "default", "name": meta.get("name") or "",
                    "address": (ingress[0] or {}).get("ip") or "" if ingress else ""})
    return out


def release_load_balancers(cluster_id: Any, on_line: Callable[[str], None], *,
                           wait_s: float = _WAIT_S, poll_s: float = _POLL_S,
                           sleep: Callable[[float], None] = time.sleep) -> Dict[str, Any]:
    """Delete every LoadBalancer Service and wait for OCI to remove its load
    balancer. Returns what was done; never raises (the destroy goes on)."""
    if not cluster_id:
        on_line("The cluster was never registered in KubeSight: if Services made OCI load balancers, "
                "delete them in OCI after the destroy.")
        return {"checked": False, "left": []}
    cid = str(cluster_id)
    try:
        services = load_balancer_services(cid)
    except Exception as exc:  # noqa: BLE001 — a dead cluster must not block its own destroy
        on_line(f"Could not list the cluster's LoadBalancer Services ({str(exc)[:200]}). Any OCI load "
                "balancer they made stays after the destroy: delete it in OCI.")
        return {"checked": False, "left": []}
    if not services:
        on_line("No LoadBalancer Services in the cluster: no OCI load balancers to remove first.")
        return {"checked": True, "deleted": [], "left": []}
    names = [f"{s['namespace']}/{s['name']}" for s in services]
    on_line(f"Removing the cluster's {len(services)} LoadBalancer Service(s) first, so OCI deletes their "
            f"load balancers: {', '.join(names)}.")
    refused = []
    for svc in services:
        try:
            _kubectl(cid, ["delete", "service", svc["name"], "-n", svc["namespace"], "--wait=false"])
        except Exception as exc:  # noqa: BLE001
            refused.append(f"{svc['namespace']}/{svc['name']}: {str(exc)[:160]}")
    for line in refused:
        on_line(f"Could not delete {line}")
    waited = 0.0
    left = names
    while waited <= wait_s:
        try:
            now = {f"{s['namespace']}/{s['name']}" for s in load_balancer_services(cid)}
        except Exception:  # noqa: BLE001
            now = set(left)
        left = [name for name in names if name in now]
        if not left:
            on_line("OCI removed the load balancers. Destroying the cluster now.")
            return {"checked": True, "deleted": names, "left": []}
        sleep(poll_s)
        waited += poll_s
    on_line(f"After {int(wait_s // 60)} minutes these Services still wait for OCI to remove their load "
            f"balancers: {', '.join(left)}. The destroy goes on; check OCI for load balancers left behind.")
    return {"checked": True, "deleted": [n for n in names if n not in left], "left": left}
