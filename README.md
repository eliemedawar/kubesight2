# KubeSight

KubeSight is a company Kubernetes operations dashboard: fleet visibility, workload inspection, log streaming, alerting, upgrade prechecks, and user/RBAC administration.

## Architecture

| Layer | Stack |
|--------|--------|
| Frontend | React (Vite), client-side RBAC filtering |
| Backend | Flask, SQLAlchemy, JWT auth |
| Data | PostgreSQL or SQLite |
| Kubernetes | `kubectl` subprocess (real mode) or mock fixtures |

```
frontend/src/          React UI (pages, components, hooks, api/)
backend/api/           Flask blueprints, services, access engine
backend/api/services/  Business logic (routes stay thin)
backend/tests/         pytest suite
```

## Features

- **Dashboard & clusters** — health, nodes, alerts summary
- **Cluster Management** — register custom clusters with kubeconfig
- **Namespaces & resources** — pods, deployments, services (RBAC-filtered)
- **Logs** — live/previous pod logs
- **Alerts** — CPU threshold alerts, email/Slack/webhook routing
- **Upgrade Safe Mode** — precheck + controlled workflow
- **User management** — roles, permissions, fine-grained access rules
- **Audit logs** — security-relevant actions

## Quick start

### Backend

```powershell
cd backend
pip install -r requirements.txt
copy .env.example .env
# Edit JWT_SECRET_KEY in .env
python app.py
```

API: http://127.0.0.1:5000 — health check: `GET /health`

### Frontend

```powershell
cd frontend
npm install
npm run dev
```

Open the Vite URL (usually http://localhost:5173). Sign in with seeded users below.

## Default users

| Username | Password | Role |
|----------|----------|------|
| `admin` | `admin123` | Admin (full access) |
| `operator` | `operator123` | Operator (clusters, alerts, upgrade precheck) |
| `viewer` | `viewer123` | Viewer (read-only, scoped clusters) |

## Environment variables

| Variable | Description |
|----------|-------------|
| `DATABASE_URL` | SQLAlchemy URL (falls back to SQLite if Postgres unreachable) |
| `JWT_SECRET_KEY` | Required for production |
| `JWT_EXPIRY_HOURS` | Token lifetime (default 8) |
| `K8S_REAL_MODE` | `false` mock, `true` live kubectl, `auto` detect contexts |
| `K8S_KUBECONFIG` | Optional kubeconfig path |
| `KUBESIGHT_KUBECONFIG_DIR` | Stored custom cluster kubeconfigs |
| `ALERT_CPU_THRESHOLD_PERCENT` | CPU alert threshold (default 80) |
| `SMTP_*` | Email delivery for alerts |
| `VITE_API_BASE_URL` | Frontend API origin (see `frontend/.env.example`) |

Copy templates: [backend/.env.example](backend/.env.example), [frontend/.env.example](frontend/.env.example)

## Mock vs real Kubernetes

- **Mock** (`K8S_REAL_MODE=false`): deterministic demo data, no cluster required.
- **Real** (`K8S_REAL_MODE=true` or `auto` with kubectl contexts): live `kubectl` for list/get/logs/top.
- **Custom clusters**: add via UI; kubeconfigs stored under `backend/data/kubeconfigs/` (gitignored).

## RBAC

- JWT on all `/api/*` routes (except login/health).
- Role permissions + optional per-user access rules (cluster/namespace/resource).
- Frontend hides unauthorized UI; backend always enforces.

Details: [backend/README.md](backend/README.md)

## Testing

### Default (SQLite, no Docker)

```powershell
cd backend
pip install -r requirements.txt
python -m pytest tests
```

Uses in-memory SQLite when `TEST_DATABASE_URL` is not set. Kubernetes stays in mock mode; `kubectl` is mocked where needed.

### PostgreSQL (closer to production)

Create a dedicated test database (not your production DB):

```powershell
createdb kubesight_test
```

Or with Docker Compose:

```powershell
docker compose -f docker-compose.test.yml up -d
```

Then run tests with `TEST_DATABASE_URL` (credentials are examples only — use your own test user/password):

```powershell
# Local PostgreSQL
$env:TEST_DATABASE_URL="postgresql://kubesight_test:kubesight_test@localhost:5432/kubesight_test"
cd backend
python -m pytest tests

# Docker Compose (port 5433 on host)
$env:TEST_DATABASE_URL="postgresql://kubesight_test:kubesight_test@localhost:5433/kubesight_test"
python -m pytest tests
```

From repo root:

```powershell
$env:TEST_DATABASE_URL="postgresql://kubesight_test:kubesight_test@localhost:5433/kubesight_test"
python -m pytest backend/tests
```

Tests refuse to run when `FLASK_ENV` or `APP_ENV` is `production`. They never use production `DATABASE_URL` — only `TEST_DATABASE_URL` or in-memory SQLite. Each test gets a clean schema (`drop_all` / `create_all` / seed).

## Troubleshooting

| Issue | What to check |
|-------|----------------|
| Backend down | `python app.py`, port 5000, `DATABASE_URL` |
| Frontend cannot reach API | Vite proxy (dev) or `VITE_API_BASE_URL` / `public/config.js` |
| Real mode empty | Docker Desktop Kubernetes running, valid `~/.kube/config` |
| Metrics/alerts missing | metrics-server installed (`kubectl top pods`) |
| Invalid kubeconfig | Re-test connection in Cluster Management |
| 401 / 403 | Token expired, or role lacks permission |
| Email not sent | SMTP settings in `.env`, routing enabled on Alerts page |

## Project layout

```
frontend/src/
  api/           HTTP client modules
  components/    layout, common UI, feature widgets
  context/       AuthContext
  hooks/         usePermission, etc.
  pages/         Route-level screens
  utils/         authz.js, formatters.js

backend/api/
  routes/        HTTP handlers
  services/      Business logic
  access_engine.py
  k8s_provider.py
```

## License

Internal / company use — configure secrets and production hardening before deployment.
