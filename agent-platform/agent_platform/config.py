"""The department file: which models, which MCP servers, which agents.

One file per department (``departments/<name>/agents.yaml``, mounted from a
ConfigMap). The worker validates it on start and refuses to start on any
error, so a typo is a crash-looping pod in review, never an agent with the
wrong tools in production.

Two kinds of agent:

* ``ticket``     — handles one ticket task at a time inside the ticket
  workflow, acting through an MCP toolset narrowed by ``tools``.
* ``completion`` — no tools; answers one request through the OpenAI-compatible
  endpoint with a JSON object. KubeSight addresses it by putting the agent's
  name in the request's ``model`` field.

Agent names and toolset ids are part of every running workflow's history
(Temporal replays by them): never rename one once deployed.
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Dict, List, Literal, Optional

import yaml
from pydantic import BaseModel, ConfigDict, Field, PrivateAttr, field_validator, model_validator

_NAME = re.compile(r"^[a-z][a-z0-9_]{1,40}$")


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)


class ModelSpec(_Strict):
    # "openai" = any OpenAI-compatible chat-completions API: OpenAI itself, or
    # the AI proxy in front of the providers (baseUrlEnv set).
    provider: Literal["openai"] = "openai"
    model: str = Field(min_length=1)
    # Environment variables, never values: the file is a ConfigMap.
    base_url_env: Optional[str] = Field(default=None, alias="baseUrlEnv")
    api_key_env: str = Field(default="MODEL_API_KEY", alias="apiKeyEnv")
    timeout_seconds: int = Field(default=300, alias="timeoutSeconds", ge=10, le=3600)


class ModelsSpec(_Strict):
    default: ModelSpec
    # Tried in order when the default fails with a provider error.
    fallbacks: List[ModelSpec] = Field(default_factory=list)


class McpSpec(_Strict):
    # The toolset id. Fixed once deployed.
    id: str
    # Empty = KubeSight's own MCP server (KUBESIGHT_URL/api/mcp).
    url: Optional[str] = None
    timeout_seconds: int = Field(default=120, alias="timeoutSeconds", ge=5, le=900)

    @field_validator("id")
    @classmethod
    def _id(cls, value: str) -> str:
        if not _NAME.match(value):
            raise ValueError("an MCP id is lowercase letters, digits and underscores")
        return value


class ToolSelection(_Strict):
    # Glob patterns over the server's tool names.
    allow: List[str] = Field(default_factory=list)
    # Also every tool the server marks readOnlyHint.
    read_only: bool = Field(default=False, alias="readOnly")
    # Wins over both of the above.
    deny: List[str] = Field(default_factory=list)


class AgentSpec(_Strict):
    kind: Literal["ticket", "completion"]
    # Markdown file, relative to the department file. Completion agents take
    # their instructions from each request instead.
    instructions: Optional[str] = None
    toolset: Optional[str] = None
    tools: ToolSelection = Field(default_factory=ToolSelection)
    retries: int = Field(default=2, ge=0, le=10)
    # Model round trips one run may make (Hermes' max_turns).
    max_requests: int = Field(default=40, alias="maxRequests", ge=1, le=200)

    @model_validator(mode="after")
    def _shape(self) -> "AgentSpec":
        if self.kind == "ticket":
            if not self.instructions:
                raise ValueError("a ticket agent needs instructions")
            if not self.toolset:
                raise ValueError("a ticket agent needs a toolset")
        if self.kind == "completion" and self.toolset:
            raise ValueError("a completion agent has no tools")
        return self


class DepartmentConfig(_Strict):
    department: str
    task_queue: str = Field(alias="taskQueue")
    models: ModelsSpec
    mcp: Dict[str, McpSpec] = Field(default_factory=dict)
    agents: Dict[str, AgentSpec]

    # Set by load(): where instructions files are read from.
    _base_dir: Path = PrivateAttr(default=Path("."))

    @field_validator("agents")
    @classmethod
    def _names(cls, agents: Dict[str, AgentSpec]) -> Dict[str, AgentSpec]:
        for name in agents:
            if not _NAME.match(name):
                raise ValueError(f"agent name {name!r}: lowercase letters, digits and underscores")
        return agents

    @model_validator(mode="after")
    def _references(self) -> "DepartmentConfig":
        tickets = [n for n, a in self.agents.items() if a.kind == "ticket"]
        if len(tickets) > 1:
            raise ValueError("a department has at most one ticket agent")
        for name, agent in self.agents.items():
            if agent.toolset and agent.toolset not in self.mcp:
                raise ValueError(f"agent {name!r} uses toolset {agent.toolset!r}, which mcp does not define")
        return self

    @property
    def ticket_agent(self) -> Optional[str]:
        return next((n for n, a in self.agents.items() if a.kind == "ticket"), None)

    def instructions(self, name: str) -> str:
        spec = self.agents[name]
        if not spec.instructions:
            return ""
        return (self._base_dir / spec.instructions).read_text(encoding="utf-8").strip()


def load(path: str | Path) -> DepartmentConfig:
    path = Path(path)
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    config = DepartmentConfig.model_validate(raw)
    # The chart names the model (agentPlatform.model.name) without editing the file.
    override = os.getenv("MODEL_NAME", "").strip()
    if override:
        config.models.default.model = override
    config._base_dir = path.parent
    for name in config.agents:
        # Read now, so a missing prompt fails the start, not the first ticket.
        config.instructions(name)
    return config
