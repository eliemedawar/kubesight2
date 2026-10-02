"""Parallel stage groups: which stages run at the same time, and the rules.

A group is a run of CONSECUTIVE stages that share one non-empty
``parallelGroup`` name — lint, unit tests and Sonar, say. The build starts every
member together, waits for the slowest, and only then moves on. Failure follows
Jenkins' ``parallel`` default: a failing member does not stop its siblings, they
run to completion, and the group fails afterwards if any member that is not
``continueOnFailure`` failed — the stages after it are skipped exactly as they
are after any other failure. A group may instead ``failFast``: the first such
failure stops the members still running.

The rules, enforced on save here and mirrored in the editor
(``frontend/.../pipeline/stageModel.js``):

* **Consecutive.** One name, one run. The same name split by another stage is
  refused rather than quietly turned into two groups.
* **Runner stages that do not build the tree.** Command, scan and container
  image stages may run in parallel (BuildKit builds several images at once
  without complaint). A checkout may not: every stage after it — the group's
  own members included — needs the tree it clones. Deploy, approval and app
  store upload stages run on the KubeSight server after the runner is done,
  one at a time, and never join a group.
* **Two to eight members.** A group needs at least two ENABLED members — a
  group of one is just a stage. Disabled members may stay in the group: they
  are left out of builds like any disabled stage. Eight is the ceiling because
  on Kubernetes every member is a container holding its resource requests for
  the rest of the build (see ``resources.effective_pod_requests``).
* **One fail-fast switch per group.** Stored on every member; when members
  disagree on save, any member asking for fail-fast turns it on for all.

At run time, a member skipped by its own run condition (or because the runner
cannot execute its kind) is closed as skipped when the group starts and the
others still run together. A group left with one runnable member simply runs it.

What this cannot check: whether the members depend on each other. They share
ONE workspace, so two members writing the same file race. The editor says so.

Where the cluster cannot run members side by side — Kubernetes before 1.29,
which has no native sidecar containers, or ``CI_PARALLEL_STAGES=off`` — a group
runs its members one after another with the same failure semantics, and every
member's log says why.
"""

from __future__ import annotations

import os
import re
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from ...models_ci import SERVER_STAGE_TYPES

# Stage kinds that may share a group. Kept small and explicit: a kind added to
# the catalog later has to be thought about before it can run alongside others.
PARALLEL_STAGE_TYPES = frozenset({"command", "scan", "container_image"})

MIN_GROUP_SIZE = 2
MAX_GROUP_SIZE = 8
MAX_NAME_CHARS = 64

PARALLEL = "parallel"
SEQUENTIAL = "sequential"

# Timeouts of members running side by side in ONE pod. The pod enforces them
# itself (a member's own ``timeout`` wrapper first, the group's barrier a few
# seconds later), because the engine's usual answer to a stage over its time —
# deleting the build's Job — would take every sibling down with it. The engine
# keeps a later backstop of its own for a pod that stopped reporting.
POD_TIMEOUT_GRACE_SECONDS = 15
ENGINE_TIMEOUT_GRACE_SECONDS = 120

_ENV_MODES = ("auto", "on", "off")

_KIND_LABELS = {
    "checkout": "Checkout",
    "deploy": "Deploy",
    "approval": "Approval",
    "store_upload": "App store upload",
    "publish_artifact": "Publish artifact",
}


class GroupError(ValueError):
    """A parallel group broke one of the rules above. Message is user-facing."""

    code = "invalid_parallel_group"


# ---------------------------------------------------------------------------
# Names
# ---------------------------------------------------------------------------

def normalize_name(value: Any) -> Optional[str]:
    """A group name as stored: whitespace collapsed, None when empty.

    Compared case-insensitively (:func:`group_key`), so "Checks" and "checks"
    are one group; the first member's spelling is what every member keeps.
    """
    if value is None:
        return None
    text = re.sub(r"\s+", " ", str(value)).strip()
    if not text:
        return None
    if any(ord(char) < 32 for char in text):
        raise GroupError("A parallel group name cannot contain control characters.")
    if len(text) > MAX_NAME_CHARS:
        raise GroupError(
            f"Parallel group names are at most {MAX_NAME_CHARS} characters — "
            f"'{text[:24]}…' is longer."
        )
    return text


def group_key(value: Any) -> str:
    return (normalize_name(value) or "").lower() if value else ""


# ---------------------------------------------------------------------------
# Save-time validation
# ---------------------------------------------------------------------------

