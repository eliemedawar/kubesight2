# Reference: KubeSight frontend

As rendered by `charts/kubesight` with `deploy/ai-cluster/values-ai-cluster.yaml`,
release `kubesight`, namespace `kubesight`.

## Objects

| Kind | Name | Namespace |
|---|---|---|
| Deployment | `kubesight-frontend` | `kubesight` |
| Service (LoadBalancer) | `frontend-service` | `kubesight` |
| ConfigMap | `kubesight-frontend-nginx` (key `default.conf`) | `kubesight` |
| ExternalSecret → Secret | `kubesight-tls` | `kubesight` |
| NetworkPolicy | `kubesight-frontend-ingress` | `kubesight` |

Shared with the rest of `kubesight`: NetworkPolicies `default-deny` and
`allow-dns-and-same-namespace`, and ExternalSecret `regcred` (the pull secret).

## Image and ports

| | |
|---|---|
| Image | `registry.areeba.com/kubesight-frontend:1.0.0`, pulled with `regcred` |
| Container ports | `http` 80 (probes, in-cluster), `https` 8443 (TLS) |
| Service | `frontend-service`, port 443 → `https` (8443), `externalTrafficPolicy: Local` |
| Probes | readiness and liveness `GET /` on port 80 |
| Service account | default, no token mounted (`automountServiceAccountToken: false`) |

## Load balancer

| Annotation | Value |
|---|---|
| `service.beta.kubernetes.io/oci-load-balancer-internal` | `"true"` |
| `service.beta.kubernetes.io/oci-load-balancer-shape` | `flexible` |
| `service.beta.kubernetes.io/oci-load-balancer-shape-flex-min` | `"10"` |
| `service.beta.kubernetes.io/oci-load-balancer-shape-flex-max` | `"100"` |

It lands in the subnet `OCIDUAILB` (10.43.10.0/24). `kubesight` may have 2 load
balancers; this is one.

## Settings

| Values key | Value here |
|---|---|
| `publicHost` | `kubesight.areeba.com` |
| `ingress.enabled` | `false` |
| `ingress.proxyBodySize` | `512m` (nginx `client_max_body_size`) |
| `frontend.replicas` | `2` |
| `frontend.serveApi` | `true` |
| `frontend.tls.secretName` | `kubesight-tls` |
| `frontend.service.type` | `LoadBalancer` |
| `frontend.service.externalTrafficPolicy` | `Local` |
| `frontend.resources` | requests 50m CPU / 64Mi; limits 200m CPU / 128Mi |

## nginx routes

| Location | Goes to |
|---|---|
| `/api` (prefix) | `http://backend-service.kubesight.svc.cluster.local:5000`; no buffering, 310 s read/send timeout, `X-Forwarded-*` headers set |
| `= /health` | the backend's `/health` |
| `/assets/` | files, cached for a year (`immutable`) |
| `/` | the app; unknown paths fall back to `index.html` (`no-cache`) |

## Volumes

| Name | From | Mounted at |
|---|---|---|
| `nginx` | ConfigMap `kubesight-frontend-nginx` | `/etc/nginx/conf.d/default.conf` (read only) |
| `tls` | Secret `kubesight-tls` | `/etc/nginx/tls` (read only) |

## Secrets

| Secret | Keys | From OpenBao |
|---|---|---|
| `kubesight-tls` (type `kubernetes.io/tls`) | `tls.crt`, `tls.key` | `kv/kubesight/tls`, refreshed every hour |
| `regcred` (type `kubernetes.io/dockerconfigjson`) | `.dockerconfigjson` | `kv/kubesight/registry` (`registry`, `username`, `password`) |

## Network rules

| Rule | Effect on the frontend |
|---|---|
| `default-deny` | nothing in or out unless allowed below |
| `allow-dns-and-same-namespace` | DNS to `kube-dns`; traffic to and from pods in `kubesight` |
| `kubesight-frontend-ingress` | in: TCP 8443 from `10.43.10.0/24` |

## Labels

| Where | Labels |
|---|---|
| Every object | `app.kubernetes.io/part-of: kubesight`, `app.kubernetes.io/instance: kubesight`, `app.kubernetes.io/managed-by: Helm`, `helm.sh/chart: kubesight-0.1.0`, `areeba.com/module: kubesight-frontend`, `areeba.com/change: <change ID>` |
| Pods | `app: kubesight-frontend`, `areeba.com/module: kubesight-frontend` |

## Quota use (namespace `kubesight`)

Two replicas use 100m CPU and 128Mi of the 3 CPU / 8Gi requests, 256Mi of the
16Gi memory limits, and 2 of the 30 pods.
