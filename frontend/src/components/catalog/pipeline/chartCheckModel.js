/**
 * The Chart check stage (backend: services/ci/chart_check_stage.py). Its
 * settings are three keys of the stage's environment, so a stage needs no
 * field of its own: the chart folder, the values file and the rule set.
 */
import { repositoryChartProblems } from "./deployModel.js";

export const CHART_CHECK_ENV = {
  chart: "KUBESIGHT_CHART_PATH",
  values: "KUBESIGHT_VALUES_PATH",
  policy: "KUBESIGHT_CHART_POLICY",
};

export const blankChartCheckEnv = () => ({ [CHART_CHECK_ENV.chart]: "", [CHART_CHECK_ENV.policy]: "ai-cluster" });

/** The stage's settings, read from its env. */
export const chartCheckSettings = (stage) => {
  const env = stage?.env || {};
  return {
    chartPath: env[CHART_CHECK_ENV.chart] || "",
    valuesPath: env[CHART_CHECK_ENV.values] || "",
    policy: env[CHART_CHECK_ENV.policy] || "",
  };
};

/** The env after changing one setting; an empty one is left out. */
export function withChartCheckSetting(env, key, value) {
  // Kept as typed ("deploy/" on the way to "deploy/values.yaml"); the server
  // tidies the path when the pipeline is saved.
  const next = { ...(env || {}) };
  const name = CHART_CHECK_ENV[key];
  const text = String(value || "");
  if (text.trim() || key === "chart") next[name] = text;
  else delete next[name];
  return next;
}

export function chartCheckSummary(stage) {
  const { chartPath, policy } = chartCheckSettings(stage);
  if (!chartPath) return "No chart folder yet";
  return `Renders ${chartPath}${policy ? " and checks the AI cluster rules" : ""}`;
}

/** What a save would refuse (pipelines._chart_check_env). */
export function chartCheckProblems(stage) {
  const { chartPath, valuesPath, policy } = chartCheckSettings(stage);
  const problems = repositoryChartProblems({ chartPath, valuesPath }).map((problem) => ({
    ...problem,
    field: "chartCheck",
  }));
  if (policy && policy !== "ai-cluster") {
    problems.push({ field: "chartCheck", message: `“${policy}” is not a rule set KubeSight knows.` });
  }
  return problems;
}
