"""Helm release detail must not leak Secret data or chart credentials, and
/health must report liveness only."""

import json
from types import SimpleNamespace
from unittest.mock import patch

from api.services import helm_service
from api.services.helm_service import HIDDEN_HELM_VALUE, get_release_detail

MANIFEST = """apiVersion: v1
kind: Secret
metadata:
  name: db
data:
  password: c3VwZXJzZWNyZXQ=
---
apiVersion: apps/v1
kind: Deployment
metadata:
  name: api
"""

VALUES = {
    "replicaCount": 2,
    "image": {"tag": "1.2.3"},
    "postgresql": {"auth": {"username": "app", "password": "hunter2", "existingSecret": ""}},
    "apiKey": "abc",
    "DB_PASSWORD": "x",
    "monkey": "banana",
    "tls": {"privateKey": "-----BEGIN-----"},
    "extraEnv": [{"name": "SMTP_TOKEN", "token": "t0k"}],
}


def _runner(access, args):
    if args[:2] == ["get", "manifest"]:
        return MANIFEST
    if args[:2] == ["get", "values"]:
        return json.dumps(VALUES)
    return json.dumps({"info": {"status": "deployed"}, "version": 3})


def _detail(perms):
    user = SimpleNamespace(id=1) if perms is not None else None
    with patch.object(helm_service, "is_helm_installed", return_value=True), \
         patch.object(helm_service, "_resolve_access", return_value=None), \
         patch.object(helm_service, "user_has_permission", side_effect=lambda u, k: k in (perms or ())):
        return get_release_detail("c1", "payments", "api", user=user, run_helm_fn=_runner)


def test_without_reveal_manifest_has_no_secret_data_and_credentials_are_masked():
    detail = _detail({"helm:view", "helm:values:view"})
    assert "c3VwZXJzZWNyZXQ=" not in detail["manifest"]
    assert "c3VwZXJzZWNyZXQ=" not in detail["renderedManifest"]
    assert detail["secretValuesHidden"] is True and detail["valuesHidden"] is False
    values = detail["valuesSummary"]
    assert values["postgresql"]["auth"]["password"] == HIDDEN_HELM_VALUE
    assert values["postgresql"]["auth"]["username"] == "app"
    assert values["postgresql"]["auth"]["existingSecret"] == ""  # empty stays empty
    assert values["apiKey"] == HIDDEN_HELM_VALUE
    assert values["DB_PASSWORD"] == HIDDEN_HELM_VALUE
    assert values["tls"]["privateKey"] == HIDDEN_HELM_VALUE
    assert values["extraEnv"][0]["token"] == HIDDEN_HELM_VALUE
    assert values["monkey"] == "banana"
    assert values["replicaCount"] == 2 and values["image"]["tag"] == "1.2.3"


def test_without_values_permission_values_are_not_returned():
    detail = _detail({"helm:view"})
    assert detail["valuesSummary"] == {}
    assert detail["valuesHidden"] is True


def test_internal_caller_without_user_gets_the_redacted_form():
    detail = _detail(None)
    assert detail["valuesSummary"] == {}
    assert "c3VwZXJzZWNyZXQ=" not in detail["manifest"]


def test_reveal_returns_raw_manifest_and_values():
    detail = _detail({"helm:view", "helm:values:view", "secrets:reveal"})
    assert "c3VwZXJzZWNyZXQ=" in detail["manifest"]
    assert detail["valuesSummary"]["postgresql"]["auth"]["password"] == "hunter2"
    assert detail["secretValuesHidden"] is False


def test_health_reports_liveness_only(client):
    res = client.get("/health")
    assert res.status_code == 200
    data = res.get_json()["data"]
    assert data["status"] == "ok"
    assert data["database"] == "ok"
    assert "users" not in json.dumps(data)
