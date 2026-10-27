"""Building a department's agents from its file.

Every agent carries a :class:`TemporalDurability` capability, so inside a
workflow each model request and each MCP call is a Temporal activity: retried
on its own, recorded in history, never repeated on replay. Outside a workflow
(tests, a REPL) the same agent runs directly.

Retries belong to Temporal: the provider SDK is built with ``max_retries=0``,
otherwise a 429 would be retried inside an activity that Temporal then retries
again.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from datetime import timedelta
from fnmatch import fnmatchcase
from typing import Any, Callable, Dict, List, Optional

from openai import AsyncOpenAI
from pydantic_ai import Agent, ModelRetry, RunContext
from pydantic_ai.durable_exec.temporal import TemporalDurability
from pydantic_ai.mcp import MCPToolset
from pydantic_ai.models import Model
from pydantic_ai.models.fallback import FallbackModel
from pydantic_ai.models.openai import OpenAIChatModel
from pydantic_ai.providers.openai import OpenAIProvider
from pydantic_ai.tools import ToolDefinition
from pydantic_ai.toolsets import AbstractToolset
from temporalio.common import RetryPolicy
from temporalio.workflow import ActivityConfig

from . import settings as platform_settings
from .config import DepartmentConfig, ModelSpec, ToolSelection
from .contracts import CompletionDeps, RunDeps, TicketOutcome

ON_BEHALF_OF = "X-KubeSight-On-Behalf-Of"

# Model calls: a stalled or rate-limited provider usually clears.
_MODEL_RETRY = RetryPolicy(
    initial_interval=timedelta(seconds=1),
    backoff_coefficient=2.0,
    maximum_interval=timedelta(minutes=1),
    maximum_attempts=5,
)
# MCP calls: a write tool already refuses a task it settled, so a retry after a
# dropped connection cannot act twice — but there is no reason to insist.
_TOOL_RETRY = RetryPolicy(
    initial_interval=timedelta(seconds=2),
    backoff_coefficient=2.0,
    maximum_interval=timedelta(seconds=30),
    maximum_attempts=3,
)


class ConfigError(RuntimeError):
    pass


def build_model(spec: ModelSpec) -> Model:
    api_key = platform_settings.secret(spec.api_key_env)
    if not api_key:
        raise ConfigError(f"{spec.api_key_env} is not set (the key for model {spec.model!r}).")
    base_url = None
    if spec.base_url_env:
        base_url = os.getenv(spec.base_url_env, "").strip()
        if not base_url:
            raise ConfigError(f"{spec.base_url_env} is not set (the base URL for model {spec.model!r}).")
    client = AsyncOpenAI(
        api_key=api_key,
        base_url=base_url,
        max_retries=0,
        timeout=float(spec.timeout_seconds),
    )
    return OpenAIChatModel(spec.model, provider=OpenAIProvider(openai_client=client))


def build_models(config: DepartmentConfig) -> Model:
    primary = build_model(config.models.default)
    if not config.models.fallbacks:
        return primary
    return FallbackModel(primary, *(build_model(spec) for spec in config.models.fallbacks))


def tool_filter(selection: ToolSelection) -> Callable[[RunContext[Any], ToolDefinition], bool]:
    """The agent sees a tool only when the selection lets it.

    Deny wins; then an explicit allow; then, if ``readOnly``, whatever the
    server itself marks read-only. Anything else does not exist for the agent —
    it cannot be prompt-injected into calling a tool it was never shown.
    """

    def allowed(ctx: RunContext[Any], tool: ToolDefinition) -> bool:
        name = tool.name
        if any(fnmatchcase(name, pattern) for pattern in selection.deny):
            return False
        if any(fnmatchcase(name, pattern) for pattern in selection.allow):
            return True
        if selection.read_only:
            annotations = (tool.metadata or {}).get("annotations") or {}
            return annotations.get("readOnlyHint") is True
        return False

    return allowed


def _kubesight_token(ctx: RunContext[Any]) -> str:
    # Read in the activity, from the pod's Secret: never in deps or history.
    return platform_settings.get().kubesight_agent_token


def _on_behalf_of(ctx: RunContext[Any]) -> Dict[str, str]:
    deps = ctx.deps
    requester = getattr(deps, "requester", "") or ""
    department = getattr(deps, "department", "") or ""
    return {ON_BEHALF_OF: f"{department}:{requester}"[:200]} if requester else {}


def build_toolset(config: DepartmentConfig, name: str) -> MCPToolset:
    spec = config.mcp[name]
    url = spec.url or platform_settings.get().mcp_url
    return MCPToolset(
        url,
        id=spec.id,
        auth=_kubesight_token,
        headers=_on_behalf_of,
        read_timeout=float(spec.timeout_seconds),
        # KubeSight's server lists the same tools for the life of a token.
        cache_tools=True,
    )


def _json_object_validator(ctx: RunContext[CompletionDeps], output: str) -> str:
    """A completion must be one JSON object; with a contract, exactly its keys.

    Only shape is checked here. KubeSight still validates every field itself —
    this just stops an obviously broken answer from making the round trip.
    """
    if not ctx.deps.json_object:
        return output
    text = output.strip()
    if text.startswith("```"):
        text = text.strip("`")
        text = text[text.find("{"):] if "{" in text else text
    try:
        value, end = json.JSONDecoder().raw_decode(text)
    except json.JSONDecodeError as exc:
        raise ModelRetry(f"Return exactly one JSON object and nothing else ({exc.msg}).") from exc
    trailing = text[end:].strip()
    if trailing and set(trailing) != {"}"}:
        raise ModelRetry("Return exactly one JSON object, with nothing after its closing brace.")
    if not isinstance(value, dict):
        raise ModelRetry("Return a JSON object, not another JSON value.")
    required = ctx.deps.required_keys
    if required:
        missing = [key for key in required if key not in value]
        extra = [key for key in value if key not in required]
        if missing or extra:
            parts = []
            if missing:
                parts.append("missing " + ", ".join(missing))
            if extra:
                parts.append("not allowed " + ", ".join(extra))
            raise ModelRetry(
                "The top-level keys must be exactly: " + ", ".join(required) + " (" + "; ".join(parts) + ")."
            )
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


@dataclass
class Department:
    config: DepartmentConfig
    model_name: str
    ticket: Optional[Agent[RunDeps, TicketOutcome]] = None
    completions: Dict[str, Agent[CompletionDeps, str]] = field(default_factory=dict)

    @property
    def name(self) -> str:
        return self.config.department

    def agents(self) -> List[Agent[Any, Any]]:
        return [a for a in [self.ticket, *self.completions.values()] if a is not None]

    def usage_limit(self, agent_name: str) -> int:
        return self.config.agents[agent_name].max_requests


def build_department(
    config: DepartmentConfig,
    *,
    model: Optional[Model] = None,
    toolsets: Optional[Dict[str, AbstractToolset[Any]]] = None,
) -> Department:
    """Every agent of the department. ``model``/``toolsets`` replace the real
    ones in tests (a FunctionModel, a FunctionToolset with the same id)."""
    model = model or build_models(config)
    model_name = config.models.default.model
    model_config = ActivityConfig(
        start_to_close_timeout=timedelta(seconds=config.models.default.timeout_seconds + 30),
        retry_policy=_MODEL_RETRY,
    )
    department = Department(config=config, model_name=model_name)
    for name, spec in config.agents.items():
        if spec.kind == "ticket":
            assert spec.toolset
            mcp_spec = config.mcp[spec.toolset]
            base = (toolsets or {}).get(spec.toolset) or build_toolset(config, spec.toolset)
            department.ticket = Agent(
                model,
                name=name,
                instructions=config.instructions(name),
                deps_type=RunDeps,
                output_type=TicketOutcome,
                retries=spec.retries,
                toolsets=[base.filtered(tool_filter(spec.tools))],
                capabilities=[
                    TemporalDurability(
                        model_activity_config=model_config,
                        toolset_activity_config={
                            mcp_spec.id: ActivityConfig(
                                start_to_close_timeout=timedelta(seconds=mcp_spec.timeout_seconds + 15),
                                retry_policy=_TOOL_RETRY,
                            )
                        },
                    )
                ],
            )
        else:
            agent: Agent[CompletionDeps, str] = Agent(
                model,
                name=name,
                deps_type=CompletionDeps,
                output_type=str,
                retries=spec.retries,
                capabilities=[TemporalDurability(model_activity_config=model_config)],
            )
            agent.output_validator(_json_object_validator)
            department.completions[name] = agent
    return department
