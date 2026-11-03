"""One-click inbound webhook setup in Zoho Desk and Jira.

* Setup creates the two webhooks (tickets + comments) with the secret in the
  URL, mints a secret only when none is set, and adopts an existing webhook
  (same URL, an older address, the legacy /api/zoho path) instead of adding a
  second one.
* A loopback address is refused before anything is sent, unless a gateway base
  URL is set — and then the gateway's address is what gets registered.
* Changing the secret moves the registered webhooks to the new one.
* The inbound routes answer Desk's validation GET and understand the native
  Desk event list (layout-filtered) and Jira's comment_created body.
"""

import json

import pytest

from api.services import jira_client, zoho_client, zoho_sync_service, jira_sync_service
from api.services import ticketing
from api.services.ticketing import webhook_setup

from .conftest import auth_headers

PUBLIC = "https://kubesight.example.com"


@pytest.fixture()
def desk(app, monkeypatch):
    """A fake Desk webhook store behind the client functions."""
    monkeypatch.setenv("PUBLIC_BASE_URL", PUBLIC)
    hooks = []
    calls = []

    def _list(cfg):
        return [dict(h) for h in hooks]

    def _create(cfg, body):
        calls.append(("create", body))
        hook = {"id": str(100 + len(hooks)), **body}
        hooks.append(hook)
        return hook

    def _update(cfg, hook_id, body):
        calls.append(("update", hook_id, body))
        for hook in hooks:
            if hook["id"] == hook_id:
                hook.update(body)
        return {}

    monkeypatch.setattr(zoho_client, "list_webhooks", _list)
    monkeypatch.setattr(zoho_client, "create_webhook", _create)
    monkeypatch.setattr(zoho_client, "update_webhook", _update)
    zoho_sync_service.update_config({"departmentId": "555", "layoutId": "777"})
    return {"hooks": hooks, "calls": calls}


def _zoho():
    return ticketing.get("zoho")


def _secret_in(url):
    return url.split("secret=", 1)[1]


def test_setup_creates_both_desk_webhooks_and_mints_a_secret(desk):
    result = webhook_setup.setup(_zoho())

    assert result["ok"] is True
    assert result["secretGenerated"] is True
    assert [w["action"] for w in result["webhooks"]] == ["created", "created"]
    tickets, comments = desk["hooks"]
    assert tickets["url"].startswith(f"{PUBLIC}/api/ticketing/zoho/inbound?secret=")
    assert comments["url"].startswith(f"{PUBLIC}/api/ticketing/zoho/inbound/comment?secret=")
    assert tickets["subscriptions"] == {"Ticket_Add": {"departmentIds": ["555"]}}
    assert comments["subscriptions"]["Ticket_Thread_Add"] == {"departmentIds": ["555"], "direction": "in"}
    # The minted secret is the one stored, and the inbound route accepts it.
    assert zoho_sync_service.verify_inbound_secret(_secret_in(tickets["url"]))
    # Responses never carry it.
    assert "••••" in result["webhooks"][0]["url"]
    assert _secret_in(tickets["url"]) not in json.dumps(result)
    # The ignoreSourceId is what KubeSight sends on its own Desk calls.
    row = zoho_sync_service.get_or_create_config()
    assert tickets["ignoreSourceId"] == row.webhook_source_id
    assert zoho_sync_service._to_client_config(row).source_id == row.webhook_source_id


def test_setup_keeps_an_existing_secret_and_is_idempotent(desk):
    zoho_sync_service.update_config({"inboundSecret": "kept-secret"})
    first = webhook_setup.setup(_zoho())
    assert first["secretGenerated"] is False
    assert _secret_in(desk["hooks"][0]["url"]) == "kept-secret"

    second = webhook_setup.setup(_zoho())
    assert [w["action"] for w in second["webhooks"]] == ["unchanged", "unchanged"]
    assert len(desk["hooks"]) == 2
    assert second["status"]["inSync"] is True


