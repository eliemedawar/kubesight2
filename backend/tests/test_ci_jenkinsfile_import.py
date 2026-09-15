"""Importing a Jenkinsfile into a pipeline draft.

Two things are being protected here, and they pull in opposite directions.

The first is that the translation is *useful*: a stage keeps its name, its shell
survives intact, a boolean parameter arrives as a boolean, and a ``when`` clause
becomes the run condition that actually gates the stage. Tests that only asserted
"it produced some stages" would pass on a translation nobody could use.

The second is that it is *honest*. Everything that did not translate has to come
back as a note, and the draft has to be saveable as-is or say why not — the whole
point of not writing anything is that a person gets to see the difference before
it replaces a pipeline that works. So the last tests take a draft and save it
through the real API.
"""

from __future__ import annotations

import pytest

from api.db import db
from api.models_application_intelligence import BitbucketCredentialProfile
from api.secret_encryption import encrypt_secret
from api.services.ci import jenkinsfile, portability
from tests.conftest import auth_headers

SIMPLE = """
pipeline {
    agent { label 'linux' }

    parameters {
        booleanParam(name: 'RUN_TESTS', defaultValue: true, description: 'Run the suite.')
        string(name: 'REGISTRY', defaultValue: 'registry.areeba.com', description: 'Registry prefix.')
        choice(name: 'ENVIRONMENT', choices: ['dev', 'uat', 'prod'], description: 'Where to deploy.')
        text(name: 'DOCKERFILE', defaultValue: 'FROM alpine\\nRUN true\\n', description: 'Image recipe.')
    }

    environment {
        APP_NAME = 'checkout-api'
    }

    stages {
        stage('Build') {
            agent { docker { image 'maven:3.9-eclipse-temurin-21' } }
            steps {
                sh 'mvn -B -DskipTests package'
            }
        }
        stage('Test') {
            when {
                expression { params.RUN_TESTS }
            }
            steps {
                dir('backend') {
                    sh '''
                        python3 -m venv .venv
                        . .venv/bin/activate
                        pytest tests -q
                    '''
                }
                archiveArtifacts artifacts: 'backend/reports/*.xml', allowEmptyArchive: true
            }
        }
    }
}
"""


def _stage(draft, name):
    for stage in draft["stages"]:
        if stage["name"] == name:
            return stage
    raise AssertionError(f"no stage named {name!r} in {[s['name'] for s in draft['stages']]}")


def _messages(draft, level=None):
    return [
        note["message"]
        for note in draft["notes"]
        if level is None or note["level"] == level
    ]


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------

def test_stage_names_commands_and_working_directory_survive():
    draft = jenkinsfile.parse(SIMPLE)

    assert [stage["name"] for stage in draft["stages"]] == ["Checkout", "Build", "Test"]

    build = _stage(draft, "Build")
    assert build["commands"] == ["mvn -B -DskipTests package"]
    # `agent { docker { image } }` is the stage image; that is how a stage names
    # what it runs in.
    assert build["image"] == "maven:3.9-eclipse-temurin-21"
    assert build["runnerLabels"] == ["linux"]

    test = _stage(draft, "Test")
    # A stage whose whole body is one dir() puts the path on the stage, so the
    # commands read the way they did in Jenkins.
    assert test["workingDirectory"] == "backend"
    assert test["commands"] == [
        "python3 -m venv .venv",
        ". .venv/bin/activate",
        "pytest tests -q",
    ]
    assert test["artifacts"] == [
        {"path": "backend/reports/*.xml", "type": "binary"}
    ]


def test_parameters_keep_their_name_type_and_default():
    draft = jenkinsfile.parse(SIMPLE)
    by_name = {param["name"]: param for param in draft["parameters"]}

    assert by_name["RUN_TESTS"]["type"] == "boolean"
    # Stored as the string a stage receives, not as a Python bool.
    assert by_name["RUN_TESTS"]["default"] == "true"
    assert by_name["RUN_TESTS"]["description"] == "Run the suite."

    assert by_name["REGISTRY"]["type"] == "text"
    assert by_name["REGISTRY"]["default"] == "registry.areeba.com"

    assert by_name["ENVIRONMENT"]["type"] == "choice"
    assert by_name["ENVIRONMENT"]["choices"] == ["dev", "uat", "prod"]
    assert by_name["ENVIRONMENT"]["default"] == "dev"

    # A `text` parameter is a whole file, so it keeps its newlines.
    assert by_name["DOCKERFILE"]["type"] == "multiline"
    assert by_name["DOCKERFILE"]["default"] == "FROM alpine\nRUN true\n"


