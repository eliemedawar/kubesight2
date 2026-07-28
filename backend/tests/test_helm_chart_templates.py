import base64
import io
import json
import tarfile
import zipfile
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


CHART_ENTRIES = {
    "areeba-txm/Chart.yaml": (
        "apiVersion: v2\nname: areeba-txm\nversion: 0.1.0\nappVersion: '1.4.2'\n"
        "type: application\ndescription: Transaction manager\n"
    ),
    "areeba-txm/values.yaml": (
        "replicas: 2\nimage:\n  repository: example/orders\n"
        "  tag: latest\ncredentials:\n  password: should-be-removed\n"
    ),
    "areeba-txm/values-prod.yaml": (
        "replicas: 6\ncredentials:\n  password: prod-password-must-vanish\n"
    ),
    "areeba-txm/values-uat.yml": "replicas: 2\n",
    "areeba-txm/templates/deployment.yaml": (
        "apiVersion: apps/v1\nkind: Deployment\nmetadata:\n  name: '{{ .Release.Name }}'\n"
    ),
    "areeba-txm/templates/service.yaml": (
        "apiVersion: v1\nkind: Service\nmetadata:\n  name: '{{ .Release.Name }}'\n"
    ),
    "areeba-txm/templates/_helpers.tpl": '{{- define "areeba-txm.name" -}}txm{{- end -}}',
    "__MACOSX/areeba-txm/._Chart.yaml": "junk",
    "areeba-txm/.DS_Store": "junk",
}


def _zip_base64(entries: dict, **kwargs) -> str:
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w", zipfile.ZIP_DEFLATED) as archive:
        for path, content in entries.items():
            archive.writestr(path, content, **kwargs)
    return base64.b64encode(stream.getvalue()).decode("ascii")


def _tgz_base64(entries: dict) -> str:
    stream = io.BytesIO()
    with tarfile.open(fileobj=stream, mode="w:gz") as archive:
        for path, content in entries.items():
            raw = content.encode("utf-8")
            info = tarfile.TarInfo(name=f"./{path}")
            info.size = len(raw)
            info.mode = 0o644
            archive.addfile(info, io.BytesIO(raw))
    return base64.b64encode(stream.getvalue()).decode("ascii")


def _import_archive(client, token, entries, **payload):
    return client.post(
        "/api/helm/chart-templates/import/archive",
        headers=auth_headers(token),
        json={
            "filename": "chart.zip",
            "archiveBase64": _zip_base64(entries),
            **payload,
        },
    )


def test_zip_archive_with_chart_is_imported_and_secrets_are_scrubbed(client, admin_token):
    response = _import_archive(client, admin_token, CHART_ENTRIES)
    assert response.status_code == 201, response.get_json()
    data = response.get_json()["data"]
    assert data["sourceType"] == "archive-chart"
    assert data["name"] == "areeba-txm"
    assert data["version"] == "0.1.0"
    assert data["appVersion"] == "1.4.2"
    assert data["description"] == "Transaction manager"
    assert data["sourceRef"] == "chart.zip"

    chart = data["chart"]
    assert chart["apiVersion"] == "v2"
    assert chart["type"] == "application"
    assert chart["hasChartYaml"] and chart["hasValuesYaml"]
    assert data["templateCount"] == 3
    kinds = {item["path"]: item["kinds"] for item in chart["templates"]}
    assert kinds["templates/deployment.yaml"] == ["Deployment"]
    assert kinds["templates/service.yaml"] == ["Service"]
    assert kinds["templates/_helpers.tpl"] == []

    # Environment values files are detected, described, and kept without secrets.
    assert data["valuesFileCount"] == 2
    environments = {item["path"]: item for item in chart["valuesFiles"]}
    assert set(environments) == {"values-prod.yaml", "values-uat.yml"}
    assert environments["values-prod.yaml"]["environment"] == "prod"
    assert environments["values-prod.yaml"]["keyCount"] == 2
    assert environments["values-uat.yml"]["environment"] == "uat"
    assert "prod-password-must-vanish" not in json.dumps(data)

    detail = client.get(
        f"/api/helm/chart-templates/{data['id']}",
        headers=auth_headers(admin_token),
    ).get_json()["data"]
    password = next(
        item for item in detail["variables"] if item["path"] == "credentials.password"
    )
    assert password["required"] is True
    assert "should-be-removed" not in json.dumps(detail)


