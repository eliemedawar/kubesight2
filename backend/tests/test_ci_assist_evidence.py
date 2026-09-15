"""Reading a repository well enough to build it, without cloning it.

The argument for the light path is that everything needed to plan a build lives
in about thirty machine-readable files. These tests are that argument made
checkable: the right files are read, the wrong ones are not, the structure is
described correctly, and a monorepo service sees only its own directory.
"""

from __future__ import annotations

import pytest

from api.db import db
from api.models_application_intelligence import BitbucketCredentialProfile
from api.models_ci import CiService
from api.secret_encryption import encrypt_secret
from api.services.ci_assist import evidence as evidence_module
from tests.fixtures import fake_source


@pytest.fixture()
def service(app):
    with app.app_context():
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
            application_type="java_gradle",
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


def paths(result):
    return [item["path"] for item in result.files]


# ---------------------------------------------------------------------------
# What gets read
# ---------------------------------------------------------------------------

def test_the_build_descriptors_are_read_and_the_source_is_not(service):
    """Source files are the bulk of a repository, decide nothing about how it
    is built, and are the highest-value place to hide an instruction. Reading
    them would cost context and buy an injection surface."""
    fake_source.FAKE.load(fake_source.JAVA_GRADLE)
    result = evidence_module.collect(service)

    assert "build.gradle" in paths(result)
    assert "settings.gradle" in paths(result)
    assert "gradle/wrapper/gradle-wrapper.properties" in paths(result)
    assert "Dockerfile" in paths(result)
    assert not any(path.startswith("src/") for path in paths(result))


def test_the_whole_tree_travels_even_though_most_of_it_is_not_read(service):
    """Structure is cheap and decisive. Knowing src/test exists is what makes a
    test stage a fact rather than an assumption."""
    fake_source.FAKE.load(fake_source.JAVA_GRADLE)
    result = evidence_module.collect(service)
    assert "src/test/java/com/acme/ApplicationTest.java" in result.tree
    assert "src/main/java/com/acme/Application.java" in result.tree


def test_a_file_whose_name_promises_credentials_is_never_read(service):
    """Not redacted — not read. Choosing not to fetch is a stronger guarantee
    than fetching and then trying to remove the interesting parts."""
    fake_source.FAKE.load(
        {
            **fake_source.ANDROID,
            "local.properties": "storePassword=hunter2\nkeyAlias=release\n",
            ".env": "DB_PASSWORD=hunter2\n",
        }
    )
    result = evidence_module.collect(service)
    assert "local.properties" not in paths(result)
    assert ".env" not in paths(result)
    assert not any("hunter2" in item["content"] for item in result.files)


def test_what_is_read_is_redacted_on_the_way_in(service):
    """Redaction happens at collection, not at send: nothing downstream of this
    point should ever be holding the unredacted text in the first place."""
    fake_source.FAKE.load(
        {
            "build.gradle": (
                "repositories { maven { url 'https://deploy:s3cr3t@nexus.areeba.com/repo' } }\n"
            ),
            "gradlew": "#!/bin/sh\n",
        }
    )
    result = evidence_module.collect(service)
    body = next(item["content"] for item in result.files if item["path"] == "build.gradle")
    assert "s3cr3t" not in body
    assert "nexus.areeba.com" in body


def test_build_output_directories_are_ignored(service):
    fake_source.FAKE.load(
        {
            **fake_source.NODE_NPM,
            "node_modules/left-pad/package.json": '{"name":"left-pad"}',
            "dist/package.json": '{"name":"built"}',
        }
    )
    result = evidence_module.collect(service)
    assert "node_modules/left-pad/package.json" not in result.tree
    assert "dist/package.json" not in result.tree
    assert "package.json" in paths(result)


def test_the_file_budget_spends_itself_on_the_most_decisive_files_first(service, monkeypatch):
    """When the budget runs out it must run out on the least useful file. A
    pom.xml dropped in favour of a tsconfig.json would be a wrong answer."""
    monkeypatch.setattr(evidence_module, "MAX_FILES", 2)
    fake_source.FAKE.load(fake_source.JAVA_GRADLE)
    result = evidence_module.collect(service)

    assert len(result.files) == 2
    assert set(paths(result)) <= {"build.gradle", "settings.gradle"}
    assert result.coverage["filesSkippedForBudget"] > 0


# ---------------------------------------------------------------------------
# Deterministic facts — the part that cannot be imagined
# ---------------------------------------------------------------------------

def test_wrapper_presence_is_a_fact_not_an_opinion(service):
    fake_source.FAKE.load(fake_source.JAVA_GRADLE)
    facts = evidence_module.collect(service).deterministic
    assert facts["hasGradleWrapper"] is True
    assert facts["hasMavenWrapper"] is False
    assert facts["hasGradleBuild"] is True


