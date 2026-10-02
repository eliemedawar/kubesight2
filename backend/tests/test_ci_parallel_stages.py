"""Parallel stage groups: consecutive stages sharing a parallelGroup run together.

What is locked here:

* the save rules (consecutive, runner stages that do not build the tree, two to
  eight members with at least two turned on, one fail-fast switch per group)
  and the canonical form a save stores;
* the engine: members start in the same pass and are all "running" at once,
  the build waits for the slowest, a failed member's siblings run to completion
  (Jenkins' ``parallel`` default) and the stages after the group are skipped,
  ``continueOnFailure`` members, fail-fast, run-condition-skipped members,
  timeouts that cancel one member only, cancellation mid-group, resuming a
  group whose start was interrupted, and the same failure rules when the
  runner can only run members one at a time;
* agents: every member is its own task, claimed as the agent's capacity allows,
  and a member waiting its turn is not on the clock;
* the Kubernetes Job: members as native sidecars (``restartPolicy: Always``),
  the barrier after them, ordering against post actions and the collector, the
  sidecar request accounting — and the member and barrier scripts run for real
  under ``sh``;
* ``poll`` over a pod whose sidecars are still running;
* the version gate: 1.28 runs a group one stage at a time and says why, 1.30
  lays it out side by side, and ``CI_PARALLEL_STAGES`` overrides it.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
from datetime import datetime, timedelta, timezone

import pytest

from api.db import db
from api.models_application_intelligence import BitbucketCredentialProfile
from api.models_ci import CiAgentTask, CiBuild, CiPipelineStage, CiRunner
from api.secret_encryption import encrypt_secret
from api.services.ci import parallel_groups
from api.services.ci import resources as ci_resources
from api.services.ci.runners import base as runner_base
from api.services.ci.runners import kubernetes as k8s
from api.services.ci.runners.base import (
    CANCELLED,
    FAILED,
    QUEUED,
    RUNNING,
    SKIPPED,
    SUCCEEDED,
    TIMEOUT,
    RunnerHandle,
    StageExecution,
)
from tests.conftest import auth_headers

SH = shutil.which("sh")
needs_sh = pytest.mark.skipif(not SH, reason="needs a POSIX sh")


# ---------------------------------------------------------------------------
# Fixtures and helpers
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _clean_runner_state(monkeypatch):
    monkeypatch.delenv("CI_PARALLEL_STAGES", raising=False)
    k8s.reset_cluster_version_cache()
    yield
    k8s.set_kubectl_runner(None)
    k8s.reset_cluster_version_cache()


@pytest.fixture()
def service(app, client, admin_token):
    """A service with source connected; returns (service_id, pipeline_id)."""
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
        json={"name": "Checkout Api", "applicationType": "java"},
        headers=auth_headers(admin_token),
    ).get_json()["data"]["id"]
    client.put(
        f"/api/ci/services/{service_id}/source",
        json={
            "repositoryUrl": "https://bitbucket.org/areeba/checkout-api",
            "defaultBranch": "main",
            "credentialProfileId": credential_id,
        },
        headers=auth_headers(admin_token),
    )
    pipeline_id = client.get(
        f"/api/ci/services/{service_id}/pipelines", headers=auth_headers(admin_token)
    ).get_json()["data"]["items"][0]["id"]
    return service_id, pipeline_id


def _command(name, *, group=None, labels=("mock",), **extra):
    stage = {"name": name, "stageType": "command", "commands": [f"run {name}"], "runnerLabels": list(labels)}
    if group:
        stage["parallelGroup"] = group
    stage.update(extra)
    return stage


def _checks(labels=("mock",), fail_fast=False, **member_extra):
    """Checkout -> [Lint, Test, Sonar] -> Package."""
    members = []
    for name in ("Lint", "Test", "Sonar"):
        members.append(
            _command(
                name,
                group="Checks",
                labels=labels,
                parallelFailFast=fail_fast,
                **member_extra.get(name, {}),
            )
        )
    return (
        [{"name": "Checkout", "stageType": "checkout", "runnerLabels": list(labels)}]
        + members
        + [_command("Package", labels=labels)]
    )


def _save(client, token, pipeline_id, stages, parameters=None):
    return client.put(
        f"/api/ci/pipelines/{pipeline_id}",
        json={"parameters": parameters or [], "stages": stages},
        headers=auth_headers(token),
    )


def _trigger(client, token, service_id, variables=None):
    response = client.post(
        f"/api/ci/services/{service_id}/builds",
        json={"variables": variables} if variables else {},
        headers=auth_headers(token),
    )
    assert response.status_code in (200, 201), response.get_json()
    return response.get_json()["data"]["id"]


class Scripted:
    """A per-stage runner whose every poll answer the test decides.

    ``script[name]`` is a list of statuses, one consumed per poll (the last one
    repeats). Records which stages started in which pass, what was cancelled,
    and — through ``waiting`` — can say a member has not been claimed yet.
    """

    runner_type = "mock"

    def __init__(self):
        self.script = {}
        self.runs = {}
        self.started = []
        self.cancelled = []
        self.pass_no = 0
        self.waiting = set()

    def supported_stage_types(self):
        return {"checkout", "command", "scan", "container_image"}

    def can_run(self, requirements):
        return True

    def start(self, execution):
        ref = f"scripted-{execution.stage_id}"
        self.runs[ref] = execution.stage_name
        self.started.append((self.pass_no, execution.stage_name))
        return RunnerHandle(runner_id=0, external_ref=ref)

    def poll(self, handle):
        name = self.runs.get(handle.external_ref)
        if name is None:
            return FAILED
        queue = self.script.get(name) or [SUCCEEDED]
        status = queue.pop(0) if len(queue) > 1 else queue[0]
        self.script[name] = queue
        return status

    def drain_logs(self, handle, after_seq):
        return iter(())

    def collect_artifacts(self, handle):
        return []

    def cancel(self, handle):
        self.cancelled.append(self.runs.get(handle.external_ref))

    def cleanup(self, handle):
        return None


class ScriptedWithClaims(Scripted):
    """The same, with a pull runner's ``running_since``."""

    def running_since(self, handle):
        name = self.runs.get(handle.external_ref)
        return None if name in self.waiting else datetime.now(timezone.utc) - timedelta(seconds=1)


@pytest.fixture()
def scripted(monkeypatch):
    adapter = Scripted()
    monkeypatch.setitem(runner_base._ADAPTERS, "mock", adapter)
    return adapter


def _pass(app, adapter=None):
    from api.services.ci import engine

    if adapter is not None:
        adapter.pass_no += 1
    with app.app_context():
        engine.advance_ci_builds()


def _statuses(app, build_id):
    with app.app_context():
        db.session.expire_all()
        build = db.session.get(CiBuild, build_id)
        return build.status, {stage.name: stage.status for stage in build.stages}


def _drive(app, adapter, build_id, passes=30):
    for _ in range(passes):
        _pass(app, adapter)
        status, _ = _statuses(app, build_id)
        if status not in ("queued", "running"):
            break
    return _statuses(app, build_id)


# ---------------------------------------------------------------------------
# Save rules
# ---------------------------------------------------------------------------


