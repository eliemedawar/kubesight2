# KubeSight on the AI cluster

The plan for deploying KubeSight (backend, frontend, Hermes, CI, BuildKit) on
`OCIDUAI-K8S`, following `ai-cluster-devops-guide.md` (version 1, 2026-10-28).
It covers the session prompt's first tasks: 1 (access check), 2 (inventory),
3 (mapping), and the commands for 5–8.

Everything is installed by **one Helm release** of `charts/kubesight` with
[`values-ai-cluster.yaml`](values-ai-cluster.yaml). The rendered release holds
no Namespace, PersistentVolume or Secret, and no ClusterRole or
ClusterRoleBinding except the backend's two from CHG-KS-005. Every object
carries `areeba.com/module` and `areeba.com/change`.

## 1. Check access (read only)

On the bastion, as `kprodai`. Paste the output back; none of it is secret.

```bash
kubectl config current-context                     # ai-cluster
kubectl auth whoami
kubectl get ns kubesight kubesight-ci kubesight-build ai-guard -L pod-security.kubernetes.io/enforce
for n in kubesight kubesight-ci kubesight-build ai-guard; do kubectl -n $n get secretstore openbao; done
kubectl -n kubesight get secret postgres -o go-template='{{range $k,$v := .data}}{{$k}}{{"\n"}}{{end}}'   # key names only
kubectl -n kubesight get externalsecret -o custom-columns=NAME:.metadata.name,KEY:.spec.dataFrom[*].extract.key,STATUS:.status.conditions[0].reason
kubectl -n kubesight get configmap postgres-ca
kubectl get storageclass
kubectl get ingressclass                           # expected: none
kubectl get crd ciliumnetworkpolicies.cilium.io externalsecrets.external-secrets.io
for n in kubesight kubesight-ci kubesight-build; do kubectl -n $n describe resourcequota; kubectl -n $n get limitrange; done
kubectl -n kube-system get configmap cilium-config -o yaml | grep -E 'kube-proxy-replacement|enable-l7-proxy|policy-cidr-match-mode'
getent hosts registry.areeba.com || echo "registry.areeba.com does not resolve from the bastion"
```

These confirm what the values file assumes. If one differs, the values change
before anything is installed:

- the keys of the platform's Secret `postgres` (`username`, `password`);
- the storage class `oci-bv-retain`;
- no ingress controller;
- the Cilium and ESO kinds;
- the quotas;
- Cilium's L7 proxy, which DNS-based rules need.

**Result (2026-10-28):** everything as assumed. The one exception is the
platform's `postgres` ExternalSecret, which maps fields one by one, so the
OpenBao field names are not visible. The backend therefore builds its URL in
the pod from the Kubernetes Secret `postgres` (keys `username`, `password`)
instead of reading OpenBao for it.

Also:
- Cilium runs with `kube-proxy-replacement: false`. kube-proxy keeps the load
  balancer's address as the source with `externalTrafficPolicy: Local`, which
  the frontend's rule relies on.
- `enable-l7-proxy: true`, so host-name rules work.
- `registry.areeba.com` resolves to 10.43.17.16.

## 2. Inventory: KubeSight as it runs today (UAT)

From the live export of 2026-10-21 (`charts/export-live-objects.sh`) and the code.

| Component | Image | Port | Data | Today in UAT |
|---|---|---|---|---|
| Backend (Flask/gunicorn) | `registry.areeba.com/kubesight-backend:<tag>` | 5000 | `/var/lib/kubesight` (kubeconfigs, CI artifacts, mobile binaries, analysis artifacts, OpenTofu work dirs) | in the container: lost on every rollout |
| Frontend (nginx + SPA) | `registry.areeba.com/kubesight-frontend:<tag>` | 80 | none | behind ingress-nginx |
| Hermes (agent) | `registry.areeba.com/kubesight-hermes:v2026.7.20` | 8642 | `/opt/data` (memory, sessions, config) | ChatGPT/Codex login (`hermes-codex-auth`) |
| CI build pods | backend image + each stage's own image | — | CI cache volume (ReadWriteMany) | ns `kubesight-ci` |
| Analysis Jobs | backend image | — | — | ns `kubesight-analysis` |
| BuildKit | `moby/buildkit:v0.23.2-rootless` | 1234 | emptyDir cache | ns `kubesight-buildkit`, 2 replicas |

**Secrets** (names only):

