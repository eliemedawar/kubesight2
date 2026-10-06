"""Webhook triggers: CRUD, verification, and turning a delivery into builds.

A trigger is a saved Run build — pipeline, ref, build-input values — plus a URL
and a secret. When something calls the URL the delivery is verified, reduced to
a *plan* (which refs to build, with which inputs, or why nothing), and the plan
is executed through :func:`engine.trigger_build`, the same function the Run
build button and the schedules call. Readiness checks, parameter validation,
the snapshot, the audit entry and the dispatch wake-up are the ones every other
build gets.

Planning is separate from executing so the page can show what a body WOULD do
("Preview") with exactly the code a real delivery runs.

What a request may change is bounded by the person who saved the trigger:

* the ref only when ``allow_ref_override`` is on (or a mapping they wrote names
  it), and then only inside the branch / tag filters;
* build inputs only those listed in ``allowed_inputs`` (or mapped), each value
  still validated against the pipeline's declared parameters;
* everything else is fixed on the trigger.

A request that asks for more is REFUSED with the reason, never partly applied:
a build that silently ignored half of what it was asked would run differently
from what the caller believes it started.

Builds run as whoever last saved the trigger, re-checked on every delivery —
the rule schedules and Deploy-stage targets follow.
"""

from __future__ import annotations

import fnmatch
import hashlib
import hmac
import logging
import os
import re
import secrets as secrets_module
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

from ...audit import log_audit
from ...db import db
from ...models_ci import CiBuild, CiPipeline, CiService
from ...models_ci_webhooks import WEBHOOK_KINDS, CiWebhookDelivery, CiWebhookTrigger
from ...secret_encryption import decrypt_secret, encrypt_secret
from . import pipelines as pipelines_service
from . import schedules as schedules_service

logger = logging.getLogger(__name__)

MAX_TRIGGERS_PER_SERVICE = 20
MAX_NAME_CHARS = 120
MAX_FILTERS = 20
MAX_MAPPINGS = 25
MAX_ALLOWED_INPUTS = 25
# One push can carry dozens of refs (a tag sweep, a mirror). Each is a build,
# and a shared runner fleet should not take fifty from one request.
MAX_REFS_PER_DELIVERY = max(1, int(os.getenv("CI_WEBHOOK_MAX_REFS_PER_DELIVERY", "5") or 5))
# A sender stuck in a loop — or a script with a bug — must not queue builds
# faster than anybody can notice. Per trigger, over the last minute.
MAX_BUILDS_PER_MINUTE = max(1, int(os.getenv("CI_WEBHOOK_MAX_BUILDS_PER_MINUTE", "12") or 12))
DELIVERIES_KEPT = 50
MAX_BODY_BYTES = 1024 * 1024
MAX_PAYLOAD_PATHS = 200

REF_TARGETS = ("ref:branch", "ref:tag", "ref:commit")
_ACTIVE_BUILD_STATUSES = ("queued", "running")
_INPUT_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,63}$")
# What a ref may look like. Deliberately narrower than git allows: the value
# ends up as an argument to `git clone --branch`, so anything starting with a
# dash, carrying whitespace or a `..` is refused before it gets near a shell.
_REF_RE = re.compile(r"^[A-Za-z0-9._/@+-]{1,255}$")
_COMMIT_RE = re.compile(r"^[0-9a-fA-F]{7,64}$")
_PATH_RE = re.compile(r"^[A-Za-z0-9_$@-]+(?:\.[A-Za-z0-9_$@-]+)*$")

# Headers a sender uses to name its delivery, so a redelivery is recognised.
DELIVERY_KEY_HEADERS = (
    "X-Request-UUID",          # Bitbucket Cloud
    "X-GitHub-Delivery",       # GitHub
    "X-Gitlab-Event-UUID",     # GitLab
    "X-KubeSight-Delivery",
    "Idempotency-Key",
)


class WebhookError(ValueError):
    """A trigger could not be saved or used. Message is user-facing."""


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _aware(value: Optional[datetime]) -> Optional[datetime]:
    if value is None:
        return None
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def _iso(value: Optional[datetime]) -> Optional[str]:
    value = _aware(value)
    return value.isoformat() if value else None


def _clean(value: Any, limit: int) -> str:
    return " ".join(str(value or "").split())[:limit]


def _new_secret() -> str:
    return secrets_module.token_urlsafe(32)


def _new_public_id() -> str:
    return "wh_" + secrets_module.token_urlsafe(18)


# ---------------------------------------------------------------------------
# Reads
# ---------------------------------------------------------------------------

def get_trigger(service: CiService, trigger_id: int) -> CiWebhookTrigger:
    row = db.session.get(CiWebhookTrigger, int(trigger_id))
    if row is None or row.service_id != service.id:
        raise LookupError("Webhook not found.")
    return row


def inbound_path(row: CiWebhookTrigger) -> str:
    return f"/api/ci/hooks/{row.public_id}"


def _base_url() -> str:
    from .merge_checks import delivery

    return delivery.public_base_url()


_UNREACHABLE_HOSTS = ("localhost", "127.", "0.0.0.0", "[::1]", "::1")


def _base_problem(base: str) -> str:
    host = base.split("://", 1)[-1].split("/", 1)[0].split(":", 1)[0].lower() if base else ""
    if not base or any(host.startswith(prefix) for prefix in _UNREACHABLE_HOSTS):
        return (
            "This installation has no public address a sender outside it can reach. "
            "Set PUBLIC_BASE_URL to the address KubeSight is reachable at."
        )
    return ""


def inbound_url(row: CiWebhookTrigger) -> str:
    return f"{_base_url()}{inbound_path(row)}"


def _pipeline_problem(row: CiWebhookTrigger) -> Optional[str]:
    if not row.pipeline_id:
        return None
    pipeline = db.session.get(CiPipeline, int(row.pipeline_id))
    if pipeline is None or pipeline.service_id != row.service_id:
        return (
            f"The pipeline this webhook runs (#{row.pipeline_id}) no longer exists. "
            "Edit the webhook and pick another pipeline."
        )
    if (pipeline.purpose or "build") != "build":
        return f"Pipeline '{pipeline.name}' is a merge check pipeline, not a build pipeline."
    return None


