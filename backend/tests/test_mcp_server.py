"""KubeSight as an MCP server.

Two things are being protected here, and they are not the same thing.

The **protocol** has to be right or no client connects at all — and the failure
modes are quiet ones: answering a notification, or sending a body where the spec
says none. A client following the rules hangs or disconnects, and the error it
reports points somewhere else entirely.

The **boundary** has to be right or an agent sees more than the person who gave
it a token, or changes more than it was meant to. Every tool checks a
permission, the tools that write are enumerated rather than counted, and each
tool's annotations say which it is — all three are asserted here rather than
left to the reading of the module.

The **surface** is the third thing, and it is new. Eighty tools is past what
anybody verifies by hand, so the last two tests in this file call every read
tool once and refuse to let one go untested quietly. They catch the cheap
mistake this kind of change makes: a relative import that is one dot short, or
a service signature that moved. Both raise on the first call and never before.
"""

from __future__ import annotations

import pathlib
import re

import pytest

from api.db import db
from api.models_ci import CiPipeline, CiPipelineStage, CiRunner, CiSecret, CiService
from tests.conftest import auth_headers


def rpc(client, token, method, params=None, request_id=1):
    body = {"jsonrpc": "2.0", "method": method}
    if request_id is not None:
        body["id"] = request_id
    if params is not None:
        body["params"] = params
    return client.post("/api/mcp", json=body, headers=auth_headers(token))


def call_tool(client, token, name, arguments=None):
    response = rpc(client, token, "tools/call", {"name": name, "arguments": arguments or {}})
    assert response.status_code == 200, response.get_data(as_text=True)
    return response.get_json()["result"]


@pytest.fixture()
def service(app):
    row = CiService(
        name="Issuing", slug="issuing", application_type="java_gradle",
        repository_provider="bitbucket",
        repository_url="https://bitbucket.org/areebasal/issuing.git",
        repository_workspace="areebasal", repository_name="issuing",
        default_branch="main",
    )
    db.session.add(row)
    db.session.flush()
    # The stage below references it. A saved reference to a secret that does not
    # exist is a state the validator refuses to create, so the fixture must not
    # start there either.
    db.session.add(
        CiSecret(scope="service", service_id=row.id, key="NEXUS_USER", value_cipher="x")
    )
    pipeline = CiPipeline(service_id=row.id, name="default", is_default=True)
    db.session.add(pipeline)
    db.session.flush()
    pipeline.stages.append(
        CiPipelineStage(
            position=0, name="Build JAR", stage_type="command",
            image="registry.areeba.com/gradle:9.1.0-jdk25-alpine",
            runner_labels=["linux", "java"], commands=["gradle clean build"],
            secret_refs=[{"name": "NEXUS_USER", "envVar": "NEXUS_USER"}],
        )
    )
    db.session.commit()
    return row


# ---------------------------------------------------------------------------
# Protocol
# ---------------------------------------------------------------------------

def test_initialize_agrees_a_version_and_advertises_only_what_exists(client, admin_token):
    """Claiming a capability this server does not implement leaves a client
    waiting for a feature that never arrives."""
    result = rpc(client, admin_token, "initialize",
                 {"protocolVersion": "2025-06-18", "capabilities": {}}).get_json()["result"]

    assert result["protocolVersion"] == "2025-06-18"
    assert result["serverInfo"]["name"] == "kubesight"
    assert result["capabilities"] == {"tools": {"listChanged": False}}
    assert "resources" not in result["capabilities"]
    # The instructions are read once and carried all session, and with a surface
    # this wide their job is routing: name every domain, so a model picks the
    # right tool out of eighty without reading eighty descriptions.
    instructions = result["instructions"]
    for domain in ("ci", "clusters", "workloads", "deploys", "observability", "apps", "platform"):
        assert domain in instructions
    # And say where the gates are. A model that assumes it can deploy to an
    # approval-gated cluster wastes a call and reports a rule as a failure.
    assert "kubesight_deploy_eligibility" in instructions
    assert "approv" in instructions.lower()


def test_an_unfamiliar_protocol_version_is_answered_not_refused(client, admin_token):
    """Clients ship ahead of servers. Refusing a version string this server has
    not heard of breaks every one of them."""
    result = rpc(client, admin_token, "initialize",
                 {"protocolVersion": "2099-01-01"}).get_json()["result"]
    assert result["protocolVersion"] == "2025-06-18"


def test_a_notification_is_never_answered(client, admin_token):
    """A JSON-RPC notification has no id and MUST NOT get a reply. A client
    following the spec disconnects when it gets one, and blames the next call."""
    response = client.post(
        "/api/mcp",
        json={"jsonrpc": "2.0", "method": "notifications/initialized"},
        headers=auth_headers(admin_token),
    )
    assert response.status_code == 202
    assert response.get_data() == b""


def test_ping_answers(client, admin_token):
    assert rpc(client, admin_token, "ping").get_json()["result"] == {}


def test_a_batch_returns_one_reply_per_request(client, admin_token):
    response = client.post(
        "/api/mcp",
        json=[
            {"jsonrpc": "2.0", "id": 1, "method": "ping"},
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
        ],
        headers=auth_headers(admin_token),
    )
    replies = response.get_json()
    # Two requests, one notification — two replies.
    assert [item["id"] for item in replies] == [1, 2]


def test_an_unknown_method_is_a_jsonrpc_error_not_a_crash(client, admin_token):
    error = rpc(client, admin_token, "resources/list").get_json()["error"]
    assert error["code"] == -32601


def test_malformed_json_is_rejected_cleanly(client, admin_token):
    response = client.post(
        "/api/mcp", data="{not json", content_type="application/json",
        headers=auth_headers(admin_token),
    )
    assert response.status_code == 400
    assert response.get_json()["error"]["code"] == -32700


def test_discovery_needs_no_token_and_leaks_nothing(client):
    """Somebody pointing a client at the wrong URL should get an answer rather
    than a 405 — but not a list of this installation's services."""
    payload = client.get("/api/mcp").get_json()
    assert payload["protocol"] == "mcp"
    assert payload["readOnly"] is False
    # "readOnly: false" alone says a write exists but not how far it reaches,
    # and that second thing is what somebody deciding to hand over a token needs.
    # With writes scoped by the token rather than by a fixed list, the honest
    # summary is the shape of the gates: permissions, and the approvals that
    # survive them.
    assert "permissions" in payload["writes"]
    assert "approv" in payload["writes"].lower()
    assert payload["domains"]
    assert "tools" not in payload


def test_the_endpoint_requires_authentication(client):
    response = client.post(
        "/api/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"}
    )
    assert response.status_code in (401, 403)


# ---------------------------------------------------------------------------
# The boundary
# ---------------------------------------------------------------------------