| Secret | Keys | Moves to |
|---|---|---|
| `kubesight-secret`, `kubesight-encryption`, `kubesight-hermes-credentials` | `JWT_SECRET_KEY`, `FLASK_SECRET_KEY`, `KUBESIGHT_SECRET_KEY`, `HERMES_API_TOKEN` | `kv/kubesight/backend` (+ `KUBESIGHT_HERMES_MCP_TOKEN`, `KUBESIGHT_INITIAL_ADMIN_PASSWORD`) |
| `kubesight-secret` | `DATABASE_URL`, `DATABASE_USER`, `DATABASE_PASSWORD` | the platform's Secret `postgres` (`username`, `password`) → `DATABASE_URL` put together in the pod |
| `hermes-codex-auth` | `auth.json` | `kv/kubesight/hermes` (`auth.json`), a new login of UAT's ChatGPT account, never a copy of UAT's file |
| `areebasecret-2026-2027` (TLS) | `tls.crt`, `tls.key` | `kv/kubesight/tls` |
| `regcred` / `areeba-registry` | `.dockerconfigjson` | `kv/kubesight/registry`, `kv/kubesight-ci/registry` |

Credentials typed into KubeSight's UI (cluster kubeconfigs, Bitbucket,
Zoho/Jira, vCenter, registries, SMTP) are not Kubernetes Secrets: they live
in the database, encrypted with `KUBESIGHT_SECRET_KEY`.

**Database:** one PostgreSQL database, created and migrated by the backend
itself at start-up (tables, then numbered boot migrations, serialised by an
advisory lock). It holds advisory locks for leader election, so it needs a
**session-mode** pooler, which `pooler-kubesight` is.

**Outside calls** (each needs a Cilium rule *and* a firewall opening, added as
the integration is turned on):

- the clusters KubeSight manages: their API servers, and SSH to nodes for the Cluster Builder and upgrades;
- Bitbucket;
- Nexus `registry.areeba.com`, from the backend, the CI pods and BuildKit;
- Zoho Desk and Jira;
- SMTP;
- vCenter (OpenTofu);
- Red Hat Satellite;
- Google Play and App Store Connect;
- Jenkins (legacy deploys);
- `api-oci.areeba.com`, which UAT pins in `hostAliases` to 193.123.81.215;
- for Hermes: OpenAI (`chatgpt.com`, `auth.openai.com`), `models.dev` and Telegram (`api.telegram.org`, CHG-KS-006).

**Cluster-wide needs:** UAT gives the backend two ClusterRoles (read the
whole cluster; deploy anywhere). That is how KubeSight manages the cluster *it
runs in*. The first install shipped none (`rbac.clusterWide: false`), so
KubeSight managed only other clusters, through their kubeconfigs, and showed
this one empty. With [CHG-KS-005](changes/CHG-KS-005-manage-this-cluster.md)
(`rbac.clusterWide: true`, once the platform agrees) it gets the same two as
UAT and manages this cluster too. The namespaced Roles to run Jobs in
`kubesight-ci` stay.

## 3. Mapping to the platform

| Need | Here |
|---|---|
| Namespaces | the platform's `kubesight`, `kubesight-ci` (CI **and** analysis), `kubesight-build` (`createNamespaces: false`) |
| Secrets | OpenBao → 6 ExternalSecrets in the chart (`hermes-env` from CHG-KS-006) (`externalSecrets.items`); the database login is the platform's Secret `postgres` |
| Database | `pooler-kubesight.databases.svc:5432`, `verify-full`, CA from `postgres-ca` mounted at `/etc/postgres-ca`; pool cut to 12 + 12 connections (the login allows 40) |
| Backend and Hermes data | 50 GiB each on `oci-bv-retain` (50 GiB is the smallest volume) |
| CI dependency cache | **off**: it needs ReadWriteMany, which block volumes cannot do |
| Builds | BuildKit in `kubesight-build`: `docker.io/moby/buildkit:v0.23.2-rootless`, uid 1000, no added capabilities, unconfined seccomp/AppArmor, one-at-a-time updates to stay in the quota |
| Web access | Envoy Gateway `kubesight-gateway` (class `areeba-fabric`, internal OCI network load balancer `10.43.10.238:443`, TLS from `kv/kubesight/tls`) → `frontend-service:80` (ClusterIP, plain HTTP) → nginx, which proxies `/api` to the backend. Gateway and route: [gateway/](gateway/), applied with kubectl (CHG-KS-004). Until 2026-10-30: an internal OCI load balancer → nginx on 8443 |
| Hermes's model | OpenAI through a ChatGPT/Codex login (as in UAT), `gpt-5.6-sol`, login in `kv/kubesight/hermes`; egress to `chatgpt.com`, `auth.openai.com` and `models.dev` only |
| Network | default deny in all three namespaces; DNS; same-namespace in `kubesight` only; backend → pooler, → API server; CI/analysis → backend:5000; the gateway's Envoy pods → frontend:80; per-build rules created by the backend |
| Cluster-wide | the backend's reader and deployer ClusterRoles, as in UAT (CHG-KS-005); nothing else |

