"""The merge check report people read: one PDF per pull request, every check in it.

A merge check leaves its evidence in two places. The verdict, the gate and each
tool's count are on the ``CiMergeCheck`` row; the findings themselves are in the
report file each tool's stage keeps as a build artifact — ESLint's JSON,
Hadolint's JSON, PMD's text, and so on. Eleven formats, read by a developer one
download at a time.

This module reads all of them into one shape and draws a single document: what
was decided and why, a table of every check against its limit, then each
check's findings, worst first. It is rendered when asked for and never stored,
so it cannot drift from the check it describes and does not sit among the
build's artifacts.

The counts in the PDF are the ones the gate decided with (the row's metrics),
not ones recomputed from the files. A tool's report can hold more than the gate
counted — Hadolint's style hints, SwiftLint's warnings — and the PDF says so
rather than letting the two numbers disagree silently.
"""

from __future__ import annotations

import json
import posixpath
import re
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from ....models_ci import CiArtifact
from ....models_merge_checks import MERGE_CHECK_TOOLS, CiMergeCheck
from .. import artifacts as artifacts_service
from ..code_scan_report import _BRAND, _FAIL, _INK, _MUTED, _PASS, _RULE, _SEV, _SOFT, _t, _when
from . import stages
from .linters import LINTERS
from .policy import TOOL_CAP_FIELD, _tool_label

# A report bigger than this is not read into memory to be rendered.
MAX_REPORT_BYTES = 64 * 1024 * 1024
# Findings written out per check, and in the whole document. The tables still
# count every one; the files on the build have the rest.
MAX_FINDINGS_PER_TOOL = 150
MAX_FINDINGS_TOTAL = 600

# Where the Kubernetes runner checks the repository out. Tools that print
# absolute paths are shown relative to it.
_CHECKOUT_PREFIXES = ("/workspace/source/", "./")

# What "a problem" means for the three tools whose rule is not in LINTERS.
_COUNTS = {
    "eslint": "Errors, plus warnings when the gate counts them.",
    "semgrep": "Findings at or above the severity the gate counts.",
    "sonar": "Issues SonarQube reports at or above the severity the gate counts.",
    "dependency_check": "Vulnerable dependencies at or above the severity the gate counts.",
}

VERDICT_LABEL = {
    "allowed": ("MERGE ALLOWED", _PASS),
    "blocked": ("MERGE BLOCKED", _FAIL),
    "unknown": ("NOT CHECKED", (201, 122, 0)),
}


class MergeCheckReportError(Exception):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


def available(check: CiMergeCheck) -> bool:
    """A report exists once the check has an answer — any answer."""
    return check.state in ("passed", "failed", "error")


def pdf_filename(check: CiMergeCheck) -> str:
    slug = check.service.slug if check.service else "service"
    pr = re.sub(r"[^A-Za-z0-9_-]+", "-", str(check.pull_request_id or check.id))
    return f"{slug}-pr-{pr}-merge-check.pdf"


# ---------------------------------------------------------------------------
# Reading each tool's report
# ---------------------------------------------------------------------------

def _finding(level: str, rule: Any, file: Any, line: Any, message: Any, severity: str = "") -> Dict[str, Any]:
    try:
        number = int(line) if line not in (None, "") else None
    except (TypeError, ValueError):
        number = None
    level = level if level in ("error", "warning", "info") else "info"
    return {
        "level": level,
        # What the tool itself calls it (CRITICAL, style, ...), shown beside the
        # colour, which only has three values.
        "severity": (severity or level).upper(),
        "rule": str(rule or "").strip(),
        "file": str(file or "").strip(),
        "line": number,
        "message": " ".join(str(message or "").split()),
    }


def _level(raw: Any) -> str:
    value = str(raw or "").strip().lower()
    if value in ("error", "critical", "high", "blocker", "fatal", "2"):
        return "error"
    if value in ("warning", "warn", "medium", "major", "1"):
        return "warning"
    return "info"


