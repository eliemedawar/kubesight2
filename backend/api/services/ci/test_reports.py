"""Test results and coverage, read out of the report files a build kept.

A stage declares its reports in "Files to keep" like any other output (type
``test-report`` or ``coverage-report``). This module turns one such file into
numbers — passed/failed/skipped, the failed cases, line and branch coverage —
and folds those numbers into one summary per build. It is pure: no database,
no Flask. :mod:`test_summary` is the part that stores what this returns.

Formats are recognised by what the file CONTAINS, never by its name. Every
tool names its output differently (``TEST-*.xml``, ``junit.xml``,
``report.xml``, ``coverage.xml`` that is really Cobertura), and the one thing a
name can tell us reliably is nothing.

Everything here reads files a build produced, which means files anybody who
can push a commit controls. So:

* every XML document goes through ``defusedxml`` — an entity declaration (the
  XXE and billion-laughs shapes) is refused outright, and an external DTD is
  never fetched;
* XML is streamed with ``iterparse`` and each test case is dropped once it has
  been counted, so a 50 MB surefire file costs a few kilobytes of memory, not
  a tree of millions of nodes;
* every string kept is truncated, and the failed-case list is capped. A build
  with ten thousand failures does not get a ten-thousand-row summary.

A file that cannot be read raises :class:`ReportError` with a sentence a
person can act on. The caller records that sentence; nothing here is allowed
to fail an upload or a build.
"""

from __future__ import annotations

import io
import json
import math
import os
import re
from typing import IO, Any, Callable, Dict, Iterable, List, Optional, Tuple

from defusedxml import ElementTree as SafeET
from defusedxml.common import DefusedXmlException

TEST_REPORT = "test-report"
COVERAGE_REPORT = "coverage-report"
REPORT_TYPES = (TEST_REPORT, COVERAGE_REPORT)

# Words people type in the "Kind" box that mean one of the two. The stage
# editor offered "coverage" before ``coverage-report`` existed, so pipelines
# saved with it must keep working; the rest are what people guess.
TYPE_ALIASES = {
    "coverage": COVERAGE_REPORT,
    "cobertura": COVERAGE_REPORT,
    "jacoco": COVERAGE_REPORT,
    "lcov": COVERAGE_REPORT,
    "junit": TEST_REPORT,
    "junit-xml": TEST_REPORT,
    "test-results": TEST_REPORT,
    "tests": TEST_REPORT,
    "test-reports": TEST_REPORT,
}

def _env_mb(name: str, default: int) -> int:
    try:
        return max(1, int(os.getenv(name, "").strip() or default))
    except ValueError:
        return default


# Bigger than any real report: a surefire run of a large monolith with its
# captured output stays well under this. Past it the file is not read at all.
MAX_REPORT_BYTES = _env_mb("CI_TEST_REPORT_MAX_MB", 64) * 1024 * 1024
# Istanbul's JSON is read whole (json has no streaming parser in the stdlib),
# so it gets a lower ceiling than the streamed formats.
MAX_JSON_BYTES = min(MAX_REPORT_BYTES, 32 * 1024 * 1024)

# Enough to fix a build from; the full list belongs in the tool's own report.
MAX_FAILURES = 100
MAX_MESSAGE_CHARS = 500
MAX_DETAIL_CHARS = 2000
MAX_NAME_CHARS = 300
# Per-file entries kept on a build. A Maven build writes one surefire file per
# test CLASS, so hundreds are normal; the totals always count every file.
MAX_REPORT_ENTRIES = 300
MAX_COVERAGE_ENTRIES = 200
MAX_ERROR_ENTRIES = 50

SNIFF_BYTES = 64 * 1024

SUMMARY_VERSION = 1


class ReportError(ValueError):
    """A report that could not be read, said so a person can fix it."""


class NotCounted(ReportError):
    """A file we recognise and deliberately do not count.

    TestNG writes ``testng-results.xml`` into ``surefire-reports`` beside the
    JUnit files Surefire writes for the same run, so the common
    ``surefire-reports/*.xml`` pattern collects both. Counting the second
    would double every test; calling it unreadable would be a false alarm.
    """


# ---------------------------------------------------------------------------
# Types and detection
# ---------------------------------------------------------------------------

def normalize_type(value: Optional[str]) -> str:
    raw = str(value or "").strip().lower()
    return TYPE_ALIASES.get(raw, raw)


def _local(tag: Any) -> str:
    """``{namespace}Tag`` -> ``Tag``. TRX is namespaced; most others are not."""
    text = tag if isinstance(tag, str) else ""
    return text.rsplit("}", 1)[-1]


def _strip_bom(data: bytes) -> bytes:
    return data[3:] if data.startswith(b"\xef\xbb\xbf") else data


def _root_element(path: str) -> Tuple[str, Dict[str, str]]:
    """The first element of an XML file, read without reading the rest."""
    try:
        with open(path, "rb") as handle:
            for _event, element in SafeET.iterparse(handle, events=("start",)):
                return _local(element.tag), dict(element.attrib)
    except DefusedXmlException as exc:
        raise ReportError(_refused(exc)) from exc
    except SafeET.ParseError as exc:
        raise ReportError(f"Not well-formed XML ({exc}).") from exc
    raise ReportError("The file has no XML element in it.")


def _refused(exc: Exception) -> str:
    # Said plainly: an entity in a test report is either an attack or a tool
    # nobody should be using, and in both cases nothing in it was read.
    return (
        "The XML declares entities or a DTD with entities, which is refused for "
        f"safety; nothing in it was read ({type(exc).__name__})."
    )


