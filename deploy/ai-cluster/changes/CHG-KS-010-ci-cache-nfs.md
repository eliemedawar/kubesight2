# CHG-KS-010: Put the CI dependency cache on NFS

| Field | Value |
|---|---|
| ID | CHG-KS-010 |
| Modules | kubesight-ci |
| Status | Done 2026-11-06: the cache is in use. Open: the directory is world-writable until the platform lets root change its owner (see Evidence) |
| Asked by | Elie, 2026-11-06: "10.43.11.211:/kubesight … is the nfs that i will be putting caches" |
| Depends on | CHG-KS-002; the OCI File Storage export `10.43.11.211:/kubesight`; the platform applying one PersistentVolume |
| Prepared by | ____ |
| Approved by | ____ (name, date) |
| Run by | ____ (name, date) |

## What

1. **Directory** `ci-cache` in the export, owned by uid 65532 (the user build
   pods run as), mode 2775. Whoever mounts the export as root does it.
2. **PersistentVolume** `kubesight-ci-cache`, applied by the platform (it is
   cluster-wide): NFS `10.43.11.211:/kubesight/ci-cache`, ReadWriteMany,
   100Gi, Retain, reserved for the claim `kubesight-ci/ci-cache`
   ([storage/ci-cache-pv.yaml](../storage/ci-cache-pv.yaml)).
3. **A `helm upgrade`** with `ci.cache` in `values-ai-cluster.yaml` set to
   `enabled: true`, `size: 100Gi`, `storageClass: "-"`. The chart makes the
   claim `ci-cache` in `kubesight-ci` (no storage class, so it binds to that
   volume) and sets `CI_CACHE_CLAIM_NAME=ci-cache` on the backend, which
   restarts once.

Every build pod then mounts the claim at `/kubesight-cache`. Each service gets
its own folder there (Maven, Gradle, npm, pip, ...). Builds that used to
download their dependencies every time reuse them.

## Why

Builds download all their dependencies on every run. The cache must be
ReadWriteMany (build pods run on any node, several at once), which OCI block
volumes cannot do. NFS can.

## Before you start

