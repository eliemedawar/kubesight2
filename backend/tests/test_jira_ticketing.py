"""The Jira ticketing provider, and the seam both providers now sit behind.

Three things are worth locking in, because each is somewhere the two providers
genuinely differ and a regression would be silent:

1. **The option publish is a diff, not a replace.** Zoho PATCHes a whole array;
   Jira has to create what is new, re-enable what came back, and *disable* what
   fell out — never delete, because Jira refuses to delete an option an existing
   issue uses. Getting that backwards would either lose the retired options'
   history or fail every sync on a busy project.

2. **The cascade is one field, not a mapping.** Jira has no dependency-mapping
   API, so the Environment → Application relationship is published as a
   cascading-select tree. With no cascade field configured, the honest outcome is
   "skipped" — not an error, and not a silent success.

3. **The two providers share one deploy surface and one intake log.** The source
   cluster/namespaces are one record, and the inbound-ticket table is filtered by
   provider so each tab shows only its own deliveries.

The HTTP layer is stubbed at ``jira_client._request`` — the same seam the Zoho
tests stub — so these exercise real client code (auth headers, paging, the diff)
without a network.
"""

import json
from urllib.parse import urlsplit

import pytest

from api.db import db
from api.models import JiraIntegration, ZohoInboundTicket
from api.services import jira_client
from api.services import jira_sync_service as svc
from api.services import ticketing
from api.services import ticketing_targets as targets
from api.services import zoho_sync_service as zoho_svc

from .conftest import auth_headers

APP_FIELD = "customfield_10050"
ENV_FIELD = "customfield_10051"
VAR_FIELD = "customfield_10052"
CASCADE_FIELD = "customfield_10060"
TAG_KEY = "customfield_10070"
CLUSTER = "prod-us-east"

DEPLOYMENTS = {"payments": ["payments-api"], "checkout": ["cart-svc"]}


def _cfg():
    return jira_client.JiraConfig(
        base_url="https://example.atlassian.net",
        deployment_type="cloud",
        email="zagent@areeba.com",
        api_token="tok",
        project_key="KUB",
        screen_id="10120",
        app_field_id=APP_FIELD,
        environment_field_id=ENV_FIELD,
    )


class FakeJira:
    """A tiny in-memory Jira: option contexts keyed by field id, plus a call log.

    Only the endpoints this integration touches are modelled. Anything else
    returns 404 so an unexpected call fails loudly instead of silently passing.
    """

    def __init__(self, options=None):
        # field id -> [{id, value, disabled, optionId?}]
        self.options = options or {}
        self.calls = []
        self._next_id = 900

    def _new_id(self):
        self._next_id += 1
        return str(self._next_id)

    def __call__(self, method, url, headers=None, body=None):
        payload = json.loads(body.decode()) if body else None
        self.calls.append((method, url, payload))
        # Paged reads carry ?startAt=…; match on the path so the query never
        # changes which endpoint a stub thinks it is answering.
        path = urlsplit(url).path

        if path.endswith("/myself"):
            return 200, {"displayName": "Z Agent", "accountId": "acc-1"}
        if "/project/" in path:
            return 200, {"key": "KUB", "name": "KubeSight"}
        if "/context/" in path and path.endswith("/option") and method == "GET":
            field = path.split("/field/")[1].split("/")[0]
            return 200, {"values": list(self.options.get(field, [])), "isLast": True}
        if path.endswith("/context"):
            field = path.split("/field/")[1].split("/")[0]
            return 200, {"values": [{"id": f"ctx-{field}", "isGlobalContext": True}], "isLast": True}
        if path.endswith("/option") and method == "POST":
            field = path.split("/field/")[1].split("/")[0]
            created = []
            for option in payload["options"]:
                row = {"id": self._new_id(), "value": option["value"], "disabled": False}
                if option.get("optionId"):
                    row["optionId"] = option["optionId"]
                self.options.setdefault(field, []).append(row)
                created.append(row)
            return 200, {"options": created}
        if path.endswith("/option") and method == "PUT":
            field = path.split("/field/")[1].split("/")[0]
            for update in payload["options"]:
                for row in self.options.get(field, []):
                    if str(row["id"]) == str(update["id"]):
                        row["disabled"] = bool(update.get("disabled"))
            return 200, {}
        if path.endswith("/option/move"):
            return 200, {}
        if path.endswith("/transitions") and method == "GET":
            return 200, {"transitions": [{"id": "31", "name": "Done", "to": {"name": "Done"}}]}
        if path.endswith("/transitions") and method == "POST":
            return 204, {}
        if path.endswith("/comment"):
            return 201, {"id": "c1"}
        if path.endswith("/assignee"):
            return 204, {}
        if path.endswith("/user/search"):
            return 200, [{"emailAddress": "zagent@areeba.com", "accountId": "acc-1"}]
        return 404, {"errorMessages": [f"unstubbed: {method} {url}"]}


