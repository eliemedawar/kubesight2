"""Hermes ticket agent: Hermes handles each ticket through the kubesight_ticket_* tools.

Hermes itself is faked: ``hermes.run_task`` is replaced by a function that does
what Hermes would do mid-call — invoke the tool functions — and then returns its
closing line. Everything downstream is real: the catalog, the guard rails in
the tools, deploy-automation runs, the approval state machine, follow-ups.
Ticket write-back is captured at the ``ticketing`` seam.
"""

import json
from datetime import datetime, timedelta, timezone

import pytest

from api.db import db
from api.models import (
    DeployAutomationRun,
    DeploymentRequestSetting,
    TicketInterpretation,
    ZohoDeploymentSnapshot,
    ZohoInboundTicket,
)
from api.services import ticketing_targets
from api.services.ticket_agent import engine, hermes, settings as agent_settings

from .conftest import auth_headers

CLUSTER = "prod-us-east"
NAMESPACE = "payments"
DEPLOYMENT = "payments-api"


@pytest.fixture()
def writes(monkeypatch):
    """Every comment / status the agent (or the run) writes onto a ticket."""
    log = []
    monkeypatch.setattr(
        "api.services.ticketing.post_comment",
        lambda provider, ticket_id, comment, public=False: log.append(
            {"kind": "comment", "ticket": ticket_id, "comment": comment, "public": public}
        ),
    )
    monkeypatch.setattr(
        "api.services.ticketing.report_outcome",
        lambda provider, ticket_id, outcome, comment=None, resolution=None, public=False: log.append(
            {"kind": "status", "ticket": ticket_id, "outcome": outcome, "comment": comment, "public": public}
        ),
    )
    return log


@pytest.fixture()
def agent(app, monkeypatch):
    """Agent on, pointed at a (fake) dedicated Hermes, one published target."""
    monkeypatch.setenv("TICKET_AGENT_HERMES_URL", "https://hermes.example.com/v1/chat/completions")
    monkeypatch.setenv("TICKET_AGENT_HERMES_TOKEN", "hermes-key")
    row = ticketing_targets.get_or_create_config("zoho")
    row.source_cluster_id = CLUSTER
    row.selected_namespaces = json.dumps([NAMESPACE])
    db.session.add(ZohoDeploymentSnapshot(cluster_id=CLUSTER, namespace=NAMESPACE, deployment_name=DEPLOYMENT))
    db.session.commit()
    # Direct apply (no Change Bundle) so runs can finish in the mock cluster.
    approvals = DeploymentRequestSetting.query.first() or DeploymentRequestSetting()
    approvals.cluster_required_approvals = {CLUSTER: 0}
    db.session.add(approvals)
    db.session.commit()
    agent_settings.update({"enabled": True})
    return row


def _fake_hermes(monkeypatch, behaviour):
    """Replace Hermes: ``behaviour(message)`` acts through the tools, returns a summary."""
    seen = []

    def run_task(message, session_key=None):
        seen.append(message)
        summary = behaviour(message) or {"outcome": "nothing", "summary": "did nothing"}
        return json.dumps(summary), "hermes-test"

    monkeypatch.setattr(hermes, "run_task", run_task)
    return seen


def _ticket(tag="v9.9.9", subject="Deploy payments-api v9.9.9 to payments"):
    ticket = ZohoInboundTicket(
        provider="zoho",
        ticket_id=f"zt-{datetime.now(timezone.utc).timestamp()}",
        ticket_number="DR-7001",
        subject=subject,
        raw_app_value=DEPLOYMENT,
        tag=tag,
        resolved=True,
        app_service_id=ZohoDeploymentSnapshot.query.filter_by(deployment_name=DEPLOYMENT).first().id,
        payload={"description": "<p>Please deploy <b>v9.9.9</b></p>"},
        received_at=datetime.now(timezone.utc),
    )
    db.session.add(ticket)
    db.session.commit()
    return ticket


