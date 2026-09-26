"""Inbound webhooks fail closed, the legacy /api/ai surface is gone, and the
hermes-agent service account holds exactly what Hermes uses.

* Ticketing webhooks (Zoho + Jira, intake + comment, new and legacy paths)
  refuse every delivery with 403 until an inbound secret is stored, 401 on a
  wrong one, and accept the right one by header or ``?secret=``.
* ``/api/ai/*`` is not routed any more.
* A token minted for ``hermes-agent`` sees the ticket-agent tools over MCP and
  nothing that manages integrations.
"""

import pytest

from api.db import db
from api.models import Permission, Role, User
from api.rbac_data import HERMES_AGENT_PERMISSIONS

from .conftest import auth_headers

ZOHO_ENDPOINTS = (
    "/api/ticketing/zoho/inbound",
    "/api/ticketing/zoho/inbound/comment",
    "/api/zoho/inbound",
    "/api/zoho/inbound/comment",
)
JIRA_ENDPOINTS = (
    "/api/ticketing/jira/inbound",
    "/api/ticketing/jira/inbound/comment",
)


def _body(path):
    # The comment webhooks want a ticket + comment; the intake wants a ticket.
    if path.endswith("/comment"):
        return {"ticketId": "no-such-ticket", "comment": "hello"}
    return {"ticketId": "no-such-ticket", "ticketNumber": "DR-1"}


# ---------------------------------------------------------------------------
# Ticketing webhooks
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("path", ZOHO_ENDPOINTS + JIRA_ENDPOINTS)
def test_ticketing_webhook_is_closed_when_no_secret_is_configured(client, path):
    response = client.post(path, json=_body(path), headers={"X-Ticketing-Secret": "anything"})
    assert response.status_code == 403
    error = response.get_json()["error"]
    assert "No inbound webhook secret is configured" in error
    assert "Inbound webhook" in error


@pytest.mark.parametrize("path", ZOHO_ENDPOINTS)
def test_zoho_webhooks_verify_the_configured_secret(client, app, path):
    from api.services import zoho_sync_service

    zoho_sync_service.update_config({"inboundSecret": "zoho-hook"})
    assert zoho_sync_service.inbound_secret_configured() is True

    assert client.post(path, json=_body(path)).status_code == 401
    assert (
        client.post(path, json=_body(path), headers={"X-Ticketing-Secret": "wrong"}).status_code
        == 401
    )
    # Neutral header, provider header, and query-string fallback all work.
    assert (
        client.post(path, json=_body(path), headers={"X-Ticketing-Secret": "zoho-hook"}).status_code
        == 200
    )
    assert (
        client.post(path, json=_body(path), headers={"X-Zoho-Secret": "zoho-hook"}).status_code
        == 200
    )
    assert client.post(f"{path}?secret=zoho-hook", json=_body(path)).status_code == 200


@pytest.mark.parametrize("path", JIRA_ENDPOINTS)
def test_jira_webhooks_verify_the_configured_secret(client, app, path):
    from api.services import jira_sync_service

    jira_sync_service.update_config({"inboundSecret": "jira-hook"})
    assert jira_sync_service.inbound_secret_configured() is True

    assert client.post(path, json=_body(path)).status_code == 401
    assert (
        client.post(path, json=_body(path), headers={"X-Jira-Secret": "wrong"}).status_code == 401
    )
    assert (
        client.post(path, json=_body(path), headers={"X-Jira-Secret": "jira-hook"}).status_code
        == 200
    )
    assert client.post(f"{path}?secret=jira-hook", json=_body(path)).status_code == 200


def test_one_providers_secret_does_not_open_the_other(client, app):
    from api.services import zoho_sync_service

    zoho_sync_service.update_config({"inboundSecret": "zoho-hook"})
    response = client.post(
        "/api/ticketing/jira/inbound",
        json=_body("/api/ticketing/jira/inbound"),
        headers={"X-Ticketing-Secret": "zoho-hook"},
    )
    assert response.status_code == 403


def test_verify_inbound_secret_is_false_without_a_stored_secret(app):
    from api.services import jira_sync_service, zoho_sync_service

    for sync in (zoho_sync_service, jira_sync_service):
        assert sync.inbound_secret_configured() is False
        assert sync.verify_inbound_secret(None) is False
        assert sync.verify_inbound_secret("") is False
        assert sync.verify_inbound_secret("anything") is False


def test_clearing_the_secret_closes_the_webhook_again(client, app):
    from api.services import zoho_sync_service

    zoho_sync_service.update_config({"inboundSecret": "zoho-hook"})
    zoho_sync_service.update_config({"clearInboundSecret": True})
    response = client.post(
        "/api/ticketing/zoho/inbound",
        json=_body("/api/ticketing/zoho/inbound"),
        headers={"X-Ticketing-Secret": "zoho-hook"},
    )
    assert response.status_code == 403


