# KubeSight — Architecture & Feature Audit

**Audit date:** 2026-06-03  
**Scope:** Full repository (`frontend/`, `backend/`, tests, configuration)  
**Method:** Static code review — no application code was modified.  
**Audience:** External architect / security review.

---

## SECTION 1 — PROJECT OVERVIEW

### Purpose of the application

KubeSight is an internal **Kubernetes operations dashboard** for fleet visibility and day‑2 operations: cluster registration, workload inspection, log retrieval, CPU-based alerting, upgrade prechecks/workflows, application inventory/catalog, Helm lifecycle, and user/RBAC administration with audit logging.

It targets **company operators and viewers** managing multiple clusters (mock demo data, local kubeconfig contexts, or DB-registered custom clusters).

### Main features

| Area | Capability |
|------|------------|
| Dashboard | Per-cluster health, utilization, version status, alerts summary, inventory summary, audit/operational activity |
| Clusters | List discovered + custom clusters; overview, namespaces, resources (pods/deployments/services) |
| Cluster Management | CRUD custom clusters via kubeconfig (or manual token/cert kubeconfig build) |
| Logs | Pod log snapshot (live flag exists; not true streaming WebSocket) |
| Alerts | CPU threshold alerts from `kubectl top`; email on poll; routing UI for Slack/webhook (delivery incomplete) |
| Upgrade Safe Mode | Extended prechecks, provider detection, plan/instructions; limited automated execution |
| Inventory | App discovery/grouping, catalog metadata, YAML + image deploy, Helm integration |
| Users & RBAC | Roles, permissions, legacy cluster/namespace grants, fine-grained `access_rules` |
| Settings | Theme, refresh interval, default cluster, notification routing |
| Audit | Security-relevant actions logged to DB |

### Architecture overview

```
┌─────────────────────────────────────────────────────────────────┐
│  Browser (React + Vite SPA)                                      │
│  - AuthContext + localStorage JWT                                │
│  - Client-side RBAC mirroring access_engine                      │
└───────────────────────────┬─────────────────────────────────────┘
                            │ HTTPS/HTTP REST (Bearer JWT)
┌───────────────────────────▼─────────────────────────────────────┐
│  Flask API (blueprints → thin routes → services)                 │
│  - decorators: require_auth, require_permission, cluster/ns    │
│  - access_engine.py (RBAC + access rules)                        │
└───────────┬─────────────────────────────┬───────────────────────┘
            │ SQLAlchemy                     │ subprocess kubectl
┌───────────▼──────────┐         ┌──────────▼──────────────────────┐
│ PostgreSQL / SQLite  │         │ Kubernetes API (real mode)       │
│ users, roles, clusters│         │ or mock_data.py fixtures       │
│ catalog, audit, etc. │         └─────────────────────────────────┘
└──────────────────────┘
```

**Deployment shape:** Single Flask process + static/Vite frontend; no separate worker queue, no ingress controller integration beyond what operators configure externally.

### Frontend stack

- **React 19** + **Vite 6**
- No React Router — **page state** in `App.jsx` (`activePage` string)
- CSS in `index.css` (no component library)
- API modules under `frontend/src/api/` with shared `client.js` (fetch + JWT header)
- Auth: `AuthContext`, `authStorage.js` (localStorage)

### Backend stack

- **Flask 3** + **Flask-CORS** + **Flask-SQLAlchemy 3**
- **PyJWT** (HS256), **PyYAML**, **python-dotenv**
- **pytest** test suite
- Optional **PostgreSQL** (`psycopg2-binary`); falls back to SQLite if Postgres unreachable

### Database

- SQLAlchemy ORM; tables created on startup (`db.create_all` via app init + seed)
- `migrate_rbac.py` for legacy column migrations (SQLite ALTER limitations acknowledged)
- Single-row `app_settings` pattern (id=1 implied by `query.first()`)

### Authentication

- **JWT Bearer** on protected `/api/*` routes
- `POST /api/auth/login` → `{ token, user }`
- `GET /api/auth/me` → profile with permissions + access payload
- `POST /api/auth/logout` — **client-side oriented** (no server-side token blocklist)
- `AUTH_REQUIRED=false` bypasses auth decorators (debug only)
- Default dev secret: `kubesight-dev-secret-change-me` if `JWT_SECRET_KEY` unset

### RBAC model

1. **Role permissions** — many-to-many `roles` ↔ `permissions` (43 permission keys in `rbac_data.py`)
2. **Admin bypass** — role name `admin` skips cluster/namespace/resource checks
3. **Legacy grants** — `user_cluster_access`, `user_namespace_access` (used when user has **no** `access_rules`)
4. **Fine-grained rules** — `access_rules` table: cluster/namespace/resource_type/name/container/port, `permission_key`, `effect` (allow/deny), specificity ordering, deny wins at same specificity
5. **Enforcement** — backend decorators + inline checks (`can_view_logs`, deployment access, etc.); frontend mirrors logic in `utils/authz.js` + `lib/grantedAccess.js`

### Kubernetes integration approach

- **Not** an in-cluster operator or client-go library
- **`kubectl` subprocess** with optional `--kubeconfig` and `--context`
- **Modes:**
  - `K8S_REAL_MODE=false` → mock fixtures (`mock_data.py`), except active custom DB clusters still use real kubectl
  - `K8S_REAL_MODE=true` → live kubectl against default kubeconfig contexts
  - `K8S_REAL_MODE=auto` (default) → real if kubectl has contexts **or** custom clusters exist
- **Custom clusters:** kubeconfig YAML written to `backend/data/kubeconfigs/cluster-{id}.yaml` (gitignored)
- **Metrics/alerts:** require metrics-server (`kubectl top pods`)

---

## SECTION 2 — FILE STRUCTURE

Important paths only (excluding `node_modules/`, `frontend/dist/`, `.pytest_cache/`).

