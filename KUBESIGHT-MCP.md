# KubeSight as an MCP server

Lets an agent — Hermes, Claude Code, anything that speaks MCP — ask KubeSight
questions and act on the answers. "Which services are failing?" "Why did build
214 stop?" "What version of payments is in prod?" "Roll it back."

**Eighty-five tools across seven domains.** Sixty-four read, twenty-one write. There is no
fixed list of what an agent may change: it may do exactly what its token's
permissions allow, through the same services the UI posts to — so the gates
inside those services still apply, and nothing here can route around one.

---

## Connect it

### 1. Make a token

The MCP server has no identity of its own. It acts as whoever the token belongs
to, under the same RBAC as every other route — so an agent sees and changes
exactly what that person would, and no more. `tools/list` is filtered to that
token, so a permission you do not grant is a tool the agent never sees.

Administration → API Tokens → Create a token. It is shown once; only its prefix
is stored afterwards.

**Mint the token for the job.** This is the whole access control story, so it is
worth a minute:

| You want the agent to… | Give it |
|---|---|
| answer questions and nothing else | the `viewer` role, plus `ci_runners:view` |
| debug CI and fix pipelines | + `ci_pipelines:edit`, `ci_builds:run`, `ci_builds:cancel`, `ci_builds:retry` |
| explain why a pull request was blocked | + `ci_merge_checks:view` |
| move the org-wide quality gate | + `ci_merge_checks:manage` — think before granting this |
| operate workloads | + `apps:deploy` |
| deploy | + `apps:dryrun`, `apps:diff`, and `deployment_requests:request` so it can ask |
| manage Helm | + `helm:upgrade`, `helm:rollback`; add `helm:uninstall` only deliberately |

`ci_merge_checks:manage` deserves its own moment. It is the only permission here
that changes what is *allowed to be merged*, across every service that inherits
the policy — and an agent that can relax a gate to get a merge through defeats
the gate. Grant it when somebody wants an agent to tune the number for them, and
not by default. It still cannot switch a service's checks off or re-send a
verdict; there is no tool for either, deliberately.

`ci_runners:view` is worth adding even to a read-only token: "no runner is
online" is the single most common reason a build sits queued, and without that
permission the agent cannot see it and will guess at something else instead.

Two permissions are worth withholding on purpose:

- **`deployment_requests:manage` / `change_bundles:manage`.** Holding them
  changes nothing here — there is no tool that approves — but a token that holds
  them is a token that would, if one were ever added.
- **`helm:uninstall`.** It deletes PersistentVolumeClaims that a rollback does
  not bring back.

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

Then give it the skill — **the whole directory, not just `SKILL.md`**:

```bash
cp -r .claude/skills/kubesight/. "$HERMES_HOME/skills/kubesight/"
ls "$HERMES_HOME/skills/kubesight/references/"   # expect 8 files
```

`SKILL.md` is a router; the per-domain pages behind it are where the actual
knowledge is. Copying it alone leaves Hermes with a table of contents pointing
at nothing — it follows a link, finds nothing, and answers from the raw tool
descriptions, which is a worse outcome than shipping no skill at all.
`connect-hermes-to-kubesight.sh` prints this step.

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

### 4. Check it

```bash
curl -s http://127.0.0.1:5000/api/mcp | jq          # no token needed
curl -s -X POST http://127.0.0.1:5000/api/mcp \
  -H "Authorization: Bearer ksa_..." -H "Content-Type: application/json" \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/list"}' \
  | jq -r '.result.tools[] | "\(.annotations.kubesightDomain)\t\(.name)"' | sort
```

The domain column is the quickest way to see what a token actually unlocked.

---

## The seven domains

Tools carry their domain in `annotations.kubesightDomain`, and the `kubesight`
skill is split into one reference file per domain — so an agent answering a
question about a build reads a page about builds, not a page about Helm. That
split is the difference between a fast answer and a slow one at this size.

| Domain | Answers | Writes |
|---|---|---|
| **ci** | services, pipelines, builds, build logs, runners, repository source | pipeline edits; run / cancel / retry a build |
| **clusters** | clusters, nodes, namespaces, resources, events, topology | — |
| **workloads** | what is running and at what version | restart, scale, rollback, exec |
| **deploys** | apply, dry run, diff, approvals, change bundles, Helm | apply, request approval, Helm upgrade / rollback / uninstall |
| **observability** | pod logs, alerts, alert policies, audit, dashboard | enable/disable an alert policy |
| **apps** | application analysis, application services, clients, components | — |
| **platform** | registries, ticketing, mobile releases, users, roles | start / cancel a ticket automation run |

---

## The boundaries, and why they are where they are

**Writes are gated by the services, not by this server.** Every write tool calls
the same function the UI's button calls. That is the load-bearing decision: it
means a cluster configured to require an approved deployment request still
refuses a deploy without one, a Helm upgrade still needs its exact confirmation
phrase, and every change still writes the audit row it always did. A gate that
lived in the MCP layer would be a second implementation to keep in step, and
would be wrong the first time somebody changed the first one.

**Approving is not exposed.** `kubesight_deployment_request_create` exists;
nothing votes on it. An agent that can both request and approve a change is an
approval process with one participant. The same goes for change bundles.

**No MCP identity.** There is no service account and no bypass. Every tool
checks its own permission against the calling token's user through the same
access engine the HTTP routes use, and cluster- and namespace-scoped tools check
access as well — because `resources:view` says *what*, not *where*. An agent
holding a viewer's token is a viewer.

**Deletion is absent.** No tool deletes a workload, a service, a cluster
connection or a user. Restart, scale, rollback and pipeline edits are all
reversible; a deletion leaves nothing to put back, and an audit row does not
undo it. `helm:uninstall` is the one exception and it is marked destructive.

**Secret values are unreachable**, including by an admin token. A pipeline
stores references; `kubesight_pipeline_get` returns names. Log output is masked
before it is written, not on the way out.

**Tool calls are audited, answers are not.** `mcp_tools_called` records which
tools ran, which of them wrote, which domains were touched, and for whom.
Arguments and results are deliberately not recorded — a service slug is
harmless, but the habit of logging tool arguments is how something sensitive
eventually ends up in an audit row.

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
backend/api/mcp/protocol.py         JSON-RPC, version negotiation, notifications
backend/api/mcp/tools/registry.py   the table, the permission check, dispatch
backend/api/mcp/tools/common.py     cluster/namespace resolution, service unwrapping
backend/api/mcp/tools/{ci,clusters,workloads,deploys,observability,apps,platform}.py
backend/api/routes/mcp.py           the HTTP endpoint and its auditing
backend/tests/test_mcp_server.py    protocol, boundary, and every read tool called once
.claude/skills/kubesight/           router + one reference file per domain
```

Two tests are worth knowing about before adding a tool, because they will fail
and they are meant to:

- `test_every_tool_declares_honestly_whether_it_writes` enumerates every write
  tool. A new one has to be added to that list deliberately — the list *is* the
  review.
- `test_every_read_tool_actually_runs` calls every read tool once with fixture
  arguments. A new required argument has to be added to `_SMOKE_ARGUMENTS`, or
  a companion test names the tool as untested.

Three more keep the skill and the server in step: every tool the skill names
must exist, every tool must be taught somewhere, and every domain must have a
reference file.
