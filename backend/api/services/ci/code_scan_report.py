"""The code scan report people read: a PDF built from a stage's Semgrep results.

The build keeps Semgrep's JSON as a ``scan-report`` artifact (see
``code_scan``). JSON is what a tool reads; the people who have to act on a
failed gate — a developer on another team, a lead, an auditor — need a
document that says what failed, why, where, and what to change. This module
turns the first into the second, and emails it to whoever the person pressing
"Send" chose. Nothing here sends on its own.

The verdict printed on the PDF is the one the build reached (the gate writes it
into the report), not one recomputed with today's settings, so the PDF and the
build log always tell the same story.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

from ...db import db
from ...models import User
from ...models_ci import CiArtifact, CiBuild, CiBuildStage, CiPipelineStage
from . import artifacts as artifacts_service
from . import code_scan
from . import code_scan_gate as gate

logger = logging.getLogger(__name__)

# A report bigger than this is not read into memory to be rendered.
MAX_REPORT_BYTES = 64 * 1024 * 1024
# Findings written out in full. The summary tables still count every one.
MAX_DETAILED_FINDINGS = 300
MAX_NOTE_CHARS = 2000

COUNT_FROM_LABEL = {
    "info": "every severity",
    "warning": "WARNING and ERROR",
    "error": "ERROR only",
}


class CodeScanReportError(Exception):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


# ---------------------------------------------------------------------------
# Finding the report
# ---------------------------------------------------------------------------

def stage_gate(build: CiBuild, stage: CiBuildStage) -> Optional[Dict[str, Any]]:
    """The gate this build's stage ran under, from the build's own snapshot."""
    stages = (build.pipeline_snapshot or {}).get("stages") or []
    if not 0 <= stage.position < len(stages):
        return None
    candidate = (stages[stage.position] or {}).get("codeScan")
    return candidate if code_scan.armed(candidate) else None


def report_artifact(build: CiBuild, stage: CiBuildStage) -> Optional[CiArtifact]:
    return (
        CiArtifact.query.filter_by(
            build_id=build.id,
            build_stage_id=stage.id,
            artifact_type="scan-report",
            name=code_scan.report_artifact_name(stage.position),
        )
        .order_by(CiArtifact.id.desc())
        .first()
    )


def load_report(artifact: CiArtifact) -> Dict[str, Any]:
    if artifact.storage_backend != "local" or not artifact.storage_ref:
        raise CodeScanReportError("The scan results are not stored on this server.", 404)
    if artifact.size_bytes and artifact.size_bytes > MAX_REPORT_BYTES:
        raise CodeScanReportError("The scan results are too large to turn into a PDF.", 413)
    try:
        with artifacts_service.get_store("local").open(artifact) as handle:
            data = json.load(handle)
    except (OSError, ValueError) as exc:
        raise CodeScanReportError(f"The scan results could not be read ({exc}).", 404)
    if not isinstance(data, dict):
        raise CodeScanReportError("The scan results are not a Semgrep report.", 422)
    return data


def saved_recipients(build: CiBuild, stage: CiBuildStage) -> List[str]:
    """Who the stage's gate says to offer — the LIVE pipeline first.

    Recipients are an address book, not build history: somebody added to the
    list today should be offered on yesterday's failed build too. The snapshot
    is the fallback for a stage that has since been deleted.
    """
    live = db.session.get(CiPipelineStage, stage.pipeline_stage_id) if stage.pipeline_stage_id else None
    if live is not None and isinstance(live.code_scan, dict):
        return list(live.code_scan.get("recipients") or [])
    snapshot = stage_gate(build, stage) or {}
    return list(snapshot.get("recipients") or [])


def recipient_suggestions() -> List[Dict[str, str]]:
    """KubeSight's own users with an address, for the dialog's picker."""
    rows = (
        User.query.filter(User.is_active.is_(True), User.email != "")
        .order_by(User.username.asc())
        .limit(500)
        .all()
    )
    return [
        {"username": row.username, "name": row.full_name or row.username, "email": row.email}
        for row in rows
        if row.email and "@" in row.email
    ]


def _decided(report: Dict[str, Any], gate_config: Dict[str, Any]) -> Tuple[Dict[str, Any], int, str, str]:
    """Summary, allowed, count-from and verdict — as the BUILD decided them."""
    stamped = (report.get("kubesight") or {}).get("gate") or {}
    count_from = str(stamped.get("countFrom") or gate_config.get("countFrom") or "info")
    allowed = int(stamped.get("maxBlocking", gate_config.get("maxBlocking") or 0) or 0)
    summary = gate.summarize(report, count_from)
    verdict = str(stamped.get("verdict") or gate.verdict(summary, allowed))
    return summary, allowed, count_from, verdict


def overview(build: CiBuild, stage: CiBuildStage) -> Dict[str, Any]:
    """What the build drawer shows above a gated stage's log."""
    gate_config = stage_gate(build, stage)
    if gate_config is None:
        raise CodeScanReportError("This stage has no code scan quality gate.", 404)
    artifact = report_artifact(build, stage)
    payload: Dict[str, Any] = {
        "maxBlocking": int(gate_config.get("maxBlocking") or 0),
        "countFrom": gate_config.get("countFrom") or "info",
        "reportAvailable": False,
        "verdict": None,
        "recipients": saved_recipients(build, stage),
    }
    if artifact is None:
        payload["reason"] = (
            "The build is still running."
            if stage.status in ("pending", "running") or build.status in ("queued", "running")
            else "This stage saved no scan results, so there is nothing to report."
        )
        return payload
    try:
        report = load_report(artifact)
    except CodeScanReportError as exc:
        payload["reason"] = str(exc)
        return payload
    summary, allowed, count_from, verdict = _decided(report, gate_config)
    payload.update(
        {
            "reportAvailable": True,
            "artifactId": artifact.id,
            "verdict": verdict,
            "maxBlocking": allowed,
            "countFrom": count_from,
            "blocking": summary["blocking"],
            "blockingBySeverity": summary["blockingBySeverity"],
            "total": summary["total"],
            "filesAffected": len(summary["files"]),
        }
    )
    return payload


