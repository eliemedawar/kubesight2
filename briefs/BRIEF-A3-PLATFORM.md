# Brief — Track A3, Platform & Security

You are one of three AI agents working this repository in parallel. You have no
visibility into the other two. Read `OWNERSHIP.md`, `CONTRACTS.md`, and
`COORDINATION.md` at the repo root before writing code. This brief is
self-contained; you are not expected to have prior context.

## The product

KubeSight is a Kubernetes operations platform being taken from an internal tool
to a commercial self-hosted product. Positioning: *a governed Kubernetes
operations platform that helps platform teams observe, approve and safely execute
changes across their clusters.* Target customer runs 3–50 clusters with a small
platform team and real approval and audit requirements. It will be installed by
customers into their own infrastructure, which is why your track exists.

Stack: Flask + SQLAlchemy backend (`backend/api/`, 52 modules, 33 route
blueprints), React frontend, Helm/k8s deployment. Roughly 152k lines total, 155
commits.

## Your track

Platform and security. Three things, in this order: **CI first** (it blocks the
other two agents), then **session and secret hardening**, then **the Helm chart
and supply chain**.

Your work is deliberately the most greenfield of the three tracks — mostly new
files, fewest edits to hot paths — because you start with the least context.

## Task 1 — CI. Days 1–3. This blocks everyone.

**There is no CI in this repository.** No `.github/workflows` directory exists.
Three agents are about to commit in parallel with no automated gate. This is the
first thing you build and nothing else starts until it is green.

Minimum viable pipeline:

- Frontend: `cd frontend && npm ci && npm test` — `vitest run`, 79 existing test
  files, must stay green.
- Frontend: production build must pass (`npm run build`, Vite 6).
- Backend: pytest against `backend/tests`. Note
  `backend/api/testing_config.py` — tests default to
  `sqlite:///:memory:` and honour `TEST_DATABASE_URL` for Postgres. Run both if
  cheap; SQLite alone if not.
- Run on push to `master` and on every `track/*` branch.
- Branch protection on `master`: no merge on red.

Announce in `COORDINATION.md` the moment this lands.

## Task 2 — Production startup guards. Days 3–10.

Small, self-contained, and the highest security value per line in the whole
plan. Contract 5 in `CONTRACTS.md` has the full list.

When `KUBESIGHT_ENV=production`, refuse to start — exit non-zero, name the
offending setting — if any of these hold:

| Guard | Where the risk lives |
|---|---|
| Default or missing secret key | — |
| Debug mode on | — |
| Authentication disabled | `auth_required_enabled()` in `backend/api/auth_utils.py` can disable auth wholesale; `backend/api/decorators.py` short-circuits on it |
| Default seeded users enabled | `backend/api/seed.py` creates demo users and roles |
| Credential encryption key missing | `backend/api/secret_encryption.py` |
| Unsafe CORS | `CORS_ORIGINS` defaults to `*` — `backend/api/__init__.py:175-180` |
| Migrations not at head | Depends on A1 landing Alembic — ship the other six first, add this after |

All of it goes in **`backend/api/production_guards.py`** (yours, new), exposing
`run_startup_guards(app)`. You do **not** edit `backend/api/__init__.py` — A1
owns it. File an insertion request in `COORDINATION.md` for the single call site;
`create_app` is at line 183.

## Task 3 — Sessions and secrets. Weeks 2–10.

Today the frontend stores a JWT in `localStorage` (`frontend/src/authStorage.js`).
Replace with cookie sessions per contract 4 in `CONTRACTS.md`.

**Read this before touching auth:** the existing flow is not just login. It
includes forced first-login password change and TOTP enrolment, and
`backend/api/decorators.py` rejects any protected endpoint for a user with
`first_login_completed == False`. That behaviour is load-bearing. Every endpoint
in `backend/api/routes/auth.py` must keep working:

```
POST /api/auth/login
POST /api/auth/first-login/change-password
POST /api/auth/first-login/totp/setup
POST /api/auth/first-login/totp/verify
POST /api/auth/mfa/verify
GET  /api/auth/me
POST /api/auth/logout
```

Target:

- Short-lived access session cookie: `HttpOnly`, `Secure`, `SameSite=Lax`.
- Rotating single-use refresh token; reuse detection revokes the family.
- Server-side session records — listable and revocable, with device metadata.
- CSRF double-submit on every cookie-authenticated mutation, header
  `X-CSRF-Token`. New endpoints: `GET /api/auth/csrf`, `POST /api/auth/refresh`.
- Configurable session duration, global logout.

New tables go in **`backend/api/models_auth.py`** (yours, new). Do **not** touch
`backend/api/models.py` — it is 2,846 lines, 62 classes, and A1 owns it
exclusively. The repo already establishes the split-module convention
(`models_application_intelligence.py`, `models_cluster_build.py`).

Then secrets: separate the JWT signing key from the credential encryption key,
support key rotation, ensure stored secrets are never returned through any API,
audit every secret create/update/test/delete.

The frontend agent is building its client against contract 4 right now, without
being able to see your code. Coordinate the cutover in `COORDINATION.md` — a
silent change to cookie names, CSRF header, or refresh semantics breaks them.

## Task 4 — OIDC. Weeks 10–16.

OIDC only. SAML and SCIM are Enterprise-tier and deferred past first release
unless a design partner blocks on them. Group-to-role mapping, verified domains,
MFA recovery codes, admin recovery path.

## Task 5 — Helm chart and supply chain. Weeks 16–24.

Official chart supporting external PostgreSQL, external Redis, external object
storage, ingress and TLS, existing Secrets or secret-store CSI, pod security
contexts, network policies, resource limits, affinity and topology spread, image
pull secrets, offline registry. Bundled dependencies only for trials.

Existing `k8s/` and the several `Dockerfile*` variants at repo root are yours —
consolidate them.

Installation experience: preflight checker, values schema validation, install
verification command, first-admin bootstrap, upgrade compatibility check,
rollback procedure, diagnostic bundle export.

Supply chain: dependency and container scanning, SBOM generation, image signing,
secret scanning. Wire into the CI you built in task 1.

## Task 6 — Licensing. Weeks 24–27.

Signed offline license files, cluster/node entitlements, expiry with grace
period, non-disruptive expiry — a lapsed license must never disrupt a customer's
running workloads. Server-side entitlement service; never rely on hidden
frontend controls for entitlement.

## Protocol

- Branch `track/platform`. Rebase on `master` daily.
- Merge only on green CI.
- Log status in `COORDINATION.md` each session.
- Your auth work is the track where a silent mistake is worst. Expect the human
  reviewer to gate it manually rather than merging on CI alone.

## Do not

- Edit `backend/api/models.py`, `backend/api/__init__.py`,
  `backend/api/routes/__init__.py`, anything under `backend/api/services/`, or
  anything under `frontend/`. File an insertion request instead.
- Change a contract in `CONTRACTS.md` unilaterally.
- Disable or weaken the first-login and MFA enforcement to simplify the session
  rework.
- Delete or rewrite existing tests to make them pass.
