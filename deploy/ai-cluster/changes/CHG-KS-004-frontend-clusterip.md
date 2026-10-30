# CHG-KS-004: Serve the frontend inside the cluster only, behind the Envoy gateway

| Field | Value |
|---|---|
| ID | CHG-KS-004 |
| Modules | kubesight-frontend |
| Status | Done on the cluster 2026-10-30 (Helm revision 8, route fixed); open: users' networks to the gateway, the DNS record |
| Asked by | Elie, 2026-10-30: "make it cluster ip", then "make the service like this" (the Service edited on the cluster to ClusterIP 80 → 80) |
| Depends on | The Gateway `kubesight-gateway` (namespace `kubesight`, class `areeba-fabric`, Envoy Gateway) with `PROGRAMMED True`, ending TLS for `kubesight.areeba.com` and routing to `frontend-service.kubesight.svc:80`; the DNS record for `kubesight.areeba.com` pointing at the gateway's address |
| Prepared by | ____ |
| Approved by | ____ (name, date) |
| Run by | ____ (name, date) |

## What

In `deploy/ai-cluster/values-ai-cluster.yaml`:
- `frontend.service.type`: `LoadBalancer` → `ClusterIP`. The OCI annotations
  and `externalTrafficPolicy: Local` go; they apply to load balancers only.
- `frontend.tls.secretName`: `kubesight-tls` → empty. nginx stops listening on
  8443; the Service becomes **port 80 → container port 80, plain HTTP**. The
  gateway ends TLS. This is the Service as edited by hand on 2026-10-30
  (`kubectl edit`), now written by the chart; only the port's name differs
  (`http`, as it carries HTTP).
- `publicScheme: https` (new chart value). Without the frontend's own TLS the
  chart would otherwise write `http://kubesight.areeba.com` into the links
  KubeSight sends (`KUBESIGHT_PUBLIC_URL`, `PUBLIC_BASE_URL`, `CORS_ORIGINS`).
- `networkPolicies.frontendIngressFrom`: the load balancers' subnet
  `10.43.10.0/24` → the gateway's proxy pods, by namespace and pod label, on
  port 80 (the chart picks 80 because TLS is off). The values file holds
  placeholders in angle brackets; Kubernetes rejects them, so the dry run fails
  until they are filled in.
- `frontend.replicas`: 2 → 1, matching what runs (it was changed with
  `kubectl edit`, which made revisions 4 and 5 fail on a conflict).

The ExternalSecret `kubesight-tls` stays: the gateway's HTTPS listener uses
it as its certificate (same namespace).

Outside the Helm release, applied with kubectl and kept in
[`deploy/ai-cluster/gateway/`](../gateway/):
- `gateway.yaml`: the Gateway `kubesight-gateway`, class `areeba-fabric`,
  HTTPS 443 for `kubesight.areeba.com`, certificate `kubesight-tls`.
- `httproute.yaml`: the route `kubesight-route`, **one rule**, `/` →
  `frontend-service:80`. nginx forwards `/api` to the backend. A second rule
  sending `/api` to `backend-service:5000` made every API call answer 503:
  the backend lets in only the frontend's pods.

The upgrade deletes the internal OCI load balancer if it still exists. Its
address stops answering at once: **anything still using it, including the DNS
record, breaks** until the gateway serves the name.

## Why

People are to reach KubeSight through the Envoy gateway instead of a load
balancer of its own.

## Before you start

1. The Gateway is programmed. It was created with class `envoy-gateway`, which
   does not exist (status stayed `Unknown`); the class is `areeba-fabric`.
   ```bash
   kubectl -n kubesight get gateway kubesight-gateway     # PROGRAMMED True, an ADDRESS
   ```
2. Its route sends `kubesight.areeba.com` to `frontend-service` port **80**:
   ```bash
   kubectl -n kubesight get httproute -o yaml | grep -B2 -A4 backendRefs
   ```
3. The proxy pods' namespace and labels (Envoy Gateway puts them in
   `envoy-gateway-system` unless set otherwise):
   ```bash
   kubectl get pods -A -l gateway.envoyproxy.io/owning-gateway-name=kubesight-gateway --show-labels
   ```
   Done 2026-10-30: namespace `envoy-gateway-system`, labels
   `gateway.envoyproxy.io/owning-gateway-name=kubesight-gateway` and
   `gateway.envoyproxy.io/owning-gateway-namespace=kubesight` (this Gateway's
   proxy pods only, not every Envoy on the cluster).
4. The Gateway and its route carry `areeba.com/module=kubesight-frontend` and
   `areeba.com/change=CHG-KS-004`.

## Steps

On the bastion, in `~/kubesight`:

