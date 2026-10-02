"""The App store upload stage: one of the build's binaries goes to a store.

No runner executes this stage, and it does not talk to Google Play or App
Store Connect itself. It hands the build's file to the Mobile Apps publish
machinery and watches it, so a pipeline publish is the same thing as a publish
from the Mobile Apps page — same gate, same steps, same release record:

1. **Authority.** Publishing is admin-only. The stage publishes with the rights
   of the administrator who saved its target (``storeUpload.authorizedBy``),
   re-checked now: someone who has lost admin stops publishing through the
   pipeline too.
2. **The file.** The build's first AAB/APK (Play) or IPA (App Store) kept as an
   artifact — or the first matching the stage's name pattern. On Kubernetes the
   collector uploads artifacts before the Job reports success, so they are in
   the store by the time any server stage starts.
3. **Into Mobile Apps.** ``mobile_app_service.ingest_ci_build`` copies it into
   the registered app's binary store and probes its signature.
4. **Publish.** ``mobile_app_service.start_publish`` — which refuses a binary
   whose signature was stripped (SafeCore shielding does this), with the same
   explanation the Mobile Apps page gives. The stage fails with it instead of
   uploading something the store would reject.
5. **Watch.** The ``MobileAppPublish`` steps (credentials → upload → release →
   confirm) are mirrored onto the stage until the publish is published or
   failed. App Store processing can take a while; the stage's timeout bounds
   how long the build waits, not the publish itself.

Never twice: the publish is looked up before one is started, and the moment a
publish is requested is persisted first, so a restart (or a second pass)
adopts the publish that exists instead of uploading the same binary again.
"""

from __future__ import annotations

import fnmatch
import logging
import time
from datetime import timezone
from typing import Any, Dict, Optional, Tuple

from ...audit import log_audit
from ...db import db
from ...models_ci import CiArtifact, CiBuild, CiBuildStage
from . import server_stage_base as base
from . import store_upload_config

logger = logging.getLogger(__name__)

PREFIX = "[store]"
_ACTIVE = ("queued", "uploading", "processing")


# ---------------------------------------------------------------------------
# Engine entry points
# ---------------------------------------------------------------------------

def start(build: CiBuild, stage: CiBuildStage, definition: Dict[str, Any]) -> None:
    config = definition.get("storeUpload") if isinstance(definition.get("storeUpload"), dict) else None
    base.begin(
        stage,
        {"kind": "store_upload", "phase": "starting", "outcome": None, "message": "", "target": _target(config)},
    )
    earlier = base.earlier_failure(build, stage)
    if earlier:
        _finish(stage, "skipped", f"Nothing was published: {earlier}", outcome="skipped")
        return
    if config is None or not config.get("appId"):
        _fail(stage, "This App store upload stage names no mobile application. Pick one in the pipeline editor.")
        return
    _log(stage, f"Publishing to {store_upload_config.target_label(config)}")
    _prepare(build, stage, config)


def advance(build: CiBuild, stage: CiBuildStage, definition: Dict[str, Any]) -> None:
    current = base.state(stage)
    config = definition.get("storeUpload") if isinstance(definition.get("storeUpload"), dict) else {}
    timeout = base.timeout_of(definition)
    if base.elapsed(stage) > timeout:
        _on_timeout(build, stage, config, timeout)
        return
    phase = current.get("phase")
    if phase == "publishing" and current.get("publishId"):
        _watch(build, stage, config)
    elif phase in ("starting", "preparing", "publishing"):
        # A restart between "about to publish" and "publish recorded":
        # _prepare looks for the publish before it would start one.
        _prepare(build, stage, config)
    else:
        _fail(stage, f"The App store upload stage lost track of its progress (phase '{phase}').")


def cancel(build: CiBuild, stage: CiBuildStage) -> None:
    from ...models import MobileAppPublish

    current = base.state(stage)
    note = " Nothing was published."
    pub = db.session.get(MobileAppPublish, int(current.get("publishId") or 0)) if current.get("publishId") else None
    if pub is not None and pub.status in _ACTIVE:
        note = (
            f" Publish #{pub.id} was already {pub.status} and cannot be stopped from here — "
            "it carries on; check the release in Mobile Apps."
        )
    elif pub is not None and pub.status == "published":
        note = f" Publish #{pub.id} had already completed."
    _audit("ci_store_upload_cancelled", build, stage, None, base.state(stage), publishId=current.get("publishId"))
    _finish(stage, "cancelled", f"Cancelled by request.{note}", outcome="cancelled")


