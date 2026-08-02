# Brief — Track A1, Backend Core

One of three parallel agent tracks. See `OWNERSHIP.md`, `CONTRACTS.md`,
`COORDINATION.md` at the repo root.

This track is **the critical path**. If A1 slips, the ship date slips; A2 and A3
both finish with slack. It also carries two coordination duties the other tracks
depend on: owning the contracts, and applying insertion requests to the shared
app-factory files.

## Mission

Make the backend durable enough to sell, then move cluster access behind an
agent. Three phases: Alembic and the job platform, migrating the threaded jobs,
then the KubeSight cluster agent.

## Current state — the facts that matter

**No Alembic.** Schema is `db.create_all()` plus hand-written `ALTER TABLE`
statements in `backend/api/migrate_rbac.py` executed during normal startup
(`_portable_type` at `:44`, drop path at `:53`). Comments in that file confirm the
pattern is deliberate but acknowledged as a stopgap. `seed_defaults()` runs at
`backend/api/__init__.py:237` and does repair passes over permissions and grants
on every boot.

**Production work runs on daemon threads.** At least eleven sites, listed in
contract 3 of `CONTRACTS.md`. The consequential ones:

- `services/deploy_automation_service.py:1923` — deploys
- `services/alert_policy_scheduler.py:179` — alert evaluation, started from the
  app factory at `__init__.py:264`
- `services/cluster_build/executor.py:1952` — cluster builds
- `services/zoho_sync_service.py:1764` — ticket writeback
- `services/mobile_app_service.py:787` — mobile builds

All `threading.Thread(daemon=True)`. They die with the process, never retry, and
leave no durable record. A deploy interrupted by a pod restart is the failure
that ends a pilot.

**`models.py` is 2,846 lines, 62 classes.** You are its sole writer. New tables go
in new domain modules following the existing convention
(`models_application_intelligence.py`, `models_cluster_build.py`).

**Cluster access is direct and kubeconfig-based.** `k8s_provider.py` is 2,430
lines. `models.py:220` stores `kubeconfig_path`; `connection_method` defaults to
`"kubeconfig"` at `:216`. The target architecture says the control plane must not
hold customer kubeconfigs — that is not true today and only becomes true when the
agent migration completes.

**Response envelope and RBAC already exist and are good.** `response.py` gives
`success_response` / `error_response`; `decorators.py` gives `require_auth`,
`require_admin`, cluster and namespace access checks. Build on them, do not
reinvent.

## Coordination duties

1. **Own the contracts.** `CONTRACTS.md` is frozen. Review every change proposal
   in `COORDINATION.md`, ack or reject, keep the file accurate.
2. **Apply insertion requests** to `backend/api/__init__.py` and
   `backend/api/routes/__init__.py`. A3's `run_startup_guards(app)` call is the
   first one due. Key lines: `create_app` at `:183`, `register_blueprints` at
   `:229`, `seed_defaults()` at `:237`, `start_alert_policy_scheduler` at `:264`.
3. **Publish the integration status service early.** A2 is blocked on contract 2
   for the Integrations hub.

## Tasks in order

### 1. Contracts and Alembic (weeks 1–4)

Days 1–3: finalize `CONTRACTS.md` before the other tracks build. Then:

- Introduce Alembic with an initial migration reflecting current schema.
- Stop schema mutation during normal startup — retire the `ALTER TABLE` path in
  `migrate_rbac.py`.
- Separate the seed repair passes from boot; make them an explicit command.
- Migration tests in CI (A3 owns the pipeline — file a request for the job).

### 2. Job platform (weeks 4–8)

Build to contract 3. Tables in `models_jobs.py`. Persistent state, retry policy,
timeout, cancellation, idempotency key, progress events, dead-letter, audit
attribution. Separate worker deployment from the API process. Add a reaper for
jobs stranded in `running` with a stale heartbeat.

### 3. Migrate the threaded jobs (weeks 8–12)

Deploy automation first — highest blast radius. Then alert evaluation, cluster
builds, Zoho sync, mobile builds, application analysis. Each handler must be
idempotent and must redact payloads before logging.

**The integration health service is already built** — it is sitting uncommitted in
the working tree: `services/integrations_service.py` (776 lines) and
`routes/integrations.py` (117 lines), normalizing all nine providers to contract
2. A2 is therefore not blocked on you for the hub.

Your job on it is not to rebuild it: commit it, add test coverage (there is none
yet), and hold the line on the two invariants its own docstring sets out — GET
never tests, and only the four customer-facing states ever escape the service.

### 4. Agent — protocol and enrollment (weeks 12–16)

Outbound mTLS. Short-lived single-use registration tokens. Permission profiles
(visibility only / visibility + logs / deployment ops / lifecycle / custom).
Never require cluster-admin for standard install. Signed results bound to the
agent session. Every command gets a unique ID and rides the job platform, which
is why phase 2 comes first.

### 5. Agent — dual mode (weeks 16–20)

Both transports live at once, switchable per cluster. Build a feature-parity
checklist covering every current direct-access path in `k8s_provider.py`,
including log streaming (`k8s_provider.py:2219`), the cluster build executor, and
deploy automation.

### 6. Agent — switchover (weeks 20–24)

Every design-partner cluster on the agent, parity checklist green, then delete
kubeconfig storage. Until this lands, the security claim in the architecture
diagram is not true — do not let it be marketed before the date.

## Protocol

- Branch `track/backend`. Rebase on `master` daily.
- Merge only on green CI. Run `/verify` before any merge crossing the API
  boundary.
- Log status in `COORDINATION.md` each session.

## Do not

- Edit anything under `frontend/`, or A3's files: `production_guards.py`,
  `models_auth.py`, `routes/auth.py`, `routes/api_tokens.py`, `auth_utils.py`,
  `passwords.py`, `secret_encryption.py`, `.github/`, `helm/`, `k8s/`.
- Add new tables to `models.py`.
- Change a contract without acks from affected tracks.
- Start the agent before the job platform is durable.
