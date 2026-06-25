# KubeSight — Dev → Production Documentation

This document captures the full technology stack, versions, and everything required to take
KubeSight from local development to a production Kubernetes deployment.

> **Scope:** Hermes is intentionally excluded from this document.

---

## 1. Technology Stack & Versions

### Backend

| Component | Technology | Version |
|-----------|-----------|---------|
| Language | Python | **3.12** (`python:3.12-slim` base image) |
| Web framework | Flask | `>=3.0.0,<4.0.0` |
| CORS | flask-cors | `>=4.0.0,<5.0.0` |
| ORM | Flask-SQLAlchemy | `>=3.1.1,<4.0.0` |
| DB driver | psycopg2-binary | `>=2.9.9,<3.0.0` |
| Config | python-dotenv | `>=1.0.1,<2.0.0` |
| YAML | PyYAML | `>=6.0.1,<7.0.0` |
| Auth (JWT) | PyJWT | `>=2.8.0,<3.0.0` |
| Crypto | cryptography | `>=42.0.0,<44.0.0` |
| WSGI server | gunicorn | `>=21.2.0,<23.0.0` |
| Testing | pytest | `>=8.0.0,<9.0.0` |
| Timezone data | tzdata | `>=2024.1` |
| `kubectl` (in image) | — | **v1.32.2** |
| `helm` (in image) | — | **v3.17.2** |

**Runtime command (production):**
```sh
gunicorn -w 1 --threads 8 -b 0.0.0.0:5000 --timeout 120 "api:create_app()"
```
Single worker keeps the in-process alert scheduler and caches singular; 8 threads provide
concurrency for blocking `kubectl`/`helm`/log-stream calls.

### Frontend

| Component | Technology | Version |
|-----------|-----------|---------|
| Build/runtime image | Node | **20** (`node:20-alpine`, build stage) |
| UI library | React | `^19.1.0` |
| React DOM | react-dom | `^19.1.0` |
| Build tool | Vite | `^6.3.5` |
| Vite React plugin | @vitejs/plugin-react | `^4.5.2` |
| Tests | Vitest | `^3.2.4` |
| Web server (prod) | nginx | `nginx:alpine` |

The frontend is built to static assets (`vite build` → `/dist`) and served by nginx. The SPA
falls back to `index.html` (`try_files $uri $uri/ /index.html`).

### Data & Platform

| Component | Technology | Version |
|-----------|-----------|---------|
| Database | PostgreSQL | **16** (`postgres:16`) |
| Local fallback DB | SQLite | bundled (used only if `DATABASE_URL` unset) |
| Orchestration | Kubernetes | targets `kubectl` v1.32.x API |
| Ingress | nginx ingress controller | `ingressClassName: nginx` |

---

## 2. Architecture

```
                      ┌──────────────────────────┐
   Browser  ──────►   │  Ingress (kubesight.local)│
                      └────────────┬─────────────┘
                       /api, /health │     / (SPA)
                   ┌───────────────┐ │ ┌──────────────────┐
                   │ backend-svc   │ │ │ frontend-svc      │
                   │ :5000 (Flask) │◄┘ │ :80 (nginx+React) │
                   └───────┬───────┘   └──────────────────┘
                           │
                   ┌───────▼────────┐        ┌───────────────────┐
                   │ postgres-svc   │        │ Target K8s cluster │
                   │ :5432          │        │ (kubectl / helm)   │
                   └────────────────┘        └───────────────────┘
```

| Layer | Stack |
|-------|-------|
| Frontend | React (Vite), client-side RBAC filtering, served via nginx |
| Backend | Flask + SQLAlchemy + JWT auth, gunicorn |
| Data | PostgreSQL (SQLite fallback in dev) |
| Kubernetes access | `kubectl` / `helm` subprocess (real mode) or mock fixtures |

Routing through the ingress:
- `/api`  → `backend-service:5000` (Flask blueprints)
- `/health` → `backend-service:5000` (health check, not under `/api`)
- `/`     → `frontend-service:80` (static React SPA)

The frontend is built with `VITE_API_BASE_URL=""` so it calls the API same-origin through the
ingress (`/api`).

