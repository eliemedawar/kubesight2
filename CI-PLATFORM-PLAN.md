# KubeSight Native CI — Architecture Analysis & Implementation Plan

Rebuild the Service Catalog around buildable applications, add a native CI engine
with pluggable runners, and hand successful artifacts to the existing CD path.
No Jenkins. No Hermes.

---

## 1. Current architecture relevant to this feature

### 1.1 Application shape

| Concern | How it works today | Files |
| --- | --- | --- |
| App factory | `create_app()` registers blueprints, runs migrations + seed, starts one background scheduler thread | `backend/api/__init__.py` |
| Serving | `gunicorn -w 1 --threads 8` — **one process**, so in-process singletons (scheduler, TTL caches, semaphores) are safe and singular | `backend/k8s_entrypoint.sh` |
| Migrations | `alembic/` exists but is **empty**. Schema = `db.create_all()` + hand-written idempotent migrators run at boot | `api/migrate_rbac.py:959` (`run_migrations`) |
| Models | Core in `api/models.py` (2846 lines); large features get their own module: `models_cluster_build.py`, `models_application_intelligence.py` | — |
| Services | `api/services/*.py`; large features get a package: `api/services/cluster_build/`, `api/services/ticketing/` | — |
| Routes | One blueprint per feature in `api/routes/`, registered in `api/routes/__init__.py` | — |
| Responses | `success_response(data)` / `error_response(msg, status)`, camelCase JSON keys | `api/response.py` |
| Kubernetes | **kubectl subprocess only** — no `kubernetes` python client in `requirements.txt`. TTL-cached reads, circuit breaker per cluster | `api/k8s_provider.py` |
| HTTP clients | `urllib` (no `requests` dependency) | `api/services/registry_client.py`, `application_intelligence_bitbucket.py` |
| Secrets at rest | Fernet, key from `ALERT_ROUTING_SECRET_KEY` or `JWT_SECRET_KEY`; columns named `*_cipher` / `*_encrypted` | `api/secret_encryption.py` |
| Background work | One 15s tick thread calls each feature's `advance_*()`; heavy work dispatched to daemon threads behind a semaphore | `api/services/alert_policy_scheduler.py`, `mobile_app_service.py:765` |
| RBAC | `permissions` table + `@require_permission("resource:action")`; keys are snake_case `resource:action` | `api/decorators.py`, `api/rbac_data.py` |
| Frontend nav | `NAV_PAGES` array with `permission` / `anyPermissions` + `section`; icons keyed by page id in `Sidebar.jsx`; page switch is a `case` in `App.jsx` | `frontend/src/utils/authz.js:68` |
| Frontend API | One `<feature>Api.js` per domain, all through `request()` | `frontend/src/api/client.js` |

### 1.2 The existing "Service Catalog"

It is **not** a build catalog. It is a *logical architecture* catalog:

- `ServiceBlueprint` — a reusable design: components, connections, requirements.
- `AppService` — one deployed instance of a blueprint, with `AppServiceComponentMapping`
  recording how each logical component maps to a real Kubernetes object.
- UI: `ServiceCatalogPage.jsx` (485 lines) + `BlueprintEditorModal` + `DeployFromBlueprintWizard`.
- Permissions: `service_blueprints:view|create|update|delete|deploy`, `app_services:*`.

There is **zero** source-repository, build, or artifact concept in it.

### 1.3 What already looks like CI in the codebase

Four separate systems already do pieces of CI, none of them reusable as-is:

1. **Application Intelligence** — the closest thing to a build system already here.
   `IntelligenceApplication` holds *name, slug, repo provider, repo URL, workspace,
   repo name, default branch, credential profile FK, subdirectory, Dockerfile path*.
   `ApplicationAnalysis` is a queued → running → completed job with a worker
   Kubernetes Job, a callback token, stage/progress tracking, and artifacts.
   `application_analysis_jobs.py` is a **hardened, production-grade ephemeral Job
   launcher**: locked-down Job + per-job NetworkPolicy + per-job Secret, ownerRef
   GC, restricted securityContext, TTL cleanup, resource limits, egress proxy.
2. **Cluster Builder** — `ClusterBuild` / `ClusterBuildNode` / `ClusterBuildStep`.
   A DB-persisted, restart-safe, resumable multi-phase state machine with per-step
   status/attempt/log_tail/error. This is the exact shape a CI build needs.
3. **Mobile Applications** — pulls APK/AAB/IPA artifacts *from Jenkins*, stores them
   on disk under `MOBILE_ARTIFACT_DIR`, then publishes to Play / App Store Connect.
   The publish half is valuable; the fetch half is a Jenkins dependency.
4. **Deploy Automation** — Zoho ticket → Jenkins router job → registry verify →
   Change Bundle or direct image apply. `DeployAutomationRun` is another DB state
   machine advanced by the scheduler tick.

