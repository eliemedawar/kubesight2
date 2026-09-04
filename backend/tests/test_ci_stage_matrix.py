"""The stage matrix: column alignment across edited pipelines, and statistics.

Builds are written straight to the tables here rather than driven through the
engine: the point under test is how a *history* of differently shaped builds is
aligned into one grid, which is tedious to produce through the scheduler and
trivial to state directly.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from api.db import db
from api.models_ci import CiBuild, CiBuildStage, CiLogChunk
from api.services.ci import stage_matrix as matrix_service
from tests.conftest import auth_headers


@pytest.fixture()
def service_id(client, admin_token):
    return client.post(
        "/api/ci/services",
        json={"name": "Profile MS", "applicationType": "java"},
        headers=auth_headers(admin_token),
    ).get_json()["data"]["id"]


def make_build(service_id, number, status, stages, *, snapshot_stages=None, branch="main"):
    """One build row plus its stages. ``stages`` is (name, status, duration)."""
    build = CiBuild(
        service_id=service_id,
        number=number,
        status=status,
        trigger_type="manual",
        branch=branch,
        commit_sha=f"{number:08x}",
        pipeline_snapshot={
            "stages": snapshot_stages
            if snapshot_stages is not None
            else [{"name": name} for name, _, _ in stages]
        },
    )
    db.session.add(build)
    db.session.flush()
    for position, (name, stage_status, duration) in enumerate(stages):
        db.session.add(
            CiBuildStage(
                build_id=build.id,
                position=position,
                name=name,
                stage_type="command",
                status=stage_status,
                duration_seconds=duration,
            )
        )
    db.session.commit()
    return build.id


def fetch(client, token, service_id, **query):
    response = client.get(
        f"/api/ci/services/{service_id}/stage-matrix",
        query_string=query,
        headers=auth_headers(token),
    )
    assert response.status_code == 200, response.get_json()
    return response.get_json()["data"]


# ---------------------------------------------------------------------------
# Alignment
# ---------------------------------------------------------------------------

def test_columns_follow_the_newest_build_and_rows_are_newest_first(
    app, client, admin_token, service_id
):
    with app.app_context():
        make_build(service_id, 1, "success", [("Checkout", "success", 20), ("Build", "success", 60)])
        make_build(
            service_id,
            2,
            "success",
            [("Checkout", "success", 18), ("Build", "success", 55), ("Push", "success", 9)],
        )

    data = fetch(client, admin_token, service_id)

    assert [column["key"] for column in data["columns"]] == ["checkout", "build", "push"]
    assert [row["number"] for row in data["rows"]] == [2, 1]


def test_rows_are_ordered_by_build_number_not_row_id(app, client, admin_token, service_id):
    """Written out of order on purpose: "newest" means the newest build."""
    with app.app_context():
        make_build(service_id, 9, "success", [("Checkout", "success", 20), ("Push", "success", 9)])
        make_build(service_id, 8, "success", [("Checkout", "success", 18)])

    data = fetch(client, admin_token, service_id)

    assert [row["number"] for row in data["rows"]] == [9, 8]
    # The newest build still sets the column order, whatever the row ids say.
    assert [column["key"] for column in data["columns"]] == ["checkout", "push"]


def test_a_stage_a_build_never_had_is_null_not_a_blank_pass(
    app, client, admin_token, service_id
):
    """The distinction the whole grid rests on: absent versus not-yet-run."""
    with app.app_context():
        make_build(service_id, 1, "success", [("Checkout", "success", 20)])
        make_build(
            service_id,
            2,
            "running",
            [("Checkout", "success", 18), ("Sonar Scan", "pending", None)],
        )

    data = fetch(client, admin_token, service_id)
    older = next(row for row in data["rows"] if row["number"] == 1)
    newer = next(row for row in data["rows"] if row["number"] == 2)

    assert older["cells"]["sonar-scan"] is None
    assert newer["cells"]["sonar-scan"]["status"] == "pending"


def test_a_stage_only_older_builds_had_keeps_its_place_in_the_order(
    app, client, admin_token, service_id
):
    """A removed middle stage stays in the middle, not dumped at the end."""
    with app.app_context():
        make_build(
            service_id,
            1,
            "success",
            [
                ("Checkout", "success", 20),
                ("Lint", "success", 8),
                ("Build", "success", 60),
            ],
        )
        make_build(service_id, 2, "success", [("Checkout", "success", 19), ("Build", "success", 58)])

    data = fetch(client, admin_token, service_id)

    assert [column["key"] for column in data["columns"]] == ["checkout", "lint", "build"]


def test_a_stage_that_used_to_run_first_still_comes_first(
    app, client, admin_token, service_id
):
    with app.app_context():
        make_build(
            service_id,
            1,
            "success",
            [("Prepare", "success", 5), ("Checkout", "success", 20), ("Build", "success", 60)],
        )
        make_build(service_id, 2, "success", [("Checkout", "success", 19), ("Build", "success", 58)])

    data = fetch(client, admin_token, service_id)

    assert [column["key"] for column in data["columns"]] == ["prepare", "checkout", "build"]


def test_a_renamed_stage_becomes_its_own_column(app, client, admin_token, service_id):
    """Truthful and visible: we cannot know a rename from a replacement."""
    with app.app_context():
        make_build(service_id, 1, "success", [("Build JAR", "success", 60)])
        make_build(service_id, 2, "success", [("Package", "success", 58)])

    data = fetch(client, admin_token, service_id)

    assert [column["key"] for column in data["columns"]] == ["package", "build-jar"]
    assert next(r for r in data["rows"] if r["number"] == 2)["cells"]["build-jar"] is None


# ---------------------------------------------------------------------------
# Statistics
# ---------------------------------------------------------------------------

def test_averages_ignore_failed_runs(app, client, admin_token, service_id):
    with app.app_context():
        for number in (1, 2, 3):
            make_build(service_id, number, "success", [("Build", "success", 60)])
        make_build(service_id, 4, "failed", [("Build", "failed", 1)])

    data = fetch(client, admin_token, service_id)
    column = data["columns"][0]

    assert column["avgSeconds"] == 60
    assert column["sampleSize"] == 3
    assert column["failures"] == 1


def test_no_average_until_there_are_three_passes(app, client, admin_token, service_id):
    with app.app_context():
        make_build(service_id, 1, "success", [("Build", "success", 60)])
        make_build(service_id, 2, "success", [("Build", "success", 62)])

    data = fetch(client, admin_token, service_id)

    assert data["columns"][0]["avgSeconds"] is None
    assert data["columns"][0]["sampleSize"] == 2
    assert data["minSamplesForAverage"] == matrix_service.MIN_SAMPLES_FOR_AVERAGE


def test_share_of_build_sums_to_one_over_columns_with_an_average(
    app, client, admin_token, service_id
):
    with app.app_context():
        for number in (1, 2, 3):
            make_build(
                service_id,
                number,
                "success",
                [("Checkout", "success", 20), ("Build", "success", 60)],
            )

    data = fetch(client, admin_token, service_id)
    shares = [column["shareOfBuild"] for column in data["columns"]]

    assert shares == [0.25, 0.75]


# ---------------------------------------------------------------------------
# Cell payload
# ---------------------------------------------------------------------------

def test_a_failed_cell_carries_its_log_tail_and_a_passing_one_does_not(
    app, client, admin_token, service_id
):
    with app.app_context():
        build_id = make_build(
            service_id,
            1,
            "failed",
            [("Checkout", "success", 20), ("Build Image", "failed", 19)],
        )
        stages = {
            stage.name: stage
            for stage in CiBuildStage.query.filter_by(build_id=build_id).all()
        }
        failed = stages["Build Image"]
        failed.exit_code = 1
        for seq, content in enumerate(
            ["step 1/5", "step 2/5", "ERROR: no such file", "[kubesight-exit] 1"], start=1
        ):
            db.session.add(
                CiLogChunk(build_stage_id=failed.id, seq=seq, content=content)
            )
        db.session.add(
            CiLogChunk(build_stage_id=stages["Checkout"].id, seq=1, content="cloned")
        )
        db.session.commit()

    cells = fetch(client, admin_token, service_id)["rows"][0]["cells"]

    assert [line["content"] for line in cells["build-image"]["logTail"]] == [
        "step 2/5",
        "ERROR: no such file",
        "[kubesight-exit] 1",
    ]
    assert cells["build-image"]["exitCode"] == 1
    assert cells["checkout"]["logTail"] == []


def test_continue_on_failure_comes_from_the_builds_own_snapshot(
    app, client, admin_token, service_id
):
    """A red cell in a build that kept going has to say so."""
    with app.app_context():
        make_build(
            service_id,
            1,
            "failed",
            [("Checkout", "success", 20), ("Sonar Scan", "failed", 12)],
            snapshot_stages=[
                {"name": "Checkout"},
                {"name": "Sonar Scan", "continueOnFailure": True},
            ],
        )

    cells = fetch(client, admin_token, service_id)["rows"][0]["cells"]

    assert cells["sonar-scan"]["continueOnFailure"] is True
    assert cells["checkout"]["continueOnFailure"] is False


def test_a_stage_after_a_failure_reads_as_not_reached(app, client, admin_token, service_id):
    """The engine writes 'skipped' for both; the grid must not paint them alike."""
    with app.app_context():
        make_build(
            service_id,
            1,
            "failed",
            [
                ("Checkout", "success", 20),
                ("Build", "failed", 12),
                ("Push", "skipped", None),
            ],
        )

    cells = fetch(client, admin_token, service_id)["rows"][0]["cells"]

    assert cells["push"]["skipKind"] == matrix_service.SKIP_NOT_REACHED
    assert cells["checkout"]["skipKind"] is None


def test_a_skip_the_runner_could_not_run_reads_as_unavailable_and_carries_its_log(
    app, client, admin_token, service_id
):
    """The case that matters: a green build that produced no image."""
    explanation = (
        "[kubesight] Skipped: this stage is a 'container_image' stage. "
        "Set CI_BUILDKIT_ADDR to build images. Nothing was built, and no "
        "artifact was recorded."
    )
    with app.app_context():
        build_id = make_build(
            service_id,
            1,
            "success",
            [("Checkout", "success", 20), ("Build Image", "skipped", None)],
        )
        stage = CiBuildStage.query.filter_by(build_id=build_id, name="Build Image").one()
        # The engine stamps started_at before declining a stage it cannot run.
        stage.started_at = datetime(2026, 9, 4, 12, 0, tzinfo=timezone.utc)
        db.session.add(CiLogChunk(build_stage_id=stage.id, seq=1, content=explanation))
        db.session.commit()

    data = fetch(client, admin_token, service_id)
    cells = data["rows"][0]["cells"]
    image = next(column for column in data["columns"] if column["key"] == "build-image")

    assert cells["build-image"]["skipKind"] == matrix_service.SKIP_UNAVAILABLE
    assert cells["build-image"]["logTail"][0]["content"] == explanation
    assert image["skips"] == 1
    assert image["notReached"] == 0


def test_a_cancelled_stage_pre_empts_the_rest_rather_than_blaming_the_runner(
    app, client, admin_token, service_id
):
    with app.app_context():
        make_build(
            service_id,
            1,
            "cancelled",
            [
                ("Checkout", "success", 20),
                ("Unit Tests", "cancelled", 44),
                ("Build Image", "skipped", None),
            ],
        )

    data = fetch(client, admin_token, service_id)
    cells = data["rows"][0]["cells"]
    image = next(column for column in data["columns"] if column["key"] == "build-image")

    assert cells["build-image"]["skipKind"] == matrix_service.SKIP_NOT_REACHED
    assert image["skips"] == 0
    assert image["failures"] == 0


def test_a_skip_that_never_started_is_not_reached_even_with_nothing_before_it(
    app, client, admin_token, service_id
):
    """A build cancelled while queued: no stage failed, and none ran."""
    with app.app_context():
        make_build(
            service_id,
            1,
            "cancelled",
            [("Checkout", "skipped", None), ("Build", "skipped", None)],
        )

    cells = fetch(client, admin_token, service_id)["rows"][0]["cells"]

    assert cells["checkout"]["skipKind"] == matrix_service.SKIP_NOT_REACHED
    assert cells["build"]["skipKind"] == matrix_service.SKIP_NOT_REACHED


def test_a_rerun_reusing_earlier_stages_says_so_instead_of_warning(
    app, client, admin_token, service_id
):
    with app.app_context():
        build = CiBuild(
            service_id=service_id,
            number=1,
            status="success",
            trigger_type="retry",
            branch="main",
            pipeline_snapshot={
                "stages": [
                    {"name": "Checkout", "stageType": "checkout"},
                    {"name": "Build", "stageType": "command"},
                    {"name": "Build Image", "stageType": "container_image"},
                ],
                "restore": {"startFromPosition": 2, "fromBuildNumber": 7},
            },
        )
        db.session.add(build)
        db.session.flush()
        for position, (name, stage_status, duration) in enumerate(
            [("Checkout", "success", 20), ("Build", "skipped", None), ("Build Image", "success", 24)]
        ):
            db.session.add(
                CiBuildStage(
                    build_id=build.id,
                    position=position,
                    name=name,
                    stage_type="command",
                    status=stage_status,
                    duration_seconds=duration,
                )
            )
        db.session.commit()

    data = fetch(client, admin_token, service_id)
    cells = data["rows"][0]["cells"]
    build_column = next(column for column in data["columns"] if column["key"] == "build")

    assert cells["build"]["skipKind"] == matrix_service.SKIP_REUSED
    assert cells["build"]["reusedFromBuildNumber"] == 7
    # Reusing work is not a problem the verdict line should shout about.
    assert build_column["skips"] == 0


def test_the_engines_skip_reason_reaches_the_cell(app, client, admin_token, service_id):
    reason = "This runner cannot build container images: CI_BUILDKIT_ADDR is not set."
    with app.app_context():
        build_id = make_build(service_id, 1, "success", [("Build Image", "skipped", None)])
        stage = CiBuildStage.query.filter_by(build_id=build_id).one()
        stage.error = reason
        db.session.commit()

    cells = fetch(client, admin_token, service_id)["rows"][0]["cells"]

    assert cells["build-image"]["error"] == reason


def test_a_running_cell_carries_started_at_so_the_grid_can_count_up(
    app, client, admin_token, service_id
):
    with app.app_context():
        build_id = make_build(service_id, 1, "running", [("Build", "running", None)])
        stage = CiBuildStage.query.filter_by(build_id=build_id).one()
        stage.started_at = datetime(2026, 9, 4, 12, 0, tzinfo=timezone.utc)
        db.session.commit()

    cell = fetch(client, admin_token, service_id)["rows"][0]["cells"]["build"]

    assert cell["durationSeconds"] is None
    assert cell["startedAt"].startswith("2026-09-04T12:00")


# ---------------------------------------------------------------------------
# Request handling and access
# ---------------------------------------------------------------------------

def test_limit_is_clamped_and_total_reports_the_whole_history(
    app, client, admin_token, service_id
):
    with app.app_context():
        for number in range(1, 6):
            make_build(service_id, number, "success", [("Build", "success", 60)])

    data = fetch(client, admin_token, service_id, limit=2)

    assert [row["number"] for row in data["rows"]] == [5, 4]
    assert data["total"] == 5
    assert fetch(client, admin_token, service_id, limit=999)["limit"] == matrix_service.MAX_BUILDS


def test_status_filter_narrows_the_rows(app, client, admin_token, service_id):
    with app.app_context():
        make_build(service_id, 1, "success", [("Build", "success", 60)])
        make_build(service_id, 2, "failed", [("Build", "failed", 4)])

    data = fetch(client, admin_token, service_id, status="failed")

    assert [row["number"] for row in data["rows"]] == [2]


def test_a_service_with_no_builds_is_an_empty_grid(client, admin_token, service_id):
    data = fetch(client, admin_token, service_id)

    assert data["columns"] == []
    assert data["rows"] == []


def test_unknown_service_is_a_404(client, admin_token):
    response = client.get(
        "/api/ci/services/99999/stage-matrix", headers=auth_headers(admin_token)
    )
    assert response.status_code == 404


def test_viewer_can_read_the_matrix(app, client, admin_token, viewer_token, service_id):
    with app.app_context():
        make_build(service_id, 1, "success", [("Build", "success", 60)])

    assert fetch(client, viewer_token, service_id)["rows"][0]["number"] == 1


def test_unauthenticated_request_is_rejected(client, service_id):
    assert client.get(f"/api/ci/services/{service_id}/stage-matrix").status_code == 401
