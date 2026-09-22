"""Merge checks: a pull request comes in, a verdict goes back.

The whole feature, in the order it happens:

1. Bitbucket posts a pull request event to ``/api/ci/merge-checks/inbound/<slug>``.
2. :func:`ingest` verifies the shared secret, decides whether this event and
   this destination branch are ones the service watches, and — if they are —
   opens a :class:`CiMergeCheck` row and triggers an ORDINARY build of the
   service's merge check pipeline, pinned to the pull request's head commit.
3. The CI engine runs it like any other build. Each check stage prints one
   ``##kubesight-metric`` line (see :mod:`.metrics`).
4. :func:`settle` — called from the engine's own pass — notices the build has
   finished, reads those lines, judges them against the resolved quality gate,
   and writes the verdict onto the check row.
5. :mod:`.delivery` hands the verdict to Bitbucket as a commit build status,
   retrying until it lands. Bitbucket's branch restriction is what turns that
   status into an actual block on the Merge button.

Two boundaries are worth stating because they are what keep this feature from
growing into a second CI:

*It owns no execution.* Everything between steps 2 and 4 is the existing build
engine, unchanged. This module triggers a build and later reads its logs.

*It decides nothing at delivery time.* The verdict is computed once, in step 4,
against a gate that is COPIED onto the row. Relaxing the policy tomorrow cannot
rewrite what was decided today, and a redelivery cannot change its mind.
"""

from __future__ import annotations

import fnmatch
import logging
import secrets as secrets_module
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

from ....audit import log_audit
from ....db import db
from ....models_ci import CiBuild, CiPipeline, CiSecret, CiService
from ....models_merge_checks import (
    DEFAULT_EVENTS,
    MERGE_CHECK_EVENTS,
    MERGE_CHECK_TOOLS,
    CiMergeCheck,
    CiMergeCheckConfig,
)
from ....secret_encryption import decrypt_secret, encrypt_secret
from .. import engine as engine_service
from .. import pipelines as pipelines_service
from . import delivery, metrics, stages
from .policy import (
    GATE_FIELDS,
    PolicyError,
    apply_gate_fields,
    evaluate,
    gate_payload,
    get_policy,
    resolve_gate,
)

logger = logging.getLogger(__name__)

# The name of the pipeline merge checks run as. Fixed, because the config points
# at it by id and a person renaming it in the editor must not orphan the gate —
# the id is the link, this is only what it is called when it is created.
PIPELINE_NAME = "Merge checks"

# How many checks one settle pass will judge and deliver. A ceiling rather than
# "all of them": this runs inside the CI engine's pass, and a backlog of two
# hundred must not hold up the build that somebody is watching.
SETTLE_BATCH = 20


class MergeCheckError(ValueError):
    """Something a user asked for that cannot be done, in their words."""


def _now() -> datetime:
    return datetime.now(timezone.utc)


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

def get_config(service: CiService) -> Optional[CiMergeCheckConfig]:
    return CiMergeCheckConfig.query.filter_by(service_id=service.id).first()


def _new_secret() -> str:
    return secrets_module.token_urlsafe(32)


def ensure_config(service: CiService, *, actor=None) -> CiMergeCheckConfig:
    """The service's configuration, created disabled on first touch.

    Created rather than returned as None so the webhook URL and its secret
    exist to be copied BEFORE the gate is switched on. Configuring Bitbucket
    first and turning the gate on second is the order people actually work in,
    and a UI that demands the reverse gets a half-configured webhook.
    """
    row = get_config(service)
    if row is not None:
        return row
    policy = get_policy()
    row = CiMergeCheckConfig(
        service_id=service.id,
        enabled=False,
        tools=list(MERGE_CHECK_TOOLS),
        events=list(DEFAULT_EVENTS),
        target_branches=[],
        gate_mode="inherit",
        inbound_secret_encrypted=encrypt_secret(_new_secret()),
        status_key="KUBESIGHT-MERGE",
        post_comment=True,
        created_by_user_id=getattr(actor, "id", None),
    )
    if policy.enabled_by_default:
        row.enabled = True
    db.session.add(row)
    db.session.commit()
    return row


