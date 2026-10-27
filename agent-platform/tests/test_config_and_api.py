"""The department file, the tool filter, the JSON check and the request parsing."""

import json
from pathlib import Path

import pytest
from fastapi import HTTPException
from pydantic import ValidationError
from pydantic_ai.tools import ToolDefinition

from agent_platform.agents import _json_object_validator, tool_filter
from agent_platform.api import parse_chat_request
from agent_platform.config import DepartmentConfig, ToolSelection, load
from agent_platform.contracts import CompletionDeps
from pydantic_ai import ModelRetry

INFRA = Path(__file__).resolve().parents[1] / "departments" / "infra" / "agents.yaml"


def test_infra_department_file_is_valid():
    config = load(INFRA)
    assert config.task_queue == "infra"
    assert config.ticket_agent == "tickets"
    assert set(config.agents) == {"tickets", "analysis", "ci_assist"}
    assert "kubesight_ticket_execute" in config.instructions("tickets")
    # The closing line is a typed result now, not a JSON line to parse.
    assert "reply with ONLY one line of JSON" not in config.instructions("tickets")


def _base(**agents):
    return {
        "department": "infra",
        "taskQueue": "infra",
        "models": {"default": {"model": "m"}},
        "mcp": {"kubesight": {"id": "kubesight"}},
        "agents": agents,
    }


@pytest.mark.parametrize(
    "agents, message",
    [
        ({"t": {"kind": "ticket", "toolset": "kubesight"}}, "instructions"),
        ({"tickets": {"kind": "ticket", "instructions": "x.md"}}, "toolset"),
        ({"tickets": {"kind": "ticket", "instructions": "x.md", "toolset": "nope"}}, "nope"),
        ({"a": {"kind": "completion", "toolset": "kubesight"}}, "no tools"),
        ({"Bad-Name": {"kind": "completion"}}, "agent name"),
        ({"analysis": {"kind": "completion", "surprise": 1}}, "surprise"),
    ],
)
def test_department_file_errors_are_refused(agents, message):
    with pytest.raises(ValidationError) as caught:
        DepartmentConfig.model_validate(_base(**agents))
    assert message in str(caught.value)


def _tool(name, read_only):
    return ToolDefinition(
        name=name,
        parameters_json_schema={"type": "object"},
        metadata={"annotations": {"readOnlyHint": read_only}},
    )


def test_tool_filter_shows_ticket_tools_and_reads_only():
    allowed = tool_filter(ToolSelection(allow=["kubesight_ticket_*"], read_only=True, deny=["kubesight_pod_exec"]))
    assert allowed(None, _tool("kubesight_ticket_execute", False))
    assert allowed(None, _tool("kubesight_pod_logs", True))
    assert not allowed(None, _tool("kubesight_deploy_apply", False))
    assert not allowed(None, _tool("kubesight_workload_restart", False))
    # A tool with no annotations is not assumed to be read-only.
    assert not allowed(None, ToolDefinition(name="kubesight_x", parameters_json_schema={"type": "object"}))
    # Deny wins even over a read-only tool.
    assert not allowed(None, _tool("kubesight_pod_exec", True))


class _Ctx:
    def __init__(self, deps):
        self.deps = deps


def _deps(keys=None):
    return CompletionDeps(department="infra", requester="t", json_object=True, required_keys=keys)


def test_json_check_accepts_one_object_and_a_stray_brace():
    assert json.loads(_json_object_validator(_Ctx(_deps()), '{"a": 1}}')) == {"a": 1}
    assert json.loads(_json_object_validator(_Ctx(_deps(["a", "b"])), '{"a": 1, "b": 2}')) == {"a": 1, "b": 2}


@pytest.mark.parametrize(
    "text, keys, said",
    [
        ("here you go", None, "JSON object"),
        ("[1, 2]", None, "not another JSON value"),
        ('{"a": 1} and more', None, "nothing after"),
        ('{"a": 1}', ["a", "b"], "missing b"),
        ('{"a": 1, "c": 3}', ["a"], "not allowed c"),
    ],
)
def test_json_check_sends_the_problem_back(text, keys, said):
    with pytest.raises(ModelRetry) as caught:
        _json_object_validator(_Ctx(_deps(keys)), text)
    assert said in str(caught.value)


def test_chat_request_carries_the_contract_keys():
    analysis = {
        "task": "analyze_repository_evidence",
        "result_contract": {"exact_top_level_keys": True, "template": {"findings": [], "limitations": []}},
    }
    request = parse_chat_request(
        {
            "model": "analysis",
            "messages": [
                {"role": "system", "content": "Return JSON."},
                {"role": "user", "content": json.dumps(analysis)},
            ],
            "response_format": {"type": "json_object"},
        }
    )
    assert request.agent == "analysis"
    assert request.instructions == "Return JSON."
    assert request.required_keys == ["findings", "limitations"]

    ci = {"resultContract": {"exactTopLevelKeys": True, "template": {"applicationProfile": {}, "pipeline": {}}}}
    request = parse_chat_request(
        {"model": "ci_assist", "messages": [{"role": "user", "content": json.dumps(ci)}],
         "response_format": {"type": "json_object"}}
    )
    assert request.required_keys == ["applicationProfile", "pipeline"]

    plain = parse_chat_request({"model": "analysis", "messages": [{"role": "user", "content": "hi"}]})
    assert plain.json_object is False and plain.required_keys is None


def test_chat_request_needs_a_user_message():
    with pytest.raises(HTTPException):
        parse_chat_request({"model": "analysis", "messages": [{"role": "system", "content": "x"}]})
