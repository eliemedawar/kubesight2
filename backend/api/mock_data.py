from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List


def _iso_now(offset_minutes: int = 0) -> str:
    dt = datetime.now(timezone.utc) + timedelta(minutes=offset_minutes)
    return dt.isoformat()


USERS = {
    "admin": {
        "password": "admin123",
        "name": "Cluster Admin",
        "roles": ["admin", "viewer"],
    },
    "viewer": {
        "password": "viewer123",
        "name": "Read Only User",
        "roles": ["viewer"],
    },
}

CLUSTERS: List[Dict[str, Any]] = [
    {
        "id": "prod-us-east",
        "name": "Production US-East",
        "provider": "aws",
        "region": "us-east-1",
        "status": "healthy",
        "k8sVersion": "v1.30.2",
        "nodes": 18,
        "lastSync": _iso_now(-1),
    },
    {
        "id": "staging-eu-west",
        "name": "Staging EU-West",
        "provider": "gcp",
        "region": "europe-west1",
        "status": "warning",
        "k8sVersion": "v1.29.9",
        "nodes": 9,
        "lastSync": _iso_now(-3),
    },
]

CLUSTER_OVERVIEWS: Dict[str, Dict[str, Any]] = {
    "prod-us-east": {
        "clusterId": "prod-us-east",
        "healthScore": 97,
        "workloads": {"deployments": 143, "statefulsets": 16, "daemonsets": 8},
        "resources": {
            "cpu": {"usedCores": 191, "capacityCores": 256},
            "memory": {"usedGiB": 612, "capacityGiB": 768},
            "storage": {"usedGiB": 14200, "capacityGiB": 20000},
        },
        "pods": {"running": 1298, "pending": 7, "failed": 3},
        "updatedAt": _iso_now(-1),
    },
    "staging-eu-west": {
        "clusterId": "staging-eu-west",
        "healthScore": 83,
        "workloads": {"deployments": 61, "statefulsets": 8, "daemonsets": 5},
        "resources": {
            "cpu": {"usedCores": 54, "capacityCores": 96},
            "memory": {"usedGiB": 162, "capacityGiB": 320},
            "storage": {"usedGiB": 2800, "capacityGiB": 7000},
        },
        "pods": {"running": 402, "pending": 5, "failed": 11},
        "updatedAt": _iso_now(-2),
    },
}

NAMESPACES: Dict[str, List[Dict[str, Any]]] = {
    "prod-us-east": [
        {
            "name": "kube-system",
            "pods": 124,
            "status": "active",
            "cpuUsageCores": 42.8,
            "cpuLimitCores": 64,
            "memoryUsageGiB": 123.4,
            "memoryLimitGiB": 192,
        },
        {
            "name": "payments",
            "pods": 212,
            "status": "active",
            "cpuUsageCores": 88.2,
            "cpuLimitCores": 120,
            "memoryUsageGiB": 284.6,
            "memoryLimitGiB": 384,
        },
        {
            "name": "checkout",
            "pods": 144,
            "status": "active",
            "cpuUsageCores": 51.1,
            "cpuLimitCores": 96,
            "memoryUsageGiB": 176.2,
            "memoryLimitGiB": 256,
        },
    ],
    "staging-eu-west": [
        {
            "name": "kube-system",
            "pods": 63,
            "status": "active",
            "cpuUsageCores": 16.4,
            "cpuLimitCores": 32,
            "memoryUsageGiB": 61.9,
            "memoryLimitGiB": 96,
        },
        {
            "name": "sandbox",
            "pods": 57,
            "status": "active",
            "cpuUsageCores": 19.7,
            "cpuLimitCores": 40,
            "memoryUsageGiB": 54.8,
            "memoryLimitGiB": 96,
        },
        {
            "name": "internal-tools",
            "pods": 42,
            "status": "active",
            "cpuUsageCores": 11.2,
            "cpuLimitCores": 24,
            "memoryUsageGiB": 45.1,
            "memoryLimitGiB": 80,
        },
    ],
}

