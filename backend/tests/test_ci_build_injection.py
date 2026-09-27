"""Build variables must not be able to rewrite the generated image-build shell.

The container_image stage is the one container with the registry push
credentials mounted, and the engine splices DOCKERFILE_PATH / IMAGE_TAG /
IMAGE_NAME into its buildctl line. A user who may only RUN builds used to be
able to send ``DOCKERFILE_PATH=x;cat /kubesight-docker/config.json;#/Dockerfile``
and have it executed. These tests pin the three layers that stop it: the
trigger refuses the value, the pipeline save refuses it, and the runner both
re-checks and shell-quotes every value it interpolates.
"""

from __future__ import annotations

import shlex

import pytest

from api.db import db
from api.models_application_intelligence import BitbucketCredentialProfile
from api.models_ci import CiBuild
from api.secret_encryption import encrypt_secret
from api.services.ci import build_inputs
from api.services.ci.runners import kubernetes as k8s
from api.services.ci.runners.base import RunnerError, StageExecution
from tests.conftest import auth_headers

MALICIOUS_DOCKERFILE = "x;cat /kubesight-docker/config.json;#/Dockerfile"
MALICIOUS_TAG = "v1;cat /kubesight-docker/config.json"


# ---------------------------------------------------------------------------
# Trigger time
# ---------------------------------------------------------------------------

@pytest.fixture()
def service_id(app, client, admin_token):
    with app.app_context():
        credential = BitbucketCredentialProfile(
            name="injection-token",
            provider="bitbucket",
            credential_type="repository_access_token",
            secret_cipher=encrypt_secret("clone-token-value"),
            read_only=True,
            enabled=True,
        )
        db.session.add(credential)
        db.session.commit()
        credential_id = credential.id

    created = client.post(
        "/api/ci/services",
        json={"name": "Injection App", "applicationType": "node"},
        headers=auth_headers(admin_token),
    ).get_json()["data"]["id"]
    client.put(
        f"/api/ci/services/{created}/source",
        json={
            "repositoryUrl": "https://bitbucket.org/areeba/injection-app",
            "defaultBranch": "master",
            "credentialProfileId": credential_id,
        },
        headers=auth_headers(admin_token),
    )
    return created


def _trigger(client, token, service_id, variables):
    return client.post(
        f"/api/ci/services/{service_id}/builds",
        json={"branch": "master", "variables": variables},
        headers=auth_headers(token),
    )


def _build_count(app):
    with app.app_context():
        return CiBuild.query.count()


@pytest.mark.parametrize(
    "variables",
    [
        {"DOCKERFILE_PATH": MALICIOUS_DOCKERFILE},
        {"DOCKERFILE_PATH": "/etc/passwd"},
        {"DOCKERFILE_PATH": "../../outside/Dockerfile"},
        {"DOCKERFILE_PATH": "docker/$(id)/Dockerfile"},
        {"IMAGE_TAG": MALICIOUS_TAG},
        {"IMAGE_TAG": "${REGISTRY_PASSWORD}"},
        {"IMAGE_TAG": "-leading-dash"},
        {"IMAGE_NAME": "app;id"},
        {"PATH": "/workspace/source/bin"},
        {"LD_PRELOAD": "/workspace/source/evil.so"},
        {"not a name": "x"},
    ],
)
def test_a_hostile_trigger_value_is_refused_before_anything_is_queued(
    app, client, admin_token, service_id, variables
):
    before = _build_count(app)
    response = _trigger(client, admin_token, service_id, variables)
    assert response.status_code == 400, response.get_json()
    assert _build_count(app) == before


def test_a_declared_parameter_with_a_reserved_name_is_checked_too(app, admin_token):
    """Declaring DOCKERFILE_PATH as a text parameter must not reopen the hole."""
    from api.models_ci import CiPipeline, CiService
    from api.services.ci import pipelines as pipelines_service
    from api.services.ci.pipelines import PipelineError

    with app.app_context():
        service = CiService(name="Declared Svc", slug="declared-svc")
        db.session.add(service)
        db.session.commit()
        pipeline = CiPipeline(
            service_id=service.id,
            name="default",
            parameters=[{"name": "DOCKERFILE_PATH", "type": "text", "default": "Dockerfile"}],
        )
        db.session.add(pipeline)
        db.session.commit()

        with pytest.raises(PipelineError):
            pipelines_service.validate_parameter_values(
                pipeline, {"DOCKERFILE_PATH": MALICIOUS_DOCKERFILE}
            )
        assert pipelines_service.validate_parameter_values(
            pipeline, {"DOCKERFILE_PATH": "docker/app.Dockerfile"}
        ) == {"DOCKERFILE_PATH": "docker/app.Dockerfile"}