def _action(**overrides):
    base = {
        "action": "deploy_image",
        "environment": NAMESPACE,
        "application": DEPLOYMENT,
        "tag": "v9.9.9",
        "confidence": "High",
        "understanding": "Deploy payments-api v9.9.9 to payments.",
        "comment": "Understood: deploying payments-api v9.9.9 to payments. Starting now.",
    }
    base.update(overrides)
    return base


# ---------------------------------------------------------------------------
# Intake → Hermes → tools
# ---------------------------------------------------------------------------

def test_webhook_hands_every_ticket_to_hermes_which_executes(client, agent, writes, monkeypatch):
    def behaviour(message):
        assert message["task"] == "handle_new_ticket"
        # Read live from the (mock) cluster's chosen namespace — every
        # deployment in it, not just the ones a dropdown sync once stored.
        assert {
            "environment": NAMESPACE, "application": DEPLOYMENT,
            "allowedActions": ["deploy_image", "set_env_var", "restart"],
        } in message["catalog"]
        assert {e["environment"] for e in message["catalog"]} == {NAMESPACE}
        # The Desk HTML description reaches Hermes as text.
        assert "Please deploy v9.9.9" in message["ticket"]["description"]
        engine.execute(message["ticketRecordId"], _action())
        return {"outcome": "executed", "summary": "deploying v9.9.9"}

    seen = _fake_hermes(monkeypatch, behaviour)
    response = client.post("/api/ticketing/zoho/inbound", json={
        "ticketId": "9001", "ticketNumber": "DR-9001", "subject": "deploy please",
        "description": "<p>Please deploy <b>v9.9.9</b></p>",
        "cf": {"cf_application": DEPLOYMENT, "cf_environment": NAMESPACE, "cf_tag": "v9.9.9"},
    })
    assert response.status_code == 200
    assert len(seen) == 1

    record = ZohoInboundTicket.query.filter_by(ticket_id="9001").one()
    task = TicketInterpretation.query.filter_by(ticket_record_id=record.id).one()
    assert task.status == "executed" and task.route == "execute"
    assert task.final_message == "deploying v9.9.9"
    run = db.session.get(DeployAutomationRun, task.run_id)
    assert run.change_type == "image" and run.triggered_by == "hermes"
    # Exactly one run: the dropdown auto-run stayed out of the way.
    assert DeployAutomationRun.query.filter_by(ticket_record_id=record.id).count() == 1

    # Hermes' comment went on the ticket, publicly; the run's own "started"
    # write-back moved the status without a second comment.
    comments = [w for w in writes if w["kind"] == "comment"]
    assert comments and comments[0]["comment"].startswith("Understood") and comments[0]["public"] is True
    started = [w for w in writes if w["kind"] == "status" and w["outcome"] == "started"]
    assert started and started[0]["comment"] is None

    # A redelivered webhook for the same ticket does not wake Hermes again.
    client.post("/api/ticketing/zoho/inbound", json={"ticketId": "9001", "ticketNumber": "DR-9001"})
    assert len(seen) == 1


def test_agent_off_keeps_the_dropdown_path(client, app, writes, monkeypatch):
    seen = _fake_hermes(monkeypatch, lambda m: None)
    agent_settings.update({"enabled": True})  # on, but no dedicated Hermes URL
    assert agent_settings.is_active() is False
    client.post("/api/ticketing/zoho/inbound", json={"ticketId": "9100", "ticketNumber": "DR-9100"})
    assert seen == []
    assert TicketInterpretation.query.count() == 0


def test_hermes_returning_without_acting_is_an_error_not_silence(client, agent, writes, monkeypatch):
    _fake_hermes(monkeypatch, lambda m: {"outcome": "nothing", "summary": "not sure"})
    ticket = _ticket()
    engine.queue_ticket(ticket.id)
    task = TicketInterpretation.query.filter_by(ticket_record_id=ticket.id).one()
    assert task.status == "error"
    assert "without acting" in task.error and "not sure" in task.error
    assert writes == []  # nothing is written on the requester's ticket


