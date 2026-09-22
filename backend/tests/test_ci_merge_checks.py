"""Merge checks: the webhook, the gate, the verdict, and what Bitbucket is told.

These drive the real path — a webhook body goes in, an ordinary build runs on
the mock runner, tool output is settled into a verdict, and the verdict is
handed to a stand-in for the source host. The only thing replaced is the HTTP
call to Bitbucket, because that is the one part a test cannot own.
"""

from __future__ import annotations

import pytest

from api.db import db
from api.models_application_intelligence import BitbucketCredentialProfile
from api.models_ci import CiBuild, CiLogChunk, CiService
from api.models_merge_checks import CiMergeCheck, CiMergeCheckConfig
from api.secret_encryption import encrypt_secret
from tests.conftest import auth_headers


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture()
def service_id(app, client, admin_token):
    """A node service with a WRITE-capable credential and source connected."""
    with app.app_context():
        credential = BitbucketCredentialProfile(
            name="ci-write",
            provider="bitbucket",
            credential_type="repository_access_token",
            secret_cipher=encrypt_secret("write-token"),
            read_only=False,
            enabled=True,
        )
        db.session.add(credential)
        db.session.commit()
        credential_id = credential.id

    created = client.post(
        "/api/ci/services",
        json={"name": "Checkout Web", "applicationType": "node"},
        headers=auth_headers(admin_token),
    ).get_json()["data"]
    client.put(
        f"/api/ci/services/{created['id']}/source",
        json={
            "repositoryUrl": "https://bitbucket.org/areeba/checkout-web",
            "defaultBranch": "main",
            "credentialProfileId": credential_id,
        },
        headers=auth_headers(admin_token),
    )
    return created["id"]


def _enable(client, token, service_id, **overrides):
    payload = {
        "enabled": True,
        "tools": ["eslint", "dependency_check"],
        "events": ["pullrequest:created", "pullrequest:updated"],
        "gateMode": "override",
        "maxTotalProblems": 5,
        **overrides,
    }
    return client.put(
        f"/api/ci/services/{service_id}/merge-checks",
        json=payload,
        headers=auth_headers(token),
    )


def _secret(client, token, service_id):
    response = client.get(
        f"/api/ci/services/{service_id}/merge-checks/secret",
        headers=auth_headers(token),
    )
    assert response.status_code == 200
    return response.get_json()["data"]["secret"]


def _pull_request_body(commit="abc123def456", destination="main", pr_id="7"):
    return {
        "repository": {"full_name": "areeba/checkout-web"},
        "pullrequest": {
            "id": pr_id,
            "title": "Add the refund endpoint",
            "author": {"display_name": "Rita"},
            "source": {
                "branch": {"name": "feature/refunds"},
                "commit": {"hash": commit},
            },
            "destination": {"branch": {"name": destination}},
            "links": {"html": {"href": "https://bitbucket.org/areeba/checkout-web/pull-requests/7"}},
        },
    }


def _post_hook(client, service_slug, secret, body, event="pullrequest:created"):
    return client.post(
        f"/api/ci/merge-checks/inbound/{service_slug}",
        json=body,
        headers={"X-KubeSight-Secret": secret, "X-Event-Key": event},
    )


def _slug(app, service_id):
    with app.app_context():
        return db.session.get(CiService, service_id).slug


def _drain(app, max_passes: int = 40, settle: bool = False):
    """Run the CI engine until every build is terminal.

    Settlement is held back by default. In production a check settles in the
    same pass the build finishes in, because the runner has been pumping the
    tool's output into the log all along. The mock runner executes nothing, so
    a test has to write that output itself — which it can only do once the
    build has finished, and only if settlement has not already judged it as
    having reported nothing.
    """
    from api.services.ci import engine
    from api.services.ci.runners import mock as mock_runner

    original_seconds = mock_runner._STAGE_SECONDS
    original_settle = engine._settle_merge_checks
    mock_runner._STAGE_SECONDS = 0.0
    if not settle:
        engine._settle_merge_checks = lambda: 0
    try:
        with app.app_context():
            for _ in range(max_passes):
                engine.advance_ci_builds()
                if not CiBuild.query.filter(
                    CiBuild.status.in_(("queued", "running"))
                ).count():
                    return
    finally:
        mock_runner._STAGE_SECONDS = original_seconds
        engine._settle_merge_checks = original_settle


