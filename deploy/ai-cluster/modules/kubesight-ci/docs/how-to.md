# How-to guides: CI and analysis runs

Every guide that changes the cluster needs its own change record, with the
labels and a person's "yes", before you run it. Settings change through Helm:

```bash
helm upgrade kubesight charts/kubesight -n kubesight -f deploy/ai-cluster/values-ai-cluster.yaml \
  --set image.tag=<tag> --set-string 'commonLabels.areeba\.com/change=<change ID>'
```

Edit `deploy/ai-cluster/values-ai-cluster.yaml` in Git first, never with `--set`
alone, so the file stays the truth.

## Turn on the dependency cache, once the platform connects NFS

The cache (Maven, Gradle, npm and so on) is off. It needs a ReadWriteMany
volume, and OCI block volumes are ReadWriteOnce only.

The chart's NFS mode (`ci.cache.nfs.server` and `path`) **creates a
PersistentVolume**, which is a cluster-wide object, so don't use it here. Ask
the platform for one of these:

- **a PersistentVolume they make, plus its claim in `kubesight-ci`.** Then set:
  ```yaml
  ci:
    cache:
      enabled: true
      existingClaim: <the claim's name>
  ```
- **a ReadWriteMany StorageClass.** Then set `ci.cache.enabled: true` and
  `ci.cache.storageClass: <class>`. The chart makes the claim `ci-cache`
  (20Gi, ReadWriteMany). It counts against the namespace's 4 claims and 200Gi.

Either way, the export must belong to uid 65532 before the first build
(`chown 65532:65532 <path> && chmod 2775 <path>`, done by whoever runs the
NFS server). Then run the `helm upgrade`, start one build, and check its pod
mounted the claim at `/kubesight-cache`.

## Write stage images in full

CRI-O refuses short names. In each service's pipeline, give every stage image
with its registry:

| Refused | Accepted |
|---|---|
| `maven:3.9` | `docker.io/library/maven:3.9` |
| `node:20` | `docker.io/library/node:20` |
| `gradle:8-jdk17` | `docker.io/library/gradle:8-jdk17` |

A refused image shows as `ErrImagePull` on the build pod:

```bash
kubectl -n kubesight-ci get events --field-selector reason=Failed --sort-by=.lastTimestamp | tail
```

## Let builds reach a repository on another port

Build pods may use 443 out, plus the ports in `ci.extraEgressPorts`
(today `"4443"`, Nexus Maven). Add one, comma-separated, for example
`ci.extraEgressPorts: "4443,8081"`, then run the `helm upgrade`. The
company firewall may also need an opening: ask the platform.

## Change a stage's CPU or memory limit

The defaults are `ci.stage.cpuLimit: "2"` and `ci.stage.memoryLimit: 4Gi`. A
service or a stage can override them in KubeSight's UI.

The namespace allows **16Gi of memory limits** in total. Builds and analysis
Jobs share it, so at 4Gi only about four can run at once; the others wait. Raise
one service's limit in the UI rather than the default. Ask the platform for a
bigger quota if builds queue too long.

## Find and clean a stuck build

KubeSight marks a build failed after 120 minutes without progress
(`ci.staleBuildMinutes`). To look at one:

```bash
kubectl -n kubesight-ci get jobs -l app.kubernetes.io/name=kubesight-ci
kubectl -n kubesight-ci describe job ci-b<build>-<service>
kubectl -n kubesight-ci get events --sort-by=.lastTimestamp | tail -30
```

**Cancel it from KubeSight first**, with **Cancel** in the build drawer. Delete
the Job yourself only if that fails, and under a change record:

```bash
kubectl -n kubesight-ci delete job ci-b<build>-<service>     # its pod, Secret and NetworkPolicy follow
```

## Read an analysis run's logs

```bash
kubectl -n kubesight-ci get jobs,pods -l app.kubernetes.io/name=kubesight-application-analysis
kubectl -n kubesight-ci logs job/ks-app-analysis-<id>-<suffix> --all-containers
```

## Check the network with Hubble

Run this on the Cilium agent of the pod's node (see the Tutorial):

```bash
kubectl -n kube-system exec <cilium pod> -c cilium-agent -- hubble observe --namespace kubesight-ci --verdict DROPPED --last 50
```

| What is dropped | Why |
|---|---|
| A port other than 443 | The port is not in `ci.extraEgressPorts` |
| Port 1234 to `kubesight-build` | `CI_BUILDKIT_NAMESPACE` is wrong |
| Port 5000 to `kubesight` | The `kubesight-backend-callbacks` rule is missing |
