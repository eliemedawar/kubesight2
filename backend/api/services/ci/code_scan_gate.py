"""The code-scan quality gate: count Semgrep's blocking findings, pass or fail.

ONE file, two callers, on purpose:

* the build — the Kubernetes runner feeds this file's source to ``python3 -``
  inside the scan stage's own container, after the stage's commands, so the
  verdict is reached where the report is and before the next stage starts;
* the server — ``code_scan_report`` imports it to rebuild the same summary from
  the uploaded report when somebody downloads or sends the PDF.

A count written twice drifts, and a PDF that says "5 blocking" about a build
the log says failed on 8 is worse than no PDF. So this module is standard
library only and imports nothing from the package — it has to run in a
Semgrep image that has never heard of KubeSight.

Semgrep's own exit code is not the verdict. It exits 0 on a finished scan
whatever it found, and 1 only with ``--error``; the gate replaces both with
"more blocking findings than this stage allows".
"""

import json
import os
import sys

# Semgrep has used two severity vocabularies. The gate buckets both into the
# three it has always printed, because that is what the log shows people.
_BUCKET = {
    "CRITICAL": "error",
    "ERROR": "error",
    "HIGH": "error",
    "WARNING": "warning",
    "MEDIUM": "warning",
    "INFO": "info",
    "LOW": "info",
    "INVENTORY": "info",
    "EXPERIMENT": "info",
}
SEVERITIES = ("error", "warning", "info")  # worst first

# What "count findings from" means: this bucket and everything worse.
COUNTED = {
    "info": {"error", "warning", "info"},
    "warning": {"error", "warning"},
    "error": {"error"},
}

# The report keeps code context for this many findings, this many lines each.
# Bounded so a scan with 20,000 findings does not become a 200 MB artifact.
MAX_SNIPPETS = 500
SNIPPET_CONTEXT = 2
SNIPPET_MAX_LINES = 14

# A finding ABOUT a secret quotes the secret. The build log masks known secret
# values, but this report is a file that gets emailed, so the snippet of a
# secrets finding is withheld rather than trusted to a mask it never passed.
_SECRET_WORDS = (
    "secret",
    "password",
    "passwd",
    "credential",
    "api-key",
    "apikey",
    "api_key",
    "private-key",
    "private_key",
    "token",
    "cwe-798",
    "cwe-259",
)


def bucket(severity):
    return _BUCKET.get(str(severity or "").upper(), "warning")


def is_blocking(result):
    """Every finding blocks unless Semgrep itself says it does not.

    ``is_ignored`` is a ``# nosemgrep`` comment; the explicit ``False`` flags
    are what a Semgrep AppSec policy writes for a "monitor" rule. A local
    registry scan sets neither, which is why its summary reads "8 (8 blocking)".
    """
    extra = result.get("extra") or {}
    if extra.get("is_ignored"):
        return False
    if extra.get("is_blocking") is False or extra.get("blocking") is False:
        return False
    return True


def is_secret_finding(result):
    extra = result.get("extra") or {}
    metadata = extra.get("metadata") or {}
    haystack = " ".join(
        str(part)
        for part in (
            result.get("check_id"),
            metadata.get("category"),
            metadata.get("subcategory"),
            metadata.get("cwe"),
            metadata.get("technology"),
        )
        if part
    ).lower()
    return any(word in haystack for word in _SECRET_WORDS)


def summarize(report, count_from="info"):
    """What the gate counts, from a Semgrep JSON report. Pure."""
    counted = COUNTED.get(str(count_from or "info").lower(), COUNTED["info"])
    results = [item for item in (report.get("results") or []) if isinstance(item, dict)]
    by_severity = {name: 0 for name in SEVERITIES}
    blocking_by_severity = {name: 0 for name in SEVERITIES}
    files = {}
    rules = {}
    blocking = 0
    ignored = 0
    for result in results:
        extra = result.get("extra") or {}
        level = bucket(extra.get("severity"))
        by_severity[level] += 1
        if not is_blocking(result):
            ignored += 1
            continue
        if level not in counted:
            continue
        blocking += 1
        blocking_by_severity[level] += 1
        path = str(result.get("path") or "?")
        files[path] = files.get(path, 0) + 1
        rule = str(result.get("check_id") or "?")
        entry = rules.setdefault(rule, {"count": 0, "severity": level})
        entry["count"] += 1
    paths = report.get("paths") or {}
    scanned = paths.get("scanned")
    return {
        "total": len(results),
        "blocking": blocking,
        "countFrom": str(count_from or "info").lower(),
        "bySeverity": by_severity,
        "blockingBySeverity": blocking_by_severity,
        "notBlocking": ignored,
        "scanErrors": len(report.get("errors") or []),
        "filesScanned": len(scanned) if isinstance(scanned, list) else None,
        "files": files,
        "rules": rules,
        "semgrepVersion": report.get("version") or "",
    }


def verdict(summary, max_blocking):
    allowed = max(0, int(max_blocking or 0))
    return "passed" if summary["blocking"] <= allowed else "failed"