---

## 3. Local Development

### Backend
```powershell
cd backend
pip install -r requirements.txt
# create .env and set at minimum JWT_SECRET_KEY
python app.py
```
- API: `http://127.0.0.1:5000` — health check: `GET /health`
- If `DATABASE_URL` is unset it falls back to SQLite (`kubesight.db`).
- Tables are auto-created and seeded (default users + one settings row) on startup.

### Frontend
```powershell
cd frontend
npm install
npm run dev
```
Open the Vite URL (usually `http://localhost:5173`).

### Default seeded users
| Username | Password | Role |
|----------|----------|------|
| `admin` | `admin123` | Admin (full access) |
| `operator` | `operator123` | Operator (clusters, alerts, upgrade precheck) |
| `viewer` | `viewer123` | Viewer (read-only, scoped clusters) |

> **Change/disable these before production.**

---

## 4. Configuration Reference

### ConfigMap (`k8s/configmap.yaml`) — non-secret settings
| Variable | Prod value | Description |
|----------|-----------|-------------|
| `APP_ENV` | `production` | Application environment |
| `FLASK_DEBUG` | `false` | Debug mode off in prod |
| `AUTH_REQUIRED` | `true` | Enforce JWT auth (only set `false` for local debug) |
| `JWT_EXPIRY_HOURS` | `8` | JWT token lifetime |
| `DATABASE_NAME` | `kubesight` | Postgres database name |
| `K8S_REAL_MODE` | `true` | Live `kubectl` mode (`false`=mock, `auto`=detect) |
| `K8S_CONTEXT_NAME` | `kubesight-prod` | In-cluster kube context name |
| `KUBESIGHT_KUBECONFIG_DIR` | `/data/kubeconfigs` | Stored custom-cluster kubeconfigs |
| `HELM_BINARY` | `helm` | Helm binary name/path |
| `ALERT_CPU_THRESHOLD_PERCENT` | `80` | CPU alert threshold |
| `CORS_ORIGINS` | `http://kubesight.local` | Allowed CORS origin(s) |
| `PUBLIC_API_URL` | `""` | Public API origin (empty = same-origin) |
| `KUBESIGHT_AUTO_UPGRADE` | `false` | Enable automated kubeadm/minikube upgrades |

### Secret (`k8s/secret.yaml`) — sensitive values
| Key | Description |
|-----|-------------|
| `DATABASE_USER` | Postgres user (must match `DATABASE_URL`) |
| `DATABASE_PASSWORD` | Postgres password (must match `DATABASE_URL`) |
| `DATABASE_URL` | `postgresql+psycopg2://kubesight:...@postgres-service:5432/kubesight` |
| `JWT_SECRET_KEY` | **Regenerate** before prod: `openssl rand -hex 32` |
| `FLASK_SECRET_KEY` | **Regenerate** before prod: `openssl rand -hex 32` |

### Optional env (SMTP for alert email)
| Variable | Description |
|----------|-------------|
| `SMTP_HOST` / `SMTP_PORT` | SMTP server (e.g. `smtp.gmail.com` / `587`) |
| `SMTP_FROM` | From address |
| `SMTP_USER` / `SMTP_PASSWORD` | SMTP credentials (use an app password) |
| `SMTP_USE_TLS` | `true` / `false` |

---

## 5. Building Images

```powershell
# Backend  (installs kubectl v1.32.2 + helm v3.17.2 into the image)
docker build -t kubesight-backend:v2 ./backend

# Frontend (multi-stage: node:20 build → nginx:alpine serve)
docker build -t kubesight-frontend:v2 ./frontend
```

The deployments reference `kubesight-backend:v2` and `kubesight-frontend:v2` with
`imagePullPolicy: IfNotPresent`. For a remote cluster, tag and push to your registry and update
the `image:` fields in `k8s/backend-deployment.yaml` and `k8s/frontend-deployment.yaml`
(set `imagePullPolicy: Always` for registry-hosted images).

---

## 6. Production Deployment (Kubernetes)

All manifests live in `k8s/`. Apply in dependency order:

