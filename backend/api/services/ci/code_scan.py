"""A quality gate on a command stage that scans source code.

The stage keeps its own commands — whatever ``semgrep scan ...`` line somebody
wrote — and KubeSight adds two things around them:

1. before: a ``semgrep`` shell function that runs the real binary with
   ``--json-output=<report>`` added, so the scan writes its full results to a
   file as well as printing the readable summary it always printed. A command
   the author already wrote does not have to change;
2. after: :mod:`code_scan_gate`, which counts the blocking findings in that
   file and fails the stage when there are more than the stage allows.

Semgrep's own exit code stops being the verdict: it exits 0 on a finished scan
however much it found. With ``--error`` it exits 1 on any finding, and the shim
turns that 1 back into "the gate decides" — two thresholds on one stage would
mean the stricter one silently wins.

The report is kept as a ``scan-report`` artifact whether the gate passed or
failed (the Kubernetes runner collects it after the last stage, failed or not),
and the PDF people download or send is built from it.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Dict, List, Optional

TOOLS = ("semgrep",)
COUNT_FROM = ("info", "warning", "error")
MAX_ALLOWED = 100000
MAX_RECIPIENTS = 25

_EMAIL_RE = re.compile(r"^[^@\s,;<>]+@[^@\s,;<>]+\.[^@\s,;<>]+$")
_GATE_SOURCE = Path(__file__).with_name("code_scan_gate.py")
_HEREDOC = "KS_CODE_SCAN_GATE"

# Semgrep subcommands that do not scan. Passed straight through: adding an
# output flag to `semgrep --version` would only break it.
_NOT_A_SCAN = (
    "--version",
    "--help",
    "-h",
    "login",
    "logout",
    "lsp",
    "mcp",
    "publish",
    "show",
    "install-semgrep-pro",
)


class CodeScanConfigError(ValueError):
    pass


def default_config() -> Dict[str, Any]:
    return {"enabled": True, "tool": "semgrep", "maxBlocking": 0, "countFrom": "info", "recipients": []}


def armed(config: Any) -> bool:
    return isinstance(config, dict) and config.get("enabled") is not False


def clean_recipients(value: Any, *, source: str = "Recipients") -> List[str]:
    """Distinct, valid email addresses, in the order given."""
    if value in (None, ""):
        return []
    if isinstance(value, str):
        value = re.split(r"[\s,;]+", value)
    if not isinstance(value, list):
        raise CodeScanConfigError(f"{source} must be a list of email addresses.")
    seen: List[str] = []
    for item in value:
        address = str(item or "").strip()
        if not address:
            continue
        if len(address) > 254 or not _EMAIL_RE.match(address):
            raise CodeScanConfigError(f"{source}: '{address[:80]}' is not an email address.")
        if address.lower() not in {entry.lower() for entry in seen}:
            seen.append(address)
    if len(seen) > MAX_RECIPIENTS:
        raise CodeScanConfigError(f"{source}: at most {MAX_RECIPIENTS} addresses.")
    return seen


# The stage kinds a gate can sit on: a command stage that runs semgrep itself,
# and a scan stage whose script KubeSight generates around it (scan_stage.py,
# which also refuses the gate on a scan stage running any OTHER tool).
STAGE_TYPES = ("command", "scan")


def normalize(value: Any, stage_type: str, stage_name: str) -> Optional[Dict[str, Any]]:
    """A command or scan stage's quality gate, or None.

    None and ``{"enabled": false}`` are kept apart the way the image scan keeps
    them: never configured, versus configured and parked. Refused on any other
    stage kind rather than dropped — a gate that silently vanished on save is
    one somebody still believes in.
    """
    if value in (None, "", {}):
        return None
    if not isinstance(value, dict):
        raise CodeScanConfigError(f"Stage '{stage_name}' has an invalid quality gate.")
    if stage_type not in STAGE_TYPES:
        raise CodeScanConfigError(
            f"Stage '{stage_name}' is a {stage_type} stage. The code scan quality gate goes "
            "on the command stage that runs the scanner, or on a Semgrep scan stage."
        )
    tool = str(value.get("tool") or "semgrep").strip().lower()
    if tool not in TOOLS:
        raise CodeScanConfigError(
            f"Stage '{stage_name}': the quality gate supports {', '.join(TOOLS)}, not '{tool[:32]}'."
        )
    raw_max = value.get("maxBlocking", 0)
    try:
        max_blocking = int(raw_max if raw_max not in (None, "") else 0)
    except (TypeError, ValueError):
        raise CodeScanConfigError(
            f"Stage '{stage_name}': allowed blocking findings must be a whole number."
        )
    if not 0 <= max_blocking <= MAX_ALLOWED:
        raise CodeScanConfigError(
            f"Stage '{stage_name}': allowed blocking findings must be between 0 and {MAX_ALLOWED}."
        )
    count_from = str(value.get("countFrom") or "info").strip().lower()
    if count_from not in COUNT_FROM:
        raise CodeScanConfigError(
            f"Stage '{stage_name}': count findings from one of {', '.join(COUNT_FROM)}."
        )
    return {
        "enabled": value.get("enabled") is not False,
        "tool": tool,
        "maxBlocking": max_blocking,
        "countFrom": count_from,
        "recipients": clean_recipients(
            value.get("recipients"), source=f"Stage '{stage_name}' report recipients"
        ),
    }


def report_file(position: int) -> str:
    """The report's name inside the workspace's .kubesight directory."""
    return f"code-scan-{int(position)}.json"


def report_artifact_path(position: int) -> str:
    """Where the Kubernetes collector finds it. Outside /workspace/source so a
    later image build never sends it to BuildKit as part of the context."""
    return f"/workspace/.kubesight/{report_file(position)}"


def report_artifact_name(position: int) -> str:
    return f"code-scan-stage-{int(position) + 1}.json"


def _gate_source() -> str:
    source = _GATE_SOURCE.read_text(encoding="utf-8")
    if _HEREDOC in source:  # pragma: no cover - guards an edit to the gate file
        raise RuntimeError(f"code_scan_gate.py must not contain the line {_HEREDOC}.")
    return source


def wrap_commands(commands: List[str], config: Dict[str, Any], position: int) -> List[str]:
    """The stage's commands with the shim before them and the gate after.

    Returned as ONE script line: the runners join lines with newlines and a
    heredoc has to reach them intact. Runs inside the stage's ``set -e``
    subshell, so an ``exit 1`` here is the stage failing and nothing more.
    """
    max_blocking = int(config.get("maxBlocking") or 0)
    count_from = str(config.get("countFrom") or "info")
    if count_from not in COUNT_FROM:
        count_from = "info"
    not_a_scan = "|".join(_NOT_A_SCAN)
    prelude = f"""# -- KubeSight quality gate: Semgrep also writes its results to a file --
