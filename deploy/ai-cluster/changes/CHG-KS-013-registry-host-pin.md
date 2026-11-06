# CHG-KS-013: Make registry.areeba.com mean the internal Nexus inside the cluster

| Field | Value |
|---|---|
| ID | CHG-KS-013 |
| Modules | kubesight-buildkit, kubesight-backend |
| Status | Proposed |
| Asked by | Elie, 2026-11-06: an image build failed pulling its base image from `registry.areeba.com` |
| Depends on | CHG-KS-012 (the rules to `10.43.17.16`) |
| Prepared by | ____ |
| Approved by | ____ (name, date) |
| Run by | ____ (name, date) |

## What

A `helm upgrade` with `hostAliases` in `deploy/ai-cluster/values-ai-cluster.yaml`
for `buildkit` and `backend`: `10.43.17.16  registry.areeba.com`. Each pod's
`/etc/hosts` then sends that name to the internal Nexus. Both Deployments roll
(BuildKit one pod at a time; the backend restarts once).

## Why

The first image build on the cluster failed on its `FROM`:

```
#2 [internal] load metadata for registry.areeba.com/adoptopenjdk/openjdk11:jdk-11.0.11_9-alpine-slim
#2 ERROR: failed to do request: Head "https://registry.areeba.com/v2/adoptopenjdk/openjdk11/manifests/jdk-11.0.11_9-alpine-slim":
dial tcp 139.185.49.213:443: i/o timeout
```

Inside the cluster `registry.areeba.com` resolves to the public address
`139.185.49.213`, which does not answer from here. The nodes pull from the
internal Nexus, `10.43.17.16`. BuildKit resolves `FROM` images itself, through
the cluster's DNS, so it gets the public answer.

**The proper fix is the platform's:** have the cluster DNS (CoreDNS, or the
VCN's private zone it forwards to) answer `registry.areeba.com` with
`10.43.17.16`, as the nodes see it. Then every pod is right without pins, and
this change is undone.

## Before you start

Confirm the two answers differ:

```bash
getent hosts registry.areeba.com                                                        # on a node / the bastion
kubectl -n kubesight-build exec deploy/buildkitd -- nslookup registry.areeba.com        # inside the cluster
```

## Steps

On the bastion: pull in `~/kubesight-ai`, copy `charts/kubesight` and
`deploy/ai-cluster` to `~/kubesight`, then in `~/kubesight`:

```bash
CHG=CHG-KS-013
TAG=$(kubectl -n kubesight get deploy kubesight-backend -o jsonpath='{.spec.template.spec.containers[0].image}' | sed 's/.*://'); echo "TAG=$TAG"
ARGS="-n kubesight -f deploy/ai-cluster/values-ai-cluster.yaml --set image.tag=$TAG --set-string commonLabels.areeba\.com/change=$CHG"

# Read only
helm template kubesight charts/kubesight $ARGS | grep -A3 'hostAliases:'      # twice: backend and buildkitd
helm upgrade kubesight charts/kubesight $ARGS --dry-run=server > /dev/null && echo "helm dry run OK"

# After a person's explicit "yes":
helm upgrade kubesight charts/kubesight $ARGS
kubectl -n kubesight-build rollout status deploy/buildkitd
kubectl -n kubesight rollout status deploy/kubesight-backend
```

## Check

```bash
kubectl -n kubesight-build exec deploy/buildkitd -- grep registry.areeba.com /etc/hosts    # 10.43.17.16
```

Run the build again: the `FROM registry.areeba.com/...` step loads its
metadata and the build goes on.

## Undo

Set both `hostAliases` to `[]`, then the same upgrade. Do it once the
platform's DNS answers `10.43.17.16` inside the cluster.

## Evidence

Upgrade: ____
