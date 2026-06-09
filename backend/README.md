# Backend Database Setup (PostgreSQL)

This backend now supports PostgreSQL through SQLAlchemy.
It can also run in two Kubernetes data modes:
- `K8S_REAL_MODE=false` (default): mock cluster/resource data
- `K8S_REAL_MODE=true`: live data via `kubectl` + kubeconfig contexts

## 1) Install dependencies

```powershell
cd backend
pip install -r requirements.txt
```

## 2) Configure database URL

Copy `.env.example` and set `DATABASE_URL`, or export directly in your shell:

```powershell
$env:DATABASE_URL="postgresql+psycopg2://postgres:postgres@localhost:5432/kubesight"
$env:K8S_REAL_MODE="true"
# Optional when kubeconfig is not in default location:
# $env:K8S_KUBECONFIG="$env:USERPROFILE\.kube\config"
```

If `DATABASE_URL` is not set, the app uses SQLite (`kubesight.db`) for local fallback.

## 3) Run API

```powershell
python app.py
```

On startup, the app creates tables automatically and seeds:
- Users (`admin`, `viewer`)
- One global settings row

## Real Kubernetes mode checklist

When using `K8S_REAL_MODE=true`:
- Ensure `kubectl` is installed and accessible in PATH.
- Ensure kubeconfig contains the contexts you want to expose.
- Cluster IDs in API are derived from context names (sanitized).
- Logs endpoint calls `kubectl logs` for selected context/namespace/pod.

## Cluster Management (custom clusters)

Use the **Cluster Management** page in the UI (or the API below) to register company clusters with **kubeconfig authentication** — DNS and port alone are not enough for real access.

- `GET /api/clusters/custom` — list registered clusters (no kubeconfig content returned)
- `POST /api/clusters/custom` — add cluster (`name`, `host`, `port`, `protocol`, `kubeconfigContent`, optional `contextName`)
- `PUT /api/clusters/custom/:id` — update metadata and/or replace kubeconfig
- `DELETE /api/clusters/custom/:id` — soft-delete (`is_active=false`)
- `POST /api/clusters/custom/:id/test` — run `kubectl cluster-info` and `kubectl get nodes`

Custom clusters appear in `GET /api/clusters` with IDs like `custom-1`, `custom-2`, and work across Overview, Namespaces, Resources, Logs, Alerts, and Upgrade Safe Mode.

Kubeconfig files are written under `backend/data/kubeconfigs/` by default (override with `KUBESIGHT_KUBECONFIG_DIR`). This directory is gitignored — do not commit credentials.

## Authentication & access control

The API uses JWT Bearer authentication. Sign in via the UI or `POST /api/auth/login`.

Default seeded users (passwords stored hashed):

| Username | Password   | Role    |
|----------|------------|---------|
| `admin`  | `admin123` | Admin   |
| `viewer` | `viewer123`| Viewer  |

Configure in `.env`:

```powershell
$env:JWT_SECRET_KEY="your-long-random-secret-at-least-32-characters"
$env:JWT_EXPIRY_HOURS="8"
$env:AUTH_REQUIRED="true"
```

**Roles:** `admin` (full access), `operator` (read + alerts + upgrade precheck), `viewer` (read-only on allowed clusters).

**Permissions** are enforced on every protected route (e.g. `clusters:view`, `users:view`, `settings:manage`). Admins bypass cluster/namespace restrictions.

**Cluster & namespace access:** Non-admin users can be limited to specific cluster IDs (`custom-1`, mock IDs, or kubectl context IDs) and optional namespace rows. Empty access lists for admin mean all clusters.

**Fine-grained access rules (`access_rules` table):** Per-user rules with `resource_type` (cluster, namespace, pod, deployment, service, container, service_port), `permission_key`, and `effect` (allow/deny). More specific rules override broader ones; deny wins at the same specificity. APIs:

