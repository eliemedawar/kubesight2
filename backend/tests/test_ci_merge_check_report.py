"""The merge check PDF: every tool's report read into one document.

Each tool's report file is stored the way a runner stores it (a build artifact
named after the tool), so the test covers the lookup as well as the parsing.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from api.db import db
from api.models_ci import CiBuild, CiBuildStage, CiService
from api.models_merge_checks import CiMergeCheck
from api.services.ci.merge_checks import report
from tests.conftest import auth_headers

ESLINT = [
    {
        "filePath": "/workspace/source/src/app.js",
        "messages": [
            {"ruleId": "no-undef", "severity": 2, "message": "'x' is not defined.", "line": 4},
            {"ruleId": "no-unused-vars", "severity": 1, "message": "'y' is unused.", "line": 9},
        ],
    }
]
HADOLINT = [
    {"file": "Dockerfile", "line": 3, "code": "DL3008", "level": "warning", "message": "Pin versions in apt get install."},
    {"file": "Dockerfile", "line": 1, "code": "DL3006", "level": "style", "message": "Always tag the image explicitly."},
]
PMD = "/workspace/source/src/Main.java:12:\tUnusedLocalVariable:\tAvoid unused local variables such as 'n'.\n"
DART = "Resolving dependencies...\nERROR|COMPILE_TIME_ERROR|UNDEFINED_IDENTIFIER|/workspace/source/lib/main.dart|7|3|1|Undefined name 'foo'.\n"


@pytest.fixture()
def checked(app, tmp_path, monkeypatch):
    """A finished, blocked merge check whose stages kept their report files."""
    from api.services.ci import artifacts as artifacts_service
    from api.services.ci.runners.base import ArtifactRef

    monkeypatch.setenv("CI_ARTIFACT_DIR", str(tmp_path / "artifacts"))
    files = {
        "eslint": ("eslint-report.json", json.dumps(ESLINT)),
        "hadolint": ("hadolint-report.json", json.dumps(HADOLINT)),
        "pmd": ("pmd-report.txt", PMD),
        "dart_analyze": ("dart-analyze.txt", DART),
    }
    with app.app_context():
        service = CiService(name="Checkout Web", slug="checkout-web")
        db.session.add(service)
        db.session.flush()
        build = CiBuild(service_id=service.id, number=59, status="success", commit_sha="9345e313" * 5)
        db.session.add(build)
        db.session.flush()
        stage = CiBuildStage(build_id=build.id, position=0, name="Checks", status="success")
        db.session.add(stage)
        db.session.flush()
        for tool, (name, body) in files.items():
            path = tmp_path / name
            path.write_text(body, encoding="utf-8")
            artifacts_service.record_artifact(
                service_id=service.id, build_id=build.id, build_stage_id=stage.id,
                ref=ArtifactRef(name=tool, artifact_type="test-report", local_path=str(path)),
                commit=True,
            )
        check = CiMergeCheck(
            service_id=service.id, build_id=build.id, pull_request_id="34",
            title="fix(docker): pass the Dockerfile lint", author="Rita",
            source_branch="dockerfile-lint", destination_branch="master",
            commit_sha="9345e313" * 5, state="failed", verdict="blocked",
            total_problems=4, delivery_state="delivered",
            gate={"maxTotalProblems": 10, "maxEslintProblems": 0},
            reasons=["ESLint reported 1 problem; this service allows at most 0."],
            metrics={
                "eslint": {"status": "ok", "problems": 1},
                "hadolint": {"status": "ok", "problems": 1},
                "pmd": {"status": "ok", "problems": 1},
                "dart_analyze": {"status": "ok", "problems": 1},
                "sonar": {"status": "ok", "problems": 0, "bugs": 0},
                "shellcheck": {"status": "skipped", "problems": 0, "message": "This check was skipped."},
            },
        )
        db.session.add(check)
        db.session.commit()
        return SimpleNamespace(check_id=check.id)


def test_every_tools_report_is_read_into_one_shape(app, checked):
    with app.app_context():
        entries = {e["tool"]: e for e in report.collect(db.session.get(CiMergeCheck, checked.check_id))}

    eslint = entries["eslint"]
    assert eslint["limit"] == 0 and eslint["counted"] == 1
    # Worst first, and shown from the repository root.
    assert [(f["level"], f["rule"], f["file"], f["line"]) for f in eslint["findings"]] == [
        ("error", "no-undef", "src/app.js", 4),
        ("warning", "no-unused-vars", "src/app.js", 9),
    ]
    assert [f["rule"] for f in entries["hadolint"]["findings"]] == ["DL3008", "DL3006"]
    assert entries["pmd"]["findings"][0]["file"] == "src/Main.java"
    assert entries["pmd"]["findings"][0]["rule"] == "UnusedLocalVariable"
    # pub's own output in the same file is not a finding.
    assert [f["message"] for f in entries["dart_analyze"]["findings"]] == ["Undefined name 'foo'."]
    assert "SonarQube" in entries["sonar"]["note"]
    assert entries["shellcheck"]["findings"] == [] and entries["shellcheck"]["counted"] is None


def test_the_pdf_is_a_real_document(app, checked):
    with app.app_context():
        pdf = report.render_pdf(db.session.get(CiMergeCheck, checked.check_id))
    assert pdf.startswith(b"%PDF-")
    assert len(pdf) > 3000


def test_the_pdf_downloads_with_a_ticket(client, admin_token, checked):
    base = f"/api/ci/merge-checks/runs/{checked.check_id}"
    ticket = client.post(f"{base}/report-ticket", headers=auth_headers(admin_token)).get_json()["data"]["ticket"]
    response = client.get(f"{base}/report?ticket={ticket}")
    assert response.status_code == 200
    assert response.mimetype == "application/pdf"
    assert "checkout-web-pr-34-merge-check.pdf" in response.headers["Content-Disposition"]
    assert response.data.startswith(b"%PDF-")


def test_a_check_still_running_has_no_report(app, client, admin_token, checked):
    with app.app_context():
        row = db.session.get(CiMergeCheck, checked.check_id)
        row.state, row.verdict = "running", None
        db.session.commit()
    response = client.post(
        f"/api/ci/merge-checks/runs/{checked.check_id}/report-ticket", headers=auth_headers(admin_token)
    )
    assert response.status_code == 409


def test_a_check_that_could_not_run_still_gets_a_report(app, checked):
    # The "Not checked" rows: no findings to list, but a document that says why.
    with app.app_context():
        row = db.session.get(CiMergeCheck, checked.check_id)
        row.state, row.verdict, row.metrics = "error", "unknown", {}
        row.build_id = None
        row.error = "The build for this check never started."
        row.reasons = [row.error]
        db.session.commit()
        pdf = report.render_pdf(row)
    assert pdf.startswith(b"%PDF-")