KS_CODE_SCAN_REPORT="${{KUBESIGHT_WORKSPACE:-/workspace}}/.kubesight/{report_file(position)}"
mkdir -p "$(dirname "$KS_CODE_SCAN_REPORT")"
rm -f "$KS_CODE_SCAN_REPORT"
semgrep() {{
  case "${{1:-}}" in
    {not_a_scan}) command semgrep "$@"; return $? ;;
  esac
  KS_SEMGREP_RC=0
  command semgrep "$@" --json-output="$KS_CODE_SCAN_REPORT" || KS_SEMGREP_RC=$?
  if [ "$KS_SEMGREP_RC" -eq 1 ] && [ -s "$KS_CODE_SCAN_REPORT" ]; then
    echo "[kubesight] Semgrep exited 1 because it found something - the quality gate below decides."
    KS_SEMGREP_RC=0
  fi
  return "$KS_SEMGREP_RC"
}}
"""
    gate = f"""
# -- KubeSight quality gate --
if [ ! -s "$KS_CODE_SCAN_REPORT" ]; then
  echo "[kubesight] Quality gate FAILED: Semgrep saved no results, so nothing could be checked." >&2
  echo "[kubesight] Run 'semgrep scan ...' directly in this stage's commands - a semgrep started" >&2
  echo "[kubesight] from inside another script is not seen by the gate." >&2
  exit 1
fi
if ! command -v python3 >/dev/null 2>&1; then
  echo "[kubesight] Quality gate FAILED: python3 is not in this stage's image. The Semgrep image has it." >&2
  exit 1
fi
python3 - "$KS_CODE_SCAN_REPORT" {max_blocking} {count_from} <<'{_HEREDOC}'
{_gate_source()}
{_HEREDOC}
"""
    body = "\n".join(commands or ["true"])
    return [prelude + body + "\n" + gate]


def artifact_spec(position: int) -> Dict[str, Any]:
    """The report, as the Kubernetes collector's artifact spec."""
    return {
        "path": report_artifact_path(position),
        "type": "scan-report",
        "name": report_artifact_name(position),
        "workdir": "",
        "stagePosition": position,
    }
