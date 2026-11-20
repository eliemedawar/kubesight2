"""A service's own defaults for the build inputs of a shared pipeline.

What is locked here:

* the shared pipeline owns the questions; each service using it picks what
  they start as, and only its own Run Build dialog, builds, schedules and
  webhooks see that;
* a default is checked against the shared definitions when saved (unknown
  input, an option that is not offered) but read leniently at build time, so a
  shared pipeline changed underneath never stops a service from building;
* "same as shared" is not stored, so it keeps following the shared pipeline;
* stopping with "copy" keeps what the service built with; switching to another
  shared pipeline forgets defaults meant for the old one's inputs.
"""

from __future__ import annotations

from api.db import db
from api.models_ci import CiBuild, CiPipeline
from tests.conftest import auth_headers
from tests.test_ci_shared_pipelines import (  # noqa: F401 - fixture
    _attach,
    _cmd,
    _create_pipeline,
    _run,
    _service,
    credential_id,
)

VARIANT = {
    "name": "APP_VARIANT",
    "type": "choice",
    "label": "Application variant",
    "choices": ["issuing", "acquiring"],
    "default": "issuing",
    "required": True,
}
DEBUG = {"name": "DEBUG", "type": "boolean", "default": "false"}


def _shared_with_inputs(client, token, parameters=(VARIANT, DEBUG), name="Verto UI Shared"):
    home = _create_pipeline(client, token, name, stages=[_cmd("Build", "echo $APP_VARIANT")])
    saved = client.put(
        f"/api/ci/pipelines/{home.pipeline_id}",
        json={"parameters": list(parameters)},
        headers=auth_headers(token),
    )
    assert saved.status_code == 200, saved.get_json()
    return home


def _set_defaults(client, token, pipeline_id, defaults):
    return client.put(
        f"/api/ci/pipelines/{pipeline_id}",
        json={"parameterDefaults": defaults},
        headers=auth_headers(token),
    )


def _dialog(client, token, service_id):
    items = client.get(
        f"/api/ci/services/{service_id}/parameters", headers=auth_headers(token)
    ).get_json()["data"]["items"]
    return {item["name"]: item for item in items}


def _variables(app, build_id):
    with app.app_context():
        return db.session.get(CiBuild, build_id).pipeline_snapshot["variables"]


def test_each_service_starts_the_shared_inputs_at_its_own_defaults(app, client, admin_token, credential_id):
    issuing = _service(client, admin_token, credential_id, "Issuing UI")
    acquiring = _service(client, admin_token, credential_id, "Acquiring UI")
    home = _shared_with_inputs(client, admin_token)
    _attach(client, admin_token, issuing.id, home.id)
    _attach(client, admin_token, acquiring.id, home.id)

    response = _set_defaults(
        client, admin_token, acquiring.pipeline_id, {"APP_VARIANT": "acquiring", "DEBUG": True}
    )
    assert response.status_code == 200, response.get_json()
    data = response.get_json()["data"]
    # Answered as the editor shows a linked pipeline: shared stages, shared
    # definitions untouched, this service's defaults beside them.
    assert data["linkedPipeline"]["id"] == home.id
    assert [s["name"] for s in data["stages"]] == ["Build"]
    assert data["parameterDefaults"] == {"APP_VARIANT": "acquiring", "DEBUG": "true"}
    assert {p["name"]: p["default"] for p in data["parameters"]} == {"APP_VARIANT": "issuing", "DEBUG": "false"}

    assert _dialog(client, admin_token, acquiring.id)["APP_VARIANT"]["default"] == "acquiring"
    assert _dialog(client, admin_token, acquiring.id)["DEBUG"]["default"] == "true"
    assert _dialog(client, admin_token, issuing.id)["APP_VARIANT"]["default"] == "issuing"

    built = _run(app, client, admin_token, acquiring.id)
    assert built["status"] == "success", built
    assert _variables(app, built["id"]) == {"APP_VARIANT": "acquiring", "DEBUG": "true"}
    other = _run(app, client, admin_token, issuing.id)
    assert _variables(app, other["id"]) == {"APP_VARIANT": "issuing", "DEBUG": "false"}

    # The shared pipeline itself is unchanged.
    with app.app_context():
        shared = db.session.get(CiPipeline, home.pipeline_id)
        assert {p["name"]: p["default"] for p in shared.parameters} == {"APP_VARIANT": "issuing", "DEBUG": "false"}


def test_an_answer_given_at_run_time_still_wins(app, client, admin_token, credential_id):
    service = _service(client, admin_token, credential_id)
    home = _shared_with_inputs(client, admin_token)
    _attach(client, admin_token, service.id, home.id)
    _set_defaults(client, admin_token, service.pipeline_id, {"APP_VARIANT": "acquiring"})
    response = client.post(
        f"/api/ci/services/{service.id}/builds",
        json={"variables": {"APP_VARIANT": "issuing"}},
        headers=auth_headers(admin_token),
    )
    assert response.status_code == 201, response.get_json()
    assert _variables(app, response.get_json()["data"]["id"])["APP_VARIANT"] == "issuing"


