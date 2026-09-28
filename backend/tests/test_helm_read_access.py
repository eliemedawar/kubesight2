"""The REST Helm read routes are scoped to the caller's clusters and namespaces.

A user with helm:view limited to one namespace must not be able to list every
release on a cluster (``helm list -A``), read a release in a namespace they
cannot see, or run helm against a cluster they have no access to.
"""

import json
from unittest.mock import patch

import pytest

from api.db import db
from api.models import AccessRule, User
from tests.conftest import auth_headers

CLUSTER = "prod-us-east"
OTHER_CLUSTER = "staging-eu-west"

RELEASES = [
    {"name": "pay-api", "namespace": "payments", "chart": "api-1.0.0", "status": "deployed"},
    {"name": "prometheus", "namespace": "monitoring", "chart": "prometheus-25.8.0", "status": "deployed"},
    {"name": "checkout", "namespace": "checkout", "chart": "web-2.0.0", "status": "deployed"},
]


def _fake_helm(calls):
    def runner(access, args, extra_env=None):
        calls.append(list(args))
        if args[0] == "list":
            if "-n" in args:
                ns = args[args.index("-n") + 1]
                return json.dumps([r for r in RELEASES if r["namespace"] == ns])
            return json.dumps(RELEASES)
        if args[0] == "status":
            return json.dumps({"info": {"status": "deployed"}, "version": 1})
        if args[:2] == ["get", "values"]:
            return "{}"
        if args[0] == "repo":
            return "[]"
        if args[0] == "search":
            return "[]"
        return ""

    return runner


@pytest.fixture()
def helm_calls():
    calls = []
    with patch("api.services.helm_service.is_helm_installed", return_value=True), \
         patch("api.services.helm_service._resolve_access", return_value=None), \
         patch("api.services.helm_service.run_helm", side_effect=_fake_helm(calls)):
        yield calls


@pytest.fixture()
def restricted_viewer(client, admin_token):
    """The viewer, limited to prod-us-east / payments."""
    users = client.get("/api/users", headers=auth_headers(admin_token)).get_json()["data"]["items"]
    viewer_id = next(u for u in users if u["username"] == "viewer")["id"]
    res = client.put(
        f"/api/users/{viewer_id}",
        headers=auth_headers(admin_token),
        json={
            "clusterAccess": [CLUSTER],
            "namespaceAccess": [{"clusterId": CLUSTER, "namespace": "payments"}],
        },
    )
    assert res.status_code == 200
    db.session.expire_all()
    assert AccessRule.query.filter_by(user_id=viewer_id).count() >= 2
    login = client.post("/api/auth/login", json={"username": "viewer", "password": "viewer123"})
    return login.get_json()["data"]["token"]


def _names(res):
    assert res.status_code == 200, res.get_json()
    return sorted(r["name"] for r in res.get_json()["data"])


def test_all_namespaces_list_is_filtered_to_the_users_namespaces(client, restricted_viewer, helm_calls):
    res = client.get(f"/api/helm/releases?cluster={CLUSTER}", headers=auth_headers(restricted_viewer))
    assert _names(res) == ["pay-api"]


def test_admin_sees_every_release(client, admin_token, helm_calls):
    res = client.get(f"/api/helm/releases?cluster={CLUSTER}", headers=auth_headers(admin_token))
    assert _names(res) == ["checkout", "pay-api", "prometheus"]


def test_list_in_own_namespace_is_allowed(client, restricted_viewer, helm_calls):
    res = client.get(
        f"/api/helm/releases?cluster={CLUSTER}&namespace=payments", headers=auth_headers(restricted_viewer)
    )
    assert _names(res) == ["pay-api"]


def test_list_in_other_namespace_is_forbidden(client, restricted_viewer, helm_calls):
    res = client.get(
        f"/api/helm/releases?cluster={CLUSTER}&namespace=monitoring", headers=auth_headers(restricted_viewer)
    )
    assert res.status_code == 403
    assert not helm_calls


def test_list_on_other_cluster_is_forbidden(client, restricted_viewer, helm_calls):
    res = client.get(f"/api/helm/releases?cluster={OTHER_CLUSTER}", headers=auth_headers(restricted_viewer))
    assert res.status_code == 403
    assert not helm_calls


def test_release_detail_in_other_namespace_is_forbidden(client, restricted_viewer, helm_calls):
    res = client.get(
        f"/api/helm/releases/prometheus?cluster={CLUSTER}&namespace=monitoring",
        headers=auth_headers(restricted_viewer),
    )
    assert res.status_code == 403
    assert not helm_calls


