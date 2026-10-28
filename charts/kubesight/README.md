# KubeSight Helm chart

One release installs everything KubeSight runs in a cluster:

| Part | Namespace | What |
|---|---|---|
| Backend | release namespace (`kubesight`) | `kubesight-backend` Deployment, `backend-service`, `kubesight-config` ConfigMap, data volume, ServiceAccount + ClusterRoles |
| Frontend | release namespace | `kubesight-frontend`, `frontend-service`, Ingress `kubesight-ingress` |
| Hermes | release namespace | `kubesight-hermes` Deployment + Service, config, KubeSight skill, data volume, NetworkPolicy |
| Native CI | `kubesight-ci` | build ServiceAccount, launcher Role, quota, limits, dependency cache volume |
| BuildKit | `kubesight-buildkit` | `buildkitd` (2 replicas), `buildkitd` + `buildkitd-pods` Services, NetworkPolicy |
| Application Intelligence | `kubesight-analysis` | worker ServiceAccount, launcher Role, quota, limits |

PostgreSQL is not part of it: KubeSight uses an external database.

## Before you install

- Kubernetes 1.29 or newer, `kubectl` and `helm` 3 pointed at the cluster as a cluster admin.
- An ingress controller, and a DNS record for the host name pointing at it.
- metrics-server (CPU alerts, `kubectl top`).
- PostgreSQL 14+ with an empty database and a user that owns it.
- The images in the registry: `kubesight-backend` and `kubesight-frontend` at the release tag, `kubesight-hermes`, and `moby/buildkit` reachable.
- Storage: a StorageClass, or NFS exports for the backend data, Hermes data and CI cache (set `nfs.server` and `nfs.path` per volume). The CI cache export must belong to uid 65532:
  `sudo mkdir -p <path> && sudo chown 65532:65532 <path> && sudo chmod 2775 <path>`

## Secrets you create

Nothing secret goes in a values file. Create these in the `kubesight` namespace first:

```bash
kubectl create namespace kubesight

# The database (required)
kubectl -n kubesight create secret generic kubesight-database \
  --from-literal=DATABASE_URL='postgresql://kubesight:<password>@<host>:5432/kubesight'

# The certificate (when ingress.tls.mode is existingSecret)
kubectl -n kubesight create secret tls kubesight-tls --cert=tls.crt --key=tls.key

# Hermes's LLM login (the Codex auth.json of the production account)
kubectl -n kubesight create secret generic hermes-codex-auth --from-file=auth.json

# Optional: anything else Hermes reads from its environment, e.g. Telegram
kubectl -n kubesight create secret generic hermes-env --from-literal=TELEGRAM_BOT_TOKEN=<token>
```

The registry pull secret (`areeba-registry`) must exist in all four namespaces, or set
`imagePullSecret.create=true` with `--set imagePullSecret.username=… --set imagePullSecret.password=…`
and the chart creates it in each.

## Install

```bash
cp charts/kubesight/values-production.example.yaml values-production.yaml   # fill it in
helm upgrade --install kubesight charts/kubesight -n kubesight \
  -f values-production.yaml --set image.tag=<x.y.z>
kubectl -n kubesight rollout status deploy/kubesight-backend
```

The first boot creates the tables, so the backend takes a minute or two to become ready.

## First sign-in

The database starts empty, with one user, `admin`, whose password is generated:

```bash
kubectl -n kubesight get secret kubesight-secrets \
  -o jsonpath='{.data.KUBESIGHT_INITIAL_ADMIN_PASSWORD}' | base64 -d; echo
```

It is a one-time password: the first sign-in must replace it and set up MFA. The demo
accounts (`viewer`, `operator`) are not created unless `backend.seedDemoUsers=true`.

Hermes is already connected to KubeSight's MCP server: **API Tokens** lists its token as
"Hermes MCP (set by the deployment)". Revoking it there sticks. To issue a new one, delete
the `KUBESIGHT_HERMES_MCP_TOKEN` key from `kubesight-secrets`, run `helm upgrade`, then
restart both pods (a changed Secret does not restart them by itself); the backend retires
the old token as it starts:

```bash
kubectl -n kubesight rollout restart deploy/kubesight-backend deploy/kubesight-hermes
```

## Upgrade and roll back

Test the release in UAT first, then:

```bash
helm upgrade kubesight charts/kubesight -n kubesight -f values-production.yaml --set image.tag=<new x.y.z>
helm history kubesight -n kubesight
helm rollback kubesight <revision> -n kubesight
```

The CI build pods and the analysis worker run the backend image at the same tag, so they
move with the backend. A rollback does not undo database migrations: take a database
backup before upgrading.

## What the chart generates, and what it keeps

On first install the chart creates `kubesight-secrets` with `JWT_SECRET_KEY`,
`FLASK_SECRET_KEY`, `KUBESIGHT_SECRET_KEY`, `HERMES_API_TOKEN`, `KUBESIGHT_HERMES_MCP_TOKEN`
and `KUBESIGHT_INITIAL_ADMIN_PASSWORD`. Every upgrade reads them back from the cluster and
keeps them.

**Back up `kubesight-secrets`.** `KUBESIGHT_SECRET_KEY` encrypts every credential stored in
the database (cluster kubeconfigs, registry, Bitbucket, Zoho/Jira, vCenter). Losing it
means entering all of them again.

