import { describe, expect, it } from "vitest";

import {
  PRESETS,
  blankSchedule,
  conditionSentence,
  conditionsFor,
  effectiveValues,
  formProblems,
  formatRun,
  formatUntil,
  outcomeOf,
  pipelineFor,
  presetFor,
  railSummary,
  timeZoneOptions,
  toForm,
  toPayload,
} from "./scheduleModel.js";

const PARAMS = [
  { name: "NIGHTLY_SCAN", type: "boolean", default: "false" },
  { name: "TARGET", type: "choice", choices: ["uat", "prod"], default: "uat" },
  { name: "NOTE", type: "text", default: "" },
];

describe("presets", () => {
  it("recognises its own expressions and nothing else", () => {
    expect(presetFor("0 2 * * *")).toBe("0 2 * * *");
    expect(presetFor("  0   8 * * 1-5 ")).toBe("0 8 * * 1-5");
    expect(presetFor("0 3 * * 0")).toBe("0 3 * * 0");
    // Same meaning, different text: the server decides what it means; the
    // form only matches what it wrote.
    expect(presetFor("0 3 * * SUN")).toBe("custom");
    expect(presetFor("")).toBe("custom");
  });

  it("offers the four quick choices and Custom", () => {
    expect(PRESETS.map((preset) => preset.label)).toEqual([
      "Nightly 02:00",
      "Weekdays 08:00",
      "Every hour",
      "Weekly",
      "Custom",
    ]);
  });
});

describe("form <-> payload", () => {
  it("starts a new schedule as a nightly in the given zone", () => {
    const form = blankSchedule({ timezone: "Asia/Beirut" });
    expect(form).toMatchObject({ name: "Nightly build", cron: "0 2 * * *", timezone: "Asia/Beirut", enabled: true, skipIfRunning: true });
  });

  it("round-trips an API schedule", () => {
    const form = toForm({
      id: 4,
      name: "Weekly",
      cron: "0 3 * * 0",
      timezone: "Europe/Berlin",
      pipelineId: 12,
      refType: "tag",
      branch: "v1.2.0",
      variables: { TARGET: "prod" },
      enabled: false,
      skipIfRunning: false,
    });
    expect(form.pipelineId).toBe("12");
    expect(toPayload(form, PARAMS)).toEqual({
      name: "Weekly",
      cron: "0 3 * * 0",
      timezone: "Europe/Berlin",
      pipelineId: 12,
      refType: "tag",
      branch: "v1.2.0",
      variables: { TARGET: "prod" },
      enabled: false,
      skipIfRunning: false,
    });
  });

  it("only sends inputs the chosen pipeline declares", () => {
    const form = { ...blankSchedule({ timezone: "UTC" }), variables: { TARGET: "prod", GONE: "x" } };
    expect(toPayload(form, PARAMS).variables).toEqual({ TARGET: "prod" });
    expect(toPayload({ ...form, pipelineId: "" }, PARAMS).pipelineId).toBeNull();
  });

  it("falls back to each input's default, booleans as the strings a stage sees", () => {
    expect(effectiveValues(PARAMS, { NIGHTLY_SCAN: "true" })).toEqual({
      NIGHTLY_SCAN: "true",
      TARGET: "uat",
      NOTE: "",
    });
    expect(effectiveValues([{ name: "B", type: "boolean", default: true }], {})).toEqual({ B: "true" });
  });

  it("says what is missing before the round-trip", () => {
    expect(formProblems({ name: " ", cron: "", refType: "tag", branch: "" })).toEqual({
      name: expect.any(String),
      cron: expect.any(String),
      branch: expect.stringContaining("tag"),
    });
    expect(formProblems(blankSchedule({ timezone: "UTC" }))).toEqual({});
  });
});

describe("pipelines and conditions", () => {
  const pipelines = [
    { id: 1, name: "build", isDefault: true, conditions: [{ stage: "Dependency-Check", variable: "NIGHTLY_SCAN", operator: "equals", value: "true" }] },
    { id: 2, name: "release", isDefault: false, conditions: [] },
  ];

  it("resolves '' to the default pipeline", () => {
    expect(pipelineFor(pipelines, "").name).toBe("build");
    expect(pipelineFor(pipelines, "2").name).toBe("release");
    expect(pipelineFor(pipelines, "9")).toBeNull();
  });

  it("names the stages an input switches", () => {
    const [condition] = conditionsFor(pipelines[0], "NIGHTLY_SCAN");
    expect(conditionSentence(condition)).toBe("Dependency-Check runs only when NIGHTLY_SCAN is true");
    expect(conditionSentence({ ...condition, operator: "not_equals" })).toBe("Dependency-Check runs unless NIGHTLY_SCAN is true");
    expect(conditionsFor(pipelines[1], "NIGHTLY_SCAN")).toEqual([]);
  });
});

describe("times", () => {
  it("writes a run on the schedule zone's wall clock", () => {
    // 23:00 UTC on 1 Jul is 02:00 the next morning in Beirut (UTC+3 in summer).
    expect(formatRun("2026-07-01T23:00:00+00:00", "Asia/Beirut")).toBe("Thu 2 Jul, 02:00");
    expect(formatRun("2026-07-01T23:00:00+00:00", "UTC")).toBe("Wed 1 Jul, 23:00");
    // A naive timestamp is UTC, as everywhere in this API.
    expect(formatRun("2026-07-01T23:00:00", "UTC")).toBe("Wed 1 Jul, 23:00");
    expect(formatRun(null, "UTC")).toBe("—");
  });

  it("says how far off the next run is", () => {
    const now = Date.parse("2026-10-01T10:00:00Z");
    expect(formatUntil("2026-10-01T10:00:30Z", now)).toBe("in under a minute");
    expect(formatUntil("2026-10-01T10:45:00Z", now)).toBe("in 45m");
    expect(formatUntil("2026-10-01T13:20:00Z", now)).toBe("in 3h 20m");
    expect(formatUntil("2026-10-01T13:00:00Z", now)).toBe("in 3h");
    expect(formatUntil("2026-10-04T10:00:00Z", now)).toBe("in 3d");
  });

  it("always lists UTC first and the zones in play", () => {
    const zones = timeZoneOptions("Asia/Beirut", "Etc/Unusual");
    expect(zones[0]).toBe("UTC");
    expect(zones).toContain("Asia/Beirut");
    expect(zones).toContain("Etc/Unusual");
  });
});

describe("outcomes and the rail", () => {
  it("reads the last run", () => {
    expect(outcomeOf({}).label).toBe("Not run yet");
    expect(outcomeOf({ lastOutcome: "failed", lastError: "pipeline gone" })).toMatchObject({ tone: "danger", detail: "pipeline gone" });
    expect(outcomeOf({ lastOutcome: "skipped", lastError: "still running" }).tone).toBe("warn");
    expect(outcomeOf({ lastOutcome: "triggered", lastBuild: { number: 7, status: "success" } })).toMatchObject({
      tone: "ok",
      label: "Build #7 success",
    });
    expect(outcomeOf({ lastOutcome: "triggered", lastBuild: { number: 8, status: "running" } }).tone).toBe("info");
  });

  it("summarises the section for the rail", () => {
    expect(railSummary(null)).toEqual({ text: "", tone: "" });
    expect(railSummary([])).toEqual({ text: "None", tone: "" });
    expect(railSummary([{ enabled: true }, { enabled: false }])).toEqual({ text: "1 active", tone: "" });
    expect(railSummary([{ enabled: false }]).text).toBe("All paused");
    expect(railSummary([{ enabled: true, lastOutcome: "failed" }]).tone).toBe("warn");
  });
});