def test_a_group_saves_with_one_spelling_and_one_fail_fast_switch(client, admin_token, service):
    _, pipeline_id = service
    stages = _checks()
    stages[1]["parallelGroup"] = "  Checks "
    stages[2]["parallelGroup"] = "checks"
    stages[3]["parallelGroup"] = "CHECKS"
    stages[2]["parallelFailFast"] = True
    saved = _save(client, admin_token, pipeline_id, stages)
    assert saved.status_code == 200, saved.get_json()
    by_name = {stage["name"]: stage for stage in saved.get_json()["data"]["stages"]}
    assert {by_name[n]["parallelGroup"] for n in ("Lint", "Test", "Sonar")} == {"Checks"}
    # Any member asking for fail-fast turns it on for the group.
    assert all(by_name[n]["parallelFailFast"] for n in ("Lint", "Test", "Sonar"))
    assert by_name["Checkout"]["parallelGroup"] is None
    assert by_name["Package"]["parallelFailFast"] is False


@pytest.mark.parametrize(
    "stages, message",
    [
        (
            [_command("Lint", group="g"), _command("Build"), _command("Test", group="g")],
            "must sit next to each other",
        ),
        (
            [
                {"name": "Checkout", "stageType": "checkout", "parallelGroup": "g"},
                _command("Lint", group="g"),
            ],
            "a checkout cannot run in parallel",
        ),
        ([_command("Lint", group="g")], "needs at least two stages"),
        (
            [_command("Lint", group="g"), _command("Test", group="g", enabled=False)],
            "only 'Lint' is turned on",
        ),
        ([_command(f"S{i}", group="g") for i in range(9)], "at most 8"),
        ([_command("Lint", group="x" * 65), _command("Test", group="x" * 65)], "at most 64"),
    ],
)
def test_save_refuses_a_group_that_breaks_a_rule(client, admin_token, service, stages, message):
    _, pipeline_id = service
    refused = _save(client, admin_token, pipeline_id, stages)
    assert refused.status_code == 400
    assert message in refused.get_json()["error"]


def test_server_stages_never_join_a_group():
    stages = [
        {"name": "Build", "stage_type": "command", "parallel_group": "g", "enabled": True},
        {"name": "Ship", "stage_type": "deploy", "parallel_group": "g", "enabled": True},
    ]
    with pytest.raises(parallel_groups.GroupError, match="run on the KubeSight server"):
        parallel_groups.validate(stages)


def test_disabled_members_may_stay_in_a_group_that_still_has_two():
    stages = [
        {"name": "Lint", "stage_type": "command", "parallel_group": "g", "enabled": True},
        {"name": "Test", "stage_type": "scan", "parallel_group": "G", "enabled": False},
        {"name": "Image", "stage_type": "container_image", "parallel_group": "g", "enabled": True},
    ]
    parallel_groups.validate(stages)
    assert [stage["parallel_group"] for stage in stages] == ["g", "g", "g"]


def test_a_build_snapshot_left_with_one_runnable_member_has_no_group():
    """Disabled stages are not in a build, so the snapshot may hold a lone one."""
    definitions = [
        {"stageType": "checkout"},
        {"stageType": "command", "parallelGroup": "g"},
        {"stageType": "command"},
    ]
    assert parallel_groups.runs(definitions) == []
    assert parallel_groups.group_positions(definitions, 1) == [1]


# ---------------------------------------------------------------------------
# Engine — side by side on a per-stage runner
# ---------------------------------------------------------------------------


def test_members_start_together_and_the_build_waits_for_the_slowest(
    app, client, admin_token, service, scripted
):
    service_id, pipeline_id = service
    assert _save(client, admin_token, pipeline_id, _checks()).status_code == 200
    scripted.script = {"Lint": [SUCCEEDED], "Test": [RUNNING, RUNNING, RUNNING, SUCCEEDED], "Sonar": [RUNNING, SUCCEEDED]}
    build_id = _trigger(client, admin_token, service_id)

    _pass(app, scripted)  # dispatch: Checkout starts
    _pass(app, scripted)  # Checkout succeeds -> the whole group starts in this pass
    _, stages = _statuses(app, build_id)
    assert [stages[name] for name in ("Lint", "Test", "Sonar")] == ["running"] * 3
    started = dict((name, number) for number, name in scripted.started)
    assert started["Lint"] == started["Test"] == started["Sonar"]

    _pass(app, scripted)
    _, stages = _statuses(app, build_id)
    # Lint is done, Test and Sonar still run, and Package waits for all of them.
    assert stages["Lint"] == "success"
    assert stages["Test"] == "running"
    assert stages["Package"] == "pending"

    status, stages = _drive(app, scripted, build_id)
    assert status == "success"
    assert set(stages.values()) == {"success"}
    # Package started only after the slowest member finished.
    order = [name for _, name in scripted.started]
    assert order.index("Package") > order.index("Test")


def test_a_failed_member_lets_its_siblings_finish_then_fails_the_group(
    app, client, admin_token, service, scripted
):
    service_id, pipeline_id = service
    _save(client, admin_token, pipeline_id, _checks())
    scripted.script = {"Lint": [FAILED], "Test": [RUNNING, RUNNING, SUCCEEDED], "Sonar": [SUCCEEDED]}
    build_id = _trigger(client, admin_token, service_id)

    status, stages = _drive(app, scripted, build_id)
    assert status == "failed"
    assert stages["Lint"] == "failed"
    # Jenkins `parallel`: siblings run to completion, nobody kills them.
    assert stages["Test"] == "success"
    assert stages["Sonar"] == "success"
    assert stages["Package"] == "skipped"
    assert scripted.cancelled == []
    assert "Package" not in [name for _, name in scripted.started]


def test_a_continue_on_failure_member_does_not_stop_the_pipeline(
    app, client, admin_token, service, scripted
):
    service_id, pipeline_id = service
    _save(client, admin_token, pipeline_id, _checks(Lint={"continueOnFailure": True}))
    scripted.script = {"Lint": [FAILED]}
    build_id = _trigger(client, admin_token, service_id)

    status, stages = _drive(app, scripted, build_id)
    assert stages["Lint"] == "failed"
    assert stages["Package"] == "success"
    # Still a red build: continue-on-failure is "keep going", not "pretend".
    assert status == "failed"


def test_fail_fast_stops_the_running_siblings(app, client, admin_token, service, scripted):
    service_id, pipeline_id = service
    _save(client, admin_token, pipeline_id, _checks(fail_fast=True))
    scripted.script = {"Lint": [FAILED], "Test": [RUNNING], "Sonar": [RUNNING]}
    build_id = _trigger(client, admin_token, service_id)

    status, stages = _drive(app, scripted, build_id)
    assert stages["Lint"] == "failed"
    assert stages["Test"] == "cancelled"
    assert stages["Sonar"] == "cancelled"
    assert stages["Package"] == "skipped"
    assert sorted(scripted.cancelled) == ["Sonar", "Test"]
    # The build FAILED — a sibling did. Nobody cancelled it.
    assert status == "failed"
    with app.app_context():
        build = db.session.get(CiBuild, build_id)
        test = next(stage for stage in build.stages if stage.name == "Test")
        assert "'Lint' failed" in test.error


def test_a_member_skipped_by_its_run_condition_leaves_the_rest_running_together(
    app, client, admin_token, service, scripted
):
    service_id, pipeline_id = service
    stages = _checks(Sonar={"runCondition": {"variable": "RUN_SONAR", "operator": "equals", "value": "true"}})
    parameters = [{"name": "RUN_SONAR", "type": "boolean", "default": "false"}]
    assert _save(client, admin_token, pipeline_id, stages, parameters).status_code == 200
    scripted.script = {"Lint": [RUNNING, SUCCEEDED], "Test": [RUNNING, SUCCEEDED]}
    build_id = _trigger(client, admin_token, service_id, {"RUN_SONAR": "false"})

    _pass(app, scripted)
    _pass(app, scripted)
    _, stages = _statuses(app, build_id)
    assert stages["Sonar"] == "skipped"
    assert stages["Lint"] == stages["Test"] == "running"

    status, stages = _drive(app, scripted, build_id)
    assert status == "success"
    assert stages["Package"] == "success"
    assert "Sonar" not in [name for _, name in scripted.started]