```bash
CHG=CHG-KS-004
TAG=$(kubectl -n kubesight get deploy kubesight-backend -o jsonpath='{.spec.template.spec.containers[0].image}' | sed 's/.*://'); echo "TAG=$TAG"
ARGS="-n kubesight -f deploy/ai-cluster/values-ai-cluster.yaml --set image.tag=$TAG --set-string commonLabels.areeba\.com/change=$CHG"

# Read only
grep -n "<gateway" deploy/ai-cluster/values-ai-cluster.yaml && echo "STOP: fill in the gateway first"
helm template kubesight charts/kubesight $ARGS | grep -A10 'name: frontend-service$'          # type: ClusterIP, port 80
helm template kubesight charts/kubesight $ARGS | grep -A16 'name: kubesight-frontend-ingress'  # the gateway pods, port 80
helm template kubesight charts/kubesight $ARGS | grep PUBLIC_BASE_URL                         # https://kubesight.areeba.com
helm upgrade kubesight charts/kubesight $ARGS --dry-run=server > /dev/null && echo "helm dry run OK"

# After a person's explicit "yes", and once the gateway route is ready:
helm upgrade kubesight charts/kubesight $ARGS
```

The frontend restarts once (its nginx config changes). The backend restarts
only if its config changed, which it does not: the links stay `https://`.

If the upgrade stops on a conflict over `frontend-service` (the hand edit owns
those fields), run it again with `--force-conflicts` (Helm 4) or
`--force` (Helm 3): the chart now writes the same values.

Then the gateway objects:

```bash
kubectl diff  -f deploy/ai-cluster/gateway/      # only the labels and the route's rules should differ
kubectl apply -f deploy/ai-cluster/gateway/ --dry-run=server && kubectl apply -f deploy/ai-cluster/gateway/
kubectl -n kubesight get httproute kubesight-route -o jsonpath='{range .status.parents[*].conditions[*]}{.type}={.status}{"\n"}{end}'   # Accepted=True, ResolvedRefs=True
```

## Check

```bash
kubectl -n kubesight get service frontend-service          # TYPE ClusterIP, 80/TCP, no EXTERNAL-IP
helm history kubesight -n kubesight | tail -1              # deployed
curl -sS -o /dev/null -w '%{http_code}\n' https://kubesight.areeba.com/health    # 200, through the gateway
curl -ksi -m 20 --resolve kubesight.areeba.com:443:10.43.10.238 https://kubesight.areeba.com/api/auth/me | head -3   # server: nginx, 401 = the backend answered
```

Without the gateway, from the bastion:

```bash
kubectl -n kubesight port-forward svc/frontend-service 8080:80
curl -sS -o /dev/null -w '%{http_code}\n' http://127.0.0.1:8080/health           # 200
```

## Undo

Put back `frontend.tls.secretName: kubesight-tls`, remove `publicScheme`, put
back the `service` block (`LoadBalancer`, `externalTrafficPolicy: Local`, the
four OCI annotations) and `frontendIngressFrom: [{ipBlock: {cidr: 10.43.10.0/24}}]`,
then the same upgrade. OCI creates a **new** load balancer with a new address,
so DNS must follow it.

## Docs to update with the gateway's details

`modules/kubesight-frontend/docs/` (index, tutorial, reference, explanation,
how-to: the load balancer and TLS sections) and `README.md` (web access, open
items).

## Evidence

2026-10-30, from the bastion:

```
$ kubectl get gatewayclass
areeba-fabric   gateway.envoyproxy.io/gatewayclass-controller   True
  (the Gateway first named class envoy-gateway, which does not exist: PROGRAMMED Unknown)
$ kubectl -n kubesight get gateway
kubesight-gateway   areeba-fabric   10.43.10.238   True
$ kubectl -n envoy-gateway-system get svc | grep kubesight
envoy-kubesight-kubesight-gateway-33d82a43   LoadBalancer   10.96.99.171   10.43.10.238   443:30751/TCP
  (oci-network-load-balancer internal, is-preserve-source true, type nlb)

Before the upgrade, through the gateway:
  /health -> upstream connect error ... reset reason: connection timeout   (old rule: 10.43.10.0/24 on 8443)

helm upgrade ... --force-conflicts   (revision 7 had failed: conflict with "kubectl-edit" on .spec.type)
8   Fri Oct 30 13:15:30 2026   deployed   kubesight-0.1.0   1.1.171   Upgrade complete
frontend-service   ClusterIP   10.96.216.197   <none>   80/TCP

/health -> HTTP/2 200, server: nginx
/api/... -> 503 upstream connect error ... connection timeout
  (route had /api -> backend-service:5000; fixed to one rule / -> frontend-service:80)
```

Laptops: connection timeout to 10.43.10.238:443 while the bastion connects;
the network load balancer keeps the client's address, so the users' networks
must be opened to the gateway and the nodes (platform).
