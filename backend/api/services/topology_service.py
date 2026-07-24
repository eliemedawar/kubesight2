"""Automatic cluster topology assembly.

Turns already-collected cluster inventory into the ``{nodes, edges}`` graph the
shared :mod:`TopologyViewer` renders. Nothing here queries Kubernetes directly —
the routes fetch inventory (via ``k8s_provider`` in real mode, or the mock data
dicts) and hand it to these pure builders, so the same functions serve both
modes and stay easy to unit-test.

Two levels, matching the drill-down flow:
  * Level 1 (:func:`build_cluster_topology`) — the cluster fanning out to its
    namespaces (logical) and worker nodes (physical), via two group hubs.
  * Level 2 (:func:`build_namespace_topology`) — one namespace's pods, with
    ``Ingress → Service → pod`` edges (flat pods).

Node ``componentStatus`` uses the same vocabulary as the viewer
(healthy / degraded / unhealthy / unknown); namespace nodes additionally carry
``kind: "namespace"`` + ``namespace`` so the client knows they drill in.
"""

from typing import Any, Dict, List, Optional

from ..k8s_provider import is_failed_pod_status, is_issue_pod_status

HEALTHY = "healthy"
DEGRADED = "degraded"
UNHEALTHY = "unhealthy"
UNKNOWN = "unknown"

# Lower = worse; used to fold several child statuses into one summary status.
_SEVERITY = {UNHEALTHY: 0, DEGRADED: 1, HEALTHY: 2, UNKNOWN: 3}


def _worst(statuses: List[Optional[str]]) -> str:
    present = [s for s in statuses if s]
    if not present:
        return HEALTHY
    return min(present, key=lambda s: _SEVERITY.get(s, 3))


def _description(*parts: Any) -> str:
    """Join compact node metadata for the viewer tooltip."""
    return " · ".join(str(part).strip() for part in parts if part not in (None, ""))


def _node_component_status(node: Dict[str, Any]) -> str:
    """Map a node row (``node_health_from_k8s`` or the simple Ready/NotReady
    shape) onto the viewer's status vocabulary."""
    raw = str(node.get("status") or "").strip().lower()
    if raw in ("ready", "healthy"):
        return HEALTHY
    if raw == "warning":
        return DEGRADED
    if raw in ("notready", "not ready", "critical"):
        return UNHEALTHY
    if node.get("ready") is True:
        return HEALTHY
    if node.get("ready") is False:
        return UNHEALTHY
    return UNKNOWN


def _node_role(node: Dict[str, Any]) -> str:
    roles = node.get("roles")
    if isinstance(roles, list) and roles:
        return ", ".join(str(r) for r in roles)
    return "Node"


def _pod_component_status(status: Any) -> str:
    text = str(status or "").strip().lower()
    if text in ("running", "completed", "succeeded"):
        return HEALTHY
    if is_failed_pod_status(text):
        return UNHEALTHY
    if is_issue_pod_status(status):
        return DEGRADED
    return HEALTHY


