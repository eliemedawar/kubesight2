"""Native CI replacing Jenkins in the deploy automation and Mobile Applications.

The contract under test: with a CI service registered whose slug matches the
deployment (or custom environment) name, a ticket-driven run builds on
KubeSight's own CI — Jenkins disabled, unconfigured, and never contacted.
"""

from __future__ import annotations

import os
import tempfile
from unittest.mock import patch

import pytest

from api.db import db
from api.models import DeployAutomationRun, MobileApplication, MobileAppBuild
from api.models_application_intelligence import BitbucketCredentialProfile
from api.models_ci import CiBuild, CiService
from api.secret_encryption import encrypt_secret
from api.services.deploy_automation_service import (
    _do_check,
    _do_poll_build,
    _do_trigger_custom,
    get_or_create_jenkins,
)
from tests.conftest import auth_headers


@pytest.fixture()
def runnable_service(app, client, admin_token):
    """An active CI service (slug payment-service) with source + pipeline."""
    with app.app_context():
        credential = BitbucketCredentialProfile(
            name="ci-token",
            provider="bitbucket",
            credential_type="repository_access_token",
            secret_cipher=encrypt_secret("clone-token"),
            read_only=True,
            enabled=True,
        )
        db.session.add(credential)
        db.session.commit()
        credential_id = credential.id

    service_id = client.post(
        "/api/ci/services",
        json={"name": "Payment Service", "applicationType": "java"},
        headers=auth_headers(admin_token),
    ).get_json()["data"]["id"]
    client.put(
        f"/api/ci/services/{service_id}/source",
        json={
            "repositoryUrl": "https://bitbucket.org/areeba/payment-service",
            "credentialProfileId": credential_id,
        },
        headers=auth_headers(admin_token),
    )
    pipeline_id = client.get(
        f"/api/ci/services/{service_id}/pipelines", headers=auth_headers(admin_token)
    ).get_json()["data"]["items"][0]["id"]
    client.put(
        f"/api/ci/pipelines/{pipeline_id}",
        json={
            "stages": [
                {"name": "Checkout", "stageType": "checkout", "runnerLabels": ["mock"]},
                {"name": "Build", "stageType": "command", "commands": ["mvn package"],
                 "runnerLabels": ["mock"]},
            ]
        },
        headers=auth_headers(admin_token),
    )
    return service_id


def _make_run(app, **overrides) -> int:
    with app.app_context():
        run = DeployAutomationRun(
            cluster_id=overrides.get("cluster_id", "prod-us-east"),
            namespace=overrides.get("namespace", "payments"),
            deployment_name=overrides.get("deployment_name", "payment-service"),
            image_repo=overrides.get("image_repo", "nexus.company.local/payment-service"),
            image_tag=overrides.get("image_tag", "v1.72.1"),
            ticket_tag=overrides.get("ticket_tag", "1.72.1"),
            ticket_number=overrides.get("ticket_number", "DR-145"),
            status=overrides.get("status", "checking_image"),
            change_type="image",
        )
        db.session.add(run)
        db.session.commit()
        return run.id


def _drain_ci(app, max_passes=40):
    from api.services.ci import engine
    from api.services.ci.runners import mock as mock_runner

    original = mock_runner._STAGE_SECONDS
    mock_runner._STAGE_SECONDS = 0.0
    try:
        with app.app_context():
            for _ in range(max_passes):
                engine.advance_ci_builds()
                if not CiBuild.query.filter(
                    CiBuild.status.in_(("queued", "running"))
                ).count():
                    return
    finally:
        mock_runner._STAGE_SECONDS = original


# ---------------------------------------------------------------------------
# Deploy automation builds on native CI — Jenkins never configured
# ---------------------------------------------------------------------------

def test_missing_image_triggers_a_native_ci_build(app, runnable_service):
    run_id = _make_run(app)
    with app.app_context():
        run = db.session.get(DeployAutomationRun, run_id)
        jrow = get_or_create_jenkins()
        assert not jrow.enabled  # Jenkins is OFF for the whole test.

        with patch(
            "api.services.registry_service.check_image",
            return_value={"status": "not_found", "message": "", "registry": "", "enforcement": "block"},
        ):
            _do_check(run, jrow)
        db.session.commit()

        assert run.status == "building"
        assert run.ci_build_id is not None
        assert run.jenkins_queue_url is None  # Jenkins untouched.

        build = db.session.get(CiBuild, run.ci_build_id)
        assert build.trigger_type == "automation"
        # The ticket's resolved tag is pinned onto the build.
        assert build.pipeline_snapshot["variables"]["IMAGE_TAG"] == "v1.72.1"
        assert build.pipeline_snapshot["variables"]["TICKET_TAG"] == "1.72.1"

        step = next(s for s in run.steps if s["key"] == "build")
        assert "KubeSight CI build" in step["detail"]


