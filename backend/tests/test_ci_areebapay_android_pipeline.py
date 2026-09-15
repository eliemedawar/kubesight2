"""The areebapay-v2 Android port, driven end to end.

``tools/seed_areebapay_android_pipeline.py`` carries the Android half of the
areebapay-v2 Jenkins job — the APK and AAB stages, with the Mac agent and every
iOS stage left behind. These tests drive that definition through the real API
and the mock runner, so the port and the engine cannot drift apart without
something here going red.

They also pin the four Jenkins bugs the port deliberately does not reproduce:
if someone "restores fidelity" later, the test says which bug they restored.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

from api.db import db
from api.models_application_intelligence import BitbucketCredentialProfile
from api.models_ci import CiBuild
from api.secret_encryption import encrypt_secret
from tests.conftest import auth_headers


def _seed_module():
    path = (
        Path(__file__).resolve().parents[1]
        / "tools"
        / "seed_areebapay_android_pipeline.py"
    )
    spec = importlib.util.spec_from_file_location("seed_areebapay_android", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture()
def service_id(app, client, admin_token):
    """An Android service with source connected, ready for the pipeline."""
    with app.app_context():
        credential = BitbucketCredentialProfile(
            name="areebapay-token",
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
        json={"name": "AreebaPay Android", "applicationType": "android"},
        headers=auth_headers(admin_token),
    ).get_json()["data"]["id"]

    client.put(
        f"/api/ci/services/{created}/source",
        json={
            "repositoryUrl": "https://bitbucket.org/areebasal/areebapay-v2.git",
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


def _statuses(client, admin_token, build_id):
    stages = client.get(
        f"/api/ci/builds/{build_id}", headers=auth_headers(admin_token)
    ).get_json()["data"]["stages"]
    return {stage["name"]: stage["status"] for stage in stages}


def _save_seeded_pipeline(client, admin_token, service_id, seed):
    """The seed's own ordering: secrets first, then the pipeline that refs them."""
    for key, value in seed.PLACEHOLDER_SECRETS.items():
        assert (
            client.post(
                f"/api/ci/services/{service_id}/secrets",
                json={"key": key, "value": value},
                headers=auth_headers(admin_token),
            ).status_code
            == 201
        ), key

    pipeline_id = _pipeline_id(client, admin_token, service_id)
    stages = [
        {**stage, "runnerLabels": ["mock"]}
        for stage in seed.build_stages(seed.DEFAULT_IMAGE)
    ]
    response = client.put(
        f"/api/ci/pipelines/{pipeline_id}",
        json={"parameters": seed.PARAMETERS, "stages": stages},
        headers=auth_headers(admin_token),
    )
    assert response.status_code == 200, response.get_json()
    return response.get_json()["data"]


def test_the_android_pipeline_saves_with_its_inputs_and_gates(
    app, client, admin_token, service_id
):
    seed = _seed_module()
    saved = _save_seeded_pipeline(client, admin_token, service_id, seed)

    assert [stage["name"] for stage in saved["stages"]] == [
        "Checkout",
        "Resolve version",
        "Write configuration",
        "Install dependencies",
        "Build APK",
        "Build AAB",
    ]
    gated = {
        stage["name"]: stage["runCondition"]["variable"]
        for stage in saved["stages"]
        if stage["runCondition"]
    }
    assert gated == {"Build APK": "BuildOnlyApk", "Build AAB": "BuildOnlyAab"}

    # The APK and the AAB are what Mobile Applications ingests, so their
    # declared types are part of the contract, not decoration.
    types = {
        stage["name"]: [spec["type"] for spec in stage["artifacts"]]
        for stage in saved["stages"]
        if stage["artifacts"]
    }
    assert types == {"Build APK": ["apk"], "Build AAB": ["aab"]}


def test_building_only_the_aab_skips_the_apk_stage(app, client, admin_token, service_id):
    """The Jenkins ``when { equals expected: 'true', actual: BuildOnlyAab }``."""
    seed = _seed_module()
    _save_seeded_pipeline(client, admin_token, service_id, seed)

    build_id = client.post(
        f"/api/ci/services/{service_id}/builds",
        json={
            "branch": "master",
            "variables": {"BuildOnlyAab": "true", "BuildOnlyApk": "false"},
        },
        headers=auth_headers(admin_token),
    ).get_json()["data"]["id"]
    _drain(app)

    statuses = _statuses(client, admin_token, build_id)
    assert statuses["Write configuration"] == "success"
    assert statuses["Install dependencies"] == "success"
    assert statuses["Build AAB"] == "success"
    assert statuses["Build APK"] == "skipped"


