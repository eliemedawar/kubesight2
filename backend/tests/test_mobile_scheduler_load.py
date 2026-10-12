"""What the mobile scheduler tick does, and no longer does, on its own thread.

* Re-signing hands the upload of the binary to a worker: the tick only marks
  the run as launching; a launch orphaned by a restart is failed by the sweep.
* Jenkins / App Store Connect are asked about a row at most once a minute, a
  "still processing" answer is written only when it changes, and Apple gets a
  bounded time to process an upload.
* The Mobile Apps list counts builds and publishes in a fixed number of queries.
* A CI artifact lands in the mobile store by hard link (or one copy-and-hash pass).
"""

from __future__ import annotations

import hashlib
import io
import os
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from sqlalchemy import event

from api.db import db
from api.models import MobileAppBuild, MobileApplication, MobileAppPublish, MobileAppResign
from api.services import app_store_client
from api.services import mobile_app_service as svc
from tests.conftest import auth_headers
from tests.test_mobile_resign import (  # noqa: F401  (fixture used by name)
    _aab,
    _create_app,
    _fake_jenkins,
    _upload,
    artifact_dir,
)


@contextmanager
def _statements():
    statements: list[str] = []

    def before_cursor_execute(conn, cursor, statement, parameters, context, executemany):
        statements.append(statement)

    event.listen(db.engine, "before_cursor_execute", before_cursor_execute)
    try:
        yield statements
    finally:
        event.remove(db.engine, "before_cursor_execute", before_cursor_execute)


# ---------------------------------------------------------------------------
# Re-sign launch on a worker
# ---------------------------------------------------------------------------

def _queued_resign(app, client, admin_token, monkeypatch):
    created = _create_app(client, admin_token)
    build = _upload(client, admin_token, created["id"], _aab(signed=False)).get_json()["data"]
    seen = _fake_jenkins(monkeypatch)
    held = []
    # A worker pool that takes the work but has not run it yet.
    monkeypatch.setattr(svc, "_try_dispatch", lambda work: held.append(work) or True)
    resp = client.post(f"/api/mobile-apps/builds/{build['id']}/resign", headers=auth_headers(admin_token))
    assert resp.status_code == 202, resp.get_json()
    with app.app_context():
        row = MobileAppResign.query.filter_by(build_id=build["id"]).first()
        return row.id, held, seen


def test_the_tick_does_not_upload_the_binary(app, client, admin_token, artifact_dir, monkeypatch):
    resign_id, held, seen = _queued_resign(app, client, admin_token, monkeypatch)
    # The request's kick marked it and handed it over; nothing was uploaded.
    assert "uploaded" not in seen and len(held) == 1
    with app.app_context():
        row = MobileAppResign.query.get(resign_id)
        assert row.status == "running" and row.job_ref == {"launching": True}
        steps = {s["key"]: s for s in row.steps}
        assert steps["launch"]["status"] == "run"
        # Another tick neither re-dispatches nor polls a launch in flight.
        svc.advance_mobile_resigns()
        assert len(held) == 1
        assert MobileAppResign.query.get(resign_id).job_ref == {"launching": True}

        held[0](app)  # the worker runs
        row = MobileAppResign.query.get(resign_id)
        assert row.status == "running"
        assert row.job_ref["queueUrl"]
        assert {s["key"]: s["status"] for s in row.steps}["launch"] == "done"
    assert "uploaded" in seen


def test_a_launch_orphaned_by_a_restart_fails(app, client, admin_token, artifact_dir, monkeypatch):
    resign_id, held, _ = _queued_resign(app, client, admin_token, monkeypatch)
    with app.app_context():
        MobileAppResign.query.filter_by(id=resign_id).update(
            {"updated_at": datetime.now(timezone.utc) - timedelta(hours=2)}
        )
        db.session.commit()
        svc.advance_mobile_resigns()
        row = MobileAppResign.query.get(resign_id)
        assert row.status == "failed"
        assert "interrupted" in row.error
        # The abandoned worker, if it was alive after all, changes nothing.
        held[0](app)
        assert MobileAppResign.query.get(resign_id).status == "failed"


def test_no_free_worker_puts_the_run_back_in_the_queue(app, client, admin_token, artifact_dir, monkeypatch):
    created = _create_app(client, admin_token)
    build = _upload(client, admin_token, created["id"], _aab(signed=False)).get_json()["data"]
    _fake_jenkins(monkeypatch)
    monkeypatch.setattr(svc, "_try_dispatch", lambda work: False)
    client.post(f"/api/mobile-apps/builds/{build['id']}/resign", headers=auth_headers(admin_token))
    with app.app_context():
        row = MobileAppResign.query.filter_by(build_id=build["id"]).first()
        assert row.status == "queued" and row.job_ref is None


# ---------------------------------------------------------------------------
# Throttled polls
# ---------------------------------------------------------------------------

def test_a_row_is_polled_at_most_once_a_minute(monkeypatch):
    monkeypatch.setattr(svc, "_poll_throttled", lambda: True)
    monkeypatch.setattr(svc, "_next_poll", {})
    assert svc._poll_due("publish", 7) is True
    assert svc._poll_due("publish", 7) is False
    assert svc._poll_due("publish", 8) is True  # per row
    assert svc._poll_due("resign", 7) is True  # per kind
    svc._next_poll[("publish", 7)] = 0.0  # a minute later
    assert svc._poll_due("publish", 7) is True


