"""Downloading a file from a browser.

The bug this exists to prevent: the Artifacts tab's Download link was a plain
``<a href>``. A browser navigation sends no Authorization header, KubeSight's
session is a bearer token in JavaScript rather than a cookie, so every download
arrived anonymous, was refused, and Chrome saved the 401 JSON body as
``download.json`` under the message "Try to sign in to the site."

The fix is a download ticket: a short-lived token naming ONE resource, which
travels in the URL where a navigation can carry it. Most of what is asserted
here is what that ticket must NOT be able to do.
"""

from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone

import jwt
import pytest

from api import auth_utils
from api.db import db
from api.models_ci import CiArtifact, CiService
from api.services.ci import artifacts as artifacts_service
from tests.conftest import auth_headers

PAYLOAD = b"pretend this is a 197MB jar"


@pytest.fixture()
def store(tmp_path, monkeypatch):
    monkeypatch.setenv("CI_ARTIFACT_DIR", str(tmp_path / "artifacts"))
    return str(tmp_path / "artifacts")


def _service():
    row = CiService.query.filter_by(slug="test123").first()
    if row is None:
        row = CiService(name="Test123", slug="test123", application_type="java")
        db.session.add(row)
        db.session.commit()
    return row


def _artifact(name="app.jar", backend="local"):
    service = _service()

    ref = f"{service.id}/6/{name}"
    if backend == "local":
        path = os.path.join(artifacts_service.artifact_root(), ref)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "wb") as handle:
            handle.write(PAYLOAD)
    row = CiArtifact(
        service_id=service.id,
        build_id=6,
        artifact_type="jar",
        name=name,
        storage_backend=backend,
        storage_ref=ref if backend == "local" else None,
        uri=None if backend == "local" else "nexus:9443/test123:1.0",
        size_bytes=len(PAYLOAD),
        created_at=datetime.now(timezone.utc),
    )
    db.session.add(row)
    db.session.commit()
    return row


def _ticket(client, token, artifact_id):
    response = client.post(
        f"/api/ci/artifacts/{artifact_id}/download-ticket", headers=auth_headers(token)
    )
    assert response.status_code == 200, response.get_json()
    return response.get_json()["data"]["ticket"]


# ---------------------------------------------------------------------------
# The bug itself
# ---------------------------------------------------------------------------

def test_a_download_with_no_credential_at_all_is_still_refused(app, client, store):
    """The ticket must not have opened the door for everyone. This is the
    request the old <a href> made, and it must keep failing."""
    with app.app_context():
        row = _artifact()
        artifact_id = row.id

    response = client.get(f"/api/ci/artifacts/{artifact_id}/download")
    assert response.status_code == 401


def test_a_ticket_downloads_the_file_with_no_authorization_header(app, client, admin_token, store):
    """The whole point: a browser navigation, carrying only the URL."""
    with app.app_context():
        row = _artifact()
        artifact_id = row.id

    ticket = _ticket(client, admin_token, artifact_id)
    response = client.get(f"/api/ci/artifacts/{artifact_id}/download?ticket={ticket}")

    assert response.status_code == 200
    assert response.data == PAYLOAD
    # Content-Disposition is what makes the browser save it rather than render
    # it, and what names the file — the client deliberately sends no `download`
    # attribute so this wins.
    assert "attachment" in response.headers["Content-Disposition"]
    assert "app.jar" in response.headers["Content-Disposition"]


def test_the_ordinary_bearer_token_still_works(app, client, admin_token, store):
    """A script or the API has a header and should not need a ticket."""
    with app.app_context():
        row = _artifact()
        artifact_id = row.id

    response = client.get(
        f"/api/ci/artifacts/{artifact_id}/download", headers=auth_headers(admin_token)
    )
    assert response.status_code == 200
    assert response.data == PAYLOAD


# ---------------------------------------------------------------------------
# What a ticket must not be able to do
# ---------------------------------------------------------------------------

def test_a_ticket_for_one_artifact_cannot_fetch_another(app, client, admin_token, store):
    """Otherwise the ticket is a general read credential with a short life,
    which is a much bigger thing than it looks."""
    with app.app_context():
        first = _artifact(name="app.jar").id
        second = _artifact(name="issuing-1.75.1.jar").id

    ticket = _ticket(client, admin_token, first)
    response = client.get(f"/api/ci/artifacts/{second}/download?ticket={ticket}")
    assert response.status_code == 401


def test_a_ticket_is_not_accepted_as_a_bearer_token(app, client, admin_token, store):
    """It is a valid JWT signed with the same secret. The `purpose` claim is the
    only thing stopping it from being a session, so check that it does."""
    with app.app_context():
        artifact_id = _artifact().id

    ticket = _ticket(client, admin_token, artifact_id)
    response = client.get("/api/ci/services", headers=auth_headers(ticket))
    assert response.status_code == 401


def test_a_ticket_cannot_reach_a_write_endpoint(app, client, admin_token, store):
    with app.app_context():
        artifact_id = _artifact().id

    ticket = _ticket(client, admin_token, artifact_id)
    response = client.delete(
        f"/api/ci/artifacts/{artifact_id}", headers=auth_headers(ticket)
    )
    assert response.status_code == 401


