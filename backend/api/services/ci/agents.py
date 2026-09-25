"""Agent enrolment and the claim protocol.

An agent is a machine KubeSight cannot reach — somebody's Mac mini, a build VM
behind a firewall. So the agent reaches in: it authenticates with a token issued
once when the runner is created, heartbeats to say it is alive, and claims work
addressed to it.

The claim payload is built HERE, at claim time, and never stored. It carries the
stage's commands, environment and decrypted secrets, which is why the endpoint
that serves it is token-authenticated and why nothing writes it to a row.
"""

from __future__ import annotations

import hmac
import logging
import os
import secrets as secrets_module
from datetime import datetime, timedelta, timezone
from hashlib import sha256
from typing import Any, Dict, List, Optional, Tuple

from ...audit import log_audit
from ...db import db
from . import cache_layout
from ...models_ci import RUNNER_TYPES, CiAgentTask, CiBuild, CiBuildStage, CiRunner

logger = logging.getLogger(__name__)

AGENT_RUNNER_TYPES = ("agent_linux", "agent_macos")

# How long an agent may go quiet before it is treated as offline. The agent
# heartbeats far more often; this is the grace, not the interval.
HEARTBEAT_GRACE_SECONDS = 90

# How often an idle agent asks for work. Handed out on every heartbeat so the
# fleet's pickup latency is one server-side setting rather than an argument
# somebody has to change on every machine (an agent started with --poll keeps
# its own value). A claim is one indexed lookup, so seconds here cost far more
# in perceived slowness than they save in load.
DEFAULT_AGENT_POLL_SECONDS = 2.0


def agent_poll_seconds() -> float:
    raw = os.getenv("CI_AGENT_POLL_SECONDS", "").strip()
    if not raw:
        return DEFAULT_AGENT_POLL_SECONDS
    try:
        return max(0.2, min(60.0, float(raw)))
    except ValueError:
        return DEFAULT_AGENT_POLL_SECONDS


