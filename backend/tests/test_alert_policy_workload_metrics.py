from api.k8s_metrics import aggregate_pod_usage_percents, memory_usage_percent, pod_usage_percents


def test_pod_usage_percents_uses_limits():
    pod = {
        "metadata": {"name": "api-1", "namespace": "kubesight"},
        "spec": {
            "containers": [
                {
                    "name": "api",
                    "resources": {
                        "requests": {"cpu": "250m", "memory": "512Mi"},
                        "limits": {"cpu": "1", "memory": "1Gi"},
                    },
                }
            ]
        },
    }
    cpu_pct, mem_pct = pod_usage_percents(pod, {"cpu": 0.5, "memory": 512.0})
    assert cpu_pct == 50.0
    assert mem_pct == memory_usage_percent(512.0, 1024.0)


def test_pod_usage_percents_falls_back_to_requests():
    pod = {
        "metadata": {"name": "api-1", "namespace": "kubesight"},
        "spec": {
            "containers": [
                {
                    "name": "api",
                    "resources": {"requests": {"cpu": "500m", "memory": "256Mi"}},
                }
            ]
        },
    }
    cpu_pct, mem_pct = pod_usage_percents(pod, {"cpu": 0.25, "memory": 128.0})
    assert cpu_pct == 50.0
    assert mem_pct == 50.0


def test_aggregate_pod_usage_percents_sums_deployment_pods():
    pods = [
        {
            "metadata": {"name": "api-1", "namespace": "kubesight"},
            "spec": {
                "containers": [
                    {
                        "name": "api",
                        "resources": {"limits": {"cpu": "1", "memory": "1Gi"}},
                    }
                ]
            },
        },
        {
            "metadata": {"name": "api-2", "namespace": "kubesight"},
            "spec": {
                "containers": [
                    {
                        "name": "api",
                        "resources": {"limits": {"cpu": "1", "memory": "1Gi"}},
                    }
                ]
            },
        },
    ]
    top_by_pod = {
        ("kubesight", "api-1"): {"cpu": 0.5, "memory": 512.0},
        ("kubesight", "api-2"): {"cpu": 0.25, "memory": 256.0},
    }
    cpu_pct, mem_pct = aggregate_pod_usage_percents(pods, top_by_pod)
    assert cpu_pct == 37.5
    assert mem_pct == 37.5