def test_tgz_archive_is_imported_with_chart_metadata(client, admin_token, app):
    response = client.post(
        "/api/helm/chart-templates/import/archive",
        headers=auth_headers(admin_token),
        json={
            "filename": "areeba-txm-0.1.0-323a1ece9b87.tgz",
            "archiveBase64": _tgz_base64(CHART_ENTRIES),
        },
    )
    assert response.status_code == 201, response.get_json()
    data = response.get_json()["data"]
    assert data["sourceType"] == "archive-chart"
    assert data["name"] == "areeba-txm"
    assert data["version"] == "0.1.0"
    assert data["sourceRef"] == "areeba-txm-0.1.0-323a1ece9b87.tgz"
    assert data["templateCount"] == 3
    assert data["valuesFileCount"] == 2
    assert "prod-password-must-vanish" not in json.dumps(data)

    # The detected environment values files stay part of the deployable package.
    with app.app_context():
        packaged = base64.b64decode(chart_archive_base64(data["id"]))
    with tarfile.open(fileobj=io.BytesIO(packaged), mode="r:gz") as archive:
        names = archive.getnames()
        contents = "\n".join(
            archive.extractfile(member).read().decode("utf-8", errors="ignore")
            for member in archive.getmembers()
            if member.isfile()
        )
    assert any(name.endswith("values-prod.yaml") for name in names)
    assert any(name.endswith("templates/deployment.yaml") for name in names)
    assert "prod-password-must-vanish" not in contents


def test_zip_archive_of_raw_manifests_is_converted(client, admin_token):
    response = _import_archive(
        client,
        admin_token,
        {
            "manifests/dev.yaml": DEPLOYMENT_DEV,
            "manifests/prod.yaml": DEPLOYMENT_PROD_AND_SERVICE,
            "manifests/secret.yaml": SECRET,
        },
        name="Payments Platform",
    )
    assert response.status_code == 201, response.get_json()
    data = response.get_json()["data"]
    assert data["sourceType"] == "archive-yaml"
    assert data["resourceCount"] == 3

    detail = client.get(
        f"/api/helm/chart-templates/{data['id']}",
        headers=auth_headers(admin_token),
    ).get_json()["data"]
    serialized = json.dumps(detail)
    assert "do-not-store-this" not in serialized
    assert "database-password-must-not-persist" not in serialized


def test_archive_with_multiple_charts_is_resolved_by_path(client, admin_token):
    entries = {
        "bundle/charts/alpha/Chart.yaml": "apiVersion: v2\nname: alpha\nversion: 1.0.0\n",
        "bundle/charts/beta/Chart.yaml": "apiVersion: v2\nname: beta\nversion: 2.0.0\n",
        "bundle/charts/beta/templates/configmap.yaml": (
            "apiVersion: v1\nkind: ConfigMap\nmetadata:\n  name: beta\n"
        ),
    }
    ambiguous = _import_archive(client, admin_token, entries)
    assert ambiguous.status_code == 400
    assert "Multiple Helm charts" in ambiguous.get_json()["error"]

    missing_path = _import_archive(client, admin_token, entries, path="bundle/charts/gamma")
    assert missing_path.status_code == 400
    assert "does not exist in the archive" in missing_path.get_json()["error"]

    resolved = _import_archive(client, admin_token, entries, path="bundle/charts/beta")
    assert resolved.status_code == 201, resolved.get_json()
    data = resolved.get_json()["data"]
    assert data["name"] == "beta"
    assert data["version"] == "2.0.0"
    assert data["sourceRef"] == "chart.zip:bundle/charts/beta"
    assert data["templateCount"] == 1
    assert any("no values.yaml" in warning for warning in data["warnings"])


