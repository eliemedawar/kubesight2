"""A CI service's links to the deployments it builds.

What is locked here:

* a link is made by hand, by saving a fixed-target Deploy stage, or by that
  stage deploying — unless the stage opts out — and a workload belongs to one
  service only (a stage never takes one another service has);
* the inventory names the service that builds each linked row;
* the deploy automation asks the link first, before Deploy-stage and slug guesses;
* a Deploy stage in "linked" mode deploys to the building service's linked
  deployment, with the rights of whoever made the link — and fails with the
  reason when there is none, several, or nobody who could deploy there;
* that is what lets one shared pipeline deploy each service to its own workload.

Clusters are the mock ones: prod-us-east has payments/payments-api.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

import pytest

from api.db import db
from api.models import DeployAutomationRun, DeploymentRequestSetting, User
from api.models_application_intelligence import BitbucketCredentialProfile
from api.models_ci import CiBuild, CiServiceDeployment
from api.secret_encryption import encrypt_secret
from tests.conftest import auth_headers

TARGET = {"clusterId": "prod-us-east", "namespace": "payments", "workloadName": "payments-api"}


def _deploy(**overrides):
    config = {
        "clusterId": "prod-us-east",
        "namespace": "payments",
        "deploymentName": "payments-api",
        "containerName": "",
        "image": "registry.local/payments:2.0.0",
        "createIfMissing": False,
        "manifest": "",
    }
    config.update(overrides)
    return config


def _stages(deploy):
    return [
        {"name": "Build", "stageType": "command", "commands": ["make"], "runnerLabels": ["mock"]},
        {"name": "Deploy", "stageType": "deploy", "deploy": deploy},
    ]


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
def credential_id(app):
    with app.app_context():
        row = BitbucketCredentialProfile(
            name="ci-token", provider="bitbucket", credential_type="repository_access_token",
            secret_cipher=encrypt_secret("clone-token-value"), read_only=True, enabled=True,
        )
        db.session.add(row)
        db.session.commit()
        return row.id


def _service(client, token, credential_id, name="Payments Api"):
    service_id = client.post(
        "/api/ci/services", json={"name": name, "applicationType": "java"}, headers=auth_headers(token)
    ).get_json()["data"]["id"]
    client.put(
        f"/api/ci/services/{service_id}/source",
        json={
            "repositoryUrl": f"https://bitbucket.org/areeba/{name.lower().replace(' ', '-')}",
            "defaultBranch": "main",
            "credentialProfileId": credential_id,
        },
        headers=auth_headers(token),
    )
    pipeline_id = client.get(
        f"/api/ci/services/{service_id}/pipelines", headers=auth_headers(token)
    ).get_json()["data"]["items"][0]["id"]
    return SimpleNamespace(id=service_id, pipeline_id=pipeline_id)


def _link(client, token, service_id, **overrides):
    return client.post(
        f"/api/ci/services/{service_id}/deployments",
        json={**TARGET, **overrides},
        headers=auth_headers(token),
    )


def _links(client, token, service_id, live=True):
    return client.get(
        f"/api/ci/services/{service_id}/deployments?live={'true' if live else 'false'}",
        headers=auth_headers(token),
    ).get_json()["data"]["items"]


def _save(client, token, pipeline_id, stages):
    return client.put(
        f"/api/ci/pipelines/{pipeline_id}", json={"parameters": [], "stages": stages}, headers=auth_headers(token)
    )


def _run(app, client, token, service_id, max_passes=60):
    from api.services.ci import engine
    from api.services.ci.runners import mock as mock_runner

    response = client.post(f"/api/ci/services/{service_id}/builds", json={}, headers=auth_headers(token))
    assert response.status_code == 201, response.get_json()
    build_id = response.get_json()["data"]["id"]
    original = mock_runner._STAGE_SECONDS
    mock_runner._STAGE_SECONDS = 0.0
    try:
        with app.app_context():
            for _ in range(max_passes):
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
# Making links
# ---------------------------------------------------------------------------

def test_a_deployment_is_linked_by_hand_and_shows_its_live_state(client, admin_token, credential_id):
    service = _service(client, admin_token, credential_id)
    response = _link(client, admin_token, service.id, environment="PROD")
    assert response.status_code == 201, response.get_json()
    link = response.get_json()["data"]
    assert link["source"] == "manual"
    assert link["environment"] == "PROD"
    assert link["canDeployThrough"] is True  # admin could deploy there.

    items = _links(client, admin_token, service.id)
    assert items[0]["live"]["state"] == "found"
    assert items[0]["live"]["image"]

    detail = client.get(f"/api/ci/services/{service.id}", headers=auth_headers(admin_token)).get_json()["data"]
    assert detail["deploymentLinkCount"] == 1


def test_a_workload_belongs_to_one_service(client, admin_token, credential_id):
    first = _service(client, admin_token, credential_id, "Payments Api")
    second = _service(client, admin_token, credential_id, "Payments Two")
    assert _link(client, admin_token, first.id).status_code == 201
    response = _link(client, admin_token, second.id)
    assert response.status_code == 400
    assert "already linked to Payments Api" in response.get_json()["error"]


def test_a_link_can_be_relabelled_and_removed(client, admin_token, credential_id):
    service = _service(client, admin_token, credential_id)
    link = _link(client, admin_token, service.id).get_json()["data"]
    updated = client.put(
        f"/api/ci/services/{service.id}/deployments/{link['id']}",
        json={"environment": "UAT", "containerName": "app"},
        headers=auth_headers(admin_token),
    ).get_json()["data"]
    assert updated["environment"] == "UAT" and updated["containerName"] == "app"
    assert client.delete(
        f"/api/ci/services/{service.id}/deployments/{link['id']}", headers=auth_headers(admin_token)
    ).status_code == 200
    assert _links(client, admin_token, service.id, live=False) == []


def test_bad_names_are_refused(client, admin_token, credential_id):
    service = _service(client, admin_token, credential_id)
    assert _link(client, admin_token, service.id, namespace="Bad_NS").status_code == 400
    assert _link(client, admin_token, service.id, environment="<script>").status_code == 400


def test_a_pipeline_home_has_no_links(client, admin_token):
    home = client.post(
        "/api/ci/shared-pipelines", json={"name": "Cleanup"}, headers=auth_headers(admin_token)
    ).get_json()["data"]
    assert _link(client, admin_token, home["id"]).status_code == 404


# ---------------------------------------------------------------------------
# Deploy stages make them
# ---------------------------------------------------------------------------

def test_saving_a_deploy_stage_links_its_target(client, admin_token, credential_id):
    service = _service(client, admin_token, credential_id)
    assert _save(client, admin_token, service.pipeline_id, _stages(_deploy())).status_code == 200
    items = _links(client, admin_token, service.id, live=False)
    assert [(i["clusterId"], i["namespace"], i["workloadName"], i["source"]) for i in items] == [
        ("prod-us-east", "payments", "payments-api", "deploy_stage")
    ]
    assert items[0]["canDeployThrough"] is True


def test_a_deploy_stage_can_opt_out_of_linking(client, admin_token, credential_id):
    service = _service(client, admin_token, credential_id)
    _save(client, admin_token, service.pipeline_id, _stages(_deploy(linkToService=False)))
    assert _links(client, admin_token, service.id, live=False) == []


def test_a_deploy_stage_never_takes_another_services_link(client, admin_token, credential_id):
    owner = _service(client, admin_token, credential_id, "Payments Api")
    other = _service(client, admin_token, credential_id, "Payments Two")
    _link(client, admin_token, owner.id)
    assert _save(client, admin_token, other.pipeline_id, _stages(_deploy())).status_code == 200
    assert _links(client, admin_token, other.id, live=False) == []
    assert len(_links(client, admin_token, owner.id, live=False)) == 1


def test_a_successful_deploy_links_the_deployment(app, client, admin_token, credential_id):
    """A pipeline saved before links existed links on its first deploy."""
    service = _service(client, admin_token, credential_id)
    _save(client, admin_token, service.pipeline_id, _stages(_deploy()))
    with app.app_context():
        CiServiceDeployment.query.delete()
        db.session.commit()
    build = _run(app, client, admin_token, service.id)
    assert build["status"] == "success", build
    assert [i["source"] for i in _links(client, admin_token, service.id, live=False)] == ["deploy_stage"]


def test_the_deploy_target_picker_says_who_has_each_deployment(client, admin_token, credential_id):
    service = _service(client, admin_token, credential_id)
    _link(client, admin_token, service.id)
    data = client.get(
        "/api/ci/deploy-targets?clusterId=prod-us-east&namespace=payments", headers=auth_headers(admin_token)
    ).get_json()["data"]
    row = next(item for item in data["deployments"] if item["name"] == "payments-api")
    assert row["linkedService"]["id"] == service.id


# ---------------------------------------------------------------------------
# Readers: inventory, deploy automation
# ---------------------------------------------------------------------------

def test_the_inventory_names_the_service_that_builds_a_row(client, admin_token, credential_id):
    service = _service(client, admin_token, credential_id)
    _link(client, admin_token, service.id, environment="PROD")
    items = client.get("/api/inventory?cluster=prod-us-east", headers=auth_headers(admin_token)).get_json()["data"]
    linked = [item for item in items if item.get("ciService")]
    assert linked, "no inventory row carried its CI service"
    row = next(item for item in linked if item["namespace"] == "payments")
    assert row["ciService"]["id"] == service.id
    assert row["ciService"]["environment"] == "PROD"
    assert all(item.get("ciService") is None for item in items if item not in linked)

    detail = client.get(f"/api/inventory/{row['id']}", headers=auth_headers(admin_token)).get_json()["data"]
    assert detail["ciService"]["slug"] == "payments-api"


def test_the_deploy_automation_asks_the_link_first(app, client, admin_token, credential_id):
    from api.services import deploy_automation_service as automation

    # A service whose slug matches the deployment, and another linked to it.
    _service(client, admin_token, credential_id, "Payments Api")
    linked = _service(client, admin_token, credential_id, "Checkout Builder")
    _link(client, admin_token, linked.id)
    with app.app_context():
        run = DeployAutomationRun(
            cluster_id="prod-us-east", namespace="payments", deployment_name="payments-api",
            image_repo="registry.local/payments-api", image_tag="1.0", status="queued", change_type="image",
        )
        service = automation._native_ci_service(run)
        assert service is not None and service.id == linked.id


# ---------------------------------------------------------------------------
# Linked-mode Deploy stages
# ---------------------------------------------------------------------------

LINKED = {"target": "linked", "image": "registry.local/payments:3.0.0"}


def test_a_linked_stage_deploys_to_the_services_linked_deployment(app, client, admin_token, credential_id):
    service = _service(client, admin_token, credential_id)
    _link(client, admin_token, service.id)
    saved = _save(client, admin_token, service.pipeline_id, _stages(dict(LINKED)))
    assert saved.status_code == 200, saved.get_json()
    build = _run(app, client, admin_token, service.id)
    assert build["status"] == "success", build
    deploy = build["stages"][1]
    assert deploy["deploy"]["target"]["deploymentName"] == "payments-api"
    assert deploy["deploy"]["image"] == "registry.local/payments:3.0.0"
    with app.app_context():
        snapshot = db.session.get(CiBuild, build["id"]).pipeline_snapshot
        config = snapshot["stages"][1]["deploy"]
        assert config["clusterId"] == "prod-us-east" and config["linkId"]
        assert config["authorizedBy"]["username"] == "admin"


def test_a_linked_stage_with_nothing_linked_fails_with_the_reason(app, client, admin_token, credential_id):
    service = _service(client, admin_token, credential_id)
    _save(client, admin_token, service.pipeline_id, _stages(dict(LINKED)))
    build = _run(app, client, admin_token, service.id)
    assert build["status"] == "failed"
    log = _logs(client, admin_token, build["id"], build["stages"][1]["id"])
    assert "not linked to a deployment yet" in log or "not linked to a deployment yet" in (build["stages"][1]["error"] or "")


def test_several_links_need_the_stage_to_name_an_environment(app, client, admin_token, credential_id):
    service = _service(client, admin_token, credential_id)
    _link(client, admin_token, service.id, environment="PROD")
    _link(client, admin_token, service.id, workloadName="payments-worker", environment="UAT")
    _save(client, admin_token, service.pipeline_id, _stages(dict(LINKED)))
    build = _run(app, client, admin_token, service.id)
    assert build["status"] == "failed"
    assert "does not say which" in (build["stages"][1]["error"] or "")

    _save(client, admin_token, service.pipeline_id, _stages(dict(LINKED, environment="prod")))
    build = _run(app, client, admin_token, service.id)
    assert build["status"] == "success", build
    assert build["stages"][1]["deploy"]["target"]["deploymentName"] == "payments-api"


def test_a_link_made_by_someone_who_cannot_deploy_is_not_deployed_through(
    app, client, admin_token, credential_id
):
    from api.services.ci import deployment_links

    service = _service(client, admin_token, credential_id)
    with app.app_context():
        admin = User.query.filter_by(username="admin").first()
        from api.models_ci import CiService

        row = db.session.get(CiService, service.id)
        with patch("api.access_engine.user_has_permission", return_value=False):
            link = deployment_links.add_link(row, dict(TARGET), actor=admin)
    assert link["canDeployThrough"] is False
    _save(client, admin_token, service.pipeline_id, _stages(dict(LINKED)))
    build = _run(app, client, admin_token, service.id)
    assert build["status"] == "failed"
    assert "Deploy as me" in (build["stages"][1]["error"] or "")

    fixed = client.put(
        f"/api/ci/services/{service.id}/deployments/{link['id']}",
        json={"reauthorize": True},
        headers=auth_headers(admin_token),
    ).get_json()["data"]
    assert fixed["canDeployThrough"] is True
    assert _run(app, client, admin_token, service.id)["status"] == "success"


def test_one_shared_pipeline_deploys_each_service_to_its_own_deployment(
    app, client, admin_token, credential_id
):
    first = _service(client, admin_token, credential_id, "Payments Api")
    second = _service(client, admin_token, credential_id, "Payments Worker")
    _link(client, admin_token, first.id)
    _link(client, admin_token, second.id, workloadName="payments-worker")
    home = client.post(
        "/api/ci/shared-pipelines", json={"name": "Build and ship"}, headers=auth_headers(admin_token)
    ).get_json()["data"]
    saved = _save(client, admin_token, home["pipelineId"], _stages(dict(LINKED)))
    assert saved.status_code == 200, saved.get_json()
    for service in (first, second):
        client.post(
            f"/api/ci/services/{service.id}/shared-pipeline",
            json={"sharedPipelineId": home["id"]},
            headers=auth_headers(admin_token),
        )
    with app.app_context():
        # The shared pipeline's own save links nothing.
        assert CiServiceDeployment.query.filter_by(service_id=home["id"]).count() == 0
    targets = []
    for service in (first, second):
        build = _run(app, client, admin_token, service.id)
        with app.app_context():
            snapshot = db.session.get(CiBuild, build["id"]).pipeline_snapshot
        targets.append(snapshot["stages"][1]["deploy"]["deploymentName"])
    assert targets == ["payments-api", "payments-worker"]


def test_a_shared_pipeline_run_on_its_own_says_why_a_linked_stage_cannot_deploy(
    app, client, admin_token
):
    home = client.post(
        "/api/ci/shared-pipelines", json={"name": "Build and ship"}, headers=auth_headers(admin_token)
    ).get_json()["data"]
    _save(client, admin_token, home["pipelineId"], _stages(dict(LINKED)))
    build = _run(app, client, admin_token, home["id"])
    assert build["status"] == "failed"
    assert "no service behind it" in (build["stages"][1]["error"] or "")


def test_only_someone_who_can_deploy_sets_a_linked_stage(app, client, admin_token, credential_id):
    from api.models_ci import CiPipeline
    from api.services.ci import pipelines

    service = _service(client, admin_token, credential_id)
    with app.app_context():
        admin = User.query.filter_by(username="admin").first()
        with patch("api.access_engine.user_has_permission", return_value=False):
            with pytest.raises(pipelines.PipelineError, match="can deploy applications"):
                pipelines.update_pipeline(
                    db.session.get(CiPipeline, service.pipeline_id),
                    {"stages": _stages(dict(LINKED))},
                    actor=admin,
                )
