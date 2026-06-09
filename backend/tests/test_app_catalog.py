"""Tests for application catalog, deployment workflows, and RBAC."""

import json
from unittest.mock import patch

import yaml

from tests.conftest import auth_headers


SAMPLE_DEPLOYMENT_YAML = """
apiVersion: apps/v1
kind: Deployment
metadata:
  name: backend-api
  namespace: payments
spec:
  replicas: 2
  selector:
    matchLabels:
      app: backend-api
  template:
    metadata:
      labels:
        app: backend-api
    spec:
      containers:
      - name: backend-api
        image: nginx:1.25
        ports:
        - containerPort: 8080
---
apiVersion: v1
kind: Service
metadata:
  name: backend-service
  namespace: payments
spec:
  selector:
    app: backend-api
  ports:
  - port: 8080
"""


def test_catalog_register_update_remove(client, admin_token):
    workloads = client.get(
        "/api/inventory/workloads?clusterId=prod-us-east&namespace=payments",
        headers=auth_headers(admin_token),
    )
    assert workloads.status_code == 200
    items = workloads.get_json()["data"]
    assert any(w["name"] == "payments-api" for w in items)

    register = client.post(
        "/api/inventory/register",
        headers=auth_headers(admin_token),
        json={
            "clusterId": "prod-us-east",
            "namespace": "payments",
            "workloadType": "Deployment",
            "workloadName": "payments-api",
            "displayName": "payments-api",
            "ownerTeam": "Platform",
            "environment": "Production",
            "criticality": "High",
            "contactEmail": "team@example.com",
            "tags": ["payments", "api"],
        },
    )
    assert register.status_code == 201
    entry = register.get_json()["data"]
    entry_id = entry["id"]

    listing = client.get("/api/inventory", headers=auth_headers(admin_token)).get_json()["data"]
    row = next(i for i in listing if i.get("catalogEntryId") == entry_id)
    assert row["ownerTeam"] == "Platform"
    assert row["environment"] == "Production"
    assert row["criticality"] == "High"
    assert row["source"] == "Registered"

    update = client.put(
        f"/api/inventory/{entry_id}",
        headers=auth_headers(admin_token),
        json={"ownerTeam": "Payments Squad", "criticality": "Critical"},
    )
    assert update.status_code == 200
    assert update.get_json()["data"]["ownerTeam"] == "Payments Squad"

    remove = client.delete(
        f"/api/inventory/{entry_id}",
        headers=auth_headers(admin_token),
    )
    assert remove.status_code == 200
    assert remove.get_json()["data"]["removed"] is True

    listing_after = client.get("/api/inventory", headers=auth_headers(admin_token)).get_json()["data"]
    assert not any(i.get("catalogEntryId") == entry_id for i in listing_after)


def test_inventory_merge_shows_unassigned_without_catalog(client, admin_token):
    listing = client.get("/api/inventory", headers=auth_headers(admin_token)).get_json()["data"]
    unregistered = next(i for i in listing if i["name"] == "checkout-api")
    assert unregistered["ownerTeam"] == "Unassigned"
    assert unregistered["environment"] == "Not set"
    assert unregistered["source"] == "Discovered"


def test_viewer_cannot_register(client, viewer_token):
    response = client.post(
        "/api/inventory/register",
        headers=auth_headers(viewer_token),
        json={
            "clusterId": "prod-us-east",
            "namespace": "payments",
            "workloadType": "Deployment",
            "workloadName": "payments-api",
            "displayName": "payments-api",
        },
    )
    assert response.status_code == 403


def test_operator_can_dryrun_but_not_apply(client, operator_token):
    validate = client.post(
        "/api/inventory/deploy/yaml/validate",
        headers=auth_headers(operator_token),
        json={"namespace": "payments", "yaml": SAMPLE_DEPLOYMENT_YAML},
    )
    assert validate.status_code == 200
    assert validate.get_json()["data"]["valid"] is True

    with patch("api.services.deployment_service._run_kubectl_for_cluster", return_value="dry-run ok"):
        dry = client.post(
            "/api/inventory/deploy/yaml/dry-run",
            headers=auth_headers(operator_token),
            json={
                "clusterId": "prod-us-east",
                "namespace": "payments",
                "yaml": SAMPLE_DEPLOYMENT_YAML,
            },
        )
    assert dry.status_code == 200

    apply_resp = client.post(
        "/api/inventory/deploy/yaml/apply",
        headers=auth_headers(operator_token),
        json={
            "clusterId": "prod-us-east",
            "namespace": "payments",
            "yaml": SAMPLE_DEPLOYMENT_YAML,
            "confirmation": "APPLY payments",
        },
    )
    assert apply_resp.status_code == 403