def _clean_tools(value: Any, current: List[str]) -> List[str]:
    if value is None:
        return list(current)
    if not isinstance(value, (list, tuple)):
        raise MergeCheckError("Checks must be a list.")
    chosen = [str(item).strip() for item in value]
    unknown = [item for item in chosen if item not in MERGE_CHECK_TOOLS]
    if unknown:
        raise MergeCheckError(f"'{unknown[0]}' is not a merge check KubeSight runs.")
    # Canonical order, not the order they arrived in: the pipeline's stage order
    # is cheapest-first for a reason, and a UI must not be able to reverse it.
    return [tool for tool in MERGE_CHECK_TOOLS if tool in set(chosen)]


def _clean_events(value: Any, current: List[str]) -> List[str]:
    if value is None:
        return list(current)
    if not isinstance(value, (list, tuple)):
        raise MergeCheckError("Events must be a list.")
    chosen = [str(item).strip() for item in value if str(item).strip()]
    unknown = [item for item in chosen if item not in MERGE_CHECK_EVENTS]
    if unknown:
        raise MergeCheckError(f"'{unknown[0]}' is not a pull request event.")
    if not chosen:
        raise MergeCheckError("Choose at least one event to run checks on.")
    return chosen


def _clean_branches(value: Any, current: List[str]) -> List[str]:
    if value is None:
        return list(current)
    if not isinstance(value, (list, tuple)):
        raise MergeCheckError("Target branches must be a list.")
    cleaned = [str(item).strip()[:255] for item in value if str(item).strip()]
    if len(cleaned) > 50:
        raise MergeCheckError("That is more target branch patterns than is useful.")
    return cleaned


MAX_COMMAND_LINES = 100
MAX_COMMAND_CHARS = 4000


def _clean_custom_commands(value: Any, current: Dict[str, Any]) -> Dict[str, Any]:
    """Per-tool script overrides, as {tool: [line, ...]}.

    A tool mapped to an empty value is REMOVED rather than stored as an empty
    script — that is what "reset to the default" sends, and a stage with no
    commands would fail validation rather than fall back.

    The limits mirror ``pipelines._command_lines`` because these end up in the
    same place: a pipeline stage, validated by the same code.
    """
    if value is None:
        return dict(current or {})
    if not isinstance(value, dict):
        raise MergeCheckError("Custom commands must be given per check.")
    cleaned = dict(current or {})
    for tool, raw in value.items():
        if tool not in MERGE_CHECK_TOOLS:
            raise MergeCheckError(f"'{tool}' is not a merge check KubeSight runs.")
        if isinstance(raw, str):
            lines = raw.splitlines()
        elif isinstance(raw, (list, tuple)):
            lines = [str(item) for item in raw]
        elif raw is None:
            lines = []
        else:
            raise MergeCheckError(f"The {tool} script must be text.")
        lines = [line.rstrip()[:MAX_COMMAND_CHARS] for line in lines[:MAX_COMMAND_LINES]]
        while lines and not lines[0].strip():
            lines.pop(0)
        while lines and not lines[-1].strip():
            lines.pop()
        if lines:
            cleaned[tool] = lines
        else:
            cleaned.pop(tool, None)
    return cleaned


