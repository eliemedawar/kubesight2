"""Minimal Jenkins client for the deploy-automation ROUTER pipeline.

KubeSight only ever talks to ONE Jenkins job: a router pipeline (maintained by
the DevOps team) that receives APP_NAME / NAMESPACE / IMAGE_TAG / TICKET,
dispatches to the correct downstream build job, waits on it and propagates its
result. KubeSight therefore needs just three operations: trigger the router
(``buildWithParameters``), resolve the returned queue item to a build, and poll
that build's result. See DEPLOY-AUTOMATION-PLAN.md §1 for the contract.

Auth is a Jenkins user + API token over HTTP Basic — token auth is exempt from
Jenkins CSRF crumbs, so no crumb dance is needed. Only the standard library is
used (urllib), matching zoho_client / registry_client.
"""

from __future__ import annotations

import base64
import json
import ssl
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any, Dict, Optional, Tuple
from urllib.parse import quote, urlencode

_TIMEOUT_SECONDS = 15

# Router build results KubeSight treats as success / failure. ABORTED and
# UNSTABLE both count as failure — the image must provably exist afterwards.
SUCCESS_RESULTS = {"SUCCESS"}


class JenkinsError(Exception):
    """A Jenkins API call failed. ``status`` mirrors the HTTP code when known."""

    def __init__(self, message: str, status: Optional[int] = None):
        super().__init__(message)
        self.status = status


@dataclass
class JenkinsConfig:
    """Everything a router call needs, with the API token already decrypted."""

    base_url: str
    username: str
    api_token: str
    router_job_path: str
    verify_tls: bool = True


def job_url(cfg: JenkinsConfig) -> str:
    """Absolute URL of the router job: ``folder/router`` → ``/job/folder/job/router``."""
    segments = [s for s in (cfg.router_job_path or "").split("/") if s.strip()]
    if not segments:
        raise JenkinsError("No router job path configured.")
    path = "/".join(f"job/{quote(s, safe='')}" for s in segments)
    return f"{cfg.base_url.rstrip('/')}/{path}"


def _ssl_context(cfg: JenkinsConfig) -> Optional[ssl.SSLContext]:
    if cfg.verify_tls:
        return None
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    return ctx


def _headers(cfg: JenkinsConfig) -> Dict[str, str]:
    if not (cfg.username and cfg.api_token):
        raise JenkinsError("Jenkins username / API token are not configured.")
    raw = f"{cfg.username}:{cfg.api_token}".encode("utf-8")
    return {"Authorization": f"Basic {base64.b64encode(raw).decode('ascii')}"}


def _request(
    cfg: JenkinsConfig,
    method: str,
    url: str,
    *,
    body: Optional[bytes] = None,
) -> Tuple[int, Dict[str, str], Dict[str, Any]]:
    """One HTTP call. Returns (status, lowercased-headers, parsed-json-or-{})."""
    req = urllib.request.Request(url, data=body, headers=_headers(cfg), method=method)
    try:
        with urllib.request.urlopen(req, timeout=_TIMEOUT_SECONDS, context=_ssl_context(cfg)) as resp:
            raw = resp.read().decode("utf-8", "replace")
            headers = {k.lower(): v for k, v in resp.headers.items()}
            try:
                parsed = json.loads(raw) if raw.strip() else {}
            except ValueError:
                parsed = {}
            return resp.status, headers, parsed
    except urllib.error.HTTPError as exc:
        detail = ""
        try:
            detail = (exc.read() or b"").decode("utf-8", "replace")[:300]
        except Exception:
            pass
        if exc.code == 401:
            raise JenkinsError("Jenkins rejected the credentials (401).", 401) from exc
        if exc.code == 403:
            raise JenkinsError(
                "Jenkins refused the request (403) — check the user's build permission on the router job.",
                403,
            ) from exc
        if exc.code == 404:
            raise JenkinsError("Jenkins returned 404 — check the base URL and router job path.", 404) from exc
        raise JenkinsError(f"Jenkins call failed (HTTP {exc.code}). {detail}".strip(), exc.code) from exc
    except urllib.error.URLError as exc:
        raise JenkinsError(f"Could not reach Jenkins ({exc.reason}).") from exc
    except TimeoutError as exc:
        raise JenkinsError("Jenkins request timed out.") from exc


def test_connection(cfg: JenkinsConfig) -> Dict[str, Any]:
    """Verify auth + that the router job exists. Returns its display name/URL."""
    _, _, payload = _request(cfg, "GET", f"{job_url(cfg)}/api/json?tree=fullDisplayName,url,buildable")
    if payload.get("buildable") is False:
        raise JenkinsError("The router job exists but is disabled (not buildable).")
    return {
        "job": payload.get("fullDisplayName") or cfg.router_job_path,
        "url": payload.get("url") or job_url(cfg),
    }


def trigger_build(cfg: JenkinsConfig, params: Dict[str, str]) -> str:
    """POST buildWithParameters on the router. Returns the queue item URL."""
    body = urlencode({k: str(v) for k, v in params.items()}).encode("ascii")
    url = f"{job_url(cfg)}/buildWithParameters"
    req = urllib.request.Request(
        url,
        data=body,
        headers={**_headers(cfg), "Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=_TIMEOUT_SECONDS, context=_ssl_context(cfg)) as resp:
            if resp.status not in (200, 201):
                raise JenkinsError(f"Jenkins did not accept the build (HTTP {resp.status}).", resp.status)
            location = resp.headers.get("Location") or ""
    except urllib.error.HTTPError as exc:
        if exc.code == 400:
            raise JenkinsError(
                "Jenkins rejected the parameters (400) — the router job may not be parameterized "
                "with APP_NAME/NAMESPACE/IMAGE_TAG/TICKET yet.",
                400,
            ) from exc
        raise JenkinsError(f"Triggering the router build failed (HTTP {exc.code}).", exc.code) from exc
    except urllib.error.URLError as exc:
        raise JenkinsError(f"Could not reach Jenkins ({exc.reason}).") from exc

    if not location:
        raise JenkinsError("Jenkins accepted the build but returned no queue location header.")
    return location.rstrip("/")


def queue_state(cfg: JenkinsConfig, queue_url: str) -> Dict[str, Any]:
    """State of a queue item. Returns one of:

    - ``{"state": "pending", "why": str}`` — still waiting for an executor
    - ``{"state": "cancelled"}`` — removed from the queue without building
    - ``{"state": "building", "buildNumber": int, "buildUrl": str}``
    """
    _, _, payload = _request(cfg, "GET", f"{queue_url}/api/json")
    if payload.get("cancelled"):
        return {"state": "cancelled"}
    executable = payload.get("executable") or {}
    if executable.get("number"):
        return {
            "state": "building",
            "buildNumber": int(executable["number"]),
            "buildUrl": str(executable.get("url") or "").rstrip("/"),
        }
    return {"state": "pending", "why": payload.get("why") or ""}


def build_state(cfg: JenkinsConfig, build_url: str) -> Dict[str, Any]:
    """Result of a build: ``{"building": bool, "result": str|None, "durationMs": int}``."""
    _, _, payload = _request(cfg, "GET", f"{build_url}/api/json?tree=building,result,duration,url")
    return {
        "building": bool(payload.get("building")),
        "result": payload.get("result"),
        "durationMs": int(payload.get("duration") or 0),
        "url": payload.get("url") or build_url,
    }