def _report(app, build_id, tool, line):
    """Put one tool's result line into the build's log, as a runner would.

    The mock runner echoes command TEXT and executes nothing, so a test that
    wants a tool to have reported something has to say what it reported. This
    writes the same row the real runner's log pump would.
    """
    from api.services.ci.merge_checks import stages as stage_defs

    with app.app_context():
        build = db.session.get(CiBuild, build_id)
        stage = next(
            s for s in build.stages if s.name == stage_defs.stage_name_for(tool)
        )
        db.session.add(
            CiLogChunk(
                build_stage_id=stage.id,
                seq=900,
                stream="stdout",
                content=line,
            )
        )
        db.session.commit()


@pytest.fixture()
def captured_verdicts(monkeypatch):
    """Stand in for Bitbucket. Records every write instead of making one."""
    from api.services.ci.source.bitbucket import BitbucketSourceProvider

    sent = {"statuses": [], "comments": []}

    def fake_status(self, ref, credential, **kwargs):
        sent["statuses"].append({"repository": ref.full_name, **kwargs})

    def fake_comment(self, ref, credential, **kwargs):
        sent["comments"].append({"repository": ref.full_name, **kwargs})

    monkeypatch.setattr(BitbucketSourceProvider, "post_check_verdict", fake_status)
    monkeypatch.setattr(BitbucketSourceProvider, "post_pull_request_note", fake_comment)
    return sent


# ---------------------------------------------------------------------------
# The gate, as a function
# ---------------------------------------------------------------------------

def test_a_gate_of_five_allows_five_and_blocks_six():
    from api.services.ci.merge_checks.policy import evaluate

    gate = {"maxTotalProblems": 5, "blockOnToolError": True}
    at_the_limit = evaluate(gate, {"eslint": {"status": "ok", "problems": 5}})
    assert at_the_limit["verdict"] == "allowed"
    assert at_the_limit["totalProblems"] == 5

    over = evaluate(gate, {"eslint": {"status": "ok", "problems": 6}})
    assert over["verdict"] == "blocked"
    assert "at most 5" in over["reasons"][0]


def test_per_tool_caps_apply_on_top_of_the_total():
    from api.services.ci.merge_checks.policy import evaluate

    gate = {"maxTotalProblems": 20, "maxSonarProblems": 0, "blockOnToolError": True}
    outcome = evaluate(
        gate,
        {
            "eslint": {"status": "ok", "problems": 3},
            "sonar": {"status": "ok", "problems": 1},
        },
    )
    assert outcome["verdict"] == "blocked"
    assert outcome["totalProblems"] == 4
    assert "SonarQube" in outcome["reasons"][0]


def test_a_tool_that_could_not_run_blocks_rather_than_counting_as_clean():
    from api.services.ci.merge_checks.policy import evaluate

    gate = {"maxTotalProblems": 5, "blockOnToolError": True}
    outcome = evaluate(gate, {"eslint": {"status": "missing", "problems": 0}})
    assert outcome["verdict"] == "blocked"
    assert outcome["totalProblems"] == 0
    assert "has not been checked" in outcome["reasons"][0]

    relaxed = evaluate({**gate, "blockOnToolError": False}, {"eslint": {"status": "missing"}})
    assert relaxed["verdict"] == "allowed"
    assert "warning only" in relaxed["reasons"][0]


def test_a_service_override_wins_over_the_policy_and_inherit_ignores_it(app):
    from api.services.ci.merge_checks.policy import get_policy, resolve_gate

    with app.app_context():
        policy = get_policy()
        policy.max_total_problems = 10
        db.session.add(policy)
        db.session.commit()

        config = CiMergeCheckConfig(
            service_id=1, gate_mode="inherit", max_total_problems=2
        )
        assert resolve_gate(config, policy)["maxTotalProblems"] == 10
        config.gate_mode = "override"
        assert resolve_gate(config, policy)["maxTotalProblems"] == 2
        # An override that leaves a field blank still inherits THAT field.
        assert resolve_gate(config, policy)["blockOnToolError"] is True


