"""Evidence in, a validated proposal out — or an honest account of why not.

The shape of this module is the shape of the guarantee. Every path through it
ends in one of three states, and none of them is "a pipeline nobody checked":

    analyzed   a profile and a pipeline that passed KubeSight's own validator
    partial    a profile, and a pipeline that did not pass, with the reasons
    failed     neither, with a message a person can act on

``partial`` is not a degraded ``analyzed``; it is a different answer. The
profile is usually still right — "Java 17, Gradle 8.7" is read from files — even
when the pipeline built around it is not. Throwing that away because the
pipeline failed would make the user re-establish facts KubeSight already knows.

The correction loop is bounded twice over: by an attempt count, and by whether
the errors are *changing*. A model that returns the same objection twice will
return it a third time, and spending the last attempt to confirm that only makes
the user wait longer for the same answer.
"""

from __future__ import annotations

import logging
import os
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

from ...audit import log_audit
from ...db import db
from ...models_ci import CiRepositoryAnalysis, CiRunner, CiService
from ..application_intelligence_hermes import HermesError
from ..application_intelligence_security import safe_error
from ..ci import build_environments, generated, pipelines as pipelines_service
from ..ci.runners.base import available_runner_types
from . import evidence as evidence_module
from . import examples as examples_module
from . import hermes, profile as profile_module

logger = logging.getLogger(__name__)

DEFAULT_REPAIR_ATTEMPTS = 2
MAX_REPAIR_ATTEMPTS = 3

# What the user is told while this runs. Real steps, not a fabricated
# percentage: each one is announced when it actually starts.
STEPS = {
    "connecting": (5, "Connecting to the repository"),
    "structure": (20, "Inspecting project structure"),
    "reading": (35, "Reading build configuration"),
    "detecting": (55, "Detecting application and framework"),
    "generating": (70, "Generating pipeline"),
    "validating": (85, "Validating pipeline"),
    "repairing": (90, "Correcting pipeline"),
    "done": (100, "Complete"),
}


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _model_name() -> str:
    from . import hermes as hermes_module

    return hermes_module._model()


def repair_attempts() -> int:
    try:
        value = int(os.getenv("CI_ASSIST_REPAIR_ATTEMPTS", "").strip() or DEFAULT_REPAIR_ATTEMPTS)
    except ValueError:
        value = DEFAULT_REPAIR_ATTEMPTS
    return max(0, min(MAX_REPAIR_ATTEMPTS, value))


# ---------------------------------------------------------------------------
# What KubeSight can actually execute
# ---------------------------------------------------------------------------

