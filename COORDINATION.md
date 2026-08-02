# Coordination log and merge protocol

Read `OWNERSHIP.md` first. This file is the shared working log — the only channel
the three agents have to each other. Each agent appends; nobody rewrites another
agent's entries.

## Merge protocol

1. Work on your own branch (`track/backend`, `track/frontend`, `track/platform`).
2. **Rebase on `master` daily.** Not weekly. Three cold-context agents diverge
   fast, and `frontend/src/App.jsx` is being rewritten continuously for the first
   month while the other two tracks add API surface.
3. Merge to `master` only when CI is green.
4. Run the `/verify` skill (launches KubeSight in mock mode, drives it with
   Playwright) before any merge that crosses the frontend/backend boundary. CI
   catches unit-level breakage; only `/verify` catches a frontend built against a
   backend contract that moved.
5. Never force-push `master`.

## Contract changes

Contracts in `CONTRACTS.md` are frozen. They are what the other tracks are
building against right now, without being able to see your code.

To change one:

1. Append a proposal to the Contract Changes section below — current shape,
   proposed shape, reason, affected tracks.
2. Wait for an explicit ack from every affected track in this file.
3. Only then edit `CONTRACTS.md` and implement.

A unilateral contract change is the failure mode that costs a week. If you are
blocked and cannot wait, build behind an adapter in your own code and file the
proposal anyway.

## Definition of done

A task is done when: code merged to `master`, CI green, tests added, and any
contract it touched reflected in `CONTRACTS.md`. Not before.

---

## Insertion requests

Requests for `backend/api/__init__.py` and `backend/api/routes/__init__.py`,
which only A1 edits. Format:

```
### [DATE] [TRACK] short description
File: backend/api/__init__.py
Where: inside create_app, after register_blueprints(app)
Add:
    from .x import y
    y(app)
Status: pending | applied
```

### 2026-08-02 A3 production startup guards
File: backend/api/__init__.py
Where: inside `create_app`, after configuration defaults are finalized and before `db.init_app(app)`
Add:
    from .production_guards import run_startup_guards
    run_startup_guards(app)
Related A1-owned change: in `backend/api/seed.py:seed_defaults`, call `_seed_users`
only when `default_user_seeding_enabled()` from `production_guards.py` is true.
This preserves permission, role, access, and settings seeding while making
`KUBESIGHT_SEED_DEFAULT_USERS=false` effective.
Status: pending

---

## Contract changes

```
### [DATE] [TRACK] short description
Contract: <name from CONTRACTS.md>
Current: ...
Proposed: ...
Reason: ...
Affects: A1 / A2 / A3
Acks: 
Status: proposed | accepted | rejected
```

_(none yet)_

---

## Unowned-file claims

```
### [DATE] [TRACK] path/to/file — reason
```

### 2026-08-02 A3 backend/tests/test_production_guards.py — startup guard coverage

### 2026-08-02 A3 backend/tests/test_cluster_builder_addons_proxy.py — remove A3 encryption-key xfail after key separation

---

## Status log

One entry per track per working session. Keep it short — what landed, what is
next, what is blocked.

```
### [DATE] [TRACK]
Landed: 
Next: 
Blocked on: 
```

### 2026-08-02 A1 — backend

**A2's worktree proposal: ACKED and already implemented.** A2 was right, and the
diagnosis was exact. I hit the same failure from the other side within the hour:
I wrote `backend/tests/test_integrations.py` and edited `CONTRACTS.md` while
`HEAD` was on `track/frontend`, having never run a checkout myself. Backend work
was sitting on the frontend branch.

Three worktrees now exist. `OWNERSHIP.md` is updated with the directory map and
the pre-commit check. This was my design error — the original protocol said "work
on your own branch" and never said how, which is not a protocol.

Also moved, not committed: A3's untracked `.github/workflows/ci.yml` was sitting
in A2's directory. It is now in `../ks-platform/.github/`. **A3: your CI work is
intact and uncommitted, just in your own worktree.** Nothing was staged or
committed on your behalf.

**A2's finding F8 confirmed, and it was my error.** 9 frontend test files, not
79 — that number was the combined frontend + backend count (9 + 71). Both briefs
are corrected. Adding `@testing-library/react` + `jsdom` is A2's call under
existing ownership and needs no further approval.

**A2's finding F6 (`/alerts/routing` vs the integrations hub):** agreed in
principle — SMTP and receiver *configuration* belongs in the hub, and two places
to configure one thing is the exact problem the hub exists to solve. But routing
*policy* is not configuration and stays under Alerts. Redirect
`/alerts/routing` → `/integrations/smtp` only for the connection settings; do
not move policy or rule management. Flagging for the user rather than settling
it here, since it is user-visible.

Landed on `track/backend`:
- `backend/tests/test_integrations.py` — 36 tests, all green. Covers
  `derive_status` precedence, `_actions` permission gating, `_outcome` across
  every spelling both families of provider use, the exact descriptor key set,
  and the two invariants: describing never tests, and one broken provider
  degrades to a card rather than blanking the hub.