def _build_brief(build: Optional[CiBuild]) -> Optional[Dict[str, Any]]:
    if build is None:
        return None
    return {"id": build.id, "number": build.number, "status": build.status, "branch": build.branch}


def trigger_to_dict(row: CiWebhookTrigger) -> Dict[str, Any]:
    pipeline = db.session.get(CiPipeline, int(row.pipeline_id)) if row.pipeline_id else None
    run_as = row.updated_by or row.created_by
    base = _base_url()
    return {
        "id": row.id,
        "serviceId": row.service_id,
        "name": row.name,
        "kind": row.kind,
        "url": f"{base}{inbound_path(row)}",
        "path": inbound_path(row),
        # Whether a sender outside this installation could reach the URL at
        # all — a loopback address is shown, but flagged.
        "urlProblem": _base_problem(base) or None,
        "secretSet": bool(row.secret_encrypted),
        "pipelineId": row.pipeline_id,
        "pipelineName": pipeline.name if pipeline else None,
        "pipelineProblem": _pipeline_problem(row),
        "branch": row.branch,
        "refType": row.ref_type or "branch",
        "variables": dict(row.variables or {}) if isinstance(row.variables, dict) else {},
        "allowedInputs": list(row.allowed_inputs or []),
        "allowRefOverride": bool(row.allow_ref_override),
        "mappings": [dict(item) for item in (row.mappings or []) if isinstance(item, dict)],
        "branchFilters": list(row.branch_filters or []),
        "buildTags": bool(row.build_tags),
        "tagFilters": list(row.tag_filters or []),
        "enabled": bool(row.enabled),
        "skipIfRunning": bool(row.skip_if_running),
        "lastDeliveryAt": _iso(row.last_delivery_at),
        "lastOutcome": row.last_outcome,
        "lastMessage": row.last_message,
        "lastBuild": _build_brief(row.last_build),
        "lastRejectedAt": _iso(row.last_rejected_at),
        "createdBy": row.created_by.username if row.created_by else None,
        "runsAs": run_as.username if run_as else None,
        "createdAt": _iso(row.created_at),
        "updatedAt": _iso(row.updated_at),
    }


def list_triggers(service: CiService) -> List[Dict[str, Any]]:
    rows = service.webhook_triggers.order_by(CiWebhookTrigger.name).all()
    return [trigger_to_dict(row) for row in rows]


def delivery_to_dict(row: CiWebhookDelivery) -> Dict[str, Any]:
    ids = [int(item) for item in (row.build_ids or []) if str(item).isdigit()]
    builds = {build.id: build for build in CiBuild.query.filter(CiBuild.id.in_(ids)).all()} if ids else {}
    return {
        "id": row.id,
        "receivedAt": _iso(row.received_at),
        "event": row.event,
        "deliveryKey": row.delivery_key,
        "outcome": row.outcome,
        "message": row.message,
        "refs": list(row.refs or []),
        "builds": [_build_brief(builds[item]) for item in ids if item in builds],
        "payloadPaths": list(row.payload_paths or []),
        "testedBy": row.tested_by.username if row.tested_by else None,
    }


def list_deliveries(row: CiWebhookTrigger, *, limit: int = 25) -> List[Dict[str, Any]]:
    items = (
        row.deliveries.order_by(CiWebhookDelivery.received_at.desc(), CiWebhookDelivery.id.desc())
        .limit(max(1, min(int(limit or 25), DELIVERIES_KEPT)))
        .all()
    )
    return [delivery_to_dict(item) for item in items]


# ---------------------------------------------------------------------------
# Writes
# ---------------------------------------------------------------------------

def _clean_patterns(value: Any, label: str) -> List[str]:
    if value in (None, ""):
        return []
    if isinstance(value, str):
        value = [part for part in re.split(r"[,\n]", value)]
    if not isinstance(value, list):
        raise WebhookError(f"{label} must be a list of patterns.")
    out: List[str] = []
    for item in value:
        text = str(item or "").strip()
        if not text:
            continue
        if len(text) > 255 or any(ch.isspace() for ch in text):
            raise WebhookError(f"'{text[:40]}' is not a usable {label.lower()[:-1]} pattern.")
        if text not in out:
            out.append(text)
    if len(out) > MAX_FILTERS:
        raise WebhookError(f"At most {MAX_FILTERS} {label.lower()}.")
    return out


def _clean_mappings(value: Any) -> List[Dict[str, str]]:
    if value in (None, ""):
        return []
    if not isinstance(value, list):
        raise WebhookError("Mappings must be a list of {target, path}.")
    out: List[Dict[str, str]] = []
    seen = set()
    for item in value:
        if not isinstance(item, dict):
            raise WebhookError("Each mapping is a {target, path} pair.")
        target = str(item.get("target") or "").strip()
        path = _normalise_path(item.get("path"))
        if not target and not path:
            continue  # an empty row the form left behind
        if not path or not _PATH_RE.match(path):
            raise WebhookError(
                f"'{item.get('path') or ''}' is not a path into the body. Write it like "
                "release.tag_name or push.changes.0.new.name."
            )
        if target not in REF_TARGETS and not _INPUT_NAME_RE.match(target):
            raise WebhookError(f"'{target}' is not a build input name or a ref (branch, tag, commit).")
        if target in seen:
            raise WebhookError(f"'{_target_label(target)}' is mapped twice.")
        seen.add(target)
        out.append({"target": target, "path": path})
    if len(out) > MAX_MAPPINGS:
        raise WebhookError(f"At most {MAX_MAPPINGS} mappings.")
    if "ref:branch" in seen and "ref:tag" in seen:
        raise WebhookError("Map the branch or the tag, not both — a build checks out one ref.")
    return out


def _target_label(target: str) -> str:
    return {"ref:branch": "Branch", "ref:tag": "Tag", "ref:commit": "Commit"}.get(target, target)


