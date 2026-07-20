"""Tests for Mobile Applications: registration CRUD/RBAC, ticket-driven build
ingestion, the artifact download state machine, and store publishing."""

from __future__ import annotations

import pytest

from api.db import db
from api.models import DeployAutomationRun, MobileAppBuild, MobileApplication, MobileAppPublish
from api.services import app_store_client, google_play_client, jenkins_client
from api.services import mobile_app_service as svc
from api.services.zoho_sync_service import CUSTOM_SOURCE_CLUSTER
from tests.conftest import auth_headers


BUILD_URL = "http://jenkins.local:8080/job/POS-APK/898"


@pytest.fixture()
def artifact_dir(tmp_path, monkeypatch):
    root = tmp_path / "mobile_artifacts"
    monkeypatch.setenv("MOBILE_ARTIFACT_DIR", str(root))
    return root


def _app_payload(**overrides):
    payload = {
        "name": "POS",
        "description": "Point of sale app",
        "zohoEnvironment": "POS Mobile",
        "jenkinsJobPath": "POS-APK",
        "artifactConfig": {
            "android": {"source": "workspace", "path": "execution/node/71/ws/pos.apk"}
        },
        "androidPackageName": "com.areeba.pos",
        "playServiceAccountJson": '{"client_email": "svc@x.iam", "private_key": "k"}',
    }
    payload.update(overrides)
    return payload


def _create_app(client, admin_token, **overrides):
    resp = client.post(
        "/api/mobile-apps", json=_app_payload(**overrides), headers=auth_headers(admin_token)
    )
    assert resp.status_code == 201, resp.get_json()
    return resp.get_json()["data"]


def _fake_download(payload=b"fake-apk-bytes"):
    def fake(cfg, url, dest_path, **kwargs):
        with open(dest_path, "wb") as fh:
            fh.write(payload)
        return {"size": len(payload), "sha256": "a" * 64}

    return fake


# ---------------------------------------------------------------------------
# CRUD + RBAC
# ---------------------------------------------------------------------------

def test_app_crud_routes(client, admin_token, artifact_dir):
    created = _create_app(client, admin_token)
    assert created["playServiceAccountConfigured"] is True
    assert created["platforms"] == ["android"]
    assert "playServiceAccountJson" not in created  # secret never serialized back

    app_id = created["id"]
    resp = client.get("/api/mobile-apps", headers=auth_headers(admin_token))
    assert resp.status_code == 200
    items = resp.get_json()["data"]["items"]
    assert any(item["id"] == app_id for item in items)

    resp = client.put(
        f"/api/mobile-apps/{app_id}",
        json={"description": "updated", "clearPlayServiceAccount": True},
        headers=auth_headers(admin_token),
    )
    assert resp.status_code == 200
    updated = resp.get_json()["data"]
    assert updated["description"] == "updated"
    assert updated["playServiceAccountConfigured"] is False

    resp = client.delete(f"/api/mobile-apps/{app_id}", headers=auth_headers(admin_token))
    assert resp.status_code == 200


def test_rbac_gating(client, admin_token, operator_token, viewer_token, artifact_dir):
    created = _create_app(client, admin_token)
    # Operator has mobile_apps:view but not manage.
    resp = client.get("/api/mobile-apps", headers=auth_headers(operator_token))
    assert resp.status_code == 200
    resp = client.post(
        "/api/mobile-apps", json=_app_payload(name="Other", zohoEnvironment="Env2"),
        headers=auth_headers(operator_token),
    )
    assert resp.status_code == 403
    # Viewer has no mobile_apps permissions at all.
    resp = client.get("/api/mobile-apps", headers=auth_headers(viewer_token))
    assert resp.status_code == 403
    # Publish is admin-only even for a user who could hold manage.
    resp = client.post(
        "/api/mobile-apps/builds/1/publish",
        json={"store": "google_play", "target": "internal"},
        headers=auth_headers(operator_token),
    )
    assert resp.status_code == 403
    assert created["id"]


def test_environment_options_endpoint(client, app, admin_token, operator_token):
    """The form's dropdown source: custom Zoho environment names, manage-gated."""
    from api.services.zoho_sync_service import set_source

    with app.app_context():
        set_source(
            None,
            [],
            None,
            [
                {"name": "POS Mobile", "applications": ["pos"]},
                {"name": "Wallet", "applications": ["wallet"]},
            ],
        )
    resp = client.get("/api/mobile-apps/environments", headers=auth_headers(admin_token))
    assert resp.status_code == 200
    assert resp.get_json()["data"]["items"] == ["POS Mobile", "Wallet"]
    # Operator holds mobile_apps:view only — the manage-gated list is refused.
    resp = client.get("/api/mobile-apps/environments", headers=auth_headers(operator_token))
    assert resp.status_code == 403


