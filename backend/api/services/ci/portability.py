"""Will this pipeline run on the runner it lands on?

A pipeline is written once and can be assigned to either kind of runner, and
the two are not the same machine:

* The **Kubernetes runner** puts every stage in one pod: a shared emptyDir at
  ``/workspace``, a read-only root filesystem, a non-root uid, no Docker
  socket, and a container image per stage.
* An **agent** uses the machine as it is: its own directory for the workspace
  (which is *not* ``/workspace``), the user's home, whatever tools happen to be
  installed, and no containerisation at all — that is the point of an agent,
  and the only way an iOS build works.

Both export ``KUBESIGHT_WORKSPACE`` and ``KUBESIGHT_SOURCE`` precisely so one
pipeline can run on either. The failures this module reports are the ones where
a stage quietly assumes one of them: an absolute ``/workspace`` path, a
``docker build``, an ``apt-get install``. Each finding says which runner it
breaks on, because "portable" is not the goal for every pipeline — knowing
where a stage cannot run is.

Pure text analysis over the stage list, so it works on an unsaved edit and
costs nothing to run on every keystroke.
"""

from __future__ import annotations

import re
from typing import Any, Dict, Iterable, List, Optional

# Levels: "error" — the stage cannot work there at all; "warning" — it works
# but differently, and the difference has bitten someone; "info" — worth
# knowing, no action implied.
ERROR = "error"
WARNING = "warning"
INFO = "info"

KUBERNETES = "kubernetes"
AGENT = "agent"

_WORKSPACE_PATH = re.compile(r"(?<![\w$/])/workspace(?:/|\b)")
_CACHE_PATH = re.compile(r"(?<![\w$/])/(?:kubesight-)?cache(?:/|\b)")
_DOCKER_CMD = re.compile(r"(?<![\w./-])docker(?:-compose)?\s+(build|run|push|images|ps)\b")
_INSTALL_CMD = re.compile(
    r"(?<![\w./-])(?:sudo\b|apt-get\s+install|apt\s+install|yum\s+install|dnf\s+install|"
    r"apk\s+add|brew\s+install)"
)
# Writes outside the two directories a build pod can write to.
_BAD_WRITE = re.compile(r">\s*/(?!workspace|tmp|cache|dev/null)[A-Za-z0-9_.-]+/")


def _finding(
    stage: Dict[str, Any],
    *,
    level: str,
    code: str,
    breaks_on: Optional[str],
    message: str,
    fix: str = "",
) -> Dict[str, Any]:
    return {
        "stagePosition": stage.get("position"),
        "stageName": stage.get("name") or f"Stage {stage.get('position')}",
        "level": level,
        "code": code,
        # Which runner kind this is about, or None when it is about both.
        "breaksOn": breaks_on,
        "message": message,
        "fix": fix,
    }


def _stage_text(stage: Dict[str, Any]) -> str:
    """Everything an author typed that could carry a path."""
    parts: List[str] = list(stage.get("commands") or [])
    parts.append(str(stage.get("workingDirectory") or ""))
    for key, value in (stage.get("env") or {}).items():
        parts.append(f"{key}={value}")
    for spec in stage.get("artifacts") or []:
        if isinstance(spec, dict):
            parts.append(str(spec.get("path") or ""))
    return "\n".join(parts)


def _targets_agent(stage: Dict[str, Any]) -> bool:
    """Whether this stage can end up on an agent.

    A stage pinned to ``runnerType: kubernetes`` never will, and telling its
    author to stop using /workspace would be noise.
    """
    runner_type = str(stage.get("runnerType") or "").strip().lower()
    if runner_type.startswith("agent"):
        return True
    return runner_type in ("", "any", "none")


