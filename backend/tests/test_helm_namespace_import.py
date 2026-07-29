import base64
import io
import json
import tarfile
from unittest.mock import patch

from api.services.helm_chart_template_service import chart_archive_base64
from tests.conftest import auth_headers


CLUSTER = "prod-us-east"
NAMESPACE = "payments"


def _discover(client, token, **overrides):
    body = {"clusterId": CLUSTER, "namespace": NAMESPACE}
    body.update(overrides)
    return client.post(
        "/api/helm/chart-templates/namespace/resources",
        headers=auth_headers(token),
        json=body,
    )


def _import(client, token, **overrides):
    body = {"clusterId": CLUSTER, "namespace": NAMESPACE}
    body.update(overrides)
    return client.post(
        "/api/helm/chart-templates/import/namespace",
        headers=auth_headers(token),
        json=body,
    )


def _chart_text(app, slug):
    """Every stored chart file as one string — what actually ships to Helm."""
    with app.app_context():
        archive = base64.b64decode(chart_archive_base64(slug))
    with tarfile.open(fileobj=io.BytesIO(archive), mode="r:gz") as tar:
        return "\n".join(
            tar.extractfile(member).read().decode("utf-8", errors="ignore")
            for member in tar.getmembers()
            if member.isfile()
        )


def test_namespace_scan_lists_importable_objects_and_explains_the_skipped_ones(
    client, admin_token
):
    response = _discover(client, admin_token)
    assert response.status_code == 200, response.get_json()
    data = response.get_json()["data"]

    kinds = {item["kind"] for item in data["resources"] if not item["skipped"]}
    assert {"Deployment", "Service", "ConfigMap", "Secret"} <= kinds
    assert data["importableCount"] == len(
        [item for item in data["resources"] if not item["skipped"]]
    )

    skipped = {
        f"{item['kind']}/{item['name']}": item["skipped"]
        for item in data["resources"]
        if item["skipped"]
    }
    assert "Cluster-provided default ServiceAccount" in skipped["ServiceAccount/default"]
    assert "CA bundle" in skipped["ConfigMap/kube-root-ca.crt"]
    assert any("Helm release" in reason for reason in skipped.values())

    # Workloads lead the preview so the chart's subject is the first thing seen.
    assert data["resources"][0]["kind"] == "Deployment"


def test_namespace_import_builds_a_chart_without_cluster_generated_fields(
    client, admin_token, app
):
    response = _import(client, admin_token, name="Payments From Cluster")
    assert response.status_code == 201, response.get_json()
    imported = response.get_json()["data"]
    assert imported["sourceType"] == "namespace"
    assert imported["sourceRef"] == f"{CLUSTER}/{NAMESPACE}"

    detail = client.get(
        f"/api/helm/chart-templates/{imported['id']}",
        headers=auth_headers(admin_token),
    ).get_json()["data"]
    chart = _chart_text(app, imported["id"]) + json.dumps(detail)

    # Everything the cluster generated must be gone from the stored chart.
    for junk in (
        "resourceVersion",
        "00000000-0000-0000-0000-000000000001",
        "10.96.44.19",
        "creationTimestamp",
        "deployment.kubernetes.io/revision",
        "pvc-9f2c1e77-mock",
        "namespace: payments",
        "nodePort",
    ):
        assert junk not in chart, junk

    # ...and so must every live Secret value, while its keys survive.
    assert "czNjcjN0" not in chart
    assert "s3cr3t-in-cluster" not in chart
    assert "DB_PASSWORD" in chart
    assert "b64enc" in chart

    sensitive = [item for item in detail["variables"] if item["sensitive"]]
    assert sensitive
    assert all(item["required"] and item["default"] == "" for item in sensitive)

    categories = {item["category"] for item in detail["variables"]}
    assert {"Images", "Scaling", "Networking", "Secrets"} <= categories


def test_namespace_import_honours_the_resource_selection(client, admin_token):
    scan = _discover(client, admin_token).get_json()["data"]
    deployment = next(
        item for item in scan["resources"] if item["kind"] == "Deployment" and not item["skipped"]
    )

    response = _import(
        client,
        admin_token,
        resources=[{"kind": deployment["kind"], "name": deployment["name"]}],
    )
    assert response.status_code == 201, response.get_json()
    imported = response.get_json()["data"]
    assert imported["resourceCount"] == 1

    detail = client.get(
        f"/api/helm/chart-templates/{imported['id']}",
        headers=auth_headers(admin_token),
    ).get_json()["data"]
    assert [item["kinds"] for item in detail["chart"]["templates"]] == [["Deployment"]]


def test_namespace_import_rejects_an_empty_selection(client, admin_token):
    response = _import(client, admin_token, resources=[])
    assert response.status_code == 400
    assert "Select at least one resource" in response.get_json()["error"]


def test_namespace_import_requires_a_cluster_and_namespace(client, admin_token):
    response = _import(client, admin_token, namespace="")
    assert response.status_code == 400
    assert "cluster" in response.get_json()["error"].lower()


def test_namespace_import_is_gated_by_the_inventory_permission(client, viewer_token):
    assert _discover(client, viewer_token).status_code == 403
    assert _import(client, viewer_token).status_code == 403


