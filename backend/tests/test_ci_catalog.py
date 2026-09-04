"""CI Service Catalog: registration, source configuration, pipelines, RBAC."""

from __future__ import annotations

import pytest

from api.db import db
from api.models_application_intelligence import BitbucketCredentialProfile
from api.secret_encryption import encrypt_secret
from tests.conftest import auth_headers


@pytest.fixture()
def credential(app):
    with app.app_context():
        row = BitbucketCredentialProfile(
            name="ci-read-only",
            provider="bitbucket",
            credential_type="repository_access_token",
            secret_cipher=encrypt_secret("test-token"),
            read_only=True,
            enabled=True,
        )
        db.session.add(row)
        db.session.commit()
        return row.id


def create_service(client, token, **overrides):
    payload = {
        "name": "Payment Service",
        "description": "Card payment API",
        "ownerTeam": "Payments",
        "criticality": "critical",
        "applicationType": "java",
        **overrides,
    }
    return client.post("/api/ci/services", json=payload, headers=auth_headers(token))


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------

def test_create_service_generates_slug_and_starter_pipeline(client, admin_token):
    response = create_service(client, admin_token)
    assert response.status_code == 201
    data = response.get_json()["data"]

    assert data["slug"] == "payment-service"
    assert data["applicationType"] == "java"
    assert data["status"] == "active"
    # A new service should be one click from runnable, not an empty editor.
    assert data["pipelineConfigured"] is True
    assert data["pipelineStageCount"] > 0
    # Source is a separate step, so a fresh service is deliberately incomplete.
    assert data["sourceConfigured"] is False


def test_slug_collision_gets_a_suffix(client, admin_token):
    create_service(client, admin_token)
    second = create_service(client, admin_token)
    assert second.status_code == 201
    assert second.get_json()["data"]["slug"] == "payment-service-2"


def test_unknown_application_type_is_rejected(client, admin_token):
    response = create_service(client, admin_token, applicationType="cobol")
    assert response.status_code == 400
    assert "Application type" in response.get_json()["error"]


def test_service_list_filters_by_search_and_type(client, admin_token):
    create_service(client, admin_token)
    create_service(
        client, admin_token, name="Ledger UI", applicationType="node", ownerTeam="Ledger"
    )

    typed = client.get(
        "/api/ci/services?applicationType=node", headers=auth_headers(admin_token)
    )
    names = [item["name"] for item in typed.get_json()["data"]["items"]]
    assert names == ["Ledger UI"]

    searched = client.get(
        "/api/ci/services?search=payment", headers=auth_headers(admin_token)
    )
    assert [i["name"] for i in searched.get_json()["data"]["items"]] == ["Payment Service"]


def test_search_also_matches_the_owner_team(client, admin_token):
    create_service(client, admin_token, ownerTeam="Payments")
    create_service(client, admin_token, name="Ledger UI", ownerTeam="Ledger")

    response = client.get(
        "/api/ci/services?search=payments", headers=auth_headers(admin_token)
    )
    assert [i["name"] for i in response.get_json()["data"]["items"]] == ["Payment Service"]


# ---------------------------------------------------------------------------
# Source
# ---------------------------------------------------------------------------

def test_source_configuration_normalizes_the_repository_url(
    client, admin_token, credential
):
    service_id = create_service(client, admin_token).get_json()["data"]["id"]
    response = client.put(
        f"/api/ci/services/{service_id}/source",
        json={
            "repositoryUrl": "https://bitbucket.org/areeba/payment-service",
            "defaultBranch": "develop",
            "workingDirectory": "services/payment",
            "credentialProfileId": credential,
        },
        headers=auth_headers(admin_token),
    )
    assert response.status_code == 200
    data = response.get_json()["data"]

    assert data["repositoryUrl"] == "https://bitbucket.org/areeba/payment-service.git"
    assert data["repositoryWorkspace"] == "areeba"
    assert data["repositoryName"] == "payment-service"
    assert data["defaultBranch"] == "develop"
    assert data["workingDirectory"] == "services/payment"
    assert data["sourceConfigured"] is True


def test_source_rejects_a_non_bitbucket_url(client, admin_token, credential):
    service_id = create_service(client, admin_token).get_json()["data"]["id"]
    response = client.put(
        f"/api/ci/services/{service_id}/source",
        json={
            "repositoryUrl": "https://gitlab.com/areeba/payment-service",
            "credentialProfileId": credential,
        },
        headers=auth_headers(admin_token),
    )
    assert response.status_code == 400


