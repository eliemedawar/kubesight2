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

_(none yet)_

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

_(none yet)_

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
