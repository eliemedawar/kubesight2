"""CI engine: queue, scheduling, stage lifecycle, cancel/retry, log masking.

These drive the real engine against the mock runner, so the queue, the
scheduler, the state machine, secret injection and log masking are all exercised
end to end. Swapping in the Kubernetes adapter must not change any assertion
here — that is the point of the runner port.
"""

from __future__ import annotations

import pytest

from api.db import db
from api.models_application_intelligence import BitbucketCredentialProfile
from api.models_ci import CiBuild, CiRunner, CiService
from api.secret_encryption import encrypt_secret
from tests.conftest import auth_headers


def test_stage_image_resolves_run_build_variables_without_a_shell():
    from api.services.ci import engine

    assert engine._resolve_stage_image(
        "registry.areeba.com/gradle:${GradleVersion}-jdk11",
        {"GradleVersion": "8.10"},
        "build jar file",
    ) == "registry.areeba.com/gradle:8.10-jdk11"


def test_stage_image_reports_a_missing_run_build_variable():
    from api.services.ci import engine

    with pytest.raises(engine.BuildError, match="GradleVersion.*empty or missing"):
        engine._resolve_stage_image(
            "registry.areeba.com/gradle:${GradleVersion}-jdk11",
            {},
            "build jar file",
        )


def test_working_directory_takes_a_build_input():
    from api.services.ci import engine

    env = {"MODULE": "ds-amex"}
    assert engine._resolve_working_directory("modules/${MODULE}", env, "Build Image") == "modules/ds-amex"
    assert engine._resolve_working_directory("modules/$MODULE/", env, "Build Image") == "modules/ds-amex"
    assert engine._resolve_working_directory("modules/${MODULE}", {"MODULE": "/ds-amex"}, "s") == "modules/ds-amex"
    # Literal paths are untouched; empty stays empty.
    assert engine._resolve_working_directory("modules/ds-amex", {}, "s") == "modules/ds-amex"
    assert engine._resolve_working_directory("", env, "s") is None


@pytest.mark.parametrize("module", ["../secrets", "a b", "x;rm -rf /", "$(id)"])
def test_working_directory_input_cannot_escape_or_inject(module):
    from api.services.ci import engine

    with pytest.raises(engine.BuildError, match="working directory"):
        engine._resolve_working_directory("modules/${MODULE}", {"MODULE": module}, "Build Image")


def test_working_directory_with_an_empty_input_fails_instead_of_building_the_parent():
    from api.services.ci import engine

    with pytest.raises(engine.BuildError, match="MODULE.*empty or missing"):
        engine._resolve_working_directory("modules/${MODULE}", {"MODULE": ""}, "Build Image")


def test_image_name_takes_a_build_input():
    from api.services.ci import engine

    registry = {"repository": "build", "repositoryTemplate": "areeba/${MODULE}"}
    engine._resolve_image_name(registry, {"MODULE": "DS-Amex"}, "Build Image")
    assert registry["repository"] == "areeba/ds-amex"
    assert "repositoryTemplate" not in registry

    literal = {"repository": "jpts", "repositoryTemplate": ""}
    engine._resolve_image_name(literal, {"MODULE": "ds-amex"}, "Build Image")
    assert literal["repository"] == "jpts"

    with pytest.raises(engine.BuildError, match="MODULE.*empty or missing"):
        engine._resolve_image_name({"repositoryTemplate": "${MODULE}"}, {}, "Build Image")


@pytest.fixture()
def runnable_service(app, client, admin_token):
    """A service with source connected and a two-stage pipeline."""
    with app.app_context():
        credential = BitbucketCredentialProfile(
            name="ci-token",
            provider="bitbucket",
            credential_type="repository_access_token",
            secret_cipher=encrypt_secret("clone-token-value"),
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
            "defaultBranch": "develop",
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
            # Explicitly parameter-free: these tests pass free-form variables,
            # which a pipeline that declares parameters rejects by design. The
            # java starter template declares SKIP_TESTS, and inheriting it here
            # would make the fixture about something other than what it says.
            "parameters": [],
            "stages": [
                {"name": "Checkout", "stageType": "checkout", "runnerLabels": ["mock"]},
                {
                    "name": "Build",
                    "stageType": "command",
                    "commands": ["mvn -B package"],
                    "runnerLabels": ["mock"],
                    "artifacts": [{"path": "target/app.jar", "type": "jar"}],
                },
            ]
        },
        headers=auth_headers(admin_token),
    )
    return service_id


def _store_legacy_stage(app, pipeline_id, *, name, stage_type):
    """Append a stage row the way a pipeline saved before a type was retired
    left it — straight to the table, past today's save validation."""
    from api.models_ci import CiPipeline, CiPipelineStage

    with app.app_context():
        pipeline = db.session.get(CiPipeline, pipeline_id)
        position = len(pipeline.stages)
        db.session.add(
            CiPipelineStage(
                pipeline_id=pipeline_id,
                position=position,
                name=name,
                stage_type=stage_type,
                runner_labels=["mock"],
                commands=[],
                env={},
                secret_refs=[],
                artifacts=[],
                timeout_seconds=1800,
                enabled=True,
            )
        )
        db.session.commit()
    # Requests share the fixture's session, which still holds the pipeline's
    # stage list as it was before this insert.
    db.session.expire_all()


def _drain(app, max_passes: int = 40):
    """Run the scheduler tick until every build reaches a terminal state.

    The mock runner reports RUNNING for a couple of seconds, so this also
    shortens its stage duration — the test asserts on state transitions, not on
    wall-clock behaviour.
    """
    from api.services.ci import engine
    from api.services.ci.runners import mock as mock_runner

    original = mock_runner._STAGE_SECONDS
    mock_runner._STAGE_SECONDS = 0.0
    try:
        with app.app_context():
            for _ in range(max_passes):
                engine.advance_ci_builds()
                pending = CiBuild.query.filter(
                    CiBuild.status.in_(("queued", "running"))
                ).count()
                if not pending:
                    return
    finally:
        mock_runner._STAGE_SECONDS = original


# ---------------------------------------------------------------------------
# Triggering and the happy path
# ---------------------------------------------------------------------------

def test_run_build_queues_and_snapshots_the_pipeline(client, admin_token, runnable_service):
    response = client.post(
        f"/api/ci/services/{runnable_service}/builds",
        json={"branch": "develop"},
        headers=auth_headers(admin_token),
    )
    assert response.status_code == 201
    data = response.get_json()["data"]

    assert data["status"] == "queued"
    assert data["number"] == 1
    assert data["branch"] == "develop"
    assert data["triggerType"] == "manual"
    # Stage rows exist up front so the UI renders the whole pipeline immediately.
    assert [stage["name"] for stage in data["stages"]] == ["Checkout", "Build"]
    assert all(stage["status"] == "pending" for stage in data["stages"])


def test_build_runs_to_success_through_the_scheduler(app, client, admin_token, runnable_service):
    build_id = client.post(
        f"/api/ci/services/{runnable_service}/builds",
        json={},
        headers=auth_headers(admin_token),
    ).get_json()["data"]["id"]

    _drain(app)

    data = client.get(
        f"/api/ci/builds/{build_id}", headers=auth_headers(admin_token)
    ).get_json()["data"]
    assert data["status"] == "success"
    assert [stage["status"] for stage in data["stages"]] == ["success", "success"]
    assert data["durationSeconds"] is not None
    assert data["error"] is None


