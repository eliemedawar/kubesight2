# Explanation: why the builders are built this way

## Builds away from the build pods

A container image stage runs the image build in a long-lived BuildKit daemon,
not in the build pod. The build pod in `kubesight-ci` runs only the client,
`buildctl`, and sends it the context and its registry login. This keeps the
build pods unprivileged: building an image needs user namespaces and mounts,
which an ordinary pod cannot have. It also lets the builders keep a layer
cache between builds.

## Rootless, inside the fence

The platform gives builds their own namespace, `kubesight-build`. Its pod
security level is `privileged`, because rootless BuildKit needs an unconfined
seccomp and AppArmor profile. A platform admission policy (the "fence") then
admits only:

- the image `docker.io/moby/buildkit:<version>-rootless`;
- `runAsUser: 1000` and `runAsNonRoot: true`;
- no privileged mode and no added capabilities;
- no host network, PID, IPC or paths.

Rootless BuildKit runs as uid 1000. It maps a range of user IDs with
`newuidmap` and `newgidmap`, which are set-uid helpers in the image, and runs
build steps without a process sandbox (`--oci-worker-no-process-sandbox`).

## Why `capabilities: null`

Elsewhere the chart drops every capability and adds back `SETUID` and
`SETGID`, which the uid mapping needs. The fence refuses any added capability.
So on this cluster the container sets no `capabilities` at all, and keeps
CRI-O's default set, which already includes `SETUID` and `SETGID`.

Helm merges a values file into the chart's defaults key by key. Leaving
`capabilities` out of our file would keep the default `add` list, so the file
sets `capabilities: null` to remove it. It also keeps
`allowPrivilegeEscalation: true`, the Kubernetes default, so the set-uid helpers
can work.

From Kubernetes 1.30, AppArmor is set by the `appArmorProfile` field. When that
field is present, the chart leaves out the old annotation.

## Two builders, and failover

Two replicas run, with a preference for different nodes. Build pods are given
two addresses:

- `CI_BUILDKIT_ADDR`, the `buildkitd` Service;
- `CI_BUILDKIT_POOL`, the headless `buildkitd-pods` Service, one DNS record per
  builder.

A build uses the first builder that answers, so one builder down, or one node
down, does not stop image builds.

## Sized to the quota

The namespace allows 1 CPU and 2Gi of requests, 10Gi of memory limits and 4
pods. Each builder requests 500m and 1Gi, so two fill the requests exactly. A
normal rolling update would start a third pod first, which the quota refuses.
The strategy is therefore `maxSurge: 0, maxUnavailable: 1`: replace one, then
the other.

Each builder may use up to 4Gi of memory (8Gi of the 10Gi) and 2 CPU.

## The layer cache has a volume of its own

Until CHG-KS-018 the cache was an `emptyDir` of up to 10Gi on the node's disk,
with no clean-up limits. BuildKit sizes its clean-up from the disk under the
cache, the node's whole disk, not the 10Gi, so the cache grew past the
limit and on 2026-11-11 the kubelet evicted a builder mid-build ("Usage of
EmptyDir volume "cache" exceeds the limit "10Gi"").

Now `buildkitd` is a StatefulSet and each builder has its own 50Gi block
volume (`oci-bv`, the smallest OCI volume; the two use the namespace's 2
claims and 100Gi). The cache is no longer node scratch space, so the kubelet
never evicts a builder over it, and it survives restarts, so builds stay
fast. `buildkitd.toml` (ConfigMap `buildkitd-config`) trims it to 35GB and
keeps 10GB of the volume free for the build in progress.

The cache is still disposable: the claims go when the StatefulSet or a
replica goes (`persistentVolumeClaimRetentionPolicy: Delete`), and a lost
cache only costs the next build its time.

## Network

`default-deny` and `allow-dns` close the namespace. The chart's own `buildkitd`
rule then opens two things:

- **in:** from `kubesight-ci` on 1234, so only build pods talk to the builders;
- **out:** DNS, and the registry ports in `buildkit.egressPorts`, to pull base
  images and push results.

Nothing in `kubesight` talks to the builders directly. The build pods'
per-build rules name `kubesight-build` through `CI_BUILDKIT_NAMESPACE`. Before
this install, the chart did not pass that setting, and the backend assumed
`kubesight-buildkit`. Every image stage would then have been blocked here.

## No token, no secrets

The builders' service account has no API token. They hold no secret of their
own: each build sends its registry login over its BuildKit session. The login
lives only for that build.