def test_a_member_over_its_timeout_is_stopped_alone(app, client, admin_token, service, scripted):
    service_id, pipeline_id = service
    _save(client, admin_token, pipeline_id, _checks(Test={"timeoutSeconds": 60}))
    scripted.script = {"Lint": [RUNNING, SUCCEEDED], "Test": [RUNNING], "Sonar": [RUNNING, RUNNING, SUCCEEDED]}
    build_id = _trigger(client, admin_token, service_id)
    _pass(app, scripted)
    _pass(app, scripted)
    with app.app_context():
        build = db.session.get(CiBuild, build_id)
        test = next(stage for stage in build.stages if stage.name == "Test")
        test.started_at = datetime.now(timezone.utc) - timedelta(seconds=120)
        db.session.commit()

    status, stages = _drive(app, scripted, build_id)
    assert stages["Test"] == "timeout"
    assert scripted.cancelled == ["Test"]
    assert stages["Lint"] == stages["Sonar"] == "success"
    assert stages["Package"] == "skipped"
    assert status == "timeout"


def test_a_member_still_waiting_for_its_agent_is_not_on_the_clock(
    app, client, admin_token, service, monkeypatch
):
    adapter = ScriptedWithClaims()
    monkeypatch.setitem(runner_base._ADAPTERS, "mock", adapter)
    service_id, pipeline_id = service
    _save(client, admin_token, pipeline_id, _checks(Test={"timeoutSeconds": 60}))
    adapter.script = {"Test": [QUEUED, QUEUED, SUCCEEDED]}
    adapter.waiting = {"Test"}
    build_id = _trigger(client, admin_token, service_id)
    _pass(app, adapter)
    _pass(app, adapter)
    with app.app_context():
        build = db.session.get(CiBuild, build_id)
        test = next(stage for stage in build.stages if stage.name == "Test")
        test.started_at = datetime.now(timezone.utc) - timedelta(seconds=600)
        db.session.commit()
    _pass(app, adapter)
    _, stages = _statuses(app, build_id)
    assert stages["Test"] == "running"  # queued behind its siblings, not timed out

    adapter.waiting = set()  # claimed: its clock starts now, not ten minutes ago
    status, stages = _drive(app, adapter, build_id)
    assert stages["Test"] == "success"
    assert status == "success"
    with app.app_context():
        build = db.session.get(CiBuild, build_id)
        test = next(stage for stage in build.stages if stage.name == "Test")
        assert (test.duration_seconds or 0) < 300


def test_cancelling_mid_group_cancels_every_running_member(app, client, admin_token, service, scripted):
    service_id, pipeline_id = service
    _save(client, admin_token, pipeline_id, _checks())
    scripted.script = {"Lint": [RUNNING], "Test": [RUNNING], "Sonar": [SUCCEEDED]}
    build_id = _trigger(client, admin_token, service_id)
    _pass(app, scripted)
    _pass(app, scripted)
    _pass(app, scripted)  # Sonar done; Lint and Test still running

    response = client.post(f"/api/ci/builds/{build_id}/cancel", headers=auth_headers(admin_token))
    assert response.status_code == 200
    status, stages = _drive(app, scripted, build_id)
    assert status == "cancelled"
    assert stages["Lint"] == stages["Test"] == "cancelled"
    assert stages["Sonar"] == "success"
    assert stages["Package"] == "skipped"
    assert sorted(scripted.cancelled) == ["Lint", "Test"]


def test_a_group_whose_start_was_interrupted_resumes(app, client, admin_token, service, scripted):
    """A restart between two member starts leaves one pending: the next pass
    starts it, from the persisted rows alone."""
    service_id, pipeline_id = service
    _save(client, admin_token, pipeline_id, _checks())
    scripted.script = {"Lint": [RUNNING, SUCCEEDED], "Test": [RUNNING, SUCCEEDED], "Sonar": [RUNNING, SUCCEEDED]}
    build_id = _trigger(client, admin_token, service_id)
    _pass(app, scripted)
    _pass(app, scripted)
    with app.app_context():
        build = db.session.get(CiBuild, build_id)
        sonar = next(stage for stage in build.stages if stage.name == "Sonar")
        sonar.status, sonar.external_ref, sonar.started_at = "pending", None, None
        db.session.commit()
        db.session.expire_all()

    _pass(app, scripted)
    _, stages = _statuses(app, build_id)
    assert stages["Sonar"] == "running"
    status, stages = _drive(app, scripted, build_id)
    assert status == "success"
    assert set(stages.values()) == {"success"}


def test_the_build_records_how_it_ran_its_groups(app, client, admin_token, service, scripted):
    service_id, pipeline_id = service
    _save(client, admin_token, pipeline_id, _checks(fail_fast=True))
    build_id = _trigger(client, admin_token, service_id)
    _drive(app, scripted, build_id)
    data = client.get(f"/api/ci/builds/{build_id}", headers=auth_headers(admin_token)).get_json()["data"]
    assert data["parallel"]["mode"] == "parallel"
    by_name = {stage["name"]: stage for stage in data["stages"]}
    assert by_name["Lint"]["parallelGroup"] == "Checks"
    assert by_name["Lint"]["parallelFailFast"] is True
    assert by_name["Package"]["parallelGroup"] is None


def test_the_stage_matrix_marks_groups_and_counts_a_group_once(app, client, admin_token, service, scripted):
    from api.services.ci import stage_matrix

    service_id, pipeline_id = service
    _save(client, admin_token, pipeline_id, _checks())
    for _ in range(3):
        _drive(app, scripted, _trigger(client, admin_token, service_id))
    with app.app_context():
        for build in CiBuild.query.filter_by(service_id=service_id).all():
            for stage in build.stages:
                stage.duration_seconds = {"Checkout": 10, "Lint": 20, "Test": 60, "Sonar": 30, "Package": 30}[stage.name]
        db.session.commit()
        matrix = stage_matrix.stage_matrix(service_id)
    columns = {column["name"]: column for column in matrix["columns"]}
    assert columns["Test"]["parallelGroup"] == "Checks"
    assert columns["Checkout"]["parallelGroup"] is None
    # A typical build is 10 + max(20, 60, 30) + 30 = 100s, not 150s.
    assert columns["Test"]["shareOfBuild"] == pytest.approx(0.6)


# ---------------------------------------------------------------------------
# Engine — one member at a time (the runner cannot, or the installation says no)
# ---------------------------------------------------------------------------


def test_switched_off_a_group_runs_in_sequence_with_the_same_failure_rules(
    app, client, admin_token, service, scripted, monkeypatch
):
    monkeypatch.setenv("CI_PARALLEL_STAGES", "off")
    service_id, pipeline_id = service
    _save(client, admin_token, pipeline_id, _checks())
    scripted.script = {"Lint": [RUNNING, FAILED], "Test": [RUNNING, SUCCEEDED], "Sonar": [SUCCEEDED]}
    build_id = _trigger(client, admin_token, service_id)

    seen_together = False
    for _ in range(30):
        _pass(app, scripted)
        status, stages = _statuses(app, build_id)
        if sum(1 for value in stages.values() if value == "running") > 1:
            seen_together = True
        if status not in ("queued", "running"):
            break
    assert not seen_together
    assert stages["Lint"] == "failed"
    # The siblings still ran: a group keeps its rules when it cannot run side by side.
    assert stages["Test"] == stages["Sonar"] == "success"
    assert stages["Package"] == "skipped"
    assert status == "failed"

    data = client.get(f"/api/ci/builds/{build_id}", headers=auth_headers(admin_token)).get_json()["data"]
    assert data["parallel"] == {"mode": "sequential", "reason": parallel_groups.OFF_REASON}
    for stage in data["stages"]:
        if stage["name"] not in ("Lint", "Test", "Sonar"):
            continue
        lines = client.get(
            f"/api/ci/builds/{build_id}/stages/{stage['id']}/logs", headers=auth_headers(admin_token)
        ).get_json()["data"]["lines"]
        assert any("ran one stage at a time" in line["content"] for line in lines), stage["name"]


