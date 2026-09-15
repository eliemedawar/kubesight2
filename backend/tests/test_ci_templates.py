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


@pytest.mark.parametrize("app_type", ["java_maven", "java_gradle"])
def test_the_java_dockerfile_copies_what_the_package_stage_produced(app_type):
    """The Dockerfile and the artifact path have to agree, or the image build
    copies a file the pipeline never produced.

    Both build tools converge on one app.jar, which is what lets a single recipe
    serve both — and what keeps it from having to know a version number.
    """
    package = next(
        stage
        for stage in templates.TEMPLATES[app_type]["stages"]
        if stage["name"] == "Package"
    )
    assert package["artifacts"][0]["path"] == "app.jar"
    assert "cp \"$jar\" app.jar" in package["commands"]
    assert "app.jar /app/app.jar" in templates.TEMPLATES[app_type]["dockerfile"]


def test_each_java_tool_looks_where_its_own_build_puts_the_jar():
    maven = next(
        s for s in templates.TEMPLATES["java_maven"]["stages"] if s["name"] == "Package"
    )["commands"][0]
    gradle = next(
        s for s in templates.TEMPLATES["java_gradle"]["stages"] if s["name"] == "Package"
    )["commands"][0]
    assert "target/*.jar" in maven and "sources|javadoc" in maven
    assert "build/libs/*.jar" in gradle and "-plain" in gradle


def test_the_legacy_java_type_still_resolves_to_a_real_kit():
    """Services registered before Java was split by build tool carry "java".
    Falling through to generic would swap a real starting point for
    "run ./build.sh" the next time anything read the template."""
    assert templates.template_for("java") is templates.TEMPLATES["java_maven"]
    assert templates.dockerfile_for("java") == templates.dockerfile_for("java_maven")
    assert "java" in templates.LEGACY_TYPES


def test_legacy_types_are_marked_so_the_picker_can_hide_them():
    by_type = {item["applicationType"]: item for item in templates.list_templates()}
    assert by_type["java"]["legacy"] is True
    assert by_type["java_maven"]["legacy"] is False


def test_accessors_hand_back_copies_not_the_registry():
    """Callers mutate what they get; the module-level template every future
    service is built from must not change with them."""
    first = templates.parameters_for("java_maven")
    first.append({"name": "SMUGGLED"})
    assert len(templates.parameters_for("java_maven")) == 1

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
    assert templates._JDK_IMAGE in by_type["java_maven"]["dockerfile"]
    assert by_type["java_maven"]["parameters"][0]["name"] == "SKIP_TESTS"
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
    payload = {"name": "Kit Service", "applicationType": "java_maven", **overrides}
    return client.post("/api/ci/services", json=payload, headers=auth_headers(token))


def test_registering_a_java_service_seeds_its_dockerfile(client, admin_token):
    response = _register(client, admin_token)
    assert response.status_code == 201
    data = response.get_json()["data"]
    assert data["hasInlineDockerfile"] is True
    assert templates._JDK_IMAGE in data["dockerfile"]


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

def test_java_builds_on_the_installations_own_jdk_image():
    """The build cluster has no route to Docker Hub, and every analysed Java
    repository here targets Java 11 — so a template pointing at a public JDK 21
    image is one that cannot pull, not merely one that is out of date."""
    for app_type in ("java_maven", "java_gradle"):
        images = {
            stage.get("image")
            for stage in templates.TEMPLATES[app_type]["stages"]
            if stage.get("image")
        }
        assert images == {templates._JDK_IMAGE}
        assert "openjdk11" in templates._JDK_IMAGE
        assert templates._JDK_IMAGE in templates.TEMPLATES[app_type]["dockerfile"]


def test_the_build_tool_version_comes_from_the_project_not_the_image():
    """The JDK image carries neither Maven nor Gradle. Using the repository's
    own wrapper is what keeps the tool at the version that project pins —
    Gradle 7.3.2 across this installation today — without a template ever
    naming a version it would then have to chase."""
    for app_type, wrapper in (("java_maven", "./mvnw"), ("java_gradle", "./gradlew")):
        build = next(
            stage
            for stage in templates.TEMPLATES[app_type]["stages"]
            if stage["name"] == "Build"
        )
        assert any(wrapper in command for command in build["commands"])
        # A missing wrapper has to say what to do about it, not exit 127.
        assert any("has no" in command and "wrapper" in command for command in build["commands"])


def test_images_can_be_repointed_without_editing_a_template(monkeypatch):
    """Another installation mirrors under different names; it should change a
    setting rather than fork this file."""
    import importlib

    monkeypatch.setenv("CI_TEMPLATE_IMAGE_REGISTRY", "nexus.example.test")
    monkeypatch.setenv("CI_TEMPLATE_NODE_IMAGE", "nexus.example.test/node:20")
    reloaded = importlib.reload(templates)
    try:
        assert reloaded._JDK_IMAGE.startswith("nexus.example.test/")
        assert reloaded._JDK_IMAGE in reloaded.dockerfile_for("java_gradle")
        node_images = {
            stage.get("image")
            for stage in reloaded.TEMPLATES["node"]["stages"]
            if stage.get("image")
        }
        assert node_images == {"nexus.example.test/node:20"}
    finally:
        # Every other test reads this module-level registry.
        monkeypatch.undo()
        importlib.reload(templates)


def test_list_templates_reports_the_real_build_images():
    """The registration form shows these instead of a version in a label, so a
    repointed image tells the truth there without anyone editing a string."""
    by_type = {item["applicationType"]: item for item in templates.list_templates()}
    assert by_type["java_gradle"]["buildImages"] == [templates._JDK_IMAGE]
    # Types that build nothing of their own advertise no image rather than a
    # misleading default.
    assert by_type["container"]["buildImages"] == []
    assert by_type["android"]["buildImages"] == []


def test_no_template_label_hardcodes_a_version():
    """A version in a label cannot follow CI_TEMPLATE_JDK_IMAGE, so it would
    start lying the first time an installation repointed its images."""
    import re

    for key, value in templates.TEMPLATES.items():
        assert not re.search(r"\d+\.\d+|JDK\s*\d|Java\s*\d", value["label"]), key