def test_transient_outage_retries_later(client, agent, monkeypatch):
    def down(message, session_key=None):
        raise hermes.HermesTransientError("Hermes is unavailable or timed out.")

    monkeypatch.setattr(hermes, "run_task", down)
    ticket = _ticket()
    engine.queue_ticket(ticket.id)
    task = TicketInterpretation.query.filter_by(ticket_record_id=ticket.id).one()
    assert task.status == "pending" and task.retry_at is not None
    assert "Retrying" in task.error


# ---------------------------------------------------------------------------
# The guard rails inside the tools
# ---------------------------------------------------------------------------

def test_execute_refuses_a_target_outside_the_catalog(client, agent, writes):
    ticket = _ticket()
    with pytest.raises(engine.AgentError) as err:
        engine.execute(ticket.id, _action(application="billing-api"))
    assert err.value.status == 422 and "not in the catalog" in str(err.value)
    assert DeployAutomationRun.query.count() == 0 and writes == []


def test_execute_refuses_low_confidence_and_dropdown_conflicts(client, agent, writes):
    ticket = _ticket()
    with pytest.raises(engine.AgentError) as err:
        engine.execute(ticket.id, _action(confidence="Medium"))
    assert err.value.status == 409 and "request_approval" in str(err.value)
    # High confidence, but the ticket's Tag field says something else.
    with pytest.raises(engine.AgentError) as err:
        engine.execute(ticket.id, _action(tag="v1.0.0"))
    assert "Tag field says v9.9.9" in str(err.value)
    # Hermes' own concern also routes to a human.
    with pytest.raises(engine.AgentError):
        engine.execute(ticket.id, _action(concerns=["prod freeze this week"]))
    assert DeployAutomationRun.query.count() == 0


def test_medium_bar_lets_medium_confidence_through(client, agent, writes):
    agent_settings.update({"minConfidence": "Medium"})
    ticket = _ticket()
    result = engine.execute(ticket.id, _action(confidence="Medium"))
    assert result["started"] is True


def test_set_status_impediment_writes_hermes_comment_publicly(client, agent, writes):
    ticket = _ticket(tag="")
    result = engine.set_status(ticket.id, "impediment", "Which environment should this go to?")
    assert result["ticketStatus"] == "impediment"
    status = [w for w in writes if w["kind"] == "status"][-1]
    assert status["outcome"] == "impediment" and status["public"] is True
    assert status["comment"] == "Which environment should this go to?"
    task = TicketInterpretation.query.filter_by(ticket_record_id=ticket.id).one()
    assert task.status == "impediment"


def test_done_is_refused_while_the_run_is_still_going(client, agent, writes):
    ticket = _ticket()
    engine.execute(ticket.id, _action())
    with pytest.raises(engine.AgentError) as err:
        engine.set_status(ticket.id, "done", "All done!")
    assert err.value.status == 409 and "still" in str(err.value)


# ---------------------------------------------------------------------------
# Approvals
# ---------------------------------------------------------------------------

def _request(ticket_id, **overrides):
    return engine.request_approval(ticket_id, _action(
        confidence="Medium",
        reasons=["environment was only implied"],
        comment="I think you want v9.9.9 on payments; a DevOps engineer will confirm.",
        commentOnApprove="Approved — deploying payments-api v9.9.9 now.",
        **overrides,
    ))


