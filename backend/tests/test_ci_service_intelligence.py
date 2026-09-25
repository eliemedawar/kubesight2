"""Application Intelligence as a tab of a CI service: one repository, one place."""

from __future__ import annotations

from api.db import db
from api.models_application_intelligence import (
    BitbucketCredentialProfile,
    IntelligenceApplication,
)
from api.models_ci import CiService
from api.secret_encryption import encrypt_secret
from tests.conftest import auth_headers


def _credential(app, *, read_only=True, name="ci-read-only"):
    with app.app_context():
        row = BitbucketCredentialProfile(
            name=name,
            provider="bitbucket",
            credential_type="repository_access_token",
            secret_cipher=encrypt_secret("test-token"),
            read_only=read_only,
            enabled=True,
        )
        db.session.add(row)
        db.session.commit()
        return row.id


def _service(client, token, credential_id=None):
    response = client.post(
        "/api/ci/services",
        json={"name": "Acquiring UI", "applicationType": "container"},
        headers=auth_headers(token),
    )
    assert response.status_code == 201
    service_id = response.get_json()["data"]["id"]
    if credential_id:
        response = client.put(
            f"/api/ci/services/{service_id}/source",
            json={
                "repositoryUrl": "https://bitbucket.org/areebasal/acquiring-app",
                "defaultBranch": "2.12.58",
                "workingDirectory": "web",
                "credentialProfileId": credential_id,
            },
            headers=auth_headers(token),
        )
        assert response.status_code == 200
    return service_id


def _url(service_id):
    return f"/api/ci/services/{service_id}/intelligence"


def test_nothing_until_enabled_then_created_from_the_service_source(app, client, admin_token):
    service_id = _service(client, admin_token, _credential(app))

    empty = client.get(_url(service_id), headers=auth_headers(admin_token))
    assert empty.status_code == 200
    assert empty.get_json()["data"] == {
        "application": None,
        "linked": False,
        "sourceConfigured": True,
    }

    enabled = client.post(_url(service_id), headers=auth_headers(admin_token))
    assert enabled.status_code == 200
    data = enabled.get_json()["data"]
    application = data["application"]
    assert data["linked"] is True
    assert application["ciServiceId"] == service_id
    assert application["repositoryWorkspace"] == "areebasal"
    assert application["repositoryName"] == "acquiring-app"
    assert application["defaultBranch"] == "2.12.58"
    assert application["repositorySubdirectory"] == "web"

    with app.app_context():
        assert db.session.get(CiService, service_id).intelligence_application_id == application["id"]

    # Enabling twice is the same application, not a second one.
    again = client.post(_url(service_id), headers=auth_headers(admin_token))
    assert again.get_json()["data"]["application"]["id"] == application["id"]
    with app.app_context():
        assert IntelligenceApplication.query.count() == 1


def test_an_earlier_analysis_of_the_same_repository_is_offered_then_linked(
    app, client, admin_token
):
    credential_id = _credential(app)
    earlier = client.post(
        "/api/applications",
        headers=auth_headers(admin_token),
        json={
            "name": "Acquiring App",
            "repositoryUrl": "https://bitbucket.org/AreebaSAL/Acquiring-App",
            "credentialProfileId": credential_id,
        },
    ).get_json()["data"]
    service_id = _service(client, admin_token, credential_id)

    offered = client.get(_url(service_id), headers=auth_headers(admin_token)).get_json()["data"]
    assert offered["application"]["id"] == earlier["id"]
    assert offered["linked"] is False

    linked = client.post(_url(service_id), headers=auth_headers(admin_token)).get_json()["data"]
    assert linked["application"]["id"] == earlier["id"]
    assert linked["linked"] is True
    assert linked["application"]["ciServiceId"] == service_id


def test_refuses_without_a_source_or_with_a_writable_credential(app, client, admin_token):
    no_source = _service(client, admin_token)
    response = client.post(_url(no_source), headers=auth_headers(admin_token))
    assert response.status_code == 400
    assert "Source tab" in response.get_json()["error"]

    writable = _credential(app, read_only=False, name="ci-writable")
    service_id = client.post(
        "/api/ci/services",
        json={"name": "Writable", "applicationType": "container"},
        headers=auth_headers(admin_token),
    ).get_json()["data"]["id"]
    client.put(
        f"/api/ci/services/{service_id}/source",
        json={
            "repositoryUrl": "https://bitbucket.org/areebasal/writable",
            "credentialProfileId": writable,
        },
        headers=auth_headers(admin_token),
    )
    response = client.post(_url(service_id), headers=auth_headers(admin_token))
    assert response.status_code == 400
    assert "read-only" in response.get_json()["error"]


def test_deleting_the_application_keeps_the_service(app, client, admin_token):
    service_id = _service(client, admin_token, _credential(app))
    application_id = client.post(
        _url(service_id), headers=auth_headers(admin_token)
    ).get_json()["data"]["application"]["id"]

    deleted = client.delete(
        f"/api/applications/{application_id}", headers=auth_headers(admin_token)
    )
    assert deleted.status_code == 200
    with app.app_context():
        service = db.session.get(CiService, service_id)
        assert service is not None
        assert service.intelligence_application_id is None


def test_unknown_service_is_404(client, admin_token):
    assert client.get(_url(999999), headers=auth_headers(admin_token)).status_code == 404
