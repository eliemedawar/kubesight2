"""The whole-layout writer's safety guards.

``PATCH /layouts/{id}`` is a full replace, so every test here is really asking
one question: can a bug in the writer silently strip or corrupt a field on the
live DevOps Request form? Zoho is stubbed by patching ``zoho_client`` module
attributes — ``zoho_layout_service`` calls them as ``zoho_client.X``, so the
patch lands at call time.
"""

import copy

import pytest

from api.db import db
from api.models import ZohoIntegration, ZohoLayoutSnapshot
from api.services import zoho_client, zoho_layout_service as svc

from .conftest import auth_headers

LAYOUT_ID = "999149000010342586"


def _layout():
    """A layout shaped like the real one: mixed field types, object-form values."""
    return {
        "id": LAYOUT_ID,
        "name": "DevOps Request",
        "layoutName": "DevOps Request",
        "departmentId": "999149000002535035",
        "isDefaultLayout": False,
        "module": "tickets",
        "sections": [
            {
                "id": 1,
                "name": "Case Information",
                "i18NLabel": "Case Information",
                "fields": [
                    {"id": "101", "apiName": "subject", "type": "Text", "isMandatory": True},
                    {
                        "id": "102",
                        "apiName": "status",
                        "type": "Picklist",
                        "isMandatory": True,
                        # Desk returns object-form values on status fields; losing
                        # the metadata would hit every ticket in the department.
                        "allowedValues": [
                            {"value": "Open", "colorCode": "#00ff00"},
                            {"value": "Closed", "colorCode": "#888888"},
                        ],
                        "defaultValue": "Open",
                        "sortBy": "userDefined",
                        "isNested": False,
                        "restoreOnReplyValues": ["Open"],
                    },
                ],
            },
            {
                "id": 2,
                "name": "Deployment Information",
                "i18NLabel": "Deployment Information",
                "fields": [
                    {
                        "id": "201",
                        "apiName": "cf_environment",
                        "type": "Picklist",
                        "isMandatory": False,
                        "allowedValues": ["-None-", "verto-preprod"],
                        "defaultValue": "-None-",
                        "sortBy": "userDefined",
                        "isNested": False,
                    },
                    {"id": "202", "apiName": "cf_tag", "type": "Text", "isMandatory": False},
                ],
            },
        ],
    }


@pytest.fixture()
def zoho(app, monkeypatch):
    """Config row + a recording Zoho stub. Returns the recorder."""
    row = ZohoIntegration.query.get(1) or ZohoIntegration(id=1)
    row.layout_id = LAYOUT_ID
    row.department_id = "999149000002535035"
    row.enabled = True
    db.session.add(row)
    db.session.commit()

    state = {"layout": _layout(), "patches": []}

    def _get_layout(cfg, fresh=False):
        return copy.deepcopy(state["layout"])

    def _update_layout(cfg, body):
        state["patches"].append(copy.deepcopy(body))
        return {"id": LAYOUT_ID}

    monkeypatch.setattr(zoho_client, "get_layout", _get_layout)
    monkeypatch.setattr(zoho_client, "update_layout", _update_layout)
    monkeypatch.setenv("ZOHO_LAYOUT_WRITE_ENABLED", "true")
    return state


# ---------------------------------------------------------------------------
# Round-trip fidelity
# ---------------------------------------------------------------------------

def test_body_carries_every_field_and_required_keys(zoho):
    plan = svc.plan_layout_write([svc.add_section("QA")])
    body = plan["body"]

    ids = [f["id"] for s in body["sections"] for f in s["fields"]]
    assert sorted(ids) == ["101", "102", "201", "202"]
    for key in ("departmentId", "isDefaultLayout", "layoutName", "module", "sections"):
        assert key in body
    # A request-behaviour flag, never echoed from the read.
    assert body["skipDeptAccessValidation"] is False