def detect_format(path: str) -> Optional[str]:
    """Which report this is, from its content. None when it is none we know.

    Raises :class:`ReportError` only for a file that looks like XML or JSON but
    cannot be read as such — a file of some other kind is simply None.
    """
    with open(path, "rb") as handle:
        head = _strip_bom(handle.read(SNIFF_BYTES))
    text = head.lstrip()
    if not text:
        return None
    if text[:1] in (b"{", b"["):
        return "istanbul-json"
    if text[:1] == b"<":
        root, attrs = _root_element(path)
        return _xml_format(root, attrs, head)
    # lcov is line-oriented text; any of its record keys at the start of a
    # line is enough, because nothing else writes SF:/DA: lines.
    if re.search(rb"(?m)^(TN|SF):", head) and re.search(rb"(?m)^(DA|LF|LH|end_of_record)", head):
        return "lcov"
    return None


def _xml_format(root: str, attrs: Dict[str, str], head: bytes) -> Optional[str]:
    if root in ("testsuites", "testsuite"):
        return "junit"
    if root == "testng-results":
        return "testng"
    if root == "TestRun":
        return "trx"
    if root in ("assemblies", "assembly"):
        return "xunit"
    if root in ("test-run", "test-results"):
        return "nunit"
    if root == "report":
        return "jacoco"
    if root == "coverage":
        # Cobertura and Clover both call their root <coverage>. Cobertura puts
        # its rates on the root; Clover nests a <project> with <metrics>.
        if "line-rate" in attrs or "lines-valid" in attrs or "branch-rate" in attrs:
            return "cobertura"
        if "clover" in attrs or b"<project" in head:
            return "clover"
        return "cobertura"
    return None


KIND_OF_FORMAT = {
    "junit": "tests",
    "trx": "tests",
    "xunit": "tests",
    "nunit": "tests",
    "cobertura": "coverage",
    "jacoco": "coverage",
    "clover": "coverage",
    "lcov": "coverage",
    "istanbul-json": "coverage",
}

FORMAT_LABEL = {
    "junit": "JUnit XML",
    "trx": "Visual Studio TRX",
    "xunit": "xUnit.net v2 XML",
    "nunit": "NUnit XML",
    "cobertura": "Cobertura",
    "jacoco": "JaCoCo",
    "clover": "Clover",
    "lcov": "lcov",
    "istanbul-json": "Istanbul JSON",
    "istanbul-summary": "Istanbul summary",
}


def parse_file(path: str) -> Dict[str, Any]:
    """Read one report file. Raises :class:`ReportError` when it cannot."""
    try:
        size = os.path.getsize(path)
    except OSError as exc:
        raise ReportError("The report file could not be opened.") from exc
    if size == 0:
        raise ReportError("The report file is empty.")
    if size > MAX_REPORT_BYTES:
        raise ReportError(
            f"The report is {size / 1048576:.1f} MB, over the "
            f"{MAX_REPORT_BYTES // 1048576} MB this server reads (CI_TEST_REPORT_MAX_MB)."
        )
    fmt = detect_format(path)
    if fmt is None:
        raise ReportError(
            "Not a test or coverage report this server can read. Supported: JUnit XML "
            "(Maven, Gradle, jest-junit, pytest, .NET JUnit loggers), TRX, xUnit.net, "
            "NUnit, Cobertura, JaCoCo, Clover, lcov and Istanbul JSON."
        )
    if fmt == "testng":
        raise NotCounted(
            "TestNG's own results file; not counted, because Surefire's TEST-*.xml "
            "files beside it report the same tests."
        )
    parser = _PARSERS[fmt]
    try:
        with open(path, "rb") as handle:
            result = parser(handle, size)
    except ReportError:
        raise
    except DefusedXmlException as exc:
        raise ReportError(_refused(exc)) from exc
    except SafeET.ParseError as exc:
        raise ReportError(f"The {FORMAT_LABEL.get(fmt, fmt)} file is not well-formed XML ({exc}).") from exc
    except (ValueError, TypeError, KeyError, AttributeError, RecursionError) as exc:
        raise ReportError(f"The {FORMAT_LABEL.get(fmt, fmt)} file could not be read ({exc}).") from exc
    result.setdefault("format", fmt)
    result["kind"] = KIND_OF_FORMAT.get(fmt, result.get("kind", "tests"))
    return result


# ---------------------------------------------------------------------------
# Small readers shared by the formats
# ---------------------------------------------------------------------------

def _clip(value: Any, limit: int) -> Optional[str]:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _number(value: Any) -> Optional[float]:
    if value is None:
        return None
    try:
        number = float(str(value).strip().replace(",", ""))
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _count(value: Any) -> Optional[int]:
    number = _number(value)
    if number is None or number < 0:
        return None
    return int(number)


def _seconds(value: Any) -> float:
    number = _number(value)
    return number if number is not None and number >= 0 else 0.0


_TIMESPAN_RE = re.compile(r"^(?:(\d+)\.)?(\d+):(\d+):(\d+(?:\.\d+)?)$")


def _timespan(value: Any) -> float:
    """TRX durations are .NET TimeSpans: ``[d.]hh:mm:ss[.fffffff]``."""
    match = _TIMESPAN_RE.match(str(value or "").strip())
    if not match:
        return _seconds(value)
    days, hours, minutes, seconds = match.groups()
    return int(days or 0) * 86400 + int(hours) * 3600 + int(minutes) * 60 + float(seconds)


def _first_line(text: Optional[str]) -> Optional[str]:
    for line in (text or "").splitlines():
        if line.strip():
            return line.strip()
    return None


def _pct(covered: Optional[int], total: Optional[int]) -> Optional[float]:
    if covered is None or total is None or total <= 0:
        return None
    return round(min(100.0, 100.0 * covered / total), 2)


def _metric(covered: Optional[int], total: Optional[int], rate: Optional[float] = None) -> Optional[Dict[str, Any]]:
    """One coverage measure. Counts when we have them, a bare rate when not.

    A total of zero is "nothing to measure", not 0% — a module with no
    branches must not drag the branch figure to zero.
    """
    if covered is not None and total is not None:
        if total <= 0:
            return None
        covered = max(0, min(covered, total))
        return {"covered": covered, "total": total, "pct": _pct(covered, total)}
    if rate is not None:
        pct = rate * 100.0 if rate <= 1.0 else rate
        return {"covered": None, "total": None, "pct": round(max(0.0, min(100.0, pct)), 2)}
    return None


