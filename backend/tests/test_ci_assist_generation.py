"""End to end, with Hermes faked and everything else real.

The model is the only thing stubbed here. Evidence is collected from a real
(in-memory) repository through the real source port, the response goes through
the real contract validator, the pipeline goes through the real KubeSight
validator, and the result is written to real rows. So these tests answer the
question that matters: given a plausible model response, does a working service
come out the other end — and given an implausible one, does nothing?
"""

from __future__ import annotations

import copy

import pytest

from api.db import db
from api.models_application_intelligence import BitbucketCredentialProfile
from api.models_ci import CiRepositoryAnalysis, CiRunner, CiSecret, CiService
from api.secret_encryption import decrypt_secret, encrypt_secret
from api.services.ci_assist import accept as accept_service
from api.services.ci_assist import analyses as analyses_service
from api.services.ci import generated
from api.services.ci_assist import generator, hermes, schema
from tests.fixtures import fake_source


@pytest.fixture()
def service(app):
    credential = BitbucketCredentialProfile(
        name="fake-read-only",
        provider=fake_source.PROVIDER,
        credential_type="repository_access_token",
        secret_cipher=encrypt_secret("token"),
        enabled=True,
    )
    db.session.add(credential)
    db.session.flush()
    row = CiService(
        name="Payment Service",
        slug="payment-service",
        application_type="generic",
        repository_provider=fake_source.PROVIDER,
        repository_url="https://fake.test/acme/payment-service.git",
        repository_workspace="acme",
        repository_name="payment-service",
        default_branch="main",
        credential_profile_id=credential.id,
    )
    db.session.add(row)
    db.session.commit()
    yield row


@pytest.fixture(autouse=True)
def hermes_available(monkeypatch):
    monkeypatch.setattr(hermes, "is_configured", lambda: True)
    monkeypatch.setattr(hermes, "configuration_hint", lambda: "")


class FakeHermes:
    """A scripted model. Each call returns the next queued response."""

    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = []

    def install(self, monkeypatch):
        monkeypatch.setattr(hermes, "propose", self._propose)
        monkeypatch.setattr(hermes, "repair", self._repair)
        return self

    def _next(self, kind, payload):
        self.calls.append({"kind": kind, **payload})
        if not self.responses:
            raise AssertionError(f"FakeHermes ran out of responses on a {kind} call")
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return schema.validate_response(copy.deepcopy(response)), "fake-model", "fake-v1"

    def _propose(
        self, *, evidence, capabilities, profile_hint=None, feedback=None, examples=None
    ):
        return self._next(
            "propose",
            {
                "evidence": evidence,
                "capabilities": capabilities,
                "hint": profile_hint,
                "feedback": feedback,
                "examples": examples,
            },
        )

    def _repair(self, *, evidence, capabilities, previous, errors, profile_hint=None):
        return self._next(
            "repair",
            {
                "evidence": evidence,
                "capabilities": capabilities,
                "previous": previous,
                "errors": errors,
                "hint": profile_hint,
            },
        )


def response(profile, stages, *, required_inputs=None, parameters=None, warnings=None):
    return {
        "schemaVersion": "1.0",
        "applicationProfile": profile,
        "pipeline": {
            "name": "default",
            "description": "Generated for tests.",
            "parameters": parameters or [],
            "stages": stages,
        },
        "requiredInputs": required_inputs or [],
        "analysis": {"warnings": warnings or [], "unknown": [], "notes": []},
    }


def checkout(labels=("linux",), runner_type=""):
    stage = {
        "name": "Checkout",
        "stageType": "checkout",
        "runnerLabels": list(labels),
        "commands": [],
        "timeoutSeconds": 600,
    }
    if runner_type:
        stage["runnerType"] = runner_type
    return stage


def run(service, monkeypatch, fake, files, **payload):
    """Request an analysis and run it inline, the way the worker would."""
    fake.install(monkeypatch)
    fake_source.FAKE.load(files)
    started = analyses_service.request_analysis(service, payload)
    generator.run(started["id"])
    row = db.session.get(CiRepositoryAnalysis, started["id"])
    db.session.refresh(row)
    return row


# ---------------------------------------------------------------------------
# The stacks
# ---------------------------------------------------------------------------

