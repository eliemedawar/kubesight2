"""Webhook triggers: saving one, calling it, and what each call builds.

Drives the real inbound route with the real planner and the real
``trigger_build``, and proves the bounds the module docstring promises: the
secret (verbatim or as a body signature) is required, a request can change only
what the trigger allows, a refused request starts nothing, redeliveries are not
built twice, and a Bitbucket push builds exactly the pushed commits that pass
the filters.
"""

from __future__ import annotations

import hashlib
import hmac
import json

import pytest

from api.db import db
from api.models import AuditLog
from api.models_application_intelligence import BitbucketCredentialProfile
from api.models_ci import CiBuild
from api.models_ci_webhooks import CiWebhookDelivery, CiWebhookTrigger
from api.secret_encryption import encrypt_secret
from api.services.ci import webhook_triggers as webhooks_service
from tests.conftest import auth_headers

PARAMS = [
    {"name": "VERSION", "type": "text", "default": ""},
    {"name": "TARGET", "type": "choice", "choices": ["uat", "prod"], "default": "uat"},
    {"name": "NIGHTLY_SCAN", "type": "boolean", "default": False},
]
COMMIT = "a1b2c3d4e5f60718293a4b5c6d7e8f9012345678"


@pytest.fixture()
def service_id(app, client, admin_token):
    with app.app_context():
        credential = BitbucketCredentialProfile(
            name="ci-token",
            provider="bitbucket",
            credential_type="repository_access_token",
            secret_cipher=encrypt_secret("clone-token-value"),
            read_only=False,
            enabled=True,
        )
        db.session.add(credential)
        db.session.commit()
        credential_id = credential.id

    headers = auth_headers(admin_token)
    sid = client.post(
        "/api/ci/services",
        json={"name": "Payment Service", "applicationType": "java"},
        headers=headers,
    ).get_json()["data"]["id"]
    client.put(
        f"/api/ci/services/{sid}/source",
        json={
            "repositoryUrl": "https://bitbucket.org/areeba/payment-service",
            "defaultBranch": "develop",
            "credentialProfileId": credential_id,
        },
        headers=headers,
    )
    pipeline_id = client.get(f"/api/ci/services/{sid}/pipelines", headers=headers).get_json()[
        "data"
    ]["items"][0]["id"]
    response = client.put(
        f"/api/ci/pipelines/{pipeline_id}",
        json={
            "parameters": PARAMS,
            "stages": [
                {"name": "Checkout", "stageType": "checkout", "runnerLabels": ["mock"]},
                {"name": "Build", "stageType": "command", "commands": ["mvn -B package"], "runnerLabels": ["mock"]},
            ],
        },
        headers=headers,
    )
    assert response.status_code == 200, response.get_json()
    return sid


def _create(client, token, service_id, **overrides):
    payload = {"name": "Release tool", "kind": "generic", **overrides}
    response = client.post(
        f"/api/ci/services/{service_id}/webhooks", json=payload, headers=auth_headers(token)
    )
    return response


def _made(client, token, service_id, **overrides):
    response = _create(client, token, service_id, **overrides)
    assert response.status_code == 201, response.get_json()
    return response.get_json()["data"]


def _call(client, hook, body=None, *, secret=None, headers=None, raw=None):
    sent = dict(headers or {})
    if secret is not None:
        sent["X-KubeSight-Secret"] = secret
    if raw is not None:
        return client.post(hook["path"], data=raw, headers={**sent, "Content-Type": "application/json"})
    if body is None:
        return client.post(hook["path"], headers=sent)
    return client.post(hook["path"], json=body, headers=sent)


def _builds(service_id):
    db.session.expire_all()
    return CiBuild.query.filter_by(service_id=service_id).order_by(CiBuild.id).all()


# ---------------------------------------------------------------------------
# Saving
# ---------------------------------------------------------------------------

def test_create_returns_the_secret_once_and_lists_without_it(client, admin_token, service_id):
    hook = _made(client, admin_token, service_id)
    assert hook["secret"] and len(hook["secret"]) > 30
    assert hook["path"].startswith("/api/ci/hooks/wh_")
    assert hook["url"].endswith(hook["path"])
    assert hook["runsAs"] == "admin" and hook["enabled"] is True

    listing = client.get(f"/api/ci/services/{service_id}/webhooks", headers=auth_headers(admin_token))
    item = listing.get_json()["data"]["items"][0]
    assert "secret" not in item and item["secretSet"] is True

    revealed = client.get(
        f"/api/ci/services/{service_id}/webhooks/{hook['id']}/secret", headers=auth_headers(admin_token)
    ).get_json()["data"]["secret"]
    assert revealed == hook["secret"]
    assert AuditLog.query.filter_by(action="ci_webhook_secret_revealed").count() == 1


