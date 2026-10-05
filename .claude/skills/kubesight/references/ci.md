# CI — services, pipelines, builds, runners, source

A catalog of services, each with a repository, a pipeline of ordered stages, and
a history of builds that produced artifacts.

## The vocabulary, because the words are load-bearing

| Term | What it actually is |
|---|---|
| **Service** | One buildable application. Has a slug, which is also its image name. |
| **Pipeline** | An ordered list of stages. **There is no dependency graph** — order is the only relationship. |
| **Stage** | One step. `checkout` (KubeSight clones; runs no commands), `command` (a shell script), `container_image` (BuildKit builds the Dockerfile and pushes it; runs no commands). |
| **Image scan** | A gate *inside* a `container_image` stage, not a stage of its own. Armed, BuildKit does not push at all: the image comes back as an archive, Trivy reads it, and only a pass reaches the push. |
| **Server stage** | `deploy`, `approval`, `store_upload` — run by KubeSight itself after every runner stage (they must be last). An `approval` stage holds a build *running* until its named users / `ci_builds:approve` holders approve (never the person who started it, unless the stage allows it); a build waiting there shows `awaitingApproval` and is listed by `status=awaiting_approval`. You cannot approve for anyone — it is a person's click in the build drawer. A `store_upload` stage publishes the build's AAB/IPA through Mobile Apps (signature gate included); only an administrator can save its target. |
| **Runner** | Where a stage executes. A stage runs on a runner whose capabilities are a **superset** of that stage's `runnerLabels`. |
| **Build** | One execution. Renders from a *snapshot* of the pipeline, so editing a pipeline never rewrites history. |
| **Secret** | Stored encrypted and write-only. A pipeline holds *references*; values are never readable, including by you. |

Two facts that explain most confusing answers:

- A **generated default** pipeline (`isGeneratedDefault: true`) is not saved. It
  is what KubeSight would run if nobody customised anything. Say so — "this
  service has no pipeline of its own yet" is different from "its pipeline does X".
- Stages share a **workspace** but not an **environment**. A file written by one
  stage is visible to the next; a variable is not, unless it was appended to
  `$KUBESIGHT_ENV`.

## Start wide, then narrow

Open with `kubesight_overview` unless the question already names a service. One
call tells you how many services exist, which are failing, which cannot build
yet, and — critically — whether **any runner is online**. A fleet with no online
runner explains every queued build at once, and it is the least obvious thing to
go looking for.

```
kubesight_services_list {search: "payment"}      → find it
kubesight_service_get   {service: "payment"}     → state, readiness, blockedReason
kubesight_pipeline_get  {service: "payment"}     → what it actually runs
kubesight_builds_list   {service: "payment"}     → history
kubesight_build_get     {buildId: 214}           → which stage failed
kubesight_build_failure {buildId: 214}           → WHY, in one call
kubesight_build_logs    {buildId: 214, stageId: 981}  → one stage, in full
```

For "why did it break", reach for `kubesight_build_failure` rather than the
`build_get` → `build_logs` pair. It finds the failed stage itself and returns
the tail of its log, and it takes `{service: "payment"}` instead of a build id
to mean "the last one that failed". Fall back to `kubesight_build_logs` when the
tail is not enough or the interesting stage is one that passed.

`service` accepts an id **or** a slug **or** a name. Use whichever the person
gave you rather than looking it up first.

## Reading the source

`kubesight_repo_tree`, `kubesight_repo_file` and `kubesight_repo_revisions` read
the service's repository straight from Bitbucket at any revision — no clone, no
build. Paths are relative to the service's **working directory**, so in a
monorepo `pom.xml` means *this service's* pom.

```
kubesight_repo_tree      {service: "payment", pathPrefix: "src/main"}
kubesight_repo_file      {service: "payment", path: "build.gradle"}
kubesight_repo_file      {service: "payment", path: "Dockerfile", revision: "release/4.2"}
kubesight_repo_revisions {service: "payment", kinds: ["branch"]}
```

Two habits worth keeping:

- **Tree first, then file.** Guessing a path costs a failed call; the tree costs
  one and tells you what is actually there.
- **`truncated: true` means the listing hit a ceiling.** On a truncated tree an
  absent path means *not seen*, not *not there* — never conclude a repository has
  no Dockerfile from one.

This is the honest way to answer "why does the build do X?". The pipeline says
what commands run; the repository says what they run against. A failure that
mentions a missing file, a wrong module name or a Gradle task that does not
exist is usually settled by reading the file, not by re-reading the log.

