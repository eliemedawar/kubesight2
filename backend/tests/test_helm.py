"""Tests for Helm deployment workflows and RBAC."""

from unittest.mock import patch

from tests.conftest import auth_headers

from api.services.helm_service import (
    HELM_MISSING_MESSAGE,
    build_chart_ref,
    expected_confirmation,
    validate_release_name,
    validate_repo_url,
    render_template,
    dry_run_release,
)


def test_validate_release_name():
    assert validate_release_name("my-release")[0] is True
    assert validate_release_name("Bad_Name")[0] is False
    assert validate_release_name("")[0] is False


def test_validate_repo_url():
    assert validate_repo_url("https://charts.bitnami.com/bitnami")[0] is True
    assert validate_repo_url("ftp://bad.example.com")[0] is False
    assert validate_repo_url("not-a-url")[0] is False


def test_build_chart_ref_repository():
    ref, local, err = build_chart_ref({
        "chartSource": "repository",
        "repositoryName": "bitnami",
        "chartName": "nginx",
        "chartVersion": "15.0.0",
    })
    assert err is None
    assert local is None
    assert "bitnami/nginx" in ref
    assert "15.0.0" in ref


def test_expected_confirmation_phrases():
    assert expected_confirmation("nginx", "default", False) == "INSTALL nginx IN default"
    assert expected_confirmation("nginx", "default", True) == "UPGRADE nginx IN default"


def test_helm_missing_behavior():
    with patch("api.services.helm_service.is_helm_installed", return_value=False):
        data, err, code = render_template({
            "clusterId": "prod-us-east",
            "namespace": "default",
            "releaseName": "demo",
            "chartSource": "repository",
            "chartName": "nginx",
            "chartVersion": "1.0.0",
        })
    assert data is None
    assert HELM_MISSING_MESSAGE in (err or "")
    assert code == 503


def test_template_rendering_mocked(client, admin_token):
    mock_output = "apiVersion: v1\nkind: Service\nmetadata:\n  name: demo\n"

    def fake_helm(access, args, extra_env=None):
        if args[0] == "template":
            return mock_output
        return "ok"

    with patch("api.services.helm_service.is_helm_installed", return_value=True):
        with patch("api.services.helm_service.run_helm", side_effect=fake_helm):
            response = client.post(
                "/api/helm/template",
                headers=auth_headers(admin_token),
                json={
                    "clusterId": "prod-us-east",
                    "namespace": "default",
                    "releaseName": "demo",
                    "chartSource": "repository",
                    "repositoryName": "bitnami",
                    "repositoryUrl": "https://charts.bitnami.com/bitnami",
                    "chartName": "nginx",
                    "chartVersion": "15.0.0",
                    "valuesYaml": "replicaCount: 1\n",
                },
            )
    assert response.status_code == 200
    data = response.get_json()["data"]
    assert "preview" in data
    assert "Service" in data.get("preview", "")


def test_dry_run_mocked(client, admin_token):
    template_yaml = "apiVersion: v1\nkind: Service\nmetadata:\n  name: demo\n"

    def fake_helm(access, args, extra_env=None):
        if args and args[0] == "template":
            return template_yaml
        return "dry-run ok"

    with patch("api.services.helm_service.is_helm_installed", return_value=True):
        with patch("api.services.helm_service.run_helm", side_effect=fake_helm):
            with patch("api.services.helm_service.release_exists", return_value=False):
                response = client.post(
                    "/api/helm/dry-run",
                    headers=auth_headers(admin_token),
                    json={
                        "clusterId": "prod-us-east",
                        "namespace": "default",
                        "releaseName": "demo",
                        "chartSource": "repository",
                        "chartName": "nginx",
                        "chartVersion": "1.0.0",
                        "valuesYaml": "",
                    },
                )
    assert response.status_code == 200
    assert response.get_json()["data"]["dryRun"] is True


def test_install_requires_confirmation(client, admin_token):
    with patch("api.services.helm_service.is_helm_installed", return_value=True):
        with patch("api.services.helm_service.run_helm", return_value="installed"):
            with patch("api.services.helm_service.release_exists", return_value=False):
                bad = client.post(
                    "/api/helm/install",
                    headers=auth_headers(admin_token),
                    json={
                        "clusterId": "prod-us-east",
                        "namespace": "default",
                        "releaseName": "demo",
                        "chartSource": "repository",
                        "chartName": "nginx",
                        "chartVersion": "1.0.0",
                        "confirmation": "WRONG",
                    },
                )
                assert bad.status_code == 400

                ok = client.post(
                    "/api/helm/install",
                    headers=auth_headers(admin_token),
                    json={
                        "clusterId": "prod-us-east",
                        "namespace": "default",
                        "releaseName": "demo",
                        "chartSource": "repository",
                        "chartName": "nginx",
                        "chartVersion": "1.0.0",
                        "confirmation": "INSTALL demo IN default",
                    },
                )
                assert ok.status_code == 200


