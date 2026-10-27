# KubeSight agent platform

PydanticAI agents run durably on Temporal. The platform replaces Hermes one flow
at a time (tickets, source analysis, CI assist). KubeSight stays the system of record
and the approval authority. The platform acts only through KubeSight's MCP server, as
the `agent-platform` account, and reports back to the backend.

```
KubeSight backend ──POST /v1/tickets/tasks──▶ API ──start / signal──▶ TicketWorkflow (one per ticket)
                  ──POST /v1/chat/completions▶ API ──start, wait────▶ CompletionWorkflow
                                                                          │ model + MCP calls are activities
KubeSight /api/mcp ◀──────────── worker (ticket agent: kubesight_ticket_* + read-only tools)
KubeSight /api/agent-platform/ticket-tasks/<id>/result ◀── worker (when a ticket task is over)
```

| Piece | File |
|---|---|
| Department file schema, loader | `agent_platform/config.py` |
| Agents: model through the AI proxy, filtered MCP toolset, JSON check | `agent_platform/agents.py` |
| `TicketWorkflow`, `CompletionWorkflow` | `agent_platform/workflows.py` |
| The report back to KubeSight | `agent_platform/activities.py` |
| HTTP API | `agent_platform/api.py` |
| Worker entry point | `agent_platform/worker.py` |
| zlib codec (analysis evidence exceeds Temporal's 2 MB payload limit) | `agent_platform/codec.py` |
| The infra department and the ticket prompt | `departments/infra/` |

Rules that keep running workflows replayable:

- Never rename an agent or an MCP toolset id once deployed.
- Workflow code only shuffles data. Model and MCP calls are activities (TemporalDurability).
- Workflows reach the worker's agents through `agent_platform.runtime`, imported by
  its full name inside `imports_passed_through()`. A relative import gets a fresh copy
  inside the sandbox, and that copy has no department.
- Inputs carry ids and the task KubeSight wrote, never a credential. Tokens are read
  inside activities.

## Run the tests

```bash
pip install -r requirements-dev.txt
python -m pytest tests -q
```

`tests/test_workflows.py` starts a local Temporal dev server. The SDK downloads it the
first time. The model is a scripted `FunctionModel`, and KubeSight's MCP server is a
`FunctionToolset` with the same id.

## Run it locally

```bash
temporal server start-dev --namespace agents          # or any Temporal 1.2x+
export KUBESIGHT_URL=http://127.0.0.1:5000 AGENT_PLATFORM_TOKEN=dev-shared \
       KUBESIGHT_AGENT_TOKEN=ksa_... MODEL_BASE_URL=https://ai-proxy.example.com/v1 \
       MODEL_API_KEY=... AGENTS_CONFIG=departments/infra/agents.yaml
python -m agent_platform.worker &
uvicorn agent_platform.api:app --port 8000
```

On the backend, set `AGENT_PLATFORM_URL=http://127.0.0.1:8000`, `AGENT_PLATFORM_TOKEN`
to the same value, `HERMES_ALLOW_LOCAL_HTTP=true`, and `AGENT_ENGINE_<FLOW>=platform`
for each flow to move. Also set `KUBESIGHT_AGENT_PLATFORM_MCP_TOKEN` to the
`KUBESIGHT_AGENT_TOKEN` above; the backend registers it on boot. Deployment is in
`charts/kubesight/README.md` under "Agent platform".
