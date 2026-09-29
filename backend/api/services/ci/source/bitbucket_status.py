"""The merge gate's own Bitbucket calls: two writes, and one read.

Everything else KubeSight does with Bitbucket is read-only, and deliberately so
— ``application_intelligence_bitbucket`` says as much in its first line. This is
the one module that writes, and it writes exactly two things:

*A commit build status*, which is what actually gates the merge. KubeSight does
not — and must not — try to block a merge itself: it has no way to stand between
a developer and the Merge button, and a gate that can be bypassed by clicking
Merge in a different tab is not a gate. Bitbucket's branch restriction "require
successful builds before merging" is the enforcement; a build status under a
stable key is how this feature reaches it. Filing the status under the SAME key
every time is what makes a re-run replace the previous verdict rather than sit
beside it contradicting itself.

*A pull request comment*, which is what explains the verdict to whoever has to
fix it. It gates nothing. It exists because "FAILED" on a status line is not an
answer to "what do I do now".

And it reads one thing: the repository's BRANCH RESTRICTIONS, to answer the only
question that matters about this feature — "is the merge actually blocked?"
KubeSight posting a red build status is necessary and not sufficient; until a
branch restriction requires passing builds, that red status is decoration and
anybody can still press Merge. KubeSight can create that restriction (below),
but it always looks rather than assumes, because a gate that cannot say whether
it is switched on is worse than no gate: it is a false sense of one.

Last, and only when somebody presses "Set up in Bitbucket": the repository's
WEBHOOK, created or corrected so it points at this service with its secret, and
the BRANCH RESTRICTIONS that make a merge wait for KubeSight's verdict.

The same protections the read client enforces apply here — HTTPS only, the API
origin pinned, no redirects followed, a response size cap — because a writer
that relaxed them would be the hole in an otherwise careful surface. The
difference is only the method and a JSON body.
"""

from __future__ import annotations

import json
from typing import Any, Dict, Optional
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

from ...application_intelligence_bitbucket import authorization_header

API_ORIGIN = "https://api.bitbucket.org"
MAX_RESPONSE_BYTES = 200_000
TIMEOUT_SECONDS = 20

# Bitbucket's own vocabulary for a build status. Merge check verdicts use three
# of the four; STOPPED is for an ordinary build that was cancelled.
STATE_SUCCESSFUL = "SUCCESSFUL"
STATE_FAILED = "FAILED"
STATE_INPROGRESS = "INPROGRESS"
STATE_STOPPED = "STOPPED"


class StatusWriteError(RuntimeError):
    """Bitbucket would not take the write.

    ``retryable`` is the whole point of this class: a 500 or a timeout is worth
    trying again in a minute, and a 401 is not — retrying a rejected credential
    forever is how an integration turns into a denial of service against its
    own host.
    """

    def __init__(self, message: str, *, status: Optional[int] = None, retryable: bool = True):
        super().__init__(message)
        self.status = status
        self.retryable = retryable


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, msg, headers, newurl):
        return None


_OPENER = build_opener(_NoRedirect)


def _validate_url(url: str, repository_ref: str) -> str:
    """Same check the read client applies, for the same reasons.

    The URL is built in this file from values that came out of the database, so
    this is a belt on top of braces — but a repository slug is user input, and
    the one place a writer must not be clever is in deciding that its own input
    is trustworthy.
    """
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
        raise StatusWriteError(
            "Refusing to write to an unexpected Bitbucket URL.", retryable=False
        )
    return url


