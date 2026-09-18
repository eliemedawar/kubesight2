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
kubesight_build_logs    {buildId: 214, stageId: 981}  → why
```

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

## The two hard questions

**"Why is this build stuck queued?"**
Almost always one of three things, in this order of likelihood:
1. No runner online — check `kubesight_overview` → `onlineRunners`.
2. The stage wants a capability nothing advertises. Compare the stage's
   `runnerLabels` against each runner's `capabilities` in
   `kubesight_runners_list`. Remember it is a **superset** test: labels spread
   across two machines match neither.
3. Every compatible runner is at capacity.

**"Why did the push not happen when the build succeeded?"**
Read the image stage's log. A blocked scan prints `Scan BLOCKED the push` and
the stage fails with nothing in the registry — the image was built and thrown
away, which is the gate working. The findings are on the build as a
`scan-report` artifact — `kubesight_artifacts_list {service: "payment"}` lists
what a service has produced; quote the severities from it rather than guessing
which CVE was the blocker. Artifact *contents* are not readable here, only their
names, types and sizes.
