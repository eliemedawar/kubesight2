"""What KubeSight actually puts on the wire to Zoho Desk.

Everything above ``zoho_client`` stubs the client out, so the request shape —
URL, query string, body — has no other coverage. These tests exist because a
missing QUERY parameter took down "Add a field" in production while the JSON
body looked perfectly correct.
"""

import json

import pytest

from api.services import zoho_client
from api.services.zoho_client import ZohoConfig

CFG = ZohoConfig(
    api_base="https://desk.zoho.com/api/v1",
    accounts_base="https://accounts.zoho.com",
    token_endpoint="https://accounts.zoho.com/oauth/v2/token",
    org_id="854214247",
    layout_id="999149000010342586",
    app_field_id="9001",
    client_id="c",
    client_secret="s",
    refresh_token="r",
)


@pytest.fixture()
def wire(monkeypatch):
    """Capture the calls ``zoho_client`` would make, and answer them 200."""
    calls = []

    def _request(method, url, *, headers=None, body=None):
        calls.append(
            {
                "method": method,
                "url": url,
                "body": json.loads(body.decode()) if body else None,
            }
        )
        return 200, {"id": "303", "apiName": "cf_region"}

    monkeypatch.setattr(zoho_client, "_request", _request)
    monkeypatch.setattr(zoho_client, "get_access_token", lambda cfg, force=False: "tok")
    monkeypatch.setattr(zoho_client, "_allowed_layout_ids", lambda: None)
    return calls


def _query(url: str) -> dict:
    from urllib.parse import parse_qs, urlparse

    return {k: v[0] for k, v in parse_qs(urlparse(url).query).items()}


def test_create_field_sends_module_in_the_query_string(wire):
    """Desk 422s "The mandatory parameter 'module' is missing" without it —
    having it in the JSON body alone is not enough."""
    zoho_client.create_org_field(CFG, {"module": "tickets", "displayLabel": "Region",
                                       "type": "Picklist", "layoutId": CFG.layout_id})

    call = wire[0]
    assert call["method"] == "POST"
    assert _query(call["url"]) == {"orgId": "854214247", "module": "tickets"}
    # Still in the body too — Desk's own create example carries it there.
    assert call["body"]["module"] == "tickets"


def test_update_field_sends_module_without_touching_the_body(wire):
    zoho_client.update_org_field(CFG, "303", {"displayLabel": "Region (new)"})

    call = wire[0]
    assert call["method"] == "PATCH"
    assert call["url"].startswith("https://desk.zoho.com/api/v1/organizationFields/303?")
    assert _query(call["url"])["module"] == "tickets"
    # The caller's body is sent verbatim — an unexpected property is its own 422.
    assert call["body"] == {"displayLabel": "Region (new)"}


def test_set_allowed_values_targets_the_layout_field(wire):
    zoho_client.set_allowed_values(
        CFG, ["-None-", "a"], field_id="9002", default_value="-None-", is_mandatory=True
    )

    call = wire[0]
    assert call["method"] == "PATCH"
    assert "/layouts/999149000010342586/fields/9002?" in call["url"]
    assert _query(call["url"]) == {"orgId": "854214247", "isMandatory": "true"}
    assert call["body"] == {
        "allowedValues": ["-None-", "a"],
        "defaultValue": "-None-",
        "sortBy": "userDefined",
        "isMandatory": True,
    }


def test_unassociate_posts_to_the_layout_field(wire):
    zoho_client.unassociate_field(CFG, "303")

    call = wire[0]
    assert call["method"] == "POST"
    assert "/layouts/999149000010342586/fields/303/unassociate?" in call["url"]
    assert _query(call["url"]) == {"orgId": "854214247"}


def test_layout_writes_are_pinned_to_the_allowed_layout(wire, monkeypatch):
    """The master rule: even a token with full scope may only touch this layout."""
    monkeypatch.setattr(zoho_client, "_allowed_layout_ids", lambda: {"some-other-layout"})
    with pytest.raises(zoho_client.ZohoError):
        zoho_client.create_org_field(CFG, {"module": "tickets", "displayLabel": "X"})
    assert wire == []
