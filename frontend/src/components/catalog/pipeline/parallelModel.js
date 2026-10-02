/**
 * Parallel groups in the pipeline editor: which stages run at the same time,
 * the rules a save enforces, and the edits that keep a group well-formed.
 *
 * A group is a run of CONSECUTIVE stages sharing one `parallelGroup` name. The
 * rules mirror backend/api/services/ci/parallel_groups.py — that file is the
 * validator; these say the same thing before Save does:
 *
 *  - consecutive: one name, one run, never split by another stage;
 *  - command, scan and container image stages only (a checkout's tree is what
 *    everything after it needs; deploy/approval/store upload run on the
 *    KubeSight server, after the runner);
 *  - two to eight members, at least two of them turned on;
 *  - one fail-fast switch for the whole group.
 *
 * Pure functions only, so the vitest suite locks the same rules the UI shows.
 */

export const PARALLEL_STAGE_TYPES = new Set(["command", "scan", "container_image"]);
export const MIN_GROUP_SIZE = 2;
export const MAX_GROUP_SIZE = 8;
export const MAX_GROUP_NAME = 64;

const KIND_LABELS = {
  checkout: "Checkout",
  deploy: "Deploy",
  approval: "Approval",
  store_upload: "App store upload",
  publish_artifact: "Publish artifact",
};

/** The name as compared: whitespace collapsed, case ignored. */
export const groupKey = (value) =>
  String(value ?? "")
    .replace(/\s+/g, " ")
    .trim()
    .toLowerCase();

const tidy = (value) => String(value ?? "").replace(/\s+/g, " ").trim();

/**
 * Every run of consecutive stages with the same non-empty group name — runs of
 * one included, because the editor has to show (and explain) those too.
 * `[{ key, name, start, end, indices }]`.
 */
export function groupRuns(stages) {
  const runs = [];
  let index = 0;
  while (index < stages.length) {
    const key = groupKey(stages[index]?.parallelGroup);
    if (!key) {
      index += 1;
      continue;
    }
    let end = index;
    while (end + 1 < stages.length && groupKey(stages[end + 1]?.parallelGroup) === key) end += 1;
    const indices = [];
    for (let position = index; position <= end; position += 1) indices.push(position);
    runs.push({ key, name: tidy(stages[index].parallelGroup), start: index, end, indices });
    index = end + 1;
  }
  return runs;
}

/** The run holding `index`, or null for a stage that runs on its own. */
export function groupAt(stages, index) {
  return groupRuns(stages).find((run) => run.indices.includes(index)) || null;
}

/** Runs of two or more: what a build actually runs side by side. */
export const parallelGroups = (stages) => groupRuns(stages).filter((run) => run.indices.length >= MIN_GROUP_SIZE);

/** Whether the group the stage at `index` is in stops at its first failure. */
export const groupFailFast = (stages, run) =>
  Boolean(run && run.indices.some((position) => stages[position]?.parallelFailFast));

function kindReason(stageType, { previous = false } = {}) {
  const label = KIND_LABELS[stageType] || stageType;
  if (stageType === "checkout") {
    return previous
      ? "The stage before is the checkout, and every stage after it needs the source it clones — nothing can run alongside it."
      : "A checkout cannot run in parallel: every stage after it needs the source it clones.";
  }
  if (["deploy", "approval", "store_upload"].includes(stageType)) {
    return previous
      ? `The stage before is a ${label} stage, which runs on the KubeSight server after the build, one at a time.`
      : `${label} stages run on the KubeSight server after the build, one at a time — they cannot join a group.`;
  }
  return previous
    ? `The stage before is a ${label} stage, which cannot run in parallel.`
    : `${label} stages cannot run in parallel.`;
}

/** Can the stage at `index` run alongside the one before it? `{ ok, reason }`. */
export function canJoinPrevious(stages, index) {
  const stage = stages[index];
  if (!stage) return { ok: false, reason: "" };
  if (index === 0) return { ok: false, reason: "The first stage has nothing before it to run alongside." };
  if (!PARALLEL_STAGE_TYPES.has(stage.stageType || "command")) {
    return { ok: false, reason: kindReason(stage.stageType) };
  }
  const previous = stages[index - 1];
  if (!PARALLEL_STAGE_TYPES.has(previous.stageType || "command")) {
    return { ok: false, reason: kindReason(previous.stageType, { previous: true }) };
  }
  const run = groupAt(stages, index - 1);
  const own = groupAt(stages, index);
  const full = (candidate) => candidate && candidate.indices.length >= MAX_GROUP_SIZE;
  if (run && run.key !== own?.key ? full(run) : !run && own && full(own)) {
    return { ok: false, reason: `That group already runs ${MAX_GROUP_SIZE} stages, the most a group can.` };
  }
  return { ok: true, reason: "" };
}

