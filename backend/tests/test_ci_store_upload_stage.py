"""The App store upload stage: one of the build's binaries goes to a store.

What is locked here:

* the configuration rules (store, track/target, file type, pattern) and that
  they agree with what Mobile Apps' start_publish accepts;
* the app defaults to the one linked to the service, and must exist;
* "whoever saved the target" — set by the server, carried over when the target
  is unchanged, refused to a non-administrator (publishing is admin-only), and
  re-checked when the stage runs;
* the run, through Mobile Apps and never around it: the build's AAB goes to a
  Play track, its IPA to TestFlight (store clients patched), with the publish's
  steps mirrored on the stage;
* the signature gate: a stripped binary is refused with Mobile Apps' own words;
* never twice: a restart mid-publish, or a publish that already exists, is
  followed instead of uploading the same binary again;
* no file of the right kind, an earlier failure, and an Approval in front.
"""

from __future__ import annotations

import io
import os
import zipfile
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from api.db import db
from api.models import MobileAppBuild, MobileAppPublish, User
from api.models_application_intelligence import BitbucketCredentialProfile
from api.models_ci import CiArtifact, CiBuild
from api.secret_encryption import encrypt_secret
from api.services import app_store_client, google_play_client
from api.services import mobile_app_service
from api.services.ci import store_upload_config
from tests.conftest import auth_headers


@pytest.fixture()
def stores(tmp_path, monkeypatch):
    """Both binary stores in a temp dir, and the store clients patched."""
    monkeypatch.setenv("CI_ARTIFACT_DIR", str(tmp_path / "ci"))
    monkeypatch.setenv("MOBILE_ARTIFACT_DIR", str(tmp_path / "mobile"))
    calls = {"play_uploads": [], "tracks": [], "asc_uploads": []}
    monkeypatch.setattr(google_play_client, "access_token", lambda cfg: "tok")
    monkeypatch.setattr(google_play_client, "create_edit", lambda cfg, tok: "edit-1")

    def play_upload(cfg, tok, edit, path, kind, progress=None):
        calls["play_uploads"].append((os.path.basename(path), kind))
        return 42

    monkeypatch.setattr(google_play_client, "upload_binary", play_upload)
    monkeypatch.setattr(
        google_play_client, "assign_track",
        lambda cfg, tok, edit, track, vc: calls["tracks"].append((track, vc)),
    )
    monkeypatch.setattr(google_play_client, "commit_edit", lambda cfg, tok, edit: None)
    monkeypatch.setattr(app_store_client, "resolve_app_id", lambda cfg: "999")

    def asc_upload(cfg, path, name, progress=None):
        calls["asc_uploads"].append(name)
        return {"buildUploadId": "u1", "appId": "999", "bundleVersion": "42"}

    monkeypatch.setattr(app_store_client, "upload_build", asc_upload)
    monkeypatch.setattr(
        app_store_client, "processing_state",
        lambda cfg, ref, version: {"state": "processing", "detail": "Apple is processing the build"},
    )
    return SimpleNamespace(root=tmp_path, calls=calls)


@pytest.fixture()
def service(app, client, admin_token):
    with app.app_context():
        credential = BitbucketCredentialProfile(
            name="ci-token",
            provider="bitbucket",
            credential_type="repository_access_token",
            secret_cipher=encrypt_secret("clone-token-value"),
            read_only=True,
            enabled=True,
        )
        db.session.add(credential)
        db.session.commit()
        credential_id = credential.id
    service_id = client.post(
        "/api/ci/services",
        json={"name": "POS App", "applicationType": "android"},
        headers=auth_headers(admin_token),
    ).get_json()["data"]["id"]
    client.put(
        f"/api/ci/services/{service_id}/source",
        json={
            "repositoryUrl": "https://bitbucket.org/areeba/pos-app",
            "defaultBranch": "main",
            "credentialProfileId": credential_id,
        },
        headers=auth_headers(admin_token),
    )
    pipeline_id = client.get(
        f"/api/ci/services/{service_id}/pipelines", headers=auth_headers(admin_token)
    ).get_json()["data"]["items"][0]["id"]
    return SimpleNamespace(id=service_id, pipeline_id=pipeline_id)