```
test/
├── README.md
├── PROJECT_AUDIT.md
├── .env.example
├── docker-compose.test.yml
│
├── frontend/
│   ├── package.json
│   ├── vite.config.js
│   ├── index.html
│   ├── .env.example
│   └── src/
│       ├── main.jsx
│       ├── App.jsx                    # Monolithic SPA shell (~1062 lines)
│       ├── index.css
│       ├── authStorage.js
│       ├── api.js                       # Re-exports (deprecated barrel)
│       ├── api/
│       │   ├── client.js
│       │   ├── authApi.js
│       │   ├── clustersApi.js
│       │   ├── dashboardApi.js
│       │   ├── alertsApi.js
│       │   ├── settingsApi.js
│       │   ├── inventoryApi.js
│       │   ├── upgradesApi.js
│       │   ├── usersApi.js
│       │   └── helmApi.js
│       ├── context/
│       │   └── AuthContext.jsx
│       ├── hooks/
│       │   └── usePermission.js
│       ├── lib/
│       │   ├── authAccess.js            # Re-export of authz
│       │   ├── grantedAccess.js
│       │   ├── permissionCatalog.js
│       │   ├── rolePresets.js
│       │   ├── accessRulesForm.js
│       │   ├── userAccessForm.js
│       │   ├── accessActions.js
│       │   └── alertDisplay.js
│       ├── utils/
│       │   ├── authz.js                 # Client RBAC engine
│       │   ├── formatters.js
│       │   └── dashboardStatus.js
│       ├── dashboard/
│       │   ├── widgetRegistry.js
│       │   ├── widgetVisibility.js
│       │   └── widgets/
│       │       └── DashboardWidgets.jsx
│       ├── pages/                       # 15 page components
│       └── components/
│           ├── layout/                  # AppShell, Sidebar, Topbar, Notifications
│           ├── common/                  # StatCard, DataTable, EmptyState, etc.
│           ├── auth/
│           ├── alerts/
│           ├── inventory/
│           └── user-management/
│
└── backend/
    ├── app.py
    ├── requirements.txt
    ├── .env.example
    ├── README.md
    ├── data/kubeconfigs/              # Runtime secrets (gitignored)
    └── api/
        ├── __init__.py                # create_app, health, seed
        ├── models.py
        ├── db.py
        ├── seed.py
        ├── migrate_rbac.py
        ├── rbac_data.py
        ├── auth_utils.py
        ├── decorators.py
        ├── access_engine.py
        ├── access.py                  # Facade over access_engine
        ├── access_rules.py
        ├── access_summary.py
        ├── cluster_access.py
        ├── cluster_store.py
        ├── kubeconfig_builder.py
        ├── k8s_provider.py              # kubectl integration (~670+ lines)
        ├── k8s_metrics.py
        ├── mock_data.py
        ├── dashboard_intelligence.py
        ├── upgrade_provider.py
        ├── alert_notifier.py
        ├── email_delivery.py
        ├── notification_routing.py
        ├── audit.py
        ├── serializers.py
        ├── passwords.py
        ├── response.py
        └── routes/                    # HTTP blueprints
        └── services/                  # Business logic
    └── tests/                         # 18 test modules, 100+ cases
```

---

## SECTION 3 — FRONTEND

### Pages

| Page key | File | Nav permission |
|----------|------|----------------|
| `dashboard` | `DashboardPage.jsx` | `overview:view` |
| `clusters` | `ClustersPage.jsx` | `clusters:view` |
| `clusterManagement` | `ClusterManagementPage.jsx` | any of `clusters:add/update/remove/test` |
| `clusterOverview` | `ClusterOverviewPage.jsx` | `overview:view` |
| `inventory` | `InventoryPage.jsx` | `inventory:view` |
| `applicationDetails` | `ApplicationDetailsPage.jsx` | (via inventory) `inventory:view` |
| `namespaces` | `NamespacesPage.jsx` | `namespaces:view` |
| `resources` | `ResourcesPage.jsx` | `resources:view` |
| `logs` | `LogsPage.jsx` | `logs:view` |
| `alerts` | `AlertsPage.jsx` | `alerts:view` |
| `upgrade` | `UpgradeSafeModePage.jsx` | `upgrades:precheck` or `upgrades:start` |
| `settings` | `SettingsPage.jsx` | `settings:view` |
| `userManagement` | `UserManagementPage.jsx` | `users:view` |
| `auditLogs` | `AuditLogsPage.jsx` | `audit:view` |
| `login` | `LoginPage.jsx` | (unauthenticated) |
| `accessDenied` | `AccessDeniedPage.jsx` | fallback |

Routing is **not** URL-based; `App.jsx` switches on `activePage`.

### Components (grouped)

**Layout:** `AppShell`, `Sidebar`, `Topbar`, `NotificationsDropdown`  
**Common:** `PageTitle`, `StatCard`, `InfoCard`, `DataTable`, `EmptyState`, `ErrorBanner`, `LoadingState`  
**Auth:** `AccessDenied`  
**Alerts:** `AlertRoutingModal`  
**Inventory:** `AddAppModal`, `EditCatalogModal`, `HelmDeployForm`, `HelmReleasePanel`  
**User management:** `UserFormModal`, `AccessRulesEditor`, `ResourceBrowser`, `EffectiveAccessPreview`  
**Dashboard:** `DashboardWidgets.jsx` (all widget implementations)

### Contexts

| Context | File | Role |
|---------|------|------|
| `AuthContext` | `context/AuthContext.jsx` | Session, login/logout, wraps `createAuthAccess(user)` |

### Hooks

| Hook | File | Role |
|------|------|------|
| `usePermission` | `hooks/usePermission.js` | Thin wrapper over `useAuth()` permission helpers |
| `useAuth` | `context/AuthContext.jsx` | Primary auth/RBAC hook |

### API services

| Module | Endpoints covered |
|--------|-------------------|
| `api/client.js` | Base URL, `request()`, 401 handler |
| `authApi.js` | login, logout, me |
| `clustersApi.js` | clusters, custom CRUD, overview, namespaces, resources, logs |
| `dashboardApi.js` | dashboard summary |
| `alertsApi.js` | alerts list, test email |
| `settingsApi.js` | settings GET/PUT |
| `inventoryApi.js` | inventory CRUD, deploy, workloads |
| `upgradesApi.js` | upgrade info, precheck, start |
| `usersApi.js` | users, roles, permissions, audit logs, access rules |
| `helmApi.js` | Helm status, releases, install/upgrade/rollback/uninstall |

### Protected routes

There is **no React Router**. Protection is:

1. If `!isAuthenticated` → `LoginPage`
2. `getVisiblePages()` filters nav by permissions
3. `isPageAllowed(pageKey)` gates `handleNavigate`
4. `useEffect` redirects to `getFirstAllowedPage()` if current page not allowed
5. Per-resource filtering via `getAllowedClusters`, `getAllowedNamespaces`, `getAllowedResources`, `filterAlertsForUser`

### Permission helpers

- `utils/authz.js` — full client mirror of backend access engine (evaluate_access, legacy vs rules, nav pages)
- `lib/grantedAccess.js` — action-level UI grants
- `lib/permissionCatalog.js` — human labels for effective permissions UI
- `lib/authAccess.js` — re-exports `authz.js` (deprecated path)

### Per-page detail

#### Dashboard (`dashboard`)

- **Purpose:** Single-cluster operational summary with widget grid.
- **Main components:** `DashboardPage`, `DashboardWidgets/*`, `widgetRegistry.js`, `widgetVisibility.js`.
- **APIs:** `GET /api/dashboard/summary?clusterId=`, `GET /api/clusters` (context), settings optional.
- **Permissions:** `overview:view` + cluster access; widgets add `alerts:view`, `upgrades:precheck`, `namespaces:view`, `audit:view`, `users:view`, etc.

#### Clusters (`clusters`)

- **Purpose:** Fleet list/cards.
- **Components:** `ClustersPage`, `StatCard`.
- **APIs:** `GET /api/clusters`.
- **Permissions:** `clusters:view`; list filtered server-side.

#### Cluster Management (`clusterManagement`)

- **Purpose:** Register/update/test/remove custom clusters (kubeconfig paste, manual connection fields).
- **Components:** `ClusterManagementPage`.
- **APIs:** `GET/POST/PUT/DELETE /api/clusters/custom`, `POST .../test`.
- **Permissions:** `clusters:add`, `clusters:update`, `clusters:remove`, `clusters:test` (page visible if **any**).

#### Cluster Overview (`clusterOverview`)

