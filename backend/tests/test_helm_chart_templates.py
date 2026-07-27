import base64
import io
import json
import tarfile
from pathlib import Path
from unittest.mock import patch

from api.services.helm_chart_template_service import (
    _scrub_static_secret_templates,
    build_values_yaml,
    chart_archive_base64,
)
from tests.conftest import auth_headers


DEPLOYMENT_DEV = """
apiVersion: apps/v1
kind: Deployment
metadata:
  name: payments
  namespace: dev
spec:
  replicas: 1
  selector:
    matchLabels:
      app: payments
  template:
    metadata:
      labels:
        app: payments
    spec:
      containers:
        - name: api
          image: registry.example/payments:1.0.0
          env:
            - name: LOG_LEVEL
              value: debug
            - name: API_TOKEN
              value: do-not-store-this
          resources:
            requests:
              cpu: 100m
              memory: 128Mi
"""


DEPLOYMENT_PROD_AND_SERVICE = """
apiVersion: apps/v1
kind: Deployment
metadata:
  name: payments
  namespace: prod
spec:
  replicas: 3
  selector:
    matchLabels:
      app: payments
  template:
    metadata:
      labels:
        app: payments
    spec:
      containers:
        - name: api
          image: registry.example/payments:2.0.0
          env:
            - name: LOG_LEVEL
              value: info
            - name: API_TOKEN
              value: another-secret
          resources:
            requests:
              cpu: 250m
              memory: 256Mi
---
apiVersion: v1
kind: Service
metadata:
  name: payments
spec:
  selector:
    app: payments
  ports:
    - port: 80
      targetPort: 8080
"""


SECRET = """
apiVersion: v1
kind: Secret
metadata:
  name: payments-credentials
type: Opaque
stringData:
  password: database-password-must-not-persist
"""


def test_static_secret_literals_in_existing_charts_are_replaced():
    files = {
        "templates/secret.yaml": (
            b"apiVersion: v1\nkind: Secret\nmetadata:\n  name: credentials\n"
            b"stringData:\n  password: literal-must-disappear\n"
        )
    }
    values = {}
    variables = []
    warnings = _scrub_static_secret_templates(files, values, variables)
    rendered = files["templates/secret.yaml"].decode("utf-8")
    assert "literal-must-disappear" not in rendered
    assert "{{ required" in rendered
    assert variables[0]["required"] is True
    assert variables[0]["sensitive"] is True
    assert warnings


def _import_yaml(client, token):
    return client.post(
        "/api/helm/chart-templates/import/yaml",
        headers=auth_headers(token),
        json={
            "name": "Payments Platform",
            "files": [
                {"name": "dev.yaml", "content": DEPLOYMENT_DEV},
                {"name": "prod.yaml", "content": DEPLOYMENT_PROD_AND_SERVICE},
                {"name": "secret.yaml", "content": SECRET},
            ],
        },
    )


def test_yaml_import_creates_reusable_chart_and_scrubs_secrets(client, admin_token, app):
    response = _import_yaml(client, admin_token)
    assert response.status_code == 201, response.get_json()
    imported = response.get_json()["data"]
    assert imported["sourceType"] == "yaml"
    assert imported["resourceCount"] == 3
    assert imported["variableCount"] >= 8
    assert imported["requiredVariableCount"] >= 2
    assert imported["warnings"]

    detail_response = client.get(
        f"/api/helm/chart-templates/{imported['id']}",
        headers=auth_headers(admin_token),
    )
    assert detail_response.status_code == 200
    detail = detail_response.get_json()["data"]
    serialized = json.dumps(detail)
    assert "do-not-store-this" not in serialized
    assert "another-secret" not in serialized
    assert "database-password-must-not-persist" not in serialized

    sensitive = [item for item in detail["variables"] if item["sensitive"]]
    assert sensitive
    assert all(item["required"] and item["default"] == "" for item in sensitive)

    answers = {item["path"]: "replacement-secret" for item in sensitive}
    with app.app_context():
        values_yaml = build_values_yaml(imported["id"], answers)
        archive_bytes = base64.b64decode(chart_archive_base64(imported["id"]))
    assert "replacement-secret" in values_yaml
    assert "do-not-store-this" not in values_yaml

    with tarfile.open(fileobj=io.BytesIO(archive_bytes), mode="r:gz") as archive:
        contents = "\n".join(
            archive.extractfile(member).read().decode("utf-8", errors="ignore")
            for member in archive.getmembers()
            if member.isfile()
        )
    assert "{{ .Values.variables." in contents
    assert "required \"" in contents
    assert "database-password-must-not-persist" not in contents