def test_successful_build_records_declared_artifacts(app, client, admin_token, runnable_service):
    build_id = client.post(
        f"/api/ci/services/{runnable_service}/builds",
        json={},
        headers=auth_headers(admin_token),
    ).get_json()["data"]["id"]
    _drain(app)

    artifacts = client.get(
        f"/api/ci/builds/{build_id}/artifacts", headers=auth_headers(admin_token)
    ).get_json()["data"]["items"]
    assert len(artifacts) == 1
    assert artifacts[0]["artifactType"] == "jar"
    assert artifacts[0]["version"] == "1"


def test_build_numbers_increment_per_service(client, admin_token, runnable_service):
    first = client.post(
        f"/api/ci/services/{runnable_service}/builds", json={}, headers=auth_headers(admin_token)
    ).get_json()["data"]
    second = client.post(
        f"/api/ci/services/{runnable_service}/builds", json={}, headers=auth_headers(admin_token)
    ).get_json()["data"]
    assert (first["number"], second["number"]) == (1, 2)


def test_runner_slot_is_released_after_the_build(app, client, admin_token, runnable_service):
    client.post(
        f"/api/ci/services/{runnable_service}/builds", json={}, headers=auth_headers(admin_token)
    )
    _drain(app)
    with app.app_context():
        runner = CiRunner.query.filter_by(name="kubesight-mock").first()
        assert runner.current_load == 0


# ---------------------------------------------------------------------------
# Gates
# ---------------------------------------------------------------------------

def test_build_is_refused_without_source(client, admin_token):
    service_id = client.post(
        "/api/ci/services",
        json={"name": "No Source", "applicationType": "generic"},
        headers=auth_headers(admin_token),
    ).get_json()["data"]["id"]

    response = client.post(
        f"/api/ci/services/{service_id}/builds", json={}, headers=auth_headers(admin_token)
    )
    assert response.status_code == 400
    assert "Connect a repository" in response.get_json()["error"]


def test_build_is_refused_when_the_service_is_paused(client, admin_token, runnable_service):
    client.put(
        f"/api/ci/services/{runnable_service}",
        json={"status": "paused"},
        headers=auth_headers(admin_token),
    )
    response = client.post(
        f"/api/ci/services/{runnable_service}/builds", json={}, headers=auth_headers(admin_token)
    )
    assert response.status_code == 400
    assert "paused" in response.get_json()["error"]


def test_build_waits_when_no_runner_is_eligible(app, client, admin_token, runnable_service):
    """A build with no compatible runner stays queued and explains why."""
    with app.app_context():
        for runner in CiRunner.query.all():
            runner.enabled = False
            db.session.add(runner)
        db.session.commit()

    build_id = client.post(
        f"/api/ci/services/{runnable_service}/builds", json={}, headers=auth_headers(admin_token)
    ).get_json()["data"]["id"]

    from api.services.ci import engine

    with app.app_context():
        engine.advance_ci_builds()

    data = client.get(
        f"/api/ci/builds/{build_id}", headers=auth_headers(admin_token)
    ).get_json()["data"]
    assert data["status"] == "queued"
    assert "No CI runner is online" in (data["queueReason"] or "")


def test_service_concurrency_limit_holds_the_second_build(app, client, admin_token, runnable_service):
    from api.services.ci import engine
    from api.services.ci.runners import mock as mock_runner

    first = client.post(
        f"/api/ci/services/{runnable_service}/builds", json={}, headers=auth_headers(admin_token)
    ).get_json()["data"]["id"]
    second = client.post(
        f"/api/ci/services/{runnable_service}/builds", json={}, headers=auth_headers(admin_token)
    ).get_json()["data"]["id"]

    # Keep the first build running so the limit is actually exercised.
    original = mock_runner._STAGE_SECONDS
    mock_runner._STAGE_SECONDS = 600.0
    try:
        with app.app_context():
            engine.advance_ci_builds()
            engine.advance_ci_builds()
            assert db.session.get(CiBuild, first).status == "running"
            held = db.session.get(CiBuild, second)
            assert held.status == "queued"
            assert "concurrent" in (held.queue_reason or "")
    finally:
        mock_runner._STAGE_SECONDS = original


# ---------------------------------------------------------------------------
# Cancel and retry
# ---------------------------------------------------------------------------

def test_cancelling_a_queued_build_is_immediate(client, admin_token, runnable_service):
    build_id = client.post(
        f"/api/ci/services/{runnable_service}/builds", json={}, headers=auth_headers(admin_token)
    ).get_json()["data"]["id"]

    response = client.post(
        f"/api/ci/builds/{build_id}/cancel", headers=auth_headers(admin_token)
    )
    assert response.status_code == 200
    assert response.get_json()["data"]["status"] == "cancelled"


def test_cancelling_a_running_build_stops_it_on_the_next_tick(
    app, client, admin_token, runnable_service
):
    from api.services.ci import engine
    from api.services.ci.runners import mock as mock_runner

    build_id = client.post(
        f"/api/ci/services/{runnable_service}/builds", json={}, headers=auth_headers(admin_token)
    ).get_json()["data"]["id"]

    original = mock_runner._STAGE_SECONDS
    mock_runner._STAGE_SECONDS = 600.0
    try:
        with app.app_context():
            engine.advance_ci_builds()
            assert db.session.get(CiBuild, build_id).status == "running"
        client.post(f"/api/ci/builds/{build_id}/cancel", headers=auth_headers(admin_token))
        with app.app_context():
            engine.advance_ci_builds()
            build = db.session.get(CiBuild, build_id)
            assert build.status == "cancelled"
            # Nothing is left claiming to be in flight.
            assert not [s for s in build.stages if s.status in ("pending", "running")]
    finally:
        mock_runner._STAGE_SECONDS = original


def test_cancelling_a_finished_build_is_rejected(app, client, admin_token, runnable_service):
    build_id = client.post(
        f"/api/ci/services/{runnable_service}/builds", json={}, headers=auth_headers(admin_token)
    ).get_json()["data"]["id"]
    _drain(app)

    response = client.post(
        f"/api/ci/builds/{build_id}/cancel", headers=auth_headers(admin_token)
    )
    assert response.status_code == 409


def test_retry_creates_a_new_build_linked_to_the_original(
    app, client, admin_token, runnable_service
):
    build_id = client.post(
        f"/api/ci/services/{runnable_service}/builds",
        json={"branch": "release/1.2"},
        headers=auth_headers(admin_token),
    ).get_json()["data"]["id"]
    _drain(app)

    response = client.post(
        f"/api/ci/builds/{build_id}/retry", headers=auth_headers(admin_token)
    )
    assert response.status_code == 201
    data = response.get_json()["data"]
    assert data["number"] == 2
    assert data["triggerType"] == "retry"
    assert data["retryOfBuildId"] == build_id
    assert data["branch"] == "release/1.2"


