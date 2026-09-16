"""The assisted-configuration API, and the promises around it.

Two halves. The first is the API itself: who may start an analysis, what comes
back while it runs, and what accepting writes. The second is the half that
matters more — that a service which never goes near any of this behaves exactly
as it did before.
"""

from __future__ import annotations

import pytest

from api.db import db
from api.models_application_intelligence import BitbucketCredentialProfile
from api.models_ci import CiRepositoryAnalysis, CiService
from api.secret_encryption import encrypt_secret
from api.services.ci_assist import hermes, jobs
from tests.conftest import auth_headers
from tests.fixtures import fake_source
from tests.test_ci_assist_generation import FakeHermes, checkout, response


@pytest.fixture()
def credential(app):
    row = BitbucketCredentialProfile(
        name="fake-read-only",
        provider=fake_source.PROVIDER,
        credential_type="repository_access_token",
        secret_cipher=encrypt_secret("token"),
        enabled=True,
    )
    db.session.add(row)
    db.session.commit()
    return row.id


@pytest.fixture()
def service_id(app, credential):
    row = CiService(
        name="Payment Service",
        slug="payment-service",
        application_type="generic",
        repository_provider=fake_source.PROVIDER,
        repository_url="https://fake.test/acme/payment-service.git",
        repository_workspace="acme",
        repository_name="payment-service",
        default_branch="main",
        credential_profile_id=credential,
    )
    db.session.add(row)
    db.session.commit()
    return row.id


@pytest.fixture(autouse=True)
def hermes_available(monkeypatch):
    monkeypatch.setattr(hermes, "is_configured", lambda: True)
    monkeypatch.setattr(hermes, "configuration_hint", lambda: "")


def good_response():
    return response(
        {
            "language": "java",
            "languageVersion": "17",
            "buildSystem": "gradle",
            "buildSystemVersion": "8.7",
            "packaging": "jar",
            "usesBuildWrapper": True,
        },
        [
            checkout(),
            {
                "name": "Build",
                "stageType": "command",
                "buildEnvironment": "java-jdk11",
                "runnerLabels": ["linux", "java"],
                "commands": ["./gradlew --no-daemon clean build"],
                "artifacts": [{"path": "build/libs/*.jar", "type": "jar"}],
            },
        ],
        required_inputs=[
            {"name": "NEXUS_PASSWORD", "kind": "secret", "required": True},
        ],
    )


# ---------------------------------------------------------------------------
# Availability
# ---------------------------------------------------------------------------

def test_availability_says_yes_when_hermes_is_configured(client, admin_token):
    data = client.get(
        "/api/ci/assist/availability", headers=auth_headers(admin_token)
    ).get_json()["data"]
    assert data["available"] is True
    assert data["schemaVersion"]


def test_availability_says_why_not_rather_than_just_no(client, admin_token, monkeypatch):
    """The wizard asks this before offering the choice. An installation with no
    Hermes should show manual configuration and a reason, not a control that
    fails when pressed."""
    monkeypatch.setattr(hermes, "is_configured", lambda: False)
    monkeypatch.setattr(hermes, "configuration_hint", lambda: "HERMES_API_URL is unset.")
    data = client.get(
        "/api/ci/assist/availability", headers=auth_headers(admin_token)
    ).get_json()["data"]
    assert data["available"] is False
    assert "HERMES_API_URL" in data["reason"]


def test_capabilities_are_readable_so_manual_configuration_benefits_too(client, admin_token):
    data = client.get(
        "/api/ci/assist/capabilities", headers=auth_headers(admin_token)
    ).get_json()["data"]
    assert any(item["key"] == "java-jdk11" for item in data["buildEnvironments"])
    assert "$KUBESIGHT_SOURCE" in data["workspaceVariables"]


# ---------------------------------------------------------------------------
# Permissions
# ---------------------------------------------------------------------------

def test_a_viewer_cannot_start_an_analysis(client, viewer_token, service_id):
    assert (
        client.post(
            f"/api/ci/services/{service_id}/analysis",
            json={},
            headers=auth_headers(viewer_token),
        ).status_code
        == 403
    )


def test_a_viewer_can_read_the_state(client, viewer_token, service_id):
    assert (
        client.get(
            f"/api/ci/services/{service_id}/analysis",
            headers=auth_headers(viewer_token),
        ).status_code
        == 200
    )


