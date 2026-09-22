---
name: kubesight
description: Answer questions about a KubeSight installation and act on it — clusters, namespaces, pods and their logs; the CI catalog, pipelines, builds and why one failed; what is deployed and at what version; deploys, Helm releases and the approvals in the way; alerts, the audit trail, application analysis, registries, ticketing automation and mobile releases. Use whenever someone asks about a cluster, a namespace, a pod, a build, a pipeline, a runner, a deploy, a Helm release, an alert, an inventory version, or wants something restarted, scaled, rolled back, built or deployed. Requires the KubeSight MCP server to be connected.
---

# Working with KubeSight

KubeSight is a Kubernetes control plane with about eighty MCP tools, grouped
into seven domains. **Read the one reference file for the domain the question
lands in — not all of them.** Each is self-contained; loading the wrong one
costs a page and loading all seven costs the answer.

## Route first

| The question is about… | Read |
|---|---|
| a build, a pipeline, a runner, a service's source | [references/ci.md](references/ci.md) |
| a pull request that was blocked, the quality gate | [references/ci.md](references/ci.md) |
| a cluster, a namespace, a pod's state, events, topology | [references/clusters.md](references/clusters.md) |
| what is running and what version; restart, scale, roll back, exec | [references/workloads.md](references/workloads.md) |
| deploying, Helm, approvals, change bundles | [references/deploys.md](references/deploys.md) |
| pod logs, alerts, who did what | [references/observability.md](references/observability.md) |
| what an application *is* — analysis, services, clients | [references/apps.md](references/apps.md) |
| registries, tickets, mobile releases, users and roles | [references/platform.md](references/platform.md) |
| **anything you are about to change** | also [references/writing.md](references/writing.md) |

Two questions are common enough to answer here:

- **"Why did build N fail?"** → `kubesight_build_failure {buildId: N}` in one
  call: it finds the failed stage and returns the tail of its log. Give it
  `{service: "payment"}` instead to mean the last failed build. Use
  `kubesight_build_logs` when you need a specific stage in full. Read
  [ci.md](references/ci.md) if the answer is not in the log.
- **"Why was my pull request blocked?"** → `kubesight_merge_checks_status`
  then `kubesight_merge_checks_history {service, pullRequest}`. Check
  `enforcement` before saying a branch is protected — a gate that reports and
  does not block looks the same from inside KubeSight.
- **"Is anything broken?"** → `kubesight_pod_issues {cluster}` for a cluster,
  `kubesight_overview` for CI. Both are one call and both are cheap.

If you genuinely cannot tell which domain a question is in, `tools/list` carries
`annotations.kubesightDomain` on every tool.

## What holds everywhere

**You are the token, and only the token.** There is no KubeSight identity behind
these tools. Every call runs as the user whose API token the server was given,
under the same RBAC as the UI — and `tools/list` already hides what that token
cannot call. So a tool that is missing is a permission that was not granted, and
a permission error names the exact key:

> `'X' needs the 'Y' permission, which this token does not have.`

Say which permission. That sentence is actionable; "I don't have access" is not.

**A write goes through the same service the UI posts to.** That is what makes
"full access" safe to hand over: the gates are inside those services, not in
this server, so nothing here can route around one. In particular —

- A cluster configured to require approvals refuses a deploy without a live
  approved request. Check `kubesight_deploy_eligibility` **before** deploying.
- A Helm install or upgrade needs its exact confirmation phrase.
- A change bundle runs when an approver votes, never when you submit it.

**You cannot approve anything.** There is no tool that votes on a deployment
request or a change bundle, deliberately, even for a token that holds the
managing permission. You can ask; a person decides.

## Answering well

- **Quote, don't paraphrase.** `blockedReason`, a log line and a validator's
  refusal are already written for a person. Repeating them verbatim beats
  summarising them.
- **Name the thing.** "Build 214 failed at Unit Tests", "3 of 5 replicas ready in
  prod-us-east/payments". A status with no number goes stale between the
  question and the answer.
- **Do not invent causes.** If the logs do not say why, say the logs do not say
  why and name the stage or the pod. A plausible guess is worse than a gap,
  because it gets acted on.
- **Claim only what you did.** Queuing a build is not finishing one; applying a
  manifest is not verifying a rollout. Say what returned, then say what you
  would check next.
- **Say what you are about to change, and where, before you change it.** Then do
  it. See [writing.md](references/writing.md) — read it once before your first
  write in a conversation.

## What you can never see

Secret values (stored encrypted and write-only — even the pipeline holds only
names), artifact file contents, and anything outside the token's permissions.
Log tools are safe to read in full because values are masked *before* they are
stored — which also means a masked line is genuinely all there is, not something
you can un-mask.