def test_picklist_object_values_are_not_normalized(zoho):
    """Flattening ``{"value": ...}`` objects to strings would strip status colors."""
    plan = svc.plan_layout_write([svc.add_section("QA")])
    status = next(
        f for s in plan["body"]["sections"] for f in s["fields"] if f["id"] == "102"
    )
    assert status["allowedValues"] == [
        {"value": "Open", "colorCode": "#00ff00"},
        {"value": "Closed", "colorCode": "#888888"},
    ]
    assert status["restoreOnReplyValues"] == ["Open"]
    assert status["isMandatory"] is True


def test_plan_does_not_write(zoho):
    svc.plan_layout_write([svc.add_section("QA")])
    assert zoho["patches"] == []


# ---------------------------------------------------------------------------
# Sections
# ---------------------------------------------------------------------------

def test_add_section_appends_with_next_ordinal(zoho):
    result = svc.apply_layout_write([svc.add_section("QA")])
    sections = zoho["patches"][0]["sections"]

    assert [s["name"] for s in sections] == ["Case Information", "Deployment Information", "QA"]
    assert [s["id"] for s in sections] == [1, 2, 3]
    assert sections[2]["fields"] == []
    # Existing sections are untouched.
    assert len(sections[0]["fields"]) == 2 and len(sections[1]["fields"]) == 2
    assert result["diff"]["sectionsAdded"] == 1
    assert result["diff"]["fieldsDropped"] == []


def test_duplicate_section_name_rejected(zoho):
    with pytest.raises(svc.LayoutWriteError, match="already exists"):
        svc.plan_layout_write([svc.add_section("case information")])


def test_mixed_section_ids_abort(zoho):
    """Synthesizing 1,2 next to real snowflake ids could collide two sections."""
    zoho["layout"]["sections"][1].pop("id")
    with pytest.raises(svc.LayoutWriteError, match="inconsistent ids"):
        svc.plan_layout_write([svc.add_section("QA")])


def test_snowflake_section_ids_echoed_verbatim(zoho):
    for section, sid in zip(zoho["layout"]["sections"], ["999149000010342590", "999149000010342591"]):
        section["id"] = sid
    svc.apply_layout_write([svc.add_section("QA")])
    ids = [s["id"] for s in zoho["patches"][0]["sections"]]
    assert ids[:2] == ["999149000010342590", "999149000010342591"]
    assert ids[2] == 999149000010342592


# ---------------------------------------------------------------------------
# Field placement
# ---------------------------------------------------------------------------

def test_place_field_moves_without_duplicating(zoho):
    svc.apply_layout_write([svc.place_field("202", "Case Information")])
    sections = zoho["patches"][0]["sections"]

    assert [f["id"] for f in sections[0]["fields"]] == ["101", "102", "202"]
    assert [f["id"] for f in sections[1]["fields"]] == ["201"]
    all_ids = [f["id"] for s in sections for f in s["fields"]]
    assert len(all_ids) == len(set(all_ids)) == 4


def test_place_field_after_sibling(zoho):
    svc.apply_layout_write([svc.place_field("202", "Case Information", after_field_id="101")])
    assert [f["id"] for f in zoho["patches"][0]["sections"][0]["fields"]] == ["101", "202", "102"]


def test_place_field_unknown_section_rejected(zoho):
    with pytest.raises(svc.LayoutWriteError, match="No section named"):
        svc.plan_layout_write([svc.place_field("202", "Nope")])


def test_add_section_then_place_field_in_it(zoho):
    svc.apply_layout_write([svc.add_section("QA"), svc.place_field("202", "QA")])
    sections = zoho["patches"][0]["sections"]
    assert [f["id"] for f in sections[2]["fields"]] == ["202"]
    assert [f["id"] for f in sections[1]["fields"]] == ["201"]


# ---------------------------------------------------------------------------
# Guards — the point of the whole module
# ---------------------------------------------------------------------------