def test_analysis_needs_both_ci_and_analyze_rights(client, admin_token, service_id, monkeypatch):
    """Holding CI permissions alone should not silently grant the ability to
    send an organisation's source to a model."""
    from api import decorators

    real = decorators.user_has_permission

    def without_analyze(user, key):
        return False if key == "applications:analyze" else real(user, key)

    monkeypatch.setattr(decorators, "user_has_permission", without_analyze)
    assert (
        client.post(
            f"/api/ci/services/{service_id}/analysis",
            json={},
            headers=auth_headers(admin_token),
        ).status_code
        == 403
    )


# ---------------------------------------------------------------------------
# The flow
# ---------------------------------------------------------------------------

def test_starting_an_analysis_returns_a_job_to_watch(
    client, admin_token, service_id, monkeypatch
):
    FakeHermes(good_response()).install(monkeypatch)
    fake_source.FAKE.load(fake_source.JAVA_GRADLE)
    monkeypatch.setattr(jobs, "submit", lambda _id: None)

    created = client.post(
        f"/api/ci/services/{service_id}/analysis",
        json={"revision": "main"},
        headers=auth_headers(admin_token),
    )
    assert created.status_code == 202
    data = created.get_json()["data"]
    assert data["state"] == "queued"
    assert data["revision"] == "main"
    # The label is a real step, not a fabricated percentage.
    assert data["currentStage"] == "Queued"


def test_the_poll_endpoint_carries_everything_the_screen_needs(
    client, admin_token, service_id, monkeypatch
):
    FakeHermes(good_response()).install(monkeypatch)
    fake_source.FAKE.load(fake_source.JAVA_GRADLE)
    client.post(
        f"/api/ci/services/{service_id}/analysis",
        json={},
        headers=auth_headers(admin_token),
    )
    data = client.get(
        f"/api/ci/services/{service_id}/analysis", headers=auth_headers(admin_token)
    ).get_json()["data"]

    latest = data["latestAnalysis"]
    assert latest["state"] == "analyzed"
    assert latest["profileSummary"] == "Java 17 · gradle 8.7"
    assert latest["derivedApplicationType"] == "java_gradle"
    assert [s["name"] for s in latest["generatedPipeline"]["stages"]] == ["Checkout", "Build"]
    assert latest["requiredInputs"][0]["name"] == "NEXUS_PASSWORD"
    assert latest["validation"]["valid"] is True


def test_accepting_creates_the_pipeline_and_returns_the_service(
    client, admin_token, service_id, monkeypatch
):
    FakeHermes(good_response()).install(monkeypatch)
    fake_source.FAKE.load(fake_source.JAVA_GRADLE)
    client.post(
        f"/api/ci/services/{service_id}/analysis",
        json={},
        headers=auth_headers(admin_token),
    )
    analysis_id = CiRepositoryAnalysis.query.filter_by(service_id=service_id).one().id

    accepted = client.post(
        f"/api/ci/analyses/{analysis_id}/accept",
        json={"inputs": {"NEXUS_PASSWORD": "hunter2"}},
        headers=auth_headers(admin_token),
    )
    assert accepted.status_code == 200
    data = accepted.get_json()["data"]
    assert [s["name"] for s in data["pipeline"]["stages"]] == ["Checkout", "Build"]
    assert data["service"]["applicationType"] == "java_gradle"
    assert data["service"]["applicationProfile"]["languageVersion"] == "17"
    assert data["service"]["analysisState"] == "analyzed"

    # The value is never read back by any route.
    secrets = client.get(
        f"/api/ci/services/{service_id}/secrets", headers=auth_headers(admin_token)
    ).get_json()["data"]
    keys = [item["key"] for item in secrets["items"]]
    assert "NEXUS_PASSWORD" in keys
    assert "hunter2" not in accepted.get_data(as_text=True)


def test_saving_secret_values_needs_the_right_to_manage_secrets(
    client, admin_token, service_id, monkeypatch
):
    FakeHermes(good_response()).install(monkeypatch)
    fake_source.FAKE.load(fake_source.JAVA_GRADLE)
    client.post(
        f"/api/ci/services/{service_id}/analysis",
        json={},
        headers=auth_headers(admin_token),
    )
    analysis_id = CiRepositoryAnalysis.query.filter_by(service_id=service_id).one().id

    from api.routes import ci_assist as routes

    monkeypatch.setattr(routes, "_has_permission", lambda user, key: False)
    refused = client.post(
        f"/api/ci/analyses/{analysis_id}/accept",
        json={"inputs": {"NEXUS_PASSWORD": "hunter2"}},
        headers=auth_headers(admin_token),
    )
    assert refused.status_code == 403
    assert "secret" in refused.get_json()["error"].lower()