def summarize(stage: CiBuildStage) -> str:
    current = base.state(stage)
    label = (current.get("target") or {}).get("label") or "the store"
    message = current.get("message") or stage.error or ""
    if current.get("outcome") == "published":
        return f"App store upload stage '{stage.name}' published to {label}. {message}".strip()
    if current.get("phase") == "publishing":
        return f"App store upload stage '{stage.name}' is publishing to {label} ({current.get('publishStatus') or 'starting'})."
    return f"App store upload stage '{stage.name}' ({label}): {message}".strip()


def describe_targets(user, service_id: Optional[int]) -> Dict[str, Any]:
    """What the stage editor needs: the registered apps, which one belongs to
    this service, what each can publish to, and whether the viewer could
    authorize a target (saving one requires that)."""
    from ...access_engine import is_admin
    from ...models import MobileApplication
    from ..mobile_app_service import configured_platforms

    items = []
    for app in MobileApplication.query.order_by(MobileApplication.name.asc()).all():
        platforms = configured_platforms(app)
        items.append(
            {
                "id": app.id,
                "name": app.name,
                "ciServiceId": app.ci_service_id,
                "linked": bool(service_id and app.ci_service_id == service_id),
                "platforms": platforms,
                "androidPackageName": app.android_package_name or "",
                "iosBundleId": app.ios_bundle_id or "",
                # Readiness only — never the credential itself.
                "playReady": bool(app.android_package_name and app.play_service_account_json_encrypted),
                "appStoreReady": bool(
                    app.ios_bundle_id and app.asc_issuer_id and app.asc_key_id and app.asc_private_key_encrypted
                ),
            }
        )
    return {
        "apps": items,
        "canPublish": bool(user is not None and is_admin(user)),
        "playTracks": list(store_upload_config.PLAY_TRACKS),
        "appStoreTargets": list(store_upload_config.APP_STORE_TARGETS),
    }


# ---------------------------------------------------------------------------
# Prepare → publish
# ---------------------------------------------------------------------------

def _prepare(build: CiBuild, stage: CiBuildStage, config: Dict[str, Any]) -> None:
    from ...models import MobileApplication
    from ..mobile_app_service import MobileAppError, start_publish

    current = base.state(stage)
    user, problem = _authorizing_user(config)
    if problem:
        _fail(stage, problem)
        return
    app = db.session.get(MobileApplication, int(config["appId"]))
    if app is None:
        _fail(stage, f"Mobile application #{config['appId']} no longer exists, so there is nowhere to publish. Nothing was published.")
        return

    artifact, problem = _pick_artifact(build, config)
    if problem:
        _fail(stage, problem)
        return
    mobile_build, problem = _ingest(app, build, artifact)
    if problem:
        _fail(stage, problem)
        return
    target = {**_target(config), "appName": app.name}
    base.save(
        stage,
        {
            "phase": "preparing",
            "target": target,
            "appId": app.id,
            "artifactId": artifact.id,
            "artifactName": artifact.name,
            "mobileBuildId": mobile_build.id,
            "version": mobile_build.version,
            "signatureState": mobile_build.signature_state,
        },
    )
    if current.get("mobileBuildId") != mobile_build.id:
        _log(stage, f"{artifact.name} is release #{mobile_build.id} of {app.name} in Mobile Apps ({mobile_build.version}).")

    existing = _existing_publish(mobile_build.id, config, current.get("publishRequestedAt"))
    if existing is not None:
        base.save(stage, {"phase": "publishing", "publishId": existing.id})
        _log(stage, f"Publish #{existing.id} of this binary to {target['label']} already exists ({existing.status}) — following it, not uploading again.")
        _watch(build, stage, config)
        return

    # Persisted BEFORE the publish is created: a restart after this point
    # finds the publish by this moment rather than starting a second one.
    base.save(stage, {"phase": "publishing", "publishRequestedAt": time.time()})
    db.session.commit()
    try:
        publish = start_publish(mobile_build.id, config["store"], config["target"], user=user)
    except MobileAppError as exc:
        # The signature gate, missing store credentials, a publish already in
        # progress: start_publish says why in words the Mobile Apps page uses.
        _fail(stage, f"{exc} Nothing was published.")
        return
    base.save(stage, {"publishId": publish["id"]})
    _log(stage, f"Publish #{publish['id']} started as {getattr(user, 'username', '?')}.")
    _audit("ci_store_upload_started", build, stage, user, base.state(stage), publishId=publish["id"])
    _watch(build, stage, config)