LIVE_OBJECTS = {
    "items": [
        {
            "apiVersion": "apps/v1",
            "kind": "Deployment",
            "metadata": {
                "name": "checkout",
                "namespace": "live",
                "uid": "abc",
                "managedFields": [{"manager": "kubectl"}],
                "labels": {"app": "checkout", "helm.sh/chart": "checkout-1.2.3"},
            },
            "spec": {
                "replicas": 2,
                "template": {
                    "metadata": {"creationTimestamp": None, "labels": {"app": "checkout"}},
                    "spec": {
                        "restartPolicy": "Always",
                        "dnsPolicy": "ClusterFirst",
                        "containers": [
                            {
                                "name": "api",
                                "image": "registry.example/checkout:9.9.9",
                                "terminationMessagePath": "/dev/termination-log",
                            }
                        ],
                    },
                },
            },
            "status": {"replicas": 2},
        },
        {
            "apiVersion": "v1",
            "kind": "Pod",
            "metadata": {
                "name": "checkout-7d9f",
                "namespace": "live",
                "ownerReferences": [{"kind": "ReplicaSet", "name": "checkout-7d9f"}],
            },
            "spec": {"containers": []},
        },
        {
            "apiVersion": "v1",
            "kind": "Secret",
            "metadata": {"name": "sh.helm.release.v1.checkout.v4", "namespace": "live"},
            "type": "helm.sh/release.v1",
            "data": {"release": "H4sIA"},
        },
    ]
}


def test_live_cluster_read_strips_owned_and_helm_managed_objects(client, admin_token, app):
    calls = []

    def fake_run(access, args, timeout=None):
        calls.append(args)
        return json.dumps(LIVE_OBJECTS)

    patches = [
        patch("api.k8s_provider.should_use_real_k8s", return_value=True),
        patch("api.k8s_provider.resolve_cluster_access", return_value=object()),
        patch("api.k8s_provider._run_for_access", side_effect=fake_run),
    ]
    for item in patches:
        item.start()
    try:
        scan = _discover(client, admin_token, namespace="live").get_json()["data"]
        response = _import(client, admin_token, namespace="live")
    finally:
        for item in patches:
            item.stop()

    # One read for every supported kind, scoped to the namespace.
    assert calls[0][:2] == ["get", ",".join(
        [
            "deployments",
            "statefulsets",
            "daemonsets",
            "cronjobs",
            "services",
            "ingresses",
            "configmaps",
            "secrets",
            "persistentvolumeclaims",
            "serviceaccounts",
            "roles",
            "rolebindings",
            "horizontalpodautoscalers",
            "poddisruptionbudgets",
            "networkpolicies",
        ]
    )]
    assert "-n" in calls[0] and "live" in calls[0]

    reasons = {item["kind"]: item["skipped"] for item in scan["resources"]}
    assert "ReplicaSet/checkout-7d9f" in reasons["Pod"]
    assert "helm.sh/release.v1" in reasons["Secret"]
    assert reasons["Deployment"] == ""

    assert response.status_code == 201, response.get_json()
    imported = response.get_json()["data"]
    assert imported["resourceCount"] == 1

    chart = _chart_text(app, imported["id"])
    assert "managedFields" not in chart
    assert "helm.sh/chart" not in chart
    assert "terminationMessagePath" not in chart
    assert "restartPolicy" not in chart
    # The parts that matter survive: labels, selectors, and a templated image.
    assert "app: checkout" in chart
    assert "{{ .Values.variables." in chart


def test_cluster_managed_objects_do_not_consume_the_import_cap(client, admin_token):
    """A namespace full of junk must still offer a full 250 importable objects."""
    items = [
        {
            "apiVersion": "apps/v1",
            "kind": "Deployment",
            "metadata": {"name": f"app-{index:03d}", "namespace": "live"},
            "spec": {"replicas": 1, "template": {"spec": {"containers": []}}},
        }
        for index in range(260)
    ]
    items += [
        {
            "apiVersion": "v1",
            "kind": "Pod",
            "metadata": {
                "name": f"app-{index:03d}-pod",
                "namespace": "live",
                "ownerReferences": [{"kind": "ReplicaSet", "name": f"app-{index:03d}"}],
            },
        }
        for index in range(200)
    ]

    patches = [
        patch("api.k8s_provider.should_use_real_k8s", return_value=True),
        patch("api.k8s_provider.resolve_cluster_access", return_value=object()),
        patch("api.k8s_provider._run_for_access", return_value=json.dumps({"items": items})),
    ]
    for item in patches:
        item.start()
    try:
        data = _discover(client, admin_token, namespace="live").get_json()["data"]
    finally:
        for item in patches:
            item.stop()

    assert data["importableCount"] == 250
    assert data["skippedCount"] == 200
    assert any("importable objects" in warning for warning in data["warnings"])


def test_missing_kind_on_older_clusters_falls_back_to_per_kind_reads(client, admin_token):
    from api.k8s_provider import K8sCommandError

    def fake_run(access, args, timeout=None):
        resource = args[1]
        if "," in resource:
            raise K8sCommandError("the server doesn't have a resource type poddisruptionbudgets")
        if resource == "poddisruptionbudgets":
            raise K8sCommandError("the server doesn't have a resource type")
        if resource == "deployments":
            return json.dumps({"items": [LIVE_OBJECTS["items"][0]]})
        return json.dumps({"items": []})

    patches = [
        patch("api.k8s_provider.should_use_real_k8s", return_value=True),
        patch("api.k8s_provider.resolve_cluster_access", return_value=object()),
        patch("api.k8s_provider._run_for_access", side_effect=fake_run),
    ]
    for item in patches:
        item.start()
    try:
        data = _discover(client, admin_token, namespace="live").get_json()["data"]
    finally:
        for item in patches:
            item.stop()

    assert data["importableCount"] == 1
    assert any("poddisruptionbudgets" in warning for warning in data["warnings"])