# ---------------------------------------------------------------------------
# The metric line
# ---------------------------------------------------------------------------

def test_only_a_real_metric_line_is_read_never_the_command_that_prints_it():
    from api.services.ci.merge_checks.metrics import parse_lines

    parsed = parse_lines(
        [
            '$ echo "##kubesight-metric tool=eslint status=ok problems=99"',
            "##kubesight-metric tool=eslint status=ok problems=4 errors=4 warnings=11",
        ]
    )
    assert parsed["eslint"]["problems"] == 4
    assert parsed["eslint"]["warnings"] == 11


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

def test_enabling_merge_checks_builds_a_pipeline_that_is_not_the_default(
    app, client, admin_token, service_id
):
    response = _enable(client, admin_token, service_id)
    assert response.status_code == 200
    data = response.get_json()["data"]
    assert data["enabled"] is True
    assert data["effectiveGate"]["maxTotalProblems"] == 5
    assert [stage["name"] for stage in data["pipelineStages"]] == [
        "Checkout",
        "ESLint",
        "Dependency-Check",
    ]

    # The Pipeline tab must not have changed, and Run Build must still run the
    # service's own pipeline.
    listed = client.get(
        f"/api/ci/services/{service_id}/pipelines", headers=auth_headers(admin_token)
    ).get_json()["data"]["items"]
    assert all(item["id"] != data["pipelineId"] for item in listed)

    with app.app_context():
        service = db.session.get(CiService, service_id)
        assert service.default_pipeline().id != data["pipelineId"]


def test_merge_checks_cannot_be_enabled_without_a_repository(
    client, admin_token
):
    created = client.post(
        "/api/ci/services",
        json={"name": "Unconnected", "applicationType": "node"},
        headers=auth_headers(admin_token),
    ).get_json()["data"]
    response = _enable(client, admin_token, created["id"])
    assert response.status_code == 400
    assert "Source tab" in response.get_json()["error"]


def test_a_read_only_credential_is_reported_as_unable_to_answer_bitbucket(
    app, client, admin_token, service_id
):
    with app.app_context():
        service = db.session.get(CiService, service_id)
        service.credential_profile.read_only = True
        db.session.commit()

    payload = client.get(
        f"/api/ci/services/{service_id}/merge-checks", headers=auth_headers(admin_token)
    ).get_json()["data"]
    assert payload["canReportVerdict"]["ok"] is False
    assert "read-only" in payload["canReportVerdict"]["reason"]


def test_viewers_may_look_but_not_configure(client, viewer_token, service_id):
    assert (
        client.get(
            f"/api/ci/services/{service_id}/merge-checks",
            headers=auth_headers(viewer_token),
        ).status_code
        == 200
    )
    assert _enable(client, viewer_token, service_id).status_code == 403
    assert (
        client.get(
            f"/api/ci/services/{service_id}/merge-checks/secret",
            headers=auth_headers(viewer_token),
        ).status_code
        == 403
    )


# ---------------------------------------------------------------------------
# The webhook
# ---------------------------------------------------------------------------

def test_the_webhook_refuses_a_wrong_or_missing_secret(
    app, client, admin_token, service_id
):
    _enable(client, admin_token, service_id)
    slug = _slug(app, service_id)

    assert _post_hook(client, slug, "not-the-secret", _pull_request_body()).status_code == 401
    assert (
        client.post(
            f"/api/ci/merge-checks/inbound/{slug}", json=_pull_request_body()
        ).status_code
        == 401
    )


