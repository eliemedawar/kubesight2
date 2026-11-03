"""A Helm Deploy stage for a chart that runs several images.

What is locked here:

* one image stays exactly as it was — no new keys, the same signature;
* several images: each row names an image stage of the pipeline (or a fixed
  image) and chart values of its own — no two rows writing the same value or
  one inside another;
* a build sets every image in ONE install/upgrade, each stage's own image into
  its own values;
* all or nothing: a stage that pushed no image, or an image the registry does
  not hold, deploys none of them.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from api.db import db
from api.models_ci import CiArtifact
from api.services.ci import deploy_config
from tests.test_ci_deploy_stage import (  # noqa: F401 — ``service`` is a fixture
    _approvals,
    _build,
    _logs,
    _run,
    _save,
    service,
)
from tests.test_ci_deploy_stage_helm import FakeHelm, _helm_deploy, _patched
from tests.test_helm_values_import import _slug


def _rows():
    return [
        {"from": "stage", "stage": "Build API",
         "imageValues": {"mode": "split", "repository": "api.image.repository", "tag": "api.image.tag"}},
        {"from": "stage", "stage": "Build Worker",
         "imageValues": {"mode": "split", "repository": "worker.image.repository", "tag": "worker.image.tag"}},
        {"from": "fixed", "image": "registry.local/migrate:b${KUBESIGHT_BUILD_NUMBER}",
         "imageValues": {"mode": "full", "path": "migrations.image"}},
    ]


def _several(chart_id, rows=None):
    deploy = _helm_deploy(chart_id, image="")
    deploy["helm"].update({"imageSet": "several", "images": rows if rows is not None else _rows()})
    return deploy


def _stages(deploy):
    return [
        {"name": "Checkout", "stageType": "checkout", "runnerLabels": ["mock"]},
        {"name": "Build API", "stageType": "container_image", "runnerLabels": ["mock"]},
        {"name": "Build Worker", "stageType": "container_image", "runnerLabels": ["mock"]},
        {"name": "Deploy", "stageType": "deploy", "deploy": deploy},
    ]


def _pushed(images):
    """The mock runner pushes nothing: record what each image stage would
    have, tied to that stage, just before the Deploy stage looks."""
    from api.services.ci import deploy_stage

    original = deploy_stage._resolve_images

    def with_artifacts(build, config):
        for stage in build.stages:
            if stage.name in images and not any(a.build_stage_id == stage.id for a in CiArtifact.query.all()):
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

def test_one_image_is_stored_and_signed_as_before():
    config = deploy_config.normalize(_helm_deploy("payments"), "deploy", "Deploy")
    assert "imageSet" not in config["helm"] and "images" not in config["helm"]
    assert deploy_config.helm_images(config) == []
    # Rows sent along with "one" are ignored, not stored.
    sent = _helm_deploy("payments")
    sent["helm"]["images"] = _rows()
    assert deploy_config.signature(deploy_config.normalize(sent, "deploy", "Deploy")) == deploy_config.signature(config)


def test_several_images_each_name_a_source_and_values_of_their_own():
    config = deploy_config.normalize(_several("payments"), "deploy", "Deploy")
    rows = deploy_config.helm_images(config)
    assert [row["from"] for row in rows] == ["stage", "stage", "fixed"]
    assert rows[0]["stage"] == "Build API" and rows[0]["image"] == ""
    assert rows[2]["image"] == "registry.local/migrate:b${KUBESIGHT_BUILD_NUMBER}" and rows[2]["stage"] == ""
    assert deploy_config.signature(config) != deploy_config.signature(
        deploy_config.normalize(_helm_deploy("payments"), "deploy", "Deploy")
    )
    moved = _rows()
    moved[1]["imageValues"]["tag"] = "worker.tag"
    assert deploy_config.signature(config) != deploy_config.signature(
        deploy_config.normalize(_several("payments", moved), "deploy", "Deploy")
    )


@pytest.mark.parametrize(
    "rows, message",
    [
        ([], "lists none"),
        ([{"from": "stage", "stage": "", "imageValues": {"mode": "full", "path": "a.image"}}], "pick the image stage"),
        ([{"from": "fixed", "image": "", "imageValues": {"mode": "full", "path": "a.image"}}], "write the image"),
        ([{"from": "fixed", "image": "has space:1", "imageValues": {"mode": "full", "path": "a.image"}}], "not an image reference"),
        ([{"from": "elsewhere", "imageValues": {"mode": "full", "path": "a.image"}}], "not where an image comes from"),
        (
            [{"from": "stage", "stage": "A", "imageValues": {"mode": "full", "path": "a.image"}},
             {"from": "stage", "stage": "B", "imageValues": {"mode": "full", "path": "a.image"}}],
            "same chart value",
        ),
        (
            [{"from": "stage", "stage": "A", "imageValues": {"mode": "full", "path": "api.image"}},
             {"from": "stage", "stage": "B",
              "imageValues": {"mode": "split", "repository": "api.image.repository", "tag": "b.tag"}}],
            "overlapping chart values",
        ),
    ],
)
def test_rows_that_cannot_work_are_refused(rows, message):
    with pytest.raises(deploy_config.DeployConfigError, match=message):
        deploy_config.normalize(_several("payments", rows), "deploy", "Deploy")


def test_every_image_goes_into_its_own_values_in_one_set():
    values = deploy_config.helm_values_for([
        ({"mode": "split", "repository": "api.image.repository", "tag": "api.image.tag"}, "reg.local:5000/api:7"),
        ({"mode": "split", "repository": "api.sidecar.repository", "tag": "api.sidecar.tag"}, "reg.local/side:1"),
        ({"mode": "full", "path": "worker.image"}, "reg.local/worker@sha256:abc"),
    ])
    assert values == {
        "api": {
            "image": {"repository": "reg.local:5000/api", "tag": "7"},
            "sidecar": {"repository": "reg.local/side", "tag": "1"},
        },
        "worker": {"image": "reg.local/worker@sha256:abc"},
    }


def test_saving_needs_each_stage_to_be_an_image_stage_and_takes_its_spelling(client, admin_token, service):
    chart_id = _slug(client, admin_token)
    rows = _rows()
    rows[0]["stage"] = "Checkout"
    response = _save(client, admin_token, service.pipeline_id, _stages(_several(chart_id, rows)))
    assert response.status_code == 400
    assert "no \"Build an image\" stage called 'Checkout'" in response.get_json()["error"]

    rows = _rows()
    rows[1]["stage"] = "build worker"
    response = _save(client, admin_token, service.pipeline_id, _stages(_several(chart_id, rows)))
    assert response.status_code == 200, response.get_json()
    saved = response.get_json()["data"]["stages"][3]["deploy"]["helm"]
    assert saved["imageSet"] == "several" and saved["images"][1]["stage"] == "Build Worker"


# ---------------------------------------------------------------------------
# Running it
# ---------------------------------------------------------------------------

def test_one_upgrade_sets_every_image(app, client, admin_token, service):
    _approvals(app, 0)
    chart_id = _slug(client, admin_token)
    assert _save(client, admin_token, service.pipeline_id, _stages(_several(chart_id))).status_code == 200
    fake = FakeHelm(
        releases={"payments": {"name": "payments", "revision": "4", "status": "deployed"}},
        current={"replicaCount": 3, "api": {"image": {"repository": "registry.local/api", "tag": "old"}}},
    )
    installed, runner = _patched(fake)
    with installed, runner, _pushed({
        "Build API": "registry.local/api:main-1",
        "Build Worker": "registry.local/worker:main-1",
    }):
        build_id = _run(app, client, admin_token, service.id)

    data = _build(client, admin_token, build_id)
    assert data["status"] == "success", data
    upgrades = [call for call in fake.calls if call[:2] == ["upgrade", "--install"]]
    assert len(upgrades) == 1
    values = fake.installed_values[0]
    assert values["replicaCount"] == 3
    assert values["api"]["image"] == {"repository": "registry.local/api", "tag": "main-1"}
    assert values["worker"]["image"] == {"repository": "registry.local/worker", "tag": "main-1"}
    assert values["migrations"]["image"] == f"registry.local/migrate:b{data['number']}"
    state = data["stages"][3]["deploy"]
    assert [row["image"] for row in state["images"]] == [
        "registry.local/api:main-1", "registry.local/worker:main-1", f"registry.local/migrate:b{data['number']}",
    ]
    assert state["images"][0]["into"] == "api.image.repository / api.image.tag"
    log = _logs(client, admin_token, build_id, data["stages"][3]["id"])
    assert "Image for worker.image.repository / worker.image.tag: registry.local/worker:main-1 (from 'Build Worker')" in log


def test_a_stage_that_pushed_nothing_deploys_none_of_them(app, client, admin_token, service):
    _approvals(app, 0)
    chart_id = _slug(client, admin_token)
    _save(client, admin_token, service.pipeline_id, _stages(_several(chart_id)))
    fake = FakeHelm()
    installed, runner = _patched(fake)
    with installed, runner, _pushed({"Build API": "registry.local/api:main-1"}):
        build_id = _run(app, client, admin_token, service.id)

    data = _build(client, admin_token, build_id)
    assert data["status"] == "failed"
    assert "Stage 'Build Worker' pushed no image" in data["stages"][3]["error"]
    assert not any(call[:2] == ["upgrade", "--install"] for call in fake.calls)


def test_an_image_the_registry_does_not_hold_deploys_none_of_them(app, client, admin_token, service):
    from api.services.ci import deploy_stage

    _approvals(app, 0)
    chart_id = _slug(client, admin_token)
    _save(client, admin_token, service.pipeline_id, _stages(_several(chart_id)))
    fake = FakeHelm()
    installed, runner = _patched(fake)

    def registry(image, cluster_id, real):
        return f"{image} was not found. Nothing was deployed." if "worker" in image else ""

    with installed, runner, _pushed({
        "Build API": "registry.local/api:main-1",
        "Build Worker": "registry.local/worker:main-1",
    }), patch.object(deploy_stage, "_check_registry", side_effect=registry):
        build_id = _run(app, client, admin_token, service.id)

    data = _build(client, admin_token, build_id)
    assert data["status"] == "failed"
    assert "registry.local/worker:main-1 was not found" in data["stages"][3]["error"]
    assert not any(call[:2] == ["upgrade", "--install"] for call in fake.calls)