def test_every_tool_declares_honestly_whether_it_writes(client, admin_token):
    """A client that asks a person before a write can only do that if the tools
    that write say so. A tool that mutated while claiming readOnlyHint would take
    that decision away from them silently."""
    tools = rpc(client, admin_token, "tools/list").get_json()["result"]["tools"]
    assert tools
    for entry in tools:
        assert entry["name"].startswith("kubesight_")
        assert entry["description"] and entry["inputSchema"]
        assert isinstance(entry["annotations"]["readOnlyHint"], bool)

    writing = {
        entry["name"]
        for entry in tools
        if not entry["annotations"]["readOnlyHint"]
    }
    # Enumerated, not counted: a new write tool should have to be added here
    # deliberately rather than slipping in under a threshold. This list IS the
    # review — everything an agent holding an admin token can change.
    assert writing == {
        # CI: the pipeline, and running one
        "kubesight_pipeline_save",
        "kubesight_pipeline_stage_add",
        "kubesight_pipeline_stage_remove",
        "kubesight_pipeline_stage_update",
        # CI: the image recipe. Writes KubeSight's own copy only — there is no
        # tool that commits to a repository, so an edit here can never reach the
        # source. Storing one on a service that had none is the change worth
        # noticing: every later build then ignores the repository's Dockerfile.
        "kubesight_dockerfile_edit",
        "kubesight_dockerfile_set",
        "kubesight_build_run",
        "kubesight_build_cancel",
        "kubesight_build_retry",
        # Merge checks: moves the quality gate for every service that inherits
        # it. Nothing here can switch a service's checks off or re-send a
        # verdict — an agent relaxing a gate to get a merge through is the exact
        # failure the gate exists to prevent.
        "kubesight_merge_check_policy_set",
        # Workloads
        "kubesight_workload_restart",
        "kubesight_workload_scale",
        "kubesight_workload_rollback",
        "kubesight_resource_restart",
        "kubesight_pod_exec",
        # Deploys, and asking to be allowed one
        "kubesight_deploy_apply",
        "kubesight_deployment_request_create",
        "kubesight_helm_upgrade",
        "kubesight_helm_rollback",
        "kubesight_helm_uninstall",
        # Observability
        "kubesight_alert_policy_set_enabled",
        # Platform
        "kubesight_automation_run_start",
        "kubesight_automation_run_cancel",
    }


def test_approving_a_change_is_not_something_an_agent_can_do(client, admin_token):
    """An agent that can both request a deploy and approve it is an approval
    process with one participant. The request tool exists; no voting tool does,
    even for a token that holds the managing permission."""
    names = {
        entry["name"]
        for entry in rpc(client, admin_token, "tools/list").get_json()["result"]["tools"]
    }
    assert "kubesight_deployment_request_create" in names
    for forbidden in ("approve", "reject", "decline", "vote"):
        assert not any(forbidden in name for name in names)


def test_the_tool_list_is_scoped_to_what_the_token_may_call(client, admin_token, viewer_token):
    """Not the security boundary — ``call`` re-checks, and that is. This is the
    economy one: a viewer's agent should not spend context reading about tools
    that will refuse it, or plan around them."""
    def names(token):
        return {
            entry["name"]
            for entry in rpc(client, token, "tools/list").get_json()["result"]["tools"]
        }

    admin_names, viewer_names = names(admin_token), names(viewer_token)
    assert viewer_names < admin_names
    assert "kubesight_services_list" in viewer_names
    assert "kubesight_pod_exec" not in viewer_names


def test_every_tool_names_a_domain_the_skill_is_split_by(client, admin_token):
    """The grouping is what makes eighty tools navigable: it is the same seven
    words the skill's reference files are named after, so an agent reads one
    page rather than all of them."""
    from api.mcp.tools import DOMAINS

    tools = rpc(client, admin_token, "tools/list").get_json()["result"]["tools"]
    seen = {entry["annotations"]["kubesightDomain"] for entry in tools}
    assert seen <= set(DOMAINS)
    # Every declared domain actually has tools in it — an empty one is a
    # reference file nobody will ever need.
    assert seen == set(DOMAINS)


def test_nothing_that_reads_is_marked_destructive(client, admin_token):
    tools = rpc(client, admin_token, "tools/list").get_json()["result"]["tools"]
    for entry in tools:
        if entry["annotations"]["readOnlyHint"]:
            assert entry["annotations"]["destructiveHint"] is False


def test_a_tool_runs_as_the_token_holder_not_as_the_server(client, viewer_token, service):
    """There is no MCP identity. An agent is exactly as privileged as the token
    it was handed — which is what makes handing one over a decision somebody can
    reason about."""
    result = call_tool(client, viewer_token, "kubesight_runners_list")
    assert result["isError"] is True
    assert "ci_runners:view" in result["content"][0]["text"]


def test_the_same_token_still_reads_what_it_may(client, viewer_token, service):
    result = call_tool(client, viewer_token, "kubesight_services_list")
    assert not result.get("isError")
    assert result["structuredContent"]["count"] >= 1


def test_an_unknown_tool_names_the_real_ones(client, admin_token):
    """A model that guessed a name should be able to correct itself from the
    answer rather than guessing again."""
    result = call_tool(client, admin_token, "kubesight_delete_everything")
    assert result["isError"] is True
    assert "kubesight_overview" in result["content"][0]["text"]


def test_a_tool_failure_is_reported_inside_the_conversation(client, admin_token):
    """isError keeps the reason where the model can read it. A transport error
    would fail the call and hide why."""
    result = call_tool(client, admin_token, "kubesight_service_get", {"service": "nope"})
    assert result["isError"] is True
    assert "No service 'nope'" in result["content"][0]["text"]


# ---------------------------------------------------------------------------
# The answers
# ---------------------------------------------------------------------------

def test_overview_says_whether_anything_can_build_at_all(client, admin_token, service):
    """The most common reason a build sits queued is that no runner is online,
    and it is the least obvious thing to go looking for."""
    payload = call_tool(client, admin_token, "kubesight_overview")["structuredContent"]
    assert "services" in payload and "onlineRunners" in payload
    assert isinstance(payload["failing"], list)
    assert "issuing" in payload["needsSetup"]


def test_a_service_can_be_found_by_slug_or_id(client, admin_token, service):
    """Agents have whichever is to hand."""
    by_slug = call_tool(client, admin_token, "kubesight_service_get", {"service": "issuing"})
    by_id = call_tool(client, admin_token, "kubesight_service_get", {"service": str(service.id)})
    assert by_slug["structuredContent"]["service"]["slug"] == "issuing"
    assert by_id["structuredContent"]["service"]["slug"] == "issuing"


def test_a_service_answer_says_why_it_cannot_build(client, admin_token, service):
    payload = call_tool(
        client, admin_token, "kubesight_service_get", {"service": "issuing"}
    )["structuredContent"]
    assert "blockedReason" in payload
    assert "readiness" in payload


def test_a_pipeline_answer_shows_the_commands_and_names_the_secrets(
    client, admin_token, service
):
    """Names and destination variables, never values — a pipeline stores
    references, and so does this. The env var travels with the name because an
    agent rewriting the stage has to be able to put it back unchanged."""
    payload = call_tool(
        client, admin_token, "kubesight_pipeline_get", {"service": "issuing"}
    )["structuredContent"]

    stage = payload["stages"][0]
    assert stage["name"] == "Build JAR"
    assert stage["commands"] == ["gradle clean build"]
    assert stage["secretRefs"] == [{"name": "NEXUS_USER", "envVar": "NEXUS_USER"}]
    assert "value" not in str(stage["secretRefs"])


def test_an_answer_comes_back_readable_and_structured(client, admin_token, service):
    """The summary is what a model reads deciding what to ask next; the structure
    is what it reads when answering precisely. Only one of the two would make the
    other expensive."""
    result = call_tool(client, admin_token, "kubesight_services_list")
    assert result["content"][0]["type"] == "text"
    assert "services" in result["content"][0]["text"]
    assert isinstance(result["structuredContent"], dict)