def _watch(build: CiBuild, stage: CiBuildStage, config: Dict[str, Any]) -> None:
    from ...models import MobileAppPublish

    current = base.state(stage)
    pub = db.session.get(MobileAppPublish, int(current.get("publishId") or 0))
    if pub is None:
        _fail(stage, f"Publish #{current.get('publishId')} that this stage was following was deleted. Check Mobile Apps.")
        return
    db.session.refresh(pub)
    label = (current.get("target") or {}).get("label") or store_upload_config.target_label(config)
    steps = [dict(step) for step in (pub.steps or [])]
    store_ref = pub.store_ref if isinstance(pub.store_ref, dict) else {}
    patch = {
        "publishStatus": pub.status,
        "steps": steps,
        "storeRef": {k: store_ref.get(k) for k in ("versionCode", "ascBuildId", "bundleVersion") if store_ref.get(k)},
    }
    running = next((s for s in steps if s.get("status") == "run"), None)
    detail = f"{running.get('key')}: {running.get('detail')}" if running else pub.status
    if detail != current.get("lastDetail"):
        patch["lastDetail"] = detail
        patch["heartbeatAt"] = time.time()
        _log(stage, detail)
    base.save(stage, patch)

    if pub.status == "published":
        what = current.get("artifactName") or "the binary"
        version = store_ref.get("versionCode")
        extra = f" (versionCode {version})" if version else ""
        message = f"Published {what}{extra} to {label} as publish #{pub.id}."
        _log(stage, f"✓ {message}")
        _audit("ci_store_upload_published", build, stage, None, base.state(stage), publishId=pub.id)
        _finish(stage, "success", message, outcome="published")
        return
    if pub.status == "failed":
        message = f"Publishing to {label} failed (publish #{pub.id}): {pub.error or 'no reason given'}"
        _audit("ci_store_upload_failed", build, stage, None, base.state(stage), publishId=pub.id, error=pub.error)
        _fail(stage, message)
        return
    base.heartbeat(stage, PREFIX, f"Still publishing to {label}: {detail}")


def _on_timeout(build: CiBuild, stage: CiBuildStage, config: Dict[str, Any], timeout: int) -> None:
    from ...models import MobileAppPublish

    current = base.state(stage)
    limit = base.limit_label(timeout)
    pub = db.session.get(MobileAppPublish, int(current.get("publishId") or 0)) if current.get("publishId") else None
    if pub is not None and pub.status in _ACTIVE:
        message = (
            f"Publishing did not finish within the stage's {limit} limit. Publish #{pub.id} is still "
            f"{pub.status} and carries on in Mobile Apps — check the release there."
        )
    elif pub is not None and pub.status == "published":
        _watch(build, stage, config)
        return
    else:
        message = f"The stage exceeded its {limit} limit before it could publish. Nothing was published."
    _log(stage, message)
    _finish(stage, "timeout", message, outcome="timed_out")


# ---------------------------------------------------------------------------
# Checks
# ---------------------------------------------------------------------------

def _authorizing_user(config: Dict[str, Any]):
    """The administrator this stage publishes as, still one — or (None, why not)."""
    from ...access_engine import is_admin
    from ...models import User

    stamp = config.get("authorizedBy") if isinstance(config.get("authorizedBy"), dict) else None
    if not stamp or not stamp.get("userId"):
        return None, (
            "Nobody has authorized this App store upload stage. An administrator must save the "
            "pipeline — publishing to a store is admin-only. Nothing was published."
        )
    user = db.session.get(User, int(stamp["userId"]))
    who = stamp.get("username") or f"user #{stamp['userId']}"
    if user is None or not getattr(user, "is_active", True):
        return None, (
            f"The store target was authorized by {who}, whose account is no longer active. An "
            "administrator must save the stage again. Nothing was published."
        )
    if not is_admin(user):
        return None, (
            f"The store target was authorized by {who}, who is no longer an administrator — "
            "publishing to a store is admin-only. An administrator must save the stage again. "
            "Nothing was published."
        )
    return user, ""


def _pick_artifact(build: CiBuild, config: Dict[str, Any]) -> Tuple[Optional[CiArtifact], str]:
    kind = config.get("artifactType") or store_upload_config.DEFAULT_ARTIFACT_TYPE.get(config.get("store"), "aab")
    pattern = (config.get("artifactPattern") or "").lower()
    rows = (
        CiArtifact.query.filter(
            CiArtifact.build_id == build.id,
            CiArtifact.storage_backend == "local",
            CiArtifact.storage_ref.isnot(None),
        )
        .order_by(CiArtifact.id.asc())
        .all()
    )
    for row in rows:
        if row.artifact_type != kind:
            continue
        name = str(row.name or "").lower()
        if pattern and not (
            fnmatch.fnmatch(name, pattern) or fnmatch.fnmatch(name.rsplit("/", 1)[-1], pattern)
        ):
            continue
        return row, ""
    kept = ", ".join(f"{r.artifact_type} {r.name}" for r in rows[:6] if r.artifact_type in ("apk", "aab", "ipa"))
    matching = f" matching '{config.get('artifactPattern')}'" if pattern else ""
    return None, (
        f"This build kept no {kind.upper()} file{matching}, so there is nothing to publish."
        + (f" (It kept: {kept}.)" if kept else "")
        + f" Keep it as a build artifact: add its path (for example **/*.{kind}) with type {kind} "
        "to “Files to keep” on the stage that builds it."
    )


