"""Parity lock on ``sync_now`` — what the live integration publishes today.

Written BEFORE the option-source binding refactor and deliberately not touched
after it. The three legacy publishes (Application ← deployments, Environment ←
namespaces, Variable ← env-var names) drive production deploys, so the refactor
that replaces those three copy-pasted blocks with a generic binding loop has to
come out byte-identical: same field ids, same values, same preserved attrs, same
call ORDER, same status message wording, same cascade delete-then-create dance.

Zoho and Kubernetes are both stubbed by monkeypatching module attributes — the
service calls them as ``zoho_client.X`` / module-level helpers, so the patch
lands at call time (the convention in ``test_deploy_automation.py``).
"""

import json

import pytest

from api.db import db
from api.models import ZohoIntegration
from api.services import zoho_client
from api.services import zoho_sync_service as svc

APP_FIELD = "9001"
ENV_FIELD = "9002"
VAR_FIELD = "9003"
CLUSTER = "prod-us-east"

# Two namespaces, one app deliberately living in BOTH — the case that makes the
# Application list de-dupe and the Environment→Application cascade non-trivial.
DEPLOYMENTS = {
    "payments": ["payments-api", "ledger-worker"],
    "checkout": ["payments-api", "cart-svc"],
}
# ENCRYPTION_KEY vs encryption_key: Zoho compares picklist values
# case-insensitively and 400s the whole PATCH on a duplicate.
ENV_VARS = {
    "payments": {
        "payments-api": ["DB_HOST", "ENCRYPTION_KEY"],
        "ledger-worker": ["DB_HOST"],
    },
    "checkout": {
        "payments-api": ["encryption_key", "LOG_LEVEL"],
        "cart-svc": [],
    },
}


@pytest.fixture()
def zoho(app, monkeypatch):
    """Configured integration + recording stubs. Returns the recorder."""
    row = ZohoIntegration.query.get(1) or ZohoIntegration(id=1)
    row.enabled = True
    row.org_id = "org-1"
    row.layout_id = "L1"
    row.app_field_id = APP_FIELD
    row.environment_field_id = ENV_FIELD
    row.variable_field_id = VAR_FIELD
    row.sync_application = True
    row.sync_environment = True
    row.sync_variables = True
    row.cascade_enabled = True
    row.source_cluster_id = CLUSTER
    row.selected_namespaces = json.dumps(["payments", "checkout"])
    row.selected_deployments = None
    row.custom_environments = None
    db.session.add(row)
    db.session.commit()

    state = {"publishes": [], "mapping_deletes": [], "mapping_creates": []}

    def _deployments(cluster_id, namespaces, fresh=False):
        return {ns: list(DEPLOYMENTS.get(ns) or []) for ns in namespaces}

    def _variables(row_, entries, fresh=False):
        return {ns: dict(vars_) for ns, vars_ in ENV_VARS.items()}

    monkeypatch.setattr(svc, "_list_deployments_by_namespace", _deployments)
    monkeypatch.setattr(svc, "_variables_for_entries", _variables)

    def _field_on_layout(cfg, field_id):
        # Someone marked Environment mandatory in Desk; the sync must not reset it.
        if str(field_id) == ENV_FIELD:
            return {"id": ENV_FIELD, "defaultValue": "-None-", "isMandatory": True}
        return {"id": str(field_id), "defaultValue": "-None-", "isMandatory": False}

    def _set_allowed_values(cfg, values, *, field_id=None, default_value="-None-",
                            sort_by="userDefined", is_mandatory=False):
        state["publishes"].append(
            {
                "fieldId": str(field_id or cfg.app_field_id),
                "values": list(values),
                "defaultValue": default_value,
                "isMandatory": is_mandatory,
            }
        )
        return {}

    def _list_mappings(cfg):
        return {
            "data": [
                {"id": "m1", "parentId": ENV_FIELD, "childId": APP_FIELD},
                {"id": "m2", "parentId": APP_FIELD, "childId": VAR_FIELD},
            ]
        }

    def _delete_mapping(cfg, mapping_id):
        state["mapping_deletes"].append(str(mapping_id))
        return {}

    def _create_mapping(cfg, body):
        state["mapping_creates"].append(dict(body))
        return {"id": f"new-{len(state['mapping_creates'])}"}

    monkeypatch.setattr(zoho_client, "field_on_layout", _field_on_layout)
    monkeypatch.setattr(zoho_client, "set_allowed_values", _set_allowed_values)
    monkeypatch.setattr(zoho_client, "list_dependency_mappings", _list_mappings)
    monkeypatch.setattr(zoho_client, "delete_dependency_mapping", _delete_mapping)
    monkeypatch.setattr(zoho_client, "create_dependency_mapping", _create_mapping)
    return state