def test_duplicate_zoho_environment_rejected(client, admin_token, artifact_dir):
    _create_app(client, admin_token)
    resp = client.post(
        "/api/mobile-apps",
        json=_app_payload(name="POS 2", zohoEnvironment="pos mobile"),  # casefolded clash
        headers=auth_headers(admin_token),
    )
    assert resp.status_code == 400
    assert "already mapped" in resp.get_json()["error"]


def test_workspace_source_requires_path(client, admin_token, artifact_dir):
    resp = client.post(
        "/api/mobile-apps",
        json=_app_payload(artifactConfig={"android": {"source": "workspace", "path": ""}}),
        headers=auth_headers(admin_token),
    )
    assert resp.status_code == 400


# ---------------------------------------------------------------------------
# Ticket-driven ingestion (custom-environment run success → pending build)
# ---------------------------------------------------------------------------

def _custom_run(**overrides):
    run = DeployAutomationRun(
        cluster_id=CUSTOM_SOURCE_CLUSTER,
        namespace="POS Mobile",
        deployment_name="pos",
        image_tag="",
        ticket_tag="1.4.2",
        ticket_number="T-1001",
        jenkins_build_number=898,
        jenkins_build_url=BUILD_URL,
        status="building",
    )
    for key, value in overrides.items():
        setattr(run, key, value)
    db.session.add(run)
    db.session.commit()
    return run


def test_custom_build_success_creates_pending_build(app, client, admin_token, artifact_dir):
    created = _create_app(client, admin_token)
    with app.app_context():
        run = _custom_run()
        svc.on_custom_build_success(run)
        db.session.commit()
        builds = MobileAppBuild.query.filter_by(app_id=created["id"]).all()
        assert len(builds) == 1
        assert builds[0].status == "pending"
        assert builds[0].version == "1.4.2"
        assert builds[0].ticket_number == "T-1001"
        # Idempotent: the same Jenkins build is not ingested twice.
        svc.on_custom_build_success(run)
        db.session.commit()
        assert MobileAppBuild.query.filter_by(app_id=created["id"]).count() == 1


def test_unmatched_environment_is_ignored(app, client, admin_token, artifact_dir):
    created = _create_app(client, admin_token, zohoEnvironment="Something Else")
    with app.app_context():
        run = _custom_run()
        svc.on_custom_build_success(run)
        db.session.commit()
        assert MobileAppBuild.query.filter_by(app_id=created["id"]).count() == 0


# ---------------------------------------------------------------------------
# Download state machine
# ---------------------------------------------------------------------------

def test_download_advances_to_available(app, client, admin_token, artifact_dir, monkeypatch):
    created = _create_app(client, admin_token)
    monkeypatch.setattr(jenkins_client, "download_file", _fake_download())
    with app.app_context():
        run = _custom_run()
        svc.on_custom_build_success(run)
        db.session.commit()
        svc.advance_mobile_builds()  # TESTING → download runs inline
        build = MobileAppBuild.query.filter_by(app_id=created["id"]).first()
        assert build.status == "available"
        assert build.file_name == "pos.apk"
        assert build.file_size == len(b"fake-apk-bytes")
        assert build.sha256 == "a" * 64
        path = svc.binary_path(build)
        assert path and path.endswith("pos.apk")
        with open(path, "rb") as fh:
            assert fh.read() == b"fake-apk-bytes"

    resp = client.get(
        f"/api/mobile-apps/builds/{build.id}/download", headers=auth_headers(admin_token)
    )
    assert resp.status_code == 200
    assert resp.data == b"fake-apk-bytes"
    assert "pos.apk" in resp.headers.get("Content-Disposition", "")


def test_download_retries_then_fails(app, client, admin_token, artifact_dir, monkeypatch):
    _create_app(client, admin_token)

    def boom(cfg, url, dest_path, **kwargs):
        raise jenkins_client.JenkinsError("Jenkins returned 404 for the artifact", 404)

    monkeypatch.setattr(jenkins_client, "download_file", boom)
    with app.app_context():
        run = _custom_run()
        svc.on_custom_build_success(run)
        db.session.commit()
        build = MobileAppBuild.query.first()
        for expected_retry in range(1, 5):
            svc.advance_mobile_builds()
            db.session.refresh(build)
            assert build.status == "pending"
            assert build.retry_count == expected_retry
        svc.advance_mobile_builds()  # 5th attempt exhausts the retry budget
        db.session.refresh(build)
        assert build.status == "failed"
        assert "404" in (build.error or "")