class _Tally:
    """Counts for one test report, plus the failed cases worth showing."""

    def __init__(self) -> None:
        self.tests = 0
        self.failed = 0
        self.errors = 0
        self.skipped = 0
        self.flaky = 0
        self.case_seconds = 0.0
        self.failures: List[Dict[str, Any]] = []
        self.suites = 0

    def case(self, outcome: str, seconds: float = 0.0) -> None:
        self.tests += 1
        self.case_seconds += seconds
        if outcome == "failed":
            self.failed += 1
        elif outcome == "error":
            self.errors += 1
        elif outcome == "skipped":
            self.skipped += 1

    def failure(self, **fields: Any) -> None:
        if len(self.failures) >= MAX_FAILURES:
            return
        self.failures.append(
            {
                "suite": _clip(fields.get("suite"), MAX_NAME_CHARS),
                "name": _clip(fields.get("name"), MAX_NAME_CHARS) or "(unnamed test)",
                "classname": _clip(fields.get("classname"), MAX_NAME_CHARS),
                "file": _clip(fields.get("file"), MAX_NAME_CHARS),
                "kind": fields.get("kind") or "failed",
                "type": _clip(fields.get("type"), 200),
                "message": _clip(fields.get("message"), MAX_MESSAGE_CHARS),
                "details": _clip(fields.get("details"), MAX_DETAIL_CHARS),
                "durationSeconds": round(float(fields.get("seconds") or 0.0), 3),
            }
        )

    def result(self, duration: Optional[float] = None) -> Dict[str, Any]:
        failed_total = self.failed + self.errors
        return {
            "tests": self.tests,
            "passed": max(0, self.tests - failed_total - self.skipped),
            "failed": self.failed,
            "errors": self.errors,
            "skipped": self.skipped,
            "flaky": self.flaky,
            "durationSeconds": round(duration if duration is not None else self.case_seconds, 3),
            "suites": self.suites,
            "failures": self.failures,
            # Past the cap the list stops but the counts do not; the UI says
            # "showing 100 of 412" from these two.
            "failuresTruncated": failed_total > len(self.failures),
        }


def _iter_xml(handle: IO[bytes]) -> Iterable[Tuple[str, Any]]:
    """iterparse with every safety on. Entities are refused, DTDs never fetched."""
    return SafeET.iterparse(
        handle, events=("start", "end"), forbid_dtd=False, forbid_entities=True, forbid_external=True
    )


# ---------------------------------------------------------------------------
# JUnit XML
#
# There is no JUnit XML standard, only the shape Ant wrote in 2001 and two
# decades of tools approximating it. What varies, and how this reads it:
#
# * root: <testsuites> (Gradle merged, jest-junit, pytest >= 5, .NET loggers)
#   or a bare <testsuite> (Surefire, Failsafe, Gradle per class, old pytest);
# * suites nest (jest-junit with describe blocks, Ant aggregates) — only test
#   CASES are counted, so a parent suite never double-counts its children;
# * the suite's own tests/failures/errors attributes are wrong often enough
#   (rerun plugins, skipped-in-setup, hand-written aggregators) that the cases
#   win whenever there are any. A suite with no cases at all — some tools
#   write summary-only files — falls back to its attributes;
# * outcomes are child elements: <failure>, <error>, <skipped>; Surefire's
#   reruns add <flakyFailure>/<flakyError> (passed on retry: counted as passed
#   and flaky) and <rerunFailure>/<rerunError> (failed every time). A few tools
#   (CTest, some Go converters) use a status attribute instead;
# * <system-out>/<system-err> are captured output, often most of the file;
#   they are discarded as soon as they are parsed.
# ---------------------------------------------------------------------------

_FAILED_STATUS = {"fail", "failed", "failure"}
_ERROR_STATUS = {"error", "errored"}
_SKIPPED_STATUS = {"skip", "skipped", "notrun", "not_run", "disabled", "ignored", "pending"}


def _parse_junit(handle: IO[bytes], _size: int) -> Dict[str, Any]:
    tally = _Tally()
    elements: List[Any] = []
    # One frame per open <testsuite>: its name, file, whether it held cases or
    # suites, and its attributes for the summary-only fallback.
    suites: List[Dict[str, Any]] = []
    root_attrs: Dict[str, str] = {}
    root_tag = ""
    top_level_seconds = 0.0
    saw_top_level_time = False

    for event, element in _iter_xml(handle):
        tag = _local(element.tag)
        if event == "start":
            if not elements:
                root_tag = tag
                root_attrs = dict(element.attrib)
            elements.append(element)
            if tag == "testsuite":
                if suites:
                    suites[-1]["childSuites"] = True
                suites.append(
                    {
                        "name": element.get("name"),
                        "file": element.get("file") or element.get("filepath"),
                        "cases": 0,
                        "childSuites": False,
                        "attrs": dict(element.attrib),
                        "depth": len(suites),
                    }
                )
            continue

        # end
        elements.pop()
        parent = elements[-1] if elements else None
        if tag == "testcase":
            frame = suites[-1] if suites else None
            if frame is not None:
                frame["cases"] += 1
            _junit_case(element, frame, tally)
            if parent is not None:
                parent.remove(element)  # counted: let it go
        elif tag == "testsuite":
            frame = suites.pop()
            tally.suites += 1
            if frame["cases"] == 0 and not frame["childSuites"]:
                _junit_suite_attributes(frame["attrs"], tally)
            if frame["depth"] == 0:
                seconds = _number(frame["attrs"].get("time"))
                if seconds is not None:
                    saw_top_level_time = True
                    top_level_seconds += max(0.0, seconds)
            if parent is not None:
                parent.remove(element)
        elif tag in ("system-out", "system-err", "properties") and parent is not None:
            # Captured output is most of a surefire file. Never needed.
            if _local(parent.tag) != "testcase":
                parent.remove(element)

    if root_tag == "testsuites" and tally.tests == 0 and tally.suites == 0:
        # A summary-only aggregate: <testsuites tests="12" failures="1"/>.
        _junit_suite_attributes(root_attrs, tally)

    duration: Optional[float] = None
    root_time = _number(root_attrs.get("time")) if root_tag == "testsuites" else None
    if root_time is not None and root_time >= 0:
        duration = root_time
    elif saw_top_level_time:
        duration = top_level_seconds
    return tally.result(duration)