def test_the_build_environments_are_readable(client, admin_token):
    payload = call_tool(client, admin_token, "kubesight_build_environments")[
        "structuredContent"
    ]
    keys = {item["key"] for item in payload["environments"]}
    assert "gradle-9-jdk25" in keys


def test_calling_a_tool_is_audited_without_recording_the_answer(
    client, admin_token, service
):
    """An agent reading the catalog is not interesting. An agent reading it
    repeatedly with somebody's token is."""
    from api.models import AuditLog

    before = AuditLog.query.filter_by(action="mcp_tools_called").count()
    call_tool(client, admin_token, "kubesight_overview")
    rows = AuditLog.query.filter_by(action="mcp_tools_called").all()

    assert len(rows) == before + 1
    assert "kubesight_overview" in str(rows[-1].details)
    # The arguments and the result are not recorded: a service slug is harmless,
    # but the habit is how something sensitive ends up in an audit row.
    assert "structuredContent" not in str(rows[-1].details)


def test_listing_tools_is_not_audited(client, admin_token):
    """Only calls are worth a row. Auditing discovery would bury them."""
    from api.models import AuditLog

    before = AuditLog.query.filter_by(action="mcp_tools_called").count()
    rpc(client, admin_token, "tools/list")
    assert AuditLog.query.filter_by(action="mcp_tools_called").count() == before


# ---------------------------------------------------------------------------
# The source a service builds from
# ---------------------------------------------------------------------------

@pytest.fixture()
def sourced(app, service, monkeypatch):
    """The same service, with a credential and a stubbed repository behind it.

    The provider is stubbed rather than the HTTP client so the test exercises
    the whole path an agent takes — tool, catalog, port — and stops exactly
    where Bitbucket would be.
    """
    from api.models_application_intelligence import BitbucketCredentialProfile
    from api.services.ci import source as source_port
    from api.services.ci.source import RevisionOption, TreeListing

    credential = BitbucketCredentialProfile(
        name="ci-read", provider="bitbucket", credential_type="app_password",
        principal="ci", secret_cipher="x", enabled=True,
    )
    db.session.add(credential)
    db.session.flush()
    service.credential_profile_id = credential.id
    db.session.commit()

    provider = source_port.get_provider("bitbucket")
    monkeypatch.setattr(
        provider, "list_tree",
        lambda ref, cred, revision: TreeListing(
            revision=revision,
            paths=["build.gradle", "settings.gradle", "src/main/App.java", "src/test/AppTest.java"],
        ),
    )
    monkeypatch.setattr(
        provider, "read_file",
        lambda ref, cred, revision, path: "\n".join(f"line {n}" for n in range(1, 11)),
    )
    monkeypatch.setattr(
        provider, "list_revisions",
        lambda ref, cred, kinds=(): [
            RevisionOption(value="main", label="Branch — main", kind="branch", commit="a" * 40),
            RevisionOption(value="v1.2", label="Tag — v1.2", kind="tag", commit="b" * 40),
        ],
    )
    return service


def test_the_repository_tree_shows_the_shape_of_the_project(client, admin_token, sourced):
    payload = call_tool(
        client, admin_token, "kubesight_repo_tree", {"service": "issuing"}
    )["structuredContent"]

    assert payload["repository"] == "areebasal/issuing"
    assert "build.gradle" in payload["paths"]
    assert payload["truncated"] is False


def test_a_path_prefix_narrows_the_tree(client, admin_token, sourced):
    """Reading a whole repository into a model is the expensive way to answer
    almost every question about it."""
    payload = call_tool(
        client, admin_token, "kubesight_repo_tree",
        {"service": "issuing", "pathPrefix": "src/test"},
    )["structuredContent"]

    assert payload["paths"] == ["src/test/AppTest.java"]


def test_a_tree_cut_short_says_so(client, admin_token, sourced):
    """An absent path in a truncated listing means 'not seen', not 'not there'.
    Anything concluding a repository has no Dockerfile has to know which."""
    payload = call_tool(
        client, admin_token, "kubesight_repo_tree", {"service": "issuing", "limit": 2}
    )["structuredContent"]

    assert len(payload["paths"]) == 2
    assert payload["truncated"] is True


def test_a_file_comes_back_with_its_line_count(client, admin_token, sourced):
    payload = call_tool(
        client, admin_token, "kubesight_repo_file",
        {"service": "issuing", "path": "build.gradle"},
    )["structuredContent"]

    assert payload["path"] == "build.gradle"
    assert payload["totalLines"] == 10
    assert payload["truncated"] is False
    assert payload["content"].startswith("line 1")


def test_a_line_window_is_honoured_and_declared(client, admin_token, sourced):
    payload = call_tool(
        client, admin_token, "kubesight_repo_file",
        {"service": "issuing", "path": "build.gradle", "startLine": 3, "endLine": 5},
    )["structuredContent"]

    assert payload["content"] == "line 3\nline 4\nline 5"
    assert (payload["startLine"], payload["endLine"]) == (3, 5)
    # The window is not the whole file, and the answer says so rather than
    # letting a model conclude the file ends at line 5.
    assert payload["truncated"] is True


def test_revisions_can_be_narrowed_to_one_kind(client, admin_token, sourced):
    payload = call_tool(
        client, admin_token, "kubesight_repo_revisions",
        {"service": "issuing", "kinds": ["branch"]},
    )["structuredContent"]

    assert payload["defaultBranch"] == "main"
    assert payload["revisions"][0]["value"] == "main"


def test_reading_source_needs_a_connected_repository_and_says_which(
    client, admin_token, service
):
    """No credential, so the reason is a setting somebody can go and fix."""
    result = call_tool(
        client, admin_token, "kubesight_repo_tree", {"service": "issuing"}
    )
    assert result["isError"] is True
    assert "Connect a repository" in result["content"][0]["text"]


def test_the_source_tools_take_no_repository_of_their_own(client, admin_token):
    """An agent cannot aim KubeSight's stored credentials at a repository nobody
    registered — every source tool starts from a service."""
    tools = rpc(client, admin_token, "tools/list").get_json()["result"]["tools"]
    for entry in tools:
        if entry["name"].startswith("kubesight_repo_"):
            properties = set(entry["inputSchema"]["properties"])
            assert "service" in entry["inputSchema"]["required"]
            assert not properties & {"repositoryUrl", "credentialProfileId", "token"}


# ---------------------------------------------------------------------------
# Editing a pipeline
# ---------------------------------------------------------------------------

def stages_of(client, token, slug="issuing"):
    return call_tool(client, token, "kubesight_pipeline_get", {"service": slug})[
        "structuredContent"
    ]["stages"]


def test_a_one_field_edit_keeps_everything_it_did_not_mention(
    client, admin_token, service
):
    """The point of the partial edit. The agent sends one field; the image, the
    labels and the secret reference it never saw are still there afterwards."""
    result = call_tool(
        client, admin_token, "kubesight_pipeline_stage_update",
        {
            "service": "issuing",
            "stage": "Build JAR",
            "changes": {"commands": ["gradle clean build -x test"]},
        },
    )
    assert not result.get("isError"), result["content"][0]["text"]

    stage = stages_of(client, admin_token)[0]
    assert stage["commands"] == ["gradle clean build -x test"]
    assert stage["image"] == "registry.areeba.com/gradle:9.1.0-jdk25-alpine"
    assert stage["runnerLabels"] == ["linux", "java"]
    assert stage["secretRefs"] == [{"name": "NEXUS_USER", "envVar": "NEXUS_USER"}]