def test_the_webhook_starts_a_build_pinned_to_the_pull_request_commit(
    app, client, admin_token, service_id
):
    _enable(client, admin_token, service_id)
    slug = _slug(app, service_id)
    secret = _secret(client, admin_token, service_id)

    response = _post_hook(client, slug, secret, _pull_request_body())
    assert response.status_code == 200
    data = response.get_json()["data"]
    assert data["state"] == "running"

    with app.app_context():
        check = db.session.get(CiMergeCheck, data["checkId"])
        build = db.session.get(CiBuild, check.build_id)
        assert build.commit_sha == "abc123def456"
        assert build.trigger_type == "webhook"
        assert build.branch == "feature/refunds"
        # The gate is copied onto the check, not looked up later.
        assert check.gate["maxTotalProblems"] == 5


def test_an_event_or_branch_this_service_ignores_answers_200_and_says_why(
    app, client, admin_token, service_id
):
    _enable(client, admin_token, service_id, targetBranches=["release/*"])
    slug = _slug(app, service_id)
    secret = _secret(client, admin_token, service_id)

    ignored = _post_hook(client, slug, secret, _pull_request_body(destination="main"))
    assert ignored.status_code == 200
    assert ignored.get_json()["data"]["checked"] is False
    assert "main" in ignored.get_json()["data"]["message"]

    wrong_event = _post_hook(
        client, slug, secret, _pull_request_body(), event="pullrequest:comment_created"
    )
    assert wrong_event.get_json()["data"]["checked"] is False

    with app.app_context():
        assert CiMergeCheck.query.count() == 0


def test_a_redelivered_webhook_does_not_start_a_second_build(
    app, client, admin_token, service_id
):
    _enable(client, admin_token, service_id)
    slug = _slug(app, service_id)
    secret = _secret(client, admin_token, service_id)

    first = _post_hook(client, slug, secret, _pull_request_body()).get_json()["data"]
    second = _post_hook(client, slug, secret, _pull_request_body()).get_json()["data"]

    assert second["duplicate"] is True
    assert second["checkId"] == first["checkId"]
    with app.app_context():
        assert CiBuild.query.count() == 1


# ---------------------------------------------------------------------------
# End to end: webhook -> build -> verdict -> Bitbucket
# ---------------------------------------------------------------------------

def _run_to_verdict(app, client, admin_token, service_id, reports):
    slug = _slug(app, service_id)
    secret = _secret(client, admin_token, service_id)
    check_id = _post_hook(client, slug, secret, _pull_request_body()).get_json()["data"][
        "checkId"
    ]
    with app.app_context():
        build_id = db.session.get(CiMergeCheck, check_id).build_id
    _drain(app)
    for tool, line in reports.items():
        _report(app, build_id, tool, line)
    from api.services.ci.merge_checks import settle

    with app.app_context():
        settle()
    return check_id


def test_a_clean_pull_request_is_reported_to_bitbucket_as_successful(
    app, client, admin_token, service_id, captured_verdicts
):
    _enable(client, admin_token, service_id)
    check_id = _run_to_verdict(
        app,
        client,
        admin_token,
        service_id,
        {
            "eslint": "##kubesight-metric tool=eslint status=ok problems=1 errors=1 warnings=0",
            "dependency_check": (
                "##kubesight-metric tool=dependency_check status=ok problems=2 "
                "critical=0 high=2 medium=4 low=1"
            ),
        },
    )

    with app.app_context():
        check = db.session.get(CiMergeCheck, check_id)
        assert check.state == "passed"
        assert check.verdict == "allowed"
        assert check.total_problems == 3
        assert check.delivery_state == "delivered"

    verdict = captured_verdicts["statuses"][-1]
    assert verdict["state"] == "passed"
    assert verdict["status_key"] == "KUBESIGHT-MERGE"
    assert verdict["commit_sha"] == "abc123def456"
    assert "3 of 5" in verdict["description"]
    assert captured_verdicts["comments"], "the explanation should reach the PR"


