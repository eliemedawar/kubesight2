"""Runner portability: the assumptions a pipeline makes about where it runs.

The cases here are real failures seen on this cluster — a stage referencing
/workspace on an agent, a docker build inside a build pod — so each test names
the runner it breaks on rather than asserting a generic "invalid".
"""

from __future__ import annotations

import pytest

from api.services.ci import portability
from tests.conftest import auth_headers


def _stage(position=1, **kw):
    return {
        "position": position,
        "name": kw.pop("name", f"Stage {position}"),
        "stageType": kw.pop("stageType", "command"),
        "runnerType": kw.pop("runnerType", None),
        "image": kw.pop("image", None),
        "workingDirectory": kw.pop("workingDirectory", None),
        "commands": kw.pop("commands", []),
        "env": kw.pop("env", {}),
        "artifacts": kw.pop("artifacts", []),
        "enabled": kw.pop("enabled", True),
        **kw,
    }


def _codes(result):
    return {finding["code"] for finding in result["findings"]}


def test_the_failure_from_build_44(app):
    """`/bin/sh: /workspace/nexus-init.gradle: No such file or directory` — an
    absolute Kubernetes path on an agent."""
    result = portability.analyze(
        [_stage(commands=["gradle -I /workspace/nexus-init.gradle clean build"])]
    )
    finding = next(f for f in result["findings"] if f["code"] == "absolute_workspace")
    assert finding["level"] == "error"
    assert finding["breaksOn"] == "agent"
    assert "$KUBESIGHT_WORKSPACE" in finding["fix"]
    assert result["portable"] is False
    assert "an agent" in result["summary"]


def test_the_exported_variables_are_not_flagged(app):
    """The whole point of $KUBESIGHT_WORKSPACE is that it is correct on both."""
    result = portability.analyze(
        [
            _stage(
                commands=[
                    'gradle -I "$KUBESIGHT_WORKSPACE/nexus-init.gradle" build',
                    'cd "$KUBESIGHT_SOURCE" && ls',
                ]
            )
        ]
    )
    assert result["findings"] == []
    assert result["portable"] is True
    assert "Nothing here depends" in result["summary"]


def test_a_stage_pinned_to_kubernetes_may_use_its_paths(app):
    """A stage that can never land on an agent is not doing anything wrong."""
    result = portability.analyze(
        [_stage(runnerType="kubernetes", commands=["cat /workspace/source/pom.xml"])]
    )
    assert "absolute_workspace" not in _codes(result)


def test_docker_and_package_installs_are_flagged_for_kubernetes(app):
    result = portability.analyze(
        [
            _stage(position=1, commands=["docker build -t app ."]),
            _stage(position=2, commands=["sudo apt-get install -y unzip"]),
        ]
    )
    codes = _codes(result)
    assert codes == {"docker_in_stage", "install_in_stage"}
    assert all(f["breaksOn"] == "kubernetes" for f in result["findings"])
    assert result["counts"]["error"] == 2
    assert "the Kubernetes runner" in result["summary"]


def test_an_image_stage_says_where_it_can_run(app):
    """It skips rather than fails on an agent, which is how a green build ends
    up producing no image."""
    result = portability.analyze([_stage(stageType="container_image")])
    finding = next(f for f in result["findings"] if f["code"] == "image_build_needs_buildkit")
    assert finding["level"] == "info"
    assert finding["breaksOn"] == "agent"
    # Info alone does not make a pipeline unportable.
    assert result["portable"] is True


def test_an_image_stage_pinned_to_kubernetes_says_nothing(app):
    """Pinning it settles the question; repeating the answer is noise."""
    result = portability.analyze(
        [_stage(stageType="container_image", runnerType="kubernetes")]
    )
    assert result["findings"] == []


def test_the_cache_path_is_a_warning_not_an_error(app):
    """It works when caching is on and silently does not when it is off."""
    result = portability.analyze([_stage(commands=["ls /cache/maven"])])
    finding = next(f for f in result["findings"] if f["code"] == "absolute_cache")
    assert finding["level"] == "warning"
    assert "$KUBESIGHT_CACHE" in finding["fix"]
    assert result["portable"] is True


def test_paths_are_found_wherever_an_author_can_type_them(app):
    result = portability.analyze(
        [
            _stage(position=1, workingDirectory="/workspace/source/api"),
            _stage(position=2, env={"GRADLE_USER_HOME": "/workspace/.gradle"}),
            _stage(position=3, artifacts=[{"path": "/workspace/source/target/*.jar"}]),
        ]
    )
    assert [f["code"] for f in result["findings"]] == ["absolute_workspace"] * 3
    assert [f["stagePosition"] for f in result["findings"]] == [1, 2, 3]


def test_a_disabled_stage_is_not_linted(app):
    result = portability.analyze(
        [_stage(enabled=False, commands=["docker build -t app /workspace"])]
    )
    assert result["findings"] == []


def test_similar_words_are_not_mistaken_for_paths(app):
    """A finding nobody believes is worse than no finding."""
    result = portability.analyze(
        [
            _stage(
                commands=[
                    "echo my/workspace-notes.txt",
                    "echo docker-compose.yml is a file",
                    "./gradlew :workspace:build",
                ]
            )
        ]
    )
    assert result["findings"] == []


def test_the_lint_route_checks_an_unsaved_edit(app, client, admin_token):
    response = client.post(
        "/api/ci/pipelines/lint",
        json={"stages": [_stage(commands=["cp /workspace/x.jar ."])]},
        headers=auth_headers(admin_token),
    )
    assert response.status_code == 200
    body = response.get_json()["data"]
    assert body["counts"]["error"] == 1
    assert body["findings"][0]["code"] == "absolute_workspace"

    bad = client.post("/api/ci/pipelines/lint", json={}, headers=auth_headers(admin_token))
    assert bad.status_code == 400


def test_a_saved_pipeline_can_be_checked_by_id(app, client, admin_token):
    service_id = client.post(
        "/api/ci/services",
        json={"name": "Profile ms", "applicationType": "java"},
        headers=auth_headers(admin_token),
    ).get_json()["data"]["id"]
    pipeline_id = client.get(
        f"/api/ci/services/{service_id}/pipelines", headers=auth_headers(admin_token)
    ).get_json()["data"]["items"][0]["id"]
    client.put(
        f"/api/ci/pipelines/{pipeline_id}",
        json={
            "stages": [
                {"name": "Checkout", "stageType": "checkout"},
                {
                    "name": "Build JAR",
                    "stageType": "command",
                    "commands": ["gradle -I /workspace/nexus-init.gradle build"],
                },
            ]
        },
        headers=auth_headers(admin_token),
    )

    body = client.get(
        f"/api/ci/pipelines/{pipeline_id}/portability", headers=auth_headers(admin_token)
    ).get_json()["data"]
    assert body["portable"] is False
    assert body["findings"][0]["stageName"] == "Build JAR"
