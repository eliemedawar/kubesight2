# File ownership map

Three agents work this repo in parallel on separate branches. Ownership is
**exclusive**: if a path is owned by another track, you do not edit it, even for a
one-line change. Violating this is the single most expensive mistake available,
because the other agent cannot see your edit and will overwrite it on their next
rebase.

| Track | Agent | Branch | Working directory |
|---|---|---|---|
| A1 — backend core | Claude Opus 5 (session 1) | `track/backend` | `../ks-backend` |
| A2 — frontend | Claude Opus 5 (session 2) | `track/frontend` | `kubesight2` (main) |
| A3 — platform & security | ChatGPT 5.6 | `track/platform` | `../ks-platform` |

## One worktree per track — read this first

The three tracks do **not** share a working directory. Each has its own `git
worktree`, so all three branches are checked out at once in separate directories
off the same repository.

This is not a preference. A2 hit the failure it prevents: with one shared
directory, `HEAD` is a single mutable pointer, so one track running
`git checkout track/platform` silently redirected another track's next commit
onto the wrong branch. Whoever checks out last owns everyone else's work.

Already created — do not re-create:

```
kubesight2/     → track/frontend   (A2)
../ks-backend   → track/backend    (A1)
../ks-platform  → track/platform   (A3)
```

**Work only inside your own directory.** Never run `git checkout <branch>` in a
worktree you did not create; `git worktree` refuses to check out a branch that is
already checked out elsewhere, which is exactly the guard rail that was missing.

Before every commit, confirm where you are:

```bash
git rev-parse --abbrev-ref HEAD
```

If it does not name your track's branch, stop and post in `COORDINATION.md`
before doing anything else.

Uncommitted work belonging to another track that appears in your directory is not
yours to commit. Move it to that track's worktree and say so in the log.

## A1 — backend core

```
backend/api/models.py                 sole writer (62 classes, 2846 lines)
backend/api/models_jobs.py            new
backend/api/services/**
backend/api/routes/**                 EXCEPT auth.py, api_tokens.py
backend/api/seed.py
backend/api/__init__.py               sole writer — see insertion protocol
backend/api/routes/__init__.py        sole writer — see insertion protocol
backend/api/migrate_rbac.py
alembic/**                            new
alembic.ini                           new
```

## A2 — frontend

```
frontend/**                           entire tree, sole writer
```

Includes `frontend/package.json`. A2 is the only track that adds frontend
dependencies.

## A3 — platform & security

```
.github/workflows/**                  new
helm/**                               new
Dockerfile*, docker-compose*.yml
backend/api/production_guards.py      new
backend/api/models_auth.py            new — sessions, refresh tokens
backend/api/routes/auth.py
backend/api/routes/api_tokens.py
backend/api/auth_utils.py
backend/api/passwords.py
backend/api/secret_encryption.py
k8s/**
```

## Unowned

Everything else — docs at root, `concept/`, `design-system/`, `backend/tools/`.
Claim it in `COORDINATION.md` before editing so the other two see it.

## Why new model modules instead of models.py

`backend/api/models.py` is 2,846 lines with 62 model classes and is needed by two
tracks at once. The repo already establishes the alternative convention:

- `backend/api/models_application_intelligence.py`
- `backend/api/models_cluster_build.py`

So new tables go in a new domain module. A1 puts the job platform tables in
`models_jobs.py`; A3 puts session and refresh-token tables in `models_auth.py`.
Neither touches `models.py`. This removes the worst backend merge conflict
entirely.

Existing tables still live in `models.py` and only A1 modifies them. If A3 needs a
column on an existing model, that is an insertion request.

## Shared-file insertion protocol

Two files require cross-track changes. A1 owns both and applies changes on
request; A2 and A3 never edit them directly.

**`backend/api/__init__.py`** — app factory. Insertion points:

| Line (approx) | What is there |
|---|---|
| 183 | `def create_app(config_object=None) -> Flask` |
| 229 | `register_blueprints(app)` |
| 237 | `seed_defaults()` |
| 264 | `start_alert_policy_scheduler(app)` |

**`backend/api/routes/__init__.py`** — blueprint registration (68 lines).

To request an insertion, append an entry to the Insertion Requests section of
`COORDINATION.md` with the exact line you want added and where. A1 applies it on
the next merge. Keep your own logic in your own module and expose a single
entry-point function so the insertion stays one line.

Example — A3's production guards:

```python
# backend/api/production_guards.py   (A3 owns)
def run_startup_guards(app) -> None: ...
```

```python
# backend/api/__init__.py            (A1 inserts, near the top of create_app)
from .production_guards import run_startup_guards
run_startup_guards(app)
```
