"""Tests for Android re-signing: the job state machine, the scoped signing
token, and the signature verification on whatever the job hands back.

The Kubernetes executor is faked throughout — these cover KubeSight's half of
the contract (launch, poll, ingest, refuse), not kubectl.
"""

from __future__ import annotations

import io
import itertools
import zipfile

import pytest

from api.db import db
from api.models import MobileAppBuild, MobileApplication, MobileAppResign
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
        "executor": "k8s_job",
        "cluster": "prod",
        "namespace": "kubesight",
        "image": "registry.local/android-signer:1",
        "keystoreSecret": "android-upload-keystore",
        "keyAlias": "upload",
    }
}


def _aab(signed: bool) -> bytes:
    buf = io.BytesIO()
    names = ["base/manifest/AndroidManifest.xml", "META-INF/MANIFEST.MF"]
    if signed:
        names.append("META-INF/UPLOAD.RSA")
    with zipfile.ZipFile(buf, "w") as zf:
        for name in names:
            zf.writestr(name, b"payload")
    return buf.getvalue()


_APP_SEQ = itertools.count(1)


def _create_app(client, admin_token, resign_config=RESIGN_CFG):
    # A Zoho environment maps to exactly one app, so each registration in a
    # test needs its own name/environment.
    n = next(_APP_SEQ)
    payload = {
        "name": f"POS {n}",
        "zohoEnvironment": f"POS Mobile {n}",
        "jenkinsJobPath": "POS-APK",
        "artifactConfig": {"android": {"source": "archive", "pattern": "*.aab"}},
        "androidPackageName": "com.areeba.pos",
        "playServiceAccountJson": '{"client_email": "svc@x.iam", "private_key": "k"}',
    }
    if resign_config is not None:
        payload["resignConfig"] = resign_config
    resp = client.post("/api/mobile-apps", json=payload, headers=auth_headers(admin_token))
    assert resp.status_code == 201, resp.get_json()
    return resp.get_json()["data"]


def _upload(client, token, app_id, payload, filename="app-release.aab"):
    return client.post(
        f"/api/mobile-apps/{app_id}/builds/upload",
        data={"file": (io.BytesIO(payload), filename), "platform": "android"},
        content_type="multipart/form-data",
        headers=auth_headers(token),
    )


def _fake_executor(monkeypatch, phase="running"):
    """Stub the cluster: record the spec, report a phase, swallow cleanup."""
    seen = {}

    def fake_launch(spec):
        seen["spec"] = spec
        return {"kind": "k8s_job", "name": f"kubesight-resign-{spec.resign_id}",
                "namespace": spec.namespace, "cluster": spec.cluster}

    monkeypatch.setattr(resign_executor, "launch", fake_launch)
    monkeypatch.setattr(resign_executor, "poll", lambda ref: {"phase": phase, "detail": ""})
    monkeypatch.setattr(resign_executor, "logs", lambda ref, tail=40: "")
    monkeypatch.setattr(resign_executor, "cleanup", lambda ref: seen.setdefault("cleaned", True))
    return seen


# ---------------------------------------------------------------------------
# Launching
# ---------------------------------------------------------------------------

def test_resign_launches_job_and_scopes_token(app, client, admin_token, artifact_dir, monkeypatch):
    created = _create_app(client, admin_token)
    build = _upload(client, admin_token, created["id"], _aab(signed=False)).get_json()["data"]
    assert build["signatureState"] == "unsigned"

    seen = _fake_executor(monkeypatch)
    resp = client.post(
        f"/api/mobile-apps/builds/{build['id']}/resign", headers=auth_headers(admin_token)
    )
    assert resp.status_code == 202, resp.get_json()

    spec = seen["spec"]
    assert spec.build_id == build["id"]
    assert spec.image == RESIGN_CFG["android"]["image"]
    assert spec.keystore_secret == "android-upload-keystore"
    assert spec.artifact_type == "aab"

    # The token names this run and this build only.
    from api.auth_utils import resign_token_claims

    claims = resign_token_claims(spec.token)
    assert claims["buildId"] == build["id"]
    assert claims["purpose"] == "resign"
    assert "sub" in claims and claims["sub"].startswith("resign:")

    with app.app_context():
        row = MobileAppResign.query.filter_by(build_id=build["id"]).first()
        assert row.status == "running"
        assert row.job_ref["namespace"] == "kubesight"
        # The token is fingerprinted, never stored.
        assert row.token_hash and spec.token not in (row.token_hash or "")


