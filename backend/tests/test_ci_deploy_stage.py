"""The Deploy stage: a pipeline's last step rolls its image out to a cluster.

What is locked here:

* the stage's configuration rules (one target, a manifest that may only
  create a Deployment and its Service, Deploy stages last);
* "whoever saved the target" — the stamp is set by the server, carried over
  when the target is unchanged, and refused to someone who cannot deploy there;
* the run: registry confirmation, namespace must exist, image-only change on an
  existing deployment, create-if-missing, the approval queue, rollout, rollback;
* the engine never hands the stage to a runner, and skips it after a failure;
* a ticket-driven run takes the stage's result as its own instead of deploying
  a second time.

Clusters here are the mock ones (prod-us-east has payments/payments-api), so the
cluster side is simulated by deploy_stage itself; the real-cluster branches are
driven by patching kubectl.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from api.db import db
from api.models import ChangeBundle, DeployAutomationRun, DeploymentRequestSetting, User
from api.models_application_intelligence import BitbucketCredentialProfile
from api.models_ci import CiArtifact, CiBuild, CiPipeline, CiService
from api.secret_encryption import encrypt_secret
from api.services.ci import deploy_config
from tests.conftest import auth_headers

MANIFEST = """apiVersion: apps/v1
kind: Deployment
metadata:
  name: brand-new
spec:
  replicas: 1
  selector:
    matchLabels: {app: brand-new}
  template:
    metadata:
      labels: {app: brand-new}
    spec:
      containers:
      - name: brand-new
        image: ${IMAGE}
        ports:
        - containerPort: 8080
---
apiVersion: v1
kind: Service
metadata:
  name: brand-new
spec:
  selector: {app: brand-new}
  ports:
  - port: 80
    targetPort: 8080
