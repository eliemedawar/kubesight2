"""CI lists load their rows in a fixed number of statements.

The builds list, the stage matrix and the Pipelines page are all polled every
few seconds while somebody watches them. Each used to ask the database a few
things per row (a build's stages, its requester and that user's collections,
a home's builds and users); the counts here must not grow with the rows.
"""

from __future__ import annotations

from datetime import datetime, timezone

from api.db import db
from api.models_ci import CiBuild, CiBuildStage
from tests.conftest import auth_headers
from tests.test_list_query_counts import _ci_service, _get, _user


def _add_running(service_id, start, count):
    for number in range(start, start + count):
        build = CiBuild(
            service_id=service_id,
            number=number,
            status="running",
            requested_by_user_id=_user().id,
            queued_at=datetime.now(timezone.utc),
        )
        db.session.add(build)
        db.session.flush()
        for position in range(3):
            db.session.add(
                CiBuildStage(
                    build_id=build.id,
                    name=f"s{position}",
                    position=position,
                    status="running" if position == 0 else "pending",
                )
            )
    db.session.commit()


def test_the_builds_list_does_not_query_per_build(client, admin_token):
    small = _ci_service(client, admin_token, builds=3)
    _, few = _get(client, admin_token, f"/api/ci/services/{small}/builds")

    large = _ci_service(client, admin_token, builds=30)
    _add_running(large, 31, 10)
    data, many = _get(client, admin_token, f"/api/ci/services/{large}/builds")

    assert many == few
    assert len(data["items"]) == 40
    assert all(item["requestedBy"] for item in data["items"])
    assert any(item["currentStage"] == "s0" for item in data["items"])

    _, everything = _get(client, admin_token, "/api/ci/builds")
    assert everything <= few + 1


def test_the_stage_matrix_does_not_query_per_build(client, admin_token):
    small = _ci_service(client, admin_token, builds=3)
    _, few = _get(client, admin_token, f"/api/ci/services/{small}/stage-matrix?limit=30")
    large = _ci_service(client, admin_token, builds=30)
    data, many = _get(client, admin_token, f"/api/ci/services/{large}/stage-matrix?limit=30")
    assert many == few
    assert len(data["rows"]) == 30


def test_the_pipelines_page_does_not_query_per_pipeline(client, admin_token):
    for index in range(2):
        response = client.post(
            "/api/ci/shared-pipelines",
            json={"name": f"Counted {index}"},
            headers=auth_headers(admin_token),
        )
        assert response.status_code in (200, 201), response.get_json()
    _, few = _get(client, admin_token, "/api/ci/shared-pipelines")
    for index in range(2, 12):
        client.post(
            "/api/ci/shared-pipelines",
            json={"name": f"Counted {index}"},
            headers=auth_headers(admin_token),
        )
    data, many = _get(client, admin_token, "/api/ci/shared-pipelines")
    assert many == few
    assert data["count"] == 12
    assert all(item["usedByCount"] == 0 for item in data["items"])


def test_the_catalog_sparkline_is_newest_first_per_service(client, admin_token):
    service_id = _ci_service(client, admin_token, builds=14)
    data, _ = _get(client, admin_token, "/api/ci/services")
    item = next(row for row in data["items"] if row["id"] == service_id)
    # _ci_service fails every third build: numbers 14..5, newest first.
    expected = ["failed" if number % 3 == 0 else "success" for number in range(14, 4, -1)]
    assert item["recentBuildStatuses"] == expected
    assert item["latestBuild"]["number"] == 14
    assert item["latestArtifact"]["name"] == "app-14.jar"
