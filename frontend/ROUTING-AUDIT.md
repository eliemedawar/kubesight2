# Routing audit — `activePage` → routes

Track A2, task 1. Deliverable before any routing code is written.

**Subject:** `frontend/src/App.jsx` (1,922 lines), `const [activePage, setActivePage] =
useState("dashboard")` at `:126`.

**Scope of this document:** every value `activePage` can hold and the route that
replaces it; every effect keyed on `activePage` or `resolvedActivePage` and the
component that owns that fetch afterwards; every non-effect read site; the app
state that has to become URL state for the "bookmarkable, filters in the URL"
requirement to be met.

**Not in scope:** the router library choice, the navigation rebuild (task 3), and
the shared component layer (task 4). Those follow from this table.

---

## 0. Summary of what makes this non-mechanical

`activePage` is not a view switch. Four things are entangled in it:

1. **Data fetching.** Six effects read it directly and five more read the
   permission-resolved `resolvedActivePage`. Two of those effects are pollers
   with intervals, one is a three-phase progressive namespace load, and one is
   a 30-second-deduped alerts fetch.
2. **Authorization.** `resolvedActivePage` (`:220-232`) is a *silent redirect*:
   an unpermitted page key is rewritten to the first allowed page and written
   back into `activePage` by an effect at `:279-283`. There is no denied state
   today — the user simply lands somewhere else.
3. **Chrome.** Cluster/namespace selector visibility, the no-clusters banner,
   the loading-overlay label, the alert badge source, the sidebar highlight, and
   the change-bundle FAB all branch on it.
4. **Two shadow copies of the same value.** `activePage` and
   `resolvedActivePage` disagree for one render on every navigation, and
   different effects read different ones. `:965` reads `activePage`; `:1066`
   reads `resolvedActivePage`. That inconsistency is load-bearing in at least
   one place (see finding F4) and must be resolved deliberately, not by picking
   one at random.

---

## A. Page inventory → target routes

27 `activePage` values (25 in `NAV_PAGES` at `utils/authz.js:75-198`, plus 2
drill-downs in `DRILL_DOWN_PAGES` at `authz.js:972`), plus the `default:` case.

Routes marked **(brief)** are frozen by `briefs/BRIEF-A2-FRONTEND.md`. Routes
marked **(proposed)** are mine and are the ones to argue about at review.

