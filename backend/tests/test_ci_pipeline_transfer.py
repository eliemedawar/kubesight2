"""Exporting a pipeline from one KubeSight and importing it into another.

What is locked here:

* the file carries the stages, build inputs and post actions — never a secret
  value, never an authority stamp — plus the secret names and deploy clusters
  the other side has to answer for;
* import maps each deploy cluster to one over here, and refuses an unmapped or
  unknown one;
* secrets the service lacks are created with the value typed at import (or
  empty, flagged), in the same transaction as the save: a refused save leaves
  none behind;
* the importer is who the Deploy stage runs as;
* a pipeline that builds with a shared pipeline links to the one of the same
  slug when it exists here.
"""

from __future__ import annotations

import yaml

from api.db import db
from api.models_ci import CiPipeline, CiSecret
from api.secret_encryption import decrypt_secret
from tests.conftest import auth_headers
from tests.test_ci_deploy_stage import _deploy, _save, service  # noqa: F401 — ``service`` is a fixture

STAGES = [
    {"name": "Checkout", "stageType": "checkout", "runnerLabels": ["mock"]},
    {
        "name": "Build",
        "stageType": "command",
        "commands": ["./gradlew build"],
        "runnerLabels": ["mock"],
        "secretRefs": [{"name": "NEXUS_PASSWORD", "envVar": "NEXUS_PASSWORD"}],
    },
    {"name": "Deploy", "stageType": "deploy", "deploy": _deploy()},
]


def _secret(client, token, service_id, key, value):
    response = client.post(
        f"/api/ci/services/{service_id}/secrets",
        json={"key": key, "value": value},
        headers=auth_headers(token),
    )
    assert response.status_code in (200, 201), response.get_json()


def _other_pipeline(client, token, name="Payments Api Prod"):
    """The service on the receiving side: registered, nothing customised."""
    service_id = client.post(
        "/api/ci/services", json={"name": name, "applicationType": "java"}, headers=auth_headers(token)
    ).get_json()["data"]["id"]
    pipeline_id = client.get(
        f"/api/ci/services/{service_id}/pipelines", headers=auth_headers(token)
    ).get_json()["data"]["items"][0]["id"]
    return service_id, pipeline_id


def _export(client, token, pipeline_id):
    response = client.get(f"/api/ci/pipelines/{pipeline_id}/export", headers=auth_headers(token))
    assert response.status_code == 200, response.get_json()
    return response.get_json()["data"]


def _import(client, token, pipeline_id, content, **body):
    return client.post(
        f"/api/ci/pipelines/{pipeline_id}/import",
        json={"content": content, **body},
        headers=auth_headers(token),
    )


def _exported(client, token, service):
    _secret(client, token, service.id, "NEXUS_PASSWORD", "uat-only-value")
    response = _save(client, token, service.pipeline_id, STAGES)
    assert response.status_code == 200, response.get_json()
    return _export(client, token, service.pipeline_id)


def test_the_file_never_carries_a_secret_value_or_a_stamp(client, admin_token, service):
    data = _exported(client, admin_token, service)
    assert data["filename"].endswith(".pipeline.yaml")
    assert "uat-only-value" not in data["content"]
    assert "authorizedBy" not in data["content"]
    document = yaml.safe_load(data["content"])
    assert document["kind"] == "KubeSightPipeline"
    assert [stage["name"] for stage in document["pipeline"]["stages"]] == ["Checkout", "Build", "Deploy"]
    assert document["secrets"] == ["NEXUS_PASSWORD"]
    assert document["clusters"][0]["id"] == "prod-us-east"
    assert document["clusters"][0]["name"]


def test_preview_lists_what_the_other_side_must_answer(client, admin_token, service):
    content = _exported(client, admin_token, service)["content"]
    _, pipeline_id = _other_pipeline(client, admin_token)
    response = client.post(
        f"/api/ci/pipelines/{pipeline_id}/import/preview",
        json={"content": content},
        headers=auth_headers(admin_token),
    )
    assert response.status_code == 200, response.get_json()
    data = response.get_json()["data"]
    assert [stage["name"] for stage in data["stages"]] == ["Checkout", "Build", "Deploy"]
    assert data["secrets"] == [{"name": "NEXUS_PASSWORD", "scope": "", "missing": True}]
    assert data["clusters"][0]["id"] == "prod-us-east"