def test_retry_reruns_the_original_snapshot_not_the_edited_pipeline(
    app, client, admin_token, runnable_service
):
    """Editing a pipeline must not rewrite what a past build ran."""
    build_id = client.post(
        f"/api/ci/services/{runnable_service}/builds", json={}, headers=auth_headers(admin_token)
    ).get_json()["data"]["id"]
    _drain(app)

    pipeline_id = client.get(
        f"/api/ci/services/{runnable_service}/pipelines", headers=auth_headers(admin_token)
    ).get_json()["data"]["items"][0]["id"]
    client.put(
        f"/api/ci/pipelines/{pipeline_id}",
        json={"stages": [{"name": "Only Stage", "stageType": "command", "commands": ["x"]}]},
        headers=auth_headers(admin_token),
    )

    original = client.get(
        f"/api/ci/builds/{build_id}", headers=auth_headers(admin_token)
    ).get_json()["data"]
    assert [stage["name"] for stage in original["stages"]] == ["Checkout", "Build"]


# ---------------------------------------------------------------------------
# Tag builds — the Jenkins-style "build this release tag" flow
# ---------------------------------------------------------------------------

def _link_registry(app, service_id):
    from api.models import RegistryConnection

    with app.app_context():
        row = RegistryConnection(
            name="nexus", base_url="nexus.example.com:8083",
            image_hosts="registry.local", username="svc", enabled=True,
            enforcement="block",
        )
        db.session.add(row)
        db.session.flush()
        service = db.session.get(CiService, service_id)
        service.registry_connection_id = row.id
        db.session.add(service)
        db.session.commit()


def test_tag_build_is_recorded_and_names_the_image_after_the_git_tag(
    app, client, admin_token, runnable_service
):
    _link_registry(app, runnable_service)
    data = client.post(
        f"/api/ci/services/{runnable_service}/builds",
        json={"branch": "v1.72.1", "refType": "tag"},
        headers=auth_headers(admin_token),
    ).get_json()["data"]
    assert data["refType"] == "tag"
    assert data["branch"] == "v1.72.1"

    from api.services.ci import engine

    with app.app_context():
        build = db.session.get(CiBuild, data["id"])
        assert build.pipeline_snapshot["refType"] == "tag"
        registry, reason = engine._registry_for(build, {})
        assert reason is None
        # The whole point of building v1.72.1 is an image called v1.72.1 —
        # no build-number suffix.
        assert registry["tag"] == "v1.72.1"


def test_branch_build_image_tag_keeps_the_build_number(
    app, client, admin_token, runnable_service
):
    _link_registry(app, runnable_service)
    data = client.post(
        f"/api/ci/services/{runnable_service}/builds",
        json={},
        headers=auth_headers(admin_token),
    ).get_json()["data"]
    # No refType given → a branch build of the default branch.
    assert data["refType"] == "branch"
    assert data["branch"] == "develop"

    from api.services.ci import engine

    with app.app_context():
        build = db.session.get(CiBuild, data["id"])
        registry, reason = engine._registry_for(build, {})
        assert reason is None
        assert registry["tag"] == f"develop-{data['number']}"


def test_unknown_ref_type_falls_back_to_branch(client, admin_token, runnable_service):
    data = client.post(
        f"/api/ci/services/{runnable_service}/builds",
        json={"branch": "develop", "refType": "bogus"},
        headers=auth_headers(admin_token),
    ).get_json()["data"]
    assert data["refType"] == "branch"


def test_retry_of_a_tag_build_keeps_the_ref_kind_and_variables(
    app, client, admin_token, runnable_service
):
    """A retried tag build must produce the same image tag the original did."""
    build_id = client.post(
        f"/api/ci/services/{runnable_service}/builds",
        json={"branch": "v2.0.0", "refType": "tag", "variables": {"FOO": "bar"}},
        headers=auth_headers(admin_token),
    ).get_json()["data"]["id"]
    _drain(app)

    retried = client.post(
        f"/api/ci/builds/{build_id}/retry", headers=auth_headers(admin_token)
    ).get_json()["data"]
    assert retried["refType"] == "tag"
    assert retried["branch"] == "v2.0.0"

    with app.app_context():
        row = db.session.get(CiBuild, retried["id"])
        assert row.pipeline_snapshot["refType"] == "tag"
        assert row.pipeline_snapshot["variables"] == {"FOO": "bar"}


# ---------------------------------------------------------------------------
# Failure behaviour
# ---------------------------------------------------------------------------

def test_a_failed_stage_fails_the_build_and_skips_the_rest(
    app, client, admin_token, runnable_service
):
    from api.services.ci import engine
    from api.services.ci.runners import base as runner_base

    build_id = client.post(
        f"/api/ci/services/{runnable_service}/builds", json={}, headers=auth_headers(admin_token)
    ).get_json()["data"]["id"]

    adapter = runner_base.get_adapter("mock")
    original_poll = adapter.poll
    adapter.poll = lambda handle: runner_base.FAILED
    try:
        _drain(app)
    finally:
        adapter.poll = original_poll

    data = client.get(
        f"/api/ci/builds/{build_id}", headers=auth_headers(admin_token)
    ).get_json()["data"]
    assert data["status"] == "failed"
    assert data["stages"][0]["status"] == "failed"
    # Downstream work must not run after a required stage fails.
    assert data["stages"][1]["status"] == "skipped"


def test_continue_on_failure_keeps_going_but_still_fails_the_build(
    app, client, admin_token, runnable_service
):
    """`continueOnFailure` means 'collect more information', not 'report green'."""
    from api.services.ci import engine
    from api.services.ci.runners import base as runner_base

    pipeline_id = client.get(
        f"/api/ci/services/{runnable_service}/pipelines", headers=auth_headers(admin_token)
    ).get_json()["data"]["items"][0]["id"]
    client.put(
        f"/api/ci/pipelines/{pipeline_id}",
        json={
            "stages": [
                {
                    "name": "Lint",
                    "stageType": "command",
                    "commands": ["lint"],
                    "continueOnFailure": True,
                    "runnerLabels": ["mock"],
                },
                {
                    "name": "Build",
                    "stageType": "command",
                    "commands": ["build"],
                    "runnerLabels": ["mock"],
                },
            ]
        },
        headers=auth_headers(admin_token),
    )

    build_id = client.post(
        f"/api/ci/services/{runnable_service}/builds", json={}, headers=auth_headers(admin_token)
    ).get_json()["data"]["id"]

    adapter = runner_base.get_adapter("mock")
    original_poll = adapter.poll
    calls = {"n": 0}

    def poll_first_fails(handle):
        calls["n"] += 1
        return runner_base.FAILED if calls["n"] == 1 else runner_base.SUCCEEDED

    adapter.poll = poll_first_fails
    try:
        _drain(app)
    finally:
        adapter.poll = original_poll

    data = client.get(
        f"/api/ci/builds/{build_id}", headers=auth_headers(admin_token)
    ).get_json()["data"]
    assert data["stages"][0]["status"] == "failed"
    assert data["stages"][1]["status"] == "success"
    assert data["status"] == "failed"


