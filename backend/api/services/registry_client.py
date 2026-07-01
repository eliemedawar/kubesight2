"""Low-level Docker Registry HTTP API V2 client.

Just enough of the protocol to answer "does this image:tag exist?" cheaply — a
``HEAD /v2/<repository>/manifests/<reference>`` that returns the manifest digest
without pulling any layers. Works against Sonatype Nexus and any other V2
registry.

Auth: Basic is the primary path. If the registry answers ``401`` with a
``WWW-Authenticate: Bearer realm=...`` challenge (Nexus with the Docker Bearer
Token realm enabled), we transparently fetch a token and retry, so the same
connection keeps working if the operator later flips that realm on.

Only the standard library is used (``urllib``), matching the rest of the backend
(see ``alert_routing_service``) — no new dependency.
"""

from __future__ import annotations

import base64
import json
import ssl
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Optional, Tuple
from urllib.parse import urlencode, urlparse

# Manifest media types we accept — v2 schema2, OCI, and the fat manifest lists.
_MANIFEST_ACCEPT = ", ".join(
    [
        "application/vnd.docker.distribution.manifest.v2+json",
        "application/vnd.docker.distribution.manifest.list.v2+json",
        "application/vnd.oci.image.manifest.v1+json",
        "application/vnd.oci.image.index.v1+json",
        "application/vnd.docker.distribution.manifest.v1+json",
    ]
)

_TIMEOUT_SECONDS = 10

# image_exists / check_manifest status values.
FOUND = "found"
NOT_FOUND = "not_found"
UNREACHABLE = "unreachable"


@dataclass
class ParsedImage:
    registry: str  # host[:port], "" for docker.io shorthand
    repository: str  # e.g. "library/nginx" or "myteam/api"
    reference: str  # tag or digest, defaults to "latest"

    @property
    def has_registry(self) -> bool:
        return bool(self.registry)


def parse_image_reference(image: str) -> Optional[ParsedImage]:
    """Split ``host/repo:tag`` (or ``repo@sha256:...``) into its parts.

    Returns ``None`` for empty/blank input. A leading path segment counts as a
    registry host only when it contains a ``.`` or ``:`` or is ``localhost`` —
    matching Docker's own heuristic — otherwise it's Docker Hub shorthand.
    """
    ref = str(image or "").strip()
    if not ref:
        return None

    registry = ""
    remainder = ref
    first, _, rest = ref.partition("/")
    if rest and ("." in first or ":" in first or first == "localhost"):
        registry = first
        remainder = rest

    # Digest reference (repo@sha256:...) takes precedence over a tag colon.
    if "@" in remainder:
        repository, _, reference = remainder.partition("@")
    else:
        # Only treat a colon in the LAST path segment as a tag separator, so a
        # registry port left in `remainder` (shouldn't happen here) isn't eaten.
        path, _, last = remainder.rpartition("/")
        if ":" in last:
            name, _, tag = last.partition(":")
            repository = f"{path}/{name}" if path else name
            reference = tag
        else:
            repository = remainder
            reference = "latest"

    repository = repository.strip("/")
    if not repository:
        return None

    # Docker Hub shorthand: bare `nginx` means `library/nginx`.
    if not registry and "/" not in repository:
        repository = f"library/{repository}"

    return ParsedImage(registry=registry, repository=repository, reference=reference)


def _ssl_context(verify_tls: bool, ca_cert: Optional[str]) -> ssl.SSLContext:
    ctx = ssl.create_default_context()
    if ca_cert and ca_cert.strip():
        try:
            ctx.load_verify_locations(cadata=ca_cert)
        except ssl.SSLError:
            pass
    if not verify_tls:
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
    return ctx


def _basic_header(username: str, password: str) -> Optional[str]:
    if not username:
        return None
    raw = f"{username}:{password}".encode("utf-8")
    return "Basic " + base64.b64encode(raw).decode("ascii")


def _parse_bearer_challenge(header: str) -> Optional[dict]:
    """Parse a ``WWW-Authenticate: Bearer realm="...",service="...",scope="..."``."""
    header = (header or "").strip()
    if not header.lower().startswith("bearer "):
        return None
    params: dict = {}
    for part in header[len("bearer "):].split(","):
        key, _, value = part.strip().partition("=")
        if key:
            params[key.strip().lower()] = value.strip().strip('"')
    return params or None


def _fetch_bearer_token(
    challenge: dict, *, auth_header: Optional[str], context: ssl.SSLContext
) -> Optional[str]:
    realm = challenge.get("realm")
    if not realm:
        return None
    query = {k: v for k, v in challenge.items() if k in ("service", "scope") and v}
    url = f"{realm}?{urlencode(query)}" if query else realm
    req = urllib.request.Request(url, method="GET")
    if auth_header:
        req.add_header("Authorization", auth_header)
    try:
        with urllib.request.urlopen(req, timeout=_TIMEOUT_SECONDS, context=context) as resp:
            body = json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, ValueError, OSError):
        return None
    return body.get("token") or body.get("access_token")


