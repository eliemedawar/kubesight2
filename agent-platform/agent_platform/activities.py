"""The one activity the platform owns: telling KubeSight a ticket task is over.

KubeSight keeps the task row and decides what the report means (a task the
tools never settled is an error an operator can retry; a follow-up gets
KubeSight's own factual comment). A KubeSight outage is retried for a day; a
4xx means KubeSight will never take this report, and retrying it is noise.
"""

from __future__ import annotations

from typing import Any, Awaitable, Callable, Dict

import httpx
from temporalio import activity
from temporalio.exceptions import ApplicationError

from . import settings as platform_settings
from .contracts import TaskReport

PostJson = Callable[[str, Dict[str, Any], Dict[str, str]], Awaitable[int]]


async def _post_json(url: str, body: Dict[str, Any], headers: Dict[str, str]) -> int:
    async with httpx.AsyncClient(timeout=30.0) as client:
        response = await client.post(url, json=body, headers=headers)
        return response.status_code


# Replaced in tests.
post_json: PostJson = _post_json


@activity.defn(name="report_ticket_task")
async def report_ticket_task(report: TaskReport) -> None:
    settings = platform_settings.get()
    url = f"{settings.kubesight_url}/api/agent-platform/ticket-tasks/{report.task_id}/result"
    headers = {
        "Authorization": f"Bearer {settings.platform_token}",
        "User-Agent": "KubeSight-agent-platform",
    }
    try:
        status = await post_json(url, report.model_dump(mode="json"), headers)
    except httpx.HTTPError as exc:
        raise ApplicationError(f"KubeSight is unreachable: {exc}") from exc
    if status == 429 or status >= 500:
        raise ApplicationError(f"KubeSight answered {status}; retrying.")
    if status >= 400:
        raise ApplicationError(
            f"KubeSight refused the report for task {report.task_id} ({status}).",
            non_retryable=True,
        )