def _eslint(data: Any) -> List[Dict[str, Any]]:
    out = []
    for entry in data if isinstance(data, list) else []:
        for msg in (entry or {}).get("messages") or []:
            severity = "error" if msg.get("severity") == 2 else "warning"
            out.append(_finding(severity, msg.get("ruleId") or "parse", entry.get("filePath"), msg.get("line"), msg.get("message")))
    return out


def _semgrep(data: Any) -> List[Dict[str, Any]]:
    out = []
    for result in (data or {}).get("results") or [] if isinstance(data, dict) else []:
        extra = result.get("extra") or {}
        severity = str(extra.get("severity") or "INFO")
        rule = str(result.get("check_id") or "").split(".")[-1]
        out.append(_finding(_level(severity), rule, result.get("path"), (result.get("start") or {}).get("line"), extra.get("message"), severity))
    return out


def _dependency_check(data: Any) -> List[Dict[str, Any]]:
    out = []
    for dep in (data or {}).get("dependencies") or [] if isinstance(data, dict) else []:
        for vuln in dep.get("vulnerabilities") or []:
            severity = str(vuln.get("severity") or "LOW")
            description = str(vuln.get("description") or "").strip()
            out.append(_finding(_level(severity), vuln.get("name"), dep.get("fileName"), None, description[:600], severity))
    return out


def _ruff(data: Any) -> List[Dict[str, Any]]:
    # Ruff has no severities: every finding counts, so every one is an error.
    return [
        _finding("error", row.get("code"), row.get("filename"), (row.get("location") or {}).get("row"), row.get("message"), "finding")
        for row in (data if isinstance(data, list) else [])
        if isinstance(row, dict)
    ]


def _swiftlint(data: Any) -> List[Dict[str, Any]]:
    return [
        _finding(_level(row.get("severity")), row.get("rule_id"), row.get("file"), row.get("line"), row.get("reason"), str(row.get("severity") or ""))
        for row in (data if isinstance(data, list) else [])
        if isinstance(row, dict)
    ]


def _hadolint(data: Any) -> List[Dict[str, Any]]:
    return [
        _finding(_level(row.get("level")), row.get("code"), row.get("file"), row.get("line"), row.get("message"), str(row.get("level") or ""))
        for row in (data if isinstance(data, list) else [])
        if isinstance(row, dict)
    ]


def _shellcheck(data: Any) -> List[Dict[str, Any]]:
    rows = data.get("comments") if isinstance(data, dict) else data
    return [
        _finding(_level(row.get("level")), f"SC{row.get('code')}" if row.get("code") else "", row.get("file"), row.get("line"), row.get("message"), str(row.get("level") or ""))
        for row in (rows if isinstance(rows, list) else [])
        if isinstance(row, dict)
    ]


# path:line:<tab>Rule:<tab>message
_PMD_RE = re.compile(r"^(?P<file>.+?):(?P<line>\d+):\s*(?P<rule>[A-Za-z][\w.-]*):\s*(?P<msg>.*)$")
# Rule - [entity] at /path/File.kt:12:5 - Signature=...
_DETEKT_RE = re.compile(r"^(?P<rule>\S+) - (?:\[(?P<entity>[^\]]*)\] )?at (?P<file>.+?):(?P<line>\d+):\d+")


def _pmd(text: str) -> List[Dict[str, Any]]:
    out = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        match = _PMD_RE.match(line)
        if match:
            out.append(_finding("error", match["rule"], match["file"], match["line"], match["msg"], "violation"))
        else:
            out.append(_finding("error", "", "", None, line, "violation"))
    return out


def _detekt(text: str) -> List[Dict[str, Any]]:
    out = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        match = _DETEKT_RE.match(line)
        if match:
            message = f"{match['rule']} in {match['entity']}" if match["entity"] else match["rule"]
            out.append(_finding("error", match["rule"], match["file"], match["line"], message, "issue"))
        else:
            out.append(_finding("error", "", "", None, line, "issue"))
    return out