@pytest.mark.parametrize(
    "overrides, message",
    [
        ({"name": ""}, "Give the webhook a name"),
        ({"kind": "jenkins"}, "either 'generic' or 'bitbucket_push'"),
        ({"allowedInputs": ["NOPE"]}, "no build input named 'NOPE'"),
        ({"mappings": [{"target": "MISSING", "path": "a.b"}]}, "no build input named 'MISSING'"),
        ({"mappings": [{"target": "VERSION", "path": "a b"}]}, "is not a path"),
        ({"mappings": [{"target": "ref:branch", "path": "a"}, {"target": "ref:tag", "path": "b"}]}, "not both"),
        ({"variables": {"TARGET": "staging"}}, "TARGET"),
        ({"refType": "tag", "branch": ""}, "Name the tag"),
        ({"branch": "-upload-pack=evil"}, "not a usable"),
    ],
)
def test_save_refuses_with_a_reason(client, admin_token, service_id, overrides, message):
    response = _create(client, admin_token, service_id, **overrides)
    assert response.status_code == 400
    assert message in response.get_json()["error"]


def test_kind_cannot_change_and_names_are_unique(client, admin_token, service_id):
    hook = _made(client, admin_token, service_id)
    response = client.put(
        f"/api/ci/services/{service_id}/webhooks/{hook['id']}",
        json={"kind": "bitbucket_push"},
        headers=auth_headers(admin_token),
    )
    assert response.status_code == 400 and "cannot change" in response.get_json()["error"]
    assert _create(client, admin_token, service_id, name="release TOOL").status_code == 400


# ---------------------------------------------------------------------------
# Inbound: authentication
# ---------------------------------------------------------------------------

def test_unknown_url_and_bad_secret_are_refused(client, admin_token, service_id):
    hook = _made(client, admin_token, service_id)
    assert client.post("/api/ci/hooks/wh_nothing-here").status_code == 404
    assert _call(client, hook).status_code == 401
    assert _call(client, hook, secret="wrong").status_code == 401
    assert _builds(service_id) == []
    row = db.session.get(CiWebhookTrigger, hook["id"])
    assert row.last_rejected_at is not None
    # A rejected call is not a delivery: anybody can send one.
    assert CiWebhookDelivery.query.count() == 0


def test_a_bare_post_with_the_secret_builds_the_default(client, admin_token, service_id):
    hook = _made(client, admin_token, service_id)
    response = _call(client, hook, secret=hook["secret"])
    assert response.status_code == 202, response.get_json()
    data = response.get_json()["data"]
    assert data["triggered"] is True and len(data["builds"]) == 1

    [build] = _builds(service_id)
    assert build.trigger_type == "webhook"
    assert build.branch == "develop"
    assert build.pipeline_snapshot["webhook"]["name"] == "Release tool"
    assert build.requested_by_user_id is not None  # runs as whoever saved it

    detail = client.get(f"/api/ci/builds/{build.id}", headers=auth_headers(admin_token)).get_json()["data"]
    assert detail["webhook"]["name"] == "Release tool"


@pytest.mark.parametrize("header", ["X-Hub-Signature-256", "X-Hub-Signature"])
def test_a_body_signature_is_accepted_instead_of_the_secret(client, admin_token, service_id, header):
    hook = _made(client, admin_token, service_id)
    raw = json.dumps({"hello": "world"}).encode()
    digest = hmac.new(hook["secret"].encode(), raw, hashlib.sha256).hexdigest()
    assert _call(client, hook, raw=raw, headers={header: f"sha256={digest}"}).status_code == 202
    tampered = json.dumps({"hello": "there"}).encode()
    assert _call(client, hook, raw=tampered, headers={header: f"sha256={digest}"}).status_code == 401


@pytest.mark.parametrize(
    "headers, query",
    [
        ({"Authorization": "Bearer {secret}"}, ""),
        ({"X-Gitlab-Token": "{secret}"}, ""),
        ({}, "?secret={secret}"),
    ],
)
def test_other_ways_of_sending_the_secret(client, admin_token, service_id, headers, query):
    hook = _made(client, admin_token, service_id)
    sent = {key: value.format(secret=hook["secret"]) for key, value in headers.items()}
    response = client.post(hook["path"] + query.format(secret=hook["secret"]), headers=sent)
    assert response.status_code == 202


# ---------------------------------------------------------------------------
# Inbound: what a request may change
# ---------------------------------------------------------------------------

