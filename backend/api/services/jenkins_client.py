"""Minimal Jenkins client for the deploy-automation ROUTER pipeline.

KubeSight only ever talks to ONE Jenkins job: a router pipeline (maintained by
the DevOps team) that receives ``APP`` / ``TAG`` / ``NAMESPACE``, dispatches to
the correct downstream build job, waits on it and propagates its result.
KubeSight therefore needs just three operations: trigger the router
(``buildWithParameters``), resolve the returned queue item to a build, and poll
that build's result. Verified live contract (2026-07-08)::

    POST {base}/job/{router}/buildWithParameters
      --user <user>:<api token>          (HTTP Basic)
      token=<remote-trigger token>       (job-level, optional)
      APP=processing-issuing  TAG=1.73.13  NAMESPACE=verto-uat

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
from urllib.parse import quote, urlencode, urlsplit

_TIMEOUT_SECONDS = 15


class _NoFollowRedirect(urllib.request.HTTPRedirectHandler):
    """Never auto-follow redirects. ``buildWithParameters`` returns 201 (modern
    Jenkins) or a 30x whose ``Location`` we read ourselves — if a proxy (Tyk)
    answers with a 307/308, urllib's default handler would transparently re-POST
    it and queue the build TWICE. Returning None here disables that."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _rebase(cfg: "JenkinsConfig", url: str) -> str:
    """Re-point a Jenkins-returned absolute URL onto the configured base URL when
    its host differs from ours.

    Jenkins builds queue/build URLs from its own configured root URL, which is
    often an internal address the backend can't reach through the gateway →
    polling times out. We keep only the path and re-attach it to ``base_url``
    (the endpoint we already reached for the trigger). Same-host absolute URLs
    are returned unchanged so a correctly-set external root URL keeps working and
    the gateway path prefix isn't doubled."""
    if not url:
        return url
    u = urlsplit(url)
    b = urlsplit(cfg.base_url)
    if u.netloc and u.netloc == b.netloc:
        return url
    return f"{cfg.base_url.rstrip('/')}{u.path}"

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
    """Everything a router call needs, with the tokens already decrypted."""

    base_url: str
    username: str
    api_token: str
    router_job_path: str
    verify_tls: bool = True
    # Job-level "Trigger builds remotely" token, sent as the `token` form field.
    build_token: str = ""


def job_url(cfg: JenkinsConfig, job_path: Optional[str] = None) -> str:
    """Absolute URL of a job: ``folder/router`` → ``/job/folder/job/router``.

    Defaults to the configured router job; pass ``job_path`` to address another
    job (e.g. a mobile app's build job) with the same connection.
    """
    raw = cfg.router_job_path if job_path is None else job_path
    segments = [s for s in (raw or "").split("/") if s.strip()]
    if not segments:
        raise JenkinsError("No Jenkins job path configured.")
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
    fields = {k: str(v) for k, v in params.items()}
    if cfg.build_token:
        fields["token"] = cfg.build_token
    body = urlencode(fields).encode("utf-8")
    return _submit_build(
        cfg, body, "application/x-www-form-urlencoded", len(body), job_path=cfg.router_job_path
    )


def _submit_build(
    cfg: JenkinsConfig,
    data,
    content_type: str,
    content_length: int,
    job_path: str = "",
) -> str:
    """POST to buildWithParameters and return the queue item URL.

    ``data`` may be bytes or a readable stream — a signing job's binary is
    hundreds of MB, and buffering that into one bytes object would put the whole
    upload in memory. http.client streams file-like bodies when Content-Length
    is set, so the caller passes both.
    """
    url = f"{job_url(cfg, job_path)}/buildWithParameters"
    req = urllib.request.Request(
        url,
        data=data,
        headers={
            **_headers(cfg),
            "Content-Type": content_type,
            "Content-Length": str(content_length),
        },
        method="POST",
    )
    # A no-redirect opener: the trigger must be a SINGLE POST. If a proxy answers
    # with a 30x, we read its Location instead of letting urllib re-POST (which
    # would queue the build twice).
    handlers = [_NoFollowRedirect()]
    ctx = _ssl_context(cfg)
    if ctx is not None:
        handlers.append(urllib.request.HTTPSHandler(context=ctx))
    opener = urllib.request.build_opener(*handlers)

    location = ""
    try:
        with opener.open(req, timeout=_TIMEOUT_SECONDS) as resp:
            if resp.status not in (200, 201):
                raise JenkinsError(f"Jenkins did not accept the build (HTTP {resp.status}).", resp.status)
            location = resp.headers.get("Location") or ""
    except urllib.error.HTTPError as exc:
        if 300 <= exc.code < 400:
            # A redirect we deliberately did not follow — the queue Location is here.
            location = exc.headers.get("Location") or ""
        else:
            detail = ""
            try:
                raw = (exc.read() or b"").decode("utf-8", "replace")
                # Jenkins error pages are HTML; the useful part is the title text
                # ("No valid crumb…", "…is missing the Build permission").
                import re as _re

                text = _re.sub(r"<[^>]+>", " ", raw)
                detail = " ".join(text.split())[:220]
            except Exception:
                pass
            if exc.code == 400:
                raise JenkinsError(
                    "Jenkins rejected the parameters (400) — the job may not declare them yet. "
                    "A pipeline's parameters only register after its first build, so run it "
                    "once manually.",
                    400,
                ) from exc
            if exc.code == 403:
                raise JenkinsError(
                    "Triggering the router build failed (HTTP 403). "
                    + (f"Jenkins said: {detail} " if detail else "")
                    + "(Usual causes: the gateway strips the Authorization header, the credential is a "
                    "password instead of an API token, or the user lacks Build permission on the job.)",
                    403,
                ) from exc
            raise JenkinsError(
                f"Triggering the router build failed (HTTP {exc.code})."
                + (f" Jenkins said: {detail}" if detail else ""),
                exc.code,
            ) from exc
    except urllib.error.URLError as exc:
        raise JenkinsError(f"Could not reach Jenkins ({exc.reason}).") from exc

    if not location:
        raise JenkinsError("Jenkins accepted the build but returned no queue location header.")
    return _rebase(cfg, location.rstrip("/"))