def test_a_hand_made_legacy_webhook_is_adopted_not_duplicated(desk):
    zoho_sync_service.update_config({"inboundSecret": "s1"})
    desk["hooks"].append({
        "id": "9", "name": "old", "url": "https://old.example.com/api/zoho/inbound?secret=s1",
        "subscriptions": {"Ticket_Add": None}, "isEnabled": True,
    })
    desk["hooks"].append({
        "id": "10", "name": "stray", "url": "https://x.example.com/api/ticketing/zoho/inbound?secret=zz",
        "subscriptions": {"Ticket_Add": None}, "isEnabled": True,
    })
    status = webhook_setup.status(_zoho())
    assert status["webhooks"][0]["id"] == "9"
    assert status["webhooks"][0]["inSync"] is False
    # The second webhook calling the tickets URL is not ours; it is listed.
    assert [o["id"] for o in status["others"]] == ["10"]

    result = webhook_setup.setup(_zoho())
    assert result["webhooks"][0] == {**result["webhooks"][0], "action": "updated", "id": "9"}
    assert desk["hooks"][0]["url"] == f"{PUBLIC}/api/ticketing/zoho/inbound?secret=s1"


def test_ticket_updates_are_opt_in(desk):
    zoho_sync_service.update_config({"webhookSendUpdates": True})
    webhook_setup.setup(_zoho())
    assert set(desk["hooks"][0]["subscriptions"]) == {"Ticket_Add", "Ticket_Update"}


def test_a_loopback_address_is_refused_unless_a_gateway_is_set(desk, monkeypatch):
    monkeypatch.setenv("PUBLIC_BASE_URL", "http://localhost:5000")
    with pytest.raises(webhook_setup.SetupError, match="gateway"):
        webhook_setup.setup(_zoho())
    assert desk["calls"] == []

    zoho_sync_service.update_config({"webhookBaseUrl": "https://tyk.example.com/kubesight/"})
    webhook_setup.setup(_zoho())
    assert desk["hooks"][0]["url"].startswith("https://tyk.example.com/kubesight/api/ticketing/zoho/inbound?")


def test_a_bad_gateway_url_is_refused_on_save(app):
    with pytest.raises(ValueError, match="gateway base URL"):
        zoho_sync_service.update_config({"webhookBaseUrl": "tyk.example.com"})


def test_changing_the_secret_moves_the_registered_webhooks(desk, client, admin_token):
    webhook_setup.setup(_zoho())
    res = client.put(
        "/api/ticketing/zoho/config", json={"inboundSecret": "rotated"}, headers=auth_headers(admin_token)
    )
    assert res.status_code == 200
    resync = res.get_json()["data"]["webhookResync"]
    assert resync["ok"] is True
    assert {_secret_in(h["url"]) for h in desk["hooks"]} == {"rotated"}


def test_a_scope_error_from_desk_is_reported(desk, monkeypatch):
    def _refuse(cfg):
        raise zoho_client._webhook_error("Listing Zoho webhooks failed", 403, {"message": "no scope"})

    monkeypatch.setattr(zoho_client, "list_webhooks", _refuse)
    status = webhook_setup.status(_zoho())
    assert status["known"] is False
    assert "Desk.webhooks" in status["reason"]
    with pytest.raises(webhook_setup.SetupError, match="re-mint"):
        webhook_setup.setup(_zoho())


def test_setup_routes_are_permission_gated(desk, client, admin_token):
    assert client.post("/api/ticketing/zoho/webhooks/setup").status_code == 401
    res = client.post("/api/ticketing/zoho/webhooks/setup", headers=auth_headers(admin_token))
    assert res.status_code == 200
    assert res.get_json()["data"]["ok"] is True
    res = client.get("/api/ticketing/zoho/webhooks", headers=auth_headers(admin_token))
    assert res.get_json()["data"]["inSync"] is True


# ---------------------------------------------------------------------------
# Inbound
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "path",
    ("/api/ticketing/zoho/inbound", "/api/ticketing/zoho/inbound/comment",
     "/api/zoho/inbound", "/api/zoho/inbound/comment", "/api/ticketing/jira/inbound"),
)
def test_desk_validation_get_answers_200(client, path):
    assert client.get(path).status_code == 200


