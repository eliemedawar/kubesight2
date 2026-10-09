"""A read-only fake Kubernetes API server for stress-testing KubeSight.

KubeSight talks to clusters by running the real ``kubectl``; this server sits
where an API server would, so every request still goes through the real
subprocess spawn, kubectl's discovery and paging, KubeSight's parsing, its
caches and its circuit breaker. Only the cluster itself is simulated.

Each fake cluster listens on its own port with plain HTTP and no auth:

    python fake_k8s.py --base-port 7101 --profile standard

Profiles describe cluster sizes (nodes, namespaces, workloads). Responses are
delayed like a real API server: a base round trip plus time per item listed,
with jitter, the odd slow request and the odd 500. A churn thread changes a
few pods every few seconds (restarts, crash loops, recoveries) so the data is
live, and the control endpoint lets a test flip a cluster into a slow or
unreachable state, or stamp a marker on a pod to check freshness:

    GET  /__control/state
    POST /__control/mode     {"mode": "normal" | "slow" | "down" | "hang"}
    POST /__control/marker   {"namespace": "...", "pod": "...", "value": "..."}
    GET  /__control/stats    request counts by path kind

Writes (POST/PUT/PATCH/DELETE on the Kubernetes API) are refused with 403,
so nothing under test can change the simulated clusters.
"""

from __future__ import annotations

import argparse
import base64
import copy
import json
import random
import threading
import time
from collections import Counter
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import parse_qs, urlparse

# --------------------------------------------------------------------------
# Cluster profiles
# --------------------------------------------------------------------------

PROFILES: Dict[str, List[Dict[str, Any]]] = {
    # Roughly a mid-size fintech estate: one big production cluster, a
    # medium one, three lower environments and a small far-away edge cluster.
    "standard": [
        {"name": "prod-large", "nodes": 30, "namespaces": 60, "deps": (8, 14), "replicas": (2, 4), "latency_ms": (40, 120)},
        {"name": "prod-medium", "nodes": 12, "namespaces": 35, "deps": (6, 12), "replicas": (1, 4), "latency_ms": (35, 100)},
        {"name": "uat", "nodes": 8, "namespaces": 30, "deps": (5, 10), "replicas": (1, 2), "latency_ms": (30, 90)},
        {"name": "sit", "nodes": 6, "namespaces": 25, "deps": (5, 10), "replicas": (1, 2), "latency_ms": (30, 90)},
        {"name": "dev", "nodes": 5, "namespaces": 25, "deps": (4, 10), "replicas": (1, 1), "latency_ms": (25, 80)},
        {"name": "edge", "nodes": 3, "namespaces": 10, "deps": (3, 8), "replicas": (1, 2), "latency_ms": (180, 420)},
    ],
    "small": [
        {"name": "small-a", "nodes": 3, "namespaces": 6, "deps": (2, 4), "replicas": (1, 2), "latency_ms": (20, 60)},
        {"name": "small-b", "nodes": 3, "namespaces": 6, "deps": (2, 4), "replicas": (1, 2), "latency_ms": (20, 60)},
    ],
}

_DOMAINS = [
    "payments", "issuing", "acquiring", "switch", "txm", "wallet", "cards", "kyc",
    "notifications", "gateway", "reporting", "settlement", "fraud", "loyalty",
    "merchant", "onboarding", "billing", "ledger", "auth", "statements",
]
_ENV_SUFFIX = ["", "-api", "-core", "-batch", "-ops", "-int"]
_COMPONENTS = [
    "api", "worker", "scheduler", "gateway", "processor", "consumer", "adapter",
    "web", "admin", "sync", "notifier", "exporter", "orchestrator", "router",
]
_IMAGES = [
    "nexus.areeba.local:8082/{ns}/{name}:{tag}",
]

