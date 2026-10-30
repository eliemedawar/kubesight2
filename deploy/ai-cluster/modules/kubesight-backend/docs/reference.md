# Reference: the backend

As rendered by `charts/kubesight` with `deploy/ai-cluster/values-ai-cluster.yaml`
and `image.tag=1.0.0`.

## Objects

| Kind | Name | Namespace |
|---|---|---|
| Deployment | `kubesight-backend` (1 replica, `Recreate`) | `kubesight` |
| Service | `backend-service` (ClusterIP, 5000) | `kubesight` |
| ServiceAccount | `kubesight-backend` | `kubesight` |
| ConfigMap | `kubesight-config` | `kubesight` |
| PersistentVolumeClaim | `kubesight-backend-data` (50Gi, RWO, `oci-bv-retain`, kept on uninstall) | `kubesight` |
| ExternalSecret | `kubesight-secrets`, `regcred` | `kubesight` |
| Role + RoleBinding | `kubesight-ci-launcher`, `kubesight-analysis-launcher` (bound to `kubesight/kubesight-backend`) | `kubesight-ci` |
| NetworkPolicy | `kubesight-backend-database`, `kubesight-backend-callbacks` | `kubesight` |
| CiliumNetworkPolicy | `kubesight-backend-egress` | `kubesight` |

## Container

| | |
|---|---|
| Image | `registry.areeba.com/kubesight-backend:1.0.0`, pull secret `regcred` |
| Port | 5000 (gunicorn: 1 worker, 16 threads, 300 s timeout) |
| Probes | `GET /health` on 5000: readiness after 15 s, liveness after 30 s (6 failures) |
| Resources | requests 250m CPU, 1Gi; limit 2Gi memory |
| Volumes | `/var/lib/kubesight` ← `kubesight-backend-data`; `/etc/postgres-ca` ← ConfigMap `postgres-ca` |
| Labels on the pod | `app: kubesight-backend`, `areeba.com/module: kubesight-backend` |

## Secrets (names only)

| Secret | Keys | From |
|---|---|---|
| `kubesight-secrets` | `JWT_SECRET_KEY`, `FLASK_SECRET_KEY`, `KUBESIGHT_SECRET_KEY`, `HERMES_API_TOKEN`, `KUBESIGHT_HERMES_MCP_TOKEN`, `KUBESIGHT_INITIAL_ADMIN_PASSWORD` | `kv/kubesight/backend` |
| `postgres` (the platform's) | `username`, `password` → env `KUBESIGHT_DB_USERNAME`, `KUBESIGHT_DB_PASSWORD` → `DATABASE_URL=postgresql://$(KUBESIGHT_DB_USERNAME):$(KUBESIGHT_DB_PASSWORD)@pooler-kubesight.databases.svc:5432/kubesight?sslmode=verify-full&sslrootcert=/etc/postgres-ca/ca.crt` | the platform, from OpenBao |
| `regcred` | `.dockerconfigjson` | `kv/kubesight/registry` |

## Settings (`kubesight-config`)

| Group | Variables and values |
|---|---|
| Runtime | `APP_ENV=production`, `AUTH_REQUIRED=true`, `FLASK_DEBUG=false`, `K8S_REAL_MODE=true`, `K8S_CONTEXT_NAME=ociduai-k8s`, `KUBESIGHT_AUTO_UPGRADE=false`, `KUBESIGHT_SEED_DEMO_USERS=false`, `JWT_EXPIRY_HOURS=8`, `ALERT_CPU_THRESHOLD_PERCENT=80` |
| URLs | `PUBLIC_BASE_URL`, `KUBESIGHT_PUBLIC_URL`, `CORS_ORIGINS` = `https://kubesight.areeba.com`; `KUBESIGHT_INTERNAL_URL=http://backend-service.kubesight.svc.cluster.local:5000` |
| Database | `DB_POOL_SIZE=12`, `DB_MAX_OVERFLOW=12` |
| Data paths | `KUBESIGHT_KUBECONFIG_DIR`, `CI_ARTIFACT_DIR`, `MOBILE_ARTIFACT_DIR`, `KUBESIGHT_TOFU_WORKDIR`, `APPLICATION_ANALYSIS_ARTIFACT_ROOT`, all under `/var/lib/kubesight` |
| CI | `CI_KUBERNETES_NAMESPACE=kubesight-ci`, `CI_SERVICE_ACCOUNT=kubesight-ci-build`, `CI_WORKER_IMAGE=registry.areeba.com/kubesight-backend:1.0.0`, `CI_BUILDKIT_ADDR=tcp://buildkitd.kubesight-build.svc.cluster.local:1234`, `CI_BUILDKIT_POOL=buildkitd-pods.kubesight-build.svc.cluster.local`, `CI_BUILDKIT_NAMESPACE=kubesight-build`, `CI_EXTRA_EGRESS_PORTS=4443`; no `CI_CACHE_CLAIM_NAME` (cache off) |
| Analysis | `APPLICATION_ANALYSIS_NAMESPACE=kubesight-ci`, `APPLICATION_ANALYSIS_SERVICE_ACCOUNT=kubesight-analysis-worker`, `APPLICATION_ANALYSIS_EXECUTION_MODE=kubernetes` |
| Hermes | `HERMES_API_URL` = `TICKET_AGENT_HERMES_URL` = `http://kubesight-hermes.kubesight.svc.cluster.local:8642/v1/chat/completions`, `HERMES_SERVICE_NAMESPACE=kubesight`, `HERMES_SERVICE_PORT=8642` |

## Values that shape it (`values-ai-cluster.yaml`)

`rbac.clusterWide: true` (CHG-KS-005) · `database.fromLogin` (Secret `postgres`, pooler, `verify-full`) ·
`secrets.existingSecret: kubesight-secrets` · `backend.persistence: {size: 50Gi, storageClass: oci-bv-retain}` ·
`backend.extraVolumes` / `extraVolumeMounts` (postgres-ca) · `backend.autoUpgrade: false` ·
`backend.inClusterContextName: ociduai-k8s` · `networkPolicies.*`

## Network rules

| Direction | Peer | Port | Rule |
|---|---|---|---|
| in | pods in `kubesight` (frontend, Hermes) | any | `allow-dns-and-same-namespace` |
| in | namespace `kubesight-ci` (CI and analysis callbacks) | 5000 | `kubesight-backend-callbacks` |
| out | `kube-dns` | 53 | `allow-dns-and-same-namespace` |
| out | pods in `kubesight` | any | `allow-dns-and-same-namespace` |
| out | `databases`, pods `cnpg.io/poolerName=pooler-kubesight` | 5432 | `kubesight-backend-database` (also for pods labelled `app=kubesight-db-client`) |
| out | the Kubernetes API | — | `kubesight-backend-egress` (Cilium, `toEntities: kube-apiserver`) |
| out | named outside systems | listed | `kubesight-backend-egress` (`networkPolicies.backendEgressFQDNs` / `backendEgressCIDRs`, empty at install) |

## Quota use (`kubesight`: 3 CPU / 8Gi requests, 16Gi memory limits, 30 pods, 6 claims / 300Gi)

The backend uses 250m CPU and 1Gi requested, a 2Gi limit, and 1 claim of 50Gi.