def test_guard_blocks_dropped_field(zoho, monkeypatch):
    """A mutation that loses a field must raise instead of unassociating it."""

    def _buggy(sections, mutation):
        sections[0]["fields"].pop()  # the bug
        return {"addedSections": [], "removedSections": [], "addedFields": [], "removedFields": []}

    monkeypatch.setitem(svc._MUTATIONS, "buggy", _buggy)
    with pytest.raises(svc.LayoutWriteError, match="would remove field"):
        svc.apply_layout_write([{"kind": "buggy"}])
    assert zoho["patches"] == []


def test_guard_blocks_duplicated_field(zoho, monkeypatch):
    def _buggy(sections, mutation):
        sections[1]["fields"].append(copy.deepcopy(sections[0]["fields"][0]))
        return {"addedSections": [], "removedSections": [], "addedFields": [], "removedFields": []}

    monkeypatch.setitem(svc._MUTATIONS, "buggy", _buggy)
    with pytest.raises(svc.LayoutWriteError, match="more than one section"):
        svc.apply_layout_write([{"kind": "buggy"}])
    assert zoho["patches"] == []


def test_guard_blocks_side_effect_on_field_attrs(zoho, monkeypatch):
    """Section moves may not change a field's required flag or option list."""

    def _buggy(sections, mutation):
        sections[0]["fields"][1]["allowedValues"] = ["Open"]
        return {"addedSections": [], "removedSections": [], "addedFields": [], "removedFields": []}

    monkeypatch.setitem(svc._MUTATIONS, "buggy", _buggy)
    with pytest.raises(svc.LayoutWriteError, match="option list"):
        svc.apply_layout_write([{"kind": "buggy"}])
    assert zoho["patches"] == []


def test_guard_blocks_unexpected_section_change(zoho, monkeypatch):
    def _buggy(sections, mutation):
        sections.pop()
        return {"addedSections": [], "removedSections": [], "addedFields": [], "removedFields": []}

    monkeypatch.setitem(svc._MUTATIONS, "buggy", _buggy)
    with pytest.raises(svc.LayoutWriteError):
        svc.apply_layout_write([{"kind": "buggy"}])
    assert zoho["patches"] == []


def test_missing_identity_key_aborts(zoho):
    """Guessing isDefaultLayout could demote the department's default layout."""
    zoho["layout"].pop("isDefaultLayout")
    with pytest.raises(svc.LayoutWriteError, match="isDefaultLayout"):
        svc.plan_layout_write([svc.add_section("QA")])


def test_empty_layout_aborts(zoho):
    zoho["layout"]["sections"] = []
    with pytest.raises(svc.LayoutWriteError, match="no sections"):
        svc.plan_layout_write([svc.add_section("QA")])


def test_unknown_mutation_rejected(zoho):
    with pytest.raises(svc.LayoutWriteError, match="Unknown layout mutation"):
        svc.plan_layout_write([{"kind": "drop_everything"}])


# ---------------------------------------------------------------------------
# Kill switch, snapshots, verification
# ---------------------------------------------------------------------------

def test_writes_disabled_by_default(zoho, monkeypatch):
    monkeypatch.delenv("ZOHO_LAYOUT_WRITE_ENABLED", raising=False)
    assert svc.writes_enabled() is False
    with pytest.raises(PermissionError, match="ZOHO_LAYOUT_WRITE_ENABLED"):
        svc.apply_layout_write([svc.add_section("QA")])
    assert zoho["patches"] == []
    # The dry-run still works, and says why.
    plan = svc.plan_layout_write([svc.add_section("QA")])
    assert plan["writesEnabled"] is False
    assert any("disabled" in w for w in plan["warnings"])


def test_snapshot_taken_before_write(zoho):
    svc.apply_layout_write([svc.add_section("QA")], reason="add_section", actor="admin")
    snap = ZohoLayoutSnapshot.query.filter_by(layout_id=LAYOUT_ID).one()
    assert snap.reason == "add_section" and snap.actor == "admin"
    # The snapshot is the PRE-write layout — no QA section in it.
    assert [s["name"] for s in snap.payload["sections"]] == [
        "Case Information",
        "Deployment Information",
    ]