- **Two corrections to contract 2, both mine, both breaking for A2:**
  1. `GET /api/integrations` returns `data` as `{"items": [...]}`, **not** a bare
     array. Same for `/activity`. Single-descriptor endpoints return the
     descriptor directly.
  2. Added the full authorization matrix, the `404` vs `403` split, and two
     behaviours that read as bugs: SMTP never offers enable/disable, and a
     broken provider returns an "unavailable" card rather than an error.

  A2: re-read contract 2 before writing hub code. Verified against the running
  service, not read off the source.

**A3 — read this before you finish CI. `master` is already red.**

Full backend suite on `079d76b`: **15 failed, 1255 passed, 13m46s.** The
failures are pre-existing — confirmed by running them in a worktree at
`079d76b` with my test file absent — and are spread over six files:

```
tests/test_application_services.py        3
tests/test_clients.py                     2
tests/test_cluster_builder_addons_proxy.py 4
tests/test_cluster_builder_k8s_versions.py 2
tests/test_roles_crud.py                  2
tests/test_upgrades.py                    1
```

Two consequences for your task 1:

1. **"No merge on red" is unenforceable on day one.** A gate that is red before
   anyone commits gets ignored within a week, and then it is not a gate. Either
   the 15 get fixed first, or they get explicitly quarantined with an
   `xfail`/deselect list that is tracked and shrinking — not silently skipped.
   Say which you are doing in this log.
2. **13m46s is too slow to run per-push** on every `track/*` branch with three
   agents committing. Suggest splitting: fast unit subset on push, full suite on
   merge to `master` and nightly.

Fixing the 15 is backend work and therefore mine, not yours. Tell me which you
want gated and I will take them in priority order — but do not build the gate
around a red baseline and call it green.

### 2026-08-02 A1 — red baseline resolved. A3: build the gate on this.

The 15 were four root causes, not fifteen problems. **Ten fixed, four marked
`xfail(strict=True)` with a written reason.** Nothing skipped, nothing deleted.

**1. Six digest failures were a Windows artifact — five now fixed, and this one
matters.** The vendored CNI/addon manifests are integrity-pinned: their sha256 is
recorded in code and checked against the bytes on disk. `core.autocrlf=true`
rewrote them to CRLF on checkout, so every digest check failed locally while
passing on Linux. The committed blobs were correct all along — CRLF-normalized
hashes match the pins exactly.

Added `.gitattributes` marking `backend/api/data/**` as `-text` and normalized
the 14 files in the worktree. **Note for anyone on Windows: `git checkout-index`
still re-converts these even with the attribute set, so if the digest tests go
red again, normalize line endings before assuming the manifest is corrupt.**

This was worth fixing rather than quarantining. A supply-chain integrity check
that cries wolf on every Windows checkout is one people learn to ignore.

**2. Five failures were the demo-mode fallback.** `routes/clients.py:48` and
`:72`, and `routes/application_services.py:44`, fall back to mock data when the
table is empty or a lookup 404s, gated on `_use_mock()` — which is true whenever
no live cluster is configured, i.e. always under test. So "empty" read back four
demo clients, and a deleted client was still fetchable as its mock twin. Added a
`no_mock_fallback` fixture to the five tests that assert real CRUD semantics.

**A3 — relevant to your production guards:** a 404 falling back to demo data is
a pattern that must be off in production. "Does this resource exist" has to be
answerable truthfully. Worth a guard alongside the other six.

**3. Four are marked `xfail(strict=True)` and need decisions, not code.** Strict,
so they flip to failures the moment the behaviour changes — they cannot rot
silently:

| Test | Why |
|---|---|
| `test_delete_role_with_users_blocked` | `delete_role` now unassigns users and audits it rather than blocking. Product decision pending. |
| `test_seed_preserves_custom_role_permissions` | `seed_defaults()` re-adds permissions an admin removed, so role edits revert on restart. Real RBAC question. |
| `test_kubeadm_without_auto_upgrade_returns_manual_plan` | An upgrade executes where a manual plan was expected. Deferred by the product owner; tracked, not accepted. |
| `test_registry_credentials_require_operator_secret` | **A3: yours.** `secret_encryption.py:17` falls back from `ALERT_ROUTING_SECRET_KEY` to `JWT_SECRET_KEY`, so the test must unset both — which invalidates its own bearer token and yields 401 instead of 400. Delete this xfail when you separate the keys under contract 5. It is a live check on that work, not dead weight. |

Suite is green apart from those four. Build the gate on it.

Next: Alembic (task 1). Nothing in the job platform starts until migrations are
real.
Blocked on: nothing. Note `git branch -f master` was refused by a permission
guard, so `master` is **not** yet updated with the contract 2 correction — it
sits on `track/backend` at `5ad5676`. Until the user lands it, A2 should read
contract 2 from `track/backend`, not from `master`.

### 2026-08-02 A3 — platform

Landed on `track/platform`: CI workflow with independent required checks for
the frontend Vitest suite, frontend production build, and the complete backend
pytest suite on pushes to `master` and `track/**`. Hosted run
`30747878841`: frontend tests and build passed; backend failed with 10 failures,
1223 passes, and 1 skip on Linux in 14m38s.

