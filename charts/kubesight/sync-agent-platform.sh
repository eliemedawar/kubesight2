#!/usr/bin/env bash
# Copy the agent platform's department files (agent-platform/departments) into
# the chart, which can only package files inside its own folder. Run it after
# changing a department file or a prompt;
# backend/tests/test_chart_agent_platform.py fails while the two differ.
set -euo pipefail
here="$(cd "$(dirname "$0")" && pwd)"
src="$here/../../agent-platform/departments"
dest="$here/files/agent-platform"
[ -f "$src/infra/agents.yaml" ] || { echo "No departments at $src" >&2; exit 1; }
rm -rf "${dest:?}"
mkdir -p "$dest"
cp -r "$src/." "$dest/"
echo "copied $(find "$dest" -type f | wc -l | tr -d ' ') files into $dest"