def test_snapshot_survives_a_failed_patch(zoho, monkeypatch):
    def _boom(cfg, body):
        raise zoho_client.ZohoError("Zoho said no", 422)

    monkeypatch.setattr(zoho_client, "update_layout", _boom)
    with pytest.raises(zoho_client.ZohoError):
        svc.apply_layout_write([svc.add_section("QA")])
    assert ZohoLayoutSnapshot.query.count() == 1


def test_snapshots_pruned_to_retention(zoho):
    for i in range(svc._SNAPSHOT_RETENTION + 3):
        svc.apply_layout_write([svc.add_section(f"QA{i}")])
        zoho["layout"]["sections"].append(
            {"id": 3 + i, "name": f"QA{i}", "i18NLabel": f"QA{i}", "fields": []}
        )
    assert ZohoLayoutSnapshot.query.count() == svc._SNAPSHOT_RETENTION


def test_post_write_verification_catches_lost_field(zoho, monkeypatch):
    """Zoho accepting the PATCH but dropping a field must not report success."""
    calls = {"n": 0}
    real = zoho["layout"]

    def _get_layout(cfg, fresh=False):
        calls["n"] += 1
        after = copy.deepcopy(real)
        if calls["n"] > 1:  # the verification read
            after["sections"][1]["fields"] = []
        return after

    monkeypatch.setattr(zoho_client, "get_layout", _get_layout)
    with pytest.raises(svc.LayoutWriteError, match="Restore from the snapshot"):
        svc.apply_layout_write([svc.add_section("QA")])


# ---------------------------------------------------------------------------
# Routes: sections + field placement
# ---------------------------------------------------------------------------

def test_plan_route_returns_body_without_writing(zoho, client, admin_token):
    resp = client.post(
        "/api/zoho/layout/plan",
        json={"sectionName": "QA"},
        headers=auth_headers(admin_token),
    )
    assert resp.status_code == 200
    data = resp.get_json()["data"]
    assert [s["name"] for s in data["body"]["sections"]][-1] == "QA"
    assert data["diff"]["sectionsAdded"] == 1
    assert zoho["patches"] == []


def test_create_section_route(zoho, client, admin_token):
    resp = client.post(
        "/api/zoho/sections", json={"name": "QA"}, headers=auth_headers(admin_token)
    )
    assert resp.status_code == 201, resp.get_json()
    assert [s["name"] for s in zoho["patches"][0]["sections"]][-1] == "QA"


def test_create_section_blocked_when_writes_disabled(zoho, client, admin_token, monkeypatch):
    monkeypatch.delenv("ZOHO_LAYOUT_WRITE_ENABLED", raising=False)
    resp = client.post(
        "/api/zoho/sections", json={"name": "QA"}, headers=auth_headers(admin_token)
    )
    assert resp.status_code == 409
    assert "ZOHO_LAYOUT_WRITE_ENABLED" in resp.get_json()["error"]
    assert zoho["patches"] == []


def test_create_section_duplicate_is_400(zoho, client, admin_token):
    resp = client.post(
        "/api/zoho/sections",
        json={"name": "Case Information"},
        headers=auth_headers(admin_token),
    )
    assert resp.status_code == 400


def test_viewer_cannot_write_layout(zoho, client, viewer_token):
    assert client.post(
        "/api/zoho/sections", json={"name": "QA"}, headers=auth_headers(viewer_token)
    ).status_code == 403
    assert zoho["patches"] == []


def test_layout_route_exposes_section_ids_and_types(zoho, client, admin_token):
    resp = client.get("/api/zoho/layout", headers=auth_headers(admin_token))
    data = resp.get_json()["data"]
    assert [s["id"] for s in data["sections"]] == [1, 2]
    assert [s["index"] for s in data["sections"]] == [0, 1]
    assert "Picklist" in data["creatableTypes"]
    assert data["layoutWritesEnabled"] is True