def test_in_sequence_a_fail_fast_group_skips_the_members_after_a_failure(
    app, client, admin_token, service, scripted, monkeypatch
):
    monkeypatch.setenv("CI_PARALLEL_STAGES", "off")
    service_id, pipeline_id = service
    _save(client, admin_token, pipeline_id, _checks(fail_fast=True))
    scripted.script = {"Lint": [FAILED]}
    build_id = _trigger(client, admin_token, service_id)
    status, stages = _drive(app, scripted, build_id)
    assert stages["Lint"] == "failed"
    assert stages["Test"] == stages["Sonar"] == stages["Package"] == "skipped"
    assert status == "failed"


# ---------------------------------------------------------------------------
# Agents
# ---------------------------------------------------------------------------


def test_on_an_agent_every_member_is_its_own_task_claimed_as_capacity_allows(
    app, client, admin_token, service
):
    from api.services.ci import agents as agents_service

    service_id, pipeline_id = service
    stages = _checks(labels=())
    for stage in stages:
        stage["runnerType"] = "agent_linux"
    assert _save(client, admin_token, pipeline_id, stages).status_code == 200
    with app.app_context():
        for runner in CiRunner.query.all():
            runner.enabled = False
        db.session.commit()
        runner, _ = agents_service.create_agent(
            {"name": "linux-parallel", "runnerType": "agent_linux", "maxConcurrent": 1}
        )
        agents_service.heartbeat(runner, {"capabilities": ["linux"]})
        runner_id = runner.id

    build_id = _trigger(client, admin_token, service_id)

    def claim_and_finish(expected_names, exit_code=0):
        with app.app_context():
            runner = db.session.get(CiRunner, runner_id)
            task = agents_service.claim_next(runner)
            assert task is not None and task["stageName"] in expected_names
            # One slot: nothing else is handed out until this one reports.
            assert agents_service.claim_next(runner) is None
            row = db.session.get(CiAgentTask, task["taskId"])
            agents_service.report_result(row, {"exitCode": exit_code})
            return task["stageName"]

    _pass(app)
    claim_and_finish({"Checkout"})
    _pass(app)
    with app.app_context():
        queued = CiAgentTask.query.filter_by(build_id=build_id, state="queued").count()
        build = db.session.get(CiBuild, build_id)
        running = sorted(stage.name for stage in build.stages if stage.status == "running")
    # All three handed over at once; the agent takes them as it can.
    assert queued == 3
    assert running == ["Lint", "Sonar", "Test"]

    done = set()
    for _ in range(3):
        done.add(claim_and_finish({"Lint", "Test", "Sonar"} - done))
        _pass(app)
    _pass(app)
    claim_and_finish({"Package"})
    _pass(app)
    status, stages = _statuses(app, build_id)
    assert status == "success", stages


def test_an_agent_with_room_for_two_claims_two_members_at_once(app, client, admin_token, service):
    from api.services.ci import agents as agents_service

    service_id, pipeline_id = service
    stages = [_command("Lint", group="g", labels=()), _command("Test", group="g", labels=())]
    for stage in stages:
        stage["runnerType"] = "agent_linux"
    _save(client, admin_token, pipeline_id, stages)
    with app.app_context():
        for runner in CiRunner.query.all():
            runner.enabled = False
        db.session.commit()
        runner, _ = agents_service.create_agent(
            {"name": "linux-wide", "runnerType": "agent_linux", "maxConcurrent": 2}
        )
        agents_service.heartbeat(runner, {"capabilities": ["linux"]})
        runner_id = runner.id
    _trigger(client, admin_token, service_id)
    _pass(app)
    with app.app_context():
        runner = db.session.get(CiRunner, runner_id)
        first = agents_service.claim_next(runner)
        second = agents_service.claim_next(runner)
        assert {first["stageName"], second["stageName"]} == {"Lint", "Test"}


# ---------------------------------------------------------------------------
# Kubernetes — the Job
# ---------------------------------------------------------------------------


def _execution(position, stage_type="command", *, group=None, mode="parallel", **kw):
    return StageExecution(
        build_id=7,
        build_number=3,
        stage_id=100 + position,
        service_slug="checkout-api",
        stage_name=kw.get("name", f"Stage {position}"),
        stage_type=stage_type,
        image=kw.get("image"),
        working_directory=None,
        commands=kw.get("commands", [f"echo stage {position}"]),
        env={},
        secrets=kw.get("secrets", {}),
        timeout_seconds=kw.get("timeout", 600),
        continue_on_failure=kw.get("cof", False),
        position=position,
        repository_url="https://bitbucket.org/areeba/checkout-api.git",
        branch="main",
        callback_url="http://backend:5000/api/ci/worker",
        callback_token="the-callback-token",
        parallel_group=group,
        parallel_fail_fast=kw.get("fail_fast", False),
        parallel_mode=mode,
        parallel_reason=kw.get("reason", ""),
    )


def _job(*executions, post_plan=None):
    first = executions[0]
    first.plan = list(executions)
    if post_plan is not None:
        first.post_plan = post_plan
    _, _, job = k8s.build_job_resources(first)
    return job


def _group_plan(mode="parallel", **kw):
    return (
        _execution(0, "checkout", mode=mode, secrets={"KUBESIGHT_GIT_TOKEN": "t"}),
        _execution(1, group="checks", mode=mode, name="Lint", **kw),
        _execution(2, group="checks", mode=mode, name="Test", cof=True, **kw),
        _execution(3, group="checks", mode=mode, name="Sonar", **kw),
        _execution(4, mode=mode, name="Package"),
    )


def test_members_are_native_sidecars_and_a_barrier_waits_after_them():
    job = _job(*_group_plan())
    spec = job["spec"]["template"]["spec"]
    names = [container["name"] for container in spec["initContainers"]]
    assert names == ["stage-0", "stage-1", "stage-2", "stage-3", "barrier-1", "stage-4"]
    policies = {c["name"]: c.get("restartPolicy") for c in spec["initContainers"]}
    assert policies["stage-1"] == policies["stage-2"] == policies["stage-3"] == "Always"
    assert policies["stage-0"] is None and policies["barrier-1"] is None and policies["stage-4"] is None
    # The pod itself never restarts anything else.
    assert spec["restartPolicy"] == "Never"
    assert [c["name"] for c in spec["containers"]] == ["collector"]

    annotations = job["spec"]["template"]["metadata"]["annotations"]
    layout = json.loads(annotations[k8s._GROUPS_ANNOTATION])
    assert layout == {"mode": "parallel", "groups": [[1, 2, 3]]}