def _do_request(
    url: str,
    *,
    method: str,
    authorization: Optional[str],
    context: ssl.SSLContext,
    accept: Optional[str] = None,
) -> Tuple[int, Optional[str]]:
    """Issue ``method`` on ``url``; returns (status_code, www_authenticate_header)."""
    req = urllib.request.Request(url, method=method)
    if accept:
        req.add_header("Accept", accept)
    if authorization:
        req.add_header("Authorization", authorization)
    try:
        with urllib.request.urlopen(req, timeout=_TIMEOUT_SECONDS, context=context) as resp:
            return resp.status, None
    except urllib.error.HTTPError as exc:
        return exc.code, exc.headers.get("WWW-Authenticate")


def _do_head(
    url: str, *, authorization: Optional[str], context: ssl.SSLContext
) -> Tuple[int, Optional[str]]:
    """HEAD ``url`` for a manifest; returns (status_code, www_authenticate_header)."""
    return _do_request(
        url, method="HEAD", authorization=authorization, context=context, accept=_MANIFEST_ACCEPT
    )


def _normalize_base(base_url: str) -> Optional[str]:
    base = str(base_url or "").strip().rstrip("/")
    if not base:
        return None
    if "://" not in base:
        base = "https://" + base
    return base


def ping(
    base_url: str,
    *,
    username: str = "",
    password: str = "",
    verify_tls: bool = True,
    ca_cert: Optional[str] = None,
) -> Tuple[str, str]:
    """Check reachability + credentials via the registry's base ``GET /v2/`` endpoint.

    This is the canonical Docker Registry V2 "ping": a 200 (after the optional
    Bearer-token handshake) means the endpoint is reachable and the credentials
    are accepted. Returns ``(status, detail)`` where status is :data:`FOUND` when
    the registry answered OK, or :data:`UNREACHABLE` otherwise.
    """
    base = _normalize_base(base_url)
    if not base:
        return UNREACHABLE, "No registry URL configured."

    context = _ssl_context(verify_tls, ca_cert)
    url = f"{base}/v2/"
    basic = _basic_header(username, password)

    try:
        status, www_auth = _do_request(url, method="GET", authorization=basic, context=context)
        if status == 401 and www_auth:
            challenge = _parse_bearer_challenge(www_auth)
            if challenge:
                token = _fetch_bearer_token(challenge, auth_header=basic, context=context)
                if token:
                    status, _ = _do_request(
                        url, method="GET", authorization=f"Bearer {token}", context=context
                    )

        if status == 200:
            return FOUND, "Connection successful."
        if status in (401, 403):
            return UNREACHABLE, "Registry rejected the credentials (authentication failed)."
        if status == 404:
            # The endpoint answered but /v2/ isn't there — usually a wrong URL or
            # a Nexus repo served under a /repository/<name>/ path.
            return UNREACHABLE, (
                "Registry did not expose a Docker V2 API at this URL (404). Check the "
                "host/port — for Nexus, use the repository's Docker connector host."
            )
        return UNREACHABLE, f"Registry returned an unexpected status ({status})."
    except urllib.error.URLError as exc:
        reason = getattr(exc, "reason", exc)
        return UNREACHABLE, f"Could not reach the registry: {reason}"
    except (ssl.SSLError, OSError) as exc:
        return UNREACHABLE, f"Could not reach the registry: {exc}"


def check_manifest(
    base_url: str,
    repository: str,
    reference: str,
    *,
    username: str = "",
    password: str = "",
    verify_tls: bool = True,
    ca_cert: Optional[str] = None,
) -> Tuple[str, str]:
    """Does ``repository:reference`` exist in the registry at ``base_url``?

    ``base_url`` may be a bare host (``nexus.example.com:8083``) or a full URL;
    ``https`` is assumed when no scheme is given. Returns ``(status, detail)``
    where status is :data:`FOUND`, :data:`NOT_FOUND`, or :data:`UNREACHABLE`.
    """
    base = str(base_url or "").strip().rstrip("/")
    if not base:
        return UNREACHABLE, "No registry URL configured."
    if "://" not in base:
        base = "https://" + base

    context = _ssl_context(verify_tls, ca_cert)
    url = f"{base}/v2/{repository}/manifests/{reference}"
    basic = _basic_header(username, password)

    try:
        status, www_auth = _do_head(url, authorization=basic, context=context)

        # Bearer-token dance: registry wants a token even though we sent Basic.
        if status == 401 and www_auth:
            challenge = _parse_bearer_challenge(www_auth)
            if challenge:
                token = _fetch_bearer_token(challenge, auth_header=basic, context=context)
                if token:
                    status, _ = _do_head(
                        url, authorization=f"Bearer {token}", context=context
                    )

        if status == 200:
            return FOUND, f"{repository}:{reference} exists in the registry."
        if status == 404:
            return NOT_FOUND, f"{repository}:{reference} was not found in the registry."
        if status in (401, 403):
            return UNREACHABLE, "Registry rejected the credentials (authentication failed)."
        return UNREACHABLE, f"Registry returned an unexpected status ({status})."
    except urllib.error.URLError as exc:
        reason = getattr(exc, "reason", exc)
        return UNREACHABLE, f"Could not reach the registry: {reason}"
    except (ssl.SSLError, OSError) as exc:
        return UNREACHABLE, f"Could not reach the registry: {exc}"


def registry_host_of(base_url: str) -> str:
    """The bare ``host[:port]`` of a configured registry URL, for image matching."""
    base = str(base_url or "").strip().rstrip("/")
    if not base:
        return ""
    if "://" in base:
        return urlparse(base).netloc
    # Bare host possibly with a path suffix — keep only the authority.
    return base.split("/", 1)[0]