def _processing_publish(app, *, since: datetime, detail: str = "crunching") -> int:
    with app.app_context():
        mobile = MobileApplication(
            name="iOS app", ios_bundle_id="com.example.ios", asc_issuer_id="iss",
            asc_key_id="KEY", asc_app_id="999",
        )
        db.session.add(mobile)
        db.session.flush()
        build = MobileAppBuild(
            app_id=mobile.id, platform="ios", artifact_type="ipa", file_name="a.ipa",
            status="available", version="1.0",
        )
        db.session.add(build)
        db.session.flush()
        pub = MobileAppPublish(
            app_id=mobile.id, build_id=build.id, store="app_store", target="testflight",
            status="processing",
            steps=[{"key": "upload", "status": "done", "detail": "", "at": since.isoformat()},
                   {"key": "release", "status": "run", "detail": detail, "at": since.isoformat()}],
            store_ref={"buildUploadId": "u1", "processingSince": since.isoformat()},
        )
        db.session.add(pub)
        db.session.commit()
        return pub.id


def test_apple_gets_a_bounded_time_to_process(app, monkeypatch):
    monkeypatch.setattr(
        app_store_client, "processing_state",
        lambda cfg, ref, version: {"state": "processing", "detail": "crunching"},
    )
    pub_id = _processing_publish(app, since=datetime.now(timezone.utc) - timedelta(hours=3))
    with app.app_context():
        svc.advance_mobile_publishes()
        pub = MobileAppPublish.query.get(pub_id)
        assert pub.status == "failed"
        assert "still processing the build after 2 h" in pub.error


def test_an_unchanged_answer_is_not_written_again(app, monkeypatch):
    monkeypatch.setattr(
        app_store_client, "processing_state",
        lambda cfg, ref, version: {"state": "processing", "detail": "crunching"},
    )
    pub_id = _processing_publish(app, since=datetime.now(timezone.utc) - timedelta(minutes=5))
    with app.app_context():
        before = MobileAppPublish.query.get(pub_id).updated_at
        with _statements() as statements:
            svc.advance_mobile_publishes()
        assert not any(
            s.lstrip().upper().startswith("UPDATE MOBILE_APP_PUBLISHES") for s in statements
        ), statements
        pub = MobileAppPublish.query.get(pub_id)
        assert pub.status == "processing" and pub.updated_at == before

        monkeypatch.setattr(
            app_store_client, "processing_state",
            lambda cfg, ref, version: {"state": "processing", "detail": "VALID soon"},
        )
        svc.advance_mobile_publishes()
        steps = {s["key"]: s for s in MobileAppPublish.query.get(pub_id).steps}
        assert steps["release"]["detail"] == "VALID soon"


# ---------------------------------------------------------------------------
# The Mobile Apps list
# ---------------------------------------------------------------------------

def _seed_apps(app, count: int, start: int) -> None:
    with app.app_context():
        for index in range(start, start + count):
            mobile = MobileApplication(name=f"App {index:03d}", zoho_environment=f"env-{index}")
            db.session.add(mobile)
            db.session.flush()
            for number in range(3):
                build = MobileAppBuild(
                    app_id=mobile.id, platform="android", artifact_type="apk",
                    file_name=f"b{number}.apk", status="available", version=f"1.{number}",
                )
                db.session.add(build)
                db.session.flush()
                db.session.add(MobileAppPublish(
                    app_id=mobile.id, build_id=build.id, store="google_play", status="published",
                ))
        db.session.commit()


def test_the_list_counts_in_a_fixed_number_of_queries(app, client, admin_token):
    _seed_apps(app, 2, 0)
    client.get("/api/mobile-apps", headers=auth_headers(admin_token))
    with _statements() as few:
        client.get("/api/mobile-apps", headers=auth_headers(admin_token))
    _seed_apps(app, 6, 2)
    with _statements() as many:
        resp = client.get("/api/mobile-apps", headers=auth_headers(admin_token))
    assert resp.status_code == 200
    items = resp.get_json()["data"]["items"]
    assert len(items) == 8
    assert len(many) == len(few), "\n".join(many)
    first = next(item for item in items if item["name"] == "App 000")
    assert first["buildCount"] == 3 and first["publishCount"] == 3
    assert first["latestBuild"]["version"] == "1.2"


# ---------------------------------------------------------------------------
# CI artifact into the mobile store
# ---------------------------------------------------------------------------

def test_a_ci_artifact_is_linked_and_its_recorded_checksum_reused(tmp_path):
    source = tmp_path / "ci" / "app.aab"
    source.parent.mkdir()
    payload = b"x" * 5000
    source.write_bytes(payload)
    store = SimpleNamespace(_absolute=lambda artifact: str(source))
    artifact = SimpleNamespace(checksum_sha256="c" * 64, size_bytes=len(payload))
    dest = tmp_path / "mobile.aab"
    with open(source, "rb") as stream:
        sha, size = svc._place_ci_artifact(store, artifact, stream, str(dest))
    assert dest.read_bytes() == payload and size == len(payload)
    if os.stat(dest).st_nlink > 1:  # linked: the CI store's checksum is trusted
        assert sha == "c" * 64
    else:  # pragma: no cover - filesystems without hard links
        assert sha == hashlib.sha256(payload).hexdigest()


def test_without_a_link_the_copy_is_hashed_in_the_same_pass(tmp_path):
    payload = b"y" * (3 * 1024 * 1024 + 17)
    store = SimpleNamespace()  # no local path to link from
    artifact = SimpleNamespace(checksum_sha256=None, size_bytes=None)
    dest = tmp_path / "copy.ipa"
    sha, size = svc._place_ci_artifact(store, artifact, io.BytesIO(payload), str(dest))
    assert dest.read_bytes() == payload
    assert (sha, size) == (hashlib.sha256(payload).hexdigest(), len(payload))