def _junit_case(element: Any, frame: Optional[Dict[str, Any]], tally: _Tally) -> None:
    outcome = "passed"
    reason = None
    flaky = False
    for child in list(element):
        child_tag = _local(child.tag)
        if child_tag in ("failure", "rerunFailure") and outcome not in ("error",):
            outcome, reason = "failed", reason or child
        elif child_tag in ("error", "rerunError"):
            outcome, reason = "error", child
        elif child_tag == "skipped" and outcome == "passed":
            outcome = "skipped"
        elif child_tag in ("flakyFailure", "flakyError"):
            flaky = True
    status = str(element.get("status") or element.get("result") or "").strip().lower()
    if outcome == "passed" and status:
        if status in _FAILED_STATUS:
            outcome = "failed"
        elif status in _ERROR_STATUS:
            outcome = "error"
        elif status in _SKIPPED_STATUS:
            outcome = "skipped"
    seconds = _seconds(element.get("time"))
    tally.case(outcome, seconds)
    if flaky and outcome == "passed":
        tally.flaky += 1
    if outcome in ("failed", "error"):
        details = reason.text if reason is not None else None
        message = (reason.get("message") if reason is not None else None) or _first_line(details)
        tally.failure(
            suite=frame["name"] if frame else None,
            name=element.get("name"),
            classname=element.get("classname") or element.get("class"),
            file=element.get("file") or element.get("filepath") or (frame["file"] if frame else None),
            kind=outcome,
            type=reason.get("type") if reason is not None else None,
            message=message,
            details=details,
            seconds=seconds,
        )


def _junit_suite_attributes(attrs: Dict[str, str], tally: _Tally) -> None:
    tests = _count(attrs.get("tests")) or 0
    failures = _count(attrs.get("failures")) or 0
    errors = _count(attrs.get("errors")) or 0
    skipped = _count(attrs.get("skipped"))
    if skipped is None:
        skipped = (_count(attrs.get("skip")) or 0) + (_count(attrs.get("disabled")) or 0)
    tally.tests += tests
    tally.failed += failures
    tally.errors += errors
    tally.skipped += skipped


# ---------------------------------------------------------------------------
# .NET: TRX, xUnit.net v2, NUnit 2/3
#
# `dotnet test` writes TRX by default and the JUnit loggers only on request,
# so a .NET shop that "has test reports" usually has these.
# ---------------------------------------------------------------------------

_TRX_FAILED = {"failed", "error", "timeout", "aborted"}
_TRX_SKIPPED = {"notexecuted", "inconclusive", "pending", "disconnected", "warning", "notrunnable"}


def _parse_trx(handle: IO[bytes], _size: int) -> Dict[str, Any]:
    tally = _Tally()
    elements: List[Any] = []
    counters: Dict[str, str] = {}
    for event, element in _iter_xml(handle):
        tag = _local(element.tag)
        if event == "start":
            elements.append(element)
            continue
        elements.pop()
        parent = elements[-1] if elements else None
        parent_tag = _local(parent.tag) if parent is not None else ""
        if tag == "UnitTestResult" and parent_tag == "Results":
            # Data-driven rows nest under InnerResults; the parent row already
            # carries their verdict, so only direct children of Results count.
            outcome_raw = str(element.get("outcome") or "").strip().lower()
            seconds = _timespan(element.get("duration"))
            if outcome_raw in _TRX_FAILED:
                outcome = "error" if outcome_raw in ("error", "aborted") else "failed"
            elif outcome_raw in _TRX_SKIPPED:
                outcome = "skipped"
            else:
                outcome = "passed"
            tally.case(outcome, seconds)
            if outcome in ("failed", "error"):
                message = details = None
                for node in element.iter():
                    node_tag = _local(node.tag)
                    if node_tag == "Message" and message is None:
                        message = node.text
                    elif node_tag == "StackTrace" and details is None:
                        details = node.text
                name = element.get("testName") or ""
                tally.failure(
                    suite=None,
                    name=name,
                    classname=name.rsplit(".", 1)[0] if "." in name else None,
                    kind=outcome,
                    message=_first_line(message) or _clip(message, MAX_MESSAGE_CHARS),
                    details="\n".join(part for part in (message, details) if part),
                    seconds=seconds,
                )
            parent.remove(element)
        elif tag == "Counters":
            counters = dict(element.attrib)
        elif tag in ("UnitTest", "Output") and parent is not None and parent_tag != "UnitTestResult":
            parent.remove(element)
    if tally.tests == 0 and counters:
        tally.tests = _count(counters.get("total")) or 0
        tally.failed = _count(counters.get("failed")) or 0
        tally.errors = (_count(counters.get("error")) or 0) + (_count(counters.get("aborted")) or 0)
        tally.skipped = (_count(counters.get("notExecuted")) or 0) + (_count(counters.get("inconclusive")) or 0)
    return tally.result()


def _parse_xunit(handle: IO[bytes], _size: int) -> Dict[str, Any]:
    tally = _Tally()
    elements: List[Any] = []
    for event, element in _iter_xml(handle):
        tag = _local(element.tag)
        if event == "start":
            elements.append(element)
            continue
        elements.pop()
        parent = elements[-1] if elements else None
        if tag == "test":
            result = str(element.get("result") or "").strip().lower()
            outcome = {"fail": "failed", "skip": "skipped", "notrun": "skipped"}.get(result, "passed")
            seconds = _seconds(element.get("time"))
            tally.case(outcome, seconds)
            if outcome == "failed":
                failure = next((c for c in element if _local(c.tag) == "failure"), None)
                message = details = None
                if failure is not None:
                    for node in failure:
                        if _local(node.tag) == "message":
                            message = node.text
                        elif _local(node.tag) == "stack-trace":
                            details = node.text
                tally.failure(
                    suite=None,
                    name=element.get("name"),
                    classname=element.get("type"),
                    kind="failed",
                    type=failure.get("exception-type") if failure is not None else None,
                    message=_first_line(message),
                    details="\n".join(part for part in (message, details) if part),
                    seconds=seconds,
                )
            if parent is not None:
                parent.remove(element)
        elif tag == "collection":
            tally.suites += 1
    return tally.result()