def test_more_problems_than_the_gate_allows_is_reported_as_failed(
    app, client, admin_token, service_id, captured_verdicts
):
    _enable(client, admin_token, service_id)
    check_id = _run_to_verdict(
        app,
        client,
        admin_token,
        service_id,
        {
            "eslint": "##kubesight-metric tool=eslint status=ok problems=4 errors=4 warnings=9",
            "dependency_check": (
                "##kubesight-metric tool=dependency_check status=ok problems=2 "
                "critical=1 high=1 medium=0 low=0"
            ),
        },
    )

    with app.app_context():
        check = db.session.get(CiMergeCheck, check_id)
        assert check.verdict == "blocked"
        assert check.total_problems == 6
        assert "at most 5" in check.reasons[0]

    verdict = captured_verdicts["statuses"][-1]
    assert verdict["state"] == "failed"
    comment = captured_verdicts["comments"][-1]["markdown"]
    assert "blocked this merge" in comment
    assert "ESLint" in comment and "Dependency-Check" in comment


def test_a_check_whose_tool_said_nothing_blocks_instead_of_passing(
    app, client, admin_token, service_id, captured_verdicts
):
    _enable(client, admin_token, service_id)
    check_id = _run_to_verdict(
        app,
        client,
        admin_token,
        service_id,
        {"eslint": "##kubesight-metric tool=eslint status=ok problems=0"},
    )

    with app.app_context():
        check = db.session.get(CiMergeCheck, check_id)
        assert check.verdict == "blocked"
        assert check.metrics["dependency_check"]["status"] == "missing"
    assert captured_verdicts["statuses"][-1]["state"] == "failed"


def test_a_delivery_that_bitbucket_refuses_permanently_stops_retrying(
    app, client, admin_token, service_id, monkeypatch
):
    from api.services.ci.source import SourceError
    from api.services.ci.source.bitbucket import BitbucketSourceProvider

    def refuse(self, ref, credential, **kwargs):
        raise SourceError("Bitbucket rejected this credential.", retryable=False)

    monkeypatch.setattr(BitbucketSourceProvider, "post_check_verdict", refuse)
    _enable(client, admin_token, service_id)
    check_id = _run_to_verdict(
        app,
        client,
        admin_token,
        service_id,
        {
            "eslint": "##kubesight-metric tool=eslint status=ok problems=0",
            "dependency_check": "##kubesight-metric tool=dependency_check status=ok problems=0",
        },
    )

    with app.app_context():
        check = db.session.get(CiMergeCheck, check_id)
        # The verdict stands; only its delivery failed, and it says so.
        assert check.verdict == "allowed"
        assert check.delivery_state == "failed"
        assert check.delivery_attempts == 1
        assert "rejected this credential" in check.delivery_error


# ---------------------------------------------------------------------------
# The installation policy
# ---------------------------------------------------------------------------

def test_the_policy_is_the_default_and_a_service_can_be_left_to_inherit_it(
    app, client, admin_token, service_id, captured_verdicts
):
    assert (
        client.put(
            "/api/ci/merge-checks/policy",
            json={"maxTotalProblems": 2},
            headers=auth_headers(admin_token),
        ).status_code
        == 200
    )
    _enable(client, admin_token, service_id, gateMode="inherit", maxTotalProblems=None)

    payload = client.get(
        f"/api/ci/services/{service_id}/merge-checks", headers=auth_headers(admin_token)
    ).get_json()["data"]
    assert payload["effectiveGate"]["maxTotalProblems"] == 2
    assert payload["effectiveGate"]["sources"]["maxTotalProblems"] == "policy"

    check_id = _run_to_verdict(
        app,
        client,
        admin_token,
        service_id,
        {
            "eslint": "##kubesight-metric tool=eslint status=ok problems=3",
            "dependency_check": "##kubesight-metric tool=dependency_check status=ok problems=0",
        },
    )
    with app.app_context():
        assert db.session.get(CiMergeCheck, check_id).verdict == "blocked"


def test_a_negative_quality_gate_is_refused(client, admin_token):
    response = client.put(
        "/api/ci/merge-checks/policy",
        json={"maxTotalProblems": -1},
        headers=auth_headers(admin_token),
    )
    assert response.status_code == 400
    assert "negative" in response.get_json()["error"]