## 4. Decisions and answers (2026-10-28)

| Question | Answer |
|---|---|
| Start empty, or carry UAT over? | **Start empty** (Elie). Keys are generated into OpenBao; the backend creates the schema; nothing is restored. |
| Host name | **`kubesight.areeba.com`** (Elie). UAT no longer uses it. |
| Release | **`1.0.0`** (Elie). Built by pushing the tag `1.0.0` to Bitbucket (the tag pipeline tests, then pushes `kubesight-backend:1.0.0` and `kubesight-frontend:1.0.0`). |
| Can the nodes pull from `registry.areeba.com`, and BuildKit push to it? | Yes (Hussein) |
| Firewall: Hermes → `api.anthropic.com`, users → the load balancer | Yes (Hussein) |
| CI cache storage | Later, on NFS; **off** for now (Hussein) |
| Schema migrations run by the backend at start-up | Accepted (Hussein) |
| OpenBao backed up | Yes (Hussein) |

2026-10-30: there is still no Claude API key, so Hermes moves to OpenAI through a
ChatGPT/Codex login, as in UAT (Elie). The firewall opening above was for
`api.anthropic.com`; `chatgpt.com` and `auth.openai.com` are asked with CHG-KS-003.

Still open: the DNS record for `kubesight.areeba.com` → `10.43.10.238` (the
gateway); users' networks to `10.43.10.238:443` and to the nodes (the network
load balancer keeps the client's address; from the bastion it works, from
laptops it timed out on 2026-10-30); and the firewall openings for each
integration as it is turned on.

**Known gaps, to fix in the backend later:**

- Objects the backend creates at run time (build Jobs, their Secrets and
  NetworkPolicies) carry no `areeba.com/*` labels.
- CI stage images must be written in full (`docker.io/library/maven:3.9`, not
  `maven:3.9`), because CRI-O refuses short names.
- The backend and frontend run as root: allowed at `baseline`, warned at
  `restricted`.

## 5. The changes

Each change has its record: what, why, steps, check, undo, and evidence
afterwards. The record holds the exact commands. Run nothing without a
person's "yes".

| Change | What | Status |
|---|---|---|
| [CHG-KS-001](changes/CHG-KS-001-openbao-secrets.md) | KubeSight's secrets into OpenBao | Proposed |
| [CHG-KS-002](changes/CHG-KS-002-install.md) | Install KubeSight 1.0.0 without Hermes, and close its namespaces, checked with Hubble | Proposed (after CHG-KS-001 and the `1.0.0` images) |
| [CHG-KS-003](changes/CHG-KS-003-hermes.md) | Add Hermes, on OpenAI through a ChatGPT/Codex login | Waiting for the platform's agreement and the firewall opening |
| [CHG-KS-005](changes/CHG-KS-005-manage-this-cluster.md) | Let KubeSight show and manage this cluster (the backend's ClusterRoles, as in UAT) | Done 2026-10-30 |
| [CHG-KS-006](changes/CHG-KS-006-hermes-telegram.md) | Connect Hermes's gateway to Telegram (its own bot) | Done 2026-10-30 (Hermes answers on Telegram) |
| [CHG-KS-007](changes/CHG-KS-007-hermes-admin-and-skill.md) | KubeSight `1.0.5`: `hermes-agent` as an admin, Hermes reads its skill, waits for the backend at start | Proposed |

## 6. Module documents

A module card and four documents per module, in the layout the platform's
portal reads (`catalog-info.yaml`, `mkdocs.yml`, `docs/`). The system entry is
[modules/catalog-info.yaml](modules/catalog-info.yaml).

| Module | What |
|---|---|
| [kubesight-backend](modules/kubesight-backend/docs/index.md) | API, background work, MCP server |
| [kubesight-frontend](modules/kubesight-frontend/docs/index.md) | web page, `/api` proxy, TLS, load balancer |
| [hermes](modules/hermes/docs/index.md) | the agent, on OpenAI |
| [kubesight-ci](modules/kubesight-ci/docs/index.md) | build pods and analysis Jobs in `kubesight-ci` |
| [kubesight-buildkit](modules/kubesight-buildkit/docs/index.md) | rootless BuildKit in `kubesight-build` |
