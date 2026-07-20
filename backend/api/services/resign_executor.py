"""Drives a Jenkins job that re-signs a mobile binary.

Shielding strips the code signature, and KubeSight cannot put it back: Android
needs the upload keystore and iOS needs macOS with a keychain. Jenkins already
has both — a Linux agent for ``jarsigner``/``apksigner``, a Mac agent for
``codesign`` — so signing stays there and KubeSight orchestrates it.

The contract with the job is deliberately small:

1. KubeSight triggers it with a source URL and a scoped token.
2. The job pulls the unsigned binary from that URL, signs it, and archives the
   signed file with ``archiveArtifacts``.
3. KubeSight polls the build, then downloads the archived artifact.

Nothing is POSTed back, so the job never has to know KubeSight's API shape — it
curls one URL and archives one file. The signing key never leaves the agent
that holds it.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Dict, Optional

from . import jenkins_client
from .jenkins_client import JenkinsConfig, JenkinsError

logger = logging.getLogger(__name__)

# Default build-parameter names, overridable per app because an existing job may
# already name them something else.
DEFAULT_SOURCE_URL_PARAM = "KUBESIGHT_SOURCE_URL"
DEFAULT_TOKEN_PARAM = "KUBESIGHT_TOKEN"

# Default glob for the signed artifact the job archives.
DEFAULT_RESULT_PATTERN = {"android": "*.aab", "ios": "*.ipa"}


class ResignExecutorError(Exception):
    """The signing job could not be triggered or read."""


@dataclass
class ResignJobSpec:
    """Everything needed to trigger one signing build."""

    resign_id: int
    build_id: int
    platform: str
    artifact_type: str
    job_path: str
    source_url: str
    token: str
    source_url_param: str = DEFAULT_SOURCE_URL_PARAM
    token_param: str = DEFAULT_TOKEN_PARAM
    artifact_type_param: str = ""
    extra_params: Optional[Dict[str, str]] = None


def build_params(spec: ResignJobSpec) -> Dict[str, str]:
    """The parameters sent to the job.

    Operator-supplied extras are applied first so they can never overwrite the
    source URL or the token — those are what make the run work at all.
    """
    params: Dict[str, str] = {}
    for key, value in (spec.extra_params or {}).items():
        name = str(key).strip()
        if name:
            params[name] = str(value)
    if spec.artifact_type_param:
        params[spec.artifact_type_param] = spec.artifact_type
    params[spec.source_url_param] = spec.source_url
    params[spec.token_param] = spec.token
    return params


def launch(cfg: JenkinsConfig, spec: ResignJobSpec) -> Dict[str, Any]:
    """Trigger the signing build. Returns the job reference to poll."""
    try:
        queue_url = jenkins_client.trigger_build(cfg, build_params(spec))
    except JenkinsError as exc:
        raise ResignExecutorError(str(exc)) from exc
    return {
        "kind": "jenkins",
        "jobPath": spec.job_path,
        "queueUrl": queue_url,
        "buildUrl": "",
        "buildNumber": None,
    }


def poll(cfg: JenkinsConfig, job_ref: Dict[str, Any]) -> Dict[str, Any]:
    """Where the signing build is now.

    ``{"phase": "queued"|"building"|"succeeded"|"failed", "detail": str}``, plus
    ``buildUrl``/``buildNumber`` once Jenkins has assigned them.
    """
    ref = dict(job_ref or {})
    build_url = ref.get("buildUrl") or ""

    # Still in the queue: resolve it to a build first.
    if not build_url:
        queue_url = ref.get("queueUrl") or ""
        if not queue_url:
            return {"phase": "failed", "detail": "no Jenkins queue reference was recorded"}
        try:
            state = jenkins_client.queue_state(cfg, queue_url)
        except JenkinsError as exc:
            raise ResignExecutorError(str(exc)) from exc
        if state.get("state") == "cancelled":
            return {"phase": "failed", "detail": "the signing build was cancelled in the queue"}
        if state.get("state") != "building":
            return {"phase": "queued", "detail": state.get("why") or "waiting for an executor"}
        ref["buildUrl"] = build_url = state.get("buildUrl") or ""
        ref["buildNumber"] = state.get("buildNumber")

    try:
        result = jenkins_client.build_state(cfg, build_url)
    except JenkinsError as exc:
        raise ResignExecutorError(str(exc)) from exc

    if result.get("building"):
        return {"phase": "building", "detail": "signing in progress", **_ref_fields(ref)}

    outcome = (result.get("result") or "").upper()
    if outcome == "SUCCESS":
        return {"phase": "succeeded", "detail": "signing build succeeded", **_ref_fields(ref)}
    if not outcome:
        # Finished but Jenkins has not written a result yet. Say "building"
        # rather than guessing — the next tick sees the real outcome.
        return {"phase": "building", "detail": "finishing", **_ref_fields(ref)}
    return {
        "phase": "failed",
        "detail": f"the signing build finished {outcome}",
        **_ref_fields(ref),
    }


def _ref_fields(ref: Dict[str, Any]) -> Dict[str, Any]:
    return {"buildUrl": ref.get("buildUrl") or "", "buildNumber": ref.get("buildNumber")}