def test_java_gradle_spring_boot(service, monkeypatch):
    """The worked example from the brief, start to finish."""
    fake = FakeHermes(
        response(
            {
                "language": "java",
                "languageVersion": "17",
                "framework": "spring-boot",
                "frameworkVersion": "3.3.2",
                "buildSystem": "gradle",
                "buildSystemVersion": "8.7",
                "usesBuildWrapper": True,
                "packaging": "jar",
                "testsDetected": True,
                "artifactPaths": ["build/libs/*.jar"],
                "containerization": {"type": "dockerfile", "dockerfilePath": "Dockerfile"},
                "evidence": {
                    "languageVersion": {
                        "value": "17",
                        "confidence": "Confirmed",
                        "source": "build.gradle",
                        "detail": "toolchain { languageVersion = 17 }",
                    },
                    "buildSystemVersion": {
                        "value": "8.7",
                        "confidence": "Confirmed",
                        "source": "gradle/wrapper/gradle-wrapper.properties",
                    },
                },
            },
            [
                checkout(),
                {
                    "name": "Build",
                    "stageType": "command",
                    "buildEnvironment": "java-jdk11",
                    "runnerLabels": ["linux", "java"],
                    "commands": ["./gradlew --no-daemon clean build -x test"],
                    "timeoutSeconds": 2400,
                },
                {
                    "name": "Unit Tests",
                    "stageType": "command",
                    "buildEnvironment": "java-jdk11",
                    "runnerLabels": ["linux", "java"],
                    "commands": ["./gradlew --no-daemon test"],
                    "artifacts": [
                        {"path": "build/test-results/test/*.xml", "type": "test-report"}
                    ],
                    "timeoutSeconds": 2400,
                },
                {
                    "name": "Package",
                    "stageType": "command",
                    "buildEnvironment": "java-jdk11",
                    "runnerLabels": ["linux", "java"],
                    "commands": [
                        "jar=$(ls -1 build/libs/*.jar | grep -v -- '-plain[.]jar$' | head -1)",
                        'cp "$jar" app.jar',
                    ],
                    "artifacts": [{"path": "app.jar", "type": "jar"}],
                },
                {
                    "name": "Build Image",
                    "stageType": "container_image",
                    "runnerType": "kubernetes",
                    "runnerLabels": ["linux"],
                    "commands": [],
                },
            ],
            required_inputs=[
                {"name": "NEXUS_USERNAME", "kind": "secret", "required": True,
                 "reason": "build.gradle resolves from nexus.areeba.com"},
                {"name": "NEXUS_PASSWORD", "kind": "secret", "required": True},
            ],
        )
    )
    row = run(service, monkeypatch, fake, fake_source.JAVA_GRADLE)

    assert row.state == "analyzed"
    # Saved, not merely validated: the review step is deliberately skipped.
    assert row.pipeline_state == "accepted"
    assert row.validation["valid"] is True

    profile = row.application_profile
    assert profile["language"] == "java"
    assert profile["languageVersion"] == "17"
    assert profile["framework"] == "spring-boot"
    assert profile["buildSystemVersion"] == "8.7"
    assert profile["derivedApplicationType"] == "java_gradle"
    # Provenance survives to the row a person reads.
    assert profile["evidence"]["buildSystemVersion"]["source"] == (
        "gradle/wrapper/gradle-wrapper.properties"
    )

    names = [stage["name"] for stage in row.generated_pipeline["stages"]]
    assert names == ["Checkout", "Build", "Unit Tests", "Package", "Build Image"]
    # The image was resolved by KubeSight, never named by the model.
    assert row.generated_pipeline["stages"][1]["image"]
    assert {item["name"] for item in row.required_inputs} == {
        "NEXUS_USERNAME",
        "NEXUS_PASSWORD",
    }


def test_java_maven(service, monkeypatch):
    fake = FakeHermes(
        response(
            {
                "language": "java",
                "languageVersion": "21",
                "framework": "spring-boot",
                "frameworkVersion": "3.2.5",
                "buildSystem": "maven",
                "buildSystemVersion": "3.9.6",
                "usesBuildWrapper": True,
                "packaging": "jar",
                "testsDetected": True,
            },
            [
                checkout(),
                {
                    "name": "Build",
                    "stageType": "command",
                    "buildEnvironment": "maven-3.9-jdk21",
                    "runnerLabels": ["linux", "java"],
                    "commands": ["./mvnw -B clean package"],
                    "artifacts": [{"path": "target/*.jar", "type": "jar"}],
                    "timeoutSeconds": 2400,
                },
            ],
        )
    )
    row = run(service, monkeypatch, fake, fake_source.JAVA_MAVEN)
    assert row.state == "analyzed"
    assert row.application_profile["derivedApplicationType"] == "java_maven"


def test_node_npm_next(service, monkeypatch):
    fake = FakeHermes(
        response(
            {
                "language": "typescript",
                "languageVersion": "22",
                "framework": "next.js",
                "frameworkVersion": "14.2.3",
                "buildSystem": "npm",
                "packageManager": "npm",
                "packaging": "bundle",
                "testsDetected": True,
            },
            [
                checkout(),
                {
                    "name": "Install",
                    "stageType": "command",
                    "buildEnvironment": "node-22",
                    "runnerLabels": ["linux", "node"],
                    "commands": ["npm ci"],
                },
                {
                    "name": "Build",
                    "stageType": "command",
                    "buildEnvironment": "node-22",
                    "runnerLabels": ["linux", "node"],
                    "commands": ["npm run build"],
                    "artifacts": [{"path": ".next/**", "type": "zip"}],
                },
            ],
        )
    )
    row = run(service, monkeypatch, fake, fake_source.NODE_NPM)
    assert row.state == "analyzed"
    assert row.application_profile["derivedApplicationType"] == "node"
    assert row.generated_pipeline["stages"][1]["image"] == "node:22-alpine"


def test_python_poetry_fastapi(service, monkeypatch):
    fake = FakeHermes(
        response(
            {
                "language": "python",
                "languageVersion": "3.12",
                "framework": "fastapi",
                "frameworkVersion": "0.111.0",
                "buildSystem": "poetry",
                "packageManager": "poetry",
                "packaging": "wheel",
                "testsDetected": True,
                "testFramework": "pytest",
            },
            [
                checkout(),
                {
                    "name": "Install",
                    "stageType": "command",
                    "buildEnvironment": "python-3.12",
                    "runnerLabels": ["linux", "python"],
                    "commands": ["python -m pip install --no-cache-dir poetry", "poetry install"],
                },
                {
                    "name": "Unit Tests",
                    "stageType": "command",
                    "buildEnvironment": "python-3.12",
                    "runnerLabels": ["linux", "python"],
                    "commands": ["poetry run pytest -q"],
                },
            ],
        )
    )
    row = run(service, monkeypatch, fake, fake_source.PYTHON_POETRY)
    assert row.state == "analyzed"
    assert row.application_profile["derivedApplicationType"] == "python"