"""


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


def _stages(deploy=None, *, extra_after=None):
    stages = [
        {"name": "Checkout", "stageType": "checkout", "runnerLabels": ["mock"]},
        {"name": "Build", "stageType": "command", "commands": ["make"], "runnerLabels": ["mock"]},
        {"name": "Deploy", "stageType": "deploy", "deploy": deploy or _deploy()},
    ]
    if extra_after:
        stages.append(extra_after)
    return stages


def _approvals(app, required: int) -> None:
    with app.app_context():
        row = DeploymentRequestSetting.query.first()
        if row is None:
            row = DeploymentRequestSetting()
            db.session.add(row)
        row.required_approvals = required
        db.session.commit()


@pytest.fixture()
def service(app, client, admin_token):
    """A service with source connected, and its pipeline id."""
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
        json={"name": "Payments Api", "applicationType": "java"},
        headers=auth_headers(admin_token),
    ).get_json()["data"]["id"]
    client.put(
        f"/api/ci/services/{service_id}/source",
        json={
            "repositoryUrl": "https://bitbucket.org/areeba/payments-api",
            "defaultBranch": "main",
            "credentialProfileId": credential_id,
        },
        headers=auth_headers(admin_token),
    )
    pipeline_id = client.get(
        f"/api/ci/services/{service_id}/pipelines", headers=auth_headers(admin_token)
    ).get_json()["data"]["items"][0]["id"]
    return SimpleNamespace(id=service_id, pipeline_id=pipeline_id)


def _save(client, token, pipeline_id, stages):
    return client.put(
        f"/api/ci/pipelines/{pipeline_id}",
        json={"parameters": [], "stages": stages},
        headers=auth_headers(token),
    )


def _run(app, client, token, service_id, max_passes=60):
    from api.services.ci import engine
    from api.services.ci.runners import mock as mock_runner

    build_id = client.post(
        f"/api/ci/services/{service_id}/builds", json={}, headers=auth_headers(token)
    ).get_json()["data"]["id"]
    original = mock_runner._STAGE_SECONDS
    mock_runner._STAGE_SECONDS = 0.0
    try:
        with app.app_context():
            for _ in range(max_passes):
                engine.advance_ci_builds()
                build = db.session.get(CiBuild, build_id)
                if build.status not in ("queued", "running"):
                    break
                # A stage waiting on approval stays running; stop there.
                deploy = next((s for s in build.stages if s.stage_type == "deploy"), None)
                if deploy is not None and (deploy.deploy_state or {}).get("phase") == "waiting_approval":
                    break
    finally:
        mock_runner._STAGE_SECONDS = original
    return build_id


def _build(client, token, build_id):
    return client.get(f"/api/ci/builds/{build_id}", headers=auth_headers(token)).get_json()["data"]


def _logs(client, token, build_id, stage_id):
    data = client.get(
        f"/api/ci/builds/{build_id}/stages/{stage_id}/logs", headers=auth_headers(token)
    ).get_json()["data"]
    return "\n".join(line["content"] for line in data["lines"])


# ---------------------------------------------------------------------------
# Configuration rules
# ---------------------------------------------------------------------------

def test_a_deploy_stage_needs_a_target():
    with pytest.raises(deploy_config.DeployConfigError, match="has no target"):
        deploy_config.normalize(None, "deploy", "Deploy")
    with pytest.raises(deploy_config.DeployConfigError, match="needs a cluster"):
        deploy_config.normalize({"namespace": "a", "deploymentName": "b"}, "deploy", "Deploy")
    with pytest.raises(deploy_config.DeployConfigError, match="Invalid namespace"):
        deploy_config.normalize(_deploy(namespace="Bad_NS"), "deploy", "Deploy")


def test_only_a_deploy_stage_carries_a_target():
    assert deploy_config.normalize(None, "command", "Build") is None
    with pytest.raises(deploy_config.DeployConfigError, match="deploys nothing"):
        deploy_config.normalize(_deploy(), "command", "Build")


@pytest.mark.parametrize(
    "manifest, message",
    [
        ("", "manifest is empty"),
        ("kind: Secret\nmetadata: {name: x}\n", "only create a Deployment"),
        (MANIFEST.replace("name: brand-new\nspec:\n  replicas", "name: other\nspec:\n  replicas", 1), "is called 'other'"),
        (MANIFEST.replace("  name: brand-new\nspec:\n  replicas", "  name: brand-new\n  namespace: elsewhere\nspec:\n  replicas", 1), "names namespace 'elsewhere'"),
        ("a: [unclosed", "not valid YAML"),
    ],
)
def test_the_create_manifest_is_held_to_a_deployment_and_its_service(manifest, message):
    with pytest.raises(deploy_config.DeployConfigError, match=message):
        deploy_config.normalize(
            _deploy(deploymentName="brand-new", createIfMissing=True, manifest=manifest),
            "deploy",
            "Deploy",
        )


def test_render_sets_the_image_on_the_target_container_only():
    config = deploy_config.normalize(
        _deploy(deploymentName="brand-new", createIfMissing=True, manifest=MANIFEST), "deploy", "Deploy"
    )
    text, created = deploy_config.render_manifest(config, "registry.local/x:1")
    docs = deploy_config.parse_manifest(text)
    assert docs[0]["spec"]["template"]["spec"]["containers"][0]["image"] == "registry.local/x:1"
    assert all(doc["metadata"]["namespace"] == "payments" for doc in docs)
    assert created == [{"kind": "Deployment", "name": "brand-new"}, {"kind": "Service", "name": "brand-new"}]


def test_swap_image_changes_nothing_but_the_image():
    live = {
        "kind": "Deployment",
        "metadata": {"name": "api"},
        "spec": {
            "replicas": 4,
            "template": {"spec": {"containers": [
                {"name": "api", "image": "r/api:1", "env": [{"name": "A", "value": "1"}]},
                {"name": "sidecar", "image": "r/proxy:9"},
            ]}},
        },
        "status": {"readyReplicas": 4},
    }
    doc = deploy_config.parse_manifest(deploy_config.swap_image(live, "api", "r/api:2"))[0]
    containers = doc["spec"]["template"]["spec"]["containers"]
    assert containers[0]["image"] == "r/api:2"
    assert containers[0]["env"] == [{"name": "A", "value": "1"}]
    assert containers[1]["image"] == "r/proxy:9"
    assert doc["spec"]["replicas"] == 4 and "status" not in doc


def test_a_multi_container_deployment_needs_the_container_named():
    live = {"spec": {"template": {"spec": {"containers": [{"name": "a"}, {"name": "b"}]}}}}
    container, why = deploy_config.pick_container(live, "")
    assert container is None and "Pick the one" in why
    assert deploy_config.pick_container(live, "b")[0]["name"] == "b"


def test_the_signature_covers_what_is_authorized():
    base = deploy_config.normalize(_deploy(), "deploy", "Deploy")
    assert deploy_config.signature(base) == deploy_config.signature(dict(base, create={"replicas": 3}))
    assert deploy_config.signature(base) != deploy_config.signature(dict(base, namespace="other"))
    assert deploy_config.signature(base) != deploy_config.signature(dict(base, image="r/x:9"))


# ---------------------------------------------------------------------------
# Saving: order and authority
# ---------------------------------------------------------------------------

def test_deploy_stages_must_be_last(client, admin_token, service):
    response = _save(
        client, admin_token, service.pipeline_id,
        _stages(extra_after={"name": "Smoke test", "stageType": "command", "commands": ["curl x"]}),
    )
    assert response.status_code == 400
    assert "Smoke test" in response.get_json()["error"] and "last" in response.get_json()["error"]


def test_saving_a_target_stamps_the_person_who_saved_it(client, admin_token, service):
    response = _save(client, admin_token, service.pipeline_id, _stages())
    assert response.status_code == 200, response.get_json()
    deploy = response.get_json()["data"]["stages"][2]["deploy"]
    assert deploy["authorizedBy"]["username"] == "admin"


def test_a_stamp_in_the_request_is_ignored(app, client, admin_token, service):
    forged = _deploy()
    forged["authorizedBy"] = {"userId": 999, "username": "mallory"}
    data = _save(client, admin_token, service.pipeline_id, _stages(forged)).get_json()["data"]
    assert data["stages"][2]["deploy"]["authorizedBy"]["username"] == "admin"


def _other_user(app, *, can_deploy: bool):
    """Someone who may edit pipelines — and deploy only if asked to."""
    with app.app_context():
        user = User.query.filter_by(username="admin").first()
        other = User(username="dev", email="dev@example.com", is_active=True)
        other.password_hash = user.password_hash
        db.session.add(other)
        db.session.commit()
        return SimpleNamespace(id=other.id, username="dev", can_deploy=can_deploy)


def _save_as(app, pipeline_id, stages, who):
    """Save through the service layer as ``who`` with only the deploy
    permission varied — the route's own RBAC is exercised elsewhere."""
    from api.services.ci import pipelines

    def can_deploy(user, key):
        return who.can_deploy if key == "apps:deploy" else True

    with app.app_context():
        actor = db.session.get(User, who.id)
        with patch("api.access_engine.user_has_permission", side_effect=can_deploy), patch(
            "api.access_engine.can_access_namespace", return_value=True
        ):
            return pipelines.update_pipeline(
                db.session.get(CiPipeline, pipeline_id), {"stages": stages}, actor=actor
            )