def test_a_member_carries_its_commands_in_its_environment_and_writes_a_done_file():
    spec = _job(*_group_plan())["spec"]["template"]["spec"]
    lint = next(c for c in spec["initContainers"] if c["name"] == "stage-1")
    script = lint["command"][2]
    env = {entry["name"]: entry.get("value") for entry in lint["env"]}
    assert "echo stage 1" in env["KUBESIGHT_STAGE_SCRIPT"]
    assert "echo stage 1" not in script
    assert 'done-$KS_POS' in script
    # A member records a failure for its GROUP; only the barrier writes the
    # fail flag, so a sibling that has not started yet is not "after a failure".
    assert "group-1-failed" in script
    assert ': > "$KS_STATE/failed"' not in script
    assert "sleep 3600" in script  # parks instead of exiting
    barrier = next(c for c in spec["initContainers"] if c["name"] == "barrier-1")
    assert barrier["image"] == k8s.worker_image()
    assert '1:600:0 2:600:1 3:600:0' in barrier["command"][2]


def test_the_barrier_and_members_come_before_post_actions_and_the_collector():
    from api.services.ci import post_actions

    post = _execution(post_actions.POSITION_BASE, "post", name="Cleanup", commands=["rm -rf build"])
    post.post_when = "always"
    post.parallel_mode = ""
    spec = _job(*_group_plan(), post_plan=[post])["spec"]["template"]["spec"]
    names = [c["name"] for c in spec["initContainers"]]
    assert names.index("barrier-1") < names.index("stage-4") < names.index(k8s.post_container_name(post.position))
    assert spec["containers"][-1]["name"] == "collector"


def test_sidecar_requests_are_counted_for_the_whole_pod():
    totals = ci_resources.effective_pod_requests(
        [
            ({"requests": {"cpu": "100m", "memory": "256Mi"}}, False),
            ({"requests": {"cpu": "500m", "memory": "1Gi"}}, True),
            ({"requests": {"cpu": "500m", "memory": "1Gi"}}, True),
            ({"requests": {"cpu": "100m", "memory": "256Mi"}}, False),
        ],
        [{"requests": {"cpu": "100m", "memory": "256Mi"}}],
    )
    # The init after the group runs beside both sidecars: 1.1 CPU; the
    # collector too. Not the 0.5 a sequential pod would need.
    assert totals["cpu"] == pytest.approx(1.1)
    assert totals["memory"] == pytest.approx(2 * 1024 ** 3 + 256 * 1024 ** 2)
    job = _job(*_group_plan())
    assert job["spec"]["template"]["metadata"]["annotations"][k8s._REQUESTS_ANNOTATION] == "0.4 CPU, 1.0Gi memory"
    barrier = next(c for c in job["spec"]["template"]["spec"]["initContainers"] if c["name"] == "barrier-1")
    assert "requests 0.4 CPU, 1.0Gi memory" in barrier["command"][2]


def test_in_sequence_members_stay_ordinary_init_containers_and_say_why():
    reason = "the build cluster runs Kubernetes v1.28.4"
    spec = _job(*_group_plan(mode="sequential", reason=reason))["spec"]["template"]["spec"]
    names = [c["name"] for c in spec["initContainers"]]
    assert names == ["stage-0", "stage-1", "stage-2", "stage-3", "barrier-1", "stage-4"]
    assert all("restartPolicy" not in c for c in spec["initContainers"])
    lint = next(c for c in spec["initContainers"] if c["name"] == "stage-1")
    assert "ran one stage at a time" in lint["command"][2]
    assert "v1.28.4" in lint["command"][2]
    assert "sleep 3600" not in lint["command"][2]


def test_a_group_left_with_one_runnable_member_is_an_ordinary_stage():
    """The plan leaves out a member skipped by its run condition."""
    spec = _job(
        _execution(0, "checkout", secrets={"KUBESIGHT_GIT_TOKEN": "t"}),
        _execution(1, group="checks", name="Lint"),
        _execution(3, name="Package"),
    )["spec"]["template"]["spec"]
    names = [c["name"] for c in spec["initContainers"]]
    assert names == ["stage-0", "stage-1", "stage-3"]
    assert "restartPolicy" not in spec["initContainers"][1]
    assert k8s._FAIL_FLAG in spec["initContainers"][1]["command"][2]


# ---------------------------------------------------------------------------
# Kubernetes — the scripts, run for real
# ---------------------------------------------------------------------------


def _write(path, text):
    path.write_text(text, encoding="utf-8", newline="\n")
    return path


def _run_member(tmp_path, execution, group, body, *, parallel=True, timeout=60):
    state = tmp_path / "state"
    script = k8s.member_stage_script(
        execution, group=group, parallel=parallel, state_dir=state.as_posix(), with_cache_prep=False
    )
    path = _write(tmp_path / f"member-{execution.position}.sh", script)
    env = dict(os.environ, KUBESIGHT_KEEPALIVE="0", KUBESIGHT_STAGE_SCRIPT=body)
    return subprocess.run([SH, path.as_posix()], env=env, capture_output=True, text=True, timeout=timeout)


def _run_barrier(tmp_path, group, *, grace=0, timeout=60):
    state = tmp_path / "state"
    script = k8s.barrier_script(group, state_dir=state.as_posix(), poll_seconds=1, grace_seconds=grace)
    path = _write(tmp_path / "barrier.sh", script)
    return subprocess.run([SH, path.as_posix()], capture_output=True, text=True, timeout=timeout)


def _members(*specs):
    return [
        _execution(position, group="checks", name=f"M{position}", timeout=spec.get("timeout", 600), cof=spec.get("cof", False), fail_fast=spec.get("fail_fast", False))
        for position, spec in specs
    ]


def _read(path):
    return path.read_text(encoding="utf-8").strip()


@needs_sh
def test_a_member_that_succeeds_writes_its_done_file(tmp_path):
    group = _members((1, {}), (2, {}))
    result = _run_member(tmp_path, group[0], group, "echo hello from lint")
    assert result.returncode == 0
    assert "hello from lint" in result.stdout
    assert "[kubesight-exit] 0" in result.stdout
    assert _read(tmp_path / "state" / "done-1") == "0"
    assert not (tmp_path / "state" / "group-1-failed").exists()


@needs_sh
def test_a_failing_member_fails_its_group_not_the_fail_flag(tmp_path):
    group = _members((1, {}), (2, {}))
    result = _run_member(tmp_path, group[0], group, "echo broken; exit 3")
    assert result.returncode == 0  # never fails the pod
    assert "[kubesight-exit] 3" in result.stdout
    assert _read(tmp_path / "state" / "done-1") == "3"
    assert (tmp_path / "state" / "group-1-failed").exists()
    assert not (tmp_path / "state" / "failed").exists()


@needs_sh
def test_a_continue_on_failure_member_leaves_only_the_soft_flag(tmp_path):
    group = _members((1, {}), (2, {"cof": True}))
    _run_member(tmp_path, group[1], group, "exit 1")
    assert (tmp_path / "state" / "failed-continued").exists()
    assert not (tmp_path / "state" / "group-1-failed").exists()


@needs_sh
def test_a_member_after_an_earlier_failure_skips_without_running(tmp_path):
    group = _members((1, {}), (2, {}))
    (tmp_path / "state").mkdir()
    (tmp_path / "state" / "failed").write_text("")
    marker = tmp_path / "ran"
    result = _run_member(tmp_path, group[0], group, f"touch {marker.as_posix()}")
    assert "[kubesight-skip]" in result.stdout
    assert _read(tmp_path / "state" / "done-1") == "skip"
    assert not marker.exists()