| # | `activePage` | Route | Component after change | Nav group | Permission gate (`authz.js`) |
|---|---|---|---|---|---|
| 1 | `dashboard` | `/` **(brief)** | `DashboardPage` | Home | `pageAllowed:1002` — `overview:view` + cluster access |
| 2 | `clusters` | `/fleet/clusters` **(brief)** | `ClustersPage` | Operate | `:1007` — `clusters:view` + cluster access |
| 3 | `clusterOverview` | `/fleet/clusters/:clusterId` **(brief)** | `ClusterOverviewPage` | — (drill-down) | `:983` — `overview:view` + cluster access |
| 4 | `clusterManagement` | `/fleet/connections` **(proposed)** | `ClusterManagementPage` | Operate | `:1009` — any of `clusters:add/update/remove/test` |
| 5 | `namespaces` | `/workloads` **(brief)** | `NamespacesPage` | Operate | `:1016` — `namespaces:view` + granted action + ns access |
| 6 | `resources` | `/workloads/:clusterId/:namespace` **(brief)** | `ResourcesPage` | Operate | `:1029` → `canAccessResourcesPage:903` |
| 7 | `topology` | `/topology` **(proposed)** | `TopologyPage` | Operate | `:1031` — `resources:view` + cluster access |
| 8 | `logs` | `/logs` **(proposed)** | `LogsPage` | Operate | `:1036` → `canAccessLogsPage:931` |
| 9 | `alerts` | `/alerts` **(brief)** | `AlertsPage` (index tab `open`) | Operate | `:1038` — `alerts:view` + granted action + cluster access |
| 9a | `alerts` (tab `history`) | `/alerts/history` **(proposed)** | `AlertsPage` → history | — | same |
| 9b | `alerts` (tab `policies`) | `/alerts/policies` **(brief)** | `AlertPoliciesPage` (nested) | — | same + policy perms, gated inside the page |
| 9c | — | `/alerts/routing` **(brief)** | see finding **F6** — conflicts with `/integrations` | — | admin-only |
| 10 | `inventory` | `/applications` **(brief)** | `InventoryPage` | Applications | `:1022` — `inventory:view` + granted action + cluster access |
| 11 | `applicationDetails` | `/applications/:applicationId` **(brief)** | `ApplicationDetailsPage` | — (drill-down) | `:977` — `inventory:view` + granted action + cluster access |
| 12 | `applicationIntelligence` | `/applications/intelligence` **(proposed)** | `ApplicationIntelligencePage` | Applications | `NAV_PAGES:124` — `applications:view` |
| 13 | `applicationServices` | `/applications/services` **(proposed)** | `ApplicationServicesPage` | Applications | `NAV_PAGES:118` — `app_services:view` |
| 14 | `serviceCatalog` | `/applications/catalog` **(proposed)** | `ServiceCatalogPage` | Applications | `NAV_PAGES:112` — `service_blueprints:view` |
| 15 | `components` | `/applications/components` **(proposed)** | `ComponentsPage` | Applications | `NAV_PAGES:130` — `components:view` |
| 16 | `clients` | `/applications/clients` **(proposed)** | `ClientsPage` | Applications | `NAV_PAGES:136` — `clients:view` |
| 17 | `deploymentRequests` | `/changes/requests` **(brief)** | `DeploymentRequestsPage` | Changes | `:1062` — `deployment_requests:view` |
| 18 | `myRequests` | `/changes/my-requests` **(proposed)** | `MyRequestsPage` | Changes | `:1064` — `deployment_requests:request` |
| 19 | `changeBundles` | `/changes/bundles` **(brief)** | `ChangeBundlesPage` | Changes | `:1066` — `change_bundles:create` or `:view` |
| 20 | `ticketing` | `/changes/ticketing` **(proposed)** | `TicketingPage` | Changes | `:1054` — `ticketing:view` |
| 21 | `upgrade` | `/fleet/upgrades` **(proposed)** | `UpgradeSafeModePage` | Operate | `:1044` — `upgrades:precheck`/`start` + granted action + cluster access |
| 22 | `clusterBuilder` | `/fleet/builder` **(proposed)** | `ClusterBuilderPage` | Operate | `NAV_PAGES:186` — any of `cluster_builds:view/create/execute` |
| 23 | `mobileApps` | `/mobile-apps` **(proposed)** | `MobileAppsPage` | Applications | `:1056` — `mobile_apps:view` |
| 24 | `userManagement` | `/admin/users` **(brief)** | `UserManagementPage` | Administration | `:1058` — `users:view` |
| 25 | `auditLogs` | `/admin/audit` **(brief)** | `AuditLogsPage` | Administration | `:1060` — `audit:view` |
| 26 | `settings` | `/admin/settings` **(brief)** | `SettingsPage` | Administration | `:1050` — any of `SETTINGS_ENTRY_PERMISSIONS` |
| 27 | `imageRegistries` | `/admin/registries` **(proposed)** | `ImageRegistriesPage` | hidden (deep link only) | `:1052` — `registries:view`; `NAV_PAGES.hidden = true` (`authz.js:166`) |
| — | `default:` (`App.jsx:1722`) | `*` | `NotFoundPage` (**new**) | — | see finding **F1** |
| — | *(none — new)* | `/integrations`, `/integrations/:provider`, `/integrations/:provider/configuration`, `/integrations/:provider/activity` **(brief)** | `IntegrationsHub` / `IntegrationDetail`, promoted out of Settings | Administration | per-integration, server-side (`CONTRACTS.md` §2) |
| — | *(none — exists, unwired)* | `/access-denied` | `pages/AccessDeniedPage.jsx` | — | see finding **F2** |