def capabilities_payload() -> Dict[str, Any]:
    """The menu a proposal must choose from.

    Assembled from what is REGISTERED, not from a list in a file: the runner
    labels are the capabilities the fleet actually advertises, so a proposal
    cannot ask for a machine nobody has. This is the same set the validator
    checks against, which is why a well-formed proposal passes first time.
    """
    from ...models_ci import RUNNER_TYPES, STAGE_TYPES

    labels: set = set()
    for runner in CiRunner.query.filter(CiRunner.enabled.is_(True)).all():
        labels.update(
            str(item).strip().lower()
            for item in (runner.capabilities or [])
            if str(item).strip()
        )

    shipped = set(available_runner_types())

    # The fleet as it actually is, machine by machine.
    #
    # A merged list of every label anybody advertises reads as one capable
    # runner, which is how a proposal ends up asking for "macos" and "java" on
    # the same stage — both labels exist, no single machine has both. Showing
    # the runners individually is the difference between "these words are
    # allowed" and "this is what you can run on".
    fleet = []
    for runner in CiRunner.query.order_by(CiRunner.name.asc()).all():
        if runner.runner_type not in shipped:
            continue
        fleet.append(
            {
                "name": runner.name,
                "type": runner.runner_type,
                "os": runner.os or "",
                "arch": runner.arch or "",
                "capabilities": sorted(
                    str(item).strip().lower()
                    for item in (runner.capabilities or [])
                    if str(item).strip()
                ),
                # Said plainly so a proposal can prefer a machine that will
                # actually pick the work up.
                "state": (
                    "online"
                    if runner.enabled and runner.status == "online"
                    else "offline" if runner.enabled else "disabled"
                ),
            }
        )

    return {
        # Only the types with an executor are offered. The others validate but
        # skip at run time, and proposing one produces a green build that made
        # nothing — the single worst outcome this feature could have.
        "stageTypes": ["checkout", "command", "container_image"],
        "runnerTypes": [item for item in RUNNER_TYPES if item in shipped],
        "runnerLabels": sorted(labels),
        # What each stage's runnerLabels are actually matched against. A stage
        # runs on a runner whose capabilities are a SUPERSET of its labels, so
        # every label on one stage has to be satisfied by one machine.
        "runners": fleet,
        "runnerSelection": (
            "A stage runs on a runner whose capabilities contain ALL of that "
            "stage's runnerLabels. Choose labels that one machine in `runners` "
            "satisfies; labels spread across two machines match neither, and "
            "the build queues forever. Prefer a runner whose state is online."
        ),
        "buildEnvironments": [
            {
                "key": item["key"],
                "label": item["label"],
                "provides": item["provides"],
                "labels": item["labels"],
                "configured": item["configured"],
                "notes": item["notes"],
            }
            for item in build_environments.catalog()
        ],
        "parameterTypes": list(pipelines_service.PARAMETER_TYPES),
        "artifactTypes": [
            "jar", "war", "zip", "binary", "apk", "aab", "ipa",
            "test-report", "coverage-report", "sbom",
        ],
        "stageTypesNotExecutable": [
            item for item in STAGE_TYPES if item not in ("checkout", "command", "container_image")
        ],
        "workspaceVariables": {
            "$KUBESIGHT_WORKSPACE": "The build workspace, shared by every stage.",
            "$KUBESIGHT_SOURCE": "The checkout, inside the workspace. Stages start here.",
            "$KUBESIGHT_ENV": "Append NAME=value to export a variable to LATER stages.",
            "$KUBESIGHT_BUILD_NUMBER": "This build's number.",
            "$KUBESIGHT_BRANCH": "The branch or tag being built.",
            "$KUBESIGHT_COMMIT": "The commit SHA being built.",
            "$KUBESIGHT_SERVICE": "The service slug.",
        },
        "limits": {
            "maxStages": generated.MAX_STAGES,
            "maxCommandsPerStage": 100,
            "minTimeoutSeconds": 30,
            "maxTimeoutSeconds": 24 * 3600,
        },
        "notes": [
            "Every stage shares one workspace. A file written by one stage is visible to the next.",
            "A stage's environment does NOT carry into the next stage; append to $KUBESIGHT_ENV for that.",
            "A checkout stage runs no commands — KubeSight performs the checkout itself.",
            "A container_image stage runs no commands — BuildKit builds the Dockerfile.",
        ],
    }


# ---------------------------------------------------------------------------
# Progress
# ---------------------------------------------------------------------------

def _step(row: CiRepositoryAnalysis, key: str) -> None:
    percent, label = STEPS[key]
    row.progress_percent = percent
    row.current_stage = label
    row.last_heartbeat_at = _now()
    db.session.add(row)
    db.session.commit()


def _cancelled(row: CiRepositoryAnalysis) -> bool:
    db.session.refresh(row)
    return bool(row.cancel_requested)


def _finish(
    row: CiRepositoryAnalysis,
    state: str,
    *,
    failure_stage: str = "",
    message: str = "",
) -> None:
    row.state = state
    row.completed_at = _now()
    row.last_heartbeat_at = _now()
    if state in ("analyzed", "partial"):
        row.progress_percent = 100
        row.current_stage = STEPS["done"][1]
    if failure_stage:
        row.failure_stage = failure_stage
    if message:
        row.safe_error_message = message
    db.session.add(row)

    # The service carries its own copy of this, because the catalog and the
    # service header read it without loading an analysis. Leaving it at
    # "analyzing" after the analysis ended would show a spinner next to a row
    # that finished — the two must not be able to disagree.
    service = row.service
    if service is not None:
        if state == "cancelled":
            # A cancelled attempt establishes nothing. Fall back to whatever was
            # already known rather than recording a state that never happened.
            service.analysis_state = (
                "analyzed" if service.application_profile else "not_analyzed"
            )
        else:
            service.analysis_state = state
        db.session.add(service)
    db.session.commit()


