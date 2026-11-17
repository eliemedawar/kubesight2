"""Volume claims, volumes, storage classes, autoscalers, network policies and
events on the Resources page: list shapes, the access rules, the side-rail
summary, and describe/YAML for the new kinds."""

from api.access_engine import filter_namespace_resources
from api.access_rules import apply_access_rules
from api.k8s_provider import (
    CLUSTER_SCOPED_LIST_KEYS,
    NAMESPACE_RESOURCE_LIST_KEYS,
    _build_namespace_hpas,
    _build_namespace_network_policies,
    _build_namespace_pvcs,
    _build_persistent_volumes,
    _build_storage_classes,
)
from api.models import User
from api.services.resource_actions_service import _normalize_kind, get_resource_yaml
from api.services.resource_summary import row_tone, status_tone, summarize_namespace_resources
from tests.conftest import auth_headers

NEW_KEYS = ("events", "hpas", "networkpolicies", "pvcs", "pvs", "storageclasses")


def _namespace_rules(cluster_id, namespace, keys):
    return [
        {
            "clusterId": cluster_id,
            "namespace": namespace,
            "resourceType": "namespace",
            "permissionKey": key,
            "effect": "allow",
        }
        for key in keys
    ]


def _viewer_on(app, namespace="kubesight", extra=(), cluster_id="docker-desktop"):
    viewer = User.query.filter_by(username="viewer").first()
    apply_access_rules(
        viewer,
        _namespace_rules(
            cluster_id,
            namespace,
            ["clusters:view", "namespaces:view", "resources:view", *extra],
        ),
    )
    from api.db import db

    db.session.commit()
    return viewer


# --- list shapes -------------------------------------------------------------


def test_new_kinds_are_listed_kinds():
    for key in NEW_KEYS:
        assert key in NAMESPACE_RESOURCE_LIST_KEYS
    assert CLUSTER_SCOPED_LIST_KEYS == {"pvs", "storageclasses"}


def test_pvc_rows_carry_phase_health_and_the_pods_using_them():
    items = _build_namespace_pvcs(
        "payments",
        [
            {
                "metadata": {"name": "data-0"},
                "spec": {"accessModes": ["ReadWriteOnce"], "storageClassName": "oci-bv", "volumeName": "pvc-1"},
                "status": {"phase": "Bound", "capacity": {"storage": "20Gi"}, "accessModes": ["ReadWriteOnce"]},
            },
            {
                "metadata": {"name": "data-1"},
                "spec": {
                    "accessModes": ["ReadWriteMany"],
                    "resources": {"requests": {"storage": "5Gi"}},
                },
                "status": {"phase": "Pending"},
            },
        ],
        {"data-0": ["db-0"]},
    )
    bound, pending = items
    assert bound["status"] == "Bound" and bound["health"] == "ok"
    assert bound["capacity"] == "20Gi" and bound["accessModes"] == "RWO"
    assert bound["usedBy"] == ["db-0"] and bound["volume"] == "pvc-1"
    assert pending["health"] == "warn"
    # Not bound yet: the size asked for stands in for the size given.
    assert pending["capacity"] == "5Gi" and pending["accessModes"] == "RWX"
    assert pending["storageClass"] == "-" and pending["usedBy"] == []


def test_pv_rows_name_their_claim():
    (pv,) = _build_persistent_volumes(
        [
            {
                "metadata": {"name": "pvc-9"},
                "spec": {
                    "capacity": {"storage": "8Gi"},
                    "accessModes": ["ReadWriteOncePod"],
                    "persistentVolumeReclaimPolicy": "Retain",
                    "claimRef": {"namespace": "monitoring", "name": "prom-0"},
                },
                "status": {"phase": "Released"},
            }
        ]
    )
    assert pv["claimNamespace"] == "monitoring" and pv["claimName"] == "prom-0"
    assert pv["accessModes"] == "RWOP" and pv["reclaimPolicy"] == "Retain"
    assert pv["health"] == "warn"


