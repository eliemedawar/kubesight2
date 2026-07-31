"""Option-source bindings: any picklist fed by a live KubeSight source.

The mechanism is the one the Application / Environment / Variable fields have
always used (locked in by ``test_zoho_sync_parity.py``); these tests cover what
generalizing it added — the provider registry, the shared case-insensitive
canonicalization, cascade ordering, and the binding CRUD guards.
"""

import json

import pytest

from api.db import db
from api.models import ZohoFieldBinding, ZohoIntegration
from api.services import ticketing_targets as targets
from api.services import zoho_client
from api.services import zoho_option_sources as sources
from api.services import zoho_sync_service as svc

from .conftest import auth_headers

APP_FIELD = "9001"
ENV_FIELD = "9002"
VAR_FIELD = "9003"
REGION_FIELD = "9101"  # an ordinary picklist the operator binds
TAG_FIELD = "9102"  # a Text field — never bindable
CLUSTER = "prod-us-east"

DEPLOYMENTS = {"payments": ["payments-api"], "checkout": ["cart-svc"]}


def _layout():
    return {
        "id": "L1",
        "name": "DevOps Request",
        "sections": [
            {
                "id": 1,
                "name": "Deployment Information",
                "fields": [
                    {"id": APP_FIELD, "apiName": "cf_application", "type": "Picklist",
                     "allowedValues": ["-None-"], "defaultValue": "-None-"},
                    {"id": ENV_FIELD, "apiName": "cf_environment", "type": "Picklist",
                     "allowedValues": ["-None-"], "defaultValue": "-None-"},
                    {"id": VAR_FIELD, "apiName": "cf_variable", "type": "Picklist",
                     "allowedValues": ["-None-"], "defaultValue": "-None-"},
                    {"id": REGION_FIELD, "apiName": "cf_region", "type": "Picklist",
                     "displayLabel": "Region", "allowedValues": ["-None-"],
                     "defaultValue": "-None-"},
                    {"id": TAG_FIELD, "apiName": "cf_tag", "type": "Text",
                     "displayLabel": "Tag"},
                ],
            }
        ],
    }


@pytest.fixture()
def zoho(app, monkeypatch):
    row = ZohoIntegration.query.get(1) or ZohoIntegration(id=1)
    row.enabled = True
    row.org_id = "org-1"
    row.layout_id = "L1"
    row.app_field_id = APP_FIELD
    row.environment_field_id = ENV_FIELD
    row.variable_field_id = VAR_FIELD
    row.sync_application = True
    row.sync_environment = True
    row.sync_variables = False
    row.cascade_enabled = True
    db.session.add(row)
    source = targets.get_or_create_config("zoho")
    source.source_cluster_id = CLUSTER
    source.selected_namespaces = json.dumps(["payments", "checkout"])
    db.session.add(source)
    db.session.commit()

    state = {"publishes": [], "mapping_creates": [], "mapping_deletes": []}

    monkeypatch.setattr(
        svc, "_list_deployments_by_namespace",
        lambda cluster_id, namespaces, fresh=False: {
            ns: list(DEPLOYMENTS.get(ns) or []) for ns in namespaces
        },
    )
    monkeypatch.setattr(svc, "_variables_for_entries", lambda *a, **k: {})
    monkeypatch.setattr(zoho_client, "get_layout", lambda cfg, fresh=False: _layout())
    monkeypatch.setattr(
        zoho_client, "field_on_layout",
        lambda cfg, field_id: next(
            (f for f in _layout()["sections"][0]["fields"] if f["id"] == str(field_id)), None
        ),
    )

    def _set_allowed_values(cfg, values, *, field_id=None, **kwargs):
        state["publishes"].append({"fieldId": str(field_id), "values": list(values)})
        return {}

    monkeypatch.setattr(zoho_client, "set_allowed_values", _set_allowed_values)
    monkeypatch.setattr(zoho_client, "list_dependency_mappings", lambda cfg: {"data": []})
    monkeypatch.setattr(
        zoho_client, "delete_dependency_mapping",
        lambda cfg, mid: state["mapping_deletes"].append(str(mid)) or {},
    )
    monkeypatch.setattr(
        zoho_client, "create_dependency_mapping",
        lambda cfg, body: state["mapping_creates"].append(dict(body)) or {"id": "m-new"},
    )
    return state