@pytest.fixture()
def jira(app, monkeypatch):
    """A configured Jira integration over a shared, populated deploy surface."""
    row = JiraIntegration.query.get(1) or JiraIntegration(id=1)
    row.enabled = True
    row.base_url = "https://example.atlassian.net"
    row.email = "zagent@areeba.com"
    row.api_token_encrypted = None
    row.project_key = "KUB"
    row.screen_id = "10120"
    row.app_field_id = APP_FIELD
    row.app_field_api_name = APP_FIELD
    row.environment_field_id = ENV_FIELD
    row.environment_field_api_name = ENV_FIELD
    row.variable_field_id = VAR_FIELD
    row.variable_field_api_name = VAR_FIELD
    row.tag_field_api_name = TAG_KEY
    row.sync_application = True
    row.sync_environment = True
    row.sync_variables = False
    row.cascade_enabled = True
    db.session.add(row)

    source = targets.get_or_create_config()
    source.source_cluster_id = CLUSTER
    source.selected_namespaces = json.dumps(["payments", "checkout"])
    source.selected_deployments = None
    source.custom_environments = None
    db.session.add(source)
    db.session.commit()

    monkeypatch.setattr(
        zoho_svc,
        "_list_deployments_by_namespace",
        lambda cluster_id, namespaces, fresh=False: {
            ns: list(DEPLOYMENTS.get(ns) or []) for ns in namespaces
        },
    )
    # The token is decrypted from the row; short-circuit so the fixture does not
    # need a Fernet key set up.
    monkeypatch.setattr(svc, "_to_client_config", lambda row_: _cfg())
    return row


# ---------------------------------------------------------------------------
# Option publishing — the diff
# ---------------------------------------------------------------------------

def test_publishing_creates_new_options_and_disables_removed_ones(jira, monkeypatch):
    """The core difference from Zoho: options are reconciled, never replaced.

    'checkout' is already there (kept), 'legacy-ns' is gone from the cluster
    (disabled, NOT deleted — an old issue still references it), 'payments' is new.
    """
    fake = FakeJira(
        {
            ENV_FIELD: [
                {"id": "1", "value": "checkout", "disabled": False},
                {"id": "2", "value": "legacy-ns", "disabled": False},
            ]
        }
    )
    monkeypatch.setattr(jira_client, "_request", fake)

    result = jira_client.set_options(_cfg(), ENV_FIELD, ["payments", "checkout"])

    assert result.created == 1, "payments is new"
    assert result.disabled == 1, "legacy-ns fell out of the published list"
    assert result.unchanged == 1, "checkout was already published"

    by_value = {o["value"]: o for o in fake.options[ENV_FIELD]}
    assert by_value["legacy-ns"]["disabled"] is True
    assert by_value["checkout"]["disabled"] is False
    # Nothing was DELETEd — Jira refuses to delete an option an issue uses.
    assert not [c for c in fake.calls if c[0] == "DELETE"]


def test_a_returning_option_is_re_enabled_rather_than_duplicated(jira, monkeypatch):
    """A namespace that comes back must reuse its option, not create a second one."""
    fake = FakeJira({ENV_FIELD: [{"id": "1", "value": "payments", "disabled": True}]})
    monkeypatch.setattr(jira_client, "_request", fake)

    result = jira_client.set_options(_cfg(), ENV_FIELD, ["payments"])

    assert result.created == 0
    assert result.reenabled == 1
    assert len(fake.options[ENV_FIELD]) == 1
    assert fake.options[ENV_FIELD][0]["disabled"] is False