`helm uninstall` leaves behind the Secret, the volumes (PVCs and NFS PVs) and the
`kubesight-ci`, `kubesight-buildkit` and `kubesight-analysis` namespaces, so removing the
release never deletes data. A reinstall with the same release name takes them back.

## Settings

Every setting is in [values.yaml](values.yaml) with an explanation. Any backend
environment variable not covered there goes in `backend.config` (plain values) or in a
Secret named in `backend.extraEnvFromSecrets`.

## Calling Hermes through an AI proxy

KubeSight can send its Hermes requests to an AI proxy that forwards them to Hermes:

```yaml
hermes:
  apiUrl: https://ai-proxy.example.com/v1/chat/completions
  networkPolicy:
    extraIngressFrom:
      - namespaceSelector: {matchLabels: {kubernetes.io/metadata.name: ai-proxy}}
```

The URL must be `https://`, or `http://` to an in-cluster `…svc.cluster.local` Service
(the analysis Jobs then get an egress rule for that namespace and port). It cannot carry
a query string or `user:password@`. Leave `extraIngressFrom` empty when the proxy reaches
Hermes from the release namespace. A proxy outside the cluster needs its own route to the
`kubesight-hermes` Service, because the chart exposes it only inside the cluster.

The proxy must:

- **Forward the key.** KubeSight sends `Authorization: Bearer <HERMES_API_TOKEN>`, which is
  also Hermes's `API_SERVER_KEY`. Either pass it through, or check your own proxy key and
  send `HERMES_API_TOKEN` from `kubesight-secrets` to Hermes. For your own key, put it as
  `HERMES_API_TOKEN` in a Secret named in `backend.extraEnvFromSecrets`. That Secret is
  read after `kubesight-secrets`, so its value wins for the backend, while Hermes keeps
  the generated key.
- **Pass everything through as is.** That covers the request body, the
  `X-Hermes-Session-Key` header (it keeps ticket text out of the operators' Hermes memory)
  and the whole JSON response. KubeSight reads the response's `hermes` object to tell an
  unfinished run from an answer. If the proxy routes by model name, send `hermes-agent`
  and `hermes-analysis` to Hermes.
- **Wait long enough.** Ticket-agent calls run up to 600 s and analysis up to
  `analysis.hermesTimeoutSeconds` (900 s). Request and response bodies reach 4 MB.

**Integrations** in KubeSight shows the configured URL, and the ticket agent's **Test
connection** confirms the proxy answers. Hermes's own calls to KubeSight's MCP server do
not go through the proxy.

## On a cluster a platform team runs

Where namespaces, cluster-wide objects, secrets and network rules belong to a platform
team, these settings keep the release inside its namespaces:

| Setting | Effect |
|---|---|
| `createNamespaces: false` | the CI, BuildKit and analysis namespaces must already exist |
| `rbac.clusterWide: false` | no ClusterRoles: the backend has only the launcher Roles in the CI and analysis namespaces |
| `ci.manageQuota`, `analysis.manageQuota: false` | the namespace's own quota and container defaults apply |
| `externalSecrets.items` | every Secret comes from a secret store through an ExternalSecret; name them in `secrets.existingSecret`, `database.existingSecret`, `frontend.tls.secretName`, `hermes.extraEnvFromSecrets`, `imagePullSecret.name` |
| `database.fromLogin` | `DATABASE_URL` is put together in the pod from a login Secret the platform keeps (user name and password keys), so a rotated password follows by itself |
| `frontend.serveApi`, `frontend.tls.secretName`, `frontend.service` | no ingress controller: the frontend's nginx sends `/api` to the backend and serves HTTPS on 8443 behind a load balancer |
| `networkPolicies.*` | default deny in every KubeSight namespace, then DNS, the database, the API server, callbacks, the load balancer and named outside hosts (Cilium) |
| `hermes.networkPolicy.egressFQDNs` | Hermes reaches only these host names (Cilium) |
| `buildkit.securityContext`, `buildkit.strategy` | fit a builder namespace's admission fence and quota |
| `moduleLabelKey`, `commonLabels` | a module label on every object and pod, and a change ID on every object |

[`deploy/ai-cluster/values-ai-cluster.yaml`](../../deploy/ai-cluster/values-ai-cluster.yaml)
uses all of them.

## Hermes skill

`files/hermes-skills/kubesight` is a copy of `.claude/skills/kubesight` (Helm can only
package files inside the chart). After changing the skill run
`charts/kubesight/sync-hermes-skill.sh`; `backend/tests/test_chart_hermes_skill.py`
fails while they differ. Hermes gets the chart's copy on every start.

## Differences from the UAT installation

UAT was set up by hand and is not managed by this chart. Production differs on purpose:

- The backend has a data volume, so CI artifacts, mobile binaries and kubeconfig files
  survive a restart (in UAT they are lost on every rollout).
- The CI and analysis workers always run the backend's own tag (UAT pins older tags).
- Hermes has its own data volume instead of sharing the analysis-artifacts volume.
- The backend's ClusterRole includes `nodes/proxy` for disk and PVC usage.
- The Hermes Service is `kubesight-hermes` (UAT: `hermes-application-intelligence`).
