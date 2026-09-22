"""Telling Bitbucket what the gate decided, and not stopping until it lands.

Delivery is separated from the verdict on purpose. Deciding is instant, local
and cannot fail; telling a third-party host is none of those things. Keeping
them apart means a network blip never corrupts a verdict, and a delivery that
failed can be retried without re-running anything.

The retry is on the row, not in memory: ``delivery_state='pending'`` with a
``next_delivery_at`` is what survives a backend restart, and the CI engine's
ordinary pass is what picks it up. A backoff, because a host that just refused
a write is not helped by being asked again immediately, and a cap, because an
integration that retries forever is a denial of service with good intentions.

Two things are sent, in this order and with this priority:

1. the commit build status — the thing that actually gates the merge, and the
   only one whose failure counts as a failed delivery;
2. the pull request comment — the explanation. Best effort: a verdict that
   reached Bitbucket but whose comment did not is a delivered verdict, and
   re-posting the status to retry the comment would be worse than the gap.
"""

from __future__ import annotations

import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from flask import has_request_context, request

from ....db import db
from ....models_merge_checks import MAX_DELIVERY_ATTEMPTS, CiMergeCheck
from .. import source as source_port
from .policy import TOOL_CAP_FIELD
from .stages import tool_label

logger = logging.getLogger(__name__)

# Backoff between delivery attempts, in seconds, indexed by attempt count. The
# last value repeats. Short at first (a blip), long after (an outage).
_BACKOFF_SECONDS = (15, 30, 60, 120, 300, 600, 900)

# The port's verdict vocabulary. `unknown` is not in it: a check that could not
# reach a verdict still reports `failed` to the host, because "we could not
# check this" must not read as "nothing to see here" on the merge button.
_PORT_STATE = {"allowed": "passed", "blocked": "failed", "unknown": "failed"}


def _now() -> datetime:
    return datetime.now(timezone.utc)


def public_base_url() -> str:
    """Where a developer following the status link ends up.

    Shares the environment variables the deployment-request emails already use,
    because it is the same question — what is this installation's address from
    outside — and answering it twice invites two different answers.
    """
    configured = (
        os.getenv("MERGE_CHECK_BASE_URL", "")
        or os.getenv("PUBLIC_BASE_URL", "")
        or os.getenv("APP_PUBLIC_URL", "")
    ).strip()
    if configured:
        return configured.rstrip("/")
    if has_request_context():
        return request.host_url.rstrip("/")
    return ""


def check_url(check: CiMergeCheck) -> str:
    """The page that explains this verdict, in KubeSight's own hash routing.

    Deep-links to the service's Merge Checks tab with the build open, so the
    link on a red build status lands on the logs rather than on a dashboard.
    """
    base = public_base_url()
    if not base:
        return ""
    if check.build_id:
        return (
            f"{base}/#/catalog/{check.service_id}"
            f"?tab=mergeChecks&build={check.build_id}"
        )
    return f"{base}/#/catalog/{check.service_id}?tab=mergeChecks"


# ---------------------------------------------------------------------------
# The comment
# ---------------------------------------------------------------------------

def comment_markdown(check: CiMergeCheck) -> str:
    """The explanation left on the pull request.

    Written to be read by whoever has to fix it: the verdict first, then the
    per-tool numbers against their caps, then the reasons in words, then the
    link. No build ids, no stage names, nothing that means something only to
    the person who configured the pipeline.
    """
    gate = dict(check.gate or {})
    metrics = dict(check.metrics or {})
    allowed = check.verdict == "allowed"
    heading = (
        "**KubeSight merge checks passed**"
        if allowed
        else "**KubeSight merge checks blocked this merge**"
    )
    lines: List[str] = [heading, ""]

    total_cap = gate.get("maxTotalProblems")
    total = check.total_problems if check.total_problems is not None else 0
    lines.append(
        f"{total} problem{'' if total == 1 else 's'} found"
        + (f", of {total_cap} allowed." if total_cap is not None else ".")
    )
    lines.append("")
    lines.append("| Check | Problems | Limit | Result |")
    lines.append("| --- | --- | --- | --- |")
    for tool, report in metrics.items():
        if not isinstance(report, dict):
            continue
        status = str(report.get("status") or "")
        cap = gate.get(TOOL_CAP_FIELD.get(tool, ""), None)
        if status == "ok":
            problems = report.get("problems", 0)
            over = cap is not None and problems > cap
            result = "over the limit" if over else "ok"
        elif status == "skipped":
            problems, result = "—", "not applicable"
        else:
            problems, result = "—", "did not run"
        lines.append(
            f"| {tool_label(tool)} | {problems} | "
            f"{'no limit' if cap is None else cap} | {result} |"
        )

    reasons = list(check.reasons or [])
    if reasons:
        lines.extend(["", "**Why:**"])
        lines.extend(f"- {reason}" for reason in reasons)

    url = check_url(check)
    if url:
        lines.extend(["", f"[Full logs in KubeSight]({url})"])
    return "\n".join(lines)


def status_description(check: CiMergeCheck) -> str:
    """The one line Bitbucket shows on the status itself."""
    from .policy import summarize

    return summarize(
        {
            "verdict": check.verdict or "unknown",
            "totalProblems": check.total_problems or 0,
            "reasons": list(check.reasons or []),
        },
        dict(check.gate or {}),
    )


# ---------------------------------------------------------------------------
# Delivery
# ---------------------------------------------------------------------------

def _provider_and_ref(service):
    handler = source_port.get_provider(service.repository_provider)
    ref = handler.parse_repository_url(service.repository_url)
    return handler, ref