def test_someone_who_cannot_deploy_keeps_the_stamp_when_the_target_is_unchanged(
    app, client, admin_token, service
):
    _save(client, admin_token, service.pipeline_id, _stages())
    dev = _other_user(app, can_deploy=False)
    stages = _stages()
    stages[1]["commands"] = ["make all"]  # An unrelated edit.
    data = _save_as(app, service.pipeline_id, stages, dev)
    assert data["stages"][2]["deploy"]["authorizedBy"]["username"] == "admin"


def test_someone_who_cannot_deploy_there_cannot_change_the_target(app, client, admin_token, service):
    from api.services.ci.pipelines import PipelineError

    _save(client, admin_token, service.pipeline_id, _stages())
    dev = _other_user(app, can_deploy=False)
    with pytest.raises(PipelineError, match="You cannot deploy to prod-us-east/payments"):
        _save_as(app, service.pipeline_id, _stages(_deploy(image="registry.local/payments:6.6.6")), dev)


def test_changing_the_target_re_stamps_it(app, client, admin_token, service):
    _save(client, admin_token, service.pipeline_id, _stages())
    dev = _other_user(app, can_deploy=True)
    data = _save_as(app, service.pipeline_id, _stages(_deploy(deploymentName="ledger-worker")), dev)
    assert data["stages"][2]["deploy"]["authorizedBy"]["username"] == "dev"


# ---------------------------------------------------------------------------
# Running
# ---------------------------------------------------------------------------

def test_an_existing_deployment_gets_only_its_image_changed(app, client, admin_token, service):
    _approvals(app, 0)
    _save(client, admin_token, service.pipeline_id, _stages())
    build_id = _run(app, client, admin_token, service.id)

    data = _build(client, admin_token, build_id)
    assert data["status"] == "success", data
    deploy = data["stages"][2]
    assert deploy["status"] == "success"
    assert deploy["runnerName"] is None  # No runner ever touched it.
    state = deploy["deploy"]
    assert state["outcome"] == "deployed"
    assert state["previousImage"] == "ghcr.io/mock/payments:v2.8.1"
    assert state["image"] == "registry.local/payments:2.0.0"
    assert state["created"] is False
    log = _logs(client, admin_token, build_id, deploy["id"])
    assert "Existing deployment" in log and "Rolled out" in log