# ---------------------------------------------------------------------------
# The PDF
# ---------------------------------------------------------------------------

_ASCII = {
    "‘": "'", "’": "'", "‚": "'", "‛": "'",
    "“": '"', "”": '"', "„": '"',
    "–": "-", "—": "-", "−": "-",
    "…": "...", "•": "*", " ": " ",
    "→": "->", "←": "<-", "⇒": "=>",
    "≤": "<=", "≥": ">=", "≠": "!=",
    "\t": "    ",
}


def _t(value: Any) -> str:
    """Text the PDF's built-in fonts can draw (Latin-1)."""
    text = str(value if value is not None else "")
    for src, dst in _ASCII.items():
        text = text.replace(src, dst)
    text = "".join(ch for ch in text if ch == "\n" or ord(ch) >= 32)
    return text.encode("latin-1", "replace").decode("latin-1")


# Cardinal: brand red for identity, Ember for "this failed", never swapped.
_INK = (26, 26, 26)
_MUTED = (107, 107, 107)
_RULE = (226, 224, 220)
_SOFT = (246, 245, 242)
_BRAND = (196, 30, 58)
_FAIL = (224, 59, 46)
_PASS = (31, 138, 76)
_SEV = {"error": (224, 59, 46), "warning": (201, 122, 0), "info": (60, 110, 180)}


def _short_rule(check_id: str) -> str:
    return str(check_id or "?").split(".")[-1]


def _when(value: Optional[datetime]) -> str:
    if not value:
        return "-"
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).strftime("%d %b %Y, %H:%M UTC")


def _references(result: Dict[str, Any]) -> List[str]:
    metadata = (result.get("extra") or {}).get("metadata") or {}
    refs: List[str] = []
    for key in ("cwe", "owasp"):
        value = metadata.get(key)
        for item in value if isinstance(value, list) else [value] if value else []:
            refs.append(str(item))
    for key in ("references", "source"):
        value = metadata.get(key)
        for item in value if isinstance(value, list) else [value] if value else []:
            if str(item).startswith("http"):
                refs.append(str(item))
    seen: List[str] = []
    for item in refs:
        if item not in seen:
            seen.append(item)
    return seen[:6]