def test_a_stage_type_with_no_executor_is_skipped_not_succeeded(
    app, client, admin_token, runnable_service
):
    """A build must never report success for work KubeSight cannot do.

    A `container_image` stage has no builder until BuildKit ships. Dispatching
    it to a runner would run zero commands, exit 0, and report success — a build
    claiming it pushed an image that does not exist.
    """
    pipeline_id = client.get(
        f"/api/ci/services/{runnable_service}/pipelines", headers=auth_headers(admin_token)
    ).get_json()["data"]["items"][0]["id"]
    client.put(
        f"/api/ci/pipelines/{pipeline_id}",
        json={
            "stages": [
                {
                    "name": "Compile",
                    "stageType": "command",
                    "commands": ["mvn package"],
                    "runnerLabels": ["mock"],
                },
                {"name": "Build Image", "stageType": "container_image", "runnerLabels": ["mock"]},
            ]
        },
        headers=auth_headers(admin_token),
    )
    # Scan stages can no longer be SAVED, but one stored before that still has
    # to load, and still has to skip rather than pass.
    _store_legacy_stage(app, pipeline_id, name="Scan", stage_type="scan")

    build_id = client.post(
        f"/api/ci/services/{runnable_service}/builds", json={}, headers=auth_headers(admin_token)
    ).get_json()["data"]["id"]
    _drain(app)

    data = client.get(
        f"/api/ci/builds/{build_id}", headers=auth_headers(admin_token)
    ).get_json()["data"]
    statuses = {stage["name"]: stage["status"] for stage in data["stages"]}
    assert statuses["Compile"] == "success"
    assert statuses["Build Image"] == "skipped"
    assert statuses["Scan"] == "skipped"
    # A skip is not a failure — the build still passes.
    assert data["status"] == "success"

    # ...and the stage log says exactly why, rather than sitting empty.
    image_stage = next(s for s in data["stages"] if s["name"] == "Build Image")
    logs = client.get(
        f"/api/ci/builds/{build_id}/stages/{image_stage['id']}/logs",
        headers=auth_headers(admin_token),
    ).get_json()["data"]
    text = " ".join(line["content"] for line in logs["lines"])
    assert "Skipped" in text and "BuildKit" in text
    assert "no artifact was recorded" in text

    # Nothing was invented on the way past.
    artifacts = client.get(
        f"/api/ci/builds/{build_id}/artifacts", headers=auth_headers(admin_token)
    ).get_json()["data"]["items"]
    assert not [a for a in artifacts if a["artifactType"] == "container-image"]


def test_a_pipeline_of_only_unimplemented_stages_still_completes(
    app, client, admin_token, runnable_service
):
    """Consecutive skips must not stall the build one tick at a time."""
    pipeline_id = client.get(
        f"/api/ci/services/{runnable_service}/pipelines", headers=auth_headers(admin_token)
    ).get_json()["data"]["items"][0]["id"]
    client.put(
        f"/api/ci/pipelines/{pipeline_id}",
        json={
            "stages": [
                {"name": "Image", "stageType": "container_image", "runnerLabels": ["mock"]},
            ]
        },
        headers=auth_headers(admin_token),
    )
    _store_legacy_stage(app, pipeline_id, name="Publish", stage_type="publish_artifact")
    build_id = client.post(
        f"/api/ci/services/{runnable_service}/builds", json={}, headers=auth_headers(admin_token)
    ).get_json()["data"]["id"]

    from api.services.ci import engine

    # A single pass must resolve the whole run of skips.
    with app.app_context():
        engine.advance_ci_builds()
        engine.advance_ci_builds()

    data = client.get(
        f"/api/ci/builds/{build_id}", headers=auth_headers(admin_token)
    ).get_json()["data"]
    assert data["status"] == "success"
    assert all(stage["status"] == "skipped" for stage in data["stages"])


# ---------------------------------------------------------------------------
# Logs
# ---------------------------------------------------------------------------

def test_stage_logs_are_readable_by_offset(app, client, admin_token, runnable_service):
    build_id = client.post(
        f"/api/ci/services/{runnable_service}/builds", json={}, headers=auth_headers(admin_token)
    ).get_json()["data"]["id"]
    _drain(app)

    build = client.get(
        f"/api/ci/builds/{build_id}", headers=auth_headers(admin_token)
    ).get_json()["data"]
    stage_id = build["stages"][1]["id"]

    first = client.get(
        f"/api/ci/builds/{build_id}/stages/{stage_id}/logs?after=0&limit=2",
        headers=auth_headers(admin_token),
    ).get_json()["data"]
    assert len(first["lines"]) == 2
    assert first["hasMore"] is True

    second = client.get(
        f"/api/ci/builds/{build_id}/stages/{stage_id}/logs?after={first['nextSeq']}",
        headers=auth_headers(admin_token),
    ).get_json()["data"]
    assert second["lines"][0]["seq"] > first["lines"][-1]["seq"]
    assert second["complete"] is True


def test_secret_values_are_masked_out_of_logs(app, client, admin_token, runnable_service):
    """A secret injected into a stage must never appear in stored output."""
    secret_value = "sup3r-s3cret-nexus-password"
    client.post(
        f"/api/ci/services/{runnable_service}/secrets",
        json={"key": "NEXUS_PASSWORD", "value": secret_value},
        headers=auth_headers(admin_token),
    )
    pipeline_id = client.get(
        f"/api/ci/services/{runnable_service}/pipelines", headers=auth_headers(admin_token)
    ).get_json()["data"]["items"][0]["id"]
    client.put(
        f"/api/ci/pipelines/{pipeline_id}",
        json={
            "stages": [
                {
                    "name": "Publish",
                    "stageType": "command",
                    # A pipeline author echoing a secret is exactly the case the
                    # masker exists for.
                    "commands": [f"echo {secret_value}"],
                    "secretRefs": [{"name": "NEXUS_PASSWORD"}],
                    "runnerLabels": ["mock"],
                }
            ]
        },
        headers=auth_headers(admin_token),
    )

    build_id = client.post(
        f"/api/ci/services/{runnable_service}/builds", json={}, headers=auth_headers(admin_token)
    ).get_json()["data"]["id"]
    _drain(app)

    build = client.get(
        f"/api/ci/builds/{build_id}", headers=auth_headers(admin_token)
    ).get_json()["data"]
    stage_id = build["stages"][0]["id"]
    logs = client.get(
        f"/api/ci/builds/{build_id}/stages/{stage_id}/logs?limit=5000",
        headers=auth_headers(admin_token),
    ).get_data(as_text=True)

    assert secret_value not in logs
    assert "***" in logs


def test_clone_credentials_are_masked_out_of_checkout_logs(
    app, client, admin_token, runnable_service
):
    build_id = client.post(
        f"/api/ci/services/{runnable_service}/builds", json={}, headers=auth_headers(admin_token)
    ).get_json()["data"]["id"]
    _drain(app)

    build = client.get(
        f"/api/ci/builds/{build_id}", headers=auth_headers(admin_token)
    ).get_json()["data"]
    for stage in build["stages"]:
        logs = client.get(
            f"/api/ci/builds/{build_id}/stages/{stage['id']}/logs?limit=5000",
            headers=auth_headers(admin_token),
        ).get_data(as_text=True)
        assert "clone-token-value" not in logs


