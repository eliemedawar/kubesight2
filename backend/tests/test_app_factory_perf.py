"""App-factory settings that trade nothing visible for CPU: JSON and gzip."""

from __future__ import annotations

import gzip
import json

from flask import jsonify


def test_json_keys_keep_insertion_order(app):
    assert app.json.sort_keys is False
    with app.app_context():
        assert app.json.dumps({"b": 1, "a": 2}).startswith('{"b"')


def test_large_json_is_gzipped_and_round_trips(app):
    payload = {"items": [{"name": f"pod-{i}", "z": i, "a": "x" * 20} for i in range(400)]}

    @app.route("/__test/big-json")
    def big_json():
        return jsonify(payload)

    client = app.test_client()
    response = client.get("/__test/big-json", headers={"Accept-Encoding": "gzip"})
    assert response.status_code == 200
    assert response.headers["Content-Encoding"] == "gzip"
    assert "Accept-Encoding" in response.headers["Vary"]
    body = json.loads(gzip.decompress(response.data))
    assert body == payload
    assert list(body["items"][0].keys()) == ["name", "z", "a"]

    plain = client.get("/__test/big-json")
    assert "Content-Encoding" not in plain.headers
    assert plain.get_json() == payload


def test_gzip_level_comes_from_the_environment(monkeypatch):
    from api import _gzip_level_from_env

    monkeypatch.delenv("RESPONSE_GZIP_LEVEL", raising=False)
    assert _gzip_level_from_env() == 4
    monkeypatch.setenv("RESPONSE_GZIP_LEVEL", "9")
    assert _gzip_level_from_env() == 9
    monkeypatch.setenv("RESPONSE_GZIP_LEVEL", "0")
    assert _gzip_level_from_env() == 1
    monkeypatch.setenv("RESPONSE_GZIP_LEVEL", "fast")
    assert _gzip_level_from_env() == 4
