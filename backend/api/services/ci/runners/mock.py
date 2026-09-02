"""Simulated runner.

Executes a pipeline's shape without executing its commands: stages transition,
logs accumulate, declared artifacts appear. It exists so the engine, the queue,
the scheduler and the whole UI can be exercised — and demoed in mock mode —
before any real executor ships, and so the test suite never needs a cluster.

It runs entirely in-process against a small in-memory table keyed by handle. A
backend restart loses that table; :meth:`poll` reports FAILED for a handle it
does not recognise, which is exactly what the engine's orphan reaper expects.
"""

from __future__ import annotations

import threading
import time
from typing import Dict, Iterator, List

from .base import (
    FAILED,
    RUNNING,
    SUCCEEDED,
    ArtifactRef,
    LogChunk,
    RunnerHandle,
    StageExecution,
    StageRequirements,
    capabilities_cover,
)

# How long a simulated stage "runs" before reporting success. Short enough that
# a build completes while a user watches, long enough that the UI shows the
# running state rather than flashing past it.
_STAGE_SECONDS = 2.0


class _MockRun:
    __slots__ = ("execution", "started_at", "cancelled", "chunks")

    def __init__(self, execution: StageExecution):
        self.execution = execution
        self.started_at = time.monotonic()
        self.cancelled = False
        self.chunks: List[LogChunk] = []


class MockRunnerAdapter:
    """In-process adapter. See :mod:`..base` for the contract."""

    runner_type = "mock"

    def __init__(self) -> None:
        self._runs: Dict[str, _MockRun] = {}
        self._lock = threading.Lock()

    # -- contract ----------------------------------------------------------

    def supported_stage_types(self) -> set:
        return {"checkout", "command"}

    def can_run(self, requirements: StageRequirements) -> bool:
        if requirements.runner_type and requirements.runner_type != self.runner_type:
            return False
        # The mock runner claims every label so any pipeline is demoable, but it
        # still goes through the same capability check as a real runner.
        return capabilities_cover(_MOCK_CAPABILITIES, requirements.labels)

    def start(self, execution: StageExecution) -> RunnerHandle:
        ref = f"mock-{execution.build_id}-{execution.stage_id}"
        run = _MockRun(execution)
        run.chunks = list(_synthetic_log(execution))
        with self._lock:
            self._runs[ref] = run
        return RunnerHandle(runner_id=0, external_ref=ref, metadata={"simulated": True})

    def poll(self, handle: RunnerHandle) -> str:
        with self._lock:
            run = self._runs.get(handle.external_ref)
        if run is None:
            # Lost to a restart. Reported as failed rather than silently
            # succeeding — a build must never claim an outcome it cannot show.
            return FAILED
        if run.cancelled:
            return FAILED
        if time.monotonic() - run.started_at < _STAGE_SECONDS:
            return RUNNING
        return SUCCEEDED

    def drain_logs(self, handle: RunnerHandle, after_seq: int) -> Iterator[LogChunk]:
        with self._lock:
            run = self._runs.get(handle.external_ref)
        if run is None:
            return iter(())
        # Output appears progressively so the UI shows a log growing, not a log
        # arriving all at once at the end.
        elapsed = time.monotonic() - run.started_at
        progress = min(1.0, elapsed / _STAGE_SECONDS) if _STAGE_SECONDS else 1.0
        visible = max(1, int(len(run.chunks) * progress))
        return iter([c for c in run.chunks[:visible] if c.seq > after_seq])

    def collect_artifacts(self, handle: RunnerHandle) -> List[ArtifactRef]:
        with self._lock:
            run = self._runs.get(handle.external_ref)
        if run is None:
            return []
        refs: List[ArtifactRef] = []
        for spec in run.execution.artifacts or []:
            if not isinstance(spec, dict):
                continue
            path = str(spec.get("path") or "").strip()
            if not path:
                continue
            refs.append(
                ArtifactRef(
                    name=str(spec.get("name") or path.rsplit("/", 1)[-1]),
                    artifact_type=str(spec.get("type") or "binary"),
                    uri=f"mock://{run.execution.service_slug}/"
                    f"{run.execution.build_number}/{path}",
                    metadata={"simulated": True, "declaredPath": path},
                )
            )
        return refs

    def cancel(self, handle: RunnerHandle) -> None:
        with self._lock:
            run = self._runs.get(handle.external_ref)
            if run is not None:
                run.cancelled = True

    def cleanup(self, handle: RunnerHandle) -> None:
        with self._lock:
            self._runs.pop(handle.external_ref, None)


_MOCK_CAPABILITIES = (
    "mock", "linux", "java", "java21", "java17", "node", "python",
    "docker", "android", "generic", "kubernetes",
)

# The mock runner never sees a stage type the engine cannot execute — those are
# skipped upstream in ``engine.EXECUTABLE_STAGE_TYPES``. It therefore only has
# to simulate checkout and command stages, and can never be the reason a build
# claims work that did not happen.


def _synthetic_log(execution: StageExecution) -> List[LogChunk]:
    """Plausible output for a stage, echoing what the real runner would do.

    Only command *text* is echoed. Secret values are never rendered here; the
    masker downstream is a second line of defence, not the first.
    """
    lines: List[LogChunk] = []

    def add(text: str, stream: str = "stdout") -> None:
        lines.append(LogChunk(seq=len(lines) + 1, content=text, stream=stream))

    add(f"[mock runner] stage '{execution.stage_name}' ({execution.stage_type})", "system")
    if execution.working_directory:
        add(f"[mock runner] working directory: {execution.working_directory}", "system")
    if execution.env:
        add(f"[mock runner] environment: {', '.join(sorted(execution.env))}", "system")
    if execution.secrets:
        add(
            f"[mock runner] secrets injected: {', '.join(sorted(execution.secrets))}",
            "system",
        )

    if execution.stage_type == "checkout":
        add(f"Cloning {execution.repository_url or 'repository'} ...")
        add(f"Checking out {execution.branch or 'default branch'}")
        if execution.commit_sha:
            add(f"HEAD is now at {execution.commit_sha[:12]}")
        add("Checkout complete.")
    else:
        for command in execution.commands or []:
            add(f"$ {command}")
            add(f"[mock runner] not executed — simulated success")

    for spec in execution.artifacts or []:
        if isinstance(spec, dict) and spec.get("path"):
            add(f"[mock runner] would collect artifact: {spec['path']}", "system")

    add(f"[mock runner] stage '{execution.stage_name}' succeeded", "system")
    return lines