def render_pdf(build: CiBuild, stage: CiBuildStage, report: Dict[str, Any]) -> bytes:
    try:
        from fpdf import FPDF
        from fpdf.enums import WrapMode, XPos, YPos
    except ImportError:  # pragma: no cover - an image built before fpdf2 was added
        raise CodeScanReportError(
            "PDF support is not installed on this KubeSight server (the fpdf2 package). "
            "Rebuild the backend image from the current requirements.txt.",
            501,
        )

    gate_config = stage_gate(build, stage) or {}
    summary, allowed, count_from, verdict = _decided(report, gate_config)
    service = build.service
    service_name = service.name if service else "Service"
    failed = verdict == "failed"
    generated = datetime.now(timezone.utc)
    requested_by = build.requested_by.username if build.requested_by else None

    class Report(FPDF):
        def footer(self):
            self.set_y(-12)
            self.set_font("Helvetica", "", 7.5)
            self.set_text_color(*_MUTED)
            self.cell(
                0, 5,
                _t(f"KubeSight  |  {service_name}  |  build #{build.number}  |  generated {_when(generated)}"),
                align="L",
            )
            self.set_x(self.l_margin)
            self.cell(0, 5, f"Page {self.page_no()} of {{nb}}", align="R")

    pdf = Report(orientation="P", unit="mm", format="A4")
    pdf.set_title(_t(f"Code scan report - {service_name} build #{build.number}"))
    pdf.set_author("KubeSight")
    pdf.set_margins(16, 16, 16)
    pdf.set_auto_page_break(True, margin=18)
    pdf.alias_nb_pages()
    pdf.add_page()
    width = pdf.w - pdf.l_margin - pdf.r_margin

    def line_break(height: float = 4) -> None:
        pdf.ln(height)

    def heading(text: str, size: float = 12.5) -> None:
        if pdf.get_y() > pdf.h - 40:
            pdf.add_page()
        pdf.set_font("Helvetica", "B", size)
        pdf.set_text_color(*_INK)
        pdf.cell(0, 7, _t(text), new_x=XPos.LMARGIN, new_y=YPos.NEXT)
        pdf.set_draw_color(*_RULE)
        pdf.set_line_width(0.3)
        pdf.line(pdf.l_margin, pdf.get_y(), pdf.l_margin + width, pdf.get_y())
        line_break(2.5)

    def paragraph(text: str, size: float = 9.5, color=_INK, style: str = "") -> None:
        pdf.set_font("Helvetica", style, size)
        pdf.set_text_color(*color)
        pdf.multi_cell(0, size * 0.5, _t(text), align="L", new_x=XPos.LMARGIN, new_y=YPos.NEXT)

    # -- Masthead -----------------------------------------------------------
    pdf.set_fill_color(*_BRAND)
    pdf.rect(pdf.l_margin, pdf.get_y(), 10, 1.4, style="F")
    line_break(3.5)
    pdf.set_font("Helvetica", "B", 8)
    pdf.set_text_color(*_BRAND)
    pdf.cell(0, 4, "KUBESIGHT  |  SOURCE CODE SCAN REPORT", new_x=XPos.LMARGIN, new_y=YPos.NEXT)
    line_break(1)
    pdf.set_font("Helvetica", "B", 20)
    pdf.set_text_color(*_INK)
    pdf.multi_cell(0, 9, _t(f"{service_name} - build #{build.number}"), new_x=XPos.LMARGIN, new_y=YPos.NEXT)
    meta = [
        ("Stage", stage.name),
        ("Branch / tag", build.branch or "-"),
        ("Commit", (build.commit_sha or "-")[:12]),
        ("Scanned", _when(stage.finished_at or build.finished_at or build.created_at)),
    ]
    if requested_by:
        meta.append(("Started by", requested_by))
    meta.append(("Scanner", f"Semgrep {summary['semgrepVersion']}".strip()))
    line_break(1.5)
    col = width / 3
    for index, (label, value) in enumerate(meta):
        x = pdf.l_margin + (index % 3) * col
        if index % 3 == 0 and index:
            line_break(10.5)
        y = pdf.get_y()
        pdf.set_xy(x, y)
        pdf.set_font("Helvetica", "", 7.5)
        pdf.set_text_color(*_MUTED)
        pdf.cell(col, 4, _t(label.upper()))
        pdf.set_xy(x, y + 4)
        pdf.set_font("Courier" if label == "Commit" else "Helvetica", "B", 9.5)
        pdf.set_text_color(*_INK)
        pdf.cell(col - 2, 5, _t(value)[:48])
        pdf.set_xy(pdf.l_margin, y)
    line_break(14)

    # -- Verdict ------------------------------------------------------------
    tone = _FAIL if failed else _PASS
    top = pdf.get_y()
    pdf.set_fill_color(*_SOFT)
    pdf.rect(pdf.l_margin, top, width, 30, style="F")
    pdf.set_fill_color(*tone)
    pdf.rect(pdf.l_margin, top, 2.2, 30, style="F")
    pdf.set_xy(pdf.l_margin + 7, top + 4)
    pdf.set_font("Helvetica", "B", 8)
    pdf.set_text_color(*tone)
    pdf.cell(0, 4, "QUALITY GATE FAILED" if failed else "QUALITY GATE PASSED")
    pdf.set_xy(pdf.l_margin + 7, top + 9)
    pdf.set_font("Helvetica", "B", 15)
    pdf.set_text_color(*_INK)
    blocking = summary["blocking"]
    pdf.cell(
        0, 8,
        _t(f"{blocking} blocking finding{'' if blocking == 1 else 's'} - the gate allows {allowed}"),
    )
    pdf.set_xy(pdf.l_margin + 7, top + 18)
    pdf.set_font("Helvetica", "", 9)
    pdf.set_text_color(*_MUTED)
    pdf.multi_cell(
        width - 12, 4.4,
        _t(
            f"Counting {COUNT_FROM_LABEL.get(count_from, 'every severity')}. "
            + (
                "The build stopped at this stage, and nothing after it ran."
                if failed
                else "The build carried on past this stage."
            )
        ),
    )
    pdf.set_y(top + 34)

    heading("What this means", 11)
    if failed:
        paragraph(
            "Semgrep - a static analysis tool - read the source code of this build and found "
            f"{blocking} place{'' if blocking == 1 else 's'} matching rules for known security or "
            f"quality problems. This service allows at most {allowed}, so the build was stopped "
            "before anything was built or deployed from this code."
        )
        line_break(1.5)
        paragraph(
            "To get the build through: fix the findings listed below, starting with ERROR, then run "
            "the build again. A finding that is a false positive can be silenced on its own line with "
            "a nosemgrep comment - \"// nosemgrep\" in TypeScript, JavaScript or Java, \"# nosemgrep\" "
            "in Python or shell - and the gate stops counting it."
        )
    else:
        paragraph(
            f"Semgrep found {blocking} blocking finding{'' if blocking == 1 else 's'}, within the "
            f"{allowed} this service allows, so the build carried on. The findings are still worth "
            "fixing - they are listed below."
        )
    line_break(4)

    # -- Numbers ------------------------------------------------------------
    sev = summary["blockingBySeverity"]
    tiles = [
        ("Blocking", str(blocking), tone),
        ("ERROR", str(sev["error"]), _SEV["error"]),
        ("WARNING", str(sev["warning"]), _SEV["warning"]),
        ("INFO", str(sev["info"]), _SEV["info"]),
        ("Files affected", str(len(summary["files"])), _INK),
    ]
    gap = 3
    tile_w = (width - gap * (len(tiles) - 1)) / len(tiles)
    top = pdf.get_y()
    for index, (label, value, color) in enumerate(tiles):
        x = pdf.l_margin + index * (tile_w + gap)
        pdf.set_draw_color(*_RULE)
        pdf.set_line_width(0.3)
        pdf.rect(x, top, tile_w, 17)
        pdf.set_xy(x + 3, top + 2.5)
        pdf.set_font("Helvetica", "", 7.5)
        pdf.set_text_color(*_MUTED)
        pdf.cell(tile_w - 6, 4, _t(label.upper()))
        pdf.set_xy(x + 3, top + 7)
        pdf.set_font("Helvetica", "B", 15)
        pdf.set_text_color(*color)
        pdf.cell(tile_w - 6, 8, value)
    pdf.set_y(top + 20)
    extras = []
    if summary["filesScanned"] is not None:
        extras.append(f"{summary['filesScanned']} files scanned")
    if summary["notBlocking"]:
        extras.append(f"{summary['notBlocking']} marked non-blocking (not counted)")
    outside = summary["total"] - summary["notBlocking"] - blocking
    if outside > 0:
        extras.append(f"{outside} below the counted severity (not counted)")
    if summary["scanErrors"]:
        extras.append(f"{summary['scanErrors']} file(s) Semgrep could not fully analyse")
    if extras:
        paragraph("  |  ".join(extras), 8.5, _MUTED)
    line_break(4)

    # -- Tables -------------------------------------------------------------
    def table(title: str, headers: List[str], rows: List[List[str]], widths: List[float], mono_col: int = -1):
        if not rows:
            return
        # A short table is kept on one page rather than leaving a row stranded.
        needed = 16 + 6.5 * len(rows)
        if needed < pdf.h - 40 and pdf.get_y() + needed > pdf.h - 20:
            pdf.add_page()
        heading(title, 11)
        pdf.set_font("Helvetica", "B", 8)
        pdf.set_text_color(*_MUTED)
        for head, w in zip(headers, widths):
            pdf.cell(w, 6, _t(head.upper()))
        pdf.ln(6)
        for row in rows:
            if pdf.get_y() > pdf.h - 26:
                pdf.add_page()
            pdf.set_draw_color(*_RULE)
            pdf.line(pdf.l_margin, pdf.get_y(), pdf.l_margin + width, pdf.get_y())
            for index, (value, w) in enumerate(zip(row, widths)):
                if index == 1 and value in ("ERROR", "WARNING", "INFO"):
                    pdf.set_font("Helvetica", "B", 8)
                    pdf.set_text_color(*_SEV[value.lower()])
                else:
                    pdf.set_font("Courier" if index == mono_col else "Helvetica", "", 8.5)
                    pdf.set_text_color(*_INK)
                text = _t(value)
                limit = int(w / (1.9 if index == mono_col else 1.75))
                if len(text) > limit:
                    text = "..." + text[-(limit - 3):] if index == mono_col else text[: limit - 3] + "..."
                pdf.cell(w, 6.5, text)
            pdf.ln(6.5)
        line_break(5)

    rules = sorted(summary["rules"].items(), key=lambda item: (-item[1]["count"], item[0]))
    table(
        "Problems by rule",
        ["Rule", "Severity", "Findings"],
        [[_short_rule(rule), data["severity"].upper(), str(data["count"])] for rule, data in rules[:15]],
        [width - 50, 30, 20],
    )
    files = sorted(summary["files"].items(), key=lambda item: (-item[1], item[0]))
    table(
        "Files with the most findings",
        ["File", "Findings"],
        [[path, str(count)] for path, count in files[:12]],
        [width - 20, 20],
        mono_col=0,
    )

    # -- Every finding ------------------------------------------------------
    rows = gate.counted_results(report, count_from)
    if rows:
        if pdf.get_y() > pdf.h - 90:
            pdf.add_page()
        heading(f"Findings ({len(rows)})", 13)
        if len(rows) > MAX_DETAILED_FINDINGS:
            paragraph(
                f"The first {MAX_DETAILED_FINDINGS} are written out below, worst first. The tables "
                "above count all of them; the full list is in the scan-report artifact on the build.",
                8.5, _MUTED,
            )
            line_break(2)
    for number, result in enumerate(rows[:MAX_DETAILED_FINDINGS], start=1):
        extra = result.get("extra") or {}
        level = gate.bucket(extra.get("severity"))
        if pdf.get_y() > pdf.h - 55:
            pdf.add_page()
        pdf.set_font("Helvetica", "B", 7.5)
        pdf.set_text_color(255, 255, 255)
        pdf.set_fill_color(*_SEV[level])
        chip = level.upper()
        chip_w = pdf.get_string_width(chip) + 5
        pdf.cell(chip_w, 5, chip, fill=True, align="C")
        pdf.set_x(pdf.get_x() + 2.5)
        pdf.set_font("Helvetica", "B", 10)
        pdf.set_text_color(*_INK)
        pdf.cell(0, 5, _t(f"{number}. {_short_rule(result.get('check_id'))}")[:90], new_x=XPos.LMARGIN, new_y=YPos.NEXT)
        line_break(1)
        start_line = (result.get("start") or {}).get("line") or "?"
        pdf.set_font("Courier", "B", 8.5)
        pdf.set_text_color(*_BRAND)
        pdf.multi_cell(
            0, 4.2, _t(f"{result.get('path') or '?'}:{start_line}"),
            new_x=XPos.LMARGIN, new_y=YPos.NEXT, wrapmode=WrapMode.CHAR,
        )
        line_break(0.8)
        paragraph(str(extra.get("message") or "").strip() or "No description.", 9)

        snippet = extra.get("kubesightSnippet") or {}
        if snippet.get("withheld"):
            line_break(1)
            paragraph("Code excerpt withheld: this finding is about a secret, and the excerpt would quote it.", 8, _MUTED, "I")
        elif snippet.get("lines"):
            line_break(1.5)
            first = int(snippet.get("firstLine") or 1)
            match_start = int(snippet.get("matchStart") or first)
            match_end = int(snippet.get("matchEnd") or match_start)
            lines = snippet["lines"]
            box_top = pdf.get_y()
            height = 3.9 * len(lines) + 3
            if box_top + height > pdf.h - 20:
                pdf.add_page()
                box_top = pdf.get_y()
            pdf.set_fill_color(*_SOFT)
            pdf.rect(pdf.l_margin, box_top, width, height, style="F")
            pdf.set_y(box_top + 1.5)
            for offset, text in enumerate(lines):
                number_on = first + offset
                hit = match_start <= number_on <= match_end
                if hit:
                    pdf.set_fill_color(252, 228, 225)
                    pdf.rect(pdf.l_margin, pdf.get_y(), width, 3.9, style="F")
                    pdf.set_fill_color(*_SEV[level])
                    pdf.rect(pdf.l_margin, pdf.get_y(), 0.9, 3.9, style="F")
                pdf.set_x(pdf.l_margin + 2)
                pdf.set_font("Courier", "", 7.5)
                pdf.set_text_color(*_MUTED)
                pdf.cell(10, 3.9, str(number_on).rjust(5))
                pdf.set_text_color(*_INK)
                pdf.set_font("Courier", "B" if hit else "", 7.5)
                visible = _t(text.rstrip())
                limit = int((width - 14) / 1.6)
                pdf.cell(width - 14, 3.9, visible if len(visible) <= limit else visible[: limit - 3] + "...")
                pdf.ln(3.9)
            pdf.set_y(box_top + height)
        fix = extra.get("fix")
        if fix:
            line_break(1.5)
            pdf.set_font("Helvetica", "B", 8)
            pdf.set_text_color(*_PASS)
            pdf.cell(0, 4, "SUGGESTED CHANGE", new_x=XPos.LMARGIN, new_y=YPos.NEXT)
            pdf.set_font("Courier", "", 8)
            pdf.set_text_color(*_INK)
            pdf.multi_cell(0, 3.9, _t(str(fix))[:1200], new_x=XPos.LMARGIN, new_y=YPos.NEXT, wrapmode=WrapMode.CHAR)
        refs = _references(result)
        if refs:
            line_break(1)
            pdf.set_font("Helvetica", "", 7.5)
            pdf.set_text_color(*_MUTED)
            pdf.multi_cell(0, 3.8, _t("References: " + "  |  ".join(refs)), new_x=XPos.LMARGIN, new_y=YPos.NEXT, wrapmode=WrapMode.CHAR)
        pdf.set_font("Helvetica", "", 7)
        pdf.set_text_color(*_MUTED)
        pdf.multi_cell(0, 3.6, _t(f"Rule: {result.get('check_id') or '?'}"), new_x=XPos.LMARGIN, new_y=YPos.NEXT, wrapmode=WrapMode.CHAR)
        line_break(3)
        pdf.set_draw_color(*_RULE)
        pdf.line(pdf.l_margin, pdf.get_y(), pdf.l_margin + width, pdf.get_y())
        line_break(4)

    if not rows:
        heading("Findings", 11)
        paragraph("Nothing the gate counts. Semgrep found no blocking problems in this code.", 9.5, _MUTED)

    return bytes(pdf.output())