def test_android(service, monkeypatch):
    fake = FakeHermes(
        response(
            {
                "language": "kotlin",
                "languageVersion": "17",
                "buildSystem": "gradle",
                "buildSystemVersion": "8.6",
                "usesBuildWrapper": True,
                "packaging": "aab",
                "platformTargets": ["android"],
                "modules": ["app"],
            },
            [
                checkout(labels=["linux", "android"]),
                {
                    "name": "Build Android",
                    "stageType": "command",
                    "buildEnvironment": "android",
                    "runnerLabels": ["linux", "android"],
                    "commands": ["./gradlew --no-daemon assembleRelease bundleRelease"],
                    "artifacts": [
                        {"path": "app/build/outputs/bundle/release/*.aab", "type": "aab"}
                    ],
                    "timeoutSeconds": 3600,
                },
            ],
            required_inputs=[
                {"name": "ANDROID_KEYSTORE_PASSWORD", "kind": "secret", "required": True,
                 "reason": "A release build must be signed."}
            ],
        )
    )
    row = run(service, monkeypatch, fake, fake_source.ANDROID)
    assert row.state == "analyzed"
    assert row.application_profile["derivedApplicationType"] == "android"
    # The unconfigured Android image is a warning on the result, not a refusal.
    assert any(
        item["code"] == "build_environment_unconfigured"
        for item in row.validation["warnings"]
    )


def test_flutter(service, monkeypatch):
    fake = FakeHermes(
        response(
            {
                "language": "dart",
                "languageVersion": "3.4.0",
                "framework": "flutter",
                "frameworkVersion": "3.22.0",
                "buildSystem": "flutter",
                "packaging": "apk",
                "platformTargets": ["android", "ios"],
                "testsDetected": True,
            },
            [
                checkout(labels=["linux", "flutter"]),
                {
                    "name": "Build Flutter",
                    "stageType": "command",
                    "buildEnvironment": "flutter",
                    "runnerLabels": ["linux", "flutter"],
                    "commands": ["flutter pub get", "flutter build apk --release"],
                    "artifacts": [
                        {"path": "build/app/outputs/flutter-apk/*.apk", "type": "apk"}
                    ],
                    "timeoutSeconds": 3600,
                },
            ],
        )
    )
    row = run(service, monkeypatch, fake, fake_source.FLUTTER)
    assert row.state == "analyzed"
    assert row.application_profile["derivedApplicationType"] == "flutter"


def test_docker_only_project(service, monkeypatch):
    fake = FakeHermes(
        response(
            {
                "language": "other",
                "buildSystem": "docker",
                "packaging": "container-image",
                "containerization": {"type": "dockerfile", "dockerfilePath": "Dockerfile"},
            },
            [
                checkout(),
                {
                    "name": "Build Image",
                    "stageType": "container_image",
                    "runnerType": "kubernetes",
                    "runnerLabels": ["linux"],
                    "commands": [],
                    "env": {"DOCKERFILE_PATH": "Dockerfile"},
                },
            ],
        )
    )
    row = run(service, monkeypatch, fake, fake_source.DOCKER_ONLY)
    assert row.state == "analyzed"
    assert row.application_profile["derivedApplicationType"] == "container"


def test_a_project_nobody_can_classify_says_so_instead_of_guessing(service, monkeypatch):
    """'Custom, and here is why' is a useful answer. A confidently wrong
    classification is not."""
    fake = FakeHermes(
        response(
            {
                "language": "shell",
                "buildSystem": "none",
                "packaging": "none",
                "unknown": ["buildSystem", "packaging"],
            },
            [
                checkout(),
                {
                    "name": "Build",
                    "stageType": "command",
                    "runnerLabels": ["linux"],
                    "commands": ["./run.sh"],
                },
            ],
            warnings=["No build system was found; run.sh is the only executable entry point."],
        )
    )
    row = run(service, monkeypatch, fake, fake_source.UNKNOWN)
    assert row.state == "analyzed"
    assert row.application_profile["derivedApplicationType"] == "generic"
    assert row.application_profile["unknown"]
    assert row.warnings


# ---------------------------------------------------------------------------
# Bad responses
# ---------------------------------------------------------------------------

def test_a_response_that_is_not_the_contract_fails_the_analysis(service, monkeypatch):
    """Never parsed leniently into something workable. A response that does not
    match the contract is a bug to see, not a shape to accommodate."""
    fake = FakeHermes({"pipeline": {"stages": []}, "commentary": "here you go"})
    row = run(service, monkeypatch, fake, fake_source.JAVA_GRADLE)
    assert row.state == "failed"
    assert row.failure_stage == "Generating pipeline"
    assert row.generated_pipeline is None


def test_a_response_missing_a_required_field_fails(service, monkeypatch):
    incomplete = response({"language": "java"}, [checkout()])
    del incomplete["requiredInputs"]
    fake = FakeHermes(incomplete)
    row = run(service, monkeypatch, fake, fake_source.JAVA_GRADLE)
    assert row.state == "failed"


def test_a_word_kubesight_does_not_know_is_recorded_not_fatal(service, monkeypatch):
    """Losing a whole repository analysis because one field used an unfamiliar
    word is the mistake that made "executable jar" fail. The value is recorded
    as "other", said out loud, and everything else that was read is kept."""
    fake = FakeHermes(
        response(
            {"language": "kobol", "buildSystem": "gradle", "packaging": "jar"},
            [
                checkout(),
                {"name": "Build", "stageType": "command",
                 "buildEnvironment": "java-jdk11", "runnerLabels": ["linux", "java"],
                 "commands": ["./gradlew build"]},
            ],
        )
    )
    row = run(service, monkeypatch, fake, fake_source.JAVA_GRADLE)
    assert row.state == "analyzed"
    assert row.application_profile["language"] == "other"
    assert any("kobol" in note for note in row.warnings)