### Reserved first segments

`/applications/:applicationId` sits alongside five literal siblings
(`intelligence`, `services`, `catalog`, `components`, `clients`). Literal-before-param
match order resolves this, and application ids are inventory row ids, so a
collision is not reachable — but the reserved list belongs in the route table as
a comment so a future `/applications/reports` is not added blind.

`/fleet/connections` rather than `/fleet/clusters/connections` for the same
reason, less safely: cluster ids are operator-chosen strings and a cluster
*could* be named `connections`. Keeping it off that path removes the question.

---

## B. State that becomes URL state

The brief requires bookmarkable pages with filters and the selected tab in the
URL. These are the App-level values that have to move.

| Today | `App.jsx` | Becomes | Notes |
|---|---|---|---|
| `selectedClusterId` | `:127` | `?cluster=` on cluster-scoped routes; path segment on `/fleet/clusters/:clusterId` and `/workloads/:clusterId/:namespace` | Read/written by one `ClusterScopeProvider`; the topbar selector becomes a controlled writer of the param, not a state owner |
| `selectedNamespace` | `:128` | `?namespace=`; path segment on `/workloads/:clusterId/:namespace` | Same provider |
| `resourceActiveTab` | `:160` | `?tab=` on `/workloads/:clusterId/:namespace` | Validated against `visibleResourceTabs` (`:212`), fallback preserved from `:214-218` |
| `applicationDetailsTab` | `:145` | `?tab=` on `/applications/:applicationId` | |
| `selectedApplicationId` | `:144` | `:applicationId` path param | The state variable disappears entirely |
| `preferredLogPod` | `:159` | `?pod=` on `/logs` | Replaces the write-then-clear handshake at `:1576-1585` / `:1606` |
| Alerts tab | `AlertsPage.jsx:155` | route segment (`/alerts`, `/alerts/history`, `/alerts/policies`) | Replaces `consumeAlertsTabHint` |
| Alerts filters | `AlertsPage.jsx:162-164` | `?severity=&type=&q=` | |
| Settings section | `SettingsPage.jsx:66` | `/admin/settings/:sectionId` | Replaces `consumeSettingsSectionHint` |
| Integration provider + tab | `IntegrationsHub.jsx:106`, `IntegrationDetail.jsx:203` | `/integrations/:provider` + `/configuration`, `/activity` | Already the brief's shape |
| Ticketing provider | `TicketingPage.jsx:22` (`localStorage`) | `/changes/ticketing/:provider` | See finding **F5** |

### Deep-link hints that the router deletes

Three one-shot storage handshakes exist only because there is no router. All
three go:

- `lib/alertDisplay.js:6-22` — `setAlertsTabHint` / `consumeAlertsTabHint`
  (`sessionStorage`), written by `SettingsPage.jsx:128`, read by `AlertsPage.jsx:156`.
- `lib/settingsSections.js:206-224` — `setSettingsSectionHint` /
  `consumeSettingsSectionHint` (`sessionStorage`), read by `SettingsPage.jsx:67`.
  Its own comment says *"Session storage rather than the URL, matching how the
  rest of the app deep-links — there is no router"*.
- `TicketingPage.jsx:18-34` — `LAST_PROVIDER_KEY` (`localStorage`), same comment
  at `:16`: *"remembered in localStorage, not the URL … there is no router"*.

These are the cheapest deletions in the track and should land with the router,
not after it, so no new code is written against them.

### State that stays out of the URL

`settingsDraft` (`:152`), `seenRequestSignatures` / `dismissedRequestSignatures`
(`:165-166`), theme (`:169-180`), and tour state (`:324`) are per-user or
per-browser, not addresses. They stay in state/storage. Theme in particular must
keep the "local preference wins over API value" rule at `:58-61`.

---

## C. Effects keyed on `activePage` / `resolvedActivePage`

The core of this audit. **11 effects and 3 memos.** Every one moves.

