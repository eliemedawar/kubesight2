# Frozen interface contracts

These are the interfaces more than one track builds against. They are **frozen**:
see the change protocol in `COORDINATION.md`. Each contract names the tracks that
produce and consume it.

Everything here is either already true in the codebase or is a normalization of
something already present. Nothing is invented from scratch.

---

## 1. API response envelope

**Producer:** A1, A3 · **Consumer:** A2 · **Status:** already implemented

Defined in `backend/api/response.py`. Every JSON endpoint returns:

```json
{ "success": true,  "data": <payload>, "error": null }
{ "success": false, "data": null,      "error": "human-readable message" }
```

```python
from .response import success_response, error_response

success_response(payload)                 # 200
success_response(payload, 201)
error_response("Unauthorized", 401)
```

Rules:
- No endpoint returns a bare array or bare object. Always the envelope.
- `error` is a string safe to display to an operator. Never a stack trace, never
  a secret, never a raw driver error.
- HTTP status carries the machine meaning; `error` carries the human meaning.

Status codes in use: `400` validation, `401` unauthenticated, `403` authorized
but forbidden, `404` not found, `409` conflict, `422` semantically invalid,
`429` rate limited, `500` unexpected.

**A2:** the client is `frontend/src/api/client.js`. `frontend/src/api.js` is a
deprecated re-export shim — do not add to it; import from `./api/*` modules.

---

## 2. Integration status contract

**Producer:** A1 · **Consumer:** A2 · **Status: already implemented — A2 is not blocked**

This exists in the working tree today:

- `backend/api/services/integrations_service.py` (776 lines) — the normalizer
- `backend/api/routes/integrations.py` (117 lines) — the endpoints
- `frontend/src/lib/integrations.js`, `frontend/src/api/integrationsApi.js`

It already normalizes nine providers that disagreed on everything — Jira and Zoho
track both sync and test, Jenkins/SMTP/receivers/registries track only a test,
Bitbucket tracks neither, Hermes had no row at all; half spell success `"ok"` and
half `"success"`. The per-provider `test_connection` implementations it wraps are
at `routes/ticketing.py:136` and `:778`, `routes/registries.py:80`,
`routes/infra_connections.py:94` and `:252`.

**A2 builds against this and nothing else.** Do not hardcode a provider list.

### `GET /api/integrations`

`data` is an array of descriptors.

### `GET /api/integrations/<key>`

`data` is one descriptor. Exact emitted shape, from `_descriptor` at
`integrations_service.py:114-128`:

```json
{
  "key": "jira",
  "name": "Jira",
  "category": "Ticketing",
  "status": "connected",
  "enabled": true,
  "lastTestedAt": "2026-08-02T10:14:00Z",
  "lastSuccessfulSyncAt": "2026-08-02T10:00:00Z",
  "message": "Connection healthy",
  "capabilities": ["ticket-sync", "deployment-approval"],
  "usedBy": ["Deployment requests"],
  "actions": ["configure", "test", "disable"]
}
```

There is **no `configured` field** in the payload. Configuration state is carried
by `status: "not_configured"` and by the absence of `test` from `actions`.
`configured` exists only as an internal input to `derive_status` and `_actions`.

**`status`** is exactly one of — no other values ever reach the frontend:

| Value | Meaning |
|---|---|
| `connected` | Last test succeeded, integration is enabled and working |
| `degraded` | Enabled and configured, but last test or sync failed |
| `disabled` | Configured but switched off by an operator |
| `not_configured` | No credentials or endpoint saved yet |

Precedence is fixed in `derive_status` (`:71`) and the order matters: an
integration nobody configured is `not_configured`, not `disabled`.

Timestamps are ISO 8601, or `null`. `capabilities`, `usedBy`, and `actions` are
always arrays, possibly empty, never `null`.

`actions` is the subset of `["configure", "test", "disable", "enable"]` the
current user may perform, computed server-side in `_actions` (`:88`) from the
user's permissions. **A2 renders exactly what is in this array** and does not
compute permissions client-side. Note `enable` and `disable` are mutually
exclusive — whichever applies to the current state is present.

`message` is one short sentence for the operator; on `degraded` it says what
failed. Defaults come from `_default_message` (`:131`). It must never contain a
credential, token, or endpoint URL with embedded auth.

### `POST /api/integrations/<key>/test`

Runs a live connection test and returns the refreshed descriptor. Slow by nature
— A2 shows a pending state and does not time out under 30s.

**`GET` never tests.** Every underlying `test_connection` commits `last_test_*`
columns, so testing from a GET would rewrite history just by looking at it and
make listing the hub as slow as its slowest network round-trip. Describing reads
stored state; testing is an explicit action. Do not "helpfully" add a refresh to
the list endpoint.

### `PUT /api/integrations/<key>/enabled`

Body `{ "enabled": true|false }`. Returns the refreshed descriptor.

### `GET /api/integrations/<key>/activity`

Returns recent activity for the provider — the Activity tab of the detail screen.

### Authorization

Per-integration, not per-route. The three alert-routing integrations (SMTP,
Slack, webhooks) are admin-only because the routes behind them are. This is
already enforced in `routes/integrations.py`.

### Providers

`jira`, `zoho`, `jenkins`, `smtp`, `slack`, `webhooks`, `registry`, `bitbucket`,
`hermes`. Subject to the Gate 0 scope decision — A2 renders whatever the array
contains.