def _bind(client, token, field_id, **payload):
    return client.put(
        f"/api/zoho/fields/{field_id}/binding",
        json=payload,
        headers=auth_headers(token),
    )


def _published(state, field_id):
    return next((p for p in state["publishes"] if p["fieldId"] == field_id), None)


# ---------------------------------------------------------------------------
# Canonicalization — the funnel every source goes through
# ---------------------------------------------------------------------------

def test_canonical_values_casefold_dedupe_keeps_first_spelling():
    values, canon = sources.canonical_values(["ENCRYPTION_KEY", "encryption_key", "db_host"])
    assert values == ["-None-", "ENCRYPTION_KEY", "db_host"]
    assert canon["encryption_key"] == "ENCRYPTION_KEY"


def test_canonical_values_sanitize_and_drop_blanks():
    values, _ = sources.canonical_values(["pay/ments", "", None, "-None-", "  "])
    assert values == ["-None-", "pay ments"]


def test_align_by_parent_merges_parents_that_collide():
    """Two namespaces publishing ONE Environment value must not produce a phantom
    parent — that is a live 422 ("invalid parent value") waiting to happen."""
    by_parent = {"Verto": ["a"], "VERTO": ["b"], "gone": ["c"]}
    parent_canon = {"verto": "Verto"}
    child_canon = {"a": "a", "b": "b", "c": "c"}
    assert sources.align_by_parent(by_parent, parent_canon, child_canon) == {"Verto": ["a", "b"]}


def test_align_by_parent_drops_unpublished_children():
    aligned = sources.align_by_parent({"ns": ["kept", "dropped"]}, {"ns": "ns"}, {"kept": "kept"})
    assert aligned == {"ns": ["kept"]}


# ---------------------------------------------------------------------------
# Providers
# ---------------------------------------------------------------------------

def test_every_source_kind_resolves(zoho):
    row = svc.get_or_create_config()
    ctx = sources.SourceContext(row, svc._source_entries(row), "zoho")
    for key in sources.SOURCE_KINDS:
        options = sources.resolve(sources.Binding(REGION_FIELD, "Region", key), ctx)
        assert options.values[0] == svc.NONE_VALUE


def test_deployments_source_matches_the_legacy_application_list(zoho):
    """A binding on the Application field would publish byte-identical values."""
    row = svc.get_or_create_config()
    entries = svc._source_entries(row)
    ctx = sources.SourceContext(row, entries, "zoho")
    options = sources.resolve(sources.Binding(REGION_FIELD, "Region", "deployments"), ctx)
    assert options.values == svc._application_values(entries)


def test_env_vars_source_is_not_read_unless_asked(zoho):
    row = svc.get_or_create_config()
    ctx = sources.SourceContext(row, svc._source_entries(row), "zoho")
    sources.resolve(sources.Binding(REGION_FIELD, "Region", "namespaces"), ctx)
    assert ctx.variables_read is False
    sources.resolve(sources.Binding(REGION_FIELD, "Region", "env_vars"), ctx)
    assert ctx.variables_read is True


# ---------------------------------------------------------------------------
# Cascade ordering
# ---------------------------------------------------------------------------

def _b(field_id, parent=None, enabled=True):
    return sources.Binding(field_id, field_id, "namespaces", parent_field_id=parent, enabled=enabled)


def test_cascade_orders_parents_before_children():
    a, b, c = _b("A"), _b("B", parent="A"), _b("C", parent="B")
    active, teardown = sources.cascade_pairs([c, b, a])  # deliberately shuffled
    assert [(p.field_id, ch.field_id) for p, ch in active] == [("A", "B"), ("B", "C")]
    assert teardown == []


def test_cascade_rejects_a_cycle():
    a, b = _b("A", parent="B"), _b("B", parent="A")
    with pytest.raises(ValueError, match="cycle"):
        sources.cascade_pairs([a, b])


def test_disabled_child_becomes_a_teardown_pair():
    """Its stale mapping must still be deleted — it would keep filtering tickets."""
    active, teardown = sources.cascade_pairs([_b("A"), _b("B", parent="A", enabled=False)])
    assert active == []
    assert [(p.field_id, c.field_id) for p, c in teardown] == [("A", "B")]