def test_template_catalog_permissions(client, admin_token, viewer_token):
    denied = client.post(
        "/api/helm/chart-templates/import/yaml",
        headers=auth_headers(viewer_token),
        json={"files": [{"name": "app.yaml", "content": DEPLOYMENT_DEV}]},
    )
    assert denied.status_code == 403

    _import_yaml(client, admin_token)
    listing = client.get(
        "/api/helm/chart-templates", headers=auth_headers(admin_token)
    )
    assert listing.status_code == 200
    assert len(listing.get_json()["data"]) == 1


def test_git_existing_chart_is_imported_and_token_is_not_persisted(
    client, admin_token
):
    def fake_clone(payload, destination, askpass_dir):
        root = Path(destination)
        (root / "templates").mkdir(parents=True)
        (root / "Chart.yaml").write_text(
            "apiVersion: v2\nname: orders\nversion: 1.2.3\n",
            encoding="utf-8",
        )
        (root / "values.yaml").write_text(
            "replicas: 2\nimage:\n  repository: example/orders\n"
            "  tag: latest\ncredentials:\n  password: should-be-removed\n",
            encoding="utf-8",
        )
        (root / "templates" / "deployment.yaml").write_text(
            "apiVersion: apps/v1\nkind: Deployment\nmetadata:\n"
            "  name: '{{ .Release.Name }}'\n",
            encoding="utf-8",
        )
        return payload["repositoryUrl"]

    with patch(
        "api.services.helm_chart_template_service._git_clone",
        side_effect=fake_clone,
    ):
        response = client.post(
            "/api/helm/chart-templates/import/git",
            headers=auth_headers(admin_token),
            json={
                "repositoryUrl": "https://git.example/team/platform.git",
                "token": "one-time-token",
                "importType": "auto",
            },
        )
    assert response.status_code == 201, response.get_json()
    data = response.get_json()["data"]
    assert data["sourceType"] == "git-chart"
    assert data["version"] == "1.2.3"
    assert "one-time-token" not in json.dumps(data)

    detail = client.get(
        f"/api/helm/chart-templates/{data['id']}",
        headers=auth_headers(admin_token),
    ).get_json()["data"]
    password = next(item for item in detail["variables"] if item["path"] == "credentials.password")
    assert password["required"] is True
    assert password["default"] == ""
    assert "should-be-removed" not in json.dumps(detail)


def test_saved_chart_uses_existing_helm_preview_pipeline(client, admin_token):
    imported = _import_yaml(client, admin_token).get_json()["data"]
    detail = client.get(
        f"/api/helm/chart-templates/{imported['id']}",
        headers=auth_headers(admin_token),
    ).get_json()["data"]
    values = {
        item["path"]: "safe-replacement"
        for item in detail["variables"]
        if item["required"]
    }
    rendered = (
        "apiVersion: v1\nkind: ConfigMap\nmetadata:\n"
        "  name: payments-preview\n  namespace: default\n"
    )

    with patch("api.services.helm_service.is_helm_installed", return_value=True):
        with patch("api.services.helm_service.run_helm", return_value=rendered):
            response = client.post(
                "/api/helm/template",
                headers=auth_headers(admin_token),
                json={
                    "chartSource": "template",
                    "chartTemplateId": imported["id"],
                    "clusterId": "prod-us-east",
                    "namespace": "default",
                    "releaseName": "payments",
                    "values": values,
                },
            )
    assert response.status_code == 200
    data = response.get_json()["data"]
    assert "payments-preview" in data["preview"]
