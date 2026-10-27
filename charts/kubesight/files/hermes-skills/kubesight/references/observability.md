# Observability — logs, alerts, and who did what

## Logs

```
kubesight_pods_for_logs {cluster, namespace}                     ← names and containers
kubesight_pod_logs {cluster, namespace, pod, tail: 200}
kubesight_pod_logs {cluster, namespace, pod, contains: "exception", sinceSeconds: 3600}
kubesight_pod_logs {cluster, namespace, pod, previous: true}
```

**Get the pod name from `kubesight_pods_for_logs`.** Pod names carry a generated
suffix; a guessed one is a wasted call, and the listing also tells you the
container names and which pods this token may read at all.

**`contains` is the parameter that matters.** It filters *before* trimming, so a
narrow filter over a long window finds the error that a plain tail scrolled
past. Pulling 400 lines and searching them yourself spends context to do what an
argument does for nothing. It is case-insensitive, and the answer reports
`matchedLines` so you can say how many hits there were, not just show some.

**`previous: true` reads the last crashed instance.** This is where a
CrashLoopBackOff explains itself — the current container is too young to have
logged anything, and the reason is in the one before it. Reaching for a normal
tail on a crash-looping pod and reporting "the logs are empty" is the classic
mistake here.

`sinceSeconds` accepts exactly `900`, `3600`, `21600` or `86400`. Anything else
is refused.

Values are masked **before** they are stored, so logs are safe to read in full —
and a masked line is genuinely all there is, not something you can un-mask.

## Alerts

```
kubesight_alerts_list {severity: "critical"}       ← every visible cluster
kubesight_alerts_list {cluster: "prod"}
kubesight_alert_policies_list {cluster: "prod"}
```

Alerts come from two places and arrive as one list: some derived from the
cluster, the rest evaluated from KubeSight's own alert policies. Without a
`cluster` the tool scans every cluster the token can see, which is usually what
"are there any alerts" means.

If a cluster is unreachable, the list carries a synthetic warning row naming it
rather than silently dropping that cluster. Notice those — "no alerts" and "no
alerts *and* one cluster we could not reach" are different answers.

**An alert nobody expected usually has its rule in
`kubesight_alert_policies_list`**: what it watches, its threshold, its `for`
duration, and whether it is currently firing. That is where to look before
speculating about the cluster.

```
kubesight_alert_policy_set_enabled {policyId: 8, enabled: false}
```

Silencing a policy silences it **for everybody, indefinitely** — nothing turns it
back on by itself. If you disable one, say which, say why, and say that somebody
has to re-enable it. Silencing a noisy alert during an incident is reasonable;
silencing it and not saying so is how it stays off for a month.

## The audit trail

```
kubesight_audit_logs {action: "deployment_applied"}
kubesight_audit_logs {actor: "rgeorge", limit: 50}
```

This is how to answer "who changed this". It includes agents: every MCP tool
call is recorded as `mcp_tools_called` with the tools, the domains touched and
which of them wrote — so "did the assistant do this" is a question the audit
trail answers, including about you.

Read-only, and always will be. A record something under audit can edit is not a
record.

Useful action names: `deployment_applied`, `deployment_failed`,
`ci_pipeline_saved`, `alert_policy_toggled`, `helm_upgrade_attempted`,
`mcp_tools_called`, `forbidden_access_attempt`.

## The widest possible start

```
kubesight_dashboard_summary {cluster: "prod"}
```

One call: health, node and pod counts, namespace health, firing alerts, recent
activity. When a question is vague — "how are things looking" — this is the
answer, and everything else is a narrowing of it.