def _request(
    url: str,
    repository_ref: str,
    body: Optional[Dict[str, Any]] = None,
    *,
    method: str = "POST",
    token: str,
    credential_type: str,
    principal: str = "",
) -> Dict[str, Any]:
    """One Bitbucket call, with every guard applied in one place.

    Shared by the writes and the one read rather than copied, because the URL
    validation and the retryable/permanent split are the parts that must not
    differ between them.
    """
    safe_url = _validate_url(url, repository_ref)
    payload = json.dumps(body).encode("utf-8") if body is not None else None
    try:
        header = authorization_header(token, credential_type, principal)
    except Exception as exc:  # noqa: BLE001 - the read client raises its own type
        raise StatusWriteError(str(exc), retryable=False) from exc

    headers = {
        "Authorization": header,
        "Accept": "application/json",
        "User-Agent": "KubeSight/merge-checks",
    }
    if payload is not None:
        headers["Content-Type"] = "application/json"

    request = Request(safe_url, data=payload, method=method, headers=headers)
    try:
        with _OPENER.open(request, timeout=TIMEOUT_SECONDS) as response:
            raw = response.read(MAX_RESPONSE_BYTES + 1)
    except HTTPError as exc:
        detail = ""
        try:
            detail = exc.read(2000).decode("utf-8", "replace")
        except Exception:  # noqa: BLE001 - the body is a nicety, not the error
            detail = ""
        if exc.code in {400, 401, 403, 404}:
            raise StatusWriteError(
                _permanent_message(exc.code, detail, method=method),
                status=exc.code,
                retryable=False,
            ) from exc
        raise StatusWriteError(
            f"Bitbucket returned HTTP {exc.code}.", status=exc.code, retryable=True
        ) from exc
    except (URLError, TimeoutError) as exc:
        raise StatusWriteError(
            "Bitbucket could not be reached.", retryable=True
        ) from exc
    if len(raw) > MAX_RESPONSE_BYTES:
        raise StatusWriteError("Bitbucket's response was unexpectedly large.")
    try:
        parsed = json.loads(raw.decode("utf-8")) if raw else {}
    except (UnicodeDecodeError, json.JSONDecodeError):
        # The write went through; only the echo was unreadable. Not an error —
        # reporting one here would make the engine post the status a second time.
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _post(
    url: str,
    repository_ref: str,
    body: Dict[str, Any],
    *,
    token: str,
    credential_type: str,
    principal: str = "",
) -> Dict[str, Any]:
    return _request(
        url,
        repository_ref,
        body,
        method="POST",
        token=token,
        credential_type=credential_type,
        principal=principal,
    )


def _permanent_message(code: int, detail: str, *, method: str = "POST") -> str:
    """Why this will not work, phrased for what was actually attempted.

    The write and the read fail the same way and mean different things: a 403
    on a build status means "this token cannot report a verdict", and a 403 on
    the branch-restriction read means "this token cannot see whether the gate
    is enforced" — which is not a reason to stop reporting verdicts. One
    message for both sent people to fix the wrong thing.
    """
    reading = method.upper() == "GET"
    if code in (401, 403):
        if reading:
            return (
                "Bitbucket would not let this credential read the repository's "
                "branch restrictions. Reading them needs repository admin scope; "
                "reporting verdicts does not, and still works without it."
            )
        return (
            "Bitbucket rejected this credential for writing. The credential "
            "profile this service uses needs write access to pull requests and "
            "build statuses — a read-only token cannot report a verdict."
        )
    if code == 404:
        if reading:
            return "Bitbucket could not find this repository."
        return (
            "Bitbucket could not find the repository, commit or pull request "
            "this verdict is about."
        )
    snippet = (detail or "").strip().replace("\n", " ")[:200]
    return f"Bitbucket rejected the request{': ' + snippet if snippet else '.'}"


# ---------------------------------------------------------------------------
# The two writes
# ---------------------------------------------------------------------------

def post_build_status(
    *,
    repository_ref: str,
    commit_sha: str,
    key: str,
    state: str,
    name: str,
    description: str,
    url: str,
    token: str,
    credential_type: str,
    principal: str = "",
) -> Dict[str, Any]:
    """File (or replace) a build status on one commit.

    Bitbucket keys a status by ``(commit, key)`` and overwrites on repeat, which
    is exactly the behaviour this feature wants: re-running the checks on the
    same commit corrects the verdict in place instead of leaving the old one
    beside it for somebody to read first.

    ``url`` is where a developer goes to see why. It is required by Bitbucket
    and it is the single most useful field on the status, so it points at the
    build in KubeSight rather than at KubeSight's front page.
    """
    if not commit_sha:
        raise StatusWriteError(
            "A build status needs the commit it is about.", retryable=False
        )
    endpoint = (
        f"{API_ORIGIN}/2.0/repositories/{repository_ref}/commit/"
        f"{quote(commit_sha, safe='')}/statuses/build"
    )
    body = {
        "key": key[:40],
        "state": state,
        "name": name[:255],
        # Bitbucket truncates this itself; doing it here keeps what is stored
        # and what is shown the same thing.
        "description": (description or "")[:255],
        "url": url,
    }
    return _post(
        endpoint,
        repository_ref,
        body,
        token=token,
        credential_type=credential_type,
        principal=principal,
    )


