# KubeSight URL Routing — implementation plan

**Goal:** every view in KubeSight has an address. Browser Back returns you to the last
thing you opened (page, tab, or record). Links are shareable and survive a refresh.

> **Status: BUILT and VERIFIED 2026-09-21.** All six phases are done, plus a follow-up
> audit pass. Four Playwright suites pass against the app in mock mode — 47/47 route
> matrix, 12/12 behaviour, 17/17 rendered-tab and integrations, 28/28 tab addresses —
> with **no console or page errors anywhere**. `npm test` is green at 199 tests, 21 of
> them new in `routeUrl.test.js`. §5 records what each hazard turned out to be; the
> issues found after the first pass are **17**-**21**.

**Decisions taken** (2026-09-21):

| Decision | Choice |
|---|---|
| URL style | **Hash routing** — `http://host/#/service-catalog/payments-api/builds` |
| Depth | Page + tab + selected entity. Filters, search text and modals stay local state |
| Cluster / namespace | In the URL as query params on the pages that use the topbar selectors |

### Why hash

`vite.config.js` builds with `base: "./"` (relative asset URLs) and
`backend/api/frontend_static.py` serves **only** `/` — a path route like `/clusters` would
404 on refresh *and* resolve `./assets/…` to `/clusters/assets/…`. Hash routing needs zero
backend change, survives sub-path hosting behind the ingress, and keeps the `file://`
preview path in `api/client.js:7` working.

Security: the fragment is never sent to the server, so cluster and namespace names stay out
of Flask/ingress access logs and out of the `Referer` header on outbound requests — strictly
less leakage than path routing. The only real risk class is DOM-XSS / open redirect from an
unvalidated fragment, which applies to path routing equally and is closed by §3 (parse
against a fixed table, reject unknown values, never render a URL value as HTML, validate
`returnTo` against the route table).

The choice is isolated in `routeUrl.js`. Switching to path routing later means rewriting two
functions there, flipping `base` to `"/"`, and adding a Flask catch-all. Not a rewrite of
this work.

---

## 1. Where we are today

- **No router.** `App.jsx:129` — `const [activePage, setActivePage] = useState("dashboard")`.
  A 30-case `switch` in `renderPage()` (`App.jsx:1429`) picks the page component.
- **30 addressable pages**: 28 in `NAV_PAGES` (`utils/authz.js:69`) plus two drill-downs in
  `DRILL_DOWN_PAGES` (`utils/authz.js:990`): `applicationDetails`, `clusterOverview`.
  `clusterOverview` is currently **unreachable from the UI** — nothing navigates to it.
  Routing gives it an address; whether to link it from ClustersPage is a separate call.
- **Cross-page state is already faked** through storage, because there is no URL:
  - `lib/alertDisplay.js` — `setAlertsTabHint` / `consumeAlertsTabHint` (sessionStorage)
  - `lib/settingsSections.js` — `setSettingsSectionHint` / `consumeSettingsSectionHint`
  - `pages/TicketingPage.jsx:20` — `readLastProvider` (localStorage)
  - `components/catalog/BuildsPanel.jsx:51` — per-service view preference (localStorage)

  The first two are deep-link workarounds and get **deleted**. The last two are genuine
  preferences and stay, but the URL wins whenever it carries a value.
- **Sidebar renders `<button>`** (`components/layout/Sidebar.jsx:391`) — no href, so no
  middle-click, no "open in new tab", no status-bar preview.
- **Tests are pure-node vitest** (`vite.config.js` → `environment: "node"`, 10 test files, no
  React Testing Library). So the route parser/serializer must be a **pure module with no DOM
  imports** to be testable — that is the single most valuable test in this plan.

---

## 2. Route table

Path segments are kebab-case; page keys stay camelCase internally. `?cluster` / `?ns` appear
only on pages listed in `CLUSTER_CONTEXT_PAGE_KEYS` / `NAMESPACE_CONTEXT_PAGE_KEYS`
(`utils/authz.js:231` and `:246`) — do not widen those sets.

