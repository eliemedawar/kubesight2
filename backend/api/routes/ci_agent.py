"""The agent API — the one KubeSight surface an outside machine talks to.

Authenticated by the agent's own token, not a user session: an agent is a
machine, and nobody is sitting at it. Mirrors ``ci_worker`` in shape, and like
it, this blueprint is deliberately small — an agent can heartbeat, claim one
task, stream logs, upload artifacts, and report an outcome. Nothing else.
"""

from __future__ import annotations

import os
import tempfile

from flask import Blueprint, request

from ..db import db
from ..response import error_response, success_response
from ..services.ci import agents as agents_service
from ..services.ci import artifacts as artifacts_service
from ..services.ci.runners.base import ArtifactRef

ci_agent_bp = Blueprint("ci_agent", __name__, url_prefix="/api/ci/agent")

_MAX_ARTIFACT_BYTES = int(os.getenv("CI_MAX_ARTIFACT_MB", "512")) * 1024 * 1024


def _payload() -> dict:
    return request.get_json(silent=True) or {}


def _runner():
    """The agent making this request, from its bearer token."""
    header = request.headers.get("Authorization", "")
    token = header[len("Bearer "):].strip() if header.startswith("Bearer ") else ""
    return agents_service.authenticate(token)


@ci_agent_bp.route("/heartbeat", methods=["POST"])
def heartbeat():
    """"I am alive, here is what I can do." Also how an agent learns it has
    been disabled or drained, so it can stop asking for work."""
    try:
        runner = _runner()
    except agents_service.AgentError as exc:
        return error_response(str(exc), 401)
    return success_response(agents_service.heartbeat(runner, _payload()))


@ci_agent_bp.route("/claim", methods=["POST"])
def claim():
    """Take the next task addressed to this agent, if there is one.

    204 means "nothing for you" — the ordinary answer, and cheap enough for an
    idle agent to ask every few seconds.
    """
    try:
        runner = _runner()
    except agents_service.AgentError as exc:
        return error_response(str(exc), 401)
    task = agents_service.claim_next(runner)
    if task is None:
        return ("", 204)
    return success_response(task)


@ci_agent_bp.route("/tasks/<int:task_id>/logs", methods=["POST"])
def task_logs(task_id: int):
    """Output as the agent produces it. Masked on the way in."""
    try:
        runner = _runner()
        payload = _payload()
        task = agents_service.authorize_task(runner, task_id, payload.get("claimToken"))
    except agents_service.AgentError as exc:
        return error_response(str(exc), 401)
    lines = payload.get("lines")
    if not isinstance(lines, list):
        return error_response("Send lines as a list.", 400)
    return success_response({"accepted": agents_service.append_logs(task, lines)})


@ci_agent_bp.route("/tasks/<int:task_id>/artifacts", methods=["POST"])
def task_artifact(task_id: int):
    """A file the stage produced, streamed from the agent's disk into the store."""
    try:
        runner = _runner()
        task = agents_service.authorize_task(
            runner, task_id, request.form.get("claimToken")
        )
    except agents_service.AgentError as exc:
        return error_response(str(exc), 401)

    if request.content_length and request.content_length > _MAX_ARTIFACT_BYTES + 65536:
        return error_response("Artifact exceeds the configured size limit.", 413)
    upload = request.files.get("file")
    if upload is None:
        return error_response("An artifact needs a file.", 400)

    build = db.session.get(agents_service.CiBuild, task.build_id)
    if build is None:
        return error_response("That build no longer exists.", 409)

    name = str(request.form.get("name") or upload.filename or "artifact")[:255]
    handle, temp_path = tempfile.mkstemp(prefix="ci-agent-artifact-")
    try:
        with os.fdopen(handle, "wb") as sink:
            upload.save(sink)
        row = artifacts_service.record_artifact(
            service_id=build.service_id,
            build_id=build.id,
            build_stage_id=task.build_stage_id,
            ref=ArtifactRef(
                name=name,
                artifact_type=str(request.form.get("type") or "binary")[:32],
                local_path=temp_path,
                metadata={
                    "declaredPath": str(request.form.get("declaredPath") or "")[:512],
                    "sourcePath": str(request.form.get("sourcePath") or "")[:512],
                },
            ),
            commit_sha=build.commit_sha,
            branch=build.branch,
            version=str(build.number),
            commit=True,
        )
    finally:
        try:
            os.remove(temp_path)
        except OSError:
            pass
    return success_response({"id": row.id, "name": row.name}, status_code=201)


@ci_agent_bp.route("/tasks/<int:task_id>/result", methods=["POST"])
def task_result(task_id: int):
    """The verdict. The engine turns it into the stage's status on its next tick."""
    try:
        runner = _runner()
        payload = _payload()
        task = agents_service.authorize_task(runner, task_id, payload.get("claimToken"))
    except agents_service.AgentError as exc:
        return error_response(str(exc), 401)
    agents_service.report_result(task, payload)
    return success_response({"ok": True})