def test_hermes_being_unavailable_never_blocks_the_user(service, monkeypatch):
    """The service already exists and is configurable by hand. An unreachable
    model is a reason to offer manual configuration, not to lose the service."""
    from api.services.application_intelligence_hermes import HermesTransientError

    fake = FakeHermes(HermesTransientError("Hermes is unavailable or timed out."))
    row = run(service, monkeypatch, fake, fake_source.JAVA_GRADLE)
    assert row.state == "failed"
    assert "unavailable" in row.safe_error_message
    # The service survives untouched and is still configurable manually.
    survivor = db.session.get(CiService, service.id)
    assert survivor is not None
    assert survivor.status == "active"


def test_an_analysis_cannot_start_when_hermes_is_not_configured(service, monkeypatch):
    monkeypatch.setattr(hermes, "is_configured", lambda: False)
    monkeypatch.setattr(hermes, "configuration_hint", lambda: "HERMES_API_URL is unset.")
    with pytest.raises(analyses_service.AnalysisError) as exc:
        analyses_service.request_analysis(service, {})
    assert "HERMES_API_URL" in str(exc.value)


# ---------------------------------------------------------------------------
# The correction loop
# ---------------------------------------------------------------------------

def bad_then_good(bad_stage, good_stage, profile=None):
    profile = profile or {"language": "java", "buildSystem": "gradle", "packaging": "jar"}
    return FakeHermes(
        response(profile, [checkout(), bad_stage]),
        response(profile, [checkout(), good_stage]),
    )


def test_an_unavailable_runner_capability_is_sent_back_and_corrected(service, monkeypatch):
    """The exact bug the label check exists for, and the loop fixing it."""
    fake = bad_then_good(
        {
            "name": "Build",
            "stageType": "command",
            "runnerLabels": ["linux", "java11"],
            "commands": ["./gradlew build"],
        },
        {
            "name": "Build",
            "stageType": "command",
            "buildEnvironment": "java-jdk11",
            "runnerLabels": ["linux", "java"],
            "commands": ["./gradlew build"],
        },
    )
    row = run(service, monkeypatch, fake, fake_source.JAVA_GRADLE)

    assert row.state == "analyzed"
    # Saved, not merely validated: the review step is deliberately skipped.
    assert row.pipeline_state == "accepted"
    assert [call["kind"] for call in fake.calls] == ["propose", "repair"]
    # The repair turn carried the objection and nothing else.
    sent = fake.calls[1]["errors"]
    assert any(item["code"] == "unsatisfiable_runner_labels" for item in sent)
    for item in sent:
        assert set(item) == {"code", "stage", "field", "message"}
    assert [entry["kind"] for entry in row.attempts] == ["propose", "repair"]


def test_an_invalid_artifact_path_is_corrected(service, monkeypatch):
    fake = bad_then_good(
        {
            "name": "Build",
            "stageType": "command",
            "buildEnvironment": "java-jdk11",
            "runnerLabels": ["linux", "java"],
            "commands": ["./gradlew build"],
            "artifacts": [{"path": "/var/lib/output/app.jar", "type": "jar"}],
        },
        {
            "name": "Build",
            "stageType": "command",
            "buildEnvironment": "java-jdk11",
            "runnerLabels": ["linux", "java"],
            "commands": ["./gradlew build"],
            "artifacts": [{"path": "build/libs/*.jar", "type": "jar"}],
        },
    )
    row = run(service, monkeypatch, fake, fake_source.JAVA_GRADLE)
    assert row.state == "analyzed"
    assert any(item["code"] == "path_escape" for item in fake.calls[1]["errors"])


def test_an_embedded_credential_is_corrected(service, monkeypatch):
    fake = bad_then_good(
        {
            "name": "Build",
            "stageType": "command",
            "buildEnvironment": "java-jdk11",
            "runnerLabels": ["linux", "java"],
            "commands": ["./gradlew build -PnexusPassword=hunter2-actual-value"],
        },
        {
            "name": "Build",
            "stageType": "command",
            "buildEnvironment": "java-jdk11",
            "runnerLabels": ["linux", "java"],
            "commands": ['./gradlew build -PnexusPassword="$NEXUS_PASSWORD"'],
        },
    )
    row = run(service, monkeypatch, fake, fake_source.JAVA_GRADLE)
    assert row.state == "analyzed"
    assert any(item["code"] == "embedded_secret" for item in fake.calls[1]["errors"])
    # The literal never travelled back out in the feedback.
    assert not any(
        "hunter2-actual-value" in item["message"] for item in fake.calls[1]["errors"]
    )


def test_the_loop_stops_when_the_same_objection_comes_back(service, monkeypatch):
    """A model that repeated itself once will repeat itself again. Spending the
    last attempt to confirm that only makes the user wait longer for the answer
    they already have."""
    bad = {
        "name": "Build",
        "stageType": "command",
        "runnerLabels": ["linux", "java11"],
        "commands": ["./gradlew build"],
    }
    profile = {"language": "java", "buildSystem": "gradle", "packaging": "jar"}
    fake = FakeHermes(
        response(profile, [checkout(), bad]),
        response(profile, [checkout(), dict(bad)]),
        response(profile, [checkout(), dict(bad)]),
    )
    row = run(service, monkeypatch, fake, fake_source.JAVA_GRADLE)

    # One propose and one repair, then it stopped — not the full allowance.
    assert [call["kind"] for call in fake.calls] == ["propose", "repair"]
    assert row.attempts[-1]["note"] == "no change in errors"
    # And the pipeline is saved regardless, carrying the objection as a note.
    # KubeSight advises; it does not veto.
    assert row.state == "analyzed"
    assert row.pipeline_state == "accepted"
    assert any(
        item["code"] == "unsatisfiable_runner_labels"
        for item in row.validation["warnings"]
    )


