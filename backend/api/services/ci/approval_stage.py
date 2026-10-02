"""The Approval stage: the build waits until enough of the right people say yes.

No runner executes this stage. The engine hands it here once every runner stage
has finished (server stages are last — pipelines.check_server_stages_last), and
it is usually followed by a Deploy or App store upload stage: "build and test
on every push, ship only when somebody signs off".

* **Waiting** is a running stage in phase ``waiting_approval``, persisted on
  ``CiBuildStage.server_state`` — a backend restart resumes the wait with every
  decision already given.
* **Deciding** happens through ``decide`` (``POST …/approve`` / ``…/reject``).
  Who may decide is the stage's own list: named users and/or anyone holding
  ``ci_builds:approve``. The person who started the build may not approve it
  unless the stage allows it — the same rule as the cluster approval gate.
  One rejection fails the stage, and so the build. ``minApprovals`` distinct
  approvals pass it, and the stages after it start at once.
* **Running out of time** fails the stage "not approved in time". A wait never
  turns into a pass by itself.
* **Cancelling** the build while it waits closes the stage as cancelled.

Every decision is audited with who, when and their comment, and kept on the
stage so the build drawer shows it.
"""

from __future__ import annotations

import logging
import threading
import time
from datetime import timedelta
from typing import Any, Dict, List, Optional, Tuple

from ...audit import log_audit
from ...db import db
from ...models_ci import CiBuild, CiBuildStage
from . import approval_config
from . import server_stage_base as base

logger = logging.getLogger(__name__)

PREFIX = "[approval]"
MAX_COMMENT_CHARS = 1000
# How many permission holders one stage will email. Beyond that the message is
# a broadcast, not a request; named approvers are always included.
_MAX_NOTIFY = 50


class ApprovalError(Exception):
    """A decision was refused. ``status`` is the HTTP status the route returns."""

    def __init__(self, message: str, status: int = 409):
        super().__init__(message)
        self.status = status


# ---------------------------------------------------------------------------
# Engine entry points
# ---------------------------------------------------------------------------

def start(build: CiBuild, stage: CiBuildStage, definition: Dict[str, Any]) -> None:
    """Begin waiting. Leaves the stage running, or closes it with the reason."""
    config = definition.get("approval") if isinstance(definition.get("approval"), dict) else None
    base.begin(stage, {"kind": "approval", "phase": "starting", "outcome": None, "message": ""})

    earlier = base.earlier_failure(build, stage)
    if earlier:
        _finish(stage, "skipped", f"Nobody was asked to approve: {earlier}", outcome="skipped")
        return
    if config is None:
        _fail(stage, "This Approval stage has nobody who may approve it. Open it in the pipeline editor and name the approvers.")
        return

    timeout = base.timeout_of(definition)
    requester = build.requested_by
    started_by = (
        {"userId": requester.id, "username": requester.username} if requester is not None else None
    )
    deadline = (stage.started_at or base.now()) + timedelta(seconds=timeout)
    base.save(
        stage,
        {
            "phase": "waiting_approval",
            "instructions": config.get("instructions") or "",
            "required": int(config.get("minApprovals") or 1),
            "approvers": {
                "users": list(config.get("users") or []),
                "anyoneWithPermission": bool(config.get("anyoneWithPermission")),
                "permission": approval_config.APPROVE_PERMISSION,
            },
            "allowSelfApproval": bool(config.get("allowSelfApproval")),
            "startedBy": started_by,
            "decisions": [],
            "waitingSince": base.now().isoformat(),
            "deadlineAt": deadline.isoformat(),
            "heartbeatAt": time.time(),
        },
    )
    required = int(config.get("minApprovals") or 1)
    _log(
        stage,
        f"Waiting for {required} approval{'s' if required != 1 else ''} from "
        f"{approval_config.describe_approvers(config)} — up to {base.limit_label(timeout)}.",
    )
    if config.get("instructions"):
        _log(stage, f"Message to approvers: {config['instructions']}")
    if started_by and not config.get("allowSelfApproval"):
        _log(stage, f"{started_by['username']} started this build, so they cannot approve it.")
    _audit("ci_build_approval_requested", build, stage, None, required=required,
           approvers=approval_config.describe_approvers(config))
    if config.get("notify"):
        _notify(build, stage, config)


