import { useState } from "react";
import { TOOLS } from "../QualityGateFields.jsx";
import { CommandEditor, Segmented, Switch } from "../pipeline/controls.jsx";
import { PlIcon } from "../pipeline/icons.jsx";
import { shortImage } from "../pipeline/stageModel.js";
import {
  CATEGORY_LABEL,
  METRIC_MARKER,
  scriptKeepsMetric,
  TOOL_CATEGORY,
  toolMonogram,
} from "./mergeCheckModel.js";

const NEWLINE = "\n";

/** What a check's limit is, in words, from the gate actually in force. */
function limitText(gate, capKey) {
  const cap = gate?.[capKey];
  if (cap === null || cap === undefined) return "Counts toward the total";
  return cap === 0 ? "Any finding blocks" : `Blocks above ${cap}`;
}

/**
 * Which checks run on a pull request, and what each one runs.
 *
 * The checks that will run are listed first and in full; the rest wait behind
 * one row that says how many there are and — in automatic mode — why each is
 * off. Eleven cards of equal weight, most of them greyed out, was the old
 * answer to "what runs on my pull request?".
 */
export default function ChecksView({
  config,
  form,
  active,
  gate,
  canEdit,
  saving,
  drafts,
  onDraft,
  onSaveScript,
  onResetScript,
  onMode,
  onToggleTool,
}) {
  const [openScript, setOpenScript] = useState("");
  const [showOthers, setShowOthers] = useState(false);
  const automatic = form.toolsMode !== "custom";
  const scripts = Object.fromEntries((config.checkScripts || []).map((item) => [item.tool, item]));
  const inUse = TOOLS.filter((tool) => active.includes(tool.key));
  const others = TOOLS.filter((tool) => !active.includes(tool.key));

  const row = (tool, isActive) => {
    const script = scripts[tool.key];
    const reason = config.toolReasons?.[tool.key];
    const open = openScript === tool.key;
    const savedText = (script?.commands || []).join(NEWLINE);
    const draft = drafts[tool.key] !== undefined ? drafts[tool.key] : savedText;
    const edited = Boolean(script) && draft !== savedText;
    const keepsMetric = scriptKeepsMetric(draft, tool.key);
    const category = TOOL_CATEGORY[tool.key];
    return (
      <li key={tool.key} className={`mc-tool cat-${category}${isActive ? "" : " is-off"}${open ? " is-open" : ""}`}>
        <div className="mc-tool-row">
          <span className="mc-tool-badge" aria-hidden="true">
            {toolMonogram(tool.key)}
          </span>
          <div className="mc-tool-copy">
            <strong>
              {tool.label}
              <span className="pl-tag">{CATEGORY_LABEL[category]}</span>
              {script?.customized && <span className="pl-tag is-accent">Script edited</span>}
            </strong>
            <p>{automatic && reason ? reason : tool.hint}</p>
            {isActive && script?.image && (
              <small>
                Runs in <code>{shortImage(script.image)}</code>
              </small>
            )}
          </div>
          {isActive && <span className="mc-tool-limit">{limitText(gate, tool.capKey)}</span>}
          <div className="mc-tool-actions">
            {script && (
              <button
                type="button"
                className="btn-ghost mc-script-toggle"
                aria-expanded={open}
                onClick={() => setOpenScript(open ? "" : tool.key)}
              >
                <PlIcon name="terminal" />
                {open ? "Hide script" : canEdit ? "Script" : "View script"}
              </button>
            )}
            {!automatic && (
              <Switch
                checked={active.includes(tool.key)}
                disabled={!canEdit}
                label={active.includes(tool.key) ? "On" : "Off"}
                onChange={() => onToggleTool(tool.key)}
              />
            )}
          </div>
        </div>

        {open && script && (
          <div className="mc-script">
            <p className="pl-field-hint">
              Runs in <code>{script.image || "the runner's own image"}</code> from the repository
              root, under <code>set -e</code>. The generated script is the default; an edit
              replaces it and survives changes to the checks and severity floors.
            </p>
            <CommandEditor
              lines={draft.split(NEWLINE)}
              disabled={!canEdit}
              onChange={(lines) => onDraft(tool.key, lines.join(NEWLINE))}
            />
            {!keepsMetric && (
              <p className="pl-field-error" role="alert">
                <PlIcon name="alert" /> This script no longer prints the <code>{METRIC_MARKER} tool={tool.key}</code>{" "}
                line. Without it the gate reads the check as “not run” — never as clean — and
                blocks the merge.
              </p>
            )}
            {canEdit && (
              <div className="mc-script-actions">
                {edited && <span className="mc-unsaved">Unsaved script</span>}
                <button
                  type="button"
                  className="btn-outline btn-compact"
                  disabled={saving || !script.customized}
                  title={script.customized ? "Put the generated script back" : "This is already the generated script"}
                  onClick={() => onResetScript(tool.key)}
                >
                  <PlIcon name="reset" /> Reset to generated
                </button>
                <button
                  type="button"
                  className="primary btn-compact"
                  disabled={saving || !edited}
                  onClick={() => onSaveScript(tool.key, draft)}
                >
                  <PlIcon name="check" /> {saving ? "Saving…" : "Save script"}
                </button>
              </div>
            )}
          </div>
        )}
      </li>
    );
  };

  return (
    <div className="mc-view">
      <header className="pl-view-head">
        <div>
          <span className="pl-kicker">Run on every pull request</span>
          <h3>
            {inUse.length} {inUse.length === 1 ? "check" : "checks"}
          </h3>
          <p>
            Each one is a stage of a pipeline KubeSight generates, and prints one line the gate
            reads. A check that cannot run counts as “not checked”, never as clean.
          </p>
        </div>
        <Segmented
          label="How the checks are chosen"
          value={automatic ? "auto" : "custom"}
          disabled={!canEdit}
          options={[
            { value: "auto", label: `Automatic for ${config.applicationTypeLabel}` },
            { value: "custom", label: "Choose myself" },
          ]}
          onChange={onMode}
        />
      </header>

      {automatic && (
        <p className="pl-note">
          <PlIcon name="sparkle" />
          <span>
            Picked for the service's application type and re-picked before every pull request, so
            changing the type — or adding SonarQube secrets — takes effect without coming back here.
          </span>
        </p>
      )}

      {inUse.length === 0 ? (
        <div className="pl-empty">
          <span className="pl-empty-glyph" aria-hidden="true">
            <PlIcon name="shield" />
          </span>
          <strong>No checks selected</strong>
          <p>A pull request would be passed without anything being looked at. Turn on at least one check below.</p>
        </div>
      ) : (
        <ul className="mc-tools">{inUse.map((tool) => row(tool, true))}</ul>
      )}

      {others.length > 0 && (
        <div className="mc-others">
          {automatic ? (
            <button
              type="button"
              className={`btn-ghost mc-others-toggle${showOthers ? " is-open" : ""}`}
              aria-expanded={showOthers}
              onClick={() => setShowOthers((value) => !value)}
            >
              <PlIcon name="chevron" className="pl-setting-chevron" />
              {others.length} more {others.length === 1 ? "check isn't" : "checks aren't"} used for{" "}
              {config.applicationTypeLabel} — see why
            </button>
          ) : (
            <h4 className="pl-block-title">Available, not switched on</h4>
          )}
          {(showOthers || !automatic) && <ul className="mc-tools is-others">{others.map((tool) => row(tool, false))}</ul>}
        </div>
      )}
    </div>
  );
}