def _mobile_app(client, admin_token, service_id=None, **overrides):
    payload = {
        "name": "POS",
        "zohoEnvironment": f"POS Mobile {overrides.get('name', '')}".strip(),
        "androidPackageName": "com.areeba.pos",
        "playServiceAccountJson": '{"client_email": "svc@x.iam", "private_key": "k"}',
        "iosBundleId": "com.areeba.pos",
        "ascIssuerId": "iss-1",
        "ascKeyId": "KEY1",
        "ascPrivateKey": "-----BEGIN PRIVATE KEY-----\nx\n-----END PRIVATE KEY-----",
    }
    if service_id is not None:
        payload["ciServiceId"] = service_id
    payload.update(overrides)
    response = client.post("/api/mobile-apps", json=payload, headers=auth_headers(admin_token))
    assert response.status_code == 201, response.get_json()
    return response.get_json()["data"]["id"]


def _upload(**overrides):
    config = {"store": "google_play", "target": "internal", "artifactType": "aab", "artifactPattern": ""}
    config.update(overrides)
    return config


def _stages(upload=None, *, approval=False):
    stages = [
        {"name": "Checkout", "stageType": "checkout", "runnerLabels": ["mock"]},
        {"name": "Build", "stageType": "command", "commands": ["./gradlew bundleRelease"], "runnerLabels": ["mock"]},
    ]
    if approval:
        stages.append({
            "name": "Release sign-off", "stageType": "approval",
            "approval": {"anyoneWithPermission": True, "minApprovals": 1}, "timeoutSeconds": 3600,
        })
    stages.append({"name": "To Play", "stageType": "store_upload", "storeUpload": upload or _upload()})
    return stages


def _save(client, token, pipeline_id, stages):
    return client.put(
        f"/api/ci/pipelines/{pipeline_id}",
        json={"parameters": [], "stages": stages},
        headers=auth_headers(token),
    )


def _aab(signed=True) -> bytes:
    buf = io.BytesIO()
    names = ["base/manifest/AndroidManifest.xml", "META-INF/MANIFEST.MF"]
    if signed:
        names.append("META-INF/UPLOAD.RSA")
    with zipfile.ZipFile(buf, "w") as zf:
        for name in names:
            zf.writestr(name, b"payload")
    return buf.getvalue()


def _keep(app, build_id, name, kind, payload):
    """A file the build kept, as the collector would have uploaded it."""
    from api.services.ci import artifacts as ci_artifacts

    with app.app_context():
        build = db.session.get(CiBuild, build_id)
        rel = f"{build.service_id}/{build.id}/{name}"
        path = os.path.join(ci_artifacts.artifact_root(), rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "wb") as handle:
            handle.write(payload)
        import hashlib

        db.session.add(CiArtifact(
            service_id=build.service_id, build_id=build.id, artifact_type=kind, name=name,
            storage_backend="local", storage_ref=rel, size_bytes=len(payload),
            checksum_sha256=hashlib.sha256(payload).hexdigest(),
        ))
        db.session.commit()


def _trigger(client, token, service_id):
    return client.post(
        f"/api/ci/services/{service_id}/builds", json={}, headers=auth_headers(token)
    ).get_json()["data"]["id"]


def _advance(app, build_id, passes=40, stop_when_waiting=True):
    from api.services.ci import engine
    from api.services.ci.runners import mock as mock_runner

    original = mock_runner._STAGE_SECONDS
    mock_runner._STAGE_SECONDS = 0.0
    try:
        with app.app_context():
            for _ in range(passes):
                engine.advance_ci_builds()
                build = db.session.get(CiBuild, build_id)
                if build.status not in ("queued", "running"):
                    break
                if stop_when_waiting and any(
                    s.stage_type == "approval" and s.status == "running" for s in build.stages
                ):
                    break
    finally:
        mock_runner._STAGE_SECONDS = original


