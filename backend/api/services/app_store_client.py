"""Minimal App Store Connect API client: upload an IPA and get it to TestFlight.

Uses Apple's Build Uploads API (WWDC25): create a ``buildUploads`` resource,
reserve ``buildUploadFiles`` parts, PUT the binary chunks to the URLs Apple
returns, mark the upload complete, then poll until the build reaches
``processingState = VALID`` — fully server-side, no Xcode/Transporter/Mac.
Auth is an App Store Connect API key: an ES256 JWT (issuer id + key id + .p8
private key, max 20-minute lifetime — minted fresh per request batch).

``target = review`` submits the processed build for Beta App Review
(``betaAppReviewSubmissions``), which is what external TestFlight distribution
requires; internal testers can install as soon as the build is VALID.

Standard library ``urllib`` + PyJWT, matching the other integration clients.
The build's CFBundleShortVersionString/CFBundleVersion are read straight out of
the IPA (it is a zip; Info.plist parses with plistlib) so the declared upload
attributes always match the binary.
"""

from __future__ import annotations

import json
import os
import plistlib
import re
import time
import urllib.error
import urllib.request
import zipfile
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import quote

import jwt

_API = "https://api.appstoreconnect.apple.com/v1"
_TIMEOUT_SECONDS = 30
_CHUNK_TIMEOUT_SECONDS = 600


class AscError(Exception):
    """An App Store Connect API call failed. ``status`` mirrors the HTTP code."""

    def __init__(self, message: str, status: Optional[int] = None):
        super().__init__(message)
        self.status = status


@dataclass
class AscConfig:
    issuer_id: str
    key_id: str
    private_key: str  # the .p8 PEM contents
    bundle_id: str = ""
    app_id: str = ""


def _token(cfg: AscConfig) -> str:
    if not (cfg.issuer_id and cfg.key_id and cfg.private_key):
        raise AscError("The App Store Connect API key (issuer id, key id, .p8) is not configured.")
    now = int(time.time())
    try:
        return jwt.encode(
            {"iss": cfg.issuer_id, "iat": now, "exp": now + 900, "aud": "appstoreconnect-v1"},
            cfg.private_key,
            algorithm="ES256",
            headers={"kid": cfg.key_id},
        )
    except Exception as exc:
        raise AscError(f"Could not sign the App Store Connect token — check the .p8 key ({exc}).") from exc


def _error_detail(raw: str) -> str:
    try:
        parsed = json.loads(raw)
        errors = parsed.get("errors") or []
        if errors:
            first = errors[0]
            return f"{first.get('title') or ''}: {first.get('detail') or ''}".strip(": ")
    except Exception:
        pass
    return raw[:300]


def _request(
    cfg: AscConfig, method: str, url: str, *, body: Optional[Dict[str, Any]] = None
) -> Dict[str, Any]:
    payload = json.dumps(body).encode("utf-8") if body is not None else None
    headers = {"Authorization": f"Bearer {_token(cfg)}"}
    if payload is not None:
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=payload, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=_TIMEOUT_SECONDS) as resp:
            raw = resp.read().decode("utf-8", "replace")
            return json.loads(raw) if raw.strip() else {}
    except urllib.error.HTTPError as exc:
        detail = ""
        try:
            detail = _error_detail((exc.read() or b"").decode("utf-8", "replace"))
        except Exception:
            pass
        if exc.code == 401:
            raise AscError(
                "App Store Connect rejected the token (401) — check the issuer id, key id and "
                ".p8 key (and that the key hasn't been revoked).",
                401,
            ) from exc
        if exc.code == 403:
            raise AscError(
                "App Store Connect refused the request (403) — the API key's role may be too "
                "limited. " + (f"Apple said: {detail}" if detail else ""),
                403,
            ) from exc
        raise AscError(
            f"App Store Connect call failed (HTTP {exc.code})." + (f" {detail}" if detail else ""),
            exc.code,
        ) from exc
    except urllib.error.URLError as exc:
        raise AscError(f"Could not reach App Store Connect ({exc.reason}).") from exc
    except TimeoutError as exc:
        raise AscError("The App Store Connect request timed out.") from exc


def resolve_app_id(cfg: AscConfig) -> str:
    """The numeric ASC app id for the configured bundle id ("" when unmatched).
    Doubles as the cheapest credential-validity check."""
    if not cfg.bundle_id:
        # No bundle id to look up — still exercise auth with a minimal list call.
        _request(cfg, "GET", f"{_API}/apps?limit=1")
        return cfg.app_id or ""
    payload = _request(
        cfg, "GET", f"{_API}/apps?filter[bundleId]={quote(cfg.bundle_id, safe='')}&limit=1"
    )
    data = payload.get("data") or []
    return str(data[0]["id"]) if data else ""