def test_building_only_the_apk_skips_the_aab_stage(app, client, admin_token, service_id):
    seed = _seed_module()
    _save_seeded_pipeline(client, admin_token, service_id, seed)

    build_id = client.post(
        f"/api/ci/services/{service_id}/builds",
        json={
            "branch": "master",
            "variables": {"BuildOnlyAab": "false", "BuildOnlyApk": "true"},
        },
        headers=auth_headers(admin_token),
    ).get_json()["data"]["id"]
    _drain(app)

    statuses = _statuses(client, admin_token, build_id)
    assert statuses["Build APK"] == "success"
    assert statuses["Build AAB"] == "skipped"


def test_the_version_and_version_code_are_exported_once(app):
    """Jenkins' ``script { Build_Version = sh(...) }``, as one exported pair.

    Both Gradle stages read APP_VERSION and VERSION_CODE rather than recomputing
    them, which is what keeps an APK and an AAB from the same build from
    disagreeing about what they are.
    """
    seed = _seed_module()
    stages = seed.build_stages(seed.DEFAULT_IMAGE)
    resolve = next(s for s in stages if s["name"] == "Resolve version")

    exported = "\n".join(resolve["commands"])
    assert 'APP_VERSION=%s\\n" "$VERSION" >> "$KUBESIGHT_ENV"' in exported
    assert 'VERSION_CODE=%s\\n" "$VERSION_CODE" >> "$KUBESIGHT_ENV"' in exported
    # The Jenkins 700 + BUILD_NUMBER, with the base made an input because
    # KubeSight numbers builds per service from 1.
    assert "${VERSION_CODE_OFFSET:-700} + KUBESIGHT_BUILD_NUMBER" in exported

    for stage in stages:
        if stage["name"].startswith("Build "):
            body = "\n".join(stage["commands"])
            assert '-PversionCode="$VERSION_CODE"' in body, stage["name"]
            assert '-PversionName="$APP_VERSION"' in body, stage["name"]


def test_both_gradle_stages_inject_the_same_keystore(app):
    """The Jenkins job signed only the AAB; the APK fell through to a Mac path.

    ``/Users/devops/jenkins/.certs/...`` resolves to nothing on a build pod, so
    the port injects the keystore for both tasks from the same secret.
    """
    seed = _seed_module()
    stages = seed.build_stages(seed.DEFAULT_IMAGE)
    gradle = [s for s in stages if s["name"].startswith("Build ")]
    assert len(gradle) == 2

    for stage in gradle:
        body = "\n".join(stage["commands"])
        assert "/Users/devops" not in body, stage["name"]
        assert "base64 -d" in body, stage["name"]
        assert "-Pandroid.injected.signing.store.file=" in body, stage["name"]
        refs = {ref["name"] for ref in stage["secretRefs"]}
        assert refs == {
            "ANDROID_KEYSTORE_B64",
            "ANDROID_STORE_PASS",
            "ANDROID_KEY_ALIAS",
            "ANDROID_KEY_PASS",
        }, stage["name"]


def test_the_jenkins_bugs_are_not_carried_over(app):
    """The four defects the port fixes, each pinned to the line that fixes it."""
    seed = _seed_module()
    stages = seed.build_stages(seed.DEFAULT_IMAGE)
    config = next(s for s in stages if s["name"] == "Write configuration")
    body = "\n".join(config["commands"])

    # 1. `> .nmprc` — misspelt, so the npmrc never applied.
    assert ".nmprc" not in body
    assert "> .npmrc" in body

    # 2. envpreprod was written to .env.uat, and .env.preprod never existed.
    assert "printf %s \"$ENV_PREPROD\" > .env.preprod" in body

    # 3. `cat .env` printed an RSA private key into the build log.
    assert "cat .env" not in body
    assert "wc -l < .env" in body

    # 4. `echo '${envuat}'` breaks on a single quote and mangles PEM backslashes.
    for secret in ("ENV_UAT", "ENV_PREPROD", "ENV_PROD"):
        assert f'printf %s "${secret}"' in body

    # And the fifth, which is a property of the stage rather than a command:
    # BUILD_APK_ONLY swallowed its exception, so a failed APK build was green.
    for stage in stages:
        assert stage.get("continueOnFailure") is not True, stage["name"]


def test_no_stage_asks_for_a_mac(app):
    """The whole point of the port: nothing here needs the Mac agent."""
    seed = _seed_module()
    for stage in seed.build_stages(seed.DEFAULT_IMAGE):
        labels = stage.get("runnerLabels") or []
        assert "macos" not in labels, stage["name"]
        assert "mac" not in labels, stage["name"]
        body = "\n".join(stage.get("commands") or [])
        for mac_only in ("security unlock-keychain", "pod install", "xcrun", "fastlane"):
            assert mac_only not in body, "%s: %s" % (stage["name"], mac_only)