```powershell
# 1. Namespace
kubectl apply -f k8s/namespace.yaml

# 2. RBAC — ServiceAccount + reader/deployer ClusterRoles & bindings
kubectl apply -f k8s/rbac.yaml

# 3. Config & secrets  (EDIT secret.yaml FIRST — see hardening below)
kubectl apply -f k8s/configmap.yaml
kubectl apply -f k8s/secret.yaml

# 4. Storage (PersistentVolumes + Claims)
kubectl apply -f k8s/postgres-pv.yaml
kubectl apply -f k8s/postgres-pvc.yaml
kubectl apply -f k8s/kubeconfig-pv.yaml
kubectl apply -f k8s/kubeconfig-pvc.yaml

# 5. Database
kubectl apply -f k8s/postgres-deployment.yaml

# 6. Application
kubectl apply -f k8s/backend-deployment.yaml
kubectl apply -f k8s/frontend-deployment.yaml

# 7. Ingress
kubectl apply -f k8s/ingress.yaml
```

### Deployment summary

| Workload | Image | Replicas | Port | Requests | Limits |
|----------|-------|----------|------|----------|--------|
| `kubesight-backend` | `kubesight-backend:v2` | 1 | 5000 | 250m CPU / 512Mi | 1 CPU / 1Gi |
| `kubesight-frontend` | `kubesight-frontend:v2` | 1 | 80 | 50m CPU / 64Mi | 200m CPU / 128Mi |
| `postgres` | `postgres:16` | 1 | 5432 | 250m CPU / 256Mi | 1 CPU / 512Mi |

- **Services** (all `ClusterIP`): `backend-service:5000`, `frontend-service:80`, `postgres-service:5432`
- **Health probes:** backend `GET /health`, frontend `GET /`, postgres `pg_isready`
- **Persistent storage:** `postgres-pvc` (5Gi), `kubeconfig-pvc` (1Gi), both `storageClassName: manual`

### Kubernetes access for the backend

The backend resolves cluster credentials at startup (`k8s_entrypoint.sh`) in this order:
1. `K8S_KUBECONFIG` env path (from ConfigMap), if the file exists.
2. Mounted secret at `/etc/kubeconfig/config` (optional `host-kubeconfig` secret).
3. **In-cluster ServiceAccount** token — used when the pod manages the cluster it runs in
   (relies on the RBAC ClusterRoles in `rbac.yaml`).

To grant access to the host's kubeconfig (multi-context):
```powershell
kubectl create secret generic host-kubeconfig -n kubesight `
  --from-file=config=$env:USERPROFILE\.kube\config
# then set K8S_KUBECONFIG=/etc/kubeconfig/config in the ConfigMap
```

The `kubesight-backend` ServiceAccount has two ClusterRoles:
- **`kubesight-backend-reader`** — `get/list/watch` on nodes, pods, pods/log, services, deployments,
  metrics, ingresses, jobs, etc. (read-only fleet visibility).
- **`kubesight-backend-deployer`** — full CRUD on deployments, services, configmaps, secrets, PVCs,
  jobs, ingresses, HPAs (for Application Builder / YAML & Helm deploy). App-layer RBAC still applies.

### Verify the rollout
```powershell
kubectl get pods -n kubesight
kubectl get svc,ingress -n kubesight
kubectl logs -n kubesight deploy/kubesight-backend
```
Add `kubesight.local` to DNS/hosts pointing at the ingress controller, then open
`http://kubesight.local`.

---
## 7. Pre-Production Hardening Checklist

- [ ] **Regenerate `JWT_SECRET_KEY` and `FLASK_SECRET_KEY`** — `openssl rand -hex 32`. The
      committed values in `secret.yaml` are placeholders and must never reach production.
- [ ] **Change the Postgres password** (`DATABASE_USER`/`DATABASE_PASSWORD` and `DATABASE_URL`).
- [ ] **Change or disable the default users** (`admin`/`operator`/`viewer` and their passwords).
- [ ] **Manage secrets out-of-band** — use a sealed-secrets/Vault/SOPS workflow instead of
      committing `k8s/secret.yaml`. Confirm it is gitignored or scrubbed.