class AgentError(ValueError):
    """An agent request was rejected. Message is user-facing."""


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _aware(value: Optional[datetime]) -> Optional[datetime]:
    if value is None:
        return None
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def _hash(token: str) -> str:
    return sha256((token or "").encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# Enrolment
# ---------------------------------------------------------------------------

def create_agent(payload: Dict[str, Any], *, actor=None) -> Tuple[CiRunner, str]:
    """Register an agent and mint its token.

    Returns the row and the PLAINTEXT token, which is the only time it exists —
    only its hash is stored, exactly like an API token. The caller shows it once
    and says so.
    """
    name = " ".join(str(payload.get("name") or "").split())[:120]
    if not name:
        raise AgentError("The agent needs a name.")
    if CiRunner.query.filter_by(name=name).first():
        raise AgentError(f"A runner named '{name}' already exists.")

    runner_type = str(payload.get("runnerType") or "agent_linux").strip().lower()
    if runner_type not in AGENT_RUNNER_TYPES:
        raise AgentError(
            f"An agent is one of: {', '.join(AGENT_RUNNER_TYPES)}."
        )

    token = secrets_module.token_urlsafe(32)
    runner = CiRunner(
        name=name,
        description=" ".join(str(payload.get("description") or "").split())[:2000] or None,
        runner_type=runner_type,
        # Offline until the agent actually checks in. Claiming to be online
        # before anything connected would put builds in a queue nothing serves.
        status="offline",
        enabled=payload.get("enabled") is not False,
        capabilities=_labels(payload.get("capabilities")),
        labels=_labels(payload.get("labels")),
        max_concurrent=max(1, min(int(payload.get("maxConcurrent") or 1), 20)),
        runner_metadata=_metadata_with_workspace({}, payload.get("workspaceRoot")),
        is_builtin=False,
        token_prefix=token[:8],
        token_hash=_hash(token),
    )
    db.session.add(runner)
    db.session.commit()
    log_audit(
        "ci_agent_registered",
        actor=actor,
        target_type="ci_runner",
        target_id=str(runner.id),
        details={"name": runner.name, "type": runner.runner_type},
    )
    return runner, token


def rotate_token(runner: CiRunner, *, actor=None) -> str:
    """Issue a new token and invalidate the old one immediately."""
    if runner.runner_type not in AGENT_RUNNER_TYPES:
        raise AgentError("Only agents authenticate with a token.")
    token = secrets_module.token_urlsafe(32)
    runner.token_prefix = token[:8]
    runner.token_hash = _hash(token)
    # The old token stops working now, so the agent is offline until it is
    # reconfigured. Saying so beats showing "online" for a process that can no
    # longer authenticate.
    runner.status = "offline"
    db.session.add(runner)
    db.session.commit()
    log_audit(
        "ci_agent_token_rotated",
        actor=actor,
        target_type="ci_runner",
        target_id=str(runner.id),
        details={"name": runner.name},
    )
    return token


def _labels(value: Any) -> List[str]:
    items = value if isinstance(value, (list, tuple)) else str(value or "").split(",")
    out: List[str] = []
    for item in items[:50]:
        label = " ".join(str(item or "").split()).lower()[:64]
        if label and label not in out:
            out.append(label)
    return out


def _metadata_with_workspace(metadata: Dict[str, Any], value: Any) -> Dict[str, Any]:
    """Where this agent should put its builds.

    Advisory by nature: KubeSight cannot check that a path exists on a machine
    it cannot reach, so the agent applies it and reports back if it cannot —
    which is why an agent that rejects the path surfaces as an error on the
    runner rather than as silence.
    """
    root = str(value or "").strip()[:512]
    out = dict(metadata or {})
    if root:
        out["workspaceRoot"] = root
    else:
        out.pop("workspaceRoot", None)
    return out


def workspace_root(runner: CiRunner) -> str:
    return str((runner.runner_metadata or {}).get("workspaceRoot") or "")


def set_workspace_root(runner: CiRunner, value: Any) -> None:
    runner.runner_metadata = _metadata_with_workspace(runner.runner_metadata, value)
    db.session.add(runner)


def install_hint(runner: CiRunner) -> Dict[str, str]:
    """What to run on the machine, ready to copy.

    Registering an agent is the moment somebody has to go and do something on
    another computer, so the answer travels with the token rather than living
    in documentation they would have to find.
    """
    base = os.getenv("KUBESIGHT_PUBLIC_URL", "").strip().rstrip("/")
    url = base or "https://kubesight.example.com"
    service = "launchd (macOS)" if runner.runner_type == "agent_macos" else "systemd (Linux)"
    return {
        "url": url,
        "runnerType": runner.runner_type,
        "keepAliveWith": service,
        # The agent is one Python file with no dependencies beyond the standard
        # library, because the machines it runs on are somebody's laptop or a
        # locked-down build box.
        "command": (
            f"python3 kubesight-agent.py --url {url} --token <TOKEN>"
        ),
    }


# ---------------------------------------------------------------------------
# The agent side
# ---------------------------------------------------------------------------

def authenticate(token: str) -> CiRunner:
    """The runner this token belongs to, or a refusal.

    Compared by hash in constant time; a token is never stored or logged.
    """
    digest = _hash(token or "")
    if not token:
        raise AgentError("An agent token is required.")
    for runner in CiRunner.query.filter(
        CiRunner.runner_type.in_(AGENT_RUNNER_TYPES),
        CiRunner.token_hash.isnot(None),
    ).all():
        if hmac.compare_digest(runner.token_hash or "", digest):
            return runner
    raise AgentError("That agent token is not recognised.")


def heartbeat(runner: CiRunner, payload: Dict[str, Any]) -> Dict[str, Any]:
    """Record that the agent is alive and what it can do.

    Capabilities are reported by the AGENT, not configured in KubeSight: the
    machine knows what it has installed, and a stale list in the UI would route
    an Xcode build to a box that no longer has Xcode.
    """
    runner.last_heartbeat_at = _now()
    runner.hostname = str(payload.get("hostname") or "")[:253] or runner.hostname
    runner.os = str(payload.get("os") or "")[:32] or runner.os
    runner.os_version = str(payload.get("osVersion") or "")[:64] or runner.os_version
    runner.arch = str(payload.get("arch") or "")[:16] or runner.arch
    runner.version = str(payload.get("version") or "")[:64] or runner.version
    reported = _labels(payload.get("capabilities"))
    if reported:
        runner.capabilities = reported
    # An agent that cannot use the configured workspace says so here. Surfacing
    # it on the runner beats a machine that quietly builds somewhere else.
    problem = str(payload.get("workspaceError") or "").strip()[:2000]
    runner.last_error = problem or None
    if runner.enabled and runner.status != "draining":
        runner.status = "online"
    db.session.add(runner)
    db.session.commit()
    return {
        "runnerId": runner.id,
        "name": runner.name,
        "enabled": bool(runner.enabled),
        "status": runner.status,
        # The agent stops asking for work when told to drain, which is how a
        # machine is taken out of service without killing a build mid-flight.
        "accepting": bool(runner.enabled) and runner.status == "online",
        "heartbeatSeconds": max(10, HEARTBEAT_GRACE_SECONDS // 3),
        # How soon to ask for work again when there is none.
        "pollSeconds": agent_poll_seconds(),
        # Empty means "your own default": a path set on the command line always
        # wins, because the person at the machine knows its disks.
        "workspaceRoot": workspace_root(runner),
    }


def mark_stale_agents_offline(*, commit: bool = True) -> None:
    """An agent that stops heartbeating is offline, not online-and-silent.

    Runs on the engine tick. Without it the scheduler would keep assigning to a
    machine that has been switched off, and those builds would sit in a queue
    nobody serves.
    """
    cutoff = _now() - timedelta(seconds=HEARTBEAT_GRACE_SECONDS)
    changed = False
    for runner in CiRunner.query.filter(
        CiRunner.runner_type.in_(AGENT_RUNNER_TYPES),
        CiRunner.status == "online",
    ).all():
        last = _aware(runner.last_heartbeat_at)
        if last is None or last < cutoff:
            runner.status = "offline"
            db.session.add(runner)
            changed = True
    if changed and commit:
        db.session.commit()


def claim_next(runner: CiRunner) -> Optional[Dict[str, Any]]:
    """The next task for this agent, fully resolved, or None.

    The payload is built here and never stored: it carries decrypted secrets,
    and a row holding those would undo the care the rest of CI takes with them.
    """
    if not runner.enabled or runner.status == "draining":
        return None
    running = CiAgentTask.query.filter_by(runner_id=runner.id, state="claimed").count()
    if running >= max(1, int(runner.max_concurrent or 1)):
        return None

    task = (
        CiAgentTask.query.filter_by(runner_id=runner.id, state="queued")
        .order_by(CiAgentTask.id.asc())
        .first()
    )
    if task is None:
        return None

    stage = db.session.get(CiBuildStage, task.build_stage_id)
    build = db.session.get(CiBuild, task.build_id)
    if stage is None or build is None:
        task.state = "done"
        task.error = "The build this task belonged to is gone."
        db.session.add(task)
        db.session.commit()
        return None

    claim_token = secrets_module.token_urlsafe(24)
    task.state = "claimed"
    task.claimed_at = _now()
    task.last_heartbeat_at = _now()
    task.claim_token_hash = _hash(claim_token)
    runner.last_assigned_at = _now()
    db.session.add_all([task, runner])
    db.session.commit()

    return {**_task_payload(build, stage, task), "claimToken": claim_token}


def _task_payload(build: CiBuild, stage: CiBuildStage, task: CiAgentTask) -> Dict[str, Any]:
    """Everything the agent needs to run this one stage on its own machine."""
    from . import engine as engine_service

    definition = engine_service._definition_for(build, stage)
    execution = engine_service._build_execution(build, stage, definition)

    payload: Dict[str, Any] = {
        "taskId": task.id,
        "buildId": build.id,
        "buildNumber": build.number,
        "stageId": stage.id,
        "stageName": stage.name,
        "stageType": execution.stage_type,
        "position": stage.position,
        # One directory per build on the agent's disk, so stages of a build
        # share a workspace exactly as they do in a pod.
        "workspace": execution.workspace_ref or f"build-{build.id}",
        "workingDirectory": execution.working_directory or "",
        # The stage's container image. On Linux, an agent with docker or podman
        # runs the stage inside it — same image, same build as the cluster
        # produces. Without a runtime the agent says so and uses the machine's
        # own tools, so this is additive for every existing agent.
        "image": execution.image or "",
        # Only honoured in container mode (--memory/--cpus); a stage running
        # directly on a machine gets the machine.
        "resources": dict(execution.resources or {}),
        "commands": list(execution.commands or []),
        # Secrets are merged into the environment here, once, in flight.
        "env": {**(execution.env or {}), **(execution.secrets or {})},
        "artifacts": list(execution.artifacts or []),
        "timeoutSeconds": int(execution.timeout_seconds or 1800),
        "continueOnFailure": bool(execution.continue_on_failure),
        "hostAliases": list(execution.host_aliases or []),
    }
    if execution.stage_type == "checkout":
        payload["checkout"] = {
            "url": execution.repository_url or "",
            "revision": execution.commit_sha or execution.branch or "",
            # The same fixed-username rule the in-cluster checkout uses.
            "credentialType": (execution.secrets or {}).get(
                "KUBESIGHT_GIT_CREDENTIAL_TYPE", ""
            ),
            "token": (execution.secrets or {}).get("KUBESIGHT_GIT_TOKEN", ""),
        }
    else:
        _add_node_modules_cache(payload, execution)
    return payload


def _add_node_modules_cache(payload: Dict[str, Any], execution) -> None:
    """Keep node_modules between builds on an agent, as the Kubernetes runner does.

    The restore/save shell is added to the commands HERE, on the server, rather
    than copied into the agent script: one text for every runner, and an agent
    that is never upgraded still gets it. The agent's part is only to say where
    the archives go - ``cacheSlug`` is the directory name it uses, already made
    safe by the same rule as the cluster's per-service subtree. An agent too old
    to know ``cacheSlug`` runs the wrapper against ``$KUBESIGHT_CACHE_DIR`` in a
    container, and as a plain install without one.
    """
    payload["cacheSlug"] = cache_layout.slug_dir(execution.service_slug)
    if os.getenv("CI_CACHE_NODE_MODULES", "1").strip().lower() in _NODE_MODULES_OFF:
        return
    if not cache_layout.runs_node_install(execution.commands):
        return
    try:
        keep = max(1, int(os.getenv("CI_CACHE_NODE_MODULES_KEEP", "") or cache_layout.NODE_MODULES_KEEP))
    except ValueError:
        keep = cache_layout.NODE_MODULES_KEEP
    payload["commands"] = [
        cache_layout.node_modules_wrap(
            "\n".join(execution.commands or ["true"]),
            image=execution.image or "",
            workdir=execution.working_directory or "",
            keep=keep,
        )
    ]


_NODE_MODULES_OFF = {"off", "none", "no", "0", "false"}


def authorize_task(runner: CiRunner, task_id: int, claim_token: str) -> CiAgentTask:
    """The task this agent claimed, or a refusal.

    The claim token matters: without it a process that woke up long after its
    task was reaped could still post a result over whatever ran next.
    """
    task = db.session.get(CiAgentTask, int(task_id))
    if task is None or task.runner_id != runner.id:
        raise AgentError("That task does not belong to this agent.")
    if not task.claim_token_hash or not hmac.compare_digest(
        task.claim_token_hash, _hash(claim_token or "")
    ):
        raise AgentError("That claim is no longer valid — the task was reassigned.")
    return task


def append_logs(task: CiAgentTask, lines: List[Dict[str, Any]]) -> int:
    """Store output the agent produced, masked like every other log path."""
    from . import engine as engine_service
    from . import logs as logs_service

    stage = db.session.get(CiBuildStage, task.build_stage_id)
    build = db.session.get(CiBuild, task.build_id)
    if stage is None or build is None:
        return 0

    mask = logs_service.build_masker(engine_service._mask_values(build))
    chunks = []
    seq = int(task.log_seq or 0)
    for line in lines[:5000]:
        if not isinstance(line, dict):
            line = {"content": str(line)}
        seq += 1
        chunks.append((seq, str(line.get("content") or ""), str(line.get("stream") or "stdout")))
    if chunks:
        logs_service.append(stage, chunks, mask=mask)
        task.log_seq = seq
        task.last_heartbeat_at = _now()
        db.session.add(task)
        db.session.commit()
    return len(chunks)


def report_result(task: CiAgentTask, payload: Dict[str, Any]) -> None:
    """The agent's verdict for a task. The engine turns it into a stage status."""
    try:
        exit_code = int(payload.get("exitCode"))
    except (TypeError, ValueError):
        exit_code = -1
    task.exit_code = exit_code
    task.error = (str(payload.get("error") or "").strip() or None)
    task.state = "done"
    task.finished_at = _now()
    task.last_heartbeat_at = _now()
    db.session.add(task)
    db.session.commit()