def _mock_event(
    *,
    event_type: str,
    reason: str,
    message: str,
    involved_kind: str,
    involved_name: str,
    component: str,
    minutes_ago: int,
    count: int = 1,
) -> Dict[str, Any]:
    last = _iso_now(-minutes_ago)
    first = _iso_now(-(minutes_ago + 30))
    return {
        "type": event_type,
        "reason": reason,
        "message": message,
        "involvedKind": involved_kind,
        "involvedName": involved_name,
        "count": count,
        "firstTimestamp": first,
        "lastTimestamp": last,
        "age": f"{minutes_ago}m" if minutes_ago < 60 else f"{minutes_ago // 60}h",
        "source": {"component": component},
    }


NAMESPACE_EVENTS: Dict[str, Dict[str, List[Dict[str, Any]]]] = {
    "prod-us-east": {
        "payments": [
            _mock_event(
                event_type="Normal",
                reason="Scheduled",
                message="Successfully assigned payments/payments-api-84b5d5 to node-2",
                involved_kind="Pod",
                involved_name="payments-api-84b5d5",
                component="default-scheduler",
                minutes_ago=2,
            ),
            _mock_event(
                event_type="Normal",
                reason="Pulled",
                message='Container image "ghcr.io/mock/payments:v2.8.1" already present on machine',
                involved_kind="Pod",
                involved_name="payments-api-84b5d5",
                component="kubelet",
                minutes_ago=3,
            ),
            _mock_event(
                event_type="Normal",
                reason="Created",
                message="Created container app",
                involved_kind="Pod",
                involved_name="payments-api-84b5d5",
                component="kubelet",
                minutes_ago=4,
            ),
            _mock_event(
                event_type="Normal",
                reason="Started",
                message="Started container app",
                involved_kind="Pod",
                involved_name="payments-api-84b5d5",
                component="kubelet",
                minutes_ago=5,
            ),
            _mock_event(
                event_type="Warning",
                reason="FailedScheduling",
                message="0/3 nodes are available: 1 Insufficient memory, 2 node(s) had untolerated taint",
                involved_kind="Pod",
                involved_name="payments-api-12caab",
                component="default-scheduler",
                minutes_ago=18,
            ),
            _mock_event(
                event_type="Warning",
                reason="BackOff",
                message="Back-off restarting failed container ledger-worker in pod ledger-worker-9f2c1",
                involved_kind="Pod",
                involved_name="ledger-worker-9f2c1",
                component="kubelet",
                minutes_ago=25,
                count=4,
            ),
            _mock_event(
                event_type="Warning",
                reason="Unhealthy",
                message="Readiness probe failed: HTTP probe failed with statuscode: 503",
                involved_kind="Pod",
                involved_name="payments-api-12caab",
                component="kubelet",
                minutes_ago=40,
            ),
            _mock_event(
                event_type="Warning",
                reason="OOMKilled",
                message="Container app was OOMKilled",
                involved_kind="Pod",
                involved_name="payments-api-12caab",
                component="kubelet",
                minutes_ago=55,
            ),
        ],
        "checkout": [
            _mock_event(
                event_type="Normal",
                reason="ScalingReplicaSet",
                message="Scaled up replica set checkout-api to 4",
                involved_kind="Deployment",
                involved_name="checkout-api",
                component="deployment-controller",
                minutes_ago=12,
            ),
        ],
    },
    "staging-eu-west": {
        "sandbox": [
            _mock_event(
                event_type="Normal",
                reason="Scheduled",
                message="Successfully assigned sandbox/demo-app-68879d to node-1",
                involved_kind="Pod",
                involved_name="demo-app-68879d",
                component="default-scheduler",
                minutes_ago=6,
            ),
        ],
    },
}