def test_storage_classes_put_the_default_first():
    items = _build_storage_classes(
        [
            {"metadata": {"name": "a-fast"}, "provisioner": "x", "allowVolumeExpansion": True},
            {
                "metadata": {
                    "name": "z-default",
                    "annotations": {"storageclass.kubernetes.io/is-default-class": "true"},
                },
                "provisioner": "y",
                "reclaimPolicy": "Retain",
                "volumeBindingMode": "WaitForFirstConsumer",
            },
        ]
    )
    assert [sc["name"] for sc in items] == ["z-default", "a-fast"]
    assert items[0]["default"] and items[0]["bindingMode"] == "WaitForFirstConsumer"
    assert items[1]["reclaimPolicy"] == "Delete" and items[1]["bindingMode"] == "Immediate"
    assert items[1]["allowExpansion"] is True


def _hpa(spec=None, status=None):
    return {
        "metadata": {"name": "api"},
        "spec": {
            "scaleTargetRef": {"kind": "Deployment", "name": "api"},
            "minReplicas": 2,
            "maxReplicas": 6,
            "metrics": [
                {"type": "Resource", "resource": {"name": "cpu", "target": {"type": "Utilization", "averageUtilization": 60}}}
            ],
            **(spec or {}),
        },
        "status": {
            "currentReplicas": 3,
            "desiredReplicas": 3,
            "currentMetrics": [
                {"type": "Resource", "resource": {"name": "cpu", "current": {"averageUtilization": 41}}}
            ],
            **(status or {}),
        },
    }


def test_hpa_rows_read_metrics_and_status():
    (row,) = _build_namespace_hpas("ns", [_hpa()])
    assert row["metrics"] == "cpu 41% / 60%"
    assert (row["status"], row["health"]) == ("Healthy", "ok")
    assert (row["targetKind"], row["targetName"]) == ("Deployment", "api")

    (at_max,) = _build_namespace_hpas("ns", [_hpa(status={"currentReplicas": 6, "desiredReplicas": 6})])
    assert (at_max["status"], at_max["health"]) == ("At max replicas", "warn")

    (no_metrics,) = _build_namespace_hpas(
        "ns",
        [_hpa(status={"conditions": [{"type": "ScalingActive", "status": "False", "reason": "FailedGetResourceMetric"}]})],
    )
    assert (no_metrics["status"], no_metrics["health"]) == ("Metrics unavailable", "crit")


def test_hpa_v1_object_reads_its_cpu_fields():
    hpa = {
        "metadata": {"name": "old"},
        "spec": {"maxReplicas": 4, "targetCPUUtilizationPercentage": 80, "scaleTargetRef": {}},
        "status": {"currentReplicas": 1, "desiredReplicas": 1, "currentCPUUtilizationPercentage": 12},
    }
    (row,) = _build_namespace_hpas("ns", [hpa])
    assert row["metrics"] == "cpu 12% / 80%"


def test_network_policies_read_as_plain_rules():
    deny, allow = _build_namespace_network_policies(
        "ns",
        [
            {"metadata": {"name": "deny"}, "spec": {"podSelector": {}, "policyTypes": ["Ingress", "Egress"]}},
            {
                "metadata": {"name": "allow-api"},
                "spec": {
                    "podSelector": {"matchLabels": {"app": "api"}},
                    "ingress": [
                        {
                            "from": [
                                {"podSelector": {"matchLabels": {"app": "gw"}}},
                                {"ipBlock": {"cidr": "10.0.0.0/8", "except": ["10.1.0.0/16"]}},
                            ],
                            "ports": [{"port": 8080}, {"port": 9000, "endPort": 9010, "protocol": "UDP"}],
                        },
                        {},
                    ],
                },
            },
        ],
    )
    assert deny["appliesTo"] == "All pods"
    assert deny["ingressRules"] == ["nothing (deny all)"]
    assert deny["egressRules"] == ["nothing (deny all)"]

    assert allow["appliesTo"] == "app=api"
    # No policyTypes and no egress rules: only Ingress is governed.
    assert allow["policyTypes"] == ["Ingress"] and allow["egressRules"] == []
    assert allow["ingressRules"] == [
        "from app=gw; 10.0.0.0/8 except 10.1.0.0/16 on 8080/TCP, 9000-9010/UDP",
        "from anywhere",
    ]


