"""Starter kits per application type.

The registry in ``services/ci/templates.py`` is data, and data that is only
exercised when somebody registers a service of that type is data that rots. The
first half of this file holds every template to the same validation the API
applies to a hand-written pipeline; the second half checks what registration
actually does with it.
"""

from __future__ import annotations

import pytest

from api.services.ci import templates
from tests.conftest import auth_headers


def _image_stage_types(definition):
    return [stage.get("stageType") for stage in definition["stages"]]


# ---------------------------------------------------------------------------
# The registry itself
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("app_type", sorted(templates.TEMPLATES))
def test_every_template_saves_through_the_pipeline_validator(app, app_type, admin_token, client):
    """A template is only useful if the API accepts it verbatim.

    Registering a service is the path that proves it: the payload goes through
    ``_parameters``, ``_apply_stages`` and ``_run_condition`` exactly as a
    hand-written pipeline would.
    """
    response = client.post(
        "/api/ci/services",
        json={"name": f"Kit {app_type}", "applicationType": app_type},
        headers=auth_headers(admin_token),
    )
    assert response.status_code == 201, response.get_json()
    data = response.get_json()["data"]
    assert data["pipelineStageCount"] == len(templates.TEMPLATES[app_type]["stages"])


@pytest.mark.parametrize("app_type", sorted(templates.TEMPLATES))
def test_a_dockerfile_is_defined_exactly_where_it_is_wanted(app_type):
    """Only types whose pipeline builds an image, and that do not hand the job
    to the repository, predefine a Dockerfile."""
    definition = templates.TEMPLATES[app_type]
    dockerfile = definition["dockerfile"]
    builds_an_image = "container_image" in _image_stage_types(definition)

    if app_type == "container":
        # The one type that builds an image and must NOT carry a recipe: its
        # whole premise is the Dockerfile committed in the repository.
        assert dockerfile == ""
    elif builds_an_image:
        assert dockerfile.startswith("#") or dockerfile.startswith("FROM")
        assert "FROM " in dockerfile
    else:
        assert dockerfile == ""


@pytest.mark.parametrize("app_type", sorted(templates.TEMPLATES))
def test_predefined_dockerfiles_do_not_need_root(app_type):
    """The target clusters enforce restricted Pod Security. An image that only
    runs as root is rejected at admission, long after the build went green."""
    dockerfile = templates.TEMPLATES[app_type]["dockerfile"]
    if dockerfile:
        assert "\nUSER " in dockerfile


@pytest.mark.parametrize("app_type", sorted(templates.TEMPLATES))
def test_every_parameter_is_read_by_a_stage(app_type):
    """A parameter nothing consumes is a question with no consequence, which
    teaches people to ignore the Run Build dialog."""
    definition = templates.TEMPLATES[app_type]
    declared = {param["name"] for param in definition["parameters"]}
    consumed = {
        stage["runCondition"]["variable"]
        for stage in definition["stages"]
        if stage.get("runCondition")
    }
    assert declared == consumed, f"{app_type}: declared {declared}, consumed {consumed}"


@pytest.mark.parametrize("app_type", sorted(templates.TEMPLATES))
def test_no_template_stage_references_a_secret(app_type):
    """``pipelines._secret_refs`` rejects a reference to a secret that does not
    exist yet, so a template carrying one would make registration fail for
    every service of that type. Expected secrets are declared instead."""
    for stage in templates.TEMPLATES[app_type]["stages"]:
        assert not stage.get("secretRefs")


@pytest.mark.parametrize("app_type", sorted(templates.TEMPLATES))
def test_expected_secrets_are_keys_and_never_values(app_type):
    for item in templates.TEMPLATES[app_type]["expectedSecrets"]:
        assert set(item) == {"key", "description"}
        assert item["key"] and item["description"]
        assert "value" not in item


