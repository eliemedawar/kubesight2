"""Tests for re-signing: triggering the Jenkins signing job, following it,
collecting the archived signed binary, and refusing anything still unsigned.

Jenkins is faked throughout — these cover KubeSight's half of the contract
(trigger, poll, collect, verify, refuse), not Jenkins itself.
"""

from __future__ import annotations

import io
import itertools
import zipfile

import pytest

from api.db import db
from api.models import MobileAppBuild, MobileAppResign
from api.services import jenkins_client
from api.services import mobile_app_service as svc
from api.services import resign_executor
from tests.conftest import auth_headers


@pytest.fixture()
def artifact_dir(tmp_path, monkeypatch):
    root = tmp_path / "mobile_artifacts"
    monkeypatch.setenv("MOBILE_ARTIFACT_DIR", str(root))
    return root


RESIGN_CFG = {
    "android": {
        "executor": "jenkins",
        "jobPath": "mobile/android-resign",
        "resultPattern": "signed/*.aab",
        "fileParam": "apkfile",
    },
    "ios": {
        "executor": "jenkins",
        "jobPath": "mobile/ios-resign",
        "resultPattern": "signed/*.ipa",
        "extraParams": {"PROV_PROFILE": "areeba_AppStore.mobileprovision"},
    },
}

QUEUE_URL = "http://jenkins.local:8080/queue/item/42"
SIGN_BUILD_URL = "http://jenkins.local:8080/job/mobile/job/android-resign/17"


def _aab(signed: bool) -> bytes:
    buf = io.BytesIO()
    names = ["base/manifest/AndroidManifest.xml", "META-INF/MANIFEST.MF"]
    if signed:
        names.append("META-INF/UPLOAD.RSA")
    with zipfile.ZipFile(buf, "w") as zf:
        for name in names:
            zf.writestr(name, b"payload")
    return buf.getvalue()


def _ipa(signed: bool) -> bytes:
    buf = io.BytesIO()
    names = ["Payload/POS.app/Info.plist", "Payload/POS.app/POS"]
    if signed:
        names.append("Payload/POS.app/_CodeSignature/CodeResources")
    with zipfile.ZipFile(buf, "w") as zf:
        for name in names:
            zf.writestr(name, b"payload")
    return buf.getvalue()


_APP_SEQ = itertools.count(1)


def _create_app(client, admin_token, resign_config=RESIGN_CFG):
    # A Zoho environment maps to exactly one app, so each registration needs
    # its own name/environment.
    n = next(_APP_SEQ)
    payload = {
        "name": f"POS {n}",
        "zohoEnvironment": f"POS Mobile {n}",
        "jenkinsJobPath": "POS-APK",
        "artifactConfig": {
            "android": {"source": "archive", "pattern": "*.aab"},
            "ios": {"source": "archive", "pattern": "*.ipa"},
        },
        "androidPackageName": "com.areeba.pos",
        "playServiceAccountJson": '{"client_email": "svc@x.iam", "private_key": "k"}',
        "iosBundleId": "com.areeba.pos",
        "ascIssuerId": "issuer",
        "ascKeyId": "key",
        "ascPrivateKey": "-----BEGIN PRIVATE KEY-----\nk\n-----END PRIVATE KEY-----",
    }
    if resign_config is not None:
        payload["resignConfig"] = resign_config
    resp = client.post("/api/mobile-apps", json=payload, headers=auth_headers(admin_token))
    assert resp.status_code == 201, resp.get_json()
    return resp.get_json()["data"]


def _upload(client, token, app_id, payload, filename="app-release.aab", platform="android"):
    return client.post(
        f"/api/mobile-apps/{app_id}/builds/upload",
        data={"file": (io.BytesIO(payload), filename), "platform": platform},
        content_type="multipart/form-data",
        headers=auth_headers(token),
    )


