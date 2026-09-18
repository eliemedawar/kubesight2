---
name: kubesight
description: Answer questions about a KubeSight installation and fix its pipelines — services, builds, why a build failed, why one is stuck queued, what a service still needs before it can build; read a service's source code at any revision; and edit the pipeline a service builds with. Use whenever someone asks about KubeSight CI, a service in the catalog, a build number, a pipeline stage, a runner, a build artifact, or wants a build command, image or stage changed. Requires the KubeSight MCP server to be connected.
---

# Working with KubeSight

KubeSight is a Kubernetes control plane. This skill covers its **CI half**: a
catalog of services, each with a repository, a pipeline of ordered stages, and a
history of builds that produced artifacts.

## What you can and cannot change

Most `kubesight_*` tools read. Four of them write, and they all write the same
one thing — **a service's pipeline**:

| | |
|---|---|
| **You can read** | the catalog, pipelines, builds, stage logs, runners, artifacts, and **a service's source code** at any branch, tag or commit |
| **You can change** | the stages of a service's pipeline: commands, image, labels, env, timeouts, order, whether a stage is enabled |
| **You cannot** | start or cancel a build, create or read a secret, register or delete a service, or touch a cluster |

So: when a pipeline is wrong, fix it. When somebody wants it *run*, tell them —
you cannot press that button, and saying you did would be false.

Editing needs `ci_pipelines:edit` on the token in use. If you get a permission
error, say which permission is missing rather than trying another tool.

## The vocabulary, because the words are load-bearing

| Term | What it actually is |
|---|---|
| **Service** | One buildable application. Has a slug, which is also its image name. |
| **Pipeline** | An ordered list of stages. **There is no dependency graph** — order is the only relationship. |
| **Stage** | One step. `checkout` (KubeSight clones; runs no commands), `command` (a shell script), `container_image` (BuildKit builds the Dockerfile; runs no commands). |
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

Always open with `kubesight_overview` unless the question already names a
service. It costs one call and tells you how many services exist, which are
failing, which cannot build yet, and — critically — whether **any runner is
online**. A fleet with no online runner explains every queued build at once, and
it is the least obvious thing to go looking for.

Then narrow:

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

## Changing a pipeline

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
kubesight_pipeline_stage_remove {service: "payment", stage: "Legacy Deploy"}
kubesight_pipeline_save         {service: "payment", stages: [...]}   ← replaces everything
```

Identify a stage by **name or 1-based position**. A list or object in `changes`
**replaces** the current value — to add one command, send the whole command list.

Before you write:

- **Say what you are about to change, and to which service**, then do it. A
  pipeline edit changes what every future build of that service runs.
- **Images come from `kubesight_build_environments`.** Those are the approved
  ones; an arbitrary image is how you get a build that fails on the runner.
- **`secretRefs` may only name secrets the service already has** (see
  `expectedSecrets` in `kubesight_service_get`). You cannot create one — ask a
  person to add it.
- **`kubesight_pipeline_save` discards every existing stage.** Use it to rewrite
  a pipeline wholesale, never to change one thing.

After you write:

- Builds already running or finished are **unaffected** — each runs a snapshot of
  the pipeline as it was when it started. Only the next build sees your change.
- If the service had no pipeline of its own, your edit **materialises the
  generated default**: KubeSight's suggestion becomes that service's own
  pipeline and stops tracking the default. The answer says
  `materialisedGeneratedDefault: true`. Mention it.
- **You cannot run the build to check.** Say what you changed and that it takes
  effect on the next build; do not imply it is verified.

If a write is refused, the message is the validator's own and names the stage
and the problem — fix what it says and try once more. One case is not yours to
fix: a secret referenced by the *saved* pipeline that has since been deleted
blocks every edit, including ones that never touched it. The message says so;
relay it and ask for the secret back.

## The three questions people actually ask

**"Why did build N fail?"**
`kubesight_build_get` first — it returns `failedStages` directly, so you do not
have to scan. Then `kubesight_build_logs` for that stage id. Quote the actual
error line. Do not theorise from the stage name.

**"Why is this build stuck queued?"**
Almost always one of three things, in this order of likelihood:
1. No runner online — check `kubesight_overview` → `onlineRunners`.
2. The stage wants a capability nothing advertises. Compare the stage's
   `runnerLabels` against each runner's `capabilities` in
   `kubesight_runners_list`. Remember it is a **superset** test: labels spread
   across two machines match neither.
3. Every compatible runner is at capacity.

**"Why can't this service build?"**
`kubesight_service_get` → `blockedReason` is the literal sentence KubeSight
would show. `readiness.checks` lists each thing that must be true and which one
is not.

## Answering well

- **Quote, don't paraphrase.** `blockedReason` and the log lines are already
  written for a person. Repeating them verbatim is more useful than summarising.
- **Say which build.** "Build 214 failed at Unit Tests" — a status with no
  number is unverifiable and goes stale between the question and the answer.
- **Do not invent causes.** If the logs do not say why, say the logs do not say
  why and name the stage. A plausible-sounding guess is worse than a gap,
  because it gets acted on.
- **Claim only what you did.** You can change pipeline stages, and you should
  say plainly when you have. Everything else — running the build, adding a
  secret, bringing a runner online — is somebody else's press of a button; tell
  them where: the Builds tab to run, Settings for secrets, the Runners dialog
  for capacity.
- **A permission error is not a dead end.** `'X' needs the 'Y' permission` means
  the token in use lacks it. Say which permission, so the person can ask for it.

## What you cannot see

Secret values, artifact file contents, and anything outside the permissions of
the token in use. The log tools are safe to read in full because values are
masked *before* they are ever stored — but that also means a masked line is
genuinely all there is, not something you can un-mask.

Source is read through the service's own stored credential, so you can only read
a repository that somebody registered as a service — there is no tool that takes
a repository URL.
