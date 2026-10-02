"""Troubleshooting tickets: Hermes investigates "why is X broken?" and answers.

The investigation itself is Hermes' (read-only MCP tools); what KubeSight owns,
and what is tested here, is the answer's contract and where it leaves the
ticket:

* an answer carries evidence — a diagnosis with no findings is refused;
* the comment reaches the requester, the diagnosis and findings reach the
  DevOps team on the task row;
* on_hold keeps the conversation open: the requester's reply wakes Hermes;
* a proposed fix is NEVER run — it becomes an approval, like any change
  Hermes is not the one to decide on;
* the installation can switch troubleshooting off.

Hermes is faked exactly as in test_ticket_agent: it calls the tool functions.
"""

from api.db import db
from api.models import DeployAutomationRun, TicketInterpretation
from api.services.ticket_agent import engine, settings as agent_settings

from tests.test_ticket_agent import (  # noqa: F401  (fixtures are used by name)
    CLUSTER,
    DEPLOYMENT,
    HOOK_SECRET,
    NAMESPACE,
    _fake_hermes,
    _inbound_secret,
    _ticket,
    agent,
    writes,
)

from .conftest import auth_headers


def _answer(**overrides):
    base = {
        "comment": (
            "payments-api in payments keeps restarting because it cannot reach its database: "
            "the logs show connection refused to payments-db:5432. Your developers should check "
            "the database host setting. Reply here once it is changed and I will check again."
        ),
        "diagnosis": "CrashLoopBackOff: the app exits on startup, connection refused to payments-db:5432.",
        "findings": [
            {"finding": "Pod payments-api-7d9 restarted 14 times", "evidence": "kubesight_pod_issues"},
            {"finding": "Exits with 'Connection refused: payments-db:5432'",
             "evidence": "kubesight_pod_logs previous=true, last 20 lines"},
        ],
        "checked": ["Node capacity is fine", "The image tag is the one deployed yesterday"],
        "recommendation": "Developers: fix DB_HOST; DevOps: restart once it is set.",
        "confidence": "High",
        "status": "on_hold",
    }
    base.update(overrides)
    return base


def _problem_ticket():
    return _ticket(tag="", subject="payments-api is down in payments, why?")


def test_hermes_answers_a_problem_ticket_with_evidence(client, agent, writes, monkeypatch):
    ticket = _problem_ticket()

    def behaviour(message):
        assert message["task"] == "handle_new_ticket"
        assert message["troubleshooting"] is True
        # Where each application runs, so Hermes can go and look.
        assert {e["cluster"] for e in message["catalog"]} == {CLUSTER}
        engine.answer(message["ticketRecordId"], _answer())
        return {"outcome": "answered", "summary": "DB host wrong"}

    _fake_hermes(monkeypatch, behaviour)
    engine.queue_ticket(ticket.id)

    task = TicketInterpretation.query.filter_by(ticket_record_id=ticket.id).one()
    assert task.route == "answered" and task.status == "on_hold"
    found = task.decision["troubleshooting"]
    assert found["confidence"] == "High" and len(found["findings"]) == 2
    assert found["recommendation"].startswith("Developers")
    assert task.understanding.startswith("CrashLoopBackOff")

    # The requester sees Hermes' words, publicly, with the ticket on hold.
    status = [w for w in writes if w["kind"] == "status"][-1]
    assert status["outcome"] == "on_hold" and status["public"] is True
    assert "connection refused" in status["comment"]
    # Answering changes nothing in the cluster.
    assert DeployAutomationRun.query.filter_by(ticket_record_id=ticket.id).count() == 0

    serialized = engine.serialize(task)
    assert serialized["troubleshooting"]["findings"][1]["evidence"].startswith("kubesight_pod_logs")


def test_an_answer_without_evidence_is_refused(client, agent, writes):
    ticket = _problem_ticket()
    for missing in ({"findings": []}, {"diagnosis": ""}, {"recommendation": ""}, {"confidence": "Sure"}):
        try:
            engine.answer(ticket.id, _answer(**missing))
        except engine.AgentError as exc:
            assert exc.status == 400
        else:
            raise AssertionError(f"accepted an answer with {missing}")
    assert writes == []


