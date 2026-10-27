"""The platform's front door. KubeSight is its only caller.

``POST /v1/tickets/tasks``
    Hand one ticket task to the ticket's workflow — started if the ticket has
    none running, signalled if it has. Returns at once; the outcome comes back
    to KubeSight as a callback from the worker.

``POST /v1/chat/completions``
    OpenAI-compatible, for the callers that already speak it (source analysis,
    CI assist). ``model`` names the department's completion agent; the request
    runs as a workflow and the answer is returned in the OpenAI shape, so those
    callers switch by changing a URL.

Every call presents ``AGENT_PLATFORM_TOKEN`` as a bearer token.
"""

from __future__ import annotations

import asyncio
import hmac
import json
import time
import uuid
from datetime import timedelta
from typing import Any, Dict, List, Optional, Tuple

from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field
from temporalio.client import WorkflowFailureError
from temporalio.common import WorkflowIDConflictPolicy
from temporalio.exceptions import ApplicationError
from temporalio.service import RPCError

from . import settings as platform_settings
from . import temporal_client
from .contracts import CompletionRequest, CompletionResult, TicketRef, TicketTask
from .workflows import COMPLETION_WORKFLOW, TICKET_WORKFLOW, TicketWorkflowInput

app = FastAPI(title="KubeSight agent platform", docs_url=None, redoc_url=None, openapi_url=None)

# Failures that say the model's answer was wrong, not that the provider was
# down: KubeSight must not retry these as transient.
_CONTRACT_FAILURES = {"UnexpectedModelBehavior", "UsageLimitExceeded", "ToolRetryError"}


def _openai_error(status: int, message: str, kind: str) -> JSONResponse:
    return JSONResponse(status_code=status, content={"error": {"message": message, "type": kind}})


def authorize(authorization: str = Header(default="")) -> None:
    expected = platform_settings.get().platform_token
    if not expected:
        raise HTTPException(503, "AGENT_PLATFORM_TOKEN is not set on the agent platform.")
    scheme, _, presented = authorization.partition(" ")
    if scheme.lower() != "bearer" or not hmac.compare_digest(presented.strip(), expected):
        raise HTTPException(401, "Unauthorized")


@app.get("/healthz")
async def healthz() -> Dict[str, Any]:
    return {"ok": True}


@app.get("/readyz")
async def readyz() -> Dict[str, Any]:
    try:
        client = await temporal_client.get()
        await client.service_client.check_health()
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(503, f"Temporal is not reachable: {exc}") from exc
    return {"ok": True}


# ---------------------------------------------------------------------------
# Tickets
# ---------------------------------------------------------------------------

