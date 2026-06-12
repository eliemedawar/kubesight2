from __future__ import annotations

import json
import re
import shutil
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional, Tuple

# ---------------------------------------------------------------------------
# Simple time-based cache for external K8s version lookups.
# These hit dl.k8s.io on every page load without caching — very slow.
# ---------------------------------------------------------------------------
_version_cache: Dict[str, Any] = {}
_version_cache_lock = threading.Lock()
_VERSION_CACHE_TTL = 300  # seconds — stable.txt changes at most a few times a year

from .cluster_access import ClusterAccess
from .upgrade_config import auto_upgrade_enabled

# Re-use kubectl helpers from k8s_provider without circular imports at module level.
RunKubectlFn = Callable[[ClusterAccess, List[str]], str]

VERSION_PATTERN = re.compile(r"^v?\d+\.\d+\.\d+([+-].+)?$", re.IGNORECASE)

PROVIDER_DISPLAY = {
    "docker-desktop": "Docker Desktop",
    "kind": "kind",
    "minikube": "Minikube",
    "kubeadm": "kubeadm",
    "eks": "EKS",
    "aks": "AKS",
    "gke": "GKE",
    "unknown": "Unknown",
}

UPGRADE_PLAN_STEPS = [
    "Validate cluster readiness",
    "Validate target version",
    "Drain worker nodes",
    "Upgrade control plane",
    "Upgrade worker nodes",
    "Validate workloads",
    "Post-upgrade verification",
]


def parse_k8s_version(version: str) -> Tuple[int, int, int]:
    """Parse v1.28.3 -> (1, 28, 3) for comparison."""
    if not version:
        return (0, 0, 0)
    cleaned = version.strip().lstrip("v").split("+")[0]
    parts = cleaned.split(".")
    nums: List[int] = []
    for part in parts[:3]:
        try:
            nums.append(int("".join(ch for ch in part if ch.isdigit()) or "0"))
        except ValueError:
            nums.append(0)
    while len(nums) < 3:
        nums.append(0)
    return tuple(nums[:3])  # type: ignore[return-value]


def normalize_version(version: str) -> str:
    if not version or version.lower() == "unknown":
        return "unknown"
    cleaned = version.strip()
    if not cleaned.startswith("v"):
        cleaned = f"v{cleaned}"
    return cleaned


def validate_target_version(target_version: str) -> Tuple[bool, Optional[str]]:
    if not target_version or not target_version.strip():
        return False, "Target version is required."
    normalized = normalize_version(target_version)
    if normalized == "unknown":
        return False, "Invalid target version."
    if not VERSION_PATTERN.match(normalized):
        return False, f"Target version must match semver format (e.g. v1.31.0), got: {target_version}"
    return True, None


def _node_ready(node: Dict[str, Any]) -> bool:
    return any(
        condition.get("type") == "Ready" and condition.get("status") == "True"
        for condition in node.get("status", {}).get("conditions", [])
    )


def _node_version(node: Dict[str, Any]) -> str:
    return (
        node.get("status", {}).get("nodeInfo", {}).get("kubeletVersion")
        or node.get("status", {}).get("nodeInfo", {}).get("kubeProxyVersion")
        or "unknown"
    )


def _fetch_release_text(url: str) -> Optional[str]:
    try:
        with urllib.request.urlopen(url, timeout=5) as response:
            value = response.read().decode("utf-8").strip()
            if value:
                return normalize_version(value)
    except (urllib.error.URLError, TimeoutError, OSError):
        pass
    return None


def _fetch_latest_k8s_version() -> str:
    key = "latest_k8s_stable"
    with _version_cache_lock:
        entry = _version_cache.get(key)
        if entry and time.monotonic() - entry["ts"] < _VERSION_CACHE_TTL:
            return entry["value"]
    result = _fetch_release_text("https://dl.k8s.io/release/stable.txt") or "unknown"
    with _version_cache_lock:
        _version_cache[key] = {"value": result, "ts": time.monotonic()}
    return result


def _is_prerelease_version(version: str) -> bool:
    lowered = version.lower()
    return any(marker in lowered for marker in ("-rc", "-beta", "-alpha", "-preview"))


def _release_binary_exists(version: str) -> bool:
    normalized = normalize_version(version).lstrip("v")
    url = f"https://dl.k8s.io/release/{normalized}/bin/linux/amd64/kubeadm"
    try:
        request = urllib.request.Request(url, method="HEAD")
        with urllib.request.urlopen(request, timeout=5) as response:
            return response.status == 200
    except (urllib.error.URLError, TimeoutError, OSError):
        return False


def _fetch_latest_patch_for_minor(major: int, minor: int) -> str:
    fetched = _fetch_release_text(f"https://dl.k8s.io/release/latest-{major}.{minor}.txt")
    if fetched and not _is_prerelease_version(fetched):
        return fetched
    for patch in range(30, -1, -1):
        candidate = f"v{major}.{minor}.{patch}"
        if _release_binary_exists(candidate):
            return candidate
    return f"v{major}.{minor}.0"


def recommended_kubeadm_target(current_version: str) -> Optional[str]:
    current_tuple = parse_k8s_version(current_version)
    if current_tuple == (0, 0, 0):
        return None
    key = f"kubeadm_target_{current_tuple[0]}_{current_tuple[1] + 1}"
    with _version_cache_lock:
        entry = _version_cache.get(key)
        if entry and time.monotonic() - entry["ts"] < _VERSION_CACHE_TTL:
            return entry["value"]
    result = _fetch_latest_patch_for_minor(current_tuple[0], current_tuple[1] + 1)
    with _version_cache_lock:
        _version_cache[key] = {"value": result, "ts": time.monotonic()}
    return result


