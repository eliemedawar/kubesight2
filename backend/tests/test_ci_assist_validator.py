"""What KubeSight will and will not accept from a pipeline it did not write.

This is the security boundary of assisted configuration, so these tests are
written as the attacks and mistakes they exist to stop rather than as coverage
of a function. Nothing here involves Hermes: the validator takes a dict, which
is exactly what makes it testable and exactly what makes it trustworthy.
"""

from __future__ import annotations

import pytest

from api.db import db
from api.models_ci import CiRunner, CiSecret, CiService
from api.secret_encryption import encrypt_secret
from api.services.ci import generated


@pytest.fixture()
def service(app):
    with app.app_context():
        row = CiService(
            name="Payment Service",
            slug="payment-service",
            application_type="java_gradle",
            repository_provider="bitbucket",
            repository_url="https://bitbucket.org/acme/payment-service.git",
            repository_workspace="acme",
            repository_name="payment-service",
            default_branch="main",
        )
        db.session.add(row)
        db.session.commit()
        yield row


def stage(**overrides):
    return {
        "name": "Build",
        "stageType": "command",
        "runnerLabels": ["linux", "java"],
        "commands": ["./gradlew --no-daemon clean build"],
        "timeoutSeconds": 1800,
        **overrides,
    }


def pipeline(*stages, **overrides):
    return {
        "name": "default",
        "description": "",
        "parameters": [],
        "stages": list(stages) or [stage()],
        **overrides,
    }


def codes(verdict):
    return {item["code"] for item in verdict["errors"]}


def warning_codes(verdict):
    return {item["code"] for item in verdict["warnings"]}


# ---------------------------------------------------------------------------
# The happy path
# ---------------------------------------------------------------------------

def test_a_reasonable_pipeline_is_accepted_and_comes_back_ready_to_save(service):
    verdict = generated.validate(
        service,
        pipeline(
            {"name": "Checkout", "stageType": "checkout", "runnerLabels": ["linux"],
             "commands": [], "timeoutSeconds": 600},
            stage(buildEnvironment="java-jdk11",
                  artifacts=[{"path": "build/libs/*.jar", "type": "jar"}]),
        ),
    )
    assert verdict["valid"], verdict["errors"]
    assert verdict["pipeline"]["stages"][1]["image"]  # resolved from the catalog
    # What comes back is the editor's own payload, so accepting it is an
    # ordinary save rather than a translation.
    assert verdict["pipeline"]["stages"][0]["stageType"] == "checkout"


def test_a_pipeline_with_no_stages_would_build_nothing(service):
    verdict = generated.validate(service, {"stages": []})
    assert not verdict["valid"]
    assert "no_stages" in codes(verdict)


# ---------------------------------------------------------------------------
# Privilege — the fields that do not exist, and must not be inventable
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "smuggled",
    [
        {"privileged": True},
        {"hostPath": "/"},
        {"securityContext": {"runAsUser": 0}},
        {"serviceAccount": "cluster-admin"},
        {"nodeSelector": {"kubernetes.io/hostname": "node-1"}},
        {"volumes": [{"hostPath": {"path": "/var/run/docker.sock"}}]},
    ],
)
def test_a_field_kubesight_has_no_place_for_is_refused_not_dropped(service, smuggled):
    """The stage model has no field for any of these, so silently ignoring them
    would work — and would mean a proposal asking for root could be approved by
    somebody reading a diff that did not show it. Refusing says it out loud."""
    verdict = generated.validate(service, pipeline(stage(**smuggled)))
    assert not verdict["valid"]
    assert "unknown_field" in codes(verdict)


def test_a_dependency_graph_is_refused_because_there_is_no_executor_for_one(service):
    """KubeSight runs stages in order. Accepting dependsOn would produce a
    pipeline whose author believed in a guarantee nothing provides."""
    verdict = generated.validate(service, pipeline(stage(dependsOn=["Checkout"])))
    assert "unknown_field" in codes(verdict)


# ---------------------------------------------------------------------------
# Images
# ---------------------------------------------------------------------------

def test_a_generated_pipeline_may_not_name_its_own_image(service):
    """An image name is the field where 'plausible' and 'correct' look
    identical. A cluster with no route to Docker Hub finds out at run time."""
    verdict = generated.validate(service, pipeline(stage(image="gradle:8-jdk21")))
    assert not verdict["valid"]
    assert "image_not_permitted" in codes(verdict)


def test_an_operator_can_allow_specific_image_prefixes(service, monkeypatch):
    monkeypatch.setenv("CI_ALLOWED_IMAGE_PREFIXES", "registry.areeba.com/")
    verdict = generated.validate(
        service, pipeline(stage(image="registry.areeba.com/custom/tool:1.2"))
    )
    assert verdict["valid"], verdict["errors"]