def test_source_rejects_a_url_carrying_credentials(client, admin_token, credential):
    service_id = create_service(client, admin_token).get_json()["data"]["id"]
    response = client.put(
        f"/api/ci/services/{service_id}/source",
        json={
            "repositoryUrl": "https://user:pass@bitbucket.org/areeba/payment-service",
            "credentialProfileId": credential,
        },
        headers=auth_headers(admin_token),
    )
    assert response.status_code == 400


def test_working_directory_cannot_escape_the_repository(client, admin_token, credential):
    service_id = create_service(client, admin_token).get_json()["data"]["id"]
    response = client.put(
        f"/api/ci/services/{service_id}/source",
        json={
            "repositoryUrl": "https://bitbucket.org/areeba/payment-service",
            "credentialProfileId": credential,
            "workingDirectory": "../../etc",
        },
        headers=auth_headers(admin_token),
    )
    assert response.status_code == 400


# ---------------------------------------------------------------------------
# Pipelines
# ---------------------------------------------------------------------------

def test_pipeline_full_replace_reorders_stages(client, admin_token):
    service_id = create_service(client, admin_token).get_json()["data"]["id"]
    pipeline_id = client.get(
        f"/api/ci/services/{service_id}/pipelines", headers=auth_headers(admin_token)
    ).get_json()["data"]["items"][0]["id"]

    response = client.put(
        f"/api/ci/pipelines/{pipeline_id}",
        json={
            "name": "default",
            "stages": [
                {"name": "Checkout", "stageType": "checkout"},
                {"name": "Compile", "stageType": "command", "commands": ["mvn package"]},
                {"name": "Test", "stageType": "command", "commands": ["mvn test"]},
            ],
        },
        headers=auth_headers(admin_token),
    )
    assert response.status_code == 200
    data = response.get_json()["data"]
    assert [stage["name"] for stage in data["stages"]] == ["Checkout", "Compile", "Test"]
    assert [stage["position"] for stage in data["stages"]] == [0, 1, 2]
    # Version bumps so a build's snapshot records which revision it ran.
    assert data["version"] == 2


def test_command_stage_without_commands_is_rejected(client, admin_token):
    service_id = create_service(client, admin_token).get_json()["data"]["id"]
    pipeline_id = client.get(
        f"/api/ci/services/{service_id}/pipelines", headers=auth_headers(admin_token)
    ).get_json()["data"]["items"][0]["id"]

    response = client.put(
        f"/api/ci/pipelines/{pipeline_id}",
        json={"stages": [{"name": "Build", "stageType": "command", "commands": []}]},
        headers=auth_headers(admin_token),
    )
    assert response.status_code == 400
    assert "no commands" in response.get_json()["error"]


def test_duplicate_stage_names_are_rejected(client, admin_token):
    service_id = create_service(client, admin_token).get_json()["data"]["id"]
    pipeline_id = client.get(
        f"/api/ci/services/{service_id}/pipelines", headers=auth_headers(admin_token)
    ).get_json()["data"]["items"][0]["id"]

    response = client.put(
        f"/api/ci/pipelines/{pipeline_id}",
        json={
            "stages": [
                {"name": "Build", "stageType": "command", "commands": ["a"]},
                {"name": "build", "stageType": "command", "commands": ["b"]},
            ]
        },
        headers=auth_headers(admin_token),
    )
    assert response.status_code == 400
    assert "unique" in response.get_json()["error"]


def test_stage_referencing_an_undefined_secret_is_rejected(client, admin_token):
    service_id = create_service(client, admin_token).get_json()["data"]["id"]
    pipeline_id = client.get(
        f"/api/ci/services/{service_id}/pipelines", headers=auth_headers(admin_token)
    ).get_json()["data"]["items"][0]["id"]

    response = client.put(
        f"/api/ci/pipelines/{pipeline_id}",
        json={
            "stages": [
                {
                    "name": "Publish",
                    "stageType": "command",
                    "commands": ["./publish.sh"],
                    "secretRefs": [{"name": "NEXUS_PASSWORD"}],
                }
            ]
        },
        headers=auth_headers(admin_token),
    )
    assert response.status_code == 400
    assert "NEXUS_PASSWORD" in response.get_json()["error"]


