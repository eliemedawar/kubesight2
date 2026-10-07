"""Tiny stdlib HTTP client for the KubeSight API (no third-party packages)."""

from __future__ import annotations

import gzip
import http.client
import json
import time
from typing import Any, Dict, Optional, Tuple
from urllib.parse import urlencode


class KubeSight:
    def __init__(self, base: str = "http://127.0.0.1:5091", token: Optional[str] = None, timeout: float = 120.0):
        if base.startswith("http://"):
            base = base[len("http://"):]
        host, _, port = base.partition(":")
        self.host = host
        self.port = int(port or 80)
        self.token = token
        self.timeout = timeout

    def request(self, method: str, path: str, body: Any = None, params: Optional[Dict[str, Any]] = None,
                headers: Optional[Dict[str, str]] = None) -> Tuple[int, Any, float, int]:
        """Returns (status, parsed body or text, elapsed ms, response bytes). Never raises on HTTP errors."""
        if params:
            path = f"{path}?{urlencode(params)}"
        hdrs = {"Accept": "application/json", "Accept-Encoding": "gzip"}
        if self.token:
            hdrs["Authorization"] = f"Bearer {self.token}"
        data = None
        if body is not None:
            data = json.dumps(body).encode()
            hdrs["Content-Type"] = "application/json"
        if headers:
            hdrs.update(headers)
        started = time.perf_counter()
        conn = http.client.HTTPConnection(self.host, self.port, timeout=self.timeout)
        try:
            conn.request(method, path, body=data, headers=hdrs)
            resp = conn.getresponse()
            raw = resp.read()
            status = resp.status
            encoding = resp.getheader("Content-Encoding", "")
        finally:
            conn.close()
        elapsed = (time.perf_counter() - started) * 1000
        size = len(raw)
        if encoding == "gzip":
            raw = gzip.decompress(raw)
        try:
            parsed: Any = json.loads(raw) if raw else None
        except ValueError:
            parsed = raw.decode("utf-8", "replace")
        return status, parsed, elapsed, size

    def get(self, path: str, **params: Any):
        return self.request("GET", path, params=params or None)

    def post(self, path: str, body: Any = None):
        return self.request("POST", path, body=body)

    def login(self, username: str, password: str) -> str:
        status, body, _, _ = self.post("/api/auth/login", {"username": username, "password": password})
        if status != 200:
            raise RuntimeError(f"login failed for {username}: {status} {body}")
        data = (body or {}).get("data") or {}
        token = data.get("token") or data.get("accessToken") or (data.get("session") or {}).get("token")
        if not token:
            raise RuntimeError(f"login for {username} returned no token: {json.dumps(body)[:400]}")
        self.token = token
        return token