def advance(build: CiBuild, stage: CiBuildStage, definition: Dict[str, Any]) -> None:
    """One look at a waiting stage: time out, or keep waiting. Cheap."""
    current = base.state(stage)
    if current.get("phase") != "waiting_approval":
        _fail(stage, f"The Approval stage lost track of its progress (phase '{current.get('phase')}').")
        return
    approvals = _approvals(current)
    required = int(current.get("required") or 1)
    if approvals >= required:
        # A decision was recorded but the stage was not closed (a crash between
        # the two). The approvals are what counts.
        _pass(build, stage, current)
        return
    timeout = base.timeout_of(definition)
    if base.elapsed(stage) > timeout:
        message = (
            f"Not approved within the stage's {base.limit_label(timeout)} limit "
            f"({approvals} of {required} approval{'s' if required != 1 else ''}). "
            "Nothing after this stage ran."
        )
        _log(stage, message)
        _audit("ci_build_approval_timed_out", build, stage, None, approvals=approvals, required=required)
        _finish(stage, "timeout", message, outcome="timed_out")
        return
    base.heartbeat(
        stage, PREFIX,
        f"Still waiting for approval ({approvals} of {required}).",
    )


def cancel(build: CiBuild, stage: CiBuildStage) -> None:
    """The build was cancelled while this stage waited."""
    current = base.state(stage)
    _audit("ci_build_approval_cancelled", build, stage, None, approvals=_approvals(current))
    _finish(stage, "cancelled", "Cancelled by request while waiting for approval.", outcome="cancelled")


def summarize(stage: CiBuildStage) -> str:
    """One sentence on what an Approval stage did — for tickets and emails."""
    current = base.state(stage)
    message = current.get("message") or stage.error or ""
    if current.get("phase") == "waiting_approval":
        return (
            f"Approval stage '{stage.name}' is waiting for approval "
            f"({_approvals(current)} of {int(current.get('required') or 1)})."
        )
    return f"Approval stage '{stage.name}': {message}".strip()


# ---------------------------------------------------------------------------
# Deciding
# ---------------------------------------------------------------------------

def eligibility(build: CiBuild, stage: CiBuildStage, user, action: str = "approve") -> Tuple[bool, str, int]:
    """Whether ``user`` may approve (or reject) now: (ok, why not, HTTP status)."""
    from ...access_engine import user_has_permission

    current = base.state(stage)
    if stage.stage_type != "approval":
        return False, "This stage is not an Approval stage.", 400
    if stage.status != "running" or current.get("phase") != "waiting_approval":
        return False, "This stage is not waiting for approval.", 409
    if build.cancel_requested:
        return False, "This build is being cancelled.", 409
    if user is None:
        return False, "Sign in to answer this approval.", 401
    if not getattr(user, "is_active", True):
        return False, "Your account is not active.", 403

    approvers = current.get("approvers") or {}
    named = any(int(item.get("id") or 0) == user.id for item in approvers.get("users") or [])
    by_permission = bool(approvers.get("anyoneWithPermission")) and user_has_permission(
        user, approval_config.APPROVE_PERMISSION
    )
    if not (named or by_permission):
        who = approval_config.describe_approvers(approvers)
        return False, f"Only {who} may answer this approval.", 403

    started_by = current.get("startedBy") or {}
    if (
        action == "approve"
        and not current.get("allowSelfApproval")
        and started_by.get("userId")
        and int(started_by["userId"]) == user.id
    ):
        return False, "You started this build, so you cannot approve it; another approver must.", 403
    if action == "approve" and any(
        d.get("decision") == "approve" and int(d.get("userId") or 0) == user.id
        for d in current.get("decisions") or []
    ):
        return False, "You have already approved this build.", 409
    return True, "", 200


def viewer(build: CiBuild, stage: CiBuildStage, user) -> Dict[str, Any]:
    """What the person looking at the build may do here — for the drawer."""
    can_approve, approve_reason, _ = eligibility(build, stage, user, "approve")
    can_reject, reject_reason, _ = eligibility(build, stage, user, "reject")
    return {
        "canApprove": can_approve,
        "canReject": can_reject,
        "reason": approve_reason if not can_approve else "",
        "rejectReason": reject_reason if not can_reject else "",
    }


