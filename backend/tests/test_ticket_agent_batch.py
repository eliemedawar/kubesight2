"""One ticket, several applications — and how a missing image gets built.

Hermes hands KubeSight every application a ticket names in one call
(``changes``); KubeSight checks them all, starts one run each, and wakes Hermes
once when the last of them is over. Separately: the image check asks the
registries linked to the run's cluster, and the operator chooses which engine
builds a tag that is not there yet.
"""

from __future__ import annotations

import pytest

from api.db import db
from api.models import DeployAutomationRun, TicketInterpretation
from api.models_ci import CiPipeline, CiPipelineStage, CiService
from api.services import deploy_automation_service as automation
from api.services.ticket_agent import engine, settings as agent_settings

from .conftest import auth_headers
from .test_ticket_agent import (  # noqa: F401 — fixtures
    CLUSTER,
    DEPLOYMENT,
    NAMESPACE,
    _fake_hermes,
    _inbound_secret,
    _ticket,
    agent,
    writes,
)

OTHER = "ledger-worker"  # the mock cluster's second deployment in NAMESPACE


def _both(**overrides):
    base = {
        "changes": [
            {"action": "deploy_image", "environment": NAMESPACE, "application": DEPLOYMENT, "tag": "v9.9.9"},
            {"action": "deploy_image", "environment": NAMESPACE, "application": OTHER, "tag": "v2.0.0"},
        ],
        "confidence": "High",
        "understanding": "Deploy payments-api v9.9.9 and ledger-worker v2.0.0 to payments.",
        "comment": "Deploying payments-api v9.9.9 and ledger-worker v2.0.0 to payments now.",
    }
    base.update(overrides)
    return base


def _found(monkeypatch, missing=()):
    """Every image is in the registry except tags listed in ``missing``."""
    seen = []

    def check(image, **kw):
        seen.append({"image": image, **kw})
        status = "not_found" if any(image.endswith(f":{tag}") for tag in missing) else "found"
        return {"status": status, "image": image, "message": f"{image} {status}"}

    monkeypatch.setattr("api.services.registry_service.check_image", check)
    return seen


# ---------------------------------------------------------------------------
# Several applications in one ticket
# ---------------------------------------------------------------------------

def test_one_ticket_deploys_every_application_it_names(client, agent, writes, monkeypatch):
    checks = _found(monkeypatch)
    ticket = _ticket()
    result = engine.execute(ticket.id, _both())
    assert result["started"] is True and len(result["runs"]) == 2

    runs = DeployAutomationRun.query.filter_by(ticket_record_id=ticket.id).order_by(DeployAutomationRun.id).all()
    assert [(r.deployment_name, r.ticket_tag) for r in runs] == [(DEPLOYMENT, "v9.9.9"), (OTHER, "v2.0.0")]
    assert all(r.triggered_by == "hermes" for r in runs)
    task = TicketInterpretation.query.filter_by(ticket_record_id=ticket.id).one()
    assert task.decision["runIds"] == [r.id for r in runs]
    shown = engine.serialize(task)["changes"]
    assert [(c["deploymentName"], c["tag"], c["runId"]) for c in shown] == [
        (DEPLOYMENT, "v9.9.9", runs[0].id), (OTHER, "v2.0.0", runs[1].id)]
    # Hermes' one comment went on the ticket once.
    assert [w["comment"] for w in writes if w["kind"] == "comment"] == [_both()["comment"]]

    followups = []

    def close(message):
        followups.append(message)
        engine.set_status(message["ticketRecordId"], "done", "Both are live on payments.")
        return {"outcome": "status_set", "summary": "closed"}

    _fake_hermes(monkeypatch, close)
    for _ in range(4):
        automation.advance_runs()
    assert {r.status for r in DeployAutomationRun.query.filter_by(ticket_record_id=ticket.id)} == {"deployed"}

    # ONE follow-up, after the last run, carrying both results.
    assert len(followups) == 1
    event = followups[0]["event"]
    assert event["type"] == "runs_finished" and event["result"] == "deployed"
    assert {(r["deployment"], r["result"]) for r in event["runs"]} == {(DEPLOYMENT, "deployed"), (OTHER, "deployed")}
    assert "fallback" not in event
    done = [w for w in writes if w["kind"] == "status" and w["outcome"] == "deployed"]
    assert [w["comment"] for w in done] == ["Both are live on payments."]
    # Found in the registry → no build, just the tag swap; asked per cluster.
    assert all(c.get("cluster_id") == CLUSTER for c in checks)
    for run in DeployAutomationRun.query.filter_by(ticket_record_id=ticket.id):
        steps = {s["key"]: s for s in run.steps}
        assert steps["build"]["status"] == "skip" and "only the tag changes" in steps["build"]["detail"]