def test_resign_requires_configuration(app, client, admin_token, artifact_dir):
    created = _create_app(client, admin_token, resign_config=None)
    build = _upload(client, admin_token, created["id"], _aab(signed=False)).get_json()["data"]

    resp = client.post(
        f"/api/mobile-apps/builds/{build['id']}/resign", headers=auth_headers(admin_token)
    )
    assert resp.status_code == 409
    assert "re-signing setup" in resp.get_json()["error"].lower()


def test_resign_rejects_incomplete_configuration(app, client, admin_token, artifact_dir):
    partial = {"android": {"executor": "k8s_job", "cluster": "prod"}}
    created = _create_app(client, admin_token, resign_config=partial)
    build = _upload(client, admin_token, created["id"], _aab(signed=False)).get_json()["data"]

    resp = client.post(
        f"/api/mobile-apps/builds/{build['id']}/resign", headers=auth_headers(admin_token)
    )
    assert resp.status_code == 409
    error = resp.get_json()["error"]
    assert "namespace" in error and "image" in error and "keystoreSecret" in error


def test_resign_refuses_second_concurrent_job(app, client, admin_token, artifact_dir, monkeypatch):
    created = _create_app(client, admin_token)
    build = _upload(client, admin_token, created["id"], _aab(signed=False)).get_json()["data"]
    _fake_executor(monkeypatch)

    first = client.post(
        f"/api/mobile-apps/builds/{build['id']}/resign", headers=auth_headers(admin_token)
    )
    assert first.status_code == 202
    second = client.post(
        f"/api/mobile-apps/builds/{build['id']}/resign", headers=auth_headers(admin_token)
    )
    assert second.status_code == 409
    assert "already running" in second.get_json()["error"]


def test_ios_resign_is_refused(app, client, admin_token, artifact_dir):
    """iOS signing needs macOS and a keychain — KubeSight must not pretend
    otherwise, at config time or at request time."""
    resp = client.post(
        "/api/mobile-apps",
        json={
            "name": "iOS app",
            "resignConfig": {"ios": {"executor": "k8s_job", "cluster": "prod"}},
        },
        headers=auth_headers(admin_token),
    )
    assert resp.status_code == 400
    assert "macOS" in resp.get_json()["error"]


def test_resign_requires_manage_permission(app, client, admin_token, operator_token, artifact_dir):
    created = _create_app(client, admin_token)
    build = _upload(client, admin_token, created["id"], _aab(signed=False)).get_json()["data"]
    resp = client.post(
        f"/api/mobile-apps/builds/{build['id']}/resign", headers=auth_headers(operator_token)
    )
    assert resp.status_code == 403


# ---------------------------------------------------------------------------
# The signing job's callbacks
# ---------------------------------------------------------------------------

def _start(app, client, admin_token, monkeypatch, payload=None):
    created = _create_app(client, admin_token)
    build = _upload(
        client, admin_token, created["id"], payload if payload is not None else _aab(signed=False)
    ).get_json()["data"]
    seen = _fake_executor(monkeypatch)
    client.post(f"/api/mobile-apps/builds/{build['id']}/resign", headers=auth_headers(admin_token))
    with app.app_context():
        row = MobileAppResign.query.filter_by(build_id=build["id"]).first()
        return created, build, row.id, seen["spec"].token