### 1.4 The existing CD path (do not rewrite)

```
POST /api/inventory/deploy/image/generate   -> manifest_generator.generate_manifests()
POST /api/inventory/deploy/image/dry-run    -> deployment_service.dry_run_yaml()
POST /api/inventory/deploy/image/apply      -> deployment_service.apply_yaml()
                                               + registry_service.check_images() gate
                                               + AppCatalogEntry / ApplicationDeploymentVersion
```
Plus Change Bundles (approval + windowed execution + rollout watch + rollback) and
Helm. Permission: `apps:deploy`.

### 1.5 Jenkins footprint (what "no longer depend on Jenkins" actually costs)

| Consumer | Coupling | Retire in |
| --- | --- | --- |
| `deploy_automation_service.py` (2019 lines) | Triggers the router job, polls the build, then hands off to CD | Phase 7 |
| `mobile_app_service.py` (1717 lines) | Fetches build artifacts from a Jenkins job | Phase 6 |
| `resign_executor.py` + `k8s/jenkins/resign.Jenkinsfile` | Android/iOS re-signing runs as a Jenkins job | Phase 6 |
| `JenkinsConnection` model + settings UI + `jenkins_client.py` (500 lines) | Config surface | Phase 7 (delete last) |
| Root `Jenkinsfile` | Builds **KubeSight itself** — a build-time, not runtime, dependency | Optional dogfood, last |

---

## 2. What existing code can be reused

**Reuse directly, no changes:**

| Asset | Used for |
| --- | --- |
| `api/secret_encryption.py` | `ci_secrets`, runner tokens, registry creds |
| `api/decorators.py` `@require_permission` | Every CI route |
| `api/response.py`, `api/audit.py` | Responses + audit trail on run/cancel/retry |
| `RegistryConnection` + `registry_service.py` + `registry_client.py` | Nexus connection, push target, digest verification, availability gate |
| `api/services/deployment_service.py`, `manifest_generator.py` | The CD handoff — CI calls it, never replaces it |
| `api/services/ssh/transport.py` (paramiko, bastion, sudo, host keys) | Optional SSH-driven Linux runner without writing an agent |
| `api/services/email_delivery.py`, `alert_notifier.py` | Build failure notifications (Phase 7) |
| `frontend/src/api/client.js`, `components/common/*` | Every new UI surface |

**Reuse as a template (copy the pattern, new module):**

| Asset | Becomes |
| --- | --- |
| `application_analysis_jobs.py` | `services/ci/runners/kubernetes_job.py` — the Job+NetworkPolicy+Secret+ownerRef+TTL recipe is exactly right |
| `k8s/application-analysis-worker.yaml` | `k8s/ci-runner.yaml` — namespace w/ restricted PSS, SA, ResourceQuota, LimitRange, launcher Role/RoleBinding |
| `ClusterBuild`/`ClusterBuildStep` + `cluster_build/executor.py` | `CiBuild`/`CiBuildStage` + `services/ci/engine.py` — restart-safe resumable stage machine |
| `mobile_app_service._try_dispatch` + `advance_mobile_builds` | `services/ci/engine.advance_ci_builds` — semaphore-bounded worker threads off the tick |
| `ApiToken` hashing (`sha256`, `token_prefix`, `token_hash`) | Runner registration tokens |
| `ApplicationAnalysis.worker_callback_token_hash` + `/api/application-analysis-worker/*` | Per-build job callback auth |
| `mobile_app_service.artifact_root()` / `_store_dir_for()` | Local artifact storage backend |

**Extend rather than duplicate:**

- `BitbucketCredentialProfile` — already "credentials separate from service config",
  already supports `oauth` / `api_token` / `repository_access_token`. **Add a
  `provider` column** (default `bitbucket`) and it becomes the source-connection
  credential store for GitLab/GitHub later. Do not create a second credential table.
- `application_intelligence_bitbucket.py` — read-only branch/tag/commit/tree listing
  already exists. Generalize into `services/ci/source/bitbucket.py` with a provider
  port; Application Intelligence keeps calling it.
- `application_deployment_versions` — add nullable `ci_build_id` / `ci_artifact_id`
  to close the Git commit → build → artifact → deployment traceability chain.

---

## 3. What to replace in the current Service Catalog

**Do not delete the blueprint domain.** `ServiceBlueprint` / `AppService` /
`AppServiceComponentMapping` are wired into App Services, Clients, and the deploy
wizard. Deleting them rewrites CD, which is explicitly out of scope.