def test_stage_secret_reference_is_accepted_once_the_secret_exists(client, admin_token):
    service_id = create_service(client, admin_token).get_json()["data"]["id"]
    client.post(
        f"/api/ci/services/{service_id}/secrets",
        json={"key": "NEXUS_PASSWORD", "value": "s3cr3t"},
        headers=auth_headers(admin_token),
    )
    pipeline_id = client.get(
        f"/api/ci/services/{service_id}/pipelines", headers=auth_headers(admin_token)
    ).get_json()["data"]["items"][0]["id"]

    response = client.put(
        f"/api/ci/pipelines/{pipeline_id}",
        json={
            "stages": [
                {
                    "name": "Publish",
                    "stageType": "command",
                    "commands": ["./publish.sh"],
                    "secretRefs": [{"name": "NEXUS_PASSWORD", "envVar": "NEXUS_PW"}],
                }
            ]
        },
        headers=auth_headers(admin_token),
    )
    assert response.status_code == 200
    assert response.get_json()["data"]["stages"][0]["secretRefs"] == [
        {"name": "NEXUS_PASSWORD", "envVar": "NEXUS_PW"}
    ]


# ---------------------------------------------------------------------------
# Secrets
# ---------------------------------------------------------------------------

def test_secret_value_is_never_returned(client, admin_token):
    service_id = create_service(client, admin_token).get_json()["data"]["id"]
    created = client.post(
        f"/api/ci/services/{service_id}/secrets",
        json={"key": "API_TOKEN", "value": "super-secret-value"},
        headers=auth_headers(admin_token),
    )
    assert created.status_code == 201
    body = created.get_data(as_text=True)
    assert "super-secret-value" not in body
    assert "value_cipher" not in body

    listed = client.get(
        f"/api/ci/services/{service_id}/secrets", headers=auth_headers(admin_token)
    )
    assert "super-secret-value" not in listed.get_data(as_text=True)
    assert listed.get_json()["data"]["items"][0]["key"] == "API_TOKEN"


def test_duplicate_secret_key_is_rejected(client, admin_token):
    service_id = create_service(client, admin_token).get_json()["data"]["id"]
    body = {"key": "API_TOKEN", "value": "v1"}
    client.post(
        f"/api/ci/services/{service_id}/secrets",
        json=body,
        headers=auth_headers(admin_token),
    )
    duplicate = client.post(
        f"/api/ci/services/{service_id}/secrets",
        json=body,
        headers=auth_headers(admin_token),
    )
    assert duplicate.status_code == 400


def test_global_secret_scope_is_independent_of_service_scope(client, admin_token):
    service_id = create_service(client, admin_token).get_json()["data"]["id"]
    assert (
        client.post(
            "/api/ci/secrets",
            json={"key": "SHARED", "value": "global-value"},
            headers=auth_headers(admin_token),
        ).status_code
        == 201
    )
    # A same-named service secret is allowed: it shadows the global one.
    assert (
        client.post(
            f"/api/ci/services/{service_id}/secrets",
            json={"key": "SHARED", "value": "service-value"},
            headers=auth_headers(admin_token),
        ).status_code
        == 201
    )
    # ...but a second global with that key is not.
    assert (
        client.post(
            "/api/ci/secrets",
            json={"key": "SHARED", "value": "again"},
            headers=auth_headers(admin_token),
        ).status_code
        == 400
    )


def test_service_secrets_shadow_global_secrets(app, client, admin_token):
    service_id = create_service(client, admin_token).get_json()["data"]["id"]
    client.post(
        "/api/ci/secrets",
        json={"key": "SHARED", "value": "global-value"},
        headers=auth_headers(admin_token),
    )
    client.post(
        f"/api/ci/services/{service_id}/secrets",
        json={"key": "SHARED", "value": "service-value"},
        headers=auth_headers(admin_token),
    )
    with app.app_context():
        from api.services.ci import secrets as secrets_service

        assert secrets_service.resolve_for_service(service_id)["SHARED"] == "service-value"


# ---------------------------------------------------------------------------
# RBAC
# ---------------------------------------------------------------------------

