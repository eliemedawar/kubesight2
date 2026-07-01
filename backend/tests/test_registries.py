"""Tests for linked image registries and the pre-deploy image-availability gate."""

from __future__ import annotations

import io
import json
import urllib.error
from email.message import Message

import pytest

from api.services import registry_client, registry_service
from api.services.deployment_service import check_registry_images
from tests.conftest import auth_headers


# ---------------------------------------------------------------------------
# registry_client — image reference parsing
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "image,expected",
    [
        ("nginx", ("", "library/nginx", "latest")),
        ("nginx:1.25", ("", "library/nginx", "1.25")),
        ("myco.nexus:8083/team/api:v2", ("myco.nexus:8083", "team/api", "v2")),
        ("localhost:5000/foo", ("localhost:5000", "foo", "latest")),
        ("registry.io/a/b/c:tag", ("registry.io", "a/b/c", "tag")),
        ("repo@sha256:abc", ("", "library/repo", "sha256:abc")),
    ],
)
def test_parse_image_reference(image, expected):
    parsed = registry_client.parse_image_reference(image)
    assert (parsed.registry, parsed.repository, parsed.reference) == expected


def test_parse_image_reference_blank():
    assert registry_client.parse_image_reference("  ") is None


@pytest.mark.parametrize(
    "image,host,expected",
    [
        ("registry.old.com/team/app", "registry.new.com", "registry.new.com/team/app"),
        ("nginx", "registry.new.com", "registry.new.com/nginx"),
        ("team/app", "reg:8083", "reg:8083/team/app"),
        ("img:tag", "reg.io", "reg.io/img:tag"),
        ("anything", "", "anything"),  # no host -> unchanged
    ],
)
def test_rewrite_image_host(image, host, expected):
    from api.services.template_resolver import _rewrite_image_host

    assert _rewrite_image_host(image, host) == expected


# ---------------------------------------------------------------------------
# registry_client — Docker V2 HEAD manifest, with a fake urlopen
# ---------------------------------------------------------------------------

class _FakeResp:
    def __init__(self, status, body=b""):
        self.status = status
        self._body = body

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _http_error(code, www_authenticate=None):
    hdrs = Message()
    if www_authenticate:
        hdrs["WWW-Authenticate"] = www_authenticate
    return urllib.error.HTTPError("http://x", code, "err", hdrs, io.BytesIO(b""))


def test_check_manifest_found(monkeypatch):
    monkeypatch.setattr(
        registry_client.urllib.request, "urlopen",
        lambda req, **kw: _FakeResp(200),
    )
    status, _ = registry_client.check_manifest("nexus.example.com", "team/api", "v1")
    assert status == registry_client.FOUND


def test_check_manifest_not_found(monkeypatch):
    def fake(req, **kw):
        raise _http_error(404)

    monkeypatch.setattr(registry_client.urllib.request, "urlopen", fake)
    status, _ = registry_client.check_manifest("nexus.example.com", "team/api", "nope")
    assert status == registry_client.NOT_FOUND


def test_check_manifest_unreachable(monkeypatch):
    def fake(req, **kw):
        raise urllib.error.URLError("connection refused")

    monkeypatch.setattr(registry_client.urllib.request, "urlopen", fake)
    status, msg = registry_client.check_manifest("nexus.example.com", "team/api", "v1")
    assert status == registry_client.UNREACHABLE
    assert "reach" in msg.lower()


def test_ping_ok(monkeypatch):
    monkeypatch.setattr(
        registry_client.urllib.request, "urlopen",
        lambda req, **kw: _FakeResp(200),
    )
    status, msg = registry_client.ping("nexus.example.com", username="u", password="p")
    assert status == registry_client.FOUND
    assert "successful" in msg.lower()


def test_ping_auth_failure(monkeypatch):
    def fake(req, **kw):
        raise _http_error(401)  # no bearer challenge -> credentials rejected

    monkeypatch.setattr(registry_client.urllib.request, "urlopen", fake)
    status, msg = registry_client.ping("nexus.example.com", username="u", password="bad")
    assert status == registry_client.UNREACHABLE
    assert "credential" in msg.lower()