def validate(stages: List[Dict[str, Any]]) -> None:
    """Check and canonicalize every group in a normalized stage list.

    ``stages`` are ``pipelines.normalize_stage`` results (model kwargs):
    ``name``, ``stage_type``, ``enabled``, ``parallel_group`` and
    ``parallel_fail_fast``. Rewrites the last two in place so every member of a
    group carries the same spelling and the same fail-fast switch.
    """
    # Runs first, all of them, so a split group is reported as a split — not
    # as its first half being "a group of one".
    runs_found: List[Tuple[str, int, int]] = []  # (key, start, end)
    seen: Dict[str, int] = {}  # group key -> start of the run that used it
    index = 0
    while index < len(stages):
        key = group_key(stages[index].get("parallel_group"))
        if not key:
            stages[index]["parallel_group"] = None
            stages[index]["parallel_fail_fast"] = False
            index += 1
            continue
        end = index
        while end + 1 < len(stages) and group_key(stages[end + 1].get("parallel_group")) == key:
            end += 1
        if key in seen:
            first = stages[seen[key]]
            between = stages[index - 1]
            name = normalize_name(first["parallel_group"])
            raise GroupError(
                f"Stages in the parallel group '{name}' must sit next to each other, "
                f"but '{between['name']}' comes between '{first['name']}' and "
                f"'{stages[index]['name']}'. Move them together, or give the second set its "
                "own group."
            )
        seen[key] = index
        runs_found.append((key, index, end))
        index = end + 1

    for _, start, end in runs_found:
        members = stages[start : end + 1]
        name = normalize_name(members[0]["parallel_group"])
        _check_members(name, members)
        fail_fast = any(bool(member.get("parallel_fail_fast")) for member in members)
        for member in members:
            member["parallel_group"] = name
            member["parallel_fail_fast"] = fail_fast


def _check_members(name: str, members: List[Dict[str, Any]]) -> None:
    for member in members:
        stage_type = member.get("stage_type") or "command"
        if stage_type in PARALLEL_STAGE_TYPES:
            continue
        label = _KIND_LABELS.get(stage_type, stage_type)
        if stage_type == "checkout":
            reason = (
                "a checkout cannot run in parallel: every stage after it, the group's "
                "own members included, needs the source it clones. Keep it before the group."
            )
        elif stage_type in SERVER_STAGE_TYPES:
            reason = (
                f"{label} stages run on the KubeSight server once the build itself has "
                "finished, one at a time, so they cannot join a parallel group."
            )
        else:
            reason = f"{label} stages cannot run in parallel."
        raise GroupError(f"Stage '{member['name']}' is in the parallel group '{name}', but {reason}")

    if len(members) > MAX_GROUP_SIZE:
        raise GroupError(
            f"The parallel group '{name}' has {len(members)} stages; a group runs at most "
            f"{MAX_GROUP_SIZE} at once. Split it into two groups one after the other."
        )
    enabled = [member for member in members if member.get("enabled") is not False]
    if len(enabled) < MIN_GROUP_SIZE:
        if len(members) == 1:
            detail = f"'{members[0]['name']}' is its only stage"
        elif enabled:
            detail = f"only '{enabled[0]['name']}' is turned on"
        else:
            detail = "every stage in it is turned off"
        raise GroupError(
            f"The parallel group '{name}' needs at least two stages that are turned on, "
            f"and {detail}. Add a stage to the group (or turn one back on), or take the "
            "stage out of the group."
        )


# ---------------------------------------------------------------------------
# Run time — over a build's snapshot definitions (enabled stages only)
# ---------------------------------------------------------------------------

def _definition_key(definition: Dict[str, Any]) -> str:
    definition = definition or {}
    if (definition.get("stageType") or "command") not in PARALLEL_STAGE_TYPES:
        return ""
    try:
        return group_key(definition.get("parallelGroup"))
    except GroupError:
        return ""


def runs(definitions: Sequence[Dict[str, Any]]) -> List[List[int]]:
    """Positions of every group of two or more consecutive members.

    Read off a SNAPSHOT, so it is defensive about what it finds: a member of a
    kind that may not run in parallel ends the run rather than joining it, and
    a run of one is no group at all.
    """
    found: List[List[int]] = []
    index = 0
    while index < len(definitions):
        key = _definition_key(definitions[index])
        if not key:
            index += 1
            continue
        end = index
        while end + 1 < len(definitions) and _definition_key(definitions[end + 1]) == key:
            end += 1
        if end > index:
            found.append(list(range(index, end + 1)))
        index = end + 1
    return found