def _normalise_path(value: Any) -> str:
    """``a[0].b`` and ``$.a.0.b`` both become ``a.0.b``."""
    text = str(value or "").strip()
    if text.startswith("$."):
        text = text[2:]
    text = re.sub(r"\[(\d+)\]", r".\1", text)
    return text.strip(".")


def _clean_inputs(value: Any) -> List[str]:
    if value in (None, ""):
        return []
    if isinstance(value, str):
        value = re.split(r"[,\s]+", value)
    if not isinstance(value, list):
        raise WebhookError("Allowed inputs must be a list of names.")
    out: List[str] = []
    for item in value:
        name = str(item or "").strip()
        if not name:
            continue
        if not _INPUT_NAME_RE.match(name):
            raise WebhookError(f"'{name[:40]}' is not a build input name.")
        if name not in out:
            out.append(name)
    if len(out) > MAX_ALLOWED_INPUTS:
        raise WebhookError(f"At most {MAX_ALLOWED_INPUTS} inputs.")
    return out


def _stringify(value: Any) -> str:
    if value is True:
        return "true"
    if value is False:
        return "false"
    return str(value if value is not None else "")


def _apply(row: CiWebhookTrigger, service: CiService, payload: Dict[str, Any], *, creating: bool) -> None:
    if creating or "name" in payload:
        name = _clean(payload.get("name"), MAX_NAME_CHARS + 1)
        if not name:
            raise WebhookError("Give the webhook a name, e.g. 'Release tool' or 'Build on push'.")
        if len(name) > MAX_NAME_CHARS:
            raise WebhookError(f"A webhook name is at most {MAX_NAME_CHARS} characters.")
        clash = CiWebhookTrigger.query.filter(
            CiWebhookTrigger.service_id == service.id,
            db.func.lower(CiWebhookTrigger.name) == name.lower(),
            CiWebhookTrigger.id != (row.id or 0),
        ).first()
        if clash is not None:
            raise WebhookError(f"This service already has a webhook called '{clash.name}'.")
        row.name = name

    if creating:
        kind = str(payload.get("kind") or "generic").strip().lower()
        if kind not in WEBHOOK_KINDS:
            raise WebhookError("A webhook is either 'generic' or 'bitbucket_push'.")
        row.kind = kind
    elif "kind" in payload and str(payload.get("kind") or "") != row.kind:
        # The URL is registered somewhere as one or the other; changing what
        # it means under the sender is a new webhook, not an edit.
        raise WebhookError("A webhook's kind cannot change. Create a new one instead.")

    if "pipelineId" in payload:
        raw = payload.get("pipelineId")
        if raw in (None, "", 0):
            row.pipeline_id = None
        else:
            try:
                pipeline_id = int(raw)
            except (TypeError, ValueError):
                raise WebhookError("Pick a pipeline from the list.")
            pipeline = db.session.get(CiPipeline, pipeline_id)
            if pipeline is None or pipeline.service_id != service.id:
                raise WebhookError("That pipeline does not belong to this service.")
            if (pipeline.purpose or "build") != "build":
                raise WebhookError(
                    f"Pipeline '{pipeline.name}' is a merge check pipeline. A webhook runs a build pipeline."
                )
            row.pipeline_id = pipeline.id

    if creating or "refType" in payload:
        ref_type = _clean(payload.get("refType"), 8).lower() or "branch"
        if ref_type not in ("branch", "tag"):
            raise WebhookError("A webhook builds a branch or a tag.")
        row.ref_type = ref_type
    if creating or "branch" in payload:
        branch = _clean(payload.get("branch"), 255)
        if branch and not _valid_ref(branch):
            raise WebhookError(f"'{branch}' is not a usable branch or tag name.")
        row.branch = branch or None
    if row.kind == "generic" and row.ref_type == "tag" and not row.branch:
        raise WebhookError("Name the tag to build — a tag has no default to fall back to.")

    if creating or "variables" in payload:
        raw = payload.get("variables")
        if raw in (None, ""):
            raw = {}
        if not isinstance(raw, dict):
            raise WebhookError("Build inputs must be an object of name to value.")
        row.variables = {str(key): _stringify(value) for key, value in raw.items()}

    if creating or "allowedInputs" in payload:
        row.allowed_inputs = _clean_inputs(payload.get("allowedInputs"))
    if creating or "mappings" in payload:
        row.mappings = _clean_mappings(payload.get("mappings"))
    if creating or "branchFilters" in payload:
        row.branch_filters = _clean_patterns(payload.get("branchFilters"), "Branch filters")
    if creating or "tagFilters" in payload:
        row.tag_filters = _clean_patterns(payload.get("tagFilters"), "Tag filters")

    for key, column, default in (
        ("enabled", "enabled", True),
        ("skipIfRunning", "skip_if_running", False),
        ("allowRefOverride", "allow_ref_override", False),
        ("buildTags", "build_tags", False),
    ):
        if key in payload:
            setattr(row, column, bool(payload.get(key)))
        elif creating:
            setattr(row, column, default)

    if row.kind == "bitbucket_push":
        # A push names its own ref and carries no build inputs, so the
        # request-shaping fields mean nothing here. Cleared, so a kind that
        # does not use them never shows them as if they applied.
        row.allowed_inputs, row.mappings, row.allow_ref_override = [], [], False
        row.branch, row.ref_type = None, "branch"
        if not service.repository_url:
            raise WebhookError(
                "Connect a repository on the Source tab first — a push webhook builds that repository."
            )
        if (service.repository_provider or "bitbucket") != "bitbucket":
            raise WebhookError("Push webhooks are for Bitbucket repositories. Use a generic webhook instead.")

    _check_inputs(row, service)