- [ ] **TLS** — terminate HTTPS at the ingress (cert-manager / real certificate); update
      `CORS_ORIGINS` and the ingress `host` to your real domain.
- [ ] **`AUTH_REQUIRED=true`** and **`FLASK_DEBUG=false`** (already set in the prod ConfigMap).
- [ ] **metrics-server** installed on target clusters so `kubectl top pods` (CPU alerts) works.
- [ ] **Persistent volumes** — replace `storageClassName: manual` PVs with a real
      StorageClass (cloud disk) for durable Postgres data; plan DB backups.
- [ ] **Scope kubeconfig/ServiceAccount permissions** to only the clusters/namespaces required.
- [ ] **Configure SMTP** if alert email delivery is needed.
- [ ] **Set resource requests/limits and replicas** appropriately for production load.

## 8. Deploying to Production — Step by Step

The steps in §6 work for a single-node / local-style cluster. For a **real production deployment**
on a remote cluster (cloud or on-prem), follow this end-to-end runbook.

### Step 0 — Prerequisites
- A running Kubernetes cluster (GKE/EKS/AKS/on-prem) and `kubectl` pointed at it
  (`kubectl config current-context`).
- A container registry you can push to (e.g. GHCR, ECR, GCR, Docker Hub).
- An **ingress controller** installed (nginx ingress) and a DNS record for your domain.
- **metrics-server** installed (required for CPU alerts / `kubectl top`).
- A TLS strategy — `cert-manager` recommended for automatic certificates.

### Step 1 — Complete the hardening checklist
Do **everything in §7 first** (new secrets, DB password, default users, TLS, real
StorageClass). Do not deploy with the placeholder secrets in the repo.

### Step 2 — Build, tag, and push images to your registry
```powershell
$REG = "ghcr.io/<your-org>"      # your registry/namespace
$TAG = "v2"                       # use an immutable version tag, not :latest

docker build -t $REG/kubesight-backend:$TAG  ./backend
docker build -t $REG/kubesight-frontend:$TAG ./frontend

docker push $REG/kubesight-backend:$TAG
docker push $REG/kubesight-frontend:$TAG
```

### Step 3 — Point the manifests at your registry images
Edit the `image:` fields and pull policy:
- `k8s/backend-deployment.yaml`  → `image: ghcr.io/<your-org>/kubesight-backend:v2`
- `k8s/frontend-deployment.yaml` → `image: ghcr.io/<your-org>/kubesight-frontend:v2`
- Set `imagePullPolicy: Always` for both (registry-hosted images).
- If the registry is private, create a pull secret and reference it:
  ```powershell
  kubectl create secret docker-registry regcred -n kubesight `
    --docker-server=ghcr.io --docker-username=<user> --docker-password=<token>
  # add `imagePullSecrets: [{name: regcred}]` to each pod spec
  ```

### Step 4 — Adjust config for the real environment
- **`k8s/configmap.yaml`**: set `CORS_ORIGINS` to your real domain (e.g. `https://kubesight.example.com`),
  keep `APP_ENV=production`, `FLASK_DEBUG=false`, `AUTH_REQUIRED=true`.
- **`k8s/ingress.yaml`**: change `host: kubesight.local` to your domain and add TLS (see Step 7).
- **`k8s/secret.yaml`**: apply the rotated secrets via your out-of-band workflow
  (sealed-secrets/Vault/SOPS) rather than committing them.
- **Storage**: replace `storageClassName: manual` in the PV/PVC files with a real cloud
  StorageClass (e.g. `gp3`, `standard-rwo`, `premium-rwo`) and remove the manual `hostPath` PVs.

### Step 5 — Apply manifests in order
```powershell
kubectl apply -f k8s/namespace.yaml
kubectl apply -f k8s/rbac.yaml
kubectl apply -f k8s/configmap.yaml
kubectl apply -f k8s/secret.yaml          # or your sealed-secret equivalent
kubectl apply -f k8s/postgres-pvc.yaml    # + your StorageClass-backed PV if needed
kubectl apply -f k8s/kubeconfig-pvc.yaml
kubectl apply -f k8s/postgres-deployment.yaml
kubectl apply -f k8s/backend-deployment.yaml
kubectl apply -f k8s/frontend-deployment.yaml
kubectl apply -f k8s/ingress.yaml
```

