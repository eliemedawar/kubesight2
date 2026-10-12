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


def test_file_sqlite_runs_in_wal_with_a_busy_timeout(tmp_path):
    from sqlalchemy import text

    from api import create_app
    from api.db import db

    class FileConfig:
        TESTING = True
        SQLALCHEMY_DATABASE_URI = "sqlite:///" + str(tmp_path / "dev.db").replace("\\", "/")
        SQLALCHEMY_TRACK_MODIFICATIONS = False
        JWT_SECRET_KEY = "x" * 40

    application = create_app(FileConfig)
    with application.app_context():
        with db.engine.connect() as conn:
            assert conn.execute(text("PRAGMA journal_mode")).scalar().lower() == "wal"
            assert conn.execute(text("PRAGMA busy_timeout")).scalar() == 30000
        db.engine.dispose()


def test_memory_sqlite_is_left_alone():
    from api import _is_sqlite_file

    assert not _is_sqlite_file("sqlite:///:memory:")
    assert not _is_sqlite_file("sqlite://")
    assert not _is_sqlite_file("postgresql://u@h/db")
    assert _is_sqlite_file("sqlite:///kubesight.db")
