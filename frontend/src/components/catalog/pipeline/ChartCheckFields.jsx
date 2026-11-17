import { Field } from "./controls.jsx";
import { chartCheckSettings, withChartCheckSetting } from "./chartCheckModel.js";

/**
 * The Chart check stage: which chart folder and values file of this
 * repository to render, and whether to check the AI cluster rules. KubeSight
 * runs it itself, at the commit the build (or the pull request) is for.
 */
export default function ChartCheckFields({ ids, stage, editable, onChange, error }) {
  const settings = chartCheckSettings(stage);
  const set = (key, value) => onChange({ env: withChartCheckSetting(stage.env, key, value) });
  return (
    <>
      <div className="pl-grid pl-deploy-target">
        <Field label="Chart folder" htmlFor={`${ids}-chart-path`} hint="In this service's repository, at the commit being built." error={error}>
          <input
            id={`${ids}-chart-path`}
            className="is-mono"
            value={settings.chartPath}
            placeholder="charts/my-app"
            disabled={!editable}
            spellCheck={false}
            onChange={(event) => set("chart", event.target.value)}
          />
        </Field>
        <Field
          label="Values file"
          htmlFor={`${ids}-values-path`}
          optional
          hint="The environment's values, from the same commit, over the chart's defaults."
        >
          <input
            id={`${ids}-values-path`}
            className="is-mono"
            value={settings.valuesPath}
            placeholder="deploy/values-uat.yaml"
            disabled={!editable}
            spellCheck={false}
            onChange={(event) => set("values", event.target.value)}
          />
        </Field>
      </div>
      <label className="pl-check">
        <input
          type="checkbox"
          checked={settings.policy === "ai-cluster"}
          disabled={!editable}
          onChange={(event) => set("policy", event.target.checked ? "ai-cluster" : "")}
        />
        <span>
          <strong>Check the AI cluster rules</strong>
          <small>
            The same rules as the platform team&apos;s areeba-check. A broken rule fails the stage (and blocks a pull
            request&apos;s merge); advice goes to the log. Off, the stage only checks that the chart renders.
          </small>
        </span>
      </label>
    </>
  );
}
