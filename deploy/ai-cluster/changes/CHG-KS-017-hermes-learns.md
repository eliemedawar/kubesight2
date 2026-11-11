# CHG-KS-017: Hermes remembers and learns from tickets

| Field | Value |
|---|---|
| ID | CHG-KS-017 |
| Modules | hermes, kubesight-backend |
| Status | Proposed |
| Asked by | Elie, 2026-11-11: "i want hermes to use his memory and the skills and everything so he can become more powerfull", "even the one that handles ticket i want him to understand everything so he can be faster and more efficient over time" |
| Depends on | CHG-KS-016; a backend image built from the commit that adds `kubesight_ticket_history` |
| Prepared by | ____ |
| Approved by | ____ (name, date) |
| Run by | ____ (name, date) |

## What

1. **Memory on.** Hermes' built-in toolsets `memory` (long-term notes),
   `session_search` (search its past conversations) and `todo` (a task list
   within one task) join `kubesight` and `skills` on `api_server` (chart
   default), `cli` and `telegram`. `memory` limits raised to 6000 / 2000
   characters (Hermes' defaults are 2200 / 1375). `agent.max_turns` 20 → 30.
   All three are built in: nothing installed at first use, no new egress.
2. **`kubesight_ticket_history`** (new read-only MCP tool, `ticketing:view`):
   past tickets like the one in hand and how each ended: what Hermes
   understood, its diagnosis, the approvers' decisions, the run errors.
3. **Prompts and skill.** The ticket agent looks back before deciding and
   saves verified lessons. The Ctrl+G chat and the KubeSight skill (Telegram)
   say what may and may not be saved.

## Know before approving

- **One memory for everyone.** Hermes keeps one `MEMORY.md` + `USER.md` per
  profile in `/opt/data/memories/`. The ticket agent, every admin's Ctrl+G chat
  and Telegram all read and write the same one. `X-Hermes-Session-Key` does not
  split it (Hermes docs, "Memory is scoped per profile by design").
- **Ticket text reaches it.** The ticket agent reads requester text. The prompt
  says to save only what KubeSight showed it, never what a ticket asks, and
  that memory never overrides the catalog or the approval rules. That is a
  prompt rule, not a hard control: read `/opt/data/memories/MEMORY.md` now and
  then (see Check).
- **No shell.** `terminal`, `file` and `code_execution` stay off: Hermes is a
  KubeSight admin and reads requester text.

## Steps

The image first: tag the commit, wait for the pipeline. On the bastion, in
`~/kubesight`, after copying `charts/kubesight` and `deploy/ai-cluster`:

```bash
CHG=CHG-KS-017
TAG=____            # the new backend image
ARGS="-n kubesight -f deploy/ai-cluster/values-ai-cluster.yaml --set image.tag=$TAG --set-string commonLabels.areeba\.com/change=$CHG"

# Read only
helm template kubesight charts/kubesight $ARGS | grep -A20 'platform_toolsets:'   # memory, session_search, todo x3
helm template kubesight charts/kubesight $ARGS | grep -A5 '^    memory:'           # memory_enabled: true, 6000
helm upgrade kubesight charts/kubesight $ARGS --dry-run=server > /dev/null && echo "helm dry run OK"

# After a person's explicit "yes":
helm upgrade kubesight charts/kubesight $ARGS
kubectl -n kubesight rollout status deploy/kubesight-backend
kubectl -n kubesight rollout status deploy/kubesight-hermes
```

## Check

```bash
POD=$(kubectl -n kubesight get pod -l app.kubernetes.io/name=kubesight-hermes -o jsonpath='{.items[0].metadata.name}')
kubectl -n kubesight logs $POD -c hermes | grep -i -E "toolset|memory"     # no "unknown name(s)"
kubectl -n kubesight exec $POD -c hermes -- hermes mcp test kubesight      # lists kubesight_ticket_history
# What Hermes has saved, any time:
kubectl -n kubesight exec $POD -c hermes -- cat /opt/data/memories/MEMORY.md /opt/data/memories/USER.md
```

In Telegram: `/new`, then "remember that 'payments' means payments-api in UAT".
Then `/new` again and "what does 'payments' mean?" answers from memory. Ctrl+G
in KubeSight answers the same.

## Undo

Put `platform_toolsets` back to `[kubesight, skills]` (chart default and both
lines here) and remove the `memory:` block, then the same upgrade. The notes
stay on the volume: delete `/opt/data/memories/*.md` to forget them. The
history tool is read-only and can stay.

## Evidence

After: ____
