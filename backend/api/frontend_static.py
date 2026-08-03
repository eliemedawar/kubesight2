"""Serve the built Vite SPA from Flask so browsers can use http://127.0.0.1:5000/."""

from __future__ import annotations

import json
import os
from pathlib import Path

from flask import Flask, Response, abort, send_file, send_from_directory

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

    @app.get("/<path:spa_path>")
    def serve_spa_fallback(spa_path: str):
        """History fallback: the router owns URLs, so Flask must not 404 them.

        Without this the app has real URLs that only work if you arrive by
        clicking. A bookmark, a shared link, or F5 on /fleet/clusters returns a
        Flask 404. The Vite dev server does this fallback itself, so it is
        invisible in development and broken only in the Flask-served build --
        the one that ships.

        API and health keep returning JSON, including their 404s. Serving HTML
        there would turn every client-side error path into a parse failure, at
        the moment a caller is already handling a problem.

        Werkzeug ranks static rules above `<path:>` converters, so a registered
        blueprint route wins regardless of registration order; only genuinely
        unmatched paths arrive here.
        """
        if spa_path.startswith(("api/", "health")):
            abort(404)
        return send_file(DIST_DIR / "index.html")
