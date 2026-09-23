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


def test_build_resources_default_to_nothing_set(client, admin_token):
    """A fresh service names no envelope of its own, and the API says what the
    installation would give it \u2014 which for disk is nothing at all."""
    data = create_service(client, admin_token).get_json()["data"]

    assert data["buildResources"] == {}
    assert data["buildResourceDefaults"]["ephemeralStorage"] == "off"
    assert data["buildResourceDefaults"]["memory"] == "4Gi"


def test_build_resources_are_saved_per_field(client, admin_token):
    """Each field stands on its own: a value, "off" for no limit, or absent to
    inherit. An absent field must not be written as an empty string \u2014 that is
    what the runner reads as "the installation decides"."""
    service_id = create_service(client, admin_token).get_json()["data"]["id"]

    response = client.put(
        f"/api/ci/services/{service_id}",
        json={"buildResources": {"ephemeralStorage": "16Gi", "cpu": "off", "memory": ""}},
        headers=auth_headers(admin_token),
    )
    assert response.status_code == 200
    saved = response.get_json()["data"]["buildResources"]
    assert saved == {"cpu": "off", "ephemeralStorage": "16Gi"}


def test_build_resources_reject_a_value_kubernetes_would_not_take(client, admin_token):
    """Caught on save, not at dispatch: the alternative is a pod that fails to
    create hours later with the reason in an event nobody is watching."""
    service_id = create_service(client, admin_token).get_json()["data"]["id"]

    response = client.put(
        f"/api/ci/services/{service_id}",
        json={"buildResources": {"ephemeralStorage": "8 gigs"}},
        headers=auth_headers(admin_token),
    )
    assert response.status_code == 400
    assert "Ephemeral storage" in response.get_json()["error"]


def test_build_resources_can_be_cleared_back_to_the_default(client, admin_token):
    service_id = create_service(client, admin_token).get_json()["data"]["id"]
    headers = auth_headers(admin_token)

    client.put(
        f"/api/ci/services/{service_id}",
        json={"buildResources": {"ephemeralStorage": "16Gi"}},
        headers=headers,
    )
    response = client.put(
        f"/api/ci/services/{service_id}", json={"buildResources": {}}, headers=headers
    )

    assert response.status_code == 200
    assert response.get_json()["data"]["buildResources"] == {}


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


def test_service_secret_can_be_promoted_to_global(app, client, admin_token):
    """The value survives the move, and a second service then resolves it."""
    service_id = create_service(client, admin_token).get_json()["data"]["id"]
    other_id = create_service(client, admin_token, name="other-service").get_json()["data"][
        "id"
    ]
    secret_id = (
        client.post(
            f"/api/ci/services/{service_id}/secrets",
            json={"key": "NVD_API_KEY", "value": "nvd-value"},
            headers=auth_headers(admin_token),
        )
        .get_json()["data"]["id"]
    )

    promoted = client.put(
        f"/api/ci/secrets/{secret_id}",
        json={"scope": "global"},
        headers=auth_headers(admin_token),
    )
    assert promoted.status_code == 200
    assert promoted.get_json()["data"]["scope"] == "global"
    assert promoted.get_json()["data"]["serviceId"] is None

    with app.app_context():
        from api.services.ci import secrets as secrets_service

        assert secrets_service.resolve_for_service(other_id)["NVD_API_KEY"] == "nvd-value"


def test_global_secret_can_be_demoted_to_one_service(app, client, admin_token):
    service_id = create_service(client, admin_token).get_json()["data"]["id"]
    other_id = create_service(client, admin_token, name="other-service").get_json()["data"][
        "id"
    ]
    secret_id = (
        client.post(
            "/api/ci/secrets",
            json={"key": "NVD_API_KEY", "value": "nvd-value"},
            headers=auth_headers(admin_token),
        )
        .get_json()["data"]["id"]
    )

    demoted = client.put(
        f"/api/ci/secrets/{secret_id}",
        json={"scope": "service", "serviceId": service_id},
        headers=auth_headers(admin_token),
    )
    assert demoted.status_code == 200
    assert demoted.get_json()["data"]["serviceId"] == service_id

    with app.app_context():
        from api.services.ci import secrets as secrets_service

        assert "NVD_API_KEY" in secrets_service.resolve_for_service(service_id)
        assert "NVD_API_KEY" not in secrets_service.resolve_for_service(other_id)