def build_cluster_topology(
    cluster_id: str,
    cluster_name: Optional[str],
    namespaces: List[Dict[str, Any]],
    nodes: List[Dict[str, Any]],
    issue_pods: Optional[List[Dict[str, Any]]] = None,
) -> Dict[str, List[Dict[str, Any]]]:
    """Level 1: cluster → (Namespaces hub → namespaces) + (Nodes hub → nodes).

    ``issue_pods`` (from ``cluster_pod_issues_from_k8s``) colours each namespace
    by the worst problem pod it contains; absent that, the namespace's own
    ``status`` field is used.
    """
    issue_pods = issue_pods or []

    # Worst problem-pod status per namespace (for the namespace node colour).
    ns_health: Dict[str, str] = {}
    for pod in issue_pods:
        ns = pod.get("namespace")
        if not ns:
            continue
        ns_health[ns] = _worst([ns_health.get(ns), _pod_component_status(pod.get("status"))])

    graph_nodes: List[Dict[str, Any]] = []
    graph_edges: List[Dict[str, Any]] = []

    node_statuses = [_node_component_status(n) for n in nodes if n.get("name")]
    root_id = "cluster"
    graph_nodes.append(
        {
            "id": root_id,
            "name": cluster_name or cluster_id,
            "type": "Cluster",
            "kind": "cluster",
            "componentStatus": _worst(node_statuses) if node_statuses else HEALTHY,
        }
    )

    valid_namespaces = [ns for ns in namespaces if ns.get("name")]
    if valid_namespaces:
        ns_hub = "group:namespaces"
        ns_statuses: List[str] = []
        ns_entries: List[Dict[str, Any]] = []
        for ns in valid_namespaces:
            name = ns["name"]
            health = ns_health.get(name)
            if not health:
                status_raw = str(ns.get("status") or "").strip().lower()
                health = HEALTHY if status_raw in ("active", "") else DEGRADED
            ns_statuses.append(health)
            pod_count = ns.get("pods")
            sub = f"{pod_count} pods" if isinstance(pod_count, int) else "Namespace"
            nid = f"ns:{name}"
            ns_entries.append(
                {
                    "id": nid,
                    "name": name,
                    "type": sub,
                    "kind": "namespace",
                    "namespace": name,
                    "componentStatus": health,
                    "description": _description(name, sub, health.title()),
                }
            )
            graph_edges.append(
                {
                    "id": f"e:{ns_hub}->{nid}",
                    "sourceNodeId": ns_hub,
                    "targetNodeId": nid,
                    "kind": "contains",
                }
            )
        graph_nodes.append(
            {
                "id": ns_hub,
                "name": "Namespaces",
                "type": f"{len(valid_namespaces)} logical",
                "kind": "group",
                "componentStatus": _worst(ns_statuses),
            }
        )
        graph_edges.append(
            {
                "id": f"e:cluster->{ns_hub}",
                "sourceNodeId": root_id,
                "targetNodeId": ns_hub,
                "kind": "contains",
            }
        )
        graph_nodes.extend(ns_entries)

    valid_nodes = [n for n in nodes if n.get("name")]
    if valid_nodes:
        node_hub = "group:nodes"
        hub_statuses: List[str] = []
        node_entries: List[Dict[str, Any]] = []
        for n in valid_nodes:
            name = n["name"]
            status = _node_component_status(n)
            hub_statuses.append(status)
            nid = f"node:{name}"
            node_entries.append(
                {
                    "id": nid,
                    "name": name,
                    "type": _node_role(n),
                    "kind": "node",
                    "componentStatus": status,
                    "description": _description(name, _node_role(n), status.title()),
                }
            )
            graph_edges.append(
                {
                    "id": f"e:{node_hub}->{nid}",
                    "sourceNodeId": node_hub,
                    "targetNodeId": nid,
                    "kind": "contains",
                }
            )
        graph_nodes.append(
            {
                "id": node_hub,
                "name": "Nodes",
                "type": f"{len(valid_nodes)} physical",
                "kind": "group",
                "componentStatus": _worst(hub_statuses),
            }
        )
        graph_edges.append(
            {
                "id": f"e:cluster->{node_hub}",
                "sourceNodeId": root_id,
                "targetNodeId": node_hub,
                "kind": "contains",
            }
        )
        graph_nodes.extend(node_entries)

    # A cluster's summary must include workload health as well as physical node
    # health. Otherwise a cluster with healthy nodes and crashing pods appears
    # green at the root.
    root_statuses = list(node_statuses)
    if valid_namespaces:
        root_statuses.extend(ns_statuses)
    graph_nodes[0]["componentStatus"] = _worst(root_statuses) if root_statuses else UNKNOWN
    graph_nodes[0]["description"] = _description(
        cluster_name or cluster_id,
        f"{len(valid_namespaces)} namespaces",
        f"{len(valid_nodes)} nodes",
    )

    return {"nodes": graph_nodes, "edges": graph_edges}


def _service_ports_label(service: Dict[str, Any]) -> Optional[str]:
    ports = service.get("ports")
    if isinstance(ports, list):
        label = "/".join(str(p) for p in ports if p not in (None, ""))
        return label or None
    if isinstance(ports, str):
        return ports.strip() or None
    return None