def save_config(
    service: CiService, payload: Dict[str, Any], *, actor=None
) -> Dict[str, Any]:
    """Apply a configuration change, and rebuild the pipeline when it needs it.

    The pipeline is regenerated when the SELECTED TOOLS change or when the gate
    the stages were generated against changes — the severity floors are baked
    into the commands, so a gate edit that left the stages alone would count a
    different set of findings than the gate says it does.
    """
    row = ensure_config(service, actor=actor)
    before_tools = list(row.tools or [])
    before_gate = resolve_gate(row)
    before_commands = dict(row.custom_commands or {})

    if "enabled" in payload:
        row.enabled = bool(payload.get("enabled"))
    row.tools = _clean_tools(payload.get("tools"), before_tools)
    row.custom_commands = _clean_custom_commands(
        payload.get("customCommands"), before_commands
    )
    row.events = _clean_events(payload.get("events"), list(row.events or DEFAULT_EVENTS))
    row.target_branches = _clean_branches(
        payload.get("targetBranches"), list(row.target_branches or [])
    )
    if "postComment" in payload:
        row.post_comment = bool(payload.get("postComment"))
    if "statusKey" in payload:
        key = str(payload.get("statusKey") or "").strip().upper()[:40]
        if key and not key.replace("-", "").replace("_", "").isalnum():
            raise MergeCheckError(
                "The build status key may contain letters, numbers, - and _ only."
            )
        row.status_key = key or "KUBESIGHT-MERGE"
    if "gateMode" in payload:
        mode = str(payload.get("gateMode") or "inherit").strip().lower()
        if mode not in ("inherit", "override"):
            raise MergeCheckError("The gate must either inherit the policy or override it.")
        row.gate_mode = mode
    try:
        apply_gate_fields(row, payload)
    except PolicyError as exc:
        raise MergeCheckError(str(exc)) from exc

    if row.enabled and not row.tools:
        raise MergeCheckError(
            "Merge checks need at least one check to run. Choose ESLint, "
            "SonarQube or Dependency-Check before switching them on."
        )
    if row.enabled and not service.source_ready():
        raise MergeCheckError(
            "Connect a repository and credential on the Source tab before "
            "switching merge checks on — the verdict is reported back to it."
        )

    db.session.add(row)
    db.session.flush()

    after_gate = resolve_gate(row)
    stages_stale = (
        list(row.tools or []) != before_tools
        or _stage_relevant(after_gate) != _stage_relevant(before_gate)
        # An edited script has to reach the pipeline, and a reset has to take
        # the generated one back — both are the same "the stages no longer
        # match the configuration" condition.
        or dict(row.custom_commands or {}) != before_commands
        or row.pipeline_id is None
    )
    if row.tools and (stages_stale or payload.get("regeneratePipeline")):
        _sync_pipeline(service, row, after_gate, actor=actor)
    db.session.commit()

    log_audit(
        "ci_merge_checks_saved",
        actor=actor,
        target_type="ci_merge_check_config",
        target_id=str(row.id),
        details={
            "service": service.slug,
            "enabled": row.enabled,
            "tools": list(row.tools or []),
            "gateMode": row.gate_mode,
            "maxTotalProblems": after_gate.get("maxTotalProblems"),
        },
    )
    return config_payload(service, row)


def _stage_relevant(gate: Dict[str, Any]) -> Tuple:
    """The parts of a gate that are compiled INTO the stage commands.

    Only these three force a pipeline regeneration. The caps are read at
    evaluation time and changing one must not rewrite a pipeline somebody has
    edited by hand.
    """
    return (
        bool(gate.get("eslintCountWarnings")),
        gate.get("semgrepMinSeverity"),
        gate.get("sonarMinSeverity"),
        gate.get("dependencyMinSeverity"),
    )


def _sync_pipeline(
    service: CiService,
    config: CiMergeCheckConfig,
    gate: Dict[str, Any],
    *,
    actor=None,
) -> CiPipeline:
    """Create or rewrite the merge check pipeline from the selected tools.

    A full rewrite, not a merge: the stages are generated from the tools and the
    gate, and reconciling those against hand edits would produce a pipeline that
    is neither what was generated nor what was edited. The panel says so before
    it does it.
    """
    known_keys = {
        secret.key
        for secret in CiSecret.query.filter(
            (CiSecret.service_id == service.id) | (CiSecret.scope == "global")
        ).all()
    }
    payload = {
        "name": PIPELINE_NAME,
        "description": (
            "Runs on a pull request and reports a verdict back to the source "
            "host. Generated from the Merge Checks tab; regenerating replaces "
            "these stages."
        ),
        # Never the default. A merge check pipeline running on Run Build would
        # surprise somebody, and `default_pipeline()` is what Run Build uses.
        "isDefault": False,
        # Not a build. `purpose` is what keeps it out of the Pipeline tab, off
        # Run Build's list, and out of `default_pipeline()` — without it, a
        # service whose default is still the generated one would suddenly build
        # ESLint when somebody pressed Run Build.
        "purpose": "merge_check",
        "enabled": True,
        "parameters": [],
        "stages": stages.build_stages(
            list(config.tools or []),
            gate,
            service_slug=service.slug,
            known_secret_keys=known_keys,
            custom_commands=dict(config.custom_commands or {}),
        ),
    }

    existing = (
        db.session.get(CiPipeline, config.pipeline_id) if config.pipeline_id else None
    )
    if existing is None:
        existing = CiPipeline.query.filter_by(
            service_id=service.id, name=PIPELINE_NAME
        ).first()

    try:
        if existing is None:
            created = pipelines_service.create_pipeline(service, payload, actor=actor)
            config.pipeline_id = created["id"]
        else:
            pipelines_service.update_pipeline(existing, payload, actor=actor)
            config.pipeline_id = existing.id
    except pipelines_service.PipelineError as exc:
        raise MergeCheckError(str(exc)) from exc
    db.session.add(config)
    return db.session.get(CiPipeline, config.pipeline_id)


