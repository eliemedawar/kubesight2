"""Application Intelligence lists read a fixed number of statements.

Each application used to cost its newest analysis, a severity GROUP BY, the
posture GROUP BY of every analysis shown, the requesting user (with three
access collections) and the whole Hermes result document — only to read one
verdict out of it. Now the newest analyses, their postures and their users are
read once per page, and the verdict lives in its own column.
"""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timedelta, timezone

from sqlalchemy import event

from api.db import db
from api.models import ApplicationAnalysis, ApplicationFinding, User

from .conftest import auth_headers
from .test_application_intelligence import _create_application, _create_credential

_SEQ = {"n": 0}


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


def _application(client, token, credential_id):
    _SEQ["n"] += 1
    response = client.post(
        "/api/applications",
        headers=auth_headers(token),
        json={
            "name": f"Service {_SEQ['n']}",
            "repositoryUrl": f"https://bitbucket.org/workspace/service-{_SEQ['n']}",
            "defaultBranch": "main",
            "credentialProfileId": credential_id,
        },
    )
    assert response.status_code == 201, response.get_json()
    return response.get_json()["data"]


def _analyses(app, application_id: int, *, count: int = 2, verdict: str = "passed") -> list[int]:
    ids = []
    with app.app_context():
        user = User.query.filter_by(username="operator").first() or User.query.first()
        base = datetime.now(timezone.utc) - timedelta(hours=1)
        for index in range(count):
            row = ApplicationAnalysis(
                application_id=application_id,
                status="Completed",
                analysis_mode="Quick",
                requested_by_user_id=user.id,
                created_at=base + timedelta(minutes=index),
                result_summary={
                    "application_profile": {"build_verification": {"status": verdict}},
                    "padding": "p" * 20000,
                },
                build_verification_status=verdict,
            )
            db.session.add(row)
            db.session.flush()
            for number, (severity, status) in enumerate(
                [("Critical", "Open"), ("High", "Open"), ("High", "Resolved"), ("Low", "Open")]
            ):
                db.session.add(ApplicationFinding(
                    analysis_id=row.id, category="Source", severity=severity,
                    confidence="High", title=f"f{number}", description="d",
                    status=status, fingerprint=f"{row.id}-{number}",
                ))
            ids.append(row.id)
        db.session.commit()
    return ids


def _get(client, token, path):
    with _statements() as statements:
        response = client.get(path, headers=auth_headers(token))
    assert response.status_code == 200, response.get_json()
    # The caller's role and permissions reload whenever their cache is cold —
    # the request's authentication, not the list being measured.
    return response.get_json()["data"], [s for s in statements if "FROM roles" not in s]


def test_application_list_statement_count_does_not_grow(app, client, operator_token):
    credential = _create_credential(client, operator_token)
    for _ in range(2):
        created = _application(client, operator_token, credential["id"])
        _analyses(app, created["id"])
    _get(client, operator_token, "/api/applications")
    _, few = _get(client, operator_token, "/api/applications")

    for _ in range(5):
        created = _application(client, operator_token, credential["id"])
        _analyses(app, created["id"], count=3)
    _application(client, operator_token, credential["id"])  # never analysed
    data, many = _get(client, operator_token, "/api/applications")
    assert len(data["items"]) == 8
    assert len(many) == len(few), "\n".join(many)
    # The Hermes document is never read for a list.
    assert not any("result_summary" in s.split(" FROM ")[0] for s in many if s.lstrip().upper().startswith("SELECT"))

    analysed = [item for item in data["items"] if item["latestAnalysis"]]
    assert len(analysed) == 7
    for item in analysed:
        assert item["criticalFindingCount"] == 1
        assert item["highFindingCount"] == 2  # every status, as before
        latest = item["latestAnalysis"]
        assert latest["posture"]["openBySeverity"]["High"] == 1
        assert latest["posture"]["riskLevel"] == "Critical"
        assert latest["buildVerificationStatus"] == "passed"
        assert latest["requestedBy"]


def test_history_is_batched_and_the_verdict_backfilled(app, client, operator_token):
    credential = _create_credential(client, operator_token)
    created = _application(client, operator_token, credential["id"])
    ids = _analyses(app, created["id"], count=3, verdict="failed")
    with app.app_context():
        # Stored before the column existed.
        ApplicationAnalysis.query.filter(ApplicationAnalysis.id.in_(ids)).update(
            {"build_verification_status": None}, synchronize_session=False
        )
        db.session.commit()
    data, _ = _get(client, operator_token, f"/api/applications/{created['id']}")
    assert [item["id"] for item in data["analyses"]] == sorted(ids, reverse=True)
    assert all(item["buildVerificationStatus"] == "failed" for item in data["analyses"])
    assert all(item["posture"]["total"] == 4 for item in data["analyses"])
    with app.app_context():
        stored = {
            row.build_verification_status
            for row in ApplicationAnalysis.query.filter(ApplicationAnalysis.id.in_(ids))
        }
        assert stored == {"failed"}  # kept: the next read does not open the document

    listing, _ = _get(client, operator_token, f"/api/applications/{created['id']}/analyses")
    assert [item["posture"]["openTotal"] for item in listing["items"]] == [3, 3, 3]