def test_approval_on_telegram_then_approve_in_the_ui(client, agent, writes, admin_token, monkeypatch):
    sent = []
    monkeypatch.setattr("api.services.ticket_agent.telegram.send_message",
                        lambda token, chat, text, buttons=None: sent.append((chat, text, buttons)) or 55)
    edits = []
    monkeypatch.setattr("api.services.ticket_agent.telegram.edit_message",
                        lambda token, chat, mid, text: edits.append(text))
    agent_settings.update({"telegramEnabled": True, "telegramBotToken": "123:abc", "telegramChatId": "-100200"})

    ticket = _ticket()
    result = _request(ticket.id)
    assert result["approvalRequested"] is True and "Telegram" in result["where"]
    task = TicketInterpretation.query.filter_by(ticket_record_id=ticket.id).one()
    assert task.status == "awaiting_approval" and task.telegram_message_id == 55
    chat, text, buttons = sent[0]
    assert chat == "-100200" and "deploy payments-api v9.9.9 to payments" in text
    assert buttons[0][0]["callback_data"] == f"ka:{task.id}:a:{task.approval_nonce}"
    assert writes[-1]["comment"].startswith("I think you want")

    response = client.post(f"/api/ticket-agent/tasks/{task.id}/approve", headers=auth_headers(admin_token))
    assert response.status_code == 200, response.get_json()
    db.session.refresh(task)
    assert task.status == "executed" and task.decided_by
    run = db.session.get(DeployAutomationRun, task.run_id)
    assert run.triggered_by.startswith("hermes · approved by")
    assert any(w["comment"] == "Approved — deploying payments-api v9.9.9 now." for w in writes)
    assert "Approved by" in edits[-1]

    # A second decision is refused.
    response = client.post(f"/api/ticket-agent/tasks/{task.id}/reject", headers=auth_headers(admin_token))
    assert response.status_code == 409


def test_telegram_button_press_approves(client, agent, writes, monkeypatch):
    monkeypatch.setattr("api.services.ticket_agent.telegram.send_message", lambda *a, **k: 77)
    monkeypatch.setattr("api.services.ticket_agent.telegram.edit_message", lambda *a, **k: None)
    answered = []
    monkeypatch.setattr("api.services.ticket_agent.telegram.answer_callback",
                        lambda token, cid, text="": answered.append(text))
    agent_settings.update({"telegramEnabled": True, "telegramBotToken": "123:abc",
                           "telegramChatId": "-100200", "telegramApprovers": "@devops_lead"})
    ticket = _ticket()
    _request(ticket.id)
    task = TicketInterpretation.query.filter_by(ticket_record_id=ticket.id).one()

    def press(user, data):
        return [{"update_id": 10, "callback_query": {
            "id": "cb1", "data": data, "from": user, "message": {"chat": {"id": -100200}}}}]

    # Someone not on the approver list is refused.
    monkeypatch.setattr("api.services.ticket_agent.telegram.get_updates",
                        lambda token, offset: press({"id": 1, "username": "intern"},
                                                    f"ka:{task.id}:a:{task.approval_nonce}"))
    engine.poll_telegram()
    db.session.refresh(task)
    assert task.status == "awaiting_approval" and "approver list" in answered[-1]

    monkeypatch.setattr("api.services.ticket_agent.telegram.get_updates",
                        lambda token, offset: press({"id": 2, "username": "devops_lead"},
                                                    f"ka:{task.id}:a:{task.approval_nonce}"))
    assert engine.poll_telegram() == 1
    db.session.refresh(task)
    assert task.status == "executed" and task.decided_by == "@devops_lead (Telegram)"
    assert agent_settings.get_or_create().telegram_update_offset == 11


def test_rejection_wakes_hermes_to_write_the_impediment(client, agent, writes, monkeypatch):
    ticket = _ticket()
    _request(ticket.id)
    task = TicketInterpretation.query.filter_by(ticket_record_id=ticket.id).one()

    def followup(message):
        assert message["task"] == "followup"
        assert message["event"]["type"] == "approval_rejected"
        assert message["event"]["note"] == "wrong env"
        assert "fallback" not in message["event"]
        engine.set_status(message["ticketRecordId"], "impediment",
                          "The DevOps team did not approve this — which environment did you mean?")
        return {"outcome": "status_set", "summary": "asked which env"}

    _fake_hermes(monkeypatch, followup)
    engine.reject(task.id, "alice", note="wrong env")
    follow = TicketInterpretation.query.filter_by(ticket_record_id=ticket.id, kind="followup").one()
    assert follow.status == "impediment"
    assert writes[-1]["outcome"] == "impediment" and "which environment" in writes[-1]["comment"]