def _fake_jenkins(monkeypatch, *, signed_bytes=None, result_name="app-release.aab"):
    """Stub the Jenkins side: trigger, queue, build, artifacts, download.

    Starts held in the queue, which is what actually happens — the build has not
    even been assigned an executor when the trigger returns. Tests call
    ``_dispatch`` to let it start, so nothing races through the whole state
    machine inside the POST that requested it.
    """
    seen = {
        "queue": {"state": "pending", "why": "Waiting for next available executor"},
        "build": {"building": False, "result": "SUCCESS", "durationMs": 1000, "url": SIGN_BUILD_URL},
        "artifacts": [{"fileName": result_name, "relativePath": f"signed/{result_name}"}],
    }

    def fake_trigger(cfg, params, file_param, file_path, file_name=""):
        seen["params"] = params
        seen["jobPath"] = cfg.router_job_path
        seen["fileParam"] = file_param
        with open(file_path, "rb") as fh:
            seen["uploaded"] = fh.read()
        seen["fileName"] = file_name
        return QUEUE_URL

    monkeypatch.setattr(jenkins_client, "trigger_build_with_file", fake_trigger)
    monkeypatch.setattr(jenkins_client, "queue_state", lambda cfg, url: seen["queue"])
    monkeypatch.setattr(jenkins_client, "build_state", lambda cfg, url: seen["build"])
    monkeypatch.setattr(jenkins_client, "list_artifacts", lambda cfg, url: seen["artifacts"])
    monkeypatch.setattr(
        jenkins_client, "artifact_url", lambda cfg, build_url, rel: f"{build_url}/artifact/{rel}"
    )

    payload = signed_bytes if signed_bytes is not None else _aab(signed=True)

    def fake_download(cfg, url, dest_path, **kwargs):
        with open(dest_path, "wb") as fh:
            fh.write(payload)
        return {"size": len(payload), "sha256": "b" * 64}

    monkeypatch.setattr(jenkins_client, "download_file", fake_download)
    return seen


def _dispatch(seen):
    """Let the queued item become a running build."""
    seen["queue"] = {"state": "building", "buildNumber": 17, "buildUrl": SIGN_BUILD_URL}


# ---------------------------------------------------------------------------
# Triggering
# ---------------------------------------------------------------------------

def test_resign_uploads_the_binary_with_the_trigger(
    app, client, admin_token, artifact_dir, monkeypatch
):
    """The agents have no route back to KubeSight, so the trigger must carry the
    binary — nothing may depend on the job fetching it."""
    created = _create_app(client, admin_token)
    unsigned = _aab(signed=False)
    build = _upload(client, admin_token, created["id"], unsigned).get_json()["data"]
    assert build["signatureState"] == "unsigned"

    seen = _fake_jenkins(monkeypatch)
    resp = client.post(
        f"/api/mobile-apps/builds/{build['id']}/resign", headers=auth_headers(admin_token)
    )
    assert resp.status_code == 202, resp.get_json()

    assert seen["jobPath"] == "mobile/android-resign"
    assert seen["fileParam"] == "apkfile"
    assert seen["uploaded"] == unsigned  # the exact stored binary, byte for byte
    assert seen["fileName"] == "app-release.aab"

    with app.app_context():
        row = MobileAppResign.query.filter_by(build_id=build["id"]).first()
        assert row.status == "running"
        assert row.executor == "jenkins"
        assert row.job_ref["queueUrl"] == QUEUE_URL


def test_extra_params_cannot_collide_with_the_file_part(
    app, client, admin_token, artifact_dir, monkeypatch
):
    """A text field named like the file parameter would collide with the upload
    in the multipart body; harmless extras still pass through."""
    hostile = {
        "android": {
            **RESIGN_CFG["android"],
            "extraParams": {"apkfile": "not-the-binary", "KEY_ALIAS": "zakykey"},
        }
    }
    created = _create_app(client, admin_token, resign_config=hostile)
    build = _upload(client, admin_token, created["id"], _aab(signed=False)).get_json()["data"]
    seen = _fake_jenkins(monkeypatch)
    client.post(f"/api/mobile-apps/builds/{build['id']}/resign", headers=auth_headers(admin_token))

    assert "apkfile" not in seen["params"]
    assert seen["params"]["KEY_ALIAS"] == "zakykey"


def test_resign_requires_a_configured_job(app, client, admin_token, artifact_dir):
    created = _create_app(client, admin_token, resign_config=None)
    build = _upload(client, admin_token, created["id"], _aab(signed=False)).get_json()["data"]
    resp = client.post(
        f"/api/mobile-apps/builds/{build['id']}/resign", headers=auth_headers(admin_token)
    )
    assert resp.status_code == 409
    assert "no signing job configured" in resp.get_json()["error"].lower()


def test_config_without_job_path_is_rejected(app, client, admin_token, artifact_dir):
    resp = client.post(
        "/api/mobile-apps",
        json={"name": "No job", "resignConfig": {"android": {"executor": "jenkins"}}},
        headers=auth_headers(admin_token),
    )
    assert resp.status_code == 400
    assert "job path" in resp.get_json()["error"].lower()