def test_the_ref_is_fixed_unless_the_trigger_allows_it(client, admin_token, service_id):
    hook = _made(client, admin_token, service_id)
    response = _call(client, hook, {"branch": "release/2.0"}, secret=hook["secret"])
    assert response.status_code == 422
    assert "may not choose the ref" in response.get_json()["error"]
    assert _builds(service_id) == []

    open_hook = _made(
        client, admin_token, service_id, name="Any release", allowRefOverride=True, branchFilters=["release/*"]
    )
    assert _call(client, open_hook, {"branch": "release/2.0", "commit": COMMIT}, secret=open_hook["secret"]).status_code == 202
    [build] = _builds(service_id)
    assert build.branch == "release/2.0" and build.commit_sha == COMMIT

    outside = _call(client, open_hook, {"branch": "feature/x"}, secret=open_hook["secret"])
    assert outside.status_code == 200
    assert outside.get_json()["data"]["outcome"] == "ignored"
    assert "outside this webhook's branch filters" in outside.get_json()["data"]["message"]
    assert len(_builds(service_id)) == 1


@pytest.mark.parametrize("branch", ["-upload-pack=x", "a..b", "has space"])
def test_unsafe_refs_are_refused(client, admin_token, service_id, branch):
    hook = _made(client, admin_token, service_id, allowRefOverride=True)
    assert _call(client, hook, {"branch": branch}, secret=hook["secret"]).status_code == 422
    assert _builds(service_id) == []


def test_inputs_only_those_allowed_and_still_validated(client, admin_token, service_id):
    hook = _made(
        client, admin_token, service_id,
        allowedInputs=["VERSION", "TARGET"], variables={"NIGHTLY_SCAN": True},
    )
    refused = _call(client, hook, {"variables": {"NIGHTLY_SCAN": False}}, secret=hook["secret"])
    assert refused.status_code == 422
    assert "may not set 'NIGHTLY_SCAN'" in refused.get_json()["error"]

    bad_value = _call(client, hook, {"variables": {"TARGET": "staging"}}, secret=hook["secret"])
    assert bad_value.status_code == 422
    assert _builds(service_id) == []

    ok = _call(client, hook, {"variables": {"VERSION": "2.4.1", "TARGET": "prod"}}, secret=hook["secret"])
    assert ok.status_code == 202, ok.get_json()
    [build] = _builds(service_id)
    variables = build.pipeline_snapshot["variables"]
    assert variables["VERSION"] == "2.4.1" and variables["TARGET"] == "prod"
    assert variables["NIGHTLY_SCAN"] == "true"


def test_mappings_lift_values_out_of_a_foreign_body(client, admin_token, service_id):
    hook = _made(
        client, admin_token, service_id,
        mappings=[
            {"target": "ref:tag", "path": "ref"},
            {"target": "VERSION", "path": "release.tag_name"},
            {"target": "ref:commit", "path": "head_commit.id"},
            {"target": "TARGET", "path": "deploy[0].env"},
        ],
    )
    body = {
        "ref": "refs/tags/v2.4.1",
        "release": {"tag_name": "2.4.1"},
        "head_commit": {"id": COMMIT},
        "deploy": [{"env": "prod"}],
    }
    response = _call(client, hook, body, secret=hook["secret"])
    assert response.status_code == 202, response.get_json()
    [build] = _builds(service_id)
    assert build.branch == "v2.4.1" and build.pipeline_snapshot["refType"] == "tag"
    assert build.commit_sha == COMMIT
    assert build.pipeline_snapshot["variables"]["VERSION"] == "2.4.1"
    assert build.pipeline_snapshot["variables"]["TARGET"] == "prod"

    # The delivery log keeps the body's shape, never its values.
    delivery = CiWebhookDelivery.query.order_by(CiWebhookDelivery.id.desc()).first()
    assert "release.tag_name" in delivery.payload_paths
    assert "2.4.1" not in json.dumps(delivery.payload_paths)


def test_a_missing_mapped_path_keeps_the_default_and_says_so(client, admin_token, service_id):
    hook = _made(client, admin_token, service_id, mappings=[{"target": "VERSION", "path": "release.tag_name"}])
    response = _call(client, hook, {"other": 1}, secret=hook["secret"])
    assert response.status_code == 202
    assert any("nothing at 'release.tag_name'" in note for note in response.get_json()["data"]["notes"])


# ---------------------------------------------------------------------------
# Inbound: when nothing is built
# ---------------------------------------------------------------------------