| ID | Lines | Trigger | What it does | Owner after change |
|---|---|---|---|---|
| **M1** | `:220-232` | `activePage`, `visiblePages` | Resolves an unpermitted page key to the first allowed page | **Deleted.** Becomes a `<RequireAccess page="…">` route guard element that renders `AccessDeniedPage` instead of silently redirecting. Behaviour change — see **F2** |
| **E2** | `:279-283` | `resolvedActivePage !== activePage` | Writes the resolution back into `activePage` | **Deleted.** The URL is the single source; there is no second copy to reconcile |
| **E1** | `:400-411` | `isAuthenticated`, `visiblePages`, `activePage` | Post-login redirect off a page the user cannot see | Route guard + a `/` index redirect to `getFirstAllowedPage()`. The *login landing* half is real behaviour and must survive; the *silent bounce* half becomes an explicit denied page |
| **E9** | `:532-698` | `selectedClusterId`, `resolvedActivePage`, `isAuthenticated` | 167 lines: three-phase namespace load (lite → counts → metrics), cluster-overview fetch, resource-cache invalidation on cluster change, namespace re-validation | Split. Namespace loading → `useClusterContext(clusterId)` hook owned by a **`ClusterScopeLayout`** route wrapping every cluster-scoped route. Overview fetch (`:549-569`, `:591`, `:621-628`) → `ClusterOverviewPage`. **Highest-risk migration in the track** |
| **E10** | `:700-765` | `selectedClusterId`, `authUser.id`, `resolvedActivePage` | Loads alerts for the cluster, 30s dedupe via `alertsLoadRef`, skipped on `dashboard` | Split. Topbar badge feed → app-level `AlertFeedProvider` (it is chrome, not page data). List data → `AlertsPage`'s own fetch. Keep the dedupe; keep the dashboard skip (**F4**) |
| **E11** | `:1066-1087` | `resolvedActivePage === "dashboard"`, `selectedClusterId`, `refreshIntervalSeconds` | Loads dashboard summary + 30–60s background poll, clears on cluster change | `DashboardPage`. Poller lifetime becomes route-mount lifetime — strictly better than today |
| **E3** | `:965-972` | `activePage === "upgrade"`, `selectedClusterId` | Resets `upgradeResult`/`targetVersion`, loads upgrade info | `UpgradeSafeModePage` (route `/fleet/upgrades`) |
| **E4** | `:974-1006` | `activePage === "upgrade"`, `upgradeResult.status === "running"`, job id | 3s poll of the upgrade job | `UpgradeSafeModePage`. Note the comment at `:997`: *"Keep polling until the job completes or the user leaves the page"* — unmount is exactly the intended stop condition, so the route boundary expresses this better than the current guard |
| **E5** | `:1137-1143` | `activePage === "inventory"`, `selectedClusterId` | `loadInventory()` | `InventoryPage` (route `/applications`) |
| **E6** | `:1145-1150` | `activePage === "applicationDetails"`, `selectedApplicationId` | `loadApplicationDetail(id)` | `ApplicationDetailsPage`, keyed on the `:applicationId` param. Keep the `applicationDetailRequestRef` race guard (`:1114`, `:1120`, `:1131`) — param changes are faster than page changes were |
| **E7** | `:366-386` | `resolvedActivePage`, auth, `activeTour` | Auto-starts the page tour once per user, 800ms after mount | `TourController` mounted in the shell, keyed on the matched route's page key |
| **E8** | `:390-398` | `activeTour.pageKey !== resolvedActivePage` | Ends and marks-seen a tour when navigating away | Same `TourController`. `getTourSteps` (`tours/tourDefinitions.js:647`) is keyed by page key, so the route table must expose a stable page key per route — do not drop it |
| **M2** | `:234-236` | `resolvedActivePage` via `pageNeedsResourceData` | Enables the namespace resource cache | `ResourcesPage` / `LogsPage`. `RESOURCE_DATA_PAGES` (`accessViewState.js:83`) becomes a route-level flag; the `Set` of page keys goes away |
| **M3** | `:238-249` | `resolvedActivePage`, `resourceActiveTab` | Picks which resource list the cache fetches | `ResourcesPage`, from the `?tab=` param |