def test_defaults_are_checked_against_the_shared_inputs(client, admin_token, credential_id):
    service = _service(client, admin_token, credential_id)
    home = _shared_with_inputs(client, admin_token)
    _attach(client, admin_token, service.id, home.id)

    unknown = _set_defaults(client, admin_token, service.pipeline_id, {"NOPE": "x"})
    assert unknown.status_code == 400
    assert "no build input named 'NOPE'" in unknown.get_json()["error"]

    not_offered = _set_defaults(client, admin_token, service.pipeline_id, {"APP_VARIANT": "prod"})
    assert not_offered.status_code == 400
    assert "issuing, acquiring" in not_offered.get_json()["error"]

    # Same as shared is not stored, so it keeps following the shared pipeline.
    same = _set_defaults(client, admin_token, service.pipeline_id, {"APP_VARIANT": "issuing", "DEBUG": "false"})
    assert same.status_code == 200
    assert same.get_json()["data"]["parameterDefaults"] == {}


def test_a_service_on_its_own_pipeline_has_no_shared_defaults(client, admin_token, credential_id):
    service = _service(client, admin_token, credential_id)
    client.put(
        f"/api/ci/pipelines/{service.pipeline_id}",
        json={"stages": [_cmd("Own", "make")], "parameters": [VARIANT]},
        headers=auth_headers(admin_token),
    )
    response = _set_defaults(client, admin_token, service.pipeline_id, {"APP_VARIANT": "acquiring"})
    assert response.status_code == 400
    assert "shared pipeline" in response.get_json()["error"]


def test_a_default_the_shared_pipeline_no_longer_offers_falls_back(app, client, admin_token, credential_id):
    service = _service(client, admin_token, credential_id)
    home = _shared_with_inputs(client, admin_token)
    _attach(client, admin_token, service.id, home.id)
    _set_defaults(client, admin_token, service.pipeline_id, {"APP_VARIANT": "acquiring"})

    # The shared pipeline drops the option this service started at.
    client.put(
        f"/api/ci/pipelines/{home.pipeline_id}",
        json={"parameters": [{**VARIANT, "choices": ["issuing", "switching"]}]},
        headers=auth_headers(admin_token),
    )
    assert _dialog(client, admin_token, service.id)["APP_VARIANT"]["default"] == "issuing"
    built = _run(app, client, admin_token, service.id)
    assert built["status"] == "success", built
    assert _variables(app, built["id"])["APP_VARIANT"] == "issuing"


def test_copying_the_stages_in_keeps_the_services_defaults(app, client, admin_token, credential_id):
    service = _service(client, admin_token, credential_id)
    home = _shared_with_inputs(client, admin_token)
    _attach(client, admin_token, service.id, home.id)
    _set_defaults(client, admin_token, service.pipeline_id, {"APP_VARIANT": "acquiring"})

    data = client.post(
        f"/api/ci/services/{service.id}/shared-pipeline/detach",
        json={"mode": "copy"},
        headers=auth_headers(admin_token),
    ).get_json()["data"]
    params = {p["name"]: p for p in data["parameters"]}
    assert params["APP_VARIANT"]["default"] == "acquiring"
    assert "sharedDefault" not in params["APP_VARIANT"]
    with app.app_context():
        row = db.session.get(CiPipeline, service.pipeline_id)
        assert row.parameter_defaults is None


def test_switching_to_another_shared_pipeline_forgets_the_old_defaults(app, client, admin_token, credential_id):
    service = _service(client, admin_token, credential_id)
    first = _shared_with_inputs(client, admin_token, name="Verto UI Shared")
    second = _shared_with_inputs(client, admin_token, name="Verto UI Next")
    _attach(client, admin_token, service.id, first.id)
    _set_defaults(client, admin_token, service.pipeline_id, {"APP_VARIANT": "acquiring"})

    # Re-attaching the same one keeps them.
    _attach(client, admin_token, service.id, first.id)
    assert _dialog(client, admin_token, service.id)["APP_VARIANT"]["default"] == "acquiring"

    _attach(client, admin_token, service.id, second.id)
    assert _dialog(client, admin_token, service.id)["APP_VARIANT"]["default"] == "issuing"


def test_a_pipeline_file_carries_the_services_defaults(app, client, admin_token, credential_id):
    import yaml

    source = _service(client, admin_token, credential_id, "Acquiring UI")
    target = _service(client, admin_token, credential_id, "Acquiring UI Prod")
    home = _shared_with_inputs(client, admin_token)
    _attach(client, admin_token, source.id, home.id)
    _set_defaults(client, admin_token, source.pipeline_id, {"APP_VARIANT": "acquiring"})

    content = client.get(
        f"/api/ci/pipelines/{source.pipeline_id}/export", headers=auth_headers(admin_token)
    ).get_json()["data"]["content"]
    document = yaml.safe_load(content)["pipeline"]
    assert document["sharedPipeline"]["parameterDefaults"] == {"APP_VARIANT": "acquiring"}
    # Imported somewhere without that shared pipeline, the inputs start there.
    assert {p["name"]: p["default"] for p in document["parameters"]}["APP_VARIANT"] == "acquiring"

    response = client.post(
        f"/api/ci/pipelines/{target.pipeline_id}/import",
        json={"content": content},
        headers=auth_headers(admin_token),
    )
    assert response.status_code == 200, response.get_json()
    assert _dialog(client, admin_token, target.id)["APP_VARIANT"]["default"] == "acquiring"