def analyze_stage(stage: Dict[str, Any]) -> List[Dict[str, Any]]:
    findings: List[Dict[str, Any]] = []
    if stage.get("enabled") is False:
        return findings

    text = _stage_text(stage)
    stage_type = str(stage.get("stageType") or "command")

    # Only worth saying while the stage could actually land on an agent: once
    # it is pinned to Kubernetes, this is settled and repeating it is noise.
    if stage_type == "container_image" and _targets_agent(stage):
        # Not a mistake — a fact about where it can run. Said plainly because
        # the stage silently skips rather than failing, and a skipped image
        # stage is how a green build produces nothing.
        findings.append(
            _finding(
                stage,
                level=INFO,
                code="image_build_needs_buildkit",
                breaks_on=AGENT,
                message="Container images are built by BuildKit in the cluster, so this stage "
                "is skipped on an agent rather than run.",
                fix="Route this pipeline to the Kubernetes runner, or accept that the image "
                "is only produced there.",
            )
        )

    if _WORKSPACE_PATH.search(text) and _targets_agent(stage):
        findings.append(
            _finding(
                stage,
                level=ERROR,
                code="absolute_workspace",
                breaks_on=AGENT,
                message="/workspace is the Kubernetes runner's directory. On an agent the "
                "workspace is a path on that machine, so this fails with "
                "“No such file or directory”.",
                fix="Use $KUBESIGHT_WORKSPACE (or $KUBESIGHT_SOURCE for the checkout). Both "
                "runners export them.",
            )
        )

    if _CACHE_PATH.search(text):
        findings.append(
            _finding(
                stage,
                level=WARNING,
                code="absolute_cache",
                breaks_on=None,
                message="The cache directory exists only while the build cache is switched "
                "on, and an agent puts it somewhere else entirely. /cache is also the old "
                "path, kept mounted only so pipelines like this one keep working.",
                fix="Use $KUBESIGHT_CACHE_DIR — or the variable for the tool in question: "
                "$GRADLE_USER_HOME, $GRADLE_BUILD_CACHE_DIR, $DC_DATA_DIR, "
                "$SEMGREP_CACHE_DIR — and treat an empty value as “no cache”, so the stage "
                "still runs cold. CI-CACHE.md has the full list.",
            )
        )

    if _DOCKER_CMD.search(text):
        findings.append(
            _finding(
                stage,
                level=ERROR,
                code="docker_in_stage",
                breaks_on=KUBERNETES,
                message="Build pods have no Docker socket and cannot get one — that is what "
                "keeps repository-controlled commands off the node.",
                fix="Use a container_image stage (BuildKit) instead of docker build/push.",
            )
        )

    if _INSTALL_CMD.search(text):
        findings.append(
            _finding(
                stage,
                level=ERROR,
                code="install_in_stage",
                breaks_on=KUBERNETES,
                message="A build container runs as a non-root uid with a read-only root "
                "filesystem, so installing packages (or sudo) fails.",
                fix="Choose a stage image that already has the tool, or run this stage on an "
                "agent that does.",
            )
        )

    if _BAD_WRITE.search(text):
        findings.append(
            _finding(
                stage,
                level=WARNING,
                code="write_outside_workspace",
                breaks_on=KUBERNETES,
                message="Only /workspace and /tmp are writable in a build container; the root "
                "filesystem is read-only.",
                fix="Write under $KUBESIGHT_WORKSPACE or /tmp.",
            )
        )

    if stage.get("image") and _targets_agent(stage) and stage_type == "command":
        findings.append(
            _finding(
                stage,
                level=INFO,
                code="image_ignored_on_agent",
                breaks_on=AGENT,
                message=f"The stage image ({stage['image']}) is used on an agent only when "
                "that machine has docker or podman; otherwise the stage runs with whatever is "
                "installed there.",
                fix="Add the `container` runner label to require a machine that can run the "
                "image, or set KUBESIGHT_CONTAINER=always in the stage environment to refuse "
                "to run outside one.",
            )
        )

    return findings


def analyze(stages: Iterable[Dict[str, Any]]) -> Dict[str, Any]:
    """Findings for a whole pipeline, plus a one-line verdict for the editor."""
    findings: List[Dict[str, Any]] = []
    for stage in stages or []:
        if isinstance(stage, dict):
            findings.extend(analyze_stage(stage))

    errors = [f for f in findings if f["level"] == ERROR]
    warnings = [f for f in findings if f["level"] == WARNING]
    blocked_on = sorted({f["breaksOn"] for f in errors if f["breaksOn"]})

    if errors:
        where = " and ".join(
            {"kubernetes": "the Kubernetes runner", "agent": "an agent"}[name]
            for name in blocked_on
        ) or "some runners"
        summary = (
            f"{len(errors)} stage problem{'' if len(errors) == 1 else 's'} would fail on {where}."
        )
    elif warnings:
        summary = (
            f"{len(warnings)} thing{'' if len(warnings) == 1 else 's'} behave differently "
            "depending on the runner."
        )
    else:
        summary = "Nothing here depends on which runner takes the build."

    return {
        "summary": summary,
        "portable": not errors,
        "counts": {
            "error": len(errors),
            "warning": len(warnings),
            "info": len([f for f in findings if f["level"] == INFO]),
        },
        "findings": findings,
    }