def _dart(text: str) -> List[Dict[str, Any]]:
    # --format=machine: SEVERITY|TYPE|CODE|FILE|LINE|COLUMN|LENGTH|MESSAGE.
    # The file also carries pub's own output (2>&1), which has no pipes.
    out = []
    for raw in text.splitlines():
        parts = raw.strip().split("|", 7)
        if len(parts) < 8 or parts[0].upper() not in ("ERROR", "WARNING", "INFO"):
            continue
        out.append(_finding(_level(parts[0]), parts[2].lower(), parts[3], parts[4], parts[7], parts[0]))
    return out


_JSON_PARSERS = {
    "eslint": _eslint,
    "semgrep": _semgrep,
    "dependency_check": _dependency_check,
    "ruff": _ruff,
    "swiftlint": _swiftlint,
    "hadolint": _hadolint,
    "shellcheck": _shellcheck,
}
_TEXT_PARSERS = {"pmd": _pmd, "detekt": _detekt, "dart_analyze": _dart}


def _artifact_for(tool: str, rows: List[CiArtifact]) -> Optional[CiArtifact]:
    """The newest stored file this tool's stage declared, matched by name or path."""
    for spec in stages.tool_artifacts(tool):
        path = str(spec.get("path") or "")
        wanted = {str(spec.get("name") or ""), posixpath.basename(path)} - {""}
        for row in rows:
            declared = str((row.artifact_metadata or {}).get("declaredPath") or "")
            if row.name in wanted or (declared and (declared == path or declared.endswith("/" + path))):
                return row
    return None


def _read(artifact: CiArtifact) -> bytes:
    if artifact.storage_backend != "local" or not artifact.storage_ref:
        raise MergeCheckReportError("its report file is not stored on this server")
    if artifact.size_bytes and artifact.size_bytes > MAX_REPORT_BYTES:
        raise MergeCheckReportError("its report file is too large to include")
    try:
        with artifacts_service.get_store("local").open(artifact) as handle:
            return handle.read(MAX_REPORT_BYTES + 1)
    except (OSError, ValueError) as exc:
        raise MergeCheckReportError(f"its report file could not be read ({exc})")


def _parse(tool: str, raw: bytes) -> List[Dict[str, Any]]:
    text = raw.decode("utf-8", "replace")
    if tool in _TEXT_PARSERS:
        return _TEXT_PARSERS[tool](text)
    try:
        data = json.loads(text) if text.strip() else []
    except ValueError as exc:
        raise MergeCheckReportError(f"its report file is not valid JSON ({exc})")
    return _JSON_PARSERS[tool](data)


def _relative(findings: List[Dict[str, Any]]) -> None:
    """Show paths from the repository root, not the runner's disk."""
    for item in findings:
        path = item["file"].replace("\\", "/")
        for prefix in _CHECKOUT_PREFIXES:
            if path.startswith(prefix):
                path = path[len(prefix):]
        item["file"] = path
    absolute = [item["file"] for item in findings if item["file"].startswith("/")]
    if len(set(absolute)) > 1:
        root = posixpath.commonpath(absolute)
        if root not in ("", "/"):
            for item in findings:
                if item["file"].startswith(root + "/"):
                    item["file"] = item["file"][len(root) + 1:]


_ORDER = {"error": 0, "warning": 1, "info": 2}