def test_zip_archive_rejects_traversal_and_bad_input(client, admin_token):
    traversal = _import_archive(
        client, admin_token, {"../escaped/Chart.yaml": "apiVersion: v2\nname: bad\n"}
    )
    assert traversal.status_code == 400
    assert "escapes the archive root" in traversal.get_json()["error"]

    not_a_zip = client.post(
        "/api/helm/chart-templates/import/archive",
        headers=auth_headers(admin_token),
        json={
            "filename": "chart.zip",
            "archiveBase64": base64.b64encode(b"not a zip file").decode("ascii"),
        },
    )
    assert not_a_zip.status_code == 400
    assert "not a valid .zip archive" in not_a_zip.get_json()["error"]

    wrong_extension = _import_archive(
        client, admin_token, {"app.yaml": DEPLOYMENT_DEV}, filename="chart.rar"
    )
    assert wrong_extension.status_code == 400
    assert "are supported" in wrong_extension.get_json()["error"]

    not_an_archive = client.post(
        "/api/helm/chart-templates/import/archive",
        headers=auth_headers(admin_token),
        json={
            "filename": "chart.tgz",
            "archiveBase64": base64.b64encode(b"plain text, not compressed").decode("ascii"),
        },
    )
    assert not_an_archive.status_code == 400
    assert "not a recognised archive" in not_an_archive.get_json()["error"]

    empty = _import_archive(client, admin_token, {"__MACOSX/._app.yaml": "junk"})
    assert empty.status_code == 400
    assert "importable files" in empty.get_json()["error"]

    missing = client.post(
        "/api/helm/chart-templates/import/archive",
        headers=auth_headers(admin_token),
        json={"filename": "chart.zip"},
    )
    assert missing.status_code == 400
    assert "required" in missing.get_json()["error"]


def test_zip_archive_import_requires_permission(client, viewer_token):
    response = _import_archive(client, viewer_token, {"app.yaml": DEPLOYMENT_DEV})
    assert response.status_code == 403


def test_archive_rejects_decompression_bombs_in_both_formats(client, admin_token):
    oversized = "a: b\n" * 3_000_000  # ~14 MiB uncompressed, compresses tiny
    for filename, encoder in (("chart.zip", _zip_base64), ("chart.tgz", _tgz_base64)):
        response = client.post(
            "/api/helm/chart-templates/import/archive",
            headers=auth_headers(admin_token),
            json={
                "filename": filename,
                "archiveBase64": encoder({"bomb.yaml": oversized}),
            },
        )
        assert response.status_code == 400
        assert "10 MiB import limit" in response.get_json()["error"]


def test_tgz_archive_rejects_symlinked_members(client, admin_token):
    stream = io.BytesIO()
    with tarfile.open(fileobj=stream, mode="w:gz") as archive:
        chart = b"apiVersion: v2\nname: linked\nversion: 1.0.0\n"
        info = tarfile.TarInfo(name="linked/Chart.yaml")
        info.size = len(chart)
        archive.addfile(info, io.BytesIO(chart))
        link = tarfile.TarInfo(name="linked/passwd")
        link.type = tarfile.SYMTYPE
        link.linkname = "/etc/passwd"
        archive.addfile(link)
    response = client.post(
        "/api/helm/chart-templates/import/archive",
        headers=auth_headers(admin_token),
        json={
            "filename": "linked.tgz",
            "archiveBase64": base64.b64encode(stream.getvalue()).decode("ascii"),
        },
    )
    assert response.status_code == 400
    assert "link" in response.get_json()["error"]


