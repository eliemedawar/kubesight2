# How-to guides: CI and analysis runs

Every guide that changes the cluster needs its own change record, with the
labels and a person's "yes", before you run it. Settings change through Helm:

```bash
helm upgrade kubesight charts/kubesight -n kubesight -f deploy/ai-cluster/values-ai-cluster.yaml \
  --set image.tag=<tag> --set-string 'commonLabels.areeba\.com/change=<change ID>'
```

Edit `deploy/ai-cluster/values-ai-cluster.yaml` in Git first, never with `--set`
alone, so the file stays the truth.

## The dependency cache on NFS

The cache (Maven, Gradle, npm and so on) is on NFS: OCI File Storage,
`10.43.11.211:/kubesight/ci-cache` (CHG-KS-010). The chart's own NFS mode
(`ci.cache.nfs`) would create a PersistentVolume, which is cluster-wide, so it
is not used. Instead:

- the platform applies the volume, `deploy/ai-cluster/storage/ci-cache-pv.yaml`
  (`kubesight-ci-cache`, reserved for the claim `kubesight-ci/ci-cache`);
- the chart makes the claim, with no storage class so it binds to that volume:
  ```yaml
  ci:
    cache:
      enabled: true
      size: 100Gi
      storageClass: "-"
  ```

The directory must belong to uid 65532, mode 2775 (`mkdir -p
/kubesight/ci-cache && chown 65532:65532 /kubesight/ci-cache && chmod 2775
/kubesight/ci-cache` where the export is mounted as root). Build pods mount the claim at `/kubesight-cache`, one folder per
service.

To turn it off: `ci.cache.enabled: false` and the same upgrade. The claim,
the volume and the files stay.

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
