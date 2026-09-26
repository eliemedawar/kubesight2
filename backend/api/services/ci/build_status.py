"""Reporting ordinary builds back to the source host as a commit build status.

Merge checks already post a verdict (key ``KUBESIGHT-MERGE``). This is the same
write for every other build: INPROGRESS when it starts running against a known
commit, then SUCCESSFUL / FAILED / STOPPED when it finishes — under its own key,
``KUBESIGHT-BUILD``, so the two never overwrite each other on one commit.

Three rules shape it:

* **Never fails a build.** Everything here is best effort. A source host that
  is down, a token that was revoked, a repository that moved — all logged, none
  raised. The build's outcome is decided by its stages and nothing else.
* **Never blocks the engine.** The HTTP call runs on a short-lived daemon
  thread with everything it needs copied out of the database first, so a slow
  Bitbucket costs the CI pass nothing.
* **Silent on read-only credentials.** Most CI credentials only clone, and that
  is correct; a status they cannot post is not a problem to shout about, so it
  is skipped at debug level.

Merge check builds are skipped: their verdict is reported by the merge checks
themselves, and a second FAILED status under a different key would block a
merge the check was configured only to warn about.
"""

from __future__ import annotations

import logging
import os
import re
import threading
from types import SimpleNamespace
from typing import Any, Callable, Dict, Optional

from ...models_ci import CiBuild

logger = logging.getLogger(__name__)

STATUS_KEY = "KUBESIGHT-BUILD"

# Build status -> the source port's vocabulary (see source.SourceProvider).
_PORT_STATE = {
    "running": "running",
    "success": "passed",
    "failed": "failed",
    "timeout": "failed",
    "cancelled": "stopped",
}

_SHA_RE = re.compile(r"^[0-9a-fA-F]{7,64}$")


def enabled() -> bool:
    return os.getenv("CI_BUILD_STATUS_REPORTING", "true").strip().lower() not in {
        "0",
        "false",
        "no",
        "off",
    }


def status_key() -> str:
    return (os.getenv("CI_BUILD_STATUS_KEY", "") or STATUS_KEY).strip()[:40] or STATUS_KEY


def _run_in_thread(job: Callable[[], None]) -> None:
    threading.Thread(target=job, name="ci-build-status", daemon=True).start()


# Replaced in tests to run the post inline.
_dispatch: Callable[[Callable[[], None]], None] = _run_in_thread


def build_url(build: CiBuild) -> str:
    from .merge_checks.delivery import public_base_url

    base = public_base_url()
    if not base:
        return ""
    return f"{base}/#/service-catalog/{build.service_id}/builds?build={build.id}"


def _description(build: CiBuild, state: str) -> str:
    if state == "running":
        return f"Build #{build.number} is running."
    if state == "passed":
        return f"Build #{build.number} succeeded."
    if state == "stopped":
        return f"Build #{build.number} was cancelled."
    if build.status == "timeout":
        return f"Build #{build.number} timed out."
    return f"Build #{build.number} failed."


def _prepare(build: CiBuild) -> Optional[Dict[str, Any]]:
    """Everything the post needs, or None when this build is not reported."""
    if not enabled():
        return None
    state = _PORT_STATE.get(build.status or "")
    if state is None:
        return None
    sha = str(build.commit_sha or "").strip()
    if not _SHA_RE.match(sha):
        return None  # Nothing resolved to report against yet.
    variables = (build.pipeline_snapshot or {}).get("variables") or {}
    if str(variables.get("KUBESIGHT_MERGE_CHECK") or "").lower() == "true":
        return None
    service = build.service
    if service is None or not service.source_ready():
        return None
    credential = service.credential_profile
    if credential is None or not credential.enabled:
        return None
    if credential.read_only:
        logger.debug(
            "Build %s: credential '%s' is read-only; not reporting a build status.",
            build.id,
            credential.name,
        )
        return None

    from . import source as source_port

    handler = source_port.get_provider(service.repository_provider)
    poster = getattr(handler, "post_check_verdict", None)
    if poster is None:
        return None
    url = build_url(build)
    if not url:
        logger.debug(
            "Build %s: no public URL configured (PUBLIC_BASE_URL); not reporting "
            "a build status.",
            build.id,
        )
        return None
    ref = handler.parse_repository_url(service.repository_url)
    return {
        "poster": poster,
        "ref": ref,
        # A detached copy: the post runs off the request/engine thread, where
        # the ORM row must not be touched.
        "credential": SimpleNamespace(
            name=credential.name,
            enabled=bool(credential.enabled),
            read_only=bool(credential.read_only),
            secret_cipher=credential.secret_cipher,
            credential_type=credential.credential_type,
            principal=credential.principal,
        ),
        "kwargs": {
            "commit_sha": sha,
            "status_key": status_key(),
            "state": state,
            "name": f"KubeSight build #{build.number}",
            "description": _description(build, state),
            "url": url,
        },
        "buildId": build.id,
    }


def report(build: CiBuild) -> None:
    """Post this build's current status, if it is one that is reported. Never raises."""
    try:
        job = _prepare(build)
    except Exception:  # noqa: BLE001 - reporting must never touch the build
        logger.debug("Build %s: could not prepare a build status", build.id, exc_info=True)
        return
    if job is None:
        return

    def post() -> None:
        try:
            job["poster"](job["ref"], job["credential"], **job["kwargs"])
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "Build %s: reporting the %s build status failed: %s",
                job["buildId"],
                job["kwargs"]["state"],
                exc,
            )

    try:
        _dispatch(post)
    except Exception:  # noqa: BLE001
        logger.debug("Build %s: could not dispatch the build status", build.id, exc_info=True)
