"""Mobile Applications: registered APK/AAB/IPA apps, their Jenkins-built
binaries, and store publishing (Google Play / App Store Connect).

The trigger side is the existing Zoho→Jenkins automation: a ticket for a custom
Environment runs its configured Jenkins job, and when that build succeeds and
the environment matches a registered MobileApplication, ``on_custom_build_success``
creates a pending MobileAppBuild. The scheduler tick then downloads the artifact
out of Jenkins into KubeSight's own binary store (workspace files are volatile —
the next build overwrites them, so the download happens immediately) and posts a
"build is available" comment back to the Desk ticket.

Publishing is a second tick-advanced state machine (MobileAppPublish): an admin
picks a store + target, the upload runs on its own daemon thread (a multi-hundred
MB upload must never stall the shared scheduler tick), and the outcome is
reported to the ticket + admin emails — the same feedback loop deploys use.
"""

from __future__ import annotations

import fnmatch
import logging
import os
import re
import shutil
import threading
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from ..audit import log_audit
from ..db import db
from ..models import (
    DeployAutomationRun,
    MobileAppBuild,
    MobileApplication,
    MobileAppPublish,
    MobileAppResign,
    ZohoInboundTicket,
)
from ..secret_encryption import decrypt_secret, encrypt_secret
from . import binary_signature, jenkins_client
from .jenkins_client import JenkinsConfig, JenkinsError

logger = logging.getLogger(__name__)

PLATFORMS = ("android", "ios")
ARTIFACT_TYPES = {"android": ("apk", "aab"), "ios": ("ipa",)}
PLAY_TRACKS = ("internal", "alpha", "beta", "production")
APP_STORE_TARGETS = ("testflight", "review")

BUILD_ACTIVE_STATUSES = ("pending", "downloading")
BUILD_TERMINAL_STATUSES = ("available", "failed")
PUBLISH_ACTIVE_STATUSES = ("queued", "uploading", "processing")
PUBLISH_TERMINAL_STATUSES = ("published", "failed")

_MAX_DOWNLOAD_RETRIES = 4
# Concurrent artifact downloads / store uploads across the whole process.
_MAX_WORKERS = 2
# An "uploading"/"downloading" row untouched this long was orphaned by a restart.
_STALE_WORKER_MINUTES = 30

_worker_semaphore = threading.BoundedSemaphore(_MAX_WORKERS)


class MobileAppError(Exception):
    """A user-facing failure. ``status`` is the HTTP code to return."""

    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


def _iso(dt: Optional[datetime]) -> Optional[str]:
    # Column values are naive UTC — mark them as such, or browsers parse the
    # bare ISO string as local time and every relative timestamp drifts.
    if not dt:
        return None
    return dt.isoformat() if dt.tzinfo else f"{dt.isoformat()}Z"


def _aware(dt: Optional[datetime]) -> Optional[datetime]:
    if dt is None:
        return None
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt


# ---------------------------------------------------------------------------
# Binary store — one directory per app / build under MOBILE_ARTIFACT_DIR
# ---------------------------------------------------------------------------

def artifact_root() -> str:
    configured = os.getenv("MOBILE_ARTIFACT_DIR", "").strip()
    if configured:
        return os.path.abspath(configured)
    base = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    return os.path.join(base, "data", "mobile_artifacts")


def _safe_filename(name: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "_", (name or "").rsplit("/", 1)[-1]).strip("._")
    return cleaned or "artifact.bin"


def binary_path(build: MobileAppBuild) -> Optional[str]:
    """Absolute path of a build's stored binary, or None when not downloaded."""
    if not build.storage_path:
        return None
    return os.path.join(artifact_root(), build.storage_path)


def _store_dir_for(build: MobileAppBuild) -> str:
    rel = os.path.join(str(build.app_id), str(build.id))
    absolute = os.path.join(artifact_root(), rel)
    os.makedirs(absolute, exist_ok=True)
    return rel


# ---------------------------------------------------------------------------
# Jenkins config for a specific app job
# ---------------------------------------------------------------------------

def _jenkins_cfg(job_path: str = "") -> JenkinsConfig:
    from .deploy_automation_service import get_or_create_jenkins, _to_client_config

    row = get_or_create_jenkins()
    cfg = _to_client_config(row)
    if job_path:
        cfg = replace(cfg, router_job_path=job_path)
    return cfg


# ---------------------------------------------------------------------------
# Artifact config normalization
# ---------------------------------------------------------------------------

def _normalize_artifact_config(value: Any) -> Dict[str, Dict[str, str]]:
    """Coerce ``{"android": {"source", "pattern", "path"}, "ios": {...}}``.

    ``source`` ∈ archive | workspace. Archive entries match ``pattern`` (glob,
    default per-platform extension) against the build's archived artifacts;
    workspace entries fetch the explicit ``path`` under the build URL (e.g.
    ``execution/node/71/ws/pos.apk``).
    """
    out: Dict[str, Dict[str, str]] = {}
    if not isinstance(value, dict):
        return out
    for platform in PLATFORMS:
        entry = value.get(platform)
        if not isinstance(entry, dict):
            continue
        source = str(entry.get("source") or "archive").strip().lower()
        if source not in ("archive", "workspace"):
            source = "archive"
        pattern = str(entry.get("pattern") or "").strip()
        path = str(entry.get("path") or "").strip().lstrip("/")
        if source == "workspace" and not path:
            raise MobileAppError(
                f"The {platform} artifact is set to 'workspace' but has no file path "
                "(e.g. execution/node/71/ws/pos.apk)."
            )
        out[platform] = {"source": source, "pattern": pattern, "path": path}
    return out


_RESIGN_STR_FIELDS = (
    "executor",
    # Jenkins job that signs the binary, folder-style like jenkins_job_path.
    "jobPath",
    # Glob matched against the build's ARCHIVED artifacts to find the signed file.
    "resultPattern",
    # Jenkins FILE parameter the unsigned binary is uploaded as.
    "fileParam",
    # Optional text parameter carrying "aab" / "apk" / "ipa".
    "artifactTypeParam",
)
_RESIGN_EXECUTORS = ("jenkins",)


def _normalize_resign_config(value: Any) -> Dict[str, Dict[str, Any]]:
    """Coerce the per-platform signing-job setup.

    Everything stored here names a job, a parameter, or a URL — never key
    material. The signing key stays on whichever Jenkins agent holds it (the
    Android keystore, the macOS keychain), so nothing secret can land in this
    column even by accident.
    """
    out: Dict[str, Dict[str, Any]] = {}
    if not isinstance(value, dict):
        return out
    for platform in PLATFORMS:
        entry = value.get(platform)
        if not isinstance(entry, dict):
            continue
        cleaned: Dict[str, Any] = {
            field: str(entry.get(field) or "").strip()
            for field in _RESIGN_STR_FIELDS
            if str(entry.get(field) or "").strip()
        }
        # Static build parameters the operator wants passed through (e.g. an
        # iOS provisioning-profile name). Values are stringified; the source
        # URL and token are applied last at launch so these cannot shadow them.
        extra = entry.get("extraParams")
        if isinstance(extra, dict):
            params = {
                str(k).strip(): str(v)
                for k, v in extra.items()
                if str(k).strip() and str(v).strip()
            }
            if params:
                cleaned["extraParams"] = params
        if not cleaned:
            continue
        executor = cleaned.get("executor") or "jenkins"
        if executor not in _RESIGN_EXECUTORS:
            raise MobileAppError(
                f"Unsupported signing executor '{executor}'. "
                f"Supported: {', '.join(_RESIGN_EXECUTORS)}."
            )
        cleaned["executor"] = executor
        if not cleaned.get("jobPath"):
            raise MobileAppError(
                f"The {platform} signing setup needs a Jenkins job path."
            )
        out[platform] = cleaned
    return out