def rotate_secret(service: CiService, *, actor=None) -> str:
    """Mint a new inbound secret and return it once, in plaintext.

    Returned rather than only stored because it has to be pasted into
    Bitbucket's webhook form. It stays readable afterwards through the reveal
    on the panel — this is a shared secret between two systems, not a password.
    """
    row = ensure_config(service, actor=actor)
    value = _new_secret()
    row.inbound_secret_encrypted = encrypt_secret(value)
    db.session.add(row)
    db.session.commit()
    log_audit(
        "ci_merge_checks_secret_rotated",
        actor=actor,
        target_type="ci_merge_check_config",
        target_id=str(row.id),
        details={"service": service.slug},
    )
    return value


def inbound_secret(service: CiService) -> str:
    row = get_config(service)
    return decrypt_secret(row.inbound_secret_encrypted or "") if row else ""


# ---------------------------------------------------------------------------
# Serialization
# ---------------------------------------------------------------------------

def webhook_path(service: CiService) -> str:
    return f"/api/ci/merge-checks/inbound/{service.slug}"


def config_payload(
    service: CiService, row: Optional[CiMergeCheckConfig] = None
) -> Dict[str, Any]:
    row = row if row is not None else get_config(service)
    gate = resolve_gate(row)
    base = delivery.public_base_url()
    pipeline = db.session.get(CiPipeline, row.pipeline_id) if row and row.pipeline_id else None
    payload: Dict[str, Any] = {
        "serviceId": service.id,
        "serviceSlug": service.slug,
        "configured": row is not None,
        "enabled": bool(row.enabled) if row else False,
        "tools": list(row.tools or []) if row else list(MERGE_CHECK_TOOLS),
        "events": list(row.events or []) if row else list(DEFAULT_EVENTS),
        "targetBranches": list(row.target_branches or []) if row else [],
        "gateMode": row.gate_mode if row else "inherit",
        "statusKey": row.status_key if row else "KUBESIGHT-MERGE",
        "postComment": bool(row.post_comment) if row else True,
        "override": gate_payload(row) if row else {key: None for _c, key, _k in GATE_FIELDS},
        "effectiveGate": gate,
        "webhookPath": webhook_path(service),
        "webhookUrl": f"{base}{webhook_path(service)}" if base else "",
        "secretConfigured": bool(row and row.inbound_secret_encrypted),
        "pipelineId": row.pipeline_id if row else None,
        # Per tool: what it runs now, whether that is an edit, and what the
        # generated default is. All three, because "Reset to the default" has to
        # be able to show what it would restore without a second round trip.
        "checkScripts": _check_scripts(service, row, gate),
        "pipelineStages": (
            [
                {
                    "id": stage.id,
                    "name": stage.name,
                    "image": stage.image or "",
                    "commands": list(stage.commands or []),
                    "enabled": bool(stage.enabled),
                    "tool": (stage.env or {}).get(metrics.STAGE_TOOL_ENV, ""),
                }
                for stage in sorted(pipeline.stages, key=lambda s: s.position)
            ]
            if pipeline
            else []
        ),
        "unconfiguredEnvironments": stages.unconfigured_environments(
            list(row.tools or []) if row else []
        ),
        "sourceReady": service.source_ready(),
        "canReportVerdict": _can_report_verdict(service),
        "lastEventAt": _iso(row.last_event_at) if row else None,
        "lastError": row.last_error if row else None,
    }
    return payload