# Per-item listing cost added to the base round trip, like a real apiserver
# serialising a large list.
_MS_PER_ITEM = 0.12
_SLOW_RATE = 0.02  # fraction of requests that take ~8x longer
_ERROR_RATE = 0.003  # fraction answered with a 500


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _ts(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def _b64(text: str) -> str:
    return base64.b64encode(text.encode()).decode()


# --------------------------------------------------------------------------
# Discovery documents
# --------------------------------------------------------------------------

# (group, version, resource, kind, namespaced, shortNames)
_RESOURCES: List[Tuple[str, str, str, str, bool, List[str]]] = [
    ("", "v1", "namespaces", "Namespace", False, ["ns"]),
    ("", "v1", "nodes", "Node", False, ["no"]),
    ("", "v1", "pods", "Pod", True, ["po"]),
    ("", "v1", "services", "Service", True, ["svc"]),
    ("", "v1", "endpoints", "Endpoints", True, ["ep"]),
    ("", "v1", "configmaps", "ConfigMap", True, ["cm"]),
    ("", "v1", "secrets", "Secret", True, []),
    ("", "v1", "events", "Event", True, ["ev"]),
    ("", "v1", "persistentvolumeclaims", "PersistentVolumeClaim", True, ["pvc"]),
    ("", "v1", "persistentvolumes", "PersistentVolume", False, ["pv"]),
    ("", "v1", "serviceaccounts", "ServiceAccount", True, ["sa"]),
    ("", "v1", "resourcequotas", "ResourceQuota", True, ["quota"]),
    ("", "v1", "limitranges", "LimitRange", True, ["limits"]),
    ("", "v1", "replicationcontrollers", "ReplicationController", True, ["rc"]),
    ("apps", "v1", "deployments", "Deployment", True, ["deploy"]),
    ("apps", "v1", "replicasets", "ReplicaSet", True, ["rs"]),
    ("apps", "v1", "statefulsets", "StatefulSet", True, ["sts"]),
    ("apps", "v1", "daemonsets", "DaemonSet", True, ["ds"]),
    ("apps", "v1", "controllerrevisions", "ControllerRevision", True, []),
    ("batch", "v1", "jobs", "Job", True, []),
    ("batch", "v1", "cronjobs", "CronJob", True, ["cj"]),
    ("networking.k8s.io", "v1", "ingresses", "Ingress", True, ["ing"]),
    ("networking.k8s.io", "v1", "networkpolicies", "NetworkPolicy", True, ["netpol"]),
    ("networking.k8s.io", "v1", "ingressclasses", "IngressClass", False, []),
    ("storage.k8s.io", "v1", "storageclasses", "StorageClass", False, ["sc"]),
    ("autoscaling", "v2", "horizontalpodautoscalers", "HorizontalPodAutoscaler", True, ["hpa"]),
    ("policy", "v1", "poddisruptionbudgets", "PodDisruptionBudget", True, ["pdb"]),
    ("rbac.authorization.k8s.io", "v1", "roles", "Role", True, []),
    ("rbac.authorization.k8s.io", "v1", "rolebindings", "RoleBinding", True, []),
    ("rbac.authorization.k8s.io", "v1", "clusterroles", "ClusterRole", False, []),
    ("rbac.authorization.k8s.io", "v1", "clusterrolebindings", "ClusterRoleBinding", False, []),
    ("metrics.k8s.io", "v1beta1", "pods", "PodMetrics", True, []),
    ("metrics.k8s.io", "v1beta1", "nodes", "NodeMetrics", False, []),
    ("certificates.k8s.io", "v1", "certificatesigningrequests", "CertificateSigningRequest", False, ["csr"]),
    ("apiextensions.k8s.io", "v1", "customresourcedefinitions", "CustomResourceDefinition", False, ["crd"]),
]

_KIND_BY_RESOURCE = {(g, r): k for g, _v, r, k, _n, _s in _RESOURCES}
_NAMESPACED = {(g, r): n for g, _v, r, _k, n, _s in _RESOURCES}


def _api_resource_list(group: str, version: str) -> Dict[str, Any]:
    resources = []
    for g, v, r, k, namespaced, short in _RESOURCES:
        if g != group or v != version:
            continue
        verbs = ["get", "list"] if g == "metrics.k8s.io" else [
            "create", "delete", "deletecollection", "get", "list", "patch", "update", "watch",
        ]
        item = {"name": r, "singularName": k.lower(), "namespaced": namespaced, "kind": k, "verbs": verbs}
        if short:
            item["shortNames"] = short
        resources.append(item)
        if r == "pods" and g == "":
            for sub in ("log", "status", "exec", "eviction"):
                resources.append({"name": f"pods/{sub}", "singularName": "", "namespaced": True, "kind": "Pod", "verbs": ["get"]})
        if r == "nodes" and g == "":
            resources.append({"name": "nodes/proxy", "singularName": "", "namespaced": False, "kind": "NodeProxyOptions", "verbs": ["get"]})
        if r in ("deployments", "statefulsets", "replicasets") and g == "apps":
            resources.append({"name": f"{r}/scale", "singularName": "", "namespaced": True, "kind": "Scale", "verbs": ["get", "patch", "update"]})
    gv = f"{group}/{version}" if group else version
    return {"kind": "APIResourceList", "apiVersion": "v1", "groupVersion": gv, "resources": resources}


def _api_group_list() -> Dict[str, Any]:
    groups: Dict[str, List[str]] = {}
    for g, v, *_ in _RESOURCES:
        if g:
            groups.setdefault(g, [])
            if v not in groups[g]:
                groups[g].append(v)
    return {
        "kind": "APIGroupList",
        "apiVersion": "v1",
        "groups": [
            {
                "name": g,
                "versions": [{"groupVersion": f"{g}/{v}", "version": v} for v in vs],
                "preferredVersion": {"groupVersion": f"{g}/{vs[0]}", "version": vs[0]},
            }
            for g, vs in groups.items()
        ],
    }


# --------------------------------------------------------------------------
# Cluster state generation
# --------------------------------------------------------------------------


class FakeCluster:
    def __init__(self, spec: Dict[str, Any], seed: int):
        self.spec = spec
        self.name = spec["name"]
        self.rng = random.Random(seed)
        self.lock = threading.RLock()
        self.mode = "normal"
        self.stats: Counter = Counter()
        self.created = _now() - timedelta(days=self.rng.randint(120, 600))
        # (group, resource) -> namespace ("" for cluster scope) -> {name: obj}
        self.objects: Dict[Tuple[str, str], Dict[str, Dict[str, Any]]] = {}
        self._list_cache: Dict[Tuple[str, str, str], Tuple[int, bytes, int]] = {}
        self.version_counter = 1000
        self._generate()

    # -- object store helpers ------------------------------------------------

    def _put(self, group: str, resource: str, namespace: str, obj: Dict[str, Any]) -> None:
        self.version_counter += 1
        obj.setdefault("metadata", {})["resourceVersion"] = str(self.version_counter)
        obj["metadata"].setdefault("uid", f"{self.name}-{resource}-{namespace}-{obj['metadata']['name']}")
        self.objects.setdefault((group, resource), {}).setdefault(namespace, {})[obj["metadata"]["name"]] = obj

    def _meta(self, name: str, namespace: Optional[str] = None, *, age_days: Optional[float] = None,
              labels: Optional[Dict[str, str]] = None, owner: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        if age_days is None:
            age_days = self.rng.uniform(0.2, 90)
        created = _ts(_now() - timedelta(days=age_days))
        meta: Dict[str, Any] = {
            "name": name,
            "creationTimestamp": created,
            "labels": labels or {},
            # Real API servers attach server-side-apply bookkeeping to every
            # object; `kubectl get -o json` hides it, the raw API does not.
            "managedFields": [{
                "manager": "kubectl-client-side-apply", "operation": "Update", "apiVersion": "v1",
                "time": created, "fieldsType": "FieldsV1",
                "fieldsV1": {"f:metadata": {"f:labels": {"f:app": {}}}},
            }],
        }
        if namespace:
            meta["namespace"] = namespace
        if owner:
            meta["ownerReferences"] = [owner]
        return meta

    # -- generation ----------------------------------------------------------

    def _generate(self) -> None:
        rng = self.rng
        spec = self.spec
        nodes = []
        node_count = spec["nodes"]
        cp_count = 3 if node_count >= 8 else 1
        for i in range(node_count):
            is_cp = i < cp_count
            name = f"{self.name}-{'cp' if is_cp else 'w'}{i + 1:02d}"
            nodes.append(name)
            labels = {"kubernetes.io/hostname": name, "kubernetes.io/os": "linux"}
            if is_cp:
                labels["node-role.kubernetes.io/control-plane"] = ""
            else:
                labels["node-role.kubernetes.io/worker"] = ""
            cpu = "8" if is_cp else rng.choice(["16", "16", "32"])
            mem_gi = 32 if is_cp else rng.choice([64, 64, 128])
            conditions = [
                {"type": "MemoryPressure", "status": "False", "reason": "KubeletHasSufficientMemory"},
                {"type": "DiskPressure", "status": "False", "reason": "KubeletHasNoDiskPressure"},
                {"type": "PIDPressure", "status": "False", "reason": "KubeletHasSufficientPID"},
                {"type": "Ready", "status": "True", "reason": "KubeletReady", "message": "kubelet is posting ready status"},
            ]
            for c in conditions:
                c["lastHeartbeatTime"] = _ts(_now() - timedelta(seconds=rng.randint(5, 40)))
                c["lastTransitionTime"] = _ts(self.created)
            node = {
                "apiVersion": "v1",
                "kind": "Node",
                "metadata": self._meta(name, age_days=(_now() - self.created).days, labels=labels),
                "spec": {"podCIDR": f"10.244.{i}.0/24", **({"taints": [{"key": "node-role.kubernetes.io/control-plane", "effect": "NoSchedule"}]} if is_cp else {})},
                "status": {
                    "capacity": {"cpu": cpu, "memory": f"{mem_gi * 1024 * 1024}Ki", "pods": "110", "ephemeral-storage": "204700Mi"},
                    "allocatable": {"cpu": str(int(cpu) - 0) if is_cp else f"{int(cpu) * 1000 - 200}m", "memory": f"{mem_gi * 1024 * 1024 - 500000}Ki", "pods": "110", "ephemeral-storage": "188650Mi"},
                    "conditions": conditions,
                    "addresses": [{"type": "InternalIP", "address": f"10.20.{self._cluster_octet()}.{10 + i}"}, {"type": "Hostname", "address": name}],
                    "nodeInfo": {
                        "kubeletVersion": "v1.35.2", "kubeProxyVersion": "v1.35.2", "osImage": "Ubuntu 22.04.4 LTS",
                        "containerRuntimeVersion": "containerd://1.7.24", "kernelVersion": "5.15.0-122-generic",
                        "operatingSystem": "linux", "architecture": "amd64",
                    },
                },
            }
            self._put("", "nodes", "", node)
        self.node_names = nodes
        self.worker_names = [n for n in nodes if "-w" in n] or nodes

        # Storage classes / PVs
        self._put("storage.k8s.io", "storageclasses", "", {
            "apiVersion": "storage.k8s.io/v1", "kind": "StorageClass",
            "metadata": self._meta("nfs-client", labels={}, age_days=200),
            "provisioner": "cluster.local/nfs-subdir-external-provisioner", "reclaimPolicy": "Delete",
            "volumeBindingMode": "Immediate",
        })

        namespaces = ["kube-system", "default", "monitoring", "ingress-nginx"]
        domains = list(_DOMAINS)
        rng.shuffle(domains)
        i = 0
        while len(namespaces) < spec["namespaces"]:
            base = domains[i % len(domains)]
            suffix = _ENV_SUFFIX[(i // len(domains)) % len(_ENV_SUFFIX)]
            namespaces.append(f"{base}{suffix}")
            i += 1
        self.namespace_names = namespaces
        for ns in namespaces:
            self._put("", "namespaces", "", {
                "apiVersion": "v1", "kind": "Namespace",
                "metadata": self._meta(ns, labels={"kubernetes.io/metadata.name": ns}),
                "spec": {"finalizers": ["kubernetes"]},
                "status": {"phase": "Active"},
            })
            self._put("", "serviceaccounts", ns, {"apiVersion": "v1", "kind": "ServiceAccount", "metadata": self._meta("default", ns)})

        # System daemonsets
        for ds_name, image in (("kube-proxy", "registry.k8s.io/kube-proxy:v1.31.4"), ("calico-node", "docker.io/calico/node:v3.28.2")):
            self._daemonset("kube-system", ds_name, image)
        self._daemonset("monitoring", "node-exporter", "quay.io/prometheus/node-exporter:v1.8.2")
        for name, image in (("coredns", "registry.k8s.io/coredns/coredns:v1.11.3"), ("metrics-server", "registry.k8s.io/metrics-server/metrics-server:v0.7.2")):
            self._deployment("kube-system", name, image, replicas=2 if name == "coredns" else 1)
        self._deployment("ingress-nginx", "ingress-nginx-controller", "registry.k8s.io/ingress-nginx/controller:v1.11.3", replicas=2, svc_type="LoadBalancer")
        self._deployment("monitoring", "prometheus", "quay.io/prometheus/prometheus:v2.54.1", replicas=1)
        self._deployment("monitoring", "grafana", "docker.io/grafana/grafana:11.2.0", replicas=1)

        for ns in namespaces[4:]:
            lo, hi = spec["deps"]
            comps = list(_COMPONENTS)
            rng.shuffle(comps)
            domain = ns.split("-")[0]
            for c in comps[: rng.randint(lo, hi)]:
                rlo, rhi = spec["replicas"]
                self._deployment(ns, f"{domain}-{c}", None, replicas=rng.randint(rlo, rhi),
                                 svc_type=rng.choice(["ClusterIP"] * 6 + ["NodePort"]),
                                 ingress=rng.random() < 0.25)
            if rng.random() < 0.35:
                self._statefulset(ns, f"{domain}-redis", "docker.io/bitnami/redis:7.2.5")
            if rng.random() < 0.3:
                self._cronjob(ns, f"{domain}-nightly-report")
            self._put("", "resourcequotas", ns, {"apiVersion": "v1", "kind": "ResourceQuota", "metadata": self._meta(f"{ns}-quota", ns),
                                                  "spec": {"hard": {"requests.cpu": "20", "requests.memory": "64Gi"}},
                                                  "status": {"hard": {"requests.cpu": "20", "requests.memory": "64Gi"}, "used": {"requests.cpu": "3", "requests.memory": "9Gi"}}})

        self._events_initial()

    def _cluster_octet(self) -> int:
        return (sum(ord(c) for c in self.name) % 200) + 20

    def _image(self, ns: str, name: str) -> str:
        tag = f"{self.rng.randint(1, 4)}.{self.rng.randint(0, 30)}.{self.rng.randint(0, 99)}"
        return f"nexus.areeba.local:8082/{ns}/{name}:{tag}"

    def _container(self, name: str, image: str, cm: Optional[str], secret: Optional[str]) -> Dict[str, Any]:
        rng = self.rng
        container: Dict[str, Any] = {
            "name": name,
            "image": image,
            "imagePullPolicy": "IfNotPresent",
            "ports": [{"containerPort": rng.choice([8080, 8080, 3000, 9090, 5000]), "protocol": "TCP"}],
            "resources": {
                "requests": {"cpu": rng.choice(["100m", "200m", "250m", "500m"]), "memory": rng.choice(["256Mi", "512Mi", "1Gi"])},
                "limits": {"cpu": rng.choice(["500m", "1", "2"]), "memory": rng.choice(["512Mi", "1Gi", "2Gi"])},
            },
            "env": [
                {"name": "SPRING_PROFILES_ACTIVE", "value": "k8s"},
                {"name": "JAVA_OPTS", "value": "-Xms256m -Xmx768m"},
                {"name": "LOG_LEVEL", "value": "INFO"},
            ],
        }
        env_from = []
        if cm:
            env_from.append({"configMapRef": {"name": cm}})
        if secret:
            env_from.append({"secretRef": {"name": secret}})
        if env_from:
            container["envFrom"] = env_from
        container["readinessProbe"] = {"httpGet": {"path": "/actuator/health", "port": container["ports"][0]["containerPort"]}, "periodSeconds": 10}
        return container

    def _pod_status(self, containers: List[Dict[str, Any]], started: datetime, node: str, *, force: Optional[str] = None) -> Dict[str, Any]:
        rng = self.rng
        roll = rng.random()
        kind = force or ("crash" if roll < 0.02 else "pending" if roll < 0.03 else "imagepull" if roll < 0.04 else "oom" if roll < 0.05 else "ok")
        statuses = []
        for c in containers:
            restarts = rng.choice([0, 0, 0, 0, 0, 1, 2])
            state: Dict[str, Any] = {"running": {"startedAt": _ts(started)}}
            last_state: Dict[str, Any] = {}
            ready = True
            if kind == "crash":
                restarts = rng.randint(15, 240)
                state = {"waiting": {"reason": "CrashLoopBackOff", "message": f"back-off 5m0s restarting failed container={c['name']}"}}
                last_state = {"terminated": {"exitCode": 1, "reason": "Error", "startedAt": _ts(_now() - timedelta(minutes=6)), "finishedAt": _ts(_now() - timedelta(minutes=5))}}
                ready = False
            elif kind == "imagepull":
                restarts = 0
                state = {"waiting": {"reason": "ImagePullBackOff", "message": f'Back-off pulling image "{c["image"]}"'}}
                ready = False
            elif kind == "oom":
                restarts = rng.randint(1, 9)
                last_state = {"terminated": {"exitCode": 137, "reason": "OOMKilled", "startedAt": _ts(_now() - timedelta(hours=3)), "finishedAt": _ts(_now() - timedelta(hours=2))}}
            elif kind == "pending":
                state = {"waiting": {"reason": "ContainerCreating"}}
                ready = False
            statuses.append({
                "name": c["name"], "image": c["image"], "imageID": f"{c['image'].split(':')[0]}@sha256:{rng.getrandbits(128):032x}",
                "containerID": f"containerd://{rng.getrandbits(128):032x}",
                "ready": ready, "started": ready, "restartCount": restarts,
                "state": state, "lastState": last_state,
            })
        phase = "Pending" if kind == "pending" and rng.random() < 0.5 else "Running"
        status: Dict[str, Any] = {
            "phase": phase,
            "hostIP": f"10.20.{self._cluster_octet()}.{10 + (self.node_names.index(node) if node in self.node_names else 0)}",
            "podIP": f"10.244.{rng.randint(0, 60)}.{rng.randint(2, 250)}",
            "startTime": _ts(started),
            "qosClass": "Burstable",
            "conditions": [
                {"type": "Initialized", "status": "True"},
                {"type": "Ready", "status": "True" if all(s["ready"] for s in statuses) else "False"},
                {"type": "ContainersReady", "status": "True" if all(s["ready"] for s in statuses) else "False"},
                {"type": "PodScheduled", "status": "True"},
            ],
            "containerStatuses": statuses,
        }
        if phase == "Pending":
            status["conditions"][-1] = {"type": "PodScheduled", "status": "False", "reason": "Unschedulable", "message": "0/12 nodes are available: insufficient memory."}
            status.pop("podIP", None)
        return status

    def _pods_for(self, ns: str, owner_kind: str, owner_name: str, pod_prefix: str, template: Dict[str, Any],
                  count: int, labels: Dict[str, str], *, node_names: Optional[List[str]] = None, ordinal: bool = False) -> None:
        rng = self.rng
        for idx in range(count):
            if ordinal:
                pod_name = f"{pod_prefix}-{idx}"
            else:
                pod_name = f"{pod_prefix}-{''.join(rng.choice('bcdfghjklmnpqrstvwxz2456789') for _ in range(5))}"
            node = node_names[idx] if node_names else rng.choice(self.worker_names)
            started = _now() - timedelta(hours=rng.uniform(0.1, 24 * 40))
            spec = copy.deepcopy(template["spec"])
            spec["nodeName"] = node
            pod = {
                "apiVersion": "v1", "kind": "Pod",
                "metadata": {
                    **self._meta(pod_name, ns, labels=dict(labels)),
                    "creationTimestamp": _ts(started),
                    "ownerReferences": [{"apiVersion": "apps/v1", "kind": owner_kind, "name": owner_name, "controller": True, "uid": f"{ns}-{owner_name}"}],
                },
                "spec": spec,
                "status": self._pod_status(spec["containers"], started, node),
            }
            self._put("", "pods", ns, pod)

    def _deployment(self, ns: str, name: str, image: Optional[str], *, replicas: int, svc_type: str = "ClusterIP", ingress: bool = False) -> None:
        rng = self.rng
        image = image or self._image(ns, name)
        labels = {"app": name, "app.kubernetes.io/name": name, "app.kubernetes.io/part-of": ns.split("-")[0]}
        cm_name = f"{name}-config"
        secret_name = f"{name}-secret"
        system = ns in ("kube-system", "monitoring", "ingress-nginx")
        if not system:
            self._put("", "configmaps", ns, {
                "apiVersion": "v1", "kind": "ConfigMap", "metadata": self._meta(cm_name, ns, labels={"app": name}),
                "data": {
                    "application.yaml": f"server:\n  port: 8080\nspring:\n  application:\n    name: {name}\n" + "\n".join(f"feature{k}: true" for k in range(rng.randint(5, 40))),
                    "DB_HOST": f"{ns}-postgres.{ns}.svc", "DB_POOL": str(rng.randint(5, 40)), "FEATURE_FLAGS": "a,b,c",
                },
            })
            self._put("", "secrets", ns, {
                "apiVersion": "v1", "kind": "Secret", "type": "Opaque", "metadata": self._meta(secret_name, ns, labels={"app": name}),
                "data": {"DB_PASSWORD": _b64("not-a-real-password"), "API_KEY": _b64("fake-key"), "JWT_SECRET": _b64("fake")},
            })
        container = self._container(name.split("-")[-1] if not system else name, image, None if system else cm_name, None if system else secret_name)
        template = {"metadata": {"labels": dict(labels)}, "spec": {"containers": [container], "restartPolicy": "Always", "serviceAccountName": "default"}}
        if not system and rng.random() < 0.3:
            template["spec"]["volumes"] = [{"name": "config", "configMap": {"name": cm_name}}]
        rev = rng.randint(2, 30)
        rs_hash = f"{rng.getrandbits(40):010x}"[:10]
        dep = {
            "apiVersion": "apps/v1", "kind": "Deployment",
            "metadata": {**self._meta(name, ns, labels=dict(labels)), "annotations": {"deployment.kubernetes.io/revision": str(rev)}, "generation": rev},
            "spec": {"replicas": replicas, "selector": {"matchLabels": {"app": name}}, "template": template,
                     "strategy": {"type": "RollingUpdate", "rollingUpdate": {"maxSurge": "25%", "maxUnavailable": "25%"}}, "revisionHistoryLimit": 10},
            "status": {},
        }
        rs_name = f"{name}-{rs_hash}"
        rs = {
            "apiVersion": "apps/v1", "kind": "ReplicaSet",
            "metadata": {**self._meta(rs_name, ns, labels={**labels, "pod-template-hash": rs_hash}),
                         "annotations": {"deployment.kubernetes.io/revision": str(rev)},
                         "ownerReferences": [{"apiVersion": "apps/v1", "kind": "Deployment", "name": name, "controller": True, "uid": f"{ns}-{name}"}]},
            "spec": {"replicas": replicas, "selector": {"matchLabels": {"app": name, "pod-template-hash": rs_hash}}, "template": template},
            "status": {"replicas": replicas, "readyReplicas": replicas, "availableReplicas": replicas},
        }
        old_hash = f"{rng.getrandbits(40):010x}"[:10]
        old_rs = copy.deepcopy(rs)
        old_rs["metadata"]["name"] = f"{name}-{old_hash}"
        old_rs["metadata"]["annotations"] = {"deployment.kubernetes.io/revision": str(rev - 1)}
        old_rs["spec"]["replicas"] = 0
        old_rs["status"] = {"replicas": 0}
        self._put("apps", "replicasets", ns, rs)
        self._put("apps", "replicasets", ns, old_rs)
        self._pods_for(ns, "ReplicaSet", rs_name, rs_name, template, replicas, {**labels, "pod-template-hash": rs_hash})
        self._put("apps", "deployments", ns, dep)
        self._refresh_deployment_status(ns, name)

        port = container["ports"][0]["containerPort"]
        svc: Dict[str, Any] = {
            "apiVersion": "v1", "kind": "Service", "metadata": self._meta(name, ns, labels=dict(labels)),
            "spec": {"type": svc_type, "selector": {"app": name}, "clusterIP": f"10.96.{rng.randint(0, 250)}.{rng.randint(2, 250)}",
                     "ports": [{"name": "http", "port": 80 if svc_type != "NodePort" else port, "targetPort": port, "protocol": "TCP",
                                **({"nodePort": rng.randint(30000, 32767)} if svc_type in ("NodePort", "LoadBalancer") else {})}]},
            "status": {"loadBalancer": {"ingress": [{"ip": f"10.20.{self._cluster_octet()}.200"}]} if svc_type == "LoadBalancer" else {}},
        }
        self._put("", "services", ns, svc)
        self._refresh_endpoints(ns, name)
        if ingress:
            host = f"{name}.{self.name}.areeba.local"
            self._put("networking.k8s.io", "ingresses", ns, {
                "apiVersion": "networking.k8s.io/v1", "kind": "Ingress", "metadata": self._meta(name, ns, labels={"app": name}),
                "spec": {"ingressClassName": "nginx", "rules": [{"host": host, "http": {"paths": [{"path": "/", "pathType": "Prefix", "backend": {"service": {"name": name, "port": {"number": 80}}}}]}}],
                         "tls": [{"hosts": [host], "secretName": f"{name}-tls"}]},
                "status": {"loadBalancer": {"ingress": [{"ip": f"10.20.{self._cluster_octet()}.200"}]}},
            })
        if not system and rng.random() < 0.2:
            self._put("autoscaling", "horizontalpodautoscalers", ns, {
                "apiVersion": "autoscaling/v2", "kind": "HorizontalPodAutoscaler", "metadata": self._meta(name, ns),
                "spec": {"scaleTargetRef": {"apiVersion": "apps/v1", "kind": "Deployment", "name": name}, "minReplicas": 1, "maxReplicas": 6,
                         "metrics": [{"type": "Resource", "resource": {"name": "cpu", "target": {"type": "Utilization", "averageUtilization": 70}}}]},
                "status": {"currentReplicas": replicas, "desiredReplicas": replicas},
            })

    def _refresh_deployment_status(self, ns: str, name: str) -> None:
        dep = self.objects[("apps", "deployments")][ns][name]
        pods = [p for p in self.objects.get(("", "pods"), {}).get(ns, {}).values()
                if p["metadata"]["labels"].get("app") == name]
        ready = sum(1 for p in pods if all(cs.get("ready") for cs in p["status"].get("containerStatuses", [])))
        replicas = dep["spec"]["replicas"]
        dep["status"] = {
            "observedGeneration": dep["metadata"].get("generation", 1),
            "replicas": replicas, "updatedReplicas": replicas, "readyReplicas": ready, "availableReplicas": ready,
            **({"unavailableReplicas": replicas - ready} if ready < replicas else {}),
            "conditions": [
                {"type": "Available", "status": "True" if ready >= 1 else "False", "reason": "MinimumReplicasAvailable" if ready >= 1 else "MinimumReplicasUnavailable",
                 "lastUpdateTime": _ts(_now() - timedelta(hours=2)), "lastTransitionTime": _ts(_now() - timedelta(hours=2))},
                {"type": "Progressing", "status": "True", "reason": "NewReplicaSetAvailable",
                 "lastUpdateTime": _ts(_now() - timedelta(hours=2)), "lastTransitionTime": _ts(_now() - timedelta(days=3))},
            ],
        }

    def _refresh_endpoints(self, ns: str, name: str) -> None:
        pods = [p for p in self.objects.get(("", "pods"), {}).get(ns, {}).values()
                if p["metadata"]["labels"].get("app") == name]
        svc = self.objects.get(("", "services"), {}).get(ns, {}).get(name)
        if not svc:
            return
        port = svc["spec"]["ports"][0]["targetPort"]
        ready, not_ready = [], []
        for p in pods:
            if not p["status"].get("podIP"):
                continue
            addr = {"ip": p["status"]["podIP"], "nodeName": p["spec"]["nodeName"],
                    "targetRef": {"kind": "Pod", "name": p["metadata"]["name"], "namespace": ns}}
            (ready if all(cs.get("ready") for cs in p["status"].get("containerStatuses", [])) else not_ready).append(addr)
        subset: Dict[str, Any] = {"ports": [{"name": "http", "port": port, "protocol": "TCP"}]}
        if ready:
            subset["addresses"] = ready
        if not_ready:
            subset["notReadyAddresses"] = not_ready
        self._put("", "endpoints", ns, {"apiVersion": "v1", "kind": "Endpoints", "metadata": self._meta(name, ns, labels={"app": name}),
                                        "subsets": [subset] if (ready or not_ready) else []})

    def _daemonset(self, ns: str, name: str, image: str) -> None:
        labels = {"k8s-app": name, "app": name}
        container = self._container(name, image, None, None)
        template = {"metadata": {"labels": dict(labels)}, "spec": {"containers": [container], "hostNetwork": True}}
        n = len(self.node_names)
        self._put("apps", "daemonsets", ns, {
            "apiVersion": "apps/v1", "kind": "DaemonSet", "metadata": self._meta(name, ns, labels=dict(labels), age_days=(_now() - self.created).days),
            "spec": {"selector": {"matchLabels": {"k8s-app": name}}, "template": template},
            "status": {"currentNumberScheduled": n, "desiredNumberScheduled": n, "numberReady": n, "numberAvailable": n, "updatedNumberScheduled": n},
        })
        self._pods_for(ns, "DaemonSet", name, name, template, n, labels, node_names=self.node_names)

    def _statefulset(self, ns: str, name: str, image: str) -> None:
        labels = {"app": name, "app.kubernetes.io/name": name}
        container = self._container(name.split("-")[-1], image, None, None)
        container["volumeMounts"] = [{"name": "data", "mountPath": "/data"}]
        template = {"metadata": {"labels": dict(labels)}, "spec": {"containers": [container]}}
        replicas = self.rng.choice([1, 3])
        self._put("apps", "statefulsets", ns, {
            "apiVersion": "apps/v1", "kind": "StatefulSet", "metadata": self._meta(name, ns, labels=dict(labels)),
            "spec": {"replicas": replicas, "serviceName": name, "selector": {"matchLabels": {"app": name}}, "template": template,
                     "volumeClaimTemplates": [{"metadata": {"name": "data"}, "spec": {"accessModes": ["ReadWriteOnce"], "resources": {"requests": {"storage": "8Gi"}}}}]},
            "status": {"replicas": replicas, "readyReplicas": replicas, "currentReplicas": replicas, "updatedReplicas": replicas, "availableReplicas": replicas},
        })
        self._pods_for(ns, "StatefulSet", name, name, template, replicas, labels, ordinal=True)
        for i in range(replicas):
            pvc_name = f"data-{name}-{i}"
            pv_name = f"pvc-{self.rng.getrandbits(64):016x}"
            self._put("", "persistentvolumeclaims", ns, {
                "apiVersion": "v1", "kind": "PersistentVolumeClaim", "metadata": self._meta(pvc_name, ns, labels={"app": name}),
                "spec": {"accessModes": ["ReadWriteOnce"], "storageClassName": "nfs-client", "volumeName": pv_name, "resources": {"requests": {"storage": "8Gi"}}},
                "status": {"phase": "Bound", "capacity": {"storage": "8Gi"}, "accessModes": ["ReadWriteOnce"]},
            })
            self._put("", "persistentvolumes", "", {
                "apiVersion": "v1", "kind": "PersistentVolume", "metadata": self._meta(pv_name),
                "spec": {"capacity": {"storage": "8Gi"}, "accessModes": ["ReadWriteOnce"], "persistentVolumeReclaimPolicy": "Delete",
                         "storageClassName": "nfs-client", "claimRef": {"namespace": ns, "name": pvc_name},
                         "nfs": {"server": "10.20.1.5", "path": f"/exports/{ns}-{pvc_name}"}},
                "status": {"phase": "Bound"},
            })
        self._put("", "services", ns, {"apiVersion": "v1", "kind": "Service", "metadata": self._meta(name, ns, labels=dict(labels)),
                                       "spec": {"clusterIP": "None", "selector": {"app": name}, "ports": [{"port": 6379, "targetPort": 6379}], "type": "ClusterIP"}})
        self._refresh_endpoints(ns, name)

    def _cronjob(self, ns: str, name: str) -> None:
        image = self._image(ns, name)
        container = self._container("report", image, None, None)
        job_template = {"spec": {"template": {"spec": {"containers": [container], "restartPolicy": "OnFailure"}}, "backoffLimit": 2}}
        self._put("batch", "cronjobs", ns, {
            "apiVersion": "batch/v1", "kind": "CronJob", "metadata": self._meta(name, ns),
            "spec": {"schedule": "0 2 * * *", "suspend": False, "jobTemplate": job_template, "successfulJobsHistoryLimit": 3},
            "status": {"lastScheduleTime": _ts(_now() - timedelta(hours=self.rng.randint(1, 23))), "lastSuccessfulTime": _ts(_now() - timedelta(hours=self.rng.randint(1, 23)))},
        })
        for d in range(3):
            job_name = f"{name}-{29000000 + d * 1440 + self.rng.randint(0, 99)}"
            start = _now() - timedelta(days=d, hours=self.rng.randint(1, 3))
            failed = self.rng.random() < 0.1
            self._put("batch", "jobs", ns, {
                "apiVersion": "batch/v1", "kind": "Job",
                "metadata": {**self._meta(job_name, ns, labels={"job-name": job_name}),
                             "ownerReferences": [{"apiVersion": "batch/v1", "kind": "CronJob", "name": name, "controller": True}]},
                "spec": {**job_template["spec"], "completions": 1, "parallelism": 1},
                "status": {"startTime": _ts(start), **({"failed": 1} if failed else {"succeeded": 1, "completionTime": _ts(start + timedelta(minutes=4))}),
                           "conditions": [{"type": "Failed" if failed else "Complete", "status": "True"}]},
            })

    def _event(self, ns: str, involved: Dict[str, Any], etype: str, reason: str, message: str, age_s: float, count: int = 1) -> None:
        name = f"{involved['metadata']['name']}.{self.rng.getrandbits(48):012x}"
        when = _now() - timedelta(seconds=age_s)
        self._put("", "events", ns, {
            "apiVersion": "v1", "kind": "Event",
            "metadata": {"name": name, "namespace": ns, "creationTimestamp": _ts(when)},
            "involvedObject": {"kind": involved["kind"], "name": involved["metadata"]["name"], "namespace": ns, "apiVersion": involved.get("apiVersion", "v1")},
            "reason": reason, "message": message, "type": etype, "count": count,
            "firstTimestamp": _ts(when - timedelta(minutes=count)), "lastTimestamp": _ts(when),
            "source": {"component": "kubelet"},
        })

    def _events_initial(self) -> None:
        for ns, pods in list(self.objects.get(("", "pods"), {}).items()):
            for pod in list(pods.values()):
                reason = (pod["status"]["containerStatuses"][0]["state"].get("waiting") or {}).get("reason")
                if reason == "CrashLoopBackOff":
                    self._event(ns, pod, "Warning", "BackOff", "Back-off restarting failed container", self.rng.uniform(5, 300), count=self.rng.randint(20, 400))
                elif reason == "ImagePullBackOff":
                    self._event(ns, pod, "Warning", "Failed", f"Failed to pull image: not found", self.rng.uniform(5, 600), count=self.rng.randint(3, 50))
                elif self.rng.random() < 0.15:
                    self._event(ns, pod, "Normal", "Pulled", "Container image already present on machine", self.rng.uniform(60, 3000))

    # -- churn ---------------------------------------------------------------

    def churn(self) -> None:
        """Small live changes: a restart, a crash, a recovery. Keeps lists moving."""
        with self.lock:
            pods_by_ns = self.objects.get(("", "pods"), {})
            namespaces = [ns for ns in pods_by_ns if pods_by_ns[ns]]
            for _ in range(max(1, len(namespaces) // 15)):
                ns = self.rng.choice(namespaces)
                pod = self.rng.choice(list(pods_by_ns[ns].values()))
                cs = pod["status"]["containerStatuses"][0]
                roll = self.rng.random()
                if roll < 0.5:
                    cs["restartCount"] += 1
                    self._event(ns, pod, "Warning", "BackOff", "Back-off restarting failed container", 1)
                elif roll < 0.75 and cs["ready"]:
                    cs["ready"] = False
                    cs["state"] = {"waiting": {"reason": "CrashLoopBackOff", "message": "back-off 10s restarting failed container"}}
                    cs["restartCount"] += 1
                    pod["status"]["conditions"][1]["status"] = "False"
                    self._event(ns, pod, "Warning", "BackOff", "Back-off restarting failed container", 1)
                else:
                    cs["ready"] = True
                    cs["state"] = {"running": {"startedAt": _ts(_now())}}
                    pod["status"]["conditions"][1]["status"] = "True"
                    self._event(ns, pod, "Normal", "Started", "Started container", 1)
                self.version_counter += 1
                pod["metadata"]["resourceVersion"] = str(self.version_counter)
                app = pod["metadata"]["labels"].get("app")
                if app and app in self.objects.get(("apps", "deployments"), {}).get(ns, {}):
                    self._refresh_deployment_status(ns, app)
                if app:
                    self._refresh_endpoints(ns, app)
            # Bound event growth like the apiserver's TTL does.
            events = self.objects.get(("", "events"), {})
            for ns, items in events.items():
                if len(items) > 60:
                    oldest = sorted(items.values(), key=lambda e: e["lastTimestamp"])[: len(items) - 60]
                    for e in oldest:
                        items.pop(e["metadata"]["name"], None)
            self._list_cache.clear()

    def set_crash(self, ns: str, pod_name: str, crashing: bool) -> bool:
        """Put one pod into (or out of) CrashLoopBackOff, like the churn does."""
        with self.lock:
            pod = self.objects.get(("", "pods"), {}).get(ns, {}).get(pod_name)
            if not pod:
                return False
            cs = pod["status"]["containerStatuses"][0]
            if crashing:
                cs["ready"] = False
                cs["state"] = {"waiting": {"reason": "CrashLoopBackOff", "message": "back-off 10s restarting failed container"}}
                cs["restartCount"] += 1
                pod["status"]["conditions"][1]["status"] = "False"
            else:
                cs["ready"] = True
                cs["state"] = {"running": {"startedAt": _ts(_now())}}
                pod["status"]["conditions"][1]["status"] = "True"
            self.version_counter += 1
            pod["metadata"]["resourceVersion"] = str(self.version_counter)
            app = pod["metadata"]["labels"].get("app")
            if app and app in self.objects.get(("apps", "deployments"), {}).get(ns, {}):
                self._refresh_deployment_status(ns, app)
            if app:
                self._refresh_endpoints(ns, app)
            self._list_cache.clear()
            return True

    def set_marker(self, ns: str, pod_name: str, value: str) -> bool:
        with self.lock:
            pod = self.objects.get(("", "pods"), {}).get(ns, {}).get(pod_name)
            if not pod:
                return False
            pod["metadata"].setdefault("labels", {})["stress-marker"] = value
            self.version_counter += 1
            pod["metadata"]["resourceVersion"] = str(self.version_counter)
            self._list_cache.clear()
            return True

    # -- reads ---------------------------------------------------------------

    def list_items(self, group: str, resource: str, namespace: str) -> List[Dict[str, Any]]:
        if group == "metrics.k8s.io":
            return self._metrics(resource, namespace)
        by_ns = self.objects.get((group, resource), {})
        if not _NAMESPACED.get((group, resource), True):
            return list(by_ns.get("", {}).values())
        if namespace:
            return list(by_ns.get(namespace, {}).values())
        out: List[Dict[str, Any]] = []
        for items in by_ns.values():
            out.extend(items.values())
        return out

    def _metrics(self, resource: str, namespace: str) -> List[Dict[str, Any]]:
        rng = random.Random(int(time.time() // 15))  # metrics move every 15s
        now = _ts(_now())
        if resource == "nodes":
            out = []
            for n in self.node_names:
                cap = self.objects[("", "nodes")][""][n]["status"]["capacity"]
                cores = int(cap["cpu"])
                mem_ki = int(cap["memory"].rstrip("Ki"))
                out.append({"metadata": {"name": n}, "timestamp": now, "window": "15s",
                            "usage": {"cpu": f"{int(cores * 1000 * rng.uniform(0.08, 0.85))}m",
                                      "memory": f"{int(mem_ki * rng.uniform(0.2, 0.9))}Ki"}})
            return out
        pods = self.list_items("", "pods", namespace)
        return [{"metadata": {"name": p["metadata"]["name"], "namespace": p["metadata"]["namespace"]}, "timestamp": now, "window": "15s",
                 "containers": [{"name": c["name"], "usage": {"cpu": f"{rng.randint(1, 600)}m", "memory": f"{rng.randint(60, 1500)}Mi"}}
                                for c in p["spec"]["containers"]]}
                for p in pods if p["status"].get("phase") == "Running"]

    def get_item(self, group: str, resource: str, namespace: str, name: str) -> Optional[Dict[str, Any]]:
        for item in self.list_items(group, resource, namespace if _NAMESPACED.get((group, resource), True) else ""):
            if item["metadata"]["name"] == name:
                return item
        return None

    def node_summary(self, node: str) -> Dict[str, Any]:
        rng = random.Random(hash((node, int(time.time() // 30))))
        cap = 214_643_507_200
        pods = [p for p in self.list_items("", "pods", "") if p["spec"].get("nodeName") == node]
        pod_entries = []
        for p in pods:
            entry: Dict[str, Any] = {"podRef": {"name": p["metadata"]["name"], "namespace": p["metadata"]["namespace"], "uid": p["metadata"]["uid"]},
                                     "ephemeral-storage": {"usedBytes": rng.randint(10, 900) * 1024 * 1024, "capacityBytes": cap}}
            vols = []
            for v in p["spec"]["containers"][0].get("volumeMounts", []) or []:
                if v["name"] == "data":
                    ordinal = p["metadata"]["name"].rsplit("-", 1)[-1]
                    owner = p["metadata"]["ownerReferences"][0]["name"]
                    vols.append({"name": "data", "usedBytes": rng.randint(100, 7000) * 1024 * 1024, "capacityBytes": 8 * 1024 ** 3,
                                 "pvcRef": {"name": f"data-{owner}-{ordinal}", "namespace": p["metadata"]["namespace"]}})
            if vols:
                entry["volume"] = vols
            pod_entries.append(entry)
        used = rng.randint(20, 85) * cap // 100
        return {
            "node": {"nodeName": node, "fs": {"usedBytes": used, "capacityBytes": cap, "availableBytes": cap - used},
                     "runtime": {"imageFs": {"usedBytes": used // 2, "capacityBytes": cap, "availableBytes": cap - used // 2}}},
            "pods": pod_entries,
        }


# --------------------------------------------------------------------------
# HTTP layer
# --------------------------------------------------------------------------


def _match_selector(item: Dict[str, Any], label_sel: str, field_sel: str) -> bool:
    if label_sel:
        labels = (item.get("metadata") or {}).get("labels") or {}
        for part in label_sel.split(","):
            part = part.strip()
            if not part:
                continue
            if "!=" in part:
                k, v = part.split("!=", 1)
                if labels.get(k.strip()) == v.strip():
                    return False
            elif "=" in part:
                k, v = part.replace("==", "=").split("=", 1)
                if labels.get(k.strip()) != v.strip():
                    return False
            elif part.startswith("!"):
                if part[1:] in labels:
                    return False
            elif part not in labels:
                return False
    if field_sel:
        for part in field_sel.split(","):
            part = part.strip()
            if not part:
                continue
            negate = "!=" in part
            k, v = (part.split("!=", 1) if negate else part.replace("==", "=").split("=", 1))
            cur: Any = item
            for seg in k.strip().split("."):
                cur = cur.get(seg) if isinstance(cur, dict) else None
            matches = str(cur if cur is not None else "") == v.strip()
            if matches == negate:
                return False
    return True


def make_handler(cluster: FakeCluster, base_latency: Tuple[int, int]):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"
        server_version = "fake-kube-apiserver/1.31"

        def log_message(self, fmt, *args):  # quiet
            pass

        # ---- helpers
        def _send(self, code: int, body: Any, content_type: str = "application/json") -> None:
            data = body if isinstance(body, (bytes, bytearray)) else json.dumps(body, separators=(",", ":")).encode()
            self.send_response(code)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Audit-Id", f"{random.getrandbits(64):016x}")
            self.end_headers()
            try:
                self.wfile.write(data)
            except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                pass

        def _status(self, code: int, reason: str, message: str) -> None:
            self._send(code, {"kind": "Status", "apiVersion": "v1", "metadata": {}, "status": "Failure",
                              "message": message, "reason": reason, "code": code})

        def _delay(self, items: int = 0) -> bool:
            """Sleep like an apiserver would. Returns False if the request should fail."""
            mode = cluster.mode
            if mode == "down":
                # Connection refused is closest to closing without a response.
                self.close_connection = True
                try:
                    self.connection.shutdown(2)
                except OSError:
                    pass
                return False
            if mode == "hang":
                time.sleep(25)
                self.close_connection = True
                return False
            lo, hi = base_latency
            ms = random.uniform(lo, hi) + items * _MS_PER_ITEM
            if mode == "slow":
                ms = ms * 6 + 800
            if random.random() < _SLOW_RATE:
                ms *= 8
            time.sleep(ms / 1000.0)
            if random.random() < _ERROR_RATE:
                self._status(500, "InternalError", "etcdserver: request timed out")
                return False
            return True

        # ---- verbs
        def do_GET(self):  # noqa: N802
            parsed = urlparse(self.path)
            path = parsed.path.rstrip("/") or "/"
            qs = parse_qs(parsed.query)
            if path.startswith("/__control"):
                return self._control_get(path)
            cluster.stats[_path_kind(path)] += 1

            if path in ("/version",):
                if not self._delay():
                    return
                return self._send(200, {"major": "1", "minor": "35", "gitVersion": "v1.35.2", "gitCommit": "a78aa47129b8539636eb86a9d00e31b2720fe06b",
                                        "gitTreeState": "clean", "buildDate": "2024-12-10T18:05:47Z", "goVersion": "go1.22.9",
                                        "compiler": "gc", "platform": "linux/amd64"})
            if path in ("/healthz", "/readyz", "/livez") or path.startswith("/readyz/"):
                return self._send(200, b"ok", "text/plain")
            if path == "/api":
                return self._send(200, {"kind": "APIVersions", "versions": ["v1"],
                                        "serverAddressByClientCIDRs": [{"clientCIDR": "0.0.0.0/0", "serverAddress": "127.0.0.1:6443"}]})
            if path == "/apis":
                return self._send(200, _api_group_list())
            if path.startswith("/openapi"):
                return self._status(404, "NotFound", "openapi not served by fake")

            parts = path.strip("/").split("/")
            # /api/v1/... or /apis/<group>/<version>/...
            if parts[0] == "api" and len(parts) >= 2:
                group, version, rest = "", parts[1], parts[2:]
            elif parts[0] == "apis" and len(parts) >= 3:
                group, version, rest = parts[1], parts[2], parts[3:]
            else:
                return self._status(404, "NotFound", f"the server could not find the requested resource ({path})")
            if not rest:
                return self._send(200, _api_resource_list(group, version))

            namespace = ""
            if rest[0] == "namespaces" and len(rest) >= 3:
                namespace, rest = rest[1], rest[2:]
            elif rest[0] == "namespaces" and len(rest) == 2:
                # GET /api/v1/namespaces/<name>
                return self._get_one(group, "namespaces", "", rest[1])
            resource = rest[0]
            if (group, resource) not in _KIND_BY_RESOURCE:
                return self._status(404, "NotFound", f"the server could not find the requested resource")
            if len(rest) == 1:
                return self._list(group, version, resource, namespace, qs)
            name = rest[1]
            sub = rest[2:] if len(rest) > 2 else []
            if resource == "nodes" and sub[:1] == ["proxy"]:
                if not self._delay(50):
                    return
                if "stats/summary" in "/".join(sub):
                    with cluster.lock:
                        return self._send(200, cluster.node_summary(name))
                return self._status(404, "NotFound", "proxy path not simulated")
            if resource == "pods" and sub[:1] == ["log"]:
                return self._logs(namespace, name, qs)
            if sub:
                return self._status(404, "NotFound", f"subresource {'/'.join(sub)} not simulated")
            return self._get_one(group, resource, namespace, name)

        def _get_one(self, group: str, resource: str, namespace: str, name: str):
            with cluster.lock:
                item = cluster.get_item(group, resource, namespace, name)
                body = json.dumps(item, separators=(",", ":")).encode() if item else None
            if not self._delay(1):
                return
            if body is None:
                kind = _KIND_BY_RESOURCE.get((group, resource), resource)
                return self._status(404, "NotFound", f'{resource}{"." + group if group else ""} "{name}" not found')
            return self._send(200, body)

        def _list(self, group: str, version: str, resource: str, namespace: str, qs: Dict[str, List[str]]):
            if qs.get("watch", ["false"])[0] in ("true", "1"):
                return self._status(405, "MethodNotAllowed", "watch not simulated")
            label_sel = qs.get("labelSelector", [""])[0]
            field_sel = qs.get("fieldSelector", [""])[0]
            limit = int(qs.get("limit", ["0"])[0] or 0)
            offset = int(qs.get("continue", ["0"])[0] or 0)
            # Serialised bodies are reused until the data changes (churn and
            # control writes clear the cache), so the fake API server's own CPU
            # stays out of the measurements. Metrics move with time: not cached.
            cache_key = (group, version, resource, namespace, label_sel, field_sel, limit, offset)
            if group != "metrics.k8s.io":
                with cluster.lock:
                    cached = cluster._list_cache.get(cache_key)
                if cached is not None:
                    page_len, body = cached
                    if not self._delay(page_len):
                        return
                    return self._send(200, body)
            with cluster.lock:
                items = cluster.list_items(group, resource, namespace)
                if label_sel or field_sel:
                    items = [i for i in items if _match_selector(i, label_sel, field_sel)]
                total = len(items)
                page = items[offset: offset + limit] if limit else items[offset:]
                kind = _KIND_BY_RESOURCE[(group, resource)]
                api_version = f"{group}/{version}" if group else version
                meta: Dict[str, Any] = {"resourceVersion": str(cluster.version_counter)}
                if limit and offset + limit < total:
                    meta["continue"] = str(offset + limit)
                    meta["remainingItemCount"] = total - offset - limit
                body = json.dumps({"kind": f"{kind}List", "apiVersion": api_version, "metadata": meta,
                                   "items": [{**i, "apiVersion": i.get("apiVersion", api_version), "kind": i.get("kind", kind)} for i in page]},
                                  separators=(",", ":")).encode()
                if group != "metrics.k8s.io":
                    cluster._list_cache[cache_key] = (len(page), body)
            if not self._delay(len(page)):
                return
            self._send(200, body)

        def _logs(self, namespace: str, pod: str, qs: Dict[str, List[str]]):
            with cluster.lock:
                item = cluster.get_item("", "pods", namespace, pod)
            if not self._delay(20):
                return
            if not item:
                return self._status(404, "NotFound", f'pods "{pod}" not found')
            tail = int(qs.get("tailLines", ["500"])[0] or 500)
            tail = min(tail, 5000)
            rng = random.Random(pod)
            base = _now() - timedelta(seconds=tail * 2)
            levels = ["INFO"] * 12 + ["DEBUG"] * 3 + ["WARN", "ERROR"]
            lines = []
            for i in range(tail):
                ts = base + timedelta(seconds=i * 2)
                lvl = rng.choice(levels)
                lines.append(f"{ts.strftime('%Y-%m-%dT%H:%M:%S.%f')[:-3]}Z {lvl:5} [{pod}] c.a.{rng.choice(_COMPONENTS)}.Service - "
                             f"processed request id={rng.getrandbits(32):08x} latency={rng.randint(2, 900)}ms status={rng.choice([200, 200, 200, 201, 400, 500])}")
            if qs.get("follow", ["false"])[0] in ("true", "1"):
                return self._send(200, ("\n".join(lines) + "\n").encode(), "text/plain")
            return self._send(200, ("\n".join(lines) + "\n").encode(), "text/plain")

        def _write(self):
            """Every write is refused; only the control endpoints accept a body."""
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(length) if length else b""
            parsed = urlparse(self.path)
            if parsed.path.startswith("/__control"):
                try:
                    self._body = json.loads(raw or b"{}")
                except ValueError:
                    self._body = {}
                return self._control_post(parsed.path)
            cluster.stats["write-refused"] += 1
            self._status(403, "Forbidden", "fake cluster is read-only: writes are refused by the stress-test API server")

        do_POST = do_PUT = do_PATCH = do_DELETE = _write

        # ---- control
        def _control_get(self, path: str):
            if path.endswith("/stats"):
                return self._send(200, {"cluster": cluster.name, "mode": cluster.mode, "requests": dict(cluster.stats)})
            return self._send(200, {"cluster": cluster.name, "mode": cluster.mode,
                                    "namespaces": len(cluster.namespace_names), "nodes": len(cluster.node_names),
                                    "pods": len(cluster.list_items("", "pods", "")),
                                    "deployments": len(cluster.list_items("apps", "deployments", ""))})

        def _control_post(self, path: str):
            body = getattr(self, "_body", {}) or {}
            if path.endswith("/mode"):
                cluster.mode = body.get("mode", "normal")
                return self._send(200, {"cluster": cluster.name, "mode": cluster.mode})
            if path.endswith("/marker"):
                ok = cluster.set_marker(body.get("namespace", ""), body.get("pod", ""), body.get("value", ""))
                return self._send(200 if ok else 404, {"ok": ok})
            if path.endswith("/crash"):
                ok = cluster.set_crash(body.get("namespace", ""), body.get("pod", ""), bool(body.get("crashing", True)))
                return self._send(200 if ok else 404, {"ok": ok})
            if path.endswith("/reset-stats"):
                cluster.stats.clear()
                return self._send(200, {"ok": True})
            return self._send(404, {"error": "unknown control path"})

    return Handler


def _path_kind(path: str) -> str:
    parts = [p for p in path.strip("/").split("/") if p]
    if not parts:
        return "root"
    if parts[0] in ("api", "apis") and len(parts) <= 3 and not (parts[0] == "api" and len(parts) == 3):
        return "discovery"
    if "proxy" in parts:
        return "node-proxy"
    if parts[-1] == "log":
        return "logs"
    resource = parts[-1]
    if "namespaces" in parts:
        idx = parts.index("namespaces")
        if len(parts) > idx + 2:
            resource = parts[idx + 2]
    return f"{'metrics:' if 'metrics.k8s.io' in parts else ''}{resource}"


class _Server(ThreadingHTTPServer):
    daemon_threads = True
    request_queue_size = 256


def write_kubeconfigs(clusters: List[Tuple[FakeCluster, int]], out_dir: str) -> List[str]:
    import os

    os.makedirs(out_dir, exist_ok=True)
    paths = []
    for cluster, port in clusters:
        doc = (
            "apiVersion: v1\nkind: Config\n"
            f"clusters:\n- name: {cluster.name}\n  cluster:\n    server: http://127.0.0.1:{port}\n"
            f"contexts:\n- name: {cluster.name}\n  context:\n    cluster: {cluster.name}\n    user: stress\n"
            f"current-context: {cluster.name}\n"
            "users:\n- name: stress\n  user:\n    token: stress-test-token\n"
        )
        path = os.path.join(out_dir, f"{cluster.name}.yaml")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(doc)
        paths.append(path)
    return paths


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--base-port", type=int, default=7101)
    parser.add_argument("--profile", default="standard", choices=sorted(PROFILES))
    parser.add_argument("--kubeconfig-dir", default="stress-kubeconfigs")
    parser.add_argument("--churn-seconds", type=float, default=5.0)
    parser.add_argument("--latency-scale", type=float, default=1.0, help="multiply every cluster's latency band")
    parser.add_argument("--only-index", type=int, default=None,
                        help="serve only this cluster of the profile (run one process per cluster to avoid sharing a GIL)")
    args = parser.parse_args()

    servers = []
    clusters: List[Tuple[FakeCluster, int]] = []
    for idx, spec in enumerate(PROFILES[args.profile]):
        if args.only_index is not None and idx != args.only_index:
            continue
        cluster = FakeCluster(spec, seed=1000 + idx)
        port = args.base_port + idx
        lo, hi = spec["latency_ms"]
        handler = make_handler(cluster, (lo * args.latency_scale, hi * args.latency_scale))
        server = _Server(("127.0.0.1", port), handler)
        threading.Thread(target=server.serve_forever, daemon=True, name=f"api-{cluster.name}").start()
        servers.append(server)
        clusters.append((cluster, port))
        print(f"[fake-k8s] {cluster.name:12} http://127.0.0.1:{port}  nodes={len(cluster.node_names)} "
              f"namespaces={len(cluster.namespace_names)} pods={len(cluster.list_items('', 'pods', ''))} "
              f"deployments={len(cluster.list_items('apps', 'deployments', ''))}", flush=True)
    paths = write_kubeconfigs(clusters, args.kubeconfig_dir)
    print(f"[fake-k8s] kubeconfigs: {', '.join(paths)}", flush=True)

    def churn_loop():
        while True:
            time.sleep(args.churn_seconds)
            for cluster, _ in clusters:
                try:
                    cluster.churn()
                except Exception as exc:  # pragma: no cover - keep the loop alive
                    print(f"[fake-k8s] churn error on {cluster.name}: {exc}", flush=True)

    threading.Thread(target=churn_loop, daemon=True, name="churn").start()
    print("[fake-k8s] ready", flush=True)
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        for s in servers:
            s.shutdown()


if __name__ == "__main__":
    main()