def deliver(check: CiMergeCheck) -> Dict[str, Any]:
    """Hand one verdict to the source host. Records the outcome on the row.

    Never raises: every failure is a state on the row, because the caller is a
    scheduler pass that must go on to the next check either way.
    """
    service = check.service
    config = check.config
    if service is None or not service.source_ready():
        return _give_up(check, "The service no longer has a repository configured.")

    try:
        handler, ref = _provider_and_ref(service)
    except Exception as exc:  # noqa: BLE001 - a bad URL is permanent, not transient
        return _give_up(check, f"Could not address the repository: {exc}")

    poster = getattr(handler, "post_check_verdict", None)
    if poster is None:
        return _give_up(
            check,
            f"The {service.repository_provider} provider cannot report a verdict.",
        )

    status_key = (config.status_key if config else "") or "KUBESIGHT-MERGE"
    check.delivery_attempts = int(check.delivery_attempts or 0) + 1
    try:
        poster(
            ref,
            service.credential_profile,
            commit_sha=check.commit_sha or "",
            status_key=status_key,
            state=_PORT_STATE.get(check.verdict or "unknown", "failed"),
            name="KubeSight merge checks",
            description=status_description(check),
            url=check_url(check) or public_base_url(),
        )
    except source_port.SourceError as exc:
        return _failed(check, str(exc), retryable=getattr(exc, "retryable", True))
    except Exception as exc:  # noqa: BLE001 - never let one check stop the pass
        logger.exception("Merge check %s: unexpected delivery failure", check.id)
        return _failed(check, f"Unexpected failure reporting the verdict: {exc}")

    # The status landed. From here nothing can un-deliver this verdict, so the
    # comment is attempted inside its own try and its failure is recorded
    # WITHOUT reopening delivery — a retry would re-post the status too.
    comment_error = ""
    if config is not None and config.post_comment and check.pull_request_id:
        commenter = getattr(handler, "post_pull_request_note", None)
        if commenter is not None:
            try:
                commenter(
                    ref,
                    service.credential_profile,
                    pull_request_id=str(check.pull_request_id),
                    markdown=comment_markdown(check),
                )
            except Exception as exc:  # noqa: BLE001 - explanatory, not gating
                comment_error = f"The verdict was reported; the comment was not: {exc}"
                logger.warning("Merge check %s: %s", check.id, comment_error)

    check.delivery_state = "delivered"
    check.delivered_at = _now()
    check.delivery_error = comment_error or None
    check.next_delivery_at = None
    db.session.add(check)
    db.session.commit()
    return {"delivered": True, "warning": comment_error}


def _failed(check: CiMergeCheck, message: str, *, retryable: bool = True) -> Dict[str, Any]:
    if not retryable or check.delivery_attempts >= MAX_DELIVERY_ATTEMPTS:
        return _give_up(check, message)
    index = min(check.delivery_attempts - 1, len(_BACKOFF_SECONDS) - 1)
    check.delivery_state = "pending"
    check.delivery_error = message[:2000]
    check.next_delivery_at = _now() + timedelta(seconds=_BACKOFF_SECONDS[max(0, index)])
    db.session.add(check)
    db.session.commit()
    return {"delivered": False, "error": message, "willRetry": True}


def _give_up(check: CiMergeCheck, message: str) -> Dict[str, Any]:
    """Stop trying, and leave the reason where somebody will find it.

    The verdict stays exactly as it was. What is lost is only Bitbucket's copy
    of it — and a merge check whose verdict never reached the host is precisely
    the thing the Merge Checks tab has to show in red rather than bury.
    """
    check.delivery_state = "failed"
    check.delivery_error = message[:2000]
    check.next_delivery_at = None
    db.session.add(check)
    db.session.commit()
    logger.warning("Merge check %s will not be delivered: %s", check.id, message)
    return {"delivered": False, "error": message, "willRetry": False}


def due_checks(limit: int = 20) -> List[CiMergeCheck]:
    """Verdicts waiting to be told to the host, oldest first."""
    now = _now()
    rows = (
        CiMergeCheck.query.filter(CiMergeCheck.delivery_state == "pending")
        .filter(CiMergeCheck.verdict.isnot(None))
        .order_by(CiMergeCheck.id.asc())
        .limit(max(1, int(limit)) * 4)
        .all()
    )
    due: List[CiMergeCheck] = []
    for row in rows:
        scheduled = row.next_delivery_at
        if scheduled is not None and scheduled.tzinfo is None:
            scheduled = scheduled.replace(tzinfo=timezone.utc)
        if scheduled is None or scheduled <= now:
            due.append(row)
        if len(due) >= limit:
            break
    return due


def pending_count() -> int:
    return CiMergeCheck.query.filter(
        CiMergeCheck.delivery_state == "pending", CiMergeCheck.verdict.isnot(None)
    ).count()


def mark_running(check: CiMergeCheck) -> Optional[str]:
    """Tell the host the checks have started, so the PR shows them pending.

    Best effort and never retried: an in-progress status that did not land is
    replaced by the verdict a few minutes later anyway, and failing a check
    because its *optimistic* status did not post would be absurd.
    """
    service = check.service
    if service is None or not service.source_ready() or not check.commit_sha:
        return None
    try:
        handler, ref = _provider_and_ref(service)
        poster = getattr(handler, "post_check_verdict", None)
        if poster is None:
            return None
        poster(
            ref,
            service.credential_profile,
            commit_sha=check.commit_sha,
            status_key=(check.config.status_key if check.config else "KUBESIGHT-MERGE"),
            state="running",
            name="KubeSight merge checks",
            description="Running ESLint, SonarQube and Dependency-Check…",
            url=check_url(check),
        )
    except Exception as exc:  # noqa: BLE001 - optimistic, never load-bearing
        logger.info("Merge check %s: in-progress status not posted (%s)", check.id, exc)
        return str(exc)
    return None