| Page key | Route | Query | Sub-state source today |
|---|---|---|---|
| `dashboard` | `/dashboard` | `?cluster` | — |
| `clusters` | `/clusters` | `?cluster` | — |
| `clusterManagement` | `/cluster-management` | — | — |
| `clusterOverview` | `/clusters/:clusterId/overview` | — | drill-down, unreachable today |
| `namespaces` | `/namespaces` | `?cluster&ns` | — |
| `resources` | `/resources/:tab` | `?cluster&ns` | `App.jsx:161` `resourceActiveTab`; `ResourcesPage.jsx:205` internal fallback |
| `topology` | `/topology` | `?cluster&ns` | `TopologyPage.jsx:25` `{level, namespace}` |
| `inventory` | `/inventory/:section` (`templates`\|`helm`) | `?cluster` | `InventoryPage.jsx:53` |
| `applicationDetails` | `/inventory/app/:appId/:tab` | `?cluster` | `App.jsx:151` + `ApplicationDetailsPage.jsx:105` |
| `myRequests` | `/my-requests/:tab` (`active`\|`history`) | — | `MyRequestsPage.jsx:15` |
| `changeBundles` | `/change-bundles/:tab` | — | `ChangeBundlesPage.jsx:251` |
| `logs` | `/logs` | `?cluster&ns&pod` | `App.jsx:160` `preferredLogPod` |
| `alerts` | `/alerts/:tab` (`open`\|`history`\|`policies`) | `?cluster` | `AlertsPage.jsx:155` + tab hint |
| `serviceCatalog` | `/service-catalog` | — | `ServiceCatalogPage.jsx:64` `opened` |
| ↳ service detail | `/service-catalog/:serviceId/:tab` | `?build` | `ServiceDetailPage.jsx:60`, `:68` `openBuildId` |
|  | tabs: overview, source, application, pipeline, mergechecks, dockerfile, builds, artifacts, settings | | `routeTable.js` `TAB_VALUES.serviceDetail` — a tab absent from that list makes the whole route unparseable |
| `blueprints` | `/blueprints` · `/blueprints/:blueprintId` | — | `BlueprintsPage.jsx:270` `detail` |
| `applicationServices` | `/app-services` · `/app-services/:serviceId/:tab` | — | `ApplicationServicesPage.jsx:1542`, `:1440` |
| `applicationIntelligence` | `/application-intelligence` · `/application-intelligence/:appId/:tab` | — | `ApplicationIntelligencePage.jsx:2160` tab, `:1277` selectedId |
| `components` | `/components` · `/components/:componentId` | — | `ComponentsPage.jsx:222` |
| `clients` | `/clients` · `/clients/:clientId` | — | `ClientsPage.jsx:176` |
| `userManagement` | `/users/:tab` (`users`\|`roles`) | — | `UserManagementPage.jsx:122` |
| `auditLogs` | `/audit-logs` | — | — |
| `apiTokens` | `/api-tokens` | — | — |
| `deploymentRequests` | `/deployment-requests/:tab` | — | `DeploymentRequestsPage.jsx:126` |
| `imageRegistries` | `/image-registries` | — | — |
| `ticketing` | `/ticketing` · `/ticketing/:provider/:tab` | — | `TicketingPage.jsx:99` + `ProviderWorkspace.jsx:37` |
| `integrations` | `/integrations` · `/integrations/:integrationKey` | — | `settings/IntegrationsHub.jsx:107` `selectedKey` |
| `settings` | `/settings/:sectionId` | — | `SettingsPage.jsx:63` + section hint |
| `mobileApps` | `/mobile-apps` · `/mobile-apps/:appId` | — | `MobileAppsPage.jsx:67` |
| `clusterBuilder` | `/cluster-builder/:tab` (`floor`\|`new`\|`sources`) · `/cluster-builder/builds/:buildId` | — | `ClusterBuilderPage.jsx:37`, `:43` |
| `upgrade` | `/upgrade` | `?cluster` | — |

**Tab vocabularies to copy verbatim** — a typo here is a silent fallback to the first tab,
which is exactly the kind of bug that survives review:

- resources → `getVisibleResourceTabs()`, keys from `lib/resourceTypes.js`
- alerts → `open` `history` `policies` (`AlertsPage.jsx:341`)
- service detail → `overview` `source` `application` `pipeline` `dockerfile` `builds`
  `artifacts` `settings` (`ServiceDetailPage.jsx:20`)
- applicationIntelligence → `Overview` `Findings` `Architecture` `APIs` `Configuration`
  `Container & build` `Deployment` `History` (`ApplicationIntelligencePage.jsx:72`) —
  **slugify these**, they contain spaces and `&`