def test_native_build_success_moves_the_run_to_verification(app, runnable_service):
    run_id = _make_run(app)
    with app.app_context():
        run = db.session.get(DeployAutomationRun, run_id)
        jrow = get_or_create_jenkins()
        with patch(
            "api.services.registry_service.check_image",
            return_value={"status": "not_found", "message": "", "registry": "", "enforcement": "block"},
        ):
            _do_check(run, jrow)
        db.session.commit()

    _drain_ci(app)

    with app.app_context():
        run = db.session.get(DeployAutomationRun, run_id)
        assert db.session.get(CiBuild, run.ci_build_id).status == "success"
        _do_poll_build(run, get_or_create_jenkins())
        db.session.commit()
        assert run.status == "verifying_image"
        assert next(s for s in run.steps if s["key"] == "build")["status"] == "done"


def test_native_build_failure_fails_the_run_with_the_stage_name(app, runnable_service):
    run_id = _make_run(app)
    with app.app_context():
        run = db.session.get(DeployAutomationRun, run_id)
        with patch(
            "api.services.registry_service.check_image",
            return_value={"status": "not_found", "message": "", "registry": "", "enforcement": "block"},
        ):
            _do_check(run, get_or_create_jenkins())
        db.session.commit()

    from api.services.ci.runners import base as runner_base

    adapter = runner_base.get_adapter("mock")
    original_poll = adapter.poll
    adapter.poll = lambda handle: runner_base.FAILED
    try:
        _drain_ci(app)
    finally:
        adapter.poll = original_poll

    with app.app_context():
        run = db.session.get(DeployAutomationRun, run_id)
        _do_poll_build(run, get_or_create_jenkins())
        db.session.commit()
        assert run.status == "failed"
        assert "CI build" in run.error and "Service Catalog" in run.error


def test_without_a_ci_service_or_jenkins_the_failure_points_at_the_catalog(app):
    run_id = _make_run(app, deployment_name="unregistered-app")
    with app.app_context():
        run = db.session.get(DeployAutomationRun, run_id)
        with patch(
            "api.services.registry_service.check_image",
            return_value={"status": "not_found", "message": "", "registry": "", "enforcement": "block"},
        ):
            _do_check(run, get_or_create_jenkins())
        db.session.commit()
        assert run.status == "failed"
        assert "Register a CI service" in run.error
        assert "unregistered-app" in run.error


# ---------------------------------------------------------------------------
# Custom environments (the mobile flow) build on native CI
# ---------------------------------------------------------------------------

def _mobile_app_with_ci(app, service_id, environment="pos-uat"):
    with app.app_context():
        mobile = MobileApplication(
            name="POS App",
            enabled=True,
            zoho_environment=environment,
            ci_service_id=service_id,
            artifact_config={"android": {"pattern": "*.apk"}},
        )
        db.session.add(mobile)
        db.session.commit()
        return mobile.id


def _ci_build_with_apk(app, service_id) -> int:
    """A successful CI build whose artifact store really contains an APK."""
    from api.services.ci import artifacts as ci_artifacts
    from api.services.ci.runners.base import ArtifactRef

    with app.app_context():
        build = CiBuild(
            service_id=service_id,
            number=41,
            status="success",
            branch="develop",
            pipeline_snapshot={"stages": []},
        )
        db.session.add(build)
        db.session.flush()
        handle, path = tempfile.mkstemp(suffix=".apk")
        with os.fdopen(handle, "wb") as sink:
            sink.write(b"PK\x03\x04 fake apk bytes")
        try:
            ci_artifacts.record_artifact(
                service_id=service_id,
                build_id=build.id,
                build_stage_id=None,
                ref=ArtifactRef(name="pos-release.apk", artifact_type="apk", local_path=path),
                version="41",
                commit=True,
            )
        finally:
            os.remove(path)
        return build.id