def kubeadm_minor_jump_blocked(current_version: str, target_version: str) -> Optional[str]:
    current_tuple = parse_k8s_version(current_version)
    target_tuple = parse_k8s_version(target_version)
    if current_tuple == (0, 0, 0) or target_tuple == (0, 0, 0):
        return None
    if target_tuple[1] - current_tuple[1] <= 1:
        return None
    recommended = recommended_kubeadm_target(current_version)
    return (
        f"kubeadm supports upgrading one minor version at a time. "
        f"Upgrade {current_version} to {recommended or f'v{current_tuple[0]}.{current_tuple[1] + 1}.x'} first, "
        f"not directly to {normalize_version(target_version)}."
    )


def build_target_version_options(
    current_version: str,
    latest_available: str,
    provider: str,
) -> List[Dict[str, Any]]:
    options: List[Dict[str, Any]] = []
    current = normalize_version(current_version)
    latest = normalize_version(latest_available) if latest_available not in {"", "unknown"} else None

    if provider == "kubeadm" and current != "unknown":
        recommended = recommended_kubeadm_target(current_version)
        if recommended:
            options.append(
                {
                    "value": recommended,
                    "label": f"{recommended} (recommended)",
                    "recommended": True,
                }
            )
        if latest and latest != recommended:
            options.append(
                {
                    "value": latest,
                    "label": f"{latest} (latest stable)",
                    "recommended": False,
                }
            )
        return options

    if latest:
        options.append({"value": latest, "label": latest, "recommended": True})
    if current != "unknown" and current != latest:
        options.append({"value": current, "label": f"{current} (current)", "recommended": False})
    return options


def _cli_available(name: str) -> bool:
    return shutil.which(name) is not None


def detect_cluster_provider(
    access: ClusterAccess,
    run_kubectl: RunKubectlFn,
    *,
    version_data: Optional[Dict[str, Any]] = None,
    node_items: Optional[List[Dict[str, Any]]] = None,
) -> str:
    if version_data is None:
        try:
            version_output = run_kubectl(access, ["version", "-o", "json"])
            version_data = json.loads(version_output)
        except Exception:
            version_data = {}

    if node_items is None:
        try:
            nodes_output = run_kubectl(access, ["get", "nodes", "-o", "json"])
            node_items = json.loads(nodes_output).get("items", [])
        except Exception:
            node_items = []

    context = (access.context_name or "").lower()
    server_url = ""
    try:
        cluster_name = version_data.get("contextName") or access.context_name or ""
        if cluster_name:
            view_output = run_kubectl(access, ["config", "view", "--minify", "-o", "json"])
            view_data = json.loads(view_output)
            clusters = view_data.get("clusters", [])
            if clusters:
                server_url = (clusters[0].get("cluster", {}).get("server") or "").lower()
    except Exception:
        pass

    node_names = [n.get("metadata", {}).get("name", "").lower() for n in node_items]
    all_labels: Dict[str, str] = {}
    for node in node_items:
        labels = node.get("metadata", {}).get("labels", {}) or {}
        all_labels.update(labels)

    label_keys = " ".join(all_labels.keys()).lower()
    provider_ids = " ".join(
        (node.get("spec", {}).get("providerID") or "") for node in node_items
    ).lower()

    if "kind://" in provider_ids:
        return "kind"

    if context.startswith("kind-") or "node.kubernetes.io/kind-cluster" in label_keys:
        return "kind"

    if "docker-desktop" in context or "docker-for-desktop" in context:
        return "docker-desktop"
    if any("docker-desktop" in name for name in node_names):
        return "docker-desktop"
    if "kubernetes.docker.internal" in server_url or "docker.internal" in server_url:
        return "docker-desktop"
    if "docker://" in provider_ids:
        return "docker-desktop"

    if context == "minikube" or "minikube.k8s.io" in label_keys:
        return "minikube"

    if ".eks." in server_url or "amazonaws.com" in server_url or "eks.amazonaws.com" in label_keys:
        return "eks"
    if "eks" in provider_ids:
        return "eks"

    if ".azmk8s.io" in server_url or "kubernetes.azure.com" in label_keys:
        return "aks"

    if ".googleapis.com" in server_url or "cloud.google.com/gke" in label_keys:
        return "gke"

    if any(key.startswith("node-role.kubernetes.io/control-plane") for key in all_labels):
        if not any(p in label_keys for p in ("eks.", "azure.com", "google.com")):
            return "kubeadm"

    if node_items:
        return "kubeadm"

    return "unknown"