Source is read through the service's own stored credential, so you can only read
a repository somebody registered as a service — there is no tool that takes a
repository URL.

## Running a build

```
kubesight_build_run    {service: "payment"}                      ← default branch
kubesight_build_run    {service: "payment", branch: "release/4.2"}
kubesight_build_run    {service: "payment", variables: {SKIP_TESTS: "true"}}
kubesight_build_cancel {buildId: 214}
kubesight_build_retry  {buildId: 214}
```

Three things to get right, because getting them wrong is how you report
something that did not happen:

- **A build comes back queued, not finished.** Nothing here waits for it. Say
  "queued build #215" and, if they want the outcome, poll `kubesight_build_get`
  — do not claim it passed.
- **Retry re-runs the original's coordinates**, not the branch's current head.
  Same commit, same variables, same ref kind. To build the latest, use
  `kubesight_build_run`.
- **Cancelling a running build is a request.** The runner is told on the next
  tick, so the status will still read `running` for a moment. A *queued* build
  cancels immediately.

If `kubesight_build_run` refuses, the message is the catalog's own sentence
naming what is missing — a repository that is not connected, a pipeline that
does not exist, a required secret that was never added. Relay it.

## Changing a pipeline

Read [writing.md](writing.md) first if this is your first write in the
conversation. Then:

**Prefer the narrow tool.** `kubesight_pipeline_stage_update` changes only the
fields you name and keeps the rest of the stage exactly as it was — so you never
have to read a pipeline back and echo it, and cannot drop a field you did not
know about.

```
kubesight_pipeline_stage_update {service: "payment", stage: "Build JAR",
                                 changes: {commands: ["gradle clean build -x test"]}}
kubesight_pipeline_stage_add    {service: "payment", after: "Build JAR",
                                 stage: {name: "Test", stageType: "command",
                                         commands: ["gradle test"]}}
kubesight_pipeline_stage_update {service: "payment", stage: "Build Image",
                                 changes: {imageScan: {enabled: true, threshold: "critical",
                                                       onFail: "block"}}}
kubesight_pipeline_stage_remove {service: "payment", stage: "Legacy Deploy"}
kubesight_pipeline_save         {service: "payment", stages: [...]}   ← replaces everything
```

Identify a stage by **name or 1-based position**. A list or object in `changes`
**replaces** the current value — to add one command, send the whole command list.

Before you write:

- **Images come from `kubesight_build_environments`.** Those are the approved
  ones; an arbitrary image is how you get a build that fails on the runner.
- **`secretRefs` may only name secrets the service already has** (see
  `expectedSecrets` in `kubesight_service_get`). You cannot create one — ask a
  person to add it.
- **`kubesight_pipeline_save` discards every existing stage.** Use it to rewrite
  a pipeline wholesale, never to change one thing.
- **Never hardcode `/workspace` in commands.** It is the Kubernetes runner's
  directory; on an agent the build lives elsewhere on that machine and the stage
  fails with "No such file or directory". Write `$KUBESIGHT_SOURCE` for the
  checkout (`/workspace/source` on Kubernetes) and `$KUBESIGHT_WORKSPACE` for
  the workspace — both runners export them — and the cache paths as
  `$KUBESIGHT_CACHE_DIR`, `$GRADLE_USER_HOME` and friends, which are already set.
  Put them in `commands`, never in a stage's `env`: Kubernetes does not expand
  `$VAR` in an environment value, so it arrives as that literal text.
- **Commands run under `sh`, not bash** (`dash` in Debian-based images). Bash-only
  syntax — `[[ ]]`, arrays, `${VAR//a/b}`, `${VAR^^}`, `${VAR:0:3}` — stops the
  stage with `/bin/sh: N: Bad substitution`.
- **Write a Gradle/Groovy file with a quoted heredoc, never `printf`/`echo` of
  double-quoted lines.** The shell expands `${...}` inside double quotes, and a
  Groovy `${p.path}` is not a shell variable — so the stage dies with
  `Bad substitution` before Gradle starts. `\\${p.path}` is the same bug: `\\`
  is one literal backslash and the `${` is still expanded. With `<<'EOF'` (the
  quotes matter) the shell expands nothing and the Groovy is written exactly as
  typed:

  ```sh
  init="$KUBESIGHT_WORKSPACE/kubesight-repositories-init.gradle"
  cat > "$init" <<'EOF'
  allprojects { p ->
    repositories.all { r ->
      if (r instanceof MavenArtifactRepository && (r.url?.toString() ?: '').contains('jpos.org/maven')) {
        println "KubeSight: rewriting ${p.path} jpos repository to internal Maven mirror"
        r.setUrl('https://registry.areeba.com:4443/repository/maven-public/')
      }
    }
  }
  EOF
  ```

  When a file genuinely needs both a shell value and a Groovy `${...}`, keep the
  heredoc quoted and pass the shell value as an environment variable the Groovy
  reads (`System.getenv('NEXUS_USER')`). If you must stay inside double quotes,
  the Groovy one is `\${p.path}` — exactly one backslash.