def test_legitimate_trigger_values_still_queue_a_build(app, client, admin_token, service_id):
    response = _trigger(
        client,
        admin_token,
        service_id,
        {
            "DOCKERFILE_PATH": "docker/app.Dockerfile",
            "IMAGE_TAG": "v1.2.3",
            "IMAGE_NAME": "team/payment-service",
            "TICKET_TAG": "REL-4412",
        },
    )
    assert response.status_code == 201, response.get_json()
    with app.app_context():
        build = db.session.get(CiBuild, response.get_json()["data"]["id"])
        variables = build.pipeline_snapshot["variables"]
    assert variables["DOCKERFILE_PATH"] == "docker/app.Dockerfile"
    assert variables["IMAGE_TAG"] == "v1.2.3"


# ---------------------------------------------------------------------------
# Pipeline save
# ---------------------------------------------------------------------------

def _pipeline_id(client, admin_token, service_id):
    return client.get(
        f"/api/ci/services/{service_id}/pipelines", headers=auth_headers(admin_token)
    ).get_json()["data"]["items"][0]["id"]


@pytest.mark.parametrize(
    "stage_extra",
    [
        {"env": {"DOCKERFILE_PATH": MALICIOUS_DOCKERFILE}},
        {"env": {"DOCKERFILE_PATH": "../Dockerfile"}},
        {"workingDirectory": "../../etc"},
        {"workingDirectory": "/abs/path"},
    ],
)
def test_pipeline_save_refuses_a_hostile_image_stage(client, admin_token, service_id, stage_extra):
    response = client.put(
        f"/api/ci/pipelines/{_pipeline_id(client, admin_token, service_id)}",
        json={
            "stages": [
                {"name": "Checkout", "stageType": "checkout", "runnerLabels": ["mock"]},
                {"name": "Image", "stageType": "container_image", **stage_extra},
            ]
        },
        headers=auth_headers(admin_token),
    )
    assert response.status_code == 400


def test_pipeline_save_accepts_a_nested_dockerfile(client, admin_token, service_id):
    response = client.put(
        f"/api/ci/pipelines/{_pipeline_id(client, admin_token, service_id)}",
        json={
            "stages": [
                {"name": "Checkout", "stageType": "checkout", "runnerLabels": ["mock"]},
                {
                    "name": "Image",
                    "stageType": "container_image",
                    "workingDirectory": "services/api",
                    "env": {"DOCKERFILE_PATH": "docker/app.Dockerfile"},
                },
            ]
        },
        headers=auth_headers(admin_token),
    )
    assert response.status_code == 200, response.get_json()


# ---------------------------------------------------------------------------
# Runner: re-check and quote
# ---------------------------------------------------------------------------

def _registry(**overrides):
    registry = {
        "host": "nexus.company.local:8443", "port": 8443, "repository": "payment-service",
        "tag": "v1.2.3", "tagIsTemplate": False, "dockerfile": "Dockerfile",
        "username": "ci", "password": "reg-pass", "verifyTls": True, "connectionId": 1,
    }
    registry.update(overrides)
    return registry


def _execution(registry, **kw):
    return StageExecution(
        build_id=7, build_number=3, stage_id=101, service_slug="payment-service",
        stage_name="Image", stage_type="container_image", image=None,
        working_directory=kw.get("workdir"), commands=[], env={}, secrets={},
        artifacts=[], resources={}, host_aliases=kw.get("host_aliases", []),
        timeout_seconds=600, continue_on_failure=False, position=1,
        workspace_ref="payment-service-3",
        repository_url="https://bitbucket.org/areeba/payment-service.git",
        branch="develop", registry=registry, image_scan=kw.get("image_scan"),
        callback_url="http://backend:5000/api/ci/worker", callback_token="tok",
    )


@pytest.fixture()
def _buildkit(monkeypatch):
    monkeypatch.setenv("CI_BUILDKIT_ADDR", "tcp://buildkitd:1234")


@pytest.mark.parametrize(
    "overrides",
    [
        {"dockerfile": MALICIOUS_DOCKERFILE},
        {"dockerfile": "../Dockerfile"},
        {"tag": MALICIOUS_TAG},
        {"tag": "v$(id)", "tagIsTemplate": True},
        {"repository": "repo;id"},
        {"host": "nexus;id"},
    ],
)
@pytest.mark.parametrize("scanned", [False, True])
def test_runner_refuses_a_hostile_registry_value(_buildkit, overrides, scanned):
    scan = {"enabled": True, "threshold": "critical", "onFail": "block"} if scanned else None
    with pytest.raises(RunnerError):
        k8s.image_stage_script(_execution(_registry(**overrides), image_scan=scan), "/m.json")


