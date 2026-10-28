# Hermes (KubeSight agent)

Hermes is the AI agent behind three KubeSight features:
- **Application Intelligence:** source analysis of a service's repository;
- **the ticket agent:** it handles Zoho and Jira tickets;
- **CI assist:** it writes and repairs pipelines.

KubeSight sends it requests over an OpenAI-style HTTP API. It reaches back into
KubeSight through KubeSight's MCP server, with its own token. On the AI cluster
it calls the Claude API directly, until AI Guard runs.

| | |
|---|---|
| Owner | DevOps team |
| Purpose | Run KubeSight's agent work on Claude, using KubeSight's MCP tools |
| Version | `registry.areeba.com/kubesight-hermes:v2026.7.20`; model `claude-opus-5-5` |
| Status | Planned: added by CHG-KS-003, waiting for a Claude API key (KubeSight itself is installed by CHG-KS-002 without Hermes) |
| Namespace | `kubesight` |
| Runs as | Deployment `kubesight-hermes`, 1 replica (strategy `Recreate`) |
| Depends on | `kubesight-backend` (MCP server at `backend-service:5000/api/mcp`); the Claude API (`api.anthropic.com`) |
| Used by | `kubesight-backend` (ticket agent, CI assist); analysis Jobs in `kubesight-ci` |
| Secrets | `hermes` (from `kv/kubesight/hermes`: `ANTHROPIC_API_KEY`); `kubesight-secrets` (from `kv/kubesight/backend`: `HERMES_API_TOKEN`, `KUBESIGHT_HERMES_MCP_TOKEN`); `regcred` |
| Change records | [CHG-KS-001](../../../changes/CHG-KS-001-openbao-secrets.md) (secrets), [CHG-KS-002](../../../changes/CHG-KS-002-install.md) (install, without Hermes), [CHG-KS-003](../../../changes/CHG-KS-003-hermes.md) (Hermes) |
| Signed | ____ (fill in your name) |

## Documents

- [Tutorial](tutorial.md): a first look at the running agent, in about 15 minutes.
- [How-to guides](how-to.md): rotate the key, change the model, open a host, reissue the MCP token, check the network.
- [Explanation](explanation.md): why it calls Claude directly, and why its egress is by name.
- [Reference](reference.md): objects, ports, settings, secrets, network rules and labels.