def test_option_matching_is_case_insensitive(jira, monkeypatch):
    """Values are canonicalized casefolded upstream, so a case-only change is a
    no-op rather than a duplicate pair of options."""
    fake = FakeJira({ENV_FIELD: [{"id": "1", "value": "Payments", "disabled": False}]})
    monkeypatch.setattr(jira_client, "_request", fake)

    result = jira_client.set_options(_cfg(), ENV_FIELD, ["payments"])

    assert result.created == 0
    assert result.disabled == 0
    assert len(fake.options[ENV_FIELD]) == 1


def test_sync_publishes_both_fields_and_records_the_counts(jira, monkeypatch):
    fake = FakeJira()
    monkeypatch.setattr(jira_client, "_request", fake)

    result = svc.sync_now()

    assert result["status"] == "ok", result
    assert result["deployments"] == 2, "payments-api + cart-svc"
    assert result["namespaces"] == 2, "payments + checkout"
    published = {o["value"] for o in fake.options[APP_FIELD]}
    assert published == {"payments-api", "cart-svc"}
    # Zoho's "-None-" placeholder is meaningless in Jira (an unset select is
    # simply empty) and must never reach the option list.
    assert "-None-" not in published


def test_a_failed_cluster_read_does_not_touch_jira(jira, monkeypatch):
    """A bad source read is recorded WITHOUT writing — the alternative is emptying
    the production dropdowns because kubectl blipped."""
    fake = FakeJira()
    monkeypatch.setattr(jira_client, "_request", fake)
    monkeypatch.setattr(
        zoho_svc, "_source_entries", lambda row, fresh=False: (_ for _ in ()).throw(ValueError("cluster unreachable"))
    )

    result = svc.sync_now()

    assert result["status"] == "error"
    assert "cluster unreachable" in result["message"]
    assert fake.calls == [], "nothing was written to Jira"


# ---------------------------------------------------------------------------
# Cascade — one cascading-select field, not a mapping
# ---------------------------------------------------------------------------

def test_cascade_is_skipped_with_an_explanation_when_no_cascade_field_is_set(jira, monkeypatch):
    """Jira cannot filter one field by another, so without a cascading-select
    field there is genuinely nothing to write. That is 'skipped', not an error —
    the flat dropdowns published fine."""
    monkeypatch.setattr(jira_client, "_request", FakeJira())

    result = svc.sync_now()

    assert result["cascade"]["status"] == "skipped"
    assert "cascade field" in result["cascade"]["message"].lower()


def test_cascade_publishes_the_environment_application_tree(jira, monkeypatch):
    jira.cascade_field_id = CASCADE_FIELD
    jira.cascade_field_api_name = CASCADE_FIELD
    db.session.commit()
    fake = FakeJira()
    monkeypatch.setattr(jira_client, "_request", fake)

    result = svc.sync_now()

    assert result["cascade"]["status"] == "ok", result["cascade"]
    tree = fake.options[CASCADE_FIELD]
    parents = {o["value"]: o["id"] for o in tree if not o.get("optionId")}
    assert set(parents) == {"payments", "checkout"}
    # Each application hangs off its own namespace's option id.
    children = {o["value"]: o["optionId"] for o in tree if o.get("optionId")}
    assert children == {
        "payments-api": parents["payments"],
        "cart-svc": parents["checkout"],
    }


def test_cascade_failure_does_not_fail_the_sync(jira, monkeypatch):
    """The option lists are already published by then; a cascade problem is a
    degraded feature, not a reason to report the whole sync as failed."""
    jira.cascade_field_id = CASCADE_FIELD
    db.session.commit()

    fake = FakeJira()
    original = fake.__call__

    def explode(method, url, headers=None, body=None):
        if CASCADE_FIELD in url and method == "POST":
            return 403, {"errorMessages": ["not an administrator"]}
        return original(method, url, headers=headers, body=body)

    monkeypatch.setattr(jira_client, "_request", explode)

    result = svc.sync_now()

    assert result["status"] == "ok"
    assert result["cascade"]["status"] == "error"
    assert "administrator" in result["cascade"]["message"]