def test_a_stage_can_be_addressed_by_position_as_well_as_name(
    client, admin_token, service
):
    result = call_tool(
        client, admin_token, "kubesight_pipeline_stage_update",
        {"service": "issuing", "stage": "1", "changes": {"enabled": False}},
    )
    assert not result.get("isError"), result["content"][0]["text"]
    assert stages_of(client, admin_token)[0]["enabled"] is False


def test_an_unknown_stage_names_the_real_ones(client, admin_token, service):
    """A model that guessed should be able to correct itself from the answer."""
    result = call_tool(
        client, admin_token, "kubesight_pipeline_stage_update",
        {"service": "issuing", "stage": "Compile", "changes": {"enabled": False}},
    )
    assert result["isError"] is True
    assert "Build JAR" in result["content"][0]["text"]


def test_a_misspelled_field_is_refused_rather_than_ignored(
    client, admin_token, service
):
    """normalize_stage would drop an unknown key silently, and the agent would
    report a change that never happened."""
    result = call_tool(
        client, admin_token, "kubesight_pipeline_stage_update",
        {"service": "issuing", "stage": "Build JAR", "changes": {"command": ["make"]}},
    )
    assert result["isError"] is True
    assert "commands" in result["content"][0]["text"]
    assert stages_of(client, admin_token)[0]["commands"] == ["gradle clean build"]


def test_a_stage_is_added_where_it_was_asked_for(client, admin_token, service):
    result = call_tool(
        client, admin_token, "kubesight_pipeline_stage_add",
        {
            "service": "issuing",
            "after": "start",
            "stage": {
                "name": "Checkout", "stageType": "checkout",
                "image": "registry.areeba.com/gradle:9.1.0-jdk25-alpine",
            },
        },
    )
    assert not result.get("isError"), result["content"][0]["text"]

    names = [stage["name"] for stage in stages_of(client, admin_token)]
    assert names == ["Checkout", "Build JAR"]


def test_an_invalid_stage_is_refused_with_the_reason_the_editor_would_give(
    client, admin_token, service
):
    """The correction loop. The validator's message names the stage and what is
    wrong with it, so the next attempt can be right."""
    result = call_tool(
        client, admin_token, "kubesight_pipeline_stage_add",
        {"service": "issuing", "stage": {"name": "Test", "stageType": "command"}},
    )
    assert result["isError"] is True
    assert "no commands" in result["content"][0]["text"]
    assert len(stages_of(client, admin_token)) == 1


def test_a_stage_may_not_reference_a_secret_the_service_does_not_have(
    client, admin_token, service
):
    """An agent cannot conjure a secret by naming one — creating secrets stays
    with a person, and a reference to a missing one is caught here rather than
    at build time."""
    result = call_tool(
        client, admin_token, "kubesight_pipeline_stage_update",
        {
            "service": "issuing", "stage": "Build JAR",
            "changes": {"secretRefs": [{"name": "AWS_KEY", "envVar": "AWS_KEY"}]},
        },
    )
    assert result["isError"] is True
    assert "AWS_KEY" in result["content"][0]["text"]


def test_a_reference_broken_before_the_edit_is_not_blamed_on_the_edit(
    client, admin_token, service
):
    """Every save is a full replace, so a secret deleted after the pipeline was
    saved fails an edit that never touched it. On the face of the validator's
    message the agent cannot tell it did not cause this, and would try again with
    different stages forever."""
    CiSecret.query.filter_by(key="NEXUS_USER").delete()
    db.session.commit()

    result = call_tool(
        client, admin_token, "kubesight_pipeline_stage_update",
        {"service": "issuing", "stage": "Build JAR", "changes": {"timeoutSeconds": 900}},
    )
    message = result["content"][0]["text"]

    assert result["isError"] is True
    assert "already in the saved pipeline" in message
    assert "Nothing was changed" in message
    # And nothing was: the stage still has the timeout it started with.
    assert stages_of(client, admin_token)[0]["timeoutSeconds"] == 1800


def test_removing_the_only_stage_is_refused(client, admin_token, service):
    """A pipeline with no stages builds nothing, and a service silently unable to
    build is worse than a refusal."""
    result = call_tool(
        client, admin_token, "kubesight_pipeline_stage_remove",
        {"service": "issuing", "stage": "Build JAR"},
    )
    assert result["isError"] is True
    assert "only stage" in result["content"][0]["text"]
    assert len(stages_of(client, admin_token)) == 1


def test_a_stage_can_be_removed_once_another_exists(client, admin_token, service):
    call_tool(
        client, admin_token, "kubesight_pipeline_stage_add",
        {"service": "issuing",
         "stage": {"name": "Test", "stageType": "command", "commands": ["gradle test"]}},
    )
    result = call_tool(
        client, admin_token, "kubesight_pipeline_stage_remove",
        {"service": "issuing", "stage": "Test"},
    )
    assert not result.get("isError"), result["content"][0]["text"]
    assert [stage["name"] for stage in stages_of(client, admin_token)] == ["Build JAR"]


def test_a_full_save_replaces_the_pipeline(client, admin_token, service):
    result = call_tool(
        client, admin_token, "kubesight_pipeline_save",
        {
            "service": "issuing",
            "stages": [
                {"name": "Compile", "stageType": "command", "commands": ["mvn -B package"]},
            ],
        },
    )["structuredContent"]

    assert [stage["name"] for stage in result["stages"]] == ["Compile"]
    assert [stage["name"] for stage in stages_of(client, admin_token)] == ["Compile"]


def test_a_save_bumps_the_version_so_a_build_can_say_which_one_it_ran(
    client, admin_token, service
):
    before = call_tool(
        client, admin_token, "kubesight_pipeline_get", {"service": "issuing"}
    )["structuredContent"]["version"]
    call_tool(
        client, admin_token, "kubesight_pipeline_stage_update",
        {"service": "issuing", "stage": "Build JAR", "changes": {"timeoutSeconds": 900}},
    )
    after = call_tool(
        client, admin_token, "kubesight_pipeline_get", {"service": "issuing"}
    )["structuredContent"]["version"]
    assert after > before


def test_editing_a_generated_default_makes_it_the_service_s_own_and_says_so(
    client, admin_token, app
):
    """Bigger than the edit itself: the service stops tracking KubeSight's
    suggestion, and an agent that did not say so would be reporting half of what
    it did."""
    row = CiService(
        name="Ledger", slug="ledger", application_type="java_gradle",
        repository_provider="bitbucket",
        repository_url="https://bitbucket.org/areebasal/ledger.git",
        repository_workspace="areebasal", repository_name="ledger",
        default_branch="main",
    )
    db.session.add(row)
    db.session.commit()

    generated = call_tool(
        client, admin_token, "kubesight_pipeline_get", {"service": "ledger"}
    )["structuredContent"]
    assert generated["isGeneratedDefault"] is True
    suggested = [stage["name"] for stage in generated["stages"]]

    payload = call_tool(
        client, admin_token, "kubesight_pipeline_stage_add",
        {"service": "ledger",
         "stage": {"name": "Lint", "stageType": "command", "commands": ["gradle check"]}},
    )["structuredContent"]

    assert payload["materialisedGeneratedDefault"] is True
    saved = call_tool(
        client, admin_token, "kubesight_pipeline_get", {"service": "ledger"}
    )["structuredContent"]
    assert saved["isGeneratedDefault"] is False
    # The suggestion was added to, not replaced by, the one new stage.
    assert [stage["name"] for stage in saved["stages"]] == suggested + ["Lint"]


