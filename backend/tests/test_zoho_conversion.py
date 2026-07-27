"""Text → Picklist conversion.

Zoho cannot change a field's type in place, so "converting" `cf_tag` means a NEW
field with a NEW api name — and every KubeSight setting plus the Desk webhook
still keyed on `cf_tag` goes quietly stale. Most of these tests are about that
warning surface, because the silent half is what actually breaks deploys.
"""

import copy
import json

import pytest

from api.db import db
from api.models import ZohoIntegration, ZohoLayoutSnapshot
from api.services import zoho_client
from api.services import zoho_fields_service as fields_svc
from api.services import zoho_sync_service as svc

from .conftest import auth_headers

LAYOUT_ID = "999149000010342586"
TAG_FIELD = "202"
ENV_FIELD = "201"


def _layout():
    return {
        "id": LAYOUT_ID,
        "name": "DevOps Request",
        "layoutName": "DevOps Request",
        "departmentId": "dept-1",
        "isDefaultLayout": False,
        "module": "tickets",
        "sections": [
            {
                "id": 1,
                "name": "Deployment Information",
                "i18NLabel": "Deployment Information",
                "fields": [
                    {
                        "id": ENV_FIELD, "apiName": "cf_environment", "type": "Picklist",
                        "displayLabel": "Environment", "isMandatory": False,
                        "allowedValues": ["-None-", "payments"], "defaultValue": "-None-",
                        "sortBy": "userDefined", "isNested": False,
                    },
                    {
                        "id": TAG_FIELD, "apiName": "cf_tag", "type": "Text",
                        "displayLabel": "Tag", "isMandatory": False,
                    },
                    {
                        "id": "203", "apiName": "cf_note", "type": "Text",
                        "displayLabel": "Note", "isMandatory": False,
                    },
                ],
            }
        ],
    }


@pytest.fixture()
def zoho(app, monkeypatch):
    row = ZohoIntegration.query.get(1) or ZohoIntegration(id=1)
    row.enabled = True
    row.layout_id = LAYOUT_ID
    row.department_id = "dept-1"
    row.org_id = "org-1"
    row.environment_field_id = ENV_FIELD
    row.tag_field_api_name = "cf_tag"
    row.custom_environments = json.dumps(
        [{"name": "POS-UAT", "applications": ["pos"], "jenkinsJobPath": "pos-deploy",
          "jenkinsParams": {"repotag": "{cf_tag}", "msName": "{app}"}}]
    )
    row.job_overrides = json.dumps(
        [{"namespace": "common-dev", "deployments": ["persona-ms"],
          "jenkinsJobPath": "f/persona", "jenkinsParams": {"TAG": "{cf_tag}"}}]
    )
    db.session.add(row)
    db.session.commit()

    state = {"layout": _layout(), "patches": [], "created": [], "unassociated": []}

    def _create(cfg, body):
        new = {
            "id": "303",
            "apiName": "cf_tag_1",  # Zoho mints a NEW api name — the whole problem
            "displayLabel": body["displayLabel"],
            "type": body["type"],
            "isMandatory": body.get("isMandatory", False),
            "allowedValues": body.get("allowedValues") or ["-None-"],
            "defaultValue": "-None-",
            "sortBy": "userDefined",
            "isNested": False,
        }
        state["created"].append(copy.deepcopy(body))
        state["layout"]["sections"][0]["fields"].append(new)
        return new

    def _unassociate(cfg, field_id):
        state["unassociated"].append(str(field_id))
        state["layout"]["sections"][0]["fields"] = [
            f for f in state["layout"]["sections"][0]["fields"]
            if str(f["id"]) != str(field_id)
        ]
        return {}

    monkeypatch.setattr(zoho_client, "get_layout", lambda cfg, fresh=False: copy.deepcopy(state["layout"]))
    monkeypatch.setattr(
        zoho_client, "field_on_layout",
        lambda cfg, field_id: next(
            (copy.deepcopy(f) for f in state["layout"]["sections"][0]["fields"]
             if str(f["id"]) == str(field_id)), None
        ),
    )
    monkeypatch.setattr(zoho_client, "create_org_field", _create)
    monkeypatch.setattr(zoho_client, "unassociate_field", _unassociate)
    monkeypatch.setattr(
        zoho_client, "update_layout",
        lambda cfg, body: state["patches"].append(copy.deepcopy(body)) or {"id": LAYOUT_ID},
    )
    monkeypatch.setenv("ZOHO_LAYOUT_WRITE_ENABLED", "true")
    return state


