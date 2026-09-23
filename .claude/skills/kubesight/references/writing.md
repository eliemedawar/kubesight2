# Writing — before you change anything

Read this once, before your first write in a conversation. It applies to all
twenty-three write tools regardless of domain.

## Say it, then do it

Before a write, in one sentence: **what you are changing, where, and why.**

> Scaling `payment-worker` in `prod-us-east/payments` from 2 to 4 replicas,
> because the queue depth alert has been firing for an hour.

Then call the tool. Not a paragraph, not a request for permission on something
you were just asked to do — one line, so that somebody reading along can object
in the second before it happens. The cost of the line is nothing; the cost of a
surprise change in production is the trust to make the next one.

Two cases where you stop and ask instead of announcing:

- **`kubesight_helm_uninstall`** — it deletes everything the chart created,
  PersistentVolumeClaims included, and a rollback does not bring those back.
- **`kubesight_pod_exec`** — say the exact command, not what it is for.

## Know whether a gate applies

The gates live inside the services these tools call — the same services the UI
posts to — so nothing here can route around one. What you can do is find out
first, so a refusal is something you predicted rather than something that
happened to you:

| Before | Check |
|---|---|
| `kubesight_deploy_apply`, `kubesight_automation_run_start` | `kubesight_deploy_eligibility {cluster}` |
| `kubesight_helm_upgrade` | the confirmation phrase — see [deploys.md](deploys.md) |
| anything, on a token you have not written with yet | the tool is in `tools/list`, or the permission is not granted |

A refusal from a gate is a **policy working**. Report it as one: "prod requires
an approved deployment request and there isn't a live one — want me to raise
it?" is right. "The deploy failed" is not.

## Claim only what happened

This is the failure mode that actually causes damage, because it is the one
nobody catches until later.

| The tool returned | So say |
|---|---|
| a queued build | "queued build #215" — **not** "the build passed" |
| an applied manifest | "applied" — **not** "deployed and healthy" |
| a cancelled build | "cancellation requested; it may read running briefly" |
| a saved pipeline | "saved; it takes effect on the next build" |
| a cancelled automation run | "stopped — what it already applied is still applied" |

Every write tool returns a `changed` line saying what it did. That line is the
honest version of the answer. If you want to claim more than it says — that a
rollout is healthy, that a build passed — go and check, with
`kubesight_rollout_history`, `kubesight_inventory_list` or
`kubesight_build_get`, and say what you actually saw.

## When a write is refused

The message is the validator's own, and it names the thing. Read it and fix
what it says rather than trying a different tool — a different tool will hit the
same validator.

- **A permission error names the key.** Say which permission, and check
  `kubesight_roles_list` for which role grants it. See
  [platform.md](platform.md).
- **A validation error names the field or the stage.** Fix it and try once more.
  Twice is a pattern; stop and explain.
- **Some refusals are not yours to fix.** A CI pipeline referencing a secret
  that has since been deleted blocks every edit to that pipeline, including ones
  that never touched the secret. Relay the message and ask for the secret back.

## What no token can do here

Not a permission problem — these tools do not exist:

- **Approve** a deployment request or a change bundle. You may create a request;
  a person votes. An agent that can both ask and approve is an approval process
  with one participant.
- **Delete** a workload, a service, a cluster connection or a user.
- **Commit to a repository.** You can edit the Dockerfile KubeSight *stores* for
  a service; the file in the repository is read-only from here, and there is no
  tool that opens a pull request.
- **Read or write a secret's value.** Pipelines hold references; values are
  stored encrypted and write-only.
- **Edit platform configuration** — registries, ticketing field mappings, roles.
  Propose the change and say where in the UI it lives.

When somebody asks for one of these, say plainly that it is not something you
can do and name where it is done: the Builds tab, Settings, the Runners dialog,
the Clusters tab. That is more useful than a refusal on its own.