def test_check_manifest_bearer_token_flow(monkeypatch):
    """401 + Bearer challenge -> fetch token -> retry succeeds."""
    calls = {"head": 0}

    def fake(req, **kw):
        # The token fetch is a GET to the realm; everything else is a HEAD.
        if req.get_method() == "GET":
            return _FakeResp(200, json.dumps({"token": "abc123"}).encode())
        calls["head"] += 1
        if calls["head"] == 1:
            raise _http_error(401, www_authenticate='Bearer realm="https://auth/token",service="reg"')
        return _FakeResp(200)

    monkeypatch.setattr(registry_client.urllib.request, "urlopen", fake)
    status, _ = registry_client.check_manifest(
        "nexus.example.com", "team/api", "v1", username="u", password="p"
    )
    assert status == registry_client.FOUND
    assert calls["head"] == 2


# ---------------------------------------------------------------------------
# registry_service — matching + check_image / check_images
# ---------------------------------------------------------------------------

def _make_conn(app, **overrides):
    payload = {
        "name": "Nexus",
        "baseUrl": "nexus.example.com:8083",
        "authMode": "basic",
        "username": "svc",
        "password": "secret",
        "enforcement": "block",
    }
    payload.update(overrides)
    with app.app_context():
        return registry_service.create_connection(payload)


def test_create_and_match_connection(app, monkeypatch):
    _make_conn(app)
    with app.app_context():
        conn = registry_service.match_connection("nexus.example.com:8083")
        assert conn is not None
        assert registry_service.allowed_registry_hosts() == ["nexus.example.com:8083"]


def test_connection_uses_v2_ping(app, monkeypatch):
    conn = _make_conn(app)
    seen = {}

    def fake(req, **kw):
        seen["url"] = req.full_url
        return _FakeResp(200)

    monkeypatch.setattr(registry_client.urllib.request, "urlopen", fake)
    with app.app_context():
        result = registry_service.test_connection(conn["id"])
        assert result["status"] == "ok"
        # Pings the base /v2/ endpoint, not a fake manifest path.
        assert seen["url"].endswith("/v2/")


def test_check_image_no_matching_registry(app):
    _make_conn(app)
    with app.app_context():
        result = registry_service.check_image("docker.io/library/nginx:latest")
        assert result["status"] == "no_connection"


def test_image_host_alias_matches_when_connected_by_ip(app, monkeypatch):
    # Connected by IP, but images are pulled by hostname -> declare the alias.
    _make_conn(app, baseUrl="10.4.23.182", imageHosts=["registry.areeba.com"])
    monkeypatch.setattr(
        registry_client.urllib.request, "urlopen",
        lambda req, **kw: _FakeResp(200),
    )
    with app.app_context():
        conn = registry_service.match_connection("registry.areeba.com")
        assert conn is not None
        result = registry_service.check_image("registry.areeba.com/team/api:1.0")
        assert result["status"] == "found"
        assert "registry.areeba.com" in registry_service.allowed_registry_hosts()


def test_image_host_alias_accepts_comma_string(app):
    conn = _make_conn(app, baseUrl="10.4.23.182", imageHosts="registry.areeba.com, reg2.areeba.com")
    with app.app_context():
        assert set(conn["imageHosts"]) == {"registry.areeba.com", "reg2.areeba.com"}
        assert registry_service.match_connection("reg2.areeba.com") is not None


def test_check_images_blocks_missing(app, monkeypatch):
    _make_conn(app, enforcement="block")

    def fake(req, **kw):
        raise _http_error(404)

    monkeypatch.setattr(registry_client.urllib.request, "urlopen", fake)
    with app.app_context():
        checks, blocking = registry_service.check_images(
            ["nexus.example.com:8083/team/api:missing"]
        )
        assert blocking is True
        assert checks[0]["status"] == "not_found"


def test_check_images_warn_does_not_block(app, monkeypatch):
    _make_conn(app, enforcement="warn")

    def fake(req, **kw):
        raise _http_error(404)

    monkeypatch.setattr(registry_client.urllib.request, "urlopen", fake)
    with app.app_context():
        _, blocking = registry_service.check_images(
            ["nexus.example.com:8083/team/api:missing"]
        )
        assert blocking is False