def _check_scripts(
    service: CiService, row: Optional[CiMergeCheckConfig], gate: Dict[str, Any]
) -> List[Dict[str, Any]]:
    overrides = dict(row.custom_commands or {}) if row else {}
    selected = list(row.tools or []) if row else list(MERGE_CHECK_TOOLS)
    out: List[Dict[str, Any]] = []
    for tool in MERGE_CHECK_TOOLS:
        try:
            default = stages.generated_commands(tool, gate, service_slug=service.slug)
        except ValueError:
            continue
        custom = overrides.get(tool)
        # The image comes from the same call that builds the stage, so the
        # editor's "runs in ..." line cannot disagree with what actually runs —
        # reading it back off the saved pipeline made it depend on a second
        # lookup that is empty before the pipeline exists.
        try:
            wiring = stages.check_stage(tool, gate, service_slug=service.slug)
        except ValueError:
            wiring = {}
        out.append(
            {
                "tool": tool,
                "label": stages.tool_label(tool),
                "stageName": stages.stage_name_for(tool),
                "image": wiring.get("image") or "",
                "timeoutSeconds": wiring.get("timeoutSeconds"),
                "enabled": tool in selected,
                "customized": bool(custom),
                "commands": list(custom or default),
                "defaultCommands": default,
            }
        )
    return out


def _can_report_verdict(service: CiService) -> Dict[str, Any]:
    """Whether the configured credential can actually write a build status.

    Checked and SHOWN rather than discovered at delivery time: a read-only
    token runs every check perfectly and then cannot tell anybody, which looks
    from the outside like the feature silently not working.
    """
    credential = service.credential_profile
    if credential is None:
        return {"ok": False, "reason": "No source credential is configured."}
    if credential.read_only:
        return {
            "ok": False,
            "reason": (
                f"Credential '{credential.name}' is read-only. Reporting a verdict "
                "writes a build status, so merge checks need a credential with "
                "write access to this repository."
            ),
        }
    return {"ok": True, "reason": ""}


def merge_enforcement(service: CiService) -> Dict[str, Any]:
    """Whether Bitbucket will actually refuse the merge, asked of Bitbucket.

    Its own endpoint rather than part of the configuration payload: it is a
    live call to a third party, and a settings page that will not render while
    a source host is slow is a settings page nobody can fix the source host
    from.
    """
    config = get_config(service)
    branches = list(config.target_branches or []) if config else []
    if not service.source_ready():
        return {
            "known": False,
            "enforced": False,
            "reason": "Connect a repository first.",
        }
    from .. import source as source_port

    try:
        handler = source_port.get_provider(service.repository_provider)
        prober = getattr(handler, "check_merge_enforcement", None)
        if prober is None:
            return {
                "known": False,
                "enforced": False,
                "reason": (
                    f"KubeSight cannot inspect branch protection on "
                    f"{service.repository_provider}."
                ),
            }
        ref = handler.parse_repository_url(service.repository_url)
        result = prober(ref, service.credential_profile, branches=branches)
    except Exception as exc:  # noqa: BLE001 - "we could not look" is an answer
        return {"known": False, "enforced": False, "reason": str(exc)}

    return {"known": True, "repository": ref.full_name, **result}


def _iso(value: Optional[datetime]) -> Optional[str]:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.isoformat()


def check_payload(row: CiMergeCheck) -> Dict[str, Any]:
    build = row.build
    return {
        "id": row.id,
        "serviceId": row.service_id,
        "provider": row.provider,
        "event": row.event,
        "pullRequestId": row.pull_request_id,
        "pullRequestUrl": row.pull_request_url,
        "title": row.title,
        "author": row.author,
        "sourceBranch": row.source_branch,
        "destinationBranch": row.destination_branch,
        "commitSha": row.commit_sha,
        "shortSha": (row.commit_sha or "")[:12],
        "state": row.state,
        "verdict": row.verdict,
        "metrics": dict(row.metrics or {}),
        "gate": dict(row.gate or {}),
        "reasons": list(row.reasons or []),
        "totalProblems": row.total_problems,
        "buildId": row.build_id,
        "buildNumber": build.number if build else None,
        "buildStatus": build.status if build else None,
        "deliveryState": row.delivery_state,
        "deliveryAttempts": row.delivery_attempts,
        "deliveryError": row.delivery_error,
        "deliveredAt": _iso(row.delivered_at),
        "error": row.error,
        "createdAt": _iso(row.created_at),
        "evaluatedAt": _iso(row.evaluated_at),
    }