def test_an_environment_key_resolves_to_the_approved_image_and_its_labels(service):
    verdict = generated.validate(service, pipeline(stage(buildEnvironment="node-22",
                                                        runnerLabels=["linux"])))
    assert verdict["valid"], verdict["errors"]
    built = verdict["pipeline"]["stages"][0]
    assert built["image"] == "node:22-alpine"
    assert "node" in built["runnerLabels"]


def test_an_environment_key_nobody_has_heard_of_is_refused_with_the_list(service):
    verdict = generated.validate(service, pipeline(stage(buildEnvironment="java-jdk42")))
    assert not verdict["valid"]
    assert "unknown_build_environment" in codes(verdict)
    assert "java-jdk11" in verdict["errors"][0]["message"]


def test_an_unconfigured_environment_warns_rather_than_blocks(service):
    """Android has no mirrored image on this installation. That is a fact to
    tell somebody before they rely on the pipeline, not a reason to refuse to
    write one — the stage still runs on whatever the runner provides."""
    verdict = generated.validate(
        service,
        pipeline(stage(buildEnvironment="android", runnerLabels=["linux", "android"])),
    )
    assert verdict["valid"], verdict["errors"]
    assert "build_environment_unconfigured" in warning_codes(verdict)


# ---------------------------------------------------------------------------
# Runners
# ---------------------------------------------------------------------------

def test_a_capability_no_runner_advertises_is_refused_before_it_can_queue_forever(service):
    """This is the failure the whole check exists for: a stage labelled java11
    against a fleet advertising java17 is accepted by every structural rule and
    then waits forever with 'No online runner provides: java11'."""
    verdict = generated.validate(service, pipeline(stage(runnerLabels=["linux", "java11"])))
    assert not verdict["valid"]
    assert "unsatisfiable_runner_labels" in codes(verdict)
    assert "java11" in verdict["errors"][0]["message"]


def test_a_registered_but_offline_runner_is_a_warning_not_a_refusal(app, service):
    """A Mac agent asleep at 2am must not stop somebody designing an iOS
    pipeline. The build will queue, and saying so is the right amount."""
    with app.app_context():
        db.session.add(
            CiRunner(
                name="mac-mini-1",
                runner_type="agent_macos",
                status="offline",
                enabled=True,
                capabilities=["macos", "xcode"],
            )
        )
        db.session.commit()
        verdict = generated.validate(
            db.session.get(CiService, service.id),
            pipeline(stage(runnerLabels=["macos", "xcode"], runnerType="agent_macos")),
        )
    assert verdict["valid"], verdict["errors"]
    assert "runner_offline" in warning_codes(verdict)


def test_a_runner_type_with_no_executor_is_refused(service):
    verdict = generated.validate(service, pipeline(stage(runnerType="ssh_linux")))
    assert not verdict["valid"]
    assert "runner_type_unavailable" in codes(verdict)


# ---------------------------------------------------------------------------
# Secrets
# ---------------------------------------------------------------------------

def test_a_literal_credential_in_a_command_is_refused(service):
    verdict = generated.validate(
        service,
        pipeline(stage(commands=["./gradlew build -PnexusPassword=hunter2-actual-value"])),
    )
    assert not verdict["valid"]
    assert "embedded_secret" in codes(verdict)


def test_a_credential_inside_a_url_is_refused(service):
    verdict = generated.validate(
        service,
        pipeline(stage(commands=["curl https://deploy:s3cr3t@nexus.areeba.com/repo"])),
    )
    assert not verdict["valid"]
    assert "embedded_secret" in codes(verdict)


def test_a_literal_credential_in_the_environment_is_refused(service):
    verdict = generated.validate(
        service, pipeline(stage(env={"NEXUS_PASSWORD": "hunter2-actual-value"}))
    )
    assert not verdict["valid"]
    assert "embedded_secret" in codes(verdict)


def test_wiring_an_injected_secret_into_a_tool_is_the_correct_thing_and_is_allowed(app, service):
    """`NEXUS_PASSWORD=$NEXUS_PASSWORD` is a stage using a secret properly. A
    check that cannot tell that from a leak would forbid the right answer."""
    with app.app_context():
        db.session.add(
            CiSecret(
                scope="service",
                service_id=service.id,
                key="NEXUS_PASSWORD",
                value_cipher=encrypt_secret("x"),
            )
        )
        db.session.commit()
        verdict = generated.validate(
            db.session.get(CiService, service.id),
            pipeline(
                stage(
                    commands=['./gradlew build -PnexusPassword="$NEXUS_PASSWORD"'],
                    env={"NEXUS_PASSWORD": "${NEXUS_PASSWORD}"},
                    secretRefs=[{"name": "NEXUS_PASSWORD", "envVar": "NEXUS_PASSWORD"}],
                )
            ),
        )
    assert verdict["valid"], verdict["errors"]