# ---------------------------------------------------------------------------
# Inbound
# ---------------------------------------------------------------------------

def test_inbound_resolves_an_issue_to_an_exact_deployment(jira, monkeypatch):
    monkeypatch.setattr(jira_client, "_request", FakeJira())
    svc.sync_now()  # creates the deployment snapshots the resolver matches against

    result = svc.resolve_inbound(
        {
            "issue": {
                "id": "10001",
                "key": "KUB-7",
                "fields": {
                    "summary": "Deploy payments-api",
                    APP_FIELD: {"value": "payments-api"},
                    ENV_FIELD: {"value": "payments"},
                    TAG_KEY: "v1.4.2",
                },
            }
        }
    )

    assert result["resolved"] is True, result
    assert result["deploymentName"] == "payments-api"
    assert result["namespace"] == "payments"
    assert result["tag"] == "v1.4.2"
    record = ZohoInboundTicket.query.get(result["recordId"])
    assert record.provider == "jira", "the shared intake log records which system sent it"


def test_inbound_reads_both_halves_of_a_cascading_select(jira, monkeypatch):
    """A cascading select carries the environment AND the application in one
    value — the flat fields need not be present at all."""
    jira.cascade_field_id = CASCADE_FIELD
    jira.cascade_field_api_name = CASCADE_FIELD
    db.session.commit()
    monkeypatch.setattr(jira_client, "_request", FakeJira())
    svc.sync_now()

    result = svc.resolve_inbound(
        {
            "issue": {
                "key": "KUB-8",
                "fields": {
                    CASCADE_FIELD: {"value": "checkout", "child": {"value": "cart-svc"}},
                    TAG_KEY: "v2.0.0",
                },
            }
        }
    )

    assert result["resolved"] is True, result
    assert result["namespace"] == "checkout"
    assert result["deploymentName"] == "cart-svc"


def test_an_issue_with_no_change_is_recorded_with_a_useful_error(jira, monkeypatch):
    monkeypatch.setattr(jira_client, "_request", FakeJira())
    svc.sync_now()

    result = svc.resolve_inbound(
        {"issue": {"key": "KUB-9", "fields": {APP_FIELD: {"value": "payments-api"},
                                              ENV_FIELD: {"value": "payments"}}}}
    )

    assert result["resolved"] is True, "the deployment resolved fine"
    assert "neither a Tag nor a Variable" in result["error"]


def test_the_inbound_log_is_filtered_per_provider(jira, monkeypatch):
    """The intake table is shared, so each provider's tab must show only its own."""
    monkeypatch.setattr(jira_client, "_request", FakeJira())
    svc.sync_now()
    svc.resolve_inbound(
        {"issue": {"key": "KUB-10", "fields": {APP_FIELD: {"value": "payments-api"},
                                               ENV_FIELD: {"value": "payments"},
                                               TAG_KEY: "v1"}}}
    )
    db.session.add(ZohoInboundTicket(provider="zoho", ticket_id="Z-1", resolved=True))
    db.session.commit()

    jira_ids = [t["ticketId"] for t in svc.list_inbound_tickets(50)]
    zoho_ids = [t["ticketId"] for t in zoho_svc.list_inbound_tickets(50)]

    assert jira_ids == ["KUB-10"]
    assert zoho_ids == ["Z-1"]


# ---------------------------------------------------------------------------
# Write-back
# ---------------------------------------------------------------------------

def test_writeback_runs_the_configured_transition_and_comments(jira, monkeypatch):
    jira.ticket_writeback_enabled = True
    jira.api_token_encrypted = "x"  # only its truthiness is checked before dispatch
    jira.transition_deployed = "Done"
    jira.ticket_owner_email = "zagent@areeba.com"
    db.session.commit()
    fake = FakeJira()
    monkeypatch.setattr(jira_client, "_request", fake)

    svc.report_ticket_outcome("KUB-7", "deployed", comment="Deployed.", resolution="All good.")

    posts = [c for c in fake.calls if c[0] == "POST"]
    assert any("/transitions" in url for _m, url, _b in posts), "the transition ran"
    comment = next(b for m, url, b in posts if "/comment" in url)
    # Jira has no separate resolution field, so the resolution is appended to the
    # comment rather than dropped.
    text = json.dumps(comment)
    assert "Deployed." in text and "All good." in text


