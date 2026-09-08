"""External agent runner — the PULL half of the runner port.

KubeSight cannot reach into somebody's Mac, and that is the whole point of this
adapter. Where the Kubernetes runner applies a Job and drives it (``poll`` reads
pod status, ``drain_logs`` shells out to ``kubectl logs``), an agent runs on a
machine KubeSight has no route to. So the flow inverts:

    start()      records a claim ticket for the assigned runner and returns
    <the agent>  polls /api/ci/agent/claim, gets the resolved payload once,
                 runs it on its own machine, and posts logs, artifacts and the
                 exit code back through the agent API
    poll()       reads the ticket the agent's own callbacks updated
    drain_logs() yields nothing: those callbacks already appended the lines

The engine cannot tell the difference between this and the Kubernetes runner,
which is exactly what the port exists for.

**Why macOS needs this at all:** Apple's toolchain only runs on Apple hardware,
so an ``.ipa`` cannot be produced in a Kubernetes pod under any configuration.
An agent is not a convenience there, it is the only route.

Nothing about a task's *content* is stored. Commands, environment and decrypted
secrets are rebuilt at claim time and travel once over the API — persisting them
would put every build's secrets in the database, which the rest of CI is
careful never to do.
"""

from __future__ import annotations

import logging
import secrets as secrets_module
from datetime import datetime, timedelta, timezone
from hashlib import sha256
from typing import Any, Dict, Iterator, List, Optional

from ....db import db
from ....models_ci import CiAgentTask
from .base import (
    CANCELLED,
    FAILED,
    QUEUED,
    RUNNING,
    SKIPPED,
    SUCCEEDED,
    ArtifactRef,
    LogChunk,
    RunnerError,
    RunnerHandle,
    StageExecution,
    StageRequirements,
)

logger = logging.getLogger(__name__)

# A claimed task whose agent stops reporting is presumed lost. Generous, because
# a long compile can legitimately produce no output — the agent heartbeats on a
# timer of its own precisely so silence is not mistaken for death.
CLAIM_TIMEOUT_MINUTES = 10


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _aware(value: Optional[datetime]) -> Optional[datetime]:
    if value is None:
        return None
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def hash_token(value: str) -> str:
    return sha256((value or "").encode("utf-8")).hexdigest()


def new_token() -> str:
    return secrets_module.token_urlsafe(32)


class ExternalAgentRunnerAdapter:
    """One adapter per agent platform. Behaviour is identical; the type is what
    a stage pins and what an agent registers as, so a macOS-only stage cannot
    land on a Linux box."""

    # Agents run whatever the machine can run. container_image is deliberately
    # absent: it means "build with BuildKit", which is the cluster's job.
    SUPPORTED_STAGE_TYPES = frozenset({"checkout", "command"})

    def __init__(self, runner_type: str):
        self.runner_type = runner_type

    # -- capabilities --------------------------------------------------------

    def supported_stage_types(self) -> set:
        return set(self.SUPPORTED_STAGE_TYPES)

    def skip_reason(self, stage_type: str) -> Optional[str]:
        if stage_type == "container_image":
            return (
                "Container image builds run on the Kubernetes runner with BuildKit, "
                "not on an agent."
            )
        return None

    def can_run(self, requirements: StageRequirements) -> bool:
        return requirements.runner_type in (None, self.runner_type)

    # -- lifecycle -----------------------------------------------------------

    def start(self, execution: StageExecution) -> RunnerHandle:
        """Offer the stage to its runner. Nothing executes until an agent claims it."""
        if not execution.runner_id:
            raise RunnerError("This stage was not assigned to an agent.")
        task = CiAgentTask(
            build_id=execution.build_id,
            build_stage_id=execution.stage_id,
            runner_id=execution.runner_id,
            state="queued",
        )
        db.session.add(task)
        db.session.flush()
        return RunnerHandle(runner_id=execution.runner_id, external_ref=f"agent:{task.id}")

    def poll(self, handle: RunnerHandle) -> str:
        task = _task_for(handle)
        if task is None:
            # The ticket is gone: the build was deleted or reset underneath us.
            return FAILED
        if task.state == "queued":
            return QUEUED
        if task.state == "claimed":
            # An agent that stops heartbeating is not "still running": say so,
            # rather than pinning the build until its stage timeout expires.
            last = _aware(task.last_heartbeat_at) or _aware(task.claimed_at) or _now()
            if _now() - last > timedelta(minutes=CLAIM_TIMEOUT_MINUTES):
                task.state = "done"
                task.error = (
                    f"The agent stopped reporting for more than "
                    f"{CLAIM_TIMEOUT_MINUTES} minutes."
                )
                task.exit_code = task.exit_code if task.exit_code is not None else -1
                task.finished_at = _now()
                db.session.add(task)
                db.session.commit()
                return FAILED
            return RUNNING
        if task.exit_code == 0:
            return SUCCEEDED
        if (task.error or "").startswith("cancelled"):
            return CANCELLED
        if task.exit_code is None and task.error is None:
            return SKIPPED
        return FAILED

    def drain_logs(self, handle: RunnerHandle, after_seq: int) -> Iterator[LogChunk]:
        # The agent posts its output as it produces it, and that callback appends
        # to the stage's log directly — masked on the way in, like every other
        # path. There is nothing left for the engine to pull.
        return iter(())

    def collect_artifacts(self, handle: RunnerHandle) -> List[ArtifactRef]:
        # Uploaded by the agent through the agent API while the stage runs.
        return []

    def cancel(self, handle: RunnerHandle) -> None:
        """Ask the agent to stop. Best effort by construction: the agent finds
        out on its next heartbeat, so a stage cancelled mid-command ends when
        that command does."""
        task = _task_for(handle)
        if task is None or task.state == "done":
            return
        task.error = "cancelled by KubeSight"
        if task.state == "queued":
            # Never claimed, so nothing has to be told anything.
            task.state = "done"
            task.exit_code = -1
            task.finished_at = _now()
        db.session.add(task)
        db.session.commit()

    def cleanup(self, handle: RunnerHandle) -> None:
        return None


def _task_for(handle: RunnerHandle) -> Optional[CiAgentTask]:
    ref = (handle.external_ref or "")
    if not ref.startswith("agent:"):
        return None
    try:
        return db.session.get(CiAgentTask, int(ref.split(":", 1)[1]))
    except (TypeError, ValueError):
        return None