def test_a_partial_result_keeps_the_profile_it_did_establish(service, monkeypatch):
    """'Java 17, Gradle 8.7' was read out of files. A pipeline KubeSight refused
    says nothing about whether that reading was right, and making the user
    re-establish it would be punishing them for the model's mistake."""
    bad = {
        "name": "Build",
        "stageType": "command",
        "runnerLabels": ["linux", "java11"],
        "commands": ["./gradlew build"],
    }
    profile = {
        "language": "java",
        "languageVersion": "17",
        "buildSystem": "gradle",
        "buildSystemVersion": "8.7",
        "packaging": "jar",
    }
    fake = FakeHermes(
        response(profile, [checkout(), bad]),
        response(profile, [checkout(), dict(bad)]),
    )
    row = run(service, monkeypatch, fake, fake_source.JAVA_GRADLE)

    assert row.state == "analyzed"
    assert row.application_profile["languageVersion"] == "17"
    assert row.application_profile["buildSystemVersion"] == "8.7"
    # The objections survive as notes rather than as a refusal.
    assert row.validation["warnings"]


def test_the_repair_budget_is_bounded(service, monkeypatch):
    """Errors that keep CHANGING must still not loop forever."""
    monkeypatch.setenv("CI_ASSIST_REPAIR_ATTEMPTS", "2")
    profile = {"language": "java", "buildSystem": "gradle", "packaging": "jar"}
    fake = FakeHermes(
        response(profile, [checkout(), {"name": "Build", "stageType": "command",
                                        "runnerLabels": ["linux", "java11"],
                                        "commands": ["./gradlew build"]}]),
        response(profile, [checkout(), {"name": "Build", "stageType": "command",
                                        "runnerLabels": ["linux", "java"],
                                        "commands": ["docker build -t app ."]}]),
        response(profile, [checkout(), {"name": "Build", "stageType": "command",
                                        "runnerLabels": ["linux", "java"],
                                        "commands": ["sudo apt-get install -y curl"]}]),
        response(profile, [checkout(), {"name": "Build", "stageType": "command",
                                        "runnerLabels": ["linux", "java"],
                                        "commands": ["./gradlew build"]}]),
    )
    row = run(service, monkeypatch, fake, fake_source.JAVA_GRADLE)

    assert len([call for call in fake.calls if call["kind"] == "repair"]) == 2
    assert len(fake.calls) == 3
    # Bounded, then saved with whatever the last round produced.
    assert row.state == "analyzed"


# ---------------------------------------------------------------------------
# What the model is told
# ---------------------------------------------------------------------------

def test_the_model_is_only_offered_capabilities_that_exist(service, monkeypatch):
    fake = FakeHermes(
        response({"language": "java", "buildSystem": "gradle"}, [checkout()])
    )
    run(service, monkeypatch, fake, fake_source.JAVA_GRADLE)
    capabilities = fake.calls[0]["capabilities"]

    # Runner labels come from the registered fleet, not from a list in a file.
    assert "java" in capabilities["runnerLabels"]
    assert "java11" not in capabilities["runnerLabels"]
    # Only stage types with an executor are offered: proposing one without
    # produces a green build that made nothing.
    assert capabilities["stageTypes"] == ["checkout", "command", "container_image"]
    assert "scan" in capabilities["stageTypesNotExecutable"]
    assert "ssh_linux" not in capabilities["runnerTypes"]
    assert any(item["key"] == "java-jdk11" for item in capabilities["buildEnvironments"])


def test_a_user_supplied_profile_is_presented_as_settled_not_suggested(
    app, service, monkeypatch
):
    fake = FakeHermes(
        response(
            {"language": "java", "languageVersion": "17", "buildSystem": "gradle"},
            [checkout(), {"name": "Build", "stageType": "command",
                          "buildEnvironment": "java-jdk11",
                          "runnerLabels": ["linux", "java"],
                          "commands": ["./gradlew build"]}],
        )
    )
    row = run(
        service,
        monkeypatch,
        fake,
        fake_source.JAVA_GRADLE,
        applicationProfile={
            "language": "java",
            "languageVersion": "21",
            "buildSystem": "gradle",
            "packaging": "jar",
        },
    )
    hint = fake.calls[0]["hint"]

    assert hint["languageVersion"] == "21"
    # The model answered 17; the user said 21, and the user wins.
    assert row.application_profile["languageVersion"] == "21"


def test_generating_from_a_profile_reads_no_repository_at_all(service, monkeypatch):
    """The manual path, and the only one available when the source host is
    unreachable."""
    fake = FakeHermes(
        response(
            {"language": "python", "buildSystem": "pip", "packaging": "wheel"},
            [checkout(), {"name": "Build", "stageType": "command",
                          "buildEnvironment": "python-3.12",
                          "runnerLabels": ["linux", "python"],
                          "commands": ["python -m pip wheel --no-deps -w dist ."]}],
        )
    )
    fake_source.FAKE.load({})
    fake_source.FAKE.fail_with = "The source host must not be contacted here."
    try:
        row = run(
            service,
            monkeypatch,
            fake,
            {},
            mode="profile",
            applicationProfile={
                "language": "python",
                "languageVersion": "3.12",
                "buildSystem": "pip",
                "packaging": "wheel",
            },
        )
    finally:
        fake_source.FAKE.fail_with = None

    assert row.state == "analyzed"
    assert row.mode == "profile"
    assert fake.calls[0]["evidence"]["files"] == []


# ---------------------------------------------------------------------------
# Accepting
# ---------------------------------------------------------------------------

