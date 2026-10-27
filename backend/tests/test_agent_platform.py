"""KubeSight's side of the agent platform (PydanticAI + Temporal).

The platform itself is faked at the HTTP seam (``agent_platform.submit_ticket_task``):
what it would do — the agent acting through the ticket tools, then the report
back to ``/api/agent-platform/...`` — is done by the test, against the real
tools, task rows, callbacks and fallbacks.
"""

from hashlib import sha256

import pytest

from api.db import db
from api.models import ApiToken, AuditLog, DeployAutomationRun, TicketInterpretation, User, ZohoInboundTicket
from api.rbac_data import AGENT_PLATFORM_PERMISSIONS
from api.seed import DEPLOYMENT_AGENT_PLATFORM_TOKEN_NAME, seed_defaults
from api.services import agent_platform
from api.services.ticket_agent import engine, settings as agent_settings

from .conftest import auth_headers
from .test_ticket_agent import (  # noqa: F401 — fixtures
    HOOK,
    NAMESPACE,
    _action,
    _inbound_secret,
    _ticket,
    agent,
    writes,
)

PLATFORM_URL = "http://kubesight-agent-platform.kubesight-agents.svc:8000"
PLATFORM_TOKEN = "platform-shared-secret"
BEARER = {"Authorization": f"Bearer {PLATFORM_TOKEN}"}


@pytest.fixture()
def platform(monkeypatch):
    monkeypatch.setenv("AGENT_PLATFORM_URL", PLATFORM_URL)
    monkeypatch.setenv("AGENT_PLATFORM_TOKEN", PLATFORM_TOKEN)
    monkeypatch.setenv("AGENT_ENGINE_TICKETS", "platform")
    submitted = []

    def submit(**task):
        submitted.append(task)
        return {"workflowId": f"ticket-{task['provider']}-{task['record_id']}"}

    monkeypatch.setattr(agent_platform, "submit_ticket_task", submit)
    return submitted


def _report(client, task_id, headers=BEARER, **body):
    return client.post(f"/api/agent-platform/ticket-tasks/{task_id}/result", json=body, headers=headers)


def test_switches_default_to_hermes(monkeypatch):
    for flow in agent_platform.FLOWS:
        monkeypatch.delenv(f"AGENT_ENGINE_{flow.upper()}", raising=False)
        assert agent_platform.engine(flow) == "hermes"
        assert agent_platform.chat_target(flow) is None
    monkeypatch.setenv("AGENT_ENGINE_ANALYSIS", "Platform")
    assert agent_platform.engine("analysis") == "platform"
    monkeypatch.setenv("AGENT_ENGINE_ANALYSIS", "something-else")
    assert agent_platform.engine("analysis") == "hermes"


def test_ticket_goes_to_the_platform_without_holding_a_thread(client, agent, platform, writes):
    response = client.post("/api/ticketing/zoho/inbound", json={
        "ticketId": "9301", "ticketNumber": "DR-9301", "subject": "deploy please",
        "description": "Please deploy v9.9.9",
    }, headers=HOOK)
    assert response.status_code == 200

    record = ZohoInboundTicket.query.filter_by(ticket_id="9301").one()
    task = TicketInterpretation.query.filter_by(ticket_record_id=record.id).one()
    # Handed over and waiting for the report, not blocked on a model call.
    assert task.status == "running"
    assert len(platform) == 1
    sent = platform[0]
    assert sent["provider"] == "zoho" and sent["record_id"] == record.id and sent["task_id"] == task.id
    assert sent["kind"] == "handle"
    assert sent["message"]["task"] == "handle_new_ticket"
    assert {e["environment"] for e in sent["message"]["catalog"]} == {NAMESPACE}

    # What the platform's agent does mid-run: act through the ticket tools.
    engine.execute(record.id, _action())

    assert _report(client, task.id, headers={"Authorization": "Bearer wrong"},
                   status="finished").status_code == 401
    response = _report(client, task.id, status="finished", outcome="executed",
                       summary="Deploying v9.9.9.", model="gpt-5.6-sol")
    assert response.status_code == 200, response.get_json()
    assert response.get_json()["data"] == {"status": "executed", "settled": True}

    db.session.expire_all()
    task = db.session.get(TicketInterpretation, task.id)
    assert task.status == "executed"
    assert task.final_message == "Deploying v9.9.9."
    assert task.model == "gpt-5.6-sol"
    assert db.session.get(DeployAutomationRun, task.run_id).triggered_by == "hermes"


def test_a_report_without_acting_is_an_error(client, agent, platform, writes):
    ticket = _ticket()
    engine.queue_ticket(ticket.id)
    task = TicketInterpretation.query.filter_by(ticket_record_id=ticket.id).one()
    assert task.status == "running"

    _report(client, task.id, status="finished", outcome="nothing", summary="not sure")
    db.session.expire_all()
    task = db.session.get(TicketInterpretation, task.id)
    assert task.status == "error"
    assert "without acting" in task.error and "not sure" in task.error
    assert writes == []