/** Whether the stage at `index` runs alongside the stage before it. */
export const joinedToPrevious = (stages, index) =>
  index > 0 &&
  Boolean(groupKey(stages[index]?.parallelGroup)) &&
  groupKey(stages[index]?.parallelGroup) === groupKey(stages[index - 1]?.parallelGroup);

/** A group name no other run in the pipeline uses. */
export function uniqueGroupName(stages, base = "Parallel") {
  const taken = new Set(groupRuns(stages).map((run) => run.key));
  for (let number = 1; ; number += 1) {
    const candidate = `${base} ${number}`;
    if (!taken.has(groupKey(candidate))) return candidate;
  }
}

const withGroup = (stage, name, failFast) => ({
  ...stage,
  parallelGroup: name || null,
  parallelFailFast: name ? Boolean(failFast) : false,
});

/**
 * Tidy the groups after any structural edit: a run of one is just a stage
 * again, and a name reused by a second, separate run is renamed — a save
 * would refuse it as "split", and the second run is a different group anyway.
 * Returns the same array when nothing changed.
 */
export function normalizeGroups(stages) {
  let next = stages;
  const edit = (position, stage) => {
    if (next === stages) next = [...stages];
    next[position] = stage;
  };
  const seen = new Set();
  for (const run of groupRuns(stages)) {
    if (run.indices.length < MIN_GROUP_SIZE) {
      for (const position of run.indices) edit(position, withGroup(stages[position], null, false));
      continue;
    }
    if (seen.has(run.key)) {
      const name = uniqueGroupName(next);
      const failFast = groupFailFast(stages, run);
      for (const position of run.indices) edit(position, withGroup(stages[position], name, failFast));
      seen.add(groupKey(name));
      continue;
    }
    seen.add(run.key);
  }
  return next;
}

/** "Run in parallel with the previous stage": join its group, or start one. */
export function joinPrevious(stages, index) {
  if (!canJoinPrevious(stages, index).ok) return stages;
  const previousRun = groupAt(stages, index - 1);
  const ownRun = groupAt(stages, index);
  const next = [...stages];
  if (previousRun) {
    next[index] = withGroup(stages[index], previousRun.name, groupFailFast(stages, previousRun));
  } else if (ownRun && ownRun.indices.length >= MIN_GROUP_SIZE) {
    // The first stage of a group reaching back: the previous stage joins it.
    if (ownRun.indices.length >= MAX_GROUP_SIZE) return stages;
    next[index - 1] = withGroup(stages[index - 1], ownRun.name, groupFailFast(stages, ownRun));
  } else {
    const name = uniqueGroupName(stages);
    next[index - 1] = withGroup(stages[index - 1], name, false);
    next[index] = withGroup(stages[index], name, false);
  }
  return normalizeGroups(next);
}

/**
 * Take the stage at `index` out of its group, so it runs on its own again.
 * Leaving from the middle splits the group: the stages after it keep running
 * together as a group of their own.
 */
export function leaveGroup(stages, index) {
  const run = groupAt(stages, index);
  if (!run) return stages;
  const next = [...stages];
  next[index] = withGroup(stages[index], null, false);
  const after = run.indices.filter((position) => position > index);
  if (after.length >= MIN_GROUP_SIZE && index > run.start) {
    const name = uniqueGroupName(next);
    for (const position of after) next[position] = withGroup(stages[position], name, groupFailFast(stages, run));
  }
  return normalizeGroups(next);
}

/** Rename the group the stage at `index` is in — every member at once. */
export function renameGroup(stages, index, name) {
  const run = groupAt(stages, index);
  if (!run) return stages;
  const next = [...stages];
  for (const position of run.indices) next[position] = { ...stages[position], parallelGroup: name };
  return next;
}

/** Turn fail-fast on or off for the whole group. */
export function setGroupFailFast(stages, index, value) {
  const run = groupAt(stages, index);
  if (!run) return stages;
  const next = [...stages];
  for (const position of run.indices) next[position] = { ...stages[position], parallelFailFast: Boolean(value) };
  return next;
}

