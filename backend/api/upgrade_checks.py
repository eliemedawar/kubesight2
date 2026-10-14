"""Upgrade readiness checks.

Everything the checks need is read from the cluster ONCE and in parallel: eight
``kubectl get`` calls that used to run one after another (with ``kube-system``
pods read twice and every pod read a third time). The wall time of a precheck
is now its slowest read instead of the sum of all of them, and ``kubectl top``
— which waits out the whole request timeout on a cluster without
metrics-server — is gone: the metrics API's APIService answers the same
question from a read we already make.

A check is ``{key, name, group, status, details, fix?, objects?}``. ``status``
is ``passed``, ``warning`` or ``failed``; a failed check blocks the upgrade
unless an admin waives it (see ``services.upgrade_center``).
"""

from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable, Dict, List, Optional

GROUPS = [
    {"key": "workloads", "label": "Draining and workloads"},
    {"key": "health", "label": "Cluster health"},
    {"key": "versions", "label": "Versions and APIs"},
    {"key": "storage", "label": "Storage"},
]

READS: Dict[str, List[str]] = {
    "version": ["version", "-o", "json"],
    "nodes": ["get", "nodes", "-o", "json"],
    "pods": ["get", "pods", "--all-namespaces", "-o", "json"],
    "pvc": ["get", "pvc", "--all-namespaces", "-o", "json"],
    "storageclass": ["get", "storageclass", "-o", "json"],
    "pdb": ["get", "pdb", "--all-namespaces", "-o", "json"],
    "apiservices": ["get", "apiservices", "-o", "json"],
    "workloads": ["get", "deployments,statefulsets", "--all-namespaces", "-o", "json"],
}

_MAX_OBJECTS = 20
METRICS_APISERVICE = "v1beta1.metrics.k8s.io"


def check(
    key: str,
    name: str,
    group: str,
    status: str,
    details: str = "",
    *,
    fix: Optional[str] = None,
    objects: Optional[List[str]] = None,
) -> Dict[str, Any]:
    entry: Dict[str, Any] = {"key": key, "name": name, "group": group, "status": status}
    if details:
        entry["details"] = details
    if fix:
        entry["fix"] = fix
    if objects:
        entry["objects"] = list(objects)[:_MAX_OBJECTS]
        entry["objectCount"] = len(objects)
    return entry


def summarize(checks: List[Dict[str, Any]]) -> Dict[str, int]:
    out = {"passed": 0, "warning": 0, "failed": 0}
    for item in checks:
        status = item.get("status")
        if status in out:
            out[status] += 1
    return out


def fetch_reads(access, run_kubectl: Callable, names: Optional[List[str]] = None) -> Dict[str, Any]:
    """Run the reads in parallel. Each value is the parsed JSON or the Exception."""
    wanted = {name: READS[name] for name in (names or READS)}

    def _one(args: List[str]) -> Any:
        return json.loads(run_kubectl(access, list(args)))

    results: Dict[str, Any] = {}
    with ThreadPoolExecutor(max_workers=len(wanted), thread_name_prefix="upgrade-precheck") as pool:
        futures = {name: pool.submit(_one, args) for name, args in wanted.items()}
        for name, future in futures.items():
            try:
                results[name] = future.result()
            except Exception as exc:  # noqa: BLE001 — each read fails on its own
                results[name] = exc
    return results


def _items(value: Any) -> List[Dict[str, Any]]:
    return value.get("items", []) if isinstance(value, dict) else []


def _ref(obj: Dict[str, Any]) -> str:
    meta = obj.get("metadata", {}) or {}
    ns = meta.get("namespace")
    return f"{ns}/{meta.get('name')}" if ns else str(meta.get("name"))


def _join(names: List[str], limit: int = 5) -> str:
    shown = ", ".join(names[:limit])
    return f"{shown} and {len(names) - limit} more" if len(names) > limit else shown


def _plural(n: int, word: str, many: Optional[str] = None) -> str:
    return f"{n} {word if n == 1 else (many or word + 's')}"