def _by_field(state, field_id):
    return next(p for p in state["publishes"] if p["fieldId"] == field_id)


# ---------------------------------------------------------------------------
# The three legacy publishes
# ---------------------------------------------------------------------------

def test_publishes_all_three_fields_in_order(zoho):
    result = svc.sync_now()

    assert result["status"] == "ok"
    # Application, then Environment, then Variable — the order the cascade's
    # parent-before-child rebuild depends on having already published.
    assert [p["fieldId"] for p in zoho["publishes"]] == [APP_FIELD, ENV_FIELD, VAR_FIELD]


def test_application_values_dedupe_across_namespaces(zoho):
    svc.sync_now()
    values = _by_field(zoho, APP_FIELD)["values"]

    assert values[0] == svc.NONE_VALUE
    # Values are BARE deployment names, so payments-api — running in both
    # namespaces — is one option. The cascade is what keeps it unambiguous.
    assert values[1:] == ["payments-api", "ledger-worker", "cart-svc"]


def test_environment_values_are_the_selected_namespaces(zoho):
    svc.sync_now()
    assert _by_field(zoho, ENV_FIELD)["values"] == [svc.NONE_VALUE, "payments", "checkout"]


def test_variable_values_casefold_dedupe_and_sort(zoho):
    svc.sync_now()
    values = _by_field(zoho, VAR_FIELD)["values"]

    assert values[0] == svc.NONE_VALUE
    # ENCRYPTION_KEY wins over encryption_key (first seen), and only once.
    assert values[1:] == ["DB_HOST", "ENCRYPTION_KEY", "LOG_LEVEL"]


def test_preserved_attrs_are_read_back_not_reset(zoho):
    svc.sync_now()
    assert _by_field(zoho, ENV_FIELD)["isMandatory"] is True
    assert _by_field(zoho, APP_FIELD)["isMandatory"] is False
    assert all(p["defaultValue"] == svc.NONE_VALUE for p in zoho["publishes"])


def test_default_value_falls_back_when_no_longer_published(zoho, monkeypatch):
    monkeypatch.setattr(
        zoho_client,
        "field_on_layout",
        lambda cfg, field_id: {"defaultValue": "gone-namespace", "isMandatory": False},
    )
    svc.sync_now()
    assert _by_field(zoho, ENV_FIELD)["defaultValue"] == svc.NONE_VALUE


def test_message_and_counts(zoho):
    result = svc.sync_now()

    assert result["message"] == (
        "Published 3 deployment(s) -> Application; 2 namespace(s) -> Environment; "
        "3 variable name(s) -> Variable to Zoho."
    )
    assert result["deployments"] == 3
    assert result["namespaces"] == 2
    assert result["variables"] == 3
    assert result["count"] == 3
    assert result["lastSyncStatus"] == "ok"
    assert result["lastSyncedCount"] == 3


# ---------------------------------------------------------------------------
# Per-field toggles
# ---------------------------------------------------------------------------

def test_toggles_off_publish_nothing(zoho):
    row = svc.get_or_create_config()
    row.sync_application = row.sync_environment = row.sync_variables = False
    db.session.commit()

    result = svc.sync_now()
    assert zoho["publishes"] == []
    assert result["message"] == "Nothing published — all field syncs are turned off."


def test_variable_sync_off_skips_only_that_field(zoho):
    row = svc.get_or_create_config()
    row.sync_variables = False
    db.session.commit()

    result = svc.sync_now()
    assert [p["fieldId"] for p in zoho["publishes"]] == [APP_FIELD, ENV_FIELD]
    assert "Variable" not in result["message"]