def test_expression_on_a_parameter_becomes_the_run_condition():
    draft = jenkinsfile.parse(SIMPLE)
    assert _stage(draft, "Test")["runCondition"] == {
        "variable": "RUN_TESTS",
        "operator": "equals",
        "value": "true",
    }


def test_a_checkout_stage_is_added_because_jenkins_cloned_for_the_whole_job():
    draft = jenkinsfile.parse(SIMPLE)
    first = draft["stages"][0]
    assert first["stageType"] == "checkout"
    assert first["commands"] == []


def test_jenkins_controller_labels_do_not_become_runner_capabilities():
    draft = jenkinsfile.parse(
        """
        pipeline {
            agent { label 'master && linux' }
            stages {
                stage('Build') { steps { sh 'make' } }
            }
        }
        """
    )

    assert all(stage["runnerLabels"] == ["linux"] for stage in draft["stages"])
    assert any(
        "controller label master was removed" in message
        for message in _messages(draft, jenkinsfile.INFO)
    )


def test_jenkins_host_and_gradle_home_workarounds_become_kubesight_native_fields():
    draft = jenkinsfile.parse(
        '''
        pipeline {
            agent { label 'master' }
            stages {
                stage('build jar file') {
                    agent { docker { image 'registry.areeba.com/gradle:8-jdk11' } }
                    steps {
                        sh """
                            echo '${params.gradleproperties}' > /home/gradle/.gradle/gradle.properties
                            grep -q 'registry.areeba.com' /etc/hosts || echo '10.43.17.16 registry.areeba.com' >> /etc/hosts
                            gradle clean build
                            cp build/libs/app.jar /workspace/source/app.jar
                        """
                    }
                }
            }
        }
        '''
    )

    stage = _stage(draft, "build jar file")
    assert stage["runnerLabels"] == []
    assert stage["hostAliases"] == [
        {"ip": "10.43.17.16", "hostnames": ["registry.areeba.com"]}
    ]
    assert stage["commands"] == [
        'export GRADLE_USER_HOME="$KUBESIGHT_WORKSPACE/.gradle"',
        'mkdir -p "$GRADLE_USER_HOME"',
        'printf \'%s\' "${gradleproperties}" > "$GRADLE_USER_HOME/gradle.properties"',
        "gradle clean build",
        "cp build/libs/app.jar ${KUBESIGHT_SOURCE}/app.jar",
    ]
    assert "/etc/hosts" not in "\n".join(stage["commands"])
    assert "/home/gradle" not in "\n".join(stage["commands"])
    assert "write_outside_workspace" not in {
        finding["code"] for finding in portability.analyze(draft["stages"])["findings"]
    }


def test_jenkins_interpolated_multiline_value_is_not_written_literally():
    draft = jenkinsfile.parse(
        '''
        pipeline {
            agent any
            parameters {
                text(name: 'settinggradle', defaultValue: '', description: '')
            }
            stages {
                stage('Gradle settings') {
                    steps {
                        sh """
                            echo "rootProject.name = 'issuing'" > settings.gradle
                            echo '${settinggradle}' >> settings.gradle
                        """
                    }
                }
            }
        }
        '''
    )

    stage = _stage(draft, "Gradle settings")
    assert stage["commands"] == [
        'echo "rootProject.name = \'issuing\'" > settings.gradle',
        'printf \'%s\\n\' "${settinggradle}" >> settings.gradle',
    ]
    assert any(
        "Jenkins-interpolated file value" in message
        for message in _messages(draft, jenkinsfile.INFO)
    )


def test_the_job_environment_is_copied_onto_every_stage():
    draft = jenkinsfile.parse(SIMPLE)
    assert _stage(draft, "Build")["env"]["APP_NAME"] == "checkout-api"
    assert _stage(draft, "Test")["env"]["APP_NAME"] == "checkout-api"