def test_resign_refuses_second_concurrent_run(app, client, admin_token, artifact_dir, monkeypatch):
    created = _create_app(client, admin_token)
    build = _upload(client, admin_token, created["id"], _aab(signed=False)).get_json()["data"]
    _fake_jenkins(monkeypatch)

    first = client.post(
        f"/api/mobile-apps/builds/{build['id']}/resign", headers=auth_headers(admin_token)
    )
    assert first.status_code == 202
    second = client.post(
        f"/api/mobile-apps/builds/{build['id']}/resign", headers=auth_headers(admin_token)
    )
    assert second.status_code == 409
    assert "already running" in second.get_json()["error"]


def test_resign_requires_manage_permission(app, client, admin_token, operator_token, artifact_dir):
    created = _create_app(client, admin_token)
    build = _upload(client, admin_token, created["id"], _aab(signed=False)).get_json()["data"]
    resp = client.post(
        f"/api/mobile-apps/builds/{build['id']}/resign", headers=auth_headers(operator_token)
    )
    assert resp.status_code == 403


# ---------------------------------------------------------------------------
# Following the build and collecting the result
# ---------------------------------------------------------------------------

def _start(app, client, admin_token, monkeypatch, *, platform="android", unsigned=None, **kw):
    created = _create_app(client, admin_token)
    payload = unsigned if unsigned is not None else (
        _aab(signed=False) if platform == "android" else _ipa(signed=False)
    )
    name = "app-release.aab" if platform == "android" else "app-release.ipa"
    build = _upload(
        client, admin_token, created["id"], payload, filename=name, platform=platform
    ).get_json()["data"]
    seen = _fake_jenkins(monkeypatch, **kw)
    client.post(f"/api/mobile-apps/builds/{build['id']}/resign", headers=auth_headers(admin_token))
    with app.app_context():
        row = MobileAppResign.query.filter_by(build_id=build["id"]).first()
        return created, build, row.id, seen


@pytest.mark.parametrize(
    "platform,result_name,signed_bytes_fn",
    [("android", "app-release.aab", lambda: _aab(True)), ("ios", "app-release.ipa", lambda: _ipa(True))],
)
def test_signed_result_becomes_a_publishable_child_build(
    app, client, admin_token, artifact_dir, monkeypatch, platform, result_name, signed_bytes_fn
):
    """Both platforms sign on Jenkins — Android on a Linux agent, iOS on the Mac."""
    created, build, resign_id, seen = _start(
        app,
        client,
        admin_token,
        monkeypatch,
        platform=platform,
        result_name=result_name,
        signed_bytes=signed_bytes_fn(),
    )

    _dispatch(seen)
    with app.app_context():
        svc.advance_mobile_resigns()  # poll → succeeded → collect (inline in tests)
        row = MobileAppResign.query.get(resign_id)
        assert row.status == "completed", row.error
        assert [s["status"] for s in row.steps] == ["done"] * 5
        assert row.job_ref["buildUrl"] == SIGN_BUILD_URL

        child = MobileAppBuild.query.get(row.result_build_id)
        assert child.source == "resign"
        assert child.parent_build_id == build["id"]
        assert child.signature_state == "signed"
        assert child.status == "available"
        assert child.jenkins_build_number == 17

    # The signed child publishes; the stripped parent still cannot.
    store = "google_play" if platform == "android" else "app_store"
    target = "internal" if platform == "android" else "testflight"
    monkeypatch.setattr("api.services.google_play_client.access_token", lambda cfg: "tok")
    monkeypatch.setattr("api.services.google_play_client.create_edit", lambda c, t: "e1")
    monkeypatch.setattr("api.services.google_play_client.upload_binary", lambda *a: 9)
    monkeypatch.setattr("api.services.google_play_client.assign_track", lambda *a: None)
    monkeypatch.setattr("api.services.google_play_client.commit_edit", lambda *a: None)
    monkeypatch.setattr("api.services.app_store_client.upload_build", lambda *a, **k: {"id": "u1"})
    monkeypatch.setattr(
        "api.services.app_store_client.processing_state",
        lambda *a, **k: {"state": "VALID", "buildId": "b1"},
    )

    with app.app_context():
        child_id = MobileAppResign.query.get(resign_id).result_build_id
    resp = client.post(
        f"/api/mobile-apps/builds/{child_id}/publish",
        json={"store": store, "target": target},
        headers=auth_headers(admin_token),
    )
    assert resp.status_code == 202, resp.get_json()

    resp = client.post(
        f"/api/mobile-apps/builds/{build['id']}/publish",
        json={"store": store, "target": target},
        headers=auth_headers(admin_token),
    )
    assert resp.status_code == 409