def test_materialising_a_default_keeps_the_build_inputs_it_came_with(
    client, admin_token, app
):
    """The generated default carries the parameters its Run Build dialog asks
    for. A save that sent only stages would drop them, and the next person would
    find a dialog that asks for nothing."""
    row = CiService(
        # iOS, because its generated default is one that defines a build input.
        name="Wallet", slug="wallet", application_type="ios",
        repository_provider="bitbucket",
        repository_url="https://bitbucket.org/areebasal/wallet.git",
        repository_workspace="areebasal", repository_name="wallet",
        default_branch="main",
    )
    db.session.add(row)
    db.session.commit()

    before = call_tool(
        client, admin_token, "kubesight_pipeline_get", {"service": "wallet"}
    )["structuredContent"]["parameters"]
    assert before, "the generated default is expected to define build inputs"

    call_tool(
        client, admin_token, "kubesight_pipeline_stage_add",
        {"service": "wallet",
         "stage": {"name": "Lint", "stageType": "command", "commands": ["swiftlint"],
                   "runnerType": "agent_macos", "runnerLabels": ["macos", "xcode"]}},
    )
    after = call_tool(
        client, admin_token, "kubesight_pipeline_get", {"service": "wallet"}
    )["structuredContent"]["parameters"]

    assert [item["name"] for item in after] == [item["name"] for item in before]


def test_a_write_is_refused_to_a_token_that_may_only_read(
    client, viewer_token, service
):
    """The whole gate. A token minted without ci_pipelines:edit reads everything
    here and changes nothing — there is no second, MCP-only switch."""
    result = call_tool(
        client, viewer_token, "kubesight_pipeline_stage_update",
        {"service": "issuing", "stage": "Build JAR", "changes": {"enabled": False}},
    )
    assert result["isError"] is True
    assert "ci_pipelines:edit" in result["content"][0]["text"]


def test_a_write_is_attributed_to_the_token_holder(client, admin_token, service):
    """The pipeline service records who saved it, and 'who' is the person whose
    token the agent is holding — never KubeSight itself."""
    from api.models import AuditLog

    call_tool(
        client, admin_token, "kubesight_pipeline_stage_update",
        {"service": "issuing", "stage": "Build JAR", "changes": {"enabled": False}},
    )
    saved = AuditLog.query.filter_by(action="ci_pipeline_saved").all()
    assert saved
    assert saved[-1].actor_user_id is not None


def test_the_audit_row_says_an_agent_did_the_writing(client, admin_token, service):
    """ci_pipeline_saved records what changed. What it cannot say is that this
    came through MCP rather than from a person in the editor."""
    from api.models import AuditLog

    call_tool(
        client, admin_token, "kubesight_pipeline_stage_update",
        {"service": "issuing", "stage": "Build JAR", "changes": {"enabled": False}},
    )
    row = AuditLog.query.filter_by(action="mcp_tools_called").all()[-1]
    assert "kubesight_pipeline_stage_update" in str(row.details["writes"])


def test_a_read_only_call_records_no_writes(client, admin_token, service):
    from api.models import AuditLog

    call_tool(client, admin_token, "kubesight_overview")
    row = AuditLog.query.filter_by(action="mcp_tools_called").all()[-1]
    assert row.details["writes"] == []


def test_a_write_answers_with_what_it_changed(client, admin_token, service):
    """A model that reads '2 stages' after an edit cannot tell whether the edit
    landed. The summary line has to say."""
    result = call_tool(
        client, admin_token, "kubesight_pipeline_stage_update",
        {"service": "issuing", "stage": "Build JAR", "changes": {"enabled": False}},
    )
    assert "saved" in result["content"][0]["text"]
    assert "Build JAR" in result["content"][0]["text"]


# ---------------------------------------------------------------------------
# The Dockerfile
# ---------------------------------------------------------------------------
#
# Two things are protected here. One is the boundary: KubeSight reads a
# repository and never writes to it, so an agent editing "the Dockerfile" is
# always editing KubeSight's copy — and on a service that had no copy, creating
# one silently retires the repository's file for every future build. That has to
# be said out loud rather than discovered from a build that ignored a commit.
#
# The other is the edit itself. A document sent back whole is a document whose
# unmentioned lines can vanish, so the narrow tool matches exact text and
# refuses anything ambiguous rather than picking the first occurrence.

_DOCKERFILE = "\n".join(
    [
        "FROM registry.areeba.com/openjdk11:jdk-11.0.11_9-alpine-slim",
        "WORKDIR /app",
        "COPY --chown=65532:65532 app.jar /app/app.jar",
        "USER 65532:65532",
        "EXPOSE 8080",
        'ENTRYPOINT ["java", "-jar", "/app/app.jar"]',
    ]
)


@pytest.fixture()
def dockerfiled(service):
    service.dockerfile = _DOCKERFILE
    db.session.commit()
    return service


def test_the_stored_dockerfile_comes_back_with_who_builds_it(
    client, admin_token, dockerfiled
):
    """One call has to answer both halves of "what does this build": the text,
    and whether any stage actually builds it. The fixture's pipeline has no
    container_image stage, which is exactly the case a model would otherwise
    report as "edited and ready"."""
    result = call_tool(client, admin_token, "kubesight_dockerfile_get", {"service": "issuing"})
    payload = result["structuredContent"]

    assert payload["source"] == "inline"
    assert payload["content"] == _DOCKERFILE
    assert payload["totalLines"] == 6
    assert payload["builtBy"] == []


def test_a_service_with_no_stored_dockerfile_says_where_the_real_one_is(
    client, admin_token, service
):
    """Not "no Dockerfile". The repository has one and builds it; KubeSight just
    does not hold a copy. Reading it needs a connected repository, and the
    fixture has no credential — so the answer says it could not be read rather
    than implying there is nothing there."""
    result = call_tool(client, admin_token, "kubesight_dockerfile_get", {"service": "issuing"})
    payload = result["structuredContent"]

    assert payload["source"] == "repository"
    assert payload["content"] is None
    assert payload["unreadable"]


def test_an_edit_changes_the_line_it_named_and_nothing_else(
    client, admin_token, dockerfiled
):
    """The whole reason the narrow tool exists: a base image bumped without
    sending the file back, so no line can be lost by not being repeated."""
    result = call_tool(
        client, admin_token, "kubesight_dockerfile_edit",
        {
            "service": "issuing",
            "replacements": [{"find": "openjdk11:jdk-11.0.11_9", "replace": "openjdk17:jdk-17.0.9"}],
        },
    )
    payload = result["structuredContent"]

    assert "openjdk17:jdk-17.0.9" in payload["content"]
    assert payload["totalLines"] == 6
    assert 'ENTRYPOINT ["java", "-jar", "/app/app.jar"]' in payload["content"]
    assert (payload["linesAdded"], payload["linesRemoved"]) == (1, 1)
    assert "FROM" in payload["diff"]