def test_engine_drives_the_kubernetes_runner_end_to_end(
    app, client, admin_token, runnable_service
):
    """Engine ↔ Kubernetes adapter over a fake cluster: one Job for the whole
    build, per-stage advancement from initContainer statuses, logs pumped, and
    success only after the collector (whole Job) finishes."""
    import json as _json

    from api.services.ci import engine
    from api.services.ci.runners import kubernetes as k8s

    class FakeCluster:
        def __init__(self):
            self.applies = 0
            self.job = None
            self.job_status = {}
            self.pod = None

        def __call__(self, args, input_text=None):
            if args[0] == "apply":
                self.applies += 1
                items = _json.loads(input_text)["items"]
                self.job = next(i for i in items if i["kind"] == "Job")
                template = self.job["spec"]["template"]
                self.pod = {
                    "metadata": {
                        "creationTimestamp": "2026-09-01T10:00:00Z",
                        "annotations": template["metadata"]["annotations"],
                    },
                    "spec": {"initContainers": template["spec"]["initContainers"]},
                    "status": {
                        "initContainerStatuses": [
                            {"name": c["name"], "state": {"waiting": {}}}
                            for c in template["spec"]["initContainers"]
                        ]
                    },
                }
                return 0, "", ""
            if args[:2] == ["get", "job"]:
                if "jsonpath" in " ".join(args):
                    return 0, "uid-1", ""
                if self.job is None:
                    return 1, "", "NotFound"
                return 0, _json.dumps({"metadata": {}, "status": self.job_status}), ""
            if args[:2] == ["get", "pods"]:
                return 0, _json.dumps({"items": [self.pod] if self.pod else []}), ""
            if args[0] == "logs":
                return 0, "cluster line 1\ncluster line 2", ""
            return 0, "", ""

        def finish_stage(self, name, exit_code=0):
            for status in self.pod["status"]["initContainerStatuses"]:
                if status["name"] == name:
                    status["state"] = {"terminated": {"exitCode": exit_code}}

    fake = FakeCluster()
    k8s.set_kubectl_runner(fake)
    try:
        # The fixture pipeline targets the mock runner; retarget it at linux so
        # capability matching selects the Kubernetes runner instead.
        pipeline_id = client.get(
            f"/api/ci/services/{runnable_service}/pipelines",
            headers=auth_headers(admin_token),
        ).get_json()["data"]["items"][0]["id"]
        client.put(
            f"/api/ci/pipelines/{pipeline_id}",
            json={
                "stages": [
                    {"name": "Checkout", "stageType": "checkout", "runnerLabels": ["linux"]},
                    {
                        "name": "Build",
                        "stageType": "command",
                        "commands": ["mvn -B package"],
                        "runnerLabels": ["linux"],
                        "image": "maven:3.9",
                    },
                ]
            },
            headers=auth_headers(admin_token),
        )
        with app.app_context():
            from api.models_ci import CiRunner

            for runner in CiRunner.query.all():
                runner.enabled = runner.runner_type == "kubernetes"
                db.session.add(runner)
            db.session.commit()

        build_id = client.post(
            f"/api/ci/services/{runnable_service}/builds",
            json={},
            headers=auth_headers(admin_token),
        ).get_json()["data"]["id"]

        with app.app_context():
            engine.advance_ci_builds()  # dispatch -> ONE Job for the build
            assert fake.applies == 1
            names = [c["name"] for c in fake.job["spec"]["template"]["spec"]["initContainers"]]
            assert names == ["stage-0", "stage-1"]

            fake.finish_stage("stage-0")
            engine.advance_ci_builds()  # stage-0 success
            engine.advance_ci_builds()  # stage-1 starts (no second apply)
            assert fake.applies == 1

            fake.finish_stage("stage-1")
            engine.advance_ci_builds()
            build = db.session.get(CiBuild, build_id)
            # Last stage done, but the collector has not finished: not success yet.
            assert build.status == "running"

            fake.job_status = {"succeeded": 1}
            engine.advance_ci_builds()
            build = db.session.get(CiBuild, build_id)
            assert build.status == "success"
            assert [s.status for s in build.stages] == ["success", "success"]
    finally:
        k8s.set_kubectl_runner(None)

    # Logs were pumped from the cluster into the stage records.
    data = client.get(
        f"/api/ci/builds/{build_id}", headers=auth_headers(admin_token)
    ).get_json()["data"]
    logs = client.get(
        f"/api/ci/builds/{build_id}/stages/{data['stages'][0]['id']}/logs",
        headers=auth_headers(admin_token),
    ).get_json()["data"]
    assert any("cluster line" in line["content"] for line in logs["lines"])


# ---------------------------------------------------------------------------
# Reaper
# ---------------------------------------------------------------------------

def test_a_lost_running_build_is_reaped(app, client, admin_token, runnable_service):
    from datetime import datetime, timedelta, timezone

    from api.services.ci import engine
    from api.services.ci.runners import mock as mock_runner

    build_id = client.post(
        f"/api/ci/services/{runnable_service}/builds", json={}, headers=auth_headers(admin_token)
    ).get_json()["data"]["id"]

    original = mock_runner._STAGE_SECONDS
    mock_runner._STAGE_SECONDS = 600.0
    try:
        with app.app_context():
            engine.advance_ci_builds()
            build = db.session.get(CiBuild, build_id)
            assert build.status == "running"
            # Backdate every sign of progress past the stale window, as a
            # restart would leave it: nothing has happened since.
            _backdate_progress(build, minutes=engine._STALE_BUILD_MINUTES + 5)

            engine.advance_ci_builds()
            assert db.session.get(CiBuild, build_id).status == "timeout"
    finally:
        mock_runner._STAGE_SECONDS = original


def _backdate_progress(build, *, minutes, logs_minutes=None):
    """Move the build, its stages and (unless told otherwise) its log lines back."""
    from datetime import datetime, timedelta, timezone

    from api.models_ci import CiLogChunk

    then = datetime.now(timezone.utc) - timedelta(minutes=minutes)
    build.started_at = then
    build.queued_at = then
    for stage in build.stages:
        if stage.started_at is not None:
            stage.started_at = then
        if stage.finished_at is not None:
            stage.finished_at = then
        db.session.add(stage)
    log_then = datetime.now(timezone.utc) - timedelta(
        minutes=minutes if logs_minutes is None else logs_minutes
    )
    for chunk in CiLogChunk.query.filter(
        CiLogChunk.build_stage_id.in_([stage.id for stage in build.stages])
    ).all():
        chunk.created_at = log_then
        db.session.add(chunk)
    db.session.add(build)
    db.session.commit()