def post_pull_request_comment(
    *,
    repository_ref: str,
    pull_request_id: str,
    markdown: str,
    token: str,
    credential_type: str,
    principal: str = "",
) -> Dict[str, Any]:
    """Leave the explanation on the pull request itself."""
    if not pull_request_id:
        raise StatusWriteError(
            "A comment needs the pull request it belongs to.", retryable=False
        )
    endpoint = (
        f"{API_ORIGIN}/2.0/repositories/{repository_ref}/pullrequests/"
        f"{quote(str(pull_request_id), safe='')}/comments"
    )
    return _post(
        endpoint,
        repository_ref,
        {"content": {"raw": markdown[:32000]}},
        token=token,
        credential_type=credential_type,
        principal=principal,
    )


# ---------------------------------------------------------------------------
# The read: is the merge actually blocked?
# ---------------------------------------------------------------------------

# Bitbucket Cloud's name for "require passing builds before merging". The
# restriction carries a `value` — the minimum number of SUCCESSFUL build
# statuses the source commit must have — and a branch pattern it applies to.
BUILD_RESTRICTION_KIND = "require_passing_builds_to_merge"
# Premium only: turns every merge check on the branch — the passing-builds one
# included — from a warning beside the Merge button into a refusal. Without it,
# Bitbucket Cloud shows "0 of 1 builds passed" and still lets anybody merge.
ENFORCE_CHECKS_KIND = "enforce_merge_checks"
# Who may push straight to the branch. Empty users and groups = nobody, which
# is what leaves "merge a pull request" as the only way in.
PUSH_KIND = "push"


def fetch_build_restrictions(
    *,
    repository_ref: str,
    token: str,
    credential_type: str,
    principal: str = "",
) -> list:
    """Every "require passing builds" restriction on this repository — plus
    any "enforce merge checks" ones, tagged by ``kind``.

    Returns ``[{"kind": ..., "pattern": "master", "matchKind": "glob", "minimum": 1}, ...]``,
    empty when the repository has none — which is the answer that matters, and
    the one this whole read exists to distinguish from "we did not look".

    Bitbucket pages this endpoint; one page is enough. A repository with more
    than a hundred build restrictions has a problem this function is not going
    to help with, and reading further would turn a page render into a walk.
    """
    endpoint = (
        f"{API_ORIGIN}/2.0/repositories/{quote(repository_ref, safe='/')}"
        f"/branch-restrictions?pagelen=100"
    )
    payload = _request(
        endpoint,
        repository_ref,
        method="GET",
        token=token,
        credential_type=credential_type,
        principal=principal,
    )
    found = []
    for item in payload.get("values") or []:
        if not isinstance(item, dict):
            continue
        kind = item.get("kind")
        if kind not in (BUILD_RESTRICTION_KIND, ENFORCE_CHECKS_KIND):
            continue
        try:
            minimum = int(item.get("value") or 0)
        except (TypeError, ValueError):
            minimum = 0
        found.append(
            {
                "kind": kind,
                "pattern": str(item.get("pattern") or ""),
                "matchKind": str(item.get("branch_match_kind") or "glob"),
                "branchType": str(item.get("branch_type") or ""),
                "minimum": minimum,
            }
        )
    return found


# ---------------------------------------------------------------------------
# The repository webhook: set up by KubeSight with the service's own credential
# ---------------------------------------------------------------------------
#
# Creating the webhook here, rather than asking somebody to copy a URL and a
# secret into Bitbucket's form, is what removes the half-configured state this
# feature used to live in most often: a webhook with a stale secret, or with
# `updated` unticked, silently runs nothing. It is still a write, so it needs
# the same credential that reports verdicts — plus Bitbucket's webhook scope.

def _hooks_endpoint(repository_ref: str, uid: str = "") -> str:
    base = f"{API_ORIGIN}/2.0/repositories/{quote(repository_ref, safe='/')}/hooks"
    return f"{base}/{quote(uid, safe='')}" if uid else base


def _webhook_error(exc: StatusWriteError) -> StatusWriteError:
    """The webhook calls fail for a different reason than a build status does."""
    if exc.status in (401, 403):
        return StatusWriteError(
            "Bitbucket would not let this credential manage the repository's "
            "webhooks. It needs the webhook scope (read and write), which is "
            "repository admin on most token types.",
            status=exc.status,
            retryable=False,
        )
    return exc