def _record_attempt(
    row: CiRepositoryAnalysis,
    *,
    kind: str,
    model: str,
    errors: List[Dict[str, str]],
    note: str = "",
) -> None:
    """One line of the proposal's history.

    Error CODES and stage names only. Enough to see that the model kept
    proposing an unavailable runner label, without putting repository content
    into a row that outlives the analysis.
    """
    entries = list(row.attempts or [])
    entries.append(
        {
            "kind": kind,
            "model": model,
            "at": _now().isoformat(),
            "errorCodes": sorted({item.get("code", "") for item in errors if item.get("code")}),
            "errorCount": len(errors),
            "note": note,
        }
    )
    row.attempts = entries[:10]
    db.session.add(row)
    # Committed immediately, and that is the point: the next thing the loop does
    # is check for cancellation, which refreshes the row from the database and
    # would otherwise throw this away. It is also the trail that survives the
    # process dying mid-loop, which is exactly when somebody wants it.
    db.session.commit()


def _signature(errors: List[Dict[str, str]]) -> frozenset:
    """What "the same objection again" means.

    Code plus stage, not the message: a reworded complaint about the same stage
    is the same complaint, and treating it as progress would spend an attempt
    on nothing.
    """
    return frozenset((item.get("code", ""), item.get("stage", "")) for item in errors)


# ---------------------------------------------------------------------------
# The run
# ---------------------------------------------------------------------------

def run(analysis_id: int) -> None:
    """Execute one analysis to completion. Never raises.

    Called on a worker thread inside an app context. Everything that can go
    wrong is recorded on the row, because a caller polling that row is the only
    thing watching — an exception escaping here would leave an analysis running
    forever from the user's point of view.
    """
    row = db.session.get(CiRepositoryAnalysis, int(analysis_id))
    if row is None or row.state not in ("queued",):
        return
    service = row.service
    if service is None:
        _finish(row, "failed", failure_stage="Scheduling", message="The service was removed.")
        return

    row.state = "analyzing"
    row.started_at = _now()
    row.pipeline_state = "generating"
    db.session.add(row)
    db.session.commit()

    try:
        _execute(row, service)
    except Exception as exc:  # noqa: BLE001 — the row is the only error channel
        logger.exception("CI assist analysis %s failed", analysis_id)
        db.session.rollback()
        row = db.session.get(CiRepositoryAnalysis, int(analysis_id))
        if row is not None and row.state not in ("analyzed", "partial", "failed", "cancelled"):
            row.pipeline_state = "not_generated"
            _finish(
                row,
                "failed",
                failure_stage=row.current_stage or "Analyzing",
                message=safe_error(exc, "Repository analysis failed safely."),
            )