- **Purpose:** Nodes, version, utilization for selected cluster.
- **Components:** `ClusterOverviewPage`.
- **APIs:** `GET /api/clusters/:id/overview`.
- **Permissions:** `overview:view` + cluster access.

#### Inventory (`inventory` / `applicationDetails`)

- **Purpose:** Application catalog, discovery, deploy modals, Helm panel on detail view.
- **Components:** `InventoryPage`, `ApplicationDetailsPage`, `AddAppModal`, `EditCatalogModal`, `HelmDeployForm`, `HelmReleasePanel`.
- **APIs:** `GET /api/inventory`, detail, register, catalog CRUD, deploy/*, `GET /api/helm/*`.
- **Permissions:** `inventory:view` (list); `inventory:register/update/remove`, `apps:*`, `helm:*` for actions.

#### Namespaces (`namespaces`)

- **Purpose:** Namespace list for cluster.
- **APIs:** `GET /api/clusters/:id/namespaces`.
- **Permissions:** `namespaces:view` + cluster access.

#### Resources (`resources`)

- **Purpose:** Pods, deployments, services tabs per namespace.
- **APIs:** `GET /api/clusters/:id/namespaces/:ns/resources`.
- **Permissions:** `resources:view` (aliases to pod/deployment/service permissions); tabs gated by `getVisibleResourceTabs()`.

#### Logs (`logs`)

- **Purpose:** Fetch log lines for pod/container.
- **APIs:** `GET /api/logs?cluster&namespace&pod&container&live&previous`.
- **Permissions:** `logs:view` + pod/container-level access rules.

#### Alerts (`alerts`)

- **Purpose:** Alert table, routing modal, test email.
- **APIs:** `GET /api/alerts`, `POST /api/alerts/notifications/email/test`, settings for routing.
- **Permissions:** `alerts:view`; `alerts:manage` for routing/test.

#### Upgrade Safe Mode (`upgrade`)

- **Purpose:** Precheck, plan, confirmation, start workflow.
- **APIs:** `GET /api/upgrades/info`, `POST /api/upgrades/precheck`, `POST /api/upgrades/start`.
- **Permissions:** `upgrades:precheck` (info/precheck); `upgrades:start` (start — **not** granted to default `operator`).

#### Settings (`settings`)

- **Purpose:** Theme, refresh, default cluster, notification routing.
- **APIs:** `GET/PUT /api/settings`.
- **Permissions:** `settings:view`; `settings:manage` to save.

#### User Management (`userManagement`)

- **Purpose:** Users, roles, access rules editor, effective access preview.
- **APIs:** `/api/users`, `/api/users/:id/access-rules`, `/api/roles`, `/api/permissions`.
- **Permissions:** `users:view` (+ create/update/disable/manage roles as granted).

#### Audit Logs (`auditLogs`)

- **Purpose:** Read-only audit trail.
- **APIs:** `GET /api/audit-logs`.
- **Permissions:** `audit:view`.

#### Login

- **APIs:** `POST /api/auth/login`.
- **Permissions:** none.

---

## SECTION 4 — BACKEND

### Models (`models.py`)

| Model | Purpose |
|-------|---------|
| `Role` | Named role, system flag, M2M permissions |
| `Permission` | Permission key + description |
| `User` | Credentials, role FK, access relations |
| `AccessRule` | Fine-grained allow/deny rules |
| `UserClusterAccess` | Legacy cluster allow list |
| `UserNamespaceAccess` | Legacy namespace allow list |
| `AuditLog` | Actor, action, target, JSON details |
| `AppSettings` | Global UI settings + notifications JSON |
| `Cluster` | Custom cluster connection metadata + kubeconfig path |
| `AppCatalogEntry` | Inventory/catalog metadata (incl. Helm fields) |
| `AlertNotificationSent` | Dedup email sends per alert id |

Association table: `role_permissions`.

### Services

| Service | Purpose | Key dependencies |
|---------|---------|------------------|
| `auth_service` | Login validation, profile | `User`, JWT, audit |
| `user_service` | User CRUD, disable, admin count guard | `User`, serializers |
| `role_service` | Role/permission listing and updates | `Role`, `Permission` |
| `cluster_service` | (thin) cluster helpers | cluster_store |
| `cluster_connection_service` | Build kubeconfig from UI payload | kubeconfig_builder, cluster_store |
| `kubernetes_service` | K8s orchestration wrapper | k8s_provider |
| `dashboard_service` | Dashboard summary aggregation | k8s_provider, mock_data, access_engine, dashboard_intelligence |
| `alert_service` | (minimal — logic mostly in k8s_provider/k8s_metrics) | k8s_metrics |
| `upgrade_service` | Upgrade info/precheck/start orchestration | upgrade_provider, audit |
| `inventory_service` | Discovery, grouping, health, RBAC filter | k8s_provider, app_catalog, mock_data |
| `app_catalog_service` | Catalog CRUD, helm linkage | models, audit |
| `deployment_service` | YAML validate/dry-run/diff/apply via kubectl | access_engine, manifest_generator |
| `helm_service` | Helm CLI subprocess operations | cluster_access |
| `settings_service` | (inline in routes) | AppSettings |
| `audit_service` | Query audit logs | AuditLog |
| `manifest_generator` | Generate Deployment/Service from image form | — |

### Routes (blueprints)

| Blueprint | Prefix | File |
|-----------|--------|------|
| auth | `/api/auth` | `routes/auth.py` |
| users | `/api/users` | `routes/users.py` |
| access_rules | `/api/users` | `routes/access_rules.py` |
| roles | `/api` | `routes/roles.py` |
| audit | `/api/audit-logs` | `routes/audit_logs.py` |
| dashboard | `/api/dashboard` | `routes/dashboard.py` |
| clusters | `/api/clusters` | `routes/clusters.py` |
| logs | `/api` | `routes/logs.py` |
| alerts | `/api` | `routes/alerts.py` |
| settings | `/api` | `routes/settings.py` |
| upgrades | `/api/upgrades` | `routes/upgrades.py` |
| inventory | `/api/inventory` | `routes/inventory.py` |
| helm | `/api/helm` | `routes/helm.py` |

### RBAC middleware (`decorators.py`)

| Decorator | Behavior |
|-----------|----------|
| `require_auth` | 401 if no valid JWT (unless `AUTH_REQUIRED=false`) |
| `require_permission(key)` | 403 + audit `forbidden_access_attempt` if role lacks permission |
| `require_cluster_access` | 403 if `can_access_cluster` fails for cluster id from path/query/body |
| `require_namespace_access` | 403 if namespace in path/args and `can_access_namespace` fails |

**Gap:** Many routes add **additional** inline checks (logs, resources, deploy) — not all use decorators alone.

### Kubernetes services

- **`k8s_provider.py`** — Primary integration: list clusters, overview, namespaces, resources, logs, alerts derivation, cluster resolution
- **`k8s_metrics.py`** — `kubectl top`, CPU alert threshold, pod metrics parsing
- **`cluster_access.py`** — `ClusterAccess` dataclass (context, kubeconfig path, cluster id)
- **`cluster_store.py`** — Persist kubeconfig files, validation, connection test (`cluster-info`, `get nodes`)

### Cluster management services

- **`cluster_connection_service.py`** — Map UI fields → kubeconfig YAML
- **`kubeconfig_builder.py`** — Manual token/cert/server assembly; server extraction from YAML

### Alert services

- **`k8s_metrics.py`** — Threshold evaluation
- **`alert_notifier.py`** — Email dispatch on `GET /alerts` poll
- **`email_delivery.py`** — SMTP
- **`notification_routing.py`** — Settings validation (Slack/webhook URLs validated but **not sent**)

### Upgrade services

- **`upgrade_provider.py`** — Prechecks, provider matrix, version skew, plans, workflow
- **`upgrade_service.py`** — Mock vs real mode routing, audit events

### Inventory services

- **`inventory_service.py`** — Discovery, grouping by labels (`app.kubernetes.io/name`, `app`), health from workload status + metrics
- **`app_catalog_service.py`** — Registered metadata overlay
- **`deployment_service.py`** — kubectl apply/diff with confirmation phrases for dangerous ops

---

## SECTION 5 — DATABASE

### All tables

#### `roles`

| Column | Type | Notes |
|--------|------|-------|
| id | Integer PK | |
| name | String(64) UNIQUE, indexed | |
| description | String(255) | |
| is_system_role | Boolean | |

#### `permissions`

| Column | Type | Notes |
|--------|------|-------|
| id | Integer PK | |
| key | String(120) UNIQUE, indexed | e.g. `clusters:view` |
| description | String(255) | |

#### `role_permissions` (association)

| Column | Type |
|--------|------|
| role_id | FK → roles.id |
| permission_id | FK → permissions.id |

#### `users`

| Column | Type | Notes |
|--------|------|-------|
| id | Integer PK | |
| username | String(120) UNIQUE, indexed | |
| email | String(255) | |
| password_hash | String(255) | bcrypt-style via `passwords.py` |
| full_name | String(255) | |
| role_id | FK → roles.id | |
| is_active | Boolean | soft-disable |
| created_at, updated_at, last_login_at | DateTime TZ | |

#### `access_rules`

| Column | Type | Notes |
|--------|------|-------|
| id | Integer PK | |
| user_id | FK, indexed | |
| cluster_id | String(120), indexed | |
| namespace | String(253) nullable | |
| resource_type | String(32) | cluster, namespace, pod, deployment, service, container, service_port |
| resource_name | String(253) nullable | |
| container_name | String(253) nullable | |
| port | Integer nullable | |
| permission_key | String(120), indexed | |
| effect | String(16) | allow / deny |
| created_at, updated_at | DateTime TZ | |

#### `user_cluster_access`

| Column | Type | Notes |
|--------|------|-------|
| id | Integer PK | |
| user_id | FK, indexed | |
| cluster_id | String(120), indexed | |
| can_view | Boolean | |
| **Unique:** (user_id, cluster_id) | | |

#### `user_namespace_access`

| Column | Type | Notes |
|--------|------|-------|
| id | Integer PK | |
| user_id | FK, indexed | |
| cluster_id | String(120), indexed | |
| namespace | String(253) | |
| can_view | Boolean | |
| **Unique:** (user_id, cluster_id, namespace) | | |

#### `audit_logs`

| Column | Type | Notes |
|--------|------|-------|
| id | Integer PK | |
| actor_user_id | FK nullable, indexed | |
| action | String(120), indexed | |
| target_type | String(64) nullable | |
| target_id | String(255) nullable | |
| details | JSON nullable | |
| created_at | DateTime TZ, indexed | |

#### `app_settings`

| Column | Type | Notes |
|--------|------|-------|
| id | Integer PK | |
| theme | String(50) | system/light/dark |
| refresh_interval_seconds | Integer | 5–3600 |
| default_cluster | String(120) | |
| notifications | JSON | alerts/upgrades flags + routing |

#### `clusters`

| Column | Type | Notes |
|--------|------|-------|
| id | Integer PK | public id `custom-{id}` |
| name, host, port, protocol | | connection metadata |
| connection_method | String(32) | kubeconfig default |
| authentication_type | String(32) nullable | |
| skip_tls_verify | Boolean | |
| connection_timeout_seconds | Integer nullable | |
| kubeconfig_path | String(512) nullable | filesystem path |
| context_name | String(255) nullable | |
| is_active | Boolean | **soft delete** |
| last_connection_status, last_connection_error, last_tested_at | | test metadata |
| created_at, updated_at | DateTime TZ | |

#### `app_catalog_entries`

| Column | Type | Notes |
|--------|------|-------|
| id | Integer PK | |
| cluster_id | String(120), indexed | |
| namespace | String(253) | |
| workload_type, workload_name | nullable | |
| display_name | String(253) | |
| owner_team, environment, criticality | nullable | |
| description, documentation_url, contact_email | | |
| tags | JSON | |
| source | String(64) | default "Registered" |
| release_name, chart_name, chart_version, app_version, helm_revision | Helm metadata | |
| created_by_user_id | FK nullable | |
| created_at, updated_at | DateTime TZ | |
| is_active | Boolean, indexed | **soft delete** |

**Index:** `ix_app_catalog_cluster_ns_workload` on (cluster_id, namespace, workload_name)

#### `alert_notifications_sent`

| Column | Type | Notes |
|--------|------|-------|
| id | Integer PK | |
| alert_id | String(255), indexed | |
| channel | String(32) | default email |
| sent_at | DateTime TZ | |
| **Unique:** (alert_id, channel) | | dedup |

### Relationships (ER summary)

```
Role ←——(M2M)——→ Permission
  ↓ 1:N
 User ——1:N——→ AccessRule
 User ——1:N——→ UserClusterAccess
 User ——1:N——→ UserNamespaceAccess
 User ——1:N——→ AuditLog (as actor)
 User ——1:N——→ AppCatalogEntry (created_by)
```

### Soft delete strategy

| Entity | Mechanism |
|--------|-----------|
| Users | `is_active=false` on DELETE API |
| Custom clusters | `is_active=false`; kubeconfig file remains on disk |
| Catalog entries | `is_active=false` |
| No soft delete | Audit logs, alert_notifications_sent, access rules (hard delete on user update/replace) |

### RBAC / cluster / inventory / audit tables

Covered above: `roles`, `permissions`, `role_permissions`, `users`, `access_rules`, `user_cluster_access`, `user_namespace_access`, `clusters`, `app_catalog_entries`, `audit_logs`.

---

## SECTION 6 — API INVENTORY

Convention: responses wrap in `{ success, data }` or `{ success: false, error }` via `response.py` unless noted.

### Health & root

#### GET `/health`

- **Description:** Liveness + DB counts + kubernetes mode.
- **Permission:** none
- **Response:** `{ status, database: { users, settingsRows }, kubernetesMode }`

#### GET `/`

- **Description:** API banner.
- **Permission:** none

---

### Authentication

#### POST `/api/auth/login`

- **Permission:** none
- **Request:** `{ username, password }`
- **Response:** `{ token, user }` (user includes permissions, accessRules, clusterAccess, namespaceAccess)

#### GET `/api/auth/me`

- **Permission:** authenticated
- **Response:** full user profile

#### POST `/api/auth/logout`

- **Permission:** authenticated
- **Response:** acknowledgment (no token revocation)

---

### Users

#### GET `/api/users`

- **Permission:** `users:view`
- **Response:** array of users

#### POST `/api/users`

- **Permission:** `users:create`
- **Request:** username, password, email, fullName, roleId, clusterAccess, namespaceAccess, accessRules
- **Response:** created user

#### GET `/api/users/:id`

- **Permission:** `users:view`

#### PUT `/api/users/:id`

- **Permission:** `users:update`
- **Request:** partial user fields + access payload
- **Guards:** cannot demote self from admin; cannot disable self; cannot disable last admin

#### DELETE `/api/users/:id`

- **Permission:** `users:disable`
- **Response:** `{ id, isActive: false }`

---

### Access rules

#### GET `/api/users/:id/access-rules`

- **Permission:** `users:view` (via users routes — same decorator pattern)

#### PUT `/api/users/:id/access-rules`

- **Permission:** `users:update` — replace all rules

#### POST `/api/users/:id/access-rules`

- **Permission:** `users:update` — append rule

#### PUT `/api/users/:id/access-rules/:ruleId`

- **Permission:** `users:update`

#### DELETE `/api/users/:id/access-rules/:ruleId`

- **Permission:** `users:update`

---

### Roles & permissions

#### GET `/api/roles`

- **Permission:** `roles:view`

#### GET `/api/permissions`

- **Permission:** `roles:view`

#### PUT `/api/roles/:id/permissions`

- **Permission:** `roles:manage`
- **Request:** `{ permissionKeys: string[] }`

---

### Audit

#### GET `/api/audit-logs`

- **Permission:** `audit:view`
- **Query:** optional filters (see `audit_logs.py`)
- **Response:** paginated/list of audit events

---

### Dashboard

#### GET `/api/dashboard/summary`

- **Permission:** `overview:view` + cluster access
- **Query:** `clusterId` (required)
- **Response:** health, utilization, version status, alerts counts, namespace health, inventory summary, activity feeds, `myAccess` summary

---

### Clusters

#### GET `/api/clusters`

- **Permission:** `clusters:view`
- **Response:** `{ items[], count }` RBAC-filtered

#### GET `/api/clusters/custom`

- **Permission:** `clusters:view`
- **Response:** management metadata (no kubeconfig body)

#### POST `/api/clusters/custom`

- **Permission:** `clusters:add`
- **Request:** name, host, port, protocol, kubeconfigContent, contextName, connection options
- **Response:** `{ cluster, test }` (auto connection test)

#### PUT `/api/clusters/custom/:id`

- **Permission:** `clusters:update`

#### DELETE `/api/clusters/custom/:id`

- **Permission:** `clusters:remove` — soft delete

#### POST `/api/clusters/custom/:id/test`

- **Permission:** `clusters:test`

#### GET `/api/clusters/:clusterId/overview`

- **Permission:** `overview:view` + cluster access

#### GET `/api/clusters/:clusterId/namespaces`

- **Permission:** `namespaces:view` + cluster access

#### GET `/api/clusters/:clusterId/namespaces/:namespace/resources`

- **Permission:** `resources:view` + cluster + namespace access

---

### Logs

#### GET `/api/logs`

- **Permission:** `logs:view` + cluster + namespace access + pod-level `can_view_logs`
- **Query:** `cluster`, `namespace`, `pod`, `container`, `live`, `previous`
- **Response:** `{ query, stream, lines[] }`

---

### Alerts

#### GET `/api/alerts`

- **Permission:** `alerts:view`
- **Query:** optional `cluster`
- **Response:** `{ items[], count, metadata }` — triggers email dispatch side effect for firing alerts
- **Note:** Non-admin multi-cluster path uses `get_user_cluster_ids`; fine-grained `filter_alerts_for_user` not applied on all branches

#### POST `/api/alerts/notifications/email/test`

- **Permission:** `alerts:manage`
- **Response:** SMTP test result

---

### Settings

#### GET `/api/settings`

- **Permission:** `settings:view`

#### PUT `/api/settings`

- **Permission:** `settings:manage`
- **Request:** `theme`, `refreshIntervalSeconds`, `defaultCluster`, `notifications` (incl. routing)

---

### Upgrades

#### GET `/api/upgrades/info`

- **Permission:** `upgrades:precheck` + cluster access
- **Query:** `clusterId`, `targetVersion`

#### POST `/api/upgrades/precheck`

- **Permission:** `upgrades:precheck` + cluster access
- **Request:** `{ clusterId, targetVersion }`

#### POST `/api/upgrades/start`

- **Permission:** `upgrades:start` + cluster access
- **Request:** `{ clusterId, targetVersion, confirmation? }`

---

### Inventory

#### GET `/api/inventory`

- **Permission:** `inventory:view`
- **Query:** cluster, namespace, name, status, workloadType, imageTag, search

#### GET `/api/inventory/workloads`

- **Permission:** `inventory:register`
- **Query:** cluster, namespace

#### GET `/api/inventory/:inventoryId`

- **Permission:** `inventory:view` — encoded `cluster|namespace|name`

#### POST `/api/inventory/register`

- **Permission:** `inventory:register`

#### PUT `/api/inventory/:catalogId`

- **Permission:** `inventory:update`

#### DELETE `/api/inventory/:catalogId`

- **Permission:** `inventory:remove`

#### GET/PUT/DELETE `/api/inventory/catalog/:entryId`

- **Permissions:** view / update / remove respectively

#### POST `/api/inventory/deploy/yaml/validate`

- **Permission:** `apps:dryrun`

#### POST `/api/inventory/deploy/yaml/dry-run`

- **Permission:** `apps:dryrun`

#### POST `/api/inventory/deploy/yaml/diff`

- **Permission:** `apps:diff`

#### POST `/api/inventory/deploy/yaml/apply`

- **Permission:** `apps:deploy` — requires confirmation phrase for production safety

#### POST `/api/inventory/deploy/image/generate`

- **Permission:** `apps:dryrun`

#### POST `/api/inventory/deploy/image/dry-run`

- **Permission:** `apps:dryrun`

#### POST `/api/inventory/deploy/image/apply`

- **Permission:** `apps:deploy`

---

### Helm

#### GET `/api/helm/status`

- **Permission:** `helm:view`

#### GET `/api/helm/releases`

- **Permission:** `helm:view` — query: cluster, namespace

#### GET `/api/helm/releases/:releaseName`

- **Permission:** `helm:view`

#### GET `/api/helm/repos`

- **Permission:** `helm:view`

#### POST `/api/helm/repos`

- **Permission:** `helm:install`

#### GET `/api/helm/charts`

- **Permission:** `helm:view`

#### POST `/api/helm/template`

- **Permission:** `helm:view`

#### POST `/api/helm/dry-run`

- **Permission:** `helm:view`

#### POST `/api/helm/install`

- **Permission:** `helm:install` — confirmation required

#### POST `/api/helm/upgrade`

- **Permission:** `helm:upgrade`

#### POST `/api/helm/rollback`

- **Permission:** `helm:rollback`

#### POST `/api/helm/uninstall`

- **Permission:** `helm:uninstall`

#### POST `/api/helm/confirmation-phrase`

- **Permission:** `helm:install`

---

## SECTION 7 — AUTHENTICATION & RBAC

### JWT flow

1. Client `POST /api/auth/login` with credentials.
2. Server validates password hash, issues HS256 JWT (`sub`=user id, `exp`=8h default).
3. Client stores token in **localStorage** (`authStorage.js`).
4. All API calls send `Authorization: Bearer <token>`.
5. `get_current_user()` decodes JWT per request; inactive users rejected.
6. On 401, client clears session via `setUnauthorizedHandler`.

**Not implemented:** refresh tokens, token revocation list, rotation, MFA.

### Roles (seeded system roles)

| Role | Intent |
|------|--------|
| `admin` | All permissions |
| `operator` | Operate clusters, alerts, inventory register, dry-run, upgrade **precheck only** (no `upgrades:start`) |
| `cluster_admin` | Inventory + deploy + Helm (no user admin) |
| `viewer` | Read-only subset |

Default users: `admin`/`admin123`, `operator`/`operator123`, `viewer`/`viewer123`.

### Permissions

43 keys defined in `rbac_data.py` — resource-oriented (`clusters:view`, `apps:deploy`, `helm:rollback`, etc.).  
**Permission aliases** in engine: e.g. `resources:view` satisfies pod/deployment/service view checks.

### Cluster access

- **Admin:** all clusters returned by k8s discovery + custom.
- **With access_rules:** need matching allow rule for `clusters:view` at cluster scope (deny overrides at same specificity).
- **Legacy only:** `user_cluster_access` rows; empty namespace list → all namespaces in cluster.

### Namespace access

- Rules at `namespace` resource_type or finer.
- Legacy: `user_namespace_access` restricts namespaces; if empty for cluster → all namespaces (when cluster allowed).

### Resource access

- Pod/deployment/service/log/port checks use `evaluate_access` with resource_type and resource_name.
- Logs: pod-level or container-level rules; namespace-level fallback.
- Service ports: separate `services:ports:view` permission + `service_port` rules.

### Permission inheritance

- **Role permissions** gate whether evaluation proceeds.
- **Access rules** further restrict **where** role permissions apply.
- **No inheritance** between roles (single role per user).
- **Deny wins** among rules at highest specificity score.

### Frontend filtering

- Duplicated logic in `utils/authz.js` (explicit comment: UX only).
- `AuthContext` exposes same helpers as backend concepts.
- Risk: drift if one side updated without the other (mitigated partially by shared specificity constants).

### Backend enforcement

- Decorators on routes + service-layer checks for deploy/logs/alerts/inventory.
- Forbidden attempts audited.

---

## SECTION 8 — CLUSTER MANAGEMENT

### How clusters are stored

**Discovered clusters:** Not persisted — derived live from kubeconfig contexts (`context` name → sanitized `cluster_id`).

**Custom clusters:** `clusters` table row + kubeconfig file on disk:
- Path: `KUBESIGHT_KUBECONFIG_DIR` or `backend/data/kubeconfigs/cluster-{dbId}.yaml`
- Public API id: `custom-{dbId}`

### Kubeconfig handling

- Validated: YAML structure, size cap 512KB, clusters/contexts required
- On create/update: written to filesystem; path stored in DB
- **Never returned** in list/detail API responses (management dict excludes secret content)
- `kubeconfig_builder.py` can synthesize kubeconfig from manual host/token/cert fields

### Manual connections

- UI supports connection method selection (kubeconfig paste vs manual fields)
- Manual builds kubeconfig via `cluster_connection_service` / `kubeconfig_builder`
- Server host/port/protocol extracted from kubeconfig when possible

### Connection testing

- `POST /api/clusters/custom/:id/test` runs `kubectl cluster-info` and `kubectl get nodes`
- Results stored: `last_connection_status`, `last_connection_error`, `last_tested_at`
- Create cluster auto-runs test

### Provider detection

- Done in `upgrade_provider.detect_cluster_provider` using version JSON, node labels, server URL heuristics
- Maps to: docker-desktop, kind, minikube, kubeadm, eks, aks, gke, etc.

### Supported providers (upgrade matrix summary)

| Provider | Upgrade supported | Execution mode |
|----------|-------------------|----------------|
| docker-desktop | No | Instructions only |
| kind | No | Recreate cluster instructions |
| minikube | If `minikube` CLI present | CLI or instructions |
| kubeadm | Plan-only | Manual + confirmation phrase |
| EKS | If `aws` + `eksctl` | CLI or instructions |
| AKS | If `az` CLI | CLI or instructions |
| GKE | If `gcloud` CLI | CLI or instructions |
| unknown | Conservative defaults | Instructions |

**Important:** KubeSight does **not** upgrade cloud control planes by itself in most cases — it prechecks and emits plans/CLI steps.

---

## SECTION 9 — DASHBOARD

### Dashboard widgets

Registered in `widgetRegistry.js` (17 widgets): Cluster Health, Kubernetes Version, CPU/Memory Usage, Nodes, Running Pods, Applications (inventory summary), Active Alerts, Version Status, Cluster Information, Alert Breakdown, Namespace Health, My Access, Recent Activity, Operational Events, User Activity, Upgrade Status.

Visibility gated by `widgetVisibility.js` + permissions + `requiresClusterAccess`.

### Data sources

Single endpoint: `GET /api/dashboard/summary?clusterId=` assembled in `dashboard_service.py` from:

- Cluster list item / overview (mock or k8s)
- Alerts (mock or derived)
- Namespaces
- Audit logs (recent)
- Inventory counts
- Upgrade/version intelligence

### Health calculations

`_compute_health` in `dashboard_service.py`:

- **Critical** if critical alerts, failed pods, or nodes not ready
- **Warning** if warning alerts or pending pods
- Else **healthy**
- Node readiness in mock mode inferred from cluster `status` string (approximation)

### Alert calculations

- Pulls alerts for cluster; buckets severities critical/warning/info
- Uses `filter_alerts_for_user` when user present

### Version calculations

- `dashboard_intelligence.evaluate_version_status` compares current vs latest from `https://dl.k8s.io/release/stable.txt`
- Minor version behind labels with icons

### Metrics sources

- Real: `kubectl top` + node/pod capacity from API responses
- Mock: embedded in `CLUSTER_OVERVIEWS` / overview fixtures
- If metrics-server missing → utilization marked unavailable (tests assert this)

---

## SECTION 10 — ALERTING

### Alert engine

- **Real mode:** `k8s_metrics` + `k8s_provider.list_alerts_from_k8s` — derives firing alerts when pod CPU usage / limit ≥ `ALERT_CPU_THRESHOLD_PERCENT` (default **80**)
- **Mock mode:** static `ALERTS` in `mock_data.py`
- Requires metrics-server for real alerts

### Thresholds

- Env: `ALERT_CPU_THRESHOLD_PERCENT` (default 80)
- Dashboard utilization thresholds: warning 70%, critical 90% (separate from alert firing)

### Notification channels

| Channel | UI/settings | Delivery |
|---------|-------------|----------|
| Email | Enabled in settings routing | **Implemented** — SMTP via `email_delivery.py`, dedup via `alert_notifications_sent` |
| Slack | Validated URL in settings | **Not implemented** — no dispatcher |
| Webhook | Validated URL in settings | **Not implemented** — no dispatcher |

Email sends on **`GET /api/alerts`** poll (side effect), not a background worker.

### Alert permissions

- Role needs `alerts:view`
- Cluster/namespace/pod scoped via `can_view_alert` / `filter_alerts_for_user` (partially on list endpoint)

### Alert filtering

- Frontend: `filterAlertsForUser` in App.jsx for display
- Backend: inconsistent — multi-cluster non-admin loop uses cluster allow list; **not always** `filter_alerts_for_user` for pod-level denies

---

## SECTION 11 — UPGRADE SAFE MODE

### Prechecks

`run_extended_prechecks` (real) / `mock_precheck` (demo) includes:

- API reachable, nodes ready, control plane health
- Target version validation (semver)
- Metrics server, PVC/storage, pod restarts, CrashLoopBackOff, pending pods, PDB, deprecated APIs, version skew, node pressure, kube-system health

### Version detection

- From `kubectl version -o json` and node kubelet versions
- Latest from dl.k8s.io stable.txt (network dependency)

### Provider detection

- See Section 8 — drives instructions vs CLI execution

### Upgrade support matrix

- Documented in `upgrade_provider.get_provider_support`
- Docker Desktop / kind: explicitly unsupported for in-place upgrade
- kubeadm: confirmation phrase required to proceed
- Cloud providers: optional CLI execution if tools installed on **KubeSight server host** (not in-cluster)

### Permissions

- `upgrades:precheck` — info + precheck (operator has this)
- `upgrades:start` — start workflow (**admin only** in default seeds; operator denied)

---

## SECTION 12 — INVENTORY

### Application discovery

- Lists workloads from namespaces user can access
- Groups by standard labels (`app.kubernetes.io/name`, `app`, `app.kubernetes.io/instance`) else workload name
- Merges **live cluster state** with **catalog entries** (`app_catalog_entries`)
- Mock mode uses `NAMESPACE_RESOURCES` + fabricated helm/inventory items

### Grouping logic

- `inventory_service` aggregates pods/deployments under app keys
- Health: `compute_status` from desired/ready replicas + failed/crashloop signals
- Image tag extracted from container image

### Metadata model

`AppCatalogEntry`: display name, owner, environment, criticality, docs URL, contact, tags, source, Helm fields, workload linkage

### Deployment support

| Method | Status |
|--------|--------|
| YAML deployment | validate, dry-run, diff, apply (kubectl) |
| Docker image deployment | manifest generator → same apply path |
| Helm | Full install/upgrade/rollback/uninstall via helm CLI |

### YAML deployment

- Dangerous resource kinds blocked for non-admin (tests cover)
- Secret values stripped from previews
- Confirmation phrase required on apply

### Docker image deployment

- Form → Deployment + Service manifests in `manifest_generator.py`

### Helm support

**Implemented:** repos, chart search, template, dry-run, install/upgrade/rollback/uninstall, confirmation phrases, catalog sync on install.  
Requires `helm` binary on server PATH. Mock fixtures when not in real mode.

---

## SECTION 13 — SECURITY REVIEW

### Security strengths

- Passwords hashed (not stored plain text)
- JWT required by default on API routes
- RBAC enforced server-side with audit on forbidden attempts
- Kubeconfig content not returned in list APIs
- Path traversal guard on kubeconfig file paths
- Deployment confirmations and dangerous-resource blocks
- Test suite covers RBAC, secret redaction, confirmation gates
- Production test guard (`FLASK_ENV=production` blocks pytest)
- Helm/YAML previews sanitize secrets in tests

### Security weaknesses

| Issue | Severity | Detail |
|-------|----------|--------|
| Default JWT secret | **High** | Predictable dev default in code and `.env` |
| Seeded weak passwords | **High** | admin123/operator123/viewer123 shipped |
| Kubeconfig on filesystem | **High** | DB breach + FS read = cluster credentials |
| `AUTH_REQUIRED=false` | **High** | Disables all auth checks |
| No rate limiting | **Medium** | Login brute-force possible |
| No token revocation | **Medium** | Stolen JWT valid until expiry |
| CORS wide open | **Medium** | `CORS(app)` allows all origins |
| kubectl subprocess | **Medium** | Relies on argument discipline; compromise of API = shell access |
| Email on GET side effect | **Low/Medium** | GET /alerts mutates state (sent tracking) |
| Slack/webhook stored | **Low** | URLs in DB JSON without delivery |
| SQLite fallback | **Medium** | Accidental prod SQLite without hardening |
| Single-tenant settings | **Low** | One global notification config |
| Logs/mock defaults | **Low** | Mock log endpoint fills defaults if params missing |

### Missing validations

- No CSRF protection (JWT in header mitigates somewhat)
- `defaultCluster` not verified against user's accessible clusters on save
- Alert list RBAC not fully using fine-grained `filter_alerts_for_user` in all code paths
- No maximum bound on audit log retention/export
- No input sanitization on free-text catalog fields (XSS risk if rendered unsafely — review React usage; generally text nodes)

### Potential privilege escalation paths

1. **`AUTH_REQUIRED=false`** in production → full API access.
2. **Role permission update** (`roles:manage`) — admin-only by default; if mis-assigned, grant `users:update` + `roles:manage`.
3. **Access rules misconfiguration** — deny rules reduce access; overly broad allow on `cluster` scope grants entire cluster.
4. **`apps:deploy` / Helm install** — any user with permission + namespace access can apply manifests to cluster (by design — ensure role assignment is tight).
5. **Custom cluster add** — user with `clusters:add` registers kubeconfig; server then runs kubectl with that identity (intended, but powerful).

### Secret handling

- Passwords: hashed
- Kubeconfig: filesystem, gitignored dir — **not encrypted at rest**
- SMTP password: environment variables
- Manual token fields in UI: used to build kubeconfig, persisted to disk

### Token handling

- localStorage (XSS exposure surface)
- HS256 symmetric secret — all services sharing one key

### Kubeconfig handling

- Reasonable validation and path checks
- Soft-deleted clusters may leave files on disk (orphan credentials)

### RBAC weaknesses

- Dual legacy + rules system increases misconfiguration risk
- Frontend/backend duplicate logic may diverge
- `get_user_cluster_ids` heuristic for alerts may over-include clusters when rules use non-view permission keys
- Operator cannot `upgrades:start` but precheck exposes plan details (acceptable)

---

## SECTION 14 — TEST COVERAGE

### Existing tests (18 modules, 100+ cases)

| Module | Focus |
|--------|-------|
| `test_auth.py` | Login, me, password hashing |
| `test_access_control.py` | Viewer denials |
| `test_access_summary.py` | Effective access computation |
| `test_role_access.py` | Role permission matrices |
| `test_roles.py` | Role APIs |
| `test_users.py` | User CRUD |
| `test_user_access_persistence.py` | Access rules persistence |
| `test_clusters.py` | Mock list, custom CRUD, kubeconfig builder |
| `test_alerts.py` | Viewer access, email test permission |
| `test_dashboard.py` | Summary, permissions, health logic |
| `test_dashboard_intelligence.py` | Version/utilization math |
| `test_dashboard_metrics.py` | Missing metrics-server behavior |
| `test_upgrades.py` | Provider detection, precheck, confirmation, RBAC |
| `test_inventory.py` | List, search, detail, filters |
| `test_app_catalog.py` | Catalog CRUD, deploy gates, secrets |
| `test_helm.py` | Helm validation, RBAC, confirmations |
| `test_settings.py` | Settings RBAC |

### Coverage areas (good)

- RBAC matrices for admin/operator/viewer
- Custom cluster lifecycle (mocked kubectl test)
- Upgrade provider logic and mock prechecks
- Dashboard health/version calculations
- Deploy/Helm confirmation and dangerous resources

### Missing tests

- **No frontend tests** (no Jest/Vitest)
- **No E2E** (Playwright/Cypress)
- Real kubectl integration tests (explicitly mocked)
- Slack/webhook routing (not implemented)
- Email dispatch integration (SMTP mocked/absent)
- `AUTH_REQUIRED=false` behavior
- Concurrent/session security
- Performance/load
- PostgreSQL-specific migrations (SQLite-biased tests)
- `access_rules` deny precedence edge cases (limited)
- Log streaming (`live=true`) behavior

### Mocking strategy

- `conftest.py`: in-memory SQLite, seeded DB per test, JWT headers
- `unittest.mock.patch` on `test_cluster_connection`, kubectl runners, helm calls
- `K8S_REAL_MODE=false` in test config
- Direct unit tests on `upgrade_provider`, `dashboard_intelligence` pure functions

---

## SECTION 15 — KNOWN ISSUES

### Bugs / behavioral issues

- **Alert RBAC inconsistency:** `GET /api/alerts` multi-cluster path may not apply `filter_alerts_for_user` pod-level rules.
- **GET side effects:** Alert email dispatch on read can surprise operators and complicate caching.
- **Mock logs:** Missing query params default to hardcoded pod/cluster — can confuse testing.
- **Duplicate route handlers:** `/api/inventory/:catalogId` and `/api/inventory/catalog/:entryId` overlap (same service).

### Technical debt

- **`App.jsx` monolith** (~1062 lines) — all data fetching and page routing in one component
- **No React Router** — no deep links, refresh loses page state
- **Duplicated RBAC** — `access_engine.py` vs `authz.js` must be kept in sync manually
- **Dual access model** — legacy cluster/namespace tables coexist with `access_rules`
- **`api.js` deprecated barrel** still used widely
- **Whitespace/formatting** — some backend files have excessive blank lines (`upgrade_service.py`, `upgrades.py`)

### Incomplete features

- **Slack alert delivery** — settings only
- **Webhook alert delivery** — settings only
- **True log streaming** — `live` flag exists; no WebSocket/SSE
- **`apps:delete` permission** — defined in RBAC but no dedicated delete workflow exposed clearly in UI/API inventory
- **cluster_admin role** — seeded but no default user; under-documented vs operator

### Placeholder / mock functionality

- Entire **mock mode** fleet (`mock_data.py`) for demos without clusters
- **Mock upgrade precheck** always passes checks with "Mock mode" messages
- **Mock inventory/helm** items merged in list views
- Frontend copy: "diff unavailable in mock mode"

### Areas needing refactor

- Split `App.jsx` into route-level data hooks or React Router loaders
- Centralize alert notification dispatch in background worker
- Encrypt kubeconfigs at rest (KMS/sealed secrets)
- Unify access model (deprecate legacy tables)
- Apply `filter_alerts_for_user` consistently on server

---

## SECTION 16 — FUTURE ROADMAP

### Short-term improvements (1–3 sprints)

- Enforce production secret validation (fail boot if default JWT secret or weak seed passwords detected)
- Implement Slack/webhook dispatchers or remove from UI until ready
- Fix alert list RBAC to always use `filter_alerts_for_user`
- Move alert email dispatch to async job (Celery/RQ) triggered by scheduler, not GET
- Add React Router + URL-based navigation
- Document `cluster_admin` role and add seed user if intended

### Medium-term improvements (quarter)

- Split `App.jsx`; introduce TanStack Query or similar for API state
- Encrypt custom cluster kubeconfigs (per-tenant DEK)
- JWT refresh + server-side session/version invalidation
- In-cluster **read-only** ServiceAccount mode as alternative to kubeconfig-on-disk
- Observability: structured logging, metrics, request tracing
- PostgreSQL-only migrations (Alembic) instead of ad-hoc `migrate_rbac.py`
- Rate limiting and login lockout

### Enterprise features

- SSO (OIDC/SAML), group → role mapping
- Multi-organization tenancy
- Approval workflows for deploy/upgrade (`upgrades:start`, `apps:deploy`)
- Compliance export for audit logs
- Per-cluster credential vault integration (HashiCorp Vault)
- Read-only audit replica and SIEM forwarding

### Scalability improvements

- Horizontal API replicas with shared DB + object storage for kubeconfigs
- Cache kubectl discovery results (with TTL)
- Separate **worker** service for kubectl/helm subprocess isolation
- Leader-elected scheduler for alerts/upgrades
- Harden subprocess pool (timeouts, concurrency limits)

---

## SECTION 17 — PROJECT SCORECARD

| Dimension | Score (1–10) | Justification |
|-----------|--------------|---------------|
| **Architecture** | **6** | Clear Flask layers and service extraction, but monolithic SPA, kubectl subprocess coupling, and no async workers limit clean scaling. |
| **Security** | **4** | Thoughtful RBAC and audit, undermined by default secrets, seeded passwords, kubeconfig on disk, open CORS, optional auth disable, no rate limits. |
| **RBAC** | **7** | Rich permission model + access rules with deny precedence; complexity from dual legacy/rules paths and minor server inconsistencies. |
| **Frontend UX** | **6** | Coherent layout and widgets; hurt by no deep linking, large App.jsx, and client-only navigation state. |
| **Backend Design** | **7** | Thin routes, dedicated services, good test hooks; some side effects on GET and duplicated orchestration in k8s_provider. |
| **Scalability** | **4** | Single-process Flask + per-request kubectl is a bottleneck; no queue, cache, or sharding story. |
| **Maintainability** | **5** | Tests help backend; frontend lacks tests; RBAC duplicated across Python/JS; mock/real branches add cognitive load. |
| **Kubernetes Functionality** | **6** | Solid read path and custom clusters; upgrades are guidance-heavy; alerts depend on metrics-server; no operator pattern. |
| **Production Readiness** | **4** | Missing secret management, HA, monitoring, token lifecycle, and complete alert channels — suitable for internal pilot with hardening. |
| **Overall** | **5.5 → 6** | Strong internal-dashboard prototype with real RBAC and kubectl integration; not yet enterprise production without security and operational investment. |

---

## Appendix — Environment variables (reference)

| Variable | Role |
|----------|------|
| `DATABASE_URL` | SQLAlchemy connection |
| `JWT_SECRET_KEY` / `JWT_EXPIRY_HOURS` | Auth |
| `AUTH_REQUIRED` | Disable auth (dangerous) |
| `K8S_REAL_MODE` | mock / true / auto |
| `K8S_KUBECONFIG` | Default kubeconfig path |
| `KUBESIGHT_KUBECONFIG_DIR` | Stored custom kubeconfigs |
| `ALERT_CPU_THRESHOLD_PERCENT` | Alert threshold |
| `SMTP_*` | Email delivery |
| `TEST_DATABASE_URL` | pytest DB |
| `VITE_API_BASE_URL` | Frontend API origin |

---

*End of audit. This document reflects the repository state at audit time and is intended for external architectural review.*