def _check_inputs(row: CiWebhookTrigger, service: CiService) -> None:
    """Fixed values, allowed inputs and mapped inputs, checked at save time.

    The delivery checks again (the pipeline can change in between), but a
    webhook that refuses every call because of a typo made today should say so
    today, in front of the person who made it.
    """
    pipeline = schedules_service._parameter_pipeline(service, row.pipeline_id)
    if pipeline is None:
        return
    try:
        pipelines_service.validate_parameter_values(pipeline, row.variables or None)
    except pipelines_service.PipelineError as exc:
        raise WebhookError(str(exc))
    declared = {param.get("name") for param in pipelines_service.parameter_definitions(pipeline)}
    if not declared:
        return  # a pipeline that declares nothing takes free-form variables
    named = list(row.allowed_inputs or []) + [
        item["target"] for item in (row.mappings or []) if item.get("target") not in REF_TARGETS
    ]
    for name in named:
        if name not in declared and name not in pipelines_service.RESERVED_VARIABLES:
            raise WebhookError(
                f"The pipeline has no build input named '{name}'. Its inputs are: "
                + ", ".join(sorted(declared))
                + "."
            )


def _audit_details(row: CiWebhookTrigger, service: CiService) -> Dict[str, Any]:
    return {
        "service": service.slug,
        "webhook": row.name,
        "kind": row.kind,
        "pipelineId": row.pipeline_id,
        "branch": row.branch,
        "refType": row.ref_type,
        "enabled": bool(row.enabled),
        "skipIfRunning": bool(row.skip_if_running),
        "allowRefOverride": bool(row.allow_ref_override),
        "allowedInputs": list(row.allowed_inputs or []),
        "mappings": [item.get("target") for item in (row.mappings or [])],
        "branchFilters": list(row.branch_filters or []),
        "buildTags": bool(row.build_tags),
        "tagFilters": list(row.tag_filters or []),
        # Names only, as for schedules.
        "variables": sorted((row.variables or {}).keys()),
    }


def create_trigger(service: CiService, payload: Dict[str, Any], *, actor=None) -> Dict[str, Any]:
    if service.webhook_triggers.count() >= MAX_TRIGGERS_PER_SERVICE:
        raise WebhookError(f"A service may have at most {MAX_TRIGGERS_PER_SERVICE} webhooks.")
    row = CiWebhookTrigger(service_id=service.id, variables={}, allowed_inputs=[], mappings=[])
    _apply(row, service, payload or {}, creating=True)
    secret = _new_secret()
    row.public_id = _new_public_id()
    row.secret_encrypted = encrypt_secret(secret)
    row.created_by_user_id = getattr(actor, "id", None)
    row.updated_by_user_id = getattr(actor, "id", None)
    db.session.add(row)
    db.session.commit()
    log_audit(
        "ci_webhook_created",
        actor=actor,
        target_type="ci_webhook_trigger",
        target_id=str(row.id),
        details=_audit_details(row, service),
    )
    # The secret is returned once here so the person creating it can paste it
    # straight into the sender; afterwards it is behind the audited reveal.
    return {**trigger_to_dict(row), "secret": secret}


def update_trigger(row: CiWebhookTrigger, payload: Dict[str, Any], *, actor=None) -> Dict[str, Any]:
    service = row.service
    before = _audit_details(row, service)
    _apply(row, service, payload or {}, creating=False)
    row.updated_by_user_id = getattr(actor, "id", None) or row.updated_by_user_id
    db.session.add(row)
    db.session.commit()
    after = _audit_details(row, service)
    log_audit(
        "ci_webhook_updated",
        actor=actor,
        target_type="ci_webhook_trigger",
        target_id=str(row.id),
        details={**after, "changed": sorted(key for key in after if after[key] != before.get(key))},
    )
    return trigger_to_dict(row)


def delete_trigger(row: CiWebhookTrigger, *, actor=None) -> None:
    service = row.service
    details = _audit_details(row, service)
    trigger_id = row.id
    db.session.delete(row)
    db.session.commit()
    log_audit(
        "ci_webhook_deleted",
        actor=actor,
        target_type="ci_webhook_trigger",
        target_id=str(trigger_id),
        details=details,
    )


def rotate_secret(row: CiWebhookTrigger, *, actor=None) -> str:
    value = _new_secret()
    row.secret_encrypted = encrypt_secret(value)
    db.session.add(row)
    db.session.commit()
    log_audit(
        "ci_webhook_secret_rotated",
        actor=actor,
        target_type="ci_webhook_trigger",
        target_id=str(row.id),
        details={"service": row.service.slug, "webhook": row.name},
    )
    return value


def reveal_secret(row: CiWebhookTrigger, *, actor=None) -> str:
    value = decrypt_secret(row.secret_encrypted or "")
    if not value:
        raise WebhookError("This webhook has no secret. Rotate it to create one.")
    log_audit(
        "ci_webhook_secret_revealed",
        actor=actor,
        target_type="ci_webhook_trigger",
        target_id=str(row.id),
        details={"service": row.service.slug, "webhook": row.name},
    )
    return value


# ---------------------------------------------------------------------------
# Verification
# ---------------------------------------------------------------------------

def verify(
    row: CiWebhookTrigger,
    *,
    provided: str = "",
    signatures: Optional[List[str]] = None,
    raw_body: Optional[bytes] = None,
) -> bool:
    """Is this call from somebody holding the secret? Constant-time throughout.

    Accepted proofs, strongest first:

    * a signature — ``sha256=<hex HMAC of the raw body>``, as Bitbucket sends
      in ``X-Hub-Signature`` and GitHub in ``X-Hub-Signature-256``; it never
      carries the secret itself;
    * the secret verbatim — ``X-KubeSight-Secret``, ``Authorization: Bearer``,
      GitLab's ``X-Gitlab-Token``, or ``?secret=`` for a sender that can do
      nothing else.

    No stored secret rejects everything: this URL starts builds.
    """
    stored = decrypt_secret(row.secret_encrypted or "")
    if not stored:
        return False
    for signature in signatures or []:
        algorithm, _, digest = str(signature or "").strip().partition("=")
        if algorithm.strip().lower() != "sha256" or not digest or raw_body is None:
            continue
        expected = hmac.new(stored.encode("utf-8"), raw_body, hashlib.sha256).hexdigest()
        if hmac.compare_digest(digest.strip().lower().encode("utf-8"), expected.encode("utf-8")):
            return True
    if provided:
        return hmac.compare_digest(str(provided).encode("utf-8"), stored.encode("utf-8"))
    return False