def _run(app, client, token, service_id, files=(("app-release.aab", "aab", None),)):
    build_id = _trigger(client, token, service_id)
    for name, kind, payload in files:
        _keep(app, build_id, name, kind, payload if payload is not None else _aab(True))
    _advance(app, build_id)
    return build_id


def _build(client, token, build_id):
    return client.get(f"/api/ci/builds/{build_id}", headers=auth_headers(token)).get_json()["data"]


def _upload_stage(data):
    return next(s for s in data["stages"] if s["stageType"] == "store_upload")


# ---------------------------------------------------------------------------
# Configuration rules
# ---------------------------------------------------------------------------

def test_the_targets_are_the_ones_mobile_apps_publishes_to():
    assert store_upload_config.PLAY_TRACKS == mobile_app_service.PLAY_TRACKS
    assert store_upload_config.APP_STORE_TARGETS == mobile_app_service.APP_STORE_TARGETS


def test_defaults_are_the_internal_track_and_testflight():
    play = store_upload_config.normalize({"store": "google_play"}, "store_upload", "Ship")
    assert (play["target"], play["artifactType"]) == ("internal", "aab")
    ios = store_upload_config.normalize({"store": "app_store"}, "store_upload", "Ship")
    assert (ios["target"], ios["artifactType"]) == ("testflight", "ipa")


@pytest.mark.parametrize(
    "value, message",
    [
        ({"store": "itunes"}, "the store must be"),
        ({"store": "google_play", "target": "vip"}, "track must be one of"),
        ({"store": "app_store", "target": "internal"}, "target must be one of"),
        ({"store": "app_store", "artifactType": "aab"}, "takes IPA files"),
        ({"store": "google_play", "artifactPattern": "$(rm -rf)"}, "not a file name pattern"),
    ],
)
def test_a_store_target_is_held_to_what_the_store_takes(value, message):
    with pytest.raises(store_upload_config.StoreUploadConfigError, match=message):
        store_upload_config.normalize(value, "store_upload", "Ship")


def test_only_a_store_upload_stage_carries_a_store_target():
    assert store_upload_config.normalize(None, "command", "Build") is None
    with pytest.raises(store_upload_config.StoreUploadConfigError, match="publishes nothing"):
        store_upload_config.normalize(_upload(), "command", "Build")


def test_the_signature_covers_what_is_authorized():
    base = store_upload_config.normalize(dict(_upload(), appId=1), "store_upload", "Ship")
    assert store_upload_config.signature(base) != store_upload_config.signature(dict(base, target="production"))
    assert store_upload_config.signature(base) != store_upload_config.signature(dict(base, appId=2))
    assert store_upload_config.signature(base) != store_upload_config.signature(dict(base, artifactPattern="*-prod.aab"))


def test_the_app_defaults_to_the_one_linked_to_the_service(client, admin_token, service, stores):
    app_id = _mobile_app(client, admin_token, service.id)
    data = _save(client, admin_token, service.pipeline_id, _stages()).get_json()["data"]
    upload = data["stages"][-1]["storeUpload"]
    assert upload["appId"] == app_id
    assert upload["authorizedBy"]["username"] == "admin"


def test_no_linked_app_and_none_picked_is_refused(client, admin_token, service, stores):
    response = _save(client, admin_token, service.pipeline_id, _stages())
    assert response.status_code == 400
    assert "none is linked to this service" in response.get_json()["error"]


def test_an_app_that_does_not_exist_is_refused(client, admin_token, service, stores):
    response = _save(client, admin_token, service.pipeline_id, _stages(_upload(appId=4242)))
    assert response.status_code == 400
    assert "does not exist" in response.get_json()["error"]