def _parse_nunit(handle: IO[bytes], _size: int) -> Dict[str, Any]:
    tally = _Tally()
    elements: List[Any] = []
    suites: List[Optional[str]] = []
    for event, element in _iter_xml(handle):
        tag = _local(element.tag)
        if event == "start":
            elements.append(element)
            if tag == "test-suite":
                suites.append(element.get("fullname") or element.get("name"))
            continue
        elements.pop()
        parent = elements[-1] if elements else None
        if tag == "test-case":
            result = str(element.get("result") or "").strip().lower()
            label = str(element.get("label") or "").strip().lower()
            executed = str(element.get("executed") or "true").strip().lower() != "false"
            if result in ("failed", "failure") and label != "error":
                outcome = "failed"
            elif result == "error" or (result == "failed" and label == "error"):
                outcome = "error"
            elif result in ("skipped", "ignored", "inconclusive", "notrunnable") or not executed:
                outcome = "skipped"
            else:
                outcome = "passed"
            seconds = _seconds(element.get("duration") or element.get("time"))
            tally.case(outcome, seconds)
            if outcome in ("failed", "error"):
                message = details = None
                for node in element.iter():
                    node_tag = _local(node.tag)
                    if node_tag == "message" and message is None:
                        message = node.text
                    elif node_tag == "stack-trace" and details is None:
                        details = node.text
                tally.failure(
                    suite=suites[-1] if suites else None,
                    name=element.get("name"),
                    classname=element.get("classname"),
                    kind=outcome,
                    message=_first_line(message),
                    details="\n".join(part for part in (message, details) if part),
                    seconds=seconds,
                )
            if parent is not None:
                parent.remove(element)
        elif tag == "test-suite":
            suites.pop()
            tally.suites += 1
    return tally.result()


# ---------------------------------------------------------------------------
# Coverage
# ---------------------------------------------------------------------------

_CONDITION_RE = re.compile(r"\((\d+)\s*/\s*(\d+)\)")


def _parse_cobertura(handle: IO[bytes], _size: int) -> Dict[str, Any]:
    """Cobertura: counts on the root when the tool wrote them, else the lines.

    coverage.py, Istanbul and coverlet all write ``lines-covered`` /
    ``lines-valid``; the original Cobertura and a few converters write only
    ``line-rate``. With neither counts nor rates the <line> elements are
    counted — under ``class/lines`` only, because every line appears a second
    time under its method and counting both doubles the figure.
    """
    elements: List[Any] = []
    root_attrs: Dict[str, str] = {}
    line_covered = line_total = branch_covered = branch_total = 0
    for event, element in _iter_xml(handle):
        tag = _local(element.tag)
        if event == "start":
            if not elements:
                root_attrs = dict(element.attrib)
                if root_attrs.get("lines-valid") is not None:
                    break  # the root says it all; skip the rest of the file
            elements.append(element)
            continue
        elements.pop()
        parent = elements[-1] if elements else None
        if tag == "line" and len(elements) >= 2 and _local(elements[-2].tag) == "class":
            line_total += 1
            if (_count(element.get("hits")) or 0) > 0:
                line_covered += 1
            if str(element.get("branch") or "").lower() == "true":
                match = _CONDITION_RE.search(element.get("condition-coverage") or "")
                if match:
                    branch_covered += int(match.group(1))
                    branch_total += int(match.group(2))
        if tag in ("line", "method", "class", "package") and parent is not None:
            parent.remove(element)

    if root_attrs.get("lines-valid") is not None:
        lines = _metric(_count(root_attrs.get("lines-covered")), _count(root_attrs.get("lines-valid")),
                        _number(root_attrs.get("line-rate")))
        branches = _metric(_count(root_attrs.get("branches-covered")), _count(root_attrs.get("branches-valid")),
                           _number(root_attrs.get("branch-rate")))
    elif line_total:
        lines = _metric(line_covered, line_total)
        branches = _metric(branch_covered, branch_total) if branch_total else _metric(
            None, None, _number(root_attrs.get("branch-rate"))
        )
    else:
        lines = _metric(None, None, _number(root_attrs.get("line-rate")))
        branches = _metric(None, None, _number(root_attrs.get("branch-rate")))
    if lines is None and branches is None:
        raise ReportError("The Cobertura report has no line or branch figures in it.")
    return {"lines": lines, "branches": branches}


def _parse_jacoco(handle: IO[bytes], _size: int) -> Dict[str, Any]:
    """JaCoCo: the <counter> elements directly under <report> are the totals.

    Every package, class and method has counters of its own; only the
    report-level ones are read, and everything below is discarded as the
    stream passes it. LINE needs classes compiled with debug info; without it
    INSTRUCTION is the closest honest figure, and the result says so.
    """
    depth = 0
    elements: List[Any] = []
    counters: Dict[str, Tuple[int, int]] = {}
    for event, element in _iter_xml(handle):
        tag = _local(element.tag)
        if event == "start":
            elements.append(element)
            depth += 1
            continue
        elements.pop()
        depth -= 1
        parent = elements[-1] if elements else None
        if tag == "counter" and depth == 1:
            missed = _count(element.get("missed")) or 0
            covered = _count(element.get("covered")) or 0
            counters[str(element.get("type") or "").upper()] = (covered, covered + missed)
        if tag in ("package", "group", "sessioninfo") and parent is not None:
            parent.remove(element)
    if not counters:
        raise ReportError("The JaCoCo report has no report-level counters (was it cut short?).")
    result: Dict[str, Any] = {}
    if "LINE" in counters:
        result["lines"] = _metric(*counters["LINE"])
    elif "INSTRUCTION" in counters:
        result["lines"] = _metric(*counters["INSTRUCTION"])
        result["note"] = "No line data (classes built without debug info); instruction coverage shown."
    else:
        result["lines"] = None
    result["branches"] = _metric(*counters["BRANCH"]) if "BRANCH" in counters else None
    return result