def test_viewer_can_read_but_not_write(client, admin_token, viewer_token):
    service_id = create_service(client, admin_token).get_json()["data"]["id"]

    assert client.get("/api/ci/services", headers=auth_headers(viewer_token)).status_code == 200
    assert (
        client.get(
            f"/api/ci/services/{service_id}/builds", headers=auth_headers(viewer_token)
        ).status_code
        == 200
    )
    assert create_service(client, viewer_token, name="Nope").status_code == 403
    assert (
        client.post(
            f"/api/ci/services/{service_id}/builds",
            json={},
            headers=auth_headers(viewer_token),
        ).status_code
        == 403
    )
    assert (
        client.delete(
            f"/api/ci/services/{service_id}", headers=auth_headers(viewer_token)
        ).status_code
        == 403
    )


def test_viewer_cannot_read_secret_names(client, admin_token, viewer_token):
    service_id = create_service(client, admin_token).get_json()["data"]["id"]
    response = client.get(
        f"/api/ci/services/{service_id}/secrets", headers=auth_headers(viewer_token)
    )
    assert response.status_code == 403


def test_operator_can_run_builds_but_not_edit_pipelines(client, admin_token, operator_token):
    service_id = create_service(client, admin_token).get_json()["data"]["id"]
    pipeline_id = client.get(
        f"/api/ci/services/{service_id}/pipelines", headers=auth_headers(operator_token)
    ).get_json()["data"]["items"][0]["id"]

    assert (
        client.put(
            f"/api/ci/pipelines/{pipeline_id}",
            json={"stages": []},
            headers=auth_headers(operator_token),
        ).status_code
        == 403
    )


def test_unauthenticated_requests_are_rejected(client):
    assert client.get("/api/ci/services").status_code == 401


def test_command_stage_keeps_heredoc_shape():
    """Commands join back into one shell script, so blank lines and leading
    indentation inside a heredoc are content, not formatting to be tidied."""
    from api.services.ci import pipelines as pipelines_service

    commands = [
        "cat > Dockerfile <<'EOF'",
        "FROM alpine",
        "",
        "    RUN echo indented",
        "EOF",
        "",
    ]
    parsed = pipelines_service._command_lines(commands)

    assert parsed == [
        "cat > Dockerfile <<'EOF'",
        "FROM alpine",
        "",
        "    RUN echo indented",
        "EOF",
    ]


def test_host_aliases_parse_text_and_structured_forms():
    """The editor sends structured entries; the text form is accepted too so a
    pasted `ip=host` block works and a saved stage round-trips unchanged."""
    from api.services.ci import pipelines as pipelines_service

    text_form = pipelines_service._host_aliases(
        "10.10.10.20=nexus.areeba.com,nexus\n\n  10.10.10.30 = db.internal  \n", "s"
    )
    assert text_form == [
        {"ip": "10.10.10.20", "hostnames": ["nexus.areeba.com", "nexus"]},
        {"ip": "10.10.10.30", "hostnames": ["db.internal"]},
    ]
    # Feeding the parsed form back in is a no-op — save/load/save is stable.
    assert pipelines_service._host_aliases(text_form, "s") == text_form


def test_host_aliases_merge_repeated_ips():
    from api.services.ci import pipelines as pipelines_service

    assert pipelines_service._host_aliases(
        "10.0.0.1=a.example\n10.0.0.1=b.example,a.example", "s"
    ) == [{"ip": "10.0.0.1", "hostnames": ["a.example", "b.example"]}]


def test_host_aliases_reject_malformed_entries():
    """A typo'd mapping must fail loudly: dropped silently, it resurfaces much
    later as a connect timeout inside a build tool."""
    import pytest as _pytest

    from api.services.ci import pipelines as pipelines_service
    from api.services.ci.pipelines import PipelineError

    for bad, expected in [
        ("not-an-ip=host.example", "invalid host alias IP"),
        ("10.0.0.1=", "no hostname"),
        ("10.0.0.1=bad host", "invalid host alias hostname"),
        ("10.0.0.1 host.example", "without '='"),
    ]:
        with _pytest.raises(PipelineError) as excinfo:
            pipelines_service._host_aliases(bad, "Build JAR")
        assert expected in str(excinfo.value)


def test_host_aliases_absent_means_none():
    """Stages saved before this field existed carry no value at all."""
    from api.services.ci import pipelines as pipelines_service

    for empty in (None, "", [], {}):
        assert pipelines_service._host_aliases(empty, "s") == []
