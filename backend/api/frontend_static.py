"""Serve the built Vite SPA from Flask so browsers can use http://127.0.0.1:5000/."""

from __future__ import annotations

import json
import os
from pathlib import Path

from flask import Flask, Response, send_file, send_from_directory

DIST_DIR = Path(__file__).resolve().parents[2] / "frontend" / "dist"


def frontend_dist_available() -> bool:
    return (DIST_DIR / "index.html").is_file()


def register_frontend_static(app: Flask) -> None:
    if not frontend_dist_available():
        return

    @app.get("/config.js")
    def serve_frontend_config():
        public_api = os.getenv("PUBLIC_API_URL", "").strip()
        payload = {"backendUrl": public_api}
        body = f"window.APP_CONFIG = {json.dumps(payload)};\n"
        return Response(body, mimetype="application/javascript")

    @app.get("/assets/<path:asset_path>")
    def serve_frontend_assets(asset_path: str):
        return send_from_directory(DIST_DIR / "assets", asset_path)

    @app.get("/")
    def serve_spa_index():
        return send_file(DIST_DIR / "index.html")