@needs_sh
def test_a_member_stops_itself_at_its_timeout(tmp_path):
    group = _members((1, {"timeout": 1}), (2, {}))
    result = _run_member(tmp_path, group[0], group, "sleep 20; echo never")
    if "timeout" not in (result.stdout + result.stderr) and not shutil.which("timeout"):
        pytest.skip("no timeout utility here; the barrier is the timeout then")
    assert "[kubesight-timeout]" in result.stdout
    assert "never" not in result.stdout
    assert _read(tmp_path / "state" / "done-1") == "timeout"
    assert (tmp_path / "state" / "group-1-failed").exists()


@needs_sh
def test_a_restarted_member_reports_again_and_never_reruns(tmp_path):
    group = _members((1, {}), (2, {}))
    state = tmp_path / "state"
    state.mkdir()
    _write(state / "done-1", "0\n")
    marker = tmp_path / "ran"
    result = _run_member(tmp_path, group[0], group, f"touch {marker.as_posix()}")
    assert "[kubesight-exit] 0" in result.stdout
    assert not marker.exists()


@needs_sh
def test_a_member_killed_mid_run_is_failed_not_rerun(tmp_path):
    group = _members((1, {}), (2, {}))
    state = tmp_path / "state"
    state.mkdir()
    (state / "started-1").write_text("")
    marker = tmp_path / "ran"
    result = _run_member(tmp_path, group[0], group, f"touch {marker.as_posix()}")
    assert "[kubesight-exit] 137" in result.stdout
    assert _read(state / "done-1") == "137"
    assert not marker.exists()


@needs_sh
def test_in_sequence_fail_fast_skips_the_members_after_a_failure(tmp_path):
    group = _members((1, {"fail_fast": True}), (2, {"fail_fast": True}))
    state = tmp_path / "state"
    state.mkdir()
    (state / "group-1-failed").write_text("")
    result = _run_member(tmp_path, group[1], group, "echo should not run", parallel=False)
    assert "[kubesight-skip]" in result.stdout
    assert "should not run" not in result.stdout


@needs_sh
def test_the_barrier_fails_the_group_for_a_failed_member(tmp_path):
    group = _members((1, {}), (2, {}), (3, {"cof": True}))
    state = tmp_path / "state"
    state.mkdir()
    _write(state / "done-1", "0\n")
    _write(state / "done-2", "1\n")
    _write(state / "done-3", "2\n")
    result = _run_barrier(tmp_path, group)
    assert result.returncode == 0
    assert "[kubesight-member] 1 ok" in result.stdout
    assert "[kubesight-member] 2 failed" in result.stdout
    assert "[kubesight-member] 3 failed" in result.stdout
    assert (state / "failed").exists()
    assert (state / "failed-continued").exists()


@needs_sh
def test_the_barrier_lets_a_continue_on_failure_failure_through(tmp_path):
    group = _members((1, {}), (2, {"cof": True}))
    state = tmp_path / "state"
    state.mkdir()
    _write(state / "done-1", "0\n")
    _write(state / "done-2", "1\n")
    _run_barrier(tmp_path, group)
    assert not (state / "failed").exists()
    assert (state / "failed-continued").exists()


@needs_sh
def test_the_barrier_waits_for_a_member_and_honours_its_timeout(tmp_path):
    group = _members((1, {}), (2, {"timeout": 1}))
    state = tmp_path / "state"
    state.mkdir()
    _write(state / "done-1", "0\n")
    started = time.monotonic()
    result = _run_barrier(tmp_path, group, grace=0)
    assert time.monotonic() - started >= 1
    assert "[kubesight-member] 2 timeout" in result.stdout
    assert (state / "timeout-2").exists()
    assert (state / "failed").exists()


@needs_sh
def test_the_barrier_sees_a_member_finish_while_it_waits(tmp_path):
    group = _members((1, {}), (2, {}))
    state = tmp_path / "state"
    state.mkdir()
    _write(state / "done-1", "0\n")
    path = _write(
        tmp_path / "barrier.sh",
        k8s.barrier_script(group, state_dir=state.as_posix(), poll_seconds=1, grace_seconds=0),
    )
    process = subprocess.Popen([SH, path.as_posix()], stdout=subprocess.PIPE, text=True)
    time.sleep(1.5)
    assert process.poll() is None  # still waiting for member 2
    _write(state / "done-2", "0\n")
    out, _ = process.communicate(timeout=60)
    assert "[kubesight-member] 2 ok" in out
    assert not (state / "failed").exists()


@needs_sh
def test_a_fail_fast_barrier_stops_waiting_at_the_first_failure(tmp_path):
    group = _members((1, {"fail_fast": True}), (2, {"fail_fast": True}))
    state = tmp_path / "state"
    state.mkdir()
    _write(state / "done-1", "1\n")
    started = time.monotonic()
    result = _run_barrier(tmp_path, group, grace=600)
    assert time.monotonic() - started < 30
    assert "[kubesight-member] 1 failed" in result.stdout
    assert "[kubesight-member] 2 cancelled" in result.stdout
    assert (state / "failed").exists()


# ---------------------------------------------------------------------------
# Kubernetes — poll over a pod whose sidecars still run
# ---------------------------------------------------------------------------


def _group_pod(statuses, *, layout=None, collector=None):
    names = [status["name"] for status in statuses]
    return {
        "metadata": {
            "name": "ci-b7-pod",
            "creationTimestamp": "2026-10-01T10:00:00Z",
            "annotations": {
                k8s._GROUPS_ANNOTATION: json.dumps(layout or {"mode": "parallel", "groups": [[1, 2]]}),
            },
        },
        "spec": {"initContainers": [{"name": name} for name in names]},
        "status": {
            "initContainerStatuses": statuses,
            "containerStatuses": [collector] if collector else [],
        },
    }


class FakePodCluster:
    def __init__(self, pod, logs, job_status=None):
        self.pod, self.logs, self.job_status = pod, logs, job_status or {"active": 1}
        self.calls = []

    def __call__(self, args, input_text=None):
        self.calls.append(args[:2])
        if args[:2] == ["get", "job"]:
            return 0, json.dumps({"status": self.job_status}), ""
        if args[:2] == ["get", "pods"]:
            return 0, json.dumps({"items": [self.pod]}), ""
        if args[0] == "logs":
            container = args[args.index("-c") + 1]
            if container not in self.logs:
                return 1, "", "waiting"
            return 0, self.logs[container], ""
        return 0, "", ""


def _ref(container):
    return RunnerHandle(runner_id=1, external_ref=f"ci-b7-checkout-api#{container}")


RUNNING_STATE = {"running": {"startedAt": "2026-10-01T10:01:00Z"}}


def test_a_running_sidecar_is_read_off_its_marker_not_its_state():
    adapter = k8s.KubernetesJobRunnerAdapter()
    pod = _group_pod(
        [
            {"name": "stage-0", "state": {"terminated": {"exitCode": 0}}},
            {"name": "stage-1", "state": RUNNING_STATE},
            {"name": "stage-2", "state": RUNNING_STATE},
            {"name": "barrier-1", "state": RUNNING_STATE},
            {"name": "stage-3", "state": {"waiting": {}}},
        ]
    )
    fake = FakePodCluster(pod, {"stage-1": "lint ok\n[kubesight-exit] 0\n", "stage-2": "testing...\n"})
    k8s.set_kubectl_runner(fake)
    # Finished (and parked in sleep): success, though its container still runs.
    assert adapter.poll(_ref("stage-1")) == SUCCEEDED
    # No marker yet: still running.
    assert adapter.poll(_ref("stage-2")) == RUNNING
    assert adapter.poll(_ref("stage-3")) == QUEUED