# ---------------------------------------------------------------------------
# Planning: what a body asks for, or why nothing
# ---------------------------------------------------------------------------

def _valid_ref(value: str) -> bool:
    return bool(_REF_RE.match(value)) and not value.startswith("-") and ".." not in value


def matches(patterns: List[str], name: str) -> bool:
    """No patterns means everything; fnmatch, as merge checks use."""
    if not patterns:
        return True
    return any(fnmatch.fnmatchcase(name or "", pattern) for pattern in patterns)


def lookup(payload: Any, path: str) -> Tuple[bool, Any]:
    """The value at a dotted path (``a.0.b``), and whether it was there."""
    current = payload
    for part in _normalise_path(path).split("."):
        if isinstance(current, dict) and part in current:
            current = current[part]
        elif isinstance(current, list) and part.isdigit() and int(part) < len(current):
            current = current[int(part)]
        else:
            return False, None
    return True, current


def payload_paths(payload: Any, *, limit: int = MAX_PAYLOAD_PATHS) -> List[str]:
    """Every dotted path to a scalar in a body — its shape, never its values."""
    out: List[str] = []

    def walk(node: Any, prefix: str, depth: int) -> None:
        if len(out) >= limit or depth > 8:
            return
        if isinstance(node, dict):
            for key, value in node.items():
                key = str(key)
                if not re.match(r"^[A-Za-z0-9_$@-]+$", key):
                    continue
                walk(value, f"{prefix}.{key}" if prefix else key, depth + 1)
        elif isinstance(node, list):
            # The first few elements describe a list's shape; all of them would
            # flood the picker with changes.0 … changes.40.
            for index, value in enumerate(node[:3]):
                walk(value, f"{prefix}.{index}" if prefix else str(index), depth + 1)
        elif prefix:
            out.append(prefix)

    walk(payload, "", 0)
    return out