def test_custom_environment_run_builds_and_ingests_from_ci(app, client, admin_token):
    """The Zoho→APK flow with zero Jenkins: custom env run → CI build →
    binaries land in the mobile store straight from the CI artifact store."""
    from api.services.zoho_sync_service import CUSTOM_SOURCE_CLUSTER

    # A CI service whose slug matches the custom environment name.
    with app.app_context():
        credential = BitbucketCredentialProfile(
            name="mobile-token", provider="bitbucket",
            credential_type="repository_access_token",
            secret_cipher=encrypt_secret("tok"), read_only=True, enabled=True,
        )
        db.session.add(credential)
        db.session.commit()
        credential_id = credential.id
    service_id = client.post(
        "/api/ci/services",
        json={"name": "POS UAT", "slug": "pos-uat", "applicationType": "android"},
        headers=auth_headers(admin_token),
    ).get_json()["data"]["id"]
    client.put(
        f"/api/ci/services/{service_id}/source",
        json={"repositoryUrl": "https://bitbucket.org/areeba/pos-app",
              "credentialProfileId": credential_id},
        headers=auth_headers(admin_token),
    )

    mobile_id = _mobile_app_with_ci(app, service_id, environment="pos-uat")
    run_id = _make_run(
        app, cluster_id=CUSTOM_SOURCE_CLUSTER, namespace="pos-uat",
        deployment_name="pos-app", status="queued",
    )

    with app.app_context():
        run = db.session.get(DeployAutomationRun, run_id)
        _do_trigger_custom(run, get_or_create_jenkins())
        db.session.commit()
        assert run.status == "building"
        assert run.ci_build_id is not None
        # The registry gate is skipped for custom environments.
        assert next(s for s in run.steps if s["key"] == "image_check")["status"] == "skip"

    _drain_ci(app)

    # Attach a real APK artifact to the finished build, then poll: the run
    # completes and the binary is ingested without any download step.
    with app.app_context():
        from api.services.ci import artifacts as ci_artifacts
        from api.services.ci.runners.base import ArtifactRef

        run = db.session.get(DeployAutomationRun, run_id)
        handle, path = tempfile.mkstemp(suffix=".apk")
        with os.fdopen(handle, "wb") as sink:
            sink.write(b"PK\x03\x04 built apk")
        try:
            ci_artifacts.record_artifact(
                service_id=service_id, build_id=run.ci_build_id, build_stage_id=None,
                ref=ArtifactRef(name="pos.apk", artifact_type="apk", local_path=path),
                commit=True,
            )
        finally:
            os.remove(path)

        _do_poll_build(run, get_or_create_jenkins())
        db.session.commit()

        assert run.status == "deployed"
        binaries = MobileAppBuild.query.filter_by(app_id=mobile_id).all()
        assert len(binaries) == 1
        binary = binaries[0]
        assert binary.status == "available"
        assert binary.ci_build_id == run.ci_build_id
        assert binary.source == "ticket"
        assert binary.ticket_number == "DR-145"
        # The bytes really moved into the mobile binary store.
        from api.services.mobile_app_service import binary_path

        assert os.path.isfile(binary_path(binary))


# ---------------------------------------------------------------------------
# Manual "Fetch latest" from CI
# ---------------------------------------------------------------------------

def test_fetch_latest_ingests_from_the_linked_ci_service(app, client, admin_token, runnable_service):
    mobile_id = _mobile_app_with_ci(app, runnable_service, environment="payments-uat")
    _ci_build_with_apk(app, runnable_service)

    from api.services.mobile_app_service import fetch_latest

    with app.app_context():
        created = fetch_latest(mobile_id)
        assert len(created) == 1
        assert created[0]["status"] == "available"
        assert created[0]["ciBuildId"] is not None
        assert created[0]["platform"] == "android"

        # Idempotent: the same CI build is not ingested twice.
        with pytest.raises(Exception):
            fetch_latest(mobile_id)


def test_fetch_latest_without_any_source_names_the_ci_catalog_first(app):
    with app.app_context():
        mobile = MobileApplication(name="Orphan App", enabled=True,
                                   artifact_config={"android": {"pattern": "*.apk"}})
        db.session.add(mobile)
        db.session.commit()
        from api.services.mobile_app_service import MobileAppError, fetch_latest

        with pytest.raises(MobileAppError) as excinfo:
            fetch_latest(mobile.id)
        assert "CI service" in str(excinfo.value)


# ---------------------------------------------------------------------------
# Per-trigger variables (the mechanism the automation rides on)
# ---------------------------------------------------------------------------

def test_trigger_variables_reach_every_stage_environment(app, client, admin_token, runnable_service):
    with app.app_context():
        from api.services.ci import engine

        service = db.session.get(CiService, runnable_service)
        data = engine.trigger_build(
            service, trigger_type="automation", variables={"IMAGE_TAG": "v9.9.9"}
        )
        build = db.session.get(CiBuild, data["id"])
        assert build.pipeline_snapshot["variables"] == {"IMAGE_TAG": "v9.9.9"}

    _drain_ci(app)

    build_id = data["id"]
    detail = client.get(
        f"/api/ci/builds/{build_id}", headers=auth_headers(admin_token)
    ).get_json()["data"]
    assert detail["status"] == "success"
    logs = client.get(
        f"/api/ci/builds/{build_id}/stages/{detail['stages'][1]['id']}/logs?limit=5000",
        headers=auth_headers(admin_token),
    ).get_data(as_text=True)
    # The mock runner echoes the env var names it received.
    assert "IMAGE_TAG" in logs