CONDITIONS = """
pipeline {
    agent any
    stages {
        stage('Deploy UAT') {
            when { equals expected: 'true', actual: DEPLOY_UAT }
            steps { sh 'ansible-playbook deploy.yml' }
        }
        stage('Deploy prod') {
            when { expression { params.ENVIRONMENT == 'prod' } }
            steps { sh 'ansible-playbook deploy.yml -e env=prod' }
        }
        stage('Skip on flag') {
            when { expression { !params.SKIP_ME } }
            steps { sh 'echo running' }
        }
        stage('Main only') {
            when { branch 'main' }
            steps { sh 'echo on main' }
        }
    }
}
"""


@pytest.mark.parametrize(
    "stage_name, expected",
    [
        ("Deploy UAT", {"variable": "DEPLOY_UAT", "operator": "equals", "value": "true"}),
        ("Deploy prod", {"variable": "ENVIRONMENT", "operator": "equals", "value": "prod"}),
        ("Skip on flag", {"variable": "SKIP_ME", "operator": "not_equals", "value": "true"}),
        # The branch is a build variable, so `when { branch }` has somewhere to go.
        ("Main only", {"variable": "KUBESIGHT_BRANCH", "operator": "equals", "value": "main"}),
    ],
)
def test_every_when_form_a_run_condition_can_hold(stage_name, expected):
    draft = jenkinsfile.parse(CONDITIONS)
    assert _stage(draft, stage_name)["runCondition"] == expected


def test_a_compound_when_keeps_one_condition_and_says_what_it_dropped():
    draft = jenkinsfile.parse(
        """
        pipeline {
            agent any
            stages {
                stage('Push') {
                    when {
                        allOf {
                            expression { params.BUILD_DOCKER }
                            expression { params.REGISTRY?.trim() }
                        }
                    }
                    steps { sh 'docker push x' }
                }
            }
        }
        """
    )
    stage = _stage(draft, "Push")
    assert stage["runCondition"] == {
        "variable": "BUILD_DOCKER",
        "operator": "equals",
        "value": "true",
    }
    # The dropped half has to be visible, or the stage runs in cases it did not
    # run in before and nobody was told.
    assert any("cannot express" in message for message in _messages(draft))


def test_parallel_branches_become_consecutive_stages_tagged_with_their_group():
    draft = jenkinsfile.parse(
        """
        pipeline {
            agent any
            stages {
                stage('Test') {
                    parallel {
                        stage('Backend') { steps { sh 'pytest' } }
                        stage('Frontend') { steps { sh 'npm test' } }
                    }
                }
            }
        }
        """
    )
    names = [stage["name"] for stage in draft["stages"]]
    assert names == ["Checkout", "Backend", "Frontend"]
    assert _stage(draft, "Backend")["parallelGroup"] == "Test"
    assert _stage(draft, "Frontend")["parallelGroup"] == "Test"
    assert any("in parallel" in message for message in _messages(draft, jenkinsfile.INFO))


def test_duplicate_branch_names_are_made_unique_because_stage_names_must_be():
    draft = jenkinsfile.parse(
        """
        pipeline {
            agent any
            stages {
                stage('A') { parallel { stage('Build') { steps { sh 'a' } } } }
                stage('B') { parallel { stage('Build') { steps { sh 'b' } } } }
            }
        }
        """
    )
    names = [stage["name"] for stage in draft["stages"] if stage["stageType"] == "command"]
    assert names == ["Build", "Build (2)"]


def test_jenkins_variables_are_renamed_to_the_ones_a_build_actually_exports():
    draft = jenkinsfile.parse(
        """
        pipeline {
            agent any
            stages {
                stage('Tag') {
                    steps {
                        sh "echo ${env.APP}-${BUILD_NUMBER} > ${WORKSPACE}/tag.txt"
                    }
                }
            }
        }
        """
    )
    assert _stage(draft, "Tag")["commands"] == [
        "echo ${APP}-${KUBESIGHT_BUILD_NUMBER} > ${KUBESIGHT_SOURCE}/tag.txt"
    ]
    assert any("KUBESIGHT_BUILD_NUMBER" in message for message in _messages(draft))


def test_a_groovy_escaped_dollar_reaches_the_shell_as_a_dollar():
    """``"\\$PASSWORD"`` in a GString is ``$PASSWORD`` by the time sh sees it.

    Keeping the backslash would send docker login the literal text ``$PASSWORD``
    — a failed login, and the password shape in the build log.
    """
    draft = jenkinsfile.parse(
        '''
        pipeline {
            agent any
            stages {
                stage('Push') {
                    steps {
                        sh """
                            echo "\\$REGISTRY_PASS" | docker login -u "\\$REGISTRY_USER" --password-stdin
                        """
                    }
                }
            }
        }
        '''
    )
    assert _stage(draft, "Push")["commands"] == [
        'echo "$REGISTRY_PASS" | docker login -u "$REGISTRY_USER" --password-stdin'
    ]