- **An init script in `init.d` already exists.** KubeSight writes
  `$GRADLE_USER_HOME/init.d/kubesight-build-cache.gradle` before every stage;
  give yours another name and pass it with `-I`, never write over that one.
- **A scan is a field on the image stage, never a stage you add.** There is no
  `scan` stage to insert between "build" and "push", because build and push are
  one stage — `imageScan` is what splits them. A stage called "Scan" after the
  image stage would run *after* the push and gate nothing.

After you write:

- Builds already running or finished are **unaffected** — each runs a snapshot of
  the pipeline as it was when it started. Only the next build sees your change.
- If the service had no pipeline of its own, your edit **materialises the
  generated default**: KubeSight's suggestion becomes that service's own
  pipeline and stops tracking the default. The answer says
  `materialisedGeneratedDefault: true`. Mention it.
- Your change is not verified until a build runs it. You *can* run one now with
  `kubesight_build_run` — say that you are doing it, and remember it comes back
  queued.

If a write is refused, the message is the validator's own and names the stage
and the problem — fix what it says and try once more. One case is not yours to
fix: a secret referenced by the *saved* pipeline that has since been deleted
blocks every edit, including ones that never touched it. The message says so;
relay it and ask for the secret back.

## The Dockerfile — which of the two, and which one you can edit

A service's image recipe lives in **one of two places**, and the answer to "edit
the Dockerfile" depends entirely on which:

| Where | What builds it | Can you change it |
|---|---|---|
| **Stored in KubeSight** (`source: "inline"`) | mounted beside the build context; the repository is never touched | yes, these tools |
| **Committed in the repository** (`source: "repository"`) | the `container_image` stage reads it at the built revision | **no** — KubeSight reads repositories, it never commits |

```
kubesight_dockerfile_get  {service: "acquiring-ui"}
kubesight_dockerfile_edit {service: "acquiring-ui",
                           replacements: [{find: "openjdk11:jdk-11.0.11_9",
                                           replace: "openjdk17:jdk-17.0.9"}]}
kubesight_dockerfile_set  {service: "acquiring-ui", dockerfile: "FROM …
…"}
kubesight_dockerfile_set  {service: "acquiring-ui", useRepositoryDockerfile: true}
```

`kubesight_dockerfile_get` returns the text from wherever it lives — it reads the
repository's copy for you when KubeSight stores none — plus `builtBy`, the image
stages that would build it. **`builtBy: []` means nothing builds this file**; an
edit to it changes no image, and saying "done" would be wrong.

**Prefer `kubesight_dockerfile_edit`.** It replaces exact snippets in the stored
text and keeps every line you did not mention, so you cannot lose one by
forgetting to repeat it. Each `find` must match **exactly once** — whitespace and
indentation included — and an ambiguous or missing one saves nothing at all,
including the replacements beside it. `kubesight_dockerfile_set` replaces the
whole document; use it to write a Dockerfile from scratch, not to change a line.

Three things to say out loud:

- **Storing the first Dockerfile retires the repository's.** On a service that
  had none, `kubesight_dockerfile_set` answers
  `startedOverridingRepository: true`, and from then on every build ignores the
  repository's file — including a fix somebody pushes there next week. Say that
  when it happens; `{useRepositoryDockerfile: true}` undoes it.
- **It takes effect on the next build**, like a pipeline edit. Nothing rebuilds
  by itself, and a build already running carries the file it started with.
- **An inline Dockerfile overrides `DOCKERFILE_PATH`.** A stage pointing at
  `docker/Dockerfile.prod` is pointing at nothing once a copy is stored; BuildKit
  is handed the stored one.

Both writes answer with `linesAdded`, `linesRemoved` and a `diff`. Quote the
diff rather than describing the change from memory — it is what was saved.

## Merge checks — the gate on a pull request