def test_publish_failure_names_the_field_and_records_error(zoho, monkeypatch):
    def _boom(cfg, values, *, field_id=None, **kwargs):
        if str(field_id) == ENV_FIELD:
            raise zoho_client.ZohoError("The allowed values has duplicate value", 400)
        return {}

    monkeypatch.setattr(zoho_client, "set_allowed_values", _boom)
    result = svc.sync_now()

    assert result["status"] == "error"
    assert result["message"].startswith("Publishing the Environment field:")
    # A failed publish must not go on to rewrite the cascade.
    assert zoho["mapping_creates"] == []


def test_source_read_failure_does_not_touch_zoho(zoho, monkeypatch):
    def _boom(cluster_id, namespaces, fresh=False):
        raise ValueError("Could not read deployments in 'payments': timeout")

    monkeypatch.setattr(svc, "_list_deployments_by_namespace", _boom)
    result = svc.sync_now()

    assert result["status"] == "error"
    assert zoho["publishes"] == []


def test_disabled_integration_refuses_to_sync(zoho):
    row = svc.get_or_create_config()
    row.enabled = False
    db.session.commit()

    with pytest.raises(ValueError, match="disabled"):
        svc.sync_now()
    assert zoho["publishes"] == []


# ---------------------------------------------------------------------------
# Cascade — Environment → Application → Variable
# ---------------------------------------------------------------------------

def test_cascade_deletes_both_mappings_before_creating_either(zoho):
    result = svc.sync_now()

    # Zoho 422s "invalid child Id" when a child already parents another mapping,
    # so BOTH managed mappings go before either is recreated.
    assert zoho["mapping_deletes"] == ["m1", "m2"]
    assert [(m["parentId"], m["childId"]) for m in zoho["mapping_creates"]] == [
        (ENV_FIELD, APP_FIELD),
        (APP_FIELD, VAR_FIELD),
    ]
    assert result["cascade"]["status"] == "ok"
    assert result["cascade"]["message"] == (
        "Cascade configured for 2 namespace(s). Variable lists mapped for 2 application(s)."
    )


def test_cascade_maps_namespaces_to_their_applications(zoho):
    svc.sync_now()
    mappings = zoho["mapping_creates"][0]["mappings"]

    assert sorted(mappings) == ["checkout", "payments"]
    assert mappings["payments"] == ["payments-api", "ledger-worker"]
    assert mappings["checkout"] == ["payments-api", "cart-svc"]


def test_cascade_child_values_use_published_variable_spellings(zoho):
    svc.sync_now()
    published = set(_by_field(zoho, VAR_FIELD)["values"])
    var_map = zoho["mapping_creates"][1]["mappings"]

    for children in var_map.values():
        # A child value Zoho never saw published fails the mapping with a 422.
        assert set(children) <= published
    # One Application value spans both namespaces, so its variables are the UNION
    # — with encryption_key folded into the published ENCRYPTION_KEY spelling.
    assert var_map["payments-api"] == ["DB_HOST", "ENCRYPTION_KEY", "LOG_LEVEL"]
    assert var_map["ledger-worker"] == ["DB_HOST"]
    # cart-svc has no env vars at all and is simply absent.
    assert "cart-svc" not in var_map


def test_cascade_disabled_is_recorded_as_skipped(zoho):
    row = svc.get_or_create_config()
    row.cascade_enabled = False
    db.session.commit()

    result = svc.sync_now()
    assert result["cascade"]["status"] == "skipped"
    assert zoho["mapping_creates"] == []
    # Publishing still happened — the cascade is layered on top.
    assert len(zoho["publishes"]) == 3


def test_cascade_error_does_not_fail_the_sync(zoho, monkeypatch):
    def _boom(cfg, body):
        raise zoho_client.ZohoError("forbidden", 403)

    monkeypatch.setattr(zoho_client, "create_dependency_mapping", _boom)
    result = svc.sync_now()

    assert result["status"] == "ok"
    assert result["cascade"]["status"] == "error"
    assert "Desk.settings.CREATE" in result["cascade"]["message"]


def test_cascade_second_level_skipped_without_variable_sync(zoho):
    row = svc.get_or_create_config()
    row.sync_variables = False
    db.session.commit()

    result = svc.sync_now()
    assert [(m["parentId"], m["childId"]) for m in zoho["mapping_creates"]] == [
        (ENV_FIELD, APP_FIELD)
    ]
    assert "Variable lists mapped" not in result["cascade"]["message"]