def test_only_an_administrator_may_move_the_quality_gate(client, operator_token):
    assert (
        client.put(
            "/api/ci/merge-checks/policy",
            json={"maxTotalProblems": 500},
            headers=auth_headers(operator_token),
        ).status_code
        == 403
    )
    assert (
        client.get(
            "/api/ci/merge-checks/policy", headers=auth_headers(operator_token)
        ).status_code
        == 200
    )


# ---------------------------------------------------------------------------
# Lifecycle
# ---------------------------------------------------------------------------

def test_deleting_the_service_takes_its_merge_checks_with_it(
    app, client, admin_token, service_id
):
    _enable(client, admin_token, service_id)
    slug = _slug(app, service_id)
    secret = _secret(client, admin_token, service_id)
    _post_hook(client, slug, secret, _pull_request_body())

    assert (
        client.delete(
            f"/api/ci/services/{service_id}", headers=auth_headers(admin_token)
        ).status_code
        == 200
    )
    with app.app_context():
        assert CiMergeCheckConfig.query.count() == 0
        assert CiMergeCheck.query.count() == 0


def test_a_service_with_only_a_merge_check_pipeline_still_builds_normally(
    app, client, admin_token, service_id
):
    """The merge check pipeline must never become what Run Build runs."""
    with app.app_context():
        from api.models_ci import CiPipeline

        for pipeline in CiPipeline.query.filter_by(service_id=service_id).all():
            db.session.delete(pipeline)
        db.session.commit()

    _enable(client, admin_token, service_id)

    with app.app_context():
        from api.models_ci import CiPipeline
        from api.services.ci import pipelines as pipelines_service

        service = db.session.get(CiService, service_id)
        merge_pipeline = CiPipeline.query.filter_by(
            service_id=service_id, purpose="merge_check"
        ).one()
        assert merge_pipeline.is_default is False
        assert service.default_pipeline() is None

        # Run Build falls back to the generated default for this application
        # type, exactly as it did before merge checks existed.
        resolved, resolved_stages = pipelines_service.resolve_for_build(service)
        assert resolved.id != merge_pipeline.id
        assert [stage.name for stage in resolved_stages] != [
            stage.name for stage in merge_pipeline.stages
        ]


# ---------------------------------------------------------------------------
# Editing what a check runs
# ---------------------------------------------------------------------------

def test_a_check_script_can_be_edited_and_reaches_the_pipeline(
    app, client, admin_token, service_id
):
    _enable(client, admin_token, service_id)
    response = client.put(
        f"/api/ci/services/{service_id}/merge-checks",
        json={"customCommands": {"eslint": "npm run lint:ci\necho done"}},
        headers=auth_headers(admin_token),
    )
    assert response.status_code == 200
    data = response.get_json()["data"]

    eslint = next(item for item in data["checkScripts"] if item["tool"] == "eslint")
    assert eslint["customized"] is True
    assert eslint["commands"] == ["npm run lint:ci", "echo done"]
    # The default is still offered, so "reset" knows what it would restore.
    assert any("package.json" in line for line in eslint["defaultCommands"])

    stage = next(s for s in data["pipelineStages"] if s["tool"] == "eslint")
    assert stage["commands"] == ["npm run lint:ci", "echo done"]
    # Its wiring is untouched — an edited script keeps its image.
    assert stage["image"]


def test_an_edited_script_survives_a_gate_change(
    app, client, admin_token, service_id
):
    """The reason overrides live on the config and not only on the stage.

    Changing a severity floor regenerates the stages. An edit that a settings
    change silently threw away would be worse than not being editable.
    """
    _enable(client, admin_token, service_id)
    client.put(
        f"/api/ci/services/{service_id}/merge-checks",
        json={"customCommands": {"eslint": "npm run lint:ci"}},
        headers=auth_headers(admin_token),
    )
    data = client.put(
        f"/api/ci/services/{service_id}/merge-checks",
        json={"dependencyMinSeverity": "critical", "eslintCountWarnings": True},
        headers=auth_headers(admin_token),
    ).get_json()["data"]

    stage = next(s for s in data["pipelineStages"] if s["tool"] == "eslint")
    assert stage["commands"] == ["npm run lint:ci"]
    # ...while the check that was NOT edited did follow the new floor.
    dc = next(s for s in data["pipelineStages"] if s["tool"] == "dependency_check")
    assert any("CRITICAL" in line and "HIGH" not in line for line in dc["commands"])


