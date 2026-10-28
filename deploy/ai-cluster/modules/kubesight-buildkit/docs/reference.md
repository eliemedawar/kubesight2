# Reference: BuildKit builders

As rendered by `charts/kubesight` with `deploy/ai-cluster/values-ai-cluster.yaml`
and `image.tag=1.0.0`.

## Objects

| Kind | Name | Namespace | `areeba.com/module` |
|---|---|---|---|
| ServiceAccount | `buildkitd` (no token) | `kubesight-build` | `kubesight-buildkit` |
| Deployment | `buildkitd` (2 replicas) | `kubesight-build` | `kubesight-buildkit` (also on its pods) |
| Service | `buildkitd` (ClusterIP, 1234) | `kubesight-build` | `kubesight-buildkit` |
| Service | `buildkitd-pods` (headless, 1234) | `kubesight-build` | `kubesight-buildkit` |
| NetworkPolicy | `buildkitd` | `kubesight-build` | `kubesight-buildkit` |
| NetworkPolicy | `default-deny`, `allow-dns` | `kubesight-build` | `kubesight` |

Every object also carries `areeba.com/change=<the install's change ID>` and
`app.kubernetes.io/part-of=kubesight`. Pods carry `app=buildkitd`.

## Container

| | |
|---|---|
| Image | `docker.io/moby/buildkit:v0.23.2-rootless` |
| Arguments | `--addr tcp://0.0.0.0:1234 --oci-worker-no-process-sandbox` |
| Port | 1234/TCP |
| Readiness | `buildctl --addr tcp://127.0.0.1:1234 debug workers`, every 20 s after 5 s |
| Security | `runAsUser: 1000`, `runAsGroup: 1000`, `runAsNonRoot: true`, `allowPrivilegeEscalation: true`, `seccompProfile: Unconfined`, `appArmorProfile: Unconfined`, no `capabilities` (CRI-O defaults) |
| Requests | 500m CPU, 1Gi memory, 2Gi ephemeral storage |
| Limits | 2 CPU, 4Gi memory, 10Gi ephemeral storage |
| Cache | `emptyDir` (10Gi limit) at `/home/user/.local/share/buildkit` |
| Placement | preferred anti-affinity on `kubernetes.io/hostname` |
| Updates | `RollingUpdate`, `maxSurge: 0`, `maxUnavailable: 1` |

## Settings on this cluster

| Value | Setting |
|---|---|
| `buildkit.enabled` | `true` (default; needs `ci.enabled`) |
| `buildkit.namespace` | `kubesight-build` |
| `buildkit.replicas` | `2` |
| `buildkit.image` | `docker.io/moby/buildkit:v0.23.2-rootless` |
| `buildkit.securityContext` | as in the table above; `capabilities: null` |
| `buildkit.strategy` | `{type: RollingUpdate, rollingUpdate: {maxSurge: 0, maxUnavailable: 1}}` |
| `buildkit.resources` | as in the table above |
| `buildkit.cacheSize` | `10Gi` (default) |
| `buildkit.egressPorts` | `[443, 8443, 8082, 5000, 9443]` (default) |
| `buildkit.hostAliases` | none |

## Backend settings that point at it (ConfigMap `kubesight-config` in `kubesight`)

| Variable | Value |
|---|---|
| `CI_BUILDKIT_ADDR` | `tcp://buildkitd.kubesight-build.svc.cluster.local:1234` |
| `CI_BUILDKIT_POOL` | `buildkitd-pods.kubesight-build.svc.cluster.local` |
| `CI_BUILDKIT_NAMESPACE` | `kubesight-build` (used in each build pod's network rule) |

## Secrets

None. Builds send their registry login over the BuildKit session. The image
comes from Docker Hub, so no pull secret is needed.

## Network rules

| Rule | Selects | Allows |
|---|---|---|
| `default-deny` | every pod | nothing |
| `allow-dns` | every pod | out to `kube-system` `k8s-app=kube-dns` on 53 UDP/TCP |
| `buildkitd` | `app=buildkitd` | in from `kubesight-ci` on 1234; out to `kube-system` on 53, and to any address on 443, 8443, 8082, 5000, 9443 |

Build pods reach the builders through their per-build rule in `kubesight-ci`
(port 1234 to `kubesight-build`).

## Limits

| | `kubesight-build` quota (the platform's) | Two builders |
|---|---|---|
| CPU requests | 1 | 1 (2 × 500m) |
| Memory requests | 2Gi | 2Gi (2 × 1Gi) |
| Memory limits | 10Gi | 8Gi (2 × 4Gi) |
| Pods | 4 | 2 |
| Claims / storage | 2 / 100Gi | none |

Pod security: enforce `privileged`, fenced by the platform's admission policy
(see the Explanation).