def test_a_brace_inside_a_shell_heredoc_does_not_end_the_stage():
    """The mask exists for exactly this: shell text that looks like Groovy."""
    draft = jenkinsfile.parse(
        """
        pipeline {
            agent any
            stages {
                stage('Write') {
                    steps {
                        sh '''
                            cat > app.json <<EOF
                            { "name": "x", "nested": { "deep": true } }
                            EOF
                            echo done
                        '''
                    }
                }
                stage('After') { steps { sh 'echo after' } }
            }
        }
        """
    )
    assert [stage["name"] for stage in draft["stages"]] == ["Checkout", "Write", "After"]
    assert "echo done" in _stage(draft, "Write")["commands"]
    assert _stage(draft, "After")["commands"] == ["echo after"]


def test_a_comment_holding_a_brace_does_not_end_the_block():
    draft = jenkinsfile.parse(
        """
        pipeline {
            agent any
            stages {
                // stage('Ghost') { steps { sh 'never' } }
                stage('Real') { steps { sh 'echo real' } }
            }
        }
        """
    )
    assert [stage["name"] for stage in draft["stages"]] == ["Checkout", "Real"]


def test_credential_bindings_become_the_secret_names_the_stage_needs():
    draft = jenkinsfile.parse(
        """
        pipeline {
            agent any
            stages {
                stage('Push') {
                    steps {
                        withCredentials([usernamePassword(
                            credentialsId: 'nexus-creds',
                            usernameVariable: 'NEXUS_USER',
                            passwordVariable: 'NEXUS_PASS'
                        )]) {
                            sh 'docker login -u "$NEXUS_USER"'
                        }
                    }
                }
            }
        }
        """
    )
    assert {entry["name"] for entry in draft["secrets"]} == {"NEXUS_USER", "NEXUS_PASS"}
    assert draft["secrets"][0]["credentialId"] == "nexus-creds"
    assert draft["secrets"][0]["usedBy"] == ["Push"]
    # The commands inside the wrapper are kept — the wrapper is what goes away.
    assert _stage(draft, "Push")["commands"] == ['docker login -u "$NEXUS_USER"']


def test_environment_credentials_become_a_secret_not_an_environment_value():
    draft = jenkinsfile.parse(
        """
        pipeline {
            agent any
            environment { NPM_TOKEN = credentials('npm-token') }
            stages { stage('Build') { steps { sh 'npm ci' } } }
        }
        """
    )
    assert [entry["name"] for entry in draft["secrets"]] == ["NPM_TOKEN"]
    assert "NPM_TOKEN" not in _stage(draft, "Build")["env"]


def test_untranslatable_steps_are_reported_rather_than_dropped_in_silence():
    draft = jenkinsfile.parse(
        """
        pipeline {
            agent any
            stages {
                stage('Ship') {
                    steps {
                        sh 'make'
                        stash name: 'out', includes: 'dist/**'
                        input message: 'Approve?'
                        someExoticPluginStep param: 'x'
                    }
                    post { always { sh 'rm -rf tmp' } }
                }
            }
        }
        """
    )
    messages = " | ".join(_messages(draft))
    assert "stash" in messages
    assert "manual approval" in messages
    assert "someExoticPluginStep" in messages
    assert "post { } block" in messages
    # Everything it *could* read is still there.
    assert _stage(draft, "Ship")["commands"] == ["make"]


def test_a_groovy_binding_is_answered_with_the_thing_that_replaces_it():
    draft = jenkinsfile.parse(
        """
        pipeline {
            agent any
            stages {
                stage('Version') {
                    steps {
                        script {
                            env.APP_VERSION = readFile('version.txt').trim()
                        }
                    }
                }
            }
        }
        """
    )
    assert any("KUBESIGHT_ENV" in message for message in _messages(draft))