def test_the_barriers_verdict_decides_a_member_it_stopped_waiting_for():
    adapter = k8s.KubernetesJobRunnerAdapter()
    pod = _group_pod(
        [
            {"name": "stage-1", "state": RUNNING_STATE},
            {"name": "stage-2", "state": RUNNING_STATE},
            {"name": "barrier-1", "state": {"terminated": {"exitCode": 0}}},
            {"name": "stage-3", "state": RUNNING_STATE},
        ]
    )
    fake = FakePodCluster(
        pod,
        {
            "stage-1": "[kubesight-exit] 1\n",
            "stage-2": "still going\n",
            "barrier-1": "[kubesight-member] 1 failed\n[kubesight-member] 2 timeout\n",
        },
    )
    k8s.set_kubectl_runner(fake)
    assert adapter.poll(_ref("stage-1")) == FAILED
    assert adapter.poll(_ref("stage-2")) == TIMEOUT
    fake.logs["barrier-1"] = "[kubesight-member] 1 failed\n[kubesight-member] 2 cancelled\n"
    assert adapter.poll(_ref("stage-2")) == CANCELLED
    fake.logs["stage-2"] = "[kubesight-skip]\n"
    fake.logs["barrier-1"] = "[kubesight-member] 2 skip\n"
    assert adapter.poll(_ref("stage-2")) == SKIPPED


def test_a_group_that_ends_the_build_waits_for_the_collector():
    adapter = k8s.KubernetesJobRunnerAdapter()
    statuses = [
        {"name": "stage-0", "state": {"terminated": {"exitCode": 0}}},
        {"name": "stage-1", "state": RUNNING_STATE},
        {"name": "stage-2", "state": RUNNING_STATE},
        {"name": "barrier-1", "state": {"terminated": {"exitCode": 0}}},
        {"name": "post-0", "state": RUNNING_STATE},
    ]
    logs = {
        "stage-1": "[kubesight-exit] 0\n",
        "stage-2": "[kubesight-exit] 0\n",
        "barrier-1": "[kubesight-member] 1 ok\n[kubesight-member] 2 ok\n",
    }
    fake = FakePodCluster(_group_pod(statuses), logs, {"active": 1})
    k8s.set_kubectl_runner(fake)
    # Post actions and the collector are still to come: not success yet.
    assert adapter.poll(_ref("stage-1")) == RUNNING
    assert adapter.poll(_ref("stage-2")) == RUNNING
    fake.job_status = {"succeeded": 1}
    assert adapter.poll(_ref("stage-1")) == SUCCEEDED
    assert adapter.poll(_ref("stage-2")) == SUCCEEDED


def test_poll_many_reads_the_pod_once_for_the_whole_group():
    adapter = k8s.KubernetesJobRunnerAdapter()
    pod = _group_pod(
        [
            {"name": "stage-1", "state": RUNNING_STATE},
            {"name": "stage-2", "state": RUNNING_STATE},
            {"name": "barrier-1", "state": {"terminated": {"exitCode": 0}}},
            {"name": "stage-3", "state": {"running": {}}},
        ],
        layout={"mode": "parallel", "groups": [[1, 2]]},
    )
    fake = FakePodCluster(pod, {"barrier-1": "[kubesight-member] 1 ok\n[kubesight-member] 2 failed\n"})
    k8s.set_kubectl_runner(fake)
    result = adapter.poll_many([_ref("stage-1"), _ref("stage-2")])
    assert fake.calls.count(["get", "pods"]) == 1
    assert fake.calls.count(["get", "job"]) == 1
    assert fake.calls.count(["logs", "job/ci-b7-checkout-api"]) == 1  # the barrier, once
    assert result == {
        _ref("stage-1").external_ref: SUCCEEDED,
        _ref("stage-2").external_ref: FAILED,
    }


def test_in_sequence_members_are_polled_like_any_stage():
    adapter = k8s.KubernetesJobRunnerAdapter()
    pod = _group_pod(
        [
            {"name": "stage-1", "state": {"terminated": {"exitCode": 0}}},
            {"name": "stage-2", "state": {"running": {}}},
            {"name": "barrier-1", "state": {"waiting": {}}},
            {"name": "stage-3", "state": {"waiting": {}}},
        ],
        layout={"mode": "sequential", "groups": [[1, 2]]},
    )
    k8s.set_kubectl_runner(FakePodCluster(pod, {"stage-1": "[kubesight-exit] 2\n"}))
    assert adapter.poll(_ref("stage-1")) == FAILED
    assert adapter.poll(_ref("stage-2")) == RUNNING


def test_barriers_and_post_containers_are_not_the_last_stage():
    adapter = k8s.KubernetesJobRunnerAdapter()
    pod = _group_pod(
        [
            {"name": "stage-0", "state": {}},
            {"name": "stage-1", "state": {}},
            {"name": "barrier-1", "state": {}},
            {"name": "post-0", "state": {}},
        ],
        layout={"mode": "parallel", "groups": []},
    )
    assert adapter._is_last_stage(pod, "stage-1")
    assert not adapter._is_last_stage(pod, "stage-0")


# ---------------------------------------------------------------------------
# Version gate
# ---------------------------------------------------------------------------


def _version_cluster(minor, major="1", calls=None):
    def runner(args, input_text=None):
        if calls is not None:
            calls.append(args[0])
        if args[0] == "version":
            return 0, json.dumps(
                {"serverVersion": {"major": major, "minor": minor, "gitVersion": f"v{major}.{minor.rstrip('+')}.4"}}
            ), ""
        return 0, "", ""

    return runner


def test_kubernetes_1_28_runs_groups_one_stage_at_a_time_and_says_why():
    k8s.set_kubectl_runner(_version_cluster("28"))
    supported, reason = k8s.KubernetesJobRunnerAdapter().parallel_capability()
    assert supported is False
    assert "v1.28.4" in reason and "1.29" in reason


@pytest.mark.parametrize("minor", ["29", "30", "33+"])
def test_kubernetes_1_29_and_newer_run_groups_side_by_side(minor):
    k8s.set_kubectl_runner(_version_cluster(minor))
    supported, reason = k8s.KubernetesJobRunnerAdapter().parallel_capability()
    assert supported is True
    assert "native sidecar" in reason


def test_the_version_is_read_once_and_cached():
    calls = []
    k8s.set_kubectl_runner(_version_cluster("30", calls=calls))
    adapter = k8s.KubernetesJobRunnerAdapter()
    adapter.parallel_capability()
    adapter.parallel_capability()
    assert calls.count("version") == 1


def test_an_unreadable_version_runs_groups_in_sequence():
    k8s.set_kubectl_runner(lambda args, input_text=None: (1, "", "Unable to connect to the server"))
    supported, reason = k8s.KubernetesJobRunnerAdapter().parallel_capability()
    assert supported is False
    assert "Unable to connect" in reason and "CI_PARALLEL_STAGES=on" in reason


def test_the_environment_overrides_the_version_check(monkeypatch):
    calls = []
    k8s.set_kubectl_runner(_version_cluster("27", calls=calls))
    monkeypatch.setenv("CI_PARALLEL_STAGES", "on")
    assert k8s.KubernetesJobRunnerAdapter().parallel_capability()[0] is True
    assert calls == []  # forced on: the cluster is not asked
    monkeypatch.setenv("CI_PARALLEL_STAGES", "off")
    k8s.set_kubectl_runner(_version_cluster("31", calls=calls))
    supported, reason = k8s.KubernetesJobRunnerAdapter().parallel_capability()
    assert supported is False and "CI_PARALLEL_STAGES=off" in reason