def test_disabled_parent_leaves_the_pair_alone():
    """The parent's list is not maintained either; deleting would destroy config."""
    active, teardown = sources.cascade_pairs([_b("A", enabled=False), _b("B", parent="A")])
    assert active == [] and teardown == []


# ---------------------------------------------------------------------------
# Binding CRUD
# ---------------------------------------------------------------------------

def test_bind_a_picklist_and_publish_it(zoho, client, admin_token):
    resp = _bind(client, admin_token, REGION_FIELD, sourceKind="namespaces")
    assert resp.status_code == 200, resp.get_json()
    data = resp.get_json()["data"]
    assert data["sourceKind"] == "namespaces" and data["label"] == "Region"

    result = svc.sync_now()
    assert result["status"] == "ok"
    assert _published(zoho, REGION_FIELD)["values"] == ["-None-", "payments", "checkout"]
    assert "2 value(s) -> Region" in result["message"]
    stored = sources.get_binding_row(REGION_FIELD)
    assert stored.last_status == "ok" and stored.last_count == 2


def test_disabled_binding_is_not_published(zoho, client, admin_token):
    _bind(client, admin_token, REGION_FIELD, sourceKind="namespaces", enabled=False)
    svc.sync_now()
    assert _published(zoho, REGION_FIELD) is None


def test_bound_field_cascades_from_its_parent(zoho, client, admin_token):
    _bind(client, admin_token, REGION_FIELD, sourceKind="deployments", parentFieldId=ENV_FIELD)
    svc.sync_now()

    created = [(m["parentId"], m["childId"]) for m in zoho["mapping_creates"]]
    assert (ENV_FIELD, REGION_FIELD) in created
    mapping = next(m for m in zoho["mapping_creates"] if m["childId"] == REGION_FIELD)["mappings"]
    assert mapping == {"payments": ["payments-api"], "checkout": ["cart-svc"]}


def test_legacy_fields_cannot_be_bound(zoho, client, admin_token):
    resp = _bind(client, admin_token, APP_FIELD, sourceKind="namespaces")
    assert resp.status_code == 400
    assert "Source tab" in resp.get_json()["error"]
    assert ZohoFieldBinding.query.count() == 0


def test_text_field_cannot_be_bound(zoho, client, admin_token):
    resp = _bind(client, admin_token, TAG_FIELD, sourceKind="namespaces")
    assert resp.status_code == 400
    assert "picklist" in resp.get_json()["error"].lower()


def test_unknown_field_is_404(zoho, client, admin_token):
    assert _bind(client, admin_token, "404404", sourceKind="namespaces").status_code == 404


def test_unknown_source_kind_is_rejected(zoho, client, admin_token):
    resp = _bind(client, admin_token, REGION_FIELD, sourceKind="crystal_ball")
    assert resp.status_code == 400
    assert "Known sources" in resp.get_json()["error"]


def test_parent_must_be_bound_to_the_grouping_source(zoho, client, admin_token):
    """``deployments`` options are grouped BY NAMESPACE, so the parent has to be
    the namespaces field — otherwise the mapping's keys mean nothing."""
    resp = _bind(
        client, admin_token, REGION_FIELD, sourceKind="deployments", parentFieldId=APP_FIELD
    )
    assert resp.status_code == 400
    assert "must be bound to that source" in resp.get_json()["error"]


def test_non_cascading_source_rejects_a_parent(zoho, client, admin_token):
    resp = _bind(
        client, admin_token, REGION_FIELD, sourceKind="namespaces", parentFieldId=ENV_FIELD
    )
    assert resp.status_code == 400
    assert "does not cascade" in resp.get_json()["error"]


def test_self_parent_rejected(zoho, client, admin_token):
    resp = _bind(
        client, admin_token, REGION_FIELD, sourceKind="deployments", parentFieldId=REGION_FIELD
    )
    assert resp.status_code == 400


def test_rebinding_replaces_rather_than_duplicates(zoho, client, admin_token):
    _bind(client, admin_token, REGION_FIELD, sourceKind="namespaces")
    _bind(client, admin_token, REGION_FIELD, sourceKind="clusters")
    assert ZohoFieldBinding.query.count() == 1
    assert sources.get_binding_row(REGION_FIELD).source_kind == "clusters"


