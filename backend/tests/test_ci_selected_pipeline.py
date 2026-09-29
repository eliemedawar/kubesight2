"""Build readiness must describe the pipeline actually selected for the run."""

import pytest

from api.db import db
from api.models_ci import CiBuild, CiPipeline, CiService
from api.services.ci import engine, pipelines
from tests.test_ci_merge_checks import (
    _enable,
    _post_hook,
    _pull_request_body,
    _secret,
    captured_verdicts,
    service_id,
)


def test_custom_service_merge_check_runs_without_a_default_build_command(
    app, client, admin_token, service_id, captured_verdicts
):
    service = db.session.get(CiService, service_id)
    service.application_type = "generic"
    for pipeline in list(service.pipelines):
        db.session.delete(pipeline)
    db.session.commit()
    configured = _enable(client, admin_token, service_id, tools=["semgrep"])
    assert configured.status_code == 200
    check_pipeline_id = configured.get_json()["data"]["pipelineId"]
    secret = _secret(client, admin_token, service_id)

    response = _post_hook(client, service.slug, secret, _pull_request_body())
    assert response.status_code == 200
    data = response.get_json()["data"]
    assert data["state"] == "running", data
    build = CiBuild.query.one()
    assert build.pipeline_id == check_pipeline_id
    assert [stage.stage_type for stage in build.stages] == ["checkout", "command"]
    assert service.default_pipeline() is None
    # A successful PR check must not make the unconfigured normal build runnable.
    with pytest.raises(engine.BuildError, match="Custom services need"):
        engine.trigger_build(service)


@pytest.mark.parametrize("blocker", ["paused", "source", "disabled", "foreign", "empty_commands"])
def test_selected_pipeline_keeps_service_and_pipeline_guards(
    app, client, admin_token, service_id, blocker
):
    service = db.session.get(CiService, service_id)
    service.application_type = "generic"
    normal = pipelines.create_pipeline(service, {
        "name": "Normal build", "isDefault": True,
        "stages": [{"name": "Build", "stageType": "command", "commands": ["echo build"]}],
    })
    selected = pipelines.create_pipeline(service, {
        "name": "Selected checks", "isDefault": False,
        "stages": [{"name": "Check", "stageType": "command", "commands": ["echo check"]}],
    })
    row = db.session.get(CiPipeline, selected["id"])
    if blocker == "paused":
        service.status = "paused"
    elif blocker == "source":
        service.repository_url = None
    elif blocker == "disabled":
        row.enabled = False
    elif blocker == "foreign":
        other = CiService(name="Other service", slug="other-service")
        db.session.add(other)
        db.session.flush()
        row.service_id = other.id
    else:
        row.stages[0].commands = []
    db.session.commit()
    assert service.default_pipeline().id == normal["id"]
    with pytest.raises((engine.BuildError, pipelines.PipelineError)):
        engine.trigger_build(service, pipeline_id=row.id)
    assert CiBuild.query.count() == 0
