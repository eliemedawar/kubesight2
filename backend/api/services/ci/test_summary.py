"""Where a build's test results and coverage are kept, and how they are read.

:mod:`test_reports` reads one file; this module decides which files to read,
writes what it found onto the artifact and the build, and serves it back.

Every report file reaches KubeSight through :func:`artifacts.record_artifact`
— the Kubernetes collector's upload, the agent's upload, anything later — and
that function calls :func:`ingest` once the bytes are stored. One hook rather
than one per upload route, so a new way of uploading cannot forget it.

Two copies, on purpose:

* the artifact's metadata gets the file's own counts (``testReport``), so the
  artifact list can say what each file held;
* the build gets the running total (``CiBuild.test_summary``), failed cases
  included. Artifacts expire after a day; the build's numbers are what the
  trend across builds is drawn from, so they must outlive them.

Nothing here may fail an upload or a build. A report that cannot be read is
recorded as a parse error on the artifact and in the build's summary, and the
upload returns 201 as it always did.
"""

from __future__ import annotations

import logging
import os
from typing import Any, Dict, List, Optional

from ...db import db
from ...models_ci import CiArtifact, CiBuild, CiBuildStage
from . import test_reports
from .test_reports import COVERAGE_REPORT, REPORT_TYPES, TEST_REPORT, NotCounted, ReportError

logger = logging.getLogger(__name__)

# Files kept under the catch-all type are looked at too, but only when their
# name says they could be a report. Somebody keeping ``target/**`` as binary
# has their surefire files in there, and seeing the tests is what they want;
# opening every jar to find out it is not XML is not.
SNIFF_TYPES = ("binary",)
SNIFF_SUFFIXES = (".xml", ".trx", ".info", ".json")

DEFAULT_TREND_BUILDS = 20
MAX_TREND_BUILDS = 50
# How far back the trend looks for builds that HAVE reports. A service whose
# merge-check builds never collect tests still gets a full trend line.
TREND_SCAN_FACTOR = 3


def _mode(artifact_type: str, name: str) -> Optional[str]:
    if artifact_type in REPORT_TYPES:
        return "explicit"
    if artifact_type in SNIFF_TYPES and str(name or "").lower().endswith(SNIFF_SUFFIXES):
        return "sniff"
    return None


def ingest(row: CiArtifact, local_path: Optional[str]) -> None:
    """Read a just-stored artifact if it is a report, and record what it held.

    Called by ``record_artifact`` after the row is in the session. Never
    raises: the worst a broken report can do is say it is broken.
    """
    if not local_path or not os.path.isfile(local_path):
        return
    mode = _mode(row.artifact_type, row.name)
    if mode is None:
        return
    result: Optional[Dict[str, Any]] = None
    error: Optional[str] = None
    try:
        if mode == "sniff":
            # A binary-typed file is only treated as a report when its content
            # unmistakably is one. Anything doubtful is left alone, silently —
            # nobody asked for it to be read.
            try:
                detected = test_reports.detect_format(local_path)
            except (ReportError, OSError):
                return
            if detected is None or os.path.getsize(local_path) > test_reports.MAX_REPORT_BYTES:
                return
        result = test_reports.parse_file(local_path)
    except NotCounted as exc:
        if mode == "explicit":
            meta = dict(row.artifact_metadata or {})
            meta["testReport"] = {"ignored": str(exc)}
            row.artifact_metadata = meta
        return
    except ReportError as exc:
        if mode == "sniff":
            return
        error = str(exc)
    except Exception:  # A parser bug must not cost anybody their upload.
        logger.exception("Reading report artifact %s failed", row.name)
        if mode == "sniff":
            return
        error = "The report could not be read because of an internal error; see the server log."

    meta = dict(row.artifact_metadata or {})
    file_info = test_reports.file_summary(result, error)
    if mode == "sniff":
        file_info["detected"] = True
    meta["testReport"] = file_info
    row.artifact_metadata = meta

    if row.build_id is None:
        return
    try:
        _fold_into_build(row, result, error)
    except Exception:
        logger.exception("Folding report %s into build %s failed", row.name, row.build_id)


def _fold_into_build(row: CiArtifact, result: Optional[Dict[str, Any]], error: Optional[str]) -> None:
    # The id must exist before it is written into the summary.
    db.session.flush()
    # Locked: a build's stages can upload at the same moment (two agents), and
    # two merges that both read the old summary would each drop the other's
    # file. FOR UPDATE serialises them on PostgreSQL; SQLite has one writer
    # anyway and ignores it.
    build = (
        CiBuild.query.filter_by(id=row.build_id)
        .with_for_update()
        .populate_existing()
        .one_or_none()
    )
    if build is None:
        return
    stage = db.session.get(CiBuildStage, row.build_stage_id) if row.build_stage_id else None
    build.test_summary = test_reports.merge(
        build.test_summary,
        artifact_id=row.id,
        name=(row.artifact_metadata or {}).get("sourcePath") or row.name,
        stage_position=stage.position if stage else None,
        stage_name=stage.name if stage else None,
        result=result,
        error=error,
    )
    db.session.add(build)


