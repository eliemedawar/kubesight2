# Hermes-assisted CI pipeline generation — design review

**Status:** BUILT (2026-09-15). Phases 0-2 are implemented, tested and verified
end-to-end against a live backend. Phase 3 (the remaining stacks) is prompt and
evidence breadth on top of the same machinery; Phase 4 is optional polish.

The design below is what was agreed. Where the build departed from it, the
departure is recorded in **Built** notes rather than by editing the original
reasoning — the argument is worth keeping next to the outcome.

**Principle being served:** the user gives KubeSight a repository; Hermes proposes the
application profile and the pipeline; KubeSight remains the authority over validation,
security, storage, scheduling and execution. The user can always configure manually,
and no build ever depends on an LLM call.

---

## 1. Current Register Service flow

Register is identity-only today, and source/pipeline are separate tabs.

```
ServiceCatalogPage "Register a service"
  → ServiceFormModal              name, slug, description, applicationType, criticality, ownerTeam
  → POST /api/ci/services         routes/ci.py:105
  → catalog.create_service        services/ci/catalog.py:453
        _apply_identity()                        validates + assigns slug
        row.dockerfile = templates.dockerfile_for(applicationType)    starter Dockerfile
        apply_source() only if payload carried repositoryUrl          (the UI never does)
        pipelines.create_pipeline(name="default", stages=[])          empty identity row
  → frontend jumps to ServiceDetailPage tab="source"
  → SourcePanel  → PUT /api/ci/services/:id/source  → catalog.apply_source
  → PipelineEditor tab
```

`PipelineEditor` loads `listCiPipelines`. Because the default pipeline has **zero saved
stages**, `pipelines.list_pipelines` substitutes `_generated_pipeline_dict` — an *unsaved*,
repository-aware default produced by `default_pipelines.for_service()`. The editor shows it
read-only under "Using KubeSight default pipeline" until the user clicks Customize.

Two facts that shape everything below:

* **Application type is already asked up front and is immutable after creation**
  (`ServiceFormModal` sets `disabled={isEdit}`; `_apply_identity` only writes it when
  `creating` or when the key is explicitly present). The redesign changes this.
* **A repository is already inspected today, without a clone.**
  `default_pipelines.inspect_repository` (`services/ci/default_pipelines.py:65`) reads a
  handful of marker files over the Bitbucket REST API through the CI source port. That is the
  seed of the Hermes evidence collector, not something to build from scratch.

## 2. Current Service Catalog models

`backend/api/models_ci.py`.

| Model | Purpose | Notes that matter here |
|---|---|---|
| `CiService` (:95) | what we build | `application_type` (single combined string), nullable source block (`repository_*`, `default_branch`, `working_directory`, `credential_profile_id`), inline `dockerfile` TEXT, nullable links to registry/blueprint/intelligence app/catalog entry, `max_concurrent_builds`, `next_build_number` |
| `CiPipeline` (:203) | how we build it | `parameters` JSON (build inputs), `version` bumped per save, `is_default` |
| `CiPipelineStage` (:248) | one stage | see §4 |
| `CiBuild` / `CiBuildStage` / `CiLogChunk` | one execution | `pipeline_snapshot` JSON — builds render and retry from the snapshot, never the live pipeline |
| `CiRunner` | where we build | `capabilities` JSON list, matched in Python |
| `CiAgentTask` | pull-runner claim ticket | stores **no** payload; secrets are rebuilt at claim time |
| `CiArtifact` | what came out | |
| `CiSecret` | encrypted, write-only | `scope` service\|global, service shadows global |

Credentials live in `BitbucketCredentialProfile`
(`models_application_intelligence.py`) — **shared** by CI and Application Intelligence.
That shared row is the single most important reuse point in this whole design.

## 3. Existing application type model

`APPLICATION_TYPES` (`models_ci.py:30`) is a 9-value tuple of *combined* strings:
`container, java_maven, java_gradle, java (legacy), node, python, android, ios, flutter, generic`.

It drives three things and gates nothing:

1. `templates.TEMPLATES[type]` → starter stage kit, starter Dockerfile, build parameters,
   `expectedSecrets` (keys only, never values).
2. `default_pipelines._PROBES[type]` → which marker files to read, and
   `resolve_default_pipeline` → the unsaved fallback pipeline.
3. Serializer/card icon, and `catalog.readiness` (`generic` requires a hand-written command).

This is exactly the conflation the brief asks to undo. **It cannot be deleted** — every
existing service carries one, the Dockerfile seeding and fallback pipeline read it, and the
`generic` special case is load-bearing in `readiness`/`can_run_build`. The proposal is
therefore: keep `application_type` as a required *derived discriminator*, and add a
structured `ApplicationProfile` beside it as the source of truth for detail. One function
maps profile → type, so the old machinery keeps working unchanged.

## 4. Current pipeline / stage model

`CiPipelineStage` fields, and how they map to what Hermes may emit:

| Field | Hermes may set | Note |
|---|---|---|
| `name`, `position` | yes | unique per pipeline (case-insensitive), max 40 stages |
| `stage_type` | yes, from `STAGE_TYPES` | `checkout`, `command`, `container_image`, `publish_artifact`, `scan`. Only `checkout`/`command`/`container_image` have executors |
| `runner_type` | yes, from `RUNNER_TYPES` or null | null = "any runner whose capabilities cover the labels" |
| `runner_labels` | yes, **from an advertised vocabulary only** | see §11 |
| `image` | **no — resolved by KubeSight** | see §13 |
| `working_directory` | yes, relative or `$KUBESIGHT_*` | |
| `commands` | yes | ≤100 lines, ≤4000 chars each |
| `env` | yes | `IMAGE_TAG` templates are re-validated |
| `secret_refs` | yes, **names only** | a reference to a non-existent secret is refused on save |
| `artifacts` | yes | `[{path, type, name}]` |
| `resources` | yes | cpu/memory/ephemeralStorage |
| `host_aliases` | yes | `[{ip, hostnames[]}]`, IP and hostname validated |
| `run_condition` | yes | `{variable, operator: equals\|not_equals, value}` only |
| `timeout_seconds` | yes | 30s – 24h |
| `continue_on_failure`, `enabled`, `parallel_group` | yes / yes / ignored | `parallel_group` is written and never read |