def test_resetting_a_script_puts_the_generated_one_back(
    app, client, admin_token, service_id
):
    _enable(client, admin_token, service_id)
    client.put(
        f"/api/ci/services/{service_id}/merge-checks",
        json={"customCommands": {"eslint": "npm run lint:ci"}},
        headers=auth_headers(admin_token),
    )
    data = client.put(
        f"/api/ci/services/{service_id}/merge-checks",
        json={"customCommands": {"eslint": []}},
        headers=auth_headers(admin_token),
    ).get_json()["data"]

    eslint = next(item for item in data["checkScripts"] if item["tool"] == "eslint")
    assert eslint["customized"] is False
    assert eslint["commands"] == eslint["defaultCommands"]
    stage = next(s for s in data["pipelineStages"] if s["tool"] == "eslint")
    assert any("package.json" in line for line in stage["commands"])


def test_an_edited_script_is_judged_by_the_metric_line_it_prints(
    app, client, admin_token, service_id, captured_verdicts
):
    """An edit keeps the gate — the sentinel is the whole contract."""
    _enable(client, admin_token, service_id, tools=["eslint"])
    client.put(
        f"/api/ci/services/{service_id}/merge-checks",
        json={"customCommands": {"eslint": "yarn lint --format json"}},
        headers=auth_headers(admin_token),
    )
    check_id = _run_to_verdict(
        app,
        client,
        admin_token,
        service_id,
        {"eslint": "##kubesight-metric tool=eslint status=ok problems=9"},
    )
    with app.app_context():
        check = db.session.get(CiMergeCheck, check_id)
        assert check.verdict == "blocked"
        assert check.total_problems == 9


def test_a_script_for_a_check_KubeSight_does_not_run_is_refused(
    client, admin_token, service_id
):
    _enable(client, admin_token, service_id)
    response = client.put(
        f"/api/ci/services/{service_id}/merge-checks",
        json={"customCommands": {"spotbugs": "echo hi"}},
        headers=auth_headers(admin_token),
    )
    assert response.status_code == 400
    assert "spotbugs" in response.get_json()["error"]


# ---------------------------------------------------------------------------
# Is the merge actually blocked?
# ---------------------------------------------------------------------------

def _fake_restrictions(monkeypatch, items):
    from api.services.ci.source import bitbucket_status

    monkeypatch.setattr(
        bitbucket_status, "fetch_build_restrictions", lambda **kwargs: items
    )


def test_enforcement_reports_a_branch_that_nothing_protects(
    client, admin_token, service_id, monkeypatch
):
    _enable(client, admin_token, service_id, targetBranches=["master"])
    _fake_restrictions(
        monkeypatch, [{"pattern": "release/*", "matchKind": "glob", "minimum": 1}]
    )
    data = client.get(
        f"/api/ci/services/{service_id}/merge-checks/enforcement",
        headers=auth_headers(admin_token),
    ).get_json()["data"]

    assert data["known"] is True
    # A restriction exists, but not on the branch this gate watches — which is
    # exactly the case a naive "any restriction" check would call protected.
    assert data["enforced"] is False
    assert data["uncovered"] == ["master"]


def test_enforcement_reports_a_protected_branch_as_enforced(
    client, admin_token, service_id, monkeypatch
):
    _enable(client, admin_token, service_id, targetBranches=["master", "release/9.1"])
    _fake_restrictions(
        monkeypatch,
        [
            {"pattern": "master", "matchKind": "glob", "minimum": 1},
            {"pattern": "release/*", "matchKind": "glob", "minimum": 2},
        ],
    )
    data = client.get(
        f"/api/ci/services/{service_id}/merge-checks/enforcement",
        headers=auth_headers(admin_token),
    ).get_json()["data"]

    assert data["enforced"] is True
    assert sorted(data["covered"]) == ["master", "release/9.1"]


