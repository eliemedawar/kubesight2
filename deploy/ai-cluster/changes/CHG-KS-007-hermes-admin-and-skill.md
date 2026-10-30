# CHG-KS-007: Hermes as an admin, with its skill, connected at every start

| Field | Value |
|---|---|
| ID | CHG-KS-007 |
| Modules | hermes, kubesight-backend |
| Status | Proposed |
| Asked by | Elie, 2026-10-30: "i want it to use the hermes-agent but i will make him admin", "make it so hermes agent is admin", and the skill ConfigMap that Hermes "cannot read" |
| Depends on | CHG-KS-006; images `kubesight-backend:1.0.5` and `kubesight-frontend:1.0.5` built by the tag pipeline |
| Prepared by | ____ |
| Approved by | ____ (name, date) |
| Run by | ____ (name, date) |

## What

Three faults seen on 2026-10-30, one upgrade to `1.0.5`:

1. **Hermes had 13 tools, not ~90.** Its MCP token belongs to `hermes-agent`,
   which the backend puts back on its own narrow role (ticket agent and
   analysis, no cluster access) at every start; the UI may not change it.
   New in the backend: `KUBESIGHT_HERMES_AGENT_ROLE`. Set, `hermes-agent` is
   put on that role at every start and the backend no longer cuts it down;
   the role itself is never changed. New chart value `hermes.agentRole`; here
   `admin`. `hermes-agent` stays a service account that cannot sign in.
2. **Hermes never read the KubeSight skill.** The ConfigMap
   `kubesight-hermes-skill` is copied to `/opt/data/skills/kubesight` as
   intended, but Hermes lists skills to the model only when a skills tool
   (`skills_list`, `skill_view`, `skill_manage`) is on, and every platform had
   `[kubesight]` only. Now `[kubesight, skills]` for `api_server` (chart
   default), `cli` and `telegram`.
3. **Telegram had no tools after the CHG-KS-006 upgrade.** Hermes connects to
   the MCP server once, at start: three tries in about 7 seconds, then it
   stops. The backend was restarting at the same time (`Connect call failed
   ... 5000`), so `kubesight` became an "unknown" toolset. Now the
   `stage-hermes-files` step waits for the backend's `/health` (at most 5
   minutes) before Hermes starts.

In `deploy/ai-cluster/values-ai-cluster.yaml`: `hermes.agentRole: admin`;
`platform_toolsets.cli` and `.telegram`: `[kubesight, skills]`.

## What this gives Hermes

Everything an admin can do in KubeSight, on every cluster, through the MCP
server, for anyone who can talk to it: KubeSight itself (the backend, with
`HERMES_API_TOKEN`) and the two Telegram accounts in `TELEGRAM_ALLOWED_USERS`.
Changes to a cluster with approvers (Clusters → Configure recipients) still
wait for another person's approval, Hermes included. The per-tool and
per-cluster switches in Settings → MCP tools still apply.

## Why

Elie wants Hermes to work on the clusters, as in UAT, with its own account
(`hermes-agent`) rather than a person's token.

## Steps

The images first, from a workstation, once the commit is pushed (done by
Claude, 2026-10-30): the tag `1.0.5` on Bitbucket starts the pipeline (tests,
then `kubesight-backend:1.0.5` and `kubesight-frontend:1.0.5`). Wait for it to
finish.

On the bastion: pull in `~/kubesight-ai`, copy `charts/kubesight` and
`deploy/ai-cluster` to `~/kubesight`, then in `~/kubesight`:

```bash
CHG=CHG-KS-007
TAG=1.0.5
ARGS="-n kubesight -f deploy/ai-cluster/values-ai-cluster.yaml --set image.tag=$TAG --set-string commonLabels.areeba\.com/change=$CHG"

# Read only
helm template kubesight charts/kubesight $ARGS | grep KUBESIGHT_HERMES_AGENT_ROLE        # "admin"
helm template kubesight charts/kubesight $ARGS | grep -A10 'platform_toolsets:'          # kubesight, skills x3
helm upgrade kubesight charts/kubesight $ARGS --dry-run=server > /dev/null && echo "helm dry run OK"

# After a person's explicit "yes":
helm upgrade kubesight charts/kubesight $ARGS
kubectl -n kubesight rollout status deploy/kubesight-backend
kubectl -n kubesight rollout status deploy/kubesight-hermes      # waits for the backend first
```

## Check

```bash
POD=$(kubectl -n kubesight get pod -l app.kubernetes.io/name=kubesight-hermes -o jsonpath='{.items[0].metadata.name}')
kubectl -n kubesight logs $POD -c hermes | grep -i -E "mcp|no valid toolsets"   # no "failed", no "unknown name(s): kubesight"
kubectl -n kubesight exec $POD -c hermes -- hermes mcp test kubesight            # ~90 tools, not 13
kubectl -n kubesight exec $POD -c hermes -- ls /opt/data/skills/kubesight        # SKILL.md references
```

In KubeSight: Users → `hermes-agent` shows the role `admin`. In Telegram:
`/new`, then "which skills do you have?" names `kubesight`, and "list the
nodes of ociduai-k8s" answers with the three nodes.

## Undo

Remove `hermes.agentRole` (or set it to `""`), put `platform_toolsets.cli` and
`.telegram` back to `[kubesight]`, then the same upgrade. At the next start
the backend puts `hermes-agent` back on its own narrow role. Going back to
`1.0.3` also undoes it: that image ignores `KUBESIGHT_HERMES_AGENT_ROLE`.

## Evidence

Before, 2026-10-30, from the bastion:

```
$ kubectl -n kubesight logs $POD -c hermes | grep -i -E "mcp|kubesight"
MCP server 'kubesight' initial connection failed (attempt 1/3) ... (attempt 3/3)
Failed to connect to MCP server 'kubesight': ... [Errno 111] Connect call failed ('10.96.121.172', 5000)
platform 'telegram' has no valid toolsets configured (unknown name(s): kubesight) - tools will be unavailable.
$ kubectl -n kubesight exec $POD -c hermes -- hermes mcp test kubesight
✓ Connected (130ms)   ✓ Tools discovered: 13   (tickets, promotion, automation runs)
Telegram: "I don't currently see a KubeSight MCP connection or its tools in this session."
```

After: ____