def _ingest(app, build: CiBuild, artifact: CiArtifact):
    """The Mobile Apps release for this file, created if it is not there yet."""
    from ...models import MobileAppBuild
    from ..mobile_app_service import ingest_ci_build

    existing = (
        MobileAppBuild.query.filter_by(app_id=app.id, ci_build_id=build.id, artifact_type=artifact.artifact_type)
        .filter(MobileAppBuild.status != "failed")
        .order_by(MobileAppBuild.id.asc())
        .first()
    )
    if existing is None:
        try:
            created = ingest_ci_build(app, build, source="pipeline", commit=False, artifact_ids=[artifact.id])
        except OSError as exc:
            return None, f"Could not copy {artifact.name} into Mobile Apps: {exc}. Nothing was published."
        existing = created[0] if created else None
        if existing is not None:
            log_audit(
                "mobile_build_registered",
                actor=None,
                target_type="mobile_app",
                target_id=str(app.id),
                details={"app": app.name, "ciBuild": build.number, "platforms": [existing.platform], "source": "pipeline"},
                commit=False,
            )
    if existing is None:
        return None, (
            f"{artifact.name} could not be read from the build's artifact store (it may have been "
            "cleaned up). Nothing was published."
        )
    if existing.sha256 and artifact.checksum_sha256 and existing.sha256 != artifact.checksum_sha256:
        return None, (
            f"This build's {artifact.artifact_type.upper()} is already in Mobile Apps as "
            f"{existing.file_name}, which is a different file from {artifact.name}. Narrow the "
            "stage's file pattern, or publish that one from Mobile Apps. Nothing was published."
        )
    return existing, ""


def _existing_publish(mobile_build_id: int, config: Dict[str, Any], requested_at: Optional[float]):
    """A publish of this binary to this store and target that already exists.

    Any status when this stage had already asked for one (it is that publish,
    whatever became of it); otherwise only one that is running or done — a
    failed manual attempt from Mobile Apps is not a reason to skip uploading.
    """
    from ...models import MobileAppPublish

    rows = (
        MobileAppPublish.query.filter_by(
            build_id=mobile_build_id, store=config["store"], target=config["target"]
        )
        .order_by(MobileAppPublish.id.desc())
        .all()
    )
    for row in rows:
        if requested_at:
            created = row.created_at
            if created is not None:
                created = created if created.tzinfo else created.replace(tzinfo=timezone.utc)
                if created.timestamp() >= float(requested_at) - 5:
                    return row
        if row.status in _ACTIVE or row.status == "published":
            return row
    return None


# ---------------------------------------------------------------------------
# State, logs, audit
# ---------------------------------------------------------------------------

def _target(config: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    config = config or {}
    return {
        "appId": config.get("appId"),
        "store": config.get("store") or "",
        "target": config.get("target") or "",
        "label": store_upload_config.target_label(config) if config.get("store") else "",
        "artifactType": config.get("artifactType") or "",
        "artifactPattern": config.get("artifactPattern") or "",
        "authorizedBy": (config.get("authorizedBy") or {}).get("username") or "",
    }


def _log(stage: CiBuildStage, message: str) -> None:
    base.log(stage, PREFIX, message)


def _fail(stage: CiBuildStage, message: str) -> None:
    _log(stage, message)
    _finish(stage, "failed", message, outcome="failed")


def _finish(stage: CiBuildStage, status: str, message: str, *, outcome: str) -> None:
    base.finish(stage, status, message, outcome=outcome)


def _audit(action: str, build: CiBuild, stage: CiBuildStage, user, state: Dict[str, Any], **extra) -> None:
    target = state.get("target") or {}
    log_audit(
        action,
        actor=user,
        target_type="ci_build",
        target_id=str(build.id),
        details={
            "service": build.service.slug if build.service else None,
            "buildNumber": build.number,
            "stage": stage.name,
            "app": target.get("appName") or target.get("appId"),
            "store": target.get("store"),
            "target": target.get("target"),
            "file": state.get("artifactName"),
            **extra,
        },
        commit=False,
    )
