#!/bin/sh
# ---------------------------------------------------------------------------
# Connect Hermes to KubeSight's MCP server.
#
# Run this ON THE NFS SERVER, in the directory that the Hermes container mounts
# as HERMES_HOME (the one holding config.yaml, skills/, memories/, SOUL.md).
#
# It writes three things and changes nothing else:
#   config.yaml   + an mcp_servers.kubesight block (existing file backed up)
#   .env          + MCP_KUBESIGHT_API_KEY=<token>          (chmod 600)
#   skills/kubesight/SKILL.md                              (how to use the tools)
#
# Two values you must set. The URL is resolved by the HERMES CONTAINER, not by
# this NFS box — check it from inside the container, not from here.
# ---------------------------------------------------------------------------

KUBESIGHT_URL="http://kubesight-backend.kubesight.svc.cluster.local:5000/api/mcp"
KUBESIGHT_TOKEN="ksa_4a28eea8428651db27baecfc869a27d9a2581c39"

# ---------------------------------------------------------------------------

set -eu

if [ "$KUBESIGHT_TOKEN" = "ksa_4a28eea8428651db27baecfc869a27d9a2581c39" ]; then
  echo "Edit KUBESIGHT_TOKEN at the top of this script first." >&2
  echo "Make one in KubeSight: Administration -> API Tokens -> Create a token." >&2
  exit 1
fi

HOME_DIR=$(pwd)

if [ ! -f "$HOME_DIR/config.yaml" ]; then
  echo "No config.yaml here. Run this inside the hermes-data directory." >&2
  exit 1
fi

# The directory is shared over NFS and already carries config.yaml.bak-* files,
# so keep to that convention rather than inventing a new one.
STAMP=$(date +%Y%m%d_%H%M%S)
cp "$HOME_DIR/config.yaml" "$HOME_DIR/config.yaml.bak-$STAMP"
echo "backed up  config.yaml.bak-$STAMP"

if grep -q "^mcp_servers:" "$HOME_DIR/config.yaml"; then
  echo >&2
  echo "config.yaml already has an mcp_servers: block." >&2
  echo "Add this under it by hand rather than letting the script append a second one:" >&2
  echo >&2
  sed 's/^/    /' <<YAML >&2
kubesight:
  url: "$KUBESIGHT_URL"
  headers:
    Authorization: "Bearer \${MCP_KUBESIGHT_API_KEY}"
  timeout: 120
  connect_timeout: 30
YAML
  exit 1
fi

# The token is interpolated from the environment at load time, so the secret
# itself never lands in config.yaml — which matters here, because every edit
# leaves a world-readable .bak copy next to it on the export.
cat >> "$HOME_DIR/config.yaml" <<YAML

# KubeSight. 17 tools: services, pipelines, builds, logs, runners, repository
# source, and pipeline editing. Everything but the four kubesight_pipeline_*
# editing tools only reads; those need ci_pipelines:edit on the token below,
# so a token minted without that permission makes this connection read-only.
mcp_servers:
  kubesight:
    url: "$KUBESIGHT_URL"
    headers:
      Authorization: "Bearer \${MCP_KUBESIGHT_API_KEY}"
    timeout: 120
    connect_timeout: 30
YAML
echo "wrote      config.yaml  (mcp_servers.kubesight)"

touch "$HOME_DIR/.env"
chmod 600 "$HOME_DIR/.env"
if grep -q "^MCP_KUBESIGHT_API_KEY=" "$HOME_DIR/.env"; then
  sed -i "s|^MCP_KUBESIGHT_API_KEY=.*|MCP_KUBESIGHT_API_KEY=$KUBESIGHT_TOKEN|" "$HOME_DIR/.env"
  echo "updated    .env         (MCP_KUBESIGHT_API_KEY)"
else
  echo "MCP_KUBESIGHT_API_KEY=$KUBESIGHT_TOKEN" >> "$HOME_DIR/.env"
  echo "wrote      .env         (MCP_KUBESIGHT_API_KEY, mode 600)"
fi

mkdir -p "$HOME_DIR/skills/kubesight"
echo "made       skills/kubesight/   — copy SKILL.md into it"

echo
echo "Now, in order:"
echo "  1. Copy SKILL.md into $HOME_DIR/skills/kubesight/"
echo "  2. Check ownership matches the uid Hermes runs as:"
echo "       ls -ln $HOME_DIR/config.yaml $HOME_DIR/.env"
echo "  3. From INSIDE the Hermes container, prove it can reach KubeSight:"
echo "       curl -s $KUBESIGHT_URL"
echo "     Expect JSON with \"protocol\":\"mcp\". No token needed for that one."
echo "  4. Restart the Hermes container. MCP servers connect at startup only."
echo "  5. Confirm:  hermes mcp list"