1. The nodes can reach the mount target. The **node** mounts NFS, not the pod,
   so the network rules on `kubesight-ci` play no part. The OCI security
   list or NSG must let the worker nodes' subnet reach `10.43.11.211` on
   TCP 111, 2048, 2049, 2050 and UDP 111, 2048 (File Storage's ports). The
   bastion mounting it does not prove the nodes can.
2. The NFS version the bastion uses: `mount | grep 10.43.11.211` (look for
   `vers=`). If it is not 3, change `mountOptions` in
   `storage/ci-cache-pv.yaml` to match.
3. The quota has room: the claim asks for 100Gi of the `kubesight-ci` quota
   (4 claims, 200Gi). `kubectl -n kubesight-ci describe resourcequota`

## Steps

**1. The directory**, on the machine with the export mounted at `/kubesight`:

```bash
sudo mkdir -p /kubesight/ci-cache
sudo chown 65532:65532 /kubesight/ci-cache
sudo chmod 2775 /kubesight/ci-cache
ls -ldn /kubesight/ci-cache        # drwxrwsr-x ... 65532 65532
```

**2. The volume**, by the platform:

```bash
kubectl apply -f deploy/ai-cluster/storage/ci-cache-pv.yaml
kubectl get pv kubesight-ci-cache          # Available (Bound after step 3)
```

**3. The upgrade.** On the bastion: pull in `~/kubesight-ai`, copy
`charts/kubesight` and `deploy/ai-cluster` to `~/kubesight`, then in
`~/kubesight`:

```bash
CHG=CHG-KS-010
TAG=$(kubectl -n kubesight get deploy kubesight-backend -o jsonpath='{.spec.template.spec.containers[0].image}' | sed 's/.*://'); echo "TAG=$TAG"
ARGS="-n kubesight -f deploy/ai-cluster/values-ai-cluster.yaml --set image.tag=$TAG --set-string commonLabels.areeba\.com/change=$CHG"

# Read only
helm template kubesight charts/kubesight $ARGS | grep -B4 -A14 'name: ci-cache$'   # the claim: ReadWriteMany, 100Gi, storageClassName ""
helm template kubesight charts/kubesight $ARGS | grep CI_CACHE_CLAIM_NAME           # "ci-cache"
helm template kubesight charts/kubesight $ARGS | grep -c 'kind: PersistentVolume$'  # 0: the chart makes no volume
helm upgrade kubesight charts/kubesight $ARGS --dry-run=server > /dev/null && echo "helm dry run OK"

# After a person's explicit "yes":
helm upgrade kubesight charts/kubesight $ARGS
kubectl -n kubesight-ci get pvc ci-cache                 # Bound, VOLUME kubesight-ci-cache
kubectl -n kubesight rollout status deploy/kubesight-backend
```

## Check

Start one build in Build Center. While it runs:

```bash
kubectl -n kubesight-ci get pods -o wide                                  # the build pod Running, not ContainerCreating
kubectl -n kubesight-ci describe pod <build pod> | grep -A3 kubesight-cache
```

After it, `/kubesight/ci-cache` on the bastion holds a folder for that
service, owned by 65532. The service's second build is faster.

- Pod stuck in `ContainerCreating` with `mount failed ... timed out`: the
  nodes cannot reach the mount target (Before you start, point 1).
- `Permission denied` under `/kubesight-cache` in the build log: step 1
  (owner 65532, mode 2775).
- Claim `Pending`: the volume is missing, or its size, access mode or
  `claimRef` does not match.

## Undo

Set `ci.cache` back to `enabled: false`, then the same upgrade: builds stop
using the cache and download again. The claim and the volume stay (both are
kept on purpose, they hold data). To remove them as well:
`kubectl -n kubesight-ci delete pvc ci-cache`, then the platform runs
`kubectl delete pv kubesight-ci-cache`. The files stay on the export.

## Evidence

2026-11-06, the export, from the bastion:

```
10.43.11.211:/kubesight               8.0E     0  8.0E   0% /kubesight
```

2026-11-06, the claim, after the upgrade:

```
$ kubectl -n kubesight-ci get pvc ci-cache
NAME       STATUS   VOLUME               CAPACITY   ACCESS MODES   STORAGECLASS   VOLUMEATTRIBUTESCLASS   AGE
ci-cache   Bound    kubesight-ci-cache   100Gi      RWX                           <unset>                 13s
```

2026-11-06, step 1: the export **squashes root** (root on the bastion writes as
65534), so `chown` is refused even as root. The directory's squashed owner
opened it to everyone instead, for now:

```
# chown 65532:65532 /kubesight/ci-cache
chown: changing ownership of '/kubesight/ci-cache': Operation not permitted
# chmod 2777 /kubesight/ci-cache
# ls -ldn /kubesight/ci-cache
drwxrwsrwx 2 65534 10000 0 Nov  6 10:24 /kubesight/ci-cache
```

**Open:** writable by any user of any host that mounts the export. To close
it, the platform sets Squash to None for the bastion's address on the
export for a moment, then `chown 65532:65532 /kubesight/ci-cache && chmod
2775 /kubesight/ci-cache`, then root squash goes back on (builds run as
65532, so root squash does not touch them).

2026-11-06, after the first build of the service `kubesight`, from the bastion:

```
$ ls -ln /kubesight/ci-cache
drwxr-sr-x 7 65532 10000 5 Nov  6 13:04 kubesight
drwxr-sr-x 5 65532 10000 3 Nov  6 13:04 _shared
$ du -sh /kubesight/ci-cache/*
47M     /kubesight/ci-cache/kubesight
16M     /kubesight/ci-cache/_shared
```

The build pod mounted the claim, and wrote the service's folder and the
shared one as 65532.