def test_followup_falls_back_to_kubesight_comment_when_hermes_does_nothing(client, agent, writes, monkeypatch):
    ticket = _ticket()
    _request(ticket.id)
    task = TicketInterpretation.query.filter_by(ticket_record_id=ticket.id).one()
    _fake_hermes(monkeypatch, lambda m: None)
    engine.reject(task.id, "alice")
    follow = TicketInterpretation.query.filter_by(ticket_record_id=ticket.id, kind="followup").one()
    assert follow.status == "error" and "posted its own update" in follow.error
    assert writes[-1]["outcome"] == "impediment"
    assert writes[-1]["comment"].startswith("The DevOps team reviewed this request")


def test_unanswered_approval_expires(client, agent, writes, monkeypatch):
    ticket = _ticket()
    _request(ticket.id)
    task = TicketInterpretation.query.filter_by(ticket_record_id=ticket.id).one()
    task.expires_at = datetime.now(timezone.utc) - timedelta(minutes=1)
    db.session.commit()
    events = []
    _fake_hermes(monkeypatch, lambda m: events.append(m["event"]["type"]) or engine.set_status(
        m["ticketRecordId"], "impediment", "Nobody could confirm this in time — please confirm the environment."))
    assert engine.expire_approvals() == 1
    db.session.refresh(task)
    assert task.status == "impediment" and events == ["approval_expired"]


# ---------------------------------------------------------------------------
# Run outcome → Hermes
# ---------------------------------------------------------------------------

def test_finished_run_wakes_hermes_to_close_the_ticket(client, agent, writes, monkeypatch):
    monkeypatch.setattr("api.services.registry_service.check_image",
                        lambda image, **kw: {"status": "found", "image": image})
    ticket = _ticket()
    engine.execute(ticket.id, _action())
    closing = []

    def followup(message):
        assert message["event"]["type"] == "run_finished" and message["event"]["result"] == "deployed"
        closing.append(message)
        engine.set_status(message["ticketRecordId"], "done", "payments-api v9.9.9 is live on payments.")
        return {"outcome": "status_set", "summary": "closed"}

    _fake_hermes(monkeypatch, followup)
    from api.services.deploy_automation_service import advance_runs

    for _ in range(4):
        advance_runs()
    run = DeployAutomationRun.query.filter_by(ticket_record_id=ticket.id).one()
    assert run.status == "deployed"
    assert len(closing) == 1
    done = [w for w in writes if w["kind"] == "status" and w["outcome"] == "deployed"]
    # Hermes' words, not KubeSight's canned "Deployment succeeded" line.
    assert [w["comment"] for w in done] == ["payments-api v9.9.9 is live on payments."]


def test_restart_run_runs_to_deployed(client, agent, writes, monkeypatch):
    monkeypatch.setattr("api.services.registry_service.check_image",
                        lambda image, **kw: {"status": "found", "image": image})
    _fake_hermes(monkeypatch, lambda m: None)
    ticket = _ticket(tag="")
    result = engine.execute(ticket.id, _action(action="restart", tag=None,
                                                understanding="Restart payments-api.",
                                                comment="Restarting payments-api now."))
    from api.services.deploy_automation_service import advance_runs

    for _ in range(4):
        advance_runs()
    run = db.session.get(DeployAutomationRun, result["runId"])
    assert run.change_type == "restart" and run.status == "deployed"
    steps = {s["key"]: s for s in run.steps}
    assert steps["build"]["status"] == "skip" and "rollout restart" in steps["deploy"]["detail"]


def test_restart_bundle_stamps_the_pod_template():
    from api.services.deploy_automation_service import RESTART_ANNOTATION, _restart_in_yaml
    import yaml

    out = yaml.safe_load(_restart_in_yaml(
        "apiVersion: apps/v1\nkind: Deployment\nmetadata:\n  name: x\nspec:\n  template:\n    spec:\n"
        "      containers:\n        - name: x\n          image: x:1\n",
        "2026-09-25T10:00:00+00:00",
    ))
    assert out["spec"]["template"]["metadata"]["annotations"][RESTART_ANNOTATION] == "2026-09-25T10:00:00+00:00"
    assert out["spec"]["template"]["spec"]["containers"][0]["image"] == "x:1"


