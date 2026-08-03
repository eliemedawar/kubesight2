"""The SPA history fallback.

This is the bug class the test suite exists for: invisible in development,
broken only in what ships. The Vite dev server does history fallback itself, so
every deep link works locally while every deep link 404s in the Flask-served
build. Nobody notices until a customer bookmarks a page.
"""

from __future__ import annotations

import pytest

from api import create_app
from api.testing_config import TestingConfig

INDEX_HTML = "<!doctype html><html><body><div id=root></div></body></html>"


@pytest.fixture()
def spa_client(tmp_path, monkeypatch):
    """An app whose SPA routes are registered against a throwaway dist.

    `register_frontend_static` returns early when no build is present, so the
    dist has to exist *before* create_app runs -- patching afterwards would
    register nothing and quietly pass every assertion below.
    """
    from api import frontend_static

    dist = tmp_path / "dist"
    (dist / "assets").mkdir(parents=True)
    (dist / "index.html").write_text(INDEX_HTML, encoding="utf-8")
    (dist / "assets" / "app.js").write_text("// built asset\n", encoding="utf-8")

    monkeypatch.setattr(frontend_static, "DIST_DIR", dist)

    app = create_app(TestingConfig)
    return app.test_client()


@pytest.mark.parametrize(
    "path",
    [
        "/fleet/clusters",
        "/fleet/clusters/prod-us-east",
        "/integrations/jira/configuration",
        "/admin/users",
        "/workloads/prod-us-east/default",
    ],
)
def test_deep_links_serve_the_spa(spa_client, path):
    """F5 on a real route returns the app, not a Flask 404."""
    response = spa_client.get(path)
    assert response.status_code == 200
    assert b"<div id=root>" in response.data


def test_root_still_serves_the_spa(spa_client):
    assert spa_client.get("/").status_code == 200


def test_unknown_api_paths_stay_json(spa_client):
    """The fallback must not swallow API 404s.

    Serving HTML here turns every client-side error path into a parse failure,
    at the moment the caller is already handling a problem.
    """
    response = spa_client.get("/api/definitely-not-a-route")
    assert response.status_code == 404
    assert response.is_json
    assert b"<div id=root>" not in response.data


def test_real_api_routes_are_not_shadowed(spa_client):
    """A registered blueprint route still answers, unauthenticated included.

    Werkzeug ranks static rules above `<path:>` converters, so registration
    order does not matter -- but that is a property worth asserting rather than
    trusting, since getting it wrong makes every API call return HTML.
    """
    response = spa_client.get("/api/integrations")
    assert response.status_code == 401
    assert response.is_json


def test_health_stays_json(spa_client):
    response = spa_client.get("/health")
    assert response.is_json


def test_assets_are_served_not_swallowed(spa_client):
    response = spa_client.get("/assets/app.js")
    assert response.status_code == 200
    assert b"built asset" in response.data


def test_missing_asset_does_not_return_the_index(spa_client):
    """A 404 for a missing bundle must stay a 404.

    If it returned index.html with status 200 the browser would try to execute
    HTML as JavaScript, and the failure would read as a syntax error in the
    bundle rather than a missing file.
    """
    response = spa_client.get("/assets/does-not-exist.js")
    assert response.status_code == 404
    assert b"<div id=root>" not in response.data