def ipa_versions(path: str) -> Tuple[str, str]:
    """(CFBundleShortVersionString, CFBundleVersion) read from the IPA's
    Info.plist — the declared upload attributes must match the binary."""
    try:
        with zipfile.ZipFile(path) as zf:
            candidates = [
                n for n in zf.namelist() if re.fullmatch(r"Payload/[^/]+\.app/Info\.plist", n)
            ]
            if not candidates:
                raise AscError("The IPA has no Payload/*.app/Info.plist — is this a valid IPA?")
            info = plistlib.loads(zf.read(candidates[0]))
    except AscError:
        raise
    except Exception as exc:
        raise AscError(f"Could not read the IPA's Info.plist ({exc}).") from exc
    short = str(info.get("CFBundleShortVersionString") or "").strip()
    build = str(info.get("CFBundleVersion") or "").strip()
    if not (short and build):
        raise AscError("The IPA's Info.plist is missing CFBundleShortVersionString/CFBundleVersion.")
    return short, build


def _upload_operations(entry: Dict[str, Any]) -> List[Dict[str, Any]]:
    attrs = entry.get("attributes") or {}
    ops = attrs.get("uploadOperations") or []
    return [op for op in ops if isinstance(op, dict) and op.get("url")]


def _put_chunk(op: Dict[str, Any], path: str) -> None:
    offset = int(op.get("offset") or 0)
    length = int(op.get("length") or 0) or (os.path.getsize(path) - offset)
    with open(path, "rb") as fh:
        fh.seek(offset)
        chunk = fh.read(length)
    headers = {}
    for item in op.get("requestHeaders") or op.get("headers") or []:
        if isinstance(item, dict) and item.get("name"):
            headers[item["name"]] = item.get("value") or ""
    req = urllib.request.Request(
        op["url"], data=chunk, headers=headers, method=str(op.get("method") or "PUT").upper()
    )
    try:
        with urllib.request.urlopen(req, timeout=_CHUNK_TIMEOUT_SECONDS):
            pass
    except urllib.error.HTTPError as exc:
        raise AscError(f"Uploading a binary chunk failed (HTTP {exc.code}).", exc.code) from exc
    except urllib.error.URLError as exc:
        raise AscError(f"Could not reach Apple's upload endpoint ({exc.reason}).") from exc


def upload_build(cfg: AscConfig, path: str, file_name: str) -> Dict[str, Any]:
    """Create the build upload, push the IPA, and mark it complete.

    Returns a store_ref: ``{buildUploadId, appId, shortVersion, bundleVersion}``.
    Processing continues asynchronously on Apple's side — poll with
    :func:`processing_state`.
    """
    app_id = cfg.app_id or resolve_app_id(cfg)
    if not app_id:
        raise AscError(
            f"No App Store Connect app matches bundle id '{cfg.bundle_id}' — create the app "
            "record in App Store Connect first."
        )
    short_version, bundle_version = ipa_versions(path)

    created = _request(
        cfg,
        "POST",
        f"{_API}/buildUploads",
        body={
            "data": {
                "type": "buildUploads",
                "attributes": {
                    "cfBundleShortVersionString": short_version,
                    "cfBundleVersion": bundle_version,
                    "platform": "IOS",
                },
                "relationships": {"app": {"data": {"type": "apps", "id": str(app_id)}}},
            }
        },
    )
    upload_id = str((created.get("data") or {}).get("id") or "")
    if not upload_id:
        raise AscError("App Store Connect did not return a build upload id.")

    size = os.path.getsize(path)
    reserved = _request(
        cfg,
        "POST",
        f"{_API}/buildUploadFiles",
        body={
            "data": {
                "type": "buildUploadFiles",
                "attributes": {
                    "fileName": file_name,
                    "fileSize": size,
                    "assetType": "ASSET",
                    "uti": "com.apple.ipa",
                },
                "relationships": {
                    "buildUpload": {"data": {"type": "buildUploads", "id": upload_id}}
                },
            }
        },
    )
    file_entry = reserved.get("data") or {}
    operations = _upload_operations(file_entry)
    if not operations:
        raise AscError("App Store Connect returned no upload instructions for the binary.")
    for op in operations:
        _put_chunk(op, path)

    file_id = str(file_entry.get("id") or "")
    if not file_id:
        raise AscError("App Store Connect did not return a build upload file id.")
    # Finalize on the FILE resource: marking buildUploadFiles uploaded=true is the
    # authoritative completion signal. buildUploads itself only allows CREATE /
    # DELETE / GET_INSTANCE — PATCHing it 403s ("does not allow 'UPDATE'").
    _request(
        cfg,
        "PATCH",
        f"{_API}/buildUploadFiles/{quote(file_id, safe='')}",
        body={
            "data": {
                "type": "buildUploadFiles",
                "id": file_id,
                "attributes": {"uploaded": True},
            }
        },
    )
    return {
        "buildUploadId": upload_id,
        "appId": str(app_id),
        "shortVersion": short_version,
        "bundleVersion": bundle_version,
    }