def test_a_store_stage_must_come_after_the_build(client, admin_token, service, stores):
    _mobile_app(client, admin_token, service.id)
    stages = _stages()
    stages.append({"name": "Notify", "stageType": "command", "commands": ["echo done"]})
    response = _save(client, admin_token, service.pipeline_id, stages)
    assert response.status_code == 400
    assert "App store upload stage 'To Play'" in response.get_json()["error"]


# ---------------------------------------------------------------------------
# Authority
# ---------------------------------------------------------------------------

def _developer(app):
    with app.app_context():
        admin = User.query.filter_by(username="admin").first()
        dev = User(username="dev", email="dev@example.com", is_active=True)
        dev.password_hash = admin.password_hash
        db.session.add(dev)
        db.session.commit()
        return dev.id


def _save_as(app, pipeline_id, stages, user_id, *, admin: bool):
    from api.models_ci import CiPipeline
    from api.services.ci import pipelines

    with app.app_context(), patch("api.access_engine.is_admin", return_value=admin):
        return pipelines.update_pipeline(
            db.session.get(CiPipeline, pipeline_id), {"stages": stages}, actor=db.session.get(User, user_id)
        )


def test_a_stamp_in_the_request_is_ignored(client, admin_token, service, stores):
    _mobile_app(client, admin_token, service.id)
    forged = dict(_upload(), authorizedBy={"userId": 999, "username": "mallory"})
    data = _save(client, admin_token, service.pipeline_id, _stages(forged)).get_json()["data"]
    assert data["stages"][-1]["storeUpload"]["authorizedBy"]["username"] == "admin"


def test_a_non_admin_keeps_the_stamp_when_the_target_is_unchanged(app, client, admin_token, service, stores):
    _mobile_app(client, admin_token, service.id)
    _save(client, admin_token, service.pipeline_id, _stages())
    dev = _developer(app)
    stages = _stages()
    stages[1]["commands"] = ["./gradlew clean bundleRelease"]  # Unrelated edit.
    data = _save_as(app, service.pipeline_id, stages, dev, admin=False)
    assert data["stages"][-1]["storeUpload"]["authorizedBy"]["username"] == "admin"


def test_a_non_admin_cannot_change_the_target(app, client, admin_token, service, stores):
    from api.services.ci.pipelines import PipelineError

    _mobile_app(client, admin_token, service.id)
    _save(client, admin_token, service.pipeline_id, _stages())
    dev = _developer(app)
    with pytest.raises(PipelineError, match="Only an administrator"):
        _save_as(app, service.pipeline_id, _stages(_upload(target="production")), dev, admin=False)


def test_the_stage_stops_when_its_authorizer_is_no_longer_an_admin(app, client, admin_token, service, stores):
    _mobile_app(client, admin_token, service.id)
    _save(client, admin_token, service.pipeline_id, _stages())
    build_id = _trigger(client, admin_token, service.id)
    _keep(app, build_id, "app-release.aab", "aab", _aab(True))
    with patch("api.access_engine.is_admin", return_value=False):
        _advance(app, build_id)
    data = _build(client, admin_token, build_id)
    stage = _upload_stage(data)
    assert stage["status"] == "failed"
    assert "authorized by admin, who is no longer an administrator" in stage["error"]
    assert stores.calls["play_uploads"] == []


def test_the_editor_is_told_who_can_publish(client, admin_token, operator_token, service, stores):
    app_id = _mobile_app(client, admin_token, service.id)
    data = client.get(
        f"/api/ci/store-upload-targets?serviceId={service.id}", headers=auth_headers(admin_token)
    ).get_json()["data"]
    assert data["canPublish"] is True
    entry = next(item for item in data["apps"] if item["id"] == app_id)
    assert entry["linked"] is True and entry["playReady"] is True and entry["appStoreReady"] is True
    assert "playServiceAccountJson" not in entry and "ascPrivateKey" not in entry
    other = client.get("/api/ci/store-upload-targets", headers=auth_headers(operator_token)).get_json()["data"]
    assert other["canPublish"] is False