def test_the_image_this_build_pushed_is_the_default(app, client, admin_token, service):
    """With no fixed image, the stage deploys the image artifact of its build."""
    from api.services.ci import deploy_stage

    _approvals(app, 0)
    _save(client, admin_token, service.pipeline_id, _stages(_deploy(image="")))
    with app.app_context():
        original = deploy_stage._resolve_image

        def with_artifact(build, stage, config, definition):
            db.session.add(CiArtifact(
                service_id=build.service_id, build_id=build.id, artifact_type="container-image",
                name="payments-api", uri="registry.local/payments-api:main-1", storage_backend="registry",
            ))
            db.session.flush()
            return original(build, stage, config, definition)

    with patch.object(deploy_stage, "_resolve_image", side_effect=with_artifact):
        build_id = _run(app, client, admin_token, service.id)
    state = _build(client, admin_token, build_id)["stages"][2]["deploy"]
    assert state["image"] == "registry.local/payments-api:main-1"


def test_a_build_that_pushed_no_image_deploys_nothing(app, client, admin_token, service):
    _approvals(app, 0)
    _save(client, admin_token, service.pipeline_id, _stages(_deploy(image="")))
    data = _build(client, admin_token, _run(app, client, admin_token, service.id))
    assert data["status"] == "failed"
    assert "pushed no image" in data["stages"][2]["error"]


def test_a_missing_deployment_is_not_created_unless_allowed(app, client, admin_token, service):
    _approvals(app, 0)
    _save(client, admin_token, service.pipeline_id, _stages(_deploy(deploymentName="brand-new")))
    data = _build(client, admin_token, _run(app, client, admin_token, service.id))
    assert data["status"] == "failed"
    assert "does not exist" in data["stages"][2]["error"]
    assert "Create it if missing" in data["stages"][2]["error"]


def test_a_missing_deployment_is_created_from_the_manifest(app, client, admin_token, service):
    _approvals(app, 0)
    _save(
        client, admin_token, service.pipeline_id,
        _stages(_deploy(deploymentName="brand-new", createIfMissing=True, manifest=MANIFEST)),
    )
    data = _build(client, admin_token, _run(app, client, admin_token, service.id))
    assert data["status"] == "success", data
    state = data["stages"][2]["deploy"]
    assert state["created"] is True
    assert state["previousImage"] is None


def test_a_missing_namespace_is_never_created(app, client, admin_token, service):
    _approvals(app, 0)
    _save(client, admin_token, service.pipeline_id, _stages(_deploy(namespace="nowhere")))
    data = _build(client, admin_token, _run(app, client, admin_token, service.id))
    assert data["status"] == "failed"
    assert "Namespace 'nowhere' does not exist" in data["stages"][2]["error"]


def test_an_image_the_registry_does_not_have_is_never_deployed(app, client, admin_token, service):
    _approvals(app, 0)
    _save(client, admin_token, service.pipeline_id, _stages())
    with patch(
        "api.services.registry_service.check_image",
        return_value={"status": "not_found", "message": "payments:2.0.0 was not found in any of this cluster's registries (nexus)."},
    ):
        data = _build(client, admin_token, _run(app, client, admin_token, service.id))
    assert data["status"] == "failed"
    assert "not found" in data["stages"][2]["error"] and "Nothing was deployed" in data["stages"][2]["error"]
    assert data["stages"][2]["deploy"].get("previousImage") is None  # Stopped before reading the target.


def test_a_real_cluster_needs_a_registry_that_confirms_the_image(app):
    from api.services.ci import deploy_stage

    with app.app_context(), patch(
        "api.services.registry_service.check_image",
        return_value={"status": "no_connection", "message": "No linked registry matches"},
    ):
        verdict = deploy_stage._check_registry("registry.local/x:1", "real-cluster", True)
    assert "cannot be confirmed" in verdict and "Link the registry" in verdict


