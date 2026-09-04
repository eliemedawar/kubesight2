"""The runner port.

Everything the CI engine knows about *where* a stage runs is in this file. The
engine never imports a concrete runner, never imports ``kubectl``, and never
branches on runner type — it resolves an adapter by name and calls this
protocol. That is what lets a Kubernetes Job, a Linux agent, and a Mac agent be
the same thing to the engine.

Two transport shapes satisfy one protocol:

* **Push** runners (Kubernetes) are driven actively: ``start`` applies a Job,
  ``poll`` reads its status, ``drain_logs`` pumps ``kubectl logs``.
* **Pull** runners (external agents) drive themselves: ``start`` marks the stage
  assignable, and ``poll``/``drain_logs`` read rows the agent's own outbound
  callbacks wrote.

The engine cannot tell the difference, which is the point.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, Iterator, List, Optional, Protocol, runtime_checkable

# Stage outcomes as the engine understands them. Deliberately a smaller set than
# CiBuildStage.status: 'skipped' is an engine decision, not a runner report.
QUEUED = "queued"
RUNNING = "running"
SUCCEEDED = "succeeded"
FAILED = "failed"
TIMEOUT = "timeout"
CANCELLED = "cancelled"

TERMINAL_STATUSES = frozenset({SUCCEEDED, FAILED, TIMEOUT, CANCELLED})


class RunnerError(RuntimeError):
    """A runner could not be driven. Fails the stage with a safe message."""


@dataclass(frozen=True)
class StageRequirements:
    """What a stage needs from a runner, independent of any runner.

    ``runner_type`` pins a specific kind of runner. When it is None the
    scheduler is free to pick any runner whose capabilities cover ``labels`` —
    that is how an Android stage can land on either a Kubernetes Job or a Linux
    agent depending on which are registered.
    """

    runner_type: Optional[str] = None
    labels: tuple = ()
    image: Optional[str] = None
    resources: Dict[str, Any] = field(default_factory=dict)


@dataclass
class StageExecution:
    """Everything a runner needs to execute one stage, fully resolved.

    ``secrets`` holds decrypted values. It is built per execution, passed to the
    adapter, and never persisted, logged, or serialized into an API response.
    """

    build_id: int
    build_number: int
    stage_id: int
    service_slug: str
    stage_name: str
    stage_type: str
    image: Optional[str]
    working_directory: Optional[str]
    commands: List[str]
    env: Dict[str, str]
    secrets: Dict[str, str] = field(default_factory=dict)
    resources: Dict[str, Any] = field(default_factory=dict)
    artifacts: List[Dict[str, Any]] = field(default_factory=list)
    # [{"ip": ..., "hostnames": [...]}] — extra name resolution for the build.
    # Pod-scoped on Kubernetes, so a whole-build runner merges every stage's.
    host_aliases: List[Dict[str, Any]] = field(default_factory=list)
    timeout_seconds: int = 1800
    continue_on_failure: bool = False
    position: int = 0
    # Shared per-build workspace identity. The Kubernetes runner maps this to a
    # Job name and an emptyDir; an agent maps it to a directory.
    workspace_ref: str = ""
    # Source coordinates, resolved once per build.
    repository_url: Optional[str] = None
    branch: Optional[str] = None
    commit_sha: Optional[str] = None
    # Registry push target for container_image stages (host, repository, tag,
    # username, password, verifyTls). Decrypted like secrets — never persisted.
    registry: Optional[Dict[str, Any]] = None
    # Worker callback: where an in-cluster job reports artifacts and metadata,
    # and the fresh plaintext token authorizing it (its hash is on the build).
    callback_url: str = ""
    callback_token: str = ""
    # Set on the FIRST stage only: the full resolved stage list for the build.
    # Whole-build runners (one Kubernetes Job per build) construct everything
    # from this; per-stage runners ignore it.
    plan: Optional[List["StageExecution"]] = None


@dataclass
class RunnerHandle:
    """A runner's own reference to in-flight work.

    Persisted on the stage row (``external_ref``) so a backend restart can
    reattach instead of orphaning the work.
    """

    runner_id: int
    external_ref: str
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass
class LogChunk:
    """One masked slice of stage output, ready to persist."""

    seq: int
    content: str
    stream: str = "stdout"


@dataclass
class ArtifactRef:
    """Something a stage produced, before it is written to ``ci_artifacts``."""

    name: str
    artifact_type: str
    # Exactly one of these is set: a local path the store should ingest, or an
    # already-published URI (a pushed container image).
    local_path: Optional[str] = None
    uri: Optional[str] = None
    digest: Optional[str] = None
    size_bytes: Optional[int] = None
    metadata: Dict[str, Any] = field(default_factory=dict)


@runtime_checkable
class RunnerAdapter(Protocol):
    """The contract every execution target implements.

    Adapters may additionally expose ``supported_stage_types() -> set[str]``;
    the engine skips (never dispatches) a stage whose type the assigned
    adapter does not support, so an unexecuted stage can never report success.
    Adapters without the method are assumed to support checkout + command.

    An adapter may also expose ``list_workspace(handle, path) -> list[dict]``
    returning ``{"name", "type", "size"}`` entries for one directory of a
    running build's workspace. It is optional: an adapter with no way to look
    inside a live workspace simply does not implement it, and the API says so
    rather than pretending the workspace is empty.
    """

    runner_type: str

    def can_run(self, requirements: StageRequirements) -> bool:
        """Whether this adapter can satisfy the stage at all (type + labels)."""

    def start(self, execution: StageExecution) -> RunnerHandle:
        """Begin the stage. Raises :class:`RunnerError` if it cannot start."""

    def poll(self, handle: RunnerHandle) -> str:
        """Current status: one of the module-level status constants."""

    def drain_logs(self, handle: RunnerHandle, after_seq: int) -> Iterator[LogChunk]:
        """Log chunks produced after ``after_seq``. Must be resumable."""

    def collect_artifacts(self, handle: RunnerHandle) -> List[ArtifactRef]:
        """Artifacts the stage declared. Called once, after it succeeds."""

    def cancel(self, handle: RunnerHandle) -> None:
        """Request termination. Best-effort; ``poll`` reports the real outcome."""

    def cleanup(self, handle: RunnerHandle) -> None:
        """Release runner-side resources. Must be safe to call more than once."""


# ---------------------------------------------------------------------------
# Adapter registry
#
# Adapters register themselves by runner type. Phase 1 ships 'mock' only; the
# Kubernetes Job adapter and the external-agent adapter drop in here without the
# engine changing.
# ---------------------------------------------------------------------------

_ADAPTERS: Dict[str, RunnerAdapter] = {}


def register_adapter(adapter: RunnerAdapter) -> None:
    _ADAPTERS[adapter.runner_type] = adapter


def get_adapter(runner_type: str) -> Optional[RunnerAdapter]:
    return _ADAPTERS.get((runner_type or "").strip())


def available_runner_types() -> List[str]:
    """Runner types with a working adapter — the scheduler will not assign to
    a runner whose adapter has not shipped yet."""
    return sorted(_ADAPTERS)


def capabilities_cover(capabilities, required_labels) -> bool:
    """Whether ``capabilities`` is a superset of ``required_labels``."""
    have = {str(c).strip().lower() for c in (capabilities or []) if str(c).strip()}
    need = {str(l).strip().lower() for l in (required_labels or []) if str(l).strip()}
    return need.issubset(have)