class _MultipartStream:
    """Reads a multipart body as prefix → file → suffix, without ever holding
    the file in memory. http.client pulls this in blocks."""

    def __init__(self, prefix: bytes, file_path: str, suffix: bytes):
        self._prefix = prefix
        self._suffix = suffix
        self._file = open(file_path, "rb")
        self._stage = 0  # 0 prefix, 1 file, 2 suffix, 3 done
        self._offset = 0

    def read(self, size: int = -1) -> bytes:
        if size is None or size < 0:
            size = 1024 * 1024
        while True:
            if self._stage == 0:
                chunk = self._prefix[self._offset : self._offset + size]
                self._offset += len(chunk)
                if self._offset >= len(self._prefix):
                    self._stage, self._offset = 1, 0
                if chunk:
                    return chunk
                continue
            if self._stage == 1:
                chunk = self._file.read(size)
                if chunk:
                    return chunk
                self._file.close()
                self._stage, self._offset = 2, 0
                continue
            if self._stage == 2:
                chunk = self._suffix[self._offset : self._offset + size]
                self._offset += len(chunk)
                if self._offset >= len(self._suffix):
                    self._stage = 3
                if chunk:
                    return chunk
                continue
            return b""

    def close(self) -> None:
        if not self._file.closed:
            self._file.close()


def trigger_build_with_file(
    cfg: JenkinsConfig,
    params: Dict[str, str],
    file_param: str,
    file_path: str,
    file_name: str = "",
) -> str:
    """Trigger a build, uploading ``file_path`` as a Jenkins *file parameter*.

    Used where the agent cannot reach KubeSight to fetch the binary itself: the
    trigger carries the payload instead. Jenkins drops a file parameter into the
    workspace under the parameter's name, so the job just reads that path.
    """
    import os
    import uuid

    if not os.path.isfile(file_path):
        raise JenkinsError(f"File to upload not found: {file_path}")

    fields = {k: str(v) for k, v in params.items()}
    if cfg.build_token:
        fields["token"] = cfg.build_token

    boundary = uuid.uuid4().hex
    name = file_name or os.path.basename(file_path)
    chunks = []
    for key, value in fields.items():
        chunks.append(
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="{key}"\r\n\r\n'
            f"{value}\r\n"
        )
    chunks.append(
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="{file_param}"; filename="{name}"\r\n'
        f"Content-Type: application/octet-stream\r\n\r\n"
    )
    prefix = "".join(chunks).encode("utf-8")
    suffix = f"\r\n--{boundary}--\r\n".encode("utf-8")
    total = len(prefix) + os.path.getsize(file_path) + len(suffix)

    stream = _MultipartStream(prefix, file_path, suffix)
    try:
        return _submit_build(
            cfg,
            stream,
            f"multipart/form-data; boundary={boundary}",
            total,
            job_path=cfg.router_job_path,
        )
    finally:
        stream.close()


def queue_state(cfg: JenkinsConfig, queue_url: str) -> Dict[str, Any]:
    """State of a queue item. Returns one of:

    - ``{"state": "pending", "why": str}`` — still waiting for an executor
    - ``{"state": "cancelled"}`` — removed from the queue without building
    - ``{"state": "building", "buildNumber": int, "buildUrl": str}``
    """
    _, _, payload = _request(cfg, "GET", f"{_rebase(cfg, queue_url).rstrip('/')}/api/json")
    if payload.get("cancelled"):
        return {"state": "cancelled"}
    executable = payload.get("executable") or {}
    if executable.get("number"):
        return {
            "state": "building",
            "buildNumber": int(executable["number"]),
            "buildUrl": _rebase(cfg, str(executable.get("url") or "")).rstrip("/"),
        }
    return {"state": "pending", "why": payload.get("why") or ""}