def test_job_fetches_source_and_posts_signed_result(
    app, client, admin_token, artifact_dir, monkeypatch
):
    created, build, resign_id, token = _start(app, client, admin_token, monkeypatch)
    headers = {"Authorization": f"Bearer {token}"}

    # 1. The job pulls the unsigned binary.
    resp = client.get(f"/api/mobile-apps/resigns/{resign_id}/source", headers=headers)
    assert resp.status_code == 200
    assert resp.data == _aab(signed=False)

    # 2. It posts the signed one back.
    resp = client.post(
        f"/api/mobile-apps/resigns/{resign_id}/result",
        data={"file": (io.BytesIO(_aab(signed=True)), "app-release.aab")},
        content_type="multipart/form-data",
        headers=headers,
    )
    assert resp.status_code == 201, resp.get_json()
    child = resp.get_json()["data"]
    assert child["signatureState"] == "signed"
    assert child["source"] == "resign"
    assert child["parentBuildId"] == build["id"]
    assert child["status"] == "available"

    with app.app_context():
        row = MobileAppResign.query.get(resign_id)
        assert row.status == "completed"
        assert row.result_build_id == child["id"]
        assert [s["status"] for s in row.steps] == ["done"] * 5

    # 3. The signed child publishes; the unsigned parent still does not.
    for build_id, expected in ((child["id"], 202), (build["id"], 409)):
        monkeypatch.setattr(
            "api.services.google_play_client.access_token", lambda cfg: "tok"
        )
        monkeypatch.setattr("api.services.google_play_client.create_edit", lambda c, t: "e1")
        monkeypatch.setattr("api.services.google_play_client.upload_binary", lambda *a: 9)
        monkeypatch.setattr("api.services.google_play_client.assign_track", lambda *a: None)
        monkeypatch.setattr("api.services.google_play_client.commit_edit", lambda *a: None)
        resp = client.post(
            f"/api/mobile-apps/builds/{build_id}/publish",
            json={"store": "google_play", "target": "internal"},
            headers=auth_headers(admin_token),
        )
        assert resp.status_code == expected