def _platform_config(app: MobileApplication, platform: str) -> Optional[Dict[str, str]]:
    cfg = app.artifact_config or {}
    entry = cfg.get(platform)
    return entry if isinstance(entry, dict) else None


def configured_platforms(app: MobileApplication) -> List[str]:
    return [p for p in PLATFORMS if _platform_config(app, p)]


def _default_pattern(platform: str) -> str:
    return "*.apk" if platform == "android" else "*.ipa"


def _artifact_type_for(file_name: str, platform: str) -> str:
    ext = (file_name or "").rsplit(".", 1)[-1].lower()
    if ext in ("apk", "aab", "ipa"):
        return ext
    return ARTIFACT_TYPES[platform][0]


# ---------------------------------------------------------------------------
# Serialization
# ---------------------------------------------------------------------------

def serialize_app(app: MobileApplication, *, with_stats: bool = False) -> Dict[str, Any]:
    data = {
        "id": app.id,
        "name": app.name or "",
        "description": app.description or "",
        "enabled": bool(app.enabled),
        "zohoEnvironment": app.zoho_environment or "",
        "jenkinsJobPath": app.jenkins_job_path or "",
        "artifactConfig": app.artifact_config or {},
        "resignConfig": app.resign_config or {},
        "androidPackageName": app.android_package_name or "",
        "playServiceAccountConfigured": bool(app.play_service_account_json_encrypted),
        "iosBundleId": app.ios_bundle_id or "",
        "ascIssuerId": app.asc_issuer_id or "",
        "ascKeyId": app.asc_key_id or "",
        "ascPrivateKeyConfigured": bool(app.asc_private_key_encrypted),
        "ascAppId": app.asc_app_id or "",
        "platforms": configured_platforms(app),
        "lastTestAt": _iso(app.last_test_at),
        "lastTestStatus": app.last_test_status,
        "lastTestMessage": app.last_test_message,
        "createdAt": _iso(app.created_at),
        "updatedAt": _iso(app.updated_at),
    }
    if with_stats:
        latest = (
            MobileAppBuild.query.filter_by(app_id=app.id)
            .order_by(MobileAppBuild.id.desc())
            .first()
        )
        data["buildCount"] = MobileAppBuild.query.filter_by(app_id=app.id).count()
        data["publishCount"] = MobileAppPublish.query.filter_by(app_id=app.id).count()
        data["latestBuild"] = serialize_build(latest) if latest else None
    return data


def serialize_build(build: MobileAppBuild) -> Dict[str, Any]:
    return {
        "id": build.id,
        "appId": build.app_id,
        "platform": build.platform,
        "artifactType": build.artifact_type,
        "version": build.version or "",
        "fileName": build.file_name or "",
        "fileSize": build.file_size,
        "sha256": build.sha256 or "",
        "signatureState": build.signature_state or "unknown",
        "jenkinsBuildNumber": build.jenkins_build_number,
        "jenkinsBuildUrl": build.jenkins_build_url or "",
        "ticketNumber": build.ticket_number or "",
        "source": build.source or "ticket",
        "parentBuildId": build.parent_build_id,
        "status": build.status,
        "error": build.error,
        "createdAt": _iso(build.created_at),
        "downloadedAt": _iso(build.downloaded_at),
    }


def serialize_publish(pub: MobileAppPublish) -> Dict[str, Any]:
    return {
        "id": pub.id,
        "appId": pub.app_id,
        "buildId": pub.build_id,
        "store": pub.store,
        "target": pub.target,
        "status": pub.status,
        "steps": pub.steps or [],
        "storeRef": pub.store_ref or {},
        "error": pub.error,
        "triggeredBy": pub.triggered_by,
        "createdAt": _iso(pub.created_at),
        "finishedAt": _iso(pub.finished_at),
    }


# ---------------------------------------------------------------------------
# CRUD
# ---------------------------------------------------------------------------

def list_apps() -> List[Dict[str, Any]]:
    apps = MobileApplication.query.order_by(MobileApplication.name.asc()).all()
    return [serialize_app(a, with_stats=True) for a in apps]


def get_app(app_id: int) -> MobileApplication:
    app = MobileApplication.query.get(int(app_id))
    if app is None:
        raise MobileAppError("Mobile application not found.", 404)
    return app


def _apply_payload(app: MobileApplication, payload: Dict[str, Any]) -> None:
    if "name" in payload:
        name = str(payload.get("name") or "").strip()
        if not name:
            raise MobileAppError("A name is required.")
        app.name = name
    for key, attr in (
        ("description", "description"),
        ("zohoEnvironment", "zoho_environment"),
        ("jenkinsJobPath", "jenkins_job_path"),
        ("androidPackageName", "android_package_name"),
        ("iosBundleId", "ios_bundle_id"),
        ("ascIssuerId", "asc_issuer_id"),
        ("ascKeyId", "asc_key_id"),
        ("ascAppId", "asc_app_id"),
    ):
        if key in payload and payload.get(key) is not None:
            setattr(app, attr, str(payload.get(key)).strip())
    if "enabled" in payload:
        app.enabled = bool(payload.get("enabled"))
    if "artifactConfig" in payload:
        app.artifact_config = _normalize_artifact_config(payload.get("artifactConfig"))
    if "resignConfig" in payload:
        app.resign_config = _normalize_resign_config(payload.get("resignConfig"))

    # Write-only secrets: set when a non-blank value arrives, clear on request.
    if str(payload.get("playServiceAccountJson") or "").strip():
        app.play_service_account_json_encrypted = encrypt_secret(
            str(payload["playServiceAccountJson"]).strip()
        )
    elif payload.get("clearPlayServiceAccount"):
        app.play_service_account_json_encrypted = None
    if str(payload.get("ascPrivateKey") or "").strip():
        app.asc_private_key_encrypted = encrypt_secret(str(payload["ascPrivateKey"]).strip())
    elif payload.get("clearAscPrivateKey"):
        app.asc_private_key_encrypted = None

    # A duplicate environment mapping would make ticket routing ambiguous.
    env = (app.zoho_environment or "").strip().casefold()
    if env:
        clash = MobileApplication.query.filter(MobileApplication.id != (app.id or 0)).all()
        for other in clash:
            if (other.zoho_environment or "").strip().casefold() == env:
                raise MobileAppError(
                    f"'{app.zoho_environment}' is already mapped to '{other.name}'."
                )


def create_app(payload: Dict[str, Any]) -> Dict[str, Any]:
    app = MobileApplication()
    payload = dict(payload or {})
    payload.setdefault("name", "")
    _apply_payload(app, payload)
    db.session.add(app)
    db.session.commit()
    return serialize_app(app)


def update_app(app_id: int, payload: Dict[str, Any]) -> Dict[str, Any]:
    app = get_app(app_id)
    _apply_payload(app, payload or {})
    db.session.add(app)
    db.session.commit()
    return serialize_app(app)


def delete_app(app_id: int) -> None:
    app = get_app(app_id)
    db.session.delete(app)
    db.session.commit()
    try:
        shutil.rmtree(os.path.join(artifact_root(), str(app_id)), ignore_errors=True)
    except Exception:
        logger.warning("Could not remove binary store dir for app %s", app_id, exc_info=True)


def delete_build(build_id: int) -> None:
    build = MobileAppBuild.query.get(int(build_id))
    if build is None:
        raise MobileAppError("Build not found.", 404)
    if build.status == "downloading":
        raise MobileAppError("This build is still downloading — try again shortly.", 409)
    app_id = build.app_id
    db.session.delete(build)
    db.session.commit()
    try:
        shutil.rmtree(
            os.path.join(artifact_root(), str(app_id), str(build_id)), ignore_errors=True
        )
    except Exception:
        logger.warning("Could not remove binary for build %s", build_id, exc_info=True)


