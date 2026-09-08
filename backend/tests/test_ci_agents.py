"""External agent runners: enrolment, the claim protocol, and its guarantees."""

import pytest

from api.db import db
from api.models_ci import CiAgentTask, CiBuild, CiBuildStage, CiRunner, CiService
from api.services.ci import agents as agents_service
from api.services.ci.agents import AgentError
from api.services.ci.runners.agent import ExternalAgentRunnerAdapter
from api.services.ci.runners.base import QUEUED, RUNNING, SUCCEEDED, FAILED, RunnerHandle


def _agent(app, name="mac-01", runner_type="agent_macos"):
    runner, token = agents_service.create_agent(
        {"name": name, "runnerType": runner_type, "maxConcurrent": 1}
    )
    return runner, token


def test_token_is_hashed_and_authenticates(app):
    with app.app_context():
        runner, token = _agent(app)
        # Only a hash is stored: the plaintext exists in the response and nowhere else.
        assert runner.token_hash and runner.token_hash != token
        assert token not in (runner.token_hash or "")
        assert agents_service.authenticate(token).id == runner.id
        with pytest.raises(AgentError):
            agents_service.authenticate(token + "x")
        with pytest.raises(AgentError):
            agents_service.authenticate("")


def test_a_new_agent_is_offline_until_it_checks_in(app):
    """Claiming to be online before anything connected would put builds in a
    queue nothing serves."""
    with app.app_context():
        runner, _ = _agent(app, name="linux-01", runner_type="agent_linux")
        assert runner.status == "offline"

        state = agents_service.heartbeat(
            runner, {"hostname": "buildbox", "capabilities": ["linux", "java"]}
        )
        assert state["accepting"] is True
        assert runner.status == "online"
        # Capabilities come from the machine, not from a form.
        assert runner.capabilities == ["linux", "java"]


def test_a_silent_agent_goes_offline(app):
    """Otherwise the scheduler keeps assigning to a switched-off machine."""
    from datetime import datetime, timedelta, timezone

    with app.app_context():
        runner, _ = _agent(app, name="ghost")
        agents_service.heartbeat(runner, {})
        assert runner.status == "online"

        runner.last_heartbeat_at = datetime.now(timezone.utc) - timedelta(
            seconds=agents_service.HEARTBEAT_GRACE_SECONDS + 30
        )
        db.session.add(runner)
        db.session.commit()

        agents_service.mark_stale_agents_offline()
        assert db.session.get(CiRunner, runner.id).status == "offline"


def _queued_task(app, runner):
    # Unique per call: one runner legitimately has several tasks queued.
    tag = f"{runner.name}-{CiAgentTask.query.count()}"
    service = CiService(name="Agent Svc " + tag, slug="agent-svc-" + tag)
    db.session.add(service)
    db.session.commit()
    build = CiBuild(service_id=service.id, number=1, status="running", branch="main")
    db.session.add(build)
    db.session.commit()
    stage = CiBuildStage(
        build_id=build.id, position=0, name="Build", stage_type="command", status="running"
    )
    db.session.add(stage)
    db.session.commit()
    task = CiAgentTask(
        build_id=build.id, build_stage_id=stage.id, runner_id=runner.id, state="queued"
    )
    db.session.add(task)
    db.session.commit()
    return build, stage, task


def test_a_claim_is_bound_to_the_agent_that_made_it(app):
    """Without the claim token a process that woke up after its task was reaped
    could post a result over whatever ran next."""
    with app.app_context():
        runner, _ = _agent(app, name="claimer", runner_type="agent_linux")
        agents_service.heartbeat(runner, {})
        _, _, task = _queued_task(app, runner)

        claimed = agents_service.claim_next(runner)
        assert claimed["taskId"] == task.id
        assert claimed["claimToken"]
        assert db.session.get(CiAgentTask, task.id).state == "claimed"

        agents_service.authorize_task(runner, task.id, claimed["claimToken"])
        with pytest.raises(AgentError):
            agents_service.authorize_task(runner, task.id, "not-the-token")

        other, _ = _agent(app, name="someone-else", runner_type="agent_linux")
        with pytest.raises(AgentError):
            agents_service.authorize_task(other, task.id, claimed["claimToken"])


def test_concurrency_and_drain_are_respected(app):
    with app.app_context():
        runner, _ = _agent(app, name="one-at-a-time", runner_type="agent_linux")
        agents_service.heartbeat(runner, {})
        _queued_task(app, runner)
        _queued_task(app, runner)

        assert agents_service.claim_next(runner) is not None
        # maxConcurrent is 1, so the second task waits rather than doubling up.
        assert agents_service.claim_next(runner) is None

        runner.status = "draining"
        db.session.add(runner)
        db.session.commit()
        assert agents_service.claim_next(runner) is None


def test_adapter_reports_the_task_state_as_a_stage_outcome(app):
    with app.app_context():
        runner, _ = _agent(app, name="reporter", runner_type="agent_linux")
        agents_service.heartbeat(runner, {})
        _, _, task = _queued_task(app, runner)
        adapter = ExternalAgentRunnerAdapter("agent_linux")
        handle = RunnerHandle(runner_id=runner.id, external_ref=f"agent:{task.id}")

        assert adapter.poll(handle) == QUEUED
        claimed = agents_service.claim_next(runner)
        assert adapter.poll(handle) == RUNNING

        agents_service.report_result(
            db.session.get(CiAgentTask, task.id), {"exitCode": 0}
        )
        assert adapter.poll(handle) == SUCCEEDED

        task2 = db.session.get(CiAgentTask, task.id)
        task2.state, task2.exit_code = "done", 2
        db.session.add(task2)
        db.session.commit()
        assert adapter.poll(handle) == FAILED


def test_an_abandoned_claim_fails_rather_than_pinning_the_build(app):
    from datetime import datetime, timedelta, timezone

    with app.app_context():
        runner, _ = _agent(app, name="vanisher", runner_type="agent_linux")
        agents_service.heartbeat(runner, {})
        _, _, task = _queued_task(app, runner)
        agents_service.claim_next(runner)

        row = db.session.get(CiAgentTask, task.id)
        row.last_heartbeat_at = datetime.now(timezone.utc) - timedelta(
            minutes=agents_service.CLAIM_TIMEOUT_MINUTES if False else 999
        )
        db.session.add(row)
        db.session.commit()

        adapter = ExternalAgentRunnerAdapter("agent_linux")
        handle = RunnerHandle(runner_id=runner.id, external_ref=f"agent:{task.id}")
        assert adapter.poll(handle) == FAILED
        assert "stopped reporting" in (db.session.get(CiAgentTask, task.id).error or "")


def test_agents_refuse_container_image_stages(app):
    """That means "build with BuildKit", which is the cluster's job. Saying so
    beats accepting the stage and running nothing."""
    with app.app_context():
        adapter = ExternalAgentRunnerAdapter("agent_macos")
        assert adapter.supported_stage_types() == {"checkout", "command"}
        assert "BuildKit" in adapter.skip_reason("container_image")
        assert adapter.skip_reason("command") is None