def get_provider_support(provider: str) -> Dict[str, Any]:
    provider = provider or "unknown"
    display = PROVIDER_DISPLAY.get(provider, "Unknown")

    if provider == "docker-desktop":
        return {
            "provider": provider,
            "providerDisplay": display,
            "upgradeSupported": False,
            "executionMode": "instructions",
            "reason": "Docker Desktop manages Kubernetes upgrades internally.",
            "instructions": {
                "title": "Docker Desktop Kubernetes Upgrade",
                "summary": "Docker Desktop Kubernetes upgrades are managed through Docker Desktop.",
                "steps": [
                    "Open Docker Desktop",
                    "Settings",
                    "Kubernetes",
                    "Upgrade Kubernetes Version",
                ],
            },
        }

    if provider == "kind":
        return {
            "provider": provider,
            "providerDisplay": display,
            "upgradeSupported": False,
            "executionMode": "instructions",
            "reason": "kind clusters must be recreated with a newer node image.",
            "instructions": {
                "title": "kind Cluster Upgrade",
                "summary": "kind clusters should be recreated using a newer node image.",
                "steps": [
                    "Export workloads and configuration",
                    "Delete the existing kind cluster: kind delete cluster --name <name>",
                    "Create a new cluster with a newer Kubernetes node image",
                    "Re-apply workloads to the new cluster",
                ],
            },
        }

    if provider == "minikube":
        minikube_available = _cli_available("minikube")
        return {
            "provider": provider,
            "providerDisplay": display,
            "upgradeSupported": minikube_available,
            "executionMode": "instructions" if not minikube_available else "execute-with-cli",
            "reason": (
                "Minikube upgrades require the minikube CLI."
                if not minikube_available
                else "Minikube supports in-place upgrades via the minikube CLI."
            ),
            "cliAvailable": minikube_available,
            "instructions": {
                "title": "Minikube Upgrade",
                "summary": "Upgrade Minikube using the minikube CLI.",
                "steps": [
                    "minikube stop",
                    "minikube delete --all --purge  # optional clean reinstall",
                    "minikube start --kubernetes-version=<target>",
                    "Verify: kubectl version",
                ],
            },
        }

    if provider == "kubeadm":
        automated = auto_upgrade_enabled()
        return {
            "provider": provider,
            "providerDisplay": display,
            "upgradeSupported": True,
            "executionMode": "execute-with-cli" if automated else "plan-only",
            "reason": (
                "KubeSight runs kubeadm upgrade steps automatically."
                if automated
                else "kubeadm upgrades require manual execution with confirmation."
            ),
            "instructions": {
                "title": "kubeadm Upgrade",
                "summary": (
                    "KubeSight installs kubeadm/kubelet/kubectl packages and runs kubeadm upgrade steps."
                    if automated
                    else "Plan the upgrade with KubeSight; execute kubeadm commands manually on each node."
                ),
                "steps": [
                    "Install kubeadm/kubelet/kubectl packages at target version (automated)"
                    if automated
                    else "Upgrade kubeadm on the first control plane node",
                    "kubeadm upgrade plan",
                    "kubeadm upgrade apply v<target>",
                    "Upgrade kubelet/kubectl on control plane and workers",
                    "Uncordon nodes after upgrade",
                ],
            },
        }

    if provider == "eks":
        aws_available = _cli_available("aws") and _cli_available("eksctl")
        return {
            "provider": provider,
            "providerDisplay": display,
            "upgradeSupported": aws_available,
            "executionMode": "execute-with-cli" if aws_available else "instructions",
            "reason": (
                "EKS upgrades require AWS CLI and eksctl."
                if not aws_available
                else "EKS upgrades can be initiated via AWS CLI / eksctl."
            ),
            "cliAvailable": aws_available,
            "instructions": {
                "title": "Amazon EKS Upgrade",
                "summary": "Upgrade the EKS control plane and node groups via AWS.",
                "steps": [
                    "aws eks update-cluster-version --name <cluster> --kubernetes-version <target>",
                    "Update managed node groups or self-managed nodes",
                    "Verify cluster and workloads after upgrade",
                ],
            },
        }

    if provider == "aks":
        az_available = _cli_available("az")
        return {
            "provider": provider,
            "providerDisplay": display,
            "upgradeSupported": az_available,
            "executionMode": "execute-with-cli" if az_available else "instructions",
            "reason": (
                "AKS upgrades require Azure CLI (az)."
                if not az_available
                else "AKS upgrades can be initiated via Azure CLI."
            ),
            "cliAvailable": az_available,
            "instructions": {
                "title": "Azure AKS Upgrade",
                "summary": "Upgrade AKS control plane and node pools via Azure CLI.",
                "steps": [
                    "az aks get-upgrades --resource-group <rg> --name <cluster>",
                    "az aks upgrade --resource-group <rg> --name <cluster> --kubernetes-version <target>",
                    "Upgrade node pools to match control plane version",
                ],
            },
        }

    if provider == "gke":
        gcloud_available = _cli_available("gcloud")
        return {
            "provider": provider,
            "providerDisplay": display,
            "upgradeSupported": gcloud_available,
            "executionMode": "execute-with-cli" if gcloud_available else "instructions",
            "reason": (
                "GKE upgrades require Google Cloud SDK (gcloud)."
                if not gcloud_available
                else "GKE upgrades can be initiated via gcloud."
            ),
            "cliAvailable": gcloud_available,
            "instructions": {
                "title": "Google GKE Upgrade",
                "summary": "Upgrade GKE control plane and node pools via gcloud.",
                "steps": [
                    "gcloud container clusters upgrade <cluster> --master --cluster-version <target>",
                    "gcloud container clusters upgrade <cluster> --node-pool <pool>",
                    "Verify workloads after node pool upgrade",
                ],
            },
        }

    return {
        "provider": "unknown",
        "providerDisplay": "Unknown",
        "upgradeSupported": False,
        "executionMode": "instructions",
        "reason": "Cluster provider could not be determined. Upgrades must be performed manually.",
        "instructions": {
            "title": "Manual Upgrade Required",
            "summary": "KubeSight cannot determine how to upgrade this cluster automatically.",
            "steps": [
                "Consult your cluster provider documentation",
                "Validate workloads and backups before upgrading",
                "Upgrade control plane, then worker nodes",
                "Verify cluster health after upgrade",
            ],
        },
    }


