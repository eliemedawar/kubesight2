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


def test_module_goes_in_the_query_and_never_in_the_body(wire):
    """Both halves verified live against Desk (2026-07-27): absent from the URL
    it 422s "The mandatory parameter 'module' is missing"; present in the body it
    422s "An extra parameter 'module' is found"."""
    zoho_client.create_org_field(CFG, {"module": "tickets", "displayLabel": "Region",
                                       "type": "Picklist", "layoutId": CFG.layout_id})

    call = wire[0]
    assert call["method"] == "POST"
    assert _query(call["url"]) == {"orgId": "854214247", "module": "tickets"}
    assert "module" not in call["body"]


def test_a_rejected_body_property_is_dropped_and_retried(wire, monkeypatch):
    """Which properties Desk accepts varies by field type and Desk version, and
    the error names the offender — so clear it rather than cost a deploy cycle."""
    calls = []

    def _request(method, url, *, headers=None, body=None):
        parsed = json.loads(body.decode())
        calls.append(parsed)
        for key in ("subType", "layoutId"):
            if key in parsed:
                return 422, {"errorCode": "UNPROCESSABLE_ENTITY",
                             "message": f"An extra parameter '{key}' is found."}
        return 200, {"id": "303"}

    monkeypatch.setattr(zoho_client, "_request", _request)
    created = zoho_client.create_org_field(
        CFG, {"displayLabel": "Region", "type": "Text", "subType": "x", "layoutId": "L1"}
    )

    assert created == {"id": "303"}
    assert [sorted(c) for c in calls] == [
        ["displayLabel", "layoutId", "subType", "type"],
        ["displayLabel", "layoutId", "type"],
        ["displayLabel", "type"],
    ]


def test_a_non_schema_error_is_raised_not_retried(wire, monkeypatch):
    def _request(method, url, *, headers=None, body=None):
        return 403, {"message": "You do not have permission"}

    monkeypatch.setattr(zoho_client, "_request", _request)
    with pytest.raises(zoho_client.ZohoError) as exc:
        zoho_client.create_org_field(CFG, {"displayLabel": "Region", "type": "Text"})
    assert exc.value.status == 403


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


def test_layout_patch_keeps_module_in_body_not_query(wire):
    zoho_client.update_layout(CFG, {"module": "tickets", "layoutName": "DevOps Request",
                                    "departmentId": "d1", "isDefaultLayout": False,
                                    "sections": []})
    call = wire[0]
    assert call["method"] == "PATCH"
    assert _query(call["url"]) == {"orgId": "854214247"}
    assert call["body"]["module"] == "tickets"


def test_layout_patch_drops_only_decorative_rejected_keys(wire, monkeypatch):
    calls = []

    def _request(method, url, *, headers=None, body=None):
        parsed = json.loads(body.decode())
        calls.append(parsed)
        if "layoutDesc" in parsed:
            return 422, {"message": "An extra parameter 'layoutDesc' is found."}
        return 200, {"id": "L1"}

    monkeypatch.setattr(zoho_client, "_request", _request)
    zoho_client.update_layout(CFG, {"layoutName": "DevOps Request", "departmentId": "d1",
                                    "isDefaultLayout": False, "layoutDesc": "x", "sections": []})
    assert len(calls) == 2 and "layoutDesc" not in calls[1]


def test_layout_patch_never_drops_an_identity_key(wire, monkeypatch):
    """Omitting departmentId or isDefaultLayout could move the layout between
    departments or demote the department's default — fail loudly instead."""

    def _request(method, url, *, headers=None, body=None):
        return 422, {"message": "An extra parameter 'isDefaultLayout' is found."}

    monkeypatch.setattr(zoho_client, "_request", _request)
    with pytest.raises(zoho_client.ZohoError, match="isDefaultLayout"):
        zoho_client.update_layout(CFG, {"layoutName": "L", "departmentId": "d1",
                                        "isDefaultLayout": False, "sections": []})


def test_layout_writes_are_pinned_to_the_allowed_layout(wire, monkeypatch):
    """The master rule: even a token with full scope may only touch this layout."""
    monkeypatch.setattr(zoho_client, "_allowed_layout_ids", lambda: {"some-other-layout"})
    with pytest.raises(zoho_client.ZohoError):
        zoho_client.create_org_field(CFG, {"module": "tickets", "displayLabel": "X"})
    assert wire == []