def _chart_entries(version: str, *, replicas: int = 2, extra: dict | None = None) -> dict:
    entries = {
        "areeba-txm/Chart.yaml": (
            f"apiVersion: v2\nname: areeba-txm\nversion: {version}\n"
            f"appVersion: '{version}'\ndescription: Transaction manager {version}\n"
        ),
        "areeba-txm/values.yaml": f"replicaCount: {replicas}\n",
        "areeba-txm/templates/deployment.yaml": (
            "apiVersion: apps/v1\nkind: Deployment\nmetadata:\n  name: txm\n"
            "spec:\n  replicas: {{ .Values.replicaCount }}\n"
        ),
    }
    entries.update(extra or {})
    return entries


def test_chart_versions_accumulate_and_track_the_current_one(client, admin_token, app):
    first = _import_archive(
        client, admin_token, _chart_entries("0.1.0"), filename="areeba-txm-0.1.0.tgz"
    )
    assert first.status_code == 201, first.get_json()
    slug = first.get_json()["data"]["id"]
    assert first.get_json()["data"]["versionCount"] == 1

    second = client.post(
        f"/api/helm/chart-templates/{slug}/versions",
        headers=auth_headers(admin_token),
        json={
            "filename": "areeba-txm-0.2.0.tgz",
            "archiveBase64": _tgz_base64(
                _chart_entries(
                    "0.2.0",
                    replicas=5,
                    extra={
                        "areeba-txm/templates/service.yaml": (
                            "apiVersion: v1\nkind: Service\nmetadata:\n  name: txm\n"
                        ),
                        "areeba-txm/values-prod.yaml": "replicaCount: 9\n",
                    },
                )
            ),
        },
    )
    assert second.status_code == 201, second.get_json()
    data = second.get_json()["data"]
    assert data["versionCount"] == 2
    assert data["version"] == "0.2.0", "the newest upload becomes current"
    assert data["templateCount"] == 2
    assert data["valuesFileCount"] == 1
    versions = {item["version"]: item for item in data["versions"]}
    assert set(versions) == {"0.1.0", "0.2.0"}
    assert versions["0.2.0"]["isCurrent"] is True
    assert versions["0.1.0"]["isCurrent"] is False
    assert versions["0.1.0"]["templateCount"] == 1
    assert versions["0.1.0"]["sourceRef"] == "areeba-txm-0.1.0.tgz"

    # Re-uploading the same chart version is refused rather than silently kept.
    duplicate = client.post(
        f"/api/helm/chart-templates/{slug}/versions",
        headers=auth_headers(admin_token),
        json={
            "filename": "areeba-txm-0.2.0.tgz",
            "archiveBase64": _tgz_base64(_chart_entries("0.2.0")),
        },
    )
    assert duplicate.status_code == 400
    assert "already exists" in duplicate.get_json()["error"]

    # The listing keeps one card per chart, with the version history attached.
    listing = client.get(
        "/api/helm/chart-templates", headers=auth_headers(admin_token)
    ).get_json()["data"]
    entry = next(item for item in listing if item["id"] == slug)
    assert entry["versionCount"] == 2
    assert entry["version"] == "0.2.0"

    # Older versions stay deployable exactly as they were uploaded.
    older = client.get(
        f"/api/helm/chart-templates/{slug}?version=0.1.0",
        headers=auth_headers(admin_token),
    ).get_json()["data"]
    assert older["version"] == "0.1.0"
    assert older["templateCount"] == 1
    with app.app_context():
        packaged = base64.b64decode(chart_archive_base64(slug, "0.1.0"))
        current = base64.b64decode(chart_archive_base64(slug))
        assert "replicaCount: 2" in build_values_yaml(slug, {}, "0.1.0")
        assert "replicaCount: 5" in build_values_yaml(slug, {})
    with tarfile.open(fileobj=io.BytesIO(packaged), mode="r:gz") as archive:
        assert not any(name.endswith("service.yaml") for name in archive.getnames())
    with tarfile.open(fileobj=io.BytesIO(current), mode="r:gz") as archive:
        assert any(name.endswith("service.yaml") for name in archive.getnames())

    missing = client.get(
        f"/api/helm/chart-templates/{slug}?version=9.9.9",
        headers=auth_headers(admin_token),
    )
    assert missing.status_code == 404


