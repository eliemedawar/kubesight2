# Brief — Track A2, Frontend

You are one of three AI agents working this repository in parallel. You have no
visibility into the other two. Read `OWNERSHIP.md`, `CONTRACTS.md`, and
`COORDINATION.md` at the repo root before writing code. This brief is
self-contained; you are not expected to have prior context.

## The product

KubeSight is a Kubernetes operations platform being taken from an internal tool
to a commercial self-hosted product. Positioning: *a governed Kubernetes
operations platform that helps platform teams observe, approve and safely execute
changes across their clusters.* Target customer runs 3–50 clusters with a small
platform team and real approval and audit requirements.

The codebase is roughly 68k lines of frontend and 84k of backend, 155 commits. It
works. This is a hardening and restructuring effort, not a greenfield build.

## Your track

Frontend. You own the entire `frontend/` tree as sole writer, including
`package.json`. Nobody else edits it. You do not edit anything outside it.

Your job in order of priority: real URL routing, a rebuilt navigation, a shared
component layer, then the Integrations hub.

## Current state — the facts that matter

**There is no router.** `frontend/package.json` has exactly two dependencies:
`react` and `react-dom`. Navigation is `useState`.

**`frontend/src/App.jsx` is 1,922 lines** and is the hard part of your track.
`const [activePage, setActivePage] = useState("dashboard")` is at line 126, and
`activePage` is threaded through roughly twenty sites. Critically, it is not only
a view switch — data-fetching effects are keyed on it:

- `:966` and `:976` — upgrade job polling, runs only when `activePage === "upgrade"`
- `:1138` — inventory fetch, keyed on `activePage === "inventory"`
- `:1146` — application details fetch, keyed on `activePage === "applicationDetails"`
- `:408` and `:225-232` — permission-based page resolution and redirect

Replacing `activePage` with routes means rehoming that fetch logic into the route
components. Do not treat this as a mechanical find-and-replace; the effects
carry real behaviour, including the permission fallback that redirects a user
away from a page they cannot see.

**34 pages** in `frontend/src/pages/`. Some may be cut in a scope-reduction pass
that has not landed yet — build the route table so that removing a page is
deleting one entry, not unpicking a switch statement.

**API client:** `frontend/src/api/client.js` is the real client.
`frontend/src/api.js` is a deprecated re-export shim with a `@deprecated` banner —
import from `./api/*` modules, never add to `api.js`.

**Auth today:** `frontend/src/authStorage.js` puts a JWT in `localStorage`. This
is being replaced by cookie sessions by another track. See contract 4 in
`CONTRACTS.md` — build the client against the target model, not the current one,
and coordinate the cutover through `COORDINATION.md`.

**Tests:** 79 test files exist and `npm test` runs `vitest run`. Keep them green.
`frontend/src/utils/clusterBuilder.test.js` alone is 1,074 lines — that is real
coverage you can break.

**Dashboard synthetic data:** `frontend/src/dashboard/useDashboardSeries.js`
seeds a random walk when no readings exist. This is fabricated data shown to
operators as if it were real. It gets deleted, replaced by honest "unavailable"
states. Do not extend it.

## Tasks in order

### 1. Audit before code (days 1–3)

Deliverable before you write any routing: a table mapping every `activePage`
value to its target route, and every effect keyed on `activePage` to the
component that will own it after the change. Commit it as
`frontend/ROUTING-AUDIT.md`. This is what makes the rest of the track reviewable.

### 2. Router foundation (weeks 1–4)

Add a router. Decompose `App.jsx`. Target routes:

```
/
/fleet/clusters            /fleet/clusters/:clusterId
/workloads                 /workloads/:clusterId/:namespace
/applications              /applications/:applicationId
/alerts                    /alerts/policies      /alerts/routing
/changes/requests          /changes/bundles
/integrations              /integrations/:provider
/integrations/:provider/configuration
/integrations/:provider/activity
/admin/users               /admin/audit          /admin/settings
```

Requirements: browser back/forward, bookmarkable pages, filters and selected tab
in the URL, route-level authorization preserving the current permission
behaviour, real not-found and access-denied pages, breadcrumbs.

`frontend/src/pages/AccessDeniedPage.jsx` already exists — wire it to the router
rather than replacing it.

### 3. Navigation (weeks 4–6)

Five groups: **Home**, **Operate** (clusters, workloads, topology, logs, alerts),
**Applications**, **Changes**, **Administration**.

- Active section stays expanded.
- Click controls expansion. **Not hover.** Hover-driven navigation is the
  specific complaint driving this rework.
- Permission filtering stays. Modules disabled by plan or config stay hidden.
- Mobile uses the same hierarchy.

### 4. Shared component layer (weeks 5–8)

Build these before the Integrations hub, so the hub is the first consumer rather
than a retrofit: page header with breadcrumbs, status cards, save/discard bar,
unsaved-change warning, loading / empty / degraded / permission states, tables
with search + filter + sort + pagination, activity timelines, copyable
identifiers, confirmation dialogs, freshness indicators.

### 5. Integrations hub (weeks 9–16)

**The backend for this already exists** — `backend/api/services/integrations_service.py`
and `backend/api/routes/integrations.py`, normalizing all nine providers. You are
not blocked on another agent for it. Partial frontend scaffolding also exists in
the working tree: `frontend/src/lib/integrations.js` and
`frontend/src/api/integrationsApi.js`. Read all four before starting.

Build strictly against contract 2 in `CONTRACTS.md`. Four states only:
`connected`, `degraded`, `disabled`, `not_configured`. There is no `configured`
field in the payload — do not expect one. Render the `actions` array the backend
sends — do not compute permissions client-side. Do not hardcode the provider
list.

Each provider detail screen: **Overview**, **Configuration**, **Activity**,
**Used by**.

Jira first and complete, as the reference implementation the rest follow.

### 6. Dashboard and search (weeks 17+)

Attention feed answering "what needs attention?" in priority order: critical
alerts, failed deployments, degraded integrations, pending approvals, unhealthy
clusters, upgrade risks, expiring credentials. Every item carries severity,
scope, detection time, owner, recommended action, and a direct link.

Delete the synthetic series. Add `Ctrl/Cmd+K` global search, permission-aware.

## Protocol

- Branch `track/frontend`. **Rebase on `master` daily** — you are rewriting
  `App.jsx` continuously for a month while two other tracks add API surface.
- Merge only on green CI.
- Run the `/verify` skill before any merge touching the API boundary.
- Log status in `COORDINATION.md` each session.
- If a backend contract blocks you, build against the contract as written and
  file a change proposal. Do not wait, and do not invent a different shape.

## Do not

- Edit anything outside `frontend/`. Need a backend change? File an insertion
  request or contract proposal in `COORDINATION.md`.
- Add to `frontend/src/api.js`.
- Change a contract unilaterally.
- Extend `useDashboardSeries.js`.
- Delete or rewrite existing tests to make them pass. Fix the code.
