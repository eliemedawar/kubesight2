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

## The layer cache is disposable

The cache lives in an `emptyDir` of up to 10Gi on the node's disk, so it is
lost when a builder restarts. That costs only time: the next build pulls and
rebuilds its layers. It also avoids a volume claim in a namespace that has
two.

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
