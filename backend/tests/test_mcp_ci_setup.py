"""MCP tools that set CI up: registering a service, standalone pipelines, and
which pipeline a service builds with.

Locked here: a created service is a real catalog service that can build; a
duplicate is refused unless asked for; credentials resolve by name, or are
picked when only one exists; a standalone pipeline is created hidden from the
catalog and is then addressable by its slug in the ordinary ci tools; attach
and detach change what a service builds; and a token without the permissions
cannot do any of it.
"""

from __future__ import annotations

import json

import pytest

from api.db import db
from api.models_application_intelligence import BitbucketCredentialProfile
from api.models_ci import CiPipeline, CiService
from api.secret_encryption import encrypt_secret
from tests.test_mcp_server import call_tool


def payload(result):
    return json.loads(result["content"][1]["text"])


def error_text(result):
    return result["content"][0]["text"]


@pytest.fixture()
def credential(app):
    with app.app_context():
        row = BitbucketCredentialProfile(
            name="ci-token", provider="bitbucket", credential_type="repository_access_token",
            secret_cipher=encrypt_secret("x"), read_only=True, enabled=True,
        )
        db.session.add(row)
        db.session.commit()
        return row.id


def _create_service(client, token, **extra):
    arguments = {
        "name": "Payments API",
        "applicationType": "java_gradle",
        "repositoryUrl": "https://bitbucket.org/areeba/payments-api",
        **extra,
    }
    return call_tool(client, token, "kubesight_service_create", arguments)


def test_a_service_is_registered_and_ready_to_build(app, client, admin_token, credential):
    result = _create_service(client, admin_token)
    assert not result.get("isError"), error_text(result)
    data = payload(result)
    assert data["slug"] == "payments-api"
    assert data["ready"] is True and data["blockedReason"] is None
    assert "only enabled credential" in data["note"]
    listed = payload(call_tool(client, admin_token, "kubesight_services_list", {}))
    assert [item["slug"] for item in listed["services"]] == ["payments-api"]


def test_a_duplicate_service_is_refused_unless_asked_for(app, client, admin_token, credential):
    _create_service(client, admin_token)
    again = _create_service(client, admin_token, name="Payments API v2")
    assert again.get("isError") and "already builds" in error_text(again)
    same_name = _create_service(client, admin_token, repositoryUrl="https://bitbucket.org/areeba/other")
    assert same_name.get("isError") and "already exists" in error_text(same_name)
    forced = _create_service(client, admin_token, name="Payments API v2", allowDuplicate=True)
    assert not forced.get("isError"), error_text(forced)


def test_the_credential_must_be_named_when_there_are_several(app, client, admin_token, credential):
    with app.app_context():
        db.session.add(
            BitbucketCredentialProfile(
                name="other-token", provider="bitbucket", credential_type="repository_access_token",
                secret_cipher=encrypt_secret("y"), read_only=True, enabled=True,
            )
        )
        db.session.commit()
    unnamed = _create_service(client, admin_token)
    assert unnamed.get("isError") and "ci-token" in error_text(unnamed)
    unknown = _create_service(client, admin_token, credential="nope")
    assert unknown.get("isError") and "No enabled source credential" in error_text(unknown)
    named = _create_service(client, admin_token, credential="other-token")
    assert not named.get("isError"), error_text(named)
    creds = payload(call_tool(client, admin_token, "kubesight_ci_credentials_list", {}))
    assert {item["name"] for item in creds["credentials"]} == {"ci-token", "other-token"}
    assert all("secret" not in json.dumps(item).lower() for item in creds["credentials"])


def test_a_service_without_a_repository_is_registered_but_blocked(app, client, admin_token):
    data = payload(call_tool(client, admin_token, "kubesight_service_create",
                             {"name": "Later", "applicationType": "node"}))
    assert data["ready"] is False and data["blockedReason"]