def test_result_still_unsigned_fails_the_job(app, client, admin_token, artifact_dir, monkeypatch):
    """A signer that returns an unsigned binary has failed, however cleanly it
    exited — the dud must never become a publishable build."""
    _, build, resign_id, token = _start(app, client, admin_token, monkeypatch)

    resp = client.post(
        f"/api/mobile-apps/resigns/{resign_id}/result",
        data={"file": (io.BytesIO(_aab(signed=False)), "app-release.aab")},
        content_type="multipart/form-data",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 422
    assert "still unsigned" in resp.get_json()["error"].lower()

    with app.app_context():
        row = MobileAppResign.query.get(resign_id)
        assert row.status == "failed"
        assert row.result_build_id is None
        verify = [s for s in row.steps if s["key"] == "verify"][0]
        assert verify["status"] == "fail"
        # No available build was left behind for someone to publish.
        assert (
            MobileAppBuild.query.filter_by(source="resign", status="available").count() == 0
        )


def test_result_rejected_without_token(app, client, admin_token, artifact_dir, monkeypatch):
    _, _, resign_id, _ = _start(app, client, admin_token, monkeypatch)
    resp = client.post(
        f"/api/mobile-apps/resigns/{resign_id}/result",
        data={"file": (io.BytesIO(_aab(signed=True)), "app-release.aab")},
        content_type="multipart/form-data",
    )
    assert resp.status_code == 401


def test_user_access_token_cannot_post_a_result(
    app, client, admin_token, artifact_dir, monkeypatch
):
    """An ordinary session token must not be usable on the machine endpoints,
    and a signing token must not be usable anywhere else."""
    _, _, resign_id, token = _start(app, client, admin_token, monkeypatch)

    resp = client.post(
        f"/api/mobile-apps/resigns/{resign_id}/result",
        data={"file": (io.BytesIO(_aab(signed=True)), "app-release.aab")},
        content_type="multipart/form-data",
        headers=auth_headers(admin_token),
    )
    assert resp.status_code == 401

    # ...and the reverse: the signing token opens nothing else.
    resp = client.get("/api/mobile-apps", headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code in (401, 403)


def test_token_for_another_resign_is_rejected(app, client, admin_token, artifact_dir, monkeypatch):
    _, _, first_id, first_token = _start(app, client, admin_token, monkeypatch)
    _, _, second_id, _ = _start(app, client, admin_token, monkeypatch)
    assert first_id != second_id

    resp = client.get(
        f"/api/mobile-apps/resigns/{second_id}/source",
        headers={"Authorization": f"Bearer {first_token}"},
    )
    assert resp.status_code == 401


def test_result_accepted_only_once(app, client, admin_token, artifact_dir, monkeypatch):
    _, _, resign_id, token = _start(app, client, admin_token, monkeypatch)
    headers = {"Authorization": f"Bearer {token}"}
    body = {"file": (io.BytesIO(_aab(signed=True)), "app-release.aab")}

    first = client.post(
        f"/api/mobile-apps/resigns/{resign_id}/result",
        data=body,
        content_type="multipart/form-data",
        headers=headers,
    )
    assert first.status_code == 201

    second = client.post(
        f"/api/mobile-apps/resigns/{resign_id}/result",
        data={"file": (io.BytesIO(_aab(signed=True)), "app-release.aab")},
        content_type="multipart/form-data",
        headers=headers,
    )
    assert second.status_code == 409


# ---------------------------------------------------------------------------
# Failure handling on the tick
# ---------------------------------------------------------------------------

def test_failed_job_fails_the_resign(app, client, admin_token, artifact_dir, monkeypatch):
    _, _, resign_id, _ = _start(app, client, admin_token, monkeypatch)
    monkeypatch.setattr(
        resign_executor, "poll", lambda ref: {"phase": "failed", "detail": "BackoffLimitExceeded"}
    )
    monkeypatch.setattr(resign_executor, "logs", lambda ref, tail=40: "jarsigner: key not found")

    with app.app_context():
        svc.advance_mobile_resigns()
        row = MobileAppResign.query.get(resign_id)
        assert row.status == "failed"
        assert "BackoffLimitExceeded" in row.error
        # The pod's own words make it into the error, not just the Job's.
        assert "key not found" in row.error


def test_vanished_job_does_not_hang_forever(app, client, admin_token, artifact_dir, monkeypatch):
    _, _, resign_id, _ = _start(app, client, admin_token, monkeypatch)
    monkeypatch.setattr(
        resign_executor,
        "poll",
        lambda ref: {"phase": "failed", "detail": "the signing job is gone from the cluster"},
    )
    with app.app_context():
        svc.advance_mobile_resigns()
        assert MobileAppResign.query.get(resign_id).status == "failed"


# ---------------------------------------------------------------------------
# Manifest rendering
# ---------------------------------------------------------------------------

def test_rendered_manifests_keep_the_token_out_of_the_job():
    import yaml

    spec = resign_executor.ResignJobSpec(
        resign_id=7,
        build_id=3,
        artifact_type="aab",
        cluster="prod",
        namespace="kubesight",
        image="registry.local/signer:1",
        callback_url="http://backend-service:5000",
        token="super-secret-token",
        keystore_secret="android-upload-keystore",
    )
    docs = list(yaml.safe_load_all(resign_executor.render_manifests(spec)))
    secret, job = docs

    assert secret["kind"] == "Secret"
    assert secret["stringData"]["token"] == "super-secret-token"

    # The Job references the token by secretKeyRef — it must not be inlined,
    # or it would be readable in `kubectl get job -o yaml`.
    assert job["kind"] == "Job"
    assert "super-secret-token" not in yaml.safe_dump(job)
    env = {e["name"]: e for e in job["spec"]["template"]["spec"]["containers"][0]["env"]}
    assert env["RESIGN_TOKEN"]["valueFrom"]["secretKeyRef"]["name"] == "kubesight-resign-7"
    # Keystore passwords are pulled from the keystore Secret by reference too.
    assert env["STORE_PASS"]["valueFrom"]["secretKeyRef"]["name"] == "android-upload-keystore"

    # A signing failure is deterministic; retrying only muddies the logs.
    assert job["spec"]["backoffLimit"] == 0
    assert job["spec"]["activeDeadlineSeconds"] == resign_executor.JOB_DEADLINE_SECONDS

    pod = job["spec"]["template"]["spec"]
    assert pod["securityContext"]["runAsNonRoot"] is True
    container = pod["containers"][0]
    assert container["securityContext"]["readOnlyRootFilesystem"] is True
    mounts = {m["name"]: m for m in container["volumeMounts"]}
    assert mounts["keystore"]["readOnly"] is True
    # A read-only root filesystem still needs writable scratch for the JDK.
    assert "tmp" in mounts and "work" in mounts