- settings → `profile` `appearance` `workspace` `notifications` `security`
  (the `link:` rows in `lib/settingsSections.js` are navigations to other pages, not sections)
- ticketing → `overview` `fieldsync` `tickets` (`ProviderWorkspace.jsx:20`)
- clusterBuilder → `floor` `new` `sources` (`ClusterBuilderPage.jsx:139`)

---

## 3. Modules to add

All under `frontend/src/routes/`.

### `routeTable.js` — pure data, no React or DOM imports

```js
export const ROUTES = [
  { page: "dashboard", path: "/dashboard", query: ["cluster"] },
  { page: "resources", path: "/resources/:tab", query: ["cluster", "ns"],
    defaults: { tab: "pods" } },
  { page: "serviceCatalog", path: "/service-catalog" },
  { page: "serviceDetail",  path: "/service-catalog/:serviceId/:tab",
    navKey: "serviceCatalog", defaults: { tab: "overview" }, query: ["build"] },
  // …one row per line of §2
];
```

- `navKey` — which sidebar entry highlights. Replaces the hardcoded
  `activePage === "applicationDetails" ? "inventory"` at `App.jsx:1857`.
- **Order matters.** List longer/more specific paths before their prefixes
  (`/inventory/app/:appId/:tab` before `/inventory/:section`). Add a test asserting no route
  in the table shadows a later one.

### `routeUrl.js` — the only file that knows about `#`

```js
parseRoute(hash)  -> { page, params, query, raw }   // never throws; unknown -> null
buildRoute(route) -> "#/service-catalog/payments-api/builds?build=1284"
```

Rules:

- `encodeURIComponent` on every param in, `decodeURIComponent` on every param out.
- Unknown page, unknown tab value, or a param failing its validator → return `null`, and the
  caller redirects to the default page. **Never** pass an unrecognised value through.
- Drop query keys not declared in that route's `query` list — this is what kills
  `?next=http://evil`.
- Omit params equal to their default, so URLs stay short and canonical.
- **Idempotence invariant:**
  `buildRoute(parseRoute(x)) === buildRoute(parseRoute(buildRoute(parseRoute(x))))`.

### `routeUrl.test.js` — node-env vitest, no DOM

- round-trip every row of `ROUTES` with representative params
- the idempotence invariant above
- params containing `/`, `%`, spaces and unicode survive a round trip
- `parseRoute("#/nope")`, `parseRoute("")`, `parseRoute("#/resources/bogus-tab")` behave
- `?next=…` and `?cluster=<script>` are dropped or escaped
- no route shadows a later one

### `useRouter.js` — the one place that touches `window.history`

```js
const { route, navigate } = useRouter();
navigate({ page, params, query }, { replace: false });
```

**The bug to avoid:** `history.replaceState()` does **not** fire `hashchange`. If you listen
to `hashchange` alone and use `replaceState` for redirects, React never re-renders and the
URL silently desyncs from the UI. So:

1. `navigate()` calls `history.pushState`/`replaceState` **and** `setRoute(next)` directly.
2. The `hashchange` + `popstate` listener exists **only** to catch user-driven back/forward
   and manual URL edits.
3. Both paths compare `buildRoute(next) === buildRoute(current)` first and no-op if equal —
   the echo guard that prevents an infinite loop.
4. `navigate()` to an identical URL is forced to `replace`, so double-clicking a nav item
   does not stack duplicate history entries.

### `RouterContext.jsx` + `useRouteParam.js`

`useRouteParam` is the lever that makes the per-page conversion mechanical. It has the **same
signature as `useState`**:

```js
// before
const [tab, setTab] = useState("overview");
// after
const [tab, setTab] = useRouteParam("tab", "overview");
```

and for query values: `const [buildId, setBuildId] = useRouteQuery("build", null);`

Each write issues a `navigate` on the current page with that one param changed. Default mode:
`push` for `useRouteParam` (tabs are things Back should undo), `replace` for `useRouteQuery`
with an explicit `{ push: true }` opt-in.

---

## 4. Push vs replace — decide once, apply everywhere

| Action | Mode |
|---|---|
| Sidebar nav, opening a record, changing a tab | **push** |
| Changing cluster or namespace in the topbar | **push** (Back should undo a cluster switch) |
| Permission fallback to first allowed page | **replace** |
| Normalising a URL (filling a default tab, dropping a stale query) | **replace** |
| An effect correcting invalid state (namespace not in list → first namespace) | **replace** |
| Closing a drawer/modal that is not in the URL | no navigation at all |
| Login redirect and post-login restore | **replace** |

