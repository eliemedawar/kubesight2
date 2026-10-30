"""Upgrading an installed release onto a stored chart while keeping its values."""

import json
from pathlib import Path
from unittest.mock import patch

import pytest
import yaml

from tests.conftest import auth_headers
from tests.test_helm_chart_templates import _import_archive
from tests.test_helm_values_import import CHART

pytestmark = pytest.mark.usefixtures("no_cluster_approvals")

# What `helm get values` returns for the installed release: only what was set.
CURRENT = {"replicaCount": 4, "database": {"password": "live-password"}, "extra": {"keep": "me"}}
TARGET = {"clusterId": "prod-us-east", "namespace": "default", "releaseName": "payments"}


class FakeHelm:
    def __init__(self, current=CURRENT):
        self.current = current
        self.calls = []
        self.values_files = []

    def __call__(self, access, args, extra_env=None):
        self.calls.append(list(args))
        if args[:2] == ["get", "values"]:
            return json.dumps(self.current)
        if "-f" in args:
            self.values_files.append(
                yaml.safe_load(Path(args[args.index("-f") + 1]).read_text(encoding="utf-8"))
            )
        if args[0] == "template":
            return "apiVersion: v1\nkind: ConfigMap\nmetadata:\n  name: payments\n"
        return "ok"


def _slug(client, token):
    return _import_archive(client, token, CHART, filename="payments-1.0.0.tgz").get_json()["data"]["id"]


def _helm(fake):
    return (
        patch("api.services.helm_service.is_helm_installed", return_value=True),
        patch("api.services.helm_service.run_helm", side_effect=fake),
    )


def test_upgrade_start_shows_current_values_and_keeps_secrets_server_side(client, admin_token):
    slug = _slug(client, admin_token)
    fake = FakeHelm()
    installed, runner = _helm(fake)
    with installed, runner:
        response = client.post(
            "/api/helm/releases/upgrade-start",
            headers=auth_headers(admin_token),
            json={**TARGET, "chartTemplateId": slug},
        )
    assert response.status_code == 200, response.get_json()
    data = response.get_json()["data"]
    assert data["fields"]["replicaCount"] == 4
    # Chart defaults fill what the release never set.
    assert data["fields"]["image.repository"] == "registry.example.internal/payments"
    assert data["fields"]["database.password"] == ""
    assert data["keptSecrets"] == ["database.password"]
    assert data["missingRequired"] == []
    assert "live-password" not in response.get_data(as_text=True)


def test_upgrade_keeping_values_layers_them_under_the_changes(client, admin_token):
    slug = _slug(client, admin_token)
    fake = FakeHelm()
    installed, runner = _helm(fake)
    with installed, runner:
        response = client.post(
            "/api/helm/upgrade",
            headers=auth_headers(admin_token),
            json={
                **TARGET,
                "chartSource": "template",
                "chartTemplateId": slug,
                "keepCurrentValues": True,
                "isUpgrade": True,
                # Only what the person changed is sent.
                "values": {"image.tag": "2.0.0"},
                "valuesYaml": "resources:\n  requests:\n    cpu: 900m\n",
                "confirmation": "UPGRADE payments IN default",
            },
        )
    assert response.status_code == 200, response.get_json()
    upgrade = next(call for call in fake.calls if call[0] == "upgrade")
    assert upgrade[:3] == ["upgrade", "--install", "payments"]
    applied = fake.values_files[-1]
    assert applied["replicaCount"] == 4
    assert applied["database"]["password"] == "live-password"
    assert applied["extra"] == {"keep": "me"}
    assert applied["image"]["tag"] == "2.0.0"
    assert applied["resources"]["requests"]["cpu"] == "900m"


def test_upgrade_refuses_when_current_values_cannot_be_read(client, admin_token):
    slug = _slug(client, admin_token)

    def broken(access, args, extra_env=None):
        if args[:2] == ["get", "values"]:
            return "not json"
        return "ok"

    with patch("api.services.helm_service.is_helm_installed", return_value=True):
        with patch("api.services.helm_service.run_helm", side_effect=broken):
            response = client.post(
                "/api/helm/upgrade",
                headers=auth_headers(admin_token),
                json={
                    **TARGET,
                    "chartSource": "template",
                    "chartTemplateId": slug,
                    "keepCurrentValues": True,
                    "isUpgrade": True,
                    "values": {},
                    "confirmation": "UPGRADE payments IN default",
                },
            )
    assert response.status_code == 400
    assert "could not be read" in response.get_json()["error"]


def test_values_import_counts_secrets_the_release_already_has(client, admin_token):
    slug = _slug(client, admin_token)
    fake = FakeHelm()
    installed, runner = _helm(fake)
    with installed, runner:
        report = client.post(
            f"/api/helm/chart-templates/{slug}/values-import",
            headers=auth_headers(admin_token),
            json={"valuesYaml": "replicaCount: 7\n", "keepCurrentValues": True, **TARGET},
        ).get_json()["data"]
    assert report["missingRequired"] == []
    assert report["fields"] == {"replicaCount": 7}


def test_upgrade_start_and_phrase_permissions(client, admin_token, viewer_token):
    slug = _slug(client, admin_token)
    denied = client.post(
        "/api/helm/releases/upgrade-start",
        headers=auth_headers(viewer_token),
        json={**TARGET, "chartTemplateId": slug},
    )
    assert denied.status_code == 403
    phrase = client.post(
        "/api/helm/confirmation-phrase",
        headers=auth_headers(admin_token),
        json={**TARGET, "isUpgrade": True, "keepCurrentValues": True},
    ).get_json()["data"]
    assert phrase["confirmation"] == "UPGRADE payments IN default"