def test_import_maps_the_cluster_and_creates_the_secret(app, client, admin_token, service):
    content = _exported(client, admin_token, service)["content"]
    service_id, pipeline_id = _other_pipeline(client, admin_token)

    unmapped = _import(client, admin_token, pipeline_id, content)
    assert unmapped.status_code == 400
    assert "Pick the cluster" in unmapped.get_json()["error"]
    unknown = _import(client, admin_token, pipeline_id, content, clusterMap={"prod-us-east": "nowhere"})
    assert unknown.status_code == 400 and "no cluster 'nowhere'" in unknown.get_json()["error"]

    response = _import(
        client, admin_token, pipeline_id, content,
        clusterMap={"prod-us-east": "staging-eu-west"},
        secretValues={"NEXUS_PASSWORD": "prod-value"},
    )
    assert response.status_code == 200, response.get_json()
    data = response.get_json()["data"]
    assert data["secretsCreated"] == [{"name": "NEXUS_PASSWORD", "empty": False}]
    deploy = data["pipeline"]["stages"][2]["deploy"]
    assert deploy["clusterId"] == "staging-eu-west"
    assert deploy["authorizedBy"]["username"] == "admin"
    with app.app_context():
        secret = CiSecret.query.filter_by(service_id=service_id, key="NEXUS_PASSWORD").one()
        assert decrypt_secret(secret.value_cipher) == "prod-value"


def test_a_secret_left_empty_is_created_empty_and_flagged(app, client, admin_token, service):
    content = _exported(client, admin_token, service)["content"]
    service_id, pipeline_id = _other_pipeline(client, admin_token)
    response = _import(client, admin_token, pipeline_id, content, clusterMap={"prod-us-east": "prod-us-east"})
    assert response.status_code == 200, response.get_json()
    assert response.get_json()["data"]["secretsCreated"] == [{"name": "NEXUS_PASSWORD", "empty": True}]
    with app.app_context():
        secret = CiSecret.query.filter_by(service_id=service_id, key="NEXUS_PASSWORD").one()
        assert "set its value" in secret.description


def test_a_refused_import_leaves_no_secret_behind(app, client, admin_token, service):
    content = _exported(client, admin_token, service)["content"]
    document = yaml.safe_load(content)
    document["pipeline"]["stages"][1]["stageType"] = "no-such-kind"
    service_id, pipeline_id = _other_pipeline(client, admin_token)
    response = _import(
        client, admin_token, pipeline_id, yaml.safe_dump(document),
        clusterMap={"prod-us-east": "prod-us-east"}, secretValues={"NEXUS_PASSWORD": "x"},
    )
    assert response.status_code == 400
    with app.app_context():
        assert CiSecret.query.filter_by(service_id=service_id).count() == 0
        assert db.session.get(CiPipeline, pipeline_id).stages == []


def test_not_an_export_is_refused(client, admin_token, service):
    response = _import(client, admin_token, service.pipeline_id, "kind: Deployment\nmetadata: {}\n")
    assert response.status_code == 400
    assert "not a KubeSight pipeline export" in response.get_json()["error"]
    response = _import(client, admin_token, service.pipeline_id, "a: [unclosed")
    assert response.status_code == 400 and "not valid YAML" in response.get_json()["error"]


def test_a_shared_pipeline_links_to_the_one_of_the_same_slug(app, client, admin_token, service):
    home = client.post(
        "/api/ci/shared-pipelines",
        json={"name": "Java Standard", "applicationType": "java", "startFrom": {"type": "template"}},
        headers=auth_headers(admin_token),
    )
    assert home.status_code in (200, 201), home.get_json()
    home_id = home.get_json()["data"]["id"]
    with app.app_context():
        from api.models_ci import CiService
        from api.services.ci import shared_pipelines

        source = db.session.get(CiService, service.id)
        shared_pipelines.attach(source, db.session.get(CiService, home_id))
    content = _export(client, admin_token, service.pipeline_id)["content"]
    assert yaml.safe_load(content)["pipeline"]["sharedPipeline"]["slug"] == "java-standard"

    _, pipeline_id = _other_pipeline(client, admin_token)
    response = _import(client, admin_token, pipeline_id, content)
    assert response.status_code == 200, response.get_json()
    assert response.get_json()["data"]["sharedPipeline"] == "java-standard"
    with app.app_context():
        assert db.session.get(CiPipeline, pipeline_id).linked_pipeline_id is not None