def test_manual_fetch_latest(app, client, admin_token, artifact_dir, monkeypatch):
    created = _create_app(client, admin_token)
    monkeypatch.setattr(
        jenkins_client, "last_successful_build", lambda cfg, job: {"number": 898, "url": BUILD_URL}
    )
    monkeypatch.setattr(jenkins_client, "download_file", _fake_download())

    resp = client.post(
        f"/api/mobile-apps/{created['id']}/fetch", headers=auth_headers(admin_token)
    )
    assert resp.status_code == 202, resp.get_json()
    builds = resp.get_json()["data"]["builds"]
    assert builds and builds[0]["source"] == "manual"

    # Same Jenkins build again → conflict.
    resp = client.post(
        f"/api/mobile-apps/{created['id']}/fetch", headers=auth_headers(admin_token)
    )
    assert resp.status_code == 409


# ---------------------------------------------------------------------------
# Direct binary upload (bypasses Jenkins)
# ---------------------------------------------------------------------------

def _upload(
    client,
    token,
    app_id,
    *,
    filename="app-release.aab",
    platform="android",
    payload=b"aab-bytes",
    version="",
):
    from io import BytesIO

    return client.post(
        f"/api/mobile-apps/{app_id}/builds/upload",
        data={
            "file": (BytesIO(payload), filename),
            "platform": platform,
            "version": version,
        },
        content_type="multipart/form-data",
        headers=auth_headers(token),
    )


def test_upload_build_creates_available_build(client, admin_token, artifact_dir):
    created = _create_app(client, admin_token)
    resp = _upload(client, admin_token, created["id"], payload=b"aab-bytes-123")
    assert resp.status_code == 201, resp.get_json()
    build = resp.get_json()["data"]
    assert build["status"] == "available"
    assert build["source"] == "upload"
    assert build["artifactType"] == "aab"
    assert build["fileName"] == "app-release.aab"
    assert build["fileSize"] == len(b"aab-bytes-123")
    # No version supplied and not an IPA → the file name is the release label.
    assert build["version"] == "app-release.aab"

    # It shows up in the build list and the stored binary downloads normally —
    # proving the publish flow (which only needs an available build on disk)
    # will accept it unchanged.
    resp = client.get(
        f"/api/mobile-apps/builds/{build['id']}/download", headers=auth_headers(admin_token)
    )
    assert resp.status_code == 200
    assert resp.data == b"aab-bytes-123"


def test_upload_build_rejects_wrong_extension(client, admin_token, artifact_dir):
    created = _create_app(client, admin_token)
    # An .ipa is not a valid Android artifact.
    resp = _upload(client, admin_token, created["id"], filename="app.ipa", platform="android")
    assert resp.status_code == 400
    assert ".apk or .aab" in resp.get_json()["error"]


def test_upload_build_requires_manage(client, admin_token, operator_token, artifact_dir):
    created = _create_app(client, admin_token)
    # Operator holds mobile_apps:view only, not manage.
    resp = _upload(client, operator_token, created["id"])
    assert resp.status_code == 403


# ---------------------------------------------------------------------------
# Publishing
# ---------------------------------------------------------------------------

def _available_build(app, client, admin_token, artifact_dir, monkeypatch):
    created = _create_app(client, admin_token)
    monkeypatch.setattr(jenkins_client, "download_file", _fake_download())
    with app.app_context():
        run = _custom_run()
        svc.on_custom_build_success(run)
        db.session.commit()
        svc.advance_mobile_builds()
        build = MobileAppBuild.query.filter_by(app_id=created["id"]).first()
        return created, build.id


def test_publish_google_play_happy_path(app, client, admin_token, artifact_dir, monkeypatch):
    created, build_id = _available_build(app, client, admin_token, artifact_dir, monkeypatch)
    calls = {}
    monkeypatch.setattr(google_play_client, "access_token", lambda cfg: "tok")
    monkeypatch.setattr(google_play_client, "create_edit", lambda cfg, tok: "edit-1")
    def fake_upload(cfg, tok, edit, path, kind):
        calls["upload"] = (path, kind)
        return 42

    monkeypatch.setattr(google_play_client, "upload_binary", fake_upload)
    monkeypatch.setattr(
        google_play_client,
        "assign_track",
        lambda cfg, tok, edit, track, vc: calls.setdefault("track", (track, vc)),
    )
    monkeypatch.setattr(
        google_play_client, "commit_edit", lambda cfg, tok, edit: calls.setdefault("commit", edit)
    )

    resp = client.post(
        f"/api/mobile-apps/builds/{build_id}/publish",
        json={"store": "google_play", "target": "beta"},
        headers=auth_headers(admin_token),
    )
    assert resp.status_code == 202, resp.get_json()

    with app.app_context():
        pub = MobileAppPublish.query.filter_by(build_id=build_id).first()
        assert pub.status == "published"
        assert pub.store_ref["versionCode"] == 42
        step_status = {s["key"]: s["status"] for s in pub.steps}
        assert step_status == {
            "credentials": "done", "upload": "done", "release": "done", "confirm": "done",
        }
    assert calls["track"] == ("beta", 42)
    assert calls["upload"][1] == "apk"

    resp = client.get(
        f"/api/mobile-apps/{created['id']}/publishes", headers=auth_headers(admin_token)
    )
    assert resp.status_code == 200
    items = resp.get_json()["data"]["items"]
    assert items and items[0]["status"] == "published"


