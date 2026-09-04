"""Look inside a running build's workspace.

The workspace is an ``emptyDir`` shared by every stage of one build: it exists
only while the build pod does, and it is gone the moment the pod is removed.
So this is a live view, not an archive — the honest answer once a build has
finished is that there is nothing left to look at, not an empty directory.

Listings only, never file content. A workspace routinely holds credentials a
stage wrote for its own use (a ``gradle.properties``, a kubeconfig); serving
file bodies would quietly turn "view this build" into "read this build's
secrets". Names, sizes and types answer what this is for: did the previous
stage produce the file the next one expects, and is it empty?
"""

from __future__ import annotations

from typing import Any, Dict, List

from ...models_ci import CiBuild, CiBuildStage
from .runners.base import RunnerError

ROOT = "/workspace"


class WorkspaceError(Exception):
    """A workspace cannot be listed, with a reason worth showing a person."""


def _normalize(path: str) -> str:
    """Confine a requested path to the workspace.

    The path is interpolated into a shell command on the far side, so this is a
    security boundary, not tidying: anything that could escape the workspace or
    the quoting is refused outright rather than sanitized into something the
    caller did not ask for.
    """
    raw = (path or ROOT).strip() or ROOT
    if not raw.startswith("/"):
        raw = f"{ROOT}/{raw}"
    while "//" in raw:
        raw = raw.replace("//", "/")
    if len(raw) > 1:
        raw = raw.rstrip("/")
    if raw != ROOT and not raw.startswith(f"{ROOT}/"):
        raise WorkspaceError("Only the build workspace can be browsed.")
    if ".." in raw.split("/"):
        raise WorkspaceError("Paths may not step outside the workspace.")
    if any(ch in raw for ch in "'\"\\\n\r$`"):
        raise WorkspaceError("That path contains characters that are not allowed.")
    return raw


def _live_stage(build: CiBuild) -> CiBuildStage:
    """The stage whose container is currently running.

    Stages share the workspace, so any running container can see what every
    earlier stage wrote — but there has to BE one, since the listing is taken
    by executing inside it.
    """
    running = [
        stage
        for stage in sorted(build.stages, key=lambda s: s.position)
        if stage.status == "running" and stage.external_ref
    ]
    if not running:
        raise WorkspaceError(
            "The workspace can only be browsed while a stage is running — it "
            "lives with the build pod and is removed when the build ends."
        )
    return running[-1]


def list_directory(build: CiBuild, path: str = ROOT) -> Dict[str, Any]:
    """One directory of ``build``'s live workspace."""
    from .engine import _adapter_for, _handle_for  # local: engine imports this module's siblings

    target = _normalize(path)
    stage = _live_stage(build)

    adapter = _adapter_for(build)
    if adapter is None:
        raise WorkspaceError("The runner that started this build is unavailable.")
    lister = getattr(adapter, "list_workspace", None)
    if lister is None:
        raise WorkspaceError(
            f"The {getattr(adapter, 'runner_type', 'assigned')} runner cannot show a "
            "live workspace."
        )
    handle = _handle_for(build, stage)
    if handle is None:
        raise WorkspaceError("That stage has not started on a runner yet.")

    try:
        entries: List[Dict[str, Any]] = lister(handle, target)
    except RunnerError as exc:
        raise WorkspaceError(str(exc) or "The workspace could not be read.")

    return {
        "path": target,
        "parent": None if target == ROOT else target.rsplit("/", 1)[0] or ROOT,
        "stage": {"name": stage.name, "position": stage.position},
        "entries": entries,
    }
