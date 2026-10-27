"""The workflows: one per ticket, one per completion.

**TicketWorkflow** (id ``ticket-<provider>-<record id>``) lives as long as the
ticket is active. Every KubeSight task on that ticket — the new ticket, a
follow-up after the deploy finished, the requester's reply — arrives as a
``task`` signal, and the workflow handles them one at a time, in order. No
thread waits on a model for ten minutes: a restart of the worker loses
nothing, Temporal resumes from the last finished model or tool call.

**CompletionWorkflow** answers one OpenAI-style request (source analysis, CI
assist) with a JSON object; the API waits for it.

Workflow code is deterministic: the agents' model and MCP calls are
activities (TemporalDurability), and everything else here only shuffles data.
"""

from __future__ import annotations

import asyncio
import json
from datetime import timedelta
from typing import Dict, List, Optional

from pydantic import BaseModel, Field
from temporalio import workflow
from temporalio.common import RetryPolicy
from temporalio.exceptions import ApplicationError

with workflow.unsafe.imports_passed_through():
    from pydantic_ai import UsageLimits

    # Absolute names: the sandbox passes a module through by its full name,
    # and the worker's installed department lives in the host's runtime.
    import agent_platform.runtime as runtime
    from agent_platform.activities import report_ticket_task
    from agent_platform.contracts import (
        CompletionDeps,
        CompletionRequest,
        CompletionResult,
        RunDeps,
        TaskReport,
        TicketRef,
        TicketTask,
    )

TICKET_WORKFLOW = "TicketWorkflow"
COMPLETION_WORKFLOW = "CompletionWorkflow"

# Hand the history over to a fresh run before it grows heavy.
_TASKS_PER_RUN = 25
# Reports kept to answer a re-sent task (KubeSight lost the first callback).
_REPORTS_KEPT = 20

_REPORT_RETRY = RetryPolicy(
    initial_interval=timedelta(seconds=5),
    backoff_coefficient=2.0,
    maximum_interval=timedelta(minutes=5),
)


class TicketWorkflowInput(BaseModel):
    ref: TicketRef
    idle_days: int = 30
    # Carried over continue-as-new.
    queue: List[TicketTask] = Field(default_factory=list)
    reports: Dict[int, TaskReport] = Field(default_factory=dict)
    handled: int = 0


def _root(exc: BaseException) -> BaseException:
    # An ActivityError says "Activity task failed"; the reason is its cause.
    while getattr(exc, "cause", None) is not None:
        exc = exc.cause  # type: ignore[attr-defined]
    return exc


def _error_text(exc: BaseException) -> str:
    root = _root(exc)
    return (str(root) or type(root).__name__)[:500]


def _error_type(exc: BaseException) -> str:
    root = _root(exc)
    return getattr(root, "type", None) or type(root).__name__