def counted_results(report, count_from="info"):
    """The findings the gate counted, worst first, then by file and line."""
    counted = COUNTED.get(str(count_from or "info").lower(), COUNTED["info"])
    rows = []
    for result in report.get("results") or []:
        if not isinstance(result, dict) or not is_blocking(result):
            continue
        level = bucket((result.get("extra") or {}).get("severity"))
        if level in counted:
            rows.append(result)
    order = {name: index for index, name in enumerate(SEVERITIES)}
    rows.sort(
        key=lambda item: (
            order[bucket((item.get("extra") or {}).get("severity"))],
            str(item.get("path") or ""),
            int(((item.get("start") or {}).get("line")) or 0),
        )
    )
    return rows


def _attach_snippets(report, root):
    """Copy the code each finding points at into the report.

    Semgrep writes ``"requires login"`` in place of the matched lines unless the
    scanner is logged in to Semgrep's cloud, and the PDF is read by people with
    no checkout. The source is right here, so the gate copies the lines itself.
    """
    attached = 0
    cache = {}
    for result in report.get("results") or []:
        if attached >= MAX_SNIPPETS:
            break
        if not isinstance(result, dict):
            continue
        extra = result.setdefault("extra", {})
        if is_secret_finding(result):
            extra["kubesightSnippet"] = {"withheld": True}
            continue
        path = str(result.get("path") or "")
        start = int(((result.get("start") or {}).get("line")) or 0)
        end = int(((result.get("end") or {}).get("line")) or start)
        if not path or start <= 0:
            continue
        full = path if os.path.isabs(path) else os.path.join(root, path)
        if full not in cache:
            try:
                with open(full, "r", encoding="utf-8", errors="replace") as handle:
                    cache[full] = handle.read().splitlines()
            except OSError:
                cache[full] = None
        lines = cache[full]
        if not lines:
            continue
        first = max(1, start - SNIPPET_CONTEXT)
        last = min(len(lines), max(end, start) + SNIPPET_CONTEXT, first + SNIPPET_MAX_LINES - 1)
        extra["kubesightSnippet"] = {
            "firstLine": first,
            "matchStart": start,
            "matchEnd": end,
            "lines": [line[:240] for line in lines[first - 1 : last]],
        }
        attached += 1
    return attached


def _location(result):
    return "%s:%s" % (result.get("path") or "?", (result.get("start") or {}).get("line") or "?")


def main(argv):
    if len(argv) < 3:
        print("[kubesight] quality gate: usage: <report.json> <max blocking> <count from>")
        return 2
    report_path, max_text, count_from = argv[0], argv[1], argv[2]
    try:
        allowed = max(0, int(max_text))
    except ValueError:
        allowed = 0
    try:
        with open(report_path, "r", encoding="utf-8") as handle:
            report = json.load(handle)
    except (OSError, ValueError) as exc:
        print("[kubesight] Quality gate FAILED: the Semgrep report could not be read (%s)." % exc)
        return 1

    summary = summarize(report, count_from)
    result = verdict(summary, allowed)
    counted_label = {
        "info": "every severity",
        "warning": "WARNING and ERROR",
        "error": "ERROR only",
    }.get(summary["countFrom"], "every severity")

    _attach_snippets(report, os.getcwd())
    report["kubesight"] = {
        "gate": {"maxBlocking": allowed, "countFrom": summary["countFrom"], "verdict": result},
        "summary": {key: value for key, value in summary.items() if key not in ("files", "rules")},
    }
    try:
        with open(report_path, "w", encoding="utf-8") as handle:
            json.dump(report, handle)
    except OSError as exc:
        # The verdict stands either way; only the PDF's code excerpts are lost.
        print("[kubesight] could not add code excerpts to the report: %s" % exc)

    sev = summary["blockingBySeverity"]
    print("")
    print("[kubesight] == quality gate ==")
    print(
        "[kubesight] %d blocking finding%s (ERROR %d, WARNING %d, INFO %d), counting %s. Allowed: %d."
        % (
            summary["blocking"],
            "" if summary["blocking"] == 1 else "s",
            sev["error"],
            sev["warning"],
            sev["info"],
            counted_label,
            allowed,
        )
    )
    if summary["notBlocking"]:
        print("[kubesight] %d more marked non-blocking (nosemgrep or a monitor rule), not counted." % summary["notBlocking"])
    if summary["scanErrors"]:
        print("[kubesight] Semgrep reported %d file(s) it could not fully analyse." % summary["scanErrors"])

    rows = counted_results(report, count_from)
    for item in rows[:25]:
        extra = item.get("extra") or {}
        print(
            "  %-8s %s  %s"
            % (bucket(extra.get("severity")).upper(), _location(item), str(item.get("check_id") or "?").split(".")[-1])
        )
    if len(rows) > 25:
        print("  ... and %d more - every one is in the PDF report." % (len(rows) - 25))

    if result == "failed":
        print(
            "[kubesight] Quality gate FAILED: %d blocking finding%s, more than the %d allowed."
            % (summary["blocking"], "" if summary["blocking"] == 1 else "s", allowed)
        )
        print("[kubesight] Download or send the PDF report from this build to see why.")
        return 1
    print("[kubesight] Quality gate passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
