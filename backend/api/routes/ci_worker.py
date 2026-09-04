"""In-cluster CI job callbacks.

The build pod's checkout container reports the resolved commit here, and its
collector container uploads declared artifacts here. Authentication is the
per-build bearer token whose sha256 lives on the build row — no user session,
no permission keys, and the token dies with the build.

These endpoints trust nothing about the caller beyond the token: uploads are
size-capped, filenames sanitized by the artifact store, and a build that is no
longer running cannot be written to.
"""

from __future__ import annotations

import hmac
import os
import tempfile
from datetime import datetime, timedelta, timezone
from hashlib import sha256
from typing import Optional, Tuple

from flask import Blueprint, request

from ..db import db
from ..models_ci import CiBuild, CiBuildStage
from ..response import error_response, success_response
from ..services.ci import artifacts as artifacts_service
from ..services.ci.runners.base import ArtifactRef

ci_worker_bp = Blueprint("ci_worker", __name__, url_prefix="/api/ci/worker")

_MAX_ARTIFACT_BYTES = int(os.getenv("CI_MAX_ARTIFACT_MB", "512")) * 1024 * 1024
# How long after a build ends its own collector may still upload. Sized to
# outlast a large artifact upload, not to keep a pod useful indefinitely.
_UPLOAD_GRACE_SECONDS = int(os.getenv("CI_ARTIFACT_UPLOAD_GRACE_SECONDS", "900"))


def _authorized_build(
    build_id: int, *, allow_finished: bool = False
) -> Tuple[Optional[CiBuild], Optional[tuple]]:
    header = request.headers.get("Authorization", "")
    if not header.startswith("Bearer "):
        return None, error_response("Unauthorized", 401)
    token = header[len("Bearer "):].strip()
    build = db.session.get(CiBuild, int(build_id))
    if build is None or not build.worker_callback_token_hash or not token:
        return None, error_response("Unauthorized", 401)
    digest = sha256(token.encode("utf-8")).hexdigest()
    if not hmac.compare_digest(digest, build.worker_callback_token_hash):
        return None, error_response("Unauthorized", 401)
    if build.status != "running":
        # A finished or cancelled build is immutable; a stale pod must not be
        # able to append to it. Artifacts are the exception: a build pod keeps
        # running after a stage fails so its collector can still upload what
        # earlier stages produced, and that upload legitimately lands after the
        # build has been marked failed. Bounded by the grace window AND by the
        # per-build token, so it is the build's own pod or nobody.
        if not (allow_finished and _within_upload_grace(build)):
            return None, error_response("This build is no longer running.", 409)
    return build, None


def _within_upload_grace(build: CiBuild) -> bool:
    finished = build.finished_at or build.started_at
    if finished is None:
        return False
    if finished.tzinfo is None:
        finished = finished.replace(tzinfo=timezone.utc)
    return datetime.now(timezone.utc) - finished <= timedelta(seconds=_UPLOAD_GRACE_SECONDS)


def _stage_for_position(build: CiBuild, raw_position) -> Optional[CiBuildStage]:
    try:
        position = int(raw_position)
    except (TypeError, ValueError):
        return None
    for stage in build.stages:
        if stage.position == position:
            return stage
    return None


@ci_worker_bp.route("/builds/<int:build_id>/meta", methods=["POST"])
def report_meta(build_id: int):
    build, err = _authorized_build(build_id)
    if err:
        return err
    payload = request.get_json(silent=True) or {}
    commit_sha = str(payload.get("commitSha") or "").strip()[:64]
    if commit_sha and all(c in "0123456789abcdefABCDEF" for c in commit_sha):
        build.commit_sha = commit_sha
    message = str(payload.get("commitMessage") or "").strip()
    if message:
        build.commit_message = message[:2000]
    db.session.add(build)
    db.session.commit()
    return success_response({"ok": True})


@ci_worker_bp.route("/builds/<int:build_id>/artifacts", methods=["POST"])
def upload_artifact(build_id: int):
    build, err = _authorized_build(build_id, allow_finished=True)
    if err:
        return err

    if request.content_length and request.content_length > _MAX_ARTIFACT_BYTES + 65536:
        return error_response("Artifact exceeds the configured size limit.", 413)

    upload = request.files.get("file")
    if upload is not None:
        # File form: the collector streams a produced file (jar, apk, report).
        stage = _stage_for_position(build, request.form.get("stagePosition"))
        name = str(request.form.get("name") or upload.filename or "artifact")[:255]
        artifact_type = str(request.form.get("type") or "binary")[:32]
        handle, temp_path = tempfile.mkstemp(prefix="ci-artifact-")
        try:
            with os.fdopen(handle, "wb") as sink:
                upload.save(sink)
            row = artifacts_service.record_artifact(
                service_id=build.service_id,
                build_id=build.id,
                build_stage_id=stage.id if stage else None,
                ref=ArtifactRef(
                    name=name,
                    artifact_type=artifact_type,
                    local_path=temp_path,
                    metadata={"declaredPath": str(request.form.get("declaredPath") or "")[:512]},
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

    # JSON form: an already-published artifact (a pushed container image) —
    # only its coordinates are recorded, no bytes travel through KubeSight.
    payload = request.get_json(silent=True) or {}
    uri = str(payload.get("uri") or "").strip()[:1024]
    if not uri:
        return error_response("An artifact needs a file or a uri.", 400)
    stage = _stage_for_position(build, payload.get("stagePosition"))
    metadata = payload.get("metadata")
    row = artifacts_service.record_artifact(
        service_id=build.service_id,
        build_id=build.id,
        build_stage_id=stage.id if stage else None,
        ref=ArtifactRef(
            name=str(payload.get("name") or uri.rsplit("/", 1)[-1])[:255],
            artifact_type=str(payload.get("type") or "container-image")[:32],
            uri=uri,
            digest=str(payload.get("digest") or "").strip()[:128] or None,
            metadata=metadata if isinstance(metadata, dict) else {},
        ),
        commit_sha=build.commit_sha,
        branch=build.branch,
        version=str(build.number),
        registry_connection_id=(
            build.service.registry_connection_id if build.service else None
        ),
        commit=True,
    )
    return success_response({"id": row.id, "name": row.name}, status_code=201)