- `GET/PUT /api/users/:id/access-rules` — list or replace all rules
- `POST/PUT/DELETE /api/users/:id/access-rules/:ruleId` — single rule CRUD

User create/update accepts `accessRules` in the JSON body. Logs require `logs:view` on the pod/container; service ports require `services:ports:view` plus a matching rule.

**User management:** `GET/POST/PUT/DELETE /api/users` (admin). `DELETE` soft-disables users (`is_active=false`).

**Audit logs:** `GET /api/audit-logs` (admin) — login, user changes, forbidden attempts.

Set `AUTH_REQUIRED=false` only for temporary local debugging without tokens.

## Alerts (CPU threshold)

Alerts fire when `(cpu_usage / cpu_limit) * 100` exceeds **80%** (override with `ALERT_CPU_THRESHOLD_PERCENT`).

Requires **metrics-server** so `kubectl top pods` works.

## Upgrades

Upgrade Safe Mode runs real kubectl prechecks (nodes ready, API health, kube-system pods) and a step-by-step workflow. It does not change your cloud control plane version — apply provider upgrades (GKE/EKS/AKS/kubeadm) after prechecks pass.

## Alert email delivery

When email routing is enabled in the UI, firing alerts trigger SMTP delivery on each `GET /api/alerts` poll (each alert is emailed once until it clears).

Configure SMTP in `.env`:

```powershell
SMTP_HOST=smtp.gmail.com
SMTP_PORT=587
SMTP_FROM=alerts@yourdomain.com
SMTP_USER=your-user
SMTP_PASSWORD=your-app-password
SMTP_USE_TLS=true
```

Local testing with [Mailpit](https://github.com/axllent/mailpit):

```powershell
docker run -p 1025:1025 -p 8025:8025 axllent/mailpit
# SMTP_HOST=localhost SMTP_PORT=1025 SMTP_USE_TLS=false
# Open http://localhost:8025 to read captured mail
```

Send a manual test from the UI (**Alerts → Edit Routing → Send test email**) or:

```powershell
curl -X POST http://localhost:5000/api/alerts/notifications/email/test
```

## Running tests

### SQLite (default)

```powershell
cd backend
pip install -r requirements.txt
python -m pytest tests
```

If `TEST_DATABASE_URL` is unset, tests use `sqlite:///:memory:` via `TestingConfig`.

### PostgreSQL test database

Use a **separate** database — never point tests at production `DATABASE_URL`.

```powershell
createdb kubesight_test
```

```powershell
$env:TEST_DATABASE_URL="postgresql://kubesight_test:kubesight_test@localhost:5432/kubesight_test"
python -m pytest tests
```

With Docker Compose from the repo root:

```powershell
docker compose -f docker-compose.test.yml up -d
$env:TEST_DATABASE_URL="postgresql://kubesight_test:kubesight_test@localhost:5433/kubesight_test"
python -m pytest backend/tests
```

### Safety

- `FLASK_ENV=production` or `APP_ENV=production` → pytest exits immediately
- Production `DATABASE_URL` is not used; tests set `DATABASE_URL` to the test URI
- `K8S_REAL_MODE=false` for all tests; `kubectl` remains mocked in cluster tests
- Each test function gets a clean database (drop/create/seed), no ordering dependency

Tests cover authentication, RBAC, cluster filtering, custom cluster CRUD, alerts, upgrades, and settings.

## Troubleshooting

| Symptom | Likely cause |
|---------|----------------|
| API unreachable | Backend not running on port 5000 |
| Real mode shows no clusters | Docker Desktop Kubernetes stopped or empty kubeconfig |
| `kubectl top` / alerts empty | metrics-server not installed |
| Custom cluster test fails | Invalid kubeconfig or unreachable API server |
| 401 Unauthorized | Missing/expired JWT — sign in again |
| 403 Forbidden | Role or access rules block the action |
| SMTP errors | Wrong `SMTP_*` values or Mailpit not listening |

