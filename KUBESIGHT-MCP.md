# KubeSight as an MCP server

Lets an agent — Hermes, Claude Code, anything that speaks MCP — ask KubeSight
questions directly. "Which services are failing?" "Why did build 214 stop?"
"What does the issuing pipeline actually run?"

**Read-only.** Ten tools, all of which answer and none of which change anything.
Triggering builds and editing pipelines stay on the ordinary API behind the
ordinary UI, with a person pressing the button.

---

## Connect it

### 1. Make a token

The MCP server has no identity of its own. It acts as whoever the token belongs
to, under the same RBAC as every other route — so an agent sees exactly what
that person sees, and no more.

Administration → API Tokens → Create a token. It is shown once; only its
prefix is stored afterwards.

Make it from a user that holds only what the tools read, rather than from an
admin account — the `viewer` role covers nine of the ten tools as shipped:

| Permission | Unlocks |
|---|---|
| `ci_services:view` | overview, services, service |
| `ci_pipelines:view` | pipeline, build environments |
| `ci_builds:view` | builds, build, logs |
| `ci_artifacts:view` | artifacts |
| `ci_runners:view` | runners — **not in `viewer`; add it** |

`ci_runners:view` is worth adding deliberately: "no runner is online" is the
single most common reason a build sits queued, and without that permission the
agent cannot see it and will guess at something else instead.

### 2. Point Hermes at it

`~/.hermes/config.yaml`:

```yaml
mcp_servers:
  kubesight:
    url: "http://backend-service.kubesight.svc.cluster.local:5000/api/mcp"
    headers:
      Authorization: "Bearer ksa_your_token_here"
    timeout: 120
```

Local development, with Hermes in its compose network:

```yaml
mcp_servers:
  kubesight:
    url: "http://host.docker.internal:5000/api/mcp"
    headers:
      Authorization: "Bearer ksa_your_token_here"
```

Hermes discovers the tools on connect and registers them like any built-in.

### 3. Or Claude Code

```json
{
  "mcpServers": {
    "kubesight": {
      "url": "http://127.0.0.1:5000/api/mcp",
      "headers": { "Authorization": "Bearer ksa_your_token_here" }
    }
  }
}
```

The `kubesight` skill in `.claude/skills/kubesight/` teaches an agent how to use
these tools well — which one to reach for, and how to read the answers.

### 4. Check it

```bash
curl -s http://127.0.0.1:5000/api/mcp | jq          # no token needed
curl -s -X POST http://127.0.0.1:5000/api/mcp \
  -H "Authorization: Bearer ksa_..." -H "Content-Type: application/json" \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/list"}' | jq '.result.tools[].name'
```

---

## The tools

| Tool | Answers |
|---|---|
| `kubesight_overview` | The build floor at a glance. **Start here.** Includes whether any runner is online — the usual reason a build sits queued. |
| `kubesight_services_list` | Find services by search, status or application type. |
| `kubesight_service_get` | One service in full, including `blockedReason` — why it cannot build, in KubeSight's own words. |
| `kubesight_pipeline_get` | Every stage in order: commands, image, runner, artifacts, secret names. |
| `kubesight_builds_list` | Build history, filterable by service and status. |
| `kubesight_build_get` | One build, with `failedStages` called out directly. |
| `kubesight_build_logs` | A stage's output. Safe to read in full — values are masked before storage. |
| `kubesight_runners_list` | The fleet and what each machine can run. |
| `kubesight_build_environments` | The approved build images and what each provides. |
| `kubesight_artifacts_list` | What a service has produced. |

---

## How it is built

Streamable HTTP, served by the Flask backend at `/api/mcp`. One blueprint, no
new process, no new dependency — the server half of the protocol is a handful of
JSON-RPC methods over a POST (`initialize`, `tools/list`, `tools/call`, `ping`),
so the `mcp` SDK would have cost a production dependency to save eighty lines.

A stdio server was the alternative and was rejected: Hermes runs in its own
container, so a stdio server would have to be packaged into that image and kept
in step with the backend it reads from.

```
backend/api/mcp/protocol.py   JSON-RPC, version negotiation, notifications
backend/api/mcp/tools.py      the ten tools, each declaring its permission
backend/api/routes/mcp.py     the HTTP endpoint and its auditing
backend/tests/test_mcp_server.py
.claude/skills/kubesight/     how an agent should use them
```

## The boundaries, and why they are where they are

**Read-only, structurally.** `tools.py` imports serializers and query helpers
and nothing that mutates. A write tool added later would inherit the trust this
surface was granted on the strength of being read-only, so writes belong behind
a separate decision.

**No MCP identity.** There is no service account and no bypass. Every tool
checks its own permission against the calling token's user through the same
access engine the HTTP routes use. An agent holding a viewer's token is a
viewer. Giving this its own identity would have created a second, invisible
permission system.

**Secret values are unreachable**, including by an agent holding an admin token.
A pipeline stores references; `kubesight_pipeline_get` returns names. Log output
is masked before it is written, not on the way out.

**Tool calls are audited, answers are not.** `mcp_tools_called` records which
tools ran and for whom. Arguments and results are deliberately not recorded — a
service slug is harmless, but the habit of logging tool arguments is how
something sensitive eventually ends up in an audit row.