class TicketTaskIn(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    provider: str = Field(min_length=1, max_length=16, pattern=r"^[a-z0-9_-]+$")
    record_id: int = Field(alias="recordId", ge=1)
    task_id: int = Field(alias="taskId", ge=1)
    kind: str = Field(max_length=16)
    message: Dict[str, Any]


def ticket_workflow_id(provider: str, record_id: int) -> str:
    return f"ticket-{provider}-{record_id}"


@app.post("/v1/tickets/tasks", status_code=202, dependencies=[Depends(authorize)])
async def ticket_task(body: TicketTaskIn) -> Dict[str, Any]:
    settings = platform_settings.get()
    client = await temporal_client.get()
    workflow_id = ticket_workflow_id(body.provider, body.record_id)
    task = TicketTask(task_id=body.task_id, kind=body.kind, message=body.message)
    # Start-or-signal in one call: a ticket that already has a workflow gets
    # the task as a signal, so a re-sent task never starts a second agent.
    handle = await client.start_workflow(
        TICKET_WORKFLOW,
        TicketWorkflowInput(
            ref=TicketRef(provider=body.provider, record_id=body.record_id),
            idle_days=settings.ticket_idle_days,
        ),
        id=workflow_id,
        task_queue=settings.task_queue,
        start_signal="task",
        start_signal_args=[task],
        id_conflict_policy=WorkflowIDConflictPolicy.USE_EXISTING,
    )
    return {"workflowId": workflow_id, "runId": handle.result_run_id}


@app.get("/v1/tickets/{provider}/{record_id}", dependencies=[Depends(authorize)])
async def ticket_status(provider: str, record_id: int) -> Dict[str, Any]:
    client = await temporal_client.get()
    handle = client.get_workflow_handle(ticket_workflow_id(provider, record_id))
    try:
        description = await handle.describe()
        running = description.status is not None and description.status.name == "RUNNING"
        state = await handle.query("status") if running else None
    except RPCError as exc:
        raise HTTPException(404, "No workflow for this ticket.") from exc
    return {
        "workflowId": handle.id,
        "status": description.status.name if description.status else None,
        "state": state,
    }


# ---------------------------------------------------------------------------
# OpenAI-compatible completions
# ---------------------------------------------------------------------------

def _text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(
            part.get("text", "") for part in content if isinstance(part, dict) and part.get("type") == "text"
        )
    return ""


def _required_keys(prompt: str) -> Optional[List[str]]:
    """The top-level keys the request's own contract fixes, if it fixes them.

    KubeSight's callers put the template in the user message: source analysis
    as ``result_contract`` (``exact_top_level_keys``), CI assist as
    ``resultContract`` (``exactTopLevelKeys``), the connection test as a bare
    ``template`` to copy.
    """
    try:
        message = json.loads(prompt)
    except ValueError:
        return None
    if not isinstance(message, dict):
        return None
    for key, exact in (("result_contract", "exact_top_level_keys"), ("resultContract", "exactTopLevelKeys")):
        contract = message.get(key)
        if isinstance(contract, dict) and contract.get(exact) and isinstance(contract.get("template"), dict):
            return list(contract["template"])
    if message.get("task") == "connection_test" and isinstance(message.get("template"), dict):
        return list(message["template"])
    return None


def parse_chat_request(body: Dict[str, Any]) -> CompletionRequest:
    messages = body.get("messages")
    if not isinstance(messages, list) or not messages:
        raise HTTPException(400, "messages is required.")
    system: List[str] = []
    user: List[str] = []
    for message in messages:
        if not isinstance(message, dict):
            continue
        role, text = message.get("role"), _text(message.get("content"))
        if role in ("system", "developer"):
            system.append(text)
        elif role == "user":
            user.append(text)
    if not user:
        raise HTTPException(400, "A user message is required.")
    prompt = "\n\n".join(user)
    response_format = body.get("response_format") or {}
    json_object = isinstance(response_format, dict) and response_format.get("type") in ("json_object", "json_schema")
    return CompletionRequest(
        agent=str(body.get("model") or ""),
        instructions="\n\n".join(system),
        prompt=prompt,
        json_object=json_object,
        required_keys=_required_keys(prompt) if json_object else None,
        requester=str(body.get("user") or "kubesight")[:120],
    )


def _chat_response(request: CompletionRequest, result: CompletionResult) -> Dict[str, Any]:
    return {
        "id": f"chatcmpl-{uuid.uuid4().hex}",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": request.agent,
        "system_fingerprint": result.model,
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": result.text},
                "finish_reason": "stop",
            }
        ],
        "usage": {
            "prompt_tokens": result.input_tokens,
            "completion_tokens": result.output_tokens,
            "total_tokens": result.input_tokens + result.output_tokens,
        },
    }


def _failure(exc: WorkflowFailureError) -> Tuple[int, str]:
    cause = exc.cause
    while getattr(cause, "cause", None) is not None and not isinstance(cause, ApplicationError):
        cause = cause.cause
    kind = getattr(cause, "type", None) or type(cause).__name__
    message = str(cause) or kind
    if kind == "UnknownAgent":
        return 404, message
    if kind in _CONTRACT_FAILURES:
        return 422, message
    return 502, message


@app.post("/v1/chat/completions", dependencies=[Depends(authorize)])
async def chat_completions(body: Dict[str, Any]):
    if body.get("stream"):
        return _openai_error(400, "Streaming is not supported.", "invalid_request_error")
    request = parse_chat_request(body)
    if not request.agent:
        return _openai_error(400, "model names the completion agent and is required.", "invalid_request_error")
    settings = platform_settings.get()
    client = await temporal_client.get()
    handle = await client.start_workflow(
        COMPLETION_WORKFLOW,
        request,
        id=f"completion-{request.agent}-{uuid.uuid4().hex}",
        task_queue=settings.task_queue,
        execution_timeout=timedelta(hours=1),
        result_type=CompletionResult,
    )
    try:
        result = await asyncio.wait_for(handle.result(), timeout=settings.completion_wait_seconds)
    except asyncio.TimeoutError:
        await handle.terminate("The caller stopped waiting.")
        return _openai_error(504, "The agent did not answer in time.", "timeout")
    except WorkflowFailureError as exc:
        status, message = _failure(exc)
        return _openai_error(status, message, "agent_error")
    return _chat_response(request, result)