# ---------------------------------------------------------------------------
# Legacy /api/ai
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "path",
    ["/api/ai/health", "/api/ai/snapshot", "/api/ai/cluster-summary", "/api/ai/unhealthy-pods"],
)
def test_legacy_ai_endpoints_are_gone(client, admin_token, path):
    assert client.get(path).status_code == 404
    assert client.get(path, headers=auth_headers(admin_token)).status_code == 404


# ---------------------------------------------------------------------------
# hermes-agent: least privilege that is actually usable
# ---------------------------------------------------------------------------

TICKET_AGENT_WRITE_TOOLS = {
    "kubesight_ticket_execute",
    "kubesight_ticket_request_approval",
    "kubesight_ticket_set_status",
    "kubesight_ticket_comment",
}


def _hermes_token(client, app, admin_token):
    with app.app_context():
        hermes_id = User.query.filter_by(username="hermes-agent").one().id
    response = client.post(
        "/api/auth/tokens",
        json={"name": "hermes-mcp", "user_id": hermes_id},
        headers=auth_headers(admin_token),
    )
    assert response.status_code == 201, response.get_json()
    return response.get_json()["data"]["token"]


def _tool_names(client, token):
    response = client.post(
        "/api/mcp",
        json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
        headers=auth_headers(token),
    )
    assert response.status_code == 200
    return {entry["name"] for entry in response.get_json()["result"]["tools"]}


def test_hermes_agent_role_is_the_ticket_agent_and_nothing_that_manages(app):
    assert set(HERMES_AGENT_PERMISSIONS) == {
        "applications:execute",
        "ticketing:view",
        "ticketing:agent",
    }
    assert "ticketing:manage" not in HERMES_AGENT_PERMISSIONS
    with app.app_context():
        hermes = User.query.filter_by(username="hermes-agent").one()
        assert {p.key for p in hermes.role.permissions} == set(HERMES_AGENT_PERMISSIONS)


def test_a_hermes_agent_token_sees_exactly_the_ticket_tools(client, app, admin_token):
    names = _tool_names(client, _hermes_token(client, app, admin_token))
    assert TICKET_AGENT_WRITE_TOOLS <= names
    assert {"kubesight_ticket_get", "kubesight_tickets_list"} <= names
    # Integration management and every other domain stay out of reach.
    assert "kubesight_automation_run_start" not in names
    assert "kubesight_automation_run_cancel" not in names
    assert not {n for n in names if n.startswith(("kubesight_cluster", "kubesight_pod", "kubesight_ci_"))}


def test_a_hermes_agent_token_cannot_rewrite_the_ticketing_integration(client, app, admin_token):
    token = _hermes_token(client, app, admin_token)
    response = client.put(
        "/api/ticketing/zoho/config",
        json={"inboundSecret": "stolen"},
        headers=auth_headers(token),
    )
    assert response.status_code in (401, 403)


def test_admin_tokens_keep_the_ticket_tools(client, admin_token):
    assert TICKET_AGENT_WRITE_TOOLS <= _tool_names(client, admin_token)


def test_ticket_agent_permission_is_granted_once_to_ticket_managers(app):
    """Existing roles that held ticketing:manage keep the ticket tools after the
    tools moved to ticketing:agent — once, so a later removal sticks."""
    from api.migrate_rbac import _grant_ticket_agent_to_ticket_managers

    with app.app_context():
        manage = Permission.query.filter_by(key="ticketing:manage").one()
        existing = Permission.query.filter_by(key="ticketing:agent").one()
        # Simulate an installation from before the key existed.
        for role in Role.query.all():
            role.permissions = [p for p in role.permissions if p.key != "ticketing:agent"]
        db.session.delete(existing)
        custom = Role(name="ticket-ops", description="custom", is_system_role=False)
        custom.permissions = [manage]
        db.session.add(custom)
        db.session.commit()

        _grant_ticket_agent_to_ticket_managers()
        custom = Role.query.filter_by(name="ticket-ops").one()
        assert "ticketing:agent" in {p.key for p in custom.permissions}

        # An operator later takes it away on purpose: the next start leaves it.
        custom.permissions = [p for p in custom.permissions if p.key != "ticketing:agent"]
        db.session.commit()
        _grant_ticket_agent_to_ticket_managers()
        custom = Role.query.filter_by(name="ticket-ops").one()
        assert "ticketing:agent" not in {p.key for p in custom.permissions}