def decide(build: CiBuild, stage: CiBuildStage, user, action: str, comment: str = "") -> Dict[str, Any]:
    """Record an approval or a rejection. Raises ApprovalError when refused."""
    if action not in ("approve", "reject"):
        raise ApprovalError("The decision must be approve or reject.", 400)
    # Locked so two approvers answering at once (or the engine timing the stage
    # out at that moment) cannot lose a decision between them. A no-op on
    # SQLite, a row lock on PostgreSQL.
    stage = (
        db.session.query(CiBuildStage)
        .filter(CiBuildStage.id == stage.id)
        .with_for_update()
        .populate_existing()
        .one()
    )
    ok, reason, status = eligibility(build, stage, user, action)
    if not ok:
        db.session.rollback()
        raise ApprovalError(reason, status)

    definition = _definition(build, stage)
    timeout = base.timeout_of(definition)
    if base.elapsed(stage) > timeout:
        db.session.rollback()
        raise ApprovalError(
            f"The approval window ({base.limit_label(timeout)}) has closed; the stage is being failed.", 409
        )

    text = " ".join(str(comment or "").split())[:MAX_COMMENT_CHARS]
    current = base.state(stage)
    decision = {
        "userId": user.id,
        "username": user.username,
        "decision": action,
        "comment": text,
        "at": base.now().isoformat(),
    }
    decisions: List[Dict[str, Any]] = list(current.get("decisions") or []) + [decision]
    base.save(stage, {"decisions": decisions})
    current = base.state(stage)
    required = int(current.get("required") or 1)

    if action == "reject":
        message = f"Rejected by {user.username}" + (f": {text}" if text else ".")
        _log(stage, message)
        _audit("ci_build_stage_rejected", build, stage, user, comment=text)
        _finish(stage, "failed", f"{message} Nothing after this stage ran.", outcome="rejected")
    else:
        approvals = _approvals(current)
        _log(
            stage,
            f"Approved by {user.username} ({approvals} of {required})" + (f": {text}" if text else "."),
        )
        _audit("ci_build_stage_approved", build, stage, user, comment=text,
               approvals=approvals, required=required)
        if approvals >= required:
            _pass(build, stage, current)
    db.session.commit()

    if stage.status != "running":
        # The stages after this one start now, not at the next tick.
        from . import engine

        engine.advance_build_now(build.id)
        engine._wake_engine()
    db.session.refresh(stage)
    return base.state(stage)


# ---------------------------------------------------------------------------
# Notification
# ---------------------------------------------------------------------------

def _recipients(build: CiBuild, config: Dict[str, Any], started_by: Optional[Dict[str, Any]]) -> List[Any]:
    from ...access_engine import user_has_permission
    from ...models import User

    excluded = (
        int(started_by["userId"])
        if started_by and started_by.get("userId") and not config.get("allowSelfApproval")
        else None
    )
    chosen: Dict[int, Any] = {}
    ids = [int(item.get("id") or 0) for item in config.get("users") or []]
    if ids:
        for user in User.query.filter(User.id.in_(ids)).all():
            if getattr(user, "is_active", True):
                chosen[user.id] = user
    if config.get("anyoneWithPermission"):
        extra = 0
        for user in User.query.filter(User.is_active.is_(True)).order_by(User.id.asc()).all():
            if user.id in chosen or not getattr(user, "email", None):
                continue
            if user_has_permission(user, approval_config.APPROVE_PERMISSION):
                chosen[user.id] = user
                extra += 1
                if extra >= _MAX_NOTIFY:
                    break
    return [
        user for user_id, user in chosen.items()
        if user_id != excluded and "@" in str(getattr(user, "email", "") or "")
    ]


