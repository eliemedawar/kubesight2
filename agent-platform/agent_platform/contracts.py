"""What crosses the wire: KubeSight ↔ API ↔ workflows ↔ activities.

Everything here lands in Temporal history in plain form, so it carries ids and
the task KubeSight wrote — never a credential. Tokens are read inside the
activities, from the pod's Secret.
"""

from __future__ import annotations

from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, Field

# Mirrors the closing line the Hermes prompt asked for, now a typed output.
TicketOutcomeKind = Literal[
    "executed",
    "approval_requested",
    "vms_requested",
    "apps_requested",
    "answered",
    "impediment",
    "status_set",
    "nothing",
]


class RunDeps(BaseModel):
    """Who a run is for. Sent to KubeSight as the on-behalf-of identity."""

    department: str
    # "zoho#4521" for a ticket; the requesting user for a chat.
    requester: str


class CompletionDeps(BaseModel):
    department: str
    requester: str
    # The request asked for a JSON object (response_format json_object).
    json_object: bool = True
    # When the request's contract fixes the top-level keys, the answer must
    # have exactly these — a mismatch goes back to the model, not to KubeSight.
    required_keys: Optional[List[str]] = None


class TicketRef(BaseModel):
    provider: str
    record_id: int


class TicketTask(BaseModel):
    """One KubeSight task row (``TicketInterpretation``) handed to the agent."""

    task_id: int
    kind: str
    # The task message KubeSight built — ticket, catalog, event — as the
    # agent's user prompt.
    message: Dict[str, Any]


class TicketOutcome(BaseModel):
    """The agent's closing answer. What it DID is recorded by the tools."""

    outcome: TicketOutcomeKind = Field(
        description="Which ticket tool settled the task, or 'nothing' if none did."
    )
    summary: str = Field(description="One sentence for the DevOps team.", max_length=1000)


class TaskReport(BaseModel):
    """Sent back to KubeSight once a ticket task is over."""

    task_id: int
    status: Literal["finished", "failed"]
    outcome: Optional[str] = None
    summary: Optional[str] = None
    model: Optional[str] = None
    error: Optional[str] = None


class CompletionRequest(BaseModel):
    agent: str
    instructions: str
    prompt: str
    json_object: bool = True
    required_keys: Optional[List[str]] = None
    requester: str = "kubesight"


class CompletionResult(BaseModel):
    text: str
    model: str
    input_tokens: int = 0
    output_tokens: int = 0
