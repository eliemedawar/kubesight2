"""The Temporal worker for one department: ``python -m agent_platform.worker``.

Validates the department file, builds its agents, and polls only that
department's task queue — so a DBA department's trouble never stalls
infrastructure tickets. Any configuration error stops the process before it
polls anything.
"""

from __future__ import annotations

import asyncio
import logging
import signal
import sys
from typing import Optional

from pydantic_ai.durable_exec.temporal import AgentPlugin
from temporalio.client import Client
from temporalio.worker import Worker

from . import runtime
from . import settings as platform_settings
from . import temporal_client
from .activities import report_ticket_task
from .agents import Department, build_department
from .config import load
from .workflows import CompletionWorkflow, TicketWorkflow

logger = logging.getLogger("agent_platform.worker")


def build_worker(client: Client, department: Department, task_queue: Optional[str] = None) -> Worker:
    runtime.install(department)
    return Worker(
        client,
        task_queue=task_queue or department.config.task_queue,
        workflows=[TicketWorkflow, CompletionWorkflow],
        activities=[report_ticket_task],
        plugins=[AgentPlugin(agent) for agent in department.agents()],
    )


async def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    settings = platform_settings.get()
    config = load(settings.agents_config)
    if config.task_queue != settings.task_queue:
        raise SystemExit(
            f"TASK_QUEUE is {settings.task_queue!r} but {settings.agents_config} serves {config.task_queue!r}."
        )
    if config.ticket_agent and not settings.kubesight_agent_token:
        raise SystemExit("KUBESIGHT_AGENT_TOKEN is not set: the ticket agent cannot reach KubeSight's MCP server.")
    if not settings.platform_token:
        raise SystemExit("AGENT_PLATFORM_TOKEN is not set: the worker cannot report back to KubeSight.")
    department = build_department(config)
    client = await temporal_client.connect()
    worker = build_worker(client, department)
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, stop.set)
        except NotImplementedError:  # Windows
            pass
    logger.info(
        "Department %s: agents %s on queue %s",
        config.department,
        ", ".join(a.name or "?" for a in department.agents()),
        config.task_queue,
    )
    async with worker:
        await stop.wait()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        sys.exit(0)
