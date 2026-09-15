"""Generated defaults use native stages and never overwrite custom pipelines."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from api.db import db
from api.models_application_intelligence import BitbucketCredentialProfile
from api.models_ci import CiBuild, CiService
from api.secret_encryption import encrypt_secret
from api.services.ci import default_pipelines, pipelines
from tests.conftest import auth_headers


def _inspection(*paths, contents=None):
    values = {path: "" for path in paths}
    values.update(contents or {})
    return {"attempted": True, "files": values, "revision": "main"}


@pytest.mark.parametrize(
    "app_type",
    [
        "container",
        "java_maven",
        "java_gradle",
        "node",
        "python",
        "android",
        "ios",
        "flutter",
    ],
)
def test_every_supported_type_resolves_to_valid_native_stages(app, app_type):
    payload = default_pipelines.resolve_default_pipeline(app_type)
    with app.app_context():
        normalized = [
            pipelines.normalize_stage(stage, index, set())
            for index, stage in enumerate(payload["stages"])
        ]
    assert normalized
    assert normalized[0]["stage_type"] == "checkout"
    assert payload["metadata"]["source"] == "kubesight_default"


def test_gradle_and_maven_prefer_wrappers_but_have_tool_image_fallbacks():
    gradle = default_pipelines.resolve_default_pipeline(
        "java_gradle", _inspection("gradlew", "build.gradle")
    )
    assert gradle["metadata"]["detectedCommand"].startswith("./gradlew")
    assert gradle["stages"][1]["image"] == default_pipelines.templates._JDK_IMAGE
    assert gradle["stages"][1]["artifacts"] == [
        {"path": "build/libs/*.jar", "type": "jar"}
    ]
    assert gradle["stages"][1]["runnerLabels"] == ["linux", "java"]

    maven = default_pipelines.resolve_default_pipeline("java_maven", _inspection("pom.xml"))
    assert maven["metadata"]["detectedCommand"] == "mvn -B clean package"
    assert maven["stages"][1]["image"] == default_pipelines._MAVEN_IMAGE
    assert maven["stages"][1]["runnerLabels"] == ["linux", "java"]


@pytest.mark.parametrize(
    ("lockfile", "expected"),
    [
        ("pnpm-lock.yaml", "corepack pnpm"),
        ("yarn.lock", "corepack yarn"),
        ("package-lock.json", "npm ci"),
    ],
)
def test_node_selects_the_package_manager_from_its_lockfile(lockfile, expected):
    payload = default_pipelines.resolve_default_pipeline(
        "node",
        _inspection(
            lockfile,
            contents={"package.json": '{"scripts":{"build":"vite build"}}'},
        ),
    )
    assert expected in payload["stages"][1]["commands"][0]
    assert "build" in payload["stages"][2]["commands"][0]


def test_node_checkout_time_detection_does_not_hide_a_failed_build():
    payload = default_pipelines.resolve_default_pipeline("node")
    command = payload["stages"][2]["commands"][0]
    assert "|| true" not in command
    assert "p.scripts&&p.scripts.build" in command


def test_python_selects_the_manifest_and_ios_requires_a_macos_runner():
    python = default_pipelines.resolve_default_pipeline(
        "python", _inspection("pyproject.toml")
    )
    assert python["stages"][1]["commands"] == [
        "python -m pip install --no-cache-dir ."
    ]

    ios = default_pipelines.resolve_default_pipeline("ios", _inspection())
    assert all(stage["runnerType"] == "agent_macos" for stage in ios["stages"])
    assert all("macos" in stage["runnerLabels"] for stage in ios["stages"])


def test_custom_type_refuses_to_guess_a_command():
    payload = default_pipelines.resolve_default_pipeline("generic")
    assert payload["stages"] == []
    assert payload["metadata"]["requiresCustomization"] is True


def test_repository_inspection_uses_the_requested_ref_and_service_subdirectory(monkeypatch):
    calls = []

    class Provider:
        @staticmethod
        def parse_repository_url(url):
            return url

        @staticmethod
        def read_file(ref, credential, revision, path):
            calls.append((ref, credential, revision, path))
            if path.endswith("gradlew"):
                return "#!/bin/sh"
            raise LookupError(path)

    monkeypatch.setattr(default_pipelines.source_port, "get_provider", lambda name: Provider())
    service = SimpleNamespace(
        application_type="java_gradle",
        working_directory="backend/service-a",
        repository_provider="bitbucket",
        repository_url="https://bitbucket.org/team/repository",
        credential_profile="credential",
        default_branch="main",
        source_ready=lambda: True,
    )

    result = default_pipelines.inspect_repository(service, "release/2.0")

    assert result["files"] == {"gradlew": "#!/bin/sh"}
    assert calls[0] == (
        service.repository_url,
        "credential",
        "release/2.0",
        "backend/service-a/gradlew",
    )


@pytest.fixture()
def fallback_service(app, client, admin_token, monkeypatch):
    with app.app_context():
        credential = BitbucketCredentialProfile(
            name="default-pipeline-source",
            provider="bitbucket",
            credential_type="repository_access_token",
            secret_cipher=encrypt_secret("source-token"),
            read_only=True,
            enabled=True,
        )
        db.session.add(credential)
        db.session.commit()
        credential_id = credential.id

    service = client.post(
        "/api/ci/services",
        json={"name": "Fallback Gradle", "applicationType": "java_gradle"},
        headers=auth_headers(admin_token),
    ).get_json()["data"]
    client.put(
        f"/api/ci/services/{service['id']}/source",
        json={
            "repositoryUrl": "https://bitbucket.org/areeba/fallback-gradle",
            "defaultBranch": "main",
            "credentialProfileId": credential_id,
        },
        headers=auth_headers(admin_token),
    )
    monkeypatch.setattr(
        default_pipelines,
        "inspect_repository",
        lambda service, revision="": _inspection("gradlew", "build.gradle"),
    )
    return service["id"]


def test_empty_saved_pipeline_is_presented_and_snapshotted_as_generated_default(
    app, client, admin_token, fallback_service
):
    data = client.get(
        f"/api/ci/services/{fallback_service}/pipelines",
        headers=auth_headers(admin_token),
    ).get_json()["data"]["items"][0]
    assert data["isGeneratedDefault"] is True
    assert data["defaultMetadata"]["detectedCommand"].startswith("./gradlew")
    assert [stage["name"] for stage in data["stages"]] == ["Checkout", "Build"]

    response = client.post(
        f"/api/ci/services/{fallback_service}/builds",
        json={"branch": "main"},
        headers=auth_headers(admin_token),
    )
    assert response.status_code == 201, response.get_json()
    with app.app_context():
        service = db.session.get(CiService, fallback_service)
        assert len(service.default_pipeline().stages) == 0
        build = db.session.get(CiBuild, response.get_json()["data"]["id"])
        assert build.pipeline_snapshot["pipelineSource"] == "kubesight_default"
        assert [stage["name"] for stage in build.pipeline_snapshot["stages"]] == [
            "Checkout",
            "Build",
        ]


def test_saved_custom_stages_take_precedence(app, client, admin_token, fallback_service):
    pipeline = client.get(
        f"/api/ci/services/{fallback_service}/pipelines",
        headers=auth_headers(admin_token),
    ).get_json()["data"]["items"][0]
    saved = client.put(
        f"/api/ci/pipelines/{pipeline['id']}",
        json={
            "name": "default",
            "stages": [
                {"name": "Checkout", "stageType": "checkout"},
                {"name": "My Build", "stageType": "command", "commands": ["make all"]},
            ],
        },
        headers=auth_headers(admin_token),
    )
    assert saved.status_code == 200

    listed = client.get(
        f"/api/ci/services/{fallback_service}/pipelines",
        headers=auth_headers(admin_token),
    ).get_json()["data"]["items"][0]
    assert listed["isGeneratedDefault"] is False
    assert [stage["name"] for stage in listed["stages"]] == ["Checkout", "My Build"]


def test_custom_service_requires_a_real_command(client, admin_token):
    service = client.post(
        "/api/ci/services",
        json={"name": "Custom App", "applicationType": "generic"},
        headers=auth_headers(admin_token),
    ).get_json()["data"]
    assert service["pipelineConfigured"] is False
    summary = client.get(
        f"/api/ci/services/{service['id']}/summary",
        headers=auth_headers(admin_token),
    ).get_json()["data"]
    check = next(item for item in summary["readiness"]["checks"] if item["key"] == "pipeline")
    assert check["ok"] is False
    assert "Custom services" in check["hint"]


def test_generated_default_does_not_require_a_placeholder_pipeline(client, admin_token):
    response = client.post(
        "/api/ci/services",
        json={
            "name": "No Saved Pipeline",
            "applicationType": "python",
            "createDefaultPipeline": False,
        },
        headers=auth_headers(admin_token),
    )
    assert response.status_code == 201
    service = response.get_json()["data"]
    assert service["pipelineId"] is None
    assert service["pipelineConfigured"] is True
    assert service["usingDefaultPipeline"] is True

    listed = client.get(
        f"/api/ci/services/{service['id']}/pipelines",
        headers=auth_headers(admin_token),
    ).get_json()["data"]["items"][0]
    assert listed["id"] is None
    assert listed["isGeneratedDefault"] is True