def test_a_reference_to_a_secret_nobody_has_and_nobody_asked_for_is_refused(service):
    verdict = generated.validate(
        service, pipeline(stage(secretRefs=[{"name": "MYSTERY_TOKEN"}]))
    )
    assert not verdict["valid"]
    assert {"invalid_stage", "undeclared_secret_ref"} & codes(verdict)


def test_a_secret_the_proposal_is_about_to_ask_for_is_accepted(service):
    """The flow is propose, collect, save. A stage may reference a secret that
    does not exist yet precisely because the proposal asks the user for it in
    the same breath — refusing that would make the flow impossible."""
    verdict = generated.validate(
        service,
        pipeline(stage(secretRefs=[{"name": "NEXUS_PASSWORD"}])),
        declared_inputs=[{"name": "NEXUS_PASSWORD", "kind": "secret", "required": True}],
    )
    assert verdict["valid"], verdict["errors"]


# ---------------------------------------------------------------------------
# Paths and forbidden commands
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("path", ["/etc", "../../../etc", "/workspace/source"])
def test_an_absolute_or_escaping_working_directory_is_refused(service, path):
    verdict = generated.validate(service, pipeline(stage(workingDirectory=path)))
    assert not verdict["valid"]
    assert {"path_escape", "portability_absolute_workspace"} & codes(verdict)


def test_a_workspace_variable_is_the_supported_way_to_name_a_directory(service):
    verdict = generated.validate(
        service, pipeline(stage(workingDirectory="$KUBESIGHT_SOURCE"))
    )
    assert verdict["valid"], verdict["errors"]


def test_an_artifact_path_may_not_step_outside_the_workspace(service):
    verdict = generated.validate(
        service, pipeline(stage(artifacts=[{"path": "../../../etc/shadow", "type": "binary"}]))
    )
    assert not verdict["valid"]
    assert "path_escape" in codes(verdict)


def test_docker_build_is_refused_because_a_build_pod_has_no_socket(service):
    """And cannot be given one — that is what keeps repository-authored
    commands off the node."""
    verdict = generated.validate(
        service, pipeline(stage(commands=["docker build -t app ."]))
    )
    assert not verdict["valid"]
    assert "portability_docker_in_stage" in codes(verdict)


def test_installing_packages_is_refused_because_the_root_filesystem_is_read_only(service):
    verdict = generated.validate(
        service, pipeline(stage(commands=["apt-get install -y curl"]))
    )
    assert not verdict["valid"]
    assert "portability_install_in_stage" in codes(verdict)


# ---------------------------------------------------------------------------
# Structure — held to exactly the same standard as a hand-written pipeline
# ---------------------------------------------------------------------------

def test_a_generated_pipeline_is_held_to_the_same_rules_as_a_typed_one(service):
    """Structural validation is literally the function that guards a manual
    save, so a proposal can never be accepted on weaker terms than a person."""
    verdict = generated.validate(service, pipeline(stage(timeoutSeconds=99999999)))
    assert not verdict["valid"]
    assert "invalid_stage" in codes(verdict)


def test_a_command_stage_with_nothing_to_run_is_refused(service):
    verdict = generated.validate(service, pipeline(stage(commands=[])))
    assert not verdict["valid"]
    assert "invalid_stage" in codes(verdict)


def test_two_stages_with_the_same_name_are_refused(service):
    verdict = generated.validate(service, pipeline(stage(), stage()))
    assert not verdict["valid"]
    assert "duplicate_stage_name" in codes(verdict)


def test_a_stage_type_with_no_executor_yet_warns_rather_than_blocks(service):
    """scan and publish_artifact validate today and skip at run time with an
    explanation. That is a deliberate product decision, not a defect."""
    verdict = generated.validate(
        service,
        pipeline(stage(stageType="scan", commands=[])),
    )
    assert verdict["valid"], verdict["errors"]
    assert "stage_type_not_executable" in warning_codes(verdict)


def test_an_image_stage_without_a_registry_warns_about_where_it_would_push(service):
    verdict = generated.validate(
        service,
        pipeline(stage(stageType="container_image", commands=[], runnerType="kubernetes")),
    )
    assert verdict["valid"], verdict["errors"]
    assert "container_image_without_registry" in warning_codes(verdict)


# ---------------------------------------------------------------------------
# Feedback
# ---------------------------------------------------------------------------

def test_feedback_carries_codes_and_messages_and_nothing_else(service):
    """A repair prompt must not become a second, quieter channel out of
    KubeSight. It may say what was wrong; it may not carry anything that was
    not already shown to the user."""
    verdict = generated.validate(service, pipeline(stage(image="gradle:8-jdk21")))
    feedback = generated.error_feedback(verdict["errors"])
    assert feedback
    for item in feedback:
        assert set(item) == {"code", "stage", "field", "message"}
