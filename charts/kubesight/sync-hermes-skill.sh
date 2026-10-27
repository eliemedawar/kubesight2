#!/usr/bin/env bash
# Copy the KubeSight skill (.claude/skills/kubesight) into the chart, which can
# only package files inside its own folder. Run it after changing the skill;
# backend/tests/test_chart_hermes_skill.py fails while the two differ.
set -euo pipefail
here="$(cd "$(dirname "$0")" && pwd)"
src="$here/../../.claude/skills/kubesight"
dest="$here/files/hermes-skills/kubesight"
[ -f "$src/SKILL.md" ] || { echo "No skill at $src" >&2; exit 1; }
rm -rf "${dest:?}"
mkdir -p "$dest"
cp -r "$src/." "$dest/"
echo "copied $(find "$dest" -type f | wc -l | tr -d ' ') files into $dest"