**Correction to the brief (§5, "dependencies between stages"):** KubeSight has **no stage
dependency graph**. A pipeline is a linear ordered list executed sequentially; `parallel_group`
is reserved and unread. Hermes must emit an *ordered list*, not a DAG. Asking it for
`dependsOn` would invent a concept the executor does not have. The validator will reject any
such key.

`pipelines.normalize_stage` (`services/ci/pipelines.py:477`) is already the complete
structural authority — it is reused verbatim rather than re-implemented.

## 5. Existing Hermes repo-access implementation

There are **two** mechanisms today, and they are very different.

### (a) Heavy path — Application Intelligence, isolated Kubernetes Job

```
POST /api/application-intelligence/applications/:id/analyses
  → application_intelligence_service.request_analysis (:798)
  → application_analysis_jobs.build_job_resources()      namespace kubesight-analysis,
                                                         per-analysis Secret, NetworkPolicy
                                                         restricted to the Hermes ns/port
  → initContainer: api/application_checkout.py           git init/fetch --depth 1, GIT_ASKPASS,
                                                         token only in env, never in the URL
  → container:     api/application_worker.py             discover_repository() → scanners →
                                                         application_intelligence_hermes.analyze()
  → POST back to /api/application-intelligence/analyses/<id>/{event,result,cleanup}
    with a per-analysis bearer token (sha256 in worker_callback_token_hash)
```

Execution modes: `kubernetes` (default), `local_docker`, `disabled` (tests).
Progress is persisted on `ApplicationAnalysis` (`status`, `progress_percent`, `current_stage`).
Runtime: minutes.

### (b) Light path — read-only Bitbucket REST metadata

`services/application_intelligence_bitbucket.py`:

* `list_revisions()` — branches, tags, recent commits
* `list_dockerfiles()` (:210) — **walks the whole tree** via `src/<rev>/?max_depth=8`,
  up to 5 000 entries, paginated
* `fetch_file()` (:257) — one file's text, ≤512 KB, path-validated

