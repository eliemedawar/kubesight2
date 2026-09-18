# Clusters — where things run

Everything in this domain reads. Nothing here changes a cluster: adding,
editing or deleting a cluster connection is a person's job in the Clusters tab,
because it is the one action that can make every other tool answer about the
wrong place.

## Naming a cluster

Every cluster tool takes `cluster` as **an id or a display name**. Use the word
the person used. If it is neither, the refusal lists the clusters this token can
see — read that rather than guessing again.

`kubesight_clusters_list` is the orientation call, and it is already filtered to
what the token may reach. A cluster that is not in that list is not a cluster
this agent can answer about.

## The drill-down

```
kubesight_clusters_list                                      → which clusters
kubesight_cluster_overview   {cluster}                       → health, nodes, capacity
kubesight_pod_issues         {cluster}                       → everything broken, one call
kubesight_namespaces_list    {cluster, search: "pay"}        → find the namespace
kubesight_namespace_resources {cluster, namespace, kind: "pods"}
kubesight_resource_get       {cluster, namespace, kind: "pod", name, as: "describe"}
kubesight_namespace_events   {cluster, namespace, kind: "Pod", name}
```

**`kubesight_pod_issues` is the tool to reach for first** when the question is
"is anything wrong". It scans every namespace the token can see in one call and
returns only pods with a problem status — CrashLoopBackOff, ImagePullBackOff,
Pending, Evicted. Walking namespaces one at a time to find the same thing costs
a dozen calls and misses the ones you did not think to check.

## Reading a resource

`kubesight_namespace_resources` without a `kind` returns **every** kind at once.
That is a lot, and it is rarely what you want — name the kind:

`pods`, `deployments`, `replicasets`, `statefulsets`, `daemonsets`, `jobs`,
`cronjobs`, `services`, `configmaps`, `secrets`, `ingress`.

Secrets come back as names and types only. Values are never returned by
anything, and no amount of asking differently changes that.

`kubesight_resource_get` has two modes and they answer different questions:

- **`as: "describe"`** — the default, and what you want for a pod that will not
  start. It carries container statuses, restart counts, the last termination
  reason, and the recent events inline.
- **`as: "yaml"`** — the spec as applied. What you want to see an env var, a
  probe, a resource limit or a volume mount as it actually is.

## Events

`kubesight_namespace_events` is where the *reason* lives. Describe output tells
you a pod is Pending; the events tell you it is Pending because no node has
enough memory, or because a PVC will not bind, or because the image pull needs a
secret that is not there.

Narrow to one object with `kind` and `name` when you have one — a busy namespace
produces a lot of events and almost none of them are about the pod you are
looking at.

## Diagnosis recipes

**A pod will not start.**
`kubesight_resource_get {as: "describe"}` → read the container status and the
last termination reason → `kubesight_namespace_events {kind: "Pod", name}` for
the scheduler's side of it. If the reason is `ImagePullBackOff`, jump to
`kubesight_image_check` in [platform.md](platform.md) — the image usually does
not exist, and no amount of cluster investigation will say so.

**A PVC is Pending.**
`kubesight_storage_classes {cluster}`. A cluster with no default StorageClass
leaves every unqualified claim unbound forever, and nothing in the PVC's own
description says that clearly.

**A node is suspected.**
`kubesight_cluster_nodes {cluster}` → status, role, version, capacity. A node
that is not Ready explains every pod that will not schedule onto it at once.

**"What talks to what?"**
`kubesight_topology {cluster}` for the cluster level — namespaces and nodes as
hubs. `kubesight_topology {cluster, namespace}` for the real
`Ingress → Service → Pod` edges inside one namespace.

A topology answer may carry `warnings` and `partial: true`. That means one API
group could not be read and the graph is missing that piece — say so rather than
reporting the graph as complete, because a missing piece reads exactly like a
piece that is not there.

## Access, and what it hides

Permission is not the same as access. `resources:view` says a token may read
resources; the access engine says *which clusters and namespaces*. Both are
checked, and the payloads are filtered afterwards by the same rules the UI uses.

The practical consequence: a listing that comes back short may be short because
the token is scoped, not because the namespace is empty. If a count looks wrong
against what somebody expects, that is the first thing to say.