def _parse_clover(handle: IO[bytes], _size: int) -> Dict[str, Any]:
    """Clover: the <metrics> of <project>. It counts statements, not lines.

    Statements are reported as the line figure (it is the closest Clover has)
    and the result notes it; conditionals are its branches.
    """
    elements: List[Any] = []
    project_metrics: Optional[Dict[str, str]] = None
    for event, element in _iter_xml(handle):
        tag = _local(element.tag)
        if event == "start":
            elements.append(element)
            continue
        elements.pop()
        parent = elements[-1] if elements else None
        if tag == "metrics" and parent is not None and _local(parent.tag) == "project":
            project_metrics = dict(element.attrib)
        if tag in ("package", "file", "line") and parent is not None:
            parent.remove(element)
    if project_metrics is None:
        raise ReportError("The Clover report has no project-level metrics.")
    return {
        "lines": _metric(_count(project_metrics.get("coveredstatements")), _count(project_metrics.get("statements"))),
        "branches": _metric(_count(project_metrics.get("coveredconditionals")), _count(project_metrics.get("conditionals"))),
        "note": "Clover counts statements; they are shown as lines.",
    }


def _parse_lcov(handle: IO[bytes], _size: int) -> Dict[str, Any]:
    """lcov: LF/LH and BRF/BRH per file record, summed.

    A record without the summary lines (some generators skip them) is counted
    from its DA/BRDA lines instead, so the total never silently drops a file.
    """
    line_total = line_hit = branch_total = branch_hit = 0
    files = 0
    record: Dict[str, int] = {}
    da_total = da_hit = brda_total = brda_hit = 0
    seen_record = False

    def close() -> None:
        nonlocal line_total, line_hit, branch_total, branch_hit, files
        nonlocal da_total, da_hit, brda_total, brda_hit
        files += 1
        line_total += record["LF"] if "LF" in record else da_total
        line_hit += record["LH"] if "LH" in record else da_hit
        branch_total += record["BRF"] if "BRF" in record else brda_total
        branch_hit += record["BRH"] if "BRH" in record else brda_hit
        record.clear()
        da_total = da_hit = brda_total = brda_hit = 0

    for raw in io.TextIOWrapper(handle, encoding="utf-8", errors="replace"):
        line = raw.strip()
        if not line:
            continue
        if line == "end_of_record":
            close()
            seen_record = False
            continue
        key, _, value = line.partition(":")
        if key == "SF":
            seen_record = True
        elif key in ("LF", "LH", "BRF", "BRH"):
            record[key] = _count(value) or 0
        elif key == "DA":
            parts = value.split(",")
            if len(parts) >= 2:
                da_total += 1
                if (_count(parts[1]) or 0) > 0:
                    da_hit += 1
        elif key == "BRDA":
            parts = value.split(",")
            if len(parts) >= 4:
                brda_total += 1
                if parts[3] not in ("-", "") and (_count(parts[3]) or 0) > 0:
                    brda_hit += 1
    if seen_record or record:
        close()  # a file cut off before its end_of_record still counts
    if files == 0:
        raise ReportError("The lcov file has no SF: records in it.")
    return {
        "lines": _metric(line_hit, line_total),
        "branches": _metric(branch_hit, branch_total),
        "files": files,
    }


def _parse_istanbul_json(handle: IO[bytes], size: int) -> Dict[str, Any]:
    """Istanbul's two JSON shapes: the summary, and the per-file raw data.

    ``coverage-summary.json`` (the ``json-summary`` reporter) carries totals.
    ``coverage-final.json`` (the ``json`` reporter) carries hit counts per
    statement and branch; lines are derived the way Istanbul itself derives
    them — a line is covered when any statement starting on it ran.
    """
    if size > MAX_JSON_BYTES:
        raise ReportError(
            f"The Istanbul JSON is over {MAX_JSON_BYTES // 1048576} MB; publish "
            "coverage-summary.json (the json-summary reporter) or lcov instead."
        )
    try:
        data = json.loads(_strip_bom(handle.read()).decode("utf-8", errors="replace"))
    except ValueError as exc:
        raise ReportError(f"The file starts like JSON but is not valid JSON ({exc}).") from exc
    if not isinstance(data, dict):
        raise ReportError("Not an Istanbul coverage report (expected a JSON object).")
    total = data.get("total")
    if isinstance(total, dict) and isinstance(total.get("lines"), dict):
        def measure(key: str) -> Optional[Dict[str, Any]]:
            block = total.get(key)
            if not isinstance(block, dict):
                return None
            return _metric(_count(block.get("covered")), _count(block.get("total")), _number(block.get("pct")))

        return {"format": "istanbul-summary", "lines": measure("lines"), "branches": measure("branches")}

    files = [value for value in data.values() if isinstance(value, dict) and "statementMap" in value]
    if not files:
        raise ReportError(
            "Not an Istanbul coverage report: expected coverage-summary.json "
            "(a 'total' block) or coverage-final.json (per-file statementMap)."
        )
    line_total = line_hit = branch_total = branch_hit = 0
    for entry in files:
        hits = entry.get("s") or {}
        lines: Dict[int, bool] = {}
        for key, location in (entry.get("statementMap") or {}).items():
            start = (location or {}).get("start") or {}
            line_number = start.get("line")
            if not isinstance(line_number, int):
                continue
            lines[line_number] = lines.get(line_number, False) or (_count(hits.get(key)) or 0) > 0
        line_total += len(lines)
        line_hit += sum(1 for covered in lines.values() if covered)
        for counts in (entry.get("b") or {}).values():
            if isinstance(counts, list):
                branch_total += len(counts)
                branch_hit += sum(1 for value in counts if (_count(value) or 0) > 0)
    return {
        "format": "istanbul-json",
        "lines": _metric(line_hit, line_total),
        "branches": _metric(branch_hit, branch_total),
        "files": len(files),
    }