def _notify(build: CiBuild, stage: CiBuildStage, config: Dict[str, Any]) -> None:
    """Email the approvers that a build is waiting. Never fails the build."""
    from . import build_status

    current = base.state(stage)
    try:
        recipients = _recipients(build, config, current.get("startedBy"))
    except Exception:  # noqa: BLE001 — a lookup failure must not fail the stage
        logger.exception("Could not work out who to notify for stage %s", stage.id)
        recipients = []
    if not recipients:
        base.save(stage, {"notified": {"recipients": [], "at": base.now().isoformat()}})
        _log(stage, "Nobody who may approve has an email address, so nobody was emailed.")
        return

    service = build.service.name if build.service else "a service"
    link = build_status.build_url(build)
    required = int(current.get("required") or 1)
    subject = f"[KubeSight] Build #{build.number} of {service} is waiting for your approval"
    lines = [
        f"CI build #{build.number} of {service} ({build.branch or 'default branch'}) is waiting at "
        f"the stage '{stage.name}' for {required} approval{'s' if required != 1 else ''}.",
    ]
    if current.get("instructions"):
        lines += ["", current["instructions"]]
    started_by = current.get("startedBy") or {}
    if started_by.get("username"):
        lines += ["", f"Started by: {started_by['username']}"]
    lines += [
        "",
        f"Approve or reject it in KubeSight: {link}" if link
        else "Approve or reject it in KubeSight: Service Catalog → the service → Builds.",
        f"It is failed if nobody approves it by {current.get('deadlineAt') or 'the stage timeout'}.",
    ]
    body = "\n".join(lines)
    addresses = [str(user.email) for user in recipients]
    base.save(
        stage,
        {"notified": {"recipients": [user.username for user in recipients], "at": base.now().isoformat()}},
    )
    _log(stage, f"Emailing {len(addresses)} approver{'s' if len(addresses) != 1 else ''}.")
    _dispatch(lambda: _send(addresses, subject, body, stage.id))


def _send(addresses: List[str], subject: str, body: str, stage_id: int) -> None:
    from ...email_delivery import EmailDeliveryError, send_email

    for address in addresses:
        try:
            send_email(address, subject, body)
        except EmailDeliveryError as exc:
            logger.warning("Approval email for stage %s to %s not sent: %s", stage_id, address, exc)
        except Exception:  # noqa: BLE001 — mail must never break a build
            logger.exception("Approval email for stage %s to %s failed", stage_id, address)


def _run_in_thread(job) -> None:
    """Send mail off the engine's pass — an SMTP server that is slow to answer
    must not hold up every other build. Inline under TESTING."""
    from flask import current_app

    try:
        app_obj = current_app._get_current_object()
    except RuntimeError:
        job()
        return
    if app_obj.config.get("TESTING"):
        job()
        return

    def runner():
        try:
            with app_obj.app_context():
                job()
        except Exception:  # noqa: BLE001
            logger.exception("Approval notification thread failed")

    threading.Thread(target=runner, name="ci-approval-mail", daemon=True).start()


# Replaced in tests when the send must be observed.
_dispatch = _run_in_thread


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _approvals(current: Dict[str, Any]) -> int:
    return len({
        int(d.get("userId") or 0)
        for d in current.get("decisions") or []
        if d.get("decision") == "approve"
    })


def _pass(build: CiBuild, stage: CiBuildStage, current: Dict[str, Any]) -> None:
    names = []
    for item in current.get("decisions") or []:
        if item.get("decision") == "approve" and item.get("username") not in names:
            names.append(item.get("username"))
    message = f"Approved by {', '.join(str(n) for n in names)}."
    _log(stage, f"✓ {message}")
    _audit("ci_build_approval_granted", build, stage, None, approvedBy=names)
    _finish(stage, "success", message, outcome="approved")


def _definition(build: CiBuild, stage: CiBuildStage) -> Dict[str, Any]:
    stages = (build.pipeline_snapshot or {}).get("stages") or []
    if 0 <= stage.position < len(stages):
        return stages[stage.position] or {}
    return {}


def _log(stage: CiBuildStage, message: str) -> None:
    base.log(stage, PREFIX, message)


def _fail(stage: CiBuildStage, message: str) -> None:
    _log(stage, message)
    _finish(stage, "failed", message, outcome="failed")


def _finish(stage: CiBuildStage, status: str, message: str, *, outcome: str) -> None:
    base.finish(stage, status, message, outcome=outcome)


def _audit(action: str, build: CiBuild, stage: CiBuildStage, user, **extra) -> None:
    log_audit(
        action,
        actor=user,
        target_type="ci_build",
        target_id=str(build.id),
        details={
            "service": build.service.slug if build.service else None,
            "buildNumber": build.number,
            "stage": stage.name,
            **extra,
        },
        commit=False,
    )