def test_a_failed_followup_falls_back_to_kubesight_comment(client, agent, platform, writes):
    ticket = _ticket()
    event = {
        "type": "run_finished",
        "fallback": {"outcome": "deployed", "comment": "Deployed v9.9.9."},
    }
    assert engine.queue_followup(ticket.id, event) is True
    task = TicketInterpretation.query.filter_by(ticket_record_id=ticket.id, kind="followup").one()
    assert task.status == "running"

    _report(client, task.id, status="failed", error="provider exploded")
    db.session.expire_all()
    task = db.session.get(TicketInterpretation, task.id)
    assert task.status == "error"
    assert "provider exploded" in task.error
    # The requester still hears the outcome.
    assert any(w["kind"] == "status" and w["outcome"] == "deployed" for w in writes)


def test_platform_down_retries_later(client, agent, monkeypatch, writes):
    monkeypatch.setenv("AGENT_PLATFORM_URL", PLATFORM_URL)
    monkeypatch.setenv("AGENT_PLATFORM_TOKEN", PLATFORM_TOKEN)
    monkeypatch.setenv("AGENT_ENGINE_TICKETS", "platform")

    def down(**task):
        raise agent_platform.PlatformUnavailable("The agent platform is unreachable or timed out.")

    monkeypatch.setattr(agent_platform, "submit_ticket_task", down)
    ticket = _ticket()
    engine.queue_ticket(ticket.id)
    task = TicketInterpretation.query.filter_by(ticket_record_id=ticket.id).one()
    assert task.status == "pending"
    assert task.retry_at is not None
    assert "unreachable" in task.error and "Retrying" in task.error


def test_agent_is_inactive_until_the_platform_is_configured(app, monkeypatch):
    monkeypatch.setenv("AGENT_ENGINE_TICKETS", "platform")
    monkeypatch.delenv("AGENT_PLATFORM_URL", raising=False)
    agent_settings.update({"enabled": True})
    assert agent_settings.is_active() is False
    assert agent_settings.serialize()["engine"] == "platform"
    assert "AGENT_PLATFORM_URL" in agent_settings.serialize()["platformHint"]
    monkeypatch.setenv("AGENT_PLATFORM_URL", PLATFORM_URL)
    monkeypatch.setenv("AGENT_PLATFORM_TOKEN", PLATFORM_TOKEN)
    assert agent_settings.is_active() is True


def test_unknown_task_and_bad_body_are_refused(client, monkeypatch):
    monkeypatch.setenv("AGENT_PLATFORM_TOKEN", PLATFORM_TOKEN)
    assert _report(client, 999999, status="finished").status_code == 404
    assert _report(client, 1, status="maybe").status_code == 400
    monkeypatch.delenv("AGENT_PLATFORM_TOKEN")
    # No secret configured: nothing is accepted.
    assert _report(client, 1, headers={"Authorization": "Bearer "}, status="finished").status_code == 401


def test_analysis_and_ci_assist_follow_their_switches(monkeypatch):
    from api.services.application_analysis_jobs import _hermes_internal_egress_rule
    from api.services.application_intelligence_hermes import analysis_target
    from api.services.ci_assist import hermes as ci_hermes

    monkeypatch.setenv("HERMES_API_URL", "https://hermes.example.com/v1/chat/completions")
    monkeypatch.setenv("HERMES_API_TOKEN", "hermes-key")
    monkeypatch.setenv("AGENT_PLATFORM_URL", PLATFORM_URL)
    monkeypatch.setenv("AGENT_PLATFORM_TOKEN", PLATFORM_TOKEN)
    monkeypatch.delenv("AGENT_ENGINE_ANALYSIS", raising=False)
    monkeypatch.delenv("AGENT_ENGINE_CI_ASSIST", raising=False)
    assert analysis_target()[0].startswith("https://hermes.example.com")
    assert ci_hermes._target() == ("https://hermes.example.com/v1/chat/completions", "hermes-key")

    monkeypatch.setenv("AGENT_ENGINE_ANALYSIS", "platform")
    monkeypatch.setenv("AGENT_ENGINE_CI_ASSIST", "platform")
    assert analysis_target() == (f"{PLATFORM_URL}/v1/chat/completions", PLATFORM_TOKEN, "analysis")
    assert ci_hermes._target() == (f"{PLATFORM_URL}/v1/chat/completions", PLATFORM_TOKEN)
    assert ci_hermes._model() == "ci_assist"
    assert ci_hermes.is_configured() is True
    # The analysis Job may reach the platform's namespace and port, nothing else in-cluster.
    rule = _hermes_internal_egress_rule()
    assert rule["to"][0]["namespaceSelector"]["matchLabels"] == {
        "kubernetes.io/metadata.name": "kubesight-agents"
    }
    assert rule["ports"] == [{"protocol": "TCP", "port": 8000}]

    monkeypatch.delenv("AGENT_PLATFORM_TOKEN")
    assert ci_hermes.is_configured() is False
    assert "AGENT_PLATFORM_TOKEN" in ci_hermes.configuration_hint()


