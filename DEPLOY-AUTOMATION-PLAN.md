# Zoho → Jenkins Deploy Automation — Build Plan

**Status (2026-07-08): BUILT end-to-end** per this plan — models (`JenkinsConnection`,
`DeployAutomationRun`), `services/jenkins_client.py`, `services/deploy_automation_service.py`,
routes under `/api/zoho/jenkins` + `/api/zoho/automation/runs`, webhook auto-run + scheduler-tick
hooks, and the frontend Deploy-automation card (pipeline chips, Jenkins config modal, per-ticket
Run button, 10s polling). Covered by `backend/tests/test_deploy_automation.py` (6 tests: config
round-trip, validations, Jenkins-off failure, bundle path incl. faithful image-only YAML swap +
duplicate-run 409 + bundle-completion watcher, direct path, RBAC). Remaining to go live: Elie's
router pipeline (parameterized APP_NAME/NAMESPACE/IMAGE_TAG/TICKET, waits on child + propagates
result), then fill the Jenkins connection in the UI and Test. §1–§4 below kept as the reference
design.

---

## 1. Decisions (interview, 2026-07-07)

| Question | Decision |
|---|---|
| When does a run start? | **Auto-run toggle + manual Run button.** Config switch: ON → every resolved ticket with a tag starts automatically on webhook arrival; OFF → operators click "Run" on a ticket row. |
| Jenkins contract | **Single router pipeline** (built by Elie, in progress). KubeSight triggers it once and **polls the router build only** — the router must wait on the routed child job and propagate its result (fail if child fails). |
| Registry gating | **Build only if missing.** HEAD-check Nexus for the image tag first; already there → skip Jenkins entirely. Missing → trigger router, then **re-check Nexus after the build** (registry is the source of truth, a green build is not enough). |
| Handoff after image is ready | **Respect the cluster's approval config**: `cluster_required_approvals(cluster_id) > 0` → create a Change Bundle (normal quorum approval + window); `== 0` → apply the image change directly, immediately. |

### Jenkins router contract (KubeSight → Jenkins)

```
POST {jenkins}/job/{router_job_path}/buildWithParameters
  APP_NAME  = <deployment name>     e.g. aims-ui
  NAMESPACE = <namespace>           from ticket cf_environment
  IMAGE_TAG = <tag>                 from ticket cf_tag
  TICKET    = <ticket number>       traceability, e.g. DR-1042
```

- Auth: Jenkins user + **API token** via HTTP Basic (no CSRF crumb needed with token auth).
- KubeSight follows the `Location` header (queue item) → queue item's `executable` → build number/URL → polls `{build}/api/json` for `building`/`result`.
- **Router requirement:** it must block on the child job and mirror its result (`SUCCESS`/`FAILURE`).

---

## 2. Run state machine

```
queued → checking_image ──found──────────────────────────┐
              │ not_found                                 │
              ▼                                           ▼
          building → verifying_image ──found──→ [handoff decision]
              │            │ not_found                    │
              ▼            ▼                    approvals>0        approvals==0
            failed       failed                    │                    │
                                                   ▼                    ▼
                                          awaiting_approval          deployed
                                             (bundle)                (direct kubectl)
                                                   │
                                    bundle completed → deployed
                                    bundle rejected/expired/failed → failed
```

Terminal: `deployed`, `failed`, `cancelled`. Active: `queued`, `checking_image`, `building`,
`verifying_image`, `awaiting_approval`. All state persisted in DB — resumable across restarts;
advanced on the existing 15s scheduler tick.

