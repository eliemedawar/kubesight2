"""Pipelines outside CI services (the Pipelines page).

What is locked here:

* a pipeline lives on a hidden home row: listed on the Pipelines page, never in
  the CI Services catalog, never matched as an application by slug;
* it runs on its own — a repository only if it checks one out, and a Checkout
  stage with nothing to check out is skipped with the reason, never faked;
* a CI service can build with it: the build runs the shared stages under the
  service's identity, records which shared pipeline and version ran, and the
  service's own stages are kept for "stop using it";
* stage edits on a service that uses one are refused (they belong to the shared
  pipeline), and editing the shared pipeline changes the next build of every
  service using it;
* secrets resolve service → shared pipeline → global;
* a shared pipeline in use cannot be deleted.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from api.db import db
from api.models_application_intelligence import BitbucketCredentialProfile
from api.models_ci import CiBuild, CiPipeline, CiSecret, CiService
from api.secret_encryption import encrypt_secret
from tests.conftest import auth_headers


def _cmd(name, *commands, **extra):
    return {"name": name, "stageType": "command", "commands": list(commands) or ["true"], "runnerLabels": ["mock"], **extra}


@pytest.fixture()
def credential_id(app):
    with app.app_context():
        row = BitbucketCredentialProfile(
            name="ci-token",
            provider="bitbucket",
            credential_type="repository_access_token",
            secret_cipher=encrypt_secret("clone-token-value"),
            read_only=True,
            enabled=True,
        )
        db.session.add(row)
        db.session.commit()
        return row.id


def _service(client, token, credential_id, name="Payments Api"):
    service_id = client.post(
        "/api/ci/services",
        json={"name": name, "applicationType": "java"},
        headers=auth_headers(token),
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


def _create_pipeline(client, token, name="Java standard", stages=None, **extra):
    response = client.post(
        "/api/ci/shared-pipelines",
        json={"name": name, **extra},
        headers=auth_headers(token),
    )
    assert response.status_code == 201, response.get_json()
    data = response.get_json()["data"]
    if stages is not None:
        saved = client.put(
            f"/api/ci/pipelines/{data['pipelineId']}",
            json={"stages": stages},
            headers=auth_headers(token),
        )
        assert saved.status_code == 200, saved.get_json()
    return SimpleNamespace(id=data["id"], pipeline_id=data["pipelineId"], data=data)


def _run(app, client, token, service_id, *, expect=201, max_passes=60):
    from api.services.ci import engine
    from api.services.ci.runners import mock as mock_runner

    response = client.post(f"/api/ci/services/{service_id}/builds", json={}, headers=auth_headers(token))
    assert response.status_code == expect, response.get_json()
    if expect != 201:
        return response.get_json()
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


def _attach(client, token, service_id, home_id):
    return client.post(
        f"/api/ci/services/{service_id}/shared-pipeline",
        json={"sharedPipelineId": home_id},
        headers=auth_headers(token),
    )


# ---------------------------------------------------------------------------
# Living outside the catalog
# ---------------------------------------------------------------------------

def test_a_pipeline_is_listed_on_its_page_and_not_in_the_catalog(client, admin_token, credential_id):
    _service(client, admin_token, credential_id)
    home = _create_pipeline(client, admin_token, "Nightly backup", stages=[_cmd("Backup", "echo backup")])

    services = client.get("/api/ci/services", headers=auth_headers(admin_token)).get_json()["data"]
    assert [item["slug"] for item in services["items"]] == ["payments-api"]

    page = client.get("/api/ci/shared-pipelines", headers=auth_headers(admin_token)).get_json()["data"]
    assert [item["id"] for item in page["items"]] == [home.id]
    item = page["items"][0]
    assert item["kind"] == "pipeline"
    assert item["stageNames"] == ["Backup"]
    assert item["usedByCount"] == 0


def test_a_new_pipeline_can_start_from_a_starter_or_a_service(client, admin_token, credential_id):
    service = _service(client, admin_token, credential_id)
    client.put(
        f"/api/ci/pipelines/{service.pipeline_id}",
        json={"stages": [_cmd("Compile", "make"), _cmd("Test", "make test")]},
        headers=auth_headers(admin_token),
    )
    starter = _create_pipeline(
        client, admin_token, "Node starter", startFrom={"type": "template", "applicationType": "node"}
    )
    assert len(starter.data["stageNames"]) >= 2

    copied = _create_pipeline(
        client, admin_token, "Copied", startFrom={"type": "service", "serviceId": service.id}
    )
    assert copied.data["stageNames"] == ["Compile", "Test"]


def test_copying_a_pipeline_that_uses_secrets_the_new_one_lacks_is_refused(
    app, client, admin_token, credential_id
):
    service = _service(client, admin_token, credential_id)
    client.post(
        f"/api/ci/services/{service.id}/secrets",
        json={"key": "NEXUS_PASSWORD", "value": "s3cret"},
        headers=auth_headers(admin_token),
    )
    saved = client.put(
        f"/api/ci/pipelines/{service.pipeline_id}",
        json={"stages": [_cmd("Publish", "make publish", secretRefs=[{"name": "NEXUS_PASSWORD"}])]},
        headers=auth_headers(admin_token),
    )
    assert saved.status_code == 200, saved.get_json()
    response = client.post(
        "/api/ci/shared-pipelines",
        json={"name": "Copied", "startFrom": {"type": "service", "serviceId": service.id}},
        headers=auth_headers(admin_token),
    )
    assert response.status_code == 400
    assert "NEXUS_PASSWORD" in response.get_json()["error"]
    with app.app_context():
        # Refused whole: no half-created home is left behind.
        assert CiService.query.filter_by(kind="pipeline").count() == 0


def test_a_pipeline_runs_on_its_own_without_a_repository(app, client, admin_token):
    home = _create_pipeline(
        client, admin_token, "Cleanup", stages=[_cmd("Clean", "echo cleaning")]
    )
    build = _run(app, client, admin_token, home.id)
    assert build["status"] == "success", build
    assert build["serviceKind"] == "pipeline"
    assert build["sharedPipeline"] is None


def test_a_checkout_stage_needs_a_repository(app, client, admin_token):
    home = _create_pipeline(
        client,
        admin_token,
        "Needs code",
        stages=[{"name": "Checkout", "stageType": "checkout", "runnerLabels": ["mock"]}, _cmd("Build", "make")],
    )
    summary = client.get(
        f"/api/ci/services/{home.id}/summary", headers=auth_headers(admin_token)
    ).get_json()["data"]
    assert summary["readiness"]["ready"] is False
    refused = _run(app, client, admin_token, home.id, expect=400)
    assert "Checkout stage needs a repository" in refused["error"]


def test_a_checkout_stage_without_a_repository_is_skipped_with_the_reason(app, client, admin_token):
    """Belt and braces: a build that reaches one (say, a retry after the
    repository was disconnected) skips it honestly instead of faking a clone."""
    from api.services.ci import engine

    home = _create_pipeline(client, admin_token, "Cleanup", stages=[_cmd("Clean", "echo cleaning")])
    with app.app_context():
        build = SimpleNamespace(service=db.session.get(CiService, home.id))
        reason = engine._skip_reason(build, None, {"stageType": "checkout"})
    assert "no repository" in reason


def test_a_pipeline_home_never_matches_a_deployment_by_slug(app, client, admin_token):
    from api.models import DeployAutomationRun
    from api.services import deploy_automation_service as automation

    _create_pipeline(client, admin_token, "payments-api", stages=[_cmd("Clean")])
    with app.app_context():
        run = DeployAutomationRun(
            cluster_id="prod-us-east", namespace="payments", deployment_name="payments-api",
            image_repo="registry.local/payments-api", image_tag="1.0", status="queued", change_type="image",
        )
        assert automation._native_ci_service(run) is None


# ---------------------------------------------------------------------------
# Attached to services
# ---------------------------------------------------------------------------

def test_a_service_builds_with_the_shared_stages(app, client, admin_token, credential_id):
    service = _service(client, admin_token, credential_id)
    client.put(
        f"/api/ci/pipelines/{service.pipeline_id}",
        json={"stages": [_cmd("Own stage", "make own")]},
        headers=auth_headers(admin_token),
    )
    home = _create_pipeline(
        client, admin_token, "Java standard", stages=[_cmd("Compile", "make"), _cmd("Test", "make test")]
    )
    attached = _attach(client, admin_token, service.id, home.id)
    assert attached.status_code == 200, attached.get_json()
    pipeline = attached.get_json()["data"]
    assert pipeline["linkedPipeline"]["id"] == home.id
    assert [s["name"] for s in pipeline["stages"]] == ["Compile", "Test"]
    assert pipeline["ownStageCount"] == 1

    detail = client.get(f"/api/ci/services/{service.id}", headers=auth_headers(admin_token)).get_json()["data"]
    assert detail["sharedPipeline"]["name"] == "Java standard"
    assert detail["pipelineStageCount"] == 2

    build = _run(app, client, admin_token, service.id)
    assert build["status"] == "success", build
    assert [s["name"] for s in build["stages"]] == ["Compile", "Test"]
    assert build["sharedPipeline"]["id"] == home.id
    assert build["pipelineId"] == service.pipeline_id  # Still the service's own row.
    with app.app_context():
        own = db.session.get(CiPipeline, service.pipeline_id)
        assert [s.name for s in own.stages] == ["Own stage"]  # Kept, dormant.
        snapshot = db.session.get(CiBuild, build["id"]).pipeline_snapshot
        assert snapshot["pipelineSource"] == "shared"

    used_by = client.get(
        f"/api/ci/shared-pipelines/{home.id}", headers=auth_headers(admin_token)
    ).get_json()["data"]["usedBy"]
    assert [item["serviceId"] for item in used_by] == [service.id]


def test_editing_the_shared_pipeline_changes_the_next_build_of_every_user(
    app, client, admin_token, credential_id
):
    first = _service(client, admin_token, credential_id, "Payments Api")
    second = _service(client, admin_token, credential_id, "Ledger Api")
    home = _create_pipeline(client, admin_token, "Standard", stages=[_cmd("Compile", "make")])
    _attach(client, admin_token, first.id, home.id)
    _attach(client, admin_token, second.id, home.id)

    client.put(
        f"/api/ci/pipelines/{home.pipeline_id}",
        json={"stages": [_cmd("Compile", "make"), _cmd("Lint", "make lint")]},
        headers=auth_headers(admin_token),
    )
    for service in (first, second):
        build = _run(app, client, admin_token, service.id)
        assert [s["name"] for s in build["stages"]] == ["Compile", "Lint"]


def test_stage_edits_on_a_service_using_a_shared_pipeline_are_refused(client, admin_token, credential_id):
    service = _service(client, admin_token, credential_id)
    home = _create_pipeline(client, admin_token, "Standard", stages=[_cmd("Compile", "make")])
    _attach(client, admin_token, service.id, home.id)
    response = client.put(
        f"/api/ci/pipelines/{service.pipeline_id}",
        json={"stages": [_cmd("Sneaky", "curl evil")]},
        headers=auth_headers(admin_token),
    )
    assert response.status_code == 400
    assert "shared pipeline 'Standard'" in response.get_json()["error"]


def test_a_pipeline_home_cannot_use_another_pipeline(client, admin_token):
    one = _create_pipeline(client, admin_token, "One", stages=[_cmd("A")])
    two = _create_pipeline(client, admin_token, "Two", stages=[_cmd("B")])
    response = _attach(client, admin_token, one.id, two.id)
    assert response.status_code == 404  # Homes are not catalog services.


def test_an_empty_shared_pipeline_cannot_be_attached(client, admin_token, credential_id):
    service = _service(client, admin_token, credential_id)
    home = _create_pipeline(client, admin_token, "Empty")
    response = _attach(client, admin_token, service.id, home.id)
    assert response.status_code == 400
    assert "no stages" in response.get_json()["error"]


def test_a_turned_off_shared_pipeline_stops_its_users_with_the_reason(
    app, client, admin_token, credential_id
):
    service = _service(client, admin_token, credential_id)
    home = _create_pipeline(client, admin_token, "Standard", stages=[_cmd("Compile", "make")])
    _attach(client, admin_token, service.id, home.id)
    client.put(f"/api/ci/services/{home.id}", json={"status": "paused"}, headers=auth_headers(admin_token))
    refused = _run(app, client, admin_token, service.id, expect=400)
    assert "turned off" in refused["error"]


def test_stop_using_restores_the_services_own_stages(app, client, admin_token, credential_id):
    service = _service(client, admin_token, credential_id)
    client.put(
        f"/api/ci/pipelines/{service.pipeline_id}",
        json={"stages": [_cmd("Own stage", "make own")]},
        headers=auth_headers(admin_token),
    )
    home = _create_pipeline(client, admin_token, "Standard", stages=[_cmd("Compile", "make")])
    _attach(client, admin_token, service.id, home.id)
    response = client.post(
        f"/api/ci/services/{service.id}/shared-pipeline/detach",
        json={"mode": "restore"},
        headers=auth_headers(admin_token),
    )
    assert response.status_code == 200, response.get_json()
    data = response.get_json()["data"]
    assert data.get("linkedPipeline") is None
    assert [s["name"] for s in data["stages"]] == ["Own stage"]


def test_stop_using_can_copy_the_shared_stages_in(app, client, admin_token, credential_id):
    service = _service(client, admin_token, credential_id)
    home = _create_pipeline(client, admin_token, "Standard", stages=[_cmd("Compile", "make"), _cmd("Test", "make t")])
    _attach(client, admin_token, service.id, home.id)
    data = client.post(
        f"/api/ci/services/{service.id}/shared-pipeline/detach",
        json={"mode": "copy"},
        headers=auth_headers(admin_token),
    ).get_json()["data"]
    assert [s["name"] for s in data["stages"]] == ["Compile", "Test"]
    # Now the service's own: editable again.
    edited = client.put(
        f"/api/ci/pipelines/{service.pipeline_id}",
        json={"stages": [_cmd("Compile", "make all")]},
        headers=auth_headers(admin_token),
    )
    assert edited.status_code == 200


def test_copying_in_refuses_when_the_service_lacks_a_secret(app, client, admin_token, credential_id):
    service = _service(client, admin_token, credential_id)
    home = _create_pipeline(client, admin_token, "Standard")
    client.post(
        f"/api/ci/services/{home.id}/secrets",
        json={"key": "NEXUS_PASSWORD", "value": "home-value"},
        headers=auth_headers(admin_token),
    )
    client.put(
        f"/api/ci/pipelines/{home.pipeline_id}",
        json={"stages": [_cmd("Publish", "make publish", secretRefs=[{"name": "NEXUS_PASSWORD"}])]},
        headers=auth_headers(admin_token),
    )
    _attach(client, admin_token, service.id, home.id)
    response = client.post(
        f"/api/ci/services/{service.id}/shared-pipeline/detach",
        json={"mode": "copy"},
        headers=auth_headers(admin_token),
    )
    assert response.status_code == 400
    assert "NEXUS_PASSWORD" in response.get_json()["error"]
    with app.app_context():
        assert db.session.get(CiPipeline, service.pipeline_id).linked_pipeline_id is not None


def test_a_shared_pipeline_in_use_cannot_be_deleted(client, admin_token, credential_id):
    service = _service(client, admin_token, credential_id)
    home = _create_pipeline(client, admin_token, "Standard", stages=[_cmd("Compile", "make")])
    _attach(client, admin_token, service.id, home.id)
    response = client.delete(f"/api/ci/services/{home.id}", headers=auth_headers(admin_token))
    assert response.status_code == 400
    assert "Payments Api" in response.get_json()["error"]

    client.post(
        f"/api/ci/services/{service.id}/shared-pipeline/detach",
        json={"mode": "restore"},
        headers=auth_headers(admin_token),
    )
    assert client.delete(f"/api/ci/services/{home.id}", headers=auth_headers(admin_token)).status_code == 200


# ---------------------------------------------------------------------------
# Secrets
# ---------------------------------------------------------------------------

def test_secrets_resolve_service_then_shared_pipeline_then_global(app):
    from api.services.ci import secrets as secrets_service

    with app.app_context():
        service = CiService(name="svc", slug="svc", kind="service")
        home = CiService(name="home", slug="home", kind="pipeline")
        db.session.add_all([service, home])
        db.session.flush()
        for scope, owner, key, value in [
            ("global", None, "A", "global-a"),
            ("global", None, "B", "global-b"),
            ("global", None, "C", "global-c"),
            ("service", home.id, "B", "home-b"),
            ("service", home.id, "C", "home-c"),
            ("service", service.id, "C", "service-c"),
        ]:
            db.session.add(CiSecret(scope=scope, service_id=owner, key=key, value_cipher=encrypt_secret(value)))
        db.session.commit()
        resolved = secrets_service.resolve_for_service(service.id, fallback_service_id=home.id)
        assert resolved == {"A": "global-a", "B": "home-b", "C": "service-c"}
        # Without the shared pipeline, the home's secrets are not visible at all.
        assert secrets_service.resolve_for_service(service.id) == {
            "A": "global-a", "B": "global-b", "C": "service-c",
        }


def test_a_build_with_a_shared_pipeline_reads_its_secrets(app, client, admin_token, credential_id):
    from api.services.ci import engine

    service = _service(client, admin_token, credential_id)
    home = _create_pipeline(client, admin_token, "Standard")
    client.post(
        f"/api/ci/services/{home.id}/secrets",
        json={"key": "NEXUS_PASSWORD", "value": "home-value"},
        headers=auth_headers(admin_token),
    )
    client.put(
        f"/api/ci/pipelines/{home.pipeline_id}",
        json={"stages": [_cmd("Publish", "make publish", secretRefs=[{"name": "NEXUS_PASSWORD"}])]},
        headers=auth_headers(admin_token),
    )
    _attach(client, admin_token, service.id, home.id)
    build_id = client.post(
        f"/api/ci/services/{service.id}/builds", json={}, headers=auth_headers(admin_token)
    ).get_json()["data"]["id"]
    with app.app_context():
        build = db.session.get(CiBuild, build_id)
        stage = build.stages[0]
        execution = engine._build_execution(build, stage, build.pipeline_snapshot["stages"][0])
        assert execution.secrets == {"NEXUS_PASSWORD": "home-value"}
        assert "home-value" in engine._mask_values(build)
