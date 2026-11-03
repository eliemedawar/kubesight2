"""A picked Deploy target that changes several deployments — a frontend and
a backend built by the same pipeline.

What is locked here:

* one deployment stays exactly as it was — no new keys, the same signature;
* several deployments: each row names a deployment of the stage's namespace,
  optionally its container, and the image stage it takes its image from (or a
  fixed image); one deployment twice only for two named containers;
* a build applies every changed Deployment as ONE change, each container
  getting its own stage's image, and links each deployment to the service;
* all or nothing: a missing image or a missing deployment deploys none;
* a failed rollout puts every previous image back;
* a ticket for one of the deployments takes the stage's result.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import patch

import pytest
import yaml

from api.db import db
from api.models import ChangeBundle
from api.models_ci import CiArtifact, CiServiceDeployment
from api.services.ci import deploy_config
from tests.test_ci_deploy_stage import (  # noqa: F401 — ``service`` is a fixture
    _approvals,
    _build,
    _deploy,
    _logs,
    _rolling_stage,
    _run,
    _save,
    service,
)

API = "registry.local/payments-api:main-1"
LEDGER = "registry.local/ledger:main-1"


def _rows():
    return [
        {"deploymentName": "payments-api", "from": "stage", "stage": "Build API"},
        {"deploymentName": "ledger-worker", "from": "stage", "stage": "Build Ledger"},
    ]


def _several(rows=None, **overrides):
    config = {
        "clusterId": "prod-us-east",
        "namespace": "payments",
        "deploymentSet": "several",
        "deployments": rows if rows is not None else _rows(),
    }
    config.update(overrides)
    return config


def _stages(deploy):
    return [
        {"name": "Checkout", "stageType": "checkout", "runnerLabels": ["mock"]},
        {"name": "Build API", "stageType": "container_image", "runnerLabels": ["mock"]},
        {"name": "Build Ledger", "stageType": "container_image", "runnerLabels": ["mock"]},
        {"name": "Deploy", "stageType": "deploy", "deploy": deploy},
    ]


def _pushed(images):
    """The mock runner pushes nothing: record what each image stage would
    have, tied to that stage, just before the Deploy stage looks."""
    from api.services.ci import deploy_stage

    original = deploy_stage._resolve_images

    def with_artifacts(build, config):
        for stage in build.stages:
            if stage.name in images and not CiArtifact.query.filter_by(build_stage_id=stage.id).count():
                db.session.add(CiArtifact(
                    service_id=build.service_id, build_id=build.id, build_stage_id=stage.id,
                    artifact_type="container-image", name=stage.name, uri=images[stage.name],
                    storage_backend="registry",
                ))
        db.session.flush()
        return original(build, config)

    return patch.object(deploy_stage, "_resolve_images", side_effect=with_artifacts)


# ---------------------------------------------------------------------------
# Configuration rules
# ---------------------------------------------------------------------------

def test_one_deployment_is_stored_and_signed_as_before():
    config = deploy_config.normalize(_deploy(), "deploy", "Deploy")
    assert "deploymentSet" not in config and "deployments" not in config
    assert deploy_config.deployment_rows(config) == []
    assert deploy_config.target_views(config) == [config]
    sent = dict(_deploy(), deployments=_rows())
    assert deploy_config.signature(deploy_config.normalize(sent, "deploy", "Deploy")) == deploy_config.signature(config)


def test_several_deployments_each_name_a_deployment_and_an_image_source():
    config = deploy_config.normalize(_several(), "deploy", "Deploy")
    assert config["deploymentName"] == "" and config["createIfMissing"] is False
    rows = deploy_config.deployment_rows(config)
    assert [row["deploymentName"] for row in rows] == ["payments-api", "ledger-worker"]
    assert [view["deploymentName"] for view in deploy_config.target_views(config)] == ["payments-api", "ledger-worker"]
    moved = _rows()
    moved[1]["stage"] = "Build API"
    assert deploy_config.signature(config) != deploy_config.signature(
        deploy_config.normalize(_several(moved), "deploy", "Deploy")
    )
    # One deployment twice is fine for two named containers.
    twice = [
        {"deploymentName": "web", "containerName": "app", "from": "stage", "stage": "A"},
        {"deploymentName": "web", "containerName": "nginx", "from": "fixed", "image": "nginx:1.27"},
    ]
    assert len(deploy_config.deployment_rows(deploy_config.normalize(_several(twice), "deploy", "Deploy"))) == 2


@pytest.mark.parametrize(
    "rows, message",
    [
        ([], "lists none"),
        ([{"deploymentName": "Bad_Name", "from": "stage", "stage": "A"}], "deployment name"),
        ([{"deploymentName": "web", "from": "stage", "stage": ""}], "pick the image stage"),
        ([{"deploymentName": "web", "from": "fixed", "image": ""}], "write the image"),
        (
            [{"deploymentName": "web", "from": "stage", "stage": "A"},
             {"deploymentName": "web", "from": "stage", "stage": "B"}],
            "both change 'web'",
        ),
        (
            [{"deploymentName": "web", "containerName": "app", "from": "stage", "stage": "A"},
             {"deploymentName": "web", "containerName": "app", "from": "stage", "stage": "B"}],
            "both change 'web'",
        ),
    ],
)
def test_rows_that_cannot_work_are_refused(rows, message):
    with pytest.raises(deploy_config.DeployConfigError, match=message):
        deploy_config.normalize(_several(rows), "deploy", "Deploy")


def test_saving_needs_image_stages_and_links_every_deployment(app, client, admin_token, service):
    rows = _rows()
    rows[1]["stage"] = "Checkout"
    response = _save(client, admin_token, service.pipeline_id, _stages(_several(rows)))
    assert response.status_code == 400
    assert "deployment 2: there is no \"Build an image\" stage called 'Checkout'" in response.get_json()["error"]

    response = _save(client, admin_token, service.pipeline_id, _stages(_several()))
    assert response.status_code == 200, response.get_json()
    saved = response.get_json()["data"]["stages"][3]["deploy"]
    assert saved["deploymentSet"] == "several" and saved["authorizedBy"]["username"] == "admin"
    with app.app_context():
        linked = {row.workload_name for row in CiServiceDeployment.query.filter_by(service_id=service.id)}
    assert linked == {"payments-api", "ledger-worker"}


# ---------------------------------------------------------------------------
# Running it
# ---------------------------------------------------------------------------

def test_each_deployment_gets_its_own_stage_image(app, client, admin_token, service):
    _approvals(app, 0)
    assert _save(client, admin_token, service.pipeline_id, _stages(_several())).status_code == 200
    with _pushed({"Build API": API, "Build Ledger": LEDGER}):
        build_id = _run(app, client, admin_token, service.id)

    data = _build(client, admin_token, build_id)
    assert data["status"] == "success", data
    state = data["stages"][3]["deploy"]
    assert state["outcome"] == "deployed"
    assert [(t["deploymentName"], t["previousImage"], t["image"]) for t in state["targets"]] == [
        ("payments-api", "ghcr.io/mock/payments:v2.8.1", API),
        ("ledger-worker", "ghcr.io/mock/ledger:v1.19.0", LEDGER),
    ]
    assert state["target"]["deployments"] == ["payments-api", "ledger-worker"]
    log = _logs(client, admin_token, build_id, data["stages"][3]["id"])
    assert f"ledger-worker — changing ledger-worker: ghcr.io/mock/ledger:v1.19.0 → {LEDGER}" in log


def test_a_gated_cluster_gets_one_change_bundle_for_all_of_them(app, client, admin_token, service):
    _approvals(app, 1)
    _save(client, admin_token, service.pipeline_id, _stages(_several()))
    with _pushed({"Build API": API, "Build Ledger": LEDGER}):
        build_id = _run(app, client, admin_token, service.id)
    state = _build(client, admin_token, build_id)["stages"][3]["deploy"]
    assert state["phase"] == "waiting_approval"
    with app.app_context():
        bundle = db.session.get(ChangeBundle, state["bundleId"])
        assert len(bundle.items) == 1
        payload = bundle.items[0].new_payload_json or {}
        text = json.dumps(payload)
    documents = [doc for doc in yaml.safe_load_all(_yaml_of(payload)) if doc]
    assert {doc["metadata"]["name"] for doc in documents} == {"payments-api", "ledger-worker"}
    assert API in text and LEDGER in text


def _yaml_of(payload):
    for value in (payload, payload.get("input") or {}):
        if isinstance(value, dict) and value.get("yaml"):
            return value["yaml"]
    raise AssertionError(f"no yaml in {payload}")


def test_a_stage_that_pushed_nothing_deploys_none_of_them(app, client, admin_token, service):
    _approvals(app, 0)
    _save(client, admin_token, service.pipeline_id, _stages(_several()))
    with _pushed({"Build API": API}):
        build_id = _run(app, client, admin_token, service.id)
    data = _build(client, admin_token, build_id)
    assert data["status"] == "failed"
    assert "Stage 'Build Ledger' pushed no image" in data["stages"][3]["error"]
    assert not data["stages"][3]["deploy"].get("targets")


def test_a_missing_deployment_deploys_none_of_them(app, client, admin_token, service):
    _approvals(app, 0)
    rows = _rows()
    rows[1]["deploymentName"] = "not-there"
    _save(client, admin_token, service.pipeline_id, _stages(_several(rows)))
    with _pushed({"Build API": API, "Build Ledger": LEDGER}):
        build_id = _run(app, client, admin_token, service.id)
    data = _build(client, admin_token, build_id)
    assert data["status"] == "failed"
    assert "Deployment 'not-there' does not exist" in data["stages"][3]["error"]
    assert data["stages"][3]["deploy"]["outcome"] == "failed"


def test_a_failed_rollout_puts_every_previous_image_back(app):
    from api.services.ci import deploy_stage

    config = deploy_config.normalize(_several(), "deploy", "Deploy")
    state = {
        "image": f"{API}, {LEDGER}",
        "targets": [
            {"deploymentName": "payments-api", "containerName": "api", "previousImage": "old/api:1", "image": API},
            {"deploymentName": "ledger-worker", "containerName": "ledger", "previousImage": LEDGER, "image": LEDGER},
        ],
    }
    calls, finished = [], {}
    stage = _rolling_stage(app, state)
    with app.app_context(), patch.object(deploy_stage, "should_use_real_k8s", return_value=True), patch(
        "api.services.deployment_service._run_kubectl_for_cluster",
        side_effect=lambda cluster, args, *_: calls.append(args) or "",
    ), patch.object(deploy_stage, "_save", lambda s, patch_: s.deploy_state.update(patch_)), patch.object(
        deploy_stage, "_log", lambda s, m: None
    ), patch.object(
        deploy_stage, "_finish",
        lambda s, status, message, outcome: finished.update(outcome=outcome, message=message),
    ), patch.object(deploy_stage, "_authorizing_user", return_value=(None, "")):
        deploy_stage._rollback_and_fail(
            SimpleNamespace(id=1, number=7, service=None), stage, config, "The new pods are failing.", status="failed"
        )
    # Only what changed is put back: ledger-worker already ran its image.
    assert calls == [["set", "image", "deployment/payments-api", "api=old/api:1", "-n", "payments"]]
    assert finished["outcome"] == "rolled_back" and "payments-api/api to old/api:1" in finished["message"]


def test_a_ticket_for_one_of_the_deployments_takes_the_stage_result(app):
    from api.services.deploy_automation_service import _ci_deploy_stage

    stage = SimpleNamespace(
        stage_type="deploy", position=3,
        deploy_state={"target": {"clusterId": "prod-us-east", "namespace": "payments",
                                 "deploymentName": "", "deployments": ["payments-api", "ledger-worker"]}},
    )
    build = SimpleNamespace(stages=[stage])
    run = SimpleNamespace(ci_build_id=1, cluster_id="prod-us-east", namespace="payments", deployment_name="ledger-worker")
    with app.app_context():
        assert _ci_deploy_stage(run, build) is stage


def test_a_pushed_image_is_deployed_by_the_name_clusters_pull_it_by(app):
    """A build pushes to the registry's IP:port; clusters pull by its hostname
    (the connection's "Image host(s)"). The Deploy stage deploys the hostname."""
    from api.models import RegistryConnection
    from api.services.ci import deploy_stage

    with app.app_context():
        row = RegistryConnection(
            name="nexus", base_url="https://10.4.23.182:9443",
            image_hosts="registry.areeba.com, nexus.areeba.com", username="svc", enabled=True,
        )
        db.session.add(row)
        db.session.flush()
        build = SimpleNamespace(service=SimpleNamespace(registry_connection_id=row.id))
        assert deploy_stage._as_pulled(build, "10.4.23.182:9443/areeba-specula-backend:main-7") == (
            "registry.areeba.com/areeba-specula-backend:main-7"
        )
        # Pushed somewhere else, or no image hosts: left exactly as it is.
        assert deploy_stage._as_pulled(build, "ghcr.io/team/app:1") == "ghcr.io/team/app:1"
        row.image_hosts = None
        assert deploy_stage._as_pulled(build, "10.4.23.182:9443/app:1") == "10.4.23.182:9443/app:1"
        assert deploy_stage._as_pulled(SimpleNamespace(service=None), "10.4.23.182:9443/app:1") == "10.4.23.182:9443/app:1"
        db.session.rollback()