def test_an_unavailable_transition_is_skipped_not_raised(jira, monkeypatch):
    """Someone closing the issue by hand removes the transition. That is a normal
    outcome, and it must not take down the assignee/comment that follow it."""
    jira.ticket_writeback_enabled = True
    jira.api_token_encrypted = "x"
    jira.transition_deployed = "Ship It"  # not offered by the stub workflow
    db.session.commit()
    fake = FakeJira()
    monkeypatch.setattr(jira_client, "_request", fake)

    svc.report_ticket_outcome("KUB-7", "deployed", comment="Deployed.")

    assert not [c for c in fake.calls if c[0] == "POST" and "/transitions" in c[1]]
    assert [c for c in fake.calls if "/comment" in c[1]], "the comment still posted"


# ---------------------------------------------------------------------------
# The shared seam
# ---------------------------------------------------------------------------

def test_both_providers_read_one_deploy_surface(jira):
    """Configuring the source from either tab configures it for both — a cluster
    is a cluster whichever system raised the ticket."""
    zoho_cfg = zoho_svc.get_config_dict()
    jira_cfg = svc.get_config_dict()

    assert zoho_cfg["sourceClusterId"] == jira_cfg["sourceClusterId"] == CLUSTER
    assert zoho_cfg["selectedNamespaces"] == jira_cfg["selectedNamespaces"] == [
        "payments",
        "checkout",
    ]

    svc.set_source(CLUSTER, ["payments"], None, None, None)
    assert zoho_svc.get_config_dict()["selectedNamespaces"] == ["payments"]


def test_enabling_jira_requires_the_essentials(app):
    with pytest.raises(ValueError) as exc:
        svc.update_config({"enabled": True})
    message = str(exc.value)
    for required in ("Base URL", "Project key", "Screen ID", "Application field ID", "API token"):
        assert required in message


def test_a_pasted_rest_suffix_is_stripped_from_the_site_url(app):
    """Operators copy the URL out of an API doc as often as out of the browser
    bar; the client appends the REST root itself, so a doubled suffix 404s with
    an error that says nothing about the real cause."""
    saved = svc.update_config({"baseUrl": "https://example.atlassian.net/rest/api/3/"})
    assert saved["baseUrl"] == "https://example.atlassian.net"


def test_the_registry_reports_both_providers(app):
    keys = ticketing.keys()
    assert set(keys) == {"jira", "zoho"}
    described = {p["key"]: p for p in ticketing.describe_all()}
    # The capability flags are what let one UI serve both without branching on
    # the provider key.
    assert described["jira"]["capabilities"]["createSections"] is True
    assert described["zoho"]["capabilities"]["createSections"] is False
    assert described["jira"]["capabilities"]["convertField"] is False
    assert described["zoho"]["capabilities"]["convertField"] is True


def test_the_providers_endpoint_is_permission_gated(app, client, admin_token):
    assert client.get("/api/ticketing/providers").status_code == 401
    res = client.get("/api/ticketing/providers", headers=auth_headers(admin_token))
    assert res.status_code == 200
    assert {p["key"] for p in res.get_json()["data"]["items"]} == {"jira", "zoho"}


def test_an_unknown_provider_is_a_404_not_a_crash(app, client, admin_token):
    res = client.get("/api/ticketing/servicenow/config", headers=auth_headers(admin_token))
    assert res.status_code == 404
    assert "servicenow" in res.get_json()["error"]


def test_an_unsupported_operation_answers_501_with_a_reason(app, client, admin_token):
    """Jira cannot convert a text field to a dropdown. The route says so rather
    than blowing up on a function the module never had."""
    res = client.get(
        f"/api/ticketing/jira/fields/{TAG_KEY}/convert", headers=auth_headers(admin_token)
    )
    assert res.status_code == 501
    assert "Jira does not support" in res.get_json()["error"]