def list_checks(service: CiService, *, limit: int = 25) -> List[Dict[str, Any]]:
    rows = (
        CiMergeCheck.query.filter_by(service_id=service.id)
        .order_by(CiMergeCheck.id.desc())
        .limit(max(1, min(int(limit), 200)))
        .all()
    )
    return [check_payload(row) for row in rows]


def get_check(check_id: int) -> CiMergeCheck:
    row = db.session.get(CiMergeCheck, int(check_id))
    if row is None:
        raise LookupError("Merge check not found.")
    return row


# ---------------------------------------------------------------------------
# Inbound
# ---------------------------------------------------------------------------

def verify_secret(config: Optional[CiMergeCheckConfig], provided: Optional[str]) -> bool:
    """Constant-time compare against the stored secret.

    A service with no secret stored rejects everything rather than accepting
    everything. The opposite convention (open when unconfigured) is what the
    ticketing webhook does, and it is defensible there because that webhook
    only files tickets. This one triggers builds and writes to a source host,
    so the unconfigured state is closed.
    """
    import hmac

    if config is None:
        return False
    stored = decrypt_secret(config.inbound_secret_encrypted or "")
    if not stored:
        return False
    return bool(provided) and hmac.compare_digest(str(provided), stored)


def _text(value: Any, limit: int = 255) -> str:
    return str(value or "").strip()[:limit]


def parse_pull_request(payload: Dict[str, Any]) -> Dict[str, Any]:
    """Bitbucket's pull request payload, reduced to what a check needs.

    Tolerant by design: a webhook body is somebody else's schema, and a missing
    optional field must not turn into a 500 that Bitbucket then retries. The
    only genuinely required fields are the head commit (there is nothing to
    check without it) and the destination branch (the gate is scoped by it).
    """
    pull = payload.get("pullrequest")
    if not isinstance(pull, dict):
        raise MergeCheckError("This payload carries no pull request.")
    source = pull.get("source") if isinstance(pull.get("source"), dict) else {}
    destination = pull.get("destination") if isinstance(pull.get("destination"), dict) else {}
    commit = source.get("commit") if isinstance(source.get("commit"), dict) else {}
    author = pull.get("author") if isinstance(pull.get("author"), dict) else {}
    links = pull.get("links") if isinstance(pull.get("links"), dict) else {}
    html = links.get("html") if isinstance(links.get("html"), dict) else {}

    def _branch(container: Dict[str, Any]) -> str:
        branch = container.get("branch") if isinstance(container.get("branch"), dict) else {}
        return _text(branch.get("name"))

    return {
        "pullRequestId": _text(pull.get("id"), 64),
        "title": _text(pull.get("title"), 500),
        "author": _text(author.get("display_name") or author.get("nickname")),
        "sourceBranch": _branch(source),
        "destinationBranch": _branch(destination),
        "commitSha": _text(commit.get("hash"), 64),
        "url": _text(html.get("href"), 1024),
    }


def branch_matches(patterns: List[str], branch: str) -> bool:
    """Whether a destination branch is one this gate applies to.

    No patterns means every branch. fnmatch rather than a regex because the
    thing people write here is ``release/*``, and a regex field invites a typo
    that silently matches nothing.
    """
    if not patterns:
        return True
    return any(fnmatch.fnmatch(branch or "", pattern) for pattern in patterns)