def test_a_failed_analysis_reports_what_happened_and_leaves_the_service_usable(
    client, admin_token, service_id, monkeypatch
):
    """Hermes must never be the reason somebody cannot create a service."""
    from api.services.application_intelligence_hermes import HermesTransientError

    FakeHermes(HermesTransientError("Hermes is unavailable or timed out.")).install(monkeypatch)
    fake_source.FAKE.load(fake_source.JAVA_GRADLE)
    client.post(
        f"/api/ci/services/{service_id}/analysis",
        json={},
        headers=auth_headers(admin_token),
    )

    data = client.get(
        f"/api/ci/services/{service_id}/analysis", headers=auth_headers(admin_token)
    ).get_json()["data"]
    assert data["latestAnalysis"]["state"] == "failed"
    assert "unavailable" in data["latestAnalysis"]["error"]

    service = client.get(
        f"/api/ci/services/{service_id}", headers=auth_headers(admin_token)
    ).get_json()["data"]
    assert service["status"] == "active"
    # The service's own copy of the state must not be left mid-flight: the
    # catalog reads it without loading an analysis, and a spinner next to a
    # row that finished is worse than no indicator at all.
    assert service["analysisState"] == "failed"
    # And the manual route is untouched.
    saved = client.post(
        f"/api/ci/services/{service_id}/pipelines",
        json={
            "name": "manual",
            "stages": [
                {"name": "Build", "stageType": "command", "commands": ["make build"]}
            ],
        },
        headers=auth_headers(admin_token),
    )
    assert saved.status_code == 201


def test_validate_generated_answers_without_saving_anything(client, admin_token, service_id):
    """The review screen calls this after every edit, so a person correcting a
    proposal is told the same things Hermes would have been.

    Under the default advise policy the objection is a note rather than a veto:
    the pipeline is saveable and the finding travels with it."""
    verdict = client.post(
        "/api/ci/pipelines/validate-generated",
        json={
            "serviceId": service_id,
            "pipeline": {
                "stages": [
                    {
                        "name": "Build",
                        "stageType": "command",
                        "runnerLabels": ["linux", "java11"],
                        "commands": ["./gradlew build"],
                    }
                ]
            },
        },
        headers=auth_headers(admin_token),
    ).get_json()["data"]

    assert verdict["valid"] is True
    assert any(
        item["code"] == "unsatisfiable_runner_labels" for item in verdict["warnings"]
    )
    # Asking what KubeSight thinks must not create anything.
    assert CiRepositoryAnalysis.query.count() == 0


def test_a_profile_can_be_corrected_by_hand_and_the_type_follows(
    client, admin_token, service_id
):
    updated = client.put(
        f"/api/ci/services/{service_id}/profile",
        json={
            "applicationProfile": {
                "language": "python",
                "languageVersion": "3.12",
                "buildSystem": "poetry",
                "packaging": "wheel",
            }
        },
        headers=auth_headers(admin_token),
    )
    assert updated.status_code == 200
    data = updated.get_json()["data"]
    assert data["applicationType"] == "python"
    assert data["profileSource"] == "manual"


def test_an_override_records_that_it_was_the_users_decision(
    client, admin_token, service_id
):
    client.put(
        f"/api/ci/services/{service_id}/profile",
        json={
            "applicationProfile": {
                "language": "java",
                "languageVersion": "17",
                "buildSystem": "gradle",
                "packaging": "jar",
            }
        },
        headers=auth_headers(admin_token),
    )
    data = client.put(
        f"/api/ci/services/{service_id}/profile",
        json={"overrides": {"languageVersion": "21"}},
        headers=auth_headers(admin_token),
    ).get_json()["data"]

    assert data["applicationProfile"]["languageVersion"] == "21"
    assert data["applicationProfile"]["overrides"]["languageVersion"]["was"] == "17"


# ---------------------------------------------------------------------------
# Nothing that existed before behaves differently
# ---------------------------------------------------------------------------

def test_a_service_that_never_meets_hermes_looks_exactly_as_it_did(client, admin_token):
    """Every new column is nullable and every new field has a null-equivalent.
    A service registered before this existed is 'not analyzed', which is the
    same thing it showed when there was nothing to say."""
    created = client.post(
        "/api/ci/services",
        json={"name": "Legacy Service", "applicationType": "java_maven"},
        headers=auth_headers(admin_token),
    ).get_json()["data"]

    assert created["analysisState"] == "not_analyzed"
    assert created["applicationProfile"] is None
    assert created["profileSource"] is None
    # The starter pipeline and Dockerfile still arrive exactly as before.
    assert created["pipelineConfigured"] is True
    assert created["pipelineStageCount"] > 0
    assert created["hasInlineDockerfile"] is True


