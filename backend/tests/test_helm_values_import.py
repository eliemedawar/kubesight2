"""Importing a person's values.yaml into a stored chart's deploy form."""

import yaml

from api.models import HelmChartTemplate
from api.services.helm_chart_template_service import _decode_files, build_values_yaml
from tests.conftest import auth_headers
from tests.test_helm_chart_templates import _import_archive


VALUES = """
replicaCount: 2
image:
  repository: registry.example.internal/payments
  tag: "1.0.0"
podAnnotations: {}
resources:
  requests:
    cpu: 100m
    memory: 128Mi
database:
  host: pg.internal
  password: must-not-persist
"""

CHART = {
    "payments/Chart.yaml": "apiVersion: v2\nname: payments\nversion: 1.0.0\n",
    "payments/values.yaml": VALUES,
    "payments/values-dev.yaml": "replicaCount: 3\n",
    "payments/templates/deployment.yaml": (
        "apiVersion: apps/v1\nkind: Deployment\nmetadata:\n  name: payments\n"
        "spec:\n  replicas: {{ .Values.replicaCount }}\n"
    ),
}

IMPORTED = """
replicaCount: 5
image:
  tag: "2.4.1"
resources:
  requests:
    cpu: 750m
database:
  password: s3cret-from-file
extraEnv:
  - name: LOG_LEVEL
    value: debug
ingress:
  apiToken: tok-123
"""


def _slug(client, token):
    response = _import_archive(client, token, CHART, filename="payments-1.0.0.tgz")
    assert response.status_code == 201, response.get_json()
    return response.get_json()["data"]["id"]


def _inspect(client, token, slug, text, **extra):
    return client.post(
        f"/api/helm/chart-templates/{slug}/values-import",
        headers=auth_headers(token),
        json={"valuesYaml": text, **extra},
    )


def test_import_fills_fields_including_secrets_and_reports_the_rest(client, admin_token):
    slug = _slug(client, admin_token)
    response = _inspect(client, admin_token, slug, IMPORTED)
    assert response.status_code == 200, response.get_json()
    report = response.get_json()["data"]

    fields = report["fields"]
    assert fields["replicaCount"] == 5
    assert fields["image.tag"] == "2.4.1"
    assert fields["database.password"] == "s3cret-from-file"
    # A key the file leaves alone keeps the chart default, not a blank.
    assert "image.repository" not in fields
    assert fields["resources.requests.cpu"] == "750m"

    assert set(report["secrets"]) == {"database.password", "ingress.apiToken"}
    assert report["secretCount"] == 2
    # Keys the chart never declares are flagged as possible typos (and still
    # applied at deploy time), once: not also as values without a field.
    assert set(report["unknown"]) == {"extraEnv", "ingress.apiToken"}
    assert report["withoutField"] == []
    assert report["missingRequired"] == []


def test_import_lists_required_values_the_file_leaves_empty(client, admin_token):
    slug = _slug(client, admin_token)
    report = _inspect(client, admin_token, slug, "replicaCount: 4\n").get_json()["data"]
    assert report["fields"] == {"replicaCount": 4}
    assert [item["path"] for item in report["missingRequired"]] == ["database.password"]


def test_import_merges_over_the_chosen_environment_file(client, admin_token):
    slug = _slug(client, admin_token)
    report = _inspect(
        client, admin_token, slug, "image:\n  tag: x\n", valuesFile="values-dev.yaml"
    ).get_json()["data"]
    assert report["valuesFile"] == "values-dev.yaml"
    assert report["fields"] == {"image.tag": "x"}


def test_import_rejects_files_that_are_not_a_values_mapping(client, admin_token):
    slug = _slug(client, admin_token)
    for text, message in (
        ("", "no values"),
        ("- a\n- b\n", "mapping"),
        ("a: [1, 2\n", "not valid YAML"),
        ("x: " + "a" * (1024 * 1024), "1 MiB"),
    ):
        response = _inspect(client, admin_token, slug, text)
        assert response.status_code == 400, text[:20]
        assert message in response.get_json()["error"]