def test_one_failed_application_fails_the_ticket_with_every_result(client, agent, writes, monkeypatch):
    _found(monkeypatch, missing=("v2.0.0",))
    agent_settings.update({"buildEngine": "kubesight_ci"})  # and no CI service builds ledger-worker
    _fake_hermes(monkeypatch, lambda m: None)  # Hermes does nothing → KubeSight's fallback
    ticket = _ticket()
    engine.execute(ticket.id, _both())
    for _ in range(4):
        automation.advance_runs()

    runs = {r.deployment_name: r for r in DeployAutomationRun.query.filter_by(ticket_record_id=ticket.id)}
    assert runs[DEPLOYMENT].status == "deployed"
    assert runs[OTHER].status == "failed" and "builds are set to KubeSight CI" in runs[OTHER].error

    follow = TicketInterpretation.query.filter_by(ticket_record_id=ticket.id, kind="followup").one()
    assert follow.event["type"] == "runs_finished" and follow.event["result"] == "failed"
    final = [w for w in writes if w["kind"] == "status" and w["outcome"] in ("deployed", "failed")]
    # One write-back for the ticket, not one per run, and it names both.
    assert len(final) == 1 and final[0]["outcome"] == "failed"
    assert DEPLOYMENT in final[0]["comment"] and OTHER in final[0]["comment"]


def test_a_change_outside_the_catalog_starts_nothing(client, agent, writes):
    ticket = _ticket()
    bad = _both()
    bad["changes"][1]["application"] = "billing-api"
    with pytest.raises(engine.AgentError) as err:
        engine.execute(ticket.id, bad)
    assert err.value.status == 422 and "changes[1]" in str(err.value) and "billing-api" in str(err.value)
    assert DeployAutomationRun.query.count() == 0 and writes == []


def test_the_same_application_twice_is_refused(client, agent, writes):
    ticket = _ticket()
    twice = _both()
    twice["changes"][1] = dict(twice["changes"][0], tag="v9.9.8")
    with pytest.raises(engine.AgentError) as err:
        engine.execute(ticket.id, twice)
    assert "repeats changes[0]" in str(err.value)
    assert DeployAutomationRun.query.count() == 0


def test_a_run_refused_midway_withdraws_the_ones_already_created(client, agent, writes, monkeypatch):
    real = automation.start_run
    calls = []

    def flaky(*args, **kwargs):
        calls.append(kwargs.get("sibling"))
        if len(calls) == 2:
            raise automation.AutomationError("The ticket's deployment snapshot no longer exists.", 404)
        return real(*args, **kwargs)

    monkeypatch.setattr(automation, "start_run", flaky)
    seen = _fake_hermes(monkeypatch, lambda m: None)
    ticket = _ticket()
    with pytest.raises(engine.AgentError) as err:
        engine.execute(ticket.id, _both())
    assert calls == [True, True]
    assert OTHER in str(err.value) and "None of the ticket's changes were started" in str(err.value)
    run = DeployAutomationRun.query.filter_by(ticket_record_id=ticket.id).one()
    assert run.status == "cancelled" and run.error.startswith("Withdrawn")
    # Withdrawing is not an outcome: nobody is woken, nothing is written.
    assert seen == [] and TicketInterpretation.query.filter_by(kind="followup").count() == 0
    assert not [w for w in writes if w["kind"] == "comment"]