def list_builds(app_id: int, limit: int = 50) -> List[Dict[str, Any]]:
    rows = (
        MobileAppBuild.query.filter_by(app_id=int(app_id))
        .order_by(MobileAppBuild.id.desc())
        .limit(max(1, min(int(limit or 50), 200)))
        .all()
    )
    return [serialize_build(b) for b in rows]


def list_publishes(app_id: Optional[int] = None, build_id: Optional[int] = None) -> List[Dict[str, Any]]:
    q = MobileAppPublish.query
    if app_id:
        q = q.filter_by(app_id=int(app_id))
    if build_id:
        q = q.filter_by(build_id=int(build_id))
    rows = q.order_by(MobileAppPublish.id.desc()).limit(100).all()
    return [serialize_publish(p) for p in rows]


# ---------------------------------------------------------------------------
# Jenkins source test + manual fetch
# ---------------------------------------------------------------------------

def test_jenkins_source(app_id: int) -> Dict[str, Any]:
    """Resolve the app's job + artifacts against its last successful build."""
    app = get_app(app_id)
    status, message = "ok", ""
    details: List[str] = []
    try:
        if not (app.jenkins_job_path or "").strip():
            raise MobileAppError("No Jenkins job path is configured for this app.")
        cfg = _jenkins_cfg(app.jenkins_job_path)
        last = jenkins_client.last_successful_build(cfg, app.jenkins_job_path)
        if not last:
            raise MobileAppError("The job exists but has no successful build yet.")
        details.append(f"Last successful build: #{last['number']}.")
        platforms = configured_platforms(app)
        if not platforms:
            raise MobileAppError("No artifact is configured for any platform.")
        for platform in platforms:
            entry = _platform_config(app, platform) or {}
            resolved = _resolve_artifact(cfg, last["url"], entry, platform)
            details.append(f"{platform}: found {resolved['fileName']}.")
    except (MobileAppError, JenkinsError) as exc:
        status, message = "error", str(exc)
    if status == "ok":
        message = " ".join(details)
    app.last_test_at = datetime.now(timezone.utc)
    app.last_test_status = status
    app.last_test_message = message
    db.session.add(app)
    db.session.commit()
    return {"status": status, "message": message}


def _resolve_artifact(
    cfg: JenkinsConfig, build_url: str, entry: Dict[str, str], platform: str
) -> Dict[str, str]:
    """Locate the platform's binary for one build → ``{fileName, url}``."""
    source = entry.get("source") or "archive"
    if source == "workspace":
        path = entry.get("path") or ""
        return {
            "fileName": _safe_filename(path),
            "url": jenkins_client.workspace_file_url(cfg, build_url, path),
        }
    pattern = entry.get("pattern") or _default_pattern(platform)
    artifacts = jenkins_client.list_artifacts(cfg, build_url)
    for item in artifacts:
        name = item.get("fileName") or ""
        rel = item.get("relativePath") or ""
        if fnmatch.fnmatch(name.lower(), pattern.lower()) or fnmatch.fnmatch(
            rel.lower(), pattern.lower()
        ):
            return {
                "fileName": _safe_filename(name),
                "url": jenkins_client.artifact_url(cfg, build_url, rel),
            }
    listed = ", ".join(i.get("relativePath") or "" for i in artifacts[:10]) or "none"
    raise MobileAppError(
        f"No archived artifact matches '{pattern}' on this build (archived: {listed}). "
        "Either archive the file in the Jenkinsfile or switch the artifact source to "
        "'workspace' with the file's path."
    )


def _existing_build(app_id: int, platform: str, build_number: Optional[int]) -> Optional[MobileAppBuild]:
    if not build_number:
        return None
    return (
        MobileAppBuild.query.filter_by(
            app_id=app_id, platform=platform, jenkins_build_number=build_number
        )
        .filter(MobileAppBuild.status != "failed")
        .first()
    )


def fetch_latest(app_id: int, user=None) -> List[Dict[str, Any]]:
    """Manually ingest the last successful Jenkins build (no ticket needed)."""
    app = get_app(app_id)
    if not (app.jenkins_job_path or "").strip():
        raise MobileAppError("No Jenkins job path is configured for this app.")
    platforms = configured_platforms(app)
    if not platforms:
        raise MobileAppError("No artifact is configured for any platform.")
    cfg = _jenkins_cfg(app.jenkins_job_path)
    try:
        last = jenkins_client.last_successful_build(cfg, app.jenkins_job_path)
    except JenkinsError as exc:
        raise MobileAppError(str(exc), 502)
    if not last:
        raise MobileAppError("The job has no successful build yet.", 404)

    created: List[MobileAppBuild] = []
    for platform in platforms:
        if _existing_build(app.id, platform, last["number"]):
            continue
        build = MobileAppBuild(
            app_id=app.id,
            platform=platform,
            artifact_type=ARTIFACT_TYPES[platform][0],
            version=f"build #{last['number']}",
            jenkins_build_number=last["number"],
            jenkins_build_url=last["url"],
            source="manual",
            status="pending",
        )
        db.session.add(build)
        created.append(build)
    if not created:
        raise MobileAppError(
            f"Build #{last['number']} is already ingested for every configured platform.", 409
        )
    db.session.commit()
    log_audit(
        "mobile_app_fetch_requested",
        actor=user,
        target_type="mobile_app",
        target_id=str(app.id),
        details={"app": app.name, "jenkinsBuild": last["number"], "platforms": platforms},
    )
    kick_workers()
    return [serialize_build(b) for b in created]


# ---------------------------------------------------------------------------
# Direct binary upload — operator supplies the final artifact, no Jenkins
# ---------------------------------------------------------------------------

_HASH_CHUNK = 1024 * 1024


def _hash_and_size(path: str) -> tuple:
    """(sha256 hex, byte size) of a file, read in chunks so large binaries
    never load into memory whole."""
    import hashlib

    digest = hashlib.sha256()
    size = 0
    with open(path, "rb") as fh:
        while True:
            chunk = fh.read(_HASH_CHUNK)
            if not chunk:
                break
            size += len(chunk)
            digest.update(chunk)
    return digest.hexdigest(), size


def create_upload_build(app_id, platform, file, version: str = "", user=None) -> Dict[str, Any]:
    """Register a binary uploaded straight through the UI as an available build.

    Sidesteps Jenkins entirely: the operator hands KubeSight the final artifact
    (a re-signed IPA, a release AAB/APK), which lands as a ready-to-publish
    MobileAppBuild with no download step. The existing publish flow accepts it
    unchanged — it only needs ``status="available"`` and a file on disk.
    """
    app = get_app(app_id)
    platform = str(platform or "").strip().lower()
    if platform not in PLATFORMS:
        raise MobileAppError("Platform must be 'android' or 'ios'.")
    if file is None or not getattr(file, "filename", ""):
        raise MobileAppError("No file was uploaded.")

    file_name = _safe_filename(file.filename)
    ext = file_name.rsplit(".", 1)[-1].lower() if "." in file_name else ""
    allowed = ARTIFACT_TYPES[platform]
    if ext not in allowed:
        pretty = " or ".join(f".{e}" for e in allowed)
        raise MobileAppError(
            f"A {platform} build must be a {pretty} file"
            + (f" (got '.{ext}')." if ext else ".")
        )

    # Commit the row first so its id anchors the store directory AND the DB
    # write lock is released before the (potentially multi-hundred-MB) save.
    # "downloading" keeps the scheduler tick from touching it — it only
    # dispatches "pending" rows and only fails stale (>30 min) in-flight ones.
    build = MobileAppBuild(
        app_id=app.id,
        platform=platform,
        artifact_type=ext,
        version=(version or "").strip() or None,
        source="upload",
        status="downloading",
    )
    db.session.add(build)
    db.session.commit()

    rel_dir = _store_dir_for(build)
    dest = os.path.join(artifact_root(), rel_dir, file_name)
    try:
        file.save(dest)
        sha256, size = _hash_and_size(dest)
        if size <= 0:
            raise MobileAppError("The uploaded file is empty.")
    except Exception as exc:
        try:
            if os.path.isfile(dest):
                os.remove(dest)
        except OSError:
            pass
        db.session.delete(build)
        db.session.commit()
        if isinstance(exc, MobileAppError):
            raise
        logger.exception("Storing uploaded mobile binary failed: app_id=%s", app_id)
        raise MobileAppError(f"Could not store the uploaded file ({exc}).", 500)

    # An IPA carries its own version in Info.plist — read it so the release
    # label matches the binary (publish reads it again for the store attrs).
    if not build.version and ext == "ipa":
        try:
            from .app_store_client import ipa_versions

            short, bundle = ipa_versions(dest)
            build.version = f"{short} ({bundle})"
        except Exception:
            pass  # unreadable version is non-fatal
    if not build.version:
        build.version = file_name

    build.file_name = file_name
    build.file_size = size
    build.sha256 = sha256
    build.storage_path = os.path.join(rel_dir, file_name)
    # Shielded binaries come back stripped — flag it here so the drawer offers
    # a re-sign instead of a publish that the store would reject.
    build.signature_state = binary_signature.detect_safe(dest, ext)
    build.status = "available"
    build.downloaded_at = datetime.now(timezone.utc)
    db.session.add(build)
    log_audit(
        "mobile_app_build_uploaded",
        actor=user,
        target_type="mobile_app_build",
        target_id=str(build.id),
        details={"app": app.name, "platform": platform, "file": file_name, "size": size},
        commit=False,
    )
    db.session.commit()
    return serialize_build(build)