def _upload_state(payload: Dict[str, Any]) -> Tuple[str, str]:
    """(state, detail) from a buildUploads resource — ``state`` may be a plain
    string or a ``{"state", "errors": [...]}`` object depending on API version."""
    attrs = (payload.get("data") or {}).get("attributes") or {}
    raw = attrs.get("state")
    if isinstance(raw, dict):
        errors = "; ".join(
            str(e.get("description") or e.get("detail") or e) for e in raw.get("errors") or []
        )
        return str(raw.get("state") or ""), errors
    return str(raw or ""), ""


def processing_state(cfg: AscConfig, store_ref: Dict[str, Any], version: str) -> Dict[str, Any]:
    """Where the uploaded build stands. Returns one of:

    - ``{"state": "processing", "detail": str}``
    - ``{"state": "failed", "detail": str}``
    - ``{"state": "done", "buildId": str}`` — processed and VALID (TestFlight-ready)
    """
    upload_id = str(store_ref.get("buildUploadId") or "")
    if upload_id:
        payload = _request(cfg, "GET", f"{_API}/buildUploads/{quote(upload_id, safe='')}")
        state, errors = _upload_state(payload)
        if state in ("FAILED", "ERROR"):
            return {"state": "failed", "detail": errors or "Apple reported the upload as failed."}
        if state and state != "COMPLETE":
            return {"state": "processing", "detail": f"Apple is processing the upload ({state})."}
        relationships = (payload.get("data") or {}).get("relationships") or {}
        build_rel = ((relationships.get("build") or {}).get("data") or {})
        if build_rel.get("id"):
            return _check_build(cfg, str(build_rel["id"]))

    # No direct build linkage — find the newest build matching the uploaded version.
    app_id = str(store_ref.get("appId") or cfg.app_id or "")
    if not app_id:
        return {"state": "processing", "detail": "waiting for the build to appear"}
    payload = _request(
        cfg,
        "GET",
        f"{_API}/builds?filter[app]={quote(app_id, safe='')}&sort=-uploadedDate&limit=10",
    )
    wanted = str(store_ref.get("bundleVersion") or version or "")
    for entry in payload.get("data") or []:
        attrs = entry.get("attributes") or {}
        if not wanted or str(attrs.get("version") or "") == wanted:
            return _check_build(cfg, str(entry.get("id")), attrs)
    return {"state": "processing", "detail": "the build has not appeared in App Store Connect yet"}


def _check_build(
    cfg: AscConfig, build_id: str, attrs: Optional[Dict[str, Any]] = None
) -> Dict[str, Any]:
    if attrs is None:
        payload = _request(cfg, "GET", f"{_API}/builds/{quote(build_id, safe='')}")
        attrs = (payload.get("data") or {}).get("attributes") or {}
    state = str(attrs.get("processingState") or "PROCESSING")
    if state == "VALID":
        return {"state": "done", "buildId": build_id}
    if state in ("FAILED", "INVALID"):
        return {"state": "failed", "detail": f"App Store Connect marked the build {state}."}
    return {"state": "processing", "detail": f"build processing ({state})"}


def submit_for_review(cfg: AscConfig, build_id: str) -> None:
    """Submit the processed build for Beta App Review (external TestFlight)."""
    if not build_id:
        raise AscError("No processed build id to submit for review.")
    _request(
        cfg,
        "POST",
        f"{_API}/betaAppReviewSubmissions",
        body={
            "data": {
                "type": "betaAppReviewSubmissions",
                "relationships": {"build": {"data": {"type": "builds", "id": str(build_id)}}},
            }
        },
    )