def test_analysis_job_is_given_the_platform_endpoint(monkeypatch):
    from api.services.application_analysis_jobs import build_job_resources

    monkeypatch.setenv("AGENT_PLATFORM_URL", PLATFORM_URL)
    monkeypatch.setenv("AGENT_PLATFORM_TOKEN", PLATFORM_TOKEN)
    monkeypatch.setenv("AGENT_ENGINE_ANALYSIS", "platform")
    resources = build_job_resources(
        analysis_id=7,
        repository_url="https://bitbucket.example.com/scm/app/repo.git",
        revision="main",
        subdirectory="",
        repository_token="repo-token",
        callback_token="callback-token-123456",
    )
    secret = next(r for r in resources if r["kind"] == "Secret")
    job = next(r for r in resources if r["kind"] == "Job")
    env = {e["name"]: e.get("value") for e in job["spec"]["template"]["spec"]["containers"][0]["env"]}
    assert env["HERMES_API_URL"] == f"{PLATFORM_URL}/v1/chat/completions"
    assert env["HERMES_APPLICATION_MODEL"] == "analysis"
    stored = secret.get("stringData") or secret.get("data") or {}
    assert "hermes-token" in stored


def test_agent_platform_account_is_seeded_with_its_fixed_role(app, monkeypatch):
    token = "ksa_agent_platform_token_0123456789"
    monkeypatch.setenv("KUBESIGHT_AGENT_PLATFORM_MCP_TOKEN", token)
    seed_defaults()
    db.session.commit()
    user = User.query.filter_by(username="agent-platform").one()
    assert user.is_service_account is True
    assert user.interactive_login_enabled is False
    assert {p.key for p in user.role.permissions} == set(AGENT_PLATFORM_PERMISSIONS)
    # Read-only views only, besides the Hermes ticket permissions.
    assert all(
        key.endswith(":view") or key in ("ticketing:agent", "applications:execute")
        for key in AGENT_PLATFORM_PERMISSIONS
    )
    stored = ApiToken.query.filter_by(token_hash=sha256(token.encode()).hexdigest()).one()
    assert stored.user_id == user.id and stored.name == DEPLOYMENT_AGENT_PLATFORM_TOKEN_NAME


def test_agent_platform_token_sees_ticket_writes_and_reads_only(client, app, monkeypatch):
    token = "ksa_agent_platform_token_0123456789"
    monkeypatch.setenv("KUBESIGHT_AGENT_PLATFORM_MCP_TOKEN", token)
    seed_defaults()
    db.session.commit()
    response = client.post(
        "/api/mcp",
        json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
        headers=auth_headers(token),
    )
    tools = response.get_json()["result"]["tools"]
    writes = {t["name"] for t in tools if not t["annotations"]["readOnlyHint"]}
    assert writes and all(name.startswith("kubesight_ticket_") for name in writes)


def test_mcp_audit_records_who_the_agent_acts_for(client, admin_token):
    headers = {**auth_headers(admin_token), "X-KubeSight-On-Behalf-Of": "infra:zoho#4521"}
    client.post(
        "/api/mcp",
        json={"jsonrpc": "2.0", "id": 1, "method": "tools/call",
              "params": {"name": "kubesight_overview", "arguments": {}}},
        headers=headers,
    )
    row = AuditLog.query.filter_by(action="mcp_tools_called").order_by(AuditLog.id.desc()).first()
    assert row.details["onBehalfOf"] == "infra:zoho#4521"


def test_integrations_card_shows_each_flow(client, admin_token, monkeypatch):
    monkeypatch.setenv("AGENT_PLATFORM_URL", PLATFORM_URL)
    monkeypatch.setenv("AGENT_PLATFORM_TOKEN", PLATFORM_TOKEN)
    monkeypatch.setenv("AGENT_ENGINE_TICKETS", "platform")
    monkeypatch.delenv("AGENT_ENGINE_ANALYSIS", raising=False)
    response = client.get("/api/integrations", headers=auth_headers(admin_token))
    items = response.get_json()["data"]
    items = items.get("items", items) if isinstance(items, dict) else items
    card = next(i for i in items if i["key"] == "agent_platform")
    assert "Tickets: platform" in card["message"] and "Source analysis: Hermes" in card["message"]
    assert card["status"] != "not_configured"
