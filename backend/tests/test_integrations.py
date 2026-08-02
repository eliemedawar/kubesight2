"""Coverage for the provider-neutral integrations hub.

The service's whole job is that nine providers with nine storage schemas come out
one shape, so the tests that matter are the ones pinning that shape and the
precedence rules behind it. The pure functions -- derive_status, _actions,
_outcome -- carry the rules and cost nothing to test; the route tests cover the
two invariants the docstrings call out, that describing never tests and that one
broken provider does not blank the hub.
"""

from unittest.mock import Mock

import pytest

from api.services import integrations_service as svc
from tests.conftest import auth_headers

# The exact descriptor contract. `configured` is deliberately absent -- it is an
# input to derive_status, never an output, and the frontend reads configuration
# state off `status` and `actions` instead.
DESCRIPTOR_KEYS = {
    "key",
    "name",
    "category",
    "status",
    "enabled",
    "lastTestedAt",
    "lastSuccessfulSyncAt",
    "message",
    "capabilities",
    "usedBy",
    "actions",
}

VALID_STATUSES = {svc.CONNECTED, svc.DEGRADED, svc.DISABLED, svc.NOT_CONFIGURED}

# Every wrapper that reaches an external host. Patched wholesale to prove a GET
# touches none of them.
TEST_FUNCTIONS = (
    "_test_ticketing",
    "_test_jenkins",
    "_test_smtp",
    "_test_receivers",
    "_test_registries",
    "_test_hermes",
)


# ─── derive_status ───


def test_unconfigured_beats_everything():
    """Nobody configured it, so it is not "disabled" and not "degraded"."""
    assert svc.derive_status(configured=False, enabled=False, last_outcome=None) == svc.NOT_CONFIGURED
    assert svc.derive_status(configured=False, enabled=True, last_outcome=False) == svc.NOT_CONFIGURED


def test_disabled_hides_a_stale_failure():
    """Reporting degraded for something deliberately switched off sends people
    chasing a problem they already resolved by switching it off."""
    assert svc.derive_status(configured=True, enabled=False, last_outcome=False) == svc.DISABLED


def test_degraded_only_on_an_actual_failure():
    assert svc.derive_status(configured=True, enabled=True, last_outcome=False) == svc.DEGRADED


def test_never_tested_reads_as_connected_not_degraded():
    """`None` means never run. Absence of a result is not a failure."""
    assert svc.derive_status(configured=True, enabled=True, last_outcome=None) == svc.CONNECTED
    assert svc.derive_status(configured=True, enabled=True, last_outcome=True) == svc.CONNECTED


# ─── _actions ───


def test_no_manage_permission_offers_no_controls():
    actions = svc._actions(configured=True, enabled=True, can_manage=False, testable=True)
    assert "configure" not in actions
    assert "disable" not in actions and "enable" not in actions


def test_viewer_may_not_provoke_a_test():
    """Testing writes last_test_* and reaches an external host."""
    assert "test" not in svc._actions(
        configured=True, enabled=True, can_manage=False, testable=False
    )


def test_unconfigured_offers_configure_only():
    actions = svc._actions(configured=False, enabled=False, can_manage=True, testable=True)
    assert actions == ["configure"]


def test_enable_and_disable_are_mutually_exclusive():
    on = svc._actions(configured=True, enabled=True, can_manage=True, testable=True)
    off = svc._actions(configured=True, enabled=False, can_manage=True, testable=True)
    assert "disable" in on and "enable" not in on
    assert "enable" in off and "disable" not in off


# ─── _outcome ───


@pytest.mark.parametrize("raw", ["ok", "success", "succeeded", "passed", "OK", "  Success  "])
def test_every_spelling_of_success(raw):
    """Half the providers say "ok" and half say "success"."""
    assert svc._outcome(raw) is True


@pytest.mark.parametrize("raw", ["error", "failed", "failure", "FAILED"])
def test_every_spelling_of_failure(raw):
    assert svc._outcome(raw) is False


@pytest.mark.parametrize("raw", [None, "", "pending", "unknown"])
def test_unrecognised_outcome_is_none_not_false(raw):
    """None means never run. Guessing "false" would show a red card for a
    provider that has simply never been tested."""
    assert svc._outcome(raw) is None


# ─── descriptor shape ───


def test_descriptor_emits_the_contract_and_nothing_else():
    descriptor = svc._descriptor(
        key="jira",
        name="Jira",
        category="Ticketing",
        configured=True,
        enabled=True,
        last_outcome=True,
        can_manage=True,
    )
    assert set(descriptor) == DESCRIPTOR_KEYS
    assert "configured" not in descriptor


def test_descriptor_never_emits_null_arrays():
    """The frontend maps over these unguarded."""
    descriptor = svc._descriptor(
        key="jira", name="Jira", category="Ticketing",
        configured=False, enabled=False, last_outcome=None,
    )
    assert descriptor["capabilities"] == []
    assert descriptor["usedBy"] == []
    assert descriptor["actions"] == []


