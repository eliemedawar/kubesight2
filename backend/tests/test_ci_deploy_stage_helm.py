"""The Deploy stage pointed at a Helm release of an Inventory chart.

What is locked here:

* the target's rules (release name, chart, where the image goes) and that the
  chart must exist when the pipeline is saved;
* the first build installs the chart with its own values — nothing has to be
  filled in, the blanked password included — plus the build's image;
* a later build upgrades the release keeping the values it runs with, and
  changes only the image values;
* a gated cluster queues the Helm change and the stage waits for it;
* the rollout is every Deployment of the release, and a failure rolls the
  release back (or uninstalls what the stage installed).

Helm itself is faked: ``run_helm`` answers from a small in-memory release table.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest
import yaml

from api.db import db
from api.models import ChangeBundle
from api.models_ci import CiBuild
from api.services.ci import deploy_config
from tests.test_ci_deploy_stage import (  # noqa: F401 — ``service`` is a fixture
    _approvals,
    _build,
    _logs,
    _rolling_stage,
    _run,
    _save,
    service,
)
from tests.test_helm_values_import import _slug

IMAGE = "registry.local/payments:2.0.0"


class FakeHelm:
    """``helm`` for one namespace: list / get values / template / upgrade --install."""

    def __init__(self, releases=None, current=None):
        self.releases = dict(releases or {})
        self.current = current or {}
        self.calls = []
        self.installed_values = []

    def __call__(self, access, args, extra_env=None):
        self.calls.append(list(args))
        if args[0] == "list":
            return json.dumps(list(self.releases.values()))
        if args[:2] == ["get", "values"]:
            return json.dumps(self.current)
        if args[0] == "template":
            return "apiVersion: v1\nkind: ConfigMap\nmetadata:\n  name: payments\n"
        if args[:2] == ["upgrade", "--install"]:
            values = yaml.safe_load(Path(args[args.index("-f") + 1]).read_text(encoding="utf-8"))
            self.installed_values.append(values)
            name = args[2]
            revision = int((self.releases.get(name) or {}).get("revision") or 0) + 1
            self.releases[name] = {"name": name, "revision": str(revision), "status": "deployed", "chart": "payments-1.0.0"}
            return "Release has been upgraded."
        return "ok"


def _patched(fake):
    return (
        patch("api.services.helm_service.is_helm_installed", return_value=True),
        patch("api.services.helm_service.run_helm", side_effect=fake),
    )


def _helm_deploy(chart_id, **overrides):
    config = {
        "target": "helm",
        "clusterId": "prod-us-east",
        "namespace": "payments",
        "releaseName": "payments",
        "image": IMAGE,
        "helm": {
            "chartTemplateId": chart_id,
            "imageValues": {"mode": "split", "repository": "image.repository", "tag": "image.tag"},
        },
    }
    config.update(overrides)
    return config


def _helm_stages(deploy):
    return [
        {"name": "Checkout", "stageType": "checkout", "runnerLabels": ["mock"]},
        {"name": "Build", "stageType": "command", "commands": ["make"], "runnerLabels": ["mock"]},
        {"name": "Deploy", "stageType": "deploy", "deploy": deploy},
    ]


# ---------------------------------------------------------------------------
# Configuration rules
# ---------------------------------------------------------------------------

def test_a_helm_target_names_a_release_a_chart_and_where_the_image_goes():
    config = deploy_config.normalize(_helm_deploy("payments"), "deploy", "Deploy")
    assert config["target"] == "helm" and config["releaseName"] == "payments"
    assert config["createIfMissing"] is False and config["linkToService"] is False
    with pytest.raises(deploy_config.DeployConfigError, match="release"):
        deploy_config.normalize(_helm_deploy("payments", releaseName=""), "deploy", "Deploy")
    with pytest.raises(deploy_config.DeployConfigError, match="not a release name"):
        deploy_config.normalize(_helm_deploy("payments", releaseName="Bad_Name"), "deploy", "Deploy")
    with pytest.raises(deploy_config.DeployConfigError, match="Inventory chart"):
        deploy_config.normalize(_helm_deploy("payments", helm={}), "deploy", "Deploy")
    with pytest.raises(deploy_config.DeployConfigError, match="different chart values"):
        deploy_config.normalize(
            _helm_deploy("payments", helm={"chartTemplateId": "payments", "imageValues": {
                "mode": "split", "repository": "image.tag", "tag": "image.tag"}}),
            "deploy", "Deploy",
        )
    with pytest.raises(deploy_config.DeployConfigError, match="not a chart value path"):
        deploy_config.normalize(
            _helm_deploy("payments", helm={"chartTemplateId": "payments", "imageValues": {
                "mode": "full", "path": "image..ref"}}),
            "deploy", "Deploy",
        )


def test_the_image_goes_into_the_named_values():
    split = deploy_config.normalize(_helm_deploy("payments"), "deploy", "Deploy")
    assert deploy_config.helm_image_values(split, "reg.local:5000/team/app:1.2") == {
        "image": {"repository": "reg.local:5000/team/app", "tag": "1.2"}
    }
    assert deploy_config.helm_image_values(split, "reg.local/app")["image"]["tag"] == "latest"
    with pytest.raises(deploy_config.DeployConfigError, match="digest"):
        deploy_config.helm_image_values(split, "reg.local/app@sha256:abc")
    full = deploy_config.normalize(
        _helm_deploy("payments", helm={"chartTemplateId": "payments", "imageValues": {"mode": "full", "path": "app.image"}}),
        "deploy", "Deploy",
    )
    assert deploy_config.helm_image_values(full, "reg.local/app@sha256:abc") == {
        "app": {"image": "reg.local/app@sha256:abc"}
    }


def test_the_signature_covers_the_release_and_the_chart():
    base = deploy_config.normalize(_helm_deploy("payments"), "deploy", "Deploy")
    assert deploy_config.signature(base) != deploy_config.signature(dict(base, releaseName="other"))
    other_chart = dict(base, helm={**base["helm"], "chartTemplateId": "other"})
    assert deploy_config.signature(base) != deploy_config.signature(other_chart)
    renamed = dict(base, helm={**base["helm"], "chartName": "Renamed"})
    assert deploy_config.signature(base) == deploy_config.signature(renamed)


def test_saving_needs_a_chart_that_exists(client, admin_token, service):
    response = _save(client, admin_token, service.pipeline_id, _helm_stages(_helm_deploy("no-such-chart")))
    assert response.status_code == 400
    assert "not found" in response.get_json()["error"]


def test_saving_writes_the_chart_name_and_stamps_the_saver(client, admin_token, service):
    chart_id = _slug(client, admin_token)
    deploy = _helm_deploy(chart_id)
    deploy["helm"]["chartName"] = "whatever the browser said"
    response = _save(client, admin_token, service.pipeline_id, _helm_stages(deploy))
    assert response.status_code == 200, response.get_json()
    saved = response.get_json()["data"]["stages"][2]["deploy"]
    assert saved["helm"]["chartName"] == "payments"
    assert saved["authorizedBy"]["username"] == "admin"


# ---------------------------------------------------------------------------
# Running it
# ---------------------------------------------------------------------------

def test_the_first_build_installs_the_chart_with_nothing_filled_in(app, client, admin_token, service):
    _approvals(app, 0)
    chart_id = _slug(client, admin_token)
    _save(client, admin_token, service.pipeline_id, _helm_stages(_helm_deploy(chart_id)))
    fake = FakeHelm()
    installed, runner = _patched(fake)
    with installed, runner:
        build_id = _run(app, client, admin_token, service.id)

    data = _build(client, admin_token, build_id)
    assert data["status"] == "success", data
    state = data["stages"][2]["deploy"]
    assert state["outcome"] == "deployed" and state["created"] is True
    install = next(call for call in fake.calls if call[:2] == ["upgrade", "--install"])
    assert install[2] == "payments" and install[install.index("--namespace") + 1] == "payments"
    # Never asked what the release runs with: there is no release yet.
    assert not any(call[:2] == ["get", "values"] for call in fake.calls)
    values = fake.installed_values[0]
    assert values["image"] == {"repository": "registry.local/payments", "tag": "2.0.0"}
    # The chart's own values; the password import blanked installs empty.
    assert values["replicaCount"] == 2 and values["database"]["password"] == ""
    log = _logs(client, admin_token, build_id, data["stages"][2]["id"])
    assert "is not installed — installing the chart" in log and "Rolled out" in log


def test_a_later_build_upgrades_keeping_the_release_values(app, client, admin_token, service):
    _approvals(app, 0)
    chart_id = _slug(client, admin_token)
    _save(client, admin_token, service.pipeline_id, _helm_stages(_helm_deploy(chart_id)))
    fake = FakeHelm(
        releases={"payments": {"name": "payments", "revision": "4", "status": "deployed"}},
        current={"replicaCount": 6, "database": {"password": "live-password"},
                 "image": {"repository": "registry.local/payments", "tag": "1.9.0"}},
    )
    installed, runner = _patched(fake)
    with installed, runner:
        build_id = _run(app, client, admin_token, service.id)

    data = _build(client, admin_token, build_id)
    assert data["status"] == "success", data
    state = data["stages"][2]["deploy"]
    assert state["created"] is False and state["helmRevision"] == 4
    values = fake.installed_values[0]
    # What the release ran with, kept — only the image moved.
    assert values["replicaCount"] == 6 and values["database"]["password"] == "live-password"
    assert values["image"] == {"repository": "registry.local/payments", "tag": "2.0.0"}


def test_a_release_mid_operation_is_left_alone(app, client, admin_token, service):
    _approvals(app, 0)
    chart_id = _slug(client, admin_token)
    _save(client, admin_token, service.pipeline_id, _helm_stages(_helm_deploy(chart_id)))
    fake = FakeHelm(releases={"payments": {"name": "payments", "revision": "4", "status": "pending-upgrade"}})
    installed, runner = _patched(fake)
    with installed, runner:
        build_id = _run(app, client, admin_token, service.id)
    data = _build(client, admin_token, build_id)
    assert data["status"] == "failed"
    assert "pending-upgrade" in data["stages"][2]["error"]
    assert not any(call[:2] == ["upgrade", "--install"] for call in fake.calls)


def test_a_gated_cluster_queues_the_helm_change_and_the_stage_waits(app, client, admin_token, service):
    from api.services.change_bundle_executor import process_due_bundles
    from api.services.ci import engine

    _approvals(app, 1)
    chart_id = _slug(client, admin_token)
    _save(client, admin_token, service.pipeline_id, _helm_stages(_helm_deploy(chart_id)))
    fake = FakeHelm()
    installed, runner = _patched(fake)
    with installed, runner:
        build_id = _run(app, client, admin_token, service.id)
        state = _build(client, admin_token, build_id)["stages"][2]["deploy"]
        assert state["phase"] == "waiting_approval"
        assert not any(call[:2] == ["upgrade", "--install"] for call in fake.calls)
        with app.app_context():
            bundle = db.session.get(ChangeBundle, state["bundleId"])
            assert bundle.items[0].action_type == "helm_install"
            bundle.status = "approved"
            db.session.commit()
            process_due_bundles()
            assert db.session.get(ChangeBundle, state["bundleId"]).status == "completed"
            for _ in range(5):
                engine.advance_ci_builds()

    data = _build(client, admin_token, build_id)
    assert data["status"] == "success", data
    assert data["stages"][2]["deploy"]["outcome"] == "deployed"
    with app.app_context():
        queued = db.session.get(ChangeBundle, state["bundleId"]).items[0].new_payload_json or {}
    # The queued change carries the image, never a value it would read later.
    helm = (queued.get("input") or queued).get("helm") or {}
    assert "2.0.0" in helm.get("valuesYaml", "") and helm.get("isInstall") is True


# ---------------------------------------------------------------------------
# Rollout and rollback
# ---------------------------------------------------------------------------

def _release_deployment(name, *, ready):
    return {
        "metadata": {
            "name": name, "generation": 2,
            "annotations": {"meta.helm.sh/release-name": "payments", "meta.helm.sh/release-namespace": "payments"},
        },
        "spec": {"replicas": 1, "selector": {"matchLabels": {"app": name}}},
        "status": {"observedGeneration": 2, "replicas": 1, "updatedReplicas": 1,
                   "availableReplicas": 1 if ready else 0},
    }


def test_the_rollout_is_every_deployment_of_the_release(app):
    from api.services.ci import deploy_stage

    config = deploy_config.normalize(_helm_deploy("payments"), "deploy", "Deploy")
    other = {"metadata": {"name": "unrelated", "annotations": {}}, "spec": {}, "status": {}}
    listed = json.dumps({"items": [_release_deployment("api", ready=True), other,
                                   _release_deployment("worker", ready=False)]})
    with app.app_context(), patch(
        "api.services.deployment_service._run_kubectl_for_cluster", return_value=listed
    ):
        documents = deploy_stage._release_deployments(config)
    assert [doc["metadata"]["name"] for doc in documents] == ["api", "worker"]
    assert deploy_stage._deployment_waiting(documents[0]) == ""
    assert deploy_stage._deployment_waiting(documents[1]) == "0/1 new pods available"


def _rollback(app, config, state):
    from api.services.ci import deploy_stage

    calls, finished = [], {}
    stage = _rolling_stage(app, state)
    with app.app_context(), patch.object(deploy_stage, "should_use_real_k8s", return_value=True), patch(
        "api.services.helm_service.run_helm", side_effect=lambda access, args, *_: calls.append(args) or ""
    ), patch("api.services.helm_service._resolve_access", return_value=object()), patch.object(
        deploy_stage, "_save", lambda s, patch_: s.deploy_state.update(patch_)
    ), patch.object(deploy_stage, "_log", lambda s, m: None), patch.object(
        deploy_stage, "_finish",
        lambda s, status, message, outcome: finished.update(outcome=outcome, message=message),
    ), patch.object(deploy_stage, "_authorizing_user", return_value=(None, "")):
        deploy_stage._rollback_and_fail(
            SimpleNamespace(id=1, number=7, service=None), stage, config, "The new pods are failing.", status="failed"
        )
    return calls, finished


def test_a_failed_upgrade_rolls_the_release_back_to_its_previous_revision(app):
    config = deploy_config.normalize(_helm_deploy("payments"), "deploy", "Deploy")
    calls, finished = _rollback(app, config, {"image": IMAGE, "created": False, "helmRevision": 4})
    assert calls == [["rollback", "payments", "4", "--namespace", "payments"]]
    assert finished["outcome"] == "rolled_back" and "revision 4" in finished["message"]


def test_a_failed_first_install_uninstalls_the_release(app):
    config = deploy_config.normalize(_helm_deploy("payments"), "deploy", "Deploy")
    calls, finished = _rollback(app, config, {"image": IMAGE, "created": True, "helmRevision": None})
    assert calls == [["uninstall", "payments", "--namespace", "payments"]]
    assert finished["outcome"] == "rolled_back"


def test_the_build_snapshot_keeps_the_helm_target(app, client, admin_token, service):
    _approvals(app, 0)
    chart_id = _slug(client, admin_token)
    _save(client, admin_token, service.pipeline_id, _helm_stages(_helm_deploy(chart_id)))
    fake = FakeHelm()
    installed, runner = _patched(fake)
    with installed, runner:
        build_id = _run(app, client, admin_token, service.id)
    with app.app_context():
        build = db.session.get(CiBuild, build_id)
        target = build.stages[2].deploy_state["target"]
    assert target["releaseName"] == "payments" and target["chartName"] == "payments"