def test_an_expired_ticket_is_refused(app, client, admin_token, store):
    with app.app_context():
        artifact_id = _artifact().id
        user = auth_utils.User.query.filter_by(username="admin").first()
        stale = datetime.now(timezone.utc) - timedelta(minutes=5)
        expired = jwt.encode(
            {
                "sub": str(user.id),
                "username": user.username,
                "purpose": auth_utils.PURPOSE_DOWNLOAD,
                "res": f"ci-artifact:{artifact_id}",
                "iat": stale,
                "exp": stale + timedelta(seconds=120),
            },
            auth_utils._jwt_secret(),
            algorithm="HS256",
        )

    response = client.get(f"/api/ci/artifacts/{artifact_id}/download?ticket={expired}")
    assert response.status_code == 401


def test_a_forged_ticket_is_refused(app, client, store):
    """Signed with the wrong secret — the whole scheme rests on this."""
    with app.app_context():
        artifact_id = _artifact().id
        now = datetime.now(timezone.utc)
        forged = jwt.encode(
            {
                "sub": "1",
                "username": "admin",
                "purpose": auth_utils.PURPOSE_DOWNLOAD,
                "res": f"ci-artifact:{artifact_id}",
                "iat": now,
                "exp": now + timedelta(seconds=120),
            },
            "not-the-real-secret",
            algorithm="HS256",
        )

    response = client.get(f"/api/ci/artifacts/{artifact_id}/download?ticket={forged}")
    assert response.status_code == 401


def test_minting_a_ticket_needs_the_view_permission(app, client, store):
    """A ticket authenticates; it does not authorize. Nobody may mint one for a
    thing they could not have read anyway."""
    with app.app_context():
        artifact_id = _artifact().id

    response = client.post(f"/api/ci/artifacts/{artifact_id}/download-ticket")
    assert response.status_code == 401


def test_the_permission_is_checked_again_when_the_file_is_served(
    app, client, admin_token, store, monkeypatch
):
    """The ticket says WHO, the permission check says WHETHER — and it runs on
    the download itself, not only at mint time."""
    with app.app_context():
        artifact_id = _artifact().id

    ticket = _ticket(client, admin_token, artifact_id)

    from api import decorators

    monkeypatch.setattr(decorators, "user_has_permission", lambda user, key: False)
    response = client.get(f"/api/ci/artifacts/{artifact_id}/download?ticket={ticket}")
    assert response.status_code == 403


# ---------------------------------------------------------------------------
# Minting
# ---------------------------------------------------------------------------

def test_no_ticket_for_an_artifact_that_is_not_there(app, client, admin_token, store):
    """The 404 belongs at mint time, not halfway into a download."""
    response = client.post(
        "/api/ci/artifacts/9999/download-ticket", headers=auth_headers(admin_token)
    )
    assert response.status_code == 404


def test_no_ticket_for_an_image_that_lives_in_a_registry(app, client, admin_token, store):
    """A container image has no bytes here to hand over; the UI hides the button
    and the API says why rather than minting a ticket that cannot be spent."""
    with app.app_context():
        artifact_id = _artifact(name="test123:1.0", backend="registry").id

    response = client.post(
        f"/api/ci/artifacts/{artifact_id}/download-ticket", headers=auth_headers(admin_token)
    )
    assert response.status_code == 400
    assert "registry" in response.get_json()["error"].lower()


def test_the_download_is_audited_as_the_person_who_minted_the_ticket(
    app, client, admin_token, store
):
    """`get_current_user` reads the header and only the header, so without the
    g.download_actor fallback every browser download is logged as nobody."""
    with app.app_context():
        artifact_id = _artifact().id

    ticket = _ticket(client, admin_token, artifact_id)
    assert client.get(f"/api/ci/artifacts/{artifact_id}/download?ticket={ticket}").status_code == 200

    with app.app_context():
        from api.models import AuditLog

        entry = (
            AuditLog.query.filter_by(action="ci_artifact_downloaded")
            .order_by(AuditLog.id.desc())
            .first()
        )
        assert entry is not None
        admin = auth_utils.User.query.filter_by(username="admin").first()
        assert entry.actor_user_id == admin.id


# ---------------------------------------------------------------------------
# Stage logs, which had the same bug
# ---------------------------------------------------------------------------

def _build_with_a_stage():
    from api.models_ci import CiBuild, CiBuildStage

    service = _service()
    build = CiBuild(service_id=service.id, number=6, status="success")
    db.session.add(build)
    db.session.commit()
    stage = CiBuildStage(
        build_id=build.id, name="Build JAR", stage_type="command", position=0, status="success"
    )
    db.session.add(stage)
    db.session.commit()
    return build, stage


def test_a_stage_log_downloads_through_a_ticket_too(app, client, admin_token):
    with app.app_context():
        build, stage = _build_with_a_stage()
        build_id, stage_id = build.id, stage.id

    response = client.post(
        f"/api/ci/builds/{build_id}/stages/{stage_id}/logs/download-ticket",
        headers=auth_headers(admin_token),
    )
    assert response.status_code == 200
    ticket = response.get_json()["data"]["ticket"]

    response = client.get(
        f"/api/ci/builds/{build_id}/stages/{stage_id}/logs/download?ticket={ticket}"
    )
    assert response.status_code == 200
    assert "attachment" in response.headers["Content-Disposition"]


def test_an_artifact_ticket_cannot_fetch_a_stage_log(app, client, admin_token, store):
    """Different resource namespaces, so one kind of ticket cannot be spent on
    the other even when the numbers line up."""
    with app.app_context():
        build, stage = _build_with_a_stage()
        build_id, stage_id = build.id, stage.id
        artifact_id = _artifact().id

    ticket = _ticket(client, admin_token, artifact_id)
    response = client.get(
        f"/api/ci/builds/{build_id}/stages/{stage_id}/logs/download?ticket={ticket}"
    )
    assert response.status_code == 401
