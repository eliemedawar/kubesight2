# Reference: CI and analysis runs

As rendered by `charts/kubesight` with `deploy/ai-cluster/values-ai-cluster.yaml`
and `image.tag=1.0.0`.

## Objects the release creates

| Kind | Name | Namespace | `areeba.com/module` |
|---|---|---|---|
| ServiceAccount | `kubesight-ci-build` | `kubesight-ci` | `kubesight-ci` |
| Role / RoleBinding | `kubesight-ci-launcher` (bound to `kubesight/kubesight-backend`) | `kubesight-ci` | `kubesight-ci` |
| ServiceAccount | `kubesight-analysis-worker` | `kubesight-ci` | `kubesight-analysis` |
| Role / RoleBinding | `kubesight-analysis-launcher` (bound to `kubesight/kubesight-backend`) | `kubesight-ci` | `kubesight-analysis` |
| ExternalSecret | `regcred` (makes the Secret `regcred`) | `kubesight-ci` | `kubesight-ci` |
| NetworkPolicy | `default-deny`, `allow-dns` | `kubesight-ci` | `kubesight` |
| NetworkPolicy | `kubesight-backend-callbacks` (lets this namespace in on 5000) | `kubesight` | `kubesight-backend` |

Every one also carries `areeba.com/change=<the install's change ID>` and
`app.kubernetes.io/part-of=kubesight`. Not created here: the namespace (the
platform's), a ResourceQuota or LimitRange (`manageQuota: false`), or the
cache claim (`ci.cache.enabled: false`).

## Objects the backend creates at run time

These carry **no** `areeba.com/*` labels yet: a known gap in the backend.

| Run | Label | Objects (same name) | Removed |
|---|---|---|---|
| Build | `app.kubernetes.io/name=kubesight-ci`, `kubesight.io/build-id`, `kubesight.io/service` | Job and NetworkPolicy `ci-b<build>-<service>`, Secret `ci-b<build>-<service>-secrets` | 900 s after the Job ends (TTL; owner references) |
| Analysis | `app.kubernetes.io/name=kubesight-application-analysis`, `kubesight.io/analysis-id` | Job and NetworkPolicy `ks-app-analysis-<id>-<suffix>`, Secret `...-credentials` | by the backend after the run |

## Launcher permissions

| Role | Resources | Verbs |
|---|---|---|
| `kubesight-ci-launcher` | jobs | create, get, list, watch, delete, patch |
| | pods | get, list, watch |
| | pods/log | get |
| | secrets, networkpolicies | create, get, delete, patch |
| | persistentvolumeclaims | create, get, list |
| `kubesight-analysis-launcher` | jobs | create, get, list, watch, delete |
| | secrets, networkpolicies | create, get, delete, patch |

## Images

| Container | Image |
|---|---|
| `stage-0` (checkout), `collector` | `registry.areeba.com/kubesight-backend:1.0.0` (`CI_WORKER_IMAGE`) |
| `stage-N` (command stages) | the stage's own image, written in full |
| image stages | the backend image as a `buildctl` client, building on `kubesight-build` |
| analysis Job | `registry.areeba.com/kubesight-backend:1.0.0` (`APPLICATION_ANALYSIS_WORKER_IMAGE`) |

## Settings on this cluster

| Value | Setting |
|---|---|
| `ci.namespace`, `analysis.namespace` | `kubesight-ci` |
| `ci.manageQuota`, `analysis.manageQuota` | `false` |
| `ci.cache.enabled` | `false` |
| `ci.stage.cpuLimit` / `memoryLimit` | `"2"` / `4Gi` |
| `ci.stage.ephemeralRequest` / `ephemeralLimit` / `workspaceSizeLimit` | `off` |
| `ci.extraEgressPorts` | `"4443"` |
| `ci.jobTtlSeconds` / `staleBuildMinutes` | `900` / `120` |
| `ci.artifacts` | autoclean, keep last 1, 1 day |
| `analysis.cpuLimit` / `memoryLimit` / `ephemeralLimit` | `"2"` / `4Gi` / `2Gi` |
| `analysis.deadlineSeconds` / `jobTtlSeconds` / `hermesTimeoutSeconds` | `1800` / `900` / `900` |

## Backend environment that drives this module (ConfigMap `kubesight-config`)

| Variable | Value |
|---|---|
| `CI_KUBERNETES_NAMESPACE` | `kubesight-ci` |
| `CI_SERVICE_ACCOUNT` | `kubesight-ci-build` |
| `CI_BACKEND_NAMESPACE` / `CI_BACKEND_PORT` | `kubesight` / `5000` |
| `CI_WORKER_CALLBACK_URL` | `http://backend-service.kubesight.svc.cluster.local:5000/api/ci/worker` |
| `CI_BUILDKIT_ADDR` | `tcp://buildkitd.kubesight-build.svc.cluster.local:1234` |
| `CI_BUILDKIT_POOL` | `buildkitd-pods.kubesight-build.svc.cluster.local` |
| `CI_BUILDKIT_NAMESPACE` | `kubesight-build` |
| `CI_EXTRA_EGRESS_PORTS` | `4443` |
| `CI_JOB_TTL_SECONDS` / `CI_STALE_BUILD_MINUTES` | `900` / `120` |
| `CI_STAGE_CPU_LIMIT` / `CI_STAGE_MEMORY_LIMIT` | `2` / `4Gi` |
| `APPLICATION_ANALYSIS_EXECUTION_MODE` | `kubernetes` |
| `APPLICATION_ANALYSIS_NAMESPACE` | `kubesight-ci` |
| `APPLICATION_ANALYSIS_SERVICE_ACCOUNT` | `kubesight-analysis-worker` |
| `APPLICATION_ANALYSIS_CALLBACK_URL` | `http://backend-service.kubesight.svc.cluster.local:5000/api/application-analysis-worker` |
| `APPLICATION_PULL_REQUEST_CALLBACK_URL` | `http://backend-service.kubesight.svc.cluster.local:5000/api/application-pull-request-worker` |
| `APPLICATION_ANALYSIS_CPU_LIMIT` / `_MEMORY_LIMIT` / `_EPHEMERAL_LIMIT` | `2` / `4Gi` / `2Gi` |
| `APPLICATION_ANALYSIS_DEADLINE_SECONDS` / `_JOB_TTL_SECONDS` | `1800` / `900` |

`CI_CACHE_CLAIM_NAME` and `CI_CACHE_STORAGE_CLASS` are unset: no cache.

## Secrets (names only)

| Secret | Keys | From |
|---|---|---|
| `regcred` | `.dockerconfigjson` | OpenBao `kv/kubesight-ci/registry` (fields `registry`, `username`, `password`) |
| `ci-b<build>-<service>-secrets` | `callback-token`, stage secrets, registry login | the backend, per build |
| `ks-app-analysis-<id>-<suffix>-credentials` | the run's tokens | the backend, per analysis |

## Network rules

| Rule | Selects | Allows |
|---|---|---|
| `default-deny` | every pod | nothing |
| `allow-dns` | every pod | out to `kube-system` `k8s-app=kube-dns` on 53 UDP/TCP |
| per build (backend) | `kubesight.io/build-id=<n>` | DNS; `kubesight`:5000; 443 anywhere; `kubesight-build`:1234; registry and `CI_EXTRA_EGRESS_PORTS` ports. No ingress. |
| per analysis (backend) | its `kubesight.io/analysis-id` labels | DNS; `kubesight`:5000; 443 anywhere (or the controlled proxy); Hermes when in-cluster |
| `kubesight-backend-callbacks` (in `kubesight`) | `app=kubesight-backend` | in from `kubesight-ci` on 5000 |

## Limits

| | `kubesight-ci` quota (the platform's) | One run at the defaults |
|---|---|---|
| CPU requests | 3 | 100m per container unless set |
| Memory requests | 8Gi | 128Mi per container unless set |
| Memory limits | 16Gi | build stage 4Gi, analysis 4Gi |
| Pods | 40 | 1 per run |
| Claims / storage | 4 / 200Gi | none (cache off) |

Pod security: enforce `baseline`, which build and analysis pods pass (non-root,
`RuntimeDefault` seccomp).