def _convert(client, token, field_id=TAG_FIELD, **payload):
    return client.post(
        f"/api/zoho/fields/{field_id}/convert", json=payload, headers=auth_headers(token)
    )


def _warning_with(data, needle):
    return next((w for w in data["warnings"] if needle in w), None)


# ---------------------------------------------------------------------------
# The dry run
# ---------------------------------------------------------------------------

def test_plan_reports_the_impact_without_creating_anything(zoho, client, admin_token):
    resp = client.get(f"/api/zoho/fields/{TAG_FIELD}/convert", headers=auth_headers(admin_token))
    assert resp.status_code == 200
    data = resp.get_json()["data"]

    assert data["field"]["apiName"] == "cf_tag"
    assert data["sectionName"] == "Deployment Information"
    # "Tag" is taken by the field being converted, which keeps its label.
    assert data["suggestedLabel"] == "Tag (list)"
    assert [k["key"] for k in data["impact"]["configKeys"]] == ["tagFieldApiName"]
    assert {p["param"] for p in data["impact"]["jenkinsParams"]} == {"repotag", "TAG"}
    assert zoho["created"] == []


def test_plan_rejects_a_field_that_is_already_a_dropdown(zoho, client, admin_token):
    resp = client.get(f"/api/zoho/fields/{ENV_FIELD}/convert", headers=auth_headers(admin_token))
    assert resp.status_code == 400
    assert "already a dropdown" in resp.get_json()["error"]


def test_plan_unknown_field_is_404(zoho, client, admin_token):
    assert client.get(
        "/api/zoho/fields/999/convert", headers=auth_headers(admin_token)
    ).status_code == 404


# ---------------------------------------------------------------------------
# Converting
# ---------------------------------------------------------------------------

def test_convert_creates_a_picklist_beside_the_old_field(zoho, client, admin_token):
    resp = _convert(client, admin_token, label="Tag (list)", values=["v1.2.3", "latest"])
    assert resp.status_code == 201, resp.get_json()
    data = resp.get_json()["data"]

    body = zoho["created"][0]
    assert body["type"] == "Picklist"
    assert body["module"] == "tickets"
    assert body["allowedValues"] == ["-None-", "v1.2.3", "latest"]
    # Placed directly after the field it replaces, so the form does not reshuffle.
    ids = [f["id"] for f in zoho["patches"][0]["sections"][0]["fields"]]
    assert ids == [ENV_FIELD, TAG_FIELD, "303", "203"]
    assert data["newField"]["id"] == "303"


def test_the_new_api_name_is_the_headline_warning(zoho, client, admin_token):
    data = _convert(client, admin_token, label="Tag (list)").get_json()["data"]

    headline = data["warnings"][0]
    assert "cf_tag_1" in headline and "cf_tag" in headline
    assert "webhook" in headline


def test_warns_about_every_config_key_left_pointing_at_the_old_field(zoho, client, admin_token):
    data = _convert(client, admin_token, label="Tag (list)").get_json()["data"]

    stale = _warning_with(data, "Still pointing at the old field")
    assert stale and "tagFieldApiName" in stale
    assert data["repointed"] == []
    assert svc.get_or_create_config().tag_field_api_name == "cf_tag"


def test_repoint_moves_the_config_keys_and_says_so(zoho, client, admin_token):
    data = _convert(client, admin_token, label="Tag (list)", repointConfig=True).get_json()["data"]

    assert data["repointed"] == ["tagFieldApiName"]
    assert svc.get_or_create_config().tag_field_api_name == "cf_tag_1"
    assert _warning_with(data, "Still pointing at the old field") is None
    assert _warning_with(data, "repointed at the new field")


def test_jenkins_param_tokens_are_reported_but_never_rewritten(zoho, client, admin_token):
    """The Jenkins job on the other side may still expect the old parameter, so
    editing an operator's param map is not KubeSight's call."""
    data = _convert(client, admin_token, label="Tag (list)", repointConfig=True).get_json()["data"]

    assert _warning_with(data, "custom environment “POS-UAT”")
    assert _warning_with(data, "job override “common-dev/persona-ms”")
    row = svc.get_or_create_config()
    assert json.loads(row.custom_environments)[0]["jenkinsParams"]["repotag"] == "{cf_tag}"
    assert json.loads(row.job_overrides)[0]["jenkinsParams"]["TAG"] == "{cf_tag}"