### Effect-adjacent async functions that move with them

Not effects, but only called by the above and therefore relocating:

| Function | Lines | Moves to |
|---|---|---|
| `loadUpgradeInfo` | `:926-963` | `UpgradeSafeModePage` |
| `normalizeUpgradePayload` | `:902-924` | `UpgradeSafeModePage` (or `utils/`) |
| `runPrecheck` | `:1217-1242` | `UpgradeSafeModePage` |
| `runUpgradeStart` | `:1244-1314` | `UpgradeSafeModePage` |
| `handleTargetVersionChange` | `:1316-1321` | `UpgradeSafeModePage` — drops its `activePage === "upgrade"` guard, which is unreachable-false once the page owns it |
| `loadDashboardSummary` | `:1008-1059` | `DashboardPage`. Its 404 branch (`:1039-1044`) calls `reloadClusters()` — that dependency crosses into cluster context, so it needs a callback from `ClusterScopeProvider`, not a direct import |
| `loadInventory` | `:1089-1107` | `InventoryPage` |
| `loadApplicationDetail` | `:1109-1135` | `ApplicationDetailsPage` |
| `handleRemoveFromInventory` | `:1184-1198` | `ApplicationDetailsPage` — its `handleNavigate("inventory")` becomes `navigate("/applications")` |
| `handleSaveCatalogEdit` | `:1200-1215` | `ApplicationDetailsPage` |
| `inventoryClusterOptions` / `inventoryNamespaceOptions` | `:1163-1182` | `InventoryPage`. Note `inventoryNamespaceOptions` (`:1179`) is computed and **never passed to anything** — dead, delete it |
| `reloadClusters` / `applyClusterList` | `:443-461` | `ClusterScopeProvider` |
| `fetchSettings` | `:301-310` | `SettingsProvider` (see §E) |
| `saveSettings` / `discardSettingsDraft` / `handleSettingsDraftChange` | `:1365-1417` | `SettingsPage` + `SettingsProvider` |
| `saveAlertRouting` / `testAlertEmailDelivery` | `:1323-1363` | Integrations config panel. **Currently dead in App** — no page receives them (see **F3**) |

---

## D. Non-effect `activePage` read sites

The rest of the ~20 sites. Each needs a decision; none is a find-and-replace.