def test_still_unsigned_result_fails_the_run(app, client, admin_token, artifact_dir, monkeypatch):
    """A job that archives an unsigned binary has failed, however green the
    build looks — the dud must never become publishable."""
    _, _, resign_id, seen = _start(app, client, admin_token, monkeypatch, signed_bytes=_aab(False))
    _dispatch(seen)

    with app.app_context():
        svc.advance_mobile_resigns()
        row = MobileAppResign.query.get(resign_id)
        assert row.status == "failed"
        assert row.result_build_id is None
        verify = [s for s in row.steps if s["key"] == "verify"][0]
        assert verify["status"] == "fail"
        assert MobileAppBuild.query.filter_by(source="resign", status="available").count() == 0


def test_failed_build_fails_the_run(app, client, admin_token, artifact_dir, monkeypatch):
    _, _, resign_id, seen = _start(app, client, admin_token, monkeypatch)
    _dispatch(seen)
    seen["build"] = {"building": False, "result": "FAILURE", "durationMs": 1, "url": SIGN_BUILD_URL}
    with app.app_context():
        svc.advance_mobile_resigns()
        row = MobileAppResign.query.get(resign_id)
        assert row.status == "failed"
        assert "FAILURE" in row.error


def test_cancelled_queue_item_fails_the_run(app, client, admin_token, artifact_dir, monkeypatch):
    _, _, resign_id, seen = _start(app, client, admin_token, monkeypatch)
    seen["queue"] = {"state": "cancelled"}
    with app.app_context():
        svc.advance_mobile_resigns()
        assert MobileAppResign.query.get(resign_id).status == "failed"


def test_missing_archived_artifact_fails_with_a_useful_message(
    app, client, admin_token, artifact_dir, monkeypatch
):
    _, _, resign_id, seen = _start(app, client, admin_token, monkeypatch)
    _dispatch(seen)
    seen["artifacts"] = []
    with app.app_context():
        svc.advance_mobile_resigns()
        row = MobileAppResign.query.get(resign_id)
        assert row.status == "failed"
        assert "no archived artifact" in row.error.lower()
        # No half-built child was left behind.
        assert MobileAppBuild.query.filter_by(source="resign").count() == 0


# ---------------------------------------------------------------------------
# Configuration round-trip (the Edit application form)
# ---------------------------------------------------------------------------

def test_resign_config_round_trips_through_the_api(app, client, admin_token, artifact_dir):
    created = _create_app(client, admin_token)
    assert created["resignConfig"]["android"]["jobPath"] == "mobile/android-resign"
    assert created["resignConfig"]["android"]["fileParam"] == "apkfile"
    assert created["resignConfig"]["ios"]["extraParams"]["PROV_PROFILE"].endswith(".mobileprovision")

    resp = client.put(
        f"/api/mobile-apps/{created['id']}",
        json={
            "resignConfig": {
                "android": {
                    "executor": "jenkins",
                    "jobPath": "mobile/android-resign-v2",
                    "resultPattern": "*.aab",
                }
            }
        },
        headers=auth_headers(admin_token),
    )
    assert resp.status_code == 200, resp.get_json()
    cfg = resp.get_json()["data"]["resignConfig"]
    assert cfg["android"]["jobPath"] == "mobile/android-resign-v2"
    assert "ios" not in cfg  # dropping a platform disables it


def test_clearing_resign_config_disables_signing(app, client, admin_token, artifact_dir):
    created = _create_app(client, admin_token)
    resp = client.put(
        f"/api/mobile-apps/{created['id']}",
        json={"resignConfig": {}},
        headers=auth_headers(admin_token),
    )
    assert resp.status_code == 200
    assert resp.get_json()["data"]["resignConfig"] == {}

    build = _upload(client, admin_token, created["id"], _aab(signed=False)).get_json()["data"]
    resp = client.post(
        f"/api/mobile-apps/builds/{build['id']}/resign", headers=auth_headers(admin_token)
    )
    assert resp.status_code == 409


