"""The agent platform's department files, and the ticket prompt, stay in step.

Two copies to keep honest while Hermes and the platform run side by side:

* The chart ships agent-platform/departments under files/agent-platform (Helm
  can only package files inside the chart); run
  charts/kubesight/sync-agent-platform.sh after editing a department.
* The platform's ticket prompt is the Hermes prompt with a typed result in
  place of the closing JSON line. A rule added to one and not the other would
  make the two engines handle the same ticket differently.
"""

import re
from pathlib import Path

from api.services.ticket_agent import hermes

REPO = Path(__file__).resolve().parents[2]
SOURCE = REPO / "agent-platform" / "departments"
CHART_COPY = REPO / "charts" / "kubesight" / "files" / "agent-platform"
PLATFORM_PROMPT = SOURCE / "infra" / "prompts" / "tickets.md"


def _files(root: Path) -> dict:
    return {
        path.relative_to(root).as_posix(): path.read_bytes().replace(b"\r\n", b"\n")
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


def test_chart_carries_the_current_department_files():
    source = _files(SOURCE)
    assert "infra/agents.yaml" in source
    assert _files(CHART_COPY) == source, "run charts/kubesight/sync-agent-platform.sh"


def _rules(text: str) -> str:
    # Everything before the closing instruction, whitespace-normalised.
    body = re.split(r"\nWhen you are done,", text, maxsplit=1)[0]
    return " ".join(body.split())


def test_platform_ticket_prompt_matches_the_hermes_prompt():
    platform = PLATFORM_PROMPT.read_text(encoding="utf-8")
    assert _rules(platform) == _rules(hermes.SYSTEM_PROMPT), (
        "The ticket rules differ between Hermes (ticket_agent/hermes.py) and the agent "
        "platform (agent-platform/departments/infra/prompts/tickets.md): change both."
    )