def test_a_snippet_that_matches_twice_is_refused_rather_than_guessed(
    client, admin_token, dockerfiled
):
    """Picking the first occurrence is how the wrong line gets rewritten, and
    the agent has no way to notice. A refusal that names the count sends it back
    to the file with enough context to be specific."""
    result = call_tool(
        client, admin_token, "kubesight_dockerfile_edit",
        {"service": "issuing", "replacements": [{"find": "65532", "replace": "1000"}]},
    )
    assert result["isError"] is True
    text = result["content"][0]["text"]
    assert "ambiguous" in text and "nothing was saved" in text
    assert db.session.get(CiService, dockerfiled.id).dockerfile == _DOCKERFILE


def test_a_snippet_that_is_not_there_saves_nothing(client, admin_token, dockerfiled):
    """Half an edit is worse than none, so a replacement is checked against the
    text before any of them are applied."""
    result = call_tool(
        client, admin_token, "kubesight_dockerfile_edit",
        {
            "service": "issuing",
            "replacements": [
                {"find": "EXPOSE 8080", "replace": "EXPOSE 9090"},
                {"find": "HEALTHCHECK", "replace": "# none"},
            ],
        },
    )
    assert result["isError"] is True
    assert db.session.get(CiService, dockerfiled.id).dockerfile == _DOCKERFILE


def test_editing_what_the_repository_owns_points_at_the_only_thing_that_can_be_done(
    client, admin_token, service
):
    """KubeSight does not commit. An agent asked to "fix the Dockerfile" on a
    service that stores none must not be left guessing at a tool that would —
    there is none, and the refusal says what the alternative actually means."""
    result = call_tool(
        client, admin_token, "kubesight_dockerfile_edit",
        {"service": "issuing", "replacements": [{"find": "FROM", "replace": "FROM"}]},
    )
    assert result["isError"] is True
    text = result["content"][0]["text"]
    assert "cannot write to a repository" in text
    assert "kubesight_dockerfile_set" in text


def test_storing_the_first_dockerfile_says_it_now_overrides_the_repository(
    client, admin_token, service
):
    """The consequence nobody asks about and everybody is surprised by: from
    here on, a fix pushed to the repository's Dockerfile changes nothing."""
    result = call_tool(
        client, admin_token, "kubesight_dockerfile_set",
        {"service": "issuing", "dockerfile": _DOCKERFILE},
    )
    payload = result["structuredContent"]

    assert payload["startedOverridingRepository"] is True
    assert "overrides the repository" in result["content"][0]["text"]
    assert "ignores the Dockerfile in the repository" in payload["note"]
    assert db.session.get(CiService, service.id).dockerfile == _DOCKERFILE


def test_a_document_with_no_from_is_refused_before_it_costs_a_build(
    client, admin_token, dockerfiled
):
    """BuildKit would reject it anyway — but an hour later, on a runner, as a
    failed build somebody has to go and read."""
    result = call_tool(
        client, admin_token, "kubesight_dockerfile_set",
        {"service": "issuing", "dockerfile": "WORKDIR /app\nEXPOSE 8080\n"},
    )
    assert result["isError"] is True
    assert "FROM" in result["content"][0]["text"]
    assert db.session.get(CiService, dockerfiled.id).dockerfile == _DOCKERFILE


def test_clearing_is_asked_for_in_words_not_by_sending_nothing(
    client, admin_token, dockerfiled
):
    """An empty string is what a broken caller sends, and it would quietly
    change which file every build uses. The decision needs its own field."""
    empty = call_tool(
        client, admin_token, "kubesight_dockerfile_set",
        {"service": "issuing", "dockerfile": "   "},
    )
    assert empty["isError"] is True
    assert "useRepositoryDockerfile" in empty["content"][0]["text"]

    cleared = call_tool(
        client, admin_token, "kubesight_dockerfile_set",
        {"service": "issuing", "useRepositoryDockerfile": True},
    )
    assert cleared["structuredContent"]["source"] == "repository"
    assert db.session.get(CiService, dockerfiled.id).dockerfile is None


def test_a_dockerfile_write_is_refused_to_a_token_that_may_only_read(
    client, viewer_token, dockerfiled
):
    """The same gate as every other write: ci_services:edit, the permission the
    Dockerfile tab's Save button needs."""
    result = call_tool(
        client, viewer_token, "kubesight_dockerfile_set",
        {"service": "issuing", "dockerfile": "FROM alpine:3.20\n"},
    )
    assert result["isError"] is True
    assert "ci_services:edit" in result["content"][0]["text"]
    assert db.session.get(CiService, dockerfiled.id).dockerfile == _DOCKERFILE


def test_a_dockerfile_write_is_audited_as_the_token_holder(
    client, admin_token, dockerfiled
):
    """It goes through catalog.update_service — the same call the UI makes — so
    it writes the row the UI writes, attributed to whoever the token belongs
    to."""
    from api.models import AuditLog

    call_tool(
        client, admin_token, "kubesight_dockerfile_edit",
        {"service": "issuing", "replacements": [{"find": "EXPOSE 8080", "replace": "EXPOSE 9090"}]},
    )
    saved = AuditLog.query.filter_by(action="ci_service_updated").all()
    assert saved and saved[-1].actor_user_id is not None


# ---------------------------------------------------------------------------
# The whole read surface, once
# ---------------------------------------------------------------------------

# Plausible arguments for every required field any read tool declares. A tool
# whose required fields are not all in here is skipped and NAMED by the test
# below — which is how a new tool with an unfamiliar argument gets noticed
# instead of quietly going untested.
_SMOKE_ARGUMENTS = {
    "cluster": "prod-us-east",
    "namespace": "default",
    "service": "issuing",
    "buildId": 1,
    "kind": "pods",
    "name": "anything",
    "pod": "anything",
    "workload": "anything",
    "inventoryId": "prod-us-east/default/anything",
    "applicationId": 1,
    "analysisId": 1,
    "serviceId": 1,
    "appId": 1,
    "bundleId": 1,
    "policyId": 1,
    "stageId": 1,
    "release": "anything",
    "provider": "zoho",
    "image": "registry.example.com/app:1.0.0",
    "path": "README.md",
    "yaml": "apiVersion: v1\nkind: ConfigMap\nmetadata:\n  name: x\n",
}


def _read_tools():
    from api.mcp.tools.registry import _REGISTRY

    return {name: entry for name, entry in _REGISTRY.items() if not entry["write"]}


def test_every_read_tool_actually_runs(client, admin_token, service):
    """Registration proves a tool exists; only a call proves it works.

    Eighty tools is well past the number anybody checks by hand, and the failure
    this catches is the cheap, silent one: a mistyped relative import or a
    service signature that moved. Either raises on the first call and never
    before it.

    A ToolError counts as working. "No pod 'anything'" is the tool doing its
    job with fixture arguments; an ImportError or a TypeError is not.
    """
    broken = []
    ran = 0
    for name, entry in sorted(_read_tools().items()):
        required = set((entry["schema"] or {}).get("required") or [])
        if not required <= set(_SMOKE_ARGUMENTS):
            continue
        arguments = {key: _SMOKE_ARGUMENTS[key] for key in required}
        result = call_tool(client, admin_token, name, arguments)
        ran += 1
        if result.get("isError"):
            text = result["content"][0]["text"]
            # The generic message the protocol substitutes for an unexpected
            # exception. A refusal written by the tool itself reads differently
            # and is fine.
            if "failed inside KubeSight" in text:
                broken.append((name, text))
    assert not broken, f"tools that raised rather than refused: {broken}"
    assert ran == len(_read_tools()), f"{ran} of {len(_read_tools())} read tools exercised"


