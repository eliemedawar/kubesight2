# CHG-KS-018: BuildKit cache on its own volume, with clean-up limits

| Field | Value |
|---|---|
| ID | CHG-KS-018 |
| Modules | kubesight-buildkit |
| Status | Proposed |
| Asked by | Elie, 2026-11-11: a builder evicted ("Usage of EmptyDir volume "cache" exceeds the limit "10Gi""), "buildkit why?", "fix it" |
| Depends on | CHG-KS-017 (same image, `main-17`; this change is chart only) |
| Prepared by | ____ |
| Approved by | ____ (name, date) |
| Run by | ____ (name, date) |

## What

On 2026-11-11 the kubelet evicted a `buildkitd` pod in `kubesight-build`:

```
Warning  Evicted  kubelet  Usage of EmptyDir volume "cache" exceeds the limit "10Gi".
Normal   Killing  kubelet  Stopping container buildkitd
```

The build running on it died, and the replacement started with an empty
cache. The chart gave BuildKit no `buildkitd.toml`. Without one, BuildKit sizes
its cache clean-up from the disk under the cache, the node's whole disk, not
the 10Gi `emptyDir` limit, so it kept caching until the kubelet threw it out.

1. **Clean-up limits** (all installs): ConfigMap `buildkitd-config` with
   `buildkitd.toml`, from the new `buildkit.gc` values. Chart default (10Gi
   `emptyDir`): keep 2GB, trim above 6GB. The pods restart when it changes.
2. **A volume per builder** (`buildkit.persistence`, on here): `buildkitd`
   becomes a StatefulSet with one 50Gi `oci-bv` claim per builder
   (`cache-buildkitd-0`, `cache-buildkitd-1`). This is OCI's smallest block
   volume, and the two use exactly the namespace's 2 claims and 100Gi. Here:
   keep 5GB, trim above 35GB, keep 10GB free. A volume is not node scratch
   space, so the kubelet never evicts a builder over its cache, and the cache
   survives restarts.

Nothing else changes: the same `buildkitd` and `buildkitd-pods` Services, the
same addresses for build pods (`CI_BUILDKIT_ADDR`, `CI_BUILDKIT_POOL`), the
same network rules, the same image.

## Know before approving

- **Quota.** The namespace allows 1 CPU of requests and 2 claims. The old
  Deployment's two pods fill the requests, so it is scaled to 0 first (step 3).
  No image build can run for the minute or two the builders are swapped.
- **A volume lives in one availability domain.** A builder whose node goes is
  rescheduled only in its volume's AD. Until then, builds use the other
  builder (failover through `CI_BUILDKIT_POOL`).
- **`fsGroup: 1000`** is new on the builder pods, so uid 1000 can write the new
  volume. The platform's admission fence must allow it: the server dry run in
  step 2 shows a refusal.
- **Clean-up runs between builds.** One build that writes more than about
  15GB on top of a full cache can still fill the volume. That fails the build,
  but the builder is not evicted.

## Steps

On the bastion:

```bash
# 1. The chart and values from Bitbucket
cd ~/kubesight-ai && git pull && git log --oneline -1
cp -r ~/kubesight-ai/charts/kubesight ~/kubesight/charts/
cp -r ~/kubesight-ai/deploy/ai-cluster ~/kubesight/deploy/
cd ~/kubesight

# 2. Read only
kubectl get storageclass oci-bv                          # must exist (else see Undo/notes)
kubectl -n kubesight-build describe resourcequota        # persistentvolumeclaims 0 used of 2
CHG=CHG-KS-018
TAG=main-17
ARGS="-n kubesight -f deploy/ai-cluster/values-ai-cluster.yaml --set image.tag=$TAG --set-string commonLabels.areeba\.com/change=$CHG"
helm template kubesight charts/kubesight $ARGS --show-only templates/buildkit.yaml | grep -E "^kind:|storage: 50Gi|Space|fsGroup"
helm upgrade kubesight charts/kubesight $ARGS --dry-run=server > /dev/null && echo "helm dry run OK"

# 3. After a person's explicit "yes", and with no image build running:
kubectl -n kubesight-build scale deploy/buildkitd --replicas=0
helm upgrade kubesight charts/kubesight $ARGS
kubectl -n kubesight-build rollout status statefulset/buildkitd
kubectl -n kubesight-build get pods,pvc -o wide
```

Expected from `helm template`: `kind: StatefulSet`, `storage: 50Gi`,
`reservedSpace = "5GB"`, `maxUsedSpace = "35GB"`, `minFreeSpace = "10GB"` and
`fsGroup: 1000`.

If `oci-bv` does not exist, set `buildkit.persistence.storageClass` to the
cluster's delete-on-release block-volume class. `oci-bv-retain` works too,
but its volumes stay behind after their claims are deleted.

## Check

```bash
kubectl -n kubesight-build get pvc                                       # 2 x Bound 50Gi
kubectl -n kubesight-build exec buildkitd-0 -- cat /home/user/.config/buildkit/buildkitd.toml
kubectl -n kubesight-build exec buildkitd-0 -- df -h /home/user/.local/share/buildkit   # ~49G, the volume
kubectl -n kubesight-build exec buildkitd-0 -- buildctl --addr tcp://127.0.0.1:1234 debug workers -v | grep -i -A4 gc
kubectl -n kubesight-build get deploy                                    # buildkitd gone
```

Then run an image build in KubeSight CI. It passes, and a second run of the
same build is faster (warm cache).

## Undo

Set `buildkit.persistence.enabled: false` in `values-ai-cluster.yaml`, then:

```bash
kubectl -n kubesight-build scale statefulset/buildkitd --replicas=0
helm upgrade kubesight charts/kubesight $ARGS
```

That brings back the Deployment with the `emptyDir`. The clean-up limits stay,
so it no longer outgrows 10Gi between builds. The claims are deleted with the
StatefulSet.

## Evidence

After: ____
