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
| Agent platform (off by default) | `kubesight-agents` | API + worker Deployments, `kubesight-agent-platform` Service, department ConfigMap, Secrets, NetworkPolicies |

PostgreSQL is not part of it: KubeSight uses an external database.

## Before you install

- Kubernetes 1.29 or newer, `kubectl` and `helm` 3 pointed at the cluster as a cluster admin.
- An ingress controller, and a DNS record for the host name pointing at it.
- metrics-server (CPU alerts, `kubectl top`).
- PostgreSQL 14+ with an empty database and a user that owns it.
- The images in the registry: `kubesight-backend` and `kubesight-frontend` at the release tag (plus `kubesight-agent-platform` when the agent platform is on), `kubesight-hermes`, and `moby/buildkit` reachable.
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

## Agent platform

The agent platform replaces Hermes: PydanticAI agents, run durably on Temporal, one flow
at a time. KubeSight stays the system of record and the approval authority. The
platform reaches it only through the MCP server, with its own `agent-platform` account,
and reports back to the backend.

| Flow | Switch (`agentPlatform.flows`) | On the platform |
|---|---|---|
| Tickets | `tickets` | One Temporal workflow per ticket. Every task on the ticket (new ticket, deploy follow-up, requester reply) is a signal to it. No backend thread waits on a model. |
| Source analysis | `analysis` | The analysis Job calls the platform's OpenAI-compatible endpoint instead of Hermes. |
| CI assist | `ciAssist` | Pipeline generation and repair, the same way. |

Each switch is `hermes` or `platform`, and switching back is the same edit. The ticket
agent sees only the `kubesight_ticket_*` tools and the read-only tools. No other write
tool is shown to it, and the `agent-platform` role makes KubeSight refuse one anyway.

**1. Temporal.** Install the official chart as its own release, on the external PostgreSQL
in separate databases. [`../temporal-values.example.yaml`](../temporal-values.example.yaml)
has the values and commands. The platform uses the Temporal namespace `agents`.

**2. Turn it on**, with every flow still on Hermes:

```yaml
agentPlatform:
  enabled: true
  model:
    baseUrl: https://ai-proxy.example.com/v1
    name: gpt-5.6-sol
```

**3. The model key.** The chart creates the `kubesight-agents` namespace, so create the key
after the install (creating the namespace first makes Helm refuse it). The worker waits in
`CreateContainerConfigError` until the Secret exists, then starts by itself:

```bash
kubectl -n kubesight-agents create secret generic agent-platform-model --from-literal=MODEL_API_KEY=<key>
```

The image `kubesight-agent-platform` is built with every release at the same tag. The chart
generates the platform's MCP token and the shared secret, and keeps them across upgrades.
The backend registers the token for `agent-platform` on boot. **Integrations → Agent
platform → Test connection** confirms the backend reaches the platform and the platform
reaches Temporal.

**4. Give it clusters to read.** Troubleshooting tickets read pods, logs and events. In
**Users**, grant the `agent-platform` account the clusters (and namespaces) it may read.
Its role is fixed and holds only `:view` permissions besides the ticket tools.

**5. Move one flow at a time**, for example `--set agentPlatform.flows.tickets=platform`,
check it in UAT, then the next. When all three are on the platform, `hermes.enabled=false`
removes Hermes.

The department file and the ticket prompt are in `agent-platform/departments/infra`.
`files/agent-platform` is the chart's copy: run `charts/kubesight/sync-agent-platform.sh`
after editing it. `backend/tests/test_chart_agent_platform.py` fails while the copies
differ, or while the ticket rules differ from Hermes' prompt.

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