# --- what needs attention ----------------------------------------------------


def test_status_tone_matches_the_page_rules():
    assert status_tone("Running") == ""
    assert status_tone("Complete") == ""
    assert status_tone("CrashLoopBackOff") == "crit"
    assert status_tone("Failed") == "crit"
    assert status_tone("Pending") == "warn"
    assert status_tone("warning") == "warn"
    assert status_tone("-") == ""


def test_a_volume_is_only_this_namespaces_problem_when_claimed_here():
    released_here = {"health": "warn", "claimNamespace": "payments"}
    released_elsewhere = {"health": "warn", "claimNamespace": "monitoring"}
    assert row_tone("pvs", released_here, "payments") == "warn"
    assert row_tone("pvs", released_elsewhere, "payments") == ""


def test_summary_counts_kinds_and_lists_problems_worst_first():
    summary = summarize_namespace_resources(
        "payments",
        {
            "namespace": "payments",
            "pods": [
                {"name": "a", "status": "Running"},
                {"name": "b", "status": "Pending"},
                {"name": "c", "status": "CrashLoopBackOff"},
            ],
            "deployments": [{"name": "d", "updateStatus": "warning"}],
            "events": [{"type": "Warning"}, {"type": "Normal"}],
            "pvs": [
                {"name": "v1", "health": "ok", "claimNamespace": "payments"},
                {"name": "v2", "health": "warn", "claimNamespace": "other"},
            ],
            "configmaps": [{"name": "x"}],
        },
    )
    kinds = summary["kinds"]
    assert kinds["pods"] == {"count": 3, "issues": 2, "tone": "crit"}
    assert kinds["deployments"] == {"count": 1, "issues": 1, "tone": "warn"}
    assert kinds["events"]["issues"] == 1
    assert kinds["configmaps"] == {"count": 1, "issues": 0, "tone": ""}
    # The rail counts volumes claimed here; the total is kept for the filter.
    assert kinds["pvs"]["count"] == 1 and kinds["pvs"]["total"] == 2 and kinds["pvs"]["issues"] == 0

    names = [(item["listKey"], item["name"]) for item in summary["attention"]]
    assert names == [("pods", "c"), ("pods", "b"), ("deployments", "d")]
    # Events are counted, not listed: each one is about a row already listed.
    assert summary["attentionTotal"] == 3


def test_summary_caps_the_attention_list():
    pods = [{"name": f"p{i}", "status": "Error"} for i in range(9)]
    summary = summarize_namespace_resources("ns", {"pods": pods})
    assert len(summary["attention"]) == 6
    assert summary["attentionTotal"] == 9


# --- access ------------------------------------------------------------------


def test_new_kinds_come_back_only_when_read(app):
    with app.app_context():
        viewer = _viewer_on(app)
        filtered = filter_namespace_resources(
            viewer, "docker-desktop", {"namespace": "kubesight", "pods": []}
        )
        for key in NEW_KEYS:
            assert key not in filtered


def test_non_admin_sees_only_volumes_claimed_from_reachable_namespaces(app):
    with app.app_context():
        viewer = _viewer_on(app)
        filtered = filter_namespace_resources(
            viewer,
            "docker-desktop",
            {
                "namespace": "kubesight",
                "pvs": [
                    {"name": "mine", "claimNamespace": "kubesight"},
                    {"name": "theirs", "claimNamespace": "kube-system"},
                    {"name": "free", "claimNamespace": ""},
                ],
                "storageclasses": [{"name": "standard"}],
                "pvcs": [{"name": "data"}],
                "networkpolicies": [{"name": "deny"}],
            },
        )
        assert [pv["name"] for pv in filtered["pvs"]] == ["mine"]
        assert len(filtered["storageclasses"]) == 1
        assert len(filtered["pvcs"]) == 1
        assert len(filtered["networkpolicies"]) == 1

        elsewhere = filter_namespace_resources(
            viewer,
            "docker-desktop",
            {"namespace": "kube-system", "pvcs": [{"name": "x"}], "storageclasses": [{"name": "s"}]},
        )
        assert elsewhere["pvcs"] == [] and elsewhere["storageclasses"] == []