Getting this wrong is the difference between "Back works" and "Back needs seven presses".

---

## 5. Hazards — where this breaks if done naively

Each is a real line in the current code. Work through them in order.

1. **`App.jsx:417–429` will wipe a URL-seeded cluster.** The effect clears
   `selectedClusterId` whenever `allowedClusters` is empty — and `allowedClusters` **is**
   empty on first render, before the core load resolves. A refresh on
   `#/resources/pods?cluster=prod` would drop `prod` before the cluster list arrives.
   **Fix:** gate the whole effect on clusters having loaded at least once (a
   `clustersLoadedRef` set in `loadCoreData`, or gate on `!loadingState.core`). Verify by
   refreshing a deep link with the network throttled.

2. **`App.jsx:282–286` becomes a render loop.** The `resolvedActivePage → setActivePage` sync
   effect exists only because there are two sources of truth. Once the URL is the single
   source, **delete it**. Leaving it in will fight `navigate()`.

3. **`App.jsx:403–413` must not run before auth resolves.** It already guards on
   `authLoading` and `visiblePages.length`; keep both, and change `setActivePage` to
   `navigate(..., { replace: true })`. If it fires while permissions are still loading it
   rewrites a valid deep link to `/dashboard`.

4. **Sidebar highlight.** `App.jsx:1857` hardcodes
   `activePage === "applicationDetails" ? "inventory" : activePage`. Replace with
   `route.navKey ?? route.page` from the table, or every new drill-down de-highlights the
   sidebar.

5. **`App.jsx:430–443` namespace correction must be `replace`.** It runs whenever the
   namespace list changes. As a `push` it injects a history entry on every cluster switch,
   so Back bounces between namespaces.

6. **Delete the hint mechanisms.** `setAlertsTabHint`/`consumeAlertsTabHint`
   (`lib/alertDisplay.js`) and `setSettingsSectionHint`/`consumeSettingsSectionHint`
   (`lib/settingsSections.js`), plus call sites at `SettingsPage.jsx:125`,
   `SettingsPage.jsx:64` and `AlertsPage.jsx:156`. Leaving them in means a sessionStorage
   value silently overrides the URL on the next mount — an intermittent bug that is very hard
   to reproduce. `SettingsPage.followLink` (`:123`) becomes a plain `navigate` with the tab
   in params.

7. **`ServiceCatalogPage.jsx:85` polling guard.** `if (!canView || opened) return` — `opened`
   now comes from the route. The poll must still stop when a service is open and restart on
   Back to the catalog, so the effect dep becomes the route param, not local state.

8. **Coach marks must not re-fire on tab changes.** `App.jsx:370–389` and `:392–401` key on
   `resolvedActivePage`. They must key on `route.page` **only** — keying on the full route
   restarts the tour on every tab change and calls `markTourSeen` for the wrong key.

9. **`ResourcesPage.jsx:205` has two sources for the tab** — an `activeTab` prop from App and
   an `internalActiveTab` fallback. Collapse to one (the route) or they will disagree.

10. **`useNamespaceResourceCache` enablement.** `App.jsx:238` computes `resourceCacheEnabled`
    from `pageNeedsResourceData(resolvedActivePage)`. Feed it `route.page`. Hand it a
    sub-route key like `serviceDetail` and the cache silently stops loading.

11. **Unauthenticated deep link.** `App.jsx` renders `LoginPage` before the shell. Capture the
    requested route, log in, then `navigate(saved, { replace: true })`. **Validate the saved
    route through `parseRoute` before using it** — that is the open-redirect guard. Store it
    in a module variable or sessionStorage keyed to this tab, never in the URL.

12. **`applicationDetails` must load from the route.** `App.jsx:1162` sets
    `selectedApplicationId` and then `setActivePage("applicationDetails")`. On a cold refresh
    of `#/inventory/app/:appId/overview` nothing has fetched the detail — the load effect has
    to trigger off the route param, not off a click handler.

13. **Params that can contain `/` or `%`.** Namespaces are DNS-1123 (safe), but service ids,
    blueprint ids and cluster ids are not guaranteed to be. `encodeURIComponent` in and
    `decodeURIComponent` out, unconditionally, with a test that round-trips a `/`.