def test_delete_binding(zoho, client, admin_token):
    _bind(client, admin_token, REGION_FIELD, sourceKind="namespaces")
    resp = client.delete(
        f"/api/zoho/fields/{REGION_FIELD}/binding", headers=auth_headers(admin_token)
    )
    assert resp.status_code == 200
    assert ZohoFieldBinding.query.count() == 0
    svc.sync_now()
    assert _published(zoho, REGION_FIELD) is None


def test_delete_missing_binding_is_404(zoho, client, admin_token):
    assert client.delete(
        f"/api/zoho/fields/{REGION_FIELD}/binding", headers=auth_headers(admin_token)
    ).status_code == 404


def test_viewer_cannot_bind(zoho, client, viewer_token):
    assert _bind(client, viewer_token, REGION_FIELD, sourceKind="namespaces").status_code == 403


# ---------------------------------------------------------------------------
# Catalogue, preview, layout view
# ---------------------------------------------------------------------------

def test_option_sources_route_lists_kinds_and_legacy_bindings(zoho, client, admin_token):
    data = client.get("/api/zoho/option-sources", headers=auth_headers(admin_token)).get_json()["data"]

    assert {s["key"] for s in data["sources"]} == set(sources.SOURCE_KINDS)
    legacy = {b["fieldId"]: b for b in data["bindings"]}
    assert legacy[APP_FIELD]["sourceKind"] == "deployments"
    assert legacy[APP_FIELD]["locked"] is True
    # sync_variables is off in this fixture, so the Variable binding is disabled.
    assert legacy[VAR_FIELD]["enabled"] is False


def test_preview_resolves_without_publishing(zoho, client, admin_token):
    resp = client.post(
        f"/api/zoho/fields/{REGION_FIELD}/binding/preview",
        json={"sourceKind": "deployments", "parentFieldId": ENV_FIELD},
        headers=auth_headers(admin_token),
    )
    assert resp.status_code == 200
    data = resp.get_json()["data"]
    assert data["values"] == ["-None-", "payments-api", "cart-svc"]
    assert data["count"] == 2
    assert data["byParent"] == {"payments": ["payments-api"], "checkout": ["cart-svc"]}
    assert zoho["publishes"] == []


def test_preview_reports_a_missing_source_instead_of_failing(zoho, client, admin_token):
    source = targets.get_or_create_config("zoho")
    source.source_cluster_id = ""
    source.selected_namespaces = json.dumps([])
    db.session.commit()

    data = client.post(
        f"/api/zoho/fields/{REGION_FIELD}/binding/preview",
        json={"sourceKind": "deployments"},
        headers=auth_headers(admin_token),
    ).get_json()["data"]
    assert data["values"] == [] and "No source selected" in data["error"]


def test_layout_carries_each_field_binding(zoho, client, admin_token):
    _bind(client, admin_token, REGION_FIELD, sourceKind="clusters")
    data = client.get("/api/zoho/layout", headers=auth_headers(admin_token)).get_json()["data"]
    fields = {f["id"]: f for f in data["sections"][0]["fields"]}

    assert fields[REGION_FIELD]["binding"]["sourceKind"] == "clusters"
    assert fields[APP_FIELD]["binding"]["locked"] is True
    assert fields[TAG_FIELD]["binding"] is None


# ---------------------------------------------------------------------------
# A broken binding must not take the production fields down with it
# ---------------------------------------------------------------------------

def test_failing_binding_degrades_to_a_warning(zoho, client, admin_token, monkeypatch):
    _bind(client, admin_token, REGION_FIELD, sourceKind="namespaces")

    def _selective(cfg, values, *, field_id=None, **kwargs):
        if str(field_id) == REGION_FIELD:
            raise zoho_client.ZohoError("The allowed values has duplicate value", 400)
        zoho["publishes"].append({"fieldId": str(field_id), "values": list(values)})
        return {}

    monkeypatch.setattr(zoho_client, "set_allowed_values", _selective)
    result = svc.sync_now()

    assert result["status"] == "ok"
    assert _published(zoho, APP_FIELD) is not None  # the deploy-critical field still went
    assert any("Region" in w for w in result["warnings"])
    assert sources.get_binding_row(REGION_FIELD).last_status == "error"