def test_operator_cannot_install(client, operator_token):
    response = client.post(
        "/api/helm/install",
        headers=auth_headers(operator_token),
        json={
            "clusterId": "prod-us-east",
            "namespace": "default",
            "releaseName": "demo",
            "chartSource": "repository",
            "chartName": "nginx",
            "chartVersion": "1.0.0",
            "confirmation": "INSTALL demo IN default",
        },
    )
    assert response.status_code == 403


def test_rollback_requires_permission(client, operator_token):
    response = client.post(
        "/api/helm/rollback",
        headers=auth_headers(operator_token),
        json={
            "clusterId": "prod-us-east",
            "namespace": "default",
            "releaseName": "demo",
        },
    )
    assert response.status_code == 403


def test_uninstall_requires_explicit_permission(client, admin_token):
    with patch("api.services.helm_service.is_helm_installed", return_value=True):
        with patch("api.services.helm_service.run_helm", return_value="uninstalled"):
            response = client.post(
                "/api/helm/uninstall",
                headers=auth_headers(admin_token),
                json={
                    "clusterId": "prod-us-east",
                    "namespace": "default",
                    "releaseName": "demo",
                },
            )
    assert response.status_code == 200


def test_inventory_includes_helm_release(client, admin_token):
    response = client.get("/api/inventory", headers=auth_headers(admin_token))
    items = response.get_json()["data"]
    helm_apps = [i for i in items if i.get("source") == "Helm"]
    assert helm_apps
    assert helm_apps[0].get("releaseName") == "prometheus"


def test_secret_values_not_exposed_in_helm_template(client, admin_token):
    secret_yaml = """apiVersion: v1
kind: Secret
metadata:
  name: demo-secret
type: Opaque
stringData:
  password: supersecret
"""

    def fake_helm(access, args, extra_env=None):
        if args[0] == "template":
            return secret_yaml
        return "ok"

    with patch("api.services.helm_service.is_helm_installed", return_value=True):
        with patch("api.services.helm_service.run_helm", side_effect=fake_helm):
            response = client.post(
                "/api/helm/template",
                headers=auth_headers(admin_token),
                json={
                    "clusterId": "prod-us-east",
                    "namespace": "default",
                    "releaseName": "demo",
                    "chartSource": "repository",
                    "chartName": "nginx",
                    "chartVersion": "1.0.0",
                },
            )
    preview = response.get_json()["data"]["preview"]
    assert "supersecret" not in preview


def test_dangerous_resource_warning(client, admin_token):
    cr_yaml = """apiVersion: rbac.authorization.k8s.io/v1
kind: ClusterRole
metadata:
  name: demo-role
rules: []
"""

    def fake_helm(access, args, extra_env=None):
        if args[0] == "template":
            return cr_yaml
        return "ok"

    with patch("api.services.helm_service.is_helm_installed", return_value=True):
        with patch("api.services.helm_service.run_helm", side_effect=fake_helm):
            response = client.post(
                "/api/helm/template",
                headers=auth_headers(admin_token),
                json={
                    "clusterId": "prod-us-east",
                    "namespace": "default",
                    "releaseName": "demo",
                    "chartSource": "repository",
                    "chartName": "nginx",
                    "chartVersion": "1.0.0",
                },
            )
    warnings = response.get_json()["data"].get("warnings") or []
    assert any("ClusterRole" in w for w in warnings)


def test_namespace_access_enforcement(client, admin_token):
    with patch("api.services.helm_service.is_helm_installed", return_value=True):
        with patch("api.services.helm_service.can_access_namespace", return_value=False):
            response = client.post(
                "/api/helm/dry-run",
                headers=auth_headers(admin_token),
                json={
                    "clusterId": "prod-us-east",
                    "namespace": "restricted",
                    "releaseName": "demo",
                    "chartSource": "repository",
                    "chartName": "nginx",
                    "chartVersion": "1.0.0",
                },
            )
    assert response.status_code == 403