def node_ready(node: Dict[str, Any]) -> bool:
    return any(
        c.get("type") == "Ready" and c.get("status") == "True"
        for c in node.get("status", {}).get("conditions", []) or []
    )


def pod_is_movable(pod: Dict[str, Any]) -> bool:
    """A pod a drain evicts: not a DaemonSet pod, not a static (mirror) pod, not finished."""
    meta = pod.get("metadata", {}) or {}
    if "kubernetes.io/config.mirror" in (meta.get("annotations") or {}):
        return False
    if any(ref.get("kind") == "DaemonSet" for ref in meta.get("ownerReferences") or []):
        return False
    return pod.get("status", {}).get("phase") not in ("Succeeded", "Failed")


def pod_ready(pod: Dict[str, Any]) -> bool:
    if pod.get("status", {}).get("phase") != "Running":
        return False
    statuses = pod.get("status", {}).get("containerStatuses") or []
    return bool(statuses) and all(c.get("ready") for c in statuses)


def workload_baseline(pods: List[Dict[str, Any]]) -> Dict[str, Any]:
    """What "healthy" looked like before the upgrade: the run compares against it."""
    by_node: Dict[str, int] = {}
    for pod in pods:
        if pod_is_movable(pod):
            node = pod.get("spec", {}).get("nodeName")
            if node:
                by_node[node] = by_node.get(node, 0) + 1
    return {
        "readyPods": sum(1 for p in pods if pod_ready(p)),
        "totalPods": sum(1 for p in pods if p.get("status", {}).get("phase") not in ("Succeeded", "Failed")),
        "podsByNode": by_node,
    }


