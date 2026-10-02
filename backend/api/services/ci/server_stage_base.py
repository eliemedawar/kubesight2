"""What every server-side stage other than Deploy shares.

The Approval and App store upload stages keep their progress on
``CiBuildStage.server_state`` (Deploy keeps its own on ``deploy_state``) and
close through the engine's ``_close_stage``, so the build row, the runner slot
and the reaper see them exactly as they see any other stage.
"""

from __future__ import annotations

import time
from datetime import datetime, timezone
from typing import Any, Dict

from ...db import db
from ...models_ci import CiBuild, CiBuildStage
from . import logs as logs_service

# A waiting stage writes a line at least this often. It is also what keeps the
# engine's idle reaper (no log output for CI_STALE_BUILD_MINUTES) from taking a
# long approval wait or a slow store upload for a lost build.
HEARTBEAT_SECONDS = 300.0


def now() -> datetime:
    return datetime.now(timezone.utc)


def state(stage: CiBuildStage) -> Dict[str, Any]:
    return dict(stage.server_state) if isinstance(stage.server_state, dict) else {}


def save(stage: CiBuildStage, patch: Dict[str, Any]) -> None:
    # A new dict every time: JSON columns only notice reassignment.
    stage.server_state = {**state(stage), **patch}
    db.session.add(stage)


def log(stage: CiBuildStage, prefix: str, message: str) -> None:
    logs_service.append_system(stage, f"{prefix} {message}", commit=False)


def heartbeat(stage: CiBuildStage, prefix: str, message: str) -> None:
    current = state(stage)
    if time.time() - float(current.get("heartbeatAt") or 0) >= HEARTBEAT_SECONDS:
        save(stage, {"heartbeatAt": time.time()})
        log(stage, prefix, message)


def begin(stage: CiBuildStage, patch: Dict[str, Any]) -> None:
    """Mark a server stage running, with its first state."""
    stage.status = "running"
    stage.started_at = now()
    stage.runner_id = None
    stage.server_state = dict(patch)
    db.session.add(stage)
    db.session.flush()


def finish(stage: CiBuildStage, status: str, message: str, *, outcome: str) -> None:
    from . import engine

    save(stage, {"phase": "done", "outcome": outcome, "message": message, "finishedAt": now().isoformat()})
    engine._close_stage(stage, status, message if status != "success" else None)


def earlier_failure(build: CiBuild, stage: CiBuildStage) -> str:
    """Why an earlier stage means this one must not act, or "".

    The same rule as the Deploy stage: ``continueOnFailure`` lets later stages
    run to gather information; it never makes a failed build fit to approve or
    to ship.
    """
    for other in sorted(build.stages, key=lambda s: s.position):
        if other.position >= stage.position:
            break
        if other.status in ("failed", "timeout", "cancelled"):
            return f"stage '{other.name}' {other.status}."
    return ""


def elapsed(stage: CiBuildStage) -> int:
    started = stage.started_at
    if started is None:
        return 0
    if started.tzinfo is None:
        started = started.replace(tzinfo=timezone.utc)
    return max(0, int((now() - started).total_seconds()))


def timeout_of(definition: Dict[str, Any]) -> int:
    try:
        return int(definition.get("timeoutSeconds") or 1800)
    except (TypeError, ValueError):
        return 1800


def limit_label(seconds: int) -> str:
    if seconds >= 3600 and seconds % 3600 == 0:
        hours = seconds // 3600
        return f"{hours} h"
    return f"{max(1, round(seconds / 60))} min"