def test_promotion_is_rejected_when_a_global_of_that_name_exists(client, admin_token):
    service_id = create_service(client, admin_token).get_json()["data"]["id"]
    client.post(
        "/api/ci/secrets",
        json={"key": "SHARED", "value": "global-value"},
        headers=auth_headers(admin_token),
    )
    secret_id = (
        client.post(
            f"/api/ci/services/{service_id}/secrets",
            json={"key": "SHARED", "value": "service-value"},
            headers=auth_headers(admin_token),
        )
        .get_json()["data"]["id"]
    )

    clash = client.put(
        f"/api/ci/secrets/{secret_id}",
        json={"scope": "global"},
        headers=auth_headers(admin_token),
    )
    assert clash.status_code == 400
    # The secret stays where it was rather than half-moving.
    listed = client.get(
        f"/api/ci/services/{service_id}/secrets", headers=auth_headers(admin_token)
    ).get_json()["data"]["items"]
    scopes = sorted(item["scope"] for item in listed if item["key"] == "SHARED")
    assert scopes == ["global", "service"]


def test_changing_scope_leaves_the_value_untouched(app, client, admin_token):
    """A move is not a rotation — pipelines referencing the name keep working."""
    service_id = create_service(client, admin_token).get_json()["data"]["id"]
    secret_id = (
        client.post(
            f"/api/ci/services/{service_id}/secrets",
            json={"key": "TOKEN", "value": "keep-me"},
            headers=auth_headers(admin_token),
        )
        .get_json()["data"]["id"]
    )
    response = client.put(
        f"/api/ci/secrets/{secret_id}",
        json={"scope": "global"},
        headers=auth_headers(admin_token),
    )
    assert "keep-me" not in response.get_data(as_text=True)

    with app.app_context():
        from api.services.ci import secrets as secrets_service

        assert secrets_service.resolve_for_service(service_id)["TOKEN"] == "keep-me"


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


def test_json_list_column_survives_a_text_typed_column():
    """PostgreSQL hands a db.JSON attribute back as a raw string when the column
    was created as TEXT. list() over that string yields one entry per character,
    which reached the editor as a row of 'undefined=' lines — decode instead.
    """
    from api.services.ci.serializers import _json_list

    stored = '[{"ip": "10.10.10.20", "hostnames": ["nexus.areeba.com", "nexus"]}]'
    assert _json_list(stored) == [
        {"ip": "10.10.10.20", "hostnames": ["nexus.areeba.com", "nexus"]}
    ]
    assert _json_list([{"ip": "10.0.0.1", "hostnames": ["a"]}]) == [
        {"ip": "10.0.0.1", "hostnames": ["a"]}
    ]
    for empty in (None, "", "not json", {}, 7):
        assert _json_list(empty) == []


def test_service_dockerfile_round_trips_and_clears(app, admin_token):
    """A Dockerfile is a document: its blank lines and indentation are content.
    Clearing it must restore the original behaviour of using the repository's
    own file, so empty is stored as NULL rather than an empty string."""
    from api.db import db
    from api.models_ci import CiService
    from api.services.ci import catalog as catalog_service

    with app.app_context():
        row = CiService(name="Dockerfile Svc", slug="dockerfile-svc")
        db.session.add(row)
        db.session.commit()

        body = "FROM alpine\n\n    RUN echo indented\nADD app.jar app.jar"
        detail = catalog_service.update_service(row, {"dockerfile": body + "\n\n"})
        assert detail["dockerfile"] == body  # only trailing whitespace removed
        assert detail["hasInlineDockerfile"] is True

        cleared = catalog_service.update_service(row, {"dockerfile": "   "})
        assert cleared["dockerfile"] == ""
        assert cleared["hasInlineDockerfile"] is False
        assert db.session.get(CiService, row.id).dockerfile is None


def test_service_dockerfile_has_a_size_limit(app, admin_token):
    import pytest as _pytest

    from api.db import db
    from api.models_ci import CiService
    from api.services.ci import catalog as catalog_service
    from api.services.ci.catalog import MAX_DOCKERFILE_CHARS, CatalogError

    with app.app_context():
        row = CiService(name="Big Dockerfile", slug="big-dockerfile")
        db.session.add(row)
        db.session.commit()
        with _pytest.raises(CatalogError):
            catalog_service.update_service(row, {"dockerfile": "x" * (MAX_DOCKERFILE_CHARS + 1)})


def _params(*entries):
    from api.services.ci import pipelines as pipelines_service

    return pipelines_service._parameters(list(entries))