def test_release_detail_on_other_cluster_is_forbidden(client, restricted_viewer, helm_calls):
    res = client.get(
        f"/api/helm/releases/pay-api?cluster={OTHER_CLUSTER}&namespace=payments",
        headers=auth_headers(restricted_viewer),
    )
    assert res.status_code == 403
    assert not helm_calls


def test_release_detail_in_own_namespace_is_allowed(client, restricted_viewer, helm_calls):
    res = client.get(
        f"/api/helm/releases/pay-api?cluster={CLUSTER}&namespace=payments",
        headers=auth_headers(restricted_viewer),
    )
    assert res.status_code == 200
    assert res.get_json()["data"]["releaseName"] == "pay-api"


def test_admin_reads_release_detail_anywhere(client, admin_token, helm_calls):
    res = client.get(
        f"/api/helm/releases/prometheus?cluster={CLUSTER}&namespace=monitoring",
        headers=auth_headers(admin_token),
    )
    assert res.status_code == 200


@pytest.mark.parametrize("path", ["/api/helm/repos", "/api/helm/charts?repo=bitnami"])
def test_repo_and_chart_reads_need_cluster_access(client, restricted_viewer, helm_calls, path):
    sep = "&" if "?" in path else "?"
    res = client.get(f"{path}{sep}cluster={OTHER_CLUSTER}", headers=auth_headers(restricted_viewer))
    assert res.status_code == 403
    assert not helm_calls
    res = client.get(f"{path}{sep}cluster={CLUSTER}", headers=auth_headers(restricted_viewer))
    assert res.status_code == 200


def test_repo_add_needs_cluster_access(client, admin_token, helm_calls):
    users = client.get("/api/users", headers=auth_headers(admin_token)).get_json()["data"]["items"]
    operator_id = next(u for u in users if u["username"] == "operator")["id"]
    res = client.put(
        f"/api/users/{operator_id}",
        headers=auth_headers(admin_token),
        json={"clusterAccess": [CLUSTER], "namespaceAccess": [{"clusterId": CLUSTER, "namespace": "payments"}]},
    )
    assert res.status_code == 200
    token = client.post(
        "/api/auth/login", json={"username": "operator", "password": "operator123"}
    ).get_json()["data"]["token"]
    res = client.post(
        "/api/helm/repos",
        headers=auth_headers(token),
        json={"clusterId": OTHER_CLUSTER, "repositoryName": "bitnami", "repositoryUrl": "https://charts.bitnami.com/bitnami"},
    )
    assert res.status_code == 403
    assert not helm_calls


def test_service_filters_for_user_and_keeps_internal_callers_unscoped(app, restricted_viewer, helm_calls):
    from api.services.helm_service import get_release_detail, list_releases

    viewer = User.query.filter_by(username="viewer").first()
    admin = User.query.filter_by(username="admin").first()
    assert [r["name"] for r in list_releases(CLUSTER, user=viewer)] == ["pay-api"]
    assert list_releases(CLUSTER, "monitoring", user=viewer) == []
    assert list_releases(OTHER_CLUSTER, user=viewer) == []
    assert len(list_releases(CLUSTER, user=admin)) == 3
    assert len(list_releases(CLUSTER)) == 3  # internal caller
    assert get_release_detail(CLUSTER, "monitoring", "prometheus", user=viewer) is None


def test_mcp_helm_release_list_is_filtered(app, restricted_viewer, helm_calls):
    from api.mcp.tools import deploys

    viewer = User.query.filter_by(username="viewer").first()
    with patch.object(deploys, "_user", return_value=viewer), \
         patch.object(deploys, "resolve_cluster", side_effect=lambda user, c: c):
        out = deploys._helm_releases({"cluster": CLUSTER})
    assert [r["name"] for r in out["releases"]] == ["pay-api"]


def test_inventory_hides_helm_releases_outside_the_users_namespaces(client, restricted_viewer, admin_token):
    res = client.get(f"/api/inventory?cluster={CLUSTER}", headers=auth_headers(restricted_viewer))
    assert res.status_code == 200
    body = res.get_json()["data"]
    items = body["items"] if isinstance(body, dict) else body
    assert all(i.get("namespace") == "payments" for i in items)
    assert not any(i.get("releaseName") == "prometheus" for i in items)

    res = client.get(f"/api/inventory?cluster={CLUSTER}", headers=auth_headers(admin_token))
    body = res.get_json()["data"]
    items = body["items"] if isinstance(body, dict) else body
    assert any(i.get("releaseName") == "prometheus" for i in items)