_PARSERS: Dict[str, Callable[[IO[bytes], int], Dict[str, Any]]] = {
    "junit": _parse_junit,
    "trx": _parse_trx,
    "xunit": _parse_xunit,
    "nunit": _parse_nunit,
    "cobertura": _parse_cobertura,
    "jacoco": _parse_jacoco,
    "clover": _parse_clover,
    "lcov": _parse_lcov,
    "istanbul-json": _parse_istanbul_json,
}


# ---------------------------------------------------------------------------
# One build's summary, folded together one report at a time
#
# Reports arrive one HTTP upload at a time — a Maven build sends one file per
# test class — so the summary is merged incrementally rather than recomputed
# from every file each time. The caller holds a lock on the build row while
# it merges, so two uploads cannot both read the old summary.
#
# The merged summary keeps what it needs to stay correct as more files come:
# running test totals, the failed cases (capped), every coverage file's own
# counts (so the build figure can be re-derived — see combine_coverage), and a
# capped list of per-file entries for the drawer.
# ---------------------------------------------------------------------------

def empty_summary() -> Dict[str, Any]:
    return {
        "version": SUMMARY_VERSION,
        "reportCount": 0,
        "testReportCount": 0,
        "coverageReportCount": 0,
        "totals": {
            "tests": 0, "passed": 0, "failed": 0, "errors": 0,
            "skipped": 0, "flaky": 0, "durationSeconds": 0.0,
        },
        "failures": [],
        "failureCount": 0,
        "reports": [],
        "coverageReports": [],
        "coverage": None,
        "errors": [],
        "errorCount": 0,
    }


def file_summary(result: Optional[Dict[str, Any]], error: Optional[str] = None) -> Dict[str, Any]:
    """What is written onto the artifact itself: the counts, never the cases.

    The artifact row is listed with every build's artifacts, so it carries the
    small part; the failed cases live on the build.
    """
    if error:
        return {"error": _clip(error, 500)}
    assert result is not None
    out: Dict[str, Any] = {"kind": result["kind"], "format": result.get("format")}
    if result["kind"] == "tests":
        for key in ("tests", "passed", "failed", "errors", "skipped", "flaky", "durationSeconds"):
            out[key] = result.get(key, 0)
    else:
        out["lines"] = result.get("lines")
        out["branches"] = result.get("branches")
        if result.get("note"):
            out["note"] = result["note"]
    return out


def merge(
    summary: Optional[Dict[str, Any]],
    *,
    artifact_id: Optional[int],
    name: str,
    stage_position: Optional[int] = None,
    stage_name: Optional[str] = None,
    result: Optional[Dict[str, Any]] = None,
    error: Optional[str] = None,
) -> Dict[str, Any]:
    """Fold one file's outcome into a build summary. Returns a new dict.

    A new dict, never the same one mutated: the summary lives in a JSON
    column, and SQLAlchemy only notices a JSON change on reassignment.
    """
    out = json.loads(json.dumps(summary)) if isinstance(summary, dict) else empty_summary()
    if out.get("version") != SUMMARY_VERSION:
        out = empty_summary()
    out["reportCount"] += 1
    entry: Dict[str, Any] = {
        "artifactId": artifact_id,
        "name": _clip(name, 255),
        "stagePosition": stage_position,
        "stage": _clip(stage_name, 120),
    }

    if error or result is None:
        out["errorCount"] += 1
        if len(out["errors"]) < MAX_ERROR_ENTRIES:
            out["errors"].append({**entry, "message": _clip(error or "Unreadable report.", 500)})
        return out

    if result["kind"] == "tests":
        out["testReportCount"] += 1
        totals = out["totals"]
        for key in ("tests", "passed", "failed", "errors", "skipped", "flaky"):
            totals[key] += int(result.get(key) or 0)
        totals["durationSeconds"] = round(totals["durationSeconds"] + float(result.get("durationSeconds") or 0.0), 3)
        out["failureCount"] += int(result.get("failed") or 0) + int(result.get("errors") or 0)
        room = MAX_FAILURES - len(out["failures"])
        for case in (result.get("failures") or [])[: max(0, room)]:
            out["failures"].append({**case, "artifactId": artifact_id, "report": entry["name"]})
        if len(out["reports"]) < MAX_REPORT_ENTRIES:
            out["reports"].append(
                {
                    **entry,
                    "kind": "tests",
                    "format": result.get("format"),
                    "tests": int(result.get("tests") or 0),
                    "failed": int(result.get("failed") or 0) + int(result.get("errors") or 0),
                    "skipped": int(result.get("skipped") or 0),
                }
            )
    else:
        out["coverageReportCount"] += 1
        coverage_entry = {
            **entry,
            "kind": "coverage",
            "format": result.get("format"),
            "lines": result.get("lines"),
            "branches": result.get("branches"),
        }
        if result.get("note"):
            coverage_entry["note"] = result["note"]
        if len(out["coverageReports"]) < MAX_COVERAGE_ENTRIES:
            out["coverageReports"].append(coverage_entry)
        if len(out["reports"]) < MAX_REPORT_ENTRIES:
            out["reports"].append(coverage_entry)
        out["coverage"] = combine_coverage(out["coverageReports"])
    return out


def _has_counts(metric: Optional[Dict[str, Any]]) -> bool:
    return bool(metric) and metric.get("total") is not None and metric.get("covered") is not None


def _sum_metric(entries: List[Dict[str, Any]], key: str) -> Optional[Dict[str, Any]]:
    metrics = [entry.get(key) for entry in entries if _has_counts(entry.get(key))]
    if not metrics:
        return None
    return _metric(sum(m["covered"] for m in metrics), sum(m["total"] for m in metrics))