| Lines | Read | Replacement |
|---|---|---|
| `:312-316` | `handleNavigate(pageKey)` — permission-checked setter | `navigate(path)`; the permission check moves to the route guard so a denied URL renders a denied page instead of doing nothing |
| `:1152-1161` | `handleSelectApplication` | `navigate('/applications/' + id)`. **Currently dead — see F3** |
| `:1446-1449`, `:1738-1741` | Dashboard `onNavigateToUpgrade` / `onNavigateToInventory` + `canOpen*` | `<Link>` to `/fleet/upgrades` / `/applications`, guarded by the same `isPageAllowed` |
| `:1510`, `:1524` | App-details "back" → inventory | `<Link to="/applications">` or router back |
| `:1576-1585` | Resources → Logs with `{clusterId, namespace, pod}` prefill | `navigate('/logs?cluster=…&namespace=…&pod=…')`. Deletes `preferredLogPod` and `onPreferredPodApplied` |
| `:1650-1657` | Cluster builder → `setActivePage("clusters")` with cluster preselect | `navigate('/fleet/clusters/' + clusterId)`. Note this one bypasses `handleNavigate` and its permission check, guarding with `isPageAllowed("clusters")` at the call site instead — preserve that intent via the guard |
| `:1193` | `handleRemoveFromInventory` → `handleNavigate("inventory")` | `navigate('/applications')` |
| `:1717` | `SettingsPage onNavigate` (link-out rows, `settingsSections.js:129/136/143`) | `<Link>` per section; `alertsTab: "policies"` (`settingsSections.js:137`) becomes `/alerts/policies` directly, deleting the hint |
| `:1762` | `<RouteLoadingFallback pageKey={resolvedActivePage}>` | Route-level `Suspense` per route; `PAGE_LABELS` (`RouteLoadingFallback.jsx:4-19`) moves into the route table so a route carries its own loading label |
| `:1768-1772` | `alertBadgeCount` — dashboard summary on `/`, alerts list elsewhere | `AlertFeedProvider`. Keeps the same two sources; the branch becomes "is the dashboard summary fresher" rather than "which page am I on" |
| `:1812-1821` | `clusterBanner` suppressed on 4 pages | Route metadata flag `suppressClusterBanner`. The current list (`userManagement`, `auditLogs`, `settings`, `imageRegistries`) is stale — it omits every non-cluster page added since (`ticketing`, `changeBundles`, `clients`, `components`, …). Fix by inverting: show the banner only where `pageNeedsClusterContext` is true |
| `:1826-1833` | Loading-overlay label, `pageLoading` only on dashboard | Route metadata + `ClusterScopeProvider` loading state |
| `:1839` | `AppShell activePage`, remapping `applicationDetails` → `inventory` for sidebar highlight | Nav-active derived from route match with a `parent` field in the route table. `Sidebar.jsx:142/302/378-380` reads it |
| `:1856-1857` | `showClusterSelector` / `showNamespaceSelector` via `pageNeedsClusterContext` / `pageNeedsNamespaceContext` (`authz.js:963-969`) | Route metadata `scope: "cluster" \| "namespace" \| "none"`. `CLUSTER_CONTEXT_PAGE_KEYS` / `NAMESPACE_CONTEXT_PAGE_KEYS` (`authz.js:213-232`) fold into the route table |
| `:1889` | Change-bundle FAB hidden on `resources` | Route metadata `hideBundleFab` |
| `:1747-1766` | `pageNode` — `NoFeaturesPage` when `visiblePages` is empty | Top-level guard above the router; unchanged behaviour |

**These branches are why the route table needs metadata, not just a path→component
map.** A route entry carries: `path`, `pageKey`, `element`, `permission gate`,
`scope`, `parent`, `loadingLabel`, `suppressClusterBanner`, `hideBundleFab`. That
is what makes the brief's "removing a page is deleting one entry" true.

---

## E. What is left in `App.jsx`

After the migration, `App.jsx` should hold only what is genuinely global:

- auth gates: `authLoading` (`:1776`), `needsOnboarding` (`:1787`), `!isAuthenticated` (`:1795`) — these stay above the router
- `visiblePages.length === 0` → `NoFeaturesPage` (`:1748`)
- theme effect (`:169-180`) → `ThemeProvider`
- request-updates poller (`:805-836`) and its seen/dismissed storage (`:776-801`, `:854-900`) → `RequestUpdatesProvider`. Note this effect is *already* page-independent — its own comment at `:804` says so. It is the one effect that needs no rehoming, only extraction
- change-bundle FAB + drawer (`:1887-1919`) and the `has-bundle-fab` body class (`:122-125`)
- `CoachMarks` host (`:1878-1886`)

New providers to create, each owning a slice of what App holds today:

| Provider | Owns | Replaces |
|---|---|---|
| `ClusterScopeProvider` | cluster list, namespace list, selected cluster/namespace synced to URL, `reloadClusters` | `:127-128`, `:136`, `:191-201`, `:413-441`, `:443-530`, E9 |
| `SettingsProvider` | `settingsDraft`, saved settings, save/discard | `:152-154`, `:301-310`, `:1365-1417` |
| `AlertFeedProvider` | topbar alert badge + notifications list | E10 (badge half) |
| `RequestUpdatesProvider` | deployment-request notifications | `:164-166`, `:776-900` |

Rough accounting: ~1,922 lines → App shell under 200, with the rest distributed
across four providers, one cluster-scope layout route, and the page components
that already exist.

---

## F. Findings