def valid_java_response():
    return response(
        {
            "language": "java",
            "languageVersion": "17",
            "buildSystem": "gradle",
            "buildSystemVersion": "8.7",
            "packaging": "jar",
            "usesBuildWrapper": True,
        },
        [
            checkout(),
            {
                "name": "Build",
                "stageType": "command",
                "buildEnvironment": "java-jdk11",
                "runnerLabels": ["linux", "java"],
                "commands": ['./gradlew build -PnexusPassword="$NEXUS_PASSWORD"'],
                "secretRefs": [{"name": "NEXUS_PASSWORD", "envVar": "NEXUS_PASSWORD"}],
                "artifacts": [{"path": "build/libs/*.jar", "type": "jar"}],
            },
        ],
        required_inputs=[
            {"name": "NEXUS_PASSWORD", "kind": "secret", "required": True},
            {"name": "IMAGE_REPOSITORY", "kind": "parameter", "required": True,
             "label": "Image repository"},
        ],
    )


def test_accepting_writes_ordinary_kubesight_configuration(service, monkeypatch):
    """After this the service builds through the normal engine and never
    consults Hermes again. That is the whole point of the feature."""
    fake = FakeHermes(valid_java_response())
    row = run(service, monkeypatch, fake, fake_source.JAVA_GRADLE)
    assert row.state == "analyzed"

    accept_service.accept(
        row,
        {
            "inputs": {
                "NEXUS_PASSWORD": "hunter2",
                "IMAGE_REPOSITORY": "registry.areeba.com/payment-service",
            }
        },
    )

    saved = db.session.get(CiService, service.id)
    pipeline = saved.default_pipeline()

    # An ordinary pipeline, nothing marking it as generated.
    assert [stage.name for stage in pipeline.stages] == ["Checkout", "Build"]
    assert pipeline.stages[1].image
    assert pipeline.stages[1].secret_refs == [
        {"name": "NEXUS_PASSWORD", "envVar": "NEXUS_PASSWORD"}
    ]
    # The secret is stored encrypted, and only its key is visible.
    secret = CiSecret.query.filter_by(service_id=saved.id, key="NEXUS_PASSWORD").one()
    assert secret.value_cipher != "hunter2"
    assert decrypt_secret(secret.value_cipher) == "hunter2"
    # A parameter answer becomes a build input, visible and overridable in
    # Run Build rather than baked invisibly into a stage.
    assert {p["name"]: p["default"] for p in pipeline.parameters}[
        "IMAGE_REPOSITORY"
    ] == "registry.areeba.com/payment-service"
    # The discriminator everything else reads is brought in line.
    assert saved.application_type == "java_gradle"
    assert saved.profile_source == "hermes"
    assert saved.analysis_state == "analyzed"
    assert saved.application_profile["languageVersion"] == "17"
    assert row.pipeline_state == "accepted"


def test_accepting_refuses_while_a_required_answer_is_missing(service, monkeypatch):
    fake = FakeHermes(valid_java_response())
    row = run(service, monkeypatch, fake, fake_source.JAVA_GRADLE)
    with pytest.raises(accept_service.AcceptError) as exc:
        accept_service.accept(row, {"inputs": {"NEXUS_PASSWORD": "hunter2"}})
    assert "Image repository" in str(exc.value)
    # Nothing was written on the way to refusing.
    assert CiSecret.query.filter_by(key="NEXUS_PASSWORD").count() == 0


def test_an_edit_that_cannot_work_is_saved_with_the_objection_attached(
    service, monkeypatch
):
    """An edit gets exactly the same scrutiny the model's version got — and
    under the advise policy that means it is kept, with the objection travelling
    beside it. The build will find no Docker socket, and the note said so."""
    fake = FakeHermes(valid_java_response())
    row = run(service, monkeypatch, fake, fake_source.JAVA_GRADLE)
    edited = copy.deepcopy(row.generated_pipeline)
    edited["stages"][1]["commands"] = ["docker build -t app ."]

    accept_service.accept(
        row,
        {
            "pipeline": edited,
            "inputs": {"NEXUS_PASSWORD": "x", "IMAGE_REPOSITORY": "r/p"},
        },
    )
    pipeline = db.session.get(CiService, service.id).default_pipeline()
    assert pipeline.stages[1].commands == ["docker build -t app ."]

    # The objection still exists — it is reported rather than enforced.
    from api.services.ci import generated

    strict = generated.validate(
        db.session.get(CiService, service.id), edited, enforce=True
    )
    assert not strict["valid"]
    assert any(e["code"] == "portability_docker_in_stage" for e in strict["errors"])


def test_a_user_edit_that_is_fine_is_what_gets_saved(service, monkeypatch):
    fake = FakeHermes(valid_java_response())
    row = run(service, monkeypatch, fake, fake_source.JAVA_GRADLE)
    edited = copy.deepcopy(row.generated_pipeline)
    edited["stages"][1]["commands"] = ["./gradlew --no-daemon clean build"]
    edited["stages"][1]["secretRefs"] = []
    accept_service.accept(
        row,
        {
            "pipeline": edited,
            "inputs": {"NEXUS_PASSWORD": "x", "IMAGE_REPOSITORY": "r/p"},
        },
    )
    pipeline = db.session.get(CiService, service.id).default_pipeline()
    assert pipeline.stages[1].commands == ["./gradlew --no-daemon clean build"]