def test_a_long_build_that_is_still_logging_is_not_reaped(
    app, client, admin_token, runnable_service
):
    """The reaper used to anchor on started_at, so a release build still
    printing after an hour was killed as 'lost'. Progress is the anchor now."""
    from api.services.ci import engine
    from api.services.ci.runners import mock as mock_runner

    build_id = client.post(
        f"/api/ci/services/{runnable_service}/builds", json={}, headers=auth_headers(admin_token)
    ).get_json()["data"]["id"]

    original = mock_runner._STAGE_SECONDS
    mock_runner._STAGE_SECONDS = 600.0
    try:
        with app.app_context():
            engine.advance_ci_builds()
            build = db.session.get(CiBuild, build_id)
            assert build.status == "running"
            # Started well past the idle window (but inside its overall
            # deadline), and a log line has just arrived.
            assert engine._STALE_BUILD_MINUTES + 10 < engine._build_deadline_minutes(build)
            _backdate_progress(build, minutes=engine._STALE_BUILD_MINUTES + 10)
            from api.services.ci import logs as logs_service

            running = next(s for s in build.stages if s.status == "running")
            logs_service.append_system(running, "still compiling")
            db.session.commit()
            engine._reap_stale_builds()
            assert db.session.get(CiBuild, build_id).status == "running"
    finally:
        mock_runner._STAGE_SECONDS = original


def test_a_build_past_the_sum_of_its_stage_timeouts_is_reaped_even_if_busy(
    app, client, admin_token, runnable_service
):
    from api.services.ci import engine
    from api.services.ci.runners import mock as mock_runner

    build_id = client.post(
        f"/api/ci/services/{runnable_service}/builds", json={}, headers=auth_headers(admin_token)
    ).get_json()["data"]["id"]

    original = mock_runner._STAGE_SECONDS
    mock_runner._STAGE_SECONDS = 600.0
    try:
        with app.app_context():
            engine.advance_ci_builds()
            build = db.session.get(CiBuild, build_id)
            deadline = engine._build_deadline_minutes(build)
            # Two stages at the default 1800s, plus the grace.
            assert deadline == 60 + engine._BUILD_DEADLINE_GRACE_MINUTES
            _backdate_progress(build, minutes=deadline + 5, logs_minutes=1)
            engine._reap_stale_builds()
            reaped = db.session.get(CiBuild, build_id)
            assert reaped.status == "timeout"
            assert "overall deadline" in reaped.error
    finally:
        mock_runner._STAGE_SECONDS = original


def test_the_overall_deadline_never_exceeds_the_hard_cap(app, monkeypatch):
    from api.services.ci import engine

    monkeypatch.setattr(engine, "_BUILD_HARD_CAP_MINUTES", 90)
    build = CiBuild(
        pipeline_snapshot={"stages": [{"timeoutSeconds": 86400}, {"timeoutSeconds": 86400}]}
    )
    assert engine._build_deadline_minutes(build) == 90


def test_queue_depth_is_reported(client, admin_token, runnable_service):
    client.post(
        f"/api/ci/services/{runnable_service}/builds", json={}, headers=auth_headers(admin_token)
    )
    response = client.get(
        f"/api/ci/services/{runnable_service}/builds", headers=auth_headers(admin_token)
    )
    assert response.get_json()["data"]["queueDepth"] == 1


# ---------------------------------------------------------------------------
# Handover latency
# ---------------------------------------------------------------------------

def test_a_finished_stage_starts_its_successor_in_the_same_pass(
    app, client, admin_token, runnable_service
):
    """A stage boundary must not cost a tick.

    Closing a stage and starting the next used to be two separate passes, so
    every boundary in a pipeline added a whole scheduler interval of dead air —
    the single largest source of "why is CI slow" when the stages themselves
    take seconds.
    """
    from api.services.ci import engine
    from api.services.ci.runners import mock as mock_runner

    build_id = client.post(
        f"/api/ci/services/{runnable_service}/builds", json={}, headers=auth_headers(admin_token)
    ).get_json()["data"]["id"]

    original = mock_runner._STAGE_SECONDS
    mock_runner._STAGE_SECONDS = 0.0
    try:
        with app.app_context():
            engine.advance_ci_builds()  # dispatch: the first stage is running
            stages = sorted(
                db.session.get(CiBuild, build_id).stages, key=lambda s: s.position
            )
            assert [s.status for s in stages] == ["running", "pending"]

            engine.advance_ci_builds()  # one pass: close the first, start the second
            stages = sorted(
                db.session.get(CiBuild, build_id).stages, key=lambda s: s.position
            )
            assert [s.status for s in stages] == ["success", "running"]
    finally:
        mock_runner._STAGE_SECONDS = original


def test_an_agent_reporting_a_result_can_claim_the_next_stage_at_once(
    app, client, admin_token, runnable_service
):
    """The agent's own loop is the fast path: reporting a stage's exit code
    queues the next stage's task before that request returns, so the claim the
    agent makes immediately afterwards finds work instead of a 204 and a wait."""
    from api.models_ci import CiRunner
    from api.services.ci import agents as agents_service
    from api.services.ci import engine

    # An agent-only fleet, and a pipeline with nothing pinned to the mock runner.
    pipeline_id = client.get(
        f"/api/ci/services/{runnable_service}/pipelines", headers=auth_headers(admin_token)
    ).get_json()["data"]["items"][0]["id"]
    client.put(
        f"/api/ci/pipelines/{pipeline_id}",
        json={
            "stages": [
                {"name": "Checkout", "stageType": "checkout"},
                {"name": "Build", "stageType": "command", "commands": ["mvn -B package"]},
            ]
        },
        headers=auth_headers(admin_token),
    )

    with app.app_context():
        for runner in CiRunner.query.all():
            runner.enabled = False
            db.session.add(runner)
        runner, token = agents_service.create_agent(
            {"name": "linux-fast", "runnerType": "agent_linux", "maxConcurrent": 1}
        )
        agents_service.heartbeat(runner, {"capabilities": ["linux", "java"]})
        db.session.commit()

    agent_headers = {"Authorization": f"Bearer {token}"}

    build_id = client.post(
        f"/api/ci/services/{runnable_service}/builds", json={}, headers=auth_headers(admin_token)
    ).get_json()["data"]["id"]
    with app.app_context():
        engine.advance_ci_builds()  # dispatch: stage 0 is offered to the agent

    first = client.post("/api/ci/agent/claim", json={}, headers=agent_headers)
    assert first.status_code == 200
    first_task = first.get_json()["data"]
    assert first_task["stageName"] == "Checkout"

    first_result = client.post(
        f"/api/ci/agent/tasks/{first_task['taskId']}/result",
        json={"exitCode": 0, "claimToken": first_task["claimToken"]},
        headers=agent_headers,
    )
    assert first_result.get_json()["data"]["cleanupWorkspace"] is False

    # No engine pass in between: the result callback did the handover.
    second = client.post("/api/ci/agent/claim", json={}, headers=agent_headers)
    assert second.status_code == 200
    assert second.get_json()["data"]["stageName"] == "Build"

    with app.app_context():
        stages = sorted(
            db.session.get(CiBuild, build_id).stages, key=lambda s: s.position
        )
        assert [s.status for s in stages] == ["success", "running"]

    second_task = second.get_json()["data"]
    final_result = client.post(
        f"/api/ci/agent/tasks/{second_task['taskId']}/result",
        json={"exitCode": 0, "claimToken": second_task["claimToken"]},
        headers=agent_headers,
    )
    assert final_result.get_json()["data"]["cleanupWorkspace"] is True


# ---------------------------------------------------------------------------
# The simulated runner never runs on a real installation
# ---------------------------------------------------------------------------

@pytest.fixture()
def real_mode(monkeypatch):
    from api import k8s_provider

    monkeypatch.setattr(k8s_provider, "is_real_mode_enabled", lambda: True)