def test_move_field_route(zoho, client, admin_token):
    resp = client.put(
        "/api/zoho/fields/202/section",
        json={"sectionName": "Case Information"},
        headers=auth_headers(admin_token),
    )
    assert resp.status_code == 200, resp.get_json()
    sections = zoho["patches"][0]["sections"]
    assert [f["id"] for f in sections[0]["fields"]] == ["101", "102", "202"]


def test_move_unknown_field_is_404(zoho, client, admin_token):
    resp = client.put(
        "/api/zoho/fields/999/section",
        json={"sectionName": "Case Information"},
        headers=auth_headers(admin_token),
    )
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# create_field with a target section (create, then place)
# ---------------------------------------------------------------------------

@pytest.fixture()
def creatable(zoho, monkeypatch):
    """Make create_org_field mint a field into the FIRST section, like Desk does."""

    def _create(cfg, body):
        new = {
            "id": "303",
            "apiName": "cf_region",
            "displayLabel": body["displayLabel"],
            "type": body["type"],
            "isMandatory": body.get("isMandatory", False),
        }
        if body["type"] == "Picklist":
            new["allowedValues"] = body.get("allowedValues") or ["-None-"]
            new["defaultValue"] = "-None-"
            new["sortBy"] = "userDefined"
            new["isNested"] = False
        zoho["layout"]["sections"][0]["fields"].append(new)
        return new

    monkeypatch.setattr(zoho_client, "create_org_field", _create)
    return zoho


def test_create_field_moves_into_requested_section(creatable, client, admin_token):
    resp = client.post(
        "/api/zoho/fields",
        json={"label": "Region", "type": "Text", "sectionName": "Deployment Information"},
        headers=auth_headers(admin_token),
    )
    assert resp.status_code == 201, resp.get_json()
    data = resp.get_json()["data"]
    assert data["sectionName"] == "Deployment Information"
    assert data["warnings"] == []

    sections = creatable["patches"][0]["sections"]
    assert [f["id"] for f in sections[0]["fields"]] == ["101", "102"]
    assert [f["id"] for f in sections[1]["fields"]] == ["201", "202", "303"]


def test_create_field_without_section_does_not_write_layout(creatable, client, admin_token):
    resp = client.post(
        "/api/zoho/fields", json={"label": "Region", "type": "Text"},
        headers=auth_headers(admin_token),
    )
    assert resp.status_code == 201
    assert resp.get_json()["data"]["sectionName"] == "Case Information"
    assert creatable["patches"] == []


def test_create_field_reports_placement_failure_without_pretending(creatable, client, admin_token, monkeypatch):
    """The field exists even when the move fails — say so rather than 500."""

    def _boom(cfg, body):
        raise zoho_client.ZohoError("layout locked", 422)

    monkeypatch.setattr(zoho_client, "update_layout", _boom)
    resp = client.post(
        "/api/zoho/fields",
        json={"label": "Region", "type": "Text", "sectionName": "Deployment Information"},
        headers=auth_headers(admin_token),
    )
    assert resp.status_code == 201
    data = resp.get_json()["data"]
    assert data["id"] == "303"
    assert data["sectionName"] == "Case Information"
    assert "stayed in 'Case Information'" in data["warnings"][0]


def test_create_field_warns_when_layout_writes_disabled(creatable, client, admin_token, monkeypatch):
    monkeypatch.delenv("ZOHO_LAYOUT_WRITE_ENABLED", raising=False)
    resp = client.post(
        "/api/zoho/fields",
        json={"label": "Region", "type": "Text", "sectionName": "Deployment Information"},
        headers=auth_headers(admin_token),
    )
    assert resp.status_code == 201
    assert "ZOHO_LAYOUT_WRITE_ENABLED" in resp.get_json()["data"]["warnings"][0]
    assert creatable["patches"] == []