def test_a_manually_configured_pipeline_is_never_touched_by_any_of_this(
    client, admin_token, service_id
):
    saved = client.post(
        f"/api/ci/services/{service_id}/pipelines",
        json={
            "name": "hand-written",
            "parameters": [
                {"name": "RELEASE", "type": "boolean", "label": "Release build"}
            ],
            "stages": [
                {
                    "name": "Build",
                    "stageType": "command",
                    "image": "registry.areeba.com/anything:1",
                    "runnerLabels": ["linux"],
                    "commands": ["make build"],
                    "timeoutSeconds": 900,
                }
            ],
        },
        headers=auth_headers(admin_token),
    )
    assert saved.status_code == 201
    data = saved.get_json()["data"]
    # A person naming an image is making a decision. The generated-pipeline
    # restriction governs proposals, not people.
    assert data["stages"][0]["image"] == "registry.areeba.com/anything:1"
    assert data["parameters"][0]["name"] == "RELEASE"


def test_the_jenkinsfile_importer_is_unaffected(client, admin_token, service_id):
    draft = client.post(
        "/api/ci/pipelines/import/jenkinsfile",
        json={
            "serviceId": service_id,
            "content": (
                "pipeline {\n"
                "  agent any\n"
                "  stages {\n"
                "    stage('Build') { steps { sh './gradlew build' } }\n"
                "  }\n"
                "}\n"
            ),
        },
        headers=auth_headers(admin_token),
    )
    assert draft.status_code == 200
    data = draft.get_json()["data"]
    # The importer supplies the Checkout stage a Jenkinsfile leaves implicit.
    assert [stage["name"] for stage in data["stages"]] == ["Checkout", "Build"]


def test_run_build_parameters_still_resolve_the_way_they_did(
    client, admin_token, service_id
):
    """Pipeline defaults are persistent and Run Build overrides are per-build.
    Nothing here changes either."""
    client.post(
        f"/api/ci/services/{service_id}/pipelines",
        json={
            "name": "hand-written",
            "parameters": [
                {"name": "TARGET", "type": "text", "label": "Target", "default": "uat"}
            ],
            "stages": [
                {"name": "Build", "stageType": "command", "commands": ["make build"]}
            ],
        },
        headers=auth_headers(admin_token),
    )
    data = client.get(
        f"/api/ci/services/{service_id}/parameters", headers=auth_headers(admin_token)
    ).get_json()["data"]
    target = next(item for item in data["items"] if item["name"] == "TARGET")
    assert target["default"] == "uat"


def test_deleting_a_service_takes_its_analyses_with_it(
    client, admin_token, service_id, monkeypatch
):
    FakeHermes(good_response()).install(monkeypatch)
    fake_source.FAKE.load(fake_source.JAVA_GRADLE)
    client.post(
        f"/api/ci/services/{service_id}/analysis",
        json={},
        headers=auth_headers(admin_token),
    )
    assert CiRepositoryAnalysis.query.count() == 1

    assert (
        client.delete(
            f"/api/ci/services/{service_id}", headers=auth_headers(admin_token)
        ).status_code
        == 200
    )
    assert CiRepositoryAnalysis.query.count() == 0


# ---------------------------------------------------------------------------
# The worker dying
# ---------------------------------------------------------------------------

def test_an_analysis_whose_worker_disappeared_is_closed_rather_than_left_spinning(
    app, service_id, monkeypatch
):
    """A visibly failed analysis is recoverable — the user presses Retry. An
    invisibly stalled one is not."""
    from datetime import datetime, timedelta, timezone

    row = CiRepositoryAnalysis(
        service_id=service_id,
        state="analyzing",
        current_stage="Generating pipeline",
        last_heartbeat_at=datetime.now(timezone.utc) - timedelta(hours=2),
    )
    db.session.add(row)
    db.session.commit()

    assert jobs.reap_stale() == 1
    db.session.refresh(row)
    assert row.state == "failed"
    assert "stopped responding" in row.safe_error_message
    assert row.failure_stage == "Generating pipeline"


def test_a_live_analysis_is_not_reaped(app, service_id):
    from datetime import datetime, timezone

    row = CiRepositoryAnalysis(
        service_id=service_id,
        state="analyzing",
        last_heartbeat_at=datetime.now(timezone.utc),
    )
    db.session.add(row)
    db.session.commit()
    assert jobs.reap_stale() == 0
