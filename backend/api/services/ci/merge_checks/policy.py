"""The quality gate: where its numbers come from, and what they decide.

Two jobs, kept in one module because they are two halves of one idea:

*Resolution* — the installation policy is the floor, a service may override it,
and :func:`resolve_gate` produces the one flat document that a check is judged
against. That document is then COPIED onto the check row, so relaxing the gate
next month never rewrites the record of a merge blocked under the old one.

*Evaluation* — :func:`evaluate` takes that document and the tools' reported
metrics and returns a verdict with the reasons written out. It does no I/O and
reads no database, which is what makes the gate testable as a function of two
dictionaries rather than of a cluster.

A cap is a MAXIMUM THAT PASSES: a gate of 5 allows 5 problems and blocks 6.
That is the reading of "the quality gate is 5" that everyone means, and the one
the UI states in words next to the field so nobody has to guess.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from ....db import db
from ....models_merge_checks import (
    MERGE_CHECK_TOOLS,
    SEVERITIES,
    TOOL_LABELS,
    CiMergeCheckPolicy,
)

# The per-tool cap field on the gate, by tool. Kept as a mapping rather than an
# f-string over the tool name: the tool is `dependency_check` and the column is
# `max_dependency_problems`, and a derived name would be wrong for exactly one
# of the three, which is the worst possible number.
TOOL_CAP_FIELD = {
    "eslint": "maxEslintProblems",
    "semgrep": "maxSemgrepProblems",
    "sonar": "maxSonarProblems",
    "dependency_check": "maxDependencyProblems",
}
TOOL_CAP_COLUMN = {
    "eslint": "max_eslint_problems",
    "semgrep": "max_semgrep_problems",
    "sonar": "max_sonar_problems",
    "dependency_check": "max_dependency_problems",
}

# Every field the two tables share, as (column, payload key). One list, so a
# new gate knob is added in one place and both the policy and the override
# read, write and serialize it.
GATE_FIELDS = (
    ("max_total_problems", "maxTotalProblems", "int"),
    ("max_eslint_problems", "maxEslintProblems", "int"),
    ("max_semgrep_problems", "maxSemgrepProblems", "int"),
    ("max_sonar_problems", "maxSonarProblems", "int"),
    ("max_dependency_problems", "maxDependencyProblems", "int"),
    ("eslint_count_warnings", "eslintCountWarnings", "bool"),
    ("semgrep_min_severity", "semgrepMinSeverity", "severity"),
    ("sonar_min_severity", "sonarMinSeverity", "severity"),
    ("dependency_min_severity", "dependencyMinSeverity", "severity"),
    ("block_on_tool_error", "blockOnToolError", "bool"),
)

# What the gate falls back to when NEITHER the service nor the policy has an
# opinion. Note that every cap is None here: an installation that has not set a
# gate does not have one, and a number invented in this file would start
# blocking merges nobody agreed to block.
GATE_DEFAULTS: Dict[str, Any] = {
    "maxTotalProblems": None,
    "maxEslintProblems": None,
    "maxSemgrepProblems": None,
    "maxSonarProblems": None,
    "maxDependencyProblems": None,
    "eslintCountWarnings": False,
    "semgrepMinSeverity": "medium",
    "sonarMinSeverity": "medium",
    "dependencyMinSeverity": "high",
    "blockOnToolError": True,
}


class PolicyError(ValueError):
    """Something an operator typed that cannot be saved, in their words."""


# ---------------------------------------------------------------------------
# Reading and writing the installation policy
# ---------------------------------------------------------------------------

def get_policy() -> CiMergeCheckPolicy:
    """The single policy row, created on first read.

    Created rather than returned as None so every caller downstream has an
    object with the right shape, and so the row exists to be edited the first
    time somebody opens the settings panel.
    """
    row = db.session.get(CiMergeCheckPolicy, 1)
    if row is None:
        row = CiMergeCheckPolicy(id=1, enabled_by_default=False)
        db.session.add(row)
        db.session.commit()
    return row


def _clean_cap(value: Any, label: str) -> Optional[int]:
    """A cap, or None for "no cap". Empty string is None, not zero.

    The distinction matters: 0 is a real and very strict gate ("no problems at
    all"), and a blank field that silently became 0 would block every merge in
    the installation the moment somebody cleared a box.
    """
    if value is None or (isinstance(value, str) and not value.strip()):
        return None
    try:
        number = int(value)
    except (TypeError, ValueError) as exc:
        raise PolicyError(f"{label} must be a whole number.") from exc
    if number < 0:
        raise PolicyError(f"{label} cannot be negative.")
    if number > 100000:
        raise PolicyError(f"{label} is unreasonably large.")
    return number


def _clean_bool(value: Any) -> Optional[bool]:
    if value is None or (isinstance(value, str) and not value.strip()):
        return None
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return bool(value)


def _clean_severity(value: Any, label: str) -> Optional[str]:
    if value is None or (isinstance(value, str) and not value.strip()):
        return None
    clean = str(value).strip().lower()
    if clean not in SEVERITIES:
        raise PolicyError(f"{label} must be one of: {', '.join(SEVERITIES)}.")
    return clean


_LABELS = {
    "max_total_problems": "The total problem limit",
    "max_eslint_problems": "The ESLint limit",
    "max_semgrep_problems": "The Semgrep limit",
    "semgrep_min_severity": "The Semgrep severity floor",
    "max_sonar_problems": "The SonarQube limit",
    "max_dependency_problems": "The Dependency-Check limit",
    "sonar_min_severity": "The SonarQube severity floor",
    "dependency_min_severity": "The Dependency-Check severity floor",
}


def apply_gate_fields(row: Any, payload: Dict[str, Any]) -> None:
    """Copy the gate fields present in ``payload`` onto ``row``.

    Absent keys are left alone; a key present and empty clears the field back
    to "no opinion". That is the difference between "I did not touch this" and
    "I want this to inherit", and both have to be expressible or a service can
    never go back to following the policy.
    """
    for column, key, kind in GATE_FIELDS:
        if key not in payload:
            continue
        raw = payload.get(key)
        label = _LABELS.get(column, key)
        if kind == "int":
            setattr(row, column, _clean_cap(raw, label))
        elif kind == "bool":
            setattr(row, column, _clean_bool(raw))
        else:
            setattr(row, column, _clean_severity(raw, label))


def gate_payload(row: Any) -> Dict[str, Any]:
    """One row's gate fields as an API document, NULLs included.

    NULLs are kept rather than filled in: on a service they mean "inherit", and
    the panel has to be able to show an empty box that is genuinely empty.
    """
    return {key: getattr(row, column, None) for column, key, _kind in GATE_FIELDS}


# ---------------------------------------------------------------------------
# Resolution
# ---------------------------------------------------------------------------

def resolve_gate(config: Any = None, policy: Any = None) -> Dict[str, Any]:
    """The gate one check is judged against: service over policy over defaults.

    ``config`` may be None (no merge-check configuration at all) and may be in
    ``inherit`` mode, in which case its own numbers are ignored entirely rather
    than merged field by field. Half-inheriting would mean a service that set a
    stricter ESLint cap last year silently keeps it after switching back to
    inherit, which is not what the word means.
    """
    policy = policy if policy is not None else get_policy()
    resolved = dict(GATE_DEFAULTS)
    sources = {key: "default" for key in resolved}

    for _column, key, _kind in GATE_FIELDS:
        value = getattr(policy, _column, None)
        if value is not None:
            resolved[key] = value
            sources[key] = "policy"

    overriding = config is not None and getattr(config, "gate_mode", "inherit") == "override"
    if overriding:
        for _column, key, _kind in GATE_FIELDS:
            value = getattr(config, _column, None)
            if value is not None:
                resolved[key] = value
                sources[key] = "service"

    resolved["mode"] = "override" if overriding else "inherit"
    resolved["sources"] = sources
    return resolved


# ---------------------------------------------------------------------------
# Evaluation
# ---------------------------------------------------------------------------

def _tool_label(tool: str) -> str:
    return TOOL_LABELS.get(tool, tool)


def evaluate(gate: Dict[str, Any], metrics: Dict[str, Any]) -> Dict[str, Any]:
    """Judge one set of tool metrics against one gate.

    Pure: no database, no clock, no network. Returns

        {"verdict": "allowed"|"blocked", "totalProblems": int,
         "reasons": [str, ...], "counted": {tool: int}}

    ``metrics`` is what the tools reported, keyed by tool:

        {"eslint": {"status": "ok", "problems": 3, "errors": 3, ...}}

    A tool with ``status`` other than ``ok`` contributes no problems — it found
    nothing because it did not look — and is handled by ``blockOnToolError``
    instead. Counting a crashed tool as zero problems and passing the merge is
    the single most dangerous thing this function could do, so it is the one
    case spelled out below rather than left to fall through.
    """
    reasons: List[str] = []
    counted: Dict[str, int] = {}
    total = 0
    broken: List[str] = []

    for tool in MERGE_CHECK_TOOLS:
        report = metrics.get(tool)
        if not isinstance(report, dict):
            continue
        status = str(report.get("status") or "").lower()
        if status == "skipped":
            continue
        if status != "ok":
            broken.append(tool)
            continue
        try:
            problems = max(0, int(report.get("problems") or 0))
        except (TypeError, ValueError):
            broken.append(tool)
            continue
        counted[tool] = problems
        total += problems

        cap = gate.get(TOOL_CAP_FIELD[tool])
        if cap is not None and problems > cap:
            reasons.append(
                f"{_tool_label(tool)} reported {problems} "
                f"{'problem' if problems == 1 else 'problems'}; "
                f"this service allows at most {cap}."
            )

    cap = gate.get("maxTotalProblems")
    if cap is not None and total > cap:
        reasons.append(
            f"{total} {'problem' if total == 1 else 'problems'} in total; "
            f"the quality gate allows at most {cap}."
        )

    if broken:
        names = ", ".join(_tool_label(tool) for tool in broken)
        if gate.get("blockOnToolError", True):
            reasons.append(
                f"{names} could not complete, so this change has not been checked."
            )
        else:
            # Recorded either way. A merge that went through unchecked is
            # something somebody will want to find later.
            reasons.append(
                f"{names} could not complete. The gate is configured not to "
                "block on a failed check, so this is a warning only."
            )

    blocking = [
        reason for reason in reasons if "warning only" not in reason
    ]
    return {
        "verdict": "blocked" if blocking else "allowed",
        "totalProblems": total,
        "reasons": reasons,
        "counted": counted,
        "brokenTools": broken,
    }


def summarize(evaluation: Dict[str, Any], gate: Dict[str, Any]) -> str:
    """The one line Bitbucket shows beside the build status.

    Bitbucket truncates this hard, so it leads with the number and the verdict
    and leaves the detail to the pull request comment.
    """
    total = evaluation.get("totalProblems", 0)
    cap = gate.get("maxTotalProblems")
    noun = "problem" if total == 1 else "problems"
    if evaluation.get("verdict") == "allowed":
        if cap is None:
            return f"Quality gate passed — {total} {noun}."
        return f"Quality gate passed — {total} of {cap} {noun} allowed."
    first = (evaluation.get("reasons") or ["Quality gate failed."])[0]
    return first[:255]
