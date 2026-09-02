"""Runner adapters.

Importing this package registers every adapter that has shipped. Adding a
runner type is one module plus one ``register_adapter`` call here — the engine
and the scheduler need no change.
"""

from .base import (  # noqa: F401
    CANCELLED,
    FAILED,
    QUEUED,
    RUNNING,
    SUCCEEDED,
    TERMINAL_STATUSES,
    TIMEOUT,
    ArtifactRef,
    LogChunk,
    RunnerAdapter,
    RunnerError,
    RunnerHandle,
    StageExecution,
    StageRequirements,
    available_runner_types,
    capabilities_cover,
    get_adapter,
    register_adapter,
)
from .kubernetes import KubernetesJobRunnerAdapter
from .mock import MockRunnerAdapter

register_adapter(MockRunnerAdapter())
register_adapter(KubernetesJobRunnerAdapter())

# Phase 5: register_adapter(ExternalAgentRunnerAdapter())
