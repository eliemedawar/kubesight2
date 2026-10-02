"""Post actions: what a pipeline does when a build ends (Jenkins' ``post {}``).

What is locked here:

* the configuration rules (types, when, recipients, the webhook URL living in
  a CI secret, cleanup commands held to the command-stage rules) and that they
  are saved with the pipeline, through the same PUT, and left alone by a save
  that does not mention them;
* notifications fire once, at the build's TRUE end — after approval and deploy
  stages — for the results their ``when`` names; cancelled triggers ``always``
  only; ``fixed`` is the first success after a failure;
* nothing is sent on the engine's pass: deliveries are claimed (committed)
  first, retried with backoff, and a claim that died mid-send is closed as
  interrupted, never sent twice;
* webhook payloads per format, the URL resolved from the secret at send time
  and never echoed;
* cleanup commands run after the runner stages, with the runner phase's
  outcome, before any server stage — and never change the build's result;
* on Kubernetes they are post-N initContainers after every stage and before
  the collector, deciding by the fail flag (run for real under ``sh``);
* the snapshot is what a build runs, a retry takes the pipeline as it is now.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from api.db import db
from api.models_application_intelligence import BitbucketCredentialProfile
from api.models_ci import CiBuild, CiBuildStage
from api.secret_encryption import encrypt_secret
from api.services.ci import post_actions
from api.services.ci.runners import base as runner_base
from api.services.ci.runners import kubernetes as k8s
from api.services.ci.runners.base import RunnerHandle, StageExecution
from tests.conftest import auth_headers


# ---------------------------------------------------------------------------
# Fixtures and helpers
# ---------------------------------------------------------------------------

@pytest.fixture()
def service(app, client, admin_token):
    with app.app_context():
        credential = BitbucketCredentialProfile(
            name="ci-token",
            provider="bitbucket",
            credential_type="repository_access_token",
            secret_cipher=encrypt_secret("clone-token-value"),
            read_only=True,
            enabled=True,
        )
        db.session.add(credential)
        db.session.commit()
        credential_id = credential.id
    service_id = client.post(
        "/api/ci/services",
        json={"name": "Payments Api", "applicationType": "java"},
        headers=auth_headers(admin_token),
    ).get_json()["data"]["id"]
    client.put(
        f"/api/ci/services/{service_id}/source",
        json={
            "repositoryUrl": "https://bitbucket.org/areeba/payments-api",
            "defaultBranch": "main",
            "credentialProfileId": credential_id,
        },
        headers=auth_headers(admin_token),
    )
    pipeline_id = client.get(
        f"/api/ci/services/{service_id}/pipelines", headers=auth_headers(admin_token)
    ).get_json()["data"]["items"][0]["id"]
    return SimpleNamespace(id=service_id, pipeline_id=pipeline_id)


@pytest.fixture(autouse=True)
def _reset_hooks():
    yield
    post_actions.set_delivery_runner(None)
    post_actions._in_flight.clear()
    k8s.set_kubectl_runner(None)


def _stages(*, approval=False):
    stages = [
        {"name": "Checkout", "stageType": "checkout", "runnerLabels": ["mock"]},
        {"name": "Build", "stageType": "command", "commands": ["make"], "runnerLabels": ["mock"]},
    ]
    if approval:
        stages.append(
            {
                "name": "Approve release",
                "stageType": "approval",
                "approval": {
                    "instructions": "Check it.",
                    "users": [],
                    "anyoneWithPermission": True,
                    "minApprovals": 1,
                    "allowSelfApproval": False,
                    "notify": False,
                },
                "timeoutSeconds": 3600,
            }
        )
    return stages


def _email(when, **extra):
    return {"type": "email", "when": when, "recipients": ["dev@example.com"], **extra}


def _cleanup(when, name=None, **extra):
    return {
        "type": "commands",
        "when": when,
        "name": name or f"Clean {when}",
        "commands": ["rm -rf build/tmp"],
        **extra,
    }


def _save(client, token, pipeline_id, post, stages=None):
    return client.put(
        f"/api/ci/pipelines/{pipeline_id}",
        json={"parameters": [], "stages": stages or _stages(), "postActions": post},
        headers=auth_headers(token),
    )


def _secret(client, token, service_id, key, value):
    response = client.post(
        f"/api/ci/services/{service_id}/secrets",
        json={"key": key, "value": value},
        headers=auth_headers(token),
    )
    assert response.status_code in (200, 201), response.get_json()


def _trigger(client, token, service_id):
    response = client.post(f"/api/ci/services/{service_id}/builds", json={}, headers=auth_headers(token))
    assert response.status_code in (200, 201), response.get_json()
    return response.get_json()["data"]["id"]


def _advance(app, build_id, *, passes=60, fail=(), fail_post=(), stage_seconds=0.0, stop_at_approval=True):
    """Drive the engine with the mock runner. ``fail`` names stages whose poll
    reports FAILED; ``fail_post`` the same for post-action rows."""
    from api.services.ci import engine
    from api.services.ci.runners import get_adapter
    from api.services.ci.runners import mock as mock_runner

    adapter = get_adapter("mock")
    original_poll = adapter.poll

    def poll(handle):
        try:
            row = db.session.get(CiBuildStage, int(handle.external_ref.rsplit("-", 1)[1]))
        except (ValueError, IndexError):
            row = None
        if row is not None and (row.name in fail or row.name in fail_post):
            return runner_base.FAILED
        return original_poll(handle)

    original_seconds = mock_runner._STAGE_SECONDS
    mock_runner._STAGE_SECONDS = stage_seconds
    adapter.poll = poll
    try:
        with app.app_context():
            for _ in range(passes):
                engine.advance_ci_builds()
                build = db.session.get(CiBuild, build_id)
                if build.status not in ("queued", "running"):
                    # One more pass: the delivery step follows the transition.
                    engine.advance_ci_builds()
                    break
                waiting = next((s for s in build.stages if s.stage_type == "approval"), None)
                if stop_at_approval and waiting is not None and waiting.status == "running":
                    break
    finally:
        adapter.poll = original_poll
        mock_runner._STAGE_SECONDS = original_seconds


def _build(client, token, build_id):
    return client.get(f"/api/ci/builds/{build_id}", headers=auth_headers(token)).get_json()["data"]


def _by_name(data):
    return {item["name"]: item for item in data["postActions"]}


def _logs(client, token, build_id, stage_id):
    data = client.get(
        f"/api/ci/builds/{build_id}/stages/{stage_id}/logs", headers=auth_headers(token)
    ).get_json()["data"]
    return "\n".join(line["content"] for line in data["lines"])


class _Mailbox:
    def __init__(self):
        self.sent = []

    def __call__(self, to, subject, body, **kwargs):
        self.sent.append({"to": to, "subject": subject, "body": body, **kwargs})


def _smtp(mailbox):
    return patch("api.email_delivery.send_email", mailbox), patch(
        "api.email_delivery.smtp_is_configured", return_value=True
    )


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "action, message",
    [
        ({"type": "sms"}, "unknown type"),
        ({"type": "email", "when": "sometimes", "recipients": ["a@b.co"]}, "'when' must be one of"),
        ({"type": "email", "when": "failure", "recipients": []}, "at least one recipient"),
        ({"type": "email", "recipients": ["not-an-address"]}, "is not an email address"),
        ({"type": "email", "recipients": [f"u{i}@example.com" for i in range(26)]}, "at most 25"),
        ({"type": "webhook", "format": "slack"}, "needs the CI secret"),
        ({"type": "webhook", "format": "slack", "urlSecret": "NOPE"}, "not defined for this service"),
        ({"type": "webhook", "format": "carrier-pigeon", "urlSecret": "HOOK"}, "format must be"),
        ({"type": "commands", "when": "fixed", "commands": ["true"]}, "cannot be 'fixed'"),
        ({"type": "commands", "commands": []}, "has no commands"),
        ({"type": "commands", "commands": ["true"], "timeoutSeconds": 5}, "timeout must be between"),
        ({"type": "commands", "commands": ["true"], "secretRefs": [{"name": "GONE"}]}, "not defined"),
        ({"type": "commands", "commands": ["true"], "workingDirectory": "../etc"}, "Cleanup"),
    ],
)
def test_invalid_post_actions_are_refused_with_a_reason(action, message):
    with pytest.raises(post_actions.PostActionError, match=message):
        post_actions.normalize([action], {"HOOK"})


def test_post_actions_normalize_to_their_saved_shape():
    saved = post_actions.normalize(
        [
            {"type": "email", "when": "failure", "recipients": "a@example.com, b@example.com; a@example.com",
             "subject": "  {service}   {result} ", "message": "Line one\nLine two"},
            {"type": "webhook", "when": "fixed", "format": "generic", "urlSecret": "HOOK"},
            {"type": "commands", "commands": ["", "rm -rf tmp", "  echo done  ", ""], "secretRefs": ["HOOK"]},
        ],
        {"HOOK"},
    )
    assert saved[0] == {
        "type": "email", "when": "failure", "recipients": ["a@example.com", "b@example.com"],
        "subject": "{service} {result}", "message": "Line one\nLine two",
    }
    assert saved[1] == {"type": "webhook", "when": "fixed", "urlSecret": "HOOK", "format": "json"}
    assert saved[2]["when"] == "always" and saved[2]["name"] == "Cleanup"
    assert saved[2]["commands"] == ["rm -rf tmp", "  echo done"]
    assert saved[2]["secretRefs"] == [{"name": "HOOK", "envVar": "HOOK"}]
    assert saved[2]["timeoutSeconds"] == post_actions.DEFAULT_CLEANUP_TIMEOUT
    with pytest.raises(post_actions.PostActionError, match="at most 10"):
        post_actions.normalize([_email("always")] * 11, set())
    with pytest.raises(post_actions.PostActionError, match="must be unique"):
        post_actions.normalize([_cleanup("always", "Tidy"), _cleanup("failure", "tidy")], set())


def test_post_actions_save_with_the_pipeline_and_survive_saves_that_omit_them(
    client, admin_token, service
):
    bad = _save(client, admin_token, service.pipeline_id, [{"type": "webhook", "urlSecret": "SLACK_URL"}])
    assert bad.status_code == 400
    assert "SLACK_URL" in bad.get_json()["error"]

    _secret(client, admin_token, service.id, "SLACK_URL", "https://hooks.slack.test/T000/B000/xyz")
    ok = _save(
        client, admin_token, service.pipeline_id,
        [_email("failure"), {"type": "webhook", "when": "always", "format": "slack", "urlSecret": "SLACK_URL"}],
    )
    assert ok.status_code == 200, ok.get_json()
    assert [a["type"] for a in ok.get_json()["data"]["postActions"]] == ["email", "webhook"]

    # A save that does not mention them (an older client, an MCP stage edit).
    client.put(
        f"/api/ci/pipelines/{service.pipeline_id}",
        json={"parameters": [], "stages": _stages()},
        headers=auth_headers(admin_token),
    )
    listed = client.get(
        f"/api/ci/services/{service.id}/pipelines", headers=auth_headers(admin_token)
    ).get_json()["data"]["items"][0]
    assert [a["type"] for a in listed["postActions"]] == ["email", "webhook"]

    cleared = _save(client, admin_token, service.pipeline_id, [])
    assert cleared.get_json()["data"]["postActions"] == []


# ---------------------------------------------------------------------------
# Notifications: which results fire which `when`
# ---------------------------------------------------------------------------

def test_a_successful_build_sends_always_and_success_and_says_why_the_rest_did_not(
    app, client, admin_token, service
):
    _save(client, admin_token, service.pipeline_id, [
        _email("always"), _email("success"), _email("failure"), _email("fixed"),
    ])
    mailbox = _Mailbox()
    send, configured = _smtp(mailbox)
    with send, configured:
        build_id = _trigger(client, admin_token, service.id)
        _advance(app, build_id)

    data = _build(client, admin_token, build_id)
    assert data["status"] == "success"
    assert len(mailbox.sent) == 2
    rows = _by_name(data)
    assert rows["Email · always"]["status"] == "success"
    assert rows["Email · on success"]["status"] == "success"
    assert rows["Email · always"]["detail"] == "Email sent to dev@example.com."
    assert rows["Email · on failure"]["status"] == "skipped"
    assert rows["Email · when fixed"]["status"] == "skipped"
    log = _logs(client, admin_token, build_id, rows["Email · when fixed"]["id"])
    assert "previous build had not failed" in log
    # Post actions are not stages: the stage list and progress are untouched.
    assert [s["name"] for s in data["stages"]] == ["Checkout", "Build"]
    assert mailbox.sent[0]["subject"] == "[KubeSight] Payments Api #1 succeeded (main)"


def test_a_failed_build_sends_always_and_failure_with_the_failed_stage(
    app, client, admin_token, service, monkeypatch
):
    monkeypatch.setenv("PUBLIC_BASE_URL", "https://kubesight.example.com")
    _save(client, admin_token, service.pipeline_id, [_email("always"), _email("success"), _email("failure")])
    mailbox = _Mailbox()
    send, configured = _smtp(mailbox)
    with send, configured:
        build_id = _trigger(client, admin_token, service.id)
        _advance(app, build_id, fail=("Build",))

    data = _build(client, admin_token, build_id)
    assert data["status"] == "failed"
    rows = _by_name(data)
    assert rows["Email · always"]["status"] == "success"
    assert rows["Email · on failure"]["status"] == "success"
    assert rows["Email · on success"]["status"] == "skipped"
    assert len(mailbox.sent) == 2
    body = mailbox.sent[0]["body"]
    assert "Payments Api build #1 failed." in body
    assert "Failed stage:" in body and "Build" in body
    assert "Branch:" in body and "main" in body
    assert f"https://kubesight.example.com/#/service-catalog/{service.id}/builds" in body
    assert "Open the build" in mailbox.sent[0]["html_body"]


def test_a_cancelled_build_sends_only_always(app, client, admin_token, service):
    from api.services.ci import engine
    from api.services.ci.runners import mock as mock_runner

    _save(client, admin_token, service.pipeline_id, [_email("always"), _email("failure"), _cleanup("always")])
    mailbox = _Mailbox()
    send, configured = _smtp(mailbox)
    original = mock_runner._STAGE_SECONDS
    mock_runner._STAGE_SECONDS = 600.0
    try:
        with send, configured:
            build_id = _trigger(client, admin_token, service.id)
            with app.app_context():
                engine.advance_ci_builds()
                engine.advance_ci_builds()
            response = client.post(f"/api/ci/builds/{build_id}/cancel", headers=auth_headers(admin_token))
            assert response.status_code == 200, response.get_json()
            with app.app_context():
                engine.advance_ci_builds()
                engine.advance_ci_builds()
    finally:
        mock_runner._STAGE_SECONDS = original

    data = _build(client, admin_token, build_id)
    assert data["status"] == "cancelled"
    rows = _by_name(data)
    assert rows["Email · always"]["status"] == "success"
    assert rows["Email · on failure"]["status"] == "skipped"
    assert "not a failure" in _logs(client, admin_token, build_id, rows["Email · on failure"]["id"])
    cleanup = rows["Clean always · always"]
    assert cleanup["status"] == "skipped"
    assert "cancelled" in _logs(client, admin_token, build_id, cleanup["id"])
    assert len(mailbox.sent) == 1


def test_a_build_cancelled_in_the_queue_sends_nothing(app, client, admin_token, service):
    _save(client, admin_token, service.pipeline_id, [_email("always")])
    mailbox = _Mailbox()
    send, configured = _smtp(mailbox)
    with send, configured:
        build_id = _trigger(client, admin_token, service.id)
        client.post(f"/api/ci/builds/{build_id}/cancel", headers=auth_headers(admin_token))
        _advance(app, build_id, passes=2)
    rows = _by_name(_build(client, admin_token, build_id))
    assert rows["Email · always"]["status"] == "skipped"
    assert mailbox.sent == []


def test_fixed_fires_on_the_first_success_after_a_failure_only(app, client, admin_token, service):
    _save(client, admin_token, service.pipeline_id, [_email("fixed")])
    mailbox = _Mailbox()
    send, configured = _smtp(mailbox)
    with send, configured:
        first = _trigger(client, admin_token, service.id)
        _advance(app, first, fail=("Build",))
        second = _trigger(client, admin_token, service.id)
        _advance(app, second)
        third = _trigger(client, admin_token, service.id)
        _advance(app, third)

    assert _by_name(_build(client, admin_token, first))["Email · when fixed"]["status"] == "skipped"
    assert _by_name(_build(client, admin_token, second))["Email · when fixed"]["status"] == "success"
    assert _by_name(_build(client, admin_token, third))["Email · when fixed"]["status"] == "skipped"
    assert len(mailbox.sent) == 1


def test_notifications_wait_for_the_approval_and_report_the_true_end(
    app, client, admin_token, operator_token, service
):
    _save(client, admin_token, service.pipeline_id, [_email("success")], stages=_stages(approval=True))
    mailbox = _Mailbox()
    send, configured = _smtp(mailbox)
    with send, configured:
        build_id = _trigger(client, admin_token, service.id)
        _advance(app, build_id)
        data = _build(client, admin_token, build_id)
        assert data["status"] == "running"
        assert data["awaitingApproval"] is not None
        assert mailbox.sent == []
        assert _by_name(data)["Email · on success"]["status"] == "pending"

        approval = next(s for s in data["stages"] if s["stageType"] == "approval")
        response = client.post(
            f"/api/ci/builds/{build_id}/stages/{approval['id']}/approve",
            json={"comment": "ship"},
            headers=auth_headers(operator_token),
        )
        assert response.status_code == 200, response.get_json()
        _advance(app, build_id)

    data = _build(client, admin_token, build_id)
    assert data["status"] == "success"
    assert len(mailbox.sent) == 1
    with app.app_context():
        build = db.session.get(CiBuild, build_id)
        row = post_actions.rows(build)[0]
        approval_row = next(s for s in build.stages if s.stage_type == "approval")
        assert row.finished_at >= approval_row.finished_at


def test_email_without_smtp_fails_visibly_and_is_not_retried(app, client, admin_token, service):
    _save(client, admin_token, service.pipeline_id, [_email("always")])
    with patch("api.email_delivery.smtp_is_configured", return_value=False):
        build_id = _trigger(client, admin_token, service.id)
        _advance(app, build_id)
    row = _by_name(_build(client, admin_token, build_id))["Email · always"]
    assert row["status"] == "failed"
    assert "SMTP" in row["error"]
    assert row["attempts"] == 1


# ---------------------------------------------------------------------------
# Webhooks: payload, secret, retry, never twice
# ---------------------------------------------------------------------------

def _facts(**overrides):
    facts = {
        "service": "Payments Api", "serviceSlug": "payments-api", "pipeline": "default",
        "buildId": 9, "buildNumber": 12, "status": "failed", "result": "failed",
        "branch": "main", "refType": "branch", "commit": "0123456789abcdef", "commitShort": "0123456789ab",
        "commitMessage": "Fix <login> & more", "trigger": "manual", "requestedBy": "admin",
        "durationSeconds": 192, "duration": "3m 12s", "finishedAt": "2026-10-02T10:00:00+00:00",
        "failedStage": "Build", "reason": "Stage 'Build' failed.",
        "tests": {"total": 10, "passed": 8, "failed": 2, "errors": 0, "skipped": 0, "linesPct": 81.5},
        "testsLine": "8 passed, 2 failed, 81.5% line coverage",
        "link": "https://ks.example.com/#/service-catalog/3/builds",
    }
    facts.update(overrides)
    return facts


def test_webhook_payloads_carry_the_same_facts_in_each_format():
    slack = post_actions.webhook_payload(_facts(), "slack")
    assert slack["text"].startswith(":x: Payments Api #12 failed")
    assert "<https://ks.example.com/#/service-catalog/3/builds|Payments Api #12>" in slack["blocks"][0]["text"]["text"]
    fields = " ".join(f["text"] for f in slack["blocks"][1]["fields"])
    assert "main" in fields and "0123456789ab" in fields and "3m 12s" in fields
    assert "*Failed stage:* Build" in slack["blocks"][2]["text"]["text"]
    assert "8 passed, 2 failed" in slack["blocks"][3]["elements"][0]["text"]

    teams = post_actions.webhook_payload(_facts(status="success", result="succeeded", failedStage="", reason=""), "teams")
    card = teams["attachments"][0]["content"]
    assert teams["type"] == "message"
    assert teams["attachments"][0]["contentType"] == "application/vnd.microsoft.card.adaptive"
    assert card["type"] == "AdaptiveCard" and card["body"][0]["color"] == "Good"
    assert {"title": "Branch", "value": "main"} in card["body"][1]["facts"]
    assert card["actions"][0]["url"] == "https://ks.example.com/#/service-catalog/3/builds"

    generic = post_actions.webhook_payload(_facts(), "json")
    assert generic["event"] == "ci.build.finished"
    assert generic["build"]["number"] == 12 and generic["build"]["status"] == "failed"
    assert generic["failedStage"] == {"name": "Build", "reason": "Stage 'Build' failed."}
    assert generic["tests"]["failed"] == 2
    json.dumps(generic)  # serializable as sent


def test_slack_text_is_escaped():
    payload = post_actions.webhook_payload(_facts(service="A <b> & c", link=""), "slack")
    assert "A &lt;b&gt; &amp; c" in payload["blocks"][0]["text"]["text"]


def test_webhook_url_comes_from_the_secret_and_is_never_shown(app, client, admin_token, service):
    url = "https://hooks.slack.test/services/T000/B000/very-secret-token"
    _secret(client, admin_token, service.id, "SLACK_URL", url)
    _save(client, admin_token, service.pipeline_id, [
        {"type": "webhook", "when": "always", "format": "slack", "urlSecret": "SLACK_URL"},
    ])
    calls = []

    def fake_post(target, body, timeout):
        calls.append((target, json.loads(body)))
        return 200

    with patch.object(post_actions, "_http_post", fake_post):
        build_id = _trigger(client, admin_token, service.id)
        _advance(app, build_id)

    assert len(calls) == 1
    assert calls[0][0] == url
    assert calls[0][1]["text"].startswith(":white_check_mark: Payments Api #1 succeeded")
    response = client.get(f"/api/ci/builds/{build_id}", headers=auth_headers(admin_token))
    assert "very-secret-token" not in response.get_data(as_text=True)
    row = _by_name(response.get_json()["data"])["Slack · always"]
    assert row["status"] == "success" and row["urlSecret"] == "SLACK_URL"
    assert row["detail"] == "Slack notified (HTTP 200)."


def test_a_failing_webhook_retries_with_backoff_then_succeeds(app, client, admin_token, service):
    from api.services.ci import engine

    _secret(client, admin_token, service.id, "HOOK", "https://hooks.example.test/x")
    _save(client, admin_token, service.pipeline_id, [
        {"type": "webhook", "when": "always", "format": "json", "urlSecret": "HOOK"},
    ])
    answers = [post_actions.DeliveryError("The webhook answered HTTP 503.", retryable=True), 200]

    def flaky(target, body, timeout):
        answer = answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer

    with patch.object(post_actions, "_http_post", flaky):
        build_id = _trigger(client, admin_token, service.id)
        _advance(app, build_id)
        row = _by_name(_build(client, admin_token, build_id))["Webhook · always"]
        assert row["status"] == "pending" and row["attempts"] == 1
        assert row["error"] == "The webhook answered HTTP 503."
        due = datetime.fromisoformat(row["nextAttemptAt"])
        assert due - datetime.now(timezone.utc) > timedelta(seconds=20)

        # Not before its time.
        with app.app_context():
            engine.advance_ci_builds()
        assert len(answers) == 1
        # Time passes.
        with app.app_context():
            stored = db.session.get(CiBuildStage, row["id"])
            stored.server_state = {**stored.server_state, "nextAttemptAt": "2000-01-01T00:00:00+00:00"}
            db.session.commit()
            engine.advance_ci_builds()

    row = _by_name(_build(client, admin_token, build_id))["Webhook · always"]
    assert row["status"] == "success" and row["attempts"] == 2
    assert row["error"] is None
    log = _logs(client, admin_token, build_id, row["id"])
    assert "Attempt 1 failed" in log and "Retrying in 30s" in log


def test_a_rejected_webhook_is_not_retried_and_a_deleted_secret_says_so(app, client, admin_token, service):
    _secret(client, admin_token, service.id, "HOOK", "https://hooks.example.test/x")
    _save(client, admin_token, service.pipeline_id, [
        {"type": "webhook", "when": "always", "format": "teams", "urlSecret": "HOOK"},
    ])
    with patch.object(
        post_actions, "_http_post",
        side_effect=post_actions.DeliveryError("The webhook answered HTTP 404.", retryable=False),
    ):
        build_id = _trigger(client, admin_token, service.id)
        _advance(app, build_id)
    row = _by_name(_build(client, admin_token, build_id))["Teams · always"]
    assert row["status"] == "failed" and row["attempts"] == 1 and "404" in row["error"]

    secrets = client.get(f"/api/ci/services/{service.id}/secrets", headers=auth_headers(admin_token)).get_json()["data"]
    items = secrets["items"] if isinstance(secrets, dict) else secrets
    secret_id = next(item["id"] for item in items if item["key"] == "HOOK")
    client.delete(f"/api/ci/secrets/{secret_id}", headers=auth_headers(admin_token))
    with patch.object(post_actions, "_http_post", side_effect=AssertionError("must not be called")):
        build_id = _trigger(client, admin_token, service.id)
        _advance(app, build_id)
    row = _by_name(_build(client, admin_token, build_id))["Teams · always"]
    assert row["status"] == "failed" and "'HOOK'" in row["error"]


def test_a_claim_is_committed_before_sending_and_never_sent_twice(app, client, admin_token, service):
    """A process that dies mid-send leaves a claim; nobody sends it again."""
    from api.services.ci import engine

    _save(client, admin_token, service.pipeline_id, [_email("always")])
    claimed = []
    # The "worker" never reports back — the process died while sending.
    post_actions.set_delivery_runner(claimed.append)
    mailbox = _Mailbox()
    send, configured = _smtp(mailbox)
    with send, configured:
        build_id = _trigger(client, admin_token, service.id)
        _advance(app, build_id)
        assert len(claimed) == 1
        with app.app_context():
            row = db.session.get(CiBuildStage, claimed[0])
            assert row.status == "running" and row.server_state["phase"] == "sending"
            # Another pass while the claim is fresh: not claimed again.
            engine.advance_ci_builds()
            assert len(claimed) == 1
            # After a restart, long past any send's timeout.
            stale = (datetime.now(timezone.utc) - timedelta(seconds=post_actions.STALE_CLAIM_SECONDS + 5)).isoformat()
            row.server_state = {**row.server_state, "claimedAt": stale}
            db.session.commit()
            post_actions.set_delivery_runner(None)
            engine.advance_ci_builds()
            engine.advance_ci_builds()

    row = _by_name(_build(client, admin_token, build_id))["Email · always"]
    assert row["status"] == "failed"
    assert "interrupted" in row["error"] and "not retried" in row["error"]
    assert mailbox.sent == []
    assert len(claimed) == 1


def test_delivery_never_runs_on_the_engine_pass(app, client, admin_token, service):
    """Outside tests the claimed row goes to the worker pool, not inline."""
    _save(client, admin_token, service.pipeline_id, [_email("always")])
    submitted = []

    class Pool:
        def submit(self, fn, *args):
            submitted.append(args)

    with patch.object(post_actions, "_pool", return_value=Pool()):
        app.config["TESTING"] = False
        try:
            build_id = _trigger(client, admin_token, service.id)
            _advance(app, build_id)
        finally:
            app.config["TESTING"] = True
    assert len(submitted) == 1
    row = _by_name(_build(client, admin_token, build_id))["Email · always"]
    assert row["status"] == "running" and row["phase"] == "sending"


def test_email_render_includes_tests_and_custom_text():
    subject, text, html_body = post_actions.render_email(
        _facts(),
        {"subject": "{service} {build} {result} on {branch}", "message": "Heads up, {service}."},
    )
    assert subject == "Payments Api #12 failed on main"
    assert text.startswith("Heads up, Payments Api.")
    assert "Tests:" in text and "8 passed, 2 failed, 81.5% line coverage" in text
    assert "Reason:" in text and "Stage 'Build' failed." in text
    assert "Fix &lt;login&gt; &amp; more" in html_body


# ---------------------------------------------------------------------------
# Cleanup commands on a per-stage runner (mock)
# ---------------------------------------------------------------------------

def test_cleanup_runs_for_the_matching_outcome_on_success(app, client, admin_token, service):
    from api.services.ci.runners import get_adapter

    _save(client, admin_token, service.pipeline_id, [
        _cleanup("always"), _cleanup("success"), _cleanup("failure"),
    ])
    adapter = get_adapter("mock")
    started = []
    original_start = adapter.start

    def start(execution):
        started.append(execution)
        return original_start(execution)

    adapter.start = start
    try:
        build_id = _trigger(client, admin_token, service.id)
        _advance(app, build_id)
    finally:
        adapter.start = original_start

    data = _build(client, admin_token, build_id)
    assert data["status"] == "success"
    rows = _by_name(data)
    assert rows["Clean always · always"]["status"] == "success"
    assert rows["Clean success · on success"]["status"] == "success"
    assert rows["Clean failure · on failure"]["status"] == "skipped"
    assert "every stage succeeded" in _logs(client, admin_token, build_id, rows["Clean failure · on failure"]["id"])
    assert "rm -rf build/tmp" in _logs(client, admin_token, build_id, rows["Clean always · always"]["id"])
    post = [e for e in started if e.stage_type == "post"]
    assert [e.position for e in post] == [1000, 1001]
    assert all(e.env["KUBESIGHT_STAGES_RESULT"] == "success" for e in post)
    assert post[0].commands == ["rm -rf build/tmp"]


def test_cleanup_on_failure_runs_after_a_failed_stage(app, client, admin_token, service):
    _save(client, admin_token, service.pipeline_id, [_cleanup("success"), _cleanup("failure")])
    build_id = _trigger(client, admin_token, service.id)
    _advance(app, build_id, fail=("Checkout",))
    data = _build(client, admin_token, build_id)
    assert data["status"] == "failed"
    assert [s["status"] for s in data["stages"]] == ["failed", "skipped"]
    rows = _by_name(data)
    assert rows["Clean failure · on failure"]["status"] == "success"
    assert rows["Clean failure · on failure"]["stagesResult"] == "failure"
    assert rows["Clean success · on success"]["status"] == "skipped"


def test_a_failing_cleanup_never_turns_a_green_build_red(app, client, admin_token, service):
    _save(client, admin_token, service.pipeline_id, [_cleanup("always", "Tidy up")])
    build_id = _trigger(client, admin_token, service.id)
    _advance(app, build_id, fail_post=("Tidy up · always",))
    data = _build(client, admin_token, build_id)
    assert data["status"] == "success"
    row = _by_name(data)["Tidy up · always"]
    assert row["status"] == "failed"
    assert "not affected" in row["error"]


def test_cleanup_runs_before_a_server_stage_starts(app, client, admin_token, service):
    _save(client, admin_token, service.pipeline_id, [_cleanup("always")], stages=_stages(approval=True))
    build_id = _trigger(client, admin_token, service.id)
    _advance(app, build_id)
    with app.app_context():
        build = db.session.get(CiBuild, build_id)
        cleanup = post_actions.rows(build)[0]
        approval = next(s for s in build.stages if s.stage_type == "approval")
        assert cleanup.status == "success"
        assert approval.status == "running"
        assert cleanup.finished_at <= approval.started_at


def test_a_cleanup_holds_the_build_until_it_ends(app, client, admin_token, service):
    """The build is decided only after its cleanup — though not by it."""
    from api.services.ci import engine
    from api.services.ci.runners import get_adapter

    _save(client, admin_token, service.pipeline_id, [_cleanup("always", "Slow")])
    adapter = get_adapter("mock")
    original_poll = adapter.poll
    held = {"post": True}

    def poll(handle):
        row = db.session.get(CiBuildStage, int(handle.external_ref.rsplit("-", 1)[1]))
        if row.stage_type == "post" and held["post"]:
            return runner_base.RUNNING
        return original_poll(handle)

    build_id = _trigger(client, admin_token, service.id)
    adapter.poll = poll
    try:
        _advance(app, build_id, passes=12)
        with app.app_context():
            build = db.session.get(CiBuild, build_id)
            assert [s.status for s in build.stages] == ["success", "success"]
            assert build.status == "running"
            assert post_actions.rows(build)[0].status == "running"
        held["post"] = False
        _advance(app, build_id)
    finally:
        adapter.poll = original_poll
    data = _build(client, admin_token, build_id)
    assert data["status"] == "success"
    assert _by_name(data)["Slow · always"]["status"] == "success"


def test_the_agent_claim_of_a_cleanup_is_a_command_payload(app, client, admin_token, service):
    from api.services.ci import agents, engine

    _secret(client, admin_token, service.id, "TOKEN", "agent-secret")
    _save(client, admin_token, service.pipeline_id, [
        _cleanup("failure", "Release lock", secretRefs=[{"name": "TOKEN", "envVar": "LOCK_TOKEN"}],
                 image="alpine:3.20", workingDirectory="app"),
    ])
    build_id = _trigger(client, admin_token, service.id)
    with app.app_context():
        build = db.session.get(CiBuild, build_id)
        row = post_actions.rows(build)[0]
        definition = engine._definition_for(build, row)
        assert definition["stageType"] == "post" and definition["postWhen"] == "failure"
        payload = agents._task_payload(build, row, SimpleNamespace(id=1))
    assert payload["commands"] == ["rm -rf build/tmp"]
    assert payload["image"] == "alpine:3.20"
    assert payload["workingDirectory"] == "app"
    assert payload["env"]["LOCK_TOKEN"] == "agent-secret"
    assert payload["position"] == 1000


# ---------------------------------------------------------------------------
# Snapshot and retry
# ---------------------------------------------------------------------------

def test_a_build_runs_its_snapshot_and_a_retry_takes_the_pipeline_as_it_is_now(
    app, client, admin_token, service
):
    _save(client, admin_token, service.pipeline_id, [_email("failure")])
    mailbox = _Mailbox()
    send, configured = _smtp(mailbox)
    with send, configured:
        build_id = _trigger(client, admin_token, service.id)
        # Edited while the build is queued: the build keeps what it was given.
        _save(client, admin_token, service.pipeline_id, [_email("always"), _cleanup("always")])
        _advance(app, build_id, fail=("Build",))
        data = _build(client, admin_token, build_id)
        assert [a["name"] for a in data["postActions"]] == ["Email · on failure"]
        assert data["postActions"][0]["status"] == "success"

        retry = client.post(f"/api/ci/builds/{build_id}/retry", headers=auth_headers(admin_token))
        assert retry.status_code in (200, 201), retry.get_json()
        retry_id = retry.get_json()["data"]["id"]
        _advance(app, retry_id)

    data = _build(client, admin_token, retry_id)
    assert [a["name"] for a in data["postActions"]] == ["Email · always", "Clean always · always"]
    assert all(a["status"] == "success" for a in data["postActions"])
    with app.app_context():
        snapshot = db.session.get(CiBuild, retry_id).pipeline_snapshot
    assert [a["type"] for a in snapshot["postActions"]] == ["email", "commands"]


def test_the_build_deadline_allows_for_cleanup():
    from api.services.ci import engine

    build = SimpleNamespace(pipeline_snapshot={
        "stages": [{"timeoutSeconds": 600}],
        "postActions": [_cleanup("always", timeoutSeconds=1200), _email("always")],
    })
    assert engine._build_deadline_minutes(build) == 10 + 20 + engine._BUILD_DEADLINE_GRACE_MINUTES


# ---------------------------------------------------------------------------
# Kubernetes: post-N containers after the stages, before the collector
# ---------------------------------------------------------------------------

def _k8s_execution(position, stage_type="command", **kw):
    return StageExecution(
        build_id=7,
        build_number=3,
        stage_id=100 + position,
        service_slug="payment-service",
        stage_name=f"Stage {position}",
        stage_type=stage_type,
        image=kw.get("image"),
        working_directory=kw.get("workdir"),
        commands=kw.get("commands", ["echo hello"]),
        env=kw.get("env", {}),
        secrets=kw.get("secrets", {}),
        timeout_seconds=kw.get("timeout", 600),
        continue_on_failure=kw.get("cof", False),
        position=position,
        workspace_ref="payment-service-3",
        repository_url="https://bitbucket.org/areeba/payment-service.git",
        branch="develop",
        callback_url="http://backend:5000/api/ci/worker",
        callback_token="the-callback-token",
        post_when=kw.get("when"),
    )


def _job_with_cleanup():
    first = _k8s_execution(0, "checkout")
    first.plan = [first, _k8s_execution(1, commands=["make"])]
    first.post_plan = [
        _k8s_execution(1000, "post", when="always", commands=["rm -rf tmp"], secrets={"LOCK_TOKEN": "s3cret"},
                       image="alpine:3.20", timeout=120),
        _k8s_execution(1001, "post", when="failure", commands=["./unlock.sh"]),
    ]
    return k8s.build_job_resources(first)


def test_cleanup_containers_follow_every_stage_and_precede_the_collector():
    secret, _, job = _job_with_cleanup()
    spec = job["spec"]["template"]["spec"]
    assert [c["name"] for c in spec["initContainers"]] == ["stage-0", "stage-1", "post-0", "post-1"]
    assert [c["name"] for c in spec["containers"]] == ["collector"]

    post0 = spec["initContainers"][2]
    assert post0["image"] == "alpine:3.20"
    assert post0["securityContext"]["readOnlyRootFilesystem"] is True
    assert {m["mountPath"] for m in post0["volumeMounts"]} >= {"/workspace", "/tmp"}
    env = {e["name"]: e for e in post0["env"]}
    assert env["KUBESIGHT_POST_WHEN"]["value"] == "always"
    assert env["LOCK_TOKEN"]["valueFrom"]["secretKeyRef"] == {"name": secret["metadata"]["name"], "key": "s1000-LOCK_TOKEN"}
    assert "s1000-LOCK_TOKEN" in secret["data"]
    script = post0["command"][2]
    assert "KS_STATE=/workspace/.kubesight" in script
    assert '"$KS_STATE/failed"' in script and '"$KS_STATE/failed-continued"' in script
    assert "timeout 120 sh -e" in script
    # Not gated by the stage guard that skips after a failure.
    assert "an earlier stage failed" not in script
    post1 = spec["initContainers"][3]["command"][2]
    assert 'if [ "$KUBESIGHT_STAGES_RESULT" != "failure" ]' in post1
    # The deadline allows for the cleanups too.
    assert job["spec"]["activeDeadlineSeconds"] == 600 + 600 + 900 + (120 + 60) + (600 + 60)


def test_a_continue_on_failure_stage_leaves_the_soft_flag():
    script = k8s._wrap_stage_script("false", continue_on_failure=True)
    assert "failed-continued" in script
    assert ': > "$KS_FLAG"' not in script


def test_no_post_plan_leaves_the_job_as_it_was():
    first = _k8s_execution(0, "checkout")
    first.plan = [first]
    job = k8s.build_job_resources(first)[2]
    assert [c["name"] for c in job["spec"]["template"]["spec"]["initContainers"]] == ["stage-0"]


def _fake_cluster(job_status, pod, logs):
    def runner(args, input_text=None):
        if args[:2] == ["get", "job"]:
            return 0, json.dumps({"metadata": {"name": args[2]}, "status": job_status}), ""
        if args[:2] == ["get", "pods"]:
            return 0, json.dumps({"items": [pod]}), ""
        if args[0] == "logs":
            container = args[args.index("-c") + 1]
            return 0, "\n".join(logs.get(container, [])), ""
        return 0, "", ""

    return runner


def test_the_last_stage_still_waits_for_the_whole_pod_and_cleanups_report_by_marker():
    adapter = k8s.KubernetesJobRunnerAdapter()
    names = ["stage-0", "stage-1", "post-0", "post-1", "post-2"]
    pod = {
        "metadata": {"creationTimestamp": "2026-10-02T10:00:00Z", "annotations": {}},
        "spec": {"initContainers": [{"name": n} for n in names]},
        "status": {"initContainerStatuses": [
            {"name": n, "state": {"terminated": {"exitCode": 0}}} for n in names
        ]},
    }
    logs = {
        "stage-0": ["[kubesight-exit] 0"],
        "stage-1": ["[kubesight-exit] 0"],
        "post-0": ["cleaning", "[kubesight-exit] 0"],
        "post-1": ["[kubesight] Skipped: ...", "[kubesight-skip]"],
        "post-2": ["rm: cannot remove", "[kubesight-exit] 1"],
    }
    ref = lambda c: RunnerHandle(runner_id=1, external_ref=f"ci-b7-payment-service#{c}")  # noqa: E731

    # Pod still running its cleanups/collector: the last STAGE waits.
    k8s.set_kubectl_runner(_fake_cluster({"active": 1}, pod, logs))
    assert adapter.poll(ref("stage-1")) == runner_base.RUNNING
    k8s.set_kubectl_runner(_fake_cluster({"succeeded": 1}, pod, logs))
    assert adapter.poll(ref("stage-1")) == runner_base.SUCCEEDED
    assert adapter.poll(ref("post-0")) == runner_base.SUCCEEDED
    assert adapter.poll(ref("post-1")) == runner_base.SKIPPED
    assert adapter.poll(ref("post-2")) == runner_base.FAILED
    # The collector's output stays on the last stage, not on a cleanup.
    assert adapter._is_last_stage(pod, "stage-1") and not adapter._is_last_stage(pod, "post-2")


def test_a_cleanup_attaches_to_its_container():
    adapter = k8s.KubernetesJobRunnerAdapter()
    handle = adapter.start(_k8s_execution(1001, "post", when="always"))
    assert handle.external_ref == "ci-b7-payment-service#post-1"


def test_the_engine_bakes_cleanups_into_a_whole_build_runner_plan(app, client, admin_token, service):
    from api.services.ci.runners import get_adapter

    _save(client, admin_token, service.pipeline_id, [_cleanup("failure", "Unlock")])
    adapter = get_adapter("mock")
    started = []
    original_start = adapter.start

    def start(execution):
        started.append(execution)
        return original_start(execution)

    adapter.start = start
    adapter.runs_whole_build = True
    try:
        build_id = _trigger(client, admin_token, service.id)
        _advance(app, build_id)
    finally:
        adapter.start = original_start
        del adapter.runs_whole_build

    first = started[0]
    assert [e.stage_type for e in first.plan] == ["checkout", "command"]
    assert [(e.stage_type, e.position, e.post_when) for e in first.post_plan] == [("post", 1000, "failure")]
    # The whole-build runner decided in the pod; the engine only attached.
    attached = [e for e in started if e.stage_type == "post"]
    assert len(attached) == 1 and attached[0].plan is None
    data = _build(client, admin_token, build_id)
    assert data["status"] == "success"
    assert data["postActions"][0]["status"] == "success"


SH = shutil.which("sh")


def _run_post(tmp_path, when, *, flag=None, commands=None, workdir=True):
    root = tmp_path / "workspace"
    (root / ".kubesight").mkdir(parents=True)
    if workdir:
        (root / "source").mkdir()
    if flag:
        (root / ".kubesight" / flag).write_text("")
    (root / ".kubesight" / "build.env").write_text("APP_VERSION=1.2.3\n")
    scratch = tmp_path / "tmp"
    scratch.mkdir()
    execution = _k8s_execution(
        1000, "post", when=when,
        commands=commands or ['echo "result=$KUBESIGHT_STAGES_RESULT version=$APP_VERSION"', "touch cleaned"],
    )
    script = k8s.post_container_script(execution, root=root.as_posix(), tmp=scratch.as_posix())
    path = tmp_path / "post.sh"
    path.write_text(script, encoding="utf-8", newline="\n")
    result = subprocess.run([SH, str(path)], capture_output=True, text=True, timeout=120)
    return result, root


@pytest.mark.skipif(not SH, reason="needs sh")
@pytest.mark.parametrize(
    "when, flag, runs, result",
    [
        ("always", None, True, "success"),
        ("always", "failed", True, "failure"),
        ("success", None, True, "success"),
        ("success", "failed", False, "failure"),
        ("failure", "failed", True, "failure"),
        ("failure", "failed-continued", True, "failure"),
        ("failure", None, False, "success"),
    ],
)
def test_the_generated_cleanup_script_decides_by_the_fail_flag(tmp_path, when, flag, runs, result):
    completed, root = _run_post(tmp_path, when, flag=flag)
    assert completed.returncode == 0, completed.stderr
    lines = completed.stdout.strip().splitlines()
    assert f"the stages ended in {result}" in completed.stdout
    if runs:
        assert f"result={result} version=1.2.3" in completed.stdout
        assert (root / "source" / "cleaned").exists()
        assert lines[-1] == "[kubesight-exit] 0"
    else:
        assert lines[-1] == "[kubesight-skip]"
        assert not (root / "source" / "cleaned").exists()


@pytest.mark.skipif(not SH, reason="needs sh")
def test_a_failing_cleanup_script_reports_its_code_and_still_exits_zero(tmp_path):
    completed, _ = _run_post(tmp_path, "always", commands=["echo partial", "false", "echo never"])
    assert completed.returncode == 0
    assert "partial" in completed.stdout and "never" not in completed.stdout
    assert completed.stdout.strip().splitlines()[-1] == "[kubesight-exit] 1"


@pytest.mark.skipif(not SH, reason="needs sh")
def test_a_cleanup_without_a_checkout_runs_from_the_workspace(tmp_path):
    completed, root = _run_post(tmp_path, "always", workdir=False, commands=["touch here"])
    assert completed.returncode == 0, completed.stderr
    assert "source directory does not exist" in completed.stdout
    assert (root / "here").exists()