def test_a_windows_batch_step_is_an_error_not_a_quiet_import():
    draft = jenkinsfile.parse(
        """
        pipeline {
            agent any
            stages { stage('Build') { steps { bat 'msbuild app.sln' } } }
        }
        """
    )
    assert any("bat step" in message for message in _messages(draft, jenkinsfile.ERROR))
    assert _stage(draft, "Build")["commands"] == ["msbuild app.sln"]


def test_a_stage_with_nothing_readable_is_flagged_because_it_cannot_save():
    draft = jenkinsfile.parse(
        """
        pipeline {
            agent any
            stages { stage('Nothing') { steps { cleanWs() } } }
        }
        """
    )
    assert any(
        "needs at least one" in message
        for message in _messages(draft, jenkinsfile.ERROR)
    )


def test_a_docker_build_is_pointed_at_the_stage_type_that_does_it_properly():
    draft = jenkinsfile.parse(
        """
        pipeline {
            agent any
            stages {
                stage('Image') {
                    steps {
                        sh 'docker build -t app:1 .'
                        sh 'docker push app:1'
                    }
                }
            }
        }
        """
    )
    # A suggestion, not a rewrite: the image name and tag would have to be
    # guessed off a command line, and a stage that builds the wrong image passes
    # review.
    assert _stage(draft, "Image")["stageType"] == "command"
    assert any("Build image stage" in message for message in _messages(draft))


def test_a_pipeline_timeout_becomes_a_per_stage_timeout():
    draft = jenkinsfile.parse(
        """
        pipeline {
            agent any
            options { timeout(time: 10, unit: 'MINUTES') }
            stages { stage('Build') { steps { sh 'make' } } }
        }
        """
    )
    assert _stage(draft, "Build")["timeoutSeconds"] == 600


def test_a_stage_timeout_wrapper_lands_on_the_stage():
    draft = jenkinsfile.parse(
        """
        pipeline {
            agent any
            stages {
                stage('Slow') {
                    steps { timeout(time: 2, unit: 'HOURS') { sh 'make world' } }
                }
            }
        }
        """
    )
    stage = _stage(draft, "Slow")
    assert stage["timeoutSeconds"] == 7200
    assert stage["commands"] == ["make world"]


# ---------------------------------------------------------------------------
# Where build inputs hide
#
# A declarative `parameters { }` block is only one of the three places a Jenkins
# job's inputs can live, and the other two are common in jobs old enough to be
# worth porting. Importing nine stages and no inputs reads as the translation
# having dropped something, so both other forms are covered here.
# ---------------------------------------------------------------------------

PROPERTIES_FORM = """
properties([
    parameters([
        booleanParam(name: 'Lebanonuat', defaultValue: false, description: 'Deploy to UAT'),
        booleanParam(name: 'Lebanonsit', defaultValue: false, description: 'Deploy to SIT'),
        string(name: 'msName', defaultValue: 'issuing', description: 'Microservice name'),
        choice(name: 'GradleVersion', choices: ['7.6', '8.5'], description: 'Gradle version')
    ])
])

pipeline {
    agent any
    stages {
        stage('Deploy uat Lebanon') {
            when { equals expected: 'true', actual: Lebanonuat }
            steps { sh 'echo deploying' }
        }
    }
}
"""


def test_parameters_declared_as_properties_rather_than_a_block():
    """The pre-declarative idiom, still used by any job that must also be
    launchable from the Jenkins UI."""
    draft = jenkinsfile.parse(PROPERTIES_FORM)
    by_name = {param["name"]: param for param in draft["parameters"]}

    assert set(by_name) == {"Lebanonuat", "Lebanonsit", "msName", "GradleVersion"}
    assert by_name["Lebanonuat"]["type"] == "boolean"
    assert by_name["Lebanonuat"]["default"] == "false"
    assert by_name["Lebanonuat"]["description"] == "Deploy to UAT"
    assert by_name["msName"]["type"] == "text"
    assert by_name["msName"]["default"] == "issuing"
    assert by_name["GradleVersion"]["choices"] == ["7.6", "8.5"]


