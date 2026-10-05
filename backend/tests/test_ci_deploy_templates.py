"""Deploy stages and deployment links that create the deployment from an
inventory template (Inventory → Templates).

What is locked here:

* a template renders for the stage's namespace and deployment name, with the
  build's image on the target container;
* a build never creates Secrets through a template, never deploys anything but
  a Deployment, and a template that needs answers a build cannot give is
  refused on save — each with a reason that names the Deploy Wizard;
* an existing deployment still only gets its image changed; a missing one is
  created from the template;
* a link can name a template, so a service can be linked to a deployment that
  is not there yet and its first linked deploy creates it;
* the inventory's template list says which CI services build from each.

Clusters are the mock ones: prod-us-east/payments exists, `brand-new` does not.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from api.db import db
from api.models import DeploymentRequestSetting, UserTemplate
from api.models_application_intelligence import BitbucketCredentialProfile
from api.models_ci import CiBuild
from api.secret_encryption import encrypt_secret
from api.services.ci import deploy_templates
from tests.conftest import auth_headers

SPEC = {
    "containers": [{"name": "web", "image": "nginx", "tag": "1.25", "ports": [8080]}],
    "resources": {"cpuRequest": "100m", "memoryRequest": "128Mi"},
    "networking": {"service": {"enabled": True, "type": "ClusterIP", "port": 80, "targetPort": 8080}},
    "scaling": {"replicas": 2},
}


def _template(app, slug="brand-new", spec=None, workload_type="Deployment", name=None):
    with app.app_context():
        db.session.add(
            UserTemplate(
                slug=slug,
                name=name or slug,
                category="Testing",
                workload_type=workload_type,
                spec=spec or SPEC,
            )
        )
        db.session.commit()
    return slug


@pytest.fixture(autouse=True)
def _no_approvals(app):
    with app.app_context():
        row = DeploymentRequestSetting.query.first()
        if row is None:
            row = DeploymentRequestSetting()
            db.session.add(row)
        row.required_approvals = 0
        db.session.commit()


@pytest.fixture()
def service(app, client, admin_token):
    with app.app_context():
        credential = BitbucketCredentialProfile(
            name="ci-token", provider="bitbucket", credential_type="repository_access_token",
            secret_cipher=encrypt_secret("clone-token-value"), read_only=True, enabled=True,
        )
        db.session.add(credential)
        db.session.commit()
        credential_id = credential.id
    service_id = client.post(
        "/api/ci/services", json={"name": "Brand New", "applicationType": "java"}, headers=auth_headers(admin_token)
    ).get_json()["data"]["id"]
    client.put(
        f"/api/ci/services/{service_id}/source",
        json={"repositoryUrl": "https://bitbucket.org/areeba/brand-new", "defaultBranch": "main",
              "credentialProfileId": credential_id},
        headers=auth_headers(admin_token),
    )
    pipeline_id = client.get(
        f"/api/ci/services/{service_id}/pipelines", headers=auth_headers(admin_token)
    ).get_json()["data"]["items"][0]["id"]
    return SimpleNamespace(id=service_id, pipeline_id=pipeline_id)


def _deploy(**overrides):
    config = {
        "clusterId": "prod-us-east",
        "namespace": "payments",
        "deploymentName": "brand-new",
        "image": "registry.local/brand-new:4.0.0",
        "createIfMissing": True,
        "create": {"source": "template", "templateId": "brand-new"},
    }
    config.update(overrides)
    return config


def _stages(deploy):
    return [
        {"name": "Build", "stageType": "command", "commands": ["make"], "runnerLabels": ["mock"]},
        {"name": "Deploy", "stageType": "deploy", "deploy": deploy},
    ]


def _save(client, token, pipeline_id, stages):
    return client.put(
        f"/api/ci/pipelines/{pipeline_id}", json={"parameters": [], "stages": stages}, headers=auth_headers(token)
    )


def _run(app, client, token, service_id):
    from api.services.ci import engine
    from api.services.ci.runners import mock as mock_runner

    build_id = client.post(
        f"/api/ci/services/{service_id}/builds", json={}, headers=auth_headers(token)
    ).get_json()["data"]["id"]
    original = mock_runner._STAGE_SECONDS
    mock_runner._STAGE_SECONDS = 0.0
    try:
        with app.app_context():
            for _ in range(60):
                engine.advance_ci_builds()
                if db.session.get(CiBuild, build_id).status not in ("queued", "running"):
                    break
    finally:
        mock_runner._STAGE_SECONDS = original
    return client.get(f"/api/ci/builds/{build_id}", headers=auth_headers(token)).get_json()["data"]


def _logs(client, token, build_id, stage_id):
    data = client.get(
        f"/api/ci/builds/{build_id}/stages/{stage_id}/logs", headers=auth_headers(token)
    ).get_json()["data"]
    return "\n".join(line["content"] for line in data["lines"])


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------

def test_a_template_renders_with_the_builds_image(app):
    _template(app)
    with app.app_context():
        text, created = deploy_templates.render(
            "brand-new", namespace="payments", deployment_name="brand-new", image="registry.local/x:9"
        )
    assert {"kind": "Deployment", "name": "brand-new"} in created
    assert any(item["kind"] == "Service" for item in created)
    assert "registry.local/x:9" in text
    assert "nginx:1.25" not in text
    assert "namespace: payments" in text


SECRET_SPEC = dict(
    SPEC,
    schema={
        "env": [
            {"key": "db_password", "required": True, "sensitive": True, "kind": "secret",
             "allowedSources": ["existingSecret", "createSecret"]},
            {"key": "SPRING_PROFILES_ACTIVE", "required": False, "default": "sit",
             "allowedSources": ["value"]},
        ],
        "volumeMounts": [
            {"mountPath": "/opt/keys/client.jks", "kind": "configMap",
             "allowedSources": ["existingConfigMap", "createConfigMap"]},
        ],
    },
)
ANSWERS = {
    "env": {"db_password": {"source": "existingSecret", "secretName": "brand-new-db", "key": "password"}},
    "volumes": {"/opt/keys/client.jks": {"source": "existingConfigMap", "configMapName": "brand-new-keys"}},
}


def test_a_required_variable_needs_an_answer(app):
    _template(app, spec=SECRET_SPEC)
    with app.app_context(), pytest.raises(deploy_templates.DeployTemplateError, match="needs an answer"):
        deploy_templates.render("brand-new", namespace="payments", deployment_name="brand-new")


def test_answers_point_at_existing_secrets_and_configmaps(app):
    _template(app, spec=SECRET_SPEC)
    with app.app_context():
        text, created = deploy_templates.render(
            "brand-new", namespace="payments", deployment_name="brand-new", answers=ANSWERS
        )
    assert "brand-new-db" in text and "brand-new-keys" in text
    assert "kind: Secret" not in text  # Referenced, never written.
    assert all(item["kind"] != "Secret" for item in created)


def test_a_build_never_creates_a_secret_through_a_template():
    from api.services.ci import deploy_config

    with pytest.raises(deploy_config.DeployConfigError, match="never writes credentials"):
        deploy_config.template_answers(
            {"env": {"db_password": {"source": "createSecret", "secretName": "x", "value": "hunter2"}}}, "Stage 'Deploy'"
        )
    with pytest.raises(deploy_config.DeployConfigError, match="does not create the files"):
        deploy_config.template_answers(
            {"volumes": {"/opt/k.jks": {"source": "createConfigMap", "configMapName": "k"}}}, "Stage 'Deploy'"
        )


def test_the_picker_lists_what_a_template_asks(app, client, admin_token):
    _template(app, spec=SECRET_SPEC)
    items = client.get("/api/ci/deploy-templates", headers=auth_headers(admin_token)).get_json()["data"]["items"]
    item = next(i for i in items if i["id"] == "brand-new")
    password = next(f for f in item["env"] if f["key"] == "db_password")
    assert password["required"] and password["sources"] == ["existingSecret"]
    assert item["volumes"] == [{"mountPath": "/opt/keys/client.jks", "kind": "configMap", "sources": ["existingConfigMap"]}]


def test_a_stage_with_answers_creates_the_deployment(app, client, admin_token, service):
    _template(app, spec=SECRET_SPEC)
    refused = _save(client, admin_token, service.pipeline_id, _stages(_deploy()))
    assert refused.status_code == 400 and "db_password" in refused.get_json()["error"]
    saved = _save(
        client, admin_token, service.pipeline_id,
        _stages(_deploy(create={"source": "template", "templateId": "brand-new", "answers": ANSWERS})),
    )
    assert saved.status_code == 200, saved.get_json()
    build = _run(app, client, admin_token, service.id)
    assert build["status"] == "success", build
    assert build["stages"][1]["deploy"]["created"] is True


def test_changing_an_answer_needs_someone_who_can_deploy():
    from api.services.ci import deploy_config

    with_answers = _deploy(create={"source": "template", "templateId": "brand-new", "answers": ANSWERS})
    other = _deploy(create={"source": "template", "templateId": "brand-new", "answers": {
        "env": {"db_password": {"source": "existingSecret", "secretName": "someone-elses-db"}}}})
    assert deploy_config.signature(deploy_config.normalize(with_answers, "deploy", "Deploy")) != (
        deploy_config.signature(deploy_config.normalize(other, "deploy", "Deploy"))
    )


def test_only_deployment_templates_can_be_used(app):
    _template(app, workload_type="StatefulSet")
    with app.app_context(), pytest.raises(deploy_templates.DeployTemplateError, match="Deployment template"):
        deploy_templates.render("brand-new", namespace="payments", deployment_name="brand-new")


def test_a_missing_template_says_so(app):
    with app.app_context(), pytest.raises(deploy_templates.DeployTemplateError, match="no longer exists"):
        deploy_templates.render("gone", namespace="payments", deployment_name="gone")


def test_the_picker_lists_templates_with_their_defaults(app, client, admin_token):
    _template(app, name="Brand New App")
    items = client.get("/api/ci/deploy-templates", headers=auth_headers(admin_token)).get_json()["data"]["items"]
    item = next(i for i in items if i["id"] == "brand-new")
    assert item["deploymentName"] == "brand-new-app"
    assert item["containers"] == ["web"]
    assert item["image"] == "nginx:1.25"
    assert item["usable"] is True


def test_the_preview_says_what_would_be_created_or_why_not(app, client, admin_token):
    _template(app)
    ok = client.post(
        "/api/ci/deploy-templates/brand-new/preview",
        json={"namespace": "payments", "deploymentName": "brand-new"},
        headers=auth_headers(admin_token),
    ).get_json()["data"]
    assert ok["ok"] is True and {"kind": "Deployment", "name": "brand-new"} in ok["creates"]
    bad = client.post(
        "/api/ci/deploy-templates/nope/preview",
        json={"namespace": "payments", "deploymentName": "brand-new"},
        headers=auth_headers(admin_token),
    ).get_json()["data"]
    assert bad["ok"] is False and "no longer exists" in bad["error"]


# ---------------------------------------------------------------------------
# Deploy stages
# ---------------------------------------------------------------------------

def test_saving_a_stage_with_an_unusable_template_is_refused(app, client, admin_token, service):
    _template(app, workload_type="StatefulSet")
    response = _save(client, admin_token, service.pipeline_id, _stages(_deploy()))
    assert response.status_code == 400
    assert "Stage 'Deploy'" in response.get_json()["error"]


def test_a_missing_deployment_is_created_from_the_template(app, client, admin_token, service):
    _template(app, name="Brand New")
    saved = _save(client, admin_token, service.pipeline_id, _stages(_deploy()))
    assert saved.status_code == 200, saved.get_json()
    assert saved.get_json()["data"]["stages"][1]["deploy"]["create"]["templateName"] == "Brand New"

    build = _run(app, client, admin_token, service.id)
    assert build["status"] == "success", build
    state = build["stages"][1]["deploy"]
    assert state["created"] is True
    assert state["containerName"] == "web"  # The template's container, not the deployment's name.
    assert any(item["kind"] == "Deployment" for item in state["createdResources"])
    log = _logs(client, admin_token, build["id"], build["stages"][1]["id"])
    assert "inventory template 'Brand New'" in log


def test_an_existing_deployment_still_only_gets_its_image(app, client, admin_token, service):
    _template(app, slug="payments-api", spec=dict(SPEC, containers=[{"name": "payments-api", "image": "x", "tag": "1"}]))
    _save(
        client, admin_token, service.pipeline_id,
        _stages(_deploy(deploymentName="payments-api", create={"source": "template", "templateId": "payments-api"})),
    )
    build = _run(app, client, admin_token, service.id)
    assert build["status"] == "success", build
    state = build["stages"][1]["deploy"]
    assert state["created"] is False
    assert state["previousImage"] == "ghcr.io/mock/payments:v2.8.1"


def test_changing_the_template_needs_someone_who_can_deploy(app, client, admin_token, service):
    """The template is part of what the target's authorizer signed off on."""
    from api.services.ci import deploy_config

    base = deploy_config.normalize(_deploy(), "deploy", "Deploy")
    other = deploy_config.normalize(_deploy(create={"source": "template", "templateId": "other"}), "deploy", "Deploy")
    assert deploy_config.signature(base) != deploy_config.signature(other)


