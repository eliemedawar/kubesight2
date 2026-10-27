"""Process settings, from the environment (the chart's ConfigMap and Secret).

Endpoints and credentials live here; what the agents ARE lives in the
department file (``config.py``). A secret may also come from a file named by
``<NAME>_FILE``, the way KubeSight reads its own.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path


def secret(name: str) -> str:
    value = os.getenv(name, "").strip()
    if value:
        return value
    path = os.getenv(f"{name}_FILE", "").strip()
    if not path:
        return ""
    try:
        return Path(path).read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def _int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, "").strip() or default)
    except ValueError:
        return default


@dataclass(frozen=True)
class Settings:
    agents_config: str
    temporal_address: str
    temporal_namespace: str
    # The department queue this process serves (worker) or starts work on (API).
    task_queue: str
    # Where KubeSight is: its MCP server is <url>/api/mcp, the platform's
    # callbacks go to <url>/api/agent-platform/...
    kubesight_url: str
    # Shared with KubeSight, both directions: KubeSight presents it to this
    # API, and the worker presents it on its callbacks.
    platform_token: str
    # The agent-platform user's ksa_ token for KubeSight's MCP server.
    kubesight_agent_token: str
    # How long the completions endpoint waits for its workflow.
    completion_wait_seconds: int
    # How long a ticket workflow stays open with nothing to do.
    ticket_idle_days: int

    @property
    def mcp_url(self) -> str:
        return f"{self.kubesight_url}/api/mcp"


@lru_cache(maxsize=1)
def get() -> Settings:
    return Settings(
        agents_config=os.getenv("AGENTS_CONFIG", "/etc/agents/agents.yaml"),
        temporal_address=os.getenv("TEMPORAL_ADDRESS", "localhost:7233"),
        temporal_namespace=os.getenv("TEMPORAL_NAMESPACE", "agents"),
        task_queue=os.getenv("TASK_QUEUE", "infra"),
        kubesight_url=os.getenv("KUBESIGHT_URL", "http://localhost:5000").rstrip("/"),
        platform_token=secret("AGENT_PLATFORM_TOKEN"),
        kubesight_agent_token=secret("KUBESIGHT_AGENT_TOKEN"),
        completion_wait_seconds=max(30, _int("COMPLETION_WAIT_SECONDS", 900)),
        ticket_idle_days=max(1, _int("TICKET_IDLE_DAYS", 30)),
    )