@workflow.defn(name=TICKET_WORKFLOW)
class TicketWorkflow:
    def __init__(self) -> None:
        self._queue: List[TicketTask] = []
        self._reports: Dict[int, TaskReport] = {}
        self._resend: List[int] = []
        self._current: Optional[int] = None
        self._closed = False
        self._handled = 0

    @workflow.signal
    def task(self, task: TicketTask) -> None:
        if task.task_id in self._reports:
            # Already answered: KubeSight is asking again because it never got
            # the report. Send the same one, do not run the agent twice.
            self._resend.append(task.task_id)
            return
        if task.task_id == self._current or any(t.task_id == task.task_id for t in self._queue):
            return
        self._queue.append(task)

    @workflow.signal
    def close(self) -> None:
        self._closed = True

    @workflow.query
    def status(self) -> dict:
        return {
            "current": self._current,
            "queued": [t.task_id for t in self._queue],
            "handled": self._handled,
            "closed": self._closed,
        }

    @workflow.run
    async def run(self, start: TicketWorkflowInput) -> None:
        self._queue = list(start.queue) + self._queue
        self._reports.update(start.reports)
        self._handled = start.handled
        idle = timedelta(days=start.idle_days)

        while True:
            try:
                await workflow.wait_condition(
                    lambda: bool(self._queue or self._resend or self._closed),
                    timeout=idle,
                    timeout_summary="ticket idle",
                )
            except asyncio.TimeoutError:
                break
            while self._resend:
                report = self._reports.get(self._resend.pop(0))
                if report is not None:
                    await self._report(report)
            if self._queue:
                task = self._queue.pop(0)
                self._current = task.task_id
                report = await self._handle(start.ref, task)
                self._remember(report)
                self._current = None
                self._handled += 1
                await self._report(report)
            elif self._closed:
                break
            if not self._resend and (
                self._handled - start.handled >= _TASKS_PER_RUN
                or workflow.info().is_continue_as_new_suggested()
            ):
                await workflow.wait_condition(workflow.all_handlers_finished)
                workflow.continue_as_new(
                    TicketWorkflowInput(
                        ref=start.ref,
                        idle_days=start.idle_days,
                        queue=self._queue,
                        reports=self._reports,
                        handled=self._handled,
                    )
                )
        await workflow.wait_condition(workflow.all_handlers_finished)

    def _remember(self, report: TaskReport) -> None:
        self._reports[report.task_id] = report
        for old in sorted(self._reports)[:-_REPORTS_KEPT]:
            del self._reports[old]

    async def _handle(self, ref: TicketRef, task: TicketTask) -> TaskReport:
        department = runtime.department()
        agent = department.ticket
        if agent is None:
            return TaskReport(
                task_id=task.task_id,
                status="failed",
                error=f"The {department.name} department has no ticket agent.",
            )
        try:
            result = await agent.run(
                json.dumps(task.message, ensure_ascii=False, separators=(",", ":")),
                deps=RunDeps(department=department.name, requester=f"{ref.provider}#{ref.record_id}"),
                usage_limits=UsageLimits(request_limit=department.usage_limit(agent.name or "")),
            )
        except Exception as exc:  # noqa: BLE001 — the report says why; KubeSight decides
            workflow.logger.warning("Ticket task %s failed: %s", task.task_id, exc)
            return TaskReport(
                task_id=task.task_id,
                status="failed",
                model=department.model_name,
                error=_error_text(exc),
            )
        return TaskReport(
            task_id=task.task_id,
            status="finished",
            outcome=result.output.outcome,
            summary=result.output.summary,
            model=department.model_name,
        )

    async def _report(self, report: TaskReport) -> None:
        try:
            await workflow.execute_activity(
                report_ticket_task,
                report,
                start_to_close_timeout=timedelta(seconds=60),
                schedule_to_close_timeout=timedelta(days=1),
                retry_policy=_REPORT_RETRY,
            )
        except Exception as exc:  # noqa: BLE001 — KubeSight's reaper asks again
            workflow.logger.warning("Could not report task %s: %s", report.task_id, exc)


@workflow.defn(name=COMPLETION_WORKFLOW)
class CompletionWorkflow:
    @workflow.run
    async def run(self, request: CompletionRequest) -> CompletionResult:
        department = runtime.department()
        agent = department.completions.get(request.agent)
        if agent is None:
            raise ApplicationError(
                f"No completion agent named {request.agent!r} in the {department.name} department.",
                type="UnknownAgent",
                non_retryable=True,
            )
        settings = (
            {"extra_body": {"response_format": {"type": "json_object"}}} if request.json_object else None
        )
        try:
            result = await agent.run(
                request.prompt,
                instructions=request.instructions,
                deps=CompletionDeps(
                    department=department.name,
                    requester=request.requester,
                    json_object=request.json_object,
                    required_keys=request.required_keys,
                ),
                model_settings=settings,
                usage_limits=UsageLimits(request_limit=department.usage_limit(request.agent)),
            )
        except Exception as exc:  # noqa: BLE001 — named, so the API can map it
            raise ApplicationError(_error_text(exc), type=_error_type(exc), non_retryable=True) from exc
        usage = result.usage
        return CompletionResult(
            text=result.output,
            model=department.model_name,
            input_tokens=usage.input_tokens or 0,
            output_tokens=usage.output_tokens or 0,
        )
