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
# Branches and tags are collected SEPARATELY, with a budget each.
#
# They used to share one `/refs` call capped at MAX_REF_ITEMS and sorted by
# name, which meant the two competed: a repository with 291 tags and 216
# branches spent 176 of its 200 slots on tags and showed 24 branches. The
# branch a person wanted was usually not in the list, and nothing said so.
MAX_BRANCH_ITEMS = 500
MAX_TAG_ITEMS = 500
MAX_REF_PAGES = 12
MAX_COMMIT_ITEMS = 25
MAX_TREE_ITEMS = 5_000

# Most recently updated first. A repository with hundreds of branches has a
# handful anybody is actually going to build, and they are the ones that moved
# recently — so if a budget has to bite, it should bite the stale end rather
# than everything after "f" in the alphabet.
_REF_SORT = "-target.date"


class BitbucketMetadataError(RuntimeError):
    """Bitbucket could not answer. ``status`` is the HTTP code where there was
    one, and None for a timeout or a connection failure — the difference decides
    whether retrying a variant of the request is sensible or just rude."""

    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.status = status


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


# Public alias. The merge-check writer needs exactly this scheme (Bearer for an
# OAuth/access token, Basic for an Atlassian API token) and must not carry a
# second copy of it — two implementations of one auth scheme drift, and the
# symptom is a 401 on a path nobody tests often.
authorization_header = _authorization_header


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
        raise BitbucketMetadataError(message, status=exc.code) from exc
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


def _collect_refs(
    base: str,
    kind: str,
    token: str,
    repository_ref: str,
    *,
    limit: int,
    credential_type: str,
    principal: str,
) -> list[dict]:
    """One kind of ref — branches or tags — with a budget of its own.

    Sorting by target date is an optimisation, not a requirement: a repository
    or a Bitbucket change that refuses the sort must still produce a list, so a
    rejected sort falls back to the unsorted endpoint rather than failing the
    whole listing.
    """
    url = f"{base}/refs/{kind}?{urlencode({'pagelen': 100, 'sort': _REF_SORT})}"
    try:
        return _collect(
            url,
            token,
            repository_ref,
            limit=limit,
            max_pages=MAX_REF_PAGES,
            credential_type=credential_type,
            principal=principal,
        )
    except BitbucketMetadataError as exc:
        # Only retry when Bitbucket REJECTED the request — a 4xx that is not
        # about the credential is what an unsupported sort field looks like.
        # A timeout, a 5xx or a rate limit means the endpoint is struggling,
        # and immediately asking it the same thing again makes that worse.
        if exc.status is None or exc.status in (401, 403, 404, 429) or exc.status >= 500:
            raise
        return _collect(
            f"{base}/refs/{kind}?{urlencode({'pagelen': 100})}",
            token,
            repository_ref,
            limit=limit,
            max_pages=MAX_REF_PAGES,
            credential_type=credential_type,
            principal=principal,
        )