def test_old_field_is_kept_by_default_and_says_why(zoho, client, admin_token):
    data = _convert(client, admin_token, label="Tag (list)").get_json()["data"]

    assert data["retired"] is False
    assert zoho["unassociated"] == []
    assert _warning_with(data, "still on the form")


def test_retire_unassociates_the_old_field_after_snapshotting(zoho, client, admin_token):
    data = _convert(client, admin_token, label="Tag (list)", retireOld=True).get_json()["data"]

    assert data["retired"] is True
    assert zoho["unassociated"] == [TAG_FIELD]
    # unassociate bypasses the whole-layout writer's guards, so the snapshot is
    # the only way back.
    assert ZohoLayoutSnapshot.query.filter_by(reason="field_conversion").count() == 1


def test_retire_is_refused_while_layout_writes_are_off(zoho, client, admin_token, monkeypatch):
    monkeypatch.delenv("ZOHO_LAYOUT_WRITE_ENABLED", raising=False)
    data = _convert(client, admin_token, label="Tag (list)", retireOld=True).get_json()["data"]

    assert data["retired"] is False
    assert zoho["unassociated"] == []
    assert _warning_with(data, "ZOHO_LAYOUT_WRITE_ENABLED")


def test_a_failed_retire_still_reports_the_new_field(zoho, client, admin_token, monkeypatch):
    def _boom(cfg, field_id):
        raise zoho_client.ZohoError("field is used by a workflow", 422)

    monkeypatch.setattr(zoho_client, "unassociate_field", _boom)
    resp = _convert(client, admin_token, label="Tag (list)", retireOld=True)

    assert resp.status_code == 201
    data = resp.get_json()["data"]
    assert data["newField"]["id"] == "303" and data["retired"] is False
    assert _warning_with(data, "could not be removed")


def test_duplicate_label_is_rejected_before_anything_is_created(zoho, client, admin_token):
    resp = _convert(client, admin_token, label="Note")
    assert resp.status_code == 400
    assert "unique field labels" in resp.get_json()["error"]
    assert zoho["created"] == []


def test_org_wide_label_collision_is_explained_not_a_502(zoho, client, admin_token, monkeypatch):
    """Labels are unique across the whole Desk org, so a field on ANOTHER layout
    can collide — invisible to the pre-check, and a bare 502 says nothing."""

    def _boom(cfg, body):
        raise zoho_client.ZohoError(
            "POST organizationFields failed (HTTP 422): duplicate display label", 422
        )

    monkeypatch.setattr(zoho_client, "create_org_field", _boom)
    resp = _convert(client, admin_token, label="Tag (list)")

    assert resp.status_code == 400
    assert "unique across the whole Desk org" in resp.get_json()["error"]


def test_label_defaults_to_a_free_variant(zoho, client, admin_token):
    _convert(client, admin_token)
    assert zoho["created"][0]["displayLabel"] == "Tag (list)"


def test_convert_can_bind_the_new_dropdown_to_a_live_source(zoho, client, admin_token):
    """The point of converting: the replacement is a dropdown KubeSight fills."""
    data = _convert(
        client, admin_token, label="Tag (list)", sourceKind="deployments",
        parentFieldId=ENV_FIELD,
    ).get_json()["data"]

    assert data["binding"]["sourceKind"] == "deployments"
    assert data["binding"]["parentFieldId"] == ENV_FIELD
    assert fields_svc.get_field_binding("303")["sourceKind"] == "deployments"


def test_a_bad_source_does_not_lose_the_new_field(zoho, client, admin_token):
    resp = _convert(client, admin_token, label="Tag (list)", sourceKind="crystal_ball")
    assert resp.status_code == 201
    data = resp.get_json()["data"]
    assert data["newField"]["id"] == "303" and data["binding"] is None
    assert _warning_with(data, "option source could not be set")


def test_viewer_cannot_convert(zoho, client, viewer_token):
    assert _convert(client, viewer_token, label="Tag (list)").status_code == 403
    assert zoho["created"] == []