def test_runner_quotes_values_that_need_it(_buildkit):
    """A value that passes the grammar but is not shell-safe still arrives as one word."""
    hostile_dir = "svc dir;id"
    script = k8s.image_stage_script(
        _execution(
            _registry(dockerfile="docker/app.Dockerfile"),
            workdir=hostile_dir,
        ),
        "/workspace/.kubesight/image-meta-1.json",
    )
    context = f"/workspace/source/{hostile_dir}"
    assert f"--local {shlex.quote('context=' + context)} " in script
    assert f"--local {shlex.quote('dockerfile=' + context + '/docker')} " in script
    assert "--opt filename=app.Dockerfile " in script
    # Every ';' in the buildctl line sits inside a quoted word: the shell sees
    # one command, and the hostile directory arrives as single argv words.
    line = next(l for l in script.splitlines() if l.startswith("buildctl "))
    unquoted = line
    for word in (shlex.quote("context=" + context), shlex.quote("dockerfile=" + context + "/docker")):
        unquoted = unquoted.replace(word, "")
    assert ";" not in unquoted
    words = shlex.split(line)
    assert f"context={context}" in words
    assert f"dockerfile={context}/docker" in words


def test_command_stage_quotes_its_working_directory():
    execution = _execution(None, workdir="a b;id")
    execution.stage_type = "command"
    execution.commands = ["echo hi"]
    script = k8s._command_stage_script(execution)
    assert f"cd {shlex.quote('/workspace/source/a b;id')}\n" in script


def test_legitimate_values_produce_the_script_they_always_did(_buildkit):
    script = k8s.image_stage_script(
        _execution(_registry(dockerfile="docker/app.Dockerfile"), workdir="services/api"),
        "/workspace/.kubesight/image-meta-1.json",
    )
    assert "--local context=/workspace/source/services/api " in script
    assert "--local dockerfile=/workspace/source/services/api/docker " in script
    assert "--opt filename=app.Dockerfile " in script
    assert "name=nexus.company.local:8443/payment-service:v1.2.3,push=true" in script
    assert "'" not in script.split("buildctl ", 1)[1]


def test_scanned_script_quotes_and_uses_one_image_ref(_buildkit):
    script = k8s.image_stage_script(
        _execution(
            _registry(tag="${APP_VERSION}-3", tagIsTemplate=True),
            image_scan={"enabled": True, "threshold": "critical", "onFail": "block"},
        ),
        "/workspace/.kubesight/image-meta-1.json",
    )
    assert "KS_IMAGE_REF=nexus.company.local:8443/payment-service:$KS_TAG\n" in script
    assert "crane push /workspace/.kubesight/image-1.tar nexus.company.local:8443/payment-service:$KS_TAG" in script
    assert 'echo "[kubesight] == push == $KS_IMAGE_REF"' in script


def test_host_alias_option_is_one_word(_buildkit):
    script = k8s.image_stage_script(
        _execution(
            _registry(),
            host_aliases=[{"ip": "10.0.0.1", "hostnames": ["nexus", "nexus.local"]}],
        ),
        "/m.json",
    )
    assert "--opt add-hosts=nexus=10.0.0.1,nexus.local=10.0.0.1 " in script


# ---------------------------------------------------------------------------
# Grammar
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("value", ["Dockerfile", "docker/app.Dockerfile", "a/b-c_d/Dockerfile.prod"])
def test_dockerfile_grammar_accepts(value):
    assert build_inputs.dockerfile_path_problem(value) is None


@pytest.mark.parametrize("value", ["", "/Dockerfile", "../x", "a/../b", "a b", "a;b", "a/", "x$y"])
def test_dockerfile_grammar_rejects(value):
    assert build_inputs.dockerfile_path_problem(value) is not None


@pytest.mark.parametrize("value", ["v1.2.3", "V1.0.27-prod", "_x", "latest"])
def test_tag_grammar_accepts(value):
    assert build_inputs.image_tag_problem(value) is None


@pytest.mark.parametrize("value", ["", ".x", "-x", "a/b", "a b", "x" * 129, "${X}"])
def test_tag_grammar_rejects(value):
    assert build_inputs.image_tag_problem(value) is not None


def test_host_grammar():
    assert build_inputs.registry_host_problem("nexus.company.local:8443") is None
    assert build_inputs.registry_host_problem("10.0.0.1:5000") is None
    assert build_inputs.registry_host_problem("[::1]:5000") is None
    assert build_inputs.registry_host_problem("nexus;id") is not None
    assert build_inputs.registry_host_problem("user:pw@nexus") is not None