def test_the_mock_runner_is_never_eligible_in_real_mode(
    app, client, admin_token, runnable_service, real_mode
):
    """It runs no commands and reports green. On a real installation a build
    it 'ran' would be a lie a deploy could act on — so the build waits, and
    says why, instead of succeeding."""
    from api.services.ci import engine, scheduler

    with app.app_context():
        assert [r.name for r in scheduler.eligible_runners()] == []

    build_id = client.post(
        f"/api/ci/services/{runnable_service}/builds", json={}, headers=auth_headers(admin_token)
    ).get_json()["data"]["id"]
    with app.app_context():
        engine.advance_ci_builds()
        mock = CiRunner.query.filter_by(name="kubesight-mock").one()
        # Status derivation agrees: it shows offline, not online-and-unused.
        assert mock.status == "offline"

    data = client.get(
        f"/api/ci/builds/{build_id}", headers=auth_headers(admin_token)
    ).get_json()["data"]
    assert data["status"] == "queued"
    assert "mock runner is never used" in data["queueReason"]


def test_the_mock_runner_cannot_be_enabled_in_real_mode(
    app, client, admin_token, real_mode
):
    with app.app_context():
        mock_id = CiRunner.query.filter_by(name="kubesight-mock").one().id
    refused = client.put(
        f"/api/ci/runners/{mock_id}", json={"enabled": True}, headers=auth_headers(admin_token)
    )
    assert refused.status_code == 409


def test_the_seeder_disarms_the_mock_runner_in_real_mode(app, real_mode):
    """An installation that started as a demo keeps no armed simulator once it
    is connected to real clusters. A fresh real install also gets the
    Kubernetes runner enabled; an existing one keeps what its operator set."""
    from api.migrate_rbac import _seed_builtin_ci_runners

    with app.app_context():
        mock = CiRunner.query.filter_by(name="kubesight-mock").one()
        mock.enabled = True
        mock.status = "online"
        kubernetes = CiRunner.query.filter_by(name="kubesight-kubernetes").one()
        assert kubernetes.enabled is False  # seeded in mock mode by the fixture
        db.session.commit()

        _seed_builtin_ci_runners()
        assert db.session.get(CiRunner, mock.id).enabled is False
        assert db.session.get(CiRunner, mock.id).status == "offline"
        # Operators own enabled on rows that exist.
        assert db.session.get(CiRunner, kubernetes.id).enabled is False

        db.session.delete(db.session.get(CiRunner, kubernetes.id))
        db.session.delete(db.session.get(CiRunner, mock.id))
        db.session.commit()
        _seed_builtin_ci_runners()
        fresh_mock = CiRunner.query.filter_by(name="kubesight-mock").one()
        fresh_kubernetes = CiRunner.query.filter_by(name="kubesight-kubernetes").one()
        assert fresh_mock.enabled is False
        assert fresh_kubernetes.enabled is True


def test_mock_mode_still_uses_the_mock_runner(app, client, admin_token, runnable_service):
    build_id = client.post(
        f"/api/ci/services/{runnable_service}/builds", json={}, headers=auth_headers(admin_token)
    ).get_json()["data"]["id"]
    _drain(app)
    data = client.get(
        f"/api/ci/builds/{build_id}", headers=auth_headers(admin_token)
    ).get_json()["data"]
    assert data["status"] == "success"


# ---------------------------------------------------------------------------
# One runner must satisfy every stage of the build
# ---------------------------------------------------------------------------

def _runner(name, *, capabilities=(), labels=(), runner_type="mock"):
    row = CiRunner(
        name=name,
        runner_type=runner_type,
        status="online",
        enabled=True,
        capabilities=list(capabilities),
        labels=list(labels),
        max_concurrent=2,
    )
    db.session.add(row)
    return row


def test_runner_selection_uses_the_union_of_every_stage(app):
    """The first stage is usually a label-less checkout. Choosing from it alone
    put an Android build on a box without the Android SDK."""
    from api.services.ci import scheduler

    with app.app_context():
        for existing in CiRunner.query.all():
            existing.enabled = False
        _runner("plain-linux", capabilities=["linux"])
        _runner("android-box", capabilities=["linux", "android"])
        db.session.commit()

        stages = [
            {"stageType": "checkout", "runnerLabels": []},
            {"stageType": "command", "runnerLabels": ["linux"]},
            {"stageType": "command", "runnerLabels": ["android"]},
        ]
        requirements = scheduler.requirements_for_build(stages)
        assert set(requirements.labels) == {"linux", "android"}
        assert scheduler.select_runner(requirements).runner.name == "android-box"


def test_stages_that_will_not_run_do_not_constrain_the_runner(app):
    from api.services.ci import scheduler

    with app.app_context():
        for existing in CiRunner.query.all():
            existing.enabled = False
        _runner("plain-linux", capabilities=["linux"])
        db.session.commit()
        stages = [
            {"stageType": "command", "runnerLabels": ["linux"]},
            {"stageType": "command", "runnerLabels": ["macos"], "skip": True},
        ]
        requirements = scheduler.requirements_for_build(
            stages, lambda definition: bool(definition.get("skip"))
        )
        assert scheduler.select_runner(requirements).ok


def test_routing_labels_count_as_well_as_capabilities(app):
    """An operator labels an agent so a stage can route to it by that label."""
    from api.services.ci import scheduler
    from api.services.ci.runners.base import StageRequirements

    with app.app_context():
        for existing in CiRunner.query.all():
            existing.enabled = False
        _runner("gpu-box", capabilities=["linux"], labels=["gpu"])
        db.session.commit()
        chosen = scheduler.select_runner(StageRequirements(labels=("linux", "gpu")))
        assert chosen.ok and chosen.runner.name == "gpu-box"


def test_no_single_runner_covering_the_union_is_said_clearly(app):
    from api.services.ci import scheduler

    with app.app_context():
        for existing in CiRunner.query.all():
            existing.enabled = False
        _runner("android-box", capabilities=["linux", "android"])
        _runner("mac", capabilities=["macos", "xcode"])
        db.session.commit()
        requirements = scheduler.requirements_for_build(
            [{"runnerLabels": ["android"]}, {"runnerLabels": ["xcode"]}]
        )
        refused = scheduler.select_runner(requirements)
        assert not refused.ok
        assert "No single online runner" in refused.reason
        assert "android" in refused.reason and "xcode" in refused.reason


def test_stages_pinning_different_runner_types_are_refused(app):
    from api.services.ci import scheduler

    with app.app_context():
        requirements = scheduler.requirements_for_build(
            [{"runnerType": "kubernetes"}, {"runnerType": "agent_macos"}]
        )
        refused = scheduler.select_runner(requirements)
        assert not refused.ok
        assert "different runner types" in refused.reason