def test_chart_version_can_be_reselected_and_deleted(client, admin_token):
    slug = _import_archive(
        client, admin_token, _chart_entries("1.0.0"), filename="txm-1.0.0.tgz"
    ).get_json()["data"]["id"]
    client.post(
        f"/api/helm/chart-templates/{slug}/versions",
        headers=auth_headers(admin_token),
        json={
            "filename": "txm-1.1.0.tgz",
            "archiveBase64": _tgz_base64(_chart_entries("1.1.0", replicas=4)),
        },
    )

    rolled_back = client.post(
        f"/api/helm/chart-templates/{slug}/versions/1.0.0/current",
        headers=auth_headers(admin_token),
    )
    assert rolled_back.status_code == 200
    assert rolled_back.get_json()["data"]["version"] == "1.0.0"

    removed = client.delete(
        f"/api/helm/chart-templates/{slug}/versions/1.1.0",
        headers=auth_headers(admin_token),
    )
    assert removed.status_code == 200
    assert removed.get_json()["data"]["versionCount"] == 1

    last = client.delete(
        f"/api/helm/chart-templates/{slug}/versions/1.0.0",
        headers=auth_headers(admin_token),
    )
    assert last.status_code == 400
    assert "only version" in last.get_json()["error"]


def test_chart_version_endpoints_require_permission(client, admin_token, viewer_token):
    slug = _import_archive(
        client, admin_token, _chart_entries("1.0.0"), filename="txm-1.0.0.tgz"
    ).get_json()["data"]["id"]
    denied = client.post(
        f"/api/helm/chart-templates/{slug}/versions",
        headers=auth_headers(viewer_token),
        json={
            "filename": "txm-2.0.0.tgz",
            "archiveBase64": _tgz_base64(_chart_entries("2.0.0")),
        },
    )
    assert denied.status_code == 403
    assert (
        client.delete(
            f"/api/helm/chart-templates/{slug}/versions/1.0.0",
            headers=auth_headers(viewer_token),
        ).status_code
        == 403
    )


def test_deploy_pipeline_uses_the_requested_chart_version(client, admin_token):
    slug = _import_archive(
        client, admin_token, _chart_entries("0.1.0"), filename="txm-0.1.0.tgz"
    ).get_json()["data"]["id"]
    client.post(
        f"/api/helm/chart-templates/{slug}/versions",
        headers=auth_headers(admin_token),
        json={
            "filename": "txm-0.3.0.tgz",
            "archiveBase64": _tgz_base64(_chart_entries("0.3.0", replicas=7)),
        },
    )
    rendered = "apiVersion: v1\nkind: ConfigMap\nmetadata:\n  name: preview\n"
    captured = {}

    def fake_run_helm(access, args, **kwargs):
        values_path = args[args.index("-f") + 1] if "-f" in args else ""
        captured["values"] = Path(values_path).read_text(encoding="utf-8") if values_path else ""
        captured["args"] = list(args)
        return rendered

    with patch("api.services.helm_service.is_helm_installed", return_value=True):
        with patch("api.services.helm_service.run_helm", side_effect=fake_run_helm):
            response = client.post(
                "/api/helm/template",
                headers=auth_headers(admin_token),
                json={
                    "chartSource": "template",
                    "chartTemplateId": slug,
                    "chartVersion": "0.1.0",
                    "clusterId": "prod-us-east",
                    "namespace": "default",
                    "releaseName": "txm",
                    "values": {},
                },
            )
    assert response.status_code == 200, response.get_json()
    assert "replicaCount: 2" in captured["values"], captured
    assert "replicaCount: 7" not in captured["values"]


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