def build_state(cfg: JenkinsConfig, build_url: str) -> Dict[str, Any]:
    """Result of a build: ``{"building": bool, "result": str|None, "durationMs": int}``."""
    url = f"{_rebase(cfg, build_url).rstrip('/')}/api/json?tree=building,result,duration,url"
    _, _, payload = _request(cfg, "GET", url)
    return {
        "building": bool(payload.get("building")),
        "result": payload.get("result"),
        "durationMs": int(payload.get("duration") or 0),
        "url": payload.get("url") or build_url,
    }


# ---------------------------------------------------------------------------
# Artifacts — used by the Mobile Applications feature to pull APK/AAB/IPA
# binaries out of a finished build.
# ---------------------------------------------------------------------------

def last_successful_build(cfg: JenkinsConfig, job_path: str) -> Optional[Dict[str, Any]]:
    """Number + URL of a job's last successful build, or None if it never built."""
    url = f"{job_url(cfg, job_path)}/lastSuccessfulBuild/api/json?tree=number,url,result"
    try:
        _, _, payload = _request(cfg, "GET", url)
    except JenkinsError as exc:
        if exc.status == 404:
            return None
        raise
    if not payload.get("number"):
        return None
    return {
        "number": int(payload["number"]),
        "url": _rebase(cfg, str(payload.get("url") or "")).rstrip("/"),
    }


def list_artifacts(cfg: JenkinsConfig, build_url: str) -> list:
    """The build's ARCHIVED artifacts: ``[{"fileName", "relativePath"}]``.

    Only artifacts saved with ``archiveArtifacts`` appear here — files left in
    the workspace do not (fetch those by explicit path via ``download_file``).
    """
    url = f"{_rebase(cfg, build_url).rstrip('/')}/api/json?tree=artifacts[fileName,relativePath]"
    _, _, payload = _request(cfg, "GET", url)
    out = []
    for item in payload.get("artifacts") or []:
        if isinstance(item, dict) and item.get("relativePath"):
            out.append(
                {
                    "fileName": item.get("fileName") or item["relativePath"].rsplit("/", 1)[-1],
                    "relativePath": item["relativePath"],
                }
            )
    return out


def artifact_url(cfg: JenkinsConfig, build_url: str, relative_path: str) -> str:
    """URL of one archived artifact of a build."""
    encoded = "/".join(quote(seg) for seg in relative_path.split("/") if seg)
    return f"{_rebase(cfg, build_url).rstrip('/')}/artifact/{encoded}"


def workspace_file_url(cfg: JenkinsConfig, build_url: str, ws_path: str) -> str:
    """URL of a file under the build's workspace browser, e.g.
    ``execution/node/71/ws/pos.apk`` for a pipeline node's workspace."""
    encoded = "/".join(quote(seg) for seg in ws_path.split("/") if seg)
    return f"{_rebase(cfg, build_url).rstrip('/')}/{encoded}"


def download_file(
    cfg: JenkinsConfig,
    url: str,
    dest_path: str,
    *,
    max_bytes: int = 2 * 1024 * 1024 * 1024,
) -> Dict[str, Any]:
    """Stream ``url`` (authed like every other call) into ``dest_path``.

    Returns ``{"size": int, "sha256": str}``. Chunked so a multi-hundred-MB
    binary never sits in memory; aborts past ``max_bytes`` (default 2 GiB).
    A generous read timeout applies per socket read, not to the whole download.
    """
    import hashlib

    req = urllib.request.Request(url, headers=_headers(cfg), method="GET")
    digest = hashlib.sha256()
    size = 0
    try:
        with urllib.request.urlopen(req, timeout=60, context=_ssl_context(cfg)) as resp:
            declared = resp.headers.get("Content-Length")
            if declared and int(declared) > max_bytes:
                raise JenkinsError(
                    f"Artifact is {int(declared)} bytes — larger than the {max_bytes} byte limit."
                )
            with open(dest_path, "wb") as fh:
                while True:
                    chunk = resp.read(1024 * 1024)
                    if not chunk:
                        break
                    size += len(chunk)
                    if size > max_bytes:
                        raise JenkinsError(
                            f"Artifact exceeded the {max_bytes} byte download limit."
                        )
                    digest.update(chunk)
                    fh.write(chunk)
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            raise JenkinsError(
                "Jenkins returned 404 for the artifact — the file is not at the configured "
                "path (workspace files are overwritten by newer builds; archiving artifacts "
                "in the Jenkinsfile is the reliable option).",
                404,
            ) from exc
        raise JenkinsError(f"Artifact download failed (HTTP {exc.code}).", exc.code) from exc
    except urllib.error.URLError as exc:
        raise JenkinsError(f"Could not reach Jenkins ({exc.reason}).") from exc
    except TimeoutError as exc:
        raise JenkinsError("Artifact download timed out.") from exc
    return {"size": size, "sha256": digest.hexdigest()}
