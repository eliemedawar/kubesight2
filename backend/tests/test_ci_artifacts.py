"""Artifact retention: what expires, what is protected, and who may clean.

The store is a real directory here (CI_ARTIFACT_DIR into tmp_path), because the
thing being tested is that files leave the disk — not that rows leave the table.
"""

from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone

import pytest

from api.db import db
from api.models_ci import CiArtifact, CiService
from api.services.ci import artifacts as artifacts_service
from tests.conftest import auth_headers


@pytest.fixture()
def store(tmp_path, monkeypatch):
    monkeypatch.setenv("CI_ARTIFACT_DIR", str(tmp_path / "artifacts"))
    monkeypatch.delenv("CI_ARTIFACT_RETENTION_DAYS", raising=False)
    monkeypatch.delenv("CI_ARTIFACT_KEEP_LAST", raising=False)
    monkeypatch.delenv("CI_ARTIFACT_AUTOCLEAN", raising=False)
    return str(tmp_path / "artifacts")


def _service(name="Payment Service", slug="payment-service") -> CiService:
    row = CiService(name=name, slug=slug, application_type="java")
    db.session.add(row)
    db.session.commit()
    return row


def _artifact(service, *, build_id, name, age_days=0, backend="local", size=1024):
    """One artifact row, with its bytes on disk when it is locally stored."""
    ref = f"{service.id}/{build_id}/{name}"
    if backend == "local":
        path = os.path.join(artifacts_service.artifact_root(), ref)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "wb") as handle:
            handle.write(b"x" * size)
    row = CiArtifact(
        service_id=service.id,
        build_id=build_id,
        artifact_type="jar" if backend == "local" else "container-image",
        name=name,
        storage_backend=backend,
        storage_ref=ref if backend == "local" else None,
        uri=None if backend == "local" else f"nexus:9443/{service.slug}:1.0",
        size_bytes=size,
        created_at=datetime.now(timezone.utc) - timedelta(days=age_days),
    )
    db.session.add(row)
    db.session.commit()
    return row


def _exists(row) -> bool:
    return os.path.isfile(os.path.join(artifacts_service.artifact_root(), row.storage_ref))


# ---------------------------------------------------------------------------
# What expires
# ---------------------------------------------------------------------------

def test_a_days_old_artifact_expires_and_a_fresh_one_does_not(app, store):
    with app.app_context():
        service = _service()
        old = _artifact(service, build_id=1, name="old.jar", age_days=3)
        fresh = _artifact(service, build_id=9, name="fresh.jar", age_days=0)

        result = artifacts_service.purge()

        assert result["deleted"] == 1
        assert result["freedBytes"] == 1024
        assert db.session.get(CiArtifact, old.id) is None
        assert db.session.get(CiArtifact, fresh.id) is not None
        assert not os.path.isfile(os.path.join(store, f"{service.id}/1/old.jar"))
        assert os.path.isfile(os.path.join(store, f"{service.id}/9/fresh.jar"))


def test_the_newest_builds_artifacts_survive_however_old_they_are(app, store):
    """"Rerun from here" restores from exactly these files, so a service that
    builds rarely must not be left with nothing to rerun."""
    with app.app_context():
        service = _service()
        ancient = _artifact(service, build_id=1, name="a.jar", age_days=40)
        newest = _artifact(service, build_id=2, name="b.jar", age_days=30)

        result = artifacts_service.purge()

        assert result["deleted"] == 1
        assert result["keptRecent"] == 1
        assert db.session.get(CiArtifact, ancient.id) is None
        assert db.session.get(CiArtifact, newest.id) is not None


def test_a_store_that_is_all_one_build_reports_why_it_cleaned_nothing(app, store):
    """The reported case: every file belongs to the newest build, so an
    expiry sweep deletes nothing however old they are. It has to say that it
    protected them rather than that they were too young, or the cleanup looks
    broken to anyone reading the message."""
    with app.app_context():
        service = _service()
        _artifact(service, build_id=20, name="a.jar", age_days=3)
        _artifact(service, build_id=20, name="b.jar", age_days=3)

        result = artifacts_service.purge()

        assert result["deleted"] == 0
        assert result["keptRecent"] == 2
        assert result["keptYoung"] == 0


def test_artifacts_inside_the_retention_window_are_counted_as_young(app, store):
    with app.app_context():
        service = _service()
        _artifact(service, build_id=1, name="a.jar", age_days=0)
        _artifact(service, build_id=2, name="b.jar", age_days=0)

        result = artifacts_service.purge()

        assert result["deleted"] == 0
        # Age is checked first, so both count as young rather than protected.
        assert result["keptYoung"] == 2
        assert result["keptRecent"] == 0


def test_the_guard_can_be_turned_off(app, store, monkeypatch):
    monkeypatch.setenv("CI_ARTIFACT_KEEP_LAST", "0")
    with app.app_context():
        service = _service()
        _artifact(service, build_id=1, name="a.jar", age_days=40)
        _artifact(service, build_id=2, name="b.jar", age_days=30)

        assert artifacts_service.purge()["deleted"] == 2


def test_container_images_are_never_touched(app, store):
    """The row is metadata pointing at a registry: deleting it frees no disk and
    loses the record of what was built."""
    with app.app_context():
        service = _service()
        image = _artifact(service, build_id=1, name="profile-ms:1.0", age_days=99, backend="registry")

        result = artifacts_service.purge()

        assert result["deleted"] == 0
        assert db.session.get(CiArtifact, image.id) is not None
        assert artifacts_service.policy()["registryOnly"] == 1