def test_no_read_tool_is_left_untested_without_saying_so(client, admin_token):
    """The skip list above is only honest if it is short and deliberate.

    A tool skipped here is a tool nobody is checking, so the set is pinned:
    adding one means adding its argument to _SMOKE_ARGUMENTS or admitting here
    that it is not covered.
    """
    untested = {
        name
        for name, entry in _read_tools().items()
        if not set((entry["schema"] or {}).get("required") or []) <= set(_SMOKE_ARGUMENTS)
    }
    assert untested == set(), f"read tools with unmapped required arguments: {sorted(untested)}"


# ---------------------------------------------------------------------------
# The skill and the server, kept in step
# ---------------------------------------------------------------------------

_SKILL_ROOT = pathlib.Path(__file__).resolve().parents[2] / ".claude" / "skills" / "kubesight"


def _skill_files():
    return [_SKILL_ROOT / "SKILL.md", *sorted((_SKILL_ROOT / "references").glob("*.md"))]


@pytest.mark.skipif(not _SKILL_ROOT.exists(), reason="skill not installed in this checkout")
def test_the_skill_names_no_tool_that_does_not_exist():
    """A skill that teaches a tool name the server does not have sends an agent
    into a refusal, and it will not learn from it — the name reads authoritative
    because it came from its own instructions."""
    from api.mcp.tools.registry import _REGISTRY

    mentioned = set()
    for path in _skill_files():
        mentioned |= set(re.findall(r"kubesight_[a-z_]+\b", path.read_text(encoding="utf-8")))
    # Prose writes `kubesight_workload_*` for a family; the trailing underscore
    # survives the word boundary and is not a tool name.
    mentioned = {name for name in mentioned if not name.endswith("_")}
    assert mentioned <= set(_REGISTRY), f"skill names tools that do not exist: {sorted(mentioned - set(_REGISTRY))}"


@pytest.mark.skipif(not _SKILL_ROOT.exists(), reason="skill not installed in this checkout")
def test_every_tool_is_taught_somewhere_in_the_skill():
    """The other direction. A tool nothing documents is a tool an agent reaches
    for by guessing, which is how it gets used wrong rather than not at all."""
    from api.mcp.tools.registry import _REGISTRY

    text = "\n".join(path.read_text(encoding="utf-8") for path in _skill_files())
    missing = sorted(name for name in _REGISTRY if name not in text)
    assert not missing, f"tools no reference file mentions: {missing}"


@pytest.mark.skipif(not _SKILL_ROOT.exists(), reason="skill not installed in this checkout")
def test_there_is_one_reference_file_per_domain():
    """The split is the point: an agent reads one page, not seven. That only
    holds if every domain has a page and no page is orphaned."""
    from api.mcp.tools import DOMAINS

    present = {path.stem for path in (_SKILL_ROOT / "references").glob("*.md")}
    # `writing` is cross-cutting rather than a domain — it governs every write.
    assert present == set(DOMAINS) | {"writing"}


@pytest.mark.skipif(not _SKILL_ROOT.exists(), reason="skill not installed in this checkout")
def test_the_router_stays_small_enough_to_always_load():
    """SKILL.md is in context for every question whether or not KubeSight comes
    up. Its job is to route in a page; a reference file that migrated into it
    would undo the split."""
    body = (_SKILL_ROOT / "SKILL.md").read_text(encoding="utf-8")
    assert len(body.splitlines()) < 120
    for domain in ("ci", "clusters", "workloads", "deploys", "observability", "apps", "platform"):
        assert f"references/{domain}.md" in body


def test_a_service_refusal_keeps_its_own_words(client, admin_token):
    """The services return ``(data, error, status)`` and their error strings are
    written for a person reading the UI — which makes them exactly right to
    repeat. A wrapper that replaced them with its own wording would throw away
    the only part of the answer that is actionable."""
    result = call_tool(
        client, admin_token, "kubesight_pod_logs",
        {"cluster": "prod-us-east", "namespace": "default", "pod": "no-such-pod"},
    )
    assert result["isError"] is True
    text = result["content"][0]["text"]
    assert "no-such-pod" in text
    # Not the generic "KubeSight refused the request" fallback, which is what a
    # wrongly-unwrapped (Response, status) tuple silently produces.
    assert "refused the request" not in text


def test_a_bare_forbidden_is_expanded_into_something_actionable(client, viewer_token):
    """Services answer access denials with the single word "Forbidden". An agent
    cannot tell a missing permission from an out-of-scope namespace from that,
    so it says "access denied" and nobody is any further forward."""
    from api.mcp.tools.common import unwrap
    from api.mcp.protocol import ToolError

    with pytest.raises(ToolError) as caught:
        unwrap((None, "Forbidden", 403), what="thing")
    message = str(caught.value)
    assert "permission" in message and "namespace" in message
    assert "kubesight_roles_list" in message


# ---------------------------------------------------------------------------
# The new domains, answering rather than merely not crashing
# ---------------------------------------------------------------------------

def test_a_cluster_can_be_named_the_way_a_person_says_it(client, admin_token):
    """Cluster ids are not what anybody says out loud. Making an agent call a
    listing tool to translate a name costs a round trip on nearly every
    cluster-shaped question."""
    listed = call_tool(client, admin_token, "kubesight_clusters_list")
    first = listed["structuredContent"]["clusters"][0]

    by_id = call_tool(client, admin_token, "kubesight_cluster_nodes", {"cluster": first["id"]})
    by_name = call_tool(client, admin_token, "kubesight_cluster_nodes", {"cluster": first["name"]})
    assert not by_id.get("isError") and not by_name.get("isError")
    assert by_id["structuredContent"]["clusterId"] == by_name["structuredContent"]["clusterId"]


def test_an_unknown_cluster_lists_the_real_ones(client, admin_token):
    """The agent asked with the word a person used. The useful answer to a wrong
    word is the right ones, not 'not found'."""
    result = call_tool(client, admin_token, "kubesight_cluster_nodes", {"cluster": "nope"})
    assert result["isError"] is True
    assert "prod-us-east" in result["content"][0]["text"]


def test_namespace_resources_can_be_narrowed_to_one_kind(client, admin_token):
    """Without a kind the payload is every kind at once, which is most of a
    context window and almost none of it the answer."""
    everything = call_tool(
        client, admin_token, "kubesight_namespace_resources",
        {"cluster": "prod-us-east", "namespace": "payments"},
    )["structuredContent"]
    just_pods = call_tool(
        client, admin_token, "kubesight_namespace_resources",
        {"cluster": "prod-us-east", "namespace": "payments", "kind": "pods"},
    )["structuredContent"]

    assert set(just_pods["counts"]) == {"pods"}
    assert set(everything["counts"]) > {"pods"}
    assert just_pods["counts"]["pods"] == everything["counts"]["pods"]


def test_an_unknown_resource_kind_names_the_real_ones(client, admin_token):
    result = call_tool(
        client, admin_token, "kubesight_namespace_resources",
        {"cluster": "prod-us-east", "namespace": "payments", "kind": "widgets"},
    )
    assert result["isError"] is True
    assert "deployments" in result["content"][0]["text"]


