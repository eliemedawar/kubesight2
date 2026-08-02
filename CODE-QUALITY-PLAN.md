# Code quality plan

Every item here comes from a defect that actually occurred, not from a style
preference. Where a rule is proposed, the incident that motivates it is named.

**Current state: 6.5/10. Realistic target: 9.** Ten would require a rewrite that
costs more than it returns on a 155k-line product with real feature depth.

## Measured baseline

| | |
|---|---|
| Total | ~155,000 lines (backend `api/` + `frontend/src`) |
| Backend tests | 78 files, ~1,380 tests, 13–16 min |
| Frontend tests | 9 files, 177 tests, ~1 s |
| `except Exception: pass` | **38** |
| Broad `except Exception` | 175 |
| Files over 1,000 lines | 12 (largest: `models.py` at 2,846) |
| Linter | none |
| Type checker | none |
| Return annotations in services | ~78% (1,294 / 1,667) |

## The one finding behind most of this

Every serious defect found in the session that produced this document shared a
shape: **a stated guarantee with nothing asserting it.**

- `apply_yaml` took a `confirmation` argument and never read it. Any string
  applied arbitrary YAML to a live namespace. 1,255 passing tests missed it.
- `claim_next` documented oldest-first ordering; 52% of same-millisecond job ids
  sorted backwards.
- `k8s_entrypoint.sh` documented why the web tier must stay at one worker; a
  comment was the only thing enforcing it.
- Five `timeAgo` implementations, three mishandling timezones identically.

The comments were usually right. The code often was not, and nothing checked.
This codebase is not badly written — it is **under-verified**, which is a far
cheaper problem.

---

# Workstream 1 — Test what is expensive to get wrong

**Owner: A1 (backend), A2 (frontend). Effort: ongoing, ~2 days to establish.**

Chasing a coverage percentage would not have caught any of the above. The rule
that would have:

> Every endpoint that **writes, authorises, or destroys** has a test asserting it
> **refuses** — not only a test asserting it works. Those are different tests and
> only one was being written.

Applies to: confirmation phrases, permission checks, idempotency, and anything
whose failure is silent.

**Acceptance:** each route module under `backend/api/routes/` with a non-GET
handler has at least one refusal test. Track the list in this file; it shrinks.

**Already done:** `test_frontend_static.py`, `test_job_queue.py`,
`test_integrations.py`, `test_stale_analyses.py` were written this way.

---

# Workstream 2 — Delete `except Exception: pass`

**Owner: A1. Effort: 2–3 days across 38 sites.**

The most repeated defect shape in the codebase. Every outbound email, the deploy
failure notifier, the auth notifications — all swallowed. A failure was
indistinguishable from a success, which is the worst property an audited system
can have.

> A broad `except` must re-raise, log at error with context, or record state.
> Never `pass`.

**Acceptance:** zero `except Exception: pass` in `backend/api/`; a lint rule
fails the build on reintroduction. Where swallowing is genuinely correct (a
best-effort cleanup), it logs at debug and says why in a comment.

---

# Workstream 3 — Static analysis

**Owner: A3 (CI wiring), A1/A2 (fixes). Effort: 1 day to gate, ongoing to clear.**

There is no linter and no type checker. `ruff` would have flagged the unused
`confirmation` parameter as dead code. `mypy` would have caught the naive
datetime comparisons behind the `timeAgo` bugs.

- `ruff` for Python, `eslint` for the frontend
- `mypy` on `backend/api/services/` first — 78% of its functions are already
  annotated, so the ramp is short
- **Gate the diff, not the tree.** Retrofitting 155k lines is a project; failing
  on newly-touched files is a config file.

**Acceptance:** CI fails on a lint or type error in changed files. Baseline
violations are recorded, not fixed in one pass.

---

# Workstream 4 — A test suite fast enough to run

**Owner: A3. Effort: 2–3 days.**

13–16 minutes is why commits reached `master` unverified during the session that
wrote this — including the author's. **A gate people cannot afford to run is not
a gate.**

Most of that time is `create_app` rebuilding the schema per test.

- Session-scoped schema with per-test transaction rollback
- Split tiers: unit (<2 min, every push), full (merge to `master` and nightly)

**Acceptance:** unit tier under two minutes; full suite still required before
merge to `master`.

---

# Workstream 5 — Break up the files two people cannot share

**Owner: A1, A2. Effort: opportunistic, no dedicated sprint.**

Twelve files exceed 1,000 lines. These are the merge-conflict generators —
exactly the problem solved by convention when `models_jobs.py` and
`models_auth.py` were split out rather than added to `models.py`.

| File | Lines |
|---|---|
| `backend/api/models.py` | 2,846 |
| `frontend/src/pages/ApplicationIntelligencePage.jsx` | 2,584 |
| `backend/api/k8s_provider.py` | 2,430 |
| `backend/api/services/helm_chart_template_service.py` | 2,362 |
| `backend/api/services/cluster_build/executor.py` | 2,062 |
| `backend/api/services/deploy_automation_service.py` | 2,019 |

**Do not schedule a refactor.** Split on the next substantial change to each,
the way `models_cluster_build.py` already was. A rewrite with no behavioural
goal is risk without return.

**Acceptance:** no *new* file passes 1,000 lines; the existing twelve shrink as
they are touched.

---

# Workstream 6 — One canonical helper per concept

**Owner: A2 (frontend), A1 (backend). Effort: ongoing.**

Five `timeAgo` implementations. Two components duplicating two others — inside a
task named "shared component layer," which is where duplication is most
expensive, because the layer's whole claim is that there is now one of each.

Both were found the same way: someone went to *use* a thing and found another
already there.

> Before adding a shared helper or component, migrate one existing consumer onto
> it first. If a consumer cannot be found, the abstraction is speculative.

**Acceptance:** `timeAgo` converged (4/5 done; `clusterBuilder` keeps its own
deliberately — its tests assert its wording, and editing tests to accommodate a
refactor inverts what tests are for).

---

# Workstream 7 — Demo mode out of production paths

**Owner: A1, with a production guard from A3. Effort: 2 days.**

`routes/clients.py:48` and `:72`, and `routes/application_services.py:44`,
return demo data when a table is empty **or when a lookup 404s**. Deleting a
client and fetching it returned its mock twin.

In a governance product, *does this resource exist* must be answerable
truthfully. A plausible fake is worse than an error.

**Acceptance:** demo data is a deployment mode selected at startup, not a branch
inside request handlers; a production guard refuses to boot with it enabled.

---

# Sequencing

| Stage | Work | Buys |
|---|---|---|
| **Week 1** | Workstreams 2, 3 | Defect *classes* stop recurring. Highest value per hour. |
| **Weeks 2–3** | Workstreams 1, 4 | Verification becomes affordable, so it happens. |
| **Ongoing** | Workstreams 5, 6, 7 | Coherence; the codebase stops resisting parallel work. |

Workstreams 2 and 3 are most of the value and are roughly a week, because they
are rules and configuration rather than refactoring.

# Explicitly not doing

- **Chasing a coverage number.** It would not have caught a single defect found
  so far, and it rewards testing the easy paths.
- **A scheduled refactor of the large files.** Behaviour-preserving rewrites with
  no functional goal are risk without return.
- **Retrofitting types across 155k lines.** Gate the diff instead.
- **Pursuing 10/10.** Nine is reachable and worth it. The last point costs more
  than it returns here.