def test_android_can_build_one_half_without_the_other():
    """The Jenkins job this replaces had BUILD_APK_ONLY / BUILD_AAB_ONLY. One
    Gradle invocation doing both could only be skipped wholesale."""
    stages = {stage["name"]: stage for stage in templates.TEMPLATES["android"]["stages"]}
    assert stages["Assemble APK"]["runCondition"]["variable"] == "BUILD_APK"
    assert stages["Assemble AAB"]["runCondition"]["variable"] == "BUILD_AAB"
    assert stages["Assemble APK"]["artifacts"][0]["type"] == "apk"
    assert stages["Assemble AAB"]["artifacts"][0]["type"] == "aab"


def test_the_java_dockerfile_copies_what_the_package_stage_produced():
    """The Dockerfile and the artifact glob have to agree, or the image build
    copies a path the pipeline never filled."""
    package = next(
        stage for stage in templates.TEMPLATES["java"]["stages"] if stage["name"] == "Package"
    )
    assert package["artifacts"][0]["path"] == "target/*.jar"
    assert "COPY target/*.jar" in templates.TEMPLATES["java"]["dockerfile"]


def test_accessors_hand_back_copies_not_the_registry():
    """Callers mutate what they get; the module-level template every future
    service is built from must not change with them."""
    first = templates.parameters_for("java")
    first.append({"name": "SMUGGLED"})
    assert len(templates.parameters_for("java")) == 1

    secrets = templates.expected_secrets_for("android")
    secrets[0]["key"] = "CHANGED"
    assert templates.expected_secrets_for("android")[0]["key"] == "ANDROID_KEYSTORE_B64"


def test_unknown_application_type_falls_back_to_generic():
    assert templates.dockerfile_for("cobol") == ""
    assert templates.parameters_for("cobol") == []
    assert templates.template_for("")["label"] == "Generic"


def test_list_templates_carries_the_whole_kit():
    by_type = {item["applicationType"]: item for item in templates.list_templates()}
    assert set(by_type) == set(templates.TEMPLATES)
    assert "FROM eclipse-temurin" in by_type["java"]["dockerfile"]
    assert by_type["java"]["parameters"][0]["name"] == "SKIP_TESTS"
    assert by_type["container"]["dockerfile"] == ""
    assert {s["key"] for s in by_type["ios"]["expectedSecrets"]} == {
        "IOS_P12_B64",
        "IOS_P12_PASSWORD",
        "IOS_PROVISIONING_PROFILE_B64",
    }


# ---------------------------------------------------------------------------
# What registration does with it
# ---------------------------------------------------------------------------

def _register(client, token, **overrides):
    payload = {"name": "Kit Service", "applicationType": "java", **overrides}
    return client.post("/api/ci/services", json=payload, headers=auth_headers(token))


def test_registering_a_java_service_seeds_its_dockerfile(client, admin_token):
    response = _register(client, admin_token)
    assert response.status_code == 201
    data = response.get_json()["data"]
    assert data["hasInlineDockerfile"] is True
    assert "eclipse-temurin" in data["dockerfile"]


def test_registering_a_container_service_leaves_the_repository_in_charge(client, admin_token):
    """The inline Dockerfile takes precedence over the repository's, so seeding
    one for the type that exists to build the repository's would override it."""
    data = _register(
        client, admin_token, name="Container App", applicationType="container"
    ).get_json()["data"]
    assert data["hasInlineDockerfile"] is False
    assert data["dockerfile"] == ""


def test_registering_an_android_service_seeds_no_dockerfile(client, admin_token):
    data = _register(
        client, admin_token, name="Wallet Android", applicationType="android"
    ).get_json()["data"]
    assert data["hasInlineDockerfile"] is False


def test_a_supplied_dockerfile_is_not_overwritten_by_the_template(client, admin_token):
    data = _register(
        client, admin_token, dockerfile="FROM scratch\nUSER 1000"
    ).get_json()["data"]
    assert data["dockerfile"] == "FROM scratch\nUSER 1000"


def test_seeding_the_dockerfile_can_be_turned_off(client, admin_token):
    data = _register(client, admin_token, createDefaultDockerfile=False).get_json()["data"]
    assert data["hasInlineDockerfile"] is False