def test_resign_config_ignores_unknown_fields(app, client, admin_token, artifact_dir):
    """Only known fields are stored — nothing can smuggle key material in."""
    created = _create_app(
        client,
        admin_token,
        resign_config={
            "android": {
                **RESIGN_CFG["android"],
                "storePassword": "hunter2",
                "keystoreBase64": "AAAA",
            }
        },
    )
    android = created["resignConfig"]["android"]
    assert "storePassword" not in android
    assert "keystoreBase64" not in android


def test_unsupported_executor_is_rejected(app, client, admin_token, artifact_dir):
    resp = client.post(
        "/api/mobile-apps",
        json={
            "name": "Bad executor",
            "resignConfig": {"android": {"executor": "k8s_job", "jobPath": "x"}},
        },
        headers=auth_headers(admin_token),
    )
    assert resp.status_code == 400
    assert "executor" in resp.get_json()["error"].lower()


# ---------------------------------------------------------------------------
# Executor unit
# ---------------------------------------------------------------------------

def test_multipart_body_streams_the_file_without_buffering_it(tmp_path):
    """The upload is hundreds of MB — the body must be produced as a stream, not
    assembled in memory, and Content-Length must match it exactly."""
    from api.services.jenkins_client import _MultipartStream

    binary = tmp_path / "app.aab"
    binary.write_bytes(b"BINARY" * 5000)

    prefix = b"--B\r\nContent-Disposition: form-data; name=\"apkfile\"\r\n\r\n"
    suffix = b"\r\n--B--\r\n"
    stream = _MultipartStream(prefix, str(binary), suffix)

    # Read in awkward chunk sizes to prove the stage transitions hold.
    out = b""
    while True:
        chunk = stream.read(7)
        if not chunk:
            break
        out += chunk

    assert out == prefix + binary.read_bytes() + suffix
    assert len(out) == len(prefix) + binary.stat().st_size + len(suffix)
    stream.close()


def test_trigger_with_file_builds_a_well_formed_multipart_body(tmp_path, monkeypatch):
    from api.services import jenkins_client as jc

    binary = tmp_path / "app.aab"
    binary.write_bytes(b"PAYLOAD")
    captured = {}

    def fake_submit(cfg, data, content_type, content_length, job_path=""):
        captured["contentType"] = content_type
        captured["length"] = content_length
        # Drain it the way http.client does: read until empty. A short read at a
        # stage boundary is legal and must not be mistaken for EOF.
        body = b""
        while True:
            chunk = data.read(8192)
            if not chunk:
                break
            body += chunk
        captured["body"] = body
        return QUEUE_URL

    monkeypatch.setattr(jc, "_submit_build", fake_submit)
    cfg = jc.JenkinsConfig(
        base_url="http://jenkins.local:8080",
        username="u",
        api_token="t",
        router_job_path="mobile/resign",
    )
    url = jc.trigger_build_with_file(cfg, {"PLATFORM": "android"}, "apkfile", str(binary))

    assert url == QUEUE_URL
    body = captured["body"]
    assert captured["contentType"].startswith("multipart/form-data; boundary=")
    assert b'name="PLATFORM"' in body and b"android" in body
    assert b'name="apkfile"; filename="app.aab"' in body
    assert b"PAYLOAD" in body
    # Content-Length must cover the whole body or the request truncates.
    assert captured["length"] >= len(b"PAYLOAD")


def test_trigger_with_file_rejects_a_missing_binary(tmp_path):
    from api.services import jenkins_client as jc

    cfg = jc.JenkinsConfig(
        base_url="http://jenkins.local:8080", username="u", api_token="t", router_job_path="x"
    )
    with pytest.raises(jc.JenkinsError, match="not found"):
        jc.trigger_build_with_file(cfg, {}, "apkfile", str(tmp_path / "nope.aab"))


def test_poll_reports_queued_until_an_executor_picks_it_up(monkeypatch):
    monkeypatch.setattr(
        jenkins_client, "queue_state", lambda cfg, url: {"state": "pending", "why": "Waiting"}
    )
    state = resign_executor.poll(None, {"queueUrl": QUEUE_URL})
    assert state["phase"] == "queued"
    assert state["detail"] == "Waiting"


def test_poll_treats_a_resultless_finished_build_as_still_running(monkeypatch):
    monkeypatch.setattr(
        jenkins_client,
        "build_state",
        lambda cfg, url: {"building": False, "result": None, "durationMs": 0, "url": url},
    )
    state = resign_executor.poll(None, {"buildUrl": SIGN_BUILD_URL})
    assert state["phase"] == "building"