# ---------------------------------------------------------------------------
# Automation hook — called when a custom-environment Jenkins run succeeds
# ---------------------------------------------------------------------------

def app_for_environment(environment: str) -> Optional[MobileApplication]:
    env = str(environment or "").strip().casefold()
    if not env:
        return None
    for app in MobileApplication.query.filter_by(enabled=True).all():
        if (app.zoho_environment or "").strip().casefold() == env:
            return app
    return None


def on_custom_build_success(run: DeployAutomationRun) -> None:
    """Ingest a ticket-driven custom-environment build. Adds rows to the caller's
    session WITHOUT committing (the automation tick owns the transaction). Never
    raises — a mobile ingest problem must not affect the deploy run."""
    try:
        app = app_for_environment(run.namespace)
        if app is None or not run.jenkins_build_url:
            return
        version = run.ticket_tag or run.image_tag or None
        for platform in configured_platforms(app):
            if _existing_build(app.id, platform, run.jenkins_build_number):
                continue
            db.session.add(
                MobileAppBuild(
                    app_id=app.id,
                    platform=platform,
                    artifact_type=ARTIFACT_TYPES[platform][0],
                    version=version,
                    jenkins_build_number=run.jenkins_build_number,
                    jenkins_build_url=run.jenkins_build_url,
                    ticket_record_id=run.ticket_record_id,
                    ticket_number=run.ticket_number,
                    run_id=run.id,
                    source="ticket",
                    status="pending",
                )
            )
        log_audit(
            "mobile_app_build_detected",
            actor=None,
            target_type="mobile_app",
            target_id=str(app.id),
            details={
                "app": app.name,
                "ticket": run.ticket_number,
                "jenkinsBuild": run.jenkins_build_number,
            },
            commit=False,
        )
    except Exception:
        logger.exception("Mobile build ingest failed for run %s", run.id)


# ---------------------------------------------------------------------------
# Download state machine (pending → downloading → available | failed)
# ---------------------------------------------------------------------------

def _try_dispatch(work) -> bool:
    """Run ``work`` inside its own app context on a daemon thread. Returns False
    when every worker slot is busy (the caller reverts the row so the next tick
    retries). Inline under TESTING so tests stay deterministic."""
    from flask import current_app

    app_obj = current_app._get_current_object()
    if app_obj.config.get("TESTING"):
        work(app_obj)
        return True
    if not _worker_semaphore.acquire(blocking=False):
        return False

    def _runner():
        try:
            with app_obj.app_context():
                work(app_obj)
        except Exception:
            logger.exception("Mobile worker thread failed")
        finally:
            _worker_semaphore.release()

    threading.Thread(target=_runner, name="mobile-app-worker", daemon=True).start()
    return True


def advance_mobile_builds() -> None:
    """Scheduler tick: recover orphans, then dispatch pending downloads."""
    stale_cutoff = datetime.now(timezone.utc) - timedelta(minutes=_STALE_WORKER_MINUTES)
    orphans = MobileAppBuild.query.filter_by(status="downloading").all()
    for build in orphans:
        if (_aware(build.updated_at) or stale_cutoff) < stale_cutoff:
            build.status = "failed"
            build.error = "The download was interrupted (backend restart). Fetch again to retry."
            db.session.add(build)
    if orphans:
        db.session.commit()

    pending = (
        MobileAppBuild.query.filter_by(status="pending")
        .order_by(MobileAppBuild.id.asc())
        .limit(_MAX_WORKERS)
        .all()
    )
    for build in pending:
        # Commit the transition BEFORE dispatching so the worker thread reads
        # committed state; revert if no worker slot was free.
        build.status = "downloading"
        db.session.add(build)
        db.session.commit()
        build_id = build.id
        if not _try_dispatch(lambda _app, bid=build_id: _download_build(bid)):
            build.status = "pending"
            db.session.add(build)
            db.session.commit()
            break


def _download_build(build_id: int) -> None:
    build = MobileAppBuild.query.get(build_id)
    if build is None or build.status != "downloading":
        return
    app = MobileApplication.query.get(build.app_id)
    if app is None:
        build.status = "failed"
        build.error = "The application registration no longer exists."
        db.session.add(build)
        db.session.commit()
        return
    try:
        entry = _platform_config(app, build.platform) or {
            "source": "archive",
            "pattern": _default_pattern(build.platform),
            "path": "",
        }
        cfg = _jenkins_cfg(app.jenkins_job_path)
        resolved = _resolve_artifact(cfg, build.jenkins_build_url or "", entry, build.platform)
        rel_dir = _store_dir_for(build)
        file_name = resolved["fileName"]
        dest = os.path.join(artifact_root(), rel_dir, file_name)
        result = jenkins_client.download_file(cfg, resolved["url"], dest)
        build.file_name = file_name
        build.file_size = result["size"]
        build.sha256 = result["sha256"]
        build.storage_path = os.path.join(rel_dir, file_name)
        build.artifact_type = _artifact_type_for(file_name, build.platform)
        build.signature_state = binary_signature.detect_safe(dest, build.artifact_type)
        build.status = "available"
        build.error = None
        build.downloaded_at = datetime.now(timezone.utc)
        db.session.add(build)
        log_audit(
            "mobile_app_build_downloaded",
            actor=None,
            target_type="mobile_app_build",
            target_id=str(build.id),
            details={
                "app": app.name,
                "file": file_name,
                "size": result["size"],
                "jenkinsBuild": build.jenkins_build_number,
            },
            commit=False,
        )
        db.session.commit()
        _notify_build_ready(app, build)
    except (MobileAppError, JenkinsError) as exc:
        _fail_or_retry_download(build, str(exc))
    except Exception as exc:  # disk errors etc.
        logger.exception("Mobile build download crashed: build_id=%s", build_id)
        _fail_or_retry_download(build, f"Unexpected error: {exc}")