def ingest(
    slug: str, payload: Dict[str, Any], *, event: str = "", provided_secret: str = ""
) -> Dict[str, Any]:
    """One webhook delivery, from verification to a queued build.

    Returns a small document the caller answers with. It always describes what
    happened, including "nothing" — a webhook that is being ignored because of
    its target branch has to say so, or the first question after switching this
    on is unanswerable.

    Raises :class:`PermissionError` for a bad secret and :class:`LookupError`
    for an unknown service; everything else is an outcome, not an error.
    """
    service = CiService.query.filter_by(slug=(slug or "").strip().lower()).first()
    if service is None:
        raise LookupError("No service with that slug.")
    config = get_config(service)
    if not verify_secret(config, provided_secret):
        raise PermissionError("Invalid or missing webhook secret.")

    config.last_event_at = _now()
    db.session.add(config)

    if not config.enabled:
        return _ignored(config, "Merge checks are switched off for this service.")

    event = (event or "").strip() or "pullrequest:created"
    if event not in (config.events or []):
        return _ignored(
            config, f"'{event}' is not an event this service runs checks on."
        )

    try:
        details = parse_pull_request(payload)
    except MergeCheckError as exc:
        return _ignored(config, str(exc))

    if not details["commitSha"]:
        return _ignored(
            config, "The payload carries no source commit, so there is nothing to check."
        )
    if not branch_matches(list(config.target_branches or []), details["destinationBranch"]):
        return _ignored(
            config,
            f"Checks do not apply to pull requests into '{details['destinationBranch']}'.",
        )

    # A webhook host retries, and `updated` fires for edits that changed no code
    # at all. Re-checking the same commit would queue a second identical build
    # and file a second identical status.
    existing = (
        CiMergeCheck.query.filter_by(
            service_id=service.id,
            commit_sha=details["commitSha"],
            pull_request_id=details["pullRequestId"],
        )
        .order_by(CiMergeCheck.id.desc())
        .first()
    )
    if existing is not None and existing.state in ("queued", "running", "passed", "failed"):
        db.session.commit()
        return {
            "accepted": True,
            "duplicate": True,
            "checkId": existing.id,
            "state": existing.state,
            "message": "This commit has already been checked.",
        }

    check = CiMergeCheck(
        service_id=service.id,
        config_id=config.id,
        provider=service.repository_provider or "bitbucket",
        event=event,
        pull_request_id=details["pullRequestId"] or None,
        pull_request_url=details["url"] or None,
        title=details["title"] or None,
        author=details["author"] or None,
        source_branch=details["sourceBranch"] or None,
        destination_branch=details["destinationBranch"] or None,
        commit_sha=details["commitSha"],
        state="queued",
        gate=resolve_gate(config),
        delivery_state="pending",
    )
    db.session.add(check)
    db.session.flush()

    try:
        build = engine_service.trigger_build(
            service,
            branch=details["sourceBranch"] or service.default_branch,
            commit_sha=details["commitSha"],
            pipeline_id=config.pipeline_id,
            trigger_type="webhook",
            variables={
                "KUBESIGHT_MERGE_CHECK": "true",
                "KUBESIGHT_PR_ID": details["pullRequestId"],
                "KUBESIGHT_PR_SOURCE_BRANCH": details["sourceBranch"],
                "KUBESIGHT_PR_TARGET_BRANCH": details["destinationBranch"],
            },
        )
    except Exception as exc:  # noqa: BLE001 - every failure here is a verdict
        # A check that could not even start is an ERROR verdict, not a silent
        # drop: the pull request still gets a red status explaining why, which
        # is the only way anybody finds out the gate is misconfigured.
        check.state = "error"
        check.verdict = "unknown"
        check.error = str(exc)[:2000]
        check.reasons = [f"The merge checks could not start: {exc}"]
        check.total_problems = 0
        check.evaluated_at = _now()
        config.last_error = str(exc)[:2000]
        db.session.add_all([check, config])
        db.session.commit()
        logger.warning("Merge check %s could not start: %s", check.id, exc)
        return {
            "accepted": True,
            "checkId": check.id,
            "state": "error",
            "message": str(exc),
        }

    check.build_id = build["id"]
    check.state = "running"
    config.last_check_id = check.id
    config.last_error = None
    db.session.add_all([check, config])
    db.session.commit()

    # Optimistic in-progress status, so the pull request shows the check as
    # running rather than as absent for the next few minutes.
    delivery.mark_running(check)

    log_audit(
        "ci_merge_check_started",
        actor=None,
        target_type="ci_merge_check",
        target_id=str(check.id),
        details={
            "service": service.slug,
            "pullRequest": check.pull_request_id,
            "commit": (check.commit_sha or "")[:12],
            "buildId": check.build_id,
        },
    )
    return {
        "accepted": True,
        "checkId": check.id,
        "buildId": check.build_id,
        "state": "running",
        "message": "Merge checks started.",
    }


