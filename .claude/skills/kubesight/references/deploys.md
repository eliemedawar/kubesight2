# Deploys — getting something into a cluster, and the approvals in the way

The shape of this domain is **check, preview, then ask**. Skipping the first two
is how you arrive at a refusal you could have predicted and report a rule as a
failure.

## Always start with eligibility

```
kubesight_deploy_eligibility {cluster: "prod"}
```

Returns `requiredApprovals`, `approvalRequired`, `hasActiveApproval`,
`eligible`, and the active request's window if there is one.

Call this before deploying to any cluster you have not deployed to in this
conversation. On a cluster configured to require approvals, `apply` fails
without a live approved request — and that failure is a policy working, not an
error. Saying so up front is the difference between "prod requires an approved
deployment request; want me to raise one?" and "the deploy failed".

Nobody is exempt — not admins, and not you holding an admin's token. The same
rule covers Helm install/upgrade/rollback/uninstall and workload
restart/scale/rollback, so check eligibility before any of those too. You can
never approve your own request: another approver has to vote on it.

## Preview before applying

```
kubesight_deploy_validate {namespace, yaml}            ← parse, no cluster contacted
kubesight_deploy_dry_run  {cluster, namespace, yaml}   ← server-side, nothing persisted
kubesight_deploy_diff     {cluster, namespace, yaml}   ← what would change
```

They catch different things and the order is cheapest-first:

- **validate** parses and reports what kinds the manifest contains and whether
  any is blocked for this token. No cluster involved.
- **dry_run** goes to the API server. This is what catches an invalid field, a
  missing CRD, or an admission webhook that will reject it — things validation
  alone cannot see.
- **diff** is the one worth reading aloud. It is the difference between "this
  updates the image" and "this also drops three environment variables", and the
  second one is the reason to run it.

## Applying

```
kubesight_deploy_apply {cluster, namespace, yaml}
```

Creates the namespace if it is missing, and **verifies every image exists in its
linked registry first** — a manifest referencing an image that was never pushed
is rejected with the image named, before anything touches the cluster.

Applying is not verifying. `apply` returns when kubectl returns, which is before
the rollout finishes. Follow with `kubesight_rollout_history` or
`kubesight_inventory_list` from [workloads.md](workloads.md) and say what you
actually saw, rather than reporting the deploy as done.

## Asking for approval

```
kubesight_deployment_requests_list {status: "pending"}
kubesight_deployment_request_create {
  cluster: "prod",
  message: "Roll payment-api to 4.2.1 — fixes the timeout in PAY-4417",
  windowStart: "2026-09-19T21:00:00Z",
  windowEnd:   "2026-09-19T23:00:00Z",
  timezone:    "Asia/Beirut"
}
```

This emails the approvers and waits. It does not deploy and it does not approve.
Once approved, **the same user** may deploy to that cluster until the window
ends — one approval covers several deploys inside it.

Rules the service enforces, so get them right rather than discovering them:

- The window **start must be in the future**, and the end after the start.
- The message is read by the people voting. Write it for them: what is changing,
  where, and why now. "Deploy" is not a message.

**There is no tool that approves.** That is deliberate: an agent that can both
request and approve is an approval process with one participant. Raise the
request, say who has to vote, and stop.

## Change bundles

```
kubesight_change_bundles_list {status: "pending"}
kubesight_change_bundle_get   {bundleId: 12}
```

A bundle is a batch of changes approved as a unit and executed by KubeSight
afterwards. Read-only here, for the same reason: you can report what is in one
and where it stands; a person votes.

`kubesight_change_bundle_get` re-validates each item against the cluster as it
is *now*, so an item that was fine when it was staged can come back invalid.
That is usually the answer to "why has this bundle not run".

## Helm

```
kubesight_helm_releases    {cluster, namespace}
kubesight_helm_release_get {cluster, namespace, release}     ← values, history, manifest
kubesight_helm_upgrade     {cluster, namespace, release, chartName, confirmation}
kubesight_helm_rollback    {cluster, namespace, release, revision: 4}
kubesight_helm_uninstall   {cluster, namespace, release}
```

**A release stuck in `pending-upgrade` is the most common Helm answer.** It
blocks every subsequent upgrade and it does not clear itself. `helm_releases`
shows it in one call; the fix is a rollback or a person with `helm` on the
command line.

**Install and upgrade need an exact confirmation phrase:**

```
UPGRADE <release> IN <namespace>      ← the release already exists
INSTALL <release> IN <namespace>      ← it does not
```

with the release name **lowercase**. The phrase is not busywork — producing it
means having named the right release in the right namespace, which is the check
it exists for. If you get it wrong, the refusal tells you the exact string;
do not guess a second time, use the one it gave you.

**Uninstall deletes everything the chart created**, and depending on the chart
that includes PersistentVolumeClaims — which a rollback does not bring back.
Confirm with a person before calling it, every time, even when the request
sounded definite.

**Read the release before you change it.** `kubesight_helm_release_get` carries
the values currently applied and the revision history. An upgrade that omits a
value the release already had does not merge it — it drops it.