# ---------------------------------------------------------------------------
# Reads
# ---------------------------------------------------------------------------

def compact(build: CiBuild) -> Optional[Dict[str, Any]]:
    return test_reports.compact(build.test_summary)


def declared_reports(build: CiBuild) -> List[Dict[str, Any]]:
    """The report files this build's pipeline asked to keep.

    Lets an empty Tests section say the right thing: "nothing is declared"
    and "declared, but no file matched" are different fixes.
    """
    declared: List[Dict[str, Any]] = []
    for stage in (build.pipeline_snapshot or {}).get("stages") or []:
        if not isinstance(stage, dict):
            continue
        for spec in stage.get("artifacts") or []:
            if not isinstance(spec, dict):
                continue
            kind = test_reports.normalize_type(spec.get("type"))
            if kind in REPORT_TYPES:
                declared.append({"stage": stage.get("name"), "path": spec.get("path"), "type": kind})
    return declared


def build_detail(build: CiBuild) -> Dict[str, Any]:
    """Everything the drawer's Tests section shows."""
    summary = build.test_summary if isinstance(build.test_summary, dict) else None
    base = {
        "buildId": build.id,
        "number": build.number,
        "status": build.status,
        "declared": declared_reports(build),
        "summary": test_reports.compact(summary),
    }
    if not summary or not summary.get("reportCount"):
        return {
            **base,
            "totals": None,
            "failures": [],
            "failureCount": 0,
            "failuresTruncated": False,
            "coverage": None,
            "reports": [],
            "reportsTruncated": False,
            "errors": [],
        }
    failures = list(summary.get("failures") or [])
    reports = list(summary.get("reports") or [])
    return {
        **base,
        "totals": summary.get("totals"),
        "failures": failures,
        "failureCount": int(summary.get("failureCount") or 0),
        "failuresTruncated": int(summary.get("failureCount") or 0) > len(failures),
        "coverage": summary.get("coverage"),
        "coverageReports": summary.get("coverageReports") or [],
        "reports": reports,
        "reportsTruncated": int(summary.get("reportCount") or 0) - int(summary.get("errorCount") or 0)
        > len(reports),
        "errors": summary.get("errors") or [],
        "errorCount": int(summary.get("errorCount") or 0),
    }


def failure_brief(build: CiBuild, limit: int = 20) -> Optional[Dict[str, Any]]:
    """The tests that failed, short enough for an agent's context.

    The message, not the stack trace: an agent reading a build failure wants
    the names and the assertion, and can open the report for the rest.
    """
    summary = build.test_summary if isinstance(build.test_summary, dict) else None
    numbers = test_reports.compact(summary)
    if numbers is None:
        return None
    failures = (summary or {}).get("failures") or []
    return {
        **numbers,
        "failedTests": [
            {
                "name": case.get("name"),
                "classname": case.get("classname"),
                "suite": case.get("suite"),
                "file": case.get("file"),
                "kind": case.get("kind"),
                "message": case.get("message"),
            }
            for case in failures[: max(1, int(limit))]
        ],
        "failedTestsShown": min(len(failures), max(1, int(limit))),
        "coverageNote": ((summary or {}).get("coverage") or {}).get("note"),
    }


def service_trend(service_id: int, limit: int = DEFAULT_TREND_BUILDS) -> Dict[str, Any]:
    """The last ``limit`` builds that collected reports, oldest first.

    Only the columns the trend draws are loaded — never the stage rows or the
    pipeline snapshot — and the summary's compact form is all that leaves.
    """
    limit = max(2, min(int(limit or DEFAULT_TREND_BUILDS), MAX_TREND_BUILDS))
    rows = (
        db.session.query(
            CiBuild.id,
            CiBuild.number,
            CiBuild.status,
            CiBuild.branch,
            CiBuild.finished_at,
            CiBuild.test_summary,
        )
        .filter(CiBuild.service_id == int(service_id))
        .order_by(CiBuild.id.desc())
        .limit(limit * TREND_SCAN_FACTOR)
        .all()
    )
    points: List[Dict[str, Any]] = []
    for build_id, number, status, branch, finished_at, summary in rows:
        numbers = test_reports.compact(summary)
        if numbers is None:
            continue
        points.append(
            {
                "buildId": build_id,
                "number": number,
                "status": status,
                "branch": branch,
                "finishedAt": finished_at.isoformat() if finished_at else None,
                "total": numbers["total"],
                "passed": numbers["passed"],
                "failed": (numbers["failed"] or 0) + (numbers["errors"] or 0)
                if numbers["total"] is not None
                else None,
                "skipped": numbers["skipped"],
                "linesPct": numbers["linesPct"],
                "branchesPct": numbers["branchesPct"],
            }
        )
        if len(points) >= limit:
            break
    points.reverse()
    return {"serviceId": int(service_id), "points": points, "limit": limit}


__all__ = [
    "COVERAGE_REPORT",
    "TEST_REPORT",
    "build_detail",
    "compact",
    "declared_reports",
    "failure_brief",
    "ingest",
    "service_trend",
]