def _health_checks(reads: Dict[str, Any], node_items: List[Dict[str, Any]], pods: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    nodes_read = reads.get("nodes")
    if isinstance(nodes_read, Exception):
        out.append(check("nodes_ready", "Nodes ready", "health", "failed", str(nodes_read)))
    else:
        not_ready = [n.get("metadata", {}).get("name", "unknown") for n in node_items if not node_ready(n)]
        if not_ready:
            out.append(check(
                "nodes_ready", "Nodes ready", "health", "failed",
                f"Not Ready: {_join(not_ready)}.",
                fix="Bring every node back to Ready before upgrading; a NotReady node cannot be drained or upgraded.",
                objects=not_ready,
            ))
        else:
            out.append(check("nodes_ready", "Nodes ready", "health", "passed", f"All {_plural(len(node_items), 'node')} Ready."))

    pods_read = reads.get("pods")
    if isinstance(pods_read, Exception):
        out.append(check("control_plane", "Control plane pods", "health", "warning", f"Could not list pods: {pods_read}"))
        out.append(check("kube_system", "kube-system pods", "health", "warning", f"Could not list pods: {pods_read}"))
    else:
        kube_pods = [p for p in pods if p.get("metadata", {}).get("namespace") == "kube-system"]
        failed = [_ref(p) for p in kube_pods if p.get("status", {}).get("phase") == "Failed"]
        not_running = [_ref(p) for p in kube_pods if p.get("status", {}).get("phase") not in ("Running", "Succeeded")]
        if failed:
            out.append(check(
                "control_plane", "Control plane pods", "health", "failed",
                f"Failed in kube-system: {_join(failed)}.",
                fix="Fix or delete the failed kube-system pods; kubeadm refuses to upgrade an unhealthy control plane.",
                objects=failed,
            ))
        elif not_running:
            out.append(check("control_plane", "Control plane pods", "health", "warning",
                             f"Not running in kube-system: {_join(not_running)}.", objects=not_running))
        else:
            out.append(check("control_plane", "Control plane pods", "health", "passed", "kube-system has no failed pods."))
        bad = [_ref(p) for p in kube_pods if p.get("status", {}).get("phase") != "Succeeded" and not pod_ready(p)]
        if bad:
            out.append(check("kube_system", "kube-system pods", "health", "warning",
                             f"Not ready: {_join(bad)}.", objects=bad))
        else:
            out.append(check("kube_system", "kube-system pods", "health", "passed",
                             f"{_plural(len(kube_pods), 'kube-system pod')} ready."))

    pressured: List[str] = []
    for node in node_items:
        name = node.get("metadata", {}).get("name", "unknown")
        for condition in node.get("status", {}).get("conditions", []) or []:
            if condition.get("type") in ("MemoryPressure", "DiskPressure", "PIDPressure") and condition.get("status") == "True":
                pressured.append(f"{name} ({condition.get('type')})")
    if pressured:
        out.append(check(
            "node_pressure", "Node pressure", "health", "warning",
            f"Under pressure: {_join(pressured)}.",
            fix="Free disk or memory first: drained pods land on the remaining nodes.",
            objects=pressured,
        ))
    else:
        out.append(check("node_pressure", "Node pressure", "health", "passed", "No memory, disk or PID pressure."))
    return out


def _workload_checks(reads: Dict[str, Any], node_items: List[Dict[str, Any]], pods: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    multi_node = len(node_items) > 1

    pdb_read = reads.get("pdb")
    if isinstance(pdb_read, Exception):
        out.append(check("pdb", "Pod disruption budgets", "workloads", "warning", f"Could not list PDBs: {pdb_read}"))
    else:
        pdbs = _items(pdb_read)
        stuck = [
            _ref(p) for p in pdbs
            if (p.get("status", {}).get("expectedPods") or 0) > 0
            and (p.get("status", {}).get("disruptionsAllowed") or 0) == 0
        ]
        if stuck:
            out.append(check(
                "pdb", "Pod disruption budgets", "workloads", "failed" if multi_node else "warning",
                f"{_plural(len(stuck), 'PDB')} allow no disruptions right now: {_join(stuck)}. "
                "Draining a node that runs their pods waits until the drain times out, then stops the run.",
                fix="Scale those workloads up by one replica, or set maxUnavailable: 1 on their PDBs, then re-check.",
                objects=stuck,
            ))
        else:
            out.append(check("pdb", "Pod disruption budgets", "workloads", "passed",
                             f"{_plural(len(pdbs), 'PDB')}, each allowing at least one disruption." if pdbs else "No PDBs."))

    workloads_read = reads.get("workloads")
    if isinstance(workloads_read, Exception):
        out.append(check("single_replica", "Single-replica workloads", "workloads", "warning",
                         f"Could not list Deployments and StatefulSets: {workloads_read}"))
    else:
        single = [
            f"{w.get('kind', 'Workload')} {_ref(w)}" for w in _items(workloads_read)
            if w.get("metadata", {}).get("namespace") != "kube-system"
            and (w.get("spec", {}).get("replicas") if w.get("spec", {}).get("replicas") is not None else 1) == 1
        ]
        if single and multi_node:
            out.append(check(
                "single_replica", "Single-replica workloads", "workloads", "warning",
                f"{_plural(len(single), 'workload')} run one replica; each is briefly unavailable while its node drains.",
                objects=single,
            ))
        else:
            out.append(check("single_replica", "Single-replica workloads", "workloads", "passed",
                             "Every workload runs more than one replica." if not single else
                             "Single-node cluster: no node is drained."))

    if isinstance(reads.get("pods"), Exception):
        exc = reads.get("pods")
        for key, name in (("crashloop", "CrashLoopBackOff"), ("restarts", "Pod restarts"), ("pending", "Pending pods")):
            out.append(check(key, name, "workloads", "warning", f"Could not list pods: {exc}"))
        return out

    crash_loop: List[str] = []
    high_restart: List[str] = []
    pending: List[str] = []
    for pod in pods:
        ref = _ref(pod)
        if pod.get("status", {}).get("phase") == "Pending":
            pending.append(ref)
        for cs in pod.get("status", {}).get("containerStatuses", []) or []:
            if (cs.get("state", {}).get("waiting") or {}).get("reason") == "CrashLoopBackOff" and ref not in crash_loop:
                crash_loop.append(ref)
            if (cs.get("restartCount") or 0) >= 10 and ref not in high_restart:
                high_restart.append(ref)
    if crash_loop:
        out.append(check(
            "crashloop", "CrashLoopBackOff", "workloads", "warning",
            f"Crash-looping: {_join(crash_loop)}. Unrelated to the upgrade, but it hides whether the upgrade broke them.",
            objects=crash_loop,
        ))
    else:
        out.append(check("crashloop", "CrashLoopBackOff", "workloads", "passed", "No pods crash-looping."))
    if high_restart:
        out.append(check("restarts", "Pod restarts", "workloads", "warning",
                         f"10 or more restarts: {_join(high_restart)}.", objects=high_restart))
    else:
        out.append(check("restarts", "Pod restarts", "workloads", "passed", "No pod restarted 10 or more times."))
    if pending:
        out.append(check(
            "pending", "Pending pods", "workloads", "warning",
            f"Waiting for a node: {_join(pending)}. Drained pods may wait the same way.",
            objects=pending,
        ))
    else:
        out.append(check("pending", "Pending pods", "workloads", "passed", "No pods waiting for a node."))
    return out


def _storage_checks(reads: Dict[str, Any]) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    pvc_read = reads.get("pvc")
    if isinstance(pvc_read, Exception):
        out.append(check("pvc", "PVC health", "storage", "warning", f"Could not list PVCs: {pvc_read}"))
    else:
        pvcs = _items(pvc_read)
        lost = [_ref(p) for p in pvcs if p.get("status", {}).get("phase") == "Lost"]
        pending = [_ref(p) for p in pvcs if p.get("status", {}).get("phase") == "Pending"]
        if lost:
            out.append(check("pvc", "PVC health", "storage", "failed", f"Lost: {_join(lost)}.",
                             fix="Recover or delete the Lost claims; pods that use them cannot start anywhere.",
                             objects=lost))
        elif pending:
            out.append(check("pvc", "PVC health", "storage", "warning", f"Pending: {_join(pending)}.", objects=pending))
        else:
            out.append(check("pvc", "PVC health", "storage", "passed", f"{_plural(len(pvcs), 'PVC')}, none Lost or Pending."))

    sc_read = reads.get("storageclass")
    if isinstance(sc_read, Exception):
        out.append(check("default_storageclass", "Default StorageClass", "storage", "warning",
                         f"Could not list StorageClasses: {sc_read}"))
    else:
        classes = _items(sc_read)
        defaults = [
            c for c in classes
            if (c.get("metadata", {}).get("annotations") or {}).get("storageclass.kubernetes.io/is-default-class") == "true"
        ]
        if not classes:
            out.append(check("default_storageclass", "Default StorageClass", "storage", "warning",
                             "No StorageClasses. Fine if every volume is a pre-made PV."))
        elif not defaults:
            out.append(check("default_storageclass", "Default StorageClass", "storage", "warning",
                             f"{_plural(len(classes), 'StorageClass', 'StorageClasses')}, none marked default. "
                             "A claim that names no class stays Pending."))
        else:
            out.append(check("default_storageclass", "Default StorageClass", "storage", "passed",
                             f"Default: {defaults[0].get('metadata', {}).get('name')}."))
    return out


def _version_checks(
    reads: Dict[str, Any],
    node_items: List[Dict[str, Any]],
    current_version: str,
    target_version: str,
    provider: str,
) -> List[Dict[str, Any]]:
    from .upgrade_provider import (
        analyze_version_skew,
        kubeadm_minor_jump_blocked,
        normalize_version,
        parse_k8s_version,
        validate_target_version,
    )

    out: List[Dict[str, Any]] = []
    valid, error = validate_target_version(target_version)
    if not valid:
        out.append(check("target", "Target version", "versions", "failed", error or "Invalid target version.",
                         fix="Pick a target from the path."))
    else:
        jump = kubeadm_minor_jump_blocked(current_version, target_version) if provider == "kubeadm" else None
        target = normalize_version(target_version)
        if jump:
            out.append(check("target", "Target version", "versions", "failed", jump,
                             fix="Upgrade to the next minor first; each hop is its own run."))
        elif parse_k8s_version(current_version) >= parse_k8s_version(target_version):
            out.append(check("target", "Target version", "versions", "warning",
                             f"The cluster is already at or above {target} ({current_version})."))
        else:
            out.append(check("target", "Target version", "versions", "passed", f"{current_version} → {target}."))

    skew = analyze_version_skew(current_version, node_items)
    if skew["warnings"]:
        out.append(check("skew", "Version skew", "versions", "warning", "; ".join(skew["warnings"][:3])))
    else:
        out.append(check("skew", "Version skew", "versions", "passed",
                         f"All {_plural(len(node_items), 'kubelet')} on the control plane's version."))

    api_read = reads.get("apiservices")
    if isinstance(api_read, Exception):
        out.append(check("apiservices", "API services", "versions", "warning", f"Could not list APIServices: {api_read}"))
        out.append(check("metrics", "Metrics server", "versions", "warning", "Unknown: APIServices could not be listed."))
        return out
    services = _items(api_read)

    def _available(svc: Dict[str, Any]) -> bool:
        return any(
            c.get("type") == "Available" and c.get("status") == "True"
            for c in svc.get("status", {}).get("conditions", []) or []
        )

    unavailable = [s.get("metadata", {}).get("name") for s in services if not _available(s)]
    if unavailable:
        out.append(check(
            "apiservices", "API services", "versions", "warning",
            f"Unavailable: {_join(unavailable)}. Requests to these APIs fail while they are down.",
            fix="Fix or remove the backing service before upgrading.",
            objects=unavailable,
        ))
    else:
        out.append(check("apiservices", "API services", "versions", "passed",
                         f"All {_plural(len(services), 'APIService')} available."))
    metrics = next((s for s in services if s.get("metadata", {}).get("name") == METRICS_APISERVICE), None)
    if metrics is None:
        out.append(check("metrics", "Metrics server", "versions", "warning",
                         "metrics-server is not installed: node and pod usage won't show during the run."))
    elif not _available(metrics):
        out.append(check("metrics", "Metrics server", "versions", "warning", "metrics-server is installed but not serving."))
    else:
        out.append(check("metrics", "Metrics server", "versions", "passed", "metrics-server is serving."))
    return out


def run_checks(access, target_version: str, run_kubectl: Callable) -> Dict[str, Any]:
    """Read the cluster once, run every check. Returns the raw material
    ``upgrade_provider`` turns into the precheck payload."""
    from .upgrade_provider import detect_cluster_provider

    reads = fetch_reads(access, run_kubectl)
    version_read = reads.get("version")
    if isinstance(version_read, Exception):
        return {
            "checks": [check("api", "API reachable", "health", "failed", str(version_read),
                             fix="Check the cluster's kubeconfig and that the API server is up.")],
            "currentVersion": "unknown",
            "versionData": {},
            "nodeItems": [],
            "baseline": None,
        }
    current_version = version_read.get("serverVersion", {}).get("gitVersion") or "unknown"
    node_items = _items(reads.get("nodes"))
    pods = _items(reads.get("pods"))
    provider = detect_cluster_provider(access, run_kubectl, version_data=version_read, node_items=node_items)

    checks = [check("api", "API reachable", "health", "passed", f"API server answered ({current_version}).")]
    checks += _health_checks(reads, node_items, pods)
    checks += _workload_checks(reads, node_items, pods)
    checks += _version_checks(reads, node_items, current_version, target_version, provider)
    checks += _storage_checks(reads)
    return {
        "checks": checks,
        "currentVersion": current_version,
        "versionData": version_read,
        "nodeItems": node_items,
        "baseline": workload_baseline(pods) if not isinstance(reads.get("pods"), Exception) else None,
    }