def test_a_standalone_pipeline_is_created_and_used_by_slug(app, client, admin_token, credential):
    result = call_tool(client, admin_token, "kubesight_shared_pipeline_create", {
        "name": "Nightly cleanup",
        "stages": [{"name": "Clean", "stageType": "command", "commands": ["echo clean"]}],
    })
    assert not result.get("isError"), error_text(result)
    data = payload(result)
    assert data["slug"] == "nightly-cleanup" and data["stages"] == ["Clean"]
    assert data["blockedReason"] is None  # No repository needed: it checks nothing out.

    # Hidden from the catalog, listed on its own page.
    services = payload(call_tool(client, admin_token, "kubesight_services_list", {}))
    assert "nightly-cleanup" not in [item["slug"] for item in services["services"]]
    homes = payload(call_tool(client, admin_token, "kubesight_shared_pipelines_list", {}))
    assert [item["slug"] for item in homes["pipelines"]] == ["nightly-cleanup"]

    # The ordinary ci tools address it by slug.
    added = call_tool(client, admin_token, "kubesight_pipeline_stage_add", {
        "service": "nightly-cleanup",
        "stage": {"name": "Report", "stageType": "command", "commands": ["echo report"]},
    })
    assert not added.get("isError"), error_text(added)
    got = payload(call_tool(client, admin_token, "kubesight_pipeline_get", {"service": "nightly-cleanup"}))
    assert [stage["name"] for stage in got["stages"]] == ["Clean", "Report"]


def test_a_standalone_pipeline_can_start_from_a_service_or_a_starter(app, client, admin_token, credential):
    _create_service(client, admin_token)
    copied = payload(call_tool(client, admin_token, "kubesight_shared_pipeline_create", {
        "name": "Copy", "startFrom": "service", "fromService": "payments-api",
    }))
    assert copied["stages"]
    starter = payload(call_tool(client, admin_token, "kubesight_shared_pipeline_create", {
        "name": "Node standard", "startFrom": "template", "applicationType": "node",
    }))
    assert starter["stages"]


def test_attach_and_detach_change_what_a_service_builds(app, client, admin_token, credential):
    _create_service(client, admin_token)
    call_tool(client, admin_token, "kubesight_shared_pipeline_create", {
        "name": "Java standard",
        "stages": [{"name": "Compile", "stageType": "command", "commands": ["./gradlew build"]}],
    })
    attached = call_tool(client, admin_token, "kubesight_shared_pipeline_attach",
                         {"service": "payments-api", "pipeline": "Java standard"})
    assert not attached.get("isError"), error_text(attached)
    assert payload(attached)["stages"] == ["Compile"]

    got = payload(call_tool(client, admin_token, "kubesight_pipeline_get", {"service": "payments-api"}))
    assert got["sharedPipeline"]["name"] == "Java standard"
    assert [stage["name"] for stage in got["stages"]] == ["Compile"]
    used = payload(call_tool(client, admin_token, "kubesight_shared_pipelines_list", {"pipeline": "java-standard"}))
    assert [entry["service"] for entry in used["pipeline"]["usedBy"]] == ["payments-api"]

    detached = payload(call_tool(client, admin_token, "kubesight_shared_pipeline_detach",
                                 {"service": "payments-api", "mode": "copy"}))
    assert detached["stages"] == ["Compile"]
    with app.app_context():
        row = CiPipeline.query.join(CiService).filter(CiService.slug == "payments-api").first()
        assert row.linked_pipeline_id is None and [s.name for s in row.stages] == ["Compile"]


def test_a_pipeline_cannot_be_attached_to_a_pipeline(app, client, admin_token):
    for name in ("One", "Two"):
        call_tool(client, admin_token, "kubesight_shared_pipeline_create", {
            "name": name, "stages": [{"name": "A", "stageType": "command", "commands": ["true"]}],
        })
    result = call_tool(client, admin_token, "kubesight_shared_pipeline_attach", {"service": "one", "pipeline": "two"})
    assert result.get("isError") and "not a CI service" in error_text(result)


def test_a_viewer_can_list_but_not_create(app, client, viewer_token):
    listed = call_tool(client, viewer_token, "kubesight_shared_pipelines_list", {})
    assert not listed.get("isError")
    for name, arguments in (
        ("kubesight_service_create", {"name": "X", "applicationType": "node"}),
        ("kubesight_shared_pipeline_create", {"name": "X"}),
    ):
        result = call_tool(client, viewer_token, name, arguments)
        assert result.get("isError") and "permission" in error_text(result)