class K8sCluster:
    """A cluster for the engine: applies record the Job, the pod mirrors its
    containers, and the test moves containers and their logs along."""

    def __init__(self, minor):
        self.minor = minor
        self.job = None
        self.job_status = {"active": 1}
        self.pod = None
        self.logs = {}

    def __call__(self, args, input_text=None):
        if args[0] == "version":
            return 0, json.dumps({"serverVersion": {"major": "1", "minor": self.minor, "gitVersion": f"v1.{self.minor}.2"}}), ""
        if args[0] == "apply":
            items = json.loads(input_text)["items"]
            self.job = next(item for item in items if item["kind"] == "Job")
            template = self.job["spec"]["template"]
            self.pod = {
                "metadata": {"creationTimestamp": "2026-10-01T10:00:00Z", "annotations": template["metadata"]["annotations"]},
                "spec": {"initContainers": template["spec"]["initContainers"]},
                "status": {
                    "initContainerStatuses": [
                        {"name": c["name"], "state": {"waiting": {}}} for c in template["spec"]["initContainers"]
                    ]
                },
            }
            return 0, "", ""
        if args[:2] == ["get", "job"]:
            if "jsonpath" in " ".join(args):
                return 0, "uid-1", ""
            return (0, json.dumps({"status": self.job_status}), "") if self.job else (1, "", "NotFound")
        if args[:2] == ["get", "pods"]:
            return 0, json.dumps({"items": [self.pod] if self.pod else []}), ""
        if args[0] == "logs":
            container = args[args.index("-c") + 1]
            return 0, self.logs.get(container, "working\n"), ""
        return 0, "", ""

    def set(self, name, state, log=None):
        for status in self.pod["status"]["initContainerStatuses"]:
            if status["name"] == name:
                status["state"] = state
        if log is not None:
            self.logs[name] = log


def _kubernetes_only(app):
    with app.app_context():
        for runner in CiRunner.query.all():
            runner.enabled = runner.runner_type == "kubernetes"
        db.session.commit()


def test_on_kubernetes_1_30_the_engine_starts_the_whole_group_in_one_pass(app, client, admin_token, service):
    service_id, pipeline_id = service
    stages = _checks(labels=("linux",))
    stages = stages[:1] + stages[1:3] + stages[4:]  # Checkout, Lint, Test, Package
    assert _save(client, admin_token, pipeline_id, stages).status_code == 200
    _kubernetes_only(app)
    cluster = K8sCluster("30")
    k8s.set_kubectl_runner(cluster)
    build_id = _trigger(client, admin_token, service_id)

    _pass(app)
    spec = cluster.job["spec"]["template"]["spec"]
    assert [c["name"] for c in spec["initContainers"]] == ["stage-0", "stage-1", "stage-2", "barrier-1", "stage-3"]
    assert spec["initContainers"][1]["restartPolicy"] == "Always"

    cluster.set("stage-0", {"terminated": {"exitCode": 0}}, "[kubesight-exit] 0\n")
    cluster.set("stage-1", RUNNING_STATE)
    cluster.set("stage-2", RUNNING_STATE)
    _pass(app)
    _, statuses = _statuses(app, build_id)
    assert statuses["Lint"] == statuses["Test"] == "running"

    cluster.set("stage-1", RUNNING_STATE, "lint\n[kubesight-exit] 0\n")
    _pass(app)
    _, statuses = _statuses(app, build_id)
    assert statuses["Lint"] == "success" and statuses["Test"] == "running"

    cluster.set("stage-2", RUNNING_STATE, "tests\n[kubesight-exit] 0\n")
    cluster.set("barrier-1", {"terminated": {"exitCode": 0}}, "[kubesight-member] 1 ok\n[kubesight-member] 2 ok\n")
    _pass(app)
    _, statuses = _statuses(app, build_id)
    assert statuses["Test"] == "success"
    assert statuses["Package"] == "running"

    cluster.set("stage-3", {"terminated": {"exitCode": 0}}, "[kubesight-exit] 0\n")
    cluster.job_status = {"succeeded": 1}
    _pass(app)
    status, statuses = _statuses(app, build_id)
    assert status == "success", statuses
    data = client.get(f"/api/ci/builds/{build_id}", headers=auth_headers(admin_token)).get_json()["data"]
    assert data["parallel"]["mode"] == "parallel"


def test_on_kubernetes_1_28_the_group_runs_in_line_and_the_build_says_why(app, client, admin_token, service):
    service_id, pipeline_id = service
    stages = _checks(labels=("linux",))
    stages = stages[:1] + stages[1:3] + stages[4:]
    _save(client, admin_token, pipeline_id, stages)
    _kubernetes_only(app)
    cluster = K8sCluster("28")
    k8s.set_kubectl_runner(cluster)
    build_id = _trigger(client, admin_token, service_id)

    _pass(app)
    spec = cluster.job["spec"]["template"]["spec"]
    assert all("restartPolicy" not in c for c in spec["initContainers"])
    lint = next(c for c in spec["initContainers"] if c["name"] == "stage-1")
    assert "ran one stage at a time" in lint["command"][2]
    assert "v1.28.2" in lint["command"][2]

    cluster.set("stage-0", {"terminated": {"exitCode": 0}}, "[kubesight-exit] 0\n")
    cluster.set("stage-1", {"running": {}})
    _pass(app)
    _, statuses = _statuses(app, build_id)
    # One at a time: Test has not started while Lint runs.
    assert statuses["Lint"] == "running" and statuses["Test"] == "pending"
    data = client.get(f"/api/ci/builds/{build_id}", headers=auth_headers(admin_token)).get_json()["data"]
    assert data["parallel"]["mode"] == "sequential"
    assert "1.28" in data["parallel"]["reason"]


# ---------------------------------------------------------------------------
# The editor's health check
# ---------------------------------------------------------------------------


def test_the_lint_warns_when_a_group_would_run_in_sequence_on_this_cluster(app, client, admin_token):
    _kubernetes_only(app)
    k8s.set_kubectl_runner(_version_cluster("28"))
    stages = [_command("Lint", group="checks", labels=()), _command("Test", group="checks", labels=())]
    for index, stage in enumerate(stages, start=1):
        stage["position"] = index
    result = client.post(
        "/api/ci/pipelines/lint", json={"stages": stages}, headers=auth_headers(admin_token)
    ).get_json()["data"]
    assert result["parallel"]["kubernetes"]["supported"] is False
    finding = next(item for item in result["findings"] if item["code"] == "parallel_runs_in_sequence")
    assert finding["stagePosition"] == 1
    assert finding["level"] == "warning"


def test_the_lint_says_nothing_about_groups_on_a_cluster_that_runs_them(app, client, admin_token):
    _kubernetes_only(app)
    k8s.set_kubectl_runner(_version_cluster("31"))
    stages = [_command("Lint", group="checks", labels=()), _command("Test", group="checks", labels=())]
    result = client.post(
        "/api/ci/pipelines/lint", json={"stages": stages}, headers=auth_headers(admin_token)
    ).get_json()["data"]
    assert result["parallel"]["kubernetes"]["supported"] is True
    assert not [item for item in result["findings"] if item["code"].startswith("parallel_runs")]


def test_the_lint_never_asks_the_cluster_about_a_pipeline_without_groups(app, client, admin_token):
    _kubernetes_only(app)
    calls = []
    k8s.set_kubectl_runner(_version_cluster("31", calls=calls))
    result = client.post(
        "/api/ci/pipelines/lint", json={"stages": [_command("Build", labels=())]}, headers=auth_headers(admin_token)
    ).get_json()["data"]
    assert result["parallel"] is None
    assert calls == []