def _plan(
    kind: str,
    refs: List[Dict[str, Any]],
    *,
    outcome: str = "build",
    message: str = "",
    notes: Optional[List[str]] = None,
    builds: Optional[List[Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    return {
        "kind": kind,
        "outcome": outcome,
        "message": message,
        "refs": refs,
        "builds": builds or [],
        "notes": notes or [],
    }


def _ref_from(target: str, raw: str) -> Tuple[str, str]:
    """(ref type, name) for a mapped/override ref value, ``refs/...`` understood."""
    value = str(raw or "").strip()
    if value.startswith("refs/tags/"):
        return "tag", value[len("refs/tags/"):]
    if value.startswith("refs/heads/"):
        return "branch", value[len("refs/heads/"):]
    return ("tag" if target == "ref:tag" else "branch"), value


def plan_generic(row: CiWebhookTrigger, service: CiService, payload: Dict[str, Any]) -> Dict[str, Any]:
    notes: List[str] = []
    ref_type = row.ref_type or "branch"
    ref = row.branch or (service.default_branch or "main")
    commit = ""
    variables: Dict[str, str] = dict(row.variables or {})

    def refused(message: str) -> Dict[str, Any]:
        return _plan("generic", [], outcome="refused", message=message, notes=notes)

    # 1. Mappings the trigger's owner wrote: lift values out of whatever the
    #    sender sends. A path that is missing leaves the default and says so.
    for mapping in row.mappings or []:
        target, path = mapping.get("target"), mapping.get("path")
        found, value = lookup(payload, path)
        if not found or value is None or value == "":
            notes.append(f"{_target_label(target)}: nothing at '{path}', so the default stands.")
            continue
        if isinstance(value, (dict, list)):
            return refused(f"'{path}' is an object or a list, not a value {_target_label(target)} can take.")
        text = _stringify(value)
        if target == "ref:commit":
            commit = text
        elif target in ("ref:branch", "ref:tag"):
            ref_type, ref = _ref_from(target, text)
        else:
            variables[target] = text

    # 2. What the body says outright, only where the trigger allows it.
    asked_ref = [key for key in ("branch", "tag", "commit") if payload.get(key) not in (None, "")]
    if asked_ref and not row.allow_ref_override:
        return refused(
            "This webhook always builds "
            + (f"tag {row.branch}" if row.ref_type == "tag" else (row.branch or "the default branch"))
            + f"; the request may not choose the ref ('{asked_ref[0]}' was sent). "
            "Allow it in the webhook's settings, or leave it out."
        )
    if payload.get("branch") not in (None, "") and payload.get("tag") not in (None, ""):
        return refused("Send a branch or a tag, not both — a build checks out one ref.")
    if payload.get("branch") not in (None, ""):
        ref_type, ref = _ref_from("ref:branch", _stringify(payload.get("branch")))
    if payload.get("tag") not in (None, ""):
        ref_type, ref = _ref_from("ref:tag", _stringify(payload.get("tag")))
    if payload.get("commit") not in (None, ""):
        commit = _stringify(payload.get("commit")).strip()

    sent = payload.get("variables")
    if sent not in (None, ""):
        if not isinstance(sent, dict):
            return refused("'variables' must be an object of input name to value.")
        allowed = set(row.allowed_inputs or [])
        for name, value in sent.items():
            name = str(name)
            if name not in allowed:
                return refused(
                    f"This webhook may not set '{name}'. "
                    + (
                        "It may set: " + ", ".join(sorted(allowed)) + "."
                        if allowed
                        else "It sets no build inputs from the request; allow them in its settings."
                    )
                )
            if isinstance(value, (dict, list)):
                return refused(f"'{name}' must be a single value.")
            variables[name] = _stringify(value)

    # 3. The ref has to be one a build can check out, and one the owner allows.
    if not _valid_ref(ref):
        return refused(f"'{ref[:80]}' is not a usable {ref_type} name.")
    if commit and not _COMMIT_RE.match(commit):
        return refused(f"'{commit[:80]}' is not a commit hash.")
    refs = [{"type": ref_type, "name": ref, "commit": commit or None}]
    if ref_type == "branch" and not matches(list(row.branch_filters or []), ref):
        return _plan(
            "generic", refs, outcome="ignored",
            message=f"Branch '{ref}' is outside this webhook's branch filters "
            f"({', '.join(row.branch_filters or [])}).",
            notes=notes,
        )
    if ref_type == "tag" and not matches(list(row.tag_filters or []), ref):
        return _plan(
            "generic", refs, outcome="ignored",
            message=f"Tag '{ref}' is outside this webhook's tag filters "
            f"({', '.join(row.tag_filters or [])}).",
            notes=notes,
        )

    return _plan(
        "generic",
        refs,
        builds=[{"refType": ref_type, "ref": ref, "commit": commit or None, "variables": variables}],
        notes=notes,
    )


def _push_ref_type(raw: Any) -> str:
    return "tag" if "tag" in str(raw or "").lower() else "branch"


def plan_bitbucket_push(
    row: CiWebhookTrigger, service: CiService, payload: Dict[str, Any], *, event: str = ""
) -> Dict[str, Any]:
    if event and event != "repo:push":
        return _plan("bitbucket_push", [], outcome="ignored", message=f"'{event}' is not a push; nothing to build.")
    push = payload.get("push") if isinstance(payload.get("push"), dict) else None
    changes = push.get("changes") if push else None
    if not isinstance(changes, list):
        return _plan("bitbucket_push", [], outcome="ignored", message="This body carries no push.")

    # A webhook pointed at the wrong repository builds the wrong code. The
    # repository in the body has to be the one this service builds.
    repository = payload.get("repository") if isinstance(payload.get("repository"), dict) else {}
    pushed_repo = str(repository.get("full_name") or "").strip().lower()
    expected = _repository_full_name(service)
    if pushed_repo and expected and pushed_repo != expected:
        return _plan(
            "bitbucket_push", [], outcome="ignored",
            message=f"This push is to {pushed_repo}; this service builds {expected}.",
        )

    notes: List[str] = []
    refs: List[Dict[str, Any]] = []
    builds: List[Dict[str, Any]] = []
    variables = dict(row.variables or {})
    for change in changes:
        if not isinstance(change, dict):
            continue
        new = change.get("new") if isinstance(change.get("new"), dict) else None
        if new is None:
            old = change.get("old") if isinstance(change.get("old"), dict) else {}
            if old.get("name"):
                notes.append(f"{_push_ref_type(old.get('type'))} {old.get('name')} was deleted; nothing to build.")
            continue
        ref_type = _push_ref_type(new.get("type"))
        name = str(new.get("name") or "").strip()
        target = new.get("target") if isinstance(new.get("target"), dict) else {}
        commit = str(target.get("hash") or "").strip()
        entry = {"type": ref_type, "name": name, "commit": commit or None}
        refs.append(entry)
        if not name or not _valid_ref(name):
            notes.append(f"'{name[:80]}' is not a usable ref name; skipped.")
            continue
        if commit and not _COMMIT_RE.match(commit):
            notes.append(f"{name}: '{commit[:80]}' is not a commit hash; skipped.")
            continue
        if ref_type == "tag":
            if not row.build_tags:
                notes.append(f"Tag {name}: this webhook does not build tags.")
                continue
            if not matches(list(row.tag_filters or []), name):
                notes.append(f"Tag {name} is outside the tag filters.")
                continue
        elif not matches(list(row.branch_filters or []), name):
            notes.append(f"Branch {name} is outside the branch filters.")
            continue
        builds.append({"refType": ref_type, "ref": name, "commit": commit or None, "variables": variables})

    if len(builds) > MAX_REFS_PER_DELIVERY:
        notes.append(
            f"{len(builds)} refs matched; only the first {MAX_REFS_PER_DELIVERY} are built "
            "(CI_WEBHOOK_MAX_REFS_PER_DELIVERY)."
        )
        builds = builds[:MAX_REFS_PER_DELIVERY]
    if not builds:
        return _plan(
            "bitbucket_push", refs, outcome="ignored",
            message=notes[-1] if notes else "Nothing in this push matches the webhook's filters.",
            notes=notes,
        )
    return _plan("bitbucket_push", refs, builds=builds, notes=notes)


def _repository_full_name(service: CiService) -> str:
    if not service.repository_url:
        return ""
    try:
        from . import source as source_port

        handler = source_port.get_provider(service.repository_provider or "bitbucket")
        return handler.parse_repository_url(service.repository_url).full_name.lower()
    except Exception:  # noqa: BLE001 - an unparseable URL just skips the check
        return ""


def plan(row: CiWebhookTrigger, payload: Dict[str, Any], *, event: str = "") -> Dict[str, Any]:
    service = row.service
    if row.kind == "bitbucket_push":
        return plan_bitbucket_push(row, service, payload, event=event)
    return plan_generic(row, service, payload)


# ---------------------------------------------------------------------------
# Executing a plan
# ---------------------------------------------------------------------------

def _recent_build_count(row: CiWebhookTrigger, now: datetime) -> int:
    since = now - timedelta(minutes=1)
    rows = row.deliveries.filter(
        CiWebhookDelivery.received_at >= since, CiWebhookDelivery.outcome == "triggered"
    ).all()
    return sum(len(item.build_ids or []) for item in rows)


def _already_built(row: CiWebhookTrigger, build: Dict[str, Any], now: datetime) -> Optional[int]:
    """A build of this exact ref and commit this trigger queued in the last
    ten minutes — how a redelivery without a delivery id is recognised."""
    if not build.get("commit"):
        return None
    since = now - timedelta(minutes=10)
    for delivery in row.deliveries.filter(
        CiWebhookDelivery.received_at >= since, CiWebhookDelivery.outcome == "triggered"
    ).all():
        for ref in delivery.refs or []:
            if (
                ref.get("name") == build["ref"]
                and ref.get("type") == build["refType"]
                and ref.get("commit") == build["commit"]
                and delivery.build_ids
            ):
                return int(delivery.build_ids[0])
    return None


def _record(
    row: CiWebhookTrigger,
    *,
    outcome: str,
    message: str,
    event: str,
    delivery_key: str = "",
    refs: Optional[List[Dict[str, Any]]] = None,
    build_ids: Optional[List[int]] = None,
    paths: Optional[List[str]] = None,
    tested_by=None,
    now: Optional[datetime] = None,
) -> CiWebhookDelivery:
    now = now or _now()
    delivery = CiWebhookDelivery(
        trigger_id=row.id,
        received_at=now,
        event=(event or "")[:64] or None,
        delivery_key=(delivery_key or "")[:128] or None,
        outcome=outcome,
        message=(message or "")[:2000] or None,
        refs=refs or [],
        build_ids=list(build_ids or []),
        payload_paths=list(paths or []),
        tested_by_user_id=getattr(tested_by, "id", None),
    )
    db.session.add(delivery)
    row = db.session.get(CiWebhookTrigger, row.id)
    row.last_delivery_at = now
    row.last_outcome = outcome
    row.last_message = (message or "")[:2000] or None
    if build_ids:
        row.last_build_id = build_ids[-1]
    db.session.add(row)
    db.session.commit()
    _prune(row)
    return delivery


def _prune(row: CiWebhookTrigger) -> None:
    stale = (
        row.deliveries.order_by(CiWebhookDelivery.received_at.desc(), CiWebhookDelivery.id.desc())
        .offset(DELIVERIES_KEPT)
        .all()
    )
    if stale:
        for item in stale:
            db.session.delete(item)
        db.session.commit()


def _result(
    outcome: str, message: str, *, plan_doc: Optional[Dict[str, Any]] = None,
    builds: Optional[List[Dict[str, Any]]] = None, delivery: Optional[CiWebhookDelivery] = None,
) -> Dict[str, Any]:
    return {
        "triggered": outcome == "triggered",
        "outcome": outcome,
        "message": message,
        "builds": builds or [],
        "notes": list((plan_doc or {}).get("notes") or []),
        "deliveryId": delivery.id if delivery else None,
    }


def execute(
    row: CiWebhookTrigger,
    payload: Dict[str, Any],
    *,
    event: str = "",
    delivery_key: str = "",
    tested_by=None,
) -> Dict[str, Any]:
    """Turn one verified delivery into builds, and record what happened.

    ``tested_by`` is the person pressing "Start a test build" on the page: the
    build then runs as them (they are right there, with their own rights) and
    a switched-off webhook still runs, because testing it before switching it
    on is the point.
    """
    from .engine import BuildError, trigger_build

    now = _now()
    service = row.service
    paths = payload_paths(payload) if row.kind == "generic" else []
    event = event or ("test" if tested_by else row.kind)

    def done(outcome: str, message: str, plan_doc=None, refs=None, builds=None) -> Dict[str, Any]:
        delivery = _record(
            row, outcome=outcome, message=message, event=event, delivery_key=delivery_key,
            refs=refs if refs is not None else list((plan_doc or {}).get("refs") or []),
            build_ids=[item["id"] for item in (builds or [])], paths=paths,
            tested_by=tested_by, now=now,
        )
        if outcome in ("triggered", "refused", "failed"):
            log_audit(
                f"ci_webhook_{outcome}",
                actor=tested_by,
                actor_user_id=None if tested_by else (row.updated_by_user_id or row.created_by_user_id),
                target_type="ci_webhook_trigger",
                target_id=str(row.id),
                details={
                    "service": service.slug,
                    "webhook": row.name,
                    "event": event,
                    "test": bool(tested_by),
                    "builds": [item["number"] for item in (builds or [])],
                    **({"reason": message[:500]} if outcome != "triggered" else {}),
                },
            )
        return _result(outcome, message, plan_doc=plan_doc, builds=builds, delivery=delivery)

    if delivery_key:
        seen = row.deliveries.filter(
            CiWebhookDelivery.delivery_key == delivery_key[:128],
            CiWebhookDelivery.outcome.in_(("triggered", "duplicate")),
        ).first()
        if seen is not None:
            return done("duplicate", "This delivery was already handled; nothing new was queued.", refs=seen.refs)

    if not row.enabled and not tested_by:
        return done("ignored", "This webhook is switched off.")

    plan_doc = plan(row, payload, event=event if row.kind == "bitbucket_push" and not tested_by else "")
    if plan_doc["outcome"] != "build":
        return done(plan_doc["outcome"], plan_doc["message"], plan_doc)

    if service.status != "active":
        return done(
            "failed",
            f"Not built: the {'pipeline' if service.is_pipeline_home else 'service'} is {service.status}.",
            plan_doc,
        )
    problem = _pipeline_problem(row)
    if problem:
        return done("failed", problem, plan_doc)
    if tested_by is not None:
        actor = tested_by
    else:
        actor, problem = schedules_service._run_as(row, "webhook")
        if problem:
            return done("failed", problem, plan_doc)

    if row.skip_if_running and row.last_build_id and not tested_by:
        previous = db.session.get(CiBuild, row.last_build_id)
        if previous is not None and previous.status in _ACTIVE_BUILD_STATUSES:
            return done(
                "ignored",
                f"Skipped: build #{previous.number} from this webhook is still {previous.status}.",
                plan_doc,
            )

    wanted = list(plan_doc["builds"])
    fresh = []
    for item in wanted:
        earlier = _already_built(row, item, now)
        if earlier is None:
            fresh.append(item)
        else:
            plan_doc["notes"].append(
                f"{item['ref']} at {item['commit'][:12]} was already built from this webhook; not queued again."
            )
    if not fresh:
        return done("duplicate", "Every ref in this delivery was already built.", plan_doc)

    budget = MAX_BUILDS_PER_MINUTE - _recent_build_count(row, now)
    if budget <= 0:
        return done(
            "ignored",
            f"Not built: this webhook already started {MAX_BUILDS_PER_MINUTE} builds in the last minute "
            "(CI_WEBHOOK_MAX_BUILDS_PER_MINUTE).",
            plan_doc,
        )
    if len(fresh) > budget:
        plan_doc["notes"].append(f"Only {budget} of {len(fresh)} refs were built: the per-minute limit.")
        fresh = fresh[:budget]

    started: List[Dict[str, Any]] = []
    errors: List[Tuple[str, str]] = []
    for item in fresh:
        try:
            build = trigger_build(
                service,
                branch=item["ref"],
                commit_sha=item["commit"] or None,
                pipeline_id=row.pipeline_id or None,
                trigger_type="webhook",
                actor=actor,
                variables=dict(item["variables"]) or None,
                ref_type=item["refType"],
                webhook={
                    "id": row.id,
                    "name": row.name,
                    "kind": row.kind,
                    "event": event,
                    **({"test": True} if tested_by else {}),
                },
            )
        except pipelines_service.PipelineError as exc:
            db.session.rollback()
            errors.append(("refused", f"{item['ref']}: {exc}"))
            continue
        except BuildError as exc:
            db.session.rollback()
            errors.append(("failed", f"{item['ref']}: {exc}"))
            continue
        started.append({"id": build["id"], "number": build["number"], "branch": build.get("branch"), "status": build.get("status")})

    for _kind, message in errors:
        plan_doc["notes"].append(message)
    if not started:
        outcome = "refused" if any(kind == "refused" for kind, _ in errors) else "failed"
        return done(outcome, errors[0][1] if errors else "Nothing was built.", plan_doc)
    numbers = ", ".join(f"#{item['number']}" for item in started)
    message = f"Build {numbers} queued." if len(started) == 1 else f"Builds {numbers} queued."
    return done("triggered", message, plan_doc, builds=started)


def ingest(
    public_id: str,
    payload: Dict[str, Any],
    *,
    event: str = "",
    provided_secret: str = "",
    signatures: Optional[List[str]] = None,
    raw_body: Optional[bytes] = None,
    delivery_key: str = "",
) -> Tuple[CiWebhookTrigger, Dict[str, Any]]:
    """One inbound call, from verification to queued builds.

    Raises :class:`LookupError` for an unknown URL and :class:`PermissionError`
    for a missing or wrong secret; everything after that is an outcome.
    """
    row = CiWebhookTrigger.query.filter_by(public_id=str(public_id or "").strip()).first()
    if row is None:
        raise LookupError("No webhook at this address.")
    if not verify(row, provided=provided_secret, signatures=signatures, raw_body=raw_body):
        row.last_rejected_at = _now()
        db.session.add(row)
        db.session.commit()
        raise PermissionError("Invalid or missing webhook secret.")
    return row, execute(row, payload, event=event, delivery_key=delivery_key)


# ---------------------------------------------------------------------------
# One-click setup in Bitbucket
# ---------------------------------------------------------------------------

PUSH_EVENTS = ["repo:push"]


def _source(service: CiService):
    from . import source as source_port

    handler = source_port.get_provider(service.repository_provider or "bitbucket")
    return source_port, handler, handler.parse_repository_url(service.repository_url)


def configure_in_source(row: CiWebhookTrigger, *, actor=None) -> Dict[str, Any]:
    """Create (or correct) the repository webhook that calls this trigger on push."""
    service = row.service
    if row.kind != "bitbucket_push":
        raise WebhookError("Only a push webhook is registered in Bitbucket. Give a generic one's URL to its sender.")
    if not service.source_ready():
        raise WebhookError("Connect a repository and credential on the Source tab first — the webhook is created with that credential.")
    problem = _base_problem(_base_url())
    if problem:
        raise WebhookError(problem)
    secret = decrypt_secret(row.secret_encrypted or "")
    if not secret:
        secret = rotate_secret(row, actor=actor)
    url = inbound_url(row)
    source_port, handler, ref = _source(service)
    writer = getattr(handler, "ensure_webhook", None)
    if writer is None:
        raise WebhookError(f"KubeSight cannot create webhooks on {service.repository_provider}.")
    try:
        webhook = writer(
            ref,
            service.credential_profile,
            url=url,
            secret=secret,
            events=list(PUSH_EVENTS),
            description=f"KubeSight build: {row.name}"[:255],
        )
    except source_port.SourceError as exc:
        message = str(exc)
        if "401" in message or "403" in message:
            message += " The service's credential needs the webhook (admin) scope on the repository."
        raise WebhookError(message) from exc
    log_audit(
        "ci_webhook_configured_in_source",
        actor=actor,
        target_type="ci_webhook_trigger",
        target_id=str(row.id),
        details={"service": service.slug, "webhook": row.name, "repository": ref.full_name, "action": webhook["action"]},
    )
    return {"webhook": {**webhook, "repository": ref.full_name}, "trigger": trigger_to_dict(row)}


def webhook_status(row: CiWebhookTrigger) -> Dict[str, Any]:
    """Whether Bitbucket already calls this trigger. A live read — so "could
    not look" is an answer, not an error."""
    service = row.service
    if row.kind != "bitbucket_push":
        return {"known": False, "exists": False, "reason": "Only push webhooks are registered in Bitbucket."}
    if not service.source_ready():
        return {"known": False, "exists": False, "reason": "Connect a repository first."}
    problem = _base_problem(_base_url())
    if problem:
        return {"known": False, "exists": False, "reason": problem}
    url = inbound_url(row)
    try:
        _port, handler, ref = _source(service)
        finder = getattr(handler, "find_webhook", None)
        if finder is None:
            return {"known": False, "exists": False, "reason": f"KubeSight cannot read webhooks on {service.repository_provider}."}
        hook = finder(ref, service.credential_profile, url=url)
    except Exception as exc:  # noqa: BLE001 - "we could not look" is an answer
        return {"known": False, "exists": False, "reason": str(exc)}
    if hook is None:
        return {"known": True, "exists": False, "url": url, "repository": ref.full_name}
    missing = sorted(set(PUSH_EVENTS) - set(hook.get("events") or []))
    return {
        "known": True,
        "exists": True,
        "url": url,
        "repository": ref.full_name,
        "active": hook.get("active"),
        "secretSet": hook.get("secretSet"),
        "events": hook.get("events"),
        "missingEvents": missing,
        "inSync": bool(hook.get("active")) and bool(hook.get("secretSet")) and not missing,
    }