def _execute(row: CiRepositoryAnalysis, service: CiService) -> None:
    # Whatever the user has already decided, in either mode. In `profile` mode
    # it is the entire application profile they typed; in `repository` mode it
    # is the corrections they made to a previous analysis, and a regenerate
    # that detected over the top of those would make them correct the same
    # field every time — which is how somebody stops using a feature.
    hint = row.application_profile or None

    # --- Evidence ---------------------------------------------------------
    if row.mode == "profile":
        # No repository is read at all: the user described the application and
        # asked for a pipeline for it. Legitimate on its own, and the only path
        # available when the source host is unreachable.
        payload: Dict[str, Any] = {
            "repository": {"revision": row.revision or "", "workingDirectory": ""},
            "tree": [],
            "files": [],
            "deterministic": {},
        }
        coverage = {"mode": "profile", "filesRead": 0}
        _step(row, "detecting")
    else:
        _step(row, "connecting")
        try:
            collected = evidence_module.collect(service, row.revision or "")
        except evidence_module.EvidenceError as exc:
            _finish(row, "failed", failure_stage="Reading repository", message=str(exc))
            return
        if _cancelled(row):
            _finish(row, "cancelled", failure_stage="Reading repository")
            return
        _step(row, "structure")
        payload = collected.as_payload()
        coverage = collected.coverage
        row.evidence_coverage = coverage
        row.revision = collected.revision
        db.session.add(row)
        db.session.commit()
        _step(row, "reading")

    capabilities = capabilities_payload()

    # --- Propose ----------------------------------------------------------
    #
    # A malformed answer is asked about, not given up on. The contract is still
    # strict — nothing unreadable is ever accepted — but "stage 1 has no name"
    # is a sentence a model acts on, and losing a whole repository analysis to
    # it would make the feature feel arbitrary.
    _step(row, "generating")
    # Shown alongside the rules: pipelines that already run here. Most of what a
    # correction round used to teach — the field names, the label discipline,
    # the level of detail — is learnable from one instance, and learning it
    # before the first answer costs nothing.
    worked = examples_module.worked_examples(
        preferred_type=(hint or {}).get("derivedApplicationType", "")
        or service.application_type
    )
    feedback: List[Dict[str, str]] = []
    result = model = prompt_version = None
    for remaining in range(repair_attempts(), -1, -1):
        try:
            result, model, prompt_version = hermes.propose(
                evidence=payload,
                capabilities=capabilities,
                profile_hint=hint,
                feedback=feedback or None,
                examples=worked,
            )
            break
        except hermes.ContractFailure as exc:
            objection = {"code": "contract", "stage": "", "field": "", "message": str(exc)}
            _record_attempt(
                row,
                kind="propose",
                model=_model_name(),
                errors=[objection],
                note="malformed response",
            )
            if not remaining:
                _finish(
                    row,
                    "failed",
                    failure_stage="Generating pipeline",
                    message=(
                        f"{exc} Hermes was asked again with the problem stated and "
                        "still did not return a usable pipeline. Configure the "
                        "service manually, or retry."
                    ),
                )
                return
            feedback = [objection]
            if _cancelled(row):
                _finish(row, "cancelled", failure_stage="Generating pipeline")
                return
        except HermesError as exc:
            # Nothing to say to an unreachable gateway.
            _record_attempt(row, kind="propose", model="", errors=[], note="failed")
            _finish(
                row,
                "failed",
                failure_stage="Generating pipeline",
                message=safe_error(exc, "Hermes could not complete the analysis."),
            )
            return

    row.hermes_model = model
    row.hermes_prompt_version = prompt_version
    row.schema_version = result.get("schemaVersion")
    db.session.add(row)

    # --- Profile ----------------------------------------------------------
    # Stored before the pipeline is validated, and kept whatever happens to it.
    # "Java 17, Gradle 8.7" was read out of files; a pipeline KubeSight refused
    # says nothing about whether that reading was right.
    try:
        detected = profile_module.normalize(result["applicationProfile"], source="hermes")
    except profile_module.ProfileError as exc:
        _finish(
            row,
            "failed",
            failure_stage="Detecting application",
            message=f"Hermes described the application in a way KubeSight cannot store: {exc}",
        )
        return

    if hint:
        # A user-supplied profile is authoritative; the model may add detail it
        # did not contradict, never replace what the user set.
        detected = _merge_user_profile(detected, hint)
    row.application_profile = detected
    # Hermes's own caveats, plus anything KubeSight had to read differently from
    # how it arrived ("executable jar" -> "jar"). A reading that was adjusted is
    # shown rather than quietly diverging from what the model actually said.
    row.warnings = [
        *list(result["analysis"]["warnings"]),
        *list(detected.get("notes") or []),
    ][:20]
    db.session.add(row)
    db.session.commit()

    if _cancelled(row):
        _finish(row, "cancelled", failure_stage="Generating pipeline")
        return

    # --- Validate, and repair if we must ----------------------------------
    #
    # Two questions, deliberately separate:
    #
    #   strict  — is there anything worth asking Hermes to fix?
    #   verdict — what will actually be saved and shown?
    #
    # The repair loop reads the first, because a correction round is only worth
    # a user's wait when there is something concrete to correct. The row stores
    # the second, because under the default advise policy the pipeline is kept
    # whatever the objections were, and the person approving it is the gate.
    _step(row, "validating")
    proposal = result["pipeline"]
    required_inputs = result["requiredInputs"]
    strict = generated.validate(
        service, proposal, declared_inputs=required_inputs, enforce=True
    )
    verdict = generated.validate(service, proposal, declared_inputs=required_inputs)
    _record_attempt(row, kind="propose", model=model, errors=strict["errors"])

    attempts_left = repair_attempts()
    seen = {_signature(strict["errors"])}
    while not strict["valid"] and attempts_left > 0:
        if _cancelled(row):
            _finish(row, "cancelled", failure_stage="Correcting pipeline")
            return
        attempts_left -= 1
        _step(row, "repairing")
        try:
            result, model, prompt_version = hermes.repair(
                evidence=payload,
                capabilities=capabilities,
                previous=proposal,
                errors=generated.error_feedback(strict["errors"]),
                profile_hint=hint or detected,
            )
        except HermesError as exc:
            # Including a malformed correction: the proposal that already
            # passed the contract stays on the row as a `partial`, which is
            # more useful than discarding it because round two was unreadable.
            _record_attempt(row, kind="repair", model=model, errors=[], note=safe_error(exc))
            break

        proposal = result["pipeline"]
        required_inputs = result["requiredInputs"]
        strict = generated.validate(
            service, proposal, declared_inputs=required_inputs, enforce=True
        )
        verdict = generated.validate(service, proposal, declared_inputs=required_inputs)
        signature = _signature(strict["errors"])
        repeated = signature in seen
        _record_attempt(
            row,
            kind="repair",
            model=model,
            errors=strict["errors"],
            note="no change in errors" if repeated and not strict["valid"] else "",
        )
        if strict["valid"]:
            break
        if repeated:
            # The same objection twice. A third attempt costs the user another
            # wait for the answer they already have.
            break
        seen.add(signature)

    row.generated_pipeline = verdict["pipeline"] or proposal
    row.required_inputs = required_inputs
    row.validation = {
        "valid": verdict["valid"],
        "errors": verdict["errors"],
        "warnings": verdict["warnings"],
        # Kept whole, secret references included. Auto-saving strips references
        # to secrets that do not exist yet; this is what they are restored from
        # when somebody supplies the values.
        "proposed": verdict["pipeline"] or proposal,
    }
    row.pipeline_state = "valid" if verdict["valid"] else "invalid"

    # A build environment the installation has not configured, or a Java version
    # nothing approved can provide, is worth saying out loud next to the result.
    for note in _environment_warnings(detected):
        if note not in (row.warnings or []):
            row.warnings = [*(row.warnings or []), note][:20]

    db.session.add(row)
    db.session.commit()

    # Save it. The review step is skipped deliberately — a pipeline in the
    # editor is reviewable at leisure and editable in place, which is a better
    # place to disagree with it than a modal standing between somebody and a
    # registered service. Anything KubeSight cannot supply (a Nexus password) is
    # recorded as still needed rather than guessed.
    saved = None
    if verdict["valid"]:
        try:
            from . import accept as accept_service

            saved = accept_service.auto_accept(row, actor=row.requested_by)
        except Exception:  # noqa: BLE001 — a save failure must not lose the analysis
            logger.exception("Auto-saving the generated pipeline failed for %s", row.id)
            db.session.rollback()
            row = db.session.get(CiRepositoryAnalysis, row.id)

    _finish(row, "analyzed" if verdict["valid"] else "partial")
    log_audit(
        "ci_analysis_completed",
        actor=row.requested_by,
        target_type="ci_service",
        target_id=str(service.id),
        details={
            "service": service.slug,
            "analysisId": row.id,
            "state": row.state,
            "pipelineState": row.pipeline_state,
            "applicationType": detected.get("derivedApplicationType"),
            "model": model,
            "promptVersion": prompt_version,
            "attempts": len(row.attempts or []),
            "filesRead": coverage.get("filesRead"),
        },
    )