def analyze_version_skew(
    control_plane_version: str,
    node_items: List[Dict[str, Any]],
) -> Dict[str, Any]:
    nodes = [
        {"name": node.get("metadata", {}).get("name", "unknown"), "version": _node_version(node)}
        for node in node_items
    ]
    cp_tuple = parse_k8s_version(control_plane_version)
    warnings: List[str] = []
    node_versions = {n["version"] for n in nodes if n["version"] != "unknown"}

    for node in nodes:
        node_tuple = parse_k8s_version(node["version"])
        if node_tuple != cp_tuple and node["version"] != "unknown":
            warnings.append(
                f"Node {node['name']} ({node['version']}) differs from control plane ({control_plane_version})."
            )

    if len(node_versions) > 1:
        warnings.append("Multiple node versions detected across the cluster.")

    if cp_tuple != (0, 0, 0) and node_versions:
        for version in node_versions:
            node_tuple = parse_k8s_version(version)
            if node_tuple[0] != cp_tuple[0] or abs(node_tuple[1] - cp_tuple[1]) > 1:
                warnings.append(
                    f"Version skew exceeds supported range: node {version} vs control plane {control_plane_version}."
                )

    status = "healthy" if not warnings else "warning"
    return {
        "controlPlaneVersion": control_plane_version,
        "nodes": nodes,
        "status": status,
        "warnings": warnings,
    }