Separate from building. A pull request arrives by webhook, KubeSight runs a
pipeline of checks (ESLint, Semgrep, SonarQube, OWASP Dependency-Check) against
the PR's head commit, counts what each reported, and posts a verdict to
Bitbucket as a commit **build status**.

**KubeSight does not block the merge and you must not say it does.** Bitbucket's
branch restriction "require passing builds" is what blocks it. A gate can be
perfectly configured and enforce nothing, and the two look identical from
inside KubeSight — so `kubesight_merge_checks_status` asks Bitbucket and returns
`enforcement`. Read it before telling anybody they are protected:

| `enforcement` | What to say |
|---|---|
| `enforced: true` | A failed check stops the merge. |
| `enforced: false` | Checks run and report; **anyone can still merge**. Name the uncovered branches. |
| `known: false` | You could not look. Say that, never "you are protected". |

```
kubesight_merge_checks_status  {service: "payment"}   → on? what runs? enforced?
kubesight_merge_checks_history {service: "payment", blockedOnly: true}
kubesight_merge_checks_history {service: "payment", pullRequest: "142"}
kubesight_merge_check_policy_get                      → the org-wide limits
```

Reading a verdict:

- `problems` against `limit` is the whole gate. A cap is a **maximum that
  passes**: 5 allows 5 and blocks 6.
- `byCheck` says which tool blocked it. A check with `status` other than `ok`
  found nothing **because it did not run** — that is a broken check, not clean
  code, and the fix is different. Never report it as zero problems.
- `reportedToBitbucket` is separate from the verdict. `failed` there means the
  gate decided correctly and Bitbucket was never told, which is an integration
  problem for a human, not a code problem for the author.
- Each check has a build behind it. `buildId` → `kubesight_build_failure` for
  the actual tool output.

`kubesight_merge_check_policy_set` moves the limit for **every service that
inherits it**. Say the current number and the proposed one and get an explicit
yes before calling it. There is deliberately no tool that switches a service's
checks off or re-sends a verdict: relaxing a gate to get a merge through is the
failure the gate exists to prevent, and it stays a human action in the UI.

## The hard questions

**"Why is this build stuck queued?"**
Almost always one of three things, in this order of likelihood:
1. No runner online — check `kubesight_overview` → `onlineRunners`.
2. The stage wants a capability nothing advertises. Compare the stage's
   `runnerLabels` against each runner's `capabilities` in
   `kubesight_runners_list`. Remember it is a **superset** test: labels spread
   across two machines match neither.
3. Every compatible runner is at capacity.