# ---------------------------------------------------------------------------
# Through the real MCP endpoint, and the settings surface
# ---------------------------------------------------------------------------

def test_hermes_can_drive_it_through_mcp(client, agent, writes, admin_token):
    from tests.test_mcp_server import call_tool

    ticket = _ticket()
    got = call_tool(client, admin_token, "kubesight_ticket_get", {"ticketRecordId": ticket.id})
    assert got["structuredContent"]["catalog"][0]["application"] == DEPLOYMENT

    refused = call_tool(client, admin_token, "kubesight_ticket_execute",
                        {"ticketRecordId": ticket.id, **_action(confidence="Low")})
    assert refused["isError"] and "approval" in refused["content"][0]["text"]

    done = call_tool(client, admin_token, "kubesight_ticket_execute", {"ticketRecordId": ticket.id, **_action()})
    assert not done.get("isError"), done
    assert done["structuredContent"]["started"] is True
    # Driven from chat, with no task KubeSight started: one is opened to record it.
    task = TicketInterpretation.query.filter_by(ticket_record_id=ticket.id).one()
    assert task.status == "executed" and task.requested_by


def test_settings_roundtrip_keeps_the_bot_token_write_only(client, admin_token, viewer_token):
    response = client.put("/api/ticket-agent/settings", headers=auth_headers(admin_token), json={
        "enabled": True, "minConfidence": "medium", "telegramBotToken": "123456:SECRET",
        "telegramChatId": "-1001", "telegramApprovers": "@a, 42",
    })
    assert response.status_code == 200, response.get_json()
    data = response.get_json()["data"]
    assert data["minConfidence"] == "Medium" and data["telegramBotTokenConfigured"] is True
    assert "SECRET" not in json.dumps(data)
    assert agent_settings.approvers() == ["a", "42"]

    bad = client.put("/api/ticket-agent/settings", headers=auth_headers(admin_token),
                     json={"minConfidence": "Low"})
    assert bad.status_code == 400
    denied = client.put("/api/ticket-agent/settings", headers=auth_headers(viewer_token), json={"enabled": False})
    assert denied.status_code == 403


def test_inbound_list_carries_the_agent_tasks(client, agent, writes, admin_token):
    ticket = _ticket()
    engine.set_status(ticket.id, "impediment", "What tag?")
    response = client.get("/api/ticketing/zoho/inbound-tickets", headers=auth_headers(admin_token))
    row = next(t for t in response.get_json()["data"]["items"] if t["id"] == ticket.id)
    assert row["agentTasks"][0]["status"] == "impediment"
    assert row["agentTasks"][0]["comment"] == "What tag?"


# ---------------------------------------------------------------------------
# Requester replies wake Hermes on a parked ticket
# ---------------------------------------------------------------------------

def _park(ticket, status="impediment", text="Which environment should this go to?"):
    engine.set_status(ticket.id, status, text)


def test_a_reply_on_a_parked_ticket_wakes_hermes_with_the_conversation(client, agent, writes, monkeypatch):
    ticket = _ticket(tag="")
    _park(ticket)
    seen = []

    def resume(message):
        seen.append(message)
        assert message["task"] == "continue_ticket"
        assert message["conversation"][-1] == {"from": "you (Hermes)", "text": "Which environment should this go to?"}
        assert message["newComments"][0]["text"] == "payments, tag v9.9.9 please"
        engine.execute(message["ticketRecordId"], _action())
        return {"outcome": "executed", "summary": "continued"}

    _fake_hermes(monkeypatch, resume)
    response = client.post(
        "/api/zoho/inbound/comment",
        json={"ticketId": ticket.ticket_id, "comment": "<p>payments, tag v9.9.9 please</p>", "author": "Rami"},
    )
    assert response.status_code == 200
    result = response.get_json()["data"]["comments"][0]
    assert result["handled"] is True, result
    assert len(seen) == 1
    resumed = TicketInterpretation.query.filter_by(ticket_record_id=ticket.id).order_by(
        TicketInterpretation.id.desc()).first()
    assert resumed.status == "executed" and resumed.event["type"] == "requester_replied"