def build_cluster_info(
    access: ClusterAccess,
    run_kubectl: RunKubectlFn,
    *,
    version_data: Optional[Dict[str, Any]] = None,
    node_items: Optional[List[Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    if version_data is None and node_items is None:
        from concurrent.futures import ThreadPoolExecutor
        with ThreadPoolExecutor(max_workers=2) as pool:
            vf = pool.submit(run_kubectl, access, ["version", "-o", "json"])
            nf = pool.submit(run_kubectl, access, ["get", "nodes", "-o", "json"])
        version_data = json.loads(vf.result())
        node_items = json.loads(nf.result()).get("items", [])
    elif version_data is None:
        version_output = run_kubectl(access, ["version", "-o", "json"])
        version_data = json.loads(version_output)
    elif node_items is None:
        nodes_output = run_kubectl(access, ["get", "nodes", "-o", "json"])
        node_items = json.loads(nodes_output).get("items", [])

    control_plane = version_data.get("serverVersion", {}).get("gitVersion") or "unknown"
    client_version = version_data.get("clientVersion", {}).get("gitVersion") or "unknown"
    provider = detect_cluster_provider(access, run_kubectl, version_data=version_data, node_items=node_items)
    provider_support = get_provider_support(provider)

    nodes = [
        {
            "name": node.get("metadata", {}).get("name", "unknown"),
            "version": _node_version(node),
            "ready": _node_ready(node),
        }
        for node in node_items
    ]

    not_ready = [n["name"] for n in nodes if not n["ready"]]
    if not node_items:
        health = "unknown"
    elif not_ready:
        health = "unhealthy"
    else:
        health = "healthy"

    return {
        "clusterId": access.cluster_id,
        "contextName": access.context_name,
        "provider": provider,
        "providerDisplay": provider_support["providerDisplay"],
        "controlPlaneVersion": control_plane,
        "kubectlClientVersion": client_version,
        "nodes": nodes,
        "health": health,
    }


def build_version_info(
    current_version: str,
    target_version: str,
    provider_support: Dict[str, Any],
    *,
    fetch_latest: bool = True,
) -> Dict[str, Any]:
    provider = provider_support.get("provider", "unknown")
    latest = _fetch_latest_k8s_version() if fetch_latest else "unknown"
    recommended = (
        recommended_kubeadm_target(current_version)
        if provider == "kubeadm"
        else None
    )
    return {
        "currentVersion": current_version,
        "latestAvailable": latest,
        "recommendedTarget": recommended,
        "targetOptions": build_target_version_options(current_version, latest, provider),
        "targetVersion": normalize_version(target_version),
        "upgradeSupported": bool(provider_support.get("upgradeSupported")),
        "reason": provider_support.get("reason", ""),
    }


def generate_upgrade_plan(provider_support: Dict[str, Any]) -> Dict[str, Any]:
    manual = provider_support.get("executionMode") in {
        "instructions",
        "plan-only",
    } or not provider_support.get("upgradeSupported")
    if provider_support.get("executionMode") == "execute-with-cli" and auto_upgrade_enabled():
        manual = False

    steps = [
        {
            "step": index + 1,
            "name": name,
            "status": "pending",
            "automated": not manual and index >= 2,
        }
        for index, name in enumerate(UPGRADE_PLAN_STEPS)
    ]

    return {
        "steps": steps,
        "manualUpgradeRequired": manual,
        "instructions": provider_support.get("instructions"),
    }


def _check_status(name: str, status: str, details: str = "") -> Dict[str, Any]:
    entry: Dict[str, Any] = {"name": name, "status": status}
    if details:
        entry["details"] = details
    return entry


def run_extended_prechecks(
    access: ClusterAccess,
    target_version: str,
    run_kubectl: RunKubectlFn,
) -> Dict[str, Any]:
    checks: List[Dict[str, Any]] = []
    current_version = "unknown"
    node_items: List[Dict[str, Any]] = []
    version_data: Dict[str, Any] = {}

    try:
        version_output = run_kubectl(access, ["version", "-o", "json"])
        version_data = json.loads(version_output)
        current_version = version_data.get("serverVersion", {}).get("gitVersion") or "unknown"
        checks.append(_check_status("API reachable", "passed", "Cluster API is reachable."))
    except Exception as exc:
        checks.append(_check_status("API reachable", "failed", str(exc)))
        return _precheck_payload(access, target_version, current_version, checks, node_items, version_data, run_kubectl)

    try:
        nodes_output = run_kubectl(access, ["get", "nodes", "-o", "json"])
        node_items = json.loads(nodes_output).get("items", [])
        not_ready = [n.get("metadata", {}).get("name", "unknown") for n in node_items if not _node_ready(n)]
        if not_ready:
            checks.append(_check_status("Nodes ready", "failed", f"Not ready: {', '.join(not_ready)}"))
        else:
            checks.append(_check_status("Nodes ready", "passed", f"All {len(node_items)} node(s) are Ready."))
    except Exception as exc:
        checks.append(_check_status("Nodes ready", "failed", str(exc)))

    try:
        pods_output = run_kubectl(access, ["get", "pods", "-n", "kube-system", "-o", "json"])
        kube_pods = json.loads(pods_output).get("items", [])
        failed = [p.get("metadata", {}).get("name") for p in kube_pods if p.get("status", {}).get("phase") == "Failed"]
        not_running = [
            p.get("metadata", {}).get("name")
            for p in kube_pods
            if p.get("status", {}).get("phase") not in ("Running", "Succeeded")
        ]
        if failed:
            checks.append(_check_status("Control plane healthy", "failed", f"Failed pods: {', '.join(failed)}"))
        elif not_running:
            checks.append(
                _check_status(
                    "Control plane healthy",
                    "warning",
                    f"Non-running kube-system pods: {', '.join(not_running[:5])}",
                )
            )
        else:
            checks.append(_check_status("Control plane healthy", "passed", "kube-system pods are healthy."))
    except Exception as exc:
        checks.append(_check_status("Control plane healthy", "warning", str(exc)))

    provider = detect_cluster_provider(access, run_kubectl, version_data=version_data, node_items=node_items)

    valid_target, target_error = validate_target_version(target_version)
    if not valid_target:
        checks.append(_check_status("Target version validation", "failed", target_error or "Invalid target version."))
    else:
        current_tuple = parse_k8s_version(current_version)
        target_tuple = parse_k8s_version(target_version)
        kubeadm_jump_error = (
            kubeadm_minor_jump_blocked(current_version, target_version)
            if provider == "kubeadm"
            else None
        )
        if kubeadm_jump_error:
            checks.append(_check_status("Target version validation", "failed", kubeadm_jump_error))
        elif current_tuple >= target_tuple:
            checks.append(
                _check_status(
                    "Target version validation",
                    "warning",
                    f"Cluster already at or above {normalize_version(target_version)} ({current_version}).",
                )
            )
        else:
            checks.append(
                _check_status(
                    "Target version validation",
                    "passed",
                    f"Upgrade path {current_version} -> {normalize_version(target_version)}.",
                )
            )

    try:
        run_kubectl(access, ["top", "nodes", "--no-headers"])
        checks.append(_check_status("Metrics server available", "passed", "metrics-server is available."))
    except Exception:
        checks.append(
            _check_status(
                "Metrics server available",
                "warning",
                "metrics-server not available; resource metrics may be limited.",
            )
        )

    # PVC Health
    try:
        pvc_output = run_kubectl(access, ["get", "pvc", "--all-namespaces", "-o", "json"])
        pvcs = json.loads(pvc_output).get("items", [])
        pending = [p.get("metadata", {}).get("name") for p in pvcs if p.get("status", {}).get("phase") == "Pending"]
        lost = [p.get("metadata", {}).get("name") for p in pvcs if p.get("status", {}).get("phase") == "Lost"]
        if lost:
            checks.append(_check_status("PVC health", "failed", f"Lost PVCs: {', '.join(lost[:10])}"))
        elif pending:
            checks.append(_check_status("PVC health", "warning", f"Pending PVCs: {', '.join(pending[:10])}"))
        else:
            checks.append(_check_status("PVC health", "passed", f"{len(pvcs)} PVC(s) checked."))
    except Exception as exc:
        checks.append(_check_status("PVC health", "warning", str(exc)))

    # Storage Health
    try:
        sc_output = run_kubectl(access, ["get", "storageclass", "-o", "json"])
        sc_items = json.loads(sc_output).get("items", [])
        if not sc_items:
            checks.append(_check_status("Storage health", "warning", "No StorageClasses found."))
        else:
            default_count = sum(
                1 for sc in sc_items if sc.get("metadata", {}).get("annotations", {}).get("storageclass.kubernetes.io/is-default-class") == "true"
            )
            detail = f"{len(sc_items)} StorageClass(es)"
            if default_count == 0:
                checks.append(_check_status("Storage health", "warning", f"{detail}; no default StorageClass."))
            else:
                checks.append(_check_status("Storage health", "passed", detail))
    except Exception as exc:
        checks.append(_check_status("Storage health", "warning", str(exc)))

    # Pod restart analysis, CrashLoopBackOff, Pending pods
    try:
        pods_output = run_kubectl(access, ["get", "pods", "--all-namespaces", "-o", "json"])
        all_pods = json.loads(pods_output).get("items", [])
        high_restart: List[str] = []
        crash_loop: List[str] = []
        pending_pods: List[str] = []

        for pod in all_pods:
            meta = pod.get("metadata", {})
            ns_name = f"{meta.get('namespace')}/{meta.get('name')}"
            phase = pod.get("status", {}).get("phase")
            if phase == "Pending":
                pending_pods.append(ns_name)
            for cs in pod.get("status", {}).get("containerStatuses", []) or []:
                waiting = cs.get("state", {}).get("waiting", {})
                if waiting.get("reason") == "CrashLoopBackOff":
                    crash_loop.append(ns_name)
                if cs.get("restartCount", 0) >= 10:
                    high_restart.append(ns_name)

        if crash_loop:
            checks.append(
                _check_status(
                    "CrashLoopBackOff detection",
                    "failed",
                    f"CrashLoopBackOff: {', '.join(crash_loop[:10])}",
                )
            )
        else:
            checks.append(_check_status("CrashLoopBackOff detection", "passed", "No CrashLoopBackOff pods."))

        if high_restart:
            checks.append(
                _check_status(
                    "Pod restart analysis",
                    "warning",
                    f"High restart count (>=10): {', '.join(high_restart[:10])}",
                )
            )
        else:
            checks.append(_check_status("Pod restart analysis", "passed", "No excessive pod restarts."))

        if pending_pods:
            checks.append(
                _check_status(
                    "Pending pods",
                    "warning",
                    f"Pending pods: {', '.join(pending_pods[:10])}",
                )
            )
        else:
            checks.append(_check_status("Pending pods", "passed", "No pending pods."))
    except Exception as exc:
        checks.append(_check_status("Pod restart analysis", "warning", str(exc)))
        checks.append(_check_status("CrashLoopBackOff detection", "warning", str(exc)))
        checks.append(_check_status("Pending pods", "warning", str(exc)))

    # PDB validation
    try:
        pdb_output = run_kubectl(access, ["get", "pdb", "--all-namespaces", "-o", "json"])
        pdbs = json.loads(pdb_output).get("items", [])
        unhealthy_pdbs = []
        for pdb in pdbs:
            status = pdb.get("status", {})
            desired = status.get("expectedPods", 0)
            healthy = status.get("currentHealthy", 0)
            name = pdb.get("metadata", {}).get("name", "unknown")
            if desired > 0 and healthy < desired:
                unhealthy_pdbs.append(name)
        if unhealthy_pdbs:
            checks.append(
                _check_status(
                    "Pod disruption budget validation",
                    "warning",
                    f"PDBs below desired healthy: {', '.join(unhealthy_pdbs[:10])}",
                )
            )
        else:
            checks.append(
                _check_status(
                    "Pod disruption budget validation",
                    "passed",
                    f"{len(pdbs)} PDB(s) validated.",
                )
            )
    except Exception as exc:
        checks.append(_check_status("Pod disruption budget validation", "warning", str(exc)))

    # Deprecated API detection (lightweight via apiservices / api resources)
    try:
        apis_output = run_kubectl(access, ["get", "apiservices", "-o", "json"])
        apis = json.loads(apis_output).get("items", [])
        unavailable = []
        for a in apis:
            conditions = a.get("status", {}).get("conditions", [])
            available = any(
                c.get("type") == "Available" and c.get("status") == "True" for c in conditions
            )
            if not available:
                unavailable.append(a.get("metadata", {}).get("name"))
        if unavailable:
            checks.append(
                _check_status(
                    "Deprecated API detection",
                    "warning",
                    f"Unavailable API services: {', '.join(unavailable[:5])}. Review deprecated APIs before upgrade.",
                )
            )
        else:
            checks.append(
                _check_status(
                    "Deprecated API detection",
                    "passed",
                    "No unavailable API services detected. Run provider-specific deprecation checks before upgrade.",
                )
            )
    except Exception as exc:
        checks.append(_check_status("Deprecated API detection", "warning", str(exc)))

    # Version skew
    skew = analyze_version_skew(current_version, node_items)
    if skew["warnings"]:
        checks.append(
            _check_status("Version skew detection", "warning", "; ".join(skew["warnings"][:3]))
        )
    else:
        checks.append(_check_status("Version skew detection", "passed", "Node versions align with control plane."))

    # Node resource pressure
    try:
        pressured: List[str] = []
        for node in node_items:
            name = node.get("metadata", {}).get("name", "unknown")
            for condition in node.get("status", {}).get("conditions", []):
                if condition.get("type") in ("MemoryPressure", "DiskPressure", "PIDPressure"):
                    if condition.get("status") == "True":
                        pressured.append(f"{name}({condition.get('type')})")
        if pressured:
            checks.append(
                _check_status("Node resource pressure", "warning", f"Pressure detected: {', '.join(pressured)}")
            )
        else:
            checks.append(_check_status("Node resource pressure", "passed", "No resource pressure on nodes."))
    except Exception as exc:
        checks.append(_check_status("Node resource pressure", "warning", str(exc)))

    # Kube-system health (broader than control plane)
    try:
        ks_output = run_kubectl(access, ["get", "pods", "-n", "kube-system", "-o", "json"])
        ks_pods = json.loads(ks_output).get("items", [])
        bad = []
        for pod in ks_pods:
            phase = pod.get("status", {}).get("phase")
            ready = all(c.get("ready") for c in pod.get("status", {}).get("containerStatuses", []) or [])
            if phase != "Running" or not ready:
                bad.append(pod.get("metadata", {}).get("name"))
        if bad:
            checks.append(
                _check_status("Kube-system health", "warning", f"Unhealthy kube-system pods: {', '.join(bad[:10])}")
            )
        else:
            checks.append(_check_status("Kube-system health", "passed", f"{len(ks_pods)} kube-system pod(s) healthy."))
    except Exception as exc:
        checks.append(_check_status("Kube-system health", "warning", str(exc)))

    return _precheck_payload(access, target_version, current_version, checks, node_items, version_data, run_kubectl)


def _precheck_payload(
    access: ClusterAccess,
    target_version: str,
    current_version: str,
    checks: List[Dict[str, Any]],
    node_items: List[Dict[str, Any]],
    version_data: Dict[str, Any],
    run_kubectl: RunKubectlFn,
) -> Dict[str, Any]:
    can_upgrade = not any(check.get("status") == "failed" for check in checks)
    provider = detect_cluster_provider(access, run_kubectl, version_data=version_data, node_items=node_items)
    provider_support = get_provider_support(provider)
    version_skew = analyze_version_skew(current_version, node_items)

    return {
        "clusterId": access.cluster_id,
        "targetVersion": normalize_version(target_version),
        "currentVersion": current_version,
        "canUpgrade": can_upgrade,
        "checks": checks,
        "provider": provider_support,
        "versionSkew": version_skew,
        "versionInfo": build_version_info(current_version, target_version, provider_support),
        "upgradePlan": generate_upgrade_plan(provider_support),
        "generatedAt": datetime.now(timezone.utc).isoformat(),
    }


def build_upgrade_info(
    access: ClusterAccess,
    target_version: str,
    run_kubectl: RunKubectlFn,
) -> Dict[str, Any]:
    from concurrent.futures import ThreadPoolExecutor

    # Fetch version info, node list, AND warm the external version cache — all in parallel.
    with ThreadPoolExecutor(max_workers=3) as pool:
        vf = pool.submit(run_kubectl, access, ["version", "-o", "json"])
        nf = pool.submit(run_kubectl, access, ["get", "nodes", "-o", "json"])
        pool.submit(_fetch_latest_k8s_version)  # warms cache; result used below via cache hit

    version_data = json.loads(vf.result())
    try:
        node_items = json.loads(nf.result()).get("items", [])
    except Exception:
        node_items = []

    # Pass already-fetched data so build_cluster_info makes zero additional kubectl calls.
    cluster_info = build_cluster_info(access, run_kubectl, version_data=version_data, node_items=node_items)
    provider_support = get_provider_support(cluster_info["provider"])
    version_info = build_version_info(
        cluster_info["controlPlaneVersion"],
        target_version,
        provider_support,
    )
    # Reuse the already-fetched node_items — no second kubectl get nodes.
    version_skew = analyze_version_skew(cluster_info["controlPlaneVersion"], node_items)

    return {
        "clusterInfo": cluster_info,
        "provider": provider_support,
        "versionInfo": version_info,
        "versionSkew": version_skew,
        "upgradePlan": generate_upgrade_plan(provider_support),
        "generatedAt": datetime.now(timezone.utc).isoformat(),
    }


def required_confirmation_text(provider: str, cluster_id: str, target_version: str) -> str:
    return f"UPGRADE {cluster_id} TO {normalize_version(target_version)}"


def run_upgrade_workflow(
    access: ClusterAccess,
    target_version: str,
    run_kubectl: RunKubectlFn,
    *,
    confirmation: Optional[str] = None,
) -> Dict[str, Any]:
    precheck = run_extended_prechecks(access, target_version, run_kubectl)
    provider_support = precheck.get("provider", {})
    provider = provider_support.get("provider", "unknown")
    execution_mode = provider_support.get("executionMode", "instructions")

    if not precheck.get("canUpgrade"):
        return {
            "upgradeId": None,
            "clusterId": access.cluster_id,
            "targetVersion": normalize_version(target_version),
            "currentVersion": precheck.get("currentVersion"),
            "status": "blocked",
            "message": "Precheck failed. Resolve failed checks before starting upgrade.",
            "steps": [],
            "checks": precheck.get("checks", []),
            "provider": provider_support,
            "upgradePlan": precheck.get("upgradePlan"),
            "executionSupported": False,
        }

    plan = precheck.get("upgradePlan") or generate_upgrade_plan(provider_support)
    instructions = provider_support.get("instructions") or {}

    def _manual_plan_response(message: str) -> Dict[str, Any]:
        steps = [
            {
                "step": index + 1,
                "name": step["name"],
                "status": "manual",
                "message": "Manual step — see upgrade instructions.",
            }
            for index, step in enumerate(plan.get("steps", []))
        ]
        return {
            "upgradeId": f"plan-{access.cluster_id}-{datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S')}",
            "clusterId": access.cluster_id,
            "targetVersion": normalize_version(target_version),
            "currentVersion": precheck.get("currentVersion"),
            "status": "manual_required",
            "message": message,
            "steps": steps,
            "activeStep": 0,
            "checks": precheck.get("checks", []),
            "provider": provider_support,
            "upgradePlan": plan,
            "instructions": instructions,
            "executionSupported": False,
            "startedAt": datetime.now(timezone.utc).isoformat(),
        }

    if provider == "kubeadm" and provider_support.get("upgradeSupported"):
        if auto_upgrade_enabled():
            from .upgrade_executor import start_automated_upgrade

            return start_automated_upgrade(
                access,
                provider=provider,
                target_version=target_version,
                current_version=precheck.get("currentVersion") or "unknown",
                run_kubectl=run_kubectl,
                precheck=precheck,
            )
        return _manual_plan_response(
            "Execute kubeadm upgrade steps manually on the control plane and worker nodes."
        )

    if (
        auto_upgrade_enabled()
        and execution_mode == "execute-with-cli"
        and provider_support.get("upgradeSupported")
        and provider in {"minikube", "eks", "aks", "gke"}
    ):
        from .upgrade_executor import start_automated_upgrade

        return start_automated_upgrade(
            access,
            provider=provider,
            target_version=target_version,
            current_version=precheck.get("currentVersion") or "unknown",
            run_kubectl=run_kubectl,
            precheck=precheck,
        )

    if execution_mode == "instructions" or not provider_support.get("upgradeSupported"):
        return _manual_plan_response(provider_support.get("reason", "Manual upgrade required."))

    # Cloud / minikube with CLI — validate readiness only, do not execute provider upgrade
    steps: List[Dict[str, Any]] = []
    for index, step_name in enumerate(UPGRADE_PLAN_STEPS):
        status = "completed"
        message = "Readiness validated."
        if index == 0:
            message = "Cluster readiness validated via precheck."
        elif index == 1:
            message = f"Target version {normalize_version(target_version)} validated."
        elif index >= 2:
            status = "manual"
            message = (
                f"Execute via {provider_support.get('providerDisplay')} CLI — "
                "KubeSight does not run provider upgrade commands automatically."
            )
        steps.append({"step": index + 1, "name": step_name, "status": status, "message": message})

    return {
        "upgradeId": f"plan-{access.cluster_id}-{datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S')}",
        "clusterId": access.cluster_id,
        "targetVersion": normalize_version(target_version),
        "currentVersion": precheck.get("currentVersion"),
        "status": "manual_required",
        "message": (
            "Prechecks passed. Complete the upgrade using your provider CLI or console. "
            "KubeSight does not execute provider upgrade commands."
        ),
        "steps": steps,
        "activeStep": 1,
        "checks": precheck.get("checks", []),
        "provider": provider_support,
        "upgradePlan": plan,
        "instructions": instructions,
        "executionSupported": False,
        "startedAt": datetime.now(timezone.utc).isoformat(),
    }


def mock_upgrade_info(cluster_id: str, target_version: str, context_name: str = "docker-desktop") -> Dict[str, Any]:
    """Mock cluster info for test / demo mode."""
    provider = "docker-desktop" if "docker" in context_name.lower() else "unknown"
    if context_name.lower().startswith("kind-"):
        provider = "kind"
    elif context_name.lower() == "minikube":
        provider = "minikube"

    provider_support = get_provider_support(provider)
    current = "v1.29.0"
    cluster_info = {
        "clusterId": cluster_id,
        "contextName": context_name,
        "provider": provider,
        "providerDisplay": provider_support["providerDisplay"],
        "controlPlaneVersion": current,
        "kubectlClientVersion": current,
        "nodes": [{"name": context_name, "version": current, "ready": True}],
        "health": "healthy",
    }
    return {
        "clusterInfo": cluster_info,
        "provider": provider_support,
        "versionInfo": {
            "currentVersion": current,
            "latestAvailable": "unknown",
            "targetVersion": normalize_version(target_version),
            "upgradeSupported": provider_support.get("upgradeSupported", False),
            "reason": provider_support.get("reason", ""),
        },
        "versionSkew": {
            "controlPlaneVersion": current,
            "nodes": [{"name": context_name, "version": current}],
            "status": "healthy",
            "warnings": [],
        },
        "upgradePlan": generate_upgrade_plan(provider_support),
        "generatedAt": datetime.now(timezone.utc).isoformat(),
    }


def mock_precheck(cluster_id: str, target_version: str, context_name: str = "docker-desktop") -> Dict[str, Any]:
    info = mock_upgrade_info(cluster_id, target_version, context_name)
    checks = [
        _check_status("API reachable", "passed", "Mock mode."),
        _check_status("Nodes ready", "passed", "Mock mode."),
        _check_status("Control plane healthy", "passed", "Mock mode."),
        _check_status("Target version validation", "passed", f"Mock path to {normalize_version(target_version)}."),
        _check_status("Metrics server available", "warning", "Mock mode."),
        _check_status("PVC health", "passed", "Mock mode."),
        _check_status("Storage health", "passed", "Mock mode."),
        _check_status("Pod restart analysis", "passed", "Mock mode."),
        _check_status("CrashLoopBackOff detection", "passed", "Mock mode."),
        _check_status("Pending pods", "passed", "Mock mode."),
        _check_status("Pod disruption budget validation", "passed", "Mock mode."),
        _check_status("Deprecated API detection", "passed", "Mock mode."),
        _check_status("Version skew detection", "passed", "Mock mode."),
        _check_status("Node resource pressure", "passed", "Mock mode."),
        _check_status("Kube-system health", "passed", "Mock mode."),
    ]
    return {
        "clusterId": cluster_id,
        "targetVersion": normalize_version(target_version),
        "currentVersion": info["clusterInfo"]["controlPlaneVersion"],
        "canUpgrade": True,
        "checks": checks,
        "provider": info["provider"],
        "versionSkew": info["versionSkew"],
        "versionInfo": info["versionInfo"],
        "upgradePlan": info["upgradePlan"],
        "generatedAt": datetime.now(timezone.utc).isoformat(),
    }