def pdf_support_problem() -> Optional[str]:
    """Why a PDF cannot be made on this server, or None."""
    try:
        import fpdf  # noqa: F401
    except ImportError:
        return (
            "PDF support is not installed on this KubeSight server (the fpdf2 package). "
            "Rebuild the backend image from the current requirements.txt."
        )
    return None


def pdf_filename(build: CiBuild) -> str:
    slug = build.service.slug if build.service else "service"
    return f"{slug}-build-{build.number}-code-scan.pdf"


def render_for_stage(build: CiBuild, stage: CiBuildStage) -> bytes:
    if stage_gate(build, stage) is None:
        raise CodeScanReportError("This stage has no code scan quality gate.", 404)
    artifact = report_artifact(build, stage)
    if artifact is None:
        raise CodeScanReportError("This stage saved no scan results, so there is no report.", 404)
    return render_pdf(build, stage, load_report(artifact))


# ---------------------------------------------------------------------------
# Sending it
# ---------------------------------------------------------------------------

def _build_link(build: CiBuild) -> str:
    from .merge_checks.delivery import public_base_url

    base = (public_base_url() or "").rstrip("/")
    return f"{base}/#/service-catalog/{build.service_id}/builds" if base else ""


def send_report(
    build: CiBuild, stage: CiBuildStage, recipients: Any, note: Any, sender: Optional[User]
) -> Dict[str, Any]:
    from ...email_delivery import EmailDeliveryError, send_email, smtp_is_configured

    try:
        addresses = code_scan.clean_recipients(recipients)
    except code_scan.CodeScanConfigError as exc:
        raise CodeScanReportError(str(exc))
    if not addresses:
        raise CodeScanReportError("Choose at least one person to send the report to.")
    if not smtp_is_configured():
        raise CodeScanReportError(
            "Email is not set up on this KubeSight. Configure SMTP in Settings, or download the PDF and send it yourself.",
            409,
        )
    note_text = str(note or "").strip()[:MAX_NOTE_CHARS]

    artifact = report_artifact(build, stage)
    if artifact is None:
        raise CodeScanReportError("This stage saved no scan results, so there is no report.", 404)
    report = load_report(artifact)
    summary, allowed, _count_from, verdict = _decided(report, stage_gate(build, stage) or {})
    pdf_bytes = render_pdf(build, stage, report)

    service_name = build.service.name if build.service else "Service"
    word = "FAILED" if verdict == "failed" else "passed"
    subject = (
        f"[KubeSight] Code scan {word}: {service_name} build #{build.number} "
        f"({summary['blocking']} blocking, {allowed} allowed)"
    )
    who = (sender.full_name or sender.username) if sender else "KubeSight"
    link = _build_link(build)
    lines = [
        f"{who} sent you the source code scan report for {service_name} build #{build.number}.",
        "",
        f"Quality gate: {word.upper()} - {summary['blocking']} blocking findings, {allowed} allowed.",
        f"Branch: {build.branch or '-'}   Commit: {(build.commit_sha or '-')[:12]}",
    ]
    if note_text:
        lines += ["", "Message:", note_text]
    lines += ["", "The full report is attached as a PDF."]
    if link:
        lines += [f"Build: {link}"]
    try:
        send_email(
            ", ".join(addresses),
            subject,
            "\n".join(lines),
            attachments=[(pdf_filename(build), pdf_bytes, "application/pdf")],
        )
    except EmailDeliveryError as exc:
        raise CodeScanReportError(str(exc), 502)
    return {"sentTo": addresses, "verdict": verdict, "blocking": summary["blocking"], "filename": pdf_filename(build)}