def _fail_or_retry_download(build: MobileAppBuild, message: str) -> None:
    build.retry_count = (build.retry_count or 0) + 1
    if build.retry_count > _MAX_DOWNLOAD_RETRIES:
        build.status = "failed"
        build.error = message
        app = MobileApplication.query.get(build.app_id)
        _notify_admins(
            f"[KubeSight] Mobile build download failed — {app.name if app else build.app_id}",
            f"Downloading the {build.platform} artifact for build "
            f"#{build.jenkins_build_number} failed after {build.retry_count} attempts.\n\n"
            f"Error: {message}\n"
            f"Jenkins build: {build.jenkins_build_url or 'n/a'}\n"
            f"Ticket: {build.ticket_number or 'manual fetch'}",
        )
    else:
        build.status = "pending"  # picked up again next tick
        build.error = f"retrying ({build.retry_count}/{_MAX_DOWNLOAD_RETRIES}): {message}"
    db.session.add(build)
    db.session.commit()


def _notify_build_ready(app: MobileApplication, build: MobileAppBuild) -> None:
    """Comment on the originating ticket once the binary is in KubeSight.

    Routed through the ticketing registry — the ticket may have come from Zoho
    Desk or Jira, and only its own row knows which.
    """
    if not build.ticket_record_id:
        return
    ticket = ZohoInboundTicket.query.get(build.ticket_record_id)
    if not (ticket and ticket.ticket_id):
        return
    from . import ticketing

    size_mb = round((build.file_size or 0) / (1024 * 1024), 1)
    ticketing.post_comment(
        ticket.provider or "zoho",
        ticket.ticket_id,
        f"The {build.platform} build ({build.file_name}, {size_mb} MB, version "
        f"{build.version or 'n/a'}) is now available in KubeSight → Mobile Applications "
        f"under '{app.name}'. It can be downloaded or published to the store from there.",
    )


def _notify_admins(subject: str, body: str) -> None:
    try:
        from ..email_delivery import send_email, smtp_is_configured
        from .deploy_automation_service import _admin_emails

        if not smtp_is_configured():
            return
        for address in _admin_emails():
            try:
                send_email(address, subject, body)
            except Exception:
                logger.warning("Mobile apps admin email to %s failed", address, exc_info=True)
    except Exception:
        logger.exception("Mobile apps admin notification failed")


# ---------------------------------------------------------------------------
# Publish state machine (queued → uploading → processing → published | failed)
# ---------------------------------------------------------------------------

_PUBLISH_STEP_KEYS = ("credentials", "upload", "release", "confirm")


def _initial_publish_steps() -> List[Dict[str, Any]]:
    return [{"key": k, "status": "wait", "detail": "", "at": None} for k in _PUBLISH_STEP_KEYS]


def _set_pub_step(pub: MobileAppPublish, key: str, status: str, detail: str = "") -> None:
    steps = [dict(s) for s in (pub.steps or _initial_publish_steps())]
    by_key = {s.get("key"): s for s in steps}
    step = by_key.get(key)
    if step is None:
        step = {"key": key, "status": "wait", "detail": "", "at": None}
        steps.append(step)
    step["status"] = status
    step["detail"] = detail
    step["at"] = datetime.now(timezone.utc).isoformat()
    pub.steps = steps


def start_publish(build_id: int, store: str, target: str, user=None) -> Dict[str, Any]:
    build = MobileAppBuild.query.get(int(build_id))
    if build is None:
        raise MobileAppError("Build not found.", 404)
    if build.status != "available":
        raise MobileAppError("Only downloaded builds can be published.", 409)
    # A shielded binary has had its signature stripped: no store will accept it
    # and no device would install it. Re-sign first. "unknown" (unreadable
    # probe) is deliberately allowed through rather than stranding a build.
    if build.signature_state == binary_signature.UNSIGNED:
        raise MobileAppError(
            "This build is unsigned — its code signature was stripped "
            "(shielding does this). Re-sign it before publishing.",
            409,
        )
    app = MobileApplication.query.get(build.app_id)
    if app is None:
        raise MobileAppError("The application registration no longer exists.", 404)

    store = str(store or "").strip()
    target = str(target or "").strip().lower()
    if store == "google_play":
        if build.platform != "android":
            raise MobileAppError("Google Play accepts Android builds (APK/AAB) only.")
        if target not in PLAY_TRACKS:
            raise MobileAppError(f"Track must be one of: {', '.join(PLAY_TRACKS)}.")
        if not app.android_package_name:
            raise MobileAppError("Set the Android package name on the app first.")
        if not app.play_service_account_json_encrypted:
            raise MobileAppError("Upload the Google Play service-account JSON key first.")
    elif store == "app_store":
        if build.platform != "ios":
            raise MobileAppError("App Store Connect accepts iOS builds (IPA) only.")
        if target not in APP_STORE_TARGETS:
            raise MobileAppError(f"Target must be one of: {', '.join(APP_STORE_TARGETS)}.")
        if not (app.asc_issuer_id and app.asc_key_id and app.asc_private_key_encrypted):
            raise MobileAppError("Set the App Store Connect API key (issuer, key id, .p8) first.")
        if not app.ios_bundle_id:
            raise MobileAppError("Set the iOS bundle id on the app first.")
    else:
        raise MobileAppError("Store must be google_play or app_store.")

    open_pub = (
        MobileAppPublish.query.filter_by(build_id=build.id, store=store)
        .filter(MobileAppPublish.status.in_(PUBLISH_ACTIVE_STATUSES))
        .first()
    )
    if open_pub:
        raise MobileAppError("A publish to this store is already in progress for this build.", 409)

    path = binary_path(build)
    if not (path and os.path.isfile(path)):
        raise MobileAppError("The stored binary is missing from disk — fetch the build again.", 409)

    pub = MobileAppPublish(
        app_id=app.id,
        build_id=build.id,
        store=store,
        target=target,
        status="queued",
        steps=_initial_publish_steps(),
        triggered_by=getattr(user, "username", None) or (user if isinstance(user, str) else None),
    )
    _set_pub_step(pub, "credentials", "wait", "queued")
    db.session.add(pub)
    db.session.commit()
    log_audit(
        "mobile_app_publish_started",
        actor=user,
        target_type="mobile_app_publish",
        target_id=str(pub.id),
        details={
            "app": app.name,
            "store": store,
            "target": target,
            "version": build.version,
            "file": build.file_name,
        },
    )
    kick_workers()
    return serialize_publish(pub)


def advance_mobile_publishes() -> None:
    """Scheduler tick: recover orphans, dispatch queued uploads, poll processing."""
    stale_cutoff = datetime.now(timezone.utc) - timedelta(minutes=_STALE_WORKER_MINUTES)
    for pub in MobileAppPublish.query.filter_by(status="uploading").all():
        if (_aware(pub.updated_at) or stale_cutoff) < stale_cutoff:
            _fail_publish(pub, "The upload was interrupted (backend restart).")
    db.session.commit()

    queued = (
        MobileAppPublish.query.filter_by(status="queued")
        .order_by(MobileAppPublish.id.asc())
        .limit(_MAX_WORKERS)
        .all()
    )
    for pub in queued:
        pub.status = "uploading"
        db.session.add(pub)
        db.session.commit()
        pub_id = pub.id
        if not _try_dispatch(lambda _app, pid=pub_id: _execute_publish(pid)):
            pub.status = "queued"
            db.session.add(pub)
            db.session.commit()
            break

    for pub in MobileAppPublish.query.filter_by(status="processing").all():
        try:
            _poll_processing(pub)
        except Exception:
            logger.exception("Publish processing poll failed: publish_id=%s", pub.id)


def _execute_publish(pub_id: int) -> None:
    pub = MobileAppPublish.query.get(pub_id)
    if pub is None or pub.status != "uploading":
        return
    build = MobileAppBuild.query.get(pub.build_id)
    app = MobileApplication.query.get(pub.app_id) if pub else None
    if build is None or app is None:
        _fail_publish(pub, "The build or application no longer exists.")
        db.session.commit()
        return
    try:
        if pub.store == "google_play":
            _publish_google_play(pub, app, build)
        else:
            _publish_app_store(pub, app, build)
    except Exception as exc:
        logger.exception("Publish failed: publish_id=%s", pub_id)
        _fail_publish(pub, str(exc))
        db.session.commit()