def _line_total(entry: Dict[str, Any]) -> int:
    metric = entry.get("lines")
    return int(metric["total"]) if _has_counts(metric) else 0


def _signature(entry: Dict[str, Any]) -> Tuple[Any, ...]:
    def part(metric: Optional[Dict[str, Any]]) -> Tuple[Any, ...]:
        metric = metric or {}
        return (metric.get("covered"), metric.get("total"), metric.get("pct"))

    return part(entry.get("lines")) + part(entry.get("branches"))


def _combine_one_format(entries: List[Dict[str, Any]]) -> Tuple[List[Dict[str, Any]], str]:
    """Which files of one format make up the build's figure, and how.

    * identical figures are one report kept twice (``coverage.xml`` copied
      into two places) and count once;
    * a file whose line total equals the sum of all the others is an
      aggregate of them (``jacoco-aggregate`` next to each module's report)
      and is used on its own — adding it to its parts would double every line;
    * otherwise they are separate modules and are summed;
    * a file with only a rate cannot be summed with anything, so the most
      complete one (the one with counts and the most lines) is used.
    """
    unique: List[Dict[str, Any]] = []
    seen = set()
    for entry in entries:
        signature = _signature(entry)
        if signature in seen:
            continue
        seen.add(signature)
        unique.append(entry)
    if len(unique) == 1:
        return unique, "single"
    if not all(_has_counts(entry.get("lines")) for entry in unique):
        best = max(unique, key=lambda entry: (_has_counts(entry.get("lines")), _line_total(entry)))
        return [best], "largest"
    totals = [_line_total(entry) for entry in unique]
    grand = sum(totals)
    # Three or more: with two, "one equals the rest" is just two modules that
    # happen to be the same size (an aggregate of ONE module is identical to
    # it, and the dedupe above already folded that case).
    if len(unique) >= 3:
        for entry, total in zip(unique, totals):
            if total > 0 and total == grand - total:
                return [entry], "aggregate"
    return unique, "sum"


def combine_coverage(entries: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """The build's coverage figure from every coverage file it kept.

    Files of one format are combined as :func:`_combine_one_format` explains.
    Two formats means the same run was usually reported twice (jest writing
    both lcov and Cobertura; coverlet writing Cobertura and OpenCover), and
    adding them would count every line twice — so the format covering the
    most lines is used and the result says the other was left out.
    """
    usable = [entry for entry in entries if entry.get("lines") or entry.get("branches")]
    if not usable:
        return None
    by_format: Dict[str, List[Dict[str, Any]]] = {}
    for entry in usable:
        by_format.setdefault(entry.get("format") or "unknown", []).append(entry)

    candidates = []
    for fmt, group in by_format.items():
        chosen, method = _combine_one_format(group)
        if method in ("sum", "single", "aggregate") and all(_has_counts(e.get("lines")) for e in chosen):
            lines = _sum_metric(chosen, "lines")
        else:
            lines = chosen[0].get("lines")
        if all(_has_counts(e.get("branches")) for e in chosen if e.get("branches")):
            branches = _sum_metric(chosen, "branches") or chosen[0].get("branches")
        else:
            branches = chosen[0].get("branches")
        candidates.append(
            {
                "format": fmt,
                "lines": lines,
                "branches": branches,
                "method": method,
                "used": len(chosen),
                "files": len(group),
            }
        )
    candidates.sort(
        key=lambda c: (_has_counts(c["lines"]), (c["lines"] or {}).get("total") or 0, c["files"]),
        reverse=True,
    )
    best = candidates[0]
    notes: List[str] = []
    if best["method"] == "aggregate":
        notes.append("An aggregate report was found; its module reports were not added to it.")
    elif best["method"] == "largest":
        notes.append(
            f"{best['files']} {FORMAT_LABEL.get(best['format'], best['format'])} files without line "
            "counts cannot be added together; the most complete one is shown."
        )
    elif best["method"] == "sum" and best["used"] > 1:
        notes.append(f"Combined from {best['used']} {FORMAT_LABEL.get(best['format'], best['format'])} reports.")
    for other in candidates[1:]:
        notes.append(
            f"{FORMAT_LABEL.get(other['format'], other['format'])} ({other['files']} file"
            f"{'' if other['files'] == 1 else 's'}) was not added: it reports the same run in another format."
        )
    coverage_notes = sorted({e["note"] for e in usable if e.get("note") and e.get("format") == best["format"]})
    notes.extend(coverage_notes)
    return {
        "format": best["format"],
        "lines": best["lines"],
        "branches": best["branches"],
        "reportsUsed": best["used"],
        "note": " ".join(notes) or None,
    }


def compact(summary: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """The few numbers a build list shows. None when nothing was collected."""
    if not isinstance(summary, dict) or not summary.get("reportCount"):
        return None
    totals = summary.get("totals") or {}
    coverage = summary.get("coverage") or {}
    has_tests = bool(summary.get("testReportCount"))
    return {
        "reportCount": int(summary.get("reportCount") or 0),
        "testReportCount": int(summary.get("testReportCount") or 0),
        "coverageReportCount": int(summary.get("coverageReportCount") or 0),
        "total": int(totals.get("tests") or 0) if has_tests else None,
        "passed": int(totals.get("passed") or 0) if has_tests else None,
        "failed": int(totals.get("failed") or 0) if has_tests else None,
        "errors": int(totals.get("errors") or 0) if has_tests else None,
        "skipped": int(totals.get("skipped") or 0) if has_tests else None,
        "flaky": int(totals.get("flaky") or 0) if has_tests else None,
        "durationSeconds": totals.get("durationSeconds") if has_tests else None,
        "linesPct": (coverage.get("lines") or {}).get("pct"),
        "branchesPct": (coverage.get("branches") or {}).get("pct"),
        "parseErrors": int(summary.get("errorCount") or 0),
    }
