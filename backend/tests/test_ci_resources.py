"""The three-layer resource envelope, on its own.

No database and no runner: these are the decisions themselves — what a user may
type, which layer wins, and the two couplings that exist because kubelet enforces
two separate ceilings. The runner tests prove the manifest that comes out of
them; these prove the rules.
"""

from __future__ import annotations

import pytest

from api.services.ci import resources


# ---------------------------------------------------------------------------
# What a user may type
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("value", ["2", "1.5", "500m", "256Mi", "8Gi", "10G"])
def test_quantities_a_human_would_type_are_accepted(value):
    assert resources.normalize({"memory": value}) == {"memory": value}


@pytest.mark.parametrize("word", ["off", "OFF", "none", "unlimited", "no"])
def test_every_off_word_normalizes_to_one_spelling(word):
    """The runner compares against one word. Accepting several spellings on the
    way in and storing them all would push that comparison into every reader."""
    assert resources.normalize({"cpu": word}) == {"cpu": "off"}


@pytest.mark.parametrize("value", ["8 gigs", "lots", "8GB", "-2", "8Gi;rm -rf /"])
def test_a_value_kubernetes_would_reject_is_rejected_here(value):
    """Rejected on save rather than at dispatch: the alternative is a pod that
    fails to create hours later, with the reason in an event nobody reads."""
    with pytest.raises(resources.ResourceError):
        resources.normalize({"ephemeralStorage": value})


def test_the_message_names_the_field_that_is_wrong():
    with pytest.raises(resources.ResourceError, match="Ephemeral storage"):
        resources.normalize({"ephemeralStorage": "loads"})


def test_an_empty_field_means_inherit_not_empty_string():
    """An empty box has to clear the override. Stored as "" it would reach the
    runner as a value and shadow the default it was meant to fall back to."""
    assert resources.normalize({"cpu": "2", "memory": ""}) == {"cpu": "2"}
    assert resources.normalize({"cpu": "", "memory": ""}) is None
    assert resources.normalize({}) is None
    assert resources.normalize(None) is None


def test_unknown_fields_are_dropped_rather_than_rejected():
    """A newer client sending a field this version does not know must not fail
    the save — it is additive by construction."""
    assert resources.normalize({"cpu": "2", "gpu": "1"}) == {"cpu": "2"}


# ---------------------------------------------------------------------------
# Which layer wins
# ---------------------------------------------------------------------------

def test_stage_beats_service_field_by_field():
    stage = {"ephemeralStorage": "16Gi"}
    service = {"cpu": "4", "ephemeralStorage": "8Gi"}

    assert resources.merge(stage, service) == {"cpu": "4", "ephemeralStorage": "16Gi"}


def test_off_is_an_answer_and_absent_is_not():
    """"No limit" on a stage has to beat a cap on the service. If "off" were
    treated as empty, the narrower choice would silently lose to the wider one."""
    assert resources.merge({"cpu": "off"}, {"cpu": "4"}) == {"cpu": "off"}
    assert resources.merge({}, {"cpu": "4"}) == {"cpu": "4"}


# ---------------------------------------------------------------------------
# Ephemeral storage: open by default, and what changes when it is not
# ---------------------------------------------------------------------------

def test_disk_is_open_out_of_the_box(monkeypatch):
    monkeypatch.delenv("CI_STAGE_EPHEMERAL_LIMIT", raising=False)
    monkeypatch.delenv("CI_STAGE_EPHEMERAL_REQUEST", raising=False)
    monkeypatch.delenv("CI_WORKSPACE_SIZE_LIMIT", raising=False)

    limit = resources.ephemeral_limit({}, scanning=False)
    assert resources.is_off(limit)
    assert resources.is_off(resources.ephemeral_request(limit))
    assert resources.is_off(resources.workspace_size_limit([], scanning=False))


def test_a_limit_brings_a_request_and_no_limit_brings_none(monkeypatch):
    """Without a request Kubernetes defaults the request TO the limit, so an 8Gi
    cap would demand 8Gi free on every candidate node."""
    monkeypatch.delenv("CI_STAGE_EPHEMERAL_REQUEST", raising=False)

    assert resources.ephemeral_request("8Gi") == "256Mi"
    assert resources.is_off(resources.ephemeral_request("off"))


def test_an_installation_request_wins_over_the_automatic_floor(monkeypatch):
    monkeypatch.setenv("CI_STAGE_EPHEMERAL_REQUEST", "1Gi")
    assert resources.ephemeral_request("8Gi") == "1Gi"


def test_a_chosen_limit_is_never_raised_for_a_scan(monkeypatch):
    """A ceiling that moves on its own is not a ceiling. The scan raise applies
    to the installation default, which nobody chose for this service."""
    monkeypatch.setenv("CI_STAGE_EPHEMERAL_LIMIT", "2Gi")

    assert resources.ephemeral_limit({"ephemeralStorage": "3Gi"}, scanning=True) == "3Gi"
    assert resources.ephemeral_limit({}, scanning=True) == "8Gi"
    assert resources.ephemeral_limit({}, scanning=False) == "2Gi"


def test_the_scan_raise_never_lowers_a_bigger_default(monkeypatch):
    monkeypatch.setenv("CI_STAGE_EPHEMERAL_LIMIT", "20Gi")
    assert resources.ephemeral_limit({}, scanning=True) == "20Gi"


# ---------------------------------------------------------------------------
# The second ceiling
# ---------------------------------------------------------------------------

def test_the_workspace_rises_to_the_largest_limit_granted(monkeypatch):
    """Kubelet evicts on whichever ceiling is hit first, so a stage granted 16Gi
    that kept a 2Gi workspace would die anyway — and say nothing about why."""
    monkeypatch.setenv("CI_WORKSPACE_SIZE_LIMIT", "2Gi")

    assert resources.workspace_size_limit(["4Gi", "16Gi", None], scanning=False) == "16Gi"


def test_an_explicit_uncap_removes_the_workspace_ceiling_too(monkeypatch):
    monkeypatch.setenv("CI_WORKSPACE_SIZE_LIMIT", "2Gi")

    assert resources.is_off(resources.workspace_size_limit(["off"], scanning=False))


def test_an_inherited_default_says_nothing_about_the_workspace(monkeypatch):
    """An installation may leave containers uncapped and still cap the volume
    they share — usually should. Only a CHOICE moves this ceiling."""
    monkeypatch.setenv("CI_WORKSPACE_SIZE_LIMIT", "5Gi")

    assert resources.workspace_size_limit([None, ""], scanning=False) == "5Gi"


def test_quantities_compare_across_units():
    assert resources.larger("1Gi", "900Mi") == "1Gi"
    assert resources.larger("2G", "2Gi") == "2Gi"  # 2Gi is the bigger of the two
    # An uncomparable value never wins by accident; the comparable one is kept.
    assert resources.larger("off", "8Gi") == "8Gi"