def test_yaml_apply_requires_confirmation(client, admin_token):
    with patch("api.services.deployment_service._run_kubectl_for_cluster", return_value="applied"):
        response = client.post(
            "/api/inventory/deploy/yaml/apply",
            headers=auth_headers(admin_token),
            json={
                "clusterId": "prod-us-east",
                "namespace": "payments",
                "yaml": SAMPLE_DEPLOYMENT_YAML,
                "confirmation": "WRONG",
            },
        )
    assert response.status_code == 400

    with patch("api.services.deployment_service._run_kubectl_for_cluster", return_value="applied"):
        response = client.post(
            "/api/inventory/deploy/yaml/apply",
            headers=auth_headers(admin_token),
            json={
                "clusterId": "prod-us-east",
                "namespace": "payments",
                "yaml": SAMPLE_DEPLOYMENT_YAML,
                "confirmation": "APPLY payments",
            },
        )
    assert response.status_code == 200
    assert response.get_json()["data"]["applied"] is True


def test_dangerous_resource_blocked_for_non_admin(client, operator_token):
    yaml_content = """
apiVersion: rbac.authorization.k8s.io/v1
kind: ClusterRole
metadata:
  name: evil-role
rules: []
"""
    response = client.post(
        "/api/inventory/deploy/yaml/validate",
        headers=auth_headers(operator_token),
        json={"namespace": "default", "yaml": yaml_content},
    )
    assert response.status_code == 403


def test_secret_values_not_in_preview(client, admin_token):
    yaml_content = """
apiVersion: v1
kind: Secret
metadata:
  name: app-secret
  namespace: default
type: Opaque
data:
  token: c2VjcmV0
stringData:
  password: hidden
"""
    response = client.post(
        "/api/inventory/deploy/yaml/validate",
        headers=auth_headers(admin_token),
        json={"namespace": "default", "yaml": yaml_content},
    )
    assert response.status_code == 200
    preview = response.get_json()["data"]["preview"]
    assert "hidden" not in preview
    assert "c2VjcmV0" not in preview
    assert "Secret" in preview


def test_manifest_generator(client, admin_token):
    response = client.post(
        "/api/inventory/deploy/image/generate",
        headers=auth_headers(admin_token),
        json={
            "appName": "demo-app",
            "namespace": "default",
            "dockerImage": "nginx",
            "imageTag": "1.25",
            "replicas": 2,
            "containerPort": 80,
            "environmentVariables": {"FOO": "bar"},
        },
    )
    assert response.status_code == 200
    data = response.get_json()["data"]
    docs = list(yaml.safe_load_all(data["yaml"]))
    kinds = {d["kind"] for d in docs}
    assert "Deployment" in kinds
    assert "Service" in kinds
    assert "ConfigMap" in kinds
    assert data["summary"]["appName"] == "demo-app"


def test_deploy_blocked_without_namespace_access(client, admin_token):
    """Admin with full access should pass; verify forbidden path via mock."""
    with patch("api.services.deployment_service.can_access_namespace", return_value=False):
        response = client.post(
            "/api/inventory/deploy/yaml/dry-run",
            headers=auth_headers(admin_token),
            json={
                "clusterId": "prod-us-east",
                "namespace": "restricted-ns",
                "yaml": SAMPLE_DEPLOYMENT_YAML,
            },
        )
    assert response.status_code == 403


def test_inventory_list_still_works_for_viewer(client, viewer_token):
    response = client.get("/api/inventory", headers=auth_headers(viewer_token))
    assert response.status_code == 200
    assert isinstance(response.get_json()["data"], list)