NAMESPACE_RESOURCES: Dict[str, Dict[str, Dict[str, Any]]] = {
    "prod-us-east": {
        "payments": {
            "namespace": "payments",
            "deployments": [
                {
                    "name": "payments-api",
                    "ready": "10/10",
                    "replicas": {"ready": 10, "desired": 10},
                    "image": "ghcr.io/mock/payments:v2.8.1",
                    "status": "healthy",
                    "cpuUsageMillicores": 2200,
                    "cpuLimitMillicores": 5000,
                    "memoryUsageMiB": 4096,
                    "memoryLimitMiB": 8192,
                    "actions": ["restart", "scale", "view-logs"],
                },
                {
                    "name": "ledger-worker",
                    "ready": "6/6",
                    "replicas": {"ready": 6, "desired": 6},
                    "image": "ghcr.io/mock/ledger:v1.19.0",
                    "status": "healthy",
                    "cpuUsageMillicores": 1300,
                    "cpuLimitMillicores": 3000,
                    "memoryUsageMiB": 2048,
                    "memoryLimitMiB": 4096,
                    "actions": ["restart", "scale", "view-logs"],
                },
            ],
            "services": [
                {"name": "payments-api", "type": "ClusterIP", "ports": [80], "actions": ["describe"]},
                {"name": "ledger-worker", "type": "ClusterIP", "ports": [8080], "actions": ["describe"]},
            ],
            "pods": [
                {
                    "name": "payments-api-84b5d5",
                    "status": "Running",
                    "restarts": 0,
                    "image": "ghcr.io/mock/payments:v2.8.1",
                    "cpuUsageMillicores": 180,
                    "memoryUsageMiB": 420,
                    "actions": ["logs", "describe"],
                },
                {
                    "name": "payments-api-12caab",
                    "status": "Running",
                    "restarts": 1,
                    "image": "ghcr.io/mock/payments:v2.8.1",
                    "cpuUsageMillicores": 210,
                    "memoryUsageMiB": 460,
                    "actions": ["logs", "describe"],
                },
            ],
        }
    },
    "staging-eu-west": {
        "sandbox": {
            "namespace": "sandbox",
            "deployments": [
                {
                    "name": "demo-app",
                    "ready": "2/2",
                    "replicas": {"ready": 2, "desired": 2},
                    "image": "ghcr.io/mock/demo:v0.9.4",
                    "status": "healthy",
                    "cpuUsageMillicores": 340,
                    "cpuLimitMillicores": 1000,
                    "memoryUsageMiB": 620,
                    "memoryLimitMiB": 1536,
                    "actions": ["restart", "scale", "view-logs"],
                },
            ],
            "services": [
                {"name": "demo-app", "type": "ClusterIP", "ports": [8080], "actions": ["describe"]},
            ],
            "pods": [
                {
                    "name": "demo-app-68879d",
                    "status": "Running",
                    "restarts": 0,
                    "image": "ghcr.io/mock/demo:v0.9.4",
                    "cpuUsageMillicores": 165,
                    "memoryUsageMiB": 315,
                    "actions": ["logs", "describe"],
                },
            ],
        }
    },
}

INVENTORY_DETAIL_EXTRAS: Dict[str, Dict[str, Any]] = {
    "prod-us-east:checkout:checkout-api": {
        "creationTime": _iso_now(-120),
        "deployments": [
            {
                "name": "checkout-api",
                "ready": "4/4",
                "replicas": {"ready": 4, "desired": 4},
                "image": "ghcr.io/mock/checkout:v3.2.0",
                "status": "healthy",
                "labels": {"app.kubernetes.io/name": "checkout-api"},
            }
        ],
        "services": [
            {"name": "checkout-service", "type": "ClusterIP", "ports": [8080], "clusterIP": "10.0.12.5"},
        ],
        "pods": [
            {
                "name": "checkout-api-7f8c9d",
                "status": "Running",
                "ready": "1/1",
                "restarts": 0,
                "node": "node-2",
                "age": "3d",
                "cpuUsage": "120m",
                "memoryUsage": "256Mi",
                "image": "ghcr.io/mock/checkout:v3.2.0",
                "actions": ["logs", "describe"],
            }
        ],
        "ingress": [
            {
                "name": "checkout-ingress",
                "host": "checkout.example.com",
                "path": "/",
                "backendService": "checkout-service",
                "tlsEnabled": True,
            }
        ],
        "configMaps": [{"name": "checkout-config", "keys": 4}],
        "secrets": [{"name": "checkout-tls", "type": "kubernetes.io/tls"}],
        "events": [
            {"type": "Normal", "reason": "ScalingReplicaSet", "message": "Scaled up replica set checkout-api to 4", "time": _iso_now(-30)},
        ],
    },
    "prod-us-east:payments:redis": {
        "creationTime": _iso_now(-240),
        "deployments": [
            {
                "name": "redis",
                "ready": "2/3",
                "replicas": {"ready": 2, "desired": 3},
                "image": "redis:7.2",
                "status": "warning",
                "labels": {"app.kubernetes.io/name": "redis"},
            }
        ],
        "services": [{"name": "redis", "type": "ClusterIP", "ports": [6379], "clusterIP": "10.0.8.12"}],
        "pods": [
            {
                "name": "redis-0",
                "status": "Running",
                "ready": "1/1",
                "restarts": 0,
                "node": "node-1",
                "age": "12d",
                "cpuUsage": "45m",
                "memoryUsage": "512Mi",
                "image": "redis:7.2",
                "actions": ["logs", "describe"],
            },
            {
                "name": "redis-1",
                "status": "CrashLoopBackOff",
                "ready": "0/1",
                "restarts": 8,
                "node": "node-3",
                "age": "12d",
                "cpuUsage": "-",
                "memoryUsage": "-",
                "image": "redis:7.2",
                "actions": ["logs", "describe"],
            },
        ],
        "configMaps": [{"name": "redis-config", "keys": 2}],
        "secrets": [{"name": "redis-auth", "type": "Opaque"}],
        "events": [
            {"type": "Warning", "reason": "BackOff", "message": "Back-off restarting failed container", "time": _iso_now(-5)},
        ],
    },
    "prod-us-east:payments:payments-api": {
        "creationTime": _iso_now(-500),
        "ingress": [
            {
                "name": "payments-ingress",
                "host": "payments.example.com",
                "path": "/api",
                "backendService": "payments-api",
                "tlsEnabled": True,
            }
        ],
        "configMaps": [{"name": "payments-config", "keys": 6}, {"name": "payments-feature-flags", "keys": 2}],
        "secrets": [{"name": "database-credentials", "type": "Opaque"}],
        "events": [
            {"type": "Normal", "reason": "SuccessfulCreate", "message": "Created pod payments-api-84b5d5", "time": _iso_now(-60)},
            {"type": "Warning", "reason": "FailedScheduling", "message": "0/3 nodes available", "time": _iso_now(-90)},
        ],
    },
}