/**
 * After a stage lands at `index` (inserted, moved, or restored): dropped
 * between two members of one group it joins that group — when its kind may —
 * and carried away from its own group it leaves it. Then the usual tidy-up.
 */
export function regroupAround(stages, index) {
  const stage = stages[index];
  if (!stage) return normalizeGroups(stages);
  const before = stages[index - 1];
  const after = stages[index + 1];
  const own = groupKey(stage.parallelGroup);
  const surrounding = groupKey(before?.parallelGroup);
  let next = stages;
  if (surrounding && surrounding === groupKey(after?.parallelGroup) && surrounding !== own) {
    next = [...stages];
    if (PARALLEL_STAGE_TYPES.has(stage.stageType || "command")) {
      const run = groupAt(stages, index - 1);
      next[index] = withGroup(stage, run.name, groupFailFast(stages, run));
    } else {
      next[index] = withGroup(stage, null, false);
    }
  } else if (own && own !== surrounding && own !== groupKey(after?.parallelGroup)) {
    next = [...stages];
    next[index] = withGroup(stage, null, false);
  }
  return normalizeGroups(next);
}

/** What a save would refuse about this stage's group. Mirrors validate(). */
export function groupProblems(stage, index, stages) {
  const key = groupKey(stage?.parallelGroup);
  if (!key) return [];
  const problems = [];
  const runs = groupRuns(stages);
  const run = runs.find((item) => item.indices.includes(index));
  const name = tidy(stage.parallelGroup);
  if (name.length > MAX_GROUP_NAME) {
    problems.push({ field: "parallel", message: `Group names are at most ${MAX_GROUP_NAME} characters.` });
  }
  if (!PARALLEL_STAGE_TYPES.has(stage.stageType || "command")) {
    problems.push({ field: "parallel", message: kindReason(stage.stageType) });
  }
  const earlier = runs.find((item) => item.key === key && item.end < run.start);
  if (earlier) {
    problems.push({
      field: "parallel",
      message: `The group “${name}” is split: “${stages[run.start - 1]?.name || "another stage"}” sits between its stages. Move them together, or rename this part.`,
    });
  }
  if (run.indices.length > MAX_GROUP_SIZE) {
    problems.push({
      field: "parallel",
      message: `“${name}” has ${run.indices.length} stages; a group runs at most ${MAX_GROUP_SIZE} at once.`,
    });
  }
  const enabled = run.indices.filter((position) => stages[position]?.enabled !== false).length;
  if (enabled < MIN_GROUP_SIZE) {
    problems.push({
      field: "parallel",
      message:
        run.indices.length < MIN_GROUP_SIZE
          ? `“${name}” has only this stage — a group needs at least two. Run it in parallel with a neighbour, or take it out of the group.`
          : `“${name}” needs at least two stages that are turned on.`,
    });
  }
  return problems;
}

/** Members of the stage's group, by index, for the sheet to list. */
export function groupMembers(stages, index) {
  const run = groupAt(stages, index);
  return run ? run.indices.map((position) => ({ index: position, stage: stages[position] })) : [];
}

/**
 * Group the items of a BUILD (or any list with `parallelGroup` set only on
 * real group members) into steps: `[{ group: name|null, failFast, items }]`.
 * Consecutive items with one name form a step; everything else is a step of
 * its own. Used by the build drawer and the pipeline strip.
 */
export function buildSteps(items) {
  const steps = [];
  for (const item of items || []) {
    const key = groupKey(item?.parallelGroup);
    const last = steps[steps.length - 1];
    if (key && last && last.key === key) {
      last.items.push(item);
      last.failFast = last.failFast || Boolean(item.parallelFailFast);
    } else {
      steps.push({
        key,
        group: key ? tidy(item.parallelGroup) : null,
        failFast: Boolean(key && item.parallelFailFast),
        items: [item],
      });
    }
  }
  // A "group" of one in a build is a stage that ran on its own.
  return steps.map((step) =>
    step.items.length < MIN_GROUP_SIZE ? { ...step, key: "", group: null, failFast: false } : step
  );
}

/** One word for a step made of several statuses, worst first. */
export function stepStatus(items) {
  const statuses = (items || []).map((item) => item.status);
  for (const status of ["running", "failed", "timeout", "cancelled", "pending", "queued"]) {
    if (statuses.includes(status)) return status;
  }
  if (statuses.length && statuses.every((status) => status === "skipped")) return "skipped";
  return statuses.includes("success") ? "success" : statuses[0] || "pending";
}