def test_an_earlier_failure_means_nothing_is_deployed(app, client, admin_token, service):
    from api.services.ci.runners import base as runner_base

    _approvals(app, 0)
    _save(client, admin_token, service.pipeline_id, _stages())
    adapter = runner_base.get_adapter("mock")
    original_poll = adapter.poll
    adapter.poll = lambda handle: runner_base.FAILED
    try:
        data = _build(client, admin_token, _run(app, client, admin_token, service.id))
    finally:
        adapter.poll = original_poll
    assert data["status"] == "failed"
    assert data["stages"][2]["status"] == "skipped"


def test_the_stage_stops_when_its_authorizer_can_no_longer_deploy(app, client, admin_token, service):
    from api.services.ci import engine
    from api.services.ci.runners import mock as mock_runner

    _approvals(app, 0)
    _save(client, admin_token, service.pipeline_id, _stages())
    build_id = client.post(
        f"/api/ci/services/{service.id}/builds", json={}, headers=auth_headers(admin_token)
    ).get_json()["data"]["id"]
    # Rights are lost after the build was started (patched only for the engine,
    # so the trigger itself still goes through the route's own checks).
    original = mock_runner._STAGE_SECONDS
    mock_runner._STAGE_SECONDS = 0.0
    try:
        with app.app_context(), patch("api.access_engine.user_has_permission", return_value=False):
            for _ in range(20):
                engine.advance_ci_builds()
    finally:
        mock_runner._STAGE_SECONDS = original
    data = _build(client, admin_token, build_id)
    assert data["stages"][2]["status"] == "failed"
    assert "authorized by admin, who can no longer deploy" in data["stages"][2]["error"]


def test_a_gated_cluster_queues_the_change_and_the_stage_waits_for_it(app, client, admin_token, service):
    from api.services.change_bundle_executor import process_due_bundles
    from api.services.ci import engine

    _approvals(app, 1)
    _save(client, admin_token, service.pipeline_id, _stages())
    build_id = _run(app, client, admin_token, service.id)

    data = _build(client, admin_token, build_id)
    assert data["status"] == "running"
    state = data["stages"][2]["deploy"]
    assert state["phase"] == "waiting_approval"
    bundle_id = state["bundleId"]

    with app.app_context():
        bundle = db.session.get(ChangeBundle, bundle_id)
        assert bundle.status == "pending_approval"
        assert bundle.items[0].action_type == "apply_yaml"
        assert bundle.requester_user_id == User.query.filter_by(username="admin").first().id
        # An approver approves; the executor applies it on its next tick.
        bundle.status = "approved"
        db.session.commit()
        process_due_bundles()
        assert db.session.get(ChangeBundle, bundle_id).status == "completed"
        # The stage is the rollout watch for this bundle — no second watcher.
        from api.models import BundleRolloutWatch

        assert BundleRolloutWatch.query.filter_by(bundle_id=bundle_id).count() == 0
        for _ in range(5):
            engine.advance_ci_builds()

    data = _build(client, admin_token, build_id)
    assert data["status"] == "success", data
    assert data["stages"][2]["deploy"]["outcome"] == "deployed"


def test_no_second_rollout_watch_even_when_the_stage_finished_first(app, client, admin_token, service):
    """The CI ticker can finish a quick rollout before the bundle executor asks
    who watches it — found running the real app, where the two are separate
    threads. The stage is still the owner once it is done."""
    from api.models import BundleRolloutWatch
    from api.services.change_bundle_executor import _start_rollout_watches
    from api.services.ci import engine

    _approvals(app, 1)
    _save(client, admin_token, service.pipeline_id, _stages())
    build_id = _run(app, client, admin_token, service.id)
    with app.app_context():
        bundle_id = db.session.get(CiBuild, build_id).stages[2].deploy_state["bundleId"]
        bundle = db.session.get(ChangeBundle, bundle_id)
        bundle.status = "completed"
        for item in bundle.items:
            item.status = "succeeded"
        db.session.commit()
        for _ in range(5):
            engine.advance_ci_builds()
        assert db.session.get(CiBuild, build_id).stages[2].status == "success"
        _start_rollout_watches(db.session.get(ChangeBundle, bundle_id))
        assert BundleRolloutWatch.query.filter_by(bundle_id=bundle_id).count() == 0