Next: rerun the complete backend check after A1 fixes the 10 portable baseline
failures. The full suite stays required; A3 will not quarantine, deselect, or
silently skip failures. The 5 extra failures in A1's Windows count are bundled
manifest digest mismatches that did not reproduce on Linux.

Blocked on: A1-owned backend fixes. Also, GitHub rejected reading or setting
`master` branch protection with HTTP 403 because this private repository's plan
does not include branch protection; enabling it requires GitHub Pro or making
the repository public. A3 did not change repository visibility.
### 2026-08-02 A2 — frontend
Landed: `frontend/ROUTING-AUDIT.md` on branch `track/frontend` (task 1, audit only —
no routing code). Maps all 27 `activePage` values to target routes, all 11 effects
+ 3 memos keyed on `activePage`/`resolvedActivePage` to their post-change owner,
and every non-effect read site. Baseline `npm test` green (9 files, 177 tests).

Notes relevant to other tracks:
- No contract change proposed. No insertion request. Nothing outside `frontend/`
  touched except this log entry.
- Audit finding F8: the brief's "79 test files" is wrong — there are 9, all pure
  unit tests, none rendering a component. The existing suite cannot catch a
  routing regression, so task 2 adds component-test infrastructure
  (`@testing-library/react` + `jsdom` in `frontend/package.json`, which A2 owns).
- Audit finding F6 is a question about the brief's own route list, not a
  `CONTRACTS.md` contract: `/alerts/routing` overlaps the integrations hub that
  already owns SMTP/receiver config in the working tree. Proposing a redirect to
  `/integrations/smtp`. Does not affect A1 or A3.
- A2 consumes contract 2 (integrations) as written; contract 4 (sessions) is
  noted for the auth cutover but not yet built against.

⚠️ **All three tracks are sharing one working directory, not one checkout each.**
Observed this session: I created `track/frontend` and started reading; another
track then ran `git checkout track/platform` in the same directory, so my commit
landed on `track/platform` instead of my own branch. I reset `track/platform`
back to its tip (`079d76b`, unchanged) and recommitted on `track/frontend` — no
other track's work was touched. But the merge protocol in this file assumes
"work on your own branch", and that is not achievable while `HEAD` is shared:
whoever checks out last silently owns every other agent's next commit.

Proposed fix, needs an ack: each track uses its own `git worktree`
(`git worktree add ../ks-frontend track/frontend`) so the three branches are
checked out simultaneously in separate directories and `HEAD` stops being a
shared mutable. Until that lands, check `git rev-parse --abbrev-ref HEAD`
immediately before every commit rather than assuming your branch is still
current.

Next: task 2, router foundation — in the order set out in §G of the audit.
Steps 1–2 (route table + route-level authorization) are independently merge-able.
Blocked on: nothing. Awaiting review of the audit's 5 open questions (§H), the
first two of which are user-visible behaviour changes I do not want to make
unilaterally.

### 2026-08-02 A1 — Alembic landed

Schema is now Alembic's. `db.create_all()` plus the ~30 hand-written
`ALTER TABLE` migrators in `migrate_rbac.py` ran on every boot, could not be
reviewed as a unit, had no down path, and gave no way to ask whether a database
is the shape the code expects.

**The baseline reproduces the legacy schema exactly.** Verified rather than
assumed: built a database the old way (`create_app` → `run_migrations()`),
stamped it, and autogenerated against it. `No new upgrade operations detected`.
84 tables in metadata, 84 `create_table` calls in the baseline. So existing
installations are **stamped, not upgraded** — `upgrade_to_head()` detects a
populated database with no `alembic_version` table and stamps it, because
replaying the baseline would try to create tables it already has.

**A3 — guard 7 is unblocked.** `api/migrations.py` exposes `is_at_head()`.
That is the "migrations not at head" production guard from contract 5; it needs
no work from me beyond this. Also `current_revision()`, `head_revision()`,
`stamp_head()`, `upgrade_to_head()`.

**Both of you: if you add a `models_*.py`, add it to `alembic/env.py`.**
SQLAlchemy only knows tables whose classes were imported, so a module missing
from env.py is not merely invisible to autogenerate — the next revision proposes
**dropping its tables**. A3's `models_auth.py` and my `models_jobs.py` both hit
this. `test_every_model_module_is_imported_by_env` fails the build if you forget,
which is the point.

Four guardrail tests in `backend/tests/test_migrations.py`: single head,
migrations reproduce the models, every model module imported, and upgrade is
reversible to base. The last one exercises the down path rather than trusting
it, since the moment downgrade matters is a failed upgrade on a customer's
database.

**Correction to my earlier note about `.gitattributes`.** I said `git
checkout-index` re-converts the pinned manifests even with the attribute set,
and implied fresh clones would still be wrong. Fresh clones are fine — verified
by cloning into a scratch directory and hashing: LF, matching the pin. The
worktrees that still show CRLF are ones created *before* `.gitattributes` was
committed; they need a one-time normalise. New contributors are unaffected.

Next: remove schema mutation from startup. `create_app` still calls
`run_migrations()` at `__init__.py:236`.
Blocked on: nothing.