def _publish_google_play(pub: MobileAppPublish, app: MobileApplication, build: MobileAppBuild) -> None:
    from . import google_play_client

    _set_pub_step(pub, "credentials", "run", "authenticating with the service account")
    db.session.add(pub)
    db.session.commit()

    sa_json = decrypt_secret(app.play_service_account_json_encrypted or "")
    cfg = google_play_client.PlayConfig(
        package_name=app.android_package_name, service_account_json=sa_json
    )
    token = google_play_client.access_token(cfg)
    _set_pub_step(pub, "credentials", "done", "service account authenticated")

    edit_id = google_play_client.create_edit(cfg, token)
    pub.store_ref = {"editId": edit_id}
    _set_pub_step(pub, "upload", "run", f"uploading {build.file_name}")
    db.session.add(pub)
    db.session.commit()

    path = binary_path(build)
    version_code = google_play_client.upload_binary(
        cfg, token, edit_id, path, build.artifact_type
    )
    pub.store_ref = {**(pub.store_ref or {}), "versionCode": version_code}
    _set_pub_step(pub, "upload", "done", f"uploaded (versionCode {version_code})")
    _set_pub_step(pub, "release", "run", f"assigning to the {pub.target} track")
    db.session.add(pub)
    db.session.commit()

    google_play_client.assign_track(cfg, token, edit_id, pub.target, version_code)
    _set_pub_step(pub, "release", "done", f"release created on {pub.target}")
    _set_pub_step(pub, "confirm", "run", "committing the edit")
    db.session.add(pub)
    db.session.commit()

    google_play_client.commit_edit(cfg, token, edit_id)
    _set_pub_step(pub, "confirm", "done", "edit committed — the release is live on the track")
    _complete_publish(pub, app, build)
    db.session.commit()


def _publish_app_store(pub: MobileAppPublish, app: MobileApplication, build: MobileAppBuild) -> None:
    from . import app_store_client

    _set_pub_step(pub, "credentials", "run", "minting the App Store Connect token")
    db.session.add(pub)
    db.session.commit()

    cfg = app_store_client.AscConfig(
        issuer_id=app.asc_issuer_id,
        key_id=app.asc_key_id,
        private_key=decrypt_secret(app.asc_private_key_encrypted or ""),
        bundle_id=app.ios_bundle_id,
        app_id=app.asc_app_id or "",
    )
    asc_app_id = app_store_client.resolve_app_id(cfg)
    if asc_app_id and asc_app_id != app.asc_app_id:
        app.asc_app_id = asc_app_id
        db.session.add(app)
    _set_pub_step(pub, "credentials", "done", "token accepted by App Store Connect")
    _set_pub_step(pub, "upload", "run", f"uploading {build.file_name}")
    db.session.add(pub)
    db.session.commit()

    upload_ref = app_store_client.upload_build(cfg, binary_path(build), build.file_name)
    pub.store_ref = {**(pub.store_ref or {}), **upload_ref}
    _set_pub_step(pub, "upload", "done", "binary delivered — Apple is processing it")
    _set_pub_step(pub, "release", "run", "waiting for App Store Connect processing")
    pub.status = "processing"
    db.session.add(pub)
    db.session.commit()
    # _poll_processing takes over on the scheduler tick.


def _poll_processing(pub: MobileAppPublish) -> None:
    """App Store uploads finish asynchronously; poll until the build is usable."""
    from . import app_store_client

    build = MobileAppBuild.query.get(pub.build_id)
    app = MobileApplication.query.get(pub.app_id)
    if build is None or app is None:
        _fail_publish(pub, "The build or application no longer exists.")
        db.session.commit()
        return
    cfg = app_store_client.AscConfig(
        issuer_id=app.asc_issuer_id,
        key_id=app.asc_key_id,
        private_key=decrypt_secret(app.asc_private_key_encrypted or ""),
        bundle_id=app.ios_bundle_id,
        app_id=app.asc_app_id or "",
    )
    state = app_store_client.processing_state(cfg, pub.store_ref or {}, build.version or "")
    if state["state"] == "processing":
        _set_pub_step(pub, "release", "run", state.get("detail") or "still processing")
        db.session.add(pub)
        db.session.commit()
        return
    if state["state"] == "failed":
        _fail_publish(pub, state.get("detail") or "App Store Connect rejected the build.")
        db.session.commit()
        return

    pub.store_ref = {**(pub.store_ref or {}), "ascBuildId": state.get("buildId") or ""}
    _set_pub_step(pub, "release", "done", "build processed — available in TestFlight")
    if pub.target == "review":
        _set_pub_step(pub, "confirm", "run", "submitting for App Review")
        db.session.add(pub)
        db.session.commit()
        app_store_client.submit_for_review(cfg, state.get("buildId") or "")
        _set_pub_step(pub, "confirm", "done", "submitted for App Review")
    else:
        _set_pub_step(pub, "confirm", "skip", "TestFlight only — no review submission")
    _complete_publish(pub, app, build)
    db.session.commit()


def _store_label(pub: MobileAppPublish) -> str:
    if pub.store == "google_play":
        return f"Google Play ({pub.target} track)"
    return "App Store Connect (TestFlight)" if pub.target == "testflight" else (
        "App Store Connect (submitted for review)"
    )


def _complete_publish(pub: MobileAppPublish, app: MobileApplication, build: MobileAppBuild) -> None:
    pub.status = "published"
    pub.finished_at = datetime.now(timezone.utc)
    pub.error = None
    db.session.add(pub)
    log_audit(
        "mobile_app_published",
        actor=None,
        target_type="mobile_app_publish",
        target_id=str(pub.id),
        details={
            "app": app.name,
            "store": pub.store,
            "target": pub.target,
            "version": build.version,
            "file": build.file_name,
            "triggeredBy": pub.triggered_by,
        },
        commit=False,
    )
    _report_publish_outcome(pub, app, build, success=True)


def _fail_publish(pub: Optional[MobileAppPublish], message: str) -> None:
    if pub is None:
        return
    pub.status = "failed"
    pub.error = message
    pub.finished_at = datetime.now(timezone.utc)
    for key in _PUBLISH_STEP_KEYS:
        steps = {s.get("key"): s.get("status") for s in (pub.steps or [])}
        if steps.get(key) == "run":
            _set_pub_step(pub, key, "fail", message)
            break
    db.session.add(pub)
    log_audit(
        "mobile_app_publish_failed",
        actor=None,
        target_type="mobile_app_publish",
        target_id=str(pub.id),
        details={"store": pub.store, "target": pub.target, "error": message},
        commit=False,
    )
    build = MobileAppBuild.query.get(pub.build_id)
    app = MobileApplication.query.get(pub.app_id)
    if app and build:
        _report_publish_outcome(pub, app, build, success=False)


def _report_publish_outcome(
    pub: MobileAppPublish, app: MobileApplication, build: MobileAppBuild, *, success: bool
) -> None:
    """Ticket comment + admin email, mirroring the deploy automation loop."""
    label = _store_label(pub)
    version = build.version or build.file_name
    if success:
        subject = f"[KubeSight] {app.name} {version} published to {label}"
        body = (
            f"{app.name} ({version}, {build.file_name}) was published to {label} "
            f"by {pub.triggered_by or 'KubeSight'}."
        )
        comment = (
            f"KubeSight published the {build.platform} build {version} of '{app.name}' "
            f"to {label}."
        )
    else:
        subject = f"[KubeSight] Publishing {app.name} {version} to {label} FAILED"
        body = (
            f"Publishing {app.name} ({version}, {build.file_name}) to {label} failed.\n\n"
            f"Error: {pub.error}\n"
            f"Triggered by: {pub.triggered_by or 'unknown'}"
        )
        comment = (
            f"KubeSight could not publish the {build.platform} build {version} of "
            f"'{app.name}' to {label}: {pub.error}"
        )
    _notify_admins(subject, body)
    if build.ticket_record_id:
        ticket = ZohoInboundTicket.query.get(build.ticket_record_id)
        if ticket and ticket.ticket_id:
            from . import ticketing

            ticketing.post_comment(ticket.provider or "zoho", ticket.ticket_id, comment)