def test_dropdowns_naming_one_of_the_applications_do_not_need_approval(client, agent, writes):
    """The ticket's dropdowns name payments-api v9.9.9 — one of the two."""
    ticket = _ticket()
    assert engine.execute(ticket.id, _both())["started"] is True


def test_dropdowns_naming_none_of_them_need_approval(client, agent, writes):
    ticket = _ticket(tag="v1.0.0")
    with pytest.raises(engine.AgentError) as err:
        engine.execute(ticket.id, _both())
    assert err.value.status == 409 and "none of Hermes' deploys use" in str(err.value)


def test_approval_runs_every_application(client, agent, writes, admin_token):
    ticket = _ticket()
    engine.request_approval(ticket.id, _both(
        confidence="Medium",
        comment="Waiting for DevOps to confirm both.",
        commentOnApprove="Approved — deploying both now.",
    ))
    task = TicketInterpretation.query.filter_by(ticket_record_id=ticket.id).one()
    assert "payments-api v9.9.9 to payments; deploy ledger-worker v2.0.0" in engine._describe_change(task)
    response = client.post(f"/api/ticket-agent/tasks/{task.id}/approve", headers=auth_headers(admin_token))
    assert response.status_code == 200, response.get_json()
    db.session.refresh(task)
    assert task.status == "executed" and len(task.decision["runIds"]) == 2
    assert DeployAutomationRun.query.filter_by(ticket_record_id=ticket.id).count() == 2


def test_a_single_application_still_reads_as_before(client, agent, writes):
    ticket = _ticket()
    result = engine.execute(ticket.id, {
        "action": "deploy_image", "environment": NAMESPACE, "application": DEPLOYMENT, "tag": "v9.9.9",
        "confidence": "High", "understanding": "Deploy.", "comment": "Deploying.",
    })
    task = TicketInterpretation.query.filter_by(ticket_record_id=ticket.id).one()
    assert task.deployment_name == DEPLOYMENT and task.tag == "v9.9.9" and task.run_id == result["runId"]
    assert len(engine.serialize(task)["changes"]) == 1


def test_only_the_agent_may_start_sibling_runs(client, agent):
    ticket = _ticket()
    with pytest.raises(automation.AutomationError):
        automation.start_run(ticket.id, origin="operator", sibling=True)


# ---------------------------------------------------------------------------
# Which engine builds a missing image
# ---------------------------------------------------------------------------

def _checking_run(deployment=DEPLOYMENT, repo="ghcr.io/mock/payments"):
    run = DeployAutomationRun(
        cluster_id=CLUSTER, namespace=NAMESPACE, deployment_name=deployment,
        change_type="image", image_repo=repo, image_tag="v9.9.9", ticket_tag="v9.9.9",
        status="checking_image", steps=automation._initial_steps(),
    )
    db.session.add(run)
    db.session.commit()
    return run


def _ci_service(slug, deploy_target=None, image_stages=0, image_env=None):
    service = CiService(name=slug, slug=slug, status="active")
    db.session.add(service)
    db.session.flush()
    pipeline = CiPipeline(service_id=service.id, name="default", is_default=True, enabled=True, stages=[])
    db.session.add(pipeline)
    db.session.flush()
    for position in range(image_stages):
        db.session.add(CiPipelineStage(pipeline_id=pipeline.id, name=f"Image {position}",
                                       stage_type="container_image", position=position,
                                       env=image_env or {}))
    if deploy_target:
        db.session.add(CiPipelineStage(pipeline_id=pipeline.id, name="Deploy", stage_type="deploy",
                                       position=10, deploy=deploy_target))
    db.session.commit()
    return service


@pytest.fixture()
def native(monkeypatch):
    started = []
    monkeypatch.setattr(automation, "_trigger_native_build",
                        lambda run, service, pipeline=None: started.append(service.slug) or True)
    return started


def test_kubesight_ci_only_never_falls_back_to_jenkins(app, monkeypatch, native):
    _found(monkeypatch, missing=("v9.9.9",))
    automation.set_build_engine("kubesight_ci")
    jenkins = []
    monkeypatch.setattr(automation.jenkins_client, "trigger_build", lambda *a, **k: jenkins.append(a))
    run = _checking_run()
    automation._do_check(run, automation.get_or_create_jenkins())
    assert run.status == "failed" and "builds are set to KubeSight CI" in run.error
    assert jenkins == [] and native == []