def test_logs_can_be_filtered_before_they_are_tailed(client, admin_token):
    """Filtering after the tail throws away the matches that were further back,
    which is exactly the case somebody is searching for."""
    plain = call_tool(
        client, admin_token, "kubesight_pod_logs",
        {"cluster": "prod-us-east", "namespace": "payments", "pod": "payments-api-84b5d5"},
    )["structuredContent"]
    assert plain["lines"]
    assert plain["matchedLines"] is None

    filtered = call_tool(
        client, admin_token, "kubesight_pod_logs",
        {"cluster": "prod-us-east", "namespace": "payments", "pod": "payments-api-84b5d5",
         "contains": "WARN"},
    )["structuredContent"]
    assert filtered["matchedLines"] == len(filtered["lines"])
    assert filtered["lines"] and all("WARN" in line for line in filtered["lines"])
    assert len(filtered["lines"]) < len(plain["lines"])


def test_an_unsupported_log_window_is_refused_with_the_allowed_ones(client, admin_token):
    result = call_tool(
        client, admin_token, "kubesight_pod_logs",
        {"cluster": "prod-us-east", "namespace": "payments", "pod": "payments-api-84b5d5",
         "sinceSeconds": 12345},
    )
    assert result["isError"] is True
    assert "3600" in result["content"][0]["text"]


def test_eligibility_answers_before_a_deploy_is_attempted(client, admin_token):
    """A refusal an agent could have predicted gets reported as a failure. This
    is the call that turns it back into a rule."""
    result = call_tool(
        client, admin_token, "kubesight_deploy_eligibility", {"cluster": "prod-us-east"}
    )["structuredContent"]
    assert set(result) >= {"approvalRequired", "hasActiveApproval", "eligible", "requiredApprovals"}


def test_every_write_reports_what_it_changed(client, admin_token):
    """Across domains, not just pipelines.

    ``_summarise`` leads with ``changed`` when a payload carries it and falls
    back to a row count otherwise — so a write that omits it answers a model
    with "3 items", from which the model cannot tell whether the write landed.
    Checked by reading the source rather than by calling twenty write tools for
    real, which is the trade this test is making deliberately.
    """
    import inspect

    from api.mcp.tools.registry import _REGISTRY

    missing = []
    for name, entry in sorted(_REGISTRY.items()):
        if not entry["write"]:
            continue
        source = inspect.getsource(entry["run"])
        # The CI editors route their summary through a shared helper — one for
        # the pipeline, one for the Dockerfile.
        helpers = ("_saved_summary", "_save_dockerfile")
        if '"changed"' not in source and not any(helper in source for helper in helpers):
            missing.append(name)
    assert not missing, f"write tools with no 'changed' summary: {missing}"


def test_the_summary_line_leads_with_the_change_not_a_count(client, admin_token, service):
    from api.mcp.tools.registry import _summarise

    summary = _summarise("t", {"changed": "scaled x to 3 replicas", "items": [1, 2, 3]})
    assert "scaled x to 3 replicas" in summary
    assert "3 items" not in summary


# ---------------------------------------------------------------------------
# Merge checks and build failures, through the agent's own surface
# ---------------------------------------------------------------------------

def test_an_agent_can_read_a_merge_gate_and_why_a_pull_request_was_blocked(
    app, client, admin_token, service
):
    from api.db import db
    from api.models_merge_checks import CiMergeCheck, CiMergeCheckConfig

    # The `app` fixture already holds an application context, so these rows go
    # in directly rather than through one of their own.
    with app.app_context():
        config = CiMergeCheckConfig(
            service_id=service.id,
            enabled=True,
            tools=["eslint", "semgrep"],
            events=["pullrequest:created"],
            target_branches=["master"],
            gate_mode="override",
            max_total_problems=5,
        )
        db.session.add(config)
        db.session.flush()
        db.session.add(
            CiMergeCheck(
                service_id=service.id,
                config_id=config.id,
                pull_request_id="142",
                title="Refund endpoint",
                author="Rita",
                destination_branch="master",
                commit_sha="9f2c71ad55be31e0",
                state="failed",
                verdict="blocked",
                total_problems=9,
                gate={"maxTotalProblems": 5},
                reasons=["9 problems in total; the quality gate allows at most 5."],
                metrics={
                    "eslint": {"status": "ok", "problems": 9},
                    "semgrep": {"status": "missing", "problems": 0},
                },
                delivery_state="delivered",
            )
        )
        db.session.commit()

    status = call_tool(
        client,
        admin_token,
        "kubesight_merge_checks_status",
        # Skipped: it would reach out to Bitbucket, which a test does not own.
        {"service": "issuing", "checkEnforcement": False},
    )
    assert status["structuredContent"]["enabled"] is True
    assert status["structuredContent"]["gate"]["maxTotalProblems"] == 5
    assert status["structuredContent"]["checks"] == ["eslint", "semgrep"]

    history = call_tool(
        client,
        admin_token,
        "kubesight_merge_checks_history",
        {"service": "issuing", "pullRequest": "142"},
    )
    entry = history["structuredContent"]["checks"][0]
    assert entry["verdict"] == "blocked"
    assert entry["problems"] == 9 and entry["limit"] == 5
    # The distinction the agent must not flatten: 9 real problems from one check,
    # and another that never ran. Reporting the second as "0 problems" would
    # read as clean.
    assert entry["byCheck"]["eslint"] == {"status": "ok", "problems": 9}
    assert entry["byCheck"]["semgrep"]["status"] == "missing"


def test_an_agent_reads_the_quality_gate_before_it_moves_it(client, admin_token):
    before = call_tool(client, admin_token, "kubesight_merge_check_policy_get")
    assert before["structuredContent"]["configured"]["maxTotalProblems"] is None

    changed = call_tool(
        client, admin_token, "kubesight_merge_check_policy_set", {"maxTotalProblems": 5}
    )
    assert changed["structuredContent"]["changed"]["maxTotalProblems"] == {
        "from": None,
        "to": 5,
    }

    after = call_tool(client, admin_token, "kubesight_merge_check_policy_get")
    assert after["structuredContent"]["effective"]["maxTotalProblems"] == 5


def test_build_failure_explains_in_one_call_and_says_so_when_nothing_failed(
    app, client, admin_token, service
):
    from api.db import db
    from api.models_ci import CiBuild, CiBuildStage, CiLogChunk

    assert service.slug == "issuing"
    empty = call_tool(
        client, admin_token, "kubesight_build_failure", {"service": "issuing"}
    )
    assert empty["structuredContent"]["failed"] is False

    with app.app_context():
        build = CiBuild(
            service_id=service.id, number=7, status="failed", branch="master",
            commit_sha="deadbeefcafe", pipeline_snapshot={"stages": []},
        )
        db.session.add(build)
        db.session.flush()
        ok = CiBuildStage(build_id=build.id, position=0, name="Checkout", status="success")
        bad = CiBuildStage(
            build_id=build.id, position=1, name="Build", status="failed",
            exit_code=1, error="Stage 'Build' failed.",
        )
        db.session.add_all([ok, bad])
        db.session.flush()
        for seq, text in enumerate(["compiling…", "error: cannot find symbol"], start=1):
            db.session.add(
                CiLogChunk(build_stage_id=bad.id, seq=seq, stream="stdout", content=text)
            )
        db.session.commit()

    found = call_tool(
        client, admin_token, "kubesight_build_failure", {"service": "issuing"}
    )["structuredContent"]
    assert found["number"] == 7
    assert [stage["name"] for stage in found["failedStages"]] == ["Build"]
    # The whole point: the reason arrives without a second call for a stage id.
    assert "cannot find symbol" in "\n".join(found["failedStages"][0]["tail"])