def test_switched_off_is_ignored_not_refused(client, admin_token, service_id):
    hook = _made(client, admin_token, service_id, enabled=False)
    response = _call(client, hook, secret=hook["secret"])
    assert response.status_code == 200
    assert response.get_json()["data"]["outcome"] == "ignored"
    assert _builds(service_id) == []


def test_a_redelivery_is_not_built_twice(client, admin_token, service_id):
    hook = _made(client, admin_token, service_id)
    headers = {"Idempotency-Key": "release-2.4.1"}
    assert _call(client, hook, secret=hook["secret"], headers=headers).status_code == 202
    again = _call(client, hook, secret=hook["secret"], headers=headers)
    assert again.status_code == 200
    assert again.get_json()["data"]["outcome"] == "duplicate"
    assert len(_builds(service_id)) == 1


def test_skip_if_running(client, admin_token, service_id):
    hook = _made(client, admin_token, service_id, skipIfRunning=True)
    assert _call(client, hook, secret=hook["secret"]).status_code == 202
    second = _call(client, hook, secret=hook["secret"])
    assert second.get_json()["data"]["outcome"] == "ignored"
    assert "still" in second.get_json()["data"]["message"]
    assert len(_builds(service_id)) == 1


def test_builds_per_minute_are_capped(client, admin_token, service_id, monkeypatch):
    monkeypatch.setattr(webhooks_service, "MAX_BUILDS_PER_MINUTE", 2)
    hook = _made(client, admin_token, service_id)
    for _ in range(2):
        assert _call(client, hook, secret=hook["secret"]).status_code == 202
    capped = _call(client, hook, secret=hook["secret"])
    assert capped.get_json()["data"]["outcome"] == "ignored"
    assert "in the last minute" in capped.get_json()["data"]["message"]
    assert len(_builds(service_id)) == 2


def test_an_owner_who_cannot_build_any_more_stops_the_webhook(client, admin_token, service_id):
    hook = _made(client, admin_token, service_id)
    row = db.session.get(CiWebhookTrigger, hook["id"])
    row.created_by_user_id = None
    row.updated_by_user_id = None
    db.session.commit()
    response = _call(client, hook, secret=hook["secret"])
    assert response.status_code == 409
    assert "Nobody owns this webhook" in response.get_json()["error"]
    assert _builds(service_id) == []


def test_deliveries_are_listed_newest_first(client, admin_token, service_id):
    hook = _made(client, admin_token, service_id)
    _call(client, hook, secret=hook["secret"])
    _call(client, hook, {"branch": "x"}, secret=hook["secret"])
    items = client.get(
        f"/api/ci/services/{service_id}/webhooks/{hook['id']}/deliveries", headers=auth_headers(admin_token)
    ).get_json()["data"]["items"]
    assert [item["outcome"] for item in items] == ["refused", "triggered"]
    assert items[1]["builds"][0]["number"] >= 1


# ---------------------------------------------------------------------------
# Preview and test from the page
# ---------------------------------------------------------------------------

def test_preview_plans_without_building(client, admin_token, service_id):
    hook = _made(client, admin_token, service_id, allowedInputs=["VERSION"])
    response = client.post(
        f"/api/ci/services/{service_id}/webhooks/{hook['id']}/preview",
        json={"payload": {"variables": {"VERSION": "9"}}},
        headers=auth_headers(admin_token),
    )
    data = response.get_json()["data"]
    assert data["outcome"] == "build"
    assert data["builds"][0]["variables"]["VERSION"] == "9"
    assert data["builds"][0]["ref"] == "develop"
    assert "variables.VERSION" in data["paths"]
    assert _builds(service_id) == []


def test_a_test_build_runs_as_the_person_even_when_switched_off(client, admin_token, service_id):
    hook = _made(client, admin_token, service_id, enabled=False)
    response = client.post(
        f"/api/ci/services/{service_id}/webhooks/{hook['id']}/test",
        json={"payload": {}},
        headers=auth_headers(admin_token),
    )
    assert response.status_code == 201, response.get_json()
    [build] = _builds(service_id)
    assert build.pipeline_snapshot["webhook"]["test"] is True
    delivery = CiWebhookDelivery.query.one()
    assert delivery.event == "test" and delivery.tested_by_user_id is not None


# ---------------------------------------------------------------------------
# Bitbucket push
# ---------------------------------------------------------------------------

def _push(*changes, repo="areeba/payment-service"):
    return {"repository": {"full_name": repo}, "push": {"changes": list(changes)}}


def _change(kind, name, commit=COMMIT):
    return {"new": {"type": kind, "name": name, "target": {"hash": commit}}, "old": None}


