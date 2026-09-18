# Workloads — what is running, and what you may do to it

The inventory is KubeSight's view of a cluster grouped **by application** rather
than by Kubernetes object. "What version of payments is in production" is an
inventory question, not a resource question — the resource tools in
[clusters.md](clusters.md) would make you assemble the answer from deployments
and image tags yourself.

## Reading

```
kubesight_inventory_list {cluster: "prod", search: "payment"}
kubesight_inventory_list {status: "unhealthy"}          ← across every visible cluster
kubesight_inventory_list {imageTag: "4.2.1"}            ← where is this version running
kubesight_inventory_get  {inventoryId: "prod/payments/payment-api"}
```

`kubesight_inventory_list` returns a `summary` alongside the rows — counts by
status across everything that matched, not just the page. Quote the summary when
the question is "how many", and the rows when it is "which".

`inventoryId` comes from the list. Do not construct one; the format is
KubeSight's and a hand-built id gets `Invalid inventory id`.

## Changing

Four writes, all through the same services the UI's buttons call, all needing
`apps:deploy` and namespace access.

```
kubesight_rollout_history  {cluster, namespace, workload}          ← read this first
kubesight_workload_restart {cluster, namespace, workload}
kubesight_workload_scale   {cluster, namespace, workload, replicas: 3}
kubesight_workload_rollback{cluster, namespace, workload}                  ← undo last
kubesight_workload_rollback{cluster, namespace, workload, revision: 7}     ← named revision
kubesight_resource_restart {cluster, namespace, kind: "statefulset", name}
```

Read [writing.md](writing.md) before your first write in a conversation. Then,
specific to these:

**Restart is the safe one.** `rollout restart` replaces pods one at a time under
the deployment's own strategy; nothing about the spec changes. It is the right
answer for a pod holding a stale config, a dead connection pool, or a rotated
secret it never re-read.

**Scale to 0 is an outage.** It stops the application without deleting it, and
to anyone watching a dashboard it looks identical to a crash. If you do it, say
you did it and say how to undo it, in the same message.

**Rollback needs the history first.** "The previous one" is not always the one
that worked — a bad deploy often follows another bad deploy. Read
`kubesight_rollout_history`, name the revision that carried the image you want,
and say which image that is.

**`kubesight_workload_*` is for deployments.** For a pod, statefulset or
daemonset use `kubesight_resource_restart`. Restarting a pod deletes it and lets
its controller recreate it — which means a pod with no controller does not come
back.

## Exec

```
kubesight_pod_exec {cluster, namespace, pod, command: "cat /etc/app/config.yaml"}
kubesight_pod_exec {cluster, namespace, pod, container: "sidecar", command: "nslookup db"}
```

This is the only tool in the whole server marked destructive, and the marking is
honest: the command runs with the container's own privileges and KubeSight
cannot tell what it will do.

- **Keep it to reading.** Check a file, a process, a DNS name, a port, an env
  var. Something that writes belongs in a manifest, where it is reviewable.
- **Say the exact command before you run it.** Not "I'll check the config" —
  the command, so somebody reading along can object before it runs.
- **A multi-container pod needs `container`.** Without it the call fails; with
  the wrong one you read the sidecar's filesystem and conclude the wrong thing.

## What is not here

There is no tool that deletes a workload. Restart, scale and rollback are all
reversible; a deletion leaves nothing to put back, and an audit row does not
undo it. A deletion that genuinely needs to happen goes through a manifest and
the ordinary deploy path in [deploys.md](deploys.md), or through a person.
