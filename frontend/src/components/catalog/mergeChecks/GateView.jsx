import QualityGateFields, { TOOLS } from "../QualityGateFields.jsx";
import { Segmented } from "../pipeline/controls.jsx";
import { PlIcon } from "../pipeline/icons.jsx";
import { toolMonogram, TOOL_CATEGORY } from "./mergeCheckModel.js";

const isSet = (value) => value !== null && value !== undefined;

/** What one check counts as a problem, from the gate in force. */
function countsText(tool, gate) {
  if (tool.counts.type === "text") return tool.counts.text;
  if (tool.counts.type === "toggle") {
    return gate?.[tool.counts.key] ? "Errors and warnings" : "Errors only";
  }
  return `${gate?.[tool.counts.key] || tool.counts.fallback} severity and above`;
}

/**
 * How many problems a pull request may have, and who decides.
 *
 * Inheriting shows the installation policy as sentences — the number that
 * passes, the number that blocks — rather than a disabled form, because a
 * greyed-out input reads as "not set" when it is set somewhere else.
 */
export default function GateView({ config, form, active, canEdit, onMode, onField }) {
  const gate = config.effectiveGate || {};
  const inherit = form.gateMode !== "override";
  const total = inherit ? gate.maxTotalProblems : isSet(form.maxTotalProblems) ? form.maxTotalProblems : gate.maxTotalProblems;
  const tools = TOOLS.filter((tool) => active.includes(tool.key));
  const fromPolicy = Object.values(gate.sources || {}).some((source) => source === "policy");

  return (
    <div className="mc-view">
      <header className="pl-view-head">
        <div>
          <span className="pl-kicker">When a pull request is blocked</span>
          <h3>Quality gate</h3>
          <p>
            The checks count what they find; the gate decides whether that many is too many. The
            limits in force are copied onto every verdict, so relaxing them later never rewrites why
            a past merge was blocked.
          </p>
        </div>
        <Segmented
          label="Whose limits apply"
          value={inherit ? "inherit" : "override"}
          disabled={!canEdit}
          options={[
            { value: "inherit", label: "Installation policy" },
            { value: "override", label: "This service's own" },
          ]}
          onChange={onMode}
        />
      </header>

      <div className={`mc-gate-hero${isSet(total) ? "" : " is-open"}`}>
        <span className="mc-gate-number">{isSet(total) ? total : "∞"}</span>
        <div>
          <strong>
            {isSet(total) ? (
              <>
                A pull request with {total} {Number(total) === 1 ? "problem" : "problems"} passes;{" "}
                {Number(total) + 1} blocks the merge.
              </>
            ) : (
              "No limit on the total — nothing is blocked by the count."
            )}
          </strong>
          <p>
            {inherit
              ? fromPolicy
                ? "Set by the installation policy (Settings → Merge checks)."
                : "KubeSight's default — the installation policy has not set one."
              : "This service's own limit. Leave a box empty to fall back to the policy."}
          </p>
        </div>
      </div>

      {inherit ? (
        <>
          <ul className="mc-rules">
            {tools.map((tool) => {
              const cap = gate[tool.capKey];
              return (
                <li key={tool.key} className={`cat-${TOOL_CATEGORY[tool.key]}`}>
                  <span className="mc-tool-badge" aria-hidden="true">
                    {toolMonogram(tool.key)}
                  </span>
                  <strong>{tool.label}</strong>
                  <span className="mc-rule-counts">Counts {countsText(tool, gate).toLowerCase()}</span>
                  <span className={`mc-rule-cap${isSet(cap) ? " is-set" : ""}`}>
                    {isSet(cap) ? (cap === 0 ? "Any finding blocks" : `Blocks above ${cap}`) : "Bound by the total only"}
                  </span>
                </li>
              );
            })}
            <li className="mc-rules-foot">
              <PlIcon name={gate.blockOnToolError ? "shield" : "alert"} />
              <span>
                A check that cannot run at all{" "}
                <strong>{gate.blockOnToolError ? "blocks the merge" : "is only a warning"}</strong>
                {gate.blockOnToolError ? " — a crashed scanner found nothing because it did not look." : "."}
              </span>
            </li>
          </ul>
          {canEdit && (
            <p className="pl-field-hint">
              Need different numbers for this service?{" "}
              <button type="button" className="pl-link" onClick={() => onMode("override")}>
                Set this service's own limits
              </button>
            </p>
          )}
        </>
      ) : (
        <div className="mc-gate-form">
          <QualityGateFields values={form} disabled={!canEdit} onChange={onField} inheritedFrom={gate} />
        </div>
      )}
    </div>
  );
}