def test_jenkins_only_skips_a_matching_ci_service(app, monkeypatch, native):
    _found(monkeypatch, missing=("v9.9.9",))
    _ci_service(DEPLOYMENT)
    automation.set_build_engine("jenkins")
    run = _checking_run()
    automation._do_check(run, automation.get_or_create_jenkins())
    assert native == []
    assert run.status == "failed" and "builds are set to Jenkins" in run.error


def test_auto_prefers_the_ci_service(app, monkeypatch, native):
    _found(monkeypatch, missing=("v9.9.9",))
    _ci_service(DEPLOYMENT)
    run = _checking_run()
    automation._do_check(run, automation.get_or_create_jenkins())
    assert native == [DEPLOYMENT]


def test_ci_service_found_by_its_deploy_stage(app, monkeypatch, native):
    _found(monkeypatch, missing=("v9.9.9",))
    _ci_service("issuing", deploy_target={"clusterId": CLUSTER, "namespace": NAMESPACE,
                                          "deploymentName": DEPLOYMENT})
    _ci_service("unrelated", deploy_target={"clusterId": CLUSTER, "namespace": "other",
                                            "deploymentName": DEPLOYMENT})
    run = _checking_run()
    automation._do_check(run, automation.get_or_create_jenkins())
    assert native == ["issuing"]


def test_ci_service_found_by_the_image_name(app, monkeypatch, native):
    _found(monkeypatch, missing=("v9.9.9",))
    _ci_service("issuing-ms")
    run = _checking_run(deployment="issuing-deployment", repo="registry.areeba.com/areeba/issuing-ms")
    automation._do_check(run, automation.get_or_create_jenkins())
    assert native == ["issuing-ms"]


def test_build_engine_is_a_ticket_agent_setting(client, admin_token):
    headers = auth_headers(admin_token)
    assert client.get("/api/ticket-agent/settings", headers=headers).get_json()["data"]["buildEngine"] == "auto"
    saved = client.put("/api/ticket-agent/settings", headers=headers, json={"buildEngine": "kubesight_ci"})
    assert saved.status_code == 200 and saved.get_json()["data"]["buildEngine"] == "kubesight_ci"
    assert automation.build_engine() == "kubesight_ci"
    bad = client.put("/api/ticket-agent/settings", headers=headers, json={"buildEngine": "travis"})
    assert bad.status_code == 400
    assert automation.build_engine() == "kubesight_ci"


# ---------------------------------------------------------------------------
# The build pushes what the deployment pulls, from the pipeline that deploys it
# ---------------------------------------------------------------------------

def test_repository_names_keep_their_path():
    from api.services.ci.engine import _sanitize_repository

    assert _sanitize_repository("areeba/Issuing-MS") == "areeba/issuing-ms"
    assert _sanitize_repository("//team//app/") == "team/app"
    assert _sanitize_repository("a b/c..d") == "a-b/c.d"
    assert _sanitize_repository("payment-service") == "payment-service"
    assert _sanitize_repository("") == "build"


@pytest.fixture()
def triggered(monkeypatch):
    """What the automation asked the CI engine to build."""
    calls = []

    def trigger_build(service, **kwargs):
        calls.append({"service": service.slug, **kwargs})
        return {"id": 900 + len(calls), "number": len(calls)}

    monkeypatch.setattr("api.services.ci.engine.trigger_build", trigger_build)
    return calls


def test_the_build_pushes_the_deployments_own_repository(app, monkeypatch, triggered):
    _found(monkeypatch, missing=("v9.9.9",))
    _ci_service("issuing-ms", image_stages=1)
    run = _checking_run(deployment="issuing-ms", repo="registry.areeba.com/areeba/issuing-ms")
    automation._do_check(run, automation.get_or_create_jenkins())
    assert run.status == "building"
    assert triggered[0]["variables"]["IMAGE_NAME"] == "areeba/issuing-ms"
    assert triggered[0]["variables"]["IMAGE_TAG"] == "v9.9.9"
    build_step = next(s for s in run.steps if s["key"] == "build")
    assert "pushing areeba/issuing-ms:v9.9.9" in build_step["detail"]


