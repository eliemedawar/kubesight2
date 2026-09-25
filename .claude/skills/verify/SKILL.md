---
name: verify
description: Launch KubeSight locally in mock mode and drive it with Playwright to verify frontend/backend changes end-to-end.
---

# Verifying KubeSight changes end-to-end

## Launch (mock mode, isolated data)

1. Copy `backend/instance/kubesight.db` to a scratch file, then deactivate
   custom clusters (required — `K8S_REAL_MODE=false` is ignored while any
   cluster row has `is_active=1`, see `backend/api/k8s_provider.py`):
   ```sql
   UPDATE clusters SET is_active=0;
   ```
2. Backend (port 5000):
   ```
   cd backend
   DATABASE_URL="sqlite:///<abs scratch path>" K8S_REAL_MODE=false python app.py
   ```
   Confirm `GET http://127.0.0.1:5000/health` reports `"kubernetesMode": "mock"`.
3. Frontend: `cd frontend && npx vite --port 5173 --strictPort`. If the port is
   busy, a dev server is probably already running against this same source
   tree (HMR picks up your edits) — just use it.

## Drive (Playwright, no browser download)

- `p.chromium.launch(channel="msedge", headless=True)`; viewport 1600x900,
  `device_scale_factor=2` for crisp screenshots.
- Login: `admin`/`admin123` (full access) or `viewer`/`viewer123` (non-admin)
  — both seeded, fully onboarded, no MFA. Inputs via `get_by_label("Username"/"Password")`.
- Sidebar nav items are always-visible `<a href="#/...">` links grouped under
  labels (layout in `frontend/src/lib/navigation.js`): scope to the sidebar,
  `page.locator("aside.sidebar").get_by_role("link", name=..., exact=True)`.
  Build Center and Architecture are workspaces — their pages are tabs in the
  `.ws-tabs` strip on top of the page (and nested in the sidebar while open).
  Ctrl+K opens the jump palette. Every view has a URL
  (`location.hash = "#/alerts/history"`). See ROUTING-PLAN.md for the table.
- Seed `kubesight.coachmarks.v1.<userId>` = `{"seen":{},"muted":true}` in an
  init script, or the tips overlay (`.cm-blocker`) swallows clicks.
- Topbar cluster/namespace selectors are custom `SearchableSelect`
  (`.ss-wrap` trigger → `.ss-dropdown .ss-option`); namespace selector only
  exists on namespace-scoped pages. Mock fixtures only populate the
  `payments` namespace with pods.

## Gotchas

- Coach marks (guided tips) auto-start on first visit per page per user —
  they sit at z-index 6000 and block clicks. Dismiss with `Escape` or clear
  via `localStorage` key `kubesight.coachmarks.v1.<userId>` in an init script
  if they get in the way of other flows.
- Judge dim/overlay rendering by sampling pixels (PIL on `page.screenshot()`),
  not by eyeballing screenshots — 42% backdrop over a light theme is easy to
  misread.
- Kill the mock backend when done so the real one can bind port 5000.