def test_a_declined_change_fails_the_stage_without_deploying(app, client, admin_token, service):
    from api.services.ci import engine

    _approvals(app, 1)
    _save(client, admin_token, service.pipeline_id, _stages())
    build_id = _run(app, client, admin_token, service.id)
    with app.app_context():
        bundle_id = db.session.get(CiBuild, build_id).stages[2].deploy_state["bundleId"]
        bundle = db.session.get(ChangeBundle, bundle_id)
        bundle.status = "rejected"
        bundle.rejection_reason = "not this week"
        db.session.commit()
        engine.advance_ci_builds()
    data = _build(client, admin_token, build_id)
    assert data["status"] == "failed"
    assert "declined: not this week" in data["stages"][2]["error"]


def test_cancelling_while_waiting_withdraws_the_change(app, client, admin_token, service):
    from api.services.ci import engine

    _approvals(app, 1)
    _save(client, admin_token, service.pipeline_id, _stages())
    build_id = _run(app, client, admin_token, service.id)
    client.post(f"/api/ci/builds/{build_id}/cancel", headers=auth_headers(admin_token))
    with app.app_context():
        engine.advance_ci_builds()
        bundle_id = db.session.get(CiBuild, build_id).stages[2].deploy_state["bundleId"]
        assert db.session.get(ChangeBundle, bundle_id).status == "rejected"
    data = _build(client, admin_token, build_id)
    assert data["status"] == "cancelled"
    assert "withdrawn" in data["stages"][2]["error"]


# ---------------------------------------------------------------------------
# Rollout and rollback against a (patched) real cluster
# ---------------------------------------------------------------------------

def _pods(image, reason, restarts=0):
    return json.dumps({"items": [{
        "metadata": {"name": "api-7d9"},
        "spec": {"containers": [{"name": "api", "image": image}]},
        "status": {"containerStatuses": [{
            "name": "api", "restartCount": restarts, "state": {"waiting": {"reason": reason, "message": "boom"}},
        }]},
    }]})


def _rolling_stage(app, state):
    stage = SimpleNamespace(
        id=1, name="Deploy", deploy_state=state, status="running", error=None,
        started_at=None, finished_at=None, duration_seconds=None,
    )
    return stage


def test_a_crash_looping_new_pod_is_a_failure(app):
    from api.services.ci import deploy_stage

    config = _deploy(deploymentName="api")
    state = {"image": "r/api:2", "containerName": "api"}
    stage = _rolling_stage(app, state)
    deployment = json.dumps({"spec": {"selector": {"matchLabels": {"app": "api"}}}})
    with app.app_context(), patch(
        "api.services.deployment_service._run_kubectl_for_cluster",
        side_effect=[deployment, _pods("r/api:2", "CrashLoopBackOff", restarts=4)],
    ), patch.object(deploy_stage, "_save", lambda s, patch_: s.deploy_state.update(patch_)):
        assert "keeps crashing" in deploy_stage._pod_problem(stage, config)


def test_an_old_pod_failing_says_nothing_about_this_rollout(app):
    from api.services.ci import deploy_stage

    config = _deploy(deploymentName="api")
    stage = _rolling_stage(app, {"image": "r/api:2", "containerName": "api"})
    deployment = json.dumps({"spec": {"selector": {"matchLabels": {"app": "api"}}}})
    with app.app_context(), patch(
        "api.services.deployment_service._run_kubectl_for_cluster",
        side_effect=[deployment, _pods("r/api:1", "CrashLoopBackOff", restarts=9)],
    ), patch.object(deploy_stage, "_save", lambda s, patch_: s.deploy_state.update(patch_)):
        assert deploy_stage._pod_problem(stage, config) == ""


def test_a_failed_rollout_puts_the_previous_image_back(app):
    from api.services.ci import deploy_stage

    calls = []
    config = _deploy(deploymentName="api")
    stage = _rolling_stage(app, {"image": "r/api:2", "previousImage": "r/api:1", "containerName": "api"})
    build = SimpleNamespace(id=1, number=7, service=None)
    finished = {}

    with app.app_context(), patch.object(deploy_stage, "should_use_real_k8s", return_value=True), patch(
        "api.services.deployment_service._run_kubectl_for_cluster",
        side_effect=lambda cluster, args: calls.append(args) or "",
    ), patch.object(deploy_stage, "_save", lambda s, patch_: s.deploy_state.update(patch_)), patch.object(
        deploy_stage, "_log", lambda s, m: None
    ), patch.object(
        deploy_stage, "_finish", lambda s, status, message, outcome: finished.update(status=status, message=message, outcome=outcome)
    ), patch.object(deploy_stage, "_authorizing_user", return_value=(None, "")):
        deploy_stage._rollback_and_fail(build, stage, config, "The new pods are failing.", status="failed")

    assert calls == [["set", "image", "deployment/api", "api=r/api:1", "-n", "payments"]]
    assert finished["outcome"] == "rolled_back"
    assert "Rolled back to r/api:1" in finished["message"]