ALERTS = [
    {
        "id": "alt-001",
        "severity": "critical",
        "clusterId": "prod-us-east",
        "namespace": "payments",
        "pod": "payments-api-84b5d5",
        "title": "High CPU on pod payments-api-84b5d5",
        "description": "CPU usage 92.0% of limit (0.460/0.500 cores) in namespace payments",
        "cpuPercent": 92.0,
        "firedAt": _iso_now(-8),
        "status": "firing",
    },
    {
        "id": "alt-002",
        "severity": "warning",
        "clusterId": "prod-us-east",
        "namespace": "platform",
        "pod": "metrics-worker-7d9f",
        "title": "High CPU on pod metrics-worker-7d9f",
        "description": "CPU usage 81.5% of limit (0.326/0.400 cores) in namespace platform",
        "cpuPercent": 81.5,
        "firedAt": _iso_now(-20),
        "status": "firing",
    },
]

SETTINGS: Dict[str, Any] = {
    "theme": "system",
    "refreshIntervalSeconds": 30,
    "defaultCluster": "prod-us-east",
    "notifications": {
        "alerts": True,
        "upgrades": True,
        "routing": {
            "email": {"enabled": False, "address": ""},
            "slack": {"enabled": False, "webhookUrl": ""},
            "webhook": {"enabled": False, "url": ""},
        },
    },
}

HELM_RELEASES: Dict[str, List[Dict[str, Any]]] = {
    "prod-us-east": [
        {
            "name": "prometheus",
            "namespace": "monitoring",
            "chart": "prometheus-25.8.0",
            "app_version": "2.47.0",
            "revision": 3,
            "status": "deployed",
            "updated": _iso_now(-120),
            "valuesSummary": {"server": {"replicaCount": 1}},
        },
    ],
}

HELM_RELEASE_DETAILS: Dict[str, Dict[str, Any]] = {
    "prod-us-east:monitoring:prometheus": {
        "releaseName": "prometheus",
        "namespace": "monitoring",
        "chartName": "prometheus",
        "chartVersion": "25.8.0",
        "appVersion": "2.47.0",
        "revision": 3,
        "status": "deployed",
        "lastDeployed": _iso_now(-120),
        "valuesSummary": {"server": {"replicaCount": 1}},
        "renderedManifest": "apiVersion: v1\nkind: Service\nmetadata:\n  name: prometheus-server\n  labels:\n    app.kubernetes.io/managed-by: Helm\n    app.kubernetes.io/instance: prometheus\n    helm.sh/chart: prometheus-25.8.0\n",
    },
}