def test_the_requester_reply_comes_back_to_hermes(client, agent, writes, monkeypatch):
    ticket = _problem_ticket()
    engine.answer(ticket.id, _answer())
    seen = []

    def resume(message):
        seen.append(message)
        assert message["task"] == "continue_ticket"
        assert message["newComments"][0]["text"] == "DB_HOST is fixed, still failing"
        engine.answer(message["ticketRecordId"], _answer(status="done", comment="Now healthy."))
        return {"outcome": "answered", "summary": "resolved"}

    _fake_hermes(monkeypatch, resume)
    response = client.post(
        "/api/zoho/inbound/comment",
        json={"ticketId": ticket.ticket_id, "comment": "DB_HOST is fixed, still failing", "author": "Rami"},
        headers={"X-Zoho-Secret": HOOK_SECRET},
    )
    assert response.get_json()["data"]["comments"][0]["handled"] is True
    assert len(seen) == 1
    latest = TicketInterpretation.query.filter_by(ticket_record_id=ticket.id).order_by(
        TicketInterpretation.id.desc()).first()
    assert latest.route == "answered" and latest.status == "done"
    assert [w for w in writes if w["kind"] == "status"][-1]["outcome"] == "deployed"

    # Closed as done: a further comment does not wake Hermes.
    out = engine.on_ticket_comment("zoho", ticket.ticket_id, "thanks!")
    assert out["handled"] is False


def test_a_proposed_fix_waits_for_a_human(client, agent, writes):
    ticket = _problem_ticket()
    result = engine.answer(ticket.id, _answer(proposedFix={
        "action": "restart",
        "environment": NAMESPACE,
        "application": DEPLOYMENT,
        "commentOnApprove": "Approved — restarting payments-api now.",
    }))
    assert result["fixProposed"] is True

    task = TicketInterpretation.query.filter_by(ticket_record_id=ticket.id).one()
    assert task.status == "awaiting_approval" and task.route == "approval"
    assert task.change_type == "restart"
    assert task.decision["troubleshooting"]["diagnosis"].startswith("CrashLoopBackOff")
    assert any("troubleshooting" in reason for reason in task.reasons)
    # Not run: a person decides.
    assert DeployAutomationRun.query.filter_by(ticket_record_id=ticket.id).count() == 0
    # The answer is the comment the requester sees while it waits.
    assert [w for w in writes if w["kind"] == "comment"][-1]["comment"].startswith("payments-api in payments")


def test_a_fix_outside_the_catalog_is_refused_before_anything_is_posted(client, agent, writes):
    ticket = _problem_ticket()
    try:
        engine.answer(ticket.id, _answer(proposedFix={
            "action": "restart", "environment": NAMESPACE, "application": "not-a-real-app",
            "commentOnApprove": "x",
        }))
    except engine.AgentError:
        pass
    else:
        raise AssertionError("a fix for an unknown application was accepted")
    assert writes == []


def test_troubleshooting_can_be_switched_off(client, agent, writes, admin_token):
    response = client.put(
        "/api/ticket-agent/settings", json={"troubleshootingEnabled": False},
        headers=auth_headers(admin_token),
    )
    assert response.status_code == 200, response.get_data(as_text=True)
    assert response.get_json()["data"]["troubleshootingEnabled"] is False

    ticket = _problem_ticket()
    try:
        engine.answer(ticket.id, _answer())
    except engine.AgentError as exc:
        assert "switched off" in str(exc)
    else:
        raise AssertionError("answered with troubleshooting off")
    assert writes == []
    agent_settings.update({"troubleshootingEnabled": True})


def test_hermes_answers_through_mcp(client, agent, writes, admin_token):
    from tests.test_mcp_server import call_tool

    ticket = _problem_ticket()
    result = call_tool(
        client, admin_token, "kubesight_ticket_answer", {"ticketRecordId": ticket.id, **_answer()}
    )
    assert not result.get("isError"), result["content"][0]["text"]
    assert result["structuredContent"]["ticketStatus"] == "on_hold"
    assert "answered ticket record" in result["content"][0]["text"]
    db.session.expire_all()
    task = TicketInterpretation.query.filter_by(ticket_record_id=ticket.id).one()
    assert task.route == "answered"