14. **Sidebar `<button>` → `<a href>`.** Gives middle-click and open-in-new-tab. `onClick`
    must `preventDefault()` for plain left clicks only — let ctrl/cmd/middle/shift through to
    the browser. Watch the CSS: `.nav-link` is styled for a `<button>`, and this codebase has
    a known CSS-specificity bug class from the Cardinal work. Check both themes and the
    flyout (`Sidebar.jsx:380–400`) after the swap.

15. **Scroll position.** On `push`, scroll the main pane to top. On `pop`, restoring the prior
    scroll is nice-to-have; at minimum do **not** scroll to top on a pop, or Back into a long
    list is disorienting.

16. **`RouteLoadingFallback`** (`components/common/RouteLoadingFallback.jsx:8`) keys its label
    off the page key — pass `route.page` and add labels for the new sub-route keys.
    *Resolved by design: sub-routes resolve to their owning `pageKey`, so the existing
    labels still apply and no new ones were needed.*

17. **Found during verification — the Settings scrollspy overwrote the address.** Opening
    `#/settings/security` never scrolled to that card, so the `IntersectionObserver` saw
    the top card (Profile) and rewrote the URL to `#/settings` a moment after load. It only
    reproduced on the slower login path, not on a warm reload — exactly the kind of race
    that survives a quick manual check. Fixed in `pages/SettingsPage.jsx` with an initial
    scroll to the addressed anchor plus a `spyReadyRef` gate that ignores observer
    callbacks until that scroll has settled.

18. **Found during verification — record ids are numbers, URL ids are strings.** Every
    `selectedId === record.id` comparison silently stopped matching once the id came back
    from the URL. Fixed with a `sameId()` helper in `ClientsPage`, `ApplicationServicesPage`
    and `MobileAppsPage`; `useEntityRoute` documents that its value is always a string.

19. **Found in the audit pass — a drill-down this plan missed entirely.**
    `pages/settings/IntegrationsHub.jsx` keeps `selectedKey` for which integration is open,
    and §2 had no route for it. Added `integrationDetail` → `/integrations/:integrationKey`.
    The lesson: §2 was built by reading the page list, and this one lives under
    `pages/settings/`, so it was never in the sweep. Anything reachable that holds a
    "which record is open" value needs an address, wherever the file sits.

20. **Found in the audit pass — one dual source survived.** `ApplicationDetailsPage.jsx`
    mirrored the routed `activeTab` prop into local state and synced it with an effect. It
    worked, because the click handler wrote both, but it is the exact pattern hazard 9 is
    about. Collapsed to the prop.

21. **Found in the audit pass — an equivalent-but-differently-spelled hash was not tidied.**
    `#audit-logs` (no slash) or a default tab written out in full parses to the current
    route, so the `hashchange` listener returned early and left the address bar in a
    non-canonical state. It now rewrites to the canonical form with `replaceState`, which
    adds no history entry. In-app links never hit this — they `preventDefault` and go
    through `navigate()` — but a hand-edited URL or an external link can.

---

## 6. Phases

Each phase ends green and shippable. Do not merge two phases into one commit.

**Phase 0 — pure modules, zero behaviour change.** `routeTable.js`, `routeUrl.js`,
`routeUrl.test.js`. `npm test` green. Nothing imports them yet. This is where the correctness
is won.

**Phase 1 — page-level routing.** `useRouter`, `RouterContext`, provider mounted in
`main.jsx`. Delete `activePage` state; fix hazards 2, 3, 4. All 30 pages reachable by URL;
Back/forward across pages works. Sidebar still `<button>` at this stage.

**Phase 2 — cluster and namespace query params.** Seed `selectedClusterId` /
`selectedNamespace` from the route on first render. Fix hazards 1 and 5. Test by refreshing a
deep link on a throttled network.

**Phase 3 — tabs, one page per commit.** Convert with `useRouteParam`, simplest first so the
pattern is proven before the hard ones: `myRequests`, `deploymentRequests`, `userManagement`,
`changeBundles`, `inventory`, `alerts` (delete the hint), `settings` (delete the hint),
`resources` (hazard 9), `clusterBuilder`, `ticketing`, `applicationIntelligence` (slugified
tabs).