### F1 — `default:` silently renders the dashboard

`App.jsx:1722-1743` duplicates the entire dashboard render block as the fallback
for an unknown page key. There is no not-found state anywhere in the app. The
brief requires a real not-found page, so this is a deliberate behaviour change:
unknown URL → `NotFoundPage`, not a silent dashboard. Flagging because it is
user-visible and someone will file it as a regression.

### F2 — the permission redirect is silent, and `AccessDeniedPage` is unmounted

`resolvedActivePage` (`:220-232`) rewrites an unpermitted page key to
`getFirstAllowedPage()`. The user is moved with no message.
`pages/AccessDeniedPage.jsx` exists but only its named export `NoFeaturesPage` is
imported (`:65-67`); the default access-denied export is never rendered. The
brief says wire it to the router rather than replace it.

**Recommendation:** navigating to a permitted-but-unreachable page renders
`AccessDeniedPage` at that URL. The redirect stays only for the `/` index, where
"land on your first allowed page" is correct rather than surprising. This is the
single behaviour change in the track most likely to need sign-off — calling it
out now rather than at merge.

### F3 — three drill-down paths are dead in the current build

Confirmed by grep across `frontend/src`:

- **`applicationDetails` is unreachable.** `handleSelectApplication` (`:1152`) is
  the only thing that sets `selectedApplicationId` and the only thing that sets
  `activePage = "applicationDetails"`. It is defined and **never passed to any
  component** — `InventoryPage` (`:1485-1498`) receives no selection callback.
- **`clusterOverview` is unreachable.** Nothing anywhere assigns it. It has a
  full render case (`:1471-1482`), a dedicated fetch branch in E9, a tour
  (`tourDefinitions.js:138`), a loading label, a sidebar icon, and an RBAC gate —
  all reachable only by editing the initial `useState`.
- **`saveAlertRouting` / `testAlertEmailDelivery`** (`:1323-1363`) plus
  `savingRouting`, `routingError`, `testingEmail`, `testEmailMessage`
  (`:155-158`) are computed and passed to nothing.

Routing does not cause this, it *fixes* it: `/fleet/clusters/:clusterId` and
`/applications/:applicationId` are reachable by URL the moment they are routes.
But it means these two screens have **no exercised code path today** and should
be treated as untested surface, not as working code being ported. Add the
entry points (cluster card click, inventory row click) as part of task 2 and
verify both render.

### F4 — `activePage` vs `resolvedActivePage` is inconsistent and one case matters

E3/E4/E5/E6 read raw `activePage`; E9/E10/E11 read `resolvedActivePage`. For one
render after any navigation these differ. The consequence that matters: E10
(alerts) skips fetching on the dashboard because the dashboard summary carries
its own alert counts (`:1768-1772`) — a real optimisation, keyed on the resolved
value. E5 (inventory) keyed on the raw value will fire once with a page key that
resolution is about to reject. Under routing there is exactly one value (the
matched route), so this class of bug disappears; noting it so the dashboard skip
is preserved intentionally and not lost as "a stale guard".

### F5 — `TicketingPage` remembers its provider in `localStorage` across users

`LAST_PROVIDER_KEY` (`TicketingPage.jsx:18`) is not namespaced by user id, unlike
the seen/dismissed request keys (`App.jsx:768-773`) and tour state
(`utils/tourStorage.js:14`), which are. Moving the provider into the URL removes
the key and the leak with it. Minor, but it lands free with this work.

### F6 — `/alerts/routing` and `/integrations` overlap

The brief's route list includes `/alerts/routing`, but the working tree has
already moved SMTP and receiver configuration into the integrations hub:
`pages/settings/integrationConfigPanels.jsx:5` imports `SmtpTab` and
`ReceiversTab` from `AlertRoutingPage.jsx`, and `lib/settingsSections.js:9-13`
documents the move — *"SMTP and receivers hid behind an admin-only tab on the
Alerts page … Those connections all live in the hub now"*.