def test_a_stage_that_cannot_be_represented_at_all_is_dropped_and_said(
    service, monkeypatch
):
    """The one thing left that a pipeline cannot survive is a stage KubeSight
    has no way to store. It is left out, the rest is kept, and the omission is
    reported — rather than the whole proposal being lost to it."""
    profile = {"language": "java", "buildSystem": "gradle", "packaging": "jar"}
    # A command stage with nothing in it: unsaveable, and it would do nothing if
    # it were. Offered twice, so the repair loop gets its chance and gives up.
    empty = {"name": "Build", "stageType": "command", "runnerLabels": ["linux"],
             "commands": []}
    fake = FakeHermes(
        response(profile, [checkout(), empty]),
        response(profile, [checkout(), dict(empty)]),
    )
    row = run(service, monkeypatch, fake, fake_source.JAVA_GRADLE)

    assert row.state == "analyzed"
    assert any(
        item["code"] in ("empty_command_stage", "stage_dropped")
        for item in row.validation["warnings"]
    )
    # The checkout stage survived; the pipeline was not thrown away.
    assert [s["name"] for s in row.generated_pipeline["stages"]][0] == "Checkout"


def test_one_analysis_at_a_time_per_service(service, monkeypatch):
    """Two proposals for the same pipeline, and whichever finished second would
    quietly win."""
    fake = FakeHermes(valid_java_response())
    fake.install(monkeypatch)
    fake_source.FAKE.load(fake_source.JAVA_GRADLE)
    # Hold the first analysis at 'queued' — without a worker pool the default
    # is to run inline, which would finish it before the second request arrives
    # and test nothing.
    monkeypatch.setattr("api.services.ci_assist.jobs.submit", lambda _id: None)

    analyses_service.request_analysis(service, {})
    with pytest.raises(analyses_service.AnalysisError) as exc:
        analyses_service.request_analysis(service, {})
    assert "already running" in str(exc.value)


# ---------------------------------------------------------------------------
# A malformed answer
# ---------------------------------------------------------------------------

class RawHermes:
    """Returns responses that have NOT been through the contract validator.

    FakeHermes validates on the way out, which is right for testing the
    pipeline logic but hides the case where the model writes something the
    contract rejects outright. This one lets that through.
    """

    def __init__(self, *payloads):
        self.payloads = list(payloads)
        self.calls = []

    def install(self, monkeypatch):
        monkeypatch.setattr(hermes, "propose", self._propose)
        return self

    def _propose(
        self, *, evidence, capabilities, profile_hint=None, feedback=None, examples=None
    ):
        self.calls.append({"kind": "propose", "feedback": feedback})
        payload = self.payloads.pop(0)
        try:
            return schema.validate_response(copy.deepcopy(payload)), "fake-model", "fake-v1"
        except schema.ContractError as exc:
            raise hermes.ContractFailure(str(exc)) from exc


def nameless_stage_response():
    """What a real Hermes returned: a stage with no name."""
    return response(
        {"language": "java", "buildSystem": "gradle", "packaging": "jar"},
        [{"stageType": "checkout", "runnerLabels": ["linux"], "commands": []}],
    )


def good_response_for_contract():
    return response(
        {"language": "java", "buildSystem": "gradle", "packaging": "jar"},
        [
            checkout(),
            {"name": "Build", "stageType": "command", "buildEnvironment": "java-jdk11",
             "runnerLabels": ["linux", "java"], "commands": ["./gradlew build"]},
        ],
    )


def test_a_malformed_response_is_asked_about_rather_than_given_up_on(service, monkeypatch):
    """A whole repository analysis must not be lost because one stage came back
    without a name. The contract stays strict — nothing unreadable is accepted —
    but the objection is a sentence a model acts on, so it is put to it."""
    fake = RawHermes(nameless_stage_response(), good_response_for_contract()).install(monkeypatch)
    fake_source.FAKE.load(fake_source.JAVA_GRADLE)
    started = analyses_service.request_analysis(service, {})
    generator.run(started["id"])
    row = db.session.get(CiRepositoryAnalysis, started["id"])
    db.session.refresh(row)

    assert row.state == "analyzed"
    assert len(fake.calls) == 2
    # The second ask carried the objection, rather than being the same request
    # sent again in the hope of a different answer.
    assert fake.calls[0]["feedback"] is None
    assert "no name" in fake.calls[1]["feedback"][0]["message"]
    assert row.attempts[0]["note"] == "malformed response"


def test_a_model_that_keeps_answering_malformed_fails_with_the_reason(service, monkeypatch):
    """Bounded, like every other loop here — and the message says both what was
    wrong and that asking again did not help."""
    monkeypatch.setenv("CI_ASSIST_REPAIR_ATTEMPTS", "1")
    fake = RawHermes(nameless_stage_response(), nameless_stage_response()).install(monkeypatch)
    fake_source.FAKE.load(fake_source.JAVA_GRADLE)
    started = analyses_service.request_analysis(service, {})
    generator.run(started["id"])
    row = db.session.get(CiRepositoryAnalysis, started["id"])
    db.session.refresh(row)

    assert row.state == "failed"
    assert len(fake.calls) == 2
    assert "no name" in row.safe_error_message
    assert "asked again" in row.safe_error_message
    # And the service is left usable, as every failure path must.
    assert db.session.get(CiService, service.id).analysis_state == "failed"


# ---------------------------------------------------------------------------
# Teaching Hermes, rather than correcting it
# ---------------------------------------------------------------------------