### Step 6 — Wait for rollout and verify
```powershell
kubectl rollout status deploy/postgres            -n kubesight
kubectl rollout status deploy/kubesight-backend   -n kubesight
kubectl rollout status deploy/kubesight-frontend  -n kubesight

kubectl get pods,svc,ingress -n kubesight
kubectl logs -n kubesight deploy/kubesight-backend
```
On first start the backend auto-creates tables and seeds data. Confirm the health check:
```powershell
kubectl exec -n kubesight deploy/kubesight-backend -- curl -s localhost:5000/health
```

### Step 7 — DNS + TLS
- Point your domain's DNS A/CNAME record at the ingress controller's external IP/hostname:
  ```powershell
  kubectl get svc -n ingress-nginx        # find the LoadBalancer EXTERNAL-IP
  ```
- Add TLS to the ingress (with cert-manager):
  ```yaml
  metadata:
    annotations:
      cert-manager.io/cluster-issuer: letsencrypt-prod
  spec:
    tls:
      - hosts: [kubesight.example.com]
        secretName: kubesight-tls
  ```

### Step 8 — First-login smoke test
- Open `https://kubesight.example.com`, log in, and immediately **change the default admin
  password / disable seed users**.
- Verify: clusters list, a namespace's resources, log streaming, and an alert path
  (needs metrics-server).

### Step 9 — Post-deploy: scaling, backups, monitoring
- **Scale** the frontend/backend replicas as needed. Note: the backend runs a single gunicorn
  worker with an in-process alert scheduler — when scaling backend replicas >1, ensure only one
  instance runs the scheduler (or front it accordingly) to avoid duplicate alert emails.
- **Database backups**: schedule `pg_dump`/volume snapshots for the Postgres PVC.
- **Monitoring**: watch pod restarts, probe failures, and backend logs.

### Updating to a new version (rolling deploy)
```powershell
docker build -t $REG/kubesight-backend:v3 ./backend ; docker push $REG/kubesight-backend:v3
kubectl set image deploy/kubesight-backend backend=$REG/kubesight-backend:v3 -n kubesight
kubectl rollout status deploy/kubesight-backend -n kubesight
# roll back if needed:
kubectl rollout undo deploy/kubesight-backend -n kubesight
```

---

## 

---

## 9. Operations Notes

- **Alerts:** fire when `(cpu_usage / cpu_limit) * 100 > ALERT_CPU_THRESHOLD_PERCENT` (default 80).
  Requires metrics-server. Email is sent once per firing alert on each `GET /api/alerts` poll until
  it clears.
- **Upgrade Safe Mode:** runs real `kubectl` prechecks (nodes ready, API health, kube-system pods).
  It does **not** change a cloud control-plane version — apply GKE/EKS/AKS/kubeadm upgrades after
  prechecks pass. Automated upgrades only with `KUBESIGHT_AUTO_UPGRADE=true` (kubeadm/minikube).
- **Custom clusters:** registered via the UI with kubeconfig auth; stored under
  `KUBESIGHT_KUBECONFIG_DIR` (`/data/kubeconfigs`, backed by `kubeconfig-pvc`). Never commit
  kubeconfigs.
- **Audit logs:** `GET /api/audit-logs` (admin) — logins, user changes, forbidden attempts.

### Troubleshooting
| Issue | What to check |
|-------|---------------|
| Backend down | Pod status, port 5000, `DATABASE_URL`, `kubectl logs deploy/kubesight-backend` |
| Frontend cannot reach API | Ingress routing of `/api`, `VITE_API_BASE_URL` build value |
| Real mode shows no clusters | Valid kubeconfig/ServiceAccount + RBAC, reachable API server |
| Metrics/alerts missing | metrics-server installed (`kubectl top pods`) |
| 401 / 403 | Token expired or role/access rules block the action |
| Email not sent | `SMTP_*` values, routing enabled on Alerts page |
| Postgres won't start | PVC bound, `PGDATA` path, credentials match secret |
```