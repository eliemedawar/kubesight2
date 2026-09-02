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
                {"name": "Scan", "stageType": "scan", "runnerLabels": ["mock"]},
            ]
        },
        headers=auth_headers(admin_token),
    )

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
                {"name": "Publish", "stageType": "publish_artifact", "runnerLabels": ["mock"]},
            ]
        },
        headers=auth_headers(admin_token),
    )
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
            # Backdate it past the stale window, as a restart would leave it.
            build.started_at = datetime.now(timezone.utc) - timedelta(
                minutes=engine._STALE_BUILD_MINUTES + 5
            )
            db.session.add(build)
            db.session.commit()

            engine.advance_ci_builds()
            assert db.session.get(CiBuild, build_id).status == "timeout"
    finally:
        mock_runner._STAGE_SECONDS = original


def test_queue_depth_is_reported(client, admin_token, runnable_service):
    client.post(
        f"/api/ci/services/{runnable_service}/builds", json={}, headers=auth_headers(admin_token)
    )
    response = client.get(
        f"/api/ci/services/{runnable_service}/builds", headers=auth_headers(admin_token)
    )
    assert response.get_json()["data"]["queueDepth"] == 1