def test_on_hold_is_a_parked_status_too(client, agent, writes, monkeypatch):
    ticket = _ticket(tag="")
    _park(ticket, "on_hold", "Waiting for your go-ahead on the time window.")
    assert [w for w in writes if w["kind"] == "status"][-1]["outcome"] == "on_hold"
    seen = _fake_hermes(monkeypatch, lambda m: None)
    out = engine.on_ticket_comment("zoho", ticket.ticket_id, "Go ahead now.")
    assert out["handled"] is True and len(seen) == 1


def test_kubesight_own_comment_echo_is_ignored(client, agent, writes, monkeypatch):
    ticket = _ticket(tag="")
    _park(ticket, text="Which environment should this go to?")
    seen = _fake_hermes(monkeypatch, lambda m: None)
    # Zoho echoes our comment back as HTML with different whitespace.
    out = engine.on_ticket_comment("zoho", ticket.ticket_id, "<div>Which environment  should this go to?</div>")
    assert out["handled"] is False and "own comment" in out["reason"]
    assert seen == []


def test_a_comment_on_a_ticket_that_is_not_parked_is_ignored(client, agent, writes, monkeypatch):
    ticket = _ticket()
    engine.execute(ticket.id, _action())  # executed, run open
    seen = _fake_hermes(monkeypatch, lambda m: None)
    out = engine.on_ticket_comment("zoho", ticket.ticket_id, "any update?")
    assert out["handled"] is False and seen == []
    assert engine.on_ticket_comment("zoho", "nope", "hi")["reason"].startswith("KubeSight has no ticket")


def test_replies_are_capped_per_ticket(client, agent, writes, monkeypatch):
    monkeypatch.setenv("TICKET_AGENT_MAX_ROUNDS", "2")
    ticket = _ticket(tag="")
    _park(ticket)  # opens handle task #1 (chat-driven)
    _fake_hermes(monkeypatch, lambda m: engine.set_status(m["ticketRecordId"], "impediment", "Still unclear, which app?"))
    assert engine.on_ticket_comment("zoho", ticket.ticket_id, "the app")["handled"] is True
    capped = engine.on_ticket_comment("zoho", ticket.ticket_id, "the payments one")
    assert capped["handled"] is False and "rounds" in capped["reason"]


def test_zoho_desk_webhook_shape_and_outgoing_threads(client, agent, writes, monkeypatch):
    ticket = _ticket(tag="")
    _park(ticket)
    seen = _fake_hermes(monkeypatch, lambda m: None)
    body = [
        {"eventType": "Ticket_Thread_Add", "payload": {"ticketId": ticket.ticket_id, "direction": "out",
                                                         "summary": "agent reply"}},
        {"eventType": "Ticket_Comment_Add", "payload": {"ticketId": ticket.ticket_id, "id": "c-1",
                                                          "content": "It is payments.",
                                                          "commenter": {"name": "Rami"}}},
    ]
    response = client.post("/api/ticketing/zoho/inbound/comment", json=body)
    assert response.status_code == 200
    comments = response.get_json()["data"]["comments"]
    assert len(comments) == 1 and comments[0]["handled"] is True
    assert seen[0]["newComments"][0] == {"id": "c-1", "author": "Rami", "text": "It is payments."}


def test_comment_webhook_checks_the_secret(client, app):
    from api.services import zoho_sync_service

    zoho_sync_service.update_config({"inboundSecret": "s3cret"})
    response = client.post("/api/zoho/inbound/comment", json={"ticketId": "1", "comment": "x"})
    assert response.status_code == 401