def test_build_parameters_normalise_each_type():
    parsed = _params(
        {"name": "DEPLOY_ENV", "type": "choice", "choices": "uat\nprod\nuat", "default": "prod"},
        {"name": "SKIP_TESTS", "type": "boolean", "default": "yes", "required": True},
        {"name": "RELEASE_REF", "type": "dynamic_choice", "source": "tags"},
        {"name": "NOTE", "label": "Release note"},
    )
    choice, boolean, dynamic, text = parsed

    assert choice["choices"] == ["uat", "prod"]  # de-duplicated, order kept
    assert choice["default"] == "prod"
    # A checkbox always has a value, so "required" is meaningless on it.
    assert boolean["default"] == "true" and boolean["required"] is False
    # Nothing is stored for a dynamic choice: the list is read from the
    # repository when Run Build opens, so a saved pipeline cannot go stale.
    assert dynamic["source"] == "tags" and "choices" not in dynamic
    assert text["type"] == "text" and text["label"] == "Release note"


def test_build_parameters_reject_unusable_definitions():
    import pytest as _pytest

    from api.services.ci.pipelines import PipelineError

    for entry, expected in [
        ({"name": "2BAD", "type": "text"}, "not usable as an environment variable"),
        ({"name": "OK", "type": "nonsense"}, "unknown type"),
        ({"name": "OK", "type": "choice", "choices": []}, "lists no options"),
        ({"name": "OK", "type": "choice", "choices": ["a"], "default": "b"}, "not one of its options"),
        ({"name": "OK", "type": "dynamic_choice", "source": "moon"}, "unknown source"),
    ]:
        with _pytest.raises(PipelineError) as excinfo:
            _params(entry)
        assert expected in str(excinfo.value)

    with _pytest.raises(PipelineError) as excinfo:
        _params({"name": "DUP", "type": "text"}, {"name": "DUP", "type": "text"})
    assert "defined twice" in str(excinfo.value)


def test_parameter_values_are_validated_not_dropped(app, admin_token):
    """A build that silently ignored a parameter would run differently from what
    was asked for and say nothing about it."""
    import pytest as _pytest

    from api.db import db
    from api.models_ci import CiPipeline, CiService
    from api.services.ci import pipelines as pipelines_service
    from api.services.ci.pipelines import PipelineError

    with app.app_context():
        service = CiService(name="Param Svc", slug="param-svc")
        db.session.add(service)
        db.session.commit()
        pipeline = CiPipeline(
            service_id=service.id,
            name="default",
            parameters=_params(
                {"name": "DEPLOY_ENV", "type": "choice", "choices": ["uat", "prod"]},
                {"name": "SKIP_TESTS", "type": "boolean"},
                {"name": "TICKET", "type": "text", "required": True},
            ),
        )
        db.session.add(pipeline)
        db.session.commit()

        accepted = pipelines_service.validate_parameter_values(
            pipeline, {"DEPLOY_ENV": "prod", "TICKET": "OPS-1", "SKIP_TESTS": True}
        )
        assert accepted == {"DEPLOY_ENV": "prod", "SKIP_TESTS": "true", "TICKET": "OPS-1"}

        # Defaults fill in what was not submitted.
        filled = pipelines_service.validate_parameter_values(pipeline, {"TICKET": "OPS-2"})
        assert filled["DEPLOY_ENV"] == "uat" and filled["SKIP_TESTS"] == "false"

        for bad, expected in [
            ({"TICKET": "OPS-3", "DEPLOY_ENV": "staging"}, "must be one of"),
            ({"DEPLOY_ENV": "uat"}, "is required"),
            ({"TICKET": "OPS-4", "NOPE": "x"}, "no parameter named"),
        ]:
            with _pytest.raises(PipelineError) as excinfo:
                pipelines_service.validate_parameter_values(pipeline, bad)
            assert expected in str(excinfo.value)


def test_a_pipeline_without_parameters_still_takes_free_variables(app, admin_token):
    """The deploy automation pins IMAGE_TAG on pipelines that declare nothing."""
    from api.db import db
    from api.models_ci import CiPipeline, CiService
    from api.services.ci import pipelines as pipelines_service

    with app.app_context():
        service = CiService(name="Free Svc", slug="free-svc")
        db.session.add(service)
        db.session.commit()
        pipeline = CiPipeline(service_id=service.id, name="default", parameters=[])
        db.session.add(pipeline)
        db.session.commit()

        assert pipelines_service.validate_parameter_values(
            pipeline, {"IMAGE_TAG": "V1.0.27-prod"}
        ) == {"IMAGE_TAG": "V1.0.27-prod"}