def _service_target_pod_names(service: Dict[str, Any], pods: List[Dict[str, Any]]) -> List[str]:
    """Pod names a service routes to.

    Real mode resolves this from live Endpoints (``targetPods`` — a comma-joined
    string of pod names, or ``-``/an IP/externalName when there are none). When
    that isn't available (mock data, headless services), fall back to matching
    pods whose name is the service name or begins with ``<service>-``.
    """
    pod_names = {str(pod.get("name")) for pod in pods if pod.get("name")}
    raw = service.get("targetPods")
    if isinstance(raw, str) and raw.strip() and raw.strip() != "-":
        # Endpoints can contain external IPs as well as Pod targetRefs. Only
        # return targets that exist in the visible pod inventory.
        return [
            target
            for target in (t.strip() for t in raw.split(","))
            if target and target in pod_names
        ]

    selector = service.get("selector")
    if isinstance(selector, dict) and selector:
        selected = []
        for pod in pods:
            labels = pod.get("labels")
            if not isinstance(labels, dict):
                continue
            if all(labels.get(key) == value for key, value in selector.items()):
                if pod.get("name"):
                    selected.append(str(pod["name"]))
        if selected:
            return selected

    name = service.get("name") or ""
    if not name:
        return []
    matches: List[str] = []
    for pod in pods:
        pod_name = pod.get("name") or ""
        if pod_name == name or pod_name.startswith(f"{name}-"):
            matches.append(pod_name)
    return matches


def _edge(
    source: str,
    target: str,
    *,
    protocol: Optional[str] = None,
    scope: Optional[str] = None,
    kind: Optional[str] = None,
) -> Dict[str, Any]:
    result: Dict[str, Any] = {
        "id": f"e:{source}->{target}",
        "sourceNodeId": source,
        "targetNodeId": target,
    }
    if protocol:
        result["protocol"] = protocol
    if scope:
        result["scope"] = scope
    if kind:
        result["kind"] = kind
    return result


def build_namespace_topology(
    namespace: str,
    pods: List[Dict[str, Any]],
    services: List[Dict[str, Any]],
    ingresses: List[Dict[str, Any]],
) -> Dict[str, List[Dict[str, Any]]]:
    """Level 2: real ``Ingress → Service → Pod`` traffic relationships.

    The namespace is context, not a traffic source, so it is intentionally not
    synthesized as a graph node. Internal services become component roots and
    unattached pods remain visible as isolated resources without a misleading
    namespace-to-resource fan-out.
    """
    graph_nodes: List[Dict[str, Any]] = []
    graph_edges: List[Dict[str, Any]] = []

    pod_ids: Dict[str, str] = {}
    pod_statuses: Dict[str, str] = {}
    for pod in pods:
        name = pod.get("name")
        if not name:
            continue
        pid = f"pod:{name}"
        pod_ids[name] = pid
        pod_statuses[name] = _pod_component_status(pod.get("status"))
        graph_nodes.append(
            {
                "id": pid,
                "name": name,
                "type": pod.get("status") or "Pod",
                "kind": "pod",
                "namespace": namespace,
                "componentStatus": pod_statuses[name],
                "description": _description(
                    name,
                    pod.get("status") or "Pod",
                    pod.get("node"),
                    pod.get("ready"),
                ),
            }
        )

    svc_ids: Dict[str, str] = {}
    svc_statuses: Dict[str, str] = {}
    for svc in services:
        name = svc.get("name")
        if not name:
            continue
        sid = f"svc:{name}"
        svc_ids[name] = sid
        targets = _service_target_pod_names(svc, pods)
        target_statuses = [pod_statuses[target] for target in targets if target in pod_statuses]
        svc_status = _worst(target_statuses) if target_statuses else UNKNOWN
        svc_statuses[name] = svc_status
        port_label = _service_ports_label(svc)
        graph_nodes.append(
            {
                "id": sid,
                "name": name,
                "type": svc.get("type") or "Service",
                "kind": "service",
                "namespace": namespace,
                "componentStatus": svc_status,
                "description": _description(
                    name,
                    svc.get("type") or "Service",
                    f"ports {port_label}" if port_label else None,
                    f"{len(targets)} endpoints",
                ),
            }
        )
        for target in targets:
            tid = pod_ids.get(target)
            if not tid:
                continue
            graph_edges.append(_edge(sid, tid, protocol=port_label, kind="routes"))

    for ing in ingresses:
        name = ing.get("name")
        if not name:
            continue
        iid = f"ing:{name}"
        backend = ing.get("backendService")
        ingress_status = svc_statuses.get(backend, UNKNOWN)
        graph_nodes.append(
            {
                "id": iid,
                "name": name,
                "type": ing.get("host") or "Ingress",
                "kind": "ingress",
                "namespace": namespace,
                "componentStatus": ingress_status,
                "description": _description(
                    name,
                    ing.get("host") or "Ingress",
                    ing.get("path"),
                    f"to {backend}" if backend else None,
                ),
            }
        )
        if backend and backend in svc_ids:
            graph_edges.append(
                _edge(iid, svc_ids[backend], scope="external", kind="routes")
            )

    return {"nodes": graph_nodes, "edges": graph_edges}