def test_a_gating_input_the_file_never_declares_is_created():
    """Parameterised on the Jenkins job, not in the Jenkinsfile.

    Without this the deploy stages import with a condition nothing can ever
    satisfy, and the Run Build dialog asks for nothing — which looks exactly
    like the import having lost them.
    """
    draft = jenkinsfile.parse(
        """
        pipeline {
            agent any
            stages {
                stage('Deploy uat Lebanon') {
                    when { equals expected: 'true', actual: Lebanonuat }
                    steps { sh 'echo uat' }
                }
                stage('Prod only') {
                    when { expression { params.ENVIRONMENT == 'prod' } }
                    steps { sh 'echo prod' }
                }
            }
        }
        """
    )
    by_name = {param["name"]: param for param in draft["parameters"]}

    # Typed from what the condition compares against.
    assert by_name["Lebanonuat"]["type"] == "boolean"
    # Unticked, so importing a pipeline never arms a deploy by accident.
    assert by_name["Lebanonuat"]["default"] == "false"
    assert by_name["ENVIRONMENT"]["type"] == "text"
    assert by_name["ENVIRONMENT"]["default"] == ""

    # Invented, so it has to be said out loud.
    assert any(
        "nothing in the Jenkinsfile declares it" in message
        for message in _messages(draft, jenkinsfile.WARNING)
    )


def test_an_inferred_parameter_never_shadows_a_declared_one():
    draft = jenkinsfile.parse(PROPERTIES_FORM)
    names = [param["name"] for param in draft["parameters"]]
    assert names.count("Lebanonuat") == 1
    # The declared description survives rather than being replaced by the
    # generated "Gates the ... stage" one.
    assert draft["parameters"][0]["description"] == "Deploy to UAT"


def test_the_build_variables_a_stage_already_has_are_not_asked_for():
    """`when { branch 'main' }` gates on KUBESIGHT_BRANCH, which the build
    exports — asking a person for it would be asking twice."""
    draft = jenkinsfile.parse(
        """
        pipeline {
            agent any
            stages {
                stage('Main only') {
                    when { branch 'main' }
                    steps { sh 'echo on main' }
                }
            }
        }
        """
    )
    assert draft["parameters"] == []


def test_a_job_that_asks_for_nothing_says_so():
    draft = jenkinsfile.parse(
        """
        pipeline {
            agent any
            stages { stage('Build') { steps { sh 'make' } } }
        }
        """
    )
    assert draft["parameters"] == []
    assert any("asks for nothing" in message for message in _messages(draft))


# ---------------------------------------------------------------------------
# What it refuses
# ---------------------------------------------------------------------------

def test_a_scripted_pipeline_is_refused_by_name():
    with pytest.raises(jenkinsfile.JenkinsfileError) as exc:
        jenkinsfile.parse("node('linux') {\n  sh 'make'\n}\n")
    assert "scripted pipeline" in str(exc.value)


def test_something_that_is_not_a_jenkinsfile_is_refused():
    with pytest.raises(jenkinsfile.JenkinsfileError):
        jenkinsfile.parse("# just a readme\n\nNothing to see.\n")


def test_an_empty_paste_is_refused():
    with pytest.raises(jenkinsfile.JenkinsfileError):
        jenkinsfile.parse("   \n  ")


def test_an_oversized_file_is_refused_rather_than_parsed():
    with pytest.raises(jenkinsfile.JenkinsfileError) as exc:
        jenkinsfile.parse("x" * (jenkinsfile.MAX_SOURCE_CHARS + 1))
    assert "KB" in str(exc.value)


# ---------------------------------------------------------------------------
# The API, and whether the draft actually saves
# ---------------------------------------------------------------------------

@pytest.fixture()
def service_id(app, client, admin_token):
    with app.app_context():
        credential = BitbucketCredentialProfile(
            name="import-token",
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
        json={"name": "Imported App", "applicationType": "java"},
        headers=auth_headers(admin_token),
    ).get_json()["data"]["id"]
    client.put(
        f"/api/ci/services/{created}/source",
        json={
            "repositoryUrl": "https://bitbucket.org/areeba/imported-app",
            "defaultBranch": "master",
            "credentialProfileId": credential_id,
        },
        headers=auth_headers(admin_token),
    )
    return created


def _import(client, token, content, service_id=None):
    payload = {"content": content}
    if service_id is not None:
        payload["serviceId"] = service_id
    return client.post(
        "/api/ci/pipelines/import/jenkinsfile",
        json=payload,
        headers=auth_headers(token),
    )