**Phase 4 — entity drill-downs.** `serviceCatalog`/`serviceDetail` (hazard 7),
`applicationDetails` (hazard 12), `mobileApps`, `blueprints`, `clients`, `components`,
`applicationServices`, cluster-builder builds, `clusterOverview`. Each one: opening pushes,
Back closes and returns to the list.

**Phase 5 — polish.** Sidebar anchors (14), login `returnTo` (11), scroll behaviour (15),
`RouteLoadingFallback` labels (16), coach-mark keys (8).

**Phase 6 — verification.** §7.

### What was actually built

| Module | Role |
|---|---|
| `frontend/src/routes/routeTable.js` | the table, tab vocabularies, slug helpers — pure data |
| `frontend/src/routes/routeUrl.js` | `parseRoute` / `buildRoute`; the only file that knows about `#` |
| `frontend/src/routes/routeUrl.test.js` | 21 tests: round trip, idempotence, encoding, rejection |
| `frontend/src/routes/RouterContext.jsx` | `RouterProvider`, `useRouter`, `useRouteParam`, `useRouteQuery`, `useEntityRoute`, `pageHref` |

`useEntityRoute(listKey, detailKey, paramName)` was added beyond the original design: it
turns a list/detail pair of routes into one `useState`-shaped value, which is what made the
Clients, App Services, Mobile Apps and Blueprints drill-downs one-line conversions.

The `componentDetail` row was dropped from the table: `ComponentsPage` has no detail view
to open, so the address would have rendered nothing.

---

## 7. Verification

Run the `verify` skill — mock mode plus Playwright — and walk this matrix. Automate what you
can; the Back-button assertions are the point of the whole exercise.

**Per route (all 30 plus every sub-route):**

1. Navigate there through the UI → URL matches §2 exactly.
2. Copy the URL, hard-refresh → same view, same tab, same record, same cluster/namespace.
3. Back → the previous view, not the dashboard.
4. Forward → returns.

**Targeted cases that historically break this kind of change:**

- Refresh `#/resources/pods?cluster=<non-default>&ns=<non-default>` on a throttled network —
  both cluster and namespace survive (hazard 1).
- Service Catalog → open a service → Builds tab → open a build → Back ×3 lands on the catalog
  list, one step per action, with the poll running again (hazard 7).
- Settings → a `link:` row that jumps to Alerts Policies → Back returns to Settings on the
  same section (hazard 6).
- Switch cluster in the topbar, then Back → previous cluster restored, exactly one step.
- Change a tab on a page with a running tour → the tour does not restart (hazard 8).
- Log out, open a deep link, log in → land on the deep link, not the dashboard (hazard 11).
- Open a deep link as a user **without** permission for it → redirected to their first allowed
  page, no flash of the forbidden page, and the URL is *replaced* not pushed, so Back does not
  re-enter the loop.
- `#/resources/bogus`, `#/nope`, `#/clients/does-not-exist` → graceful default, never a crash
  or a blank shell.
- Middle-click and ctrl-click a sidebar item → new tab on the right page (hazard 14).
- Both light and dark themes after the sidebar `<a>` swap (hazard 14).

**Regression guard:** `npm test` in `frontend/` stays green; `routeUrl.test.js` covers every
row of `ROUTES`.

**Two traps in writing these tests**, both of which produced false failures the first time:

- **Assert the canonical address, not the one you typed.** `#/resources/pods` correctly
  becomes `#/resources` (a param equal to its default is omitted) and `#/resources/configMaps`
  correctly becomes `#/resources/configmaps` (enumerated values are slugified). Both are the
  designed, unit-tested behaviour. Better still, assert what is *on screen* — the Resources
  tab bar marks the active tab with `.resources-tab-bar button.active`, not `role="tab"`.
- **Click something that is actually clickable.** In the topology graph only namespace nodes
  drill down; `.topo-node` also matches the cluster and node-pool boxes, which do nothing by
  design. Filter on the `topo-node--click` class. Sidebar links live in `inert` flyouts until
  their section is hovered, so hover `.sidebar-section-trigger` first — or skip the chrome and
  `page.goto` the address.

---

## 8. Out of scope (deliberately)

Search text, tile filters, severity filters, sort order and open modals stay in local
component state. Putting them in the URL was considered and rejected: without careful
replace-vs-push on every keystroke they flood the history stack and make Back useless — the
opposite of the goal. If a specific filter turns out to be worth sharing, it can be added
later as one declared query key on one route, with no change to this architecture.