def test_zero_days_and_no_guard_means_everything_in_scope(app, store):
    with app.app_context():
        service = _service()
        other = _service("Ledger UI", "ledger-ui")
        _artifact(service, build_id=1, name="a.jar", age_days=0)
        _artifact(service, build_id=2, name="b.jar", age_days=0)
        theirs = _artifact(other, build_id=1, name="c.jar", age_days=0)

        result = artifacts_service.purge(
            service_id=service.id, older_than_days=0, keep_last=0
        )

        assert result["deleted"] == 2
        # Scoped to the service asked for; another service's cache is not ours
        # to reclaim.
        assert db.session.get(CiArtifact, theirs.id) is not None


def test_deleting_one_artifact_removes_its_file(app, store):
    with app.app_context():
        service = _service()
        row = _artifact(service, build_id=1, name="a.jar", age_days=0, size=2048)
        path = os.path.join(store, f"{service.id}/1/a.jar")
        assert os.path.isfile(path)

        assert artifacts_service.delete_artifact(row) == 2048
        assert not os.path.isfile(path)
        # And the directories it left behind go with the next sweep.
        artifacts_service.purge()
        assert not os.path.isdir(os.path.dirname(path))


# ---------------------------------------------------------------------------
# The automatic sweep
# ---------------------------------------------------------------------------

def test_the_sweep_runs_once_per_interval(app, store):
    with app.app_context():
        service = _service()
        _artifact(service, build_id=1, name="a.jar", age_days=5)
        _artifact(service, build_id=2, name="b.jar", age_days=5)

        assert artifacts_service.purge_due() is True
        assert artifacts_service.run_due_purge() is True
        # b.jar is build 2, the newest, so one row survives the first sweep.
        assert CiArtifact.query.count() == 1
        # Immediately after, it is not due again: the marker says so.
        assert artifacts_service.purge_due() is False
        assert artifacts_service.run_due_purge() is False
        assert artifacts_service.last_purge_at() is not None


def test_the_sweep_can_be_turned_off_two_ways(app, store, monkeypatch):
    with app.app_context():
        monkeypatch.setenv("CI_ARTIFACT_AUTOCLEAN", "false")
        assert artifacts_service.purge_due() is False

        monkeypatch.setenv("CI_ARTIFACT_AUTOCLEAN", "true")
        # Keeping artifacts forever is the other way of saying "never sweep".
        monkeypatch.setenv("CI_ARTIFACT_RETENTION_DAYS", "0")
        assert artifacts_service.retention_days() == 0
        assert artifacts_service.purge_due() is False
        assert artifacts_service.policy()["autoclean"] is False


def test_retention_days_is_configurable(app, store, monkeypatch):
    monkeypatch.setenv("CI_ARTIFACT_RETENTION_DAYS", "7")
    with app.app_context():
        service = _service()
        _artifact(service, build_id=1, name="a.jar", age_days=3)
        _artifact(service, build_id=2, name="b.jar", age_days=10)
        # Build 2 is the newest AND old enough — the guard wins, so nothing goes.
        assert artifacts_service.purge()["deleted"] == 0
        assert artifacts_service.purge(keep_last=0)["deleted"] == 1


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

def test_the_policy_route_states_the_rule_and_the_usage(app, client, admin_token, store):
    with app.app_context():
        service = _service()
        _artifact(service, build_id=1, name="a.jar", age_days=0, size=4096)
        service_id = service.id

    body = client.get(
        f"/api/ci/artifacts/policy?serviceId={service_id}", headers=auth_headers(admin_token)
    ).get_json()["data"]
    assert body["retentionDays"] == 1
    assert body["keepLastBuilds"] == 1
    assert body["usage"] == {"count": 1, "bytes": 4096}


def test_purge_and_delete_need_more_than_view(app, client, viewer_token, admin_token, store):
    with app.app_context():
        service = _service()
        row = _artifact(service, build_id=1, name="a.jar", age_days=0)
        service_id, artifact_id = service.id, row.id

    # A viewer can look at artifacts, and that is all.
    assert client.get("/api/ci/artifacts/policy", headers=auth_headers(viewer_token)).status_code == 200
    assert (
        client.post(
            "/api/ci/artifacts/purge",
            json={"serviceId": service_id},
            headers=auth_headers(viewer_token),
        ).status_code
        == 403
    )
    assert (
        client.delete(
            f"/api/ci/artifacts/{artifact_id}", headers=auth_headers(viewer_token)
        ).status_code
        == 403
    )

    deleted = client.delete(
        f"/api/ci/artifacts/{artifact_id}", headers=auth_headers(admin_token)
    )
    assert deleted.status_code == 200
    assert deleted.get_json()["data"]["deleted"] is True


def test_purge_route_can_clean_everything_for_one_service(app, client, admin_token, store):
    with app.app_context():
        service = _service()
        _artifact(service, build_id=1, name="a.jar", age_days=0)
        _artifact(service, build_id=2, name="b.jar", age_days=0)
        service_id = service.id

    body = client.post(
        "/api/ci/artifacts/purge",
        json={"serviceId": service_id, "olderThanDays": 0, "keepLast": 0},
        headers=auth_headers(admin_token),
    ).get_json()["data"]
    assert body["deleted"] == 2
    assert body["usage"]["count"] == 0