def test_autoscaler_follows_what_it_scales(app, monkeypatch):
    import api.access_engine as engine

    with app.app_context():
        viewer = _viewer_on(app)
        visible = {"api"}
        monkeypatch.setattr(
            engine,
            "can_access_resource",
            lambda user, cluster, ns, rtype, name: rtype == "deployment" and name in visible,
        )
        filtered = filter_namespace_resources(
            viewer,
            "docker-desktop",
            {
                "namespace": "kubesight",
                "hpas": [
                    {"name": "api", "targetKind": "Deployment", "targetName": "api"},
                    {"name": "worker", "targetKind": "Deployment", "targetName": "worker"},
                    # A target kind access rules do not name stays with the namespace.
                    {"name": "custom", "targetKind": "Rollout", "targetName": "x"},
                ],
            },
        )
        assert [h["name"] for h in filtered["hpas"]] == ["api", "custom"]


def test_new_kinds_have_describe_aliases():
    assert _normalize_kind("pvc") == "persistentvolumeclaim"
    assert _normalize_kind("PV") == "persistentvolume"
    assert _normalize_kind("storageclasses") == "storageclass"
    assert _normalize_kind("hpa") == "horizontalpodautoscaler"
    assert _normalize_kind("netpol") == "networkpolicy"


def test_viewer_cannot_read_a_volume_claimed_elsewhere(app):
    with app.app_context():
        viewer = _viewer_on(app, namespace="payments", cluster_id="prod-us-east")
        # Mock cluster: pvc-3f2a91c0 is claimed from payments, pvc-5aa19d03 from monitoring.
        data, error, status = get_resource_yaml(viewer, "prod-us-east", "payments", "pv", "pvc-3f2a91c0")
        assert error is None and status == 200
        assert "PersistentVolume" in data["yaml"]

        data, error, status = get_resource_yaml(viewer, "prod-us-east", "payments", "pv", "pvc-5aa19d03")
        assert (error, status) == ("Forbidden", 403)


# --- HTTP --------------------------------------------------------------------


def test_list_route_serves_the_new_kinds_in_mock_mode(client, admin_token):
    headers = auth_headers(admin_token)
    base = "/api/clusters/prod-us-east/namespaces/payments/resources"
    hpas = client.get(f"{base}/hpas", headers=headers).get_json()["data"]["hpas"]
    assert {h["name"] for h in hpas} == {"payments-api", "ledger-worker"}
    events = client.get(f"{base}/events", headers=headers).get_json()["data"]["events"]
    assert events and all(e["namespace"] == "payments" for e in events)
    classes = client.get(f"{base}/storageclasses", headers=headers).get_json()["data"]["storageclasses"]
    assert classes[0]["name"] == "gp3" and classes[0]["default"] is True
    pvs = client.get(f"{base}/pvs", headers=headers).get_json()["data"]["pvs"]
    assert len(pvs) == 3


def test_summary_route_counts_every_kind(client, admin_token):
    headers = auth_headers(admin_token)
    response = client.get(
        "/api/clusters/prod-us-east/namespaces/payments/resource-summary", headers=headers
    )
    assert response.status_code == 200
    data = response.get_json()["data"]
    assert set(NAMESPACE_RESOURCE_LIST_KEYS) <= set(data["kinds"])
    assert data["kinds"]["pvcs"] == {"count": 2, "issues": 1, "tone": "warn"}
    assert data["kinds"]["pvs"]["count"] == 2 and data["kinds"]["pvs"]["total"] == 3
    attention = {(a["listKey"], a["name"]) for a in data["attention"]}
    assert ("hpas", "ledger-worker") in attention and ("pvcs", "ledger-archive") in attention
    assert data["unavailable"] == []