def has_groups(definitions: Sequence[Dict[str, Any]]) -> bool:
    return bool(runs(definitions))


def group_positions(definitions: Sequence[Dict[str, Any]], position: int) -> List[int]:
    """The positions of the group ``position`` belongs to, or ``[position]``."""
    for run in runs(definitions):
        if position in run:
            return run
    return [position]


def group_name(definitions: Sequence[Dict[str, Any]], position: int) -> Optional[str]:
    if len(group_positions(definitions, position)) < 2:
        return None
    try:
        return normalize_name((definitions[position] or {}).get("parallelGroup"))
    except GroupError:
        return None


def fail_fast(definitions: Sequence[Dict[str, Any]], positions: Iterable[int]) -> bool:
    return any(
        bool((definitions[position] or {}).get("parallelFailFast"))
        for position in positions
        if 0 <= position < len(definitions)
    )


def member_failed(status: str, definition: Dict[str, Any]) -> bool:
    """Whether this member's outcome fails its group.

    ``continueOnFailure`` means here what it means anywhere: the failure is
    recorded (and still fails the build) but does not stop what comes after.
    """
    if status in ("success", "skipped", "pending", "running"):
        return False
    return not bool((definition or {}).get("continueOnFailure"))


def critical_path_seconds(steps: Iterable[Sequence[Optional[int]]]) -> int:
    """Wall time of a pipeline whose steps are lists of member durations: a
    group costs its slowest member, a lone stage its own duration."""
    total = 0
    for durations in steps:
        values = [int(value) for value in durations if value is not None]
        if values:
            total += max(values)
    return total


# ---------------------------------------------------------------------------
# Capability
# ---------------------------------------------------------------------------

def env_mode() -> str:
    """``CI_PARALLEL_STAGES``: auto (default) | on | off.

    ``off`` makes every runner run groups one stage at a time. ``on`` only
    matters on Kubernetes, where it skips the cluster version check — for a
    cluster that reports an old version but has the SidecarContainers feature
    gate switched on by hand.
    """
    value = os.getenv("CI_PARALLEL_STAGES", "auto").strip().lower() or "auto"
    return value if value in _ENV_MODES else "auto"


OFF_REASON = (
    "Parallel stages are switched off on this installation (CI_PARALLEL_STAGES=off), "
    "so the group runs one stage at a time."
)


def resolve(adapter) -> Tuple[str, str]:
    """``(mode, reason)`` for a build on this runner adapter.

    An adapter states its own capability through an optional
    ``parallel_capability() -> (bool, reason)``; one without it dispatches
    every member as its own unit of work, which is parallel by construction.
    """
    if env_mode() == "off":
        return SEQUENTIAL, OFF_REASON
    asker = getattr(adapter, "parallel_capability", None)
    if callable(asker):
        try:
            supported, reason = asker()
        except Exception:  # pragma: no cover - a probe must never break a build
            supported, reason = False, "KubeSight could not tell whether this runner can run stages side by side."
        return (PARALLEL if supported else SEQUENTIAL), str(reason or "")
    return PARALLEL, ""


def installation_capability() -> Dict[str, Any]:
    """How groups run here, for the pipeline editor's health check.

    ``{"mode": auto|on|off, "kubernetes": {"supported", "reason"} | None}``.
    The Kubernetes answer is only looked up when an enabled Kubernetes runner
    exists — an installation without one has no cluster to ask — and comes from
    the runner's cached version read, so a keystroke in the editor does not
    cost a round trip to the cluster.
    """
    from ...models_ci import CiRunner
    from .runners import get_adapter

    mode = env_mode()
    kubernetes = None
    if mode != "off":
        has_kubernetes = (
            CiRunner.query.filter(CiRunner.runner_type == "kubernetes", CiRunner.enabled.is_(True)).count()
            > 0
        )
        adapter = get_adapter("kubernetes") if has_kubernetes else None
        asker = getattr(adapter, "parallel_capability", None)
        if callable(asker):
            supported, reason = asker()
            kubernetes = {"supported": bool(supported), "reason": str(reason or "")}
    return {"mode": mode, "kubernetes": kubernetes}


def sequential_notice(name: str, reason: str) -> str:
    """The line every member of a sequentially-run group carries in its log."""
    return (
        f"[kubesight] This stage is in the parallel group '{name}', but the group ran one "
        f"stage at a time on this build: {reason}"
    ).strip()