# ---------------------------------------------------------------------------
# Links
# ---------------------------------------------------------------------------

def test_a_link_with_a_template_creates_the_deployment_on_the_first_deploy(app, client, admin_token, service):
    _template(app)
    link = client.post(
        f"/api/ci/services/{service.id}/deployments",
        json={"clusterId": "prod-us-east", "namespace": "payments", "workloadName": "brand-new",
              "templateId": "brand-new"},
        headers=auth_headers(admin_token),
    )
    assert link.status_code == 201, link.get_json()
    assert link.get_json()["data"]["templateName"] == "brand-new"
    _save(client, admin_token, service.pipeline_id,
          _stages({"target": "linked", "image": "registry.local/brand-new:4.0.0"}))
    build = _run(app, client, admin_token, service.id)
    assert build["status"] == "success", build
    assert build["stages"][1]["deploy"]["created"] is True


def test_a_link_to_a_template_that_does_not_exist_is_refused(app, client, admin_token, service):
    response = client.post(
        f"/api/ci/services/{service.id}/deployments",
        json={"clusterId": "prod-us-east", "namespace": "payments", "workloadName": "brand-new",
              "templateId": "nope"},
        headers=auth_headers(admin_token),
    )
    assert response.status_code == 400 and "does not exist" in response.get_json()["error"]


def test_the_inventory_template_list_names_the_services_that_build_from_each(app, client, admin_token, service):
    _template(app)
    _save(client, admin_token, service.pipeline_id, _stages(_deploy()))
    items = client.get("/api/inventory/deploy/wizard/templates", headers=auth_headers(admin_token)).get_json()["data"]
    item = next(i for i in items if i["id"] == "brand-new")
    assert [s["id"] for s in item["ciServices"]] == [service.id]


def test_the_link_a_stage_makes_carries_its_template(app, client, admin_token, service):
    _template(app, spec=SECRET_SPEC)
    _save(
        client, admin_token, service.pipeline_id,
        _stages(_deploy(create={"source": "template", "templateId": "brand-new", "answers": ANSWERS})),
    )
    links = client.get(
        f"/api/ci/services/{service.id}/deployments?live=false", headers=auth_headers(admin_token)
    ).get_json()["data"]["items"]
    assert links[0]["templateId"] == "brand-new"
    assert links[0]["templateAnswers"]["env"]["db_password"]["secretName"] == "brand-new-db"
