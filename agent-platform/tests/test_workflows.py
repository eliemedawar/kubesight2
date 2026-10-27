"""The workflows on a real (local) Temporal server, with a scripted model.

The model is a FunctionModel and the KubeSight MCP server is a FunctionToolset
with the same id, so everything between them — the activities, the tool
filter, the typed output, the report back to KubeSight, the API — is the real
code. Needs the Temporal CLI dev server, which the SDK downloads on first use.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from pathlib import Path
from typing import Any, Dict, List

import httpx
import pytest
from pydantic_ai.durable_exec.temporal import PydanticAIPlugin
from pydantic_ai.messages import ModelMessage, ModelResponse, TextPart, ToolCallPart, ToolReturnPart
from pydantic_ai.models.function import AgentInfo, FunctionModel
from pydantic_ai.toolsets import FunctionToolset
from temporalio.testing import WorkflowEnvironment

from agent_platform import activities, api, temporal_client
from agent_platform.agents import build_department
from agent_platform.codec import data_converter
from agent_platform.config import load
from agent_platform.worker import build_worker

INFRA = Path(__file__).resolve().parents[1] / "departments" / "infra" / "agents.yaml"
HEADERS = {"Authorization": "Bearer test-platform-token"}


def _kubesight_tools(calls: List[Dict[str, Any]]) -> FunctionToolset:
    tools = FunctionToolset(id="kubesight")

    @tools.tool_plain(metadata={"annotations": {"readOnlyHint": False}})
    def kubesight_ticket_execute(ticketRecordId: int, comment: str) -> dict:
        calls.append({"tool": "execute", "ticket": ticketRecordId, "comment": comment})
        return {"changed": True, "status": "executed"}

    @tools.tool_plain(metadata={"annotations": {"readOnlyHint": True}})
    def kubesight_pod_logs(namespace: str) -> str:
        calls.append({"tool": "logs", "namespace": namespace})
        return "OOMKilled"

    @tools.tool_plain(metadata={"annotations": {"readOnlyHint": False}})
    def kubesight_workload_restart(name: str) -> dict:  # must never be visible
        calls.append({"tool": "restart", "name": name})
        return {"changed": True}

    return tools


def _ticket_model(seen_tools: List[List[str]]):
    def respond(messages: List[ModelMessage], info: AgentInfo) -> ModelResponse:
        seen_tools.append(sorted(t.name for t in info.function_tools))
        returned = [
            p for m in messages for p in getattr(m, "parts", []) if isinstance(p, ToolReturnPart)
        ]
        if not returned:
            task = json.loads(messages[0].parts[-1].content)  # the task KubeSight wrote
            if task.get("explode"):
                raise RuntimeError("provider exploded")
            return ModelResponse(
                parts=[
                    ToolCallPart("kubesight_pod_logs", {"namespace": "payments"}),
                    ToolCallPart(
                        "kubesight_ticket_execute",
                        {"ticketRecordId": task["ticketRecordId"], "comment": "Deploying 1.4.2."},
                    ),
                ]
            )
        return ModelResponse(
            parts=[
                ToolCallPart(
                    info.output_tools[0].name,
                    {"outcome": "executed", "summary": "Deployed payments 1.4.2 to UAT."},
                )
            ]
        )

    return FunctionModel(respond, model_name="scripted")


def _completion_model(answers: List[str]):
    def respond(messages: List[ModelMessage], info: AgentInfo) -> ModelResponse:
        return ModelResponse(parts=[TextPart(answers.pop(0))])

    return FunctionModel(respond, model_name="scripted")


async def _with_platform(model, toolsets, scenario):
    reports: List[Dict[str, Any]] = []

    async def fake_post(url: str, body: Dict[str, Any], headers: Dict[str, str]) -> int:
        assert headers["Authorization"] == "Bearer test-platform-token"
        assert url.endswith(f"/api/agent-platform/ticket-tasks/{body['task_id']}/result")
        reports.append(body)
        return 200

    activities.post_json = fake_post
    department = build_department(load(INFRA), model=model, toolsets=toolsets)
    queue = f"infra-{uuid.uuid4().hex[:8]}"
    async with await WorkflowEnvironment.start_local(
        data_converter=data_converter(), plugins=[PydanticAIPlugin()], namespace="default"
    ) as env:
        temporal_client.use(env.client)
        api.platform_settings.get.cache_clear()
        import os

        os.environ["TASK_QUEUE"] = queue
        os.environ["TEMPORAL_NAMESPACE"] = "default"
        async with build_worker(env.client, department, task_queue=queue):
            transport = httpx.ASGITransport(app=api.app)
            async with httpx.AsyncClient(transport=transport, base_url="http://platform") as http:
                await scenario(http, env.client, reports)
    api.platform_settings.get.cache_clear()


async def _wait_for(predicate, timeout=30.0):
    deadline = asyncio.get_running_loop().time() + timeout
    while not predicate():
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError("timed out waiting")
        await asyncio.sleep(0.1)


def test_ticket_task_runs_the_agent_and_reports_back():
    calls: List[Dict[str, Any]] = []
    seen: List[List[str]] = []

    async def scenario(http, client, reports):
        task = {"provider": "zoho", "recordId": 7, "taskId": 41, "kind": "handle",
                "message": {"ticketRecordId": 7, "task": "handle_new_ticket"}}
        unauthorized = await http.post("/v1/tickets/tasks", json=task)
        assert unauthorized.status_code == 401
        started = await http.post("/v1/tickets/tasks", json=task, headers=HEADERS)
        assert started.status_code == 202, started.text
        assert started.json()["workflowId"] == "ticket-zoho-7"
        await _wait_for(lambda: len(reports) == 1)
        assert reports[0] == {
            "task_id": 41, "status": "finished", "outcome": "executed",
            "summary": "Deployed payments 1.4.2 to UAT.", "model": "gpt-5.6-sol", "error": None,
        }

        # KubeSight lost the report and asks again: same report, no second run.
        again = await http.post("/v1/tickets/tasks", json=task, headers=HEADERS)
        assert again.status_code == 202
        await _wait_for(lambda: len(reports) == 2)
        assert reports[1] == reports[0]
        assert [c["tool"] for c in calls] == ["logs", "execute"]

        # A follow-up on the same ticket joins the same workflow.
        followup = dict(task, taskId=42, kind="followup")
        await http.post("/v1/tickets/tasks", json=followup, headers=HEADERS)
        await _wait_for(lambda: len(reports) == 3)
        assert reports[2]["task_id"] == 42
        status = await http.get("/v1/tickets/zoho/7", headers=HEADERS)
        assert status.json()["status"] == "RUNNING"
        assert status.json()["state"]["handled"] == 2

    asyncio.run(_with_platform(_ticket_model(seen), {"kubesight": _kubesight_tools(calls)}, scenario))
    # The restart tool was never even shown to the model.
    assert seen and all(names == ["kubesight_pod_logs", "kubesight_ticket_execute"] for names in seen)


def test_a_failed_agent_run_is_reported_as_failed():
    async def scenario(http, client, reports):
        task = {"provider": "jira", "recordId": 9, "taskId": 5, "kind": "handle",
                "message": {"ticketRecordId": 9, "explode": True}}
        await http.post("/v1/tickets/tasks", json=task, headers=HEADERS)
        await _wait_for(lambda: len(reports) == 1, timeout=90)
        assert reports[0]["status"] == "failed"
        assert "provider exploded" in reports[0]["error"]

    asyncio.run(_with_platform(_ticket_model([]), {"kubesight": _kubesight_tools([])}, scenario))


def test_completions_speak_openai_and_retry_a_broken_answer():
    answers = ['{"findings": []}', '{"findings": [], "limitations": ["none"]}', "not json at all"] + ["nope"] * 10

    async def scenario(http, client, reports):
        body = {
            "model": "analysis",
            "messages": [
                {"role": "system", "content": "Return one JSON object."},
                {"role": "user", "content": json.dumps(
                    {"result_contract": {"exact_top_level_keys": True,
                                         "template": {"findings": [], "limitations": []}}})},
            ],
            "response_format": {"type": "json_object"},
            "stream": False,
        }
        response = await http.post("/v1/chat/completions", json=body, headers=HEADERS)
        assert response.status_code == 200, response.text
        data = response.json()
        assert data["object"] == "chat.completion"
        assert json.loads(data["choices"][0]["message"]["content"]) == {"findings": [], "limitations": ["none"]}

        # Out of retries: a contract failure (422), which KubeSight does not retry.
        response = await http.post("/v1/chat/completions", json=body, headers=HEADERS)
        assert response.status_code == 422, response.text

        unknown = await http.post("/v1/chat/completions", json=dict(body, model="nobody"), headers=HEADERS)
        assert unknown.status_code == 404

    asyncio.run(_with_platform(_completion_model(answers), {"kubesight": _kubesight_tools([])}, scenario))


@pytest.mark.parametrize("size", [3_000_000])
def test_large_evidence_fits_through_temporal(size):
    evidence = {"files": [{"path": f"src/f{i}.py", "content": "x = 1\n" * 200} for i in range(size // 1200)]}

    async def scenario(http, client, reports):
        body = {
            "model": "ci_assist",
            "messages": [{"role": "user", "content": json.dumps(evidence)}],
            "response_format": {"type": "json_object"},
        }
        response = await http.post("/v1/chat/completions", json=body, headers=HEADERS)
        assert response.status_code == 200, response.text

    asyncio.run(_with_platform(_completion_model(['{"ok": true}']), {"kubesight": _kubesight_tools([])}, scenario))