def test_the_endpoint_returns_a_draft_and_writes_nothing(client, admin_token, service_id):
    before = client.get(
        f"/api/ci/services/{service_id}/pipelines", headers=auth_headers(admin_token)
    ).get_json()["data"]["items"][0]

    response = _import(client, admin_token, SIMPLE, service_id)
    assert response.status_code == 200
    draft = response.get_json()["data"]
    assert draft["counts"]["stages"] == 3
    assert draft["counts"]["parameters"] == 4

    after = client.get(
        f"/api/ci/services/{service_id}/pipelines", headers=auth_headers(admin_token)
    ).get_json()["data"]["items"][0]
    # Same revision, same stages: an import is a proposal.
    assert after["version"] == before["version"]
    assert [s["name"] for s in after["stages"]] == [s["name"] for s in before["stages"]]


def test_the_draft_saves_through_the_ordinary_pipeline_endpoint(
    client, admin_token, service_id
):
    draft = _import(client, admin_token, SIMPLE, service_id).get_json()["data"]
    assert draft["blocking"] == []

    pipeline_id = client.get(
        f"/api/ci/services/{service_id}/pipelines", headers=auth_headers(admin_token)
    ).get_json()["data"]["items"][0]["id"]

    saved = client.put(
        f"/api/ci/pipelines/{pipeline_id}",
        json={"parameters": draft["parameters"], "stages": draft["stages"]},
        headers=auth_headers(admin_token),
    )
    assert saved.status_code == 200, saved.get_json()
    body = saved.get_json()["data"]
    assert [stage["name"] for stage in body["stages"]] == ["Checkout", "Build", "Test"]
    assert {param["name"] for param in body["parameters"]} == {
        "RUN_TESTS",
        "REGISTRY",
        "ENVIRONMENT",
        "DOCKERFILE",
    }


def test_a_secret_this_service_does_not_have_is_named_and_left_off(
    client, admin_token, service_id
):
    """Left off deliberately: saving a reference to a missing secret is refused,
    and a draft that cannot be saved is worse than one that is missing a link."""
    source = """
    pipeline {
        agent any
        stages {
            stage('Push') {
                steps {
                    withCredentials([string(credentialsId: 'nexus', variable: 'NEXUS_TOKEN')]) {
                        sh 'curl -H "Authorization: Bearer $NEXUS_TOKEN" https://nexus/'
                    }
                }
            }
        }
    }
    """
    draft = _import(client, admin_token, source, service_id).get_json()["data"]

    assert draft["secrets"] == [
        {
            "name": "NEXUS_TOKEN",
            "credentialId": "nexus",
            "usedBy": ["Push"],
            "defined": False,
        }
    ]
    assert all(stage["secretRefs"] == [] for stage in draft["stages"])
    assert any("NEXUS_TOKEN" in note["message"] for note in draft["notes"])
    assert draft["blocking"] == []


def test_a_secret_the_service_already_has_is_attached_to_the_stage(
    client, admin_token, service_id
):
    client.post(
        f"/api/ci/services/{service_id}/secrets",
        json={"key": "NEXUS_TOKEN", "value": "s3cr3t"},
        headers=auth_headers(admin_token),
    )
    source = """
    pipeline {
        agent any
        stages {
            stage('Push') {
                steps {
                    withCredentials([string(credentialsId: 'nexus', variable: 'NEXUS_TOKEN')]) {
                        sh 'curl -H "Authorization: Bearer $NEXUS_TOKEN" https://nexus/'
                    }
                }
            }
        }
    }
    """
    draft = _import(client, admin_token, source, service_id).get_json()["data"]
    assert draft["secrets"][0]["defined"] is True
    assert draft["stages"][-1]["secretRefs"] == [
        {"name": "NEXUS_TOKEN", "envVar": "NEXUS_TOKEN"}
    ]


def test_a_draft_that_cannot_save_says_so_before_anyone_tries(
    client, admin_token, service_id
):
    source = """
    pipeline {
        agent any
        stages { stage('Empty') { steps { cleanWs() } } }
    }
    """
    draft = _import(client, admin_token, source, service_id).get_json()["data"]
    assert draft["blocking"]
    assert "Empty" in draft["blocking"][0]


def test_a_bad_paste_is_a_400_with_the_reason(client, admin_token, service_id):
    response = _import(client, admin_token, "not a pipeline", service_id)
    assert response.status_code == 400
    assert "declarative" in response.get_json()["error"]


def test_importing_needs_permission_to_edit_pipelines(client, viewer_token):
    response = _import(client, viewer_token, SIMPLE)
    assert response.status_code == 403