def test_unavailable_card_keeps_its_identity():
    """An "Other / Bitbucket" card would read as a different integration rather
    than a broken one."""
    card = svc._unavailable("bitbucket", "connection refused")
    assert set(card) == DESCRIPTOR_KEYS
    assert card["name"] == "Bitbucket"
    assert card["category"] == "Source control"
    assert card["actions"] == []
    assert "connection refused" in card["message"]


# ─── routes ───


def test_hub_requires_a_session(client):
    assert client.get("/api/integrations").status_code == 401


def test_admin_sees_a_well_formed_hub(client, admin_token):
    response = client.get("/api/integrations", headers=auth_headers(admin_token))
    assert response.status_code == 200
    payload = response.get_json()
    assert payload["success"] is True

    items = payload["data"]["items"]
    assert items, "admin should see every integration"
    for item in items:
        assert set(item) == DESCRIPTOR_KEYS, f"{item['key']} broke the contract"
        assert item["status"] in VALID_STATUSES
        assert isinstance(item["capabilities"], list)
        assert isinstance(item["actions"], list)
        assert set(item["actions"]) <= {"configure", "test", "disable", "enable"}


def test_unknown_integration_is_404(client, admin_token):
    response = client.get("/api/integrations/nope", headers=auth_headers(admin_token))
    assert response.status_code == 404


def test_permissions_filter_the_hub(client, admin_token, viewer_token):
    admin = client.get("/api/integrations", headers=auth_headers(admin_token)).get_json()
    viewer = client.get("/api/integrations", headers=auth_headers(viewer_token)).get_json()

    admin_keys = {i["key"] for i in admin["data"]["items"]}
    viewer_keys = {i["key"] for i in viewer["data"]["items"]}

    # Asserted explicitly so this cannot pass vacuously on an empty viewer hub --
    # a subset check alone would still hold if permissions broke and the viewer
    # saw nothing at all.
    assert viewer_keys, "viewer should still see the integrations it has view rights on"
    assert viewer_keys < admin_keys

    # SMTP, Slack and webhooks are admin-only because the routes behind them are.
    assert not viewer_keys & {"smtp", "slack", "webhooks"}


def test_viewer_cannot_reach_an_admin_only_integration(client, viewer_token):
    response = client.get("/api/integrations/smtp", headers=auth_headers(viewer_token))
    assert response.status_code == 403


def test_viewer_cannot_trigger_a_test(client, viewer_token):
    """A viewer may read the result of someone else's test but not cause one."""
    response = client.post("/api/integrations/smtp/test", headers=auth_headers(viewer_token))
    assert response.status_code == 403


def test_describing_never_tests(client, admin_token, monkeypatch):
    """Every underlying test_connection commits last_test_* columns, so testing
    from a GET would rewrite history just by rendering the page -- and make the
    hub as slow as its slowest network round-trip."""
    spies = {}
    for name in TEST_FUNCTIONS:
        spy = Mock(side_effect=AssertionError(f"{name} called from a GET"))
        monkeypatch.setattr(svc, name, spy)
        spies[name] = spy

    assert client.get("/api/integrations", headers=auth_headers(admin_token)).status_code == 200

    for name, spy in spies.items():
        assert not spy.called, f"{name} was called while describing"


def test_one_broken_provider_does_not_blank_the_hub(client, admin_token, monkeypatch):
    def explode(_user, _can_manage):
        raise RuntimeError("jira exploded")

    monkeypatch.setitem(svc._BY_KEY["jira"], "fn", explode)

    response = client.get("/api/integrations", headers=auth_headers(admin_token))
    assert response.status_code == 200

    items = {i["key"]: i for i in response.get_json()["data"]["items"]}
    assert len(items) > 1, "the other providers should still be listed"

    broken = items["jira"]
    assert broken["name"] == "Jira"
    assert "jira exploded" in broken["message"]
    assert broken["actions"] == []


def test_smtp_offers_no_switch_it_does_not_have(client, admin_token):
    response = client.get("/api/integrations/smtp", headers=auth_headers(admin_token))
    assert response.status_code == 200
    actions = response.get_json()["data"]["actions"]
    assert "enable" not in actions and "disable" not in actions


def test_enabled_requires_the_field(client, admin_token):
    response = client.put(
        "/api/integrations/jira/enabled", headers=auth_headers(admin_token), json={}
    )
    assert response.status_code == 400


def test_activity_clamps_a_hostile_limit(client, admin_token, monkeypatch):
    seen = {}

    def capture(key, *, limit=50):
        seen["limit"] = limit
        return []

    monkeypatch.setattr(svc, "activity_for", capture)

    client.get("/api/integrations/jira/activity?limit=99999", headers=auth_headers(admin_token))
    assert seen["limit"] == 200

    client.get("/api/integrations/jira/activity?limit=-5", headers=auth_headers(admin_token))
    assert seen["limit"] == 1

    client.get("/api/integrations/jira/activity?limit=abc", headers=auth_headers(admin_token))
    assert seen["limit"] == 50