def test_whole_imported_file_reaches_the_rendered_values(app, client, admin_token):
    """The deploy sends the file as valuesYaml: keys without a field apply too."""
    slug = _slug(client, admin_token)
    with app.app_context():
        rendered = yaml.safe_load(
            build_values_yaml(
                slug,
                {"database.password": "s3cret-from-file", "replicaCount": "5"},
                overlay_yaml=IMPORTED,
            )
        )
    assert rendered["replicaCount"] == 5
    assert rendered["extraEnv"] == [{"name": "LOG_LEVEL", "value": "debug"}]
    assert rendered["resources"]["requests"] == {"cpu": "750m", "memory": "128Mi"}
    assert rendered["image"]["repository"] == "registry.example.internal/payments"


def test_saved_values_file_blanks_secrets_and_becomes_selectable(app, client, admin_token):
    slug = _slug(client, admin_token)
    response = client.post(
        f"/api/helm/chart-templates/{slug}/values-files",
        headers=auth_headers(admin_token),
        json={"name": "values-prod.yaml", "valuesYaml": IMPORTED},
    )
    assert response.status_code == 201, response.get_json()
    data = response.get_json()["data"]
    assert data["savedValuesFile"] == "values-prod.yaml"
    assert set(data["secretsBlanked"]) == {"database.password", "ingress.apiToken"}
    stored = next(item for item in data["chart"]["valuesFiles"] if item["path"] == "values-prod.yaml")
    assert stored["values"]["database"]["password"] == ""
    assert stored["values"]["ingress"]["apiToken"] == ""
    assert stored["values"]["replicaCount"] == 5

    with app.app_context():
        row = HelmChartTemplate.query.filter_by(slug=slug).first()
        for definition in [row.definition] + [item.definition for item in row.versions]:
            text = _decode_files(definition)["values-prod.yaml"].decode("utf-8")
            assert "s3cret-from-file" not in text and "tok-123" not in text
        # Picked at deploy time it still asks for the password.
        report_values = yaml.safe_load(
            build_values_yaml(
                slug, {"database.password": "typed"}, values_file="values-prod.yaml"
            )
        )
        assert report_values["replicaCount"] == 5
        assert report_values["database"]["password"] == "typed"


def test_saving_over_an_existing_values_file_needs_replace(client, admin_token):
    slug = _slug(client, admin_token)
    url = f"/api/helm/chart-templates/{slug}/values-files"
    headers = auth_headers(admin_token)
    conflict = client.post(url, headers=headers, json={"name": "dev", "valuesYaml": "replicaCount: 8\n"})
    assert conflict.status_code == 409
    assert "values-dev.yaml already exists" in conflict.get_json()["error"]
    replaced = client.post(
        url, headers=headers, json={"name": "dev", "valuesYaml": "replicaCount: 8\n", "replace": True}
    )
    assert replaced.status_code == 201
    dev = next(item for item in replaced.get_json()["data"]["chart"]["valuesFiles"] if item["path"] == "values-dev.yaml")
    assert dev["values"] == {"replicaCount": 8}


def test_values_file_names_are_checked(client, admin_token):
    slug = _slug(client, admin_token)
    for name in ("", "../prod", "a b", "x" * 41):
        response = client.post(
            f"/api/helm/chart-templates/{slug}/values-files",
            headers=auth_headers(admin_token),
            json={"name": name, "valuesYaml": "replicaCount: 1\n"},
        )
        assert response.status_code == 400, name


def test_values_import_permissions(client, admin_token, viewer_token):
    slug = _slug(client, admin_token)
    # Checking a file stores nothing, so a chart viewer may do it.
    assert _inspect(client, viewer_token, slug, "replicaCount: 1\n").status_code == 200
    denied = client.post(
        f"/api/helm/chart-templates/{slug}/values-files",
        headers=auth_headers(viewer_token),
        json={"name": "prod", "valuesYaml": "replicaCount: 1\n"},
    )
    assert denied.status_code == 403