def test_image_name_is_left_alone_when_it_could_misfire(app, monkeypatch, triggered):
    _found(monkeypatch, missing=("v9.9.9",))
    # Two images built by one pipeline: one name for both would be wrong.
    _ci_service("issuing-ms", image_stages=2)
    # The pipeline author named the image themselves: theirs wins.
    _ci_service("processing-ms", image_stages=1, image_env={"IMAGE_NAME": "custom/name"})
    for name in ("issuing-ms", "processing-ms"):
        run = _checking_run(deployment=name, repo=f"registry.areeba.com/areeba/{name}")
        automation._do_check(run, automation.get_or_create_jenkins())
    assert [("IMAGE_NAME" in c["variables"]) for c in triggered] == [False, False]


def test_a_deploy_stage_match_builds_that_pipeline(app, monkeypatch, triggered):
    _found(monkeypatch, missing=("v9.9.9",))
    service = _ci_service("issuing")  # default pipeline: does not deploy the target
    other = CiPipeline(service_id=service.id, name="deploy-payments", is_default=False, enabled=True, stages=[])
    db.session.add(other)
    db.session.flush()
    db.session.add(CiPipelineStage(pipeline_id=other.id, name="Deploy", stage_type="deploy", position=0,
                                   deploy={"clusterId": CLUSTER, "namespace": NAMESPACE,
                                           "deploymentName": DEPLOYMENT}))
    db.session.commit()
    run = _checking_run()
    automation._do_check(run, automation.get_or_create_jenkins())
    assert triggered[0]["service"] == "issuing" and triggered[0]["pipeline_id"] == other.id
    assert "pipeline 'deploy-payments'" in next(s for s in run.steps if s["key"] == "build")["detail"]


def test_a_slug_match_builds_the_default_pipeline(app, monkeypatch, triggered):
    _found(monkeypatch, missing=("v9.9.9",))
    _ci_service(DEPLOYMENT)
    run = _checking_run()
    automation._do_check(run, automation.get_or_create_jenkins())
    assert triggered[0]["pipeline_id"] is None


# ---------------------------------------------------------------------------
# One change bundle for the ticket's changes on approval-gated clusters
# ---------------------------------------------------------------------------

def _gated(monkeypatch, unreachable=()):
    """Approval-gated cluster; live manifests per deployment; registry per tag."""
    from api.models import DeploymentRequestSetting

    approvals = DeploymentRequestSetting.query.first()
    approvals.cluster_required_approvals = {CLUSTER: 1}
    db.session.commit()

    def live(user, cluster_id, namespace, kind, name):
        return ({"yaml": (
            "apiVersion: apps/v1\nkind: Deployment\n"
            f"metadata:\n  name: {name}\n  namespace: {namespace}\n"
            "spec:\n  replicas: 2\n  template:\n    spec:\n      containers:\n"
            f"        - name: {name}\n          image: ghcr.io/mock/{name}:old\n"
        )}, None, 200)

    monkeypatch.setattr("api.services.resource_actions_service.get_resource_yaml", live)
    state = {"unreachable": set(unreachable)}

    def check(image, **kw):
        if any(image.endswith(f":{tag}") for tag in state["unreachable"]):
            return {"status": "unreachable", "image": image, "message": "registry timed out"}
        return {"status": "found", "image": image, "message": f"{image} found"}

    monkeypatch.setattr("api.services.registry_service.check_image", check)
    return state


def _runs(ticket):
    return {r.deployment_name: r for r in DeployAutomationRun.query.filter_by(ticket_record_id=ticket.id)}