| Item | Action |
| --- | --- |
| `ServiceCatalogPage.jsx` | **Rebuilt** as the CI catalog (service cards with repo/branch/pipeline/last build/last artifact) |
| `BlueprintEditorModal.jsx`, `DeployFromBlueprintWizard.jsx` | **Moved**, not deleted — to a new `BlueprintsPage.jsx` under *Services* |
| `NAV_PAGES` entry `serviceCatalog` | Permission changes `service_blueprints:view` → `ci_services:view`; new `blueprints` entry keeps `service_blueprints:view` |
| `service_blueprints:*` / `app_services:*` permissions | **Unchanged** — they now gate the Blueprints page |
| `ServiceBlueprint` / `AppService` tables + routes | **Unchanged** |
| Optional link | `ci_services.blueprint_id` nullable FK — a CI service may declare which blueprint describes its architecture, so Deploy-from-artifact can prefill the blueprint wizard |

**The duplication risk you must decide on:** `IntelligenceApplication` already stores
name + slug + Bitbucket repo + default branch + subdirectory + Dockerfile path +
credential profile. `ci_services` will store the same fields. That is a third place
to register "an app with a Bitbucket repo" (after Inventory's `AppCatalogEntry`).

Recommendation: ship `ci_services` as its own table in Phase 1 (Application
Intelligence is freshly hardened and Hermes-coupled; CI must not be), add a nullable
`intelligence_applications.ci_service_id` plus cross-links in both UIs ("Analyze
source" / "Set up CI"), and revisit a real merge in Phase 7 once CI is proven.
Merging on day one would put a Hermes dependency in CI's critical path.

---

## 4. Proposed backend architecture

```
api/models_ci.py                 all CI tables
api/routes/ci.py                 /api/ci/*            (user-facing, @require_permission)
api/routes/ci_agent.py           /api/ci/agent/*      (runner agents, bearer runner token)
api/routes/ci_worker.py          /api/ci/worker/*     (in-cluster job callbacks, per-build token)

api/services/ci/
    __init__.py
    catalog.py        ci_services CRUD + serialization
    source/
        __init__.py   provider port: list_branches / list_tags / resolve_commit /
                      clone_spec / verify_access
        bitbucket.py  wraps the existing read-only client + clone credential shaping
    pipelines.py      ci_pipelines + ci_pipeline_stages CRUD, validation, templates
    templates.py      per-application-type starter pipelines (java/node/python/...)
    engine.py         THE STATE MACHINE: enqueue / advance_ci_builds / cancel / retry
    queue.py          claim_next_build() port — Postgres today, Redis/RabbitMQ later
    scheduler.py      runner selection: requirements -> compatible, healthy, free runner
    runners/
        base.py       RunnerAdapter protocol + StageRequirements/StageExecution/Handle
        kubernetes.py Kubernetes Job runner (default)
        agent.py      pull-based agent runner (Linux + macOS, same adapter)
        mock.py       mock-mode runner for demos/tests
    buildkit.py       container-image stage: buildctl invocation, digest capture
    artifacts.py      artifact records + storage backend port (local | nexus-raw | s3)
    logs.py           chunked log ingest, secret masking, offset reads
    secrets.py        ci_secrets CRUD + resolution into a runner-safe payload
    handoff.py        artifact -> existing CD (image ref, prefilled deploy request)
    serializers.py
```

### 4.1 The build state machine

`engine.advance_ci_builds()` is added to the existing 15s scheduler tick. One pass:

1. **Reap** — `running` builds whose runner heartbeat is dead, whose Job vanished,
   or that passed their deadline → `FAILED` / `TIMEOUT`.
2. **Cancel** — builds with `cancel_requested` → `adapter.cancel()` → `CANCELLED`.
3. **Advance** — for each `running` build, poll the current stage:
   `succeeded` → next stage (or `SUCCESS` if last); `failed` → `FAILED` unless the
   stage is `continue_on_failure`.
4. **Dispatch** — claim `QUEUED` builds FIFO, respecting per-service and global
   concurrency limits; ask `scheduler.select_runner()` for the first stage's
   requirements; if a runner is free → `RUNNING` and start the stage. If none is
   free the build stays queued (with a `queue_reason` surfaced in the UI).

Every transition is a committed DB write *before* any thread is dispatched, so a
backend restart resumes rather than restarts — the `cluster_build/executor.py`
contract. Log pumping and artifact upload run on semaphore-bounded daemon threads,
never on the tick thread.

**The Flask container never runs a build command.** It only writes DB rows, runs
`kubectl apply/get/logs/delete`, and answers agent HTTP calls.

### 4.2 Queue abstraction

The queue is `ci_builds WHERE status='queued' ORDER BY queued_at` — no separate
table. All access goes through `queue.py`:

```python
def enqueue(build_id: int) -> None
def claim_next(limit: int) -> list[int]     # SELECT ... FOR UPDATE SKIP LOCKED on PG
def requeue(build_id: int) -> None
def depth(service_id: int | None = None) -> int
```

Postgres `SKIP LOCKED` makes it correct even if the deployment ever moves past
`-w 1`; SQLite degrades to a plain ordered query, which is safe under one worker.
Swapping in Redis/RabbitMQ later means reimplementing four functions.

---

## 5. Proposed frontend structure

```
pages/ServiceCatalogPage.jsx     rebuilt — CI service grid + filters + New service
pages/ServiceDetailPage.jsx      tabbed service page
pages/BlueprintsPage.jsx         the old catalog, moved intact
pages/CiRunnersPage.jsx          runner fleet (Operations section)

components/catalog/
    ServiceCard.jsx              name, type icon, repo, branch, pipeline state,
                                 last build (status + duration + number), last artifact
    ServiceFormModal.jsx         create/edit: name, description, owner team,
                                 criticality, application type
    tabs/OverviewTab.jsx         identity, health strip, recent builds, quick actions
    tabs/SourceTab.jsx           provider, credential profile picker, repo, branch
                                 picker (live from Bitbucket), working directory
    tabs/PipelineTab.jsx         stage strip + ordered stage list + stage editor drawer
    tabs/BuildsTab.jsx           build table + Run Build
    tabs/ArtifactsTab.jsx        artifact table + Deploy / Download
    tabs/SettingsTab.jsx         concurrency, timeouts, secrets, danger zone
    PipelineStrip.jsx            Checkout -> Build -> Test -> Scan -> Image -> Publish
    PipelineStageEditor.jsx      image, workdir, commands, env, secret refs, timeout,
                                 runner labels, continue-on-failure
    BuildDetailDrawer.jsx        header + per-stage rows, click a stage -> logs
    StageLogViewer.jsx           offset polling + follow toggle + download
    DeployArtifactButton.jsx     hands the exact image ref to the existing deploy flow

components/ci/RunnerCard.jsx, RunnerRegisterModal.jsx
api/ciApi.js
```

Reuses the Signal design system as-is (`sg-*` classes, `status-pill`, `sg-card-grid`),
`LoadingState` / `EmptyState` / `ErrorBanner`, and the existing log-viewer patterns
from `LogsPage.jsx`.

**Log transport:** offset polling (`GET /api/ci/builds/:id/stages/:sid/logs?after=<seq>`)
as the default, matching how the rest of the app reads. SSE follow is a later
optional add — every SSE viewer pins one of the 8 gunicorn threads, and pod-log
streaming already competes for those.

---

## 6. Database changes

New module `api/models_ci.py`. New tables created by `db.create_all()`; a
`_migrate_ci_columns()` added to `run_migrations()` for later column additions,
per the existing convention.

```
ci_services
  id, name, slug(unique), description, owner_team, criticality,
  application_type            container|java|node|python|android|ios|flutter|generic
  repository_provider         bitbucket (gitlab|github later)
  repository_url, repository_workspace, repository_name
  default_branch, working_directory
  credential_profile_id       -> bitbucket_credential_profiles.id
  registry_connection_id      -> registry_connections.id   (nullable, image target)
  blueprint_id                -> service_blueprints.id     (nullable, optional link)
  status                      active|paused|archived
  max_concurrent_builds, next_build_number
  created_by_user_id, created_at, updated_at

ci_pipelines
  id, service_id, name, is_default, enabled, version, description,
  created_by_user_id, created_at, updated_at
  UNIQUE(service_id, name)

ci_pipeline_stages
  id, pipeline_id, position, name,
  stage_type                  checkout|command|container_image|publish_artifact|scan
  runner_selector_json        {"type":"kubernetes","labels":["linux","java21"]}
  image, working_directory, commands_json, env_json, secret_refs_json,
  resources_json              {"cpu":"2","memory":"4Gi","ephemeralStorage":"10Gi"}
  artifacts_json              [{"path":"target/*.jar","type":"jar"}]
  timeout_seconds, continue_on_failure,
  parallel_group              nullable — reserved, sequential in v1
  created_at

ci_builds
  id, service_id, pipeline_id, number,
  status                      queued|running|success|failed|cancelled|timeout
  trigger_type                manual|webhook|api|automation
  branch, commit_sha, commit_message, requested_by_user_id,
  pipeline_snapshot_json      the pipeline as it was at trigger time
  runner_id, workspace_ref, queue_reason, cancel_requested,
  queued_at, started_at, finished_at, duration_seconds, error,
  worker_callback_token_hash
  UNIQUE(service_id, number); INDEX(status, queued_at); INDEX(service_id, id DESC)

ci_build_stages
  id, build_id, pipeline_stage_id(nullable), position, name,
  status                      pending|running|success|failed|skipped|cancelled|timeout
  attempt, runner_id, exit_code, started_at, finished_at,
  log_line_count, error
  INDEX(build_id, position)

ci_log_chunks
  id, build_stage_id, seq, stream(stdout|stderr|system), content, created_at
  INDEX(build_stage_id, seq)
  (offloaded to the artifact store past CI_LOG_INLINE_MAX_BYTES)

ci_runners
  id, name(unique), runner_type   kubernetes|agent_linux|agent_macos|ssh_linux|mock
  status                      online|offline|draining|disabled
  hostname, os, os_version, arch,
  labels_json, capabilities_json  ["linux","java21","node","android","macos","xcode"]
  max_concurrent, current_load, version,
  token_prefix, token_hash, enabled,
  last_heartbeat_at, last_error, metadata_json, created_at, updated_at

ci_artifacts
  id, service_id, build_id, build_stage_id,
  artifact_type               container-image|jar|war|zip|binary|apk|aab|ipa|
                              test-report|coverage-report|sbom
  name, version, uri, digest, size_bytes, checksum_sha256,
  storage_backend             registry|local|s3|nexus_raw
  storage_ref, registry_connection_id, commit_sha, branch,
  metadata_json, created_at
  INDEX(service_id, created_at DESC); INDEX(build_id)

ci_secrets
  id, scope(global|service), service_id(nullable), key, value_cipher,
  description, created_by_user_id, last_used_at, created_at, updated_at
  UNIQUE(scope, service_id, key)
```

**Deliberately not created:**
- `ci_build_queue` — the queue is a status + index on `ci_builds`.
- `ci_runner_capabilities` — capabilities are a JSON array, matched in Python.
  A join table buys indexed matching that a fleet of tens of runners does not need,
  and JSON columns are the established pattern here (`addons_json`, `labels`).
- `registry_connections` — **already exists**, reused unchanged.
- `source_connections` — the credential profile *is* the connection; per-service
  repo fields live on `ci_services`, mirroring the proven `IntelligenceApplication`.

**Altered tables:**
```
bitbucket_credential_profiles  + provider VARCHAR(32) DEFAULT 'bitbucket'
intelligence_applications      + ci_service_id INTEGER NULL
application_deployment_versions + ci_build_id INTEGER NULL, ci_artifact_id INTEGER NULL
mobile_applications            + ci_service_id INTEGER NULL          (Phase 6)
deploy_automation_runs         + ci_build_id INTEGER NULL            (Phase 7)
```

**New permissions** (project convention, not the dotted form):
```
ci_services:view    ci_services:create   ci_services:update   ci_services:delete
ci_pipelines:view   ci_pipelines:edit
ci_builds:run       ci_builds:cancel     ci_builds:retry
ci_logs:view        ci_artifacts:view    ci_artifacts:download
ci_runners:view     ci_runners:manage    ci_secrets:manage
```
Registered in `PERMISSIONS`, grouped under a new `{"id": "ci", "label": "CI / Service Catalog"}`
entry in `PERMISSION_GROUPS`, and added to `DEFAULT_ROLE_PERMISSIONS`. Deploying an
artifact still requires the existing `apps:deploy` — CI never gets its own deploy right.

---

## 7. Runner architecture

### 7.1 The port

```python
@dataclass(frozen=True)
class StageRequirements:
    runner_type: str | None          # "kubernetes" | "agent_linux" | "agent_macos" | None
    labels: tuple[str, ...]          # ("linux", "java21")
    image: str | None
    resources: dict

class RunnerAdapter(Protocol):
    def can_run(self, req: StageRequirements) -> bool: ...
    def start(self, ex: StageExecution) -> RunnerHandle: ...
    def poll(self, h: RunnerHandle) -> StageStatus: ...          # queued|running|succeeded|failed|timeout
    def drain_logs(self, h: RunnerHandle, after_seq: int) -> Iterator[LogChunk]: ...
    def collect_artifacts(self, h: RunnerHandle, spec) -> list[ArtifactRef]: ...
    def cancel(self, h: RunnerHandle) -> None: ...
    def cleanup(self, h: RunnerHandle) -> None: ...
```

The engine only ever talks to this. Kubernetes is one implementation among several
— that is what keeps the engine from being Kubernetes-coupled.

For **push** runners (Kubernetes) the adapter actively drives `kubectl`. For **pull**
runners (agents) the same methods read and write DB rows that the agent's outbound
HTTP calls populate. The engine cannot tell the difference.

### 7.2 Scheduler

```
select_runner(req) ->
    candidates = enabled AND status=online AND heartbeat fresh
                 AND (req.runner_type is None or matches)
                 AND req.labels ⊆ runner.capabilities
                 AND current_load < max_concurrent
    order by (current_load / max_concurrent) asc, last_assigned_at asc
    -> first, or None (build stays QUEUED with queue_reason)
```

Examples: an iOS stage declares `labels: ["macos","xcode"]` and can only land on a
Mac agent; a Java stage declares `type: kubernetes` and lands on the Job runner; an
Android stage declares `labels: ["android"]` and can land on either, depending on
which runners are registered.

### 7.3 Agent protocol (Linux + macOS, one binary)

**Pull-based, outbound-only.** A Mac in an office or a Linux box behind NAT can reach
KubeSight; KubeSight cannot reach them. Inbound-connection designs die on the first
firewall.

```
POST /api/ci/agent/register   Bearer <bootstrap token>  -> {runnerId, runnerToken}
POST /api/ci/agent/heartbeat  Bearer <runner token>
     {status, currentLoad, capabilities, version}       -> {cancelJobIds: [...]}
POST /api/ci/agent/lease                                -> 200 job payload | 204 none
POST /api/ci/agent/jobs/<id>/logs   {seq, stream, lines}
POST /api/ci/agent/jobs/<id>/status {status, exitCode, error}
POST /api/ci/agent/jobs/<id>/artifacts  multipart
```

Runner tokens are hashed with sha256 exactly like `ApiToken.token_hash` and scoped
to `/api/ci/agent/*` only. This is runner identity, not user identity — it is not a
second auth system, it reuses the same hashing and the same `@require_permission`
model for everything a human touches.

The lease response carries the resolved secret values for that job over HTTPS; the
agent holds them in memory, injects them as env vars, and never writes them to disk.

**Linux agent isolation:** the agent runs each job inside a container
(`docker run --rm` / `podman run --rm`) using the stage image, with the workspace
bind-mounted. Running commands directly on the host is a configuration flag, off by
default.

**macOS agent:** no container runtime — Xcode needs the host. Isolation is a
per-build temp workspace, a per-build temporary keychain that is deleted on exit,
and a dedicated non-admin user account. Documented as weaker isolation; that is
inherent to iOS builds, not a KubeSight choice.

**Alternative already available:** `api/services/ssh/transport.py` (paramiko,
bastion, host-key policy, sudo) can drive a Linux runner over SSH with no agent to
install. Worth shipping as `ssh_linux` alongside the agent — it costs one adapter
and removes the "install our binary" hurdle for the first customer.

### 7.4 Kubernetes Job runner — workspace decision

The spec says one Job per stage. That conflicts with workspace continuity: checkout
writes the source, build reads it. Two workable shapes:

| | A. One Job per build, stages as ordered initContainers + final container, shared `emptyDir` | B. One Job per stage + per-build workspace PVC |
| --- | --- | --- |
| Workspace | Free, no storage prerequisite | Needs a StorageClass |
| Per-stage image | Yes — each initContainer has its own | Yes |
| Per-stage logs | Yes — `kubectl logs -c <stage>` | Yes |
| Per-stage retry | No — retry re-runs the pod | Yes |
| Per-stage resources | Declared per container, but the pod reserves the sum | True per stage |
| `continue_on_failure` | Only via `sh -c 'cmd \|\| true'` | Native |
| Node pinning | Not needed | RWO forces every later Job onto the PVC's node |
| Proven here? | **Yes** — `application_analysis_jobs.py` ships exactly this | No |

**Recommendation:** ship **A** in Phase 3 and add **B** in Phase 4 behind
`CI_WORKSPACE_MODE=single_job|pvc`. A gets working Java/Node/Python builds with no
storage prerequisite, reusing a hardened launcher that already exists in this repo.
B is what you want once per-stage retry and heterogeneous resource profiles matter,
and it needs the node-pinning workaround for RWO volumes (record `pod.spec.nodeName`
from the first Job, pin the rest) or an RWX StorageClass.

Either way the `RunnerAdapter` boundary hides which one is active.

**Per-build resources created** (all garbage-collected via ownerRef to the Job, the
`application_analysis_jobs.launch()` trick):
```
Secret        ci-<buildId>-<rand>-secrets     resolved ci_secrets + git credential
Secret        ci-<buildId>-<rand>-registry    docker config.json for Nexus
NetworkPolicy ci-<buildId>-<rand>             DNS + KubeSight callback + registry/proxy only
Job           ci-<serviceSlug>-<number>       restricted securityContext, TTL, deadline
```
Namespace `kubesight-ci` with `pod-security.kubernetes.io/enforce: restricted`,
ResourceQuota and LimitRange — `k8s/application-analysis-worker.yaml` cloned.

---

## 8. BuildKit architecture

**Shape: a shared rootless `buildkitd` in the cluster; build Jobs are thin clients.**

```
[ ci_build_stage: stage_type=container_image ]
        |
        v
Kubernetes Job  image: moby/buildkit:<ver>-rootless  (client only, no privileges)
  command: buildctl --addr tcp://buildkitd.kubesight-ci.svc.cluster.local:1234
             --tlscacert/--tlscert/--tlskey  (mTLS)
             build
             --frontend dockerfile.v0
             --local context=/workspace/<workingDir>
             --local dockerfile=/workspace/<dockerfileDir>
             --opt filename=<Dockerfile>
             --opt build-arg:<...>
             --output type=image,name=<registry>/<repo>:<tag>,push=true
             --metadata-file /workspace/buildkit-meta.json
        |
        v
buildkitd  Deployment (replicas configurable), rootless, its own ServiceAccount,
           mTLS-only listener, no host mounts, no docker socket
        |
        v
Nexus  (auth from RegistryConnection -> per-build Secret -> ~/.docker/config.json)
        |
        v
ci_artifacts  type=container-image, uri, digest (from containerimage.digest),
              registry, repository, tag, build_id, commit_sha
```

Why not the alternatives:
- **DinD** — requires `privileged: true`. Rejected.
- **Host docker socket mount** — root-equivalent on the node. Rejected.
- **Kaniko** — archived upstream. Rejected.
- **`buildctl-daemonless.sh` inside each build Job** — no shared daemon to run, but
  it needs `seccompProfile: Unconfined` and an unconfined AppArmor profile on
  *every build pod*, which the `restricted` PSS profile on the CI namespace forbids.
  Rejected as the default; usable as a documented fallback in clusters without a
  buildkitd.

**The one security concession, stated plainly:** rootless `buildkitd` still needs
`seccompProfile: Unconfined` (and usually `/dev/fuse` or a userns-enabled kernel).
That relaxation applies to **one deployment in its own namespace** — not to build
Jobs, which stay `restricted`. If the cluster cannot permit that, the fallback is a
Linux runner agent on a dedicated VM running rootless buildkit there.

Also handled: `--export-cache`/`--import-cache` against a Nexus `:buildcache` tag
(Phase 7), and `RegistryConnection.ca_cert` + `verify_tls` honored for self-signed
Nexus instances.

---

## 9. Security considerations

| Risk | Control |
| --- | --- |
| Build code is untrusted and runs arbitrary commands | Dedicated `kubesight-ci` namespace, `restricted` PSS, non-root, `readOnlyRootFilesystem`, `allowPrivilegeEscalation: false`, all capabilities dropped, `automountServiceAccountToken: false`, ResourceQuota + LimitRange |
| Build pod reaching the cluster or the internet | Per-build NetworkPolicy: egress to DNS, the KubeSight callback port, the registry, and the configured proxy CIDR — nothing else. Cloned from `application_analysis_jobs.py` |
| Secrets in logs | Masked **at ingest**, in `logs.py`, before any chunk is persisted — built from the secret values in scope for that build plus registry/git credentials. Never delegated to the runner |
| Secrets in pipeline definitions | `commands` never contain secret values; stages carry `secret_refs` only. Secrets reach a runner as env vars, materialized into a per-build K8s Secret with an ownerRef so Kubernetes GCs them with the Job |
| Secrets at rest | Fernet via the existing `secret_encryption.py`; `value_cipher` never serialized to any API response |
| Registry credentials | Live only in `RegistryConnection`; a service references the connection by id. The docker config Secret is per-build and GC'd |
| Git credentials | Existing `BitbucketCredentialProfile`; the clone runs in the checkout stage with the token injected as an env var, never in a command string or a remote URL |
| Runner impersonation | Per-runner sha256-hashed token, `/api/ci/agent/*` scope only, revocable, heartbeat-gated. A runner can only lease jobs the scheduler assigned to it |
| Runner exfiltration | A registered runner sees the source and secrets of jobs it runs — same trust model as any CI. Mitigations: runner tokens are admin-issued (`ci_runners:manage`), label-based routing keeps prod secrets off shared runners, and every registration/assignment is audited |
| Job callback forgery | Per-build callback token, hashed on the row — the `ApplicationAnalysis.worker_callback_token_hash` pattern |
| Privilege escalation via CI → CD | CI never deploys. The Deploy button calls the existing CD endpoints, which enforce `apps:deploy` + cluster/namespace access on the *human* clicking it |
| Artifact tampering | sha256 recorded for file artifacts; image digest recorded from BuildKit's metadata and re-verifiable against Nexus via `registry_client.check_manifest` |
| Log/artifact disk growth | Retention: `CI_BUILD_RETENTION_DAYS`, `CI_LOG_INLINE_MAX_BYTES` (offload past it), keep-last-N artifacts per service, pruned on the scheduler tick |
| DoS via queue flooding | Per-service `max_concurrent_builds` + a global cap + queue depth limit |

---

## 10. Phased implementation plan

Each phase is independently shippable and leaves the app working.

### Phase 1 — Service Catalog for CI *(foundation)*
- `api/models_ci.py`: `ci_services`, `ci_pipelines`, `ci_pipeline_stages`,
  `ci_builds`, `ci_build_stages`, `ci_artifacts`, `ci_secrets`, `ci_runners`,
  `ci_log_chunks`. `bitbucket_credential_profiles.provider` column.
- Permissions + `PERMISSION_GROUPS` + `DEFAULT_ROLE_PERMISSIONS`.
- `services/ci/catalog.py`, `pipelines.py`, `templates.py`, `source/bitbucket.py`.
- `routes/ci.py`: services, pipelines, builds (read-only), artifacts (read-only).
- Frontend: rebuilt `ServiceCatalogPage`, `ServiceDetailPage` with all six tabs,
  `BlueprintsPage` (old catalog moved), `ciApi.js`, nav + tour updates.
- Starter pipeline templates per application type.
- **Done when:** a user registers a service, connects a Bitbucket repo, picks a
  branch from a live dropdown, and edits a pipeline. No builds run yet.

### Phase 2 — CI engine *(no real execution)*
- `engine.py`, `queue.py`, `scheduler.py`, `runners/base.py`, `runners/mock.py`,
  `logs.py`, `secrets.py`.
- `advance_ci_builds()` wired into the scheduler tick.
- Run / cancel / retry endpoints + audit entries.
- Build list, build detail, per-stage logs in the UI.
- **Done when:** Run Build executes a full pipeline on the mock runner, streams
  synthetic logs, respects cancel/retry, and survives a backend restart mid-build.
  Mock mode also makes the feature demoable without a cluster.

### Phase 3 — Kubernetes Job runner
- `runners/kubernetes.py` (single-Job / initContainer mode), `k8s/ci-runner.yaml`.
- Log pump (`kubectl logs -c <stage> --follow` → masked chunks), artifact collection,
  timeouts, cleanup, orphan reaping.
- **Done when:** real `mvn clean package`, `npm ci && npm test && npm run build`,
  and `pip install && pytest` builds pass, with logs and JUnit/coverage artifacts.

### Phase 4 — BuildKit + Nexus
- `buildkit.py`, buildkitd manifests + mTLS bootstrap, `CI_WORKSPACE_MODE=pvc` and
  the per-stage-Job runner, container-image artifact records with digests.
- Deploy button on a container-image artifact → existing CD, with
  `ci_build_id`/`ci_artifact_id` recorded on `application_deployment_versions`.
- **Done when:** commit → image in Nexus → one click deploys that exact digest, and
  the Inventory version history shows which build produced it.

### Phase 5 — External runner agent
- `routes/ci_agent.py`, `runners/agent.py`, runner registration UI + `CiRunnersPage`.
- The agent itself (single Python binary, Docker/Podman job isolation).
- `runners/ssh_linux` reusing `services/ssh/transport.py`.
- **Done when:** a Linux box registers, heartbeats, leases a job, and reports back.

### Phase 6 — Mobile *(Jenkins retirement, part 1)*
- macOS agent build (Xcode/fastlane capabilities, temp keychain).
- Android and iOS pipeline templates; APK/AAB/IPA artifact types.
- Signing as pipeline stages, replacing `resign_executor.py` and `resign.Jenkinsfile`.
- `mobile_applications.ci_service_id`; `MobileAppBuild` rows created from
  `ci_artifacts` instead of fetched from Jenkins. **Play/App Store publishing is
  untouched** — it keeps working off the same stored binary.
- **Done when:** Mobile Applications no longer calls Jenkins.

### Phase 7 — Automation *(Jenkins retirement, part 2)*
- Bitbucket webhooks: push / PR / merge / tag → trigger rules per service.
- `deploy_automation_service` triggers a `ci_build` instead of a Jenkins router job;
  `deploy_automation_runs.ci_build_id`.
- Approvals, notifications, build caching, concurrency controls, retention pruning.
- Delete `jenkins_client.py`, `JenkinsConnection`, the Jenkins settings panel,
  `k8s/jenkins/`.
- Optional: revisit the `IntelligenceApplication` ↔ `ci_services` merge.
- **Done when:** `grep -ri jenkins backend/api frontend/src` is empty.

---

## Open decisions for you

1. **Application Intelligence overlap** — separate `ci_services` now with a
   cross-link (recommended), or fold `IntelligenceApplication` into `ci_services`
   in Phase 1 and accept the churn on a freshly hardened feature?
2. **Kubernetes workspace mode** — ship the proven single-Job/initContainer shape
   first (recommended), or go straight to per-stage Jobs + PVC and take the
   StorageClass + node-pinning dependency in Phase 3?
3. **buildkitd** — is `seccompProfile: Unconfined` on one deployment in its own
   namespace acceptable in your clusters? If not, container image builds must run on
   a dedicated Linux runner VM instead.
4. **The old blueprint catalog** — move to a "Blueprints" page (recommended), or
   nest it as a tab inside the new Service Catalog?
5. **Root `Jenkinsfile`** — KubeSight builds itself with Jenkins today. In scope for
   removal (dogfood KubeSight's own CI), or out of scope?
