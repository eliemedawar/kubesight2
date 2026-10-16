"""Where an installed app answers: its Ingress hosts and the address behind them.

A ticket's requester wants to open the app, so the follow-up Hermes writes from
carries each Ingress host (the DNS name to create) and the IP it must point at.

The address is the Ingress status when the controller publishes one. The
Cluster Builder's NGINX Ingress is a NodePort service and publishes none, so
then it is the controller service's LoadBalancer or external IPs, or else the
workers' addresses with the NodePort.

Reading never fails a request: anything that goes wrong comes back as a
sentence next to whatever could be read.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Dict, Iterable, List, Optional, Tuple

logger = logging.getLogger(__name__)

CONTROLLER_NAMESPACE = "nginx-ingress"
CONTROLLER_SERVICE = "nginx-ingress"
_CONTROL_PLANE_LABELS = ("node-role.kubernetes.io/control-plane", "node-role.kubernetes.io/master")


def _get(access, args: List[str]) -> Dict[str, Any]:
    from ...k8s_provider import _run_for_access

    return json.loads(_run_for_access(access, args, timeout=30) or "{}")


def _status_addresses(obj: Dict[str, Any]) -> List[str]:
    entries = ((obj.get("status") or {}).get("loadBalancer") or {}).get("ingress") or []
    return [e.get("ip") or e.get("hostname") for e in entries if e.get("ip") or e.get("hostname")]


def _node_addresses(access) -> List[str]:
    """Ready workers' InternalIPs; every ready node when there are no workers."""
    nodes = _get(access, ["get", "nodes", "-o", "json"]).get("items") or []

    def ready(node):
        return any(c.get("type") == "Ready" and c.get("status") == "True"
                   for c in (node.get("status") or {}).get("conditions") or [])

    def address(node):
        found = {a.get("type"): a.get("address") for a in (node.get("status") or {}).get("addresses") or []}
        return found.get("InternalIP") or found.get("ExternalIP")

    live = [n for n in nodes if ready(n) and address(n)]
    workers = [n for n in live
               if not any(label in ((n.get("metadata") or {}).get("labels") or {}) for label in _CONTROL_PLANE_LABELS)]
    return [address(n) for n in (workers or live)]


def controller_entry(access) -> Optional[Dict[str, Any]]:
    """How traffic reaches the NGINX Ingress controller, or None without one."""
    from ...k8s_provider import K8sCommandError

    try:
        service = _get(access, ["-n", CONTROLLER_NAMESPACE, "get", "service", CONTROLLER_SERVICE, "-o", "json"])
    except K8sCommandError:
        return None
    spec = service.get("spec") or {}
    ports = {p.get("name"): p for p in spec.get("ports") or []}
    balanced = _status_addresses(service)
    if spec.get("type") == "LoadBalancer" and balanced:
        return {"via": "loadBalancer", "addresses": balanced, "httpPort": None, "httpsPort": None}
    if spec.get("externalIPs"):
        return {"via": "externalIP", "addresses": list(spec["externalIPs"]), "httpPort": None, "httpsPort": None}
    if spec.get("type") in ("NodePort", "LoadBalancer"):
        return {
            "via": "nodePort",
            "addresses": _node_addresses(access),
            "httpPort": (ports.get("http") or {}).get("nodePort"),
            "httpsPort": (ports.get("https") or {}).get("nodePort"),
        }
    return None


def _urls(hosts: List[str], tls_hosts: set, path: str, addresses: List[str],
          http_port: Optional[int], https_port: Optional[int]) -> List[str]:
    urls = []
    for host in hosts or addresses[:1]:
        secure = host in tls_hosts
        port = https_port if secure else http_port
        suffix = f":{port}" if port else ""
        urls.append(f"{'https' if secure else 'http'}://{host}{suffix}{path}")
    return urls


def release_ingresses(cluster_id: str, releases: Iterable[Tuple[str, str]]) -> Tuple[List[Dict[str, Any]], Optional[str]]:
    """Every Ingress the given (release, namespace) pairs installed, with its address.

    Returns (ingresses, problem). An app without an Ingress adds nothing.
    """
    from ...k8s_provider import resolve_cluster_access

    releases = [(r, ns) for r, ns in releases if r and ns]
    if not releases or not cluster_id:
        return [], None
    try:
        access = resolve_cluster_access(str(cluster_id))
        if access is None:
            return [], f"KubeSight cannot reach cluster {cluster_id} to read its Ingresses."
        entry: Optional[Dict[str, Any]] = None
        entry_read = False
        found: List[Dict[str, Any]] = []
        for namespace in sorted({ns for _, ns in releases}):
            wanted = {r for r, ns in releases if ns == namespace}
            for ing in _get(access, ["-n", namespace, "get", "ingress", "-o", "json"]).get("items") or []:
                meta = ing.get("metadata") or {}
                notes = meta.get("annotations") or {}
                release = notes.get("meta.helm.sh/release-name")
                if release not in wanted or notes.get("meta.helm.sh/release-namespace", namespace) != namespace:
                    continue
                spec = ing.get("spec") or {}
                rules = spec.get("rules") or []
                hosts = [r["host"] for r in rules if r.get("host")]
                tls_hosts = {h for t in spec.get("tls") or [] for h in t.get("hosts") or []}
                paths = [p.get("path") for r in rules for p in ((r.get("http") or {}).get("paths") or [])]
                path = next((p for p in paths if p), "/")
                addresses = _status_addresses(ing)
                via, http_port, https_port = "ingress", None, None
                if not addresses:
                    if not entry_read:
                        entry, entry_read = controller_entry(access), True
                    if entry:
                        addresses = entry["addresses"]
                        via, http_port, https_port = entry["via"], entry["httpPort"], entry["httpsPort"]
                found.append({
                    "release": release,
                    "namespace": namespace,
                    "name": meta.get("name"),
                    "className": spec.get("ingressClassName"),
                    "hosts": hosts,
                    "addresses": addresses,
                    "via": via,
                    "httpPort": http_port,
                    "httpsPort": https_port,
                    "urls": _urls(hosts, tls_hosts, path, addresses, http_port, https_port),
                })
        return found, None
    except Exception as exc:  # never let reading addresses fail a finished request
        logger.warning("Reading Ingresses on %s failed: %s", cluster_id, exc)
        return [], f"Could not read the Ingresses on cluster {cluster_id}: {str(exc)[:300]}"


def lines(ingresses: List[Dict[str, Any]]) -> List[str]:
    """Plain-language lines for a ticket comment: DNS name → IP, and the URL."""
    out = []
    for ing in ingresses:
        ips = ", ".join(ing.get("addresses") or []) or "no address yet"
        head = f"{ing['release']} ({ing['namespace']}), Ingress {ing['name']}"
        if ing.get("hosts"):
            out.append(f"{head}: point DNS {', '.join(ing['hosts'])} at {ips}")
        else:
            out.append(f"{head}: no host name, answers on {ips}")
        if ing.get("urls"):
            out.append(f"  open {', '.join(ing['urls'])}")
        if ing.get("via") == "nodePort":
            out.append("  (the ingress controller listens on a NodePort, so the URL carries that port)")
    return out