def list_revisions(
    repository_ref: str,
    token: str,
    credential_type: str = "oauth",
    principal: str = "",
    kinds: tuple = ("branch", "tag", "commit"),
) -> dict:
    """Branches, tags and recent commits — or only the kinds asked for.

    ``kinds`` exists because the callers genuinely differ: Run Build offers all
    three, while a branch picker wants branches. Fetching five pages of tags to
    fill a branch dropdown is several seconds of somebody's time spent on a list
    they will not open.
    """
    base = f"{API_ORIGIN}/2.0/repositories/{repository_ref}"
    branch_rows = (
        _collect_refs(
            base,
            "branches",
            token,
            repository_ref,
            limit=MAX_BRANCH_ITEMS,
            credential_type=credential_type,
            principal=principal,
        )
        if "branch" in kinds
        else []
    )
    tag_rows = (
        _collect_refs(
            base,
            "tags",
            token,
            repository_ref,
            limit=MAX_TAG_ITEMS,
            credential_type=credential_type,
            principal=principal,
        )
        if "tag" in kinds
        else []
    )
    # Each row already knows which endpoint it came from; the /refs payload's
    # own `type` field is not relied on, so a provider that stops setting it
    # cannot silently turn every branch into an unknown kind.
    refs = [{**row, "type": "branch"} for row in branch_rows]
    refs += [{**row, "type": "tag"} for row in tag_rows]

    commits = (
        _collect(
            f"{base}/commits?{urlencode({'pagelen': MAX_COMMIT_ITEMS})}",
            token,
            repository_ref,
            limit=MAX_COMMIT_ITEMS,
            max_pages=1,
            credential_type=credential_type,
            principal=principal,
        )
        if "commit" in kinds
        else []
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
    return {
        "items": options,
        "count": len(options),
        # A budget that bit is worth saying: the list is the newest N, and
        # anything older is reachable by typing it rather than picking it.
        "truncated": {
            "branches": len(branch_rows) >= MAX_BRANCH_ITEMS,
            "tags": len(tag_rows) >= MAX_TAG_ITEMS,
        },
    }


def list_tree(
    repository_ref: str,
    token: str,
    revision: str,
    credential_type: str = "oauth",
    principal: str = "",
    *,
    max_depth: int = 8,
) -> dict:
    """Every file path in the repository at one revision.

    One paginated walk of ``/src/<rev>/`` — the same call Dockerfile discovery
    has always made, lifted out so anything that needs to know the SHAPE of a
    repository (which build files exist, whether there is a wrapper, how many
    modules) can have it without a clone.

    ``truncated`` is true when the walk hit its own ceiling rather than the end
    of the tree. A caller must treat an absent path as "not seen", never as
    "not there", when it is set.
    """
    clean_revision = _clean_text(revision, 256)
    if not clean_revision or any(ord(char) < 32 for char in clean_revision):
        raise ValueError("A valid branch, tag, or commit is required.")
    depth = max(1, min(int(max_depth or 8), 20))
    encoded_revision = quote(clean_revision, safe="")
    base = f"{API_ORIGIN}/2.0/repositories/{repository_ref}"
    tree = _collect(
        f"{base}/src/{encoded_revision}/?"
        f"{urlencode({'pagelen': 100, 'max_depth': depth})}",
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
        if path:
            paths.add(path)
    return {
        "paths": sorted(paths),
        "count": len(paths),
        "revision": clean_revision,
        "truncated": len(tree) >= MAX_TREE_ITEMS,
    }


def _is_dockerfile(path: str) -> bool:
    filename = path.rsplit("/", 1)[-1].lower()
    return (
        filename == "dockerfile"
        or filename.startswith("dockerfile.")
        or filename.endswith(".dockerfile")
    )


def list_dockerfiles(
    repository_ref: str,
    token: str,
    revision: str,
    credential_type: str = "oauth",
    principal: str = "",
) -> dict:
    tree = list_tree(
        repository_ref, token, revision, credential_type, principal
    )
    items = [
        {"value": path, "label": path}
        for path in tree["paths"]
        if _is_dockerfile(path)
    ]
    return {"items": items, "count": len(items), "revision": tree["revision"]}


# A source file read whole, rather than metadata about it. Capped well below
# MAX_RESPONSE_BYTES because the only caller wants a configuration file: a
# Jenkinsfile that does not fit in this is a program, not a declaration.
MAX_FILE_BYTES = 512 * 1024


def fetch_file(
    repository_ref: str,
    token: str,
    revision: str,
    path: str,
    credential_type: str = "oauth",
    principal: str = "",
) -> str:
    """One file's text at one revision.

    Returns the decoded source. Raises :class:`BitbucketMetadataError` with a
    message meant for a person when the file, the revision, or the credential
    is not what it should be.
    """
    clean_revision = _clean_text(revision, 256)
    if not clean_revision or any(ord(char) < 32 for char in clean_revision):
        raise ValueError("A valid branch, tag, or commit is required.")
    clean_path = str(path or "").strip().replace("\\", "/").lstrip("/")
    if not clean_path or ".." in clean_path.split("/") or "\x00" in clean_path:
        raise ValueError("A repository-relative file path is required.")

    url = (
        f"{API_ORIGIN}/2.0/repositories/{repository_ref}"
        f"/src/{quote(clean_revision, safe='')}/{quote(clean_path, safe='/')}"
    )
    safe_url = _validate_api_url(url, repository_ref)
    request = Request(
        safe_url,
        method="GET",
        headers={
            "Authorization": _authorization_header(token, credential_type, principal),
            # The src endpoint returns the raw file for a file path and JSON for
            # a directory, so nothing is asserted about the content type here.
            "Accept": "*/*",
            "User-Agent": "KubeSight/application-intelligence",
        },
    )
    try:
        with _open_api(request, timeout=20) as response:
            raw = response.read(MAX_FILE_BYTES + 1)
    except HTTPError as exc:
        if exc.code in {401, 403}:
            message = (
                "Bitbucket rejected this credential. Verify that it has read access "
                "to the repository."
            )
        elif exc.code == 404:
            message = (
                f"'{clean_path}' was not found on {clean_revision}. "
                "Check the path and the branch."
            )
        elif exc.code == 429:
            message = "Bitbucket rate-limited the request. Try again shortly."
        else:
            message = "Bitbucket could not return that file."
        raise BitbucketMetadataError(message) from exc
    except (URLError, TimeoutError) as exc:
        raise BitbucketMetadataError("Bitbucket is temporarily unavailable.") from exc
    if len(raw) > MAX_FILE_BYTES:
        raise BitbucketMetadataError(
            f"'{clean_path}' is larger than {MAX_FILE_BYTES // 1024} KB."
        )
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise BitbucketMetadataError(
            f"'{clean_path}' is not a text file."
        ) from exc