# ---------------------------------------------------------------------------
# Running
# ---------------------------------------------------------------------------

def test_the_build_aab_goes_to_the_internal_track(app, client, admin_token, service, stores):
    app_id = _mobile_app(client, admin_token, service.id)
    _save(client, admin_token, service.pipeline_id, _stages())
    build_id = _run(app, client, admin_token, service.id)

    data = _build(client, admin_token, build_id)
    assert data["status"] == "success", data
    stage = _upload_stage(data)
    assert stage["runnerName"] is None
    state = stage["storeUpload"]
    assert state["outcome"] == "published"
    assert state["publishStatus"] == "published"
    assert state["target"]["label"] == "Google Play (internal track)"
    assert state["storeRef"]["versionCode"] == 42
    assert {s["key"]: s["status"] for s in state["steps"]} == {
        "credentials": "done", "upload": "done", "release": "done", "confirm": "done",
    }
    assert stores.calls["play_uploads"] == [("app-release.aab", "aab")]
    assert stores.calls["tracks"] == [("internal", 42)]

    with app.app_context():
        release = db.session.get(MobileAppBuild, state["mobileBuildId"])
        assert release.app_id == app_id and release.ci_build_id == build_id
        assert release.source == "pipeline" and release.signature_state == "signed"
        publish = db.session.get(MobileAppPublish, state["publishId"])
        assert publish.triggered_by == "admin" and publish.status == "published"


def test_the_build_ipa_goes_to_testflight(app, client, admin_token, service, stores):
    _mobile_app(client, admin_token, service.id)
    _save(
        client, admin_token, service.pipeline_id,
        _stages(_upload(store="app_store", target="testflight", artifactType="ipa")),
    )
    build_id = _run(app, client, admin_token, service.id, files=(("POS.ipa", "ipa", b"fake-ipa"),))

    # Apple is still processing: the stage waits on the publish.
    data = _build(client, admin_token, build_id)
    assert data["status"] == "running"
    state = _upload_stage(data)["storeUpload"]
    assert state["publishStatus"] == "processing"
    assert stores.calls["asc_uploads"] == ["POS.ipa"]

    with app.app_context(), patch.object(
        app_store_client, "processing_state", lambda cfg, ref, version: {"state": "done", "buildId": "b-77"}
    ):
        mobile_app_service.advance_mobile_publishes()
    _advance(app, build_id)
    data = _build(client, admin_token, build_id)
    assert data["status"] == "success", data
    state = _upload_stage(data)["storeUpload"]
    assert state["outcome"] == "published"
    assert {s["key"]: s["status"] for s in state["steps"]}["confirm"] == "skip"
    assert "App Store Connect (TestFlight)" in state["message"]


def test_a_name_pattern_picks_the_file(app, client, admin_token, service, stores):
    _mobile_app(client, admin_token, service.id)
    _save(client, admin_token, service.pipeline_id, _stages(_upload(artifactPattern="*-prod-release.aab")))
    build_id = _run(
        app, client, admin_token, service.id,
        files=(("app-uat-release.aab", "aab", _aab(True)), ("app-prod-release.aab", "aab", _aab(True) + b"")),
    )
    assert _build(client, admin_token, build_id)["status"] == "success"
    assert stores.calls["play_uploads"] == [("app-prod-release.aab", "aab")]