def test_the_tickets_changes_share_one_change_bundle(client, agent, writes, monkeypatch):
    from api.models import ChangeBundle

    _gated(monkeypatch)
    _fake_hermes(monkeypatch, lambda m: None)
    ticket = _ticket()
    engine.execute(ticket.id, _both())
    automation.advance_runs()

    runs = _runs(ticket)
    assert {r.status for r in runs.values()} == {"awaiting_approval"}
    bundle_ids = {r.bundle_id for r in runs.values()}
    assert len(bundle_ids) == 1 and None not in bundle_ids
    bundle = db.session.get(ChangeBundle, bundle_ids.pop())
    assert bundle.status == "pending_approval" and bundle.stop_on_failure is False
    assert sorted(i.resource_name for i in bundle.items) == sorted([DEPLOYMENT, OTHER])
    assert "2 changes" in bundle.note and "payments-api → v9.9.9" in bundle.note
    assert ChangeBundle.query.filter(ChangeBundle.status != "draft").count() == 1

    bundle.status = "completed"
    db.session.commit()
    automation.advance_runs()
    assert {r.status for r in _runs(ticket).values()} == {"deployed"}


def test_a_ready_change_waits_for_the_one_still_on_its_way(client, agent, writes, monkeypatch):
    state = _gated(monkeypatch, unreachable=("v2.0.0",))
    _fake_hermes(monkeypatch, lambda m: None)
    ticket = _ticket()
    engine.execute(ticket.id, _both())
    automation.advance_runs()

    runs = _runs(ticket)
    first, other = runs[DEPLOYMENT], runs[OTHER]
    assert first.status == "awaiting_approval" and first.bundle_id is None
    assert other.status == "checking_image"
    approval = next(s for s in first.steps if s["key"] == "approval")
    assert approval["status"] == "wait" and OTHER in approval["detail"]

    state["unreachable"].clear()
    automation.advance_runs()
    runs = _runs(ticket)
    assert runs[DEPLOYMENT].bundle_id and runs[DEPLOYMENT].bundle_id == runs[OTHER].bundle_id


def test_a_failed_sibling_is_not_waited_for(client, agent, writes, monkeypatch):
    from api.models import ChangeBundle

    _gated(monkeypatch, unreachable=("v2.0.0",))
    _fake_hermes(monkeypatch, lambda m: None)
    ticket = _ticket()
    engine.execute(ticket.id, _both())
    automation.advance_runs()
    automation.cancel_run(_runs(ticket)[OTHER].id)
    automation.advance_runs()

    first = _runs(ticket)[DEPLOYMENT]
    assert first.status == "awaiting_approval" and first.bundle_id
    bundle = db.session.get(ChangeBundle, first.bundle_id)
    assert [i.resource_name for i in bundle.items] == [DEPLOYMENT]


def test_each_run_follows_its_own_item_in_a_partly_failed_bundle(client, agent, writes, monkeypatch):
    from api.models import ChangeBundle

    _gated(monkeypatch)
    _fake_hermes(monkeypatch, lambda m: None)
    ticket = _ticket()
    engine.execute(ticket.id, _both())
    automation.advance_runs()
    bundle = db.session.get(ChangeBundle, _runs(ticket)[DEPLOYMENT].bundle_id)
    for item in bundle.items:
        item.status = "succeeded" if item.resource_name == DEPLOYMENT else "failed"
    bundle.status = "partially_failed"
    db.session.commit()
    automation.advance_runs()

    runs = _runs(ticket)
    assert runs[DEPLOYMENT].status == "deployed"
    assert runs[OTHER].status == "failed" and f"#{bundle.id}" in runs[OTHER].error


def test_cancelling_one_change_says_the_shared_bundle_goes_with_it(client, agent, writes, monkeypatch):
    from api.models import ChangeBundle

    _gated(monkeypatch)
    _fake_hermes(monkeypatch, lambda m: None)
    ticket = _ticket()
    engine.execute(ticket.id, _both())
    automation.advance_runs()
    runs = _runs(ticket)
    automation.cancel_run(runs[DEPLOYMENT].id)
    bundle = db.session.get(ChangeBundle, runs[OTHER].bundle_id)
    assert bundle.status == "rejected" and "1 other change is withdrawn" in bundle.rejection_reason
    automation.advance_runs()
    assert _runs(ticket)[OTHER].status == "failed"
