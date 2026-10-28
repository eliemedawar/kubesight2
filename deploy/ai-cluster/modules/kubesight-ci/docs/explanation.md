# Explanation: why CI and analysis runs are built this way

## Untrusted code, kept apart

A build runs whatever is in a repository: its tests, its build scripts and its
dependencies. An analysis checks out and inspects source the same way. Both
are treated as untrusted code. So they never run in the backend's pod or
namespace, and they get nothing they could misuse:

- **no Kubernetes API access:** their service accounts set
  `automountServiceAccountToken: false`, and nothing grants them a Role;
- **non-root:** uid 65532, `seccompProfile: RuntimeDefault`;
- **only their own secrets:** each build's stage secrets, callback token and
  registry login go in one short-lived Secret, owned by the Job, so it is
  deleted with it.

The backend starts them through two namespaced Roles, `kubesight-ci-launcher`
and `kubesight-analysis-launcher`. These allow creating and deleting Jobs,
their Secrets and NetworkPolicies, and reading pods and logs. Nothing in the
release reaches beyond this namespace and the backend's own. The platform's
rules allow no ClusterRole (`rbac.clusterWide: false`).

## Network: closed, then one rule per run

The chart closes the namespace with `default-deny` and opens only DNS
(`allow-dns`). Unlike `kubesight`, there is **no same-namespace allow**: one
build must not reach another build, or an analysis.

For each run, the backend then adds that run's own NetworkPolicy, selecting
only its pod:

- **a build:** DNS, port 5000 in `kubesight` (the callback), 443 out (git and
  package registries), BuildKit's port in `kubesight-build`, and the
  registry's and `ci.extraEgressPorts` ports;
- **an analysis:** DNS, port 5000 in `kubesight`, and 443 out, or a controlled
  egress proxy when one is configured. It also opens Hermes's address and
  port when that is in the cluster.

Network rules only add to each other, so a run's rule opens exactly its own
needs on top of the closed namespace. In `kubesight`, the chart's
`kubesight-backend-callbacks` rule lets these pods in on port 5000.

The BuildKit address used to be assumed to be `kubesight-buildkit`. The chart
now passes `CI_BUILDKIT_NAMESPACE=kubesight-build`. Without it, every build's
rule would have pointed at a namespace that does not exist here, and image
stages would have timed out.

## Analysis shares the namespace

The guide gives `kubesight-ci` to "CI stage pods and analysis runs". There is
no fourth namespace, and asking for one buys little: both kinds of run are
equally untrusted, and the rules above already keep each run apart. They
keep separate accounts and Roles. The analysis objects carry the module label
`kubesight-analysis`, so they can still be told apart.

## The platform's quota, not ours

Elsewhere the chart can create its own ResourceQuota and LimitRange. Here the
platform already set both, and two quotas in one namespace both apply, so the
stricter one would win by surprise. `ci.manageQuota` and
`analysis.manageQuota` are therefore off.

The platform's limits are:
- 3 CPU and 8Gi of requests;
- 16Gi of memory limits;
- 40 pods.

A container that sets nothing gets 100m and 128Mi, with a 512Mi limit.

## No dependency cache yet

The cache is one volume shared by every build pod, on any node, so it must be
ReadWriteMany. The platform's storage classes are OCI block volumes, which are
ReadWriteOnce. The cache stays off until the platform connects NFS. Builds still
work; they download their dependencies each time.

The backend has a second mode, one block volume per service
(`CI_CACHE_STORAGE_CLASS`). It is not used here, for three reasons:
- each volume is at least 50 GiB;
- the namespace allows 4 claims;
- two builds of one service on different nodes cannot attach the same volume.

## Images in full

CRI-O refuses short image names. The build pods themselves run the backend
image, written in full. Each stage's image comes from the service's pipeline,
so pipelines must name theirs in full too.