def test_a_failed_first_rollout_removes_only_what_the_stage_created(app):
    from api.services.ci import deploy_stage

    calls = []
    config = _deploy(deploymentName="brand-new")
    stage = _rolling_stage(app, {
        "image": "r/new:1", "created": True,
        "createdResources": [{"kind": "Deployment", "name": "brand-new"}],
    })
    finished = {}
    with app.app_context(), patch.object(deploy_stage, "should_use_real_k8s", return_value=True), patch(
        "api.services.deployment_service._run_kubectl_for_cluster",
        side_effect=lambda cluster, args: calls.append(args) or "",
    ), patch.object(deploy_stage, "_save", lambda s, patch_: s.deploy_state.update(patch_)), patch.object(
        deploy_stage, "_log", lambda s, m: None
    ), patch.object(
        deploy_stage, "_finish", lambda s, status, message, outcome: finished.update(outcome=outcome, message=message)
    ), patch.object(deploy_stage, "_authorizing_user", return_value=(None, "")):
        deploy_stage._rollback_and_fail(SimpleNamespace(id=1, number=1, service=None), stage, config, "x", status="timeout")
    assert calls == [["delete", "deployment", "brand-new", "-n", "payments", "--ignore-not-found=true"]]
    assert finished["outcome"] == "rolled_back"


# ---------------------------------------------------------------------------
# The runner never sees a Deploy stage
# ---------------------------------------------------------------------------

def test_a_whole_build_runner_gets_no_container_for_a_deploy_stage(app, client, admin_token, service):
    from api.services.ci import engine

    _save(client, admin_token, service.pipeline_id, _stages())
    build_id = client.post(
        f"/api/ci/services/{service.id}/builds", json={}, headers=auth_headers(admin_token)
    ).get_json()["data"]["id"]
    adapter = SimpleNamespace(supported_stage_types=lambda: {"checkout", "command"})
    with app.app_context():
        build = db.session.get(CiBuild, build_id)
        with patch.object(engine, "_build_execution", side_effect=lambda b, s, d, **k: s.name):
            plan = engine._build_plan(build, adapter, "token")
        assert plan == ["Checkout", "Build"]


# ---------------------------------------------------------------------------
# Tickets: the stage's result is the run's result
# ---------------------------------------------------------------------------

def test_a_ticket_run_takes_the_deploy_stage_result_and_does_not_deploy_again(
    app, client, admin_token, service
):
    from api.services import deploy_automation_service as automation

    _approvals(app, 0)
    _save(client, admin_token, service.pipeline_id, _stages())
    build_id = _run(app, client, admin_token, service.id)
    with app.app_context():
        run = DeployAutomationRun(
            cluster_id="prod-us-east", namespace="payments", deployment_name="payments-api",
            image_repo="registry.local/payments", image_tag="2.0.0", ticket_number="DR-9",
            status="building", change_type="image", ci_build_id=build_id,
        )
        db.session.add(run)
        db.session.commit()
        with patch.object(automation, "_do_handoff") as handoff:
            automation._do_poll_native_build(run)
            db.session.commit()
        handoff.assert_not_called()
        assert run.status == "deployed"
        steps = {s["key"]: s for s in run.steps}
        assert "Deploy stage" in steps["deploy"]["detail"]


def test_a_deploy_stage_for_another_target_leaves_the_ticket_deploy_to_the_automation(
    app, client, admin_token, service
):
    from api.services import deploy_automation_service as automation

    _approvals(app, 0)
    _save(client, admin_token, service.pipeline_id, _stages(_deploy(deploymentName="ledger-worker")))
    build_id = _run(app, client, admin_token, service.id)
    with app.app_context():
        run = DeployAutomationRun(
            cluster_id="prod-us-east", namespace="payments", deployment_name="payments-api",
            image_repo="registry.local/payments", image_tag="2.0.0", ticket_number="DR-10",
            status="building", change_type="image", ci_build_id=build_id,
        )
        db.session.add(run)
        db.session.commit()
        automation._do_poll_native_build(run)
        assert run.status == "verifying_image"