def test_publish_validation(app, client, admin_token, artifact_dir, monkeypatch):
    _, build_id = _available_build(app, client, admin_token, artifact_dir, monkeypatch)
    headers = auth_headers(admin_token)
    # Android build cannot go to the App Store.
    resp = client.post(
        f"/api/mobile-apps/builds/{build_id}/publish",
        json={"store": "app_store", "target": "testflight"},
        headers=headers,
    )
    assert resp.status_code == 400
    # Unknown track rejected.
    resp = client.post(
        f"/api/mobile-apps/builds/{build_id}/publish",
        json={"store": "google_play", "target": "vip"},
        headers=headers,
    )
    assert resp.status_code == 400


def test_publish_app_store_processing_flow(app, client, admin_token, artifact_dir, monkeypatch):
    created = _create_app(
        client,
        admin_token,
        name="POS iOS",
        zohoEnvironment="POS iOS",
        artifactConfig={"ios": {"source": "workspace", "path": "execution/node/71/ws/pos.ipa"}},
        iosBundleId="com.areeba.pos",
        ascIssuerId="iss-1",
        ascKeyId="KEY1",
        ascPrivateKey="-----BEGIN PRIVATE KEY-----\nx\n-----END PRIVATE KEY-----",
    )
    monkeypatch.setattr(jenkins_client, "download_file", _fake_download(b"fake-ipa"))
    with app.app_context():
        run = _custom_run(namespace="POS iOS")
        svc.on_custom_build_success(run)
        db.session.commit()
        svc.advance_mobile_builds()
        build = MobileAppBuild.query.filter_by(app_id=created["id"]).first()
        assert build.status == "available"
        build_id = build.id

    submitted = {}
    monkeypatch.setattr(app_store_client, "resolve_app_id", lambda cfg: "999")
    monkeypatch.setattr(
        app_store_client,
        "upload_build",
        lambda cfg, path, name: {"buildUploadId": "u1", "appId": "999", "bundleVersion": "42"},
    )
    monkeypatch.setattr(
        app_store_client,
        "submit_for_review",
        lambda cfg, bid: submitted.setdefault("buildId", bid),
    )
    # Still processing on Apple's side at upload time.
    monkeypatch.setattr(
        app_store_client,
        "processing_state",
        lambda cfg, ref, version: {"state": "processing", "detail": "crunching"},
    )

    resp = client.post(
        f"/api/mobile-apps/builds/{build_id}/publish",
        json={"store": "app_store", "target": "review"},
        headers=auth_headers(admin_token),
    )
    assert resp.status_code == 202, resp.get_json()
    with app.app_context():
        pub = MobileAppPublish.query.filter_by(build_id=build_id).first()
        assert pub.status == "processing"
        assert pub.store_ref["buildUploadId"] == "u1"

        # Apple finishes processing → the tick promotes it and submits for review.
        monkeypatch.setattr(
            app_store_client,
            "processing_state",
            lambda cfg, ref, version: {"state": "done", "buildId": "b-77"},
        )
        svc.advance_mobile_publishes()
        db.session.refresh(pub)
        assert pub.status == "published"
        assert submitted["buildId"] == "b-77"
        step_status = {s["key"]: s["status"] for s in pub.steps}
        assert step_status["confirm"] == "done"


def test_publish_failure_records_error(app, client, admin_token, artifact_dir, monkeypatch):
    _, build_id = _available_build(app, client, admin_token, artifact_dir, monkeypatch)
    monkeypatch.setattr(google_play_client, "access_token", lambda cfg: "tok")
    monkeypatch.setattr(
        google_play_client,
        "create_edit",
        lambda cfg, tok: (_ for _ in ()).throw(
            google_play_client.PlayError("Google Play refused the request (403)", 403)
        ),
    )
    resp = client.post(
        f"/api/mobile-apps/builds/{build_id}/publish",
        json={"store": "google_play", "target": "internal"},
        headers=auth_headers(admin_token),
    )
    assert resp.status_code == 202  # accepted; failure lands on the job record
    with app.app_context():
        pub = MobileAppPublish.query.filter_by(build_id=build_id).first()
        assert pub.status == "failed"
        assert "403" in (pub.error or "")