def _ignored(config: CiMergeCheckConfig, reason: str) -> Dict[str, Any]:
    config.last_error = reason[:2000]
    db.session.add(config)
    db.session.commit()
    return {"accepted": True, "checked": False, "state": "skipped", "message": reason}


# ---------------------------------------------------------------------------
# Settling — called from the CI engine's pass
# ---------------------------------------------------------------------------

def pending_work() -> bool:
    """Whether a pass has anything to do. One cheap query, called every tick."""
    unsettled = (
        CiMergeCheck.query.filter(CiMergeCheck.state.in_(("queued", "running"))).count()
    )
    return bool(unsettled) or bool(delivery.pending_count())


def settle() -> int:
    """Judge finished checks and deliver verdicts. Returns how many it touched.

    Safe to call on every engine pass: it is two indexed queries when there is
    nothing to do, and it commits after each check so a failure part-way
    through leaves the rest for the next pass rather than rolling back work
    that succeeded.
    """
    touched = 0
    rows = (
        CiMergeCheck.query.filter(CiMergeCheck.state.in_(("queued", "running")))
        .order_by(CiMergeCheck.id.asc())
        .limit(SETTLE_BATCH)
        .all()
    )
    for check in rows:
        try:
            if _evaluate_if_finished(check):
                touched += 1
        except Exception:  # noqa: BLE001 - one bad check must not stop the rest
            logger.exception("Merge check %s could not be evaluated", check.id)
            db.session.rollback()

    for check in delivery.due_checks(SETTLE_BATCH):
        try:
            delivery.deliver(check)
            touched += 1
        except Exception:  # noqa: BLE001 - delivery records its own failures
            logger.exception("Merge check %s could not be delivered", check.id)
            db.session.rollback()
    return touched


def _evaluate_if_finished(check: CiMergeCheck) -> bool:
    """Write the verdict once the check's build has reached a terminal state."""
    build = db.session.get(CiBuild, check.build_id) if check.build_id else None
    if build is None:
        finalize_error(check, "The build for this check no longer exists.")
        return True
    if build.status in ("queued", "running"):
        return False

    tools = list((check.config.tools if check.config else None) or MERGE_CHECK_TOOLS)
    reported = metrics.collect(build, tools)
    gate = dict(check.gate or {}) or resolve_gate(check.config)
    outcome = evaluate(gate, reported)

    if build.status in ("cancelled", "timeout") and outcome["verdict"] == "allowed":
        # A build that was cancelled has not checked anything, whatever its
        # stages managed to print before they were stopped.
        outcome = {
            **outcome,
            "verdict": "blocked",
            "reasons": outcome["reasons"]
            + [f"The check build was {build.status}, so nothing was verified."],
        }

    check.metrics = reported
    check.gate = gate
    check.reasons = outcome["reasons"]
    check.total_problems = outcome["totalProblems"]
    check.verdict = outcome["verdict"]
    check.state = "passed" if outcome["verdict"] == "allowed" else "failed"
    check.evaluated_at = _now()
    check.delivery_state = "pending"
    check.next_delivery_at = None
    db.session.add(check)
    db.session.commit()

    log_audit(
        "ci_merge_check_verdict",
        actor=None,
        target_type="ci_merge_check",
        target_id=str(check.id),
        details={
            "verdict": check.verdict,
            "totalProblems": check.total_problems,
            "pullRequest": check.pull_request_id,
            "commit": (check.commit_sha or "")[:12],
        },
    )
    return True


def finalize_error(check: CiMergeCheck, message: str) -> None:
    check.state = "error"
    check.verdict = "unknown"
    check.error = message[:2000]
    check.reasons = [message]
    check.total_problems = 0
    check.evaluated_at = _now()
    check.delivery_state = "pending"
    db.session.add(check)
    db.session.commit()


__all__ = [
    "MergeCheckError",
    "PIPELINE_NAME",
    "branch_matches",
    "check_payload",
    "config_payload",
    "ensure_config",
    "get_check",
    "get_config",
    "ingest",
    "inbound_secret",
    "list_checks",
    "merge_enforcement",
    "parse_pull_request",
    "pending_work",
    "rotate_secret",
    "save_config",
    "settle",
    "verify_secret",
    "webhook_path",
]