def test_a_restriction_of_zero_builds_does_not_count_as_enforcement(
    client, admin_token, service_id, monkeypatch
):
    _enable(client, admin_token, service_id, targetBranches=["master"])
    _fake_restrictions(
        monkeypatch, [{"pattern": "master", "matchKind": "glob", "minimum": 0}]
    )
    data = client.get(
        f"/api/ci/services/{service_id}/merge-checks/enforcement",
        headers=auth_headers(admin_token),
    ).get_json()["data"]
    assert data["enforced"] is False


def test_enforcement_says_so_when_bitbucket_could_not_be_asked(
    client, admin_token, service_id, monkeypatch
):
    from api.services.ci.source import bitbucket_status

    def boom(**kwargs):
        raise bitbucket_status.StatusWriteError("Bitbucket could not be reached.")

    monkeypatch.setattr(bitbucket_status, "fetch_build_restrictions", boom)
    _enable(client, admin_token, service_id)
    data = client.get(
        f"/api/ci/services/{service_id}/merge-checks/enforcement",
        headers=auth_headers(admin_token),
    ).get_json()["data"]

    # "We could not look" must never render as "you are protected".
    assert data["known"] is False
    assert data["enforced"] is False
    assert "could not be reached" in data["reason"]


# ---------------------------------------------------------------------------
# Semgrep — the same gate with no server to run
# ---------------------------------------------------------------------------

def test_semgrep_gates_a_merge_with_no_sonarqube_anywhere(
    app, client, admin_token, service_id, captured_verdicts
):
    """The point of offering Semgrep: a full verdict without a third-party server."""
    _enable(client, admin_token, service_id, tools=["semgrep"], maxTotalProblems=2)

    payload = client.get(
        f"/api/ci/services/{service_id}/merge-checks", headers=auth_headers(admin_token)
    ).get_json()["data"]
    stage = next(s for s in payload["pipelineStages"] if s["tool"] == "semgrep")
    script = "\n".join(stage["commands"])
    # No server, no credential, no upload — and no telemetry.
    assert "SONAR_HOST_URL" not in script
    assert "--metrics=off" in script
    assert "semgrep scan" in script

    check_id = _run_to_verdict(
        app,
        client,
        admin_token,
        service_id,
        {
            "semgrep": (
                "##kubesight-metric tool=semgrep status=ok problems=3 "
                "errors=1 warnings=2 info=7"
            )
        },
    )
    with app.app_context():
        check = db.session.get(CiMergeCheck, check_id)
        assert check.verdict == "blocked"
        assert check.total_problems == 3
        # The informational findings are reported and not counted, which is what
        # the severity floor means.
        assert check.metrics["semgrep"]["info"] == 7
    assert captured_verdicts["statuses"][-1]["state"] == "failed"


def test_the_semgrep_severity_floor_is_compiled_into_its_script(
    client, admin_token, service_id
):
    _enable(client, admin_token, service_id, tools=["semgrep"])

    def script_for(floor):
        data = client.put(
            f"/api/ci/services/{service_id}/merge-checks",
            json={"semgrepMinSeverity": floor},
            headers=auth_headers(admin_token),
        ).get_json()["data"]
        stage = next(s for s in data["pipelineStages"] if s["tool"] == "semgrep")
        return "\n".join(stage["commands"])

    # A floor change has to reach the STAGE, not only the gate — the counting
    # happens in the container, so a script left behind would count the wrong
    # set while the settings page claimed otherwise.
    assert "'ERROR','WARNING'" in script_for("medium")
    strict = script_for("critical")
    assert "'ERROR',)" in strict and "WARNING" not in strict.split("counted=")[1][:60]


def test_semgrep_and_sonar_can_both_be_off_or_both_on(
    client, admin_token, service_id
):
    """Neither replaces the other; a site that runs SonarQube keeps it."""
    data = _enable(
        client, admin_token, service_id, tools=["semgrep", "sonar"]
    ).get_json()["data"]
    assert [s["name"] for s in data["pipelineStages"]] == [
        "Checkout",
        "Semgrep scan",
        "SonarQube scan",
    ]
