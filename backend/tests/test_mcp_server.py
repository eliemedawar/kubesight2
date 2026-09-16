"""KubeSight as an MCP server.

Two things are being protected here, and they are not the same thing.

The **protocol** has to be right or no client connects at all — and the failure
modes are quiet ones: answering a notification, or sending a body where the spec
says none. A client following the rules hangs or disconnects, and the error it
reports points somewhere else entirely.

The **boundary** has to be right or an agent sees more than the person who gave
it a token. Every tool is read-only and every tool checks a permission; both are
asserted here rather than left to the reading of the module.
"""

from __future__ import annotations

import pytest

from api.db import db
from api.models_ci import CiPipeline, CiPipelineStage, CiRunner, CiService
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
    assert "read-only" in result["instructions"]


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
    assert payload["readOnly"] is True
    assert "tools" not in payload


def test_the_endpoint_requires_authentication(client):
    response = client.post(
        "/api/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"}
    )
    assert response.status_code in (401, 403)


# ---------------------------------------------------------------------------
# The boundary
# ---------------------------------------------------------------------------

def test_every_tool_is_declared_read_only(client, admin_token):
    """The whole premise. A write tool appearing here later would inherit the
    trust this surface was granted on the strength of being read-only."""
    tools = rpc(client, admin_token, "tools/list").get_json()["result"]["tools"]
    assert tools
    for entry in tools:
        assert entry["annotations"]["readOnlyHint"] is True
        assert entry["annotations"]["destructiveHint"] is False
        assert entry["name"].startswith("kubesight_")
        assert entry["description"] and entry["inputSchema"]


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
    """Names, never values — a pipeline stores references, and so does this."""
    payload = call_tool(
        client, admin_token, "kubesight_pipeline_get", {"service": "issuing"}
    )["structuredContent"]

    stage = payload["stages"][0]
    assert stage["name"] == "Build JAR"
    assert stage["commands"] == ["gradle clean build"]
    assert stage["secretRefs"] == ["NEXUS_USER"]
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