def test_a_build_needing_a_label_no_runner_has_waits_with_the_reason(
    app, client, admin_token, runnable_service
):
    """End to end: the label on the SECOND stage decides, not the first."""
    pipeline_id = client.get(
        f"/api/ci/services/{runnable_service}/pipelines", headers=auth_headers(admin_token)
    ).get_json()["data"]["items"][0]["id"]
    saved = client.put(
        f"/api/ci/pipelines/{pipeline_id}",
        json={
            "stages": [
                {"name": "Checkout", "stageType": "checkout"},
                {
                    "name": "Build",
                    "stageType": "command",
                    "commands": ["make"],
                    "runnerLabels": ["quantum"],
                },
            ]
        },
        headers=auth_headers(admin_token),
    )
    assert saved.status_code == 200, saved.get_json()
    build_id = client.post(
        f"/api/ci/services/{runnable_service}/builds", json={}, headers=auth_headers(admin_token)
    ).get_json()["data"]["id"]

    from api.services.ci import engine

    with app.app_context():
        engine.advance_ci_builds()
    data = client.get(
        f"/api/ci/builds/{build_id}", headers=auth_headers(admin_token)
    ).get_json()["data"]
    assert data["status"] == "queued"
    assert "quantum" in data["queueReason"]


# ---------------------------------------------------------------------------
# Retired stage types and parallel groups are refused on save
# ---------------------------------------------------------------------------

# `scan` is no longer here: it has an executor now, and a scan stage without a
# scanner is refused with its own message (tests/test_ci_scan_stage.py).
@pytest.mark.parametrize("stage_type", ["publish_artifact"])
def test_a_stage_type_with_no_executor_cannot_be_saved(
    client, admin_token, runnable_service, stage_type
):
    pipeline_id = client.get(
        f"/api/ci/services/{runnable_service}/pipelines", headers=auth_headers(admin_token)
    ).get_json()["data"]["items"][0]["id"]
    refused = client.put(
        f"/api/ci/pipelines/{pipeline_id}",
        json={"stages": [{"name": "Extra", "stageType": stage_type}]},
        headers=auth_headers(admin_token),
    )
    assert refused.status_code == 400
    assert "no executor" in refused.get_json()["error"]


def test_a_parallel_group_of_one_is_refused(client, admin_token, runnable_service):
    """A group of one stage is just a stage: saying it runs "in parallel"
    with nothing would be a promise about nothing. (The full rules live in
    test_ci_parallel_stages.py.)"""
    pipeline_id = client.get(
        f"/api/ci/services/{runnable_service}/pipelines", headers=auth_headers(admin_token)
    ).get_json()["data"]["items"][0]["id"]
    refused = client.put(
        f"/api/ci/pipelines/{pipeline_id}",
        json={
            "stages": [
                {
                    "name": "Build",
                    "stageType": "command",
                    "commands": ["make"],
                    "parallelGroup": "Tests",
                }
            ]
        },
        headers=auth_headers(admin_token),
    )
    assert refused.status_code == 400
    assert "needs at least two stages" in refused.get_json()["error"]

    from api.models_ci import CiPipelineStage

    assert CiPipelineStage.query.filter(CiPipelineStage.parallel_group.isnot(None)).count() == 0


# ---------------------------------------------------------------------------
# Build statuses reported back to the source host
# ---------------------------------------------------------------------------

class _FakeStatusHandler:
    def __init__(self, delegate, *, fail=False):
        self._delegate = delegate
        self.fail = fail
        self.posts = []

    def __getattr__(self, name):
        return getattr(self._delegate, name)

    def post_check_verdict(self, ref, credential, **kwargs):
        self.posts.append(kwargs)
        if self.fail:
            raise RuntimeError("bitbucket is down")


@pytest.fixture()
def status_handler(app, monkeypatch):
    from api.services.ci import build_status
    from api.services.ci import source as source_port

    real_get = source_port.get_provider
    handler = _FakeStatusHandler(real_get("bitbucket"))
    monkeypatch.setattr(
        source_port,
        "get_provider",
        lambda name: handler if name == "bitbucket" else real_get(name),
    )
    monkeypatch.setattr(build_status, "_dispatch", lambda job: job())
    monkeypatch.setenv("PUBLIC_BASE_URL", "https://kubesight.example")
    return handler


def _make_writable(app, service_id):
    with app.app_context():
        service = db.session.get(CiService, service_id)
        service.credential_profile.read_only = False
        db.session.commit()


SHA = "0123456789abcdef0123456789abcdef01234567"


def test_a_build_reports_inprogress_then_its_outcome_under_its_own_key(
    app, client, admin_token, runnable_service, status_handler
):
    _make_writable(app, runnable_service)
    build_id = client.post(
        f"/api/ci/services/{runnable_service}/builds",
        json={"commitSha": SHA},
        headers=auth_headers(admin_token),
    ).get_json()["data"]["id"]
    _drain(app)

    states = [post["state"] for post in status_handler.posts]
    assert states[0] == "running"
    assert states[-1] == "passed"
    assert {post["status_key"] for post in status_handler.posts} == {"KUBESIGHT-BUILD"}
    assert all(post["commit_sha"] == SHA for post in status_handler.posts)
    assert status_handler.posts[-1]["url"].endswith(
        f"/#/service-catalog/{runnable_service}/builds?build={build_id}"
    )


def test_a_cancelled_build_reports_stopped(
    app, client, admin_token, runnable_service, status_handler
):
    from api.services.ci import engine
    from api.services.ci.runners import mock as mock_runner

    _make_writable(app, runnable_service)
    build_id = client.post(
        f"/api/ci/services/{runnable_service}/builds",
        json={"commitSha": SHA},
        headers=auth_headers(admin_token),
    ).get_json()["data"]["id"]
    original = mock_runner._STAGE_SECONDS
    mock_runner._STAGE_SECONDS = 600.0
    try:
        with app.app_context():
            engine.advance_ci_builds()
        client.post(f"/api/ci/builds/{build_id}/cancel", headers=auth_headers(admin_token))
        with app.app_context():
            engine.advance_ci_builds()
    finally:
        mock_runner._STAGE_SECONDS = original
    assert status_handler.posts[-1]["state"] == "stopped"


def test_a_read_only_credential_reports_nothing(
    app, client, admin_token, runnable_service, status_handler
):
    client.post(
        f"/api/ci/services/{runnable_service}/builds",
        json={"commitSha": SHA},
        headers=auth_headers(admin_token),
    )
    _drain(app)
    assert status_handler.posts == []


def test_a_failing_status_post_never_fails_the_build(
    app, client, admin_token, runnable_service, status_handler
):
    _make_writable(app, runnable_service)
    status_handler.fail = True
    build_id = client.post(
        f"/api/ci/services/{runnable_service}/builds",
        json={"commitSha": SHA},
        headers=auth_headers(admin_token),
    ).get_json()["data"]["id"]
    _drain(app)
    data = client.get(
        f"/api/ci/builds/{build_id}", headers=auth_headers(admin_token)
    ).get_json()["data"]
    assert data["status"] == "success"
    assert status_handler.posts  # it did try


def test_merge_check_builds_leave_reporting_to_the_merge_check(
    app, client, admin_token, runnable_service, status_handler
):
    from api.services.ci import engine

    _make_writable(app, runnable_service)
    with app.app_context():
        engine.trigger_build(
            db.session.get(CiService, runnable_service),
            commit_sha=SHA,
            trigger_type="webhook",
            variables={"KUBESIGHT_MERGE_CHECK": "true"},
        )
    _drain(app)
    assert status_handler.posts == []