def _merge_user_profile(detected: Dict[str, Any], hint: Dict[str, Any]) -> Dict[str, Any]:
    """User-set fields win; everything else the model added is kept."""
    merged = dict(detected)
    overrides = dict(hint.get("overrides") or {})
    for field in profile_module.OVERRIDABLE:
        value = hint.get(field)
        if value in (None, "", False) and field not in overrides:
            continue
        if field in overrides or (value not in (None, "") and hint.get("source") == "manual"):
            merged[field] = value
    merged["overrides"] = overrides
    merged["source"] = hint.get("source") or "manual"
    merged["derivedApplicationType"] = profile_module.derive_application_type(merged)
    return merged


def _environment_warnings(detected: Dict[str, Any]) -> List[str]:
    """Where what this project needs and what this installation approves differ."""
    notes: List[str] = []
    key, warning = build_environments.best_for(
        language=detected.get("language", ""),
        language_version=detected.get("languageVersion", ""),
        build_system=detected.get("buildSystem", ""),
    )
    if warning:
        notes.append(warning)
    elif key is None and detected.get("language"):
        notes.append(
            f"No approved build environment covers {detected['language']}. The generated "
            "pipeline runs on whatever its runner provides; add an entry to the build "
            "environment catalog to pin it."
        )
    return notes