def test_push_builds_matching_branches_at_the_pushed_commit(client, admin_token, service_id):
    hook = _made(client, admin_token, service_id, name="Build on push", kind="bitbucket_push", branchFilters=["develop", "release/*"])
    other = "f" * 40
    body = _push(
        _change("branch", "develop"),
        _change("branch", "feature/x", other),
        _change("tag", "v1.0.0", other),
        {"new": None, "old": {"type": "branch", "name": "old-branch"}},
    )
    response = _call(client, hook, body, secret=hook["secret"], headers={"X-Event-Key": "repo:push"})
    assert response.status_code == 202, response.get_json()
    [build] = _builds(service_id)
    assert build.branch == "develop" and build.commit_sha == COMMIT
    notes = response.get_json()["data"]["notes"]
    assert any("feature/x is outside" in note for note in notes)
    assert any("does not build tags" in note for note in notes)
    assert any("old-branch was deleted" in note for note in notes)

    # Bitbucket retrying the same push (no delivery id) is not built again.
    again = _call(client, hook, body, secret=hook["secret"], headers={"X-Event-Key": "repo:push"})
    assert again.get_json()["data"]["outcome"] == "duplicate"
    assert len(_builds(service_id)) == 1


def test_push_tags_build_only_when_asked(client, admin_token, service_id):
    hook = _made(
        client, admin_token, service_id, name="Release tags", kind="bitbucket_push",
        branchFilters=["nothing"], buildTags=True, tagFilters=["v*"],
    )
    body = _push(_change("tag", "v2.0.0"), _change("tag", "nightly-1", "e" * 40))
    assert _call(client, hook, body, secret=hook["secret"], headers={"X-Event-Key": "repo:push"}).status_code == 202
    [build] = _builds(service_id)
    assert build.branch == "v2.0.0" and build.pipeline_snapshot["refType"] == "tag"


def test_push_from_another_repository_or_event_is_ignored(client, admin_token, service_id):
    hook = _made(client, admin_token, service_id, name="Build on push", kind="bitbucket_push")
    wrong_repo = _call(
        client, hook, _push(_change("branch", "develop"), repo="areeba/other"),
        secret=hook["secret"], headers={"X-Event-Key": "repo:push"},
    )
    assert wrong_repo.status_code == 200
    assert "this service builds areeba/payment-service" in wrong_repo.get_json()["data"]["message"]
    wrong_event = _call(
        client, hook, _push(_change("branch", "develop")),
        secret=hook["secret"], headers={"X-Event-Key": "pullrequest:created"},
    )
    assert wrong_event.get_json()["data"]["outcome"] == "ignored"
    assert _builds(service_id) == []


def test_push_webhook_is_registered_in_bitbucket(client, admin_token, service_id, monkeypatch):
    from api.services.ci.source import bitbucket_status

    hooks, writes = [], []

    def save_webhook(**kwargs):
        writes.append(kwargs)
        hooks.append({"uuid": "{h1}", "url": kwargs["url"], "events": kwargs["events"], "active": True, "secretSet": True})
        return {"uuid": "{h1}"}

    monkeypatch.setattr(bitbucket_status, "list_webhooks", lambda **kwargs: [dict(item) for item in hooks])
    monkeypatch.setattr(bitbucket_status, "save_webhook", save_webhook)
    monkeypatch.setenv("PUBLIC_BASE_URL", "https://kubesight.example.com")

    hook = _made(client, admin_token, service_id, name="Build on push", kind="bitbucket_push")
    base = f"/api/ci/services/{service_id}/webhooks/{hook['id']}"
    before = client.get(f"{base}/source-status", headers=auth_headers(admin_token)).get_json()["data"]
    assert before["known"] is True and before["exists"] is False

    response = client.post(f"{base}/setup", headers=auth_headers(admin_token))
    assert response.status_code == 200, response.get_json()
    [write] = writes
    assert write["url"] == "https://kubesight.example.com" + hook["path"]
    assert write["events"] == ["repo:push"]
    assert write["secret"] == hook["secret"]

    after = client.get(f"{base}/source-status", headers=auth_headers(admin_token)).get_json()["data"]
    assert after["exists"] is True and after["inSync"] is True


def test_setup_refuses_a_loopback_address(client, admin_token, service_id, monkeypatch):
    monkeypatch.setenv("PUBLIC_BASE_URL", "http://localhost:5055")
    hook = _made(client, admin_token, service_id, name="Build on push", kind="bitbucket_push")
    response = client.post(
        f"/api/ci/services/{service_id}/webhooks/{hook['id']}/setup", headers=auth_headers(admin_token)
    )
    assert response.status_code == 400
    assert "PUBLIC_BASE_URL" in response.get_json()["error"]