def test_a_desk_event_list_is_resolved_per_ticket_on_the_layout_only(desk, client):
    zoho_sync_service.update_config({"inboundSecret": "s"})
    body = [
        {"eventType": "Ticket_Add", "orgId": "1", "payload": {
            "id": "T-1", "ticketNumber": "101", "subject": "deploy", "layoutId": "777",
            "cf": {"cf_application": "aims-ui", "cf_environment": "dev", "cf_tag": "1.2"},
        }},
        {"eventType": "Ticket_Add", "payload": {"id": "T-2", "layoutId": "other"}},
        {"eventType": "Contact_Add", "payload": {"id": "C-1"}},
    ]
    res = client.post("/api/ticketing/zoho/inbound?secret=s", json=body)
    assert res.status_code == 200
    data = res.get_json()["data"]
    assert [t["tag"] for t in data["tickets"]] == ["1.2"]
    assert {s["reason"] for s in data["skipped"]} == {"Another layout.", "Not a ticket event."}
    from api.models import ZohoInboundTicket

    stored = ZohoInboundTicket.query.filter_by(ticket_id="T-1").one()
    assert stored.ticket_number == "101"
    assert ZohoInboundTicket.query.filter_by(ticket_id="T-2").first() is None


def test_a_flat_body_still_answers_as_one_ticket(desk, client):
    zoho_sync_service.update_config({"inboundSecret": "s"})
    res = client.post("/api/zoho/inbound?secret=s", json={"ticketId": "T-9", "ticketNumber": "9"})
    assert res.status_code == 200
    assert "tickets" not in res.get_json()["data"]


def test_jira_comment_created_is_parsed():
    from api.services.ticket_agent.engine import parse_comment_webhook

    event = {
        "webhookEvent": "comment_created",
        "issue": {"id": "10001", "key": "KUB-7", "fields": {"status": {"name": "On Hold"}}},
        "comment": {
            "id": "555", "author": {"displayName": "Requester"},
            "body": {"type": "doc", "content": [
                {"type": "paragraph", "content": [{"type": "text", "text": "use tag 1.3"}]},
            ]},
        },
    }
    assert parse_comment_webhook(event) == [{
        "ticketId": "KUB-7", "text": "use tag 1.3", "author": "Requester",
        "commentId": "555", "status": "On Hold",
    }]
    assert parse_comment_webhook({**event, "webhookEvent": "comment_updated"}) == []


def test_a_jira_comment_event_on_the_tickets_url_is_skipped():
    tickets, skipped = webhook_setup.unwrap_ticket_delivery("jira", {"webhookEvent": "comment_created"})
    assert tickets == [] and skipped[0]["reason"] == "Not an issue event."


# ---------------------------------------------------------------------------
# Jira
# ---------------------------------------------------------------------------

def test_jira_setup_registers_project_filtered_admin_webhooks(app, monkeypatch):
    monkeypatch.setenv("PUBLIC_BASE_URL", PUBLIC)
    jira_sync_service.update_config({"projectKey": "KUB", "baseUrl": "https://acme.atlassian.net"})
    hooks = []

    def _create(cfg, body):
        hook = {**body, "self": f"https://acme.atlassian.net/rest/webhooks/1.0/webhook/{len(hooks) + 1}",
                "enabled": True}
        hooks.append(hook)
        return hook

    monkeypatch.setattr(jira_client, "list_webhooks", lambda cfg: [dict(h) for h in hooks])
    monkeypatch.setattr(jira_client, "create_webhook", _create)
    monkeypatch.setattr(jira_client, "update_webhook", lambda cfg, i, b: {})

    result = webhook_setup.setup(ticketing.get("jira"))
    assert [w["id"] for w in result["webhooks"]] == ["1", "2"]
    assert hooks[0]["events"] == ["jira:issue_created"]
    assert hooks[1]["events"] == ["comment_created"]
    assert hooks[0]["filters"] == {"issue-related-events-section": 'project = "KUB"'}
    assert result["status"]["inSync"] is True