def list_webhooks(
    *,
    repository_ref: str,
    token: str,
    credential_type: str,
    principal: str = "",
) -> list:
    """The repository's webhooks, reduced to what matching one needs.

    One page: a repository with more webhooks than Bitbucket puts on a page is
    not a case this setup step is going to reason about.
    """
    try:
        payload = _request(
            _hooks_endpoint(repository_ref),
            repository_ref,
            method="GET",
            token=token,
            credential_type=credential_type,
            principal=principal,
        )
    except StatusWriteError as exc:
        raise _webhook_error(exc) from exc
    hooks = []
    for item in payload.get("values") or []:
        if not isinstance(item, dict):
            continue
        hooks.append(
            {
                "uuid": str(item.get("uuid") or ""),
                "url": str(item.get("url") or ""),
                "description": str(item.get("description") or ""),
                "events": [str(e) for e in (item.get("events") or [])],
                "active": bool(item.get("active")),
                "secretSet": bool(item.get("secret_set")),
            }
        )
    return hooks


def save_webhook(
    *,
    repository_ref: str,
    uid: str = "",
    url: str,
    secret: str,
    events: list,
    description: str,
    token: str,
    credential_type: str,
    principal: str = "",
) -> Dict[str, Any]:
    """Create the webhook, or bring an existing one (``uid``) back in line.

    The secret is always sent. Bitbucket never returns it, so there is no way to
    tell a matching secret from a stale one except by setting it.
    """
    body = {
        "description": description[:255],
        "url": url,
        "active": True,
        "events": list(events),
        "secret": secret,
    }
    try:
        return _request(
            _hooks_endpoint(repository_ref, uid),
            repository_ref,
            body,
            method="PUT" if uid else "POST",
            token=token,
            credential_type=credential_type,
            principal=principal,
        )
    except StatusWriteError as exc:
        raise _webhook_error(exc) from exc


# ---------------------------------------------------------------------------
# Branch restrictions: set up by KubeSight so a merge waits for its verdict
# ---------------------------------------------------------------------------

def _restrictions_endpoint(repository_ref: str, restriction_id: str = "", kind: str = "") -> str:
    base = f"{API_ORIGIN}/2.0/repositories/{quote(repository_ref, safe='/')}/branch-restrictions"
    if restriction_id:
        return f"{base}/{quote(str(restriction_id), safe='')}"
    return f"{base}?kind={quote(kind, safe='')}&pagelen=100" if kind else base


def _restriction_error(exc: StatusWriteError) -> StatusWriteError:
    if exc.status in (401, 403):
        return StatusWriteError(
            "Bitbucket would not let this credential change the repository's "
            "branch restrictions. That needs repository admin scope.",
            status=exc.status,
            retryable=False,
        )
    return exc


def list_branch_restrictions(
    *,
    repository_ref: str,
    kind: str,
    token: str,
    credential_type: str,
    principal: str = "",
) -> list:
    """Glob restrictions of one kind, with the id needed to update one."""
    try:
        payload = _request(
            _restrictions_endpoint(repository_ref, kind=kind),
            repository_ref,
            method="GET",
            token=token,
            credential_type=credential_type,
            principal=principal,
        )
    except StatusWriteError as exc:
        raise _restriction_error(exc) from exc
    found = []
    for item in payload.get("values") or []:
        if not isinstance(item, dict) or item.get("kind") != kind:
            continue
        found.append(
            {
                "id": item.get("id"),
                "pattern": str(item.get("pattern") or ""),
                "matchKind": str(item.get("branch_match_kind") or "glob"),
                "value": item.get("value"),
            }
        )
    return found


def save_branch_restriction(
    *,
    repository_ref: str,
    restriction_id: str = "",
    kind: str,
    pattern: str,
    value: Optional[int] = None,
    token: str,
    credential_type: str,
    principal: str = "",
) -> Dict[str, Any]:
    """Create one glob restriction, or update an existing one by id."""
    body: Dict[str, Any] = {
        "kind": kind,
        "branch_match_kind": "glob",
        "pattern": pattern,
    }
    if value is not None:
        body["value"] = int(value)
    if kind == PUSH_KIND:
        body["users"] = []
        body["groups"] = []
    try:
        return _request(
            _restrictions_endpoint(repository_ref, str(restriction_id or "")),
            repository_ref,
            body,
            method="PUT" if restriction_id else "POST",
            token=token,
            credential_type=credential_type,
            principal=principal,
        )
    except StatusWriteError as exc:
        raise _restriction_error(exc) from exc
