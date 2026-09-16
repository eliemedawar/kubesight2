---
name: kubesight
description: Answer questions about a KubeSight installation — services, pipelines, builds, why a build failed, why one is stuck queued, what a service still needs before it can build. Use whenever someone asks about KubeSight CI, a service in the catalog, a build number, a pipeline stage, a runner, or a build artifact. Requires the KubeSight MCP server to be connected.
---

# Answering questions about KubeSight

KubeSight is a Kubernetes control plane. This skill covers its **CI half**: a
catalog of services, each with a repository, a pipeline of ordered stages, and a
history of builds that produced artifacts.

The `kubesight_*` tools are **read-only**. Nothing here can trigger a build,
change a pipeline or reveal a secret's value — so you can explore freely, but
never claim to have changed anything.

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
- **Never claim to have fixed anything.** These tools read. If somebody wants a
  change, tell them where in the UI to make it — the Pipeline tab for stages,
  Settings for secrets, the Runners dialog for capacity.
- **A permission error is not a dead end.** `'X' needs the 'Y' permission` means
  the token in use lacks it. Say which permission, so the person can ask for it.

## What you cannot see

Secret values, artifact file contents, and anything outside the permissions of
the token in use. The log tools are safe to read in full because values are
masked *before* they are ever stored — but that also means a masked line is
genuinely all there is, not something you can un-mask.
