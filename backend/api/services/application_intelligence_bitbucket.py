"""Read-only Bitbucket Cloud metadata used by repository-backed dropdowns."""

from __future__ import annotations

import base64
import json
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

API_ORIGIN = "https://api.bitbucket.org"
MAX_RESPONSE_BYTES = 2_000_000
MAX_REF_ITEMS = 200
MAX_COMMIT_ITEMS = 25
MAX_TREE_ITEMS = 5_000


class BitbucketMetadataError(RuntimeError):
    pass


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, msg, headers, newurl):
        return None


_OPENER = build_opener(_NoRedirect)


def _open_api(request: Request, timeout: int):
    return _OPENER.open(request, timeout=timeout)


def _validate_api_url(url: str, repository_ref: str) -> str:
    parsed = urlsplit(url)
    expected_prefix = f"/2.0/repositories/{repository_ref}/"
    if (
        parsed.scheme != "https"
        or (parsed.hostname or "").lower() != "api.bitbucket.org"
        or parsed.port not in (None, 443)
        or parsed.username
        or parsed.password
        or parsed.fragment
        or not parsed.path.startswith(expected_prefix)
    ):
        raise BitbucketMetadataError("Bitbucket returned an unsafe metadata URL.")
    return url


def _authorization_header(
    token: str, credential_type: str, principal: str = ""
) -> str:
    if credential_type == "api_token":
        if not principal:
            raise BitbucketMetadataError(
                "An Atlassian account email is required for this API token."
            )
        encoded = base64.b64encode(
            f"{principal}:{token}".encode("utf-8")
        ).decode("ascii")
        return f"Basic {encoded}"
    return f"Bearer {token}"


def _request_json(
    url: str,
    token: str,
    repository_ref: str,
    credential_type: str = "oauth",
    principal: str = "",
) -> dict:
    safe_url = _validate_api_url(url, repository_ref)
    request = Request(
        safe_url,
        method="GET",
        headers={
            "Authorization": _authorization_header(
                token, credential_type, principal
            ),
            "Accept": "application/json",
            "User-Agent": "KubeSight/application-intelligence",
        },
    )
    try:
        with _open_api(request, timeout=20) as response:
            raw = response.read(MAX_RESPONSE_BYTES + 1)
    except HTTPError as exc:
        if exc.code in {401, 403}:
            message = (
                "Bitbucket rejected this credential. Verify that it has read access "
                "to the repository."
            )
        elif exc.code == 404:
            message = "The Bitbucket repository or revision was not found."
        elif exc.code == 429:
            message = "Bitbucket rate-limited the metadata request. Try again shortly."
        else:
            message = "Bitbucket metadata could not be loaded."
        raise BitbucketMetadataError(message) from exc
    except (URLError, TimeoutError) as exc:
        raise BitbucketMetadataError(
            "Bitbucket metadata is temporarily unavailable."
        ) from exc
    if len(raw) > MAX_RESPONSE_BYTES:
        raise BitbucketMetadataError("Bitbucket metadata exceeded the response limit.")
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise BitbucketMetadataError("Bitbucket returned malformed metadata.") from exc
    if not isinstance(payload, dict):
        raise BitbucketMetadataError("Bitbucket returned malformed metadata.")
    return payload


def _collect(
    url: str,
    token: str,
    repository_ref: str,
    *,
    limit: int,
    max_pages: int = 5,
    credential_type: str = "oauth",
    principal: str = "",
) -> list[dict]:
    items: list[dict] = []
    next_url: str | None = url
    pages = 0
    while next_url and len(items) < limit and pages < max_pages:
        payload = _request_json(
            next_url, token, repository_ref, credential_type, principal
        )
        values = payload.get("values")
        if not isinstance(values, list):
            raise BitbucketMetadataError("Bitbucket returned malformed metadata.")
        items.extend(item for item in values if isinstance(item, dict))
        candidate = payload.get("next")
        next_url = (
            _validate_api_url(candidate, repository_ref)
            if isinstance(candidate, str) and candidate
            else None
        )
        pages += 1
    return items[:limit]


def _clean_text(value: object, max_chars: int) -> str:
    return " ".join(str(value or "").split())[:max_chars]


def list_revisions(
    repository_ref: str,
    token: str,
    credential_type: str = "oauth",
    principal: str = "",
) -> dict:
    base = f"{API_ORIGIN}/2.0/repositories/{repository_ref}"
    refs = _collect(
        f"{base}/refs?{urlencode({'pagelen': 100, 'sort': 'name'})}",
        token,
        repository_ref,
        limit=MAX_REF_ITEMS,
        credential_type=credential_type,
        principal=principal,
    )
    commits = _collect(
        f"{base}/commits?{urlencode({'pagelen': MAX_COMMIT_ITEMS})}",
        token,
        repository_ref,
        limit=MAX_COMMIT_ITEMS,
        max_pages=1,
        credential_type=credential_type,
        principal=principal,
    )

    options = []
    for ref in refs:
        ref_type = _clean_text(ref.get("type"), 20).lower()
        name = _clean_text(ref.get("name"), 256)
        if ref_type not in {"branch", "tag"} or not name:
            continue
        target = ref.get("target") if isinstance(ref.get("target"), dict) else {}
        commit_hash = _clean_text(target.get("hash"), 64)
        options.append(
            {
                "value": name,
                "label": f"{ref_type.title()} — {name}",
                "type": ref_type,
                "commit": commit_hash,
            }
        )

    seen_commits = set()
    for commit in commits:
        commit_hash = _clean_text(commit.get("hash"), 64)
        if not commit_hash or commit_hash in seen_commits:
            continue
        seen_commits.add(commit_hash)
        message = _clean_text(commit.get("message"), 100) or "No commit message"
        options.append(
            {
                "value": commit_hash,
                "label": f"Commit — {commit_hash[:12]} · {message}",
                "type": "commit",
                "commit": commit_hash,
            }
        )
    return {"items": options, "count": len(options)}


def list_dockerfiles(
    repository_ref: str,
    token: str,
    revision: str,
    credential_type: str = "oauth",
    principal: str = "",
) -> dict:
    clean_revision = _clean_text(revision, 256)
    if not clean_revision or any(ord(char) < 32 for char in clean_revision):
        raise ValueError("A valid branch, tag, or commit is required.")
    encoded_revision = quote(clean_revision, safe="")
    base = f"{API_ORIGIN}/2.0/repositories/{repository_ref}"
    tree = _collect(
        f"{base}/src/{encoded_revision}/?"
        f"{urlencode({'pagelen': 100, 'max_depth': 8})}",
        token,
        repository_ref,
        limit=MAX_TREE_ITEMS,
        max_pages=50,
        credential_type=credential_type,
        principal=principal,
    )
    paths = set()
    for item in tree:
        if item.get("type") != "commit_file":
            continue
        path = _clean_text(item.get("path"), 1024).replace("\\", "/").strip("/")
        filename = path.rsplit("/", 1)[-1].lower()
        if (
            path
            and (
                filename == "dockerfile"
                or filename.startswith("dockerfile.")
                or filename.endswith(".dockerfile")
            )
        ):
            paths.add(path)
    items = [{"value": path, "label": path} for path in sorted(paths)]
    return {"items": items, "count": len(items), "revision": clean_revision}