# ---------------------------------------------------------------------------
# Deploy gate — check_registry_images over a manifest
# ---------------------------------------------------------------------------

_DEPLOY_YAML = """
apiVersion: apps/v1
kind: Deployment
metadata:
  name: api
spec:
  template:
    spec:
      containers:
        - name: api
          image: nexus.example.com:8083/team/api:missing
"""


def test_deploy_gate_blocks_missing_image(app, monkeypatch):
    _make_conn(app, enforcement="block")

    def fake(req, **kw):
        raise _http_error(404)

    monkeypatch.setattr(registry_client.urllib.request, "urlopen", fake)
    with app.app_context():
        checks, blocking, message = check_registry_images(_DEPLOY_YAML)
        assert blocking is True
        assert "not found" in message.lower()
        assert any(c["status"] == "not_found" for c in checks)


def test_deploy_gate_allows_present_image(app, monkeypatch):
    _make_conn(app, enforcement="block")
    monkeypatch.setattr(
        registry_client.urllib.request, "urlopen",
        lambda req, **kw: _FakeResp(200),
    )
    with app.app_context():
        _, blocking, message = check_registry_images(_DEPLOY_YAML)
        assert blocking is False
        assert message is None


def test_change_bundle_item_gate_marks_invalid(app, monkeypatch):
    """Staging an apply item with a missing image marks it invalid (block)."""
    from api.models import ChangeBundleItem
    from api.services.change_bundle_service import _validate_item_now

    _make_conn(app, enforcement="block")

    def fake(req, **kw):
        raise _http_error(404)

    monkeypatch.setattr(registry_client.urllib.request, "urlopen", fake)
    with app.app_context():
        item = ChangeBundleItem(
            namespace="default",
            resource_kind="Deployment",
            resource_name="api",
            yaml_preview=_DEPLOY_YAML,
            new_payload_json={"execution": {"mode": "apply"}},
        )
        _validate_item_now(item)
        assert item.validation_status == "invalid"
        assert "not found" in (item.validation_message or "").lower()


# ---------------------------------------------------------------------------
# Routes — CRUD + RBAC
# ---------------------------------------------------------------------------

def test_registry_crud_routes(client, admin_token):
    headers = auth_headers(admin_token)
    resp = client.post(
        "/api/registries",
        json={"name": "Nexus", "baseUrl": "nexus.example.com:8083", "username": "svc", "password": "p"},
        headers=headers,
    )
    assert resp.status_code == 201, resp.get_json()
    created = resp.get_json()["data"]
    assert created["passwordConfigured"] is True
    assert "password" not in created  # secret never serialized back

    conn_id = created["id"]
    resp = client.get("/api/registries", headers=headers)
    assert resp.status_code == 200
    assert any(item["id"] == conn_id for item in resp.get_json()["data"]["items"])

    resp = client.put(
        f"/api/registries/{conn_id}",
        json={"enforcement": "warn"},
        headers=headers,
    )
    assert resp.status_code == 200
    assert resp.get_json()["data"]["enforcement"] == "warn"

    resp = client.delete(f"/api/registries/{conn_id}", headers=headers)
    assert resp.status_code == 200


def test_registry_routes_require_permission(client, viewer_token):
    headers = auth_headers(viewer_token)
    # Viewer lacks registries:manage.
    resp = client.post(
        "/api/registries",
        json={"name": "Nexus", "baseUrl": "x", "username": "u", "password": "p"},
        headers=headers,
    )
    assert resp.status_code == 403


def test_check_image_route(client, admin_token, monkeypatch):
    headers = auth_headers(admin_token)
    client.post(
        "/api/registries",
        json={"name": "Nexus", "baseUrl": "nexus.example.com:8083", "username": "svc", "password": "p"},
        headers=headers,
    )
    monkeypatch.setattr(
        registry_client.urllib.request, "urlopen",
        lambda req, **kw: _FakeResp(200),
    )
    resp = client.post(
        "/api/registries/check-image",
        json={"image": "nexus.example.com:8083/team/api:v1"},
        headers=headers,
    )
    assert resp.status_code == 200
    assert resp.get_json()["data"]["status"] == "found"