def test_a_stripped_binary_is_refused_with_the_mobile_apps_explanation(app, client, admin_token, service, stores):
    _mobile_app(client, admin_token, service.id)
    _save(client, admin_token, service.pipeline_id, _stages())
    build_id = _run(app, client, admin_token, service.id, files=(("app-release.aab", "aab", _aab(signed=False)),))
    data = _build(client, admin_token, build_id)
    assert data["status"] == "failed"
    stage = _upload_stage(data)
    assert "unsigned" in stage["error"] and "Re-sign it before publishing" in stage["error"]
    assert stage["storeUpload"]["signatureState"] == "unsigned"
    assert stores.calls["play_uploads"] == []
    with app.app_context():
        assert MobileAppPublish.query.count() == 0


def test_a_build_that_kept_no_aab_publishes_nothing(app, client, admin_token, service, stores):
    _mobile_app(client, admin_token, service.id)
    _save(client, admin_token, service.pipeline_id, _stages())
    build_id = _run(app, client, admin_token, service.id, files=(("app-release.apk", "apk", b"apk"),))
    stage = _upload_stage(_build(client, admin_token, build_id))
    assert stage["status"] == "failed"
    assert "kept no AAB file" in stage["error"] and "It kept: apk app-release.apk" in stage["error"]
    assert "Files to keep" in stage["error"]


def test_a_store_failure_fails_the_stage_with_the_reason(app, client, admin_token, service, stores, monkeypatch):
    _mobile_app(client, admin_token, service.id)
    _save(client, admin_token, service.pipeline_id, _stages())

    def refuse(cfg, tok, edit, path, kind, progress=None):
        raise google_play_client.PlayError("Google Play refused the request (403)", 403)

    monkeypatch.setattr(google_play_client, "upload_binary", refuse)
    build_id = _run(app, client, admin_token, service.id)
    stage = _upload_stage(_build(client, admin_token, build_id))
    assert stage["status"] == "failed"
    assert "Google Play refused the request (403)" in stage["error"]
    assert stage["storeUpload"]["publishStatus"] == "failed"


def test_an_earlier_failure_means_nothing_is_published(app, client, admin_token, service, stores):
    from api.services.ci.runners import base as runner_base

    _mobile_app(client, admin_token, service.id)
    _save(client, admin_token, service.pipeline_id, _stages())
    adapter = runner_base.get_adapter("mock")
    original_poll = adapter.poll
    adapter.poll = lambda handle: runner_base.FAILED
    try:
        build_id = _run(app, client, admin_token, service.id)
    finally:
        adapter.poll = original_poll
    data = _build(client, admin_token, build_id)
    assert data["status"] == "failed"
    assert _upload_stage(data)["status"] == "skipped"
    assert stores.calls["play_uploads"] == []


def test_an_approval_in_front_holds_the_upload(app, client, admin_token, operator_token, service, stores):
    _mobile_app(client, admin_token, service.id)
    _save(client, admin_token, service.pipeline_id, _stages(approval=True))
    build_id = _run(app, client, admin_token, service.id)
    data = _build(client, admin_token, build_id)
    assert data["status"] == "running" and _upload_stage(data)["status"] == "pending"
    assert stores.calls["play_uploads"] == []

    approval = next(s for s in data["stages"] if s["stageType"] == "approval")
    response = client.post(
        f"/api/ci/builds/{build_id}/stages/{approval['id']}/approve",
        json={"comment": "release it"}, headers=auth_headers(operator_token),
    )
    assert response.status_code == 200
    _advance(app, build_id)
    assert _build(client, admin_token, build_id)["status"] == "success"
    assert stores.calls["play_uploads"] == [("app-release.aab", "aab")]


# ---------------------------------------------------------------------------
# Never twice
# ---------------------------------------------------------------------------