def test_a_missing_wrapper_is_stated_as_missing(service):
    fake_source.FAKE.load(
        {k: v for k, v in fake_source.JAVA_GRADLE.items() if not k.startswith("gradle")}
    )
    facts = evidence_module.collect(service).deterministic
    assert facts["hasGradleWrapper"] is False


def test_the_package_manager_is_read_off_the_lockfile(service):
    fake_source.FAKE.load({**fake_source.NODE_NPM})
    assert evidence_module.collect(service).deterministic["lockfiles"] == [
        "package-lock.json"
    ]

    fake_source.FAKE.load(
        {
            **{k: v for k, v in fake_source.NODE_NPM.items() if k != "package-lock.json"},
            "pnpm-lock.yaml": "lockfileVersion: 9",
        }
    )
    assert evidence_module.collect(service).deterministic["lockfiles"] == ["pnpm-lock.yaml"]


def test_modules_are_counted_from_where_the_build_files_are(service):
    fake_source.FAKE.load(
        {
            "settings.gradle": "include ':core', ':api'\n",
            "build.gradle": "plugins { id 'java' }\n",
            "gradlew": "#!/bin/sh\n",
            "core/build.gradle": "plugins { id 'java' }\n",
            "api/build.gradle": "plugins { id 'java' }\n",
        }
    )
    facts = evidence_module.collect(service).deterministic
    assert facts["gradleModules"] == ["api", "core"]


def test_an_android_app_module_is_recognised(service):
    fake_source.FAKE.load(fake_source.ANDROID)
    assert evidence_module.collect(service).deterministic["hasAndroidAppModule"] is True


def test_an_xcode_project_is_recognised(service):
    fake_source.FAKE.load(fake_source.IOS)
    assert evidence_module.collect(service).deterministic["xcodeProjects"] == [
        "Wallet.xcodeproj"
    ]


def test_an_existing_ci_definition_is_surfaced_because_it_is_the_best_evidence(service):
    """A Jenkinsfile states what this project's build actually needs. Nothing
    in the repository is better evidence of that."""
    fake_source.FAKE.load({**fake_source.JAVA_GRADLE, "Jenkinsfile": "pipeline { agent any }"})
    result = evidence_module.collect(service)
    assert "Jenkinsfile" in result.deterministic["ciFiles"]
    assert "Jenkinsfile" in paths(result)


def test_a_truncated_tree_says_so(service):
    """On a truncated listing an absent path means 'not seen', not 'not there'.
    Anything reasoning about absence has to be able to tell the difference."""
    fake_source.FAKE.load(fake_source.JAVA_GRADLE, truncated=True)
    result = evidence_module.collect(service)
    assert result.coverage["treeTruncated"] is True


# ---------------------------------------------------------------------------
# Monorepos
# ---------------------------------------------------------------------------

def test_a_monorepo_service_sees_only_its_own_directory(app, service):
    """Otherwise a proposal for the payments service is built from the shipping
    service's build.gradle, which would look entirely plausible."""
    with app.app_context():
        row = db.session.get(CiService, service.id)
        row.working_directory = "services/payments"
        db.session.commit()
        fake_source.FAKE.load(fake_source.MONOREPO)
        result = evidence_module.collect(row)

    assert "build.gradle" in paths(result)
    assert result.tree == sorted(["Dockerfile", "build.gradle", "gradlew"])
    assert not any("shipping" in path for path in result.tree)
    # Reads are addressed at the full repository path, not the scoped one.
    assert "services/payments/build.gradle" in fake_source.FAKE.reads


def test_a_working_directory_that_holds_nothing_says_which_directory(app, service):
    with app.app_context():
        row = db.session.get(CiService, service.id)
        row.working_directory = "services/nothing-here"
        db.session.commit()
        fake_source.FAKE.load(fake_source.MONOREPO)
        with pytest.raises(evidence_module.EvidenceError) as exc:
            evidence_module.collect(row)
    assert "services/nothing-here" in str(exc.value)


# ---------------------------------------------------------------------------
# Failure
# ---------------------------------------------------------------------------

def test_an_unconnected_service_is_told_to_connect_a_repository(app, service):
    with app.app_context():
        row = db.session.get(CiService, service.id)
        row.credential_profile_id = None
        db.session.commit()
        with pytest.raises(evidence_module.EvidenceError) as exc:
            evidence_module.collect(row)
    assert "Connect a repository" in str(exc.value)


def test_a_source_host_failure_reaches_the_user_as_its_own_message(service):
    """'Bitbucket rejected this credential' is the whole answer somebody needs.
    Wrapping it in 'analysis failed' would throw that away."""
    fake_source.FAKE.load(fake_source.JAVA_GRADLE)
    fake_source.FAKE.fail_with = "Bitbucket rejected this credential."
    try:
        with pytest.raises(evidence_module.EvidenceError) as exc:
            evidence_module.collect(service)
    finally:
        fake_source.FAKE.fail_with = None
    assert "rejected this credential" in str(exc.value)
