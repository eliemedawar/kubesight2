"""Analysis rows: requesting one, reading one, stopping one.

The row is created by the request and the work happens afterwards, so an
analysis is a thing the user can watch, retry and abandon rather than a request
that either returns or does not. Everything the UI polls is here.

One in flight per service, deliberately. Two concurrent analyses of the same
repository produce two proposals for the same pipeline, and whichever finished
second would quietly win.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from ...audit import log_audit
from ...db import db
from ...models_ci import CiRepositoryAnalysis, CiService
from . import hermes, jobs, profile as profile_module

ACTIVE_STATES = ("queued", "analyzing")
MAX_HISTORY = 20


class AnalysisError(ValueError):
    """An analysis request was refused. Message is user-facing."""


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value: Optional[datetime]) -> Optional[str]:
    return value.isoformat() if value else None


def availability() -> Dict[str, Any]:
    """Whether the assisted path can be offered, and why not if it cannot.

    Asked before the choice is shown, so an installation without Hermes offers
    manual configuration and a reason rather than a button that fails.
    """
    from . import schema

    configured = hermes.is_configured()
    return {
        "available": configured,
        "reason": "" if configured else hermes.configuration_hint(),
        "schemaVersion": schema.SCHEMA_VERSION,
    }


def analysis_to_dict(row: Optional[CiRepositoryAnalysis]) -> Optional[Dict[str, Any]]:
    if row is None:
        return None
    validation = row.validation or {}
    profile = row.application_profile or None
    return {
        "id": row.id,
        "serviceId": row.service_id,
        "state": row.state,
        "pipelineState": row.pipeline_state,
        "progressPercent": row.progress_percent,
        "currentStage": row.current_stage,
        "mode": row.mode,
        "revision": row.revision,
        "commitSha": row.commit_sha,
        "schemaVersion": row.schema_version,
        "hermesModel": row.hermes_model,
        "hermesPromptVersion": row.hermes_prompt_version,
        "applicationProfile": profile,
        "profileSummary": profile_module.summary(profile),
        "derivedApplicationType": (profile or {}).get("derivedApplicationType"),
        "generatedPipeline": row.generated_pipeline,
        "requiredInputs": list(row.required_inputs or []),
        "validation": {
            "valid": bool(validation.get("valid")),
            "errors": list(validation.get("errors") or []),
            "warnings": list(validation.get("warnings") or []),
        },
        # Codes and counts only — enough to explain "Hermes tried twice and kept
        # proposing an unavailable runner", never any repository content.
        "attempts": list(row.attempts or []),
        "warnings": list(row.warnings or []),
        "evidenceCoverage": row.evidence_coverage,
        "failureStage": row.failure_stage,
        "error": row.safe_error_message,
        "cancelRequested": bool(row.cancel_requested),
        "createdAt": _iso(row.created_at),
        "startedAt": _iso(row.started_at),
        "completedAt": _iso(row.completed_at),
        "requestedBy": (
            {"id": row.requested_by.id, "name": getattr(row.requested_by, "full_name", None)
             or getattr(row.requested_by, "username", None)}
            if row.requested_by
            else None
        ),
        "executedByAccount": row.executed_by_account,
    }


def _reap_if_stale(row: Optional[CiRepositoryAnalysis]) -> Optional[CiRepositoryAnalysis]:
    """Close an analysis whose worker is gone, on the way to showing it.

    Reaping happens here rather than on the CI engine's tick on purpose: the
    engine lives in ``services/ci``, which must not import this package, and
    the moment anybody actually cares whether an analysis is alive is the
    moment they ask for it. A restart leaves at most one stale row per service,
    and the next poll closes it.
    """
    if row is None or row.state not in ACTIVE_STATES:
        return row
    if jobs.reap_stale():
        db.session.refresh(row)
    return row


def latest_for_service(service_id: int) -> Optional[CiRepositoryAnalysis]:
    return _reap_if_stale(
        CiRepositoryAnalysis.query.filter_by(service_id=int(service_id))
        .order_by(CiRepositoryAnalysis.id.desc())
        .first()
    )


def list_for_service(service_id: int, limit: int = MAX_HISTORY) -> List[CiRepositoryAnalysis]:
    return (
        CiRepositoryAnalysis.query.filter_by(service_id=int(service_id))
        .order_by(CiRepositoryAnalysis.id.desc())
        .limit(max(1, min(int(limit or MAX_HISTORY), MAX_HISTORY)))
        .all()
    )


def get_analysis(analysis_id: int) -> CiRepositoryAnalysis:
    row = db.session.get(CiRepositoryAnalysis, int(analysis_id))
    if row is None:
        raise LookupError("Analysis not found.")
    return _reap_if_stale(row)


def _active(service_id: int) -> Optional[CiRepositoryAnalysis]:
    return (
        CiRepositoryAnalysis.query.filter(
            CiRepositoryAnalysis.service_id == int(service_id),
            CiRepositoryAnalysis.state.in_(ACTIVE_STATES),
        )
        .order_by(CiRepositoryAnalysis.id.desc())
        .first()
    )


def request_analysis(
    service: CiService, payload: Dict[str, Any], *, actor=None
) -> Dict[str, Any]:
    """Start an analysis, or explain why one cannot start.

    Refusals here are all recoverable and all say what to do instead — this
    feature must never be the reason somebody cannot register a service.
    """
    if not hermes.is_configured():
        raise AnalysisError(
            hermes.configuration_hint()
            or "Hermes is not available on this installation. Configure the service manually."
        )

    existing = _active(service.id)
    if existing is not None:
        raise AnalysisError(
            "An analysis of this service is already running. Wait for it to finish, "
            "or cancel it first."
        )

    mode = str(payload.get("mode") or "repository").strip().lower()
    if mode not in ("repository", "profile"):
        raise AnalysisError("Analysis mode must be 'repository' or 'profile'.")

    hint: Optional[Dict[str, Any]] = None
    raw_profile = payload.get("applicationProfile")
    if raw_profile:
        try:
            hint = profile_module.normalize(raw_profile, source="manual")
        except profile_module.ProfileError as exc:
            raise AnalysisError(str(exc)) from exc
    elif mode == "profile":
        raise AnalysisError(
            "Generating from a profile needs the application profile to generate from."
        )

    if mode == "repository" and not service.source_ready():
        raise AnalysisError(
            "Connect a repository and credential before analyzing this service."
        )

    revision = " ".join(str(payload.get("revision") or "").split())[:255]
    row = CiRepositoryAnalysis(
        service_id=service.id,
        state="queued",
        pipeline_state="not_generated",
        mode=mode,
        revision=revision or (service.default_branch or "main"),
        requested_by_user_id=getattr(actor, "id", None),
        application_profile=hint,
        current_stage="Queued",
        last_heartbeat_at=_now(),
    )
    db.session.add(row)

    service.analysis_state = "analyzing"
    db.session.add(service)
    db.session.commit()

    log_audit(
        "ci_analysis_requested",
        actor=actor,
        target_type="ci_service",
        target_id=str(service.id),
        details={
            "service": service.slug,
            "analysisId": row.id,
            "mode": mode,
            "revision": row.revision,
            "repository": (
                f"{service.repository_workspace}/{service.repository_name}"
                if service.source_ready()
                else None
            ),
            "executedBy": row.executed_by_account,
        },
    )

    jobs.submit(row.id)
    db.session.refresh(row)
    return analysis_to_dict(row)


def cancel(analysis: CiRepositoryAnalysis, *, actor=None) -> Dict[str, Any]:
    """Ask a running analysis to stop at its next step.

    Cooperative rather than forced: the worker checks between steps, so a
    cancel lands within one model call rather than leaving a half-written row.
    """
    if analysis.state not in ACTIVE_STATES:
        raise AnalysisError("That analysis has already finished.")
    analysis.cancel_requested = True
    db.session.add(analysis)
    db.session.commit()
    log_audit(
        "ci_analysis_cancelled",
        actor=actor,
        target_type="ci_service",
        target_id=str(analysis.service_id),
        details={"analysisId": analysis.id},
    )
    return analysis_to_dict(analysis)


def service_state(service: CiService) -> Dict[str, Any]:
    """What the service detail page needs to know about assisted configuration."""
    latest = latest_for_service(service.id)
    return {
        "availability": availability(),
        "analysisState": service.analysis_state or "not_analyzed",
        "profileSource": service.profile_source,
        "applicationProfile": service.application_profile,
        "profileSummary": profile_module.summary(service.application_profile),
        "latestAnalysis": analysis_to_dict(latest),
    }
