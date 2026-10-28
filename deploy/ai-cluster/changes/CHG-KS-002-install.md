# CHG-KS-002: Install KubeSight 1.0.0 (without Hermes) and close its namespaces

| Field | Value |
|---|---|
| ID | CHG-KS-002 |
| Modules | kubesight-backend, kubesight-frontend, kubesight-ci, kubesight-buildkit |
| Status | Proposed |
| Asked by | Hussein (platform), handover of 2026-10-28 |
| Decided | Start empty, `kubesight.areeba.com`, release `1.0.0` (Elie, 2026-10-28) |
| Depends on | CHG-KS-001 done, with its evidence |
| Prepared by | ____ |
| Approved by | ____ (name, date) |
| Run by | ____ (name, date) |

## What

One Helm release, `kubesight`, of `charts/kubesight` with
`deploy/ai-cluster/values-ai-cluster.yaml`, image tag `1.0.0`. Hermes stays off
(`hermes.enabled: false`) until there is a Claude API key: CHG-KS-003 adds it.
Until then the ticket agent, AI source analysis and CI assist are unavailable;
everything else works. It creates, in
`kubesight`, `kubesight-ci` and `kubesight-build` only:

| Kind | Objects |
|---|---|
| Deployment | `kubesight-backend`, `kubesight-frontend` (2), `buildkitd` (2, in `kubesight-build`) |
| Service | `backend-service`, `frontend-service` (internal OCI load balancer, 443), `buildkitd`, `buildkitd-pods` |
| PersistentVolumeClaim | `kubesight-backend-data` (50Gi, `oci-bv-retain`) |
| ExternalSecret | `kubesight-secrets`, `kubesight-tls`, `regcred` (in `kubesight` and `kubesight-ci`) |
| ConfigMap | `kubesight-config`, `kubesight-frontend-nginx` |
| ServiceAccount, Role, RoleBinding | `kubesight-backend`; `kubesight-ci-build`, `kubesight-analysis-worker` and the two launcher Roles in `kubesight-ci`; `buildkitd` |
| NetworkPolicy | `default-deny` and `allow-dns*` in all three namespaces; `kubesight-backend-database`, `kubesight-backend-callbacks`, `kubesight-frontend-ingress`, `buildkitd` |
| CiliumNetworkPolicy | `kubesight-backend-egress` (Kubernetes API) |

There is no Namespace, ClusterRole, ClusterRoleBinding, PersistentVolume or
Secret in the release. Every object carries `areeba.com/module` and
`areeba.com/change=CHG-KS-002`. The backend creates the database schema on
its first start (accepted by the platform, 2026-10-28).

## Why

The first deployment of KubeSight on the AI cluster. The network
rules come with it, so each namespace is closed as it is deployed (rule 6).

## Before

1. CHG-KS-001 is done.
2. The tag `1.0.0` is pushed to Bitbucket, and its pipeline has pushed
   `registry.areeba.com/kubesight-backend:1.0.0` and `…/kubesight-frontend:1.0.0`.
3. `charts/kubesight` and `deploy/ai-cluster` from this branch are on the
   bastion, for example: `scp -r charts deploy kprodai@10.43.17.34:~/kubesight/`.

## Steps

On the bastion, in `~/kubesight`:

```bash
CHG=CHG-KS-002; TAG=1.0.0
ARGS="-n kubesight -f deploy/ai-cluster/values-ai-cluster.yaml --set image.tag=$TAG --set-string commonLabels.areeba\.com/change=$CHG"

# 1. Read only: render, look, and let the API server judge it.
helm template kubesight charts/kubesight $ARGS > /tmp/kubesight-$CHG.yaml
grep -E '^kind:' /tmp/kubesight-$CHG.yaml | sort | uniq -c
kubectl apply --dry-run=server -f /tmp/kubesight-$CHG.yaml        # quotas, pod security, the build fence
helm upgrade --install kubesight charts/kubesight $ARGS --dry-run=server > /dev/null && echo "helm dry run OK"

# 2. After a person's explicit "yes":
helm upgrade --install kubesight charts/kubesight $ARGS
```

## Check

```bash
# Secrets delivered (all SecretSynced), with key names only
kubectl -n kubesight get externalsecret; kubectl -n kubesight-ci get externalsecret
kubectl -n kubesight get secret kubesight-secrets -o go-template='{{range $k,$v := .data}}{{$k}}{{"\n"}}{{end}}'

# Pods running and healthy
kubectl -n kubesight rollout status deploy/kubesight-backend --timeout=5m
kubectl -n kubesight rollout status deploy/kubesight-frontend
kubectl -n kubesight-build rollout status deploy/buildkitd
kubectl -n kubesight get pods,pvc,svc -o wide; kubectl -n kubesight-build get pods -o wide
kubectl -n kubesight logs deploy/kubesight-backend --tail=80 | grep -iE 'error|migrat|listening|booting'

# The way in
kubectl -n kubesight get svc frontend-service          # EXTERNAL-IP in 10.43.10.0/24
kubectl -n kubesight exec deploy/kubesight-frontend -- wget -qO- http://localhost/health
curl -sk https://<EXTERNAL-IP>/health                  # from the bastion, if its route allows

# Labels on everything the release made
kubectl get all,pvc,cm,externalsecret,networkpolicy,ciliumnetworkpolicy -A -l areeba.com/change=CHG-KS-002 --no-headers | wc -l
```

**Network, with Hubble.** Run this in the Cilium pod on each pod's node
(`kubectl -n kube-system get pods -l k8s-app=cilium -o wide`):

```bash
kubectl -n kube-system exec <cilium pod> -c cilium-agent -- hubble observe --namespace kubesight --last 200
kubectl -n kube-system exec <cilium pod> -c cilium-agent -- hubble observe --namespace kubesight --verdict DROPPED --last 100
```

| Must be allowed | Must be dropped |
|---|---|
| backend → `pooler-kubesight` 5432 | backend → the internet |
| backend → kube-apiserver | a pod in `kubesight-ci` → another pod there |
| load balancer (10.43.10.x) → frontend 8443 | anything from outside → backend 5000 |
| frontend → backend 5000 | |

Prove the "dropped" backend case with a one-off request:

```bash
kubectl -n kubesight exec deploy/kubesight-backend -- curl -sS -m 5 https://example.com -o /dev/null   # must time out
```

**First sign-in.** The person signing in runs this alone. It shows the one-time
password on their own screen; the first sign-in replaces it and sets up MFA.

```bash
bao kv get -mount=kv -field=KUBESIGHT_INITIAL_ADMIN_PASSWORD kubesight/backend
```

Then, in KubeSight, **API Tokens** lists "Hermes MCP (set by the deployment)".
The token is ready for Hermes, which arrives with CHG-KS-003.

Then ask the platform for the DNS record `kubesight.areeba.com` →
EXTERNAL-IP and the firewall opening for users on 443.

## Undo

```bash
helm uninstall kubesight -n kubesight
```

The claim stays (`helm.sh/resource-policy: keep`), and so do the tables in
the `kubesight` database. A new install with the same values takes both back.
To drop the data as well, after a separate "yes":
`kubectl -n kubesight delete pvc kubesight-backend-data`.

## Evidence

Paste the real output of each **Check** command here, with the date and who
ran it, then set **Status** to Done and each module card's status to Running.

```
(to fill in after the change)
```