---

## 3. Job platform interface

**Producer:** A1 · **Consumer:** A1, later A3 (agent commands) · **Status:** to build

Replaces the daemon threads currently doing production work:

```
backend/api/services/alert_policy_scheduler.py:179
backend/api/services/deploy_automation_service.py:1923
backend/api/services/zoho_sync_service.py:1764
backend/api/services/mobile_app_service.py:787
backend/api/services/cluster_build/executor.py:1952
backend/api/services/application_analysis_local_docker.py:279, :447
backend/api/services/application_pull_request_local_docker.py:203
backend/api/services/auth_service.py:262
backend/api/cache_warmer.py:33
backend/api/seed.py:460
```

All are `threading.Thread(daemon=True)` inside the Flask process. They die
silently on restart, never retry, and leave no record.

### Enqueue

```python
enqueue(
    job_type: str,
    payload: dict,
    idempotency_key: str,     # required — dedupes retries and double-submits
    max_attempts: int = 3,
    timeout_seconds: int = 300,
    actor_user_id: int | None = None,
) -> str                      # returns job_id
```

Enqueuing the same `idempotency_key` twice returns the existing `job_id` and does
not create a second job. This is what makes agent commands and deploy retries
safe.

### Job state

```
queued → running → succeeded
                 → failed        (attempts exhausted)
                 → cancelled
                 → dead_letter   (failed and not retryable)
```

Every transition is persisted before the work continues. A worker crash mid-job
leaves the row in `running` with a stale heartbeat; the reaper returns it to
`queued` if attempts remain.

### Job record

```json
{
  "jobId": "01J...",
  "jobType": "deploy.execute",
  "state": "running",
  "attempt": 1,
  "maxAttempts": 3,
  "progress": { "step": "applying manifests", "percent": 40 },
  "createdAt": "2026-08-02T10:00:00Z",
  "startedAt": "2026-08-02T10:00:02Z",
  "finishedAt": null,
  "error": null,
  "actorUserId": 7
}
```

Tables live in `backend/api/models_jobs.py`, not `models.py`.

### Rules

- Every job handler is idempotent. It may run twice.
- Every job writes an audit record attributed to `actorUserId`.
- Payloads are redacted before logging — no credentials in job rows or logs.
- Long-running work reports progress; a job with no progress event inside its
  timeout is killed and retried.

---

## 4. Session and authentication contract

**Producer:** A3 · **Consumer:** A2 · **Status:** to build

Replaces the `localStorage` JWT in `frontend/src/authStorage.js`.

### What must not break

The existing flow is more than login. A3 preserves every one of these, currently
in `backend/api/routes/auth.py`:

| Endpoint | Purpose |
|---|---|
| `POST /api/auth/login` | Credentials → session or MFA challenge |
| `POST /api/auth/first-login/change-password` | Forced password change |
| `POST /api/auth/first-login/totp/setup` | TOTP enrolment |
| `POST /api/auth/first-login/totp/verify` | Confirm enrolment |
| `POST /api/auth/mfa/verify` | MFA challenge at login |
| `GET  /api/auth/me` | Current user |
| `POST /api/auth/logout` | End session |

`backend/api/decorators.py` enforces first-login completion on every protected
endpoint (`require_auth` rejects users with `first_login_completed == False`).
That behaviour is load-bearing and stays.

### Target session model

- Short-lived access session in a cookie: `HttpOnly`, `Secure`, `SameSite=Lax`.
- Rotating refresh token, also `HttpOnly`, single-use, reuse detection revokes
  the family.
- Server-side session records so sessions can be listed and revoked.
- CSRF protection on all cookie-authenticated mutations: double-submit token,
  header `X-CSRF-Token`.
- No token in `localStorage`, no token in JS-readable storage at all.

### What A2 must know

- The client sends `credentials: "include"` on every request.
- The client reads a CSRF token from `GET /api/auth/csrf` and sends it as
  `X-CSRF-Token` on every non-GET.
- On `401`, the client calls the refresh endpoint once, then retries; a second
  `401` means log out. `frontend/src/api/client.js` already has
  `setUnauthorizedHandler` — that is the hook.
- The frontend never parses a token. Identity comes only from `GET /api/auth/me`.

`GET /api/auth/csrf` and `POST /api/auth/refresh` are new endpoints A3 adds.

---

## 5. Production startup guards

**Producer:** A3 · **Consumer:** operators · **Status:** to build

When `KUBESIGHT_ENV=production`, the app refuses to start if any of these hold.
Refuse means exit non-zero with a clear message naming the setting — never a
warning, never a degraded start.

| Guard | Current risk |
|---|---|
| Default or missing secret key | — |
| Debug mode enabled | — |
| Authentication disabled | `auth_required_enabled()` in `auth_utils.py` can turn auth off entirely |
| Default seeded users still enabled | `seed.py` creates demo users |
| Credential encryption key missing | `secret_encryption.py` |
| Database migrations not at head | No Alembic yet — coordinate with A1 |
| Unsafe CORS | `CORS_ORIGINS` defaults to `*` (`backend/api/__init__.py:175-180`) |

The migrations guard depends on A1 landing Alembic. Ship the other six first and
add it after.

Implementation lives entirely in `backend/api/production_guards.py`, exposing
`run_startup_guards(app)`. A1 inserts the single call site.
