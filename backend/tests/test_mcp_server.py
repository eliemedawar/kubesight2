"""KubeSight as an MCP server.

Two things are being protected here, and they are not the same thing.

The **protocol** has to be right or no client connects at all — and the failure
modes are quiet ones: answering a notification, or sending a body where the spec
says none. A client following the rules hangs or disconnects, and the error it
reports points somewhere else entirely.

The **boundary** has to be right or an agent sees more than the person who gave
it a token, or changes more than it was meant to. Every tool checks a
permission, only the pipeline editing tools write, and each tool's annotations
say which it is — all three are asserted here rather than left to the reading of
the module.
"""

from __future__ import annotations

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
    # The instructions have to say what the write tools are, and what is still
    # out of reach — a model that assumes it can start a build wastes a call and
    # tells somebody it did something it did not.
    assert "kubesight_pipeline_" in result["instructions"]
    assert "start a build" in result["instructions"]


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
    assert "ci_pipelines:edit" in payload["writes"]
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
    # deliberately rather than slipping in under a threshold.
    assert writing == {
        "kubesight_pipeline_save",
        "kubesight_pipeline_stage_add",
        "kubesight_pipeline_stage_remove",
        "kubesight_pipeline_stage_update",
    }
    assert all(name.startswith("kubesight_pipeline") for name in writing)


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