Edge policies decided:
- Registry check returns `no_connection` (no linked registry owns that host) → proceed to build,
  **skip the post-build verify** (can't verify; note it in the step log), trust the router result.
- Registry `unreachable` → retry ~3 ticks, then fail.
- Multi-container deployments → use the **first container** (store `container_name`); note in step detail.
- Image repo derived from the **live deployment's current image**: `parse_image_reference(current)`
  → keep registry host + repository, swap in the requested tag. Zero config per app.
- One active run per ticket AND per (cluster, namespace, deployment) — 409 on conflict.
- Ticket without a tag or unresolved → not runnable (button disabled / auto-run skips).

---

## 3. New pieces to build

### Models (`backend/api/models.py` — new tables come free via `db.create_all()`, no migration needed)

**`JenkinsConnection`** (single row, id=1, mirrors ZohoIntegration pattern):
`enabled`, `base_url`, `username`, `api_token_encrypted` (Fernet, write-only in API),
`router_job_path` (e.g. `folder/router` → URL `/job/folder/job/router`), `verify_tls` (default true),
`auto_run_tickets` (default false), `build_timeout_minutes` (45), `queue_timeout_minutes` (10),
`bundle_window_hours` (24), `last_test_at/status/message`, `updated_at`.

**`DeployAutomationRun`**:
`id`, `ticket_record_id` (FK zoho_inbound_tickets.id), `ticket_number` (denorm),
`snapshot_id`, `cluster_id`, `namespace`, `deployment_name`, `container_name`,
`image_repo` (registry/repo, no tag), `image_tag`, `status` (indexed), `error`,
`steps` (JSON list `{key,status,detail,at}` for the UI pipeline chips: image_check → build →
verify → approval → deploy), `jenkins_queue_url`, `jenkins_build_url`, `jenkins_build_number`,
`build_triggered_at`, `bundle_id` (plain int, links ChangeBundle), `auto` (bool),
`triggered_by` (username string), `created_at/updated_at/finished_at`.

### `backend/api/services/jenkins_client.py` (stdlib urllib, pattern of zoho_client/registry_client)
`JenkinsConfig` dataclass; `JenkinsError`; `test_connection(cfg)` (GET router job api/json — proves
auth + job exists); `trigger_build(cfg, params) -> queue_url`; `queue_state(cfg, queue_url)`
(pending / cancelled / `{buildNumber, buildUrl}`); `build_state(cfg, build_url)`
(`{building, result}`). `verify_tls=False` → unverified ssl context.

### `backend/api/services/deploy_automation_service.py`
- Jenkins config CRUD + test (secrets write-only, Fernet like Zoho).
- `start_run(ticket_record_id, user=None, auto=False)` — validates + creates `queued` run.
- `advance_runs()` — scheduler hook; advances every active run one step per tick, each in
  try/except so one bad run can't stall the rest.
- `cancel_run(run_id)` — marks cancelled (can't abort the Jenkins build itself).
- Handoff implementation:
  - **Bundle path:** `get_or_create_draft(None)` (user=None always creates a FRESH draft — no
    collision) → `add_item(None, bundle.id, {actionType: "edit_deployment", clusterId, namespace,
    yaml})` where `yaml` = live deployment YAML (via `get_resource_yaml`) with ONLY the container
    image swapped (yaml safe_load → mutate → dump) → `submit_bundle(None, bundle.id,
    note="Zoho DR-1042 — aims-ui → v2.14.1 (automated)", window_start=now+2min,
    window_end=now+bundle_window_hours)`. Approvers get the normal quorum emails.
  - **Direct path:** `_run_kubectl_for_cluster(cluster_id, ["set", "image",
    f"deployment/{name}", f"{container}={repo}:{tag}", "-n", namespace])`.
- Audit: `log_audit(..., actor=None)` for auto runs (established system-actor pattern),
  actor=user for manual runs. Events: run started / build triggered / bundle created /
  deployed / failed.

### Routes (extend `backend/api/routes/zoho.py`)
- `GET /api/zoho/jenkins` (zoho:view) / `PUT /api/zoho/jenkins` (zoho:manage) /
  `POST /api/zoho/jenkins/test` (zoho:manage)
- `GET /api/zoho/automation/runs?limit=50` (zoho:view)
- `POST /api/zoho/automation/runs` body `{ticketRecordId}` (zoho:manage) — manual Run
- `POST /api/zoho/automation/runs/<id>/cancel` (zoho:manage)

### Hooks
- `zoho_sync_service.resolve_inbound`: after the record commits — if resolved + tag +
  `auto_run_tickets` → `start_run(record.id, auto=True)` wrapped in try/except (webhook must
  never fail because of automation).
- `alert_policy_scheduler._scheduler_loop`: add `deploy_automation_service.advance_runs()`
  next to the existing `zoho_sync_service.run_due_sync()` call (same try/except style).

### Frontend (Zoho Integration tab)
- New **"Deploy automation"** card (after Sync health): Jenkins status pill + auto-run pill,
  "Configure Jenkins" + "Test" buttons (canManage), and the runs list — each run rendered with
  the existing Signal `sg-pipe`/`sg-pstep` pipeline chips (Image check → Build → Verify →
  Approval → Deploy), status pill, error line, Cancel on active runs.
- **Run button** on each resolved-with-tag ticket row (next to the delete button).
- **Jenkins config modal**: enabled, base URL, username, API token (write-only), router job path,
  verify TLS, auto-run toggle, build timeout, approval-window hours.
- `zohoApi.js`: getJenkinsConfig / updateJenkinsConfig / testJenkins / listAutomationRuns /
  startAutomationRun / cancelAutomationRun.
- Poll runs every ~10s while any run is active.
- New icon: play (Run). Styles into `styles/signal/zoho.css`.

---

## 4. Load-bearing codebase facts (from the 2026-07-07 exploration — verified, with locations)

1. **Approval decision:** `deployment_request_service.cluster_required_approvals(cluster_id) -> int`
   (`:356`) — per-cluster override map `DeploymentRequestSetting.cluster_required_approvals`
   (JSON `{clusterId: int}`, `models.py:1098`), else global default. `> 0` = approval required.
   Cluster ids are the **public id strings** (e.g. `custom-6`), not integer PKs.
2. **⚠️ Do NOT use the `change_image` bundle item type.** `build_item_preview` routes it through
   `manifest_generator.generate_manifests`, which regenerates a whole Deployment+Service from
   defaults (replicas=1, port 8080…) — it would clobber the live spec. Use **`edit_deployment`**
   with the full live YAML (payload key `yaml`), which sanitizes via `sanitize_for_apply` and
   applies faithfully (`change_bundle_service.py:416-436`).
3. **`submit_bundle` requires `window_start > now`** (`:736`) — use now+2min for the automation.
   If computed required == 0 it auto-approves in place (`:758`) — we bypass bundles entirely in
   that case anyway (direct kubectl), so this only matters as a fallback.
4. **Bundles need no real user:** `requester_user_id` nullable; `get_or_create_draft(None)`
   creates a fresh draft each call; `add_item`/`submit_bundle` accept user=None (skips owner +
   namespace-access checks). System audit convention is `actor=None` (what the bundle executor uses).
5. **Registry check:** `registry_service.check_image(image) -> {status: found|not_found|
   unreachable|no_connection, ...}` (`:236`); `registry_client.parse_image_reference(image) ->
   ParsedImage(registry, repository, reference)` (`:63`). Block-enforcement can hard-fail bundle
   staging/execution if the tag is missing from a `block`-mode registry — automation order
   (verify in Nexus BEFORE creating the bundle) avoids this.
6. **Direct image change:** no existing helper — use
   `deployment_service._run_kubectl_for_cluster(cluster_id, args)` (`:127`; `set` is in the
   mutating-verb allowlist, invalidates read caches automatically).
7. **Live deployment YAML:** `resource_actions_service.get_resource_yaml(user, cluster_id,
   namespace, kind, name)` (`:250`) → `data["yaml"]`. Current images also readable via
   `kubectl get deployment -o json` → `spec.template.spec.containers[*].image`.
8. **Scheduler:** `alert_policy_scheduler._scheduler_loop` ticks every 15s (`:54`); Zoho
   `run_due_sync` hook at `:92-96` — add automation advance there.
9. **Executor:** `change_bundle_executor.process_due_bundles()` already runs approved bundles in
   their window — automation only needs to create+submit the bundle and then watch
   `ChangeBundle.status` (`completed`/`failed`/`partially_failed`/`rejected`/`expired`).
10. **Notifications:** pending bundles notify approvers by **email only** (no in-app bell section
    for bundles). Automation-created bundles get that for free via `submit_bundle`.

---

## 5. Build order (resume here tomorrow)

1. Models: `JenkinsConnection` + `DeployAutomationRun` (models.py, after the Zoho models).
2. `services/jenkins_client.py`.
3. `services/deploy_automation_service.py` (config CRUD, start_run, advance_runs, cancel, serialize).
4. Routes in `routes/zoho.py`.
5. Hooks: `resolve_inbound` auto-trigger + scheduler tick.
6. Frontend: zohoApi additions → automation card + runs list (sg-pstep chips) → Jenkins config
   modal → Run button on tickets → polling.
7. Verify: backend compile + tests, frontend build + tests, stub-server screenshots
   (scratchpad `zoho-stub.cjs` + `cdp-shot.mjs` pattern — stub the new endpoints:
   `/api/zoho/jenkins`, `/api/zoho/automation/runs`).

Open items to confirm with Elie when the router pipeline exists:
- Final router job path + Jenkins base URL + service account (goes in the config UI, not code).
- Whether the router enforces its own allow-list of child jobs (recommended in the design review).
- Zoho ticket comment-back (status updates into the ticket) stays **deferred** — current token
  scope has no ticket-write; needs `Desk.tickets.UPDATE` or similar when we get there.