# ---------------------------------------------------------------------------
# Store credential tests
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Re-signing — hand a stripped binary to a Jenkins job that holds the key
# ---------------------------------------------------------------------------

_RESIGN_STEP_KEYS = ("prepare", "launch", "sign", "collect", "verify")
RESIGN_ACTIVE_STATUSES = ("queued", "running", "collecting")
RESIGN_TERMINAL_STATUSES = ("completed", "failed")

# A signing build that has not progressed in this long is wedged — the agent
# died, or the job is waiting on an executor that will never free up.
_RESIGN_TIMEOUT_MINUTES = 45


def _resign_config(app: MobileApplication, platform: str) -> Dict[str, Any]:
    cfg = (app.resign_config or {}).get(platform) if isinstance(app.resign_config, dict) else None
    return dict(cfg) if isinstance(cfg, dict) else {}


def serialize_resign(row: MobileAppResign) -> Dict[str, Any]:
    ref = row.job_ref or {}
    return {
        "id": row.id,
        "appId": row.app_id,
        "buildId": row.build_id,
        "resultBuildId": row.result_build_id,
        "platform": row.platform,
        "executor": row.executor,
        "status": row.status,
        "steps": row.steps or [],
        "jobPath": ref.get("jobPath") or "",
        "jenkinsBuildUrl": ref.get("buildUrl") or "",
        "jenkinsBuildNumber": ref.get("buildNumber"),
        "error": row.error,
        "triggeredBy": row.triggered_by,
        "createdAt": _iso(row.created_at),
        "finishedAt": _iso(row.finished_at),
    }


def _initial_resign_steps() -> List[Dict[str, Any]]:
    return [{"key": k, "status": "wait", "detail": "", "at": None} for k in _RESIGN_STEP_KEYS]


def _set_resign_step(row: MobileAppResign, key: str, status: str, detail: str = "") -> None:
    steps = [dict(s) for s in (row.steps or _initial_resign_steps())]
    by_key = {s.get("key"): s for s in steps}
    step = by_key.get(key)
    if step is None:
        step = {"key": key, "status": "wait", "detail": "", "at": None}
        steps.append(step)
    step["status"] = status
    step["detail"] = detail
    step["at"] = datetime.now(timezone.utc).isoformat()
    row.steps = steps


def list_resigns(
    app_id: Optional[int] = None, build_id: Optional[int] = None
) -> List[Dict[str, Any]]:
    query = MobileAppResign.query
    if app_id is not None:
        query = query.filter_by(app_id=int(app_id))
    if build_id is not None:
        query = query.filter_by(build_id=int(build_id))
    rows = query.order_by(MobileAppResign.created_at.desc()).limit(100).all()
    return [serialize_resign(r) for r in rows]


def start_resign(build_id: int, user=None) -> Dict[str, Any]:
    """Queue a Jenkins signing build for one stripped binary."""
    build = MobileAppBuild.query.get(int(build_id))
    if build is None:
        raise MobileAppError("Build not found.", 404)
    if build.status != "available":
        raise MobileAppError("Only downloaded builds can be re-signed.", 409)
    app = MobileApplication.query.get(build.app_id)
    if app is None:
        raise MobileAppError("The application registration no longer exists.", 404)

    cfg = _resign_config(app, build.platform)
    if not cfg:
        raise MobileAppError(
            f"No signing job configured for {build.platform} on this app. Set one first.", 409
        )
    if not cfg.get("jobPath"):
        raise MobileAppError("The signing setup has no Jenkins job path.", 409)
    if not binary_path(build) or not os.path.isfile(binary_path(build) or ""):
        raise MobileAppError("This build's binary is missing from the store.", 409)

    active = MobileAppResign.query.filter(
        MobileAppResign.build_id == build.id,
        MobileAppResign.status.in_(RESIGN_ACTIVE_STATUSES),
    ).first()
    if active is not None:
        raise MobileAppError("A signing build for this binary is already running.", 409)

    row = MobileAppResign(
        app_id=app.id,
        build_id=build.id,
        platform=build.platform,
        executor="jenkins",
        status="queued",
        steps=_initial_resign_steps(),
        triggered_by=getattr(user, "username", None),
    )
    db.session.add(row)
    log_audit(
        "mobile_app_resign_requested",
        actor=user,
        target_type="mobile_app_build",
        target_id=str(build.id),
        details={"app": app.name, "platform": build.platform, "job": cfg.get("jobPath")},
        commit=False,
    )
    db.session.commit()
    kick_workers()
    return serialize_resign(row)


def advance_mobile_resigns() -> None:
    """Tick: trigger queued signing builds, poll running ones, collect results."""
    for row in MobileAppResign.query.filter_by(status="queued").all():
        _launch_resign(row.id)

    for row in MobileAppResign.query.filter_by(status="running").all():
        _poll_resign(row.id)

    # A collect dispatched to a worker and orphaned by a restart.
    cutoff = datetime.now(timezone.utc) - timedelta(minutes=_STALE_WORKER_MINUTES)
    for row in MobileAppResign.query.filter_by(status="collecting").all():
        if (_aware(row.updated_at) or datetime.now(timezone.utc)) < cutoff:
            _set_resign_step(row, "collect", "fail", "collection stalled")
            _fail_resign(row, "Collecting the signed binary stalled and was abandoned.")


def _launch_resign(resign_id: int) -> None:
    from . import resign_executor

    row = MobileAppResign.query.get(int(resign_id))
    if row is None or row.status != "queued":
        return
    build = MobileAppBuild.query.get(row.build_id)
    app = MobileApplication.query.get(row.app_id)
    if build is None or app is None:
        _fail_resign(row, "The build or application registration no longer exists.")
        return

    cfg = _resign_config(app, row.platform)
    try:
        source = binary_path(build) or ""
        if not os.path.isfile(source):
            _fail_resign(row, "This build's binary is missing from the store.")
            return
        size_mb = os.path.getsize(source) / (1024 * 1024)
        _set_resign_step(row, "prepare", "done", f"{build.file_name} ({size_mb:.0f} MB)")

        spec = resign_executor.ResignJobSpec(
            resign_id=row.id,
            build_id=build.id,
            platform=row.platform,
            artifact_type=build.artifact_type or "",
            job_path=str(cfg.get("jobPath") or ""),
            binary_path=source,
            file_name=build.file_name or "",
            file_param=str(cfg.get("fileParam") or resign_executor.DEFAULT_FILE_PARAM),
            artifact_type_param=str(cfg.get("artifactTypeParam") or ""),
            extra_params=cfg.get("extraParams") if isinstance(cfg.get("extraParams"), dict) else {},
        )

        _set_resign_step(row, "launch", "run", f"uploading to {spec.job_path}")
        jcfg = _jenkins_cfg(spec.job_path)
        row.job_ref = resign_executor.launch(jcfg, spec)
        _set_resign_step(row, "launch", "done", "uploaded and queued in Jenkins")
        _set_resign_step(row, "sign", "run", "waiting for the signing build")
        row.status = "running"
        db.session.add(row)
        db.session.commit()
    except Exception as exc:
        logger.exception("Triggering resign build failed: resign_id=%s", resign_id)
        _set_resign_step(row, "launch", "fail", str(exc))
        _fail_resign(row, f"Could not trigger the signing job: {exc}")


