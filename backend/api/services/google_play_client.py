"""Minimal Google Play Developer API (Android Publisher v3) client.

Publishes an APK/AAB to a release track via the edits flow: create an edit →
upload the binary → assign it to a track → commit. Auth is the service
account's JSON key: a RS256-signed JWT exchanged for an OAuth access token
(scope ``androidpublisher``). Standard library ``urllib`` + PyJWT only,
matching jenkins_client / registry_client / zoho_client.

The service account must be invited to the Play Console with release
permission on the app, and the app must already exist in the console (the API
cannot create the first listing).
"""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any, Dict, Optional
from urllib.parse import quote, urlencode

import jwt

_API = "https://androidpublisher.googleapis.com/androidpublisher/v3/applications"
_UPLOAD_API = "https://androidpublisher.googleapis.com/upload/androidpublisher/v3/applications"
_SCOPE = "https://www.googleapis.com/auth/androidpublisher"
_TIMEOUT_SECONDS = 30
_UPLOAD_TIMEOUT_SECONDS = 900


class PlayError(Exception):
    """A Google Play API call failed. ``status`` mirrors the HTTP code when known."""

    def __init__(self, message: str, status: Optional[int] = None):
        super().__init__(message)
        self.status = status


@dataclass
class PlayConfig:
    package_name: str
    service_account_json: str


def _sa(cfg: PlayConfig) -> Dict[str, Any]:
    try:
        data = json.loads(cfg.service_account_json or "{}")
    except ValueError as exc:
        raise PlayError("The service-account key is not valid JSON.") from exc
    if not (data.get("client_email") and data.get("private_key")):
        raise PlayError(
            "The service-account JSON is missing client_email/private_key — export the "
            "key file from Google Cloud IAM (type 'service_account')."
        )
    return data


def _error_detail(exc: urllib.error.HTTPError) -> str:
    try:
        raw = (exc.read() or b"").decode("utf-8", "replace")
        parsed = json.loads(raw)
        return parsed.get("error", {}).get("message") or raw[:300]
    except Exception:
        return ""


def _request(
    method: str,
    url: str,
    *,
    token: str = "",
    body: Optional[bytes] = None,
    content_type: str = "application/json",
    timeout: int = _TIMEOUT_SECONDS,
    data_file=None,
    data_len: int = 0,
) -> Dict[str, Any]:
    headers: Dict[str, str] = {}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    if body is not None or data_file is not None:
        headers["Content-Type"] = content_type
    if data_file is not None:
        headers["Content-Length"] = str(data_len)
    req = urllib.request.Request(
        url, data=(data_file if data_file is not None else body), headers=headers, method=method
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", "replace")
            return json.loads(raw) if raw.strip() else {}
    except urllib.error.HTTPError as exc:
        detail = _error_detail(exc)
        if exc.code == 401:
            raise PlayError("Google rejected the credentials (401) — the key may be revoked.", 401) from exc
        if exc.code == 403:
            raise PlayError(
                "Google Play refused the request (403) — invite the service account to the "
                "Play Console with release permission on this app. "
                + (f"Google said: {detail}" if detail else ""),
                403,
            ) from exc
        if exc.code == 404:
            raise PlayError(
                f"Google Play returned 404 — is '{detail or 'the package'}' published in this "
                "developer account? The app must exist in the Play Console first.",
                404,
            ) from exc
        raise PlayError(
            f"Google Play call failed (HTTP {exc.code})." + (f" {detail}" if detail else ""),
            exc.code,
        ) from exc
    except urllib.error.URLError as exc:
        raise PlayError(f"Could not reach Google Play ({exc.reason}).") from exc
    except TimeoutError as exc:
        raise PlayError("The Google Play request timed out.") from exc


def access_token(cfg: PlayConfig) -> str:
    """Service-account JWT grant → short-lived OAuth access token."""
    sa = _sa(cfg)
    now = int(time.time())
    assertion = jwt.encode(
        {
            "iss": sa["client_email"],
            "scope": _SCOPE,
            "aud": sa.get("token_uri") or "https://oauth2.googleapis.com/token",
            "iat": now,
            "exp": now + 3600,
        },
        sa["private_key"],
        algorithm="RS256",
    )
    body = urlencode(
        {"grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer", "assertion": assertion}
    ).encode("utf-8")
    payload = _request(
        "POST",
        sa.get("token_uri") or "https://oauth2.googleapis.com/token",
        body=body,
        content_type="application/x-www-form-urlencoded",
    )
    token = payload.get("access_token")
    if not token:
        raise PlayError("Google did not return an access token for the service account.")
    return token


def _pkg(cfg: PlayConfig) -> str:
    return quote(cfg.package_name, safe="")


def create_edit(cfg: PlayConfig, token: str) -> str:
    payload = _request("POST", f"{_API}/{_pkg(cfg)}/edits", token=token, body=b"{}")
    edit_id = payload.get("id")
    if not edit_id:
        raise PlayError("Google Play did not return an edit id.")
    return str(edit_id)


def delete_edit(cfg: PlayConfig, token: str, edit_id: str) -> None:
    _request("DELETE", f"{_API}/{_pkg(cfg)}/edits/{quote(edit_id, safe='')}", token=token)


def upload_binary(cfg: PlayConfig, token: str, edit_id: str, path: str, artifact_type: str) -> int:
    """Upload the APK/AAB into the edit (streamed from disk). Returns versionCode."""
    resource = "bundles" if artifact_type == "aab" else "apks"
    url = (
        f"{_UPLOAD_API}/{_pkg(cfg)}/edits/{quote(edit_id, safe='')}/{resource}?uploadType=media"
    )
    size = os.path.getsize(path)
    with open(path, "rb") as fh:
        payload = _request(
            "POST",
            url,
            token=token,
            content_type="application/octet-stream",
            timeout=_UPLOAD_TIMEOUT_SECONDS,
            data_file=fh,
            data_len=size,
        )
    version_code = payload.get("versionCode")
    if version_code is None:
        raise PlayError("The upload succeeded but Google returned no versionCode.")
    return int(version_code)


def assign_track(cfg: PlayConfig, token: str, edit_id: str, track: str, version_code: int) -> None:
    body = json.dumps(
        {
            "track": track,
            "releases": [{"versionCodes": [str(version_code)], "status": "completed"}],
        }
    ).encode("utf-8")
    _request(
        "PUT",
        f"{_API}/{_pkg(cfg)}/edits/{quote(edit_id, safe='')}/tracks/{quote(track, safe='')}",
        token=token,
        body=body,
    )


def commit_edit(cfg: PlayConfig, token: str, edit_id: str) -> None:
    _request(
        "POST",
        f"{_API}/{_pkg(cfg)}/edits/{quote(edit_id, safe='')}:commit",
        token=token,
        body=b"{}",
    )


def test_credentials(cfg: PlayConfig) -> None:
    """Cheapest end-to-end validity check: mint a token, open an edit on the
    package (proves both auth and app access), then throw the edit away."""
    token = access_token(cfg)
    edit_id = create_edit(cfg, token)
    try:
        delete_edit(cfg, token, edit_id)
    except PlayError:
        pass  # an abandoned edit expires on its own
