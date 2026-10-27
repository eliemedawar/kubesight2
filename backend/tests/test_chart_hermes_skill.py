"""The Helm chart ships its own copy of the KubeSight skill for Hermes.

Helm can only package files inside the chart, so charts/kubesight carries a
copy of .claude/skills/kubesight. A skill edit that misses the copy would put
Hermes on an outdated skill in production; run charts/kubesight/sync-hermes-skill.sh.
"""

from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
SOURCE = REPO / ".claude" / "skills" / "kubesight"
CHART_COPY = REPO / "charts" / "kubesight" / "files" / "hermes-skills" / "kubesight"


def _files(root: Path) -> dict:
    return {
        path.relative_to(root).as_posix(): path.read_bytes().replace(b"\r\n", b"\n")
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


def test_chart_carries_the_current_kubesight_skill():
    source = _files(SOURCE)
    assert "SKILL.md" in source
    assert _files(CHART_COPY) == source, "run charts/kubesight/sync-hermes-skill.sh"