def test_hermes_is_shown_the_fleet_machine_by_machine(service, monkeypatch):
    """A merged list of every label anybody advertises reads as one very capable
    runner — which is how a proposal ends up asking for "macos" and "java" on
    the same stage. Both labels exist; no single machine has both."""
    fake = FakeHermes(valid_java_response())
    db.session.add(
        CiRunner(
            name="mac-mini-1",
            runner_type="agent_macos",
            status="offline",
            enabled=True,
            os="darwin",
            capabilities=["macos", "xcode"],
        )
    )
    db.session.commit()

    run(service, monkeypatch, fake, fake_source.JAVA_GRADLE)
    fleet = fake.calls[0]["capabilities"]["runners"]

    by_name = {item["name"]: item for item in fleet}
    assert "mac-mini-1" in by_name
    assert by_name["mac-mini-1"]["capabilities"] == ["macos", "xcode"]
    # State is said plainly, so a proposal can prefer a machine that will pick
    # the work up rather than one that is registered and asleep.
    assert by_name["mac-mini-1"]["state"] == "offline"
    assert {item["state"] for item in fleet} <= {"online", "offline", "disabled"}
    # And the rule those labels are matched by is stated, not implied.
    assert "superset" in fake.calls[0]["capabilities"]["runnerSelection"] or "ALL" in (
        fake.calls[0]["capabilities"]["runnerSelection"]
    )


def test_hermes_is_shown_pipelines_that_already_work_here(service, monkeypatch):
    """Everything else describes the shape of a KubeSight pipeline. None of it
    shows one. A model given a schema and no instance infers the conventions,
    and every round trip after that is it learning one by rejection."""
    fake = FakeHermes(valid_java_response())
    run(service, monkeypatch, fake, fake_source.JAVA_GRADLE)

    examples = fake.calls[0]["examples"]
    assert examples, "Hermes was sent no worked example"
    for item in examples:
        stages = item["pipeline"]["stages"]
        assert stages
        # An example must not demonstrate a field a proposal may not set:
        # showing a literal image would teach exactly the mistake the rules
        # spend a paragraph forbidding.
        for stage in stages:
            assert "image" not in stage
            assert set(stage) <= set(generated.STAGE_KEYS) | {"buildEnvironment"}


@pytest.mark.parametrize(
    ("application_type", "expected"),
    [
        # Java has a house pattern — the conventions the existing Jenkins jobs
        # follow, which is the more useful example of the two.
        ("java_gradle", "this organisation"),
        ("java_maven", "this organisation"),
        # A stack with no house pattern falls back to the curated starter kit.
        ("node", "starter kit"),
        ("python", "starter kit"),
    ],
)
def test_a_curated_example_exists_even_on_a_fresh_installation(
    app, service, application_type, expected
):
    """Nothing is saved yet on a new install, so a curated example is always
    included — the feature must not be worse on day one."""
    from api.models_ci import CiPipeline
    from api.services.ci_assist import examples as examples_module

    CiPipeline.query.delete()
    db.session.commit()

    examples = examples_module.worked_examples(preferred_type=application_type)
    assert examples
    assert any(expected in item["source"] for item in examples)


def test_a_real_pipeline_is_preferred_over_the_starter_kit(app, service, monkeypatch):
    """A pipeline somebody has been building with for months teaches more about
    what good looks like here than any curated example."""
    from api.services.ci import pipelines as pipelines_service
    from api.services.ci_assist import examples as examples_module

    pipelines_service.create_pipeline(
        service,
        {
            "name": "default",
            "isDefault": True,
            "stages": [
                {"name": "Build", "stageType": "command", "runnerLabels": ["linux"],
                 "commands": ["make release"], "timeoutSeconds": 900}
            ],
        },
    )
    examples = examples_module.worked_examples()
    assert any("already running" in item["source"] for item in examples)
    real = next(item for item in examples if "already running" in item["source"])
    assert real["pipeline"]["stages"][0]["commands"] == ["make release"]


def test_every_worked_example_is_itself_a_pipeline_kubesight_would_accept(service):
    """An example that would be refused teaches the model to be refused. These
    are the one thing in the request that MUST be correct, because everything
    else is described and this is demonstrated."""
    from api.services.ci import build_environments
    from api.services.ci_assist import examples as examples_module

    for item in examples_module.worked_examples(preferred_type="java_gradle"):
        pipeline = item["pipeline"]
        resolved = {**pipeline, "stages": []}
        for stage in pipeline["stages"]:
            entry = dict(stage)
            key = entry.pop("buildEnvironment", "")
            if key:
                environment = build_environments.resolve(key)
                assert environment is not None, f"unknown environment {key} in an example"
                entry["image"] = environment["image"]
                entry["runnerLabels"] = sorted(
                    set(entry.get("runnerLabels", [])) | set(environment["labels"])
                )
            resolved["stages"].append(entry)

        verdict = generated.validate(service, resolved, enforce=True)
        assert verdict["valid"], f"{item['source']}: {verdict['errors']}"


def test_the_house_example_carries_the_conventions_a_repository_cannot_show(service):
    """The things a model gets wrong every time until it is shown them: the
    version lives in version.properties and has to reach later stages, the JAR
    is normalised to app.jar, and `docker build` is a container_image stage
    rather than a command in a pod that has no Docker socket."""
    from api.services.ci_assist import examples as examples_module

    house = next(
        item
        for item in examples_module.worked_examples(preferred_type="java_gradle")
        if "this organisation" in item["source"]
    )
    stages = {stage["name"]: stage for stage in house["pipeline"]["stages"]}
    commands = " ".join(
        line for stage in stages.values() for line in stage.get("commands", [])
    )

    assert "version.properties" in commands
    assert "$KUBESIGHT_ENV" in commands
    assert "app.jar" in commands
    assert stages["Build Container Image"]["stageType"] == "container_image"
    # And it never demonstrates the thing the rules forbid.
    assert "docker build" not in commands
    assert "docker push" not in commands
    # Deploys and notifications are not part of a build, so they are not shown.
    assert not any(
        word in " ".join(stages).lower() for word in ("deploy", "notification", "slack")
    )