def test_the_starter_pipeline_carries_the_types_parameters(client, admin_token):
    service_id = _register(client, admin_token, applicationType="android").get_json()["data"]["id"]
    pipelines = client.get(
        f"/api/ci/services/{service_id}/pipelines", headers=auth_headers(admin_token)
    ).get_json()["data"]["items"]
    default = next(item for item in pipelines if item["isDefault"])
    assert [param["name"] for param in default["parameters"]] == [
        "SKIP_TESTS",
        "BUILD_APK",
        "BUILD_AAB",
    ]
    assert default["parameters"][1]["default"] == "true"


def test_expected_secrets_report_which_ones_are_still_missing(client, admin_token):
    service_id = _register(client, admin_token, applicationType="android").get_json()["data"]["id"]

    def expected():
        summary = client.get(
            f"/api/ci/services/{service_id}/summary", headers=auth_headers(admin_token)
        ).get_json()["data"]
        return {item["key"]: item["set"] for item in summary["expectedSecrets"]}

    assert expected() == {
        "ANDROID_KEYSTORE_B64": False,
        "ANDROID_STORE_PASS": False,
        "ANDROID_KEY_ALIAS": False,
        "ANDROID_KEY_PASS": False,
    }

    client.post(
        f"/api/ci/services/{service_id}/secrets",
        json={"key": "ANDROID_STORE_PASS", "value": "hunter2"},
        headers=auth_headers(admin_token),
    )
    assert expected()["ANDROID_STORE_PASS"] is True


def test_expected_secrets_are_advisory_and_do_not_block_run_build(client, admin_token):
    """A build can be entirely correct without them — the stage that would use
    one may not be in this pipeline."""
    service_id = _register(client, admin_token, applicationType="android").get_json()["data"]["id"]
    summary = client.get(
        f"/api/ci/services/{service_id}/summary", headers=auth_headers(admin_token)
    ).get_json()["data"]
    assert any(item["set"] is False for item in summary["expectedSecrets"])
    assert [check["key"] for check in summary["readiness"]["checks"]] == [
        "source",
        "pipeline",
        "active",
    ]


def test_a_type_with_no_expected_secrets_reports_an_empty_list(client, admin_token):
    service_id = _register(
        client, admin_token, applicationType="container"
    ).get_json()["data"]["id"]
    summary = client.get(
        f"/api/ci/services/{service_id}/summary", headers=auth_headers(admin_token)
    ).get_json()["data"]
    assert summary["expectedSecrets"] == []


def test_a_parameterised_starter_pipeline_still_accepts_automation_variables(
    app, client, admin_token
):
    """Deploy automation pins the image tag through per-trigger variables.

    Before the starter pipelines declared any parameters, every pipeline took
    free-form variables. A parameter list turns that check on — so without the
    engine-reserved pass-through, giving java a SKIP_TESTS parameter would have
    made every ticket-driven deploy to a newly registered service fail with
    "this pipeline has no parameter named 'IMAGE_TAG'".
    """
    from api.models_ci import CiService
    from api.services.ci import pipelines as pipelines_service

    service_id = _register(client, admin_token).get_json()["data"]["id"]
    with app.app_context():
        service = db_get(CiService, service_id)
        pipeline = service.default_pipeline()
        assert [param["name"] for param in pipeline.parameters] == ["SKIP_TESTS"]

        accepted = pipelines_service.validate_parameter_values(
            pipeline,
            {"IMAGE_TAG": "REL-4412", "TICKET_TAG": "REL-4412", "KUBESIGHT_TICKET": "TK-9"},
        )
        assert accepted["IMAGE_TAG"] == "REL-4412"
        # The parameter the pipeline does declare still gets its default.
        assert accepted["SKIP_TESTS"] == "false"

        # A genuinely unknown variable is still refused: the point of the check
        # is that a build never runs differently from what was asked for.
        with pytest.raises(pipelines_service.PipelineError):
            pipelines_service.validate_parameter_values(pipeline, {"FOO": "bar"})


def db_get(model, row_id):
    from api.db import db

    return db.session.get(model, row_id)