def test_a_restart_mid_publish_adopts_the_publish_instead_of_uploading_again(
    app, client, admin_token, service, stores, monkeypatch
):
    """The publish row was created, then the backend died before the stage
    recorded its id. The next pass must find it, not upload again."""
    from api.services.ci import store_upload_stage

    _mobile_app(client, admin_token, service.id)
    _save(client, admin_token, service.pipeline_id, _stages())
    real_start = mobile_app_service.start_publish

    def start_then_crash(*args, **kwargs):
        real_start(*args, **kwargs)
        raise RuntimeError("backend restarted")

    with patch("api.services.mobile_app_service.start_publish", side_effect=start_then_crash):
        build_id = _trigger(client, admin_token, service.id)
        _keep(app, build_id, "app-release.aab", "aab", _aab(True))
        # The crash is mid-pass: the engine records an unexpected failure.
        # Emulate the restart by putting the stage back to what was committed.
        with app.app_context():
            from api.services.ci import engine

            original = store_upload_stage.start

            def start_survives(build, stage, definition):
                try:
                    original(build, stage, definition)
                except RuntimeError:
                    db.session.rollback()

            with patch.object(store_upload_stage, "start", side_effect=start_survives):
                from api.services.ci.runners import mock as mock_runner

                mock_runner_seconds = mock_runner._STAGE_SECONDS
                mock_runner._STAGE_SECONDS = 0.0
                try:
                    for _ in range(10):
                        engine.advance_ci_builds()
                        current = db.session.get(CiBuild, build_id)
                        if any(s.stage_type == "store_upload" and s.status == "running" for s in current.stages):
                            break
                finally:
                    mock_runner._STAGE_SECONDS = mock_runner_seconds
            stage = next(s for s in db.session.get(CiBuild, build_id).stages if s.stage_type == "store_upload")
            assert stage.server_state["phase"] == "publishing"
            assert not stage.server_state.get("publishId")
            assert MobileAppPublish.query.count() == 1

    _advance(app, build_id)
    data = _build(client, admin_token, build_id)
    assert data["status"] == "success", data
    with app.app_context():
        assert MobileAppPublish.query.count() == 1
    assert stores.calls["play_uploads"] == [("app-release.aab", "aab")]
    assert "not uploading again" in "\n".join(
        line["content"] for line in client.get(
            f"/api/ci/builds/{build_id}/stages/{_upload_stage(data)['id']}/logs",
            headers=auth_headers(admin_token),
        ).get_json()["data"]["lines"]
    )


def test_a_binary_already_published_from_mobile_apps_is_not_uploaded_again(
    app, client, admin_token, service, stores
):
    app_id = _mobile_app(client, admin_token, service.id)
    _save(client, admin_token, service.pipeline_id, _stages())
    build_id = _trigger(client, admin_token, service.id)
    _keep(app, build_id, "app-release.aab", "aab", _aab(True))
    with app.app_context():
        mobile_app = mobile_app_service.get_app(app_id)
        release = mobile_app_service.ingest_ci_build(mobile_app, db.session.get(CiBuild, build_id))[0]
        mobile_app_service.start_publish(
            release.id, "google_play", "internal", user=User.query.filter_by(username="admin").first()
        )
    assert len(stores.calls["play_uploads"]) == 1
    _advance(app, build_id)
    data = _build(client, admin_token, build_id)
    assert data["status"] == "success"
    assert len(stores.calls["play_uploads"]) == 1
    with app.app_context():
        assert MobileAppPublish.query.count() == 1


def test_cancelling_mid_publish_says_the_upload_carries_on(app, client, admin_token, service, stores):
    from api.services.ci import engine

    _mobile_app(client, admin_token, service.id)
    _save(
        client, admin_token, service.pipeline_id,
        _stages(_upload(store="app_store", target="testflight", artifactType="ipa")),
    )
    build_id = _run(app, client, admin_token, service.id, files=(("POS.ipa", "ipa", b"fake-ipa"),))
    client.post(f"/api/ci/builds/{build_id}/cancel", headers=auth_headers(admin_token))
    with app.app_context():
        engine.advance_ci_builds()
    stage = _upload_stage(_build(client, admin_token, build_id))
    assert stage["status"] == "cancelled"
    assert "processing and cannot be stopped from here" in stage["error"]