**"`Could not find or load main class org.gradle.wrapper.GradleWrapperMain`"**
The repository has `gradlew` and `gradle/wrapper/gradle-wrapper.properties` but
not `gradle/wrapper/gradle-wrapper.jar` — almost always a `*.jar` line in
`.gitignore` that swallowed it. Nothing in the pipeline or the cache causes this.
Confirm before saying so: `kubesight_repo_tree {service, pathPrefix: "gradle/wrapper"}`
(and the module's own `<module>/gradle/wrapper` in a monorepo).

Two fixes; say both, and recommend the first:
1. **Commit the jar** — a developer runs `gradle wrapper` once and
   `git add -f gradle/wrapper/gradle-wrapper.jar`. KubeSight cannot commit; it is
   a person's change to the repository.
2. **Build with the image's Gradle instead** — the pipeline change you can make:
   - The Gradle version is `distributionUrl` in
     `kubesight_repo_file {service, path: "gradle/wrapper/gradle-wrapper.properties"}`
     (`gradle-7.6.1-bin.zip` → `7.6.1`).
   - The Java version is `sourceCompatibility`/`toolchain` in `build.gradle`, or
     the JDK of the runtime image in the Dockerfile (`openjdk11` → `11`).
   - The image is `<registry>/gradle:<gradle>-jdk<java>`, e.g.
     `registry.areeba.com/gradle:7.6.1-jdk11` — the registry host is the one in
     `kubesight_build_environments`, never Docker Hub directly. Prefer a catalog
     entry when one matches. Old Gradle does not run on new JDKs, so not every
     pair exists: `kubesight_image_check` the exact tag **before** saving it.
   - Set that image on the stage, replace `./gradlew` with `gradle` in its
     commands and drop any `chmod +x ./gradlew`. Keep the tasks and flags.

**"Why did the push not happen when the build succeeded?"**
Read the image stage's log. A blocked scan prints `Scan BLOCKED the push` and
the stage fails with nothing in the registry — the image was built and thrown
away, which is the gate working. The findings are on the build as a
`scan-report` artifact — `kubesight_artifacts_list {service: "payment"}` lists
what a service has produced; quote the severities from it rather than guessing
which CVE was the blocker. Artifact *contents* are not readable here, only their
names, types and sizes.

## The build cache — why a build is slow, or fails on a corrupt cache

Every stage gets a persistent per-service directory on one volume
(`/kubesight-cache/<service-slug>`), and every build tool is already pointed
into it: Gradle, Maven, npm, yarn, pnpm, pip, Go, Dependency-Check's NVD
database, Semgrep, Trivy, BuildKit's layer export, and a saved `node_modules`
per lockfile. Nothing has to be added to a pipeline for it to work.

```
kubesight_ci_cache_status {}                     → on? volume Bound? warnings? last measure
kubesight_ci_cache_status {service: "payment"}   → + that service's paths, sizes, running builds
```

Read `cache.enabled`, `cache.claim.phase` and `cache.warnings` first — a warning
there is already the answer, written for a person. `cache.source` says whether
the UI switch or the `CI_CACHE_*` environment decides, so nobody hunts for the
wrong toggle.

**The stage log says what the cache did — quote it.** Every stage prints:

| Line in the stage log | What it means |
|---|---|
| `Cache: off. Every build starts cold.` | Nobody turned it on (Runners → Build cache). Not a fault. |
| `warm: gradle/caches maven …` / `cold: …` | Per tool. A tool in `cold:` on a service's second build is the one to look at. `(shared)` marks the copy every service shares. |
| `GRADLE_USER_HOME=… ^ not …` | The stage's own Environment overrides the cache path, so that tool starts cold every build. A literal `$KUBESIGHT_CACHE_DIR/...` there means Kubernetes did not expand it — fix the stage env, or `export` it inside the commands. |
| `Cache directory … is not writable; this build runs cold.` | Volume ownership. The fix is on the NFS server (`chown 65532:65532`), a person's job. |
| `uses cache slot N` | Another build of the same service was running, so this one used its own copy for Gradle, Maven, yarn, BuildKit and Trivy — cold the first time, warm after. Expected, not an error. |
| `All N cache slots … start cold` | More builds of one service at once than `CI_CACHE_SLOTS`. |
| `node_modules cache: restored …` / `saved …` / `waiting for <stage>` | The install was skipped / stored / is queued behind a parallel stage installing in the same pod. Waiting is correct: two installs at once break `node_modules`. |
| `Waiting for another Dependency-Check scan` | The shared NVD database takes one scan at a time. |

**Errors that mean a corrupt cache, not bad code.** If a build fails with one of
these and the code did not change, the cache is the suspect:

| Error in the log | Tool |
|---|---|
| `Extracting tar content of undefined failed, the file appears to be corrupt` | yarn |
| `invalid LOC header`, `zip END header not found`, `error in opening zip file` | Maven / Gradle (a truncated jar) |
| `Timeout waiting to lock … It is currently in use by another Gradle instance` | Gradle |
| `Could not compile initialization script … kubesight-build-cache.gradle` | Gradle init script read mid-write |
| `Database may be already in use` (H2) | Dependency-Check |
| `Could not set unknown property 'removeUnusedEntriesAfterDays'` | KubeSight older than its Gradle 9 fix — upgrade, not clean |

The fix is to **empty that service's cache** and run the build again. That is a
person's action — Runners → Build cache → Clean, or `k8s/ci-cache.sh clean
<slug>` — and it is refused while a build of that service is running. There is
no tool here that deletes a cache: say which service, quote the error, and
recommend the clean. Emptying it costs one cold build, nothing else.

What is already handled, so do not recommend it as a fix:

- **Parallel stages** share the cache safely: Maven has file locks on, Node
  installs take turns, Gradle and Trivy lock themselves.
- **Two builds of one service** each get their own cache slot
  (`service.usesSlots`; a service limited to one build at a time never does).
- **Different services** never share Gradle, Maven, yarn or BuildKit — only the
  content-addressed npm/pnpm/pip/Go stores, Semgrep and the NVD database, which
  are safe to share.

"The cache is on but the build is still slow" is usually one of: the build is
the service's first (everything `cold:`), the stage overrides a cache variable
(the `^ not` line), the Gradle command has no `--build-cache` (the Gradle build
cache is configured but only used when asked for), or the pipeline runs
`gradle clean`. Read the log before guessing.