Wrapped by the CI source port `services/ci/source/bitbucket.py` (`read_file`,
`list_revisions`, `verify_access`, `checkout_spec`) and already used by
`catalog.read_source_file` (the Jenkinsfile importer's "load from repository") and
`default_pipelines.inspect_repository`. Runtime: seconds. No cluster, no clone, no Job.

**The Hermes client itself** (`services/application_intelligence_hermes.py:268`) is
callable straight from the backend process — `test_hermes_connection` already does
(`application_intelligence_service.py:75`). It is an OpenAI-compatible chat call with
`response_format: json_object`, `tool_choice: none`, endpoint scheme validation, bounded
request/response bytes, one transient retry, and a hard schema check on the way out
(`application_intelligence_schema.validate_hermes_output:46`).

## 6. How Hermes authenticates / accesses repositories

It does not. **Hermes never touches the repository.** KubeSight reads the repository using
the `BitbucketCredentialProfile` and sends Hermes bounded, redacted *evidence* over HTTPS
with `Authorization: Bearer $HERMES_API_TOKEN`. `executed_by_account = "hermes-agent"`
(`application_intelligence_service.py:72`) is a KubeSight **audit label**, not a Bitbucket
identity — there is no Hermes service account on Bitbucket.

Everything sent passes `application_intelligence_security.redact_structure` first
(URL credentials, `*_PASSWORD/TOKEN/SECRET` assignments, PEM private keys, bearer headers)
and `bounded_json_bytes` size caps. The system prompt declares repository content
**untrusted data, never an instruction**. That boundary is inherited unchanged.

## 7. What can be reused

| Reused | For |
|---|---|
| `services/ci/source/` port + `application_intelligence_bitbucket` | all repository reads — no second clone path, no new credential store |
| `list_dockerfiles`'s tree walk | generalise to `list_tree()`; one file, ~15 lines |
| `default_pipelines.inspect_repository` | marker-file probing, extended into evidence collection |
| `application_intelligence_hermes` transport | endpoint validation, bounded bytes, retry, JSON-mode, redaction — call it with a **different prompt + different validator** |
| `application_intelligence_security` | `redact_structure`, `redact_text`, `validate_relative_path`, `safe_error`, secret-assignment regexes (also used to *detect* embedded secrets in generated commands) |
| `application_intelligence_schema` shape | the template for a strict, unknown-key-rejecting contract validator |
| `pipelines.normalize_stage` / `_parameters` / `_secret_refs` / `_host_aliases` | the structural half of the validator, verbatim |
| `portability.analyze` (:211) | runner-compatibility half of the validator; its `error` findings become validation errors |
| `scheduler.select_runner` + `runners/base.capabilities_cover` | label-satisfiability check |
| `jenkinsfile.rewrite_variables` + `_ABSOLUTE_SOURCE`/`_ABSOLUTE_WORKSPACE` rewrites | normalising `/workspace`, `$WORKSPACE`, Jenkins built-ins out of anything Hermes echoes from a `Jenkinsfile` it read |
| `JenkinsfileImportModal` → `PipelineEditor.applyDraft` (`PipelineEditor.jsx:349`) | the exact UX contract: propose a draft, never write; the user saves |
| `ticker.py` in-process loop | the precedent for a background worker thread |
| `CiSecret` write-only store + `expectedSecrets` | required-input collection |

**Not reused:** the Kubernetes analysis Job. Generating a pipeline needs ~30 files and a
tree listing, not a clone, Trivy, Semgrep and an isolated namespace. Reaching for it would
turn a 20-second interaction into a multi-minute one and couple CI registration to cluster
availability. See §20 decision D1.

## 8. Proposed ApplicationProfile model

A typed document, validated on the way in, stored as one JSON column — **not** dozens of
columns and **not** a second table.

```jsonc
// ci_services.application_profile
{
  "schemaVersion": "1.0",
  "language": "java",              "languageVersion": "17",
  "framework": "spring-boot",      "frameworkVersion": "3.3.2",
  "buildSystem": "gradle",         "buildSystemVersion": "8.7",
  "usesBuildWrapper": true,
  "packageManager": null,
  "packaging": "jar",
  "projectStructure": "multi-module",         // single | multi-module | monorepo
  "modules": ["core", "api"],
  "containerization": { "type": "dockerfile", "dockerfilePath": "Dockerfile" },
  "testsDetected": true,
  "testFramework": "junit5",
  "artifactPaths": ["build/libs/*.jar"],
  "platformTargets": ["android"],             // flutter / mobile only

  // provenance — §23. Per FIELD, never a global number.
  "evidence": {
    "languageVersion":    { "value": "17",    "confidence": "Confirmed",
                            "source": "build.gradle", "detail": "toolchain { languageVersion = 17 }" },
    "buildSystemVersion": { "value": "8.7",   "confidence": "Confirmed",
                            "source": "gradle/wrapper/gradle-wrapper.properties" },
    "frameworkVersion":   { "value": "3.3.2", "confidence": "High",
                            "source": "build.gradle", "detail": "plugins { id 'org.springframework.boot' version '3.3.2' }" }
  },
  "overrides": { "languageVersion": { "was": "17", "by": 4, "at": "2026-09-16T10:02:00Z" } },
  "unknown": ["frameworkVersion"],            // honestly empty rather than guessed
  "derivedApplicationType": "java_gradle"
}
```

Rationale for a JSON column on `ci_services`:

* it is one document with one owner, always read with the service, always written whole;
* `CiPipeline.parameters` and `CiPipelineStage.host_aliases` already set the precedent, and
  `migrate_rbac._add_column_if_missing` + `_retype_json_column` is the established migration;
* a separate table would need joins in every serializer for zero query benefit.

`derivedApplicationType` keeps `application_type` in sync — computed by a pure
`profile.derive_application_type(profile)`, so the whole existing template/fallback/icon
machinery keeps working. `application_type` becomes **editable after creation** when a
profile is accepted (today it is frozen; see §16).

`confidence` is an **enum**, not a float — see §20 decision D3.

## 9. Proposed Hermes request / response contract

Contract id `kubesight.ci.pipeline-plan`, `schemaVersion: "1.0"`, versioned independently
of `application-intelligence-v2`.

**Request** (`services/ci_assist/hermes.py`, transport borrowed from
`application_intelligence_hermes`):

```jsonc
{
  "task": "generate_ci_pipeline",          // or "repair_ci_pipeline"
  "schemaVersion": "1.0",
  "trustLevel": "untrusted_repository_evidence",
  "capabilities": {                        // what KubeSight can actually execute
    "stageTypes":     ["checkout","command","container_image"],
    "runnerTypes":    ["kubernetes","agent_linux","agent_macos"],
    "runnerLabels":   ["linux","macos","java","java17","java21","node","python",
                       "android","flutter","docker","xcode","generic"],   // live, from registered runners
    "buildEnvironments": [ {"key":"java-jdk11","provides":{"java":"11"}},
                           {"key":"node-22","provides":{"node":"22"}} ],
    "parameterTypes": ["text","multiline","choice","boolean","dynamic_choice"],
    "artifactTypes":  ["jar","war","zip","binary","apk","aab","ipa","test-report"],
    "workspaceVariables": ["$KUBESIGHT_WORKSPACE","$KUBESIGHT_SOURCE","$KUBESIGHT_ENV",
                           "$KUBESIGHT_BUILD_NUMBER","$KUBESIGHT_BRANCH","$KUBESIGHT_COMMIT"],
    "limits": { "maxStages": 40, "maxCommandsPerStage": 100,
                "minTimeoutSeconds": 30, "maxTimeoutSeconds": 86400 }
  },
  "constraints": [
    "Emit an ORDERED stage list. There is no dependency graph and no parallelism.",
    "Never set a container image. Request a buildEnvironment key instead.",
    "Never embed a credential. Declare it in requiredInputs and reference it by name.",
    "Use only $KUBESIGHT_* paths. Never an absolute filesystem path.",
    "No docker build/push, no sudo, no package installation — build pods forbid all three.",
    "Emit the minimum pipeline that works. No scanning, deployment or promotion stages unless the repository configures them.",
    "State uncertainty. Never invent a version you did not read."
  ],
  "resultContract": { "exactTopLevelKeys": true, "template": {} },
  "evidence": {                             // redacted + size-bounded
    "repository": {"provider":"bitbucket","revision":"main","workingDirectory":""},
    "tree": ["gradlew","build.gradle","settings.gradle","Dockerfile","src/main/..."],
    "files": [ {"path":"build.gradle","content":"..."} ],
    "deterministic": {}                     // inspect_repository() output
  }
}
```

**Response** — exactly these top-level keys, nothing more:

```jsonc
{
  "schemaVersion": "1.0",
  "applicationProfile": {},                 // §8 minus overrides/derivedApplicationType
  "pipeline": {
    "name": "default",
    "description": "...",
    "parameters": [ { "name":"SKIP_TESTS","type":"boolean","label":"Skip tests","default":"false" } ],
    "stages": [
      { "name":"Checkout", "stageType":"checkout", "runnerLabels":["linux"], "timeoutSeconds":600 },
      { "name":"Build", "stageType":"command",
        "buildEnvironment":"java-jdk11", "runnerLabels":["linux","java"],
        "workingDirectory":"", "commands":["./gradlew --no-daemon clean build -x test"],
        "artifacts":[{"path":"build/libs/*.jar","type":"jar"}], "timeoutSeconds":2400 }
    ]
  },
  "requiredInputs": [
    { "name":"IMAGE_REPOSITORY", "kind":"parameter", "required":true,
      "label":"Image repository", "description":"Destination image repository" },
    { "name":"NEXUS_USERNAME", "kind":"secret", "required":true,
      "usedByStages":["Build"], "reason":"build.gradle resolves from https://nexus.areeba.com/..." }
  ],
  "analysis": {
    "warnings": ["No test task was found; the test stage was omitted."],
    "unknown":  ["frameworkVersion"],
    "notes":    []
  }
}
```

Note `kind` rather than `type` on required inputs, and a third kind `registry` for
"this service needs a `RegistryConnection` linked" — registry credentials are **not**
`CiSecret`s in KubeSight; `engine._registry_for` reads them off `RegistryConnection`
(`engine.py:962`). Modelling them as secrets would produce values nothing ever reads.

## 10. Proposed pipeline-generation schema

The `pipeline` object above **is** the native editor payload — the same camelCase dict
`normalize_stage` accepts and `PipelineEditor` renders — with exactly two differences:

1. `image` is **forbidden**; `buildEnvironment` (a catalog key) replaces it and KubeSight
   resolves it to an approved image + the labels that image implies.
2. Unknown keys are rejected outright (mirroring `validate_hermes_output`), which is what
   makes `privileged: true`, `hostPath`, `serviceAccount`, `dependsOn` and every other
   invented concept a hard failure rather than something to strip and hope about.

Consequence: accepting a generated pipeline is literally
`pipelines.update_pipeline(pipeline, generated_payload)`. No second representation, no
translation layer, no "Hermes pipeline" type. That is the §26 boundary made structural.

## 11. Proposed KubeSight validation layer

New module `services/ci/generated.py` — **inside** `services/ci`, importing no AI code
(it validates a plain dict), so the package docstring's "no Hermes on the build path"
invariant survives.

```python
validate(service, payload) -> {"valid": bool, "errors": [...], "warnings": [...], "pipeline": {...}}
```

Three layers, each already half-built:

**(a) Contract** — `ci_assist/schema.py`: exact top-level keys, types, enums, list caps.
Reject-not-repair, as `validate_hermes_output` does.

**(b) Structure** — `pipelines.normalize_stage` + `_parameters` + `_secret_refs` +
`_host_aliases` + `_run_condition` + `_check_image_tag_template`, per stage, collecting
every `PipelineError` instead of raising on the first. Gives stage names, unique names,
stage types, runner types, timeouts, command limits, artifact specs, host aliases,
conditions for free.

**(c) Policy** — new checks, each mapping to a numbered brief requirement:

| Check | Rejects |
|---|---|
| `unknown_field` | any key outside the contract (§11 privileged/hostPath) |
| `image_not_permitted` | a literal `image`; only a `buildEnvironment` key resolvable by the catalog, or an operator-allow-listed registry prefix |
| `unknown_build_environment` | a `buildEnvironment` key not in the catalog |
| `unsatisfiable_runner_labels` | labels no **registered** runner advertises — `scheduler.eligible_runners()` + `capabilities_cover`. **This is the check that catches the live `java11` bug (§19).** |
| `runner_type_unavailable` | a `runner_type` with no shipped adapter (`available_runner_types()`) |
| `path_escape` | absolute `working_directory`/artifact path, or `..` — `validate_relative_path` |
| `absolute_workspace_path` | literal `/workspace`, `/cache`, `/home/...` in commands — `portability` error findings |
| `forbidden_command` | `docker build/push`, `sudo`, `apt-get install` — `portability` error findings |
| `embedded_secret` | a credential-shaped literal in `env` or a command — `SECRET_ASSIGNMENT_PATTERN` / `URL_CREDENTIAL_PATTERN` / `PEM_PRIVATE_KEY_PATTERN` from `application_intelligence_security` |
| `undeclared_secret_ref` | a `secretRefs` name that is neither an existing `CiSecret` nor a declared `requiredInputs` secret |
| `timeout_out_of_range`, `too_many_stages`, `duplicate_stage_name` | limits |
| `unsupported_stage_type` | `scan`/`publish_artifact` while they have no executor → **warning**, not error (they are valid definitions today) |
| `container_image_without_registry` | an image stage on a service with no `RegistryConnection` → **warning** + a `registry` requiredInput |

`warnings` never block; `errors` do, and are exactly what goes back to Hermes (§12).
The existing `POST /api/ci/pipelines/lint` stays as-is for the hand editor; the generated
validator is the strict superset.

**Nothing Hermes produces is ever executed unvalidated, and nothing is saved without an
explicit human confirmation.**

## 12. Proposed correction loop

```
generate ──▶ validate ──▶ valid? ──yes──▶ ANALYZED, show the user
   ▲                        │
   │                       no
   │                        ▼
   └──── repair(errors) ◀── attempts left AND error set changed?
                            │
                           no ──▶ PARTIAL: show profile + read-only invalid draft +
                                  the errors + [Retry] [Configure manually]
```

* `CI_ASSIST_REPAIR_ATTEMPTS`, default **2**, hard-capped at 3.
* The repair request is `task: "repair_ci_pipeline"` with the same evidence plus
  `previousPipeline` and `validationErrors` (codes + messages + stage names only —
  no repository content is re-derived, no secret ever travels).
* **Early stop on no progress:** if the error code set is identical to the previous
  attempt's, stop immediately rather than burning the remaining attempt. A model that
  repeated itself once will repeat itself again.
* Every attempt is recorded on the analysis row (`attempts` JSON: request hash, error codes,
  model, duration) so a bad prompt is debuggable without re-running.
* Failure is never terminal for the user: `PARTIAL` still offers the profile it did
  determine, and manual configuration is always one click away (§21).

## 13. Build Environment Catalog

Today images are scattered constants: `templates._JDK_IMAGE`, `_NODE_IMAGE`,
`_PYTHON_IMAGE`, and `default_pipelines._GRADLE_IMAGE`, `_MAVEN_IMAGE`, `_ANDROID_IMAGE`,
`_FLUTTER_IMAGE` — each an `os.getenv` with a default. Hermes must never name an image.

New `services/ci/build_environments.py`:

```python
CATALOG = {
  "java-jdk11": {"image": _env("CI_TEMPLATE_JDK_IMAGE", "<registry>/adoptopenjdk/openjdk11:..."),
                 "provides": {"java": "11"}, "labels": ["linux", "java"],
                 "notes": "Carries no Maven or Gradle — the project wrapper supplies them."},
  "gradle-8-jdk11": {}, "maven-3.9-jdk21": {},
  "node-22": {}, "python-3.12": {}, "android": {}, "flutter": {},
}
resolve(key) -> {image, labels}
best_for(language, version, tool) -> key | None
```

Seeded from the existing environment variables so **behaviour today is unchanged**, and
`templates.py`/`default_pipelines.py` are refactored to read the catalog instead of holding
their own constants. Hermes asks for capabilities (`{"java":"17","gradle":true}`), KubeSight
resolves; unresolvable → `unknown_build_environment` fed back, or the user picks.

**Finding worth your attention:** this installation's only JDK image is **Java 11**, and the
built-in Kubernetes runner advertises `java, java17, java21` but **not** `java11`. So a
repository that genuinely targets Java 17 cannot be honoured by the current image set. The
catalog makes that visible and actionable (add an entry, one env var) instead of producing a
pipeline that compiles against the wrong JDK. Until entries exist, `best_for` returns the
closest approved environment and the analysis emits a warning naming the mismatch.

## 14. Required backend changes

**New package `backend/api/services/ci_assist/`** (outside `services/ci`, so CI keeps its
"imports no AI code path" invariant — the dependency points *into* CI, never out):

| File | Responsibility |
|---|---|
| `__init__.py` | package docstring stating the boundary |
| `profile.py` | `ApplicationProfile` dataclass, validation, `derive_application_type()`, merge-with-user-overrides |
| `evidence.py` | tree listing + bounded file selection through the CI source port; redaction; size budget |
| `schema.py` | the versioned request/response contract + strict validator |
| `hermes.py` | the Hermes call for `generate`/`repair` (transport pattern from `application_intelligence_hermes`) |
| `generator.py` | orchestration: evidence → generate → validate → repair loop → persist states |
| `jobs.py` | bounded background worker thread + stale-analysis reaper |

**New in `services/ci/`** (CI-owned authority, no AI imports):

* `generated.py` — the validator (§11)
* `build_environments.py` — the catalog (§13)

**Changed:**

* `services/ci/catalog.py` — accept/serve `applicationProfile`; allow `applicationType` to
  change when a profile is accepted; `create_service` gains an optional one-shot
  `source` block so the wizard is one request
* `services/ci/serializers.py` — `applicationProfile`, `analysisState`, `profileSource`
* `services/ci/pipelines.py` — expose a `collect_errors=True` mode on `_apply_stages`/
  `normalize_stage` so the validator gathers rather than raises on the first problem
* `services/application_intelligence_bitbucket.py` — generalise `list_dockerfiles`'s tree
  walk into `list_tree(repository_ref, token, revision, ...)`; `list_dockerfiles` becomes a
  filter over it (behaviour identical, one test to keep it that way)
* `services/ci/source/__init__.py` + `bitbucket.py` — add `list_tree` to the provider
  protocol, so GitLab/GitHub later is still one module
* `api/models_ci.py` — `CiService.application_profile`, `.profile_source`,
  `.analysis_state`; new `CiRepositoryAnalysis` model
* `api/migrate_rbac.py` — `_migrate_ci_columns()` additions (§15)
* `api/routes/ci_assist.py` — new blueprint (below), registered in `routes/__init__.py`
* `api/app.py` — start the assist worker beside `start_ci_engine`

**API** (new blueprint at `/api/ci`, matching existing conventions):

| Route | Permission | Purpose |
|---|---|---|
| `POST /api/ci/services/<id>/analysis` | `ci_pipelines:edit` + `applications:analyze` | start; 202 + analysis row |
| `GET  /api/ci/services/<id>/analysis` | `ci_services:view` | latest analysis (the poll target) |
| `GET  /api/ci/analyses/<id>` | `ci_services:view` | one analysis |
| `POST /api/ci/analyses/<id>/cancel` | `ci_pipelines:edit` | stop a running analysis |
| `POST /api/ci/analyses/<id>/regenerate` | `ci_pipelines:edit` | re-run from an edited profile (no repo re-read unless asked) |
| `POST /api/ci/analyses/<id>/accept` | `ci_pipelines:edit` (+`ci_secrets:manage` if secrets supplied) | write profile + pipeline + secrets, atomically |
| `POST /api/ci/pipelines/validate-generated` | `ci_pipelines:view` | validator alone, for manual mode and the editor |

**Async mechanism:** a bounded background thread pool started exactly where
`ticker.start_ci_engine` is, writing progress to `ci_repository_analyses`; the frontend
polls the `GET`. Rationale in §20 decision D2 — the K8s Job path exists for *clone + scan*,
which this does not do. Multi-replica caveat: an analysis is pinned to the replica that
started it, so a stale-row reaper (mirroring `engine._reap_stale_builds`) marks it `FAILED`
with "the analysis worker stopped responding" after `CI_ASSIST_STALE_SECONDS`.

## 15. Database / schema changes

Additive only. Three columns and one table.

```sql
ALTER TABLE ci_services ADD COLUMN application_profile JSON;        -- nullable
ALTER TABLE ci_services ADD COLUMN profile_source VARCHAR(16);      -- hermes|manual|derived|NULL
ALTER TABLE ci_services ADD COLUMN analysis_state VARCHAR(16);      -- NULL = NOT_ANALYZED

CREATE TABLE ci_repository_analyses (
  id, service_id FK ci_services ON DELETE CASCADE,
  state VARCHAR(16),          -- queued|analyzing|analyzed|partial|failed|cancelled
  pipeline_state VARCHAR(16), -- not_generated|generating|valid|invalid|user_modified
  progress_percent INT, current_stage VARCHAR(64),
  revision VARCHAR(255), commit_sha VARCHAR(64),
  requested_by_user_id FK users, executed_by_account VARCHAR(120) DEFAULT 'hermes-agent',
  mode VARCHAR(16),           -- repository|profile   (§16 manual-profile generation)
  schema_version VARCHAR(16), hermes_model VARCHAR(120), hermes_prompt_version VARCHAR(64),
  application_profile JSON, generated_pipeline JSON, required_inputs JSON,
  validation JSON,            -- {valid, errors[], warnings[]}
  attempts JSON,              -- one entry per generate/repair attempt
  warnings JSON, evidence_coverage JSON,
  safe_error_message TEXT, failure_stage VARCHAR(64),
  created_at, started_at, completed_at, last_heartbeat_at
);
```

Migration follows the house pattern exactly: the table from `db.create_all()`, the three
columns from `_add_column_if_missing` + `_retype_json_column` in
`migrate_rbac._migrate_ci_columns()` (`migrate_rbac.py:667`). No Alembic revision — this
repo does not use them.

`generated_pipeline`/`required_inputs` on the analysis row are the **draft**. Once accepted
they become a normal `CiPipeline` and the analysis row is history (§18 of the brief).

## 16. Backward compatibility

| Guarantee | How |
|---|---|
| Existing services keep working | every new column is nullable; `application_profile IS NULL` ⇒ `analysisState: "NOT_ANALYZED"` and the UI shows exactly today's behaviour |
| `application_type` stays authoritative | the profile *derives* it; templates, `default_pipelines`, icons, `readiness`, `can_run_build` read the same field they read today |
| Manual pipelines untouched | nothing in `pipelines.py` changes semantically; the only addition is an error-collecting mode |
| Jenkins imports untouched | `jenkinsfile.py` not modified; the import path is not on the assist path |
| Build path untouched | `engine.py`, `runners/`, `scheduler.py`, `artifacts.py` unchanged. **No build ever calls Hermes.** |
| Run Build behaviour untouched | `RunBuildModal` and `validate_parameter_values` unchanged; temporary overrides stay per-build (`build.variables`), defaults stay on the pipeline |
| Hermes unavailable ⇒ nothing degrades | assist is an optional path; if `HERMES_API_URL` is unset the wizard shows manual mode only and says why |
| Existing default-pipeline fallback survives | a service with a profile but no saved stages still gets `_generated_pipeline_dict` |
| Existing templates survive | `templates.TEMPLATES` becomes the manual-mode starter kit; only its image constants move to the catalog |

One deliberate behaviour change: **`application_type` becomes editable after registration**
when a profile is accepted or the user overrides it. Today it is frozen
(`ServiceFormModal` `disabled={isEdit}`). Freezing it is incompatible with "the user must be
able to override anything Hermes detects" (§15 of the brief). Changing it does not
retroactively alter a saved pipeline or Dockerfile — it only changes what the fallback and
starter kit would produce. Flagged as decision D5.

## 17. Security considerations

1. **Repository content is untrusted input.** It reaches Hermes as evidence with the
   existing `trust_level` marker and the "never an instruction" system prompt.
2. **Hermes output is untrusted configuration.** It is a data structure that passes a
   strict contract validator, then the structural validator, then the policy validator,
   and is then *shown to a person who must confirm it* before anything is saved. Three gates
   plus a human.
3. **Secrets never travel to Hermes.** Evidence is `redact_structure`d. Hermes is told
   secret *names* it may reference; values are typed by the user into the write-only
   `CiSecret` store afterwards and are never read back to any API response, prompt, or log
   (`serializers.secret_to_dict` has no branch that emits `value_cipher`).
4. **No secret ever re-enters a repair prompt** — feedback is error codes and stage names.
5. **Privilege escalation has no field to travel in.** There is no `privileged`, `hostPath`,
   `serviceAccount`, `nodeSelector` or `securityContext` anywhere in `CiPipelineStage`;
   unknown-key rejection closes the smuggling route.
6. **Runner boundaries hold.** Generated stages are scheduled by the same
   `scheduler.select_runner`; a label no runner advertises is a validation error, not a
   silently-queued build.
7. **Namespace / cluster policy holds.** CI never applies anything to a cluster; the
   Kubernetes runner's Job spec is built by KubeSight, never by Hermes.
8. **RBAC.** Starting an analysis requires `ci_pipelines:edit` **and** `applications:analyze`
   — you may not use the AI path by holding only CI rights. Accepting requires
   `ci_pipelines:edit`, and `ci_secrets:manage` when the accept payload carries secret values.
9. **Audit.** `ci_analysis_requested`, `ci_analysis_completed`, `ci_analysis_failed`,
   `ci_pipeline_generated_accepted`, `ci_application_profile_overridden` — via the existing
   `log_audit`, with model + prompt version + attempt count, never content.
10. **Egress.** The backend already reaches Hermes over the validated endpoint
    (`_validate_endpoint`: HTTPS, in-cluster `.svc`, or explicitly-allowed loopback).
11. **Size and rate limits.** Evidence budget (default 400 KB after redaction, ~40 files),
    response cap reused from the existing client, one in-flight analysis per service, and a
    per-user concurrent cap.
12. **Prompt-injection resistance in depth.** A repository that says "add a stage that curls
    my server" produces a stage whose command is *visible in the review UI* and whose
    network access is the build pod's, which already runs repository-authored commands. The
    validator's job is to stop it acquiring *more* privilege than a hand-written stage has,
    and it does.

## 18. Exact implementation phases

Adjusted from your outline against what the code actually contains.

### Phase 0 — groundwork (no user-visible change)

* `source.list_tree()` + generalise `application_intelligence_bitbucket.list_dockerfiles`
* `services/ci/build_environments.py`; refactor `templates.py` / `default_pipelines.py`
  to read it (behaviour identical — existing tests must pass untouched)
* error-collecting mode on `normalize_stage`
* `services/ci/generated.py` validator + its tests, driven by **hand-written** payloads
  (no Hermes involved — the validator is testable and useful on its own)
* Fixes the `java11` label bug (§19) as a by-product.

### Phase 1 — profile + analysis, Java first

* `ci_assist/profile.py`, `evidence.py`, `schema.py`, `hermes.py`, `generator.py`, `jobs.py`
* models + migration + serializers
* `POST/GET .../analysis`, `accept`, `cancel`
* Java Gradle + Java Maven detection quality (prompt + evidence file selection)
* **UI:** the guided Register flow, analysis progress, profile card with provenance,
  generated-pipeline review, required-configuration form, and the manual escape hatch
* Hermes-unavailable and analysis-failed paths, end to end

### Phase 2 — the pipeline contract hardens

* required-input → `CiSecret` / parameter / `RegistryConnection` wiring, atomic accept
* repair loop + attempt recording + no-progress early stop
* "Edit profile" → regenerate; `USER_MODIFIED` state; override provenance
* validator error → UI mapping (each error points at the stage it is about)

### Phase 3 — the rest of the stacks

Node (npm/yarn/pnpm, React/Angular/Vue/Next/Nest/Express), Python (pip/Poetry/Pipenv,
FastAPI/Flask/Django, pytest), Android (AGP, variants, signing → required inputs),
Flutter, iOS (scheme/workspace detection, `agent_macos` runner requirement, signing),
container-only, and the honest `custom` profile with a stated reason.

### Phase 4 — polish

* "Regenerate with Hermes", "Ask Hermes to fix this pipeline" from the editor
* build-environment catalog management UI
* policy hooks (an installation that *requires* a scan stage)
* generation from a manually-entered profile without any repository read (§16 of the brief)

Phases 0–2 are the product. 3 is breadth. 4 is optional.

## 19. Files likely to change

**Backend — new**

```
api/services/ci_assist/{__init__,profile,evidence,schema,hermes,generator,jobs}.py
api/services/ci/generated.py
api/services/ci/build_environments.py
api/routes/ci_assist.py
tests/test_ci_assist_profile.py
tests/test_ci_assist_validator.py
tests/test_ci_assist_generation.py      (Java/Maven/Node/Python/Android/Flutter/Docker/custom)
tests/test_ci_assist_repair_loop.py
tests/test_ci_assist_api.py
tests/test_ci_build_environments.py
```

**Backend — changed**

```
api/models_ci.py                        3 columns + CiRepositoryAnalysis
api/migrate_rbac.py                     _migrate_ci_columns()
api/services/ci/catalog.py              profile in/out, applicationType editable, one-shot source
api/services/ci/serializers.py          applicationProfile / analysisState / profileSource
api/services/ci/pipelines.py            collect-errors mode
api/services/ci/templates.py            images → build_environments
api/services/ci/default_pipelines.py    images → build_environments
api/services/ci/source/{__init__,bitbucket}.py       list_tree
api/services/application_intelligence_bitbucket.py   list_tree, list_dockerfiles filters it
api/routes/__init__.py                  register the blueprint
api/app.py                              start the assist worker
```

**Frontend — new**

```
components/catalog/RegisterServiceWizard.jsx     the guided flow (§1 of the brief)
components/catalog/HermesAnalysisPanel.jsx       progress + result
components/catalog/ApplicationProfileCard.jsx    detected values, provenance, override
components/catalog/RequiredConfigForm.jsx        dynamic parameter/secret/registry form
components/catalog/GeneratedPipelineReview.jsx   stage list + validation findings
api/ciAssistApi.js
```

**Frontend — changed**

```
pages/ServiceCatalogPage.jsx     "Register a service" opens the wizard
pages/ServiceDetailPage.jsx      Application profile surfaced; "Regenerate with Hermes"
components/catalog/ServiceFormModal.jsx   becomes the wizard's identity step (edit mode unchanged)
components/catalog/PipelineEditor.jsx     reuse applyDraft for a Hermes draft; show its notes
components/catalog/ciShared.jsx           profile labels/icons
```

**Unchanged on purpose:** `engine.py`, `runners/*`, `scheduler.py`, `artifacts.py`,
`jenkinsfile.py`, `RunBuildModal.jsx`, `BuildsPanel.jsx`, `StageMatrix.jsx`.

## 20. Architectural decisions that need your approval

**D1 — Reuse the *light* repo-access path, not the analysis Job.**
Evidence comes from the Bitbucket REST tree walk + bounded `fetch_file` through the existing
CI source port — no clone, no `kubesight-analysis` namespace, no scanners. ~30 files and a
tree listing is what pipeline generation needs; a clone is what *security scanning* needs.
Cost: seconds instead of minutes, and it works with no cluster (mock mode, dev laptops).
Trade-off: we do not see binary files or run scanners. I believe that is the right call for
this feature. **If you want the full Job path, say so now** — it changes §14 substantially.

**D2 — Analysis runs on an in-process background thread, not a Kubernetes Job.**
Mirrors `ticker.py`. Needs the stale-row reaper for multi-replica safety. Alternative is
reusing `application_analysis_jobs` (heavier, couples registration to cluster health).

**D3 — Confidence is an enum per field, not a float.**
Your §8 example shows `"confidence": 0.96`. Application Intelligence *deliberately banned*
model-chosen numbers — the system prompt says "Never emit numeric scores, ratings, or
percentages", and the 2026-07-31 accuracy audit removed the 0–100 scores for exactly this
reason. I propose `Confirmed | High | Medium | Low` per field plus the literal evidence
string (§23 of your brief), which is both more honest and consistent with the rest of the
product. **Confirm you accept the deviation.**

**D4 — Hermes emits an ordered stage list, not a dependency graph.**
KubeSight has no DAG (`parallel_group` is written and never read). Your §5 lists
"dependencies between stages"; the honest mapping is ordering. Adding a real DAG is a
separate, larger piece of work in `engine.py`.

**D5 — `application_type` becomes editable after registration.**
Required by "the user must be able to override anything Hermes detects". Today it is frozen.
It changes only what the fallback/starter kit would produce, never a saved pipeline.

**D6 — `ci_assist` is a separate package that imports CI; CI never imports it.**
`services/ci/__init__.py` states "This package does not import Hermes, Application
Intelligence analyses, or any AI code path." I want to keep that literally true — it is what
guarantees a build cannot depend on an LLM. The validator lives *in* CI (it validates a dict,
imports nothing AI); the Hermes call lives *outside*.

**D7 — Registry credentials are a `RegistryConnection` link, not a secret.**
`engine._registry_for` reads username/password off `RegistryConnection`. So
`REGISTRY_USERNAME`/`REGISTRY_PASSWORD` from your §7 example become one required input of
kind `registry` — "link a registry connection" — rather than two `CiSecret`s that nothing
would ever read. Nexus/Git/signing credentials remain genuine secrets.

**D8 — The wizard creates the service *before* analysing it.**
Register (identity + source) → the row exists → analyse. This makes the analysis FK
non-null, keeps §21 ("Hermes must never block service creation") structurally true, and
means a failed analysis leaves a perfectly usable, manually-configurable service. The user
still sees one continuous flow.

---

### Bug found during this review (independent of the feature)

`templates.py:272-320` labels every Java starter-kit stage `["linux", "java11"]`, but no
runner advertises `java11` — the built-in Kubernetes runner's capabilities
(`migrate_rbac.py:638`) are `linux, kubernetes, docker, java, java17, java21, node, python,
android, flutter, generic`. Applying the Java starter kit from the editor ("Reset to
template" / `from-template`) therefore produces a pipeline that queues forever with
"No online runner provides: java11." `default_pipelines.py:211` already documents the
correct label (`java`) and explains exactly why — `templates.py` was not updated to match.
One-line fix, and Phase 0's label-satisfiability check is what prevents the class recurring.


---

# What was built

Implemented 2026-09-15, following the design above with the decisions D1-D8 taken
as recommended. 153 new tests; the full backend suite passes apart from one
failure (`test_ci_jenkins_replacement.py::test_trigger_variables_reach_every_stage_environment`)
that predates this work and fails identically on a clean tree.

## Verified end-to-end, not just unit-tested

Driven against a live backend in mock mode with a stub Hermes on the configured
endpoint, so the real HTTP client, the real contract validator, the real
KubeSight validator and the real UI were all exercised:

* the guided Register flow, with the Hermes/manual choice
* an analysis running, reporting real steps, and landing on a profile
* the profile card showing per-field provenance (`read from build.gradle`)
* the generated pipeline rendering with KubeSight-resolved images
* the required-configuration form, masked secrets, and the disabled Save
* accepting it, and the result being an **ordinary** `CiPipeline`
  (`isGeneratedDefault: false`, version 2, five stages, resolved images)
* `application_type` moving `generic -> java_gradle` from the accepted profile
* the two secrets stored encrypted at rest, with no route able to read them back

## Departures from the design

**The runner-capability check has three warning states, not one.** The design
said "covered by no registered runner" is an error. That turned out to be too
strict in one specific and normal case: the built-in Kubernetes runner ships
*disabled* until an operator applies its manifest, so on a fresh installation
every capability it advertises would have read as absent and no pipeline could
be generated until the fleet was fully set up — exactly backwards. The error now
means "nothing in the fleet has ever heard of this capability"; *disabled* and
*offline* are warnings that say what to do.

**Unknown fields split into two cases.** The design made every unknown stage
field a hard error, on the argument that silently dropping `privileged: true`
lets somebody approve a pipeline whose review screen never showed it. That
argument holds for privilege and nothing else — in practice the rule refused a
perfectly good pipeline because Hermes wrote `type` instead of `stageType`.
Synonyms are now normalized (`type`, `script`, `timeout`, snake_case), genuinely
unsupported cosmetic fields are dropped with a visible warning, and a denylist
of ~25 privilege-bearing names (`privileged`, `hostPath`, `securityContext`,
`serviceAccount`, `volumes`, `nodeSelector`, …) stays a hard error. The prompt
now also states the exact camelCase field names.

**Command credential detection needed its own pattern.** The shared redaction
patterns are tuned for configuration files — an uppercase assignment at the
start of a line, or a quoted value. A build command is neither, and
`./gradlew build -PnexusPassword=hunter2` is by some distance the most common
way a credential is hardcoded in CI. `services/ci/generated._BUILD_FLAG_SECRET`
covers that case without loosening the shared patterns, which also redact
evidence on the way *out* to a model.

**A user-corrected profile is honoured in both modes.** The design only wired
the profile hint through `profile` mode. That would have meant "Regenerate with
Hermes" after an override silently detected over the top of the correction —
the precise failure §15 of the brief exists to prevent.

**The stale-analysis reaper runs on read, not on the CI engine tick.** Putting
it on the tick would have made `services/ci` import `ci_assist` and broken D6.
It runs when somebody asks for an analysis, which is also the only moment anyone
cares whether it is alive, plus once at worker-pool startup.

## Bugs found and fixed along the way

* **Most branches were missing from every branch picker (pre-existing, live).**
  `list_revisions` made ONE `/refs` call capped at 200 items sorted by name, so
  branches and tags competed for the same budget. A repository with 291 tags and
  216 branches showed **24 branches** — in the Source tab, in Run Build, in
  `dynamic_choice` parameters, everywhere. The branch somebody wanted was usually
  absent and nothing said so. Branches and tags now come from `/refs/branches`
  and `/refs/tags` with a budget each (500/500), sorted newest-first so any
  future truncation drops stale refs rather than everything after "f" in the
  alphabet, and the payload reports `truncated` per kind.
* **The branch picker paid for tags it never showed.** `list_revisions` now
  takes `kinds`, and the wizard's preview asks for branches only — 2.5s instead
  of ~10s on a repository with 450 tags.
* **A malformed Hermes response ended the whole analysis.** A contract violation
  ("Proposed stage 1 has no name") was deliberately non-retryable, which was
  right about not *silently* re-issuing the same request and wrong about giving
  up. It is now fed back — `hermes.ContractFailure` carries the objection to the
  generator, which asks again with the problem stated, bounded by the same
  attempt budget.
* **The sort fallback retried rate limits.** An unsupported `sort` field falls
  back to the unsorted endpoint, but the original `except` caught every error —
  so a 429 or a timeout was immediately retried, making a struggling endpoint
  worse. `BitbucketMetadataError` now carries the HTTP status and only an
  outright rejection triggers the fallback.
* **`analysis_state` on the service could disagree with the analysis row.** It
  was set to `analyzing` at the start and never reset on failure, so the catalog
  showed a spinner beside a row that had finished.
* **A refusal still said "before KubeSight accepted it."** The provenance line
  contradicted the verdict directly above it.


* **`java11` labels (pre-existing).** `templates.py` labelled every Java
  starter-kit stage `["linux", "java11"]`, which no runner advertises — applying
  the Java starter kit produced a pipeline that queued forever. Fixed to `java`,
  matching what `default_pipelines.py:211` already documented. The
  `unsatisfiable_runner_labels` check is what stops the class recurring.
* **`best_for` crossed build systems.** The version-distance sort discarded the
  build-tool preference, so a Gradle project targeting Java 17 was told the
  closest approved environment was the *Maven* image. The build tool is now a
  hard filter. Caught by looking at the rendered UI, not by a test.
* **An absolute Dockerfile path was rewritten rather than rejected.**
  `profile._containerization` stripped the leading slash before checking, so
  `/etc/passwd` became the perfectly valid `etc/passwd` and was accepted.
* **Attempt records were lost to the cancellation check.** `_record_attempt`
  did not commit, and the `_cancelled()` refresh immediately discarded it.

## Where things live

```
backend/api/services/ci_assist/          the model path (imports CI; CI never imports it)
  profile.py      ApplicationProfile, validation, derive_application_type
  evidence.py     repository reads through the source port, budgeted + redacted
  schema.py       the versioned contract and its strict validator
  hermes.py       the propose/repair calls, on the shared transport
  generator.py    evidence -> propose -> validate -> repair -> persist
  jobs.py         worker pool and the stale-analysis reaper
  analyses.py     request, read, cancel, serialize
  accept.py       proposal -> ordinary KubeSight rows

backend/api/services/ci/
  generated.py            the validator — KubeSight's authority, no AI imports
  build_environments.py   the approved images, one owner

backend/api/routes/ci_assist.py          the API
frontend/src/components/catalog/         RegisterServiceWizard, HermesAnalysisPanel,
                                         ApplicationProfileCard, GeneratedPipelineReview,
                                         RequiredConfigForm
frontend/src/styles/signal/ciAssist.css
```

## Still open

* **Phase 3 breadth.** Node, Python, Android, Flutter, iOS, container and custom
  all have passing generation tests, but their quality depends on prompt and
  evidence tuning against real repositories, which needs real repositories.
* **The Java 17 gap is real.** This installation's only JDK image is Java 11.
  The catalog now says so in the analysis warnings instead of silently
  downgrading, but the fix is an approved Java 17 image, not more code.
* **Multi-replica analyses** are pinned to the replica that started them. The
  reaper closes what a restart orphans; it does not resume it.