Two addresses for one thing is exactly what the Settings rework was undoing.
**Recommendation:** `/alerts/routing` redirects to `/integrations/smtp`, and the
route list in the brief is amended. Raising as a question rather than deciding
unilaterally, since the route list is in the brief.

### F7 — the integrations hub is inside Settings today, top-level in the brief

`SettingsPage.jsx:32` lazy-loads `IntegrationsHub` as a settings panel;
`settingsSections.js:113-121` registers it as a rail entry. The brief routes it
to top-level `/integrations`. The hub therefore moves out of the Settings rail
and the `integrations` group becomes a link-out row like Administration's. This
touches the untracked working-tree scaffolding (`pages/settings/IntegrationsHub.jsx`,
`IntegrationDetail.jsx`, `lib/settingsSections.js`), all inside `frontend/` and
all mine to change.

### F8 — the brief's test count is wrong

The brief says *"79 test files exist"*. The actual count is **9**:

```
src/components/common/TopologyViewer.test.js
src/components/zoho/zohoFieldMeta.test.js
src/lib/alertFeed.test.js
src/lib/apiTime.test.js
src/utils/accessViewState.test.js
src/utils/addonConfig.test.js
src/utils/applicationIntelligence.test.js
src/utils/clusterBuilder.test.js      (1,074 lines — this figure is correct)
src/utils/logFormat.test.js
```

They are all pure-function unit tests; **none renders a component and none
imports `App.jsx`**. `accessViewState.test.js` is the only one touching
routing-adjacent code, and it tests `resolveAccessViewState`, which this work does
not change.

**The consequence is the important part: the existing suite cannot catch a
routing regression.** "Keep them green" is close to free here and is not
evidence of safety. Task 2 needs new tests — route resolution, route-level
authorization, and the rehomed fetch effects — and those are net-new
infrastructure (no component-test setup exists: no `jsdom` config, no
`@testing-library/react`). Budget for it.

---

## G. Suggested order for task 2

Derived from the dependency structure above, not from the route table's order.

1. Router + route table with metadata; `App.jsx` still owns all state and passes
   it down. No behaviour change, no effect moves. Merge-able on its own.
2. `RequireAccess` guard, `NotFoundPage`, wire `AccessDeniedPage`. Resolves F1/F2.
3. `ClusterScopeProvider` + `ClusterScopeLayout`, cluster/namespace into the URL.
   This is the E9 migration — the riskiest step, and everything cluster-scoped
   waits on it.
4. Leaf pages own their own fetches, one route per commit: dashboard (E11),
   upgrade (E3/E4), inventory (E5), application details (E6), resources (M2/M3).
5. `AlertFeedProvider` (E10 split), `RequestUpdatesProvider`, `SettingsProvider`.
6. Delete the three hint handshakes; add the missing drill-down entry points
   from F3.
7. `TourController` (E7/E8). Last, because it depends on the stable per-route
   page key existing.

Steps 1–2 are independently merge-able and each leaves the app working. Step 3
is the one that cannot be half-done.

---

## H. Open questions for review

1. **F2** — is "denied URL renders `AccessDeniedPage`" the behaviour we want, or
   should the silent redirect be preserved? This changes what an operator sees
   when their permissions are reduced while they have a tab open.
2. **F6** — does `/alerts/routing` redirect to `/integrations/smtp`, or does the
   integrations hub give SMTP config back to the alerts page?
3. Are `myRequests` (`/changes/my-requests`) and `ticketing`
   (`/changes/ticketing`) in the right group, given the brief's five-group nav
   spec names neither?
4. `mobileApps`, `clusterBuilder`, `serviceCatalog`, `components`, `clients` are
   candidates for the unlanded scope-reduction pass. The route table makes each
   a one-line deletion, but confirm before I write tours, breadcrumbs, and tests
   for pages that are about to be cut.
5. **F8** — confirm that adding `@testing-library/react` + `jsdom` to
   `frontend/package.json` is in scope. A2 is sole writer of that file, but this
   is the first new dependency beyond the router.