def collect(check: CiMergeCheck) -> List[Dict[str, Any]]:
    """Every check on this pull request, with its count, limit and findings."""
    metrics = dict(check.metrics or {})
    gate = dict(check.gate or {})
    tools = [tool for tool in MERGE_CHECK_TOOLS if tool in metrics]
    tools += [tool for tool in metrics if tool not in tools]
    rows: List[CiArtifact] = []
    if check.build_id:
        rows = (
            CiArtifact.query.filter_by(build_id=check.build_id)
            .filter(CiArtifact.artifact_type.in_(("test-report", "scan-report")))
            .order_by(CiArtifact.id.desc())
            .all()
        )

    out = []
    for tool in tools:
        metric = metrics.get(tool) if isinstance(metrics.get(tool), dict) else {}
        status = str(metric.get("status") or "missing").lower()
        entry: Dict[str, Any] = {
            "tool": tool,
            "label": _tool_label(tool),
            "status": status,
            "counted": int(metric.get("problems") or 0) if status == "ok" else None,
            "limit": gate.get(TOOL_CAP_FIELD[tool]) if tool in TOOL_CAP_FIELD else None,
            "counts": str((LINTERS.get(tool) or {}).get("counts") or _COUNTS.get(tool, "")),
            "message": str(metric.get("message") or ""),
            "metric": metric,
            "findings": [],
            "note": "",
        }
        if tool == "sonar":
            entry["note"] = "SonarQube keeps its findings on its own server; open the project there for each one."
        elif status == "ok" and (tool in _JSON_PARSERS or tool in _TEXT_PARSERS):
            # Only a check that reported: a file left by one that crashed
            # part-way is not what the gate judged, and listing it would
            # contradict the "did not report" beside it.
            artifact = _artifact_for(tool, rows)
            if artifact is None:
                if status == "ok":
                    entry["note"] = "The stage kept no report file, so only its count is shown."
            else:
                try:
                    findings = _parse(tool, _read(artifact))
                except MergeCheckReportError as exc:
                    entry["note"] = f"The findings are not listed: {exc}."
                else:
                    _relative(findings)
                    findings.sort(key=lambda f: (_ORDER[f["level"]], f["file"], f["line"] or 0))
                    entry["findings"] = findings
        out.append(entry)
    return out


# ---------------------------------------------------------------------------
# The PDF
# ---------------------------------------------------------------------------

def _limit_text(limit: Any) -> str:
    return "no limit" if limit is None else str(limit)


def _result_text(entry: Dict[str, Any]) -> str:
    status = entry["status"]
    if status == "ok":
        limit = entry["limit"]
        return "Over the limit" if limit is not None and entry["counted"] > limit else "Within the limit"
    return {"skipped": "Skipped", "error": "Failed to run"}.get(status, "Did not report")