def _poll_resign(resign_id: int) -> None:
    from . import resign_executor

    row = MobileAppResign.query.get(int(resign_id))
    if row is None or row.status != "running":
        return
    app = MobileApplication.query.get(row.app_id)
    if app is None:
        _fail_resign(row, "The application registration no longer exists.")
        return

    ref = dict(row.job_ref or {})
    try:
        jcfg = _jenkins_cfg(ref.get("jobPath") or "")
        state = resign_executor.poll(jcfg, ref)
    except Exception as exc:
        logger.warning("Polling resign build failed: resign_id=%s (%s)", resign_id, exc)
        return

    # Record the build URL as soon as Jenkins assigns one, so the drawer links
    # to the running build instead of showing nothing until it finishes.
    if state.get("buildUrl") and state.get("buildUrl") != ref.get("buildUrl"):
        ref["buildUrl"] = state["buildUrl"]
        ref["buildNumber"] = state.get("buildNumber")
        row.job_ref = ref
        db.session.add(row)
        db.session.commit()

    phase = state.get("phase")
    if phase == "failed":
        detail = state.get("detail") or "the signing build failed"
        _set_resign_step(row, "sign", "fail", detail)
        _fail_resign(row, detail)
        return

    if phase == "succeeded":
        _set_resign_step(row, "sign", "done", "signing build succeeded")
        _set_resign_step(row, "collect", "run", "downloading the signed binary")
        # "collecting" keeps the tick from re-dispatching while the (possibly
        # multi-hundred-MB) download runs on a worker thread.
        row.status = "collecting"
        db.session.add(row)
        db.session.commit()
        if not _try_dispatch(lambda _app, rid=row.id: _collect_resign(rid)):
            row.status = "running"
            _set_resign_step(row, "collect", "wait", "waiting for a free worker")
            db.session.add(row)
            db.session.commit()
        return

    age = datetime.now(timezone.utc) - (_aware(row.created_at) or datetime.now(timezone.utc))
    if age > timedelta(minutes=_RESIGN_TIMEOUT_MINUTES):
        _set_resign_step(row, "sign", "fail", "timed out")
        _fail_resign(row, f"The signing build did not finish within {_RESIGN_TIMEOUT_MINUTES} min.")


def _collect_resign(resign_id: int) -> None:
    """Download the signed artifact the job archived and register it as a build."""
    row = MobileAppResign.query.get(int(resign_id))
    if row is None or row.status != "collecting":
        return
    parent = MobileAppBuild.query.get(row.build_id)
    app = MobileApplication.query.get(row.app_id)
    if parent is None or app is None:
        _fail_resign(row, "The build or application registration no longer exists.")
        return

    from . import resign_executor

    cfg = _resign_config(app, row.platform)
    ref = row.job_ref or {}
    pattern = str(
        cfg.get("resultPattern") or resign_executor.DEFAULT_RESULT_PATTERN.get(row.platform) or "*"
    )

    child = None
    try:
        jcfg = _jenkins_cfg(ref.get("jobPath") or "")
        resolved = _resolve_artifact(
            jcfg,
            ref.get("buildUrl") or "",
            {"source": "archive", "pattern": pattern},
            row.platform,
        )

        child = MobileAppBuild(
            app_id=app.id,
            platform=row.platform,
            artifact_type=_artifact_type_for(resolved["fileName"], row.platform),
            version=parent.version,
            source="resign",
            parent_build_id=parent.id,
            ticket_record_id=parent.ticket_record_id,
            ticket_number=parent.ticket_number,
            jenkins_build_number=ref.get("buildNumber"),
            jenkins_build_url=ref.get("buildUrl") or None,
            status="downloading",
        )
        db.session.add(child)
        db.session.commit()

        rel_dir = _store_dir_for(child)
        file_name = resolved["fileName"]
        dest = os.path.join(artifact_root(), rel_dir, file_name)
        result = jenkins_client.download_file(jcfg, resolved["url"], dest)

        state = binary_signature.detect_safe(dest, child.artifact_type)
        _set_resign_step(row, "collect", "done", f"{file_name} ({result['size']} bytes)")
        _set_resign_step(row, "verify", "run", "checking the signature")
        if state == binary_signature.UNSIGNED:
            # The whole point of the build was to produce a signature. Never let
            # a dud through as a publishable build.
            child.status = "failed"
            child.error = "The signing job returned an unsigned binary."
            db.session.add(child)
            db.session.commit()
            _set_resign_step(row, "verify", "fail", "the archived binary is still unsigned")
            _fail_resign(row, "The signing job archived a binary that is still unsigned.")
            return

        child.file_name = file_name
        child.file_size = result["size"]
        child.sha256 = result["sha256"]
        child.storage_path = os.path.join(rel_dir, file_name)
        child.signature_state = state
        child.status = "available"
        child.downloaded_at = datetime.now(timezone.utc)
        db.session.add(child)

        _set_resign_step(row, "verify", "done", "signature present")
        row.result_build_id = child.id
        row.status = "completed"
        row.finished_at = datetime.now(timezone.utc)
        db.session.add(row)
        log_audit(
            "mobile_app_build_resigned",
            actor=None,
            target_type="mobile_app_build",
            target_id=str(child.id),
            details={
                "app": app.name,
                "parentBuild": parent.id,
                "file": file_name,
                "size": result["size"],
                "signatureState": state,
            },
            commit=False,
        )
        db.session.commit()
    except Exception as exc:
        logger.exception("Collecting signed binary failed: resign_id=%s", resign_id)
        if child is not None and child.id and child.status == "downloading":
            try:
                db.session.delete(child)
                db.session.commit()
            except Exception:
                db.session.rollback()
        _set_resign_step(row, "collect", "fail", str(exc))
        _fail_resign(row, f"Could not collect the signed binary: {exc}")


def _fail_resign(row: MobileAppResign, message: str) -> None:
    row.status = "failed"
    row.error = message
    row.finished_at = datetime.now(timezone.utc)
    db.session.add(row)
    db.session.commit()



def test_play_credentials(app_id: int) -> Dict[str, Any]:
    app = get_app(app_id)
    if not app.android_package_name:
        raise MobileAppError("Set the Android package name first.")
    if not app.play_service_account_json_encrypted:
        raise MobileAppError("Upload the service-account JSON key first.")
    from . import google_play_client

    cfg = google_play_client.PlayConfig(
        package_name=app.android_package_name,
        service_account_json=decrypt_secret(app.play_service_account_json_encrypted),
    )
    try:
        google_play_client.test_credentials(cfg)
        return {"status": "ok", "message": f"Authenticated; {app.android_package_name} is accessible."}
    except google_play_client.PlayError as exc:
        return {"status": "error", "message": str(exc)}


def test_app_store_credentials(app_id: int) -> Dict[str, Any]:
    app = get_app(app_id)
    if not (app.asc_issuer_id and app.asc_key_id and app.asc_private_key_encrypted):
        raise MobileAppError("Set the App Store Connect issuer id, key id, and .p8 key first.")
    from . import app_store_client

    cfg = app_store_client.AscConfig(
        issuer_id=app.asc_issuer_id,
        key_id=app.asc_key_id,
        private_key=decrypt_secret(app.asc_private_key_encrypted),
        bundle_id=app.ios_bundle_id,
        app_id=app.asc_app_id or "",
    )
    try:
        resolved = app_store_client.resolve_app_id(cfg)
        if resolved and resolved != app.asc_app_id:
            app.asc_app_id = resolved
            db.session.add(app)
            db.session.commit()
        detail = f"app id {resolved}" if resolved else "no app matched the bundle id yet"
        return {"status": "ok", "message": f"Token accepted; {detail}."}
    except app_store_client.AscError as exc:
        return {"status": "error", "message": str(exc)}


def kick_workers() -> None:
    """Dispatch pending downloads/uploads immediately instead of waiting for the
    next scheduler tick (used right after a fetch/publish request)."""
    try:
        advance_mobile_builds()
        advance_mobile_publishes()
        advance_mobile_resigns()
    except Exception:
        logger.exception("Mobile apps worker kick failed")