def render_pdf(check: CiMergeCheck) -> bytes:
    try:
        from fpdf import FPDF
        from fpdf.enums import WrapMode, XPos, YPos
    except ImportError:  # pragma: no cover - an image built before fpdf2 was added
        raise MergeCheckReportError(
            "PDF support is not installed on this KubeSight server (the fpdf2 package). "
            "Rebuild the backend image from the current requirements.txt.",
            501,
        )
    if not available(check):
        raise MergeCheckReportError("This check has not finished yet, so there is no report.", 409)

    entries = collect(check)
    gate = dict(check.gate or {})
    service = check.service
    service_name = service.name if service else "Service"
    build = check.build
    verdict = check.verdict if check.verdict in VERDICT_LABEL else "unknown"
    verdict_text, tone = VERDICT_LABEL[verdict]
    title = check.title or f"Pull request #{check.pull_request_id or '?'}"
    generated = datetime.now(timezone.utc)
    pr_label = f"PR #{check.pull_request_id}" if check.pull_request_id else f"check {check.id}"

    class Report(FPDF):
        def footer(self):
            self.set_y(-12)
            self.set_font("Helvetica", "", 7.5)
            self.set_text_color(*_MUTED)
            self.cell(0, 5, _t(f"KubeSight  |  {service_name}  |  {pr_label}  |  generated {_when(generated)}"), align="L")
            self.set_x(self.l_margin)
            self.cell(0, 5, f"Page {self.page_no()} of {{nb}}", align="R")

    pdf = Report(orientation="P", unit="mm", format="A4")
    pdf.set_title(_t(f"Merge check report - {service_name} {pr_label}"))
    pdf.set_author("KubeSight")
    pdf.set_margins(16, 16, 16)
    pdf.set_auto_page_break(True, margin=18)
    pdf.alias_nb_pages()
    pdf.add_page()
    width = pdf.w - pdf.l_margin - pdf.r_margin

    def heading(text: str, size: float = 12.5) -> None:
        if pdf.get_y() > pdf.h - 40:
            pdf.add_page()
        pdf.set_font("Helvetica", "B", size)
        pdf.set_text_color(*_INK)
        pdf.cell(0, 7, _t(text), new_x=XPos.LMARGIN, new_y=YPos.NEXT)
        pdf.set_draw_color(*_RULE)
        pdf.set_line_width(0.3)
        pdf.line(pdf.l_margin, pdf.get_y(), pdf.l_margin + width, pdf.get_y())
        pdf.ln(2.5)

    def paragraph(text: str, size: float = 9.5, color=_INK, style: str = "") -> None:
        pdf.set_font("Helvetica", style, size)
        pdf.set_text_color(*color)
        pdf.multi_cell(0, size * 0.5, _t(text), align="L", new_x=XPos.LMARGIN, new_y=YPos.NEXT)

    # -- Masthead -----------------------------------------------------------
    pdf.set_fill_color(*_BRAND)
    pdf.rect(pdf.l_margin, pdf.get_y(), 10, 1.4, style="F")
    pdf.ln(3.5)
    pdf.set_font("Helvetica", "B", 8)
    pdf.set_text_color(*_BRAND)
    pdf.cell(0, 4, _t(f"KUBESIGHT  |  MERGE CHECK REPORT  |  {service_name.upper()}"), new_x=XPos.LMARGIN, new_y=YPos.NEXT)
    pdf.ln(1)
    pdf.set_font("Helvetica", "B", 18)
    pdf.set_text_color(*_INK)
    pdf.multi_cell(0, 8.5, _t(title), new_x=XPos.LMARGIN, new_y=YPos.NEXT)
    meta = [
        ("Pull request", f"#{check.pull_request_id}" if check.pull_request_id else "-"),
        ("Author", check.author or "-"),
        ("Branches", f"{check.source_branch or '?'} -> {check.destination_branch or '?'}"),
        ("Commit", (check.commit_sha or "-")[:12]),
        ("Build", f"#{build.number}" if build else "no longer kept"),
        ("Checked", _when(check.evaluated_at or check.created_at)),
    ]
    pdf.ln(1.5)
    col = width / 3
    for index, (label, value) in enumerate(meta):
        x = pdf.l_margin + (index % 3) * col
        if index % 3 == 0 and index:
            pdf.ln(10.5)
        y = pdf.get_y()
        pdf.set_xy(x, y)
        pdf.set_font("Helvetica", "", 7.5)
        pdf.set_text_color(*_MUTED)
        pdf.cell(col, 4, _t(label.upper()))
        pdf.set_xy(x, y + 4)
        pdf.set_font("Courier" if label == "Commit" else "Helvetica", "B", 9.5)
        pdf.set_text_color(*_INK)
        pdf.cell(col - 2, 5, _t(value)[:44])
        pdf.set_xy(pdf.l_margin, y)
    pdf.ln(14)

    # -- Verdict ------------------------------------------------------------
    total = check.total_problems or 0
    cap = gate.get("maxTotalProblems")
    ran = [e for e in entries if e["status"] == "ok"]
    over = any(e["limit"] is not None and e["counted"] > e["limit"] for e in ran) or (
        cap is not None and total > cap
    )
    if verdict == "unknown":
        headline = "No verdict - the checks could not finish"
    elif not ran:
        headline = "No check reported a result"
    else:
        headline = (
            f"{total} problem{'' if total == 1 else 's'} across {len(ran)} check{'' if len(ran) == 1 else 's'}"
            + (f" - the gate allows {cap}" if cap is not None else "")
        )
    top = pdf.get_y()
    pdf.set_fill_color(*_SOFT)
    pdf.rect(pdf.l_margin, top, width, 26, style="F")
    pdf.set_fill_color(*tone)
    pdf.rect(pdf.l_margin, top, 2.2, 26, style="F")
    pdf.set_xy(pdf.l_margin + 7, top + 4)
    pdf.set_font("Helvetica", "B", 8)
    pdf.set_text_color(*tone)
    pdf.cell(0, 4, verdict_text)
    pdf.set_xy(pdf.l_margin + 7, top + 9)
    pdf.set_font("Helvetica", "B", 14)
    pdf.set_text_color(*_INK)
    pdf.cell(0, 8, _t(headline))
    pdf.set_xy(pdf.l_margin + 7, top + 17.5)
    pdf.set_font("Helvetica", "", 8.5)
    pdf.set_text_color(*_MUTED)
    told = {
        "delivered": "Bitbucket was given this verdict as the pull request's build status.",
        "failed": "Bitbucket has not been told yet - delivering the verdict failed.",
        "pending": "The verdict is on its way to Bitbucket.",
    }.get(check.delivery_state, "")
    pdf.cell(0, 4, _t(told))
    pdf.set_y(top + 31)

    heading("Why", 11)
    reasons = list(check.reasons or [])
    if reasons:
        for reason in reasons:
            pdf.set_font("Helvetica", "B", 9.5)
            pdf.set_text_color(*tone)
            pdf.cell(4, 4.8, "-")
            pdf.set_font("Helvetica", "", 9.5)
            pdf.set_text_color(*_INK)
            pdf.multi_cell(width - 4, 4.8, _t(reason), align="L", new_x=XPos.LMARGIN, new_y=YPos.NEXT)
    else:
        paragraph("Every check is within its limit.")
    pdf.ln(1.5)
    if verdict == "blocked" and not over:
        paragraph(
            "The merge is blocked because some checks did not finish, not because of problems found in the "
            "code. Push again to re-run them, or ask whoever runs KubeSight to look at the build if they keep "
            "failing.",
            9, _MUTED,
        )
    elif verdict == "blocked":
        paragraph(
            "To get this pull request through: fix the findings listed below for each check that is over "
            "its limit, then push the fix to the same branch. The new commit is checked again on its own.",
            9, _MUTED,
        )
    elif verdict == "unknown":
        paragraph(
            "Nothing was judged, so this is not a list of problems in the code. Push again or ask whoever "
            "runs KubeSight to look at the build; a check that cannot run is a setup problem, not yours.",
            9, _MUTED,
        )
    pdf.ln(4)

    # -- The checks table -----------------------------------------------------
    if entries:
        heading("Checks", 11)
        widths = [width - 112, 30, 22, 22, 38]
        pdf.set_font("Helvetica", "B", 8)
        pdf.set_text_color(*_MUTED)
        for head, w in zip(["Check", "Result", "Counted", "Limit", "Findings listed"], widths):
            pdf.cell(w, 6, head.upper())
        pdf.ln(6)
        for entry in entries:
            if pdf.get_y() > pdf.h - 26:
                pdf.add_page()
            pdf.set_draw_color(*_RULE)
            pdf.line(pdf.l_margin, pdf.get_y(), pdf.l_margin + width, pdf.get_y())
            result = _result_text(entry)
            over = result == "Over the limit"
            cells = [
                (entry["label"], "B", _INK),
                (result, "B", _FAIL if over else _PASS if entry["status"] == "ok" else _MUTED),
                ("-" if entry["counted"] is None else str(entry["counted"]), "", _INK),
                (_limit_text(entry["limit"]), "", _INK),
                (str(len(entry["findings"])) if entry["findings"] else "-", "", _INK),
            ]
            for (value, style, color), w in zip(cells, widths):
                pdf.set_font("Helvetica", style, 8.5)
                pdf.set_text_color(*color)
                pdf.cell(w, 6.5, _t(value))
            pdf.ln(6.5)
        pdf.ln(5)

    # -- Each check's findings -------------------------------------------------
    written = 0
    for entry in entries:
        if pdf.get_y() > pdf.h - 60:
            pdf.add_page()
        counted = entry["counted"]
        limit = entry["limit"]
        suffix = (
            f" - {counted} counted, " + ("no limit" if limit is None else f"limit {limit}")
            if counted is not None
            else f" - {_result_text(entry).lower()}"
        )
        heading(entry["label"] + suffix, 12)
        if entry["counts"]:
            paragraph(f"What counts: {entry['counts']}", 8.5, _MUTED)
        if entry["status"] != "ok" and entry["message"]:
            paragraph(entry["message"], 9)
        if entry["note"]:
            paragraph(entry["note"], 8.5, _MUTED, "I")
        if entry["tool"] == "sonar" and entry["status"] == "ok":
            detail = [f"{key}: {entry['metric'][key]}" for key in ("bugs", "vulnerabilities", "codeSmells") if key in entry["metric"]]
            if detail:
                paragraph("  |  ".join(detail), 9)
        findings = entry["findings"]
        if entry["status"] == "ok" and not findings and not entry["note"] and entry["tool"] != "sonar":
            paragraph("No findings.", 9, _MUTED)
        if findings and counted is not None and len(findings) > counted:
            paragraph(
                f"The report lists {len(findings)} findings; {counted} of them "
                f"{'is' if counted == 1 else 'are'} the kind the gate counts.",
                8.5, _MUTED,
            )
        room = max(0, min(MAX_FINDINGS_PER_TOOL, MAX_FINDINGS_TOTAL - written))
        if len(findings) > room:
            paragraph(
                f"The first {room} are written out below, worst first; the rest are in the build's report file.",
                8.5, _MUTED,
            )
        pdf.ln(1.5)
        for item in findings[:room]:
            written += 1
            if pdf.get_y() > pdf.h - 30:
                pdf.add_page()
            pdf.set_font("Helvetica", "B", 7)
            pdf.set_text_color(255, 255, 255)
            pdf.set_fill_color(*_SEV[item["level"]])
            chip = _t(item["severity"])[:12]
            pdf.cell(pdf.get_string_width(chip) + 4, 4.4, chip, fill=True, align="C")
            pdf.set_x(pdf.get_x() + 2)
            if item["rule"]:
                pdf.set_font("Helvetica", "B", 8.5)
                pdf.set_text_color(*_INK)
                rule = _t(item["rule"])[:60]
                pdf.cell(pdf.get_string_width(rule) + 3, 4.4, rule)
            if item["file"]:
                pdf.set_font("Courier", "B", 8)
                pdf.set_text_color(*_BRAND)
                where = item["file"] + (f":{item['line']}" if item["line"] else "")
                remaining = pdf.l_margin + width - pdf.get_x()
                limit = max(10, int(remaining / 1.75))
                where = _t(where)
                pdf.cell(remaining, 4.4, where if len(where) <= limit else "..." + where[-(limit - 3):])
            pdf.ln(5)
            if item["message"]:
                pdf.set_font("Helvetica", "", 8.5)
                pdf.set_text_color(*_INK)
                pdf.multi_cell(0, 4.1, _t(item["message"])[:900], new_x=XPos.LMARGIN, new_y=YPos.NEXT, wrapmode=WrapMode.CHAR)
            pdf.ln(1)
            pdf.set_draw_color(*_RULE)
            pdf.line(pdf.l_margin, pdf.get_y(), pdf.l_margin + width, pdf.get_y())
            pdf.ln(2)
        pdf.ln(4)

    if not entries:
        heading("Checks", 11)
        paragraph(check.error or "No check reported anything for this pull request.", 9.5, _MUTED)

    return bytes(pdf.output())
